"""Supervised policy worker with exact native observations and revision-fenced I/O."""

import asyncio
from datetime import datetime, timezone
import json
import signal
import threading
from urllib.parse import unquote

from . import AccessError


class NativeCache:
    """No GET, command acknowledgement or reconnect creates an observation.

    ItemStateUpdatedEvent is acquired when its trusted loopback SSE frame arrives.
    Apple TV additionally requires the provider's actual power_reported_at;
    repeated cached updates never advance that native acquisition timestamp.
    """

    def __init__(self, fields, *, native_power=None, clock=None):
        self.fields = {field.item: field for field in fields}
        self.native_power = dict(native_power or {})
        self.timestamps = {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.values = {}
        self.lock = threading.Lock()
        self.connected = False

    def clear(self):
        with self.lock:
            self.values.clear()
            self.timestamps.clear()
            self.connected = False

    @property
    def topics(self):
        return ["openhab/items/" + name + "/stateupdated" for name in sorted(set(self.fields) | set(self.native_power.values()))]

    def project(self, event):
        if not isinstance(event, dict) or event.get("type") != "ItemStateUpdatedEvent" or event.get("topic") not in self.topics:
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
            if field.native_power:
                # The provider emits the exact acquisition timestamp BEFORE
                # each power update. Consume it once; never reuse poll receipt.
                at = self.timestamps.pop(self.native_power.get(item), None)
                if at is None:
                    self.values.pop(item, None)
                else:
                    self.values[item] = Observation(value, at, basis="native_power")
                return
            self.values[item] = Observation(value, self.clock(), basis="native")

    def read(self, items):
        with self.lock:
            return {item: self.values[item] for item in items if item in self.values}


class PolicyIO:
    def __init__(self, adapter, store, cache):
        self.adapter, self.store, self.cache = adapter, store, cache
        from oh_room_policy import expected_plan
        from oh_identity.runtime import plain
        self.plans = {room: {choice: expected_plan(plain(adapter.contract.house), room, choice)
                             for choice in ("off", *spec["sources"])} for room, spec in adapter.contract.rooms.items()}

    def read(self, items):
        return self.cache.read(items)

    def write(self, room, item, value, revision):
        if self.store.current_revision(room) != revision:
            raise AccessError("Policy revision changed")
        plans = self.plans.get(room, {})
        allowed = any(item == plan.source_item and value == plan.target or
                      any(field.item == item and field.value == value for field in plan.fields)
                      for plan in plans.values())
        if not allowed:
            raise AccessError("Worker command is not a compiled route field")
        # Worker holds the shared room/resource file locks and rechecks schedule
        # occurrence immediately before this call. No database txn spans HTTP.
        self.adapter.transport.command(item, value)


async def observe(transport, cache, stop):
    delay = 1
    while not stop.is_set():
        iterator = transport.events(cache.topics, project=cache.project).__aiter__()
        task = None
        try:
            while not stop.is_set():
                if task is None:
                    task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait([task], timeout=0.25)
                if done:
                    cache.accept(task.result())
                    task = None
                    delay = 1
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop.is_set():
                return
        finally:
            cache.clear()
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await iterator.aclose()
        for _ in range(delay * 4):
            if stop.is_set():
                return
            await asyncio.sleep(0.25)
        delay = min(30, delay * 2)


def run(config_path, *, stop=None, interval=1, wall_clock=None):
    from .service import configured
    from oh_identity.policy_store import Store
    from oh_identity.runtime import plain
    from oh_room_policy import Worker, expected_plan
    config, adapter, locks = configured(config_path, tls=False)
    store = Store()
    house = plain(adapter.contract.house)
    fields = [field for room, spec in adapter.contract.rooms.items() for choice in ("off", *spec["sources"])
              for field in expected_plan(house, room, choice).fields]
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
    io = PolicyIO(adapter, store, cache)
    worker = Worker(house, store, io, locks=locks, wall_clock=wall_clock)
    stop = stop or threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
    thread = threading.Thread(target=lambda: asyncio.run(observe(adapter.transport, cache, stop)), daemon=True)
    try:
        # Take the process lease BEFORE connecting a feed or running any writes.
        with locks.hold(["room-policy-worker"], blocking=False):
            thread.start()
            while not stop.is_set():
                worker.step()
                stop.wait(max(0.1, interval))
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(timeout=20)
        cache.clear()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def cache_items(fields):
    return {field.item for field in fields}


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
