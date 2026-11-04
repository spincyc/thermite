"""Small JSON interfaces; framework authentication owns credential handling."""

import json
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import logout
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.db import close_old_connections, connection, connections, transaction
from django.utils import timezone

from oh_features.remote_system import ADMIN_ASSETS
from oh_room_policy import STALE_AFTER, active, empty, manual_hold, with_manual_hold
from oh_routes import ASSET_ROUTES

from .auth import api, failure, login_user, not_allowed, problem_response, require_admin
from .models import PersonalToken, ReconcileState, RoomPolicy, User
from .policy_store import TOKEN_RETENTION, next_revision
from .runtime import get_adapter, policy_problem
from .services import Problem, admin_actor, audit, issue_token, is_admin, permissions, save_user


def body(request):
    if request.content_type != "application/json":
        raise Problem("json_required", 415)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeError, RecursionError):
        raise Problem("invalid_json") from None
    if not isinstance(data, dict):
        raise Problem("invalid_json")
    return data


def user_data(user):
    return {"id": user.pk, "username": user.username, "active": user.is_active, "admin": is_admin(user)}


def me_data(user, state=None):
    """The current principal: a Guest (id 0), a Member or an Admin."""
    from .security import get_state
    enabled = (state or get_state()).enabled
    return {**user_data(user), "is_guest": bool(getattr(user, "is_guest", False)), "auth_enabled": enabled,
            "can_edit": enabled and is_admin(user), "site": settings.SITE_ID, "permissions": sorted(permissions(user))}


def security(request):
    from .security import change_security, status
    try:
        if request.method == "GET":
            data = status()
        elif request.method == "POST":
            data = change_security(request, body(request))
            logout(request)
        else:
            raise Problem("method_not_allowed", 405, allow=("GET", "POST"))
        response = JsonResponse({**data, "csrf": get_token(request)})
        response["Cache-Control"] = "no-store"
        return response
    except Problem as exc:
        return problem_response(exc)


def enforcement_data(reconciled, state, now):
    """A past verification is not proof of the current lock or worker health."""
    unknown = {"state": "unknown", "reason": "awaiting_worker",
               "updated_at": reconciled.updated_at if reconciled else None}
    if state is None:
        return {**unknown, "reason": "policy_unavailable"}
    if not state.get("locked"):
        return {**unknown, "reason": "unlocked"}
    if not reconciled:
        return unknown
    age = (now - reconciled.updated_at).total_seconds()
    if not 0 <= age <= STALE_AFTER:
        return {**unknown, "reason": "stale_worker"}
    start = state.get("start")
    if start is None or reconciled.updated_at < start:
        return {**unknown, "reason": "stale_occurrence"}
    if reconciled.state.get("desired") != state.get("desired"):
        return {**unknown, "reason": "target_mismatch"}
    return {**reconciled.state, "updated_at": reconciled.updated_at}


def session(request):
    if request.method == "GET":
        response = JsonResponse({"csrf": get_token(request)})
        response["Cache-Control"] = "no-store"
        return response
    if request.method != "POST":
        return not_allowed("GET", "POST")
    try:
        from .security import require_enabled
        require_enabled()
        data = body(request)
        if set(data) != {"username", "password"}:
            raise Problem("invalid_login", 400)
        user = login_user(request, data)
        response = JsonResponse({"user": me_data(user), "csrf": get_token(request)})
        response["Cache-Control"] = "no-store"
        return response
    except Problem as exc:
        return problem_response(exc)


def token_login(request):
    if request.method != "POST":
        return not_allowed("POST")
    try:
        from .security import require_enabled
        require_enabled()
        data = body(request)
        if set(data) - {"username", "password", "name", "days"}:
            raise Problem("invalid_login", 400)
        user = login_user(request, data, create_session=False)
        token, raw = issue_token(user, data.get("name", "TUI"), data.get("days", 30))
        response = JsonResponse({"token": raw, "id": token.pk, "expires_at": token.expires_at, "user": me_data(user)}, status=201)
        response["Cache-Control"] = "no-store"
        return response
    except Problem as exc:
        return problem_response(exc)


token_login.token_login = True


def signout(request):
    if request.method != "POST":
        return not_allowed("POST")
    # CSRF still protects this browser operation. Clearing an expired cookie is
    # safe and required before an explicitly anonymous Guest request can resume.
    # An invalid terminal credential signs out as an idempotent no-op.
    from .auth import principal
    try:
        actor = principal(request)
    except Problem:
        actor = None
    if actor is not None and not getattr(actor, "is_guest", False):
        audit(actor, "logout")
    token = getattr(request, "identity_token", None)
    if token:
        token.revoked = True
        token.save(update_fields=["revoked"])
    logout(request)
    response = JsonResponse({"ok": True})
    response["Cache-Control"] = "no-store"
    return response


signout.idempotent_logout = True


@api()
def me(request):
    return JsonResponse(me_data(request.identity_user, request.identity_state))


@api(methods=("GET", "POST"), item_methods=("DELETE",), account=True)
def tokens(request, ident=None):
    user = request.identity_user
    if request.method == "GET":
        live = PersonalToken.objects.filter(user=user, revoked=False, expires_at__gt=timezone.now() - TOKEN_RETENTION)
        return JsonResponse({"tokens": [{"id": token.pk, "name": token.name, "expires_at": token.expires_at, "revoked": token.revoked}
                                        for token in live.order_by("expires_at")]})
    if request.method == "DELETE":
        token = PersonalToken.objects.get(pk=ident, user=user)
        token.revoked = True
        token.save(update_fields=["revoked"])
        audit(user, "token.revoke", token.pk)
        return JsonResponse({"ok": True})
    if getattr(request, "identity_token", None):
        raise Problem("session_required", 403)
    data = body(request)
    if set(data) - {"name", "days"}:
        raise Problem("unknown_field")
    token, raw = issue_token(user, data.get("name"), data.get("days", 30))
    return JsonResponse({"id": token.pk, "token": raw, "expires_at": token.expires_at}, status=201)


@api(methods=("GET", "POST"), item_methods=("PATCH", "DELETE"), admin=True)
def users(request, ident=None):
    if request.method == "GET":
        return JsonResponse({"users": [user_data(user) for user in User.objects.order_by("id")]})
    if request.method == "DELETE":
        user = save_user(request.identity_user, {"active": False}, ident)
    else:
        user = save_user(request.identity_user, body(request), ident)
    return JsonResponse(user_data(user), status=200 if ident else 201)


def effective_lock(adapter, room, saved, now):
    """A room's effective lock now, its desired state and its manual hold. A
    saved active policy that cannot be evaluated locks the room."""
    effective = {"locked": False, "desired": None, "allowed": True, "reason": "unlocked", "until": None,
                 "evaluated_at": now}
    state, manual = {"locked": False}, None
    if saved and active(saved.config):
        try:
            config = adapter.policy.validate(saved.config, room, adapter.contract.policy_contract())
            authorization = adapter.policy.authorize(config, now)
            state = adapter.policy.desired_at(now, config)
            manual = config.get("manual_lock")
            effective.update(locked=not authorization["allowed"], desired=authorization.get("desired"),
                             allowed=authorization["allowed"], reason=authorization.get("reason"),
                             until=state.get("end") if state else None)
        except Exception:
            effective.update(locked=True, allowed=False, reason="policy_unavailable")
            state = None
    return effective, state, manual


def policy_data(adapter, room, user, saved):
    now = timezone.now()
    effective, state, manual = effective_lock(adapter, room, saved, now)
    reconciled = ReconcileState.objects.filter(room_id=room, revision=saved.revision).first() if saved else None
    return {"room": room, "revision": saved.revision if saved else 0,
            "config": saved.config if saved else None, "can_edit": is_admin(user), "can_lock": is_admin(user),
            "manual_lock": manual, "effective": effective, "enforcement": enforcement_data(reconciled, state, now)}


def _validated(adapter, config, room):
    try:
        return adapter.policy.validate(config, room, adapter.contract.policy_contract())
    except Exception as error:
        raise policy_problem(error) from None


def _hold_valid(adapter, room, revision, hold):
    try:
        _validated(adapter, with_manual_hold(empty(room, settings.TIME_ZONE, revision), hold), room)
        return True
    except Problem:
        return False


@api(methods=("GET", "PUT"))
def room_policy(request, room):
    from .security import require_enabled
    require_enabled()
    adapter = get_adapter()
    if room not in adapter.contract.rooms:
        raise Problem("unknown_room", 404)
    if request.method == "GET":
        return JsonResponse(policy_data(adapter, room, request.identity_user, RoomPolicy.objects.filter(room_id=room).first()))
    # Same answers as every other Admin mutation: a Guest signs in, a Member is refused.
    require_admin(request, request.identity_user)
    if not adapter.policy:
        raise Problem("policy_unavailable", 503)
    data = body(request)
    if set(data) - {"revision", "config"} or not isinstance(data.get("config"), dict) or type(data.get("revision")) is not int:
        raise Problem("invalid_policy")
    with adapter.command_lock((room,), ()), transaction.atomic():
        admin_actor(request.identity_user)
        saved = RoomPolicy.objects.filter(room_id=room).first()
        current = saved.revision if saved else 0
        if current != data["revision"]:
            raise Problem("revision_conflict", 409)
        revision = next_revision(room, current)
        config = {**data["config"], "room": room, "timezone": settings.TIME_ZONE, "revision": revision}
        # Schedule editing cannot invent, replace or erase a captured hold.
        manual = manual_hold(saved.config) if saved else None
        if "manual_lock" in config and config["manual_lock"] != manual:
            raise Problem("invalid_policy")
        outcome = "ok"
        try:
            config = _validated(adapter, with_manual_hold(config, manual), room)
        except Problem:
            if manual is None or _hold_valid(adapter, room, revision, manual):
                raise
            # The stored hold names a source this house no longer offers. It
            # cannot be kept, so a schedule edit releases it.
            config = _validated(adapter, with_manual_hold(config, None), room)
            outcome = "hold_released"
        saved, _ = RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": revision,
                                                                             "updated_by": request.identity_user})
        audit(request.identity_user, "policy.update", room, outcome)
    return JsonResponse({"room": room, "revision": saved.revision, "config": saved.config})


@api()
def locks(request):
    """Every room's effective lock in one read, for the room lists. Anyone who
    reads the site sees it; while authentication is off no lock applies."""
    adapter, now = get_adapter(), timezone.now()
    rooms = list(adapter.contract.rooms)
    saved = ({row.room_id: row for row in RoomPolicy.objects.filter(room_id__in=rooms)}
             if request.identity_state.enabled else {})
    summary = {}
    for room in rooms:
        effective, _, manual = effective_lock(adapter, room, saved.get(room), now)
        summary[room] = {"locked": effective["locked"], "manual": manual is not None, "until": effective["until"],
                         "reason": effective["reason"]}
    return JsonResponse({"rooms": summary})


@api(methods=("PUT",), admin=True)
def room_lock(request, room):
    # ATOMIC_REQUESTS is off (tests assert it): no request-wide transaction
    # can span the native read below.
    adapter = get_adapter()
    if room not in adapter.contract.rooms:
        raise Problem("unknown_room", 404)
    if not adapter.policy:
        raise Problem("policy_unavailable", 503)
    data = body(request)
    spec = adapter.contract.rooms[room]
    if (set(data) - {"desired"} != {"revision", "locked"} or type(data["revision"]) is not int or data["revision"] < 0
            or type(data["locked"]) is not bool):
        raise Problem("invalid_lock")
    # An optional desired is what the caller saw: the lock captures only that,
    # never a source the room changed to since. It cannot name a new target.
    expected = data.get("desired")
    if "desired" in data and (data["locked"] is not True or expected not in ("off", *spec["sources"])):
        raise Problem("invalid_lock")
    # Hold the same room lock as commands and the worker, but no database write
    # transaction during native I/O. Recheck credentials and CAS after the read.
    with adapter.command_lock((room,), ()):
        admin_actor(request.identity_user)
        saved = RoomPolicy.objects.filter(room_id=room).first()
        current = saved.revision if saved else 0
        if current != data["revision"]:
            raise Problem("revision_conflict", 409)
        manual = manual_hold(saved.config) if saved else None
        if (manual is not None) == data["locked"]:
            if expected is not None and manual.get("desired") != expected:
                raise Problem("room_state_changed", 409)
            return JsonResponse(policy_data(adapter, room, request.identity_user, saved))
        captured = None
        if data["locked"]:
            desired = adapter.read(spec["source_item"])
            if desired not in ("off", *spec["sources"]):
                raise Problem("room_state_unavailable", 409)
            if expected is not None and desired != expected:
                raise Problem("room_state_changed", 409)
            captured = {"desired": desired, "started_at": timezone.now().isoformat()}
        with transaction.atomic():
            actor = admin_actor(request.identity_user)
            saved = RoomPolicy.objects.filter(room_id=room).first()
            if (saved.revision if saved else 0) != current:
                raise Problem("revision_conflict", 409)
            revision = next_revision(room, current)
            stored = saved.config if saved and isinstance(saved.config, dict) else empty(room, settings.TIME_ZONE, revision)
            config = with_manual_hold({**stored, "room": room, "timezone": settings.TIME_ZONE, "revision": revision}, captured)
            outcome = "ok"
            try:
                config = _validated(adapter, config, room)
            except Problem:
                if captured is not None:
                    raise
                # Unlock always succeeds. A remaining schedule that names
                # something the house no longer offers is kept for review but
                # disabled, so it can neither lock the room nor block it.
                config = {**config, "enabled": False}
                outcome = "schedule_disabled"
            saved, _ = RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": revision,
                                                                                 "updated_by": actor})
            audit(actor, "room.lock" if captured else "room.unlock", room, outcome)
        return JsonResponse(policy_data(adapter, room, request.identity_user, saved))


@api(methods=("POST",))
def command(request, room):
    data = body(request)
    if set(data) != {"action", "value"} or not isinstance(data.get("action"), str):
        raise Problem("invalid_command")
    # Ordinary controls are not security events and are not audited.
    answer = get_adapter().dispatch(request.identity_user, room, data["action"], data["value"])
    return JsonResponse(answer, status=202)


@api()
def item_state(request, item):
    return HttpResponse(get_adapter().read(item), content_type="text/plain")


def home(request):
    """Serve the normal Home asset at the clean root with identical authorization."""
    if request.method not in ("GET", "HEAD"):
        return not_allowed("GET", "HEAD")
    return asset(request, "home.html")


def page_alias(request, target):
    """Finite aliases never reflect query parameters into their redirect."""
    if request.method not in ("GET", "HEAD"):
        return not_allowed("GET", "HEAD")
    response = HttpResponseRedirect(target)
    response["Cache-Control"] = "no-store"
    return response


def asset(request, name):
    if name == "access-config.js" and request.method in ("GET", "HEAD"):
        # A deployed native-mode asset must never bypass the bounded gateway.
        response = HttpResponse("" if request.method == "HEAD" else
            'globalThis.THERMITE_ACCESS_CONFIG = Object.freeze({"version":1,"mode":"authenticated"});\n',
            content_type="application/javascript")
        response["Cache-Control"] = "no-store"
        return response
    try:
        adapter = get_adapter()
    except Problem as exc:
        return problem_response(exc)
    spec = adapter.contract.assets.get(name)
    if spec is None or not adapter.contract.asset_root:
        return failure("not_found", 404)
    if request.method not in ("GET", "HEAD"):
        return not_allowed("GET", "HEAD")
    admin_only = bool(spec.get("admin")) or name in ADMIN_ASSETS
    if spec.get("private") or admin_only or name == "house.js" or name.endswith(".json"):
        from .auth import principal
        try:
            user = principal(request)
            if admin_only:
                require_admin(request, user)
        except Problem as exc:
            if exc.status == 401 and name.endswith(".html") and "access.html" in adapter.contract.assets:
                next_path = ASSET_ROUTES.get(name, "/static/remote/" + name)
                response = HttpResponseRedirect("/account/?" + urlencode({"next": next_path}))
                response["Cache-Control"] = "no-store"
                return response
            return problem_response(exc)
    target = (adapter.contract.asset_root / name).resolve()
    if target.parent != adapter.contract.asset_root or not target.is_file():
        return failure("not_found", 404)
    response = FileResponse(target.open("rb"))
    if request.method == "HEAD":
        # Keep GET metadata and normal file disposal, without exposing a body
        # even when this view is called without a server's HEAD suppression.
        response.streaming_content = ()
    response["Cache-Control"] = "no-store" if spec.get("private") or admin_only or name == "house.js" or name.endswith(".json") else "no-cache"
    return response


@api(methods=("POST",))
def all_off(request):
    if body(request):
        raise Problem("unknown_field")
    if connection.in_atomic_block:
        # No request-owned DB transaction may remain open while its worker
        # threads acquire room/resource locks and make transport calls.
        raise Problem("runtime_busy", 503)
    from .batches import Busy, commands
    adapter = get_adapter()
    user = request.identity_user
    def dispatch(room):
        try:
            close_old_connections()
            # Original credential context is immutable and is revalidated by
            # dispatch after acquiring the same room/resource locks as usual.
            return adapter.dispatch(user, room, "source", "off")
        finally:
            connections.close_all()
    results = commands.run(adapter.contract.rooms, dispatch,
                           fatal=lambda error: not isinstance(error, Problem) or error.status == 401)
    accepted, skipped = [], []
    for room, result in results:
        if isinstance(result, Busy):
            skipped.append({"room": room, "reason": "runtime_busy"})
        elif isinstance(result, Problem):
            skipped.append({"room": room, "reason": result.code})
        else:
            accepted.append(room)
    return JsonResponse({"accepted": accepted, "skipped": skipped}, status=202)


@api(admin=True)
def pairing_items(request):
    if request.GET.get("metadata") != "pairing" or set(request.GET) - {"metadata", "fields"}:
        raise Problem("not_found", 404)
    return JsonResponse(get_adapter().pairing_list(), safe=False)


@api(methods=("GET", "POST"))
def item_resource(request, item):
    if request.method == "GET":
        if set(request.GET) - {"fields"}:
            raise Problem("not_found", 404)
        return JsonResponse(get_adapter().group(item))
    # Authorize and validate before taking every room lock.
    user = request.identity_user
    require_admin(request, user)
    adapter = get_adapter()
    if request.content_type != "text/plain" or len(request.body) > 64:
        raise Problem("invalid_pairing_command")
    try:
        value = request.body.decode("ascii")
    except UnicodeError:
        raise Problem("invalid_pairing_command") from None
    answer = adapter.pair(user, item, value)
    audit(request.identity_user, "pairing.command", item)
    return JsonResponse(answer, status=202)


@api("camera.view", methods=("GET", "POST"))
def media(request, resource):
    # Guests see cameras only while authentication is off.
    if request.GET:
        raise Problem("not_found", 404)
    if request.method == "POST" and request.content_type != "application/sdp":
        raise Problem("invalid_sdp")
    content_type, data = get_adapter().media("/unifi/media/" + resource, request.method, request.body)
    return HttpResponse(data, content_type=content_type)
