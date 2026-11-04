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
from oh_routes import ASSET_ROUTES

from .auth import api, failure, login_user
from .models import CATALOG, PersonalToken, ReconcileState, Role, RoomPolicy, User
from .runtime import get_adapter
from .services import Problem, admin_actor, audit, issue_token, is_admin, permissions, save_role, save_user


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
    return {"id": user.pk, "username": user.username, "active": user.is_active, "role": user.role_id, "rooms": user.rooms}


def role_data(role):
    return {"id": role.pk, "name": role.name, "permissions": role.permissions, "reserved": role.reserved}


def me_data(user):
    from .security import get_state
    enabled = get_state().enabled
    return {**user_data(user), "site": settings.SITE_ID, "permissions": sorted(permissions(user)),
            "can_edit": enabled and is_admin(user), "auth_enabled": enabled,
            "is_guest": bool(getattr(user, "is_guest", False))}


def security(request):
    from .security import change_security, status
    try:
        if request.method == "GET":
            data = status()
        elif request.method == "POST":
            data = change_security(request, body(request))
            logout(request)
        else:
            raise Problem("method_not_allowed", 405)
        response = JsonResponse({**data, "csrf": get_token(request)})
        response["Cache-Control"] = "no-store"
        return response
    except Problem as exc:
        return failure(exc.code, exc.status)


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
    if not 0 <= age <= 15:
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
        return failure("method_not_allowed", 405)
    try:
        from .security import require_enabled
        require_enabled()
        data = body(request)
        if set(data) != {"username", "password"}:
            raise Problem("invalid_login", 401)
        user = login_user(request, data)
        response = JsonResponse({"user": me_data(user), "csrf": get_token(request)})
        response["Cache-Control"] = "no-store"
        return response
    except Problem as exc:
        return failure(exc.code, exc.status)


def token_login(request):
    if request.method != "POST":
        return failure("method_not_allowed", 405)
    try:
        from .security import require_enabled
        require_enabled()
        data = body(request)
        if set(data) - {"username", "password", "name", "days"}:
            raise Problem("invalid_login", 401)
        user = login_user(request, data, create_session=False)
        token, raw = issue_token(user, data.get("name", "TUI"), data.get("days", 30))
        response = JsonResponse({"token": raw, "id": token.pk, "expires_at": token.expires_at, "user": me_data(user)}, status=201)
        response["Cache-Control"] = "no-store"
        return response
    except Problem as exc:
        return failure(exc.code, exc.status)


token_login.token_login = True


def signout(request):
    if request.method != "POST":
        return failure("method_not_allowed", 405)
    # CSRF still protects this browser operation. Clearing an expired cookie is
    # safe and required before an explicitly anonymous Guest request can resume.
    from .auth import principal
    try:
        actor = principal(request)
    except Problem:
        actor = None
    audit(actor, "logout")
    token = getattr(request, "identity_token", None)
    if token:
        token.revoked = True
        token.save(update_fields=["revoked"])
    logout(request)
    response = JsonResponse({"ok": True})
    response["Cache-Control"] = "no-store"
    return response


@api("site.read")
def me(request):
    return JsonResponse(me_data(request.identity_user))


@api(methods=("GET", "POST", "DELETE"), authentication=True)
def tokens(request, ident=None):
    user = request.identity_user
    if request.method == "GET":
        return JsonResponse({"tokens": [{"id": token.pk, "name": token.name, "expires_at": token.expires_at, "revoked": token.revoked}
                                        for token in PersonalToken.objects.filter(user=user)]})
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


@api("users.manage", methods=("GET", "POST", "PATCH", "DELETE"))
def users(request, ident=None):
    if ident is None and request.method in {"PATCH", "DELETE"} or ident is not None and request.method == "POST":
        raise Problem("method_not_allowed", 405)
    if request.method == "GET":
        return JsonResponse({"users": [user_data(user) for user in User.objects.order_by("id")]})
    if request.method == "DELETE":
        user = save_user(request.identity_user, {"active": False}, ident)
    else:
        user = save_user(request.identity_user, body(request), ident)
    return JsonResponse(user_data(user), status=200 if ident else 201)


@api("roles.manage", methods=("GET", "POST", "PATCH", "DELETE"))
def roles(request, ident=None):
    if ident is None and request.method in {"PATCH", "DELETE"} or ident is not None and request.method == "POST":
        raise Problem("method_not_allowed", 405)
    if request.method == "GET":
        return JsonResponse({"roles": [role_data(role) for role in Role.objects.order_by("id")], "catalog": list(CATALOG)})
    role = save_role(request.identity_user, {} if request.method == "DELETE" else body(request), ident,
                     delete=request.method == "DELETE")
    return JsonResponse(role_data(role) if role else {"ok": True}, status=200 if ident else 201)


def policy_data(adapter, room, user, saved):
    now = timezone.now()
    effective = {"locked": False, "desired": None, "allowed": True, "reason": "unlocked", "until": None,
                 "evaluated_at": now}
    state, manual = {"locked": False}, None
    if saved and (saved.config.get("enabled") is not False or saved.config.get("manual_lock") is not None):
        try:
            config = adapter.policy.validate(saved.config, room, adapter.contract.policy_contract())
            authorization = adapter.policy.authorize(config, now)
            state = adapter.policy.effective(config, now) if hasattr(adapter.policy, "effective") else None
            manual = config.get("manual_lock")
            effective.update(locked=not authorization["allowed"], desired=authorization.get("desired"),
                             allowed=authorization["allowed"], reason=authorization.get("reason"),
                             until=state.get("end") if state else None)
        except Exception:
            effective.update(locked=True, allowed=False, reason="policy_unavailable")
            state = None
    if "room.control" not in permissions(user) or user.rooms is not None and room not in user.rooms:
        effective.update(allowed=False, reason="room_read_only")
    reconciled = ReconcileState.objects.filter(room_id=room, revision=saved.revision).first() if saved else None
    return {"room": room, "revision": saved.revision if saved else 0,
            "config": saved.config if saved else None, "can_edit": is_admin(user), "can_lock": is_admin(user),
            "manual_lock": manual, "effective": effective, "enforcement": enforcement_data(reconciled, state, now)}


@api("site.read", methods=("GET", "PUT"))
def room_policy(request, room):
    from .security import require_enabled
    require_enabled()
    adapter = get_adapter()
    if room not in adapter.contract.rooms:
        raise Problem("unknown_room", 404)
    if request.method == "GET":
        return JsonResponse(policy_data(adapter, room, request.identity_user, RoomPolicy.objects.filter(room_id=room).first()))
    if not is_admin(request.identity_user):
        raise Problem("forbidden", 403)
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
        config = {**data["config"], "room": room, "timezone": settings.TIME_ZONE, "revision": current + 1}
        # Schedule editing cannot invent, replace or erase a captured hold.
        manual = saved.config.get("manual_lock") if saved else None
        if "manual_lock" in config and config["manual_lock"] != manual:
            raise Problem("invalid_policy")
        config.pop("manual_lock", None)
        if manual is not None:
            config["manual_lock"] = manual
        try:
            config = adapter.policy.validate(config, room, adapter.contract.policy_contract())
        except Exception:
            raise Problem("invalid_policy") from None
        saved, _ = RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": current + 1,
                                                                             "updated_by": request.identity_user})
        audit(request.identity_user, "policy.update", room)
    return JsonResponse({"room": room, "revision": saved.revision, "config": saved.config})


@transaction.non_atomic_requests
@api("settings.manage", methods=("PUT",), authentication=True)
def room_lock(request, room):
    adapter = get_adapter()
    if room not in adapter.contract.rooms:
        raise Problem("unknown_room", 404)
    if not adapter.policy:
        raise Problem("policy_unavailable", 503)
    data = body(request)
    if set(data) != {"revision", "locked"} or type(data["revision"]) is not int or data["revision"] < 0 or type(data["locked"]) is not bool:
        raise Problem("invalid_lock")
    # Hold the same room lock as commands and the worker, but no database write
    # transaction during native I/O. Recheck credentials and CAS after the read.
    with adapter.command_lock((room,), ()):
        admin_actor(request.identity_user)
        saved = RoomPolicy.objects.filter(room_id=room).first()
        current = saved.revision if saved else 0
        if current != data["revision"]:
            raise Problem("revision_conflict", 409)
        manual = saved.config.get("manual_lock") if saved else None
        if (manual is not None) == data["locked"]:
            return JsonResponse(policy_data(adapter, room, request.identity_user, saved))
        captured = None
        if data["locked"]:
            spec = adapter.contract.rooms[room]
            desired = adapter.read(spec["source_item"])
            if desired not in ("off", *spec["sources"]):
                raise Problem("room_state_unavailable", 409)
            captured = {"desired": desired, "started_at": timezone.now().isoformat()}
        with transaction.atomic():
            actor = admin_actor(request.identity_user)
            saved = RoomPolicy.objects.filter(room_id=room).first()
            if (saved.revision if saved else 0) != current:
                raise Problem("revision_conflict", 409)
            config = dict(saved.config) if saved else {"version": 1, "enabled": False, "daily_off": [], "locks": []}
            config.update(room=room, timezone=settings.TIME_ZONE, revision=current + 1)
            config.pop("manual_lock", None)
            if captured is not None:
                config["manual_lock"] = captured
            try:
                config = adapter.policy.validate(config, room, adapter.contract.policy_contract())
            except Exception:
                raise Problem("invalid_policy") from None
            saved, _ = RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": current + 1,
                                                                                 "updated_by": actor})
            audit(actor, "room.lock" if captured else "room.unlock", room)
        return JsonResponse(policy_data(adapter, room, request.identity_user, saved))


@api("room.control", methods=("POST",))
def command(request, room):
    data = body(request)
    if set(data) != {"action", "value"} or not isinstance(data.get("action"), str):
        raise Problem("invalid_command")
    answer = get_adapter().dispatch(request.identity_user, room, data["action"], data["value"])
    audit(request.identity_user, "room.command", room)
    return JsonResponse(answer, status=202)


@api("site.read")
def item_state(request, item):
    return HttpResponse(get_adapter().read(item), content_type="text/plain")


def home(request):
    """Serve the normal Home asset at the clean root with identical authorization."""
    if request.method not in ("GET", "HEAD"):
        response = failure("method_not_allowed", 405)
        response["Allow"] = "GET, HEAD"
        return response
    return asset(request, "home.html")


def page_alias(request, target):
    """Finite aliases never reflect query parameters into their redirect."""
    if request.method not in ("GET", "HEAD"):
        response = failure("method_not_allowed", 405)
        response["Allow"] = "GET, HEAD"
        return response
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
        return failure(exc.code, exc.status)
    spec = adapter.contract.assets.get(name)
    if spec is None or not adapter.contract.asset_root or request.method not in ("GET", "HEAD"):
        return failure("not_found", 404)
    admin_only = bool(spec.get("admin")) or name in ADMIN_ASSETS
    if spec.get("private") or admin_only or name == "house.js" or name.endswith(".json"):
        from .auth import principal
        try:
            user = principal(request)
            if admin_only:
                from .security import require_enabled
                require_enabled()
                if getattr(user, "is_guest", False):
                    raise Problem("login_required", 401)
            if "site.read" not in permissions(user):
                raise Problem("forbidden", 403)
            if admin_only and not is_admin(user):
                raise Problem("forbidden", 403)
        except Problem as exc:
            if exc.status == 401 and name.endswith(".html") and "access.html" in adapter.contract.assets:
                next_path = ASSET_ROUTES.get(name, "/static/remote/" + name)
                response = HttpResponseRedirect("/account/?" + urlencode({"next": next_path}))
                response["Cache-Control"] = "no-store"
                return response
            return failure(exc.code, exc.status)
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


@transaction.non_atomic_requests
@api("room.control", methods=("POST",))
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
    audit(user, "all_off")
    return JsonResponse({"accepted": accepted, "skipped": skipped}, status=202)


@api("pairing.manage")
def pairing_items(request):
    if request.GET.get("metadata") != "pairing" or set(request.GET) - {"metadata", "fields"}:
        raise Problem("not_found", 404)
    return JsonResponse(get_adapter().pairing_list(), safe=False)


@api(methods=("GET", "POST"))
def item_resource(request, item):
    if request.method == "GET":
        if "site.read" not in permissions(request.identity_user):
            raise Problem("forbidden", 403)
        if set(request.GET) - {"fields"}:
            raise Problem("not_found", 404)
        return JsonResponse(get_adapter().group(item))
    from .security import require_enabled
    require_enabled()
    if request.content_type != "text/plain" or len(request.body) > 64:
        raise Problem("invalid_pairing_command")
    try:
        value = request.body.decode("ascii")
    except UnicodeError:
        raise Problem("invalid_pairing_command") from None
    answer = get_adapter().pair(request.identity_user, item, value)
    audit(request.identity_user, "pairing.command", item)
    return JsonResponse(answer, status=202)


@api("site.read", methods=("GET", "POST"))
def media(request, resource):
    from .security import get_state
    # Remotes are open to Guests; camera images and video need an account
    # whenever authentication is on.
    if getattr(request.identity_user, "is_guest", False) and get_state().enabled:
        raise Problem("login_required", 401)
    if request.GET:
        raise Problem("not_found", 404)
    if request.method == "POST" and request.content_type != "application/sdp":
        raise Problem("invalid_sdp")
    content_type, data = get_adapter().media("/unifi/media/" + resource, request.method, request.body)
    return HttpResponse(data, content_type=content_type)
