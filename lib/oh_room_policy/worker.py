"""A single worker's injected I/O loop. Importing it never starts work.

Persistence calls must use short transactions. Device I/O happens only
after they return, through the same typed/revision-fenced dispatcher as
the gateway. Claim-before-send is deliberately at-most-once: an ambiguous
crash cannot safely replay a Source's curtain/light actions.
"""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Protocol
from zoneinfo import ZoneInfo

from .model import PolicyError, active, validate
from .plan import Observation, Plan, conflicts, expected_plan, repair_plan, verify
from .schedule import GRACE, Boundary, desired_at, due_boundaries, local_instant, utc

BACKOFF = (2, 5, 15, 30, 60)
# Active enforcement republishes unchanged status at this interval, so a
# status older than STALE_AFTER means the worker is not currently enforcing.
RECONCILE_HEARTBEAT = 10.0
STALE_AFTER = RECONCILE_HEARTBEAT + 5.0


@dataclass(frozen=True)
class PolicyRecord:
    config: dict
    changed_at: datetime


class Store(Protocol):
    def policies(self) -> list[PolicyRecord]: ...
    def current_revision(self, room: str) -> int | None: ...
    def claim_boundary(self, room: str, revision: int, key: str, at: datetime) -> bool: ...
    def finish_boundary(self, room: str, revision: int, key: str, outcome: dict) -> None: ...
    def save_reconcile(self, room: str, revision: int, state: dict) -> bool: ...


class IO(Protocol):
    """Device reads/writes. An IO may also offer source(room, item) -> str,
    the room's current Source Item state, so a catch-up start whose source
    is already current sends nothing; without it, the catch-up is sent."""
    def read(self, items: tuple[str, ...]) -> dict[str, Observation]: ...
    def write(self, room: str, item: str, value: str, revision: int) -> None: ...


class ResourceLocks:
    """Sorted locks, shared with an in-process dispatcher when appropriate."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}

    @contextlib.contextmanager
    def hold(self, keys):
        with self._guard:
            locks = [self._locks.setdefault(key, threading.RLock()) for key in sorted(set(keys))]
        with contextlib.ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            yield


class FileLocks:
    """Optional cross-process locks in a private service-state directory.

    Gateway and worker must share this lock namespace. A policy database
    transaction is never used as a lock around network operations.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @contextlib.contextmanager
    def hold(self, keys, *, blocking=True):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.ExitStack() as stack:
            for key in sorted(set(keys)):
                name = hashlib.sha256(key.encode()).hexdigest()
                fd = os.open(self.directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                stack.callback(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield


class Worker:
    def __init__(self, house, store: Store, io: IO, *, wall_clock=None, monotonic=time.monotonic,
                 locks=None, read_interval=5.0):
        self.house = house.data if hasattr(house, "data") else house
        self.contract = {"rooms": {room: {"sources": spec["sources"]}
                                   for room, spec in self.house["routing"]["rooms"].items()}}
        self.store, self.io = store, io
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic, self.locks = monotonic, locks or ResourceLocks()
        self.read_interval = max(1.0, read_interval)
        self._step_lock = threading.Lock()
        self._seen = {}
        self._resources = {}
        self._plans = {}
        self._runtime = {}
        self._persisted = {}
        self._policies = ()
        self._reconcile_ready = False
        self._notifications = set()
        self._notification_lock = threading.Lock()
        self._wake = threading.Event()

    def notify(self, room: str) -> None:
        """An internal state change can accelerate a read, never write backoff."""
        if room not in self.contract["rooms"]:
            return
        with self._notification_lock:
            self._notifications.add(room)
            self._wake.set()

    def wake(self):
        """Wake the supervisor for shutdown or a trusted local notification."""
        self._wake.set()

    def next_delay(self, maximum=5.0):
        """Sleep until an observation/retry or calendar boundary is due.

        The maximum bounds discovery of cross-process policy edits and wall
        clock changes; it does not set device polling or write retry frequency.
        """
        now, mono = utc(self.wall_clock()), self.monotonic()
        delay = max(0.1, maximum)
        if self._wake.is_set():
            return 0
        if self._reconcile_ready:
            for runtime in self._runtime.values():
                delay = min(delay, max(0, runtime["read_due"] - mono))
        for policy in self._policies:
            if not policy.get("enabled", True):
                continue
            active = desired_at(now, policy)
            if active["end"]:
                delay = min(delay, max(0, (active["end"] - now).total_seconds()))
            today = now.astimezone(ZoneInfo(policy["timezone"])).date()
            # The default fallback is five seconds; the next local day also
            # covers midnight and DST transitions before that wake.
            for offset in (0, 1):
                day = today + timedelta(days=offset)
                for key, point in (("daily_off", "time"), ("locks", "start")):
                    for rule in policy[key]:
                        if day.weekday() in rule["days"]:
                            at = local_instant(day, rule[point], policy["timezone"])
                            if at > now:
                                delay = min(delay, (at - now).total_seconds())
        return delay

    def wait(self, maximum=5.0):
        self._wake.wait(self.next_delay(maximum))

    def _current(self, revisions):
        batch = getattr(self.store, "current_revisions", None)
        if batch is not None:
            return batch(tuple(revisions)) == revisions
        return all(self.store.current_revision(room) == revision for room, revision in revisions.items())

    def _save(self, room, revision, state, *, occurrence=None):
        previous = self._persisted.get(room)
        now = self.monotonic()
        if previous and previous[0] == revision and previous[1] == state and previous[3] == occurrence:
            # Unlocked rows are not freshness evidence. Their next mutation or
            # revision publishes immediately; active enforcement retains a
            # bounded heartbeat even when all observations still match.
            if state.get("phase") == "unlocked" or now - previous[2] < RECONCILE_HEARTBEAT:
                return True
        if not self.store.save_reconcile(room, revision, state):
            return False
        self._persisted[room] = (revision, copy.deepcopy(state), now, occurrence)
        return True

    def _scheduled(self, policies, states):
        now = utc(self.wall_clock())
        return all(desired_at(now, policy)["key"] == states[room]["key"] for room, policy in policies.items())

    def _remember(self, key):
        if len(self._seen) >= 4096:
            del self._seen[next(iter(self._seen))]
        self._seen[key] = None

    def _boundary(self, event: Boundary, protected: list[Plan], revisions, policies, states):
        if event.key in self._seen or not self._current(revisions):
            return False
        claimed = self.store.claim_boundary(event.room, event.revision, event.key, event.at)
        self._remember(event.key)
        if not claimed:
            return False
        outcome = event.outcome
        attempted = False
        if outcome == "due" and event.target is not None:
            try:
                plan = self._plan(event.room, event.target)
            except PolicyError:
                plan = None
                outcome = "invalid_policy"
            if plan is not None and conflicts(plan, protected, boundary=True):
                outcome = "shared_resource_conflict"
            elif plan is not None:
                # Every room contributes to the conflict/revision snapshot,
                # including rooms without a policy yet. Serialize edits to that
                # snapshot through dispatch, using the shared sorted lock order.
                with self.locks.hold([*("room:" + room for room in revisions), *plan.resources]):
                    current_time = utc(self.wall_clock())
                    current = desired_at(current_time, policies[event.room])
                    if not self._current(revisions):
                        outcome = "stale_revision"
                    elif event.kind == "start" and current["key"] != event.key:
                        outcome = "stale_schedule"
                    elif event.kind != "start" and current["locked"]:
                        outcome = "coalesced_active_lock"
                    elif not self._scheduled(policies, states):
                        outcome = "stale_schedule"
                    elif current_time < event.at or event.kind != "start" and current_time - event.at > GRACE:
                        outcome = "missed"
                    elif event.catch_up and self._source_is(event.room, plan.source_item, event.target):
                        # Re-sending a current source would replay its
                        # lifecycle actions; field repairs cover any drift.
                        outcome = "already_current"
                    else:
                        attempted = True
                        try:
                            self.io.write(event.room, plan.source_item, event.target, event.revision)
                            outcome = "dispatched"
                        except Exception:
                            # The transport may have sent it before failing.
                            outcome = "dispatch_unconfirmed"
        status = "applied" if outcome == "dispatched" else "uncertain" if outcome == "dispatch_unconfirmed" else "skipped"
        self.store.finish_boundary(event.room, event.revision, event.key, {"status": status, "reason": outcome})
        return attempted

    def _source_is(self, room, item, target):
        reader = getattr(self.io, "source", None)
        if reader is None:
            return False
        try:
            return reader(room, item) == target
        except Exception:
            return False  # Unknown: send the catch-up rather than skip it.

    def _plan(self, room, choice):
        """The route for (room, choice), compiled once. The house is fixed for
        the worker's lifetime, and compiling a route costs milliseconds of CPU,
        too much for every held room on every step. An IO that has already
        compiled every route (PolicyIO.plans) lends its plans."""
        key = (room, choice)
        plan = self._plans.get(key)
        if plan is None:
            compiled = getattr(self.io, "plans", None)
            plan = compiled.get(room, {}).get(choice) if isinstance(compiled, dict) else None
            if plan is None:
                plan = expected_plan(self.house, room, choice)
            self._plans[key] = plan
        return plan

    def _room_resources(self, room):
        """Every device any of the room's routes can touch; None if unknown."""
        if room not in self._resources:
            try:
                choices = ("off", *self.contract["rooms"][room]["sources"])
                self._resources[room] = frozenset().union(
                    *(self._plan(room, choice).resources for choice in choices))
            except (PolicyError, KeyError, TypeError, ValueError):
                self._resources[room] = None
        return self._resources[room]

    def _sharing(self, invalid):
        """Rooms that share hardware with a room whose policy is unreadable.

        Its intent is unknown, so those rooms are not enforced. Unrelated
        rooms keep their locks. Unknown wiring blocks every room.
        """
        if not invalid:
            return set()
        rooms = set(self.contract["rooms"])
        touched = set()
        for room in invalid:
            resources = self._room_resources(room)
            if resources is None:
                return rooms
            touched |= resources
        blocked = set(invalid)
        for room in rooms - blocked:
            resources = self._room_resources(room)
            if resources is None or resources & touched:
                blocked.add(room)
        return blocked

    def _retry(self, runtime, mono):
        if mono >= runtime["write_due"]:
            delay = BACKOFF[min(runtime["attempt"], len(BACKOFF) - 1)]
            runtime["attempt"] += 1
            runtime["write_due"] = mono + delay
        runtime["read_due"] = min(runtime["write_due"], mono + self.read_interval)

    def step(self) -> dict:
        """One bounded pass, callable by a supervised loop or pure fake-I/O test."""
        if not self._step_lock.acquire(blocking=False):
            return {"busy": True}
        try:
            with self._notification_lock:
                self._wake.clear()
                notified, self._notifications = self._notifications, set()
            for room in notified:
                if room in self._runtime:
                    self._runtime[room]["read_due"] = 0
            result = self._step()
            if result.get("stale"):
                self._reconcile_ready = False
            return result
        finally:
            self._step_lock.release()

    def _step(self):
        self._reconcile_ready = False
        now, mono = utc(self.wall_clock()), self.monotonic()
        records = self.store.policies()
        revisions = dict.fromkeys(self.contract["rooms"])
        states, plans, valid, result, inert, invalid = {}, {}, [], {}, [], set()
        for record in records:
            config = record.config if isinstance(record.config, dict) else {}
            room = config.get("room")
            if not isinstance(room, str) or room not in self.contract["rooms"]:
                # A room removed from the house has no hardware to protect; its
                # leftover row can neither lock nor block any current room.
                if isinstance(room, str):
                    result[room] = {"phase": "ignored", "reason": "unknown_room"}
                continue
            revision = config.get("revision")
            revisions[room] = revision
            if not active(config):
                inert.append((room, revision))
                continue
            try:
                policy = validate(config, room, self.contract)
                changed_at = utc(record.changed_at)
                state = desired_at(now, policy)
                if state["locked"]:
                    plans[room] = self._plan(room, state["desired"])
                states[room] = state
                valid.append((policy, changed_at))
            except (PolicyError, KeyError, TypeError, ValueError):
                plans.pop(room, None)
                invalid.add(room)
        if not self._current(revisions):
            return {"stale": True}
        blocked = self._sharing(invalid)
        for room in set(self._runtime) - (set(plans) - blocked):
            self._runtime.pop(room, None)
        for room in tuple(self._persisted):
            if revisions.get(room) is None:
                self._persisted.pop(room, None)
        self._policies = tuple(policy for policy, _ in valid if policy["room"] not in blocked)
        for room, revision in inert:
            result[room] = {"phase": "unlocked", "desired": None}
            self._save(room, revision, result[room])
        for room in invalid:
            result[room] = {"phase": "invalid", "reason": "invalid_policy"}
            self._save(room, revisions[room], result[room])
        for policy, _ in valid:
            room = policy["room"]
            if room in blocked:
                # An unreadable neighbor's intent for shared hardware is unknown.
                result[room] = {"phase": "enforcing" if states[room]["locked"] else "unlocked",
                                "state": "degraded", "reason": "invalid_policy"}
                self._save(room, policy["revision"], result[room])
        self._reconcile_ready = True
        policies = {policy["room"]: policy for policy, _ in valid}
        for policy, changed_at in valid:
            mono = self.monotonic()
            room, revision = policy["room"], policy["revision"]
            if room in blocked:
                continue
            state = states[room]
            attempted = False
            for event in due_boundaries(now, policy, self._seen, not_before=changed_at):
                attempted = self._boundary(event, list(plans.values()), revisions, policies, states) or attempted
            if not self._scheduled(policies, states):
                return {"stale": True}
            if not state["locked"]:
                self._runtime.pop(room, None)
                result[room] = {"phase": "unlocked", "desired": None}
                self._save(room, revision, result[room])
                continue
            runtime = self._runtime.get(room)
            if runtime is None or runtime["key"] != state["key"]:
                runtime = self._runtime[room] = {"key": state["key"], "read_due": 0, "write_due": mono, "attempt": 0}
            if attempted:
                # Await normal routing's first report before any repair.
                runtime["write_due"] = max(runtime["write_due"], self.monotonic() + BACKOFF[0])
            if mono < runtime["read_due"]:
                continue
            plan = plans[room]
            status = {"phase": "enforcing", "desired": state["desired"]}
            with self.locks.hold([*("room:" + name for name in revisions), *plan.resources]):
                if not self._current(revisions):
                    return {"stale": True}
                try:
                    observations = self.io.read(plan.observed())
                    if not self._current(revisions):
                        return {"stale": True}
                    observed_now = utc(self.wall_clock())
                    if not self._scheduled(policies, states):
                        return {"stale": True}
                    mono = self.monotonic()
                    verification = verify(plan, observations, observed_now)
                    status.update(verification)
                    repair = repair_plan(plan, verification)
                    if conflicts(plan, list(plans.values())):
                        status.update(state="degraded", reason="shared_resource_conflict")
                        repair = []
                    if repair and mono >= runtime["write_due"] and self._current(revisions) and self._scheduled(policies, states):
                        item, value = repair[0]
                        try:
                            self.io.write(room, item, value, revision)
                            status["repair"] = item
                        except Exception:
                            status["reason"] = "dispatch_unconfirmed"
                        mono = self.monotonic()
                    if verification["state"] == "verified" and not status.get("reason"):
                        status["phase"] = "verified"
                        runtime.update(attempt=0, write_due=mono, read_due=mono + self.read_interval)
                    else:
                        self._retry(runtime, mono)
                except Exception:
                    status.update(state="unknown", reason="native_read_unavailable")
                    self._retry(runtime, self.monotonic())
            if not self._current(revisions) or not self._scheduled(policies, states):
                return {"stale": True}
            status["retry_seconds"] = max(0, runtime["write_due"] - self.monotonic())
            result[room] = status
            self._save(room, revision, status, occurrence=runtime["key"])
        return result
