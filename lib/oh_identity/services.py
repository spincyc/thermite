"""Short serialized mutations. Network I/O must stay outside these transactions."""

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Audit, CATALOG, RESERVED, PersonalToken, Role, User


class Problem(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def audit(actor, action, target="", outcome="ok"):
    Audit.objects.create(actor_id=getattr(actor, "pk", None), action=action, target=str(target)[:100], outcome=outcome)
    # Fixed schema and bounded retention; no arbitrary payload field.
    cutoff = Audit.objects.order_by("-id").values_list("id", flat=True)[4999:5000]
    value = next(iter(cutoff), None)
    if value:
        Audit.objects.filter(id__lt=value).delete()


def permissions(user):
    return set(user.role.permissions) & set(CATALOG) if user.is_active and user.role_id else set()


def is_admin(user):
    return bool(user.is_active and user.role_id and user.role.name == "Admin" and user.role.reserved)


def admin_actor(actor):
    from .auth import revalidate
    revalidate(actor)
    fresh = User.objects.select_related("role").filter(pk=actor.pk).first()
    if not fresh or fresh.auth_revision != actor.auth_revision or not is_admin(fresh):
        raise Problem("forbidden", 403)
    return fresh


def password(user, value):
    if not isinstance(value, str):
        raise Problem("invalid_password")
    try:
        validate_password(value, user)
    except ValidationError:
        raise Problem("invalid_password") from None
    user.set_password(value)


@transaction.atomic
def bootstrap(username, secret):
    """Trusted-local first admin only; caller owns terminal prompting."""
    if User.objects.exists():
        raise Problem("already_initialized", 409)
    for name, grants in RESERVED.items():
        Role.objects.update_or_create(name=name, defaults={"reserved": True, "permissions": list(grants)})
    user = User(username=username, role=Role.objects.get(name="Admin"))
    user.full_clean(exclude=["password"])
    password(user, secret)
    user.save()
    audit(user, "bootstrap", user.pk)
    return user


def role_for(value):
    if type(value) is not int or value < 1:
        raise Problem("unknown_role")
    try:
        return Role.objects.get(pk=value)
    except (Role.DoesNotExist, ValueError, TypeError):
        raise Problem("unknown_role") from None


def grants(value):
    from .runtime import get_adapter
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(x, str) or x not in get_adapter().contract.rooms for x in value):
        raise Problem("invalid_rooms")
    return sorted(set(value))


@transaction.atomic
def save_user(actor, data, ident=None):
    admin_actor(actor)
    user = User.objects.get(pk=ident) if ident else User()
    if set(data) - {"username", "role", "rooms", "active", "password"}:
        raise Problem("unknown_field")
    was_admin = is_admin(user)
    if "username" in data:
        user.username = data["username"]
    if "role" in data:
        user.role = role_for(data["role"])
    if "rooms" in data:
        user.rooms = grants(data["rooms"])
    if "active" in data:
        if not isinstance(data["active"], bool):
            raise Problem("invalid_active")
        user.is_active = data["active"]
    if "password" in data:
        password(user, data["password"])
    if not ident and (not user.role_id or "password" not in data):
        raise Problem("missing_field")
    if was_admin and not is_admin(user) and not User.objects.filter(is_active=True, role__name="Admin", role__reserved=True).exclude(pk=user.pk).exists():
        raise Problem("last_admin", 409)
    try:
        user.full_clean()
    except ValidationError:
        raise Problem("invalid_user") from None
    if ident:
        user.auth_revision += 1
    user.save()
    audit(actor, "user.update" if ident else "user.create", user.pk)
    return user


@transaction.atomic
def save_role(actor, data, ident=None, delete=False):
    admin_actor(actor)
    role = Role.objects.get(pk=ident) if ident else Role()
    if role.reserved:
        raise Problem("reserved_role", 409)
    if delete:
        if role.user_set.exists():
            raise Problem("role_in_use", 409)
        audit(actor, "role.delete", role.pk)
        role.delete()
        return None
    if set(data) - {"name", "permissions"}:
        raise Problem("unknown_field")
    name = data.get("name", role.name)
    values = data.get("permissions", role.permissions)
    if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name) or name.strip().casefold() in {x.casefold() for x in RESERVED} or not isinstance(values, list) or any(
            not isinstance(x, str) or x not in {"site.read", "room.control"} for x in values):
        raise Problem("invalid_role")
    role.name, role.permissions = name.strip(), sorted(set(values))
    try:
        role.full_clean()
    except ValidationError:
        raise Problem("invalid_role") from None
    role.save()
    # A role edit immediately invalidates cached sessions and issued tokens.
    from django.db.models import F
    role.user_set.update(auth_revision=F("auth_revision") + 1)
    audit(actor, "role.update", role.pk)
    return role


def issue_token(user, name, days=30):
    from .auth import revalidate
    revalidate(user)
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name) or isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise Problem("invalid_token")
    raw = "thm_" + secrets.token_urlsafe(40)
    token = PersonalToken.objects.create(user=user, name=name.strip(), digest=hashlib.sha256(raw.encode()).hexdigest(),
                                         auth_revision=user.auth_revision, expires_at=timezone.now() + timedelta(days=days))
    audit(user, "token.create", token.pk)
    return token, raw
