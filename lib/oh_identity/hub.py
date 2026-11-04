"""One upstream openHAB event subscription, shared by every client stream.

openHAB 5.2's SseResource tests every bus event against every topic filter of
every SSE subscriber, compiling each filter as a regular expression, so its
cost grows with filters times subscribers. The gateway instead holds one
upstream subscription per process with one wildcard filter per verb
(upstream_filters). The transport drops every event outside the contract's
public topics before its payload is parsed; the rest are projected once
(topics.projected), cached as each Item's latest projected value and fanned
out in process to the client streams (streams.stream) whose view has them.

A client stream joins a view. Its first frame is a snapshot of the view's
Items from the cache, `{"type":"snapshot","view":...,"states":{item: value}}`;
the view's events follow through a queue of at most QUEUE frames. A client
that falls further behind loses what it has not read and is sent one fresh
snapshot in its place, so no client grows the gateway's memory.

The upstream opens with the first client stream and closes LINGER seconds
after the last one leaves. Each (re)connection reads every cached Item again
in one request (the transport's states()) before any client is served from
the cache; an event that arrives during that read is newer and wins. While
the upstream is down, client streams stay open and receive
`{"type":"unavailable"}`; every one gets a fresh snapshot once it is back.
Reconnection backs off through BACKOFF.

Observers (observers, notify) see stream and upstream lifecycle events, for
logs and metrics; EventHub.status() reports the hub's counters.
"""

import asyncio
import json
import logging
import time
import weakref
from collections import deque

from . import topics
from .runtime import project

LOG = logging.getLogger(__name__)
#: Frames a client stream may fall behind before it is resynchronized.
QUEUE = 256
#: Seconds the upstream stays open after the last client stream ends, so a
#: reload or a tab shown again reuses it.
LINGER = 60
#: Seconds between upstream connection attempts, the last one repeating.
BACKOFF = (1, 2, 5, 10, 30)
UNAVAILABLE = b'data: {"type":"unavailable"}\n\n'
#: Callables observer(kind, fields): "stream_open", "stream_close",
#: "stream_refused", "stream_overflow", "upstream_open", "upstream_close" and
#: "resync". fields carry no credential, address or Item value.
observers = []
_hubs = weakref.WeakKeyDictionary()


def notify(kind, fields):
    for observer in list(observers):
        try:
            observer(kind, dict(fields))
        except Exception as error:
            LOG.warning("Event stream observer failed (%s).", type(error).__name__)


def upstream_filters(public):
    """One wildcard openHAB filter per verb the public topics use. Each extra
    filter costs openHAB a regular expression compilation per bus event, and
    an exact list of a site's Items is hundreds of filters; the transport
    discards the wildcard's other Items before reading their payloads."""
    verbs = {topic.rsplit("/", 1)[1] for topic in public}
    return ["openhab/items/*/" + verb for verb in ("stateupdated", "statechanged", "command") if verb in verbs]


def encode(value):
    return ("data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()


class Subscriber:
    """One client stream's place in a view: a bounded queue of encoded frames."""

    def __init__(self, hub, view, meta):
        self.hub, self.view, self.meta = hub, view, meta
        self.frames = deque()
        self.wake = asyncio.Event()
        self.overflows = 0

    def push(self, data):
        if len(self.frames) >= self.hub.queue:
            # What the client has not read is superseded by one snapshot,
            # which already holds every value those frames carried.
            self.frames.clear()
            self.overflows += 1
            self.hub.counters["overflows"] += 1
            notify("stream_overflow", self.meta)
            data = self.hub.snapshot(self.view) if self.hub.ready else UNAVAILABLE
        self.frames.append(data)
        self.wake.set()

    async def next(self, timeout):
        """The next encoded frame, or None after timeout seconds without one."""
        if not self.frames:
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except asyncio.TimeoutError:
                return None
        self.hub.counters["frames"] += 1
        return self.frames.popleft()


class EventHub:
    """The process's upstream subscription, latest-value cache and fan-out,
    bound to the event loop that serves the gateway."""

    def __init__(self, adapter, *, queue=None, linger=None, backoff=None):
        self.contract, self.transport = adapter.contract, adapter.transport
        self.views = topics.views(self.contract)
        self.items = {name: topics.state_items(view) for name, view in self.views.items()}
        self.public = frozenset().union(*self.views.values())
        self.cached = frozenset().union(*self.items.values())
        self.filters = upstream_filters(self.public)
        self.queue = QUEUE if queue is None else queue
        self.linger = LINGER if linger is None else linger
        self.backoff = BACKOFF if backoff is None else backoff
        self.loop = asyncio.get_running_loop()
        self.subscribers = {name: set() for name in self.views}
        self.values, self.version, self._snapshots = {}, 0, {}
        self.ready, self.touched, self.task, self._idle = False, None, None, None
        self.counters = dict.fromkeys(("upstream_connects", "upstream_failures", "resyncs", "events", "frames", "overflows"), 0)

    def status(self):
        """Counters and gauges for metrics: client streams per view, whether
        the upstream is serving, connecting or closed, and running totals."""
        state = "serving" if self.ready else "connecting" if self.task and not self.task.done() else "closed"
        return {"streams": {name: len(each) for name, each in self.subscribers.items()}, "upstream": state, **self.counters}

    # ---- client streams ----------------------------------------------------

    def join(self, view, meta=None):
        subscriber = Subscriber(self, view, meta or {"view": view})
        self.subscribers[view].add(subscriber)
        if self._idle:
            self._idle.cancel()
            self._idle = None
        if not self.filters:
            self.ready = True  # Nothing public: an empty snapshot, no upstream.
        if self.ready:
            subscriber.push(self.snapshot(view))
        elif self.task is None or self.task.done():
            self.task = self.loop.create_task(self._run())
        return subscriber

    def leave(self, subscriber):
        self.subscribers[subscriber.view].discard(subscriber)
        if not any(self.subscribers.values()) and self.task and not self._idle:
            self._idle = self.loop.call_later(self.linger, self._close_if_idle)

    def _close_if_idle(self):
        self._idle = None
        if not any(self.subscribers.values()) and self.task:
            self.task.cancel()

    def snapshot(self, view):
        cached = self._snapshots.get(view)
        if cached and cached[0] == self.version:
            return cached[1]
        states = {item: self.values[item] for item in sorted(self.items[view]) if item in self.values}
        data = encode({"type": "snapshot", "view": view, "states": states})
        self._snapshots[view] = (self.version, data)
        return data

    def _broadcast(self, data_for):
        for view, subscribers in self.subscribers.items():
            if subscribers:
                data = data_for(view)
                for subscriber in list(subscribers):
                    subscriber.push(data)

    # ---- upstream ----------------------------------------------------------

    def projection(self, event):
        """The transport's projection: runs before anything is buffered."""
        value = topics.projected(self.contract, self.public, event)
        if value is None:
            return None
        topic, item, verb, clean = value
        data = encode({"topic": topic, "type": topics.TYPES[verb], "payload": json.dumps(clean, separators=(",", ":"))})
        return topic, item, verb, clean["value"], data

    def receive(self, frame):
        topic, item, verb, value, data = frame
        self.counters["events"] += 1
        if verb != "command":
            if self.touched is not None:
                self.touched.add(item)
            if self.values.get(item) != value:
                self.values[item] = value
                self.version += 1
        if not self.ready:
            return
        for view, subscribers in self.subscribers.items():
            if subscribers and topic in self.views[view]:
                for subscriber in list(subscribers):
                    subscriber.push(data)

    def _read_all(self):
        """Every cached Item's state, projected; runs in a worker thread."""
        names = sorted(self.cached)
        bulk = getattr(self.transport, "states", None)
        if callable(bulk):
            raw = bulk(names)
        else:
            raw = {}
            for name in names:
                try:
                    raw[name] = self.transport.read(name)
                except Exception:
                    continue
        return {name: project(name, raw[name], self.contract.states[name]) for name in names if name in raw}

    def _synced(self, values):
        for item, value in values.items():
            if item not in self.touched:
                self.values[item] = value
        self.touched = None
        self.version += 1
        self.ready = True
        self.counters["resyncs"] += 1
        self._broadcast(self.snapshot)

    def _lost(self):
        served, self.ready, self.touched = self.ready, False, None
        self.values.clear()
        self.version += 1
        if served:
            self._broadcast(lambda view: UNAVAILABLE)

    async def _session(self):
        connected = self.loop.create_future()

        def opened():
            if not connected.done():
                connected.set_result(None)

        async def pump():
            async for frame in self.transport.subscribe(self.filters, project=self.projection, opened=opened):
                self.receive(frame)
            raise ConnectionError("Runtime event stream ended")

        reader, reading = asyncio.ensure_future(pump()), None
        try:
            await asyncio.wait([reader, connected], return_when=asyncio.FIRST_COMPLETED)
            if reader.done():
                reader.result()
            self.counters["upstream_connects"] += 1
            notify("upstream_open", {"filters": len(self.filters)})
            self.touched = set()
            started = time.monotonic()
            reading = asyncio.ensure_future(asyncio.to_thread(self._read_all))
            await asyncio.wait([reader, reading], return_when=asyncio.FIRST_COMPLETED)
            if reader.done():
                reader.result()
            values = reading.result()
            self._synced(values)
            notify("resync", {"items": len(values), "seconds": round(time.monotonic() - started, 3)})
            await reader
        finally:
            for task in (reader, reading):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (reader, reading) if task), return_exceptions=True)

    async def _run(self):
        attempt = 0
        try:
            while any(self.subscribers.values()) or self._idle:
                try:
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.counters["upstream_failures"] += 1
                    attempt = 0 if self.ready else attempt
                    notify("upstream_close", {"error": type(error).__name__})
                self._lost()
                await asyncio.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
                attempt += 1
        finally:
            self._lost()


def hub_for(adapter):
    """The adapter's hub on the running event loop, created on first use."""
    loop = asyncio.get_running_loop()
    hub = _hubs.get(adapter)
    if hub is None or hub.loop is not loop:
        hub = _hubs[adapter] = EventHub(adapter)
    return hub
