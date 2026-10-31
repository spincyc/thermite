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
    if policy.get("enabled", True):
        for rule, day, start, end in _windows(policy, now):
            if start <= now < end:
                return {"room": policy["room"], "revision": policy["revision"], "locked": True,
                        "desired": rule["desired"], "rule": rule["id"], "start": start, "end": end,
                        "key": boundary_key(policy, rule["id"], day, "start")}
    return {"room": policy["room"], "revision": policy["revision"], "locked": False, "desired": None,
            "rule": None, "start": None, "end": None, "key": None}


def effective(config: dict, aware_now: datetime) -> dict:
    return desired_at(aware_now, config)


def authorize(config: dict, aware_now: datetime) -> dict:
    """Ordinary actuation only. Identity roles, reads and grants live above it."""
    state = effective(config, aware_now)
    return {"allowed": not state["locked"], "reason": "room_locked" if state["locked"] else "unlocked",
            "desired": state["desired"], "revision": state["revision"]}


def all_off(configs: dict[str, dict], rooms: list[str], aware_now: datetime) -> dict:
    """Split ordinary All Off without creating or bypassing an override."""
    allowed, skipped = [], []
    for room in rooms:
        (skipped if room in configs and effective(configs[room], aware_now)["locked"] else allowed).append(room)
    return {"rooms": allowed, "skipped": skipped}


@dataclass(frozen=True)
class Boundary:
    key: str
    room: str
    revision: int
    at: datetime
    kind: str
    target: str | None
    outcome: str = "due"


def due_boundaries(now_utc: datetime, policy: dict, receipts=(), *, not_before: datetime | None = None,
                   grace: timedelta = GRACE) -> list[Boundary]:
    """Bounded one-shot catch-up, plus a currently active lock's start.

    Persistence supplies the trusted policy changed_at as not_before so an
    edit does not replay an older boundary. Receipts include revision and
    local occurrence date, suppressing duplicate folds and backward clocks.
    A currently active lock supersedes older unlock/daily-off actions.
    """
    now = utc(now_utc)
    if not policy.get("enabled", True):
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
                candidates.append((rule["id"], day, "daily_off", local_instant(day, rule["time"], policy["timezone"]), "off"))
    for rule, day, start, end in _windows(policy, now):
        if start <= now < end:
            candidates.append((rule["id"], day, "start", start, rule["desired"]))
        candidates.append((rule["id"], day, "unlock", end, None if rule["unlock"] == "leave" else rule["unlock"]))
    result = []
    for rule, day, kind, at, target in candidates:
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
        result.append(Boundary(key, policy["room"], policy["revision"], at, kind, target, outcome))
    return sorted(result, key=lambda event: (event.at, 0 if event.kind == "start" else 1, event.key))
