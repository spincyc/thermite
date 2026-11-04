"""UTC schedule evaluation with explicit DST, restart and catch-up rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .model import PolicyError

UTC = timezone.utc
GRACE = timedelta(minutes=15)


def utc(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PolicyError("aware_datetime_required")
    return now.astimezone(UTC)


@lru_cache(maxsize=4096)
def local_instant(day: date, clock: str, zone: str) -> datetime:
    """First fold occurrence; a gap advances to the first valid minute.

    Both folds must round-trip through UTC. Merely attaching tzinfo does
    not validate a nonexistent local time. The bound also covers an IANA
    transition that skips a whole civil day.
    """
    naive = datetime.combine(day, time.fromisoformat(clock))
    tz = ZoneInfo(zone)
    for minute in range(26 * 60 + 1):
        candidate = naive + timedelta(minutes=minute)
        valid = []
        for fold in (0, 1):
            instant = candidate.replace(tzinfo=tz, fold=fold).astimezone(UTC)
            if instant.astimezone(tz).replace(tzinfo=None) == candidate:
                valid.append(instant)
        if valid:
            return min(valid)
    raise PolicyError("unresolvable_local_time")


def _windows(policy: dict, now: datetime):
    today = now.astimezone(ZoneInfo(policy["timezone"])).date()
    # Overnight windows last at most one local day. Yesterday covers active
    # windows; two days also covers a skipped civil day / 25-hour DST day.
    for offset in range(-2, 1):
        day = today + timedelta(days=offset)
        for rule in policy["locks"]:
            if day.weekday() not in rule["days"]:
                continue
            end_day = day + timedelta(days=rule["end"] <= rule["start"])
            yield rule, day, local_instant(day, rule["start"], policy["timezone"]), local_instant(end_day, rule["end"], policy["timezone"])


def boundary_key(policy: dict, rule: str, day: date, kind: str) -> str:
    return f"{policy['room']}:{policy['revision']}:{rule}:{day.isoformat()}:{kind}"


def desired_at(now_utc: datetime, policy: dict) -> dict:
    now = utc(now_utc)
    manual = policy.get("manual_lock")
    if manual is not None:
        start = utc(datetime.fromisoformat(manual["started_at"]))
        return {"room": policy["room"], "revision": policy["revision"], "locked": True,
                "desired": manual["desired"], "rule": None, "start": start, "end": None,
                "key": f"{policy['room']}:{policy['revision']}:manual:{manual['started_at']}", "kind": "manual"}
    if policy.get("enabled", True):
        for rule, day, start, end in _windows(policy, now):
            if start <= now < end:
                return {"room": policy["room"], "revision": policy["revision"], "locked": True,
                        "desired": rule["desired"], "rule": rule["id"], "start": start, "end": end,
                        "key": boundary_key(policy, rule["id"], day, "start"), "kind": "schedule"}
    return {"room": policy["room"], "revision": policy["revision"], "locked": False, "desired": None,
            "rule": None, "start": None, "end": None, "key": None, "kind": None}


def authorize(config: dict, aware_now: datetime) -> dict:
    """Ordinary actuation only; principals and reads live in the gateway."""
    state = desired_at(aware_now, config)
    return {"allowed": not state["locked"], "reason": "manual_lock" if state["kind"] == "manual" else "room_locked" if state["locked"] else "unlocked",
            "desired": state["desired"], "revision": state["revision"]}


@dataclass(frozen=True)
class Boundary:
    key: str
    room: str
    revision: int
    at: datetime
    kind: str
    target: str | None
    outcome: str = "due"
    # A start for an occurrence that began before this revision. Its source
    # may already be current; the worker then sends nothing.
    catch_up: bool = False


def due_boundaries(now_utc: datetime, policy: dict, receipts=(), *, not_before: datetime | None = None,
                   grace: timedelta = GRACE) -> list[Boundary]:
    """Bounded one-shot catch-up, plus a currently active lock's start.

    Persistence supplies the trusted policy changed_at as not_before so an
    edit does not replay an older boundary. Receipts include revision and
    local occurrence date, suppressing duplicate folds and backward clocks.
    A currently active lock supersedes older unlock/daily-off actions.

    A lock that was already active when this revision took effect (an
    unlock into a window, re-enabled authentication, any edit mid-window)
    gets one catch-up start at not_before. Its key carries the revision, so
    each revision catches up at most once. Older daily-off and unlock
    actions are never replayed.
    """
    now = utc(now_utc)
    if policy.get("manual_lock") is not None or not policy.get("enabled", True):
        return []
    lower = utc(not_before) if not_before is not None else now - timedelta(days=2)
    seen = set(receipts)
    state = desired_at(now, policy)
    candidates = []
    today = now.astimezone(ZoneInfo(policy["timezone"])).date()
    for offset in range(-2, 1):
        day = today + timedelta(days=offset)
        for rule in policy["daily_off"]:
            if day.weekday() in rule["days"]:
                candidates.append((rule["id"], day, "daily_off", local_instant(day, rule["time"], policy["timezone"]), "off", False))
    for rule, day, start, end in _windows(policy, now):
        if start <= now < end:
            # Before this revision, the occurrence's real start was not ours to
            # send; its catch-up happens at the revision's own start instead.
            catch_up = start < lower
            candidates.append((rule["id"], day, "start", lower if catch_up else start, rule["desired"], catch_up))
        candidates.append((rule["id"], day, "unlock", end, None if rule["unlock"] == "leave" else rule["unlock"], False))
    result = []
    for rule, day, kind, at, target, catch_up in candidates:
        key = boundary_key(policy, rule, day, kind)
        if key in seen or not lower <= at <= now:
            continue
        outcome = "due"
        if kind != "start" and now - at > grace:
            outcome = "missed"
        elif kind != "start" and state["locked"]:
            outcome = "coalesced_active_lock"
        elif target is None:
            outcome = "leave"
        result.append(Boundary(key, policy["room"], policy["revision"], at, kind, target, outcome, catch_up))
    return sorted(result, key=lambda event: (event.at, 0 if event.kind == "start" else 1, event.key))
