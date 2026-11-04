"""Supervised policy worker with exact native observations and revision-fenced I/O."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import signal
import threading
import time
from urllib.parse import unquote

from . import AccessError

LOG = logging.getLogger(__name__)
# At most one reconnect warning per interval; suppressed ones are counted.
WARN_EVERY = 300
PRUNE_EVERY = 24 * 60 * 60


class NativeCache:
    """No GET, command acknowledgement or reconnect creates an observation.

    ItemStateUpdatedEvent is acquired when its trusted loopback SSE frame arrives.
    Apple TV additionally requires the provider's actual power_reported_at;
    repeated cached updates never advance that native acquisition timestamp.
    """

    def __init__(self, fields, *, native_power=None, clock=None, on_change=None):
        self.fields = {field.item: field for field in fields}
        self.native_power = dict(native_power or {})
        self._topics = tuple("openhab/items/" + name + "/stateupdated"
                             for name in sorted(set(self.fields) | set(self.native_power.values())))
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
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(value, str) or len(value) > 128:
            return None
        if item in self.fields and value not in {*self.fields[item].allowed, "NULL", "UNDEF"}:
            return None
        if item not in self.fields:
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
            if item not in self.fields:
                try:
                    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    at = None
                self.timestamps[item] = at
                return
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


class PolicyIO:
    def __init__(self, adapter, store, cache, house):
        """house is the contract's plain (mutable) house mapping."""
        self.adapter, self.store, self.cache = adapter, store, cache
        from oh_room_policy import expected_plan
        self.plans = {room: {choice: expected_plan(house, room, choice)
                             for choice in ("off", *spec["sources"])} for room, spec in adapter.contract.rooms.items()}

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


async def observe(transport, cache, stop, warnings=None):
    """Consume native events; the supervisor cancels to interrupt a quiet feed."""
    warnings = warnings or Warnings()
    delay = 1
    while not stop.is_set():
        iterator = transport.events(cache.topics, project=cache.project).__aiter__()
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
    house = plain(adapter.contract.house)
    fields = observed_fields(house, adapter.contract.rooms)
    import oh_integrations
    native_power = {}
    for device in house["devices"]:
        for integration in oh_integrations.REGISTRY:
            hook = getattr(integration, "native_power_observation", None)
            descriptor = hook(device) if hook else None
            if descriptor and descriptor["item"] in cache_items(fields):
                native_power[descriptor["item"]] = descriptor["reported_at"]
    cache = NativeCache(fields, native_power=native_power, clock=wall_clock)
    validate_batches(cache, adapter.transport.base)
    io = PolicyIO(adapter, store, cache, house)
    worker = Worker(house, store, io, locks=locks, wall_clock=wall_clock)
    rooms_by_item = {}
    for room, choices in io.plans.items():
        for plan in choices.values():
            for item in plan.observed():
                rooms_by_item.setdefault(item, set()).add(room)
    def changed(item):
        for room in rooms_by_item.get(item, ()):
            worker.notify(room)
    cache.on_change = changed
    stop = stop or threading.Event()
    previous = {}
    shutdown_requested = False
    def request_stop(*_):
        # Python may run a signal handler while this same thread holds an
        # Event's non-reentrant condition lock. Even Event.set() can deadlock
        # there; publish only a flag and let normal supervisor code clean up.
        nonlocal shutdown_requested
        shutdown_requested = True
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, request_stop)
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
    try:
        # Take the process lease BEFORE connecting a feed or running any writes.
        with locks.hold(["room-policy-worker"], blocking=False):
            if not shutdown_requested:
                thread.start()
            pruned = None
            while not shutdown_requested and not stop.is_set():
                if pruned is None or time.monotonic() - pruned >= PRUNE_EVERY:
                    pruned = time.monotonic()
                    maintain(store)
                worker.step()
                if not shutdown_requested and not stop.is_set():
                    worker.wait(min(5, interval))
    finally:
        stop.set()
        if thread.ident is not None:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(observer.cancel)
                except RuntimeError:
                    pass  # The observer finished and closed its loop meanwhile.
            thread.join(timeout=20)
        else:
            observer.cancel()
            loop.run_until_complete(asyncio.gather(observer, return_exceptions=True))
            loop.close()
        cache.clear()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if thread.is_alive():
            raise AccessError("Native observation task did not stop")
        close = getattr(adapter.transport, "close", None)
        if close:
            close()


def maintain(store):
    """Daily housekeeping never stops enforcement."""
    try:
        store.prune(datetime.now(timezone.utc))
    except Exception as error:
        LOG.warning("Identity housekeeping failed (%s); retrying tomorrow.", type(error).__name__)


def cache_items(fields):
    return {field.item for field in fields}


def observed_fields(house, rooms):
    """Every Field the native cache observes: each route plan's fields and the
    conditions their alternatives depend on, which are never written."""
    from oh_room_policy import expected_plan
    return [field for room, spec in rooms.items() for choice in ("off", *spec["sources"])
            for plan in (expected_plan(house, room, choice),) for field in (*plan.fields, *plan.conditions)]


def validate_batches(cache, base, pack=None):
    """A timestamp/power pair must share an ordered native stream."""
    if pack is None:
        from oh_identity.transport import native_batches
        pack = native_batches
    batches = [set(unquote(query).split(",")) for query in pack(cache.topics, base)]
    for power, stamp in cache.native_power.items():
        pair = {"openhab/items/" + item + "/stateupdated" for item in (power, stamp)}
        if not any(pair <= batch for batch in batches):
            raise AccessError("Native power provenance would cross unordered stream batches")
