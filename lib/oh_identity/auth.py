"""Session/PAT boundary. Bearer credentials are never accepted from a URL."""

import hashlib
import hmac
import math
import threading
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.sessions.models import Session
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.db import transaction
from django.utils import timezone

from .addresses import peer_bucket
from .models import LoginThrottle, PersonalToken, RoomPolicy, User
from .services import Problem, audit, is_admin, peer_digest, permissions

# Password attempts. A (username, peer) pair locks hard after a few failures;
# a username across all peers is slowed exponentially but never locked, so
# another peer cannot lock an administrator out; a peer is bounded across
# usernames. Only failures count. Peers are IPv4 addresses or IPv6 /64s.
WINDOW = timedelta(minutes=5)
PAIR_LIMIT = 5
PEER_LIMIT = 30
USER_FREE = 10
USER_MAX_DELAY = 60
# Argon2 is deliberately expensive; bound concurrent verifications.
HASHING = threading.BoundedSemaphore(2)
HASH_WAIT = 10


class Guest:
    """The anonymous principal: in memory only, never a stored account.

    auth_enabled is the switch it was admitted under; any later transition
    changes the security revision, which every effect path rechecks.
    """
    pk = 0
    username = "Guest"
    admin = False
    is_active = True
    is_guest = True
    auth_revision = 0

    def __init__(self, revision, auth_enabled, peer=""):
        self.auth_enabled = auth_enabled
        self._identity_context = {"guest": True, "security_revision": revision, "peer": peer}


def failure(code, status=403, *, retry_after=None, allow=None, detail=None):
    # "error" is the stable member every client reads; detail adds only fixed
    # codes/paths chosen by the server.
    response = JsonResponse({"error": code, **(detail or {})}, status=status)
    response["Cache-Control"] = "no-store"
    if retry_after is not None:
        response["Retry-After"] = str(max(1, math.ceil(retry_after)))
    if allow:
        response["Allow"] = ", ".join(allow)
    return response


def problem_response(exc):
    return failure(exc.code, exc.status, retry_after=exc.retry_after, allow=exc.allow, detail=exc.detail)


def not_allowed(*methods):
    return failure("method_not_allowed", 405, allow=methods)


def csrf_failure(request, reason=""):
    return failure("csrf_failed")


def bearer(request):
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer" or not raw or len(header) > 256 or any(c.isspace() for c in raw):
        raise Problem("invalid_token", 401)
    token = PersonalToken.objects.select_related("user").filter(digest=hashlib.sha256(raw.encode()).hexdigest()).first()
    if not token or token.revoked or token.expires_at <= timezone.now() or not token.user.is_active or token.auth_revision != token.user.auth_revision:
        raise Problem("invalid_token", 401)
    return token


class IdentityCSRF(CsrfViewMiddleware):
    def process_view(self, request, callback, callback_args, callback_kwargs):
        from .guest import listener
        if listener(request):
            # No cookie or credential reaches here; GuestListener required the
            # listener's own Origin and a JSON body instead.
            return None
        if getattr(callback, "token_login", False):
            if request.method != "POST":
                return None  # The view answers 405 with its Allow header.
            if request.content_type != "application/json" or request.headers.get("Origin") or request.COOKIES:
                return failure("invalid_token_channel")
            return None
        if request.headers.get("Authorization"):
            # No ambient cookies or browser origins on the TUI token channel.
            if request.COOKIES.get(settings.SESSION_COOKIE_NAME) or request.headers.get("Origin"):
                return failure("mixed_authentication")
            try:
                request.identity_token = bearer(request)
            except Problem as exc:
                if getattr(callback, "idempotent_logout", False):
                    # Signing out an already invalid terminal credential succeeds
                    # without effect; there is no session or token to end.
                    request.identity_token = None
                    return None
                return problem_response(exc)
            return None
        return super().process_view(request, callback, callback_args, callback_kwargs)


def principal(request):
    from .security import get_state
    state = get_state()
    # The authentication prologue reuses this read; revalidate() and effect
    # paths always read the switch again after any lock or transaction wait.
    request.identity_state = state
    peer = peer_digest(request)
    token = getattr(request, "identity_token", None)
    if token:
        # bearer() has just checked the token, account and revision.
        if not state.enabled:
            raise Problem("authentication_disabled", 403)
        token.user._identity_context = {"token": token.pk, "security_revision": state.revision, "peer": peer}
        return token.user
    if not request.user.is_authenticated:
        if request.headers.get("Authorization") or request.COOKIES.get(settings.SESSION_COOKIE_NAME):
            raise Problem("session_expired", 401)
        return Guest(state.revision, state.enabled, peer)
    if not state.enabled:
        request.session.flush()
        raise Problem("authentication_disabled", 403)
    user = User.objects.filter(pk=request.user.pk, is_active=True).first()
    if not user or request.session.get("identity_revision") != user.auth_revision:
        # Like a deleted session, the rejection happens once: the flushed
        # session's cookie is cleared, and the next request is a Guest.
        request.session.flush()
        raise Problem("session_expired", 401)
    user._identity_context = {"session": request.session.session_key, "security_revision": state.revision, "peer": peer}
    return user


def revalidate(user):
    """Recheck a queued HTTP credential after lock/DB waits, just before effects."""
    context = getattr(user, "_identity_context", None)
    if context is None:
        return  # Explicit trusted-local service calls have no HTTP credential.
    from .security import get_state
    state = get_state()
    if context.get("security_revision") != state.revision:
        raise Problem("authorization_changed", 401)
    if context.get("guest"):
        return
    if not state.enabled:
        raise Problem("authentication_disabled", 403)
    if not User.objects.filter(pk=user.pk, is_active=True, auth_revision=user.auth_revision).exists():
        raise Problem("authorization_changed", 401)
    if context.get("token"):
        valid = PersonalToken.objects.filter(pk=context["token"], user_id=user.pk, revoked=False,
                                             auth_revision=user.auth_revision, expires_at__gt=timezone.now()).exists()
    else:
        valid = Session.objects.filter(session_key=context.get("session"), expire_date__gt=timezone.now()).exists()
    if not valid:
        raise Problem("credential_expired", 401)


def require_account(request, user):
    """Account-only features: authentication must be on and a Guest must sign in."""
    if not request.identity_state.enabled:
        raise Problem("authentication_disabled", 403)
    if getattr(user, "is_guest", False):
        raise Problem("login_required", 401)


def require_admin(request, user):
    require_account(request, user)
    if not is_admin(user):
        raise Problem("forbidden", 403)


def api(permission=None, methods=("GET",), *, account=False, admin=False, item_methods=None):
    """account: a signed-in Member or Admin; admin: an Admin. A missing
    permission asks a Guest to sign in and refuses an account.
    item_methods, when given, are the methods of the routed item (ident) form."""
    def decorate(view):
        @wraps(view)
        def call(request, *args, **kwargs):
            try:
                allowed = item_methods if item_methods is not None and kwargs.get("ident") is not None else methods
                if request.method not in allowed:
                    raise Problem("method_not_allowed", 405, allow=allowed)
                user = principal(request)
                if admin:
                    require_admin(request, user)
                elif account:
                    require_account(request, user)
                if permission and permission not in permissions(user):
                    raise Problem("login_required", 401) if getattr(user, "is_guest", False) else Problem("forbidden", 403)
                request.identity_user = user
                response = view(request, *args, **kwargs)
                response["Cache-Control"] = "no-store"
                return response
            except (User.DoesNotExist, PersonalToken.DoesNotExist, RoomPolicy.DoesNotExist):
                return failure("not_found", 404)
            except Problem as exc:
                return problem_response(exc)
        return call
    return decorate


def _bucket(*parts):
    return hmac.new(settings.SECRET_KEY.encode(), "\0".join(parts).encode(), hashlib.sha256).hexdigest()


def _wait(keys, now):
    """Seconds before this attempt may run; 0 when it may run now."""
    rows = {row.key: row for row in LoginThrottle.objects.filter(key__in=keys.values(), until__gt=now)}
    pair, peer, user = rows.get(keys["pair"]), rows.get(keys["peer"]), rows.get(keys["user"])
    wait = timedelta(0)
    for row, limit in ((pair, PAIR_LIMIT), (peer, PEER_LIMIT)):
        if row and row.attempts >= limit:
            wait = max(wait, row.until - now)
    if user and user.attempts > USER_FREE:
        # until is the last failure plus WINDOW; delay doubles per failure.
        delay = timedelta(seconds=min(USER_MAX_DELAY, 2 ** min(user.attempts - USER_FREE - 1, 16)))
        wait = max(wait, user.until - WINDOW + delay - now)
    return wait.total_seconds() if wait > timedelta(0) else 0


def _failed(keys, now, account):
    """Count one failure; only existing accounts get a username bucket, so the
    table is bounded by peers rather than by attacker-chosen names."""
    with transaction.atomic():
        for kind, key in keys.items():
            if kind == "user" and not account:
                continue
            row, created = LoginThrottle.objects.get_or_create(key=key, defaults={"attempts": 1, "until": now + WINDOW})
            if not created:
                row.attempts = row.attempts + 1 if row.until > now else 1
                row.until = now + WINDOW
                row.save(update_fields=["attempts", "until"])


def login_user(request, data, create_session=True):
    username, secret = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(secret, str) or len(username) > 150 or len(secret) > 1024:
        raise Problem("invalid_login", 400)
    if create_session:
        from .security import require_enabled
        security_revision = require_enabled().revision
    peer = peer_bucket(request.META.get("REMOTE_ADDR"))
    name = username.casefold()
    # Forwarded headers are ignored; the direct peer is the only address.
    keys = {"pair": _bucket("pair", name, peer), "user": _bucket("user", name), "peer": _bucket("peer", peer)}
    now = timezone.now()
    with transaction.atomic():
        LoginThrottle.objects.filter(until__lte=now).delete()
        wait = _wait(keys, now)
    if wait:
        raise Problem("login_throttled", 429, retry_after=wait)
    if not HASHING.acquire(timeout=HASH_WAIT):
        raise Problem("login_throttled", 429, retry_after=1)
    try:
        # Recheck after the queue: failures recorded meanwhile apply here.
        wait = _wait(keys, timezone.now())
        if wait:
            raise Problem("login_throttled", 429, retry_after=wait)
        user = authenticate(request, username=username, password=secret)
        if user is None:
            _failed(keys, timezone.now(), User.objects.filter(username__iexact=username).exists())
    finally:
        HASHING.release()
    if user is None:
        audit(None, "login", outcome="denied", request=request)
        raise Problem("invalid_login", 401)
    if create_session:
        # Password verification runs outside the write transaction. The switch
        # and account revisions must still match before minting a session; an
        # off/on cycle cannot grant a login that began under an earlier switch.
        with transaction.atomic():
            state = require_enabled()
            if state.revision != security_revision or not User.objects.filter(
                    pk=user.pk, is_active=True, auth_revision=user.auth_revision).exists():
                raise Problem("authorization_changed", 401)
            login(request, user)
            request.session["identity_revision"] = user.auth_revision
    # Success never counts. It ends only this pair's lock; account-wide and
    # peer-wide limits decay on their own so a valid login cannot reset them.
    LoginThrottle.objects.filter(key=keys["pair"]).delete()
    audit(user, "login", request=request, kind="password")
    return user
