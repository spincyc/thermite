"""Who may do what on the site. There is no mode: three kinds of principal
and three settings, each independent of the others.

A client without a credential is the Guest. It reads the site (rooms, what
they play, their locks, the alerts about a room or a device) and controls
every room that is not locked. An account adds to that: a Member also sees
the cameras, and an Admin also administers the site (accounts, pairing,
these settings, room locks and schedules, the System page and the alerts
about the house as a whole). Accounts exist or they do not. With none,
every client is a Guest and nothing is administered until the owner creates
the first Admin: with a one-use setup code issued on the host (arm_setup,
first_admin) or there directly (services.bootstrap). Nothing turns
accounts off again.

The settings, each an Admin's to change and none changing another:

  guest_cameras  Off unless turned on. On, a Guest sees the cameras as a
                 Member does; off, camera media needs an account, whatever
                 else is set.
  guest_text     On unless turned off. Off, a Guest does not see free text:
                 titles, artists, apps, playback.
  room locks     Each room's own schedule and manual hold (RoomPolicy). A
                 locked room refuses every principal's commands, an Admin's
                 too (runtime.RuntimeAdapter.check_policy).

permissions() is the one place a principal's rights are worked out;
auth.require_account and auth.require_admin ask for an account or an Admin
where no single permission says it.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import SecurityState, User
from .services import Problem, admin_actor, audit

GUEST = ("site.read", "room.control")
MEMBER = (*GUEST, "camera.view")
ADMIN = (*MEMBER, "settings.manage", "users.manage", "pairing.manage")
#: The settings an Admin changes on the Security page.
GUEST_SETTINGS = ("guest_text", "guest_cameras")


def permissions(user):
    """The canonical permission list for /api/v1/me and every check. A Guest
    carries the cameras setting it was admitted under (auth.Guest)."""
    if getattr(user, "is_guest", False):
        return set(MEMBER if user.cameras else GUEST)
    if not user.is_active:
        return set()
    return set(ADMIN if user.admin else MEMBER)


def get_state():
    # A missing singleton is a damaged/unmigrated installation, never permission
    # to silently expose an existing site. Only migration/local bootstrap seeds it.
    state = SecurityState.objects.filter(pk=1).first()
    if state is None:
        raise Problem("security_unavailable", 503)
    return state


def status(state=None):
    """What every client may read: whether an account exists yet, whether a
    setup code is armed for the first, and what Guests see."""
    state = state or get_state()
    first = not User.objects.exists()
    return {"bootstrap_required": first,
            "setup_available": bool(first and state.setup_digest and state.setup_expires_at
                                    and state.setup_expires_at > timezone.now()),
            "guest_text": state.guest_text, "guest_cameras": state.guest_cameras}


def guests_see_text(state=None):
    """Whether a Guest sees free-text Items: titles, artists, apps, playback
    and the like (a contract state marked free_text)."""
    return (state or get_state()).guest_text


def hidden(user, spec, state=None):
    """Whether this principal may not see an Item of this contract state."""
    return bool(spec and spec.get("free_text") and getattr(user, "is_guest", False) and not guests_see_text(state))


def set_guests(actor, values, *, request=None):
    """An Admin's choice of what Guests see: {setting: bool} over
    GUEST_SETTINGS. A request already admitted keeps the answer it was
    admitted under; open Guest streams end at their next check
    (streams.valid) and reopen under the new one."""
    with transaction.atomic():
        actor = admin_actor(actor)
        state = get_state()
        for name, value in values.items():
            if getattr(state, name) != value:
                setattr(state, name, value)
                state.save(update_fields=[name])
                audit(actor, f"security.{name}_{'on' if value else 'off'}", request=request)
        return status(state)


@transaction.atomic
def arm_setup(ttl_minutes=15):
    """Owner CLI only: return a capability for terminal display, never an API."""
    if type(ttl_minutes) is not int or not 1 <= ttl_minutes <= 60:
        raise Problem("invalid_setup_lifetime")
    state = get_state()
    if User.objects.exists():
        raise Problem("already_initialized", 409)
    raw = secrets.token_urlsafe(32)
    state.setup_digest = hashlib.sha256(raw.encode()).hexdigest()
    state.setup_expires_at = timezone.now() + timedelta(minutes=ttl_minutes)
    state.save(update_fields=["setup_digest", "setup_expires_at"])
    audit(None, "security.setup")
    return raw


def setup_code_valid(state, data):
    """The owner-issued one-use code, unexpired."""
    code = data.get("setup_code")
    return bool(isinstance(code, str) and 1 <= len(code) <= 200 and state.setup_digest
                and state.setup_expires_at and state.setup_expires_at > timezone.now()
                and hmac.compare_digest(state.setup_digest, hashlib.sha256(code.encode()).hexdigest()))


@transaction.atomic
def first_admin(request, data):
    """Create the site's first account, an Admin, for whoever holds the
    owner's setup code: {username, password, setup_code}. It changes no
    setting and no lock. The database's write lock, taken as the
    transaction begins, lets one of two such requests through; a refused one
    leaves the code armed."""
    from .services import bootstrap
    if set(data) - {"username", "password", "setup_code"}:
        raise Problem("unknown_field")
    if User.objects.exists():
        raise Problem("already_initialized", 409)
    if not setup_code_valid(get_state(), data):
        raise Problem("invalid_setup_code", 403)
    bootstrap(data.get("username"), data.get("password"), request=request)
    return status()
