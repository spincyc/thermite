"""Read the security audit: `thermite access audit`, as the service account.

Read only. Like prune and migrate it opens the site's identity database
through its private service configuration; it changes no row. The journal
carries the same rows as `audit ...` lines (oh_identity.trail) while they
are retained there; the table keeps the latest 5000 security events and
1000 denied logins.
"""

from datetime import datetime, timedelta
import re

from . import AccessError
from .files import require_service_account

RELATIVE = re.compile(r"(\d{1,6})([smhd])")
UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
# Actions whose target is an account's ID.
ACCOUNT_TARGETS = {"login", "bootstrap", "user.create", "user.update"}


def since(value, now, zone):
    """--since: 30m, 12h, 7d, or an ISO date or time, in the site's timezone
    unless it names its own offset."""
    match = RELATIVE.fullmatch(value)
    if match:
        return now - timedelta(**{UNITS[match.group(2)]: int(match.group(1))})
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise AccessError("--since takes 30m, 12h, 7d, or an ISO date or time such as 2026-11-04T18:00") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def rows(config_path, *, after=None, actions=()):
    """Audit rows of one site, as the service account; see select."""
    require_service_account()
    from .service import check_schema, configured
    config, _, _ = configured(config_path, tls=False)
    check_schema()
    from zoneinfo import ZoneInfo
    return select(after=after, actions=actions, zone=ZoneInfo(config["timezone"]))


def select(*, after=None, actions=(), zone=None):
    """Audit rows, oldest first: each row's time, action, outcome, principal,
    actor (ID and username), target, the account a target names, and the
    peer's keyed digest (first 8 characters). Needs a configured database."""
    from datetime import timezone as utc
    from django.db.models import Q
    from django.utils import timezone
    from oh_identity.models import Audit, User
    query = Audit.objects.order_by("id")
    if after:
        query = query.filter(at__gte=since(after, timezone.now(), zone or utc.utc))
    if actions:
        selected = Q()
        for action in actions:
            # A family name (token) selects its actions (token.create, token.revoke).
            selected |= Q(action=action) | Q(action__startswith=action + ".")
        query = query.filter(selected)
    names = dict(User.objects.values_list("pk", "username"))
    result = []
    for row in query:
        entry = {"at": row.at.isoformat(), "action": row.action, "outcome": row.outcome, "principal": row.principal,
                 "actor": row.actor_id, "user": names.get(row.actor_id), "target": row.target,
                 "peer": row.peer[:8] or None}
        if row.action in ACCOUNT_TARGETS and row.target.isdigit():
            entry["account"] = names.get(int(row.target))
        result.append(entry)
    return result


def text(entry):
    from oh_identity.trail import line
    fields = {key: value for key, value in entry.items() if key != "at"}
    return entry["at"] + " " + line("audit", **fields)
