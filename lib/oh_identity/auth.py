"""Session/PAT boundary. Bearer credentials are never accepted from a URL."""

import hashlib
import hmac
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.sessions.models import Session
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.db import transaction
from django.utils import timezone

from .models import LoginThrottle, PersonalToken, Role, RoomPolicy, User
from .services import Problem, audit, is_admin, permissions


def failure(code, status=403):
    response = JsonResponse({"error": code}, status=status)
    response["Cache-Control"] = "no-store"
    return response


def csrf_failure(request, reason=""):
    return failure("csrf_failed")


def bearer(request):
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    if not header.startswith("Bearer ") or len(header) > 256:
        raise Problem("invalid_token", 401)
    raw = header[7:]
    token = PersonalToken.objects.select_related("user__role").filter(digest=hashlib.sha256(raw.encode()).hexdigest()).first()
    if not token or token.revoked or token.expires_at <= timezone.now() or not token.user.is_active or token.auth_revision != token.user.auth_revision:
        raise Problem("invalid_token", 401)
    return token


class IdentityCSRF(CsrfViewMiddleware):
    def process_view(self, request, callback, callback_args, callback_kwargs):
        if getattr(callback, "token_login", False):
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
                return failure(exc.code, exc.status)
            return None
        return super().process_view(request, callback, callback_args, callback_kwargs)


def principal(request):
    token = getattr(request, "identity_token", None)
    if token:
        token.user._identity_context = {"token": token.pk}
        return token.user
    if not request.user.is_authenticated:
        raise Problem("login_required", 401)
    user = User.objects.select_related("role").filter(pk=request.user.pk, is_active=True).first()
    if not user or request.session.get("identity_revision") != user.auth_revision:
        raise Problem("session_expired", 401)
    user._identity_context = {"session": request.session.session_key}
    return user


def revalidate(user):
    """Recheck a queued HTTP credential after lock/DB waits, just before effects."""
    context = getattr(user, "_identity_context", None)
    if context is None:
        return  # Explicit trusted-local service calls have no HTTP credential.
    if not User.objects.filter(pk=user.pk, is_active=True, auth_revision=user.auth_revision).exists():
        raise Problem("authorization_changed", 401)
    if context.get("token"):
        valid = PersonalToken.objects.filter(pk=context["token"], user_id=user.pk, revoked=False,
                                             auth_revision=user.auth_revision, expires_at__gt=timezone.now()).exists()
    else:
        valid = Session.objects.filter(session_key=context.get("session"), expire_date__gt=timezone.now()).exists()
    if not valid:
        raise Problem("credential_expired", 401)


def api(permission=None, methods=("GET",)):
    def decorate(view):
        @wraps(view)
        def call(request, *args, **kwargs):
            try:
                if request.method not in methods:
                    raise Problem("method_not_allowed", 405)
                user = principal(request)
                if permission and permission not in permissions(user):
                    raise Problem("forbidden", 403)
                if permission and permission.endswith(".manage") and not is_admin(user):
                    raise Problem("forbidden", 403)
                request.identity_user = user
                response = view(request, *args, **kwargs)
                response["Cache-Control"] = "no-store"
                return response
            except (User.DoesNotExist, PersonalToken.DoesNotExist, Role.DoesNotExist, RoomPolicy.DoesNotExist):
                return failure("not_found", 404)
            except Problem as exc:
                return failure(exc.code, exc.status)
        return call
    return decorate


def login_user(request, data, create_session=True):
    username, secret = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(secret, str) or len(username) > 150 or len(secret) > 1024:
        raise Problem("invalid_login", 401)
    now = timezone.now()
    # Account and direct peer are independently bounded; forwarded headers ignored.
    keys = [hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()
            for value in ("user:" + username.casefold(), "peer:" + request.META.get("REMOTE_ADDR", ""))]
    with transaction.atomic():
        for key in keys:
            row, _ = LoginThrottle.objects.get_or_create(key=key, defaults={"until": now})
            if row.until > now and row.attempts >= 5:
                raise Problem("login_throttled", 429)
            row.attempts = row.attempts + 1 if row.until > now else 1
            row.until = now + timedelta(minutes=5)
            row.save()
        # Bound record count against attacker-controlled usernames.
        LoginThrottle.objects.filter(until__lt=now).delete()
        if LoginThrottle.objects.count() > 10000:
            raise Problem("login_throttled", 429)
    user = authenticate(request, username=username, password=secret)
    if user is None:
        audit(None, "login", outcome="denied")
        raise Problem("invalid_login", 401)
    if create_session:
        login(request, user)
        request.session["identity_revision"] = user.auth_revision
    # Reset only the account bucket; a valid user cannot clear peer attack limits.
    LoginThrottle.objects.filter(key=keys[0]).delete()
    audit(user, "login")
    return user
