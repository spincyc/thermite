"""Durable worker hooks. Claims precede I/O; outcomes describe uncertainty honestly."""

from django.db import transaction

from dataclasses import dataclass
from datetime import datetime

from .models import BoundaryReceipt, ReconcileState, RoomPolicy

REASONS = {"", "missed", "coalesced", "leave", "shared_conflict", "stale_revision", "applied", "dispatch_failed",
           "unknown", "unavailable", "mismatch", "policy_invalid", "runtime_unavailable", "source_unknown",
           "boundary_uncertain", "room_locked", "retry_wait", "stale_observation", "dispatched", "dispatch_unconfirmed",
           "coalesced_active_lock", "shared_resource_conflict", "invalid_policy", "native_read_unavailable", "stale_schedule"}


def load_policies():
    return list(RoomPolicy.objects.values("room_id", "config", "revision"))


@transaction.atomic
def claim_boundary(key, room, revision):
    if not RoomPolicy.objects.filter(room_id=room, revision=revision).exists():
        return False
    _, created = BoundaryReceipt.objects.get_or_create(key=key, defaults={"room_id": room, "revision": revision})
    return created


def finish_boundary(key, status, room=None, revision=None):
    reason = ""
    if isinstance(status, dict):
        reason = status.get("reason") if status.get("reason") in REASONS else "unavailable"
        status = status.get("status")
    if status not in {"applied", "failed", "uncertain", "skipped"}:
        raise ValueError("Invalid boundary outcome")
    query = BoundaryReceipt.objects.filter(key=key)
    if room is not None:
        query = query.filter(room_id=room, revision=revision)
    query.update(status=status, reason=reason)


@transaction.atomic
def save_reconcile(room, revision, state):
    if not RoomPolicy.objects.filter(room_id=room, revision=revision).exists():
        return False
    # Caller supplies canonical worker status, never raw device replies.
    from .runtime import get_adapter, safe_item
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
    devices = get_adapter().contract.house.get("devices", ())
    ids = set(devices) if hasattr(devices, "keys") else {item.get("id") for item in devices if hasattr(item, "get")}
    value["limitations"] = [item for item in state.get("limitations", []) if isinstance(item, str) and item.startswith("passive_endpoint:")
                             and item.split(":", 1)[1] in ids] if isinstance(state.get("limitations"), list) else []
    ReconcileState.objects.update_or_create(room_id=room, defaults={"revision": revision, "state": value})
    return True


@dataclass(frozen=True)
class PolicyRecord:
    config: dict
    changed_at: datetime


@transaction.atomic
def rebase_timezone(name):
    """Trusted settings integration calls this when changing the site's timezone."""
    from zoneinfo import ZoneInfo
    from django.utils import timezone
    ZoneInfo(name)
    changed = 0
    for row in RoomPolicy.objects.all():
        if row.config.get("timezone") == name:
            continue
        row.revision += 1
        row.config = {**row.config, "timezone": name, "revision": row.revision}
        row.updated_at = timezone.now()
        row.save(update_fields=["revision", "config", "updated_at"])
        changed += 1
    return changed


class Store:
    """The pure worker's persistent-store protocol."""

    def policies(self):
        return [PolicyRecord(row.config, row.updated_at) for row in RoomPolicy.objects.all()]

    def current_revision(self, room):
        return RoomPolicy.objects.filter(room_id=room).values_list("revision", flat=True).first()

    def claim_boundary(self, room, revision, key, at):
        return claim_boundary(key, room, revision)

    def finish_boundary(self, room, revision, key, outcome):
        finish_boundary(key, outcome, room, revision)

    def save_reconcile(self, room, revision, state):
        return save_reconcile(room, revision, state)
