"""Durable worker hooks. Claims precede I/O; outcomes describe uncertainty honestly."""

from datetime import timedelta

from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import Exists, Max, Q
from django.utils import timezone

from oh_room_policy import PolicyRecord

from .models import BoundaryReceipt, LoginThrottle, PersonalToken, ReconcileState, RoomPolicy, SecurityState

# Every reason the worker reports; anything else is stored as "unavailable".
REASONS = {"", "unavailable", "missed", "leave", "dispatched", "dispatch_unconfirmed", "already_current", "stale_revision",
           "stale_schedule", "coalesced_active_lock", "shared_resource_conflict", "invalid_policy", "native_read_unavailable"}
# The worker never looks further back than two days (due_boundaries); a
# receipt older than this can no longer suppress anything.
RECEIPT_RETENTION = timedelta(days=3)
# Expired tokens stay listed briefly so their owner can see what ended.
TOKEN_RETENTION = timedelta(days=7)


def active_policies():
    # Keep the worker's single-query revision snapshot while fencing all policy
    # effects on the durable switch, including a missing/damaged singleton.
    return RoomPolicy.objects.filter(Exists(SecurityState.objects.filter(pk=1, enabled=True)))


def next_revision(room, current):
    """A row's next revision. A new row continues after any receipt or status
    a pruned predecessor left, so its boundary keys can never collide."""
    if current:
        return current + 1
    retained = [BoundaryReceipt.objects.filter(room_id=room).aggregate(value=Max("revision"))["value"],
                ReconcileState.objects.filter(room_id=room).aggregate(value=Max("revision"))["value"]]
    return max([value for value in retained if value is not None], default=0) + 1


@transaction.atomic
def claim_boundary(key, room, revision):
    if not active_policies().filter(room_id=room, revision=revision).exists():
        return False
    _, created = BoundaryReceipt.objects.get_or_create(key=key, defaults={"room_id": room, "revision": revision})
    return created


def finish_boundary(key, outcome, room, revision):
    status, reason = outcome.get("status"), outcome.get("reason")
    if status not in {"applied", "uncertain", "skipped"}:
        raise ValueError("Invalid boundary outcome")
    BoundaryReceipt.objects.filter(key=key, room_id=room, revision=revision).update(
        status=status, reason=reason if reason in REASONS else "unavailable")


@transaction.atomic
def save_reconcile(room, revision, state):
    if not active_policies().filter(room_id=room, revision=revision).exists():
        return False
    # Caller supplies canonical worker status, never raw device replies.
    from .runtime import get_adapter
    contract = get_adapter().contract
    sources = contract.rooms.get(room, {}).get("sources", ())
    known_items = set(contract.states)
    phase = state.get("phase")
    value = {"phase": phase if isinstance(phase, str) and phase in {"invalid", "unlocked", "enforcing", "verified"} else "invalid",
             "state": state.get("state") if isinstance(state.get("state"), str) and state["state"] in {"verified", "degraded", "unknown"} else "unknown",
             "scope": "configured_native_controls"}
    desired = state.get("desired")
    value["desired"] = desired if desired == "off" or desired in sources else None
    for field in ("matched", "mismatched", "unknown"):
        value[field] = [item for item in state.get(field, []) if isinstance(item, str) and item in known_items][:200] if isinstance(state.get(field), list) else []
    if isinstance(state.get("reason"), str) and state["reason"] in REASONS:
        value["reason"] = state["reason"]
    if isinstance(state.get("repair"), str) and state["repair"] in known_items:
        value["repair"] = state["repair"]
    retry = state.get("retry_seconds")
    if type(retry) in (float, int) and 0 <= retry <= 3600:
        value["retry_seconds"] = retry
    devices = contract.house.get("devices", ())
    ids = set(devices) if hasattr(devices, "keys") else {item.get("id") for item in devices if hasattr(item, "get")}
    value["limitations"] = [item for item in state.get("limitations", []) if isinstance(item, str) and item.startswith("passive_endpoint:")
                             and item.split(":", 1)[1] in ids] if isinstance(state.get("limitations"), list) else []
    ReconcileState.objects.update_or_create(room_id=room, defaults={"revision": revision, "state": value})
    return True


@transaction.atomic
def rebase_timezone(name):
    """Move every policy to the site's timezone. The caller holds every room
    lock. A new revision and timestamp fence old occurrences, so no earlier
    boundary is replayed; an active window gets one catch-up start."""
    from zoneinfo import ZoneInfo
    ZoneInfo(name)
    changed = 0
    now = timezone.now()
    for row in RoomPolicy.objects.all():
        if not isinstance(row.config, dict) or row.config.get("timezone") == name:
            continue
        row.revision += 1
        row.config = {**row.config, "timezone": name, "revision": row.revision}
        row.updated_at = now
        row.save(update_fields=["revision", "config", "updated_at"])
        changed += 1
    return changed


@transaction.atomic
def prune_unknown_rooms(rooms):
    """Delete policies and status for rooms the compiled house no longer has."""
    gone = sorted(set(RoomPolicy.objects.exclude(room_id__in=rooms).values_list("room_id", flat=True)))
    RoomPolicy.objects.filter(room_id__in=gone).delete()
    ReconcileState.objects.exclude(room_id__in=rooms).delete()
    if gone:
        from .services import audit
        for room in gone:
            audit(None, "policy.prune", room)
    return gone


def prune_history(now=None):
    """Daily housekeeping: drop records that can no longer affect a decision."""
    now = now or timezone.now()
    counts = {}
    with transaction.atomic():
        counts["receipts"] = BoundaryReceipt.objects.filter(updated_at__lt=now - RECEIPT_RETENTION).delete()[0]
    with transaction.atomic():
        counts["sessions"] = Session.objects.filter(expire_date__lt=now).delete()[0]
    with transaction.atomic():
        counts["tokens"] = PersonalToken.objects.filter(Q(revoked=True) | Q(expires_at__lt=now - TOKEN_RETENTION)).delete()[0]
    with transaction.atomic():
        counts["throttles"] = LoginThrottle.objects.filter(until__lte=now).delete()[0]
    return counts


class Store:
    """The pure worker's persistent-store protocol."""

    def policies(self):
        return [PolicyRecord(row.config, row.updated_at) for row in active_policies()]

    def current_revision(self, room):
        return active_policies().filter(room_id=room).values_list("revision", flat=True).first()

    def current_revisions(self, rooms):
        # Include missing rows: creation of a neighboring policy must invalidate
        # the same conflict snapshot as changing an existing revision.
        revisions = dict.fromkeys(rooms)
        revisions.update(active_policies().filter(room_id__in=revisions).values_list("room_id", "revision"))
        return revisions

    def claim_boundary(self, room, revision, key, at):
        return claim_boundary(key, room, revision)

    def finish_boundary(self, room, revision, key, outcome):
        finish_boundary(key, outcome, room, revision)

    def save_reconcile(self, room, revision, state):
        return save_reconcile(room, revision, state)

    def prune(self, now):
        return prune_history(now)
