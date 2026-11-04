"""Short serialized mutations. Network I/O must stay outside these transactions."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ADMIN, GUEST, MEMBER, Audit, PersonalToken, User


class Problem(Exception):
    """A fixed client error. detail holds only server-chosen codes/paths."""

    def __init__(self, code, status=400, *, retry_after=None, allow=None, detail=None):
        self.code, self.status = code, status
        self.retry_after, self.allow, self.detail = retry_after, allow, detail or {}


# Security events only; ordinary room commands are not audited. Denied logins
# are attacker-driven, so they have their own smaller cap and cannot evict
# account, policy or security-switch records.
AUDIT_CAP = 5000
DENIED_CAP = 1000


def peer_digest(request):
    """A keyed, non-reversible label for the direct peer's address bucket."""
    from django.conf import settings
    from .addresses import peer_bucket
    bucket = peer_bucket(request.META.get("REMOTE_ADDR"))
    return hmac.new(settings.SECRET_KEY.encode(), ("audit-peer\0" + bucket).encode(), hashlib.sha256).hexdigest()[:32]


def audit(actor, action, target="", outcome="ok", *, request=None, kind=None):
    context = getattr(actor, "_identity_context", None) or {}
    if kind is None:
        kind = ("guest" if context.get("guest") else "token" if context.get("token") else
                "session" if context.get("session") else "local" if actor is not None else "anonymous")
    peer = context.get("peer") or (peer_digest(request) if request is not None else "")
    Audit.objects.create(actor_id=getattr(actor, "pk", None) or None, action=action, target=str(target)[:100],
                         outcome=outcome, principal=kind, peer=peer)
    # Fixed schema and bounded retention; no arbitrary payload field.
    denied = outcome == "denied"
    rows = Audit.objects.filter(outcome="denied") if denied else Audit.objects.exclude(outcome="denied")
    cap = DENIED_CAP if denied else AUDIT_CAP
    value = next(iter(rows.order_by("-id").values_list("id", flat=True)[cap - 1:cap]), None)
    if value:
        rows.filter(id__lt=value).delete()


def permissions(user):
    """The canonical permission list for /api/v1/me and every check."""
    if getattr(user, "is_guest", False):
        return set(GUEST) if user.auth_enabled else {*GUEST, "camera.view"}
    if not user.is_active:
        return set()
    return set(ADMIN if user.admin else MEMBER)


def is_admin(user):
    return bool(not getattr(user, "is_guest", False) and user.is_active and user.admin)


def admin_actor(actor):
    from .security import require_enabled
    from .auth import revalidate
    require_enabled()
    revalidate(actor)
    fresh = User.objects.filter(pk=actor.pk).first()
    if not fresh or fresh.auth_revision != actor.auth_revision or not is_admin(fresh):
        raise Problem("forbidden", 403)
    return fresh


def password(user, value):
    if not isinstance(value, str) or len(value) > 1024:
        raise Problem("invalid_password")
    try:
        validate_password(value, user)
    except ValidationError:
        raise Problem("invalid_password") from None
    user.set_password(value)


@transaction.atomic
def bootstrap(username, secret):
    """First admin after local ownership or a validated owner-issued setup code."""
    if User.objects.exists():
        raise Problem("already_initialized", 409)
    if not isinstance(username, str) or not username or len(username) > 150:
        raise Problem("invalid_user")
    user = User(username=username, admin=True)
    try:
        user.full_clean(exclude=["password"])
    except ValidationError:
        raise Problem("invalid_user") from None
    password(user, secret)
    user.save()
    # Trusted local bootstrap retains its explicit enable-authentication meaning.
    from .security import transition
    from .models import SecurityState
    state, _ = SecurityState.objects.get_or_create(pk=1)
    transition(state, True, user)
    audit(user, "bootstrap", user.pk)
    return user


@transaction.atomic
def save_user(actor, data, ident=None):
    """Create or update an account: {username, password, active, admin}."""
    admin_actor(actor)
    user = User.objects.get(pk=ident) if ident else User()
    if set(data) - {"username", "active", "admin", "password"}:
        raise Problem("unknown_field")
    if not ident and not {"username", "password"} <= set(data):
        raise Problem("missing_field")
    was_admin = is_admin(user)
    if "username" in data:
        if not isinstance(data["username"], str):
            raise Problem("invalid_user")
        user.username = data["username"]
    for field, attribute in (("active", "is_active"), ("admin", "admin")):
        if field in data:
            if not isinstance(data[field], bool):
                raise Problem("invalid_" + field)
            setattr(user, attribute, data[field])
    if "password" in data:
        password(user, data["password"])
    if was_admin and not is_admin(user) and not User.objects.filter(is_active=True, admin=True).exclude(pk=user.pk).exists():
        raise Problem("last_admin", 409)
    try:
        user.full_clean()
    except ValidationError:
        raise Problem("invalid_user") from None
    if ident:
        # Any account change ends that account's sessions and tokens.
        user.auth_revision += 1
    user.save()
    audit(actor, "user.update" if ident else "user.create", user.pk)
    return user


def issue_token(user, name, days=30):
    from .security import require_enabled
    from .auth import revalidate
    require_enabled()
    revalidate(user)
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name) or isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise Problem("invalid_token")
    raw = "thm_" + secrets.token_urlsafe(40)
    token = PersonalToken.objects.create(user=user, name=name.strip(), digest=hashlib.sha256(raw.encode()).hexdigest(),
                                         auth_revision=user.auth_revision, expires_at=timezone.now() + timedelta(days=days))
    audit(user, "token.create", token.pk)
    return token, raw
