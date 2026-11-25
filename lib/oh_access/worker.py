"""Supervised policy worker with exact native observations and revision-fenced I/O."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import signal
import threading
import time

from . import AccessError, metrics

LOG = logging.getLogger(__name__)
# At most one reconnect warning per interval; suppressed ones are counted.
WARN_EVERY = 300
PRUNE_EVERY = 24 * 60 * 60
#: Seconds from a stop request within which the worker has joined its
#: threads: inside the access units' TimeoutStopSec (30), with room for the
#: rest of its shutdown.
STOP_SECONDS = 27
#: Seconds of those it waits for the music thread at most: the one request
#: it may have in flight (oh_music.client.SLOW) and a moment more.
MUSIC_STOP = 21


class Shutdown:
    """When the worker was asked to stop, and what is left of STOP_SECONDS
    from then for each thread it joins. request() is all a signal handler
    calls: it may take no lock, so it records a time and a flag. watch()
    calls each wake-up once a request comes, or the stop event is set by
    another, within poll seconds: the music thread stops and the loop wakes
    while a step may still run. The stop event itself, which ends the
    observer and clears what it observed, is the loop's to set once its
    step has ended."""

    def __init__(self, stop, *, clock=time.monotonic, poll=1.0):
        self.stop, self.clock, self.poll = stop, clock, poll
        self.requested, self.deadline = False, None

    def request(self, *_):
        if self.deadline is None:
            self.deadline = self.clock() + STOP_SECONDS
        self.requested = True

    def left(self):
        """Seconds left to join threads in."""
        return STOP_SECONDS if self.deadline is None else max(0.0, self.deadline - self.clock())

    def watch(self, *wakes):
        def watching():
            while not self.requested and not self.stop.wait(self.poll):
                pass
            self.request()
            for wake in wakes:
                wake()
        thread = threading.Thread(target=watching, daemon=True, name="shutdown")
        thread.start()
        return thread


class NativeCache:
    """No GET, command acknowledgement or reconnect creates an observation.

    ItemStateUpdatedEvent is acquired when its trusted loopback SSE frame arrives.
    Apple TV additionally requires the provider's actual power_reported_at;
    repeated cached updates never advance that native acquisition timestamp.

    A field keeps whatever its Item reports, as verify() compares it
    (oh_room_policy.canonical): a value no route uses is a mismatch, while
    NULL, UNDEF or a non-string or over-long value is None, Unknown.
    """

    def __init__(self, fields, *, native_power=None, lifecycle_items=None, clock=None, on_change=None):
        self.fields = {field.item: field for field in fields}
        self.native_power = dict(native_power or {})
        self.lifecycle_items = frozenset(lifecycle_items or ())
        self._topics = tuple("openhab/items/" + name + "/stateupdated"
                             for name in sorted(set(self.fields) | set(self.native_power.values()) | self.lifecycle_items))
        self._topic_set = frozenset(self._topics)
        self.timestamps = {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.values = {}
        self.lock = threading.Lock()
        self.connected = False
        self.on_change = on_change

    def clear(self):
        with self.lock:
            changed = tuple(self.values)
            self.values.clear()
            self.timestamps.clear()
            self.connected = False
        if self.on_change:
            for item in changed:
                self.on_change(item)

    @property
    def topics(self):
        return self._topics

    def project(self, event):
        if not isinstance(event, dict) or event.get("type") != "ItemStateUpdatedEvent" or event.get("topic") not in self._topic_set:
            return None
        item = event["topic"].split("/")[2]
        try:
            payload = json.loads(event.get("payload"))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("value")
        if item in self.fields:
            from oh_room_policy import canonical
            return item, canonical(value)
        if item in self.lifecycle_items:
            if not isinstance(value, str) or value in ("NULL", "UNDEF", "") or len(value) > 4096:
                return item, None
            return item, value
        if not isinstance(value, str) or len(value) > 128:
            return None
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
        except ValueError:
            if value not in {"NULL", "UNDEF"}:
                return None
        return item, value

    def accept(self, frame):
        from oh_room_policy import Observation
        item, value = frame
        changed = False
        with self.lock:
            self.connected = True
            if item in self.lifecycle_items:
                previous = self.values.get(item)
                if value is None:
                    self.values.pop(item, None)
                else:
                    self.values[item] = Observation(value, self.clock(), basis="native")
                current = self.values.get(item)
                changed = ((previous is None) != (current is None)
                           or previous is not None and current is not None and previous.value != current.value)
            elif item not in self.fields:
                try:
                    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    at = None
                self.timestamps[item] = at
                return
            else:
                field = self.fields[item]
                previous = self.values.get(item)
                if field.native_power:
                    # The provider emits the exact acquisition timestamp BEFORE
                    # each power update. Consume it once; never reuse poll receipt.
                    at = self.timestamps.pop(self.native_power.get(item), None)
                    if at is None:
                        self.values.pop(item, None)
                    else:
                        self.values[item] = Observation(value, at, basis="native_power")
                else:
                    self.values[item] = Observation(value, self.clock(), basis="native")
                current = self.values.get(item)
                changed = ((previous is None) != (current is None)
                           or previous is not None and current is not None and previous.value != current.value)
        # Same-value reports retain truthful acquisition times without waking
        # the policy loop for every periodic device status update.
        if changed and self.on_change:
            self.on_change(item)

    def read(self, items):
        with self.lock:
            return {item: self.values[item] for item in items if item in self.values}


def compile_plans(house, rooms):
    """Compile every room's route plans once, building routing.table(house) a single time."""
    from oh_room_policy.plan import compile_plans as _compile_plans
    return _compile_plans(house, rooms)


class PolicyIO:
    def __init__(self, adapter, store, cache, house, *, plans=None):
        """house is the contract's plain (mutable) house mapping."""
        self.adapter, self.store, self.cache = adapter, store, cache
        self.plans = compile_plans(house, adapter.contract.rooms) if plans is None else plans

    def read(self, items):
        return self.cache.read(items)

    def source(self, room, item):
        """The room's current Source Item state, only to avoid re-sending a
        source that is already current. It is never an observation."""
        if not any(plan.source_item == item for plan in self.plans.get(room, {}).values()):
            raise AccessError("Not a compiled room Source Item")
        return str(self.adapter.transport.read(item)).strip()

    def write(self, room, item, value, revision, origin="repair"):
        """Send one compiled route command; return its trail ID. origin is
        schedule (a boundary) or repair. A failure carries the ID as
        trail_id, so the worker's own line can name it."""
        if self.store.current_revision(room) != revision:
            raise AccessError("Policy revision changed")
        plans = self.plans.get(room, {})
        allowed = any(item == plan.source_item and value == plan.target or
                      any(field.item == item and field.value == value for field in plan.fields)
                      for plan in plans.values())
        if not allowed:
            raise AccessError("Worker command is not a compiled route field")
        from oh_identity.trail import Command, Origin
        action = "source" if any(item == plan.source_item for plan in plans.values()) else "field"
        record = Command(Origin(origin), None, room=room, action=action, item=item, value=value)
        # Worker holds the shared room/resource file locks and rechecks schedule
        # occurrence immediately before this call. No database txn spans HTTP.
        try:
            self.adapter.transport.command(item, value, source=record.source)
        except Exception as error:
            record.failed(error)
            error.trail_id = record.id
            raise
        record.finish()
        return record.id


class Warnings:
    """Rate-limited reconnect warnings naming only the exception class."""

    def __init__(self, every=WARN_EVERY, clock=time.monotonic):
        self.every, self.clock = every, clock
        self.last, self.suppressed = None, 0

    def failed(self, error):
        now = self.clock()
        if self.last is not None and now - self.last < self.every:
            self.suppressed += 1
            return
        LOG.warning("Native observation stream failed (%s); reconnecting. %d earlier failure(s) not logged.",
                    type(error).__name__, self.suppressed)
        self.last, self.suppressed = now, 0


#: The worker's one native subscription. openHAB tests every filter of every
#: subscriber against each event, so one wildcard costs it less than an exact
#: list; NativeCache.project keeps only its own topics, before any payload is
#: read. One stream also keeps each timestamp ahead of its power update.
OBSERVED = ["openhab/items/*/stateupdated"]


class Telemetry:
    """The policy worker's metrics (oh_access.metrics), from what it already
    does: boundaries through its store, everything else from each pass's
    result. A room a pass did not reach keeps its last state. The worker's
    rooms are routing zones, so their metrics label them `zone`. A locked
    zone's reconcile age grows until a pass verifies it, while its lock
    cannot be held too; only unlocking removes it."""

    def __init__(self, rooms, *, clock=time.time, monotonic=time.monotonic):
        self.clock, self.monotonic = clock, monotonic
        self.states = dict.fromkeys(rooms, "unlocked")
        self.verified = {}
        self.records = ()
        # Every zone's states exist from the first write; none is 1 before a pass.
        for room in self.states:
            for state in metrics.ROOM_STATES:
                metrics.POLICY_ROOM_STATE.labels(room, state).set(0)

    def store(self, store):
        telemetry = self

        class Metered:
            def __getattr__(self, name):
                return getattr(store, name)

            def policies(self):
                telemetry.records = store.policies()
                return telemetry.records

            def claim_boundary(self, room, revision, key, at):
                claimed = store.claim_boundary(room, revision, key, at)
                if claimed:
                    lag = (datetime.fromtimestamp(telemetry.clock(), timezone.utc) - at).total_seconds()
                    metrics.POLICY_LAG.observe(max(0.0, lag))
                return claimed

            def finish_boundary(self, room, revision, key, outcome):
                store.finish_boundary(room, revision, key, outcome)
                reason = outcome.get("reason") if isinstance(outcome, dict) else None
                metrics.POLICY_BOUNDARIES.labels(reason if reason in metrics.BOUNDARY_OUTCOMES else "other").inc()
        return Metered()

    def _manual(self, room):
        for record in self.records:
            config = record.config if isinstance(record.config, dict) else {}
            if config.get("room") == room:
                return config.get("manual_lock") is not None
        return False

    def _state(self, room, status):
        """A room's policy state from its pass status. Unavailable: a policy
        it cannot read, or a lock it cannot hold now: a conflict over shared
        hardware (an unreadable neighbour's, or another lock's), openHAB not
        answering its read, or a drift not yet repaired. A reading too old to
        trust (a device that reports only on change) leaves the lock held:
        nothing is wrong, and its reconcile age shows how long since it was
        checked. So does a passive endpoint, which the worker never controls;
        an unlocked room has nothing to enforce."""
        phase = status.get("phase")
        if phase == "invalid":
            return "unavailable"
        if phase not in ("enforcing", "verified"):
            return "unlocked"
        if status.get("mismatched") or status.get("reason") in (
                "invalid_policy", "shared_resource_conflict", "native_read_unavailable"):
            return "unavailable"
        return "manual" if self._manual(room) else "scheduled"

    @staticmethod
    def _verified(status):
        """Whether a pass verified the lock: every field it controls read
        back as locked. A passive endpoint has no field, so it does not
        stand in the way."""
        return status.get("phase") == "verified"

    def stepped(self, result):
        now = self.monotonic()
        for room, status in result.items() if isinstance(result, dict) else ():
            if room not in self.states or not isinstance(status, dict):
                continue
            state = self._state(room, status)
            if state != "unlocked" and self.states[room] == "unlocked":
                self.verified[room] = now  # A new lock is measured from its start.
            self.states[room] = state
            if self._verified(status):
                self.verified[room] = now
            if "repair" in status:
                metrics.POLICY_REPAIRS.labels("sent").inc()
            if status.get("reason") == "dispatch_unconfirmed":
                metrics.POLICY_REPAIRS.labels("unconfirmed").inc()
            if status.get("reason") == "native_read_unavailable":
                metrics.policy_error("read")
        for state in metrics.ROOM_STATES:
            metrics.POLICY_ROOMS.labels(state).set(sum(each == state for each in self.states.values()))
        for room, state in self.states.items():
            for each in metrics.ROOM_STATES:
                metrics.POLICY_ROOM_STATE.labels(room, each).set(1 if each == state else 0)
            # A lock that cannot be held now keeps its age, which grows
            # until a pass verifies it again.
            if state != "unlocked":
                metrics.POLICY_RECONCILE_AGE.labels(room).set(max(0.0, now - self.verified.get(room, now)))
            else:
                self.verified.pop(room, None)
                try:
                    metrics.POLICY_RECONCILE_AGE.remove(room)
                except KeyError:
                    pass
        metrics.POLICY_LOOP.set(self.clock())


async def observe(transport, cache, stop, warnings=None):
    """Consume native events; the supervisor cancels to interrupt a quiet feed."""
    warnings = warnings or Warnings()
    delay = 1
    while not stop.is_set():
        iterator = transport.subscribe(OBSERVED, project=cache.project).__aiter__()
        try:
            async for frame in iterator:
                if stop.is_set():
                    return
                cache.accept(frame)
                delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if stop.is_set():
                return
            warnings.failed(error)
            metrics.policy_error("stream")
        finally:
            cache.clear()
            await iterator.aclose()
        if stop.is_set():
            return
        await asyncio.sleep(delay)
        delay = min(30, delay * 2)


def run(config_path, *, stop=None, interval=5, wall_clock=None):
    from .files import require_service_account
    require_service_account()
    from .service import configured
    # This is a fresh service process: configure Django before importing any
    # model-backed store or runtime adapter, even when another process bootstrapped.
    config, adapter, locks = configured(config_path, tls=False)
    from .service import check_schema, rebase_timezone
    check_schema()
    rebase_timezone(adapter, config["timezone"])
    from oh_identity.policy_store import Store
    from oh_identity.runtime import plain
    from oh_room_policy import Worker
    store = Store()
    telemetry = Telemetry(adapter.contract.rooms)
    house = plain(adapter.contract.house)
    plans = compile_plans(house, adapter.contract.rooms)
    fields = observed_fields(house, adapter.contract.rooms, plans=plans)
    lifecycle_items = {plan.lifecycle_item for choices in plans.values() for plan in choices.values() if plan.lifecycle_item}
    from oh_room_policy.plan import native_power_observations
    observed = cache_items(fields)
    native_power = {item: reported_at for item, reported_at in native_power_observations(house["devices"]).items()
                    if item in observed}
    cache = NativeCache(fields, native_power=native_power, lifecycle_items=lifecycle_items, clock=wall_clock)
    io = PolicyIO(adapter, store, cache, house, plans=plans)
    worker = Worker(house, telemetry.store(store), io, locks=locks, wall_clock=wall_clock)
    rooms_by_item = {}
    for room, choices in io.plans.items():
        for plan in choices.values():
            for item in plan.observed():
                rooms_by_item.setdefault(item, set()).add(room)
    def changed(item):
        for room in rooms_by_item.get(item, ()):
            worker.notify(room)
    cache.on_change = changed
    supervise(worker, adapter, config, locks=locks, store=store, telemetry=telemetry, cache=cache, stop=stop,
              interval=interval)


def supervise(worker, adapter, config, *, locks, store, telemetry, cache, stop=None, interval=5):
    """Run a built worker until it is asked to stop: under the worker lease,
    with the native feed observed into cache and the music thread
    (music_running), each stopped and joined within STOP_SECONDS of the
    request (Shutdown)."""
    stop = stop or threading.Event()
    previous = {}
    # Python may run a signal handler while this same thread holds an
    # Event's non-reentrant condition lock. Even Event.set() can deadlock
    # there; the handler publishes only a flag and a time (Shutdown.request).
    shutdown = Shutdown(stop)
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, shutdown.request)
    loop = asyncio.new_event_loop()
    observer = loop.create_task(observe(adapter.transport, cache, stop))
    def listen():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(observer)
        except asyncio.CancelledError:
            pass
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
    thread = threading.Thread(target=listen, daemon=True)
    textfile = None

    def cancel_observer():
        if thread.ident is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(observer.cancel)
            except RuntimeError:
                pass  # The observer finished and closed its loop meanwhile.
    # The step that is running when a stop is asked for still ends, with what
    # the observer saw (its rooms' locks are held by the gateway for one
    # command at most); the music thread, which shares nothing with it, stops
    # at once.
    music_stop = threading.Event()
    shutdown.watch(music_stop.set, worker.wake)
    try:
        # Take the process lease BEFORE connecting a feed or running any writes.
        with locks.hold(["room-policy-worker"], blocking=False):
            if not shutdown.requested:
                thread.start()
            with music_running(config["state"], adapter.contract.music, music_stop, start=not shutdown.requested,
                               left=shutdown.left):
                textfile = metrics.start(metrics.WORKER, metrics.WORKER_FILE)
                pruned = None
                while not shutdown.requested and not stop.is_set():
                    if pruned is None or time.monotonic() - pruned >= PRUNE_EVERY:
                        pruned = time.monotonic()
                        maintain(store)
                    telemetry.stepped(worker.step())
                    if not shutdown.requested and not stop.is_set():
                        worker.wait(min(5, interval))
                # The step has ended: the observer stops while the music thread is joined.
                stop.set()
                cancel_observer()
    finally:
        shutdown.request()
        stop.set()
        cancel_observer()
        if thread.ident is not None:
            thread.join(timeout=shutdown.left())
        else:
            observer.cancel()
            loop.run_until_complete(asyncio.gather(observer, return_exceptions=True))
            loop.close()
        cache.clear()
        if textfile:
            textfile.stop(timeout=shutdown.left())
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if thread.is_alive():
            raise AccessError("Native observation task did not stop")
        close = getattr(adapter.transport, "close", None)
        if close:
            close()


@contextmanager
def music_running(state, music, stop, *, start=True, left=lambda: MUSIC_STOP):
    """For a host with music, the thread that holds the site's claim on
    Music Assistant and keeps its configuration (oh_music.provision): the
    worker is the site's one supervised background process and shares the
    gateway's private state, where the claim is stored; the gateway only
    reads it. The block holds the worker lease, and the thread is stopped
    and joined before the block ends, so that no pass runs once another
    worker may hold the lease: for MUSIC_STOP seconds at most, and no more
    than left() (Shutdown.left)."""
    if music is None:
        yield None
        return
    from oh_identity.runtime import plain
    from oh_music import provision
    thread = threading.Thread(target=provision.run, args=(state, plain(music), stop), daemon=True, name="music")
    if start:
        thread.start()
    try:
        yield thread
    finally:
        if thread.ident is not None:
            stop.set()
            # The music client makes no request once stop is set: the thread
            # ends when the one in flight, if any, is answered or times out.
            thread.join(timeout=min(MUSIC_STOP, left()))
            if thread.is_alive():
                LOG.warning("The music thread did not stop; it ends with the process.")


def maintain(store):
    """Daily housekeeping never stops enforcement."""
    try:
        store.prune(datetime.now(timezone.utc))
    except Exception as error:
        LOG.warning("Identity housekeeping failed (%s); retrying tomorrow.", type(error).__name__)
        metrics.policy_error("housekeeping")


def cache_items(fields):
    return {field.item for field in fields}


def observed_fields(house, rooms, *, plans=None):
    """Every Field the native cache observes: each route plan's fields and the
    conditions their alternatives depend on, which are never written."""
    compiled = compile_plans(house, rooms) if plans is None else plans
    return [field for choices in compiled.values() for plan in choices.values()
            for field in (*plan.fields, *plan.conditions)]
