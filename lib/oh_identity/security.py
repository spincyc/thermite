"""Durable authentication switch, owner-issued one-use setup capability and
what a Guest may see."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import PersonalToken, ReconcileState, RoomPolicy, SecurityState, User
from .services import Problem, audit, is_admin


def get_state():
    # A missing singleton is a damaged/unmigrated installation, never permission
    # to silently expose an existing site. Only migration/local bootstrap seeds it.
    state = SecurityState.objects.filter(pk=1).first()
    if state is None:
        raise Problem("security_unavailable", 503)
    return state


def require_enabled():
    state = get_state()
    if not state.enabled:
        raise Problem("authentication_disabled", 403)
    return state


def status(state=None):
    state = state or get_state()
    first = not User.objects.exists()
    return {"enabled": state.enabled, "revision": state.revision, "bootstrap_required": first,
            "setup_available": bool(first and state.setup_digest and state.setup_expires_at
                                    and state.setup_expires_at > timezone.now()),
            "guest_text": state.guest_text}


def guests_see_text(state=None):
    """Whether a Guest sees free-text Items: titles, artists, apps, playback
    and the like (a contract state marked free_text). Yes unless an Admin
    turned it off; with authentication off every client is a Guest, so it
    binds only while authentication is on."""
    state = state or get_state()
    return state.guest_text or not state.enabled


def hidden(user, spec, state=None):
    """Whether this principal may not see an Item of this contract state."""
    return bool(spec and spec.get("free_text") and getattr(user, "is_guest", False) and not guests_see_text(state))


def set_guest_text(actor, value, *, request=None):
    """An Admin's choice of whether Guests see free-text Items. Open Guest
    streams end at their next check (streams.valid) and reopen under it."""
    from .auth import revalidate
    with transaction.atomic():
        revalidate(actor)
        state = get_state()
        if state.guest_text != value:
            state.guest_text = value
            state.save(update_fields=["guest_text"])
            audit(actor, "security.guest_text_on" if value else "security.guest_text_off", request=request)
        return status(state)


@transaction.atomic
def arm_setup(ttl_minutes=15):
    """Owner CLI only: return a capability for terminal display, never an API."""
    if type(ttl_minutes) is not int or not 1 <= ttl_minutes <= 60:
        raise Problem("invalid_setup_lifetime")
    state = get_state()
    if state.enabled or User.objects.exists():
        raise Problem("already_initialized", 409)
    raw = secrets.token_urlsafe(32)
    state.setup_digest = hashlib.sha256(raw.encode()).hexdigest()
    state.setup_expires_at = timezone.now() + timedelta(minutes=ttl_minutes)
    state.save(update_fields=["setup_digest", "setup_expires_at"])
    audit(None, "security.setup")
    return raw


def transition(state, enabled, actor, *, request=None):
    """Caller owns the SQLite write transaction and all runtime room locks.
    request attributes the audit row to its peer; an actor without a request
    credential proved the switch with its password."""
    if state.enabled == enabled:
        return state
    state.enabled = enabled
    state.revision += 1
    state.setup_digest = ""
    state.setup_expires_at = None
    state.save()
    User.objects.update(auth_revision=F("auth_revision") + 1)
    PersonalToken.objects.update(revoked=True)
    Session.objects.all().delete()
    # Advancing and retimestamping every policy makes retained rules dormant off
    # and prevents an old boundary/observation being replayed after reenabling.
    now = timezone.now()
    for row in RoomPolicy.objects.all():
        row.revision += 1
        row.config = {**row.config, "revision": row.revision}
        row.updated_at = now
        row.save(update_fields=["revision", "config", "updated_at"])
    ReconcileState.objects.all().delete()
    password = request is not None and not getattr(actor, "_identity_context", None)
    audit(actor, "security.enable" if enabled else "security.disable", request=request,
          kind="password" if password else None)
    if actor is not None:
        actor.refresh_from_db()
    return state


def setup_code_valid(state, data):
    """The owner-issued one-use code, unexpired, for a first enable only."""
    code = data.get("setup_code")
    return bool(data["enabled"] and isinstance(code, str) and 1 <= len(code) <= 200 and state.setup_digest
                and state.setup_expires_at and state.setup_expires_at > timezone.now()
                and hmac.compare_digest(state.setup_digest, hashlib.sha256(code.encode()).hexdigest()))


def change_security(request, data):
    from .auth import login_user, principal, revalidate
    from .runtime import get_adapter
    from .services import bootstrap

    if set(data) - {"enabled", "revision", "username", "password", "setup_code"}:
        raise Problem("unknown_field")
    if type(data.get("enabled")) is not bool or type(data.get("revision")) is not int or data["revision"] < 1:
        raise Problem("invalid_security")
    state = get_state()
    if data["revision"] != state.revision:
        raise Problem("revision_conflict", 409)
    first = not User.objects.exists()
    actor = None
    if first and not setup_code_valid(state, data):
        # Checked before taking every room lock, and again inside them.
        raise Problem("invalid_setup_code", 403)
    if not first:
        # Password login has bounded account/peer throttles. A valid session may
        # supply the username, but never substitutes for the current password.
        username = data.get("username")
        if request.user.is_authenticated or getattr(request, "identity_token", None):
            actor = principal(request)
            if username is not None and username != actor.username:
                raise Problem("invalid_login", 401)
            username = actor.username
        authenticated = login_user(request, {"username": username, "password": data.get("password")}, create_session=False)
        if not is_admin(authenticated):
            raise Problem("forbidden", 403)
        if actor is not None:
            revalidate(actor)
            authenticated._identity_context = actor._identity_context.copy()
        actor = authenticated
    adapter = get_adapter()
    # All guest dispatches and worker effects hold at least their room lock.
    # Take the same locks before the database transaction; never invert ordering.
    with adapter.command_lock(tuple(sorted(adapter.contract.rooms)), ()), transaction.atomic():
        state = get_state()
        if data["revision"] != state.revision:
            raise Problem("revision_conflict", 409)
        if first:
            if User.objects.exists():
                raise Problem("already_initialized", 409)
            if not setup_code_valid(state, data):
                raise Problem("invalid_setup_code", 403)
            actor = bootstrap(data.get("username"), data.get("password"))
            state = get_state()
        else:
            revalidate(actor)
            fresh = User.objects.filter(pk=actor.pk, is_active=True).first()
            if not fresh or fresh.auth_revision != actor.auth_revision or not is_admin(fresh):
                raise Problem("authorization_changed", 401)
            if getattr(actor, "_identity_context", None):
                fresh._identity_context = actor._identity_context
            state = transition(state, data["enabled"], fresh, request=request)
        return status(state)
