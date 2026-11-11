"""Prometheus metrics of the access services, as node_exporter textfiles.

No access service opens a metrics listener, and the gateway never serves
/metrics on its LAN-facing TLS listener. Each service keeps its own
registry and, while it runs, rewrites <THERMITE_TEXTFILE_DIR>/<file>.prom
every WRITE_SECONDS: a new file renamed over the old one, mode 0640, so a
scrape never reads half a file. node_exporter's textfile collector serves
them (oh_metrics.services()); node_textfile_mtime_seconds shows a writer
that stopped. Without THERMITE_TEXTFILE_DIR (tests, a hand-run service)
nothing is written.

Every label value is finite: route and status classes, problem codes from
oh_identity.auth.PROBLEMS, client kinds, HTTP versions, outcome and stage
names chosen here, and routing zone IDs (the policy worker's rooms) from
the compiled house. No path, peer address, credential or free text becomes
a label.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

import prometheus_client
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from . import TEXTFILE_ENVIRONMENT as ENVIRONMENT
from .guest import SCOPE as GUEST_SCOPE

LOG = logging.getLogger(__name__)
WRITE_SECONDS = 10
#: Textfile names, after the units that write them (oh_access.staging).
GATEWAY_FILE = "thermite-access"
WORKER_FILE = "thermite-access-worker"
CALLBACKS_FILE = "thermite-access-callbacks"

# A textfile is one snapshot; _created samples would only add series.
prometheus_client.disable_created_metrics()

GATEWAY = CollectorRegistry(auto_describe=True)
WORKER = CollectorRegistry(auto_describe=True)
CALLBACKS = CollectorRegistry(auto_describe=True)

# ---------------------------------------------------------------- gateway

#: The HTTPS listener, and the opt-in plain-HTTP Guest listener (oh_access.guest).
LISTENERS = ("https", "guest")
ROUTES = ("command", "policy", "locks", "items", "events", "media", "asset", "auth", "admin", "other")
CODE_CLASSES = ("2xx", "3xx", "4xx", "5xx", "aborted")
REQUEST_BUCKETS = (.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)
UPSTREAM_KINDS = ("read", "command", "group", "media")
UPSTREAM_OUTCOMES = ("ok", "http_error", "timeout", "pool_timeout", "error")
HTTP_VERSIONS = ("1.1", "2")
#: The event-stream views (oh_identity.streams.VIEWS).
VIEWS = ("remote", "home", "pair")
SSE_LIMITS = ("peer", "total")
#: How an event stream ended (oh_identity.streams.closed): its client went
#: away or the gateway stopped, its credential was revoked, or it failed.
SSE_ENDS = ("client", "revoked", "error")
SSE_BUCKETS = (1, 10, 60, 300, 900, 3600, 4 * 3600, 12 * 3600)
#: The shared runtime subscription (oh_identity.hub.EventHub.status).
UPSTREAM_STATES = ("serving", "connecting", "closed")

IN_FLIGHT = Gauge("thermite_gateway_requests_in_flight", "Gateway HTTP requests (HTTP/2 streams) being served.",
                  ["route"], registry=GATEWAY)
REQUESTS = Counter("thermite_gateway_requests", "Gateway HTTP requests by listener, route class and status "
                   "class; aborted ended before a response started.", ["listener", "route", "code_class"],
                   registry=GATEWAY)
REQUEST_SECONDS = Histogram("thermite_gateway_request_seconds", "Seconds from a request's arrival to its "
                            "response start.", ["route"], buckets=REQUEST_BUCKETS, registry=GATEWAY)
PROBLEMS = Counter("thermite_gateway_problems", "Problem answers by their fixed code (oh_identity.auth.PROBLEMS).",
                   ["code"], registry=GATEWAY)
UPSTREAM_SECONDS = Histogram("thermite_gateway_upstream_request_seconds", "Seconds the gateway's runtime requests "
                             "took, by kind and outcome.", ["kind", "outcome"], buckets=REQUEST_BUCKETS,
                             registry=GATEWAY)
SSE_STREAMS = Gauge("thermite_gateway_sse_streams", "Open client event streams by view and HTTP version.",
                    ["view", "http_version"], registry=GATEWAY)
SSE_PEER_MAX = Gauge("thermite_gateway_sse_peer_streams_max", "The most open event streams any one peer holds, "
                     "by HTTP version.", ["http_version"], registry=GATEWAY)
SSE_REFUSED = Counter("thermite_gateway_sse_refused", "Event streams refused at the per-peer or gateway stream "
                      "limit.", ["limit"], registry=GATEWAY)
SSE_SECONDS = Histogram("thermite_gateway_sse_stream_seconds", "Event stream lifetimes, by how they ended.", ["end"],
                        buckets=SSE_BUCKETS, registry=GATEWAY)

#: Client telemetry (oh_identity.telemetry).
CLIENTS = ("web", "tui")
CLIENT_KINDS = ("policy_timeout", "locks_timeout", "command_timeout", "stream_error", "stream_refused",
                "snapshot_timeout")
CLIENT_PROTOCOLS = ("h2", "http/1.1", "h3", "unknown")
CLIENT_BUCKETS = (.1, .25, .5, 1, 2, 4, 8, 16)
CLIENT_EVENTS = Counter("thermite_client_events", "Client-side give-ups and stream failures clients reported.",
                        ["client", "kind"], registry=GATEWAY)
CLIENT_WAIT = Histogram("thermite_client_wait_seconds", "Seconds a client waited (queued and in flight) before "
                        "it gave up or saw the failure.", ["client", "kind"], buckets=CLIENT_BUCKETS,
                        registry=GATEWAY)
CLIENT_REPORTS = Counter("thermite_client_reports", "Accepted client telemetry reports, by the HTTP protocol the "
                         "client last used.", ["client", "protocol"], registry=GATEWAY)

for _route in ROUTES:
    IN_FLIGHT.labels(_route)
    REQUEST_SECONDS.labels(_route)
    for _listener in LISTENERS:
        for _status in CODE_CLASSES:
            REQUESTS.labels(_listener, _route, _status)
for _kind in UPSTREAM_KINDS:
    for _outcome in UPSTREAM_OUTCOMES:
        UPSTREAM_SECONDS.labels(_kind, _outcome)
for _view in VIEWS:
    for _version in HTTP_VERSIONS:
        SSE_STREAMS.labels(_view, _version)
for _version in HTTP_VERSIONS:
    SSE_PEER_MAX.labels(_version)
for _limit in SSE_LIMITS:
    SSE_REFUSED.labels(_limit)
for _end in SSE_ENDS:
    SSE_SECONDS.labels(_end)
for _client in CLIENTS:
    for _kind in CLIENT_KINDS:
        CLIENT_EVENTS.labels(_client, _kind)
        CLIENT_WAIT.labels(_client, _kind)
    for _protocol in CLIENT_PROTOCOLS:
        CLIENT_REPORTS.labels(_client, _protocol)

_PAGES = None


def route_class(path):
    """The finite route class of a request path; never the path itself."""
    global _PAGES
    if _PAGES is None:
        from oh_routes import ALIASES, ROUTES as PAGES
        _PAGES = frozenset((*PAGES, *ALIASES))
    if path.startswith("/api/v1/rooms/"):
        return "command" if path.endswith("/commands") else "policy"
    if path == "/api/v1/all-off":
        return "command"
    if path == "/api/v1/locks":
        return "locks"
    if path == "/rest/items" or path.startswith("/rest/items/"):
        return "items"
    if path.startswith("/api/v1/events/"):
        return "events"
    if path.startswith("/unifi/media/"):
        return "media"
    if path in ("/api/v1/session", "/api/v1/logout", "/api/v1/me", "/api/v1/security", "/api/v1/token-login"):
        return "auth"
    if path.startswith(("/api/v1/users", "/api/v1/tokens", "/api/v1/security/guests")):
        return "admin"
    if path in _PAGES or path.startswith("/static/"):
        return "asset"
    return "other"


def code_class(status):
    return f"{status // 100}xx" if isinstance(status, int) and 200 <= status < 600 else "aborted"


class Metered:
    """Pure ASGI wrapper counting every HTTP request it passes on, so it
    goes outside oh_access.service.Admission: Admission's immediate 503s
    count too. It goes inside oh_access.guest.mark, which names the
    listener in the scope. Other scopes pass through untouched."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.application(scope, receive, send)
        route, started, status = route_class(scope.get("path") or ""), time.monotonic(), None
        listener = "guest" if scope.get(GUEST_SCOPE) else "https"

        async def sending(message):
            nonlocal status
            if status is None and message.get("type") == "http.response.start":
                status = message.get("status")
                REQUEST_SECONDS.labels(route).observe(time.monotonic() - started)
            await send(message)

        IN_FLIGHT.labels(route).inc()
        try:
            await self.application(scope, receive, sending)
        finally:
            IN_FLIGHT.labels(route).dec()
            REQUESTS.labels(listener, route, code_class(status)).inc()


def problem(code):
    PROBLEMS.labels(code).inc()


def upstream(kind, outcome, seconds):
    UPSTREAM_SECONDS.labels(kind, outcome).observe(seconds)


_streams = {}
_streams_lock = threading.Lock()


def http_version(value):
    return "2" if str(value) in ("2", "2.0") else "1.1"


def observe(kind, fields):
    """An oh_identity.hub observer: counts client streams by view, HTTP
    version and peer digest (never an address), their lifetimes by how they
    ended, and refusals by the limit that was full."""
    if kind == "stream_open":
        key = (fields.get("peer"), fields.get("view"), http_version(fields.get("http_version")))
        with _streams_lock:
            _streams[key] = _streams.get(key, 0) + 1
    elif kind == "stream_close":
        key = (fields.get("peer"), fields.get("view"), http_version(fields.get("http_version")))
        with _streams_lock:
            left = _streams.get(key, 0) - 1
            if left > 0:
                _streams[key] = left
            else:
                _streams.pop(key, None)
        end = fields.get("reason")
        SSE_SECONDS.labels(end if end in SSE_ENDS else "error").observe(max(0.0, float(fields.get("seconds") or 0)))
    elif kind == "stream_refused":
        from oh_identity import streams
        slots = streams.slots
        SSE_REFUSED.labels("total" if slots.count() >= slots.total else "peer").inc()


def watch_streams():
    """Observe the gateway's client streams and its runtime subscription."""
    from oh_identity import hub
    if observe not in hub.observers:
        hub.observers.append(observe)


def refresh_streams():
    """Set the stream gauges from the open streams, before a write."""
    with _streams_lock:
        open_streams = list(_streams.items())
    for view in VIEWS:
        for version in HTTP_VERSIONS:
            SSE_STREAMS.labels(view, version).set(
                sum(count for (_, each_view, each), count in open_streams if (each_view, each) == (view, version)))
    for version in HTTP_VERSIONS:
        peers = {}
        for (peer, _, each), count in open_streams:
            if each == version:
                peers[peer] = peers.get(peer, 0) + count
        SSE_PEER_MAX.labels(version).set(max(peers.values(), default=0))


class Hub:
    """oh_identity.hub's own state and running totals (EventHub.status()),
    read at each write: whether the one runtime subscription is serving,
    connecting or closed, its connections, failures and resyncs, the runtime
    events it took and the frames and overflow resyncs of client streams."""

    COUNTERS = (("upstream_connects", "thermite_gateway_upstream_connects", "Runtime event subscriptions opened."),
                ("upstream_failures", "thermite_gateway_upstream_failures", "Runtime event subscriptions that failed "
                 "or ended."),
                ("resyncs", "thermite_gateway_upstream_resyncs", "Full reads of the streamed Items after the runtime "
                 "subscription (re)connected."),
                ("events", "thermite_gateway_upstream_events", "Public runtime events the gateway took."),
                ("frames", "thermite_gateway_sse_frames", "Frames sent on client event streams."),
                ("overflows", "thermite_gateway_sse_overflows", "Client event streams that fell too far behind and "
                 "were sent a fresh snapshot instead."))

    def describe(self):
        return self._families({}, "closed")

    def collect(self):
        # Never imported here: a process that has not loaded it has no hub.
        hub, totals, states, hubs = sys.modules.get("oh_identity.hub"), {}, set(), []
        for _ in range(3 if hub else 0):  # The event loop may add a hub mid-iteration.
            try:
                hubs = list(hub._hubs.values())
                break
            except RuntimeError:
                continue
        for each in hubs:
            status = each.status()
            states.add(status.get("upstream"))
            for key, _, _ in self.COUNTERS:
                totals[key] = totals.get(key, 0) + int(status.get(key) or 0)
        state = next((state for state in UPSTREAM_STATES if state in states), "closed")
        return self._families(totals, state)

    def _families(self, totals, state):
        upstream = GaugeMetricFamily("thermite_gateway_upstream_state", "The runtime event subscription: serving, "
                                     "connecting (or reconnecting), or closed with no client stream.",
                                     labels=["state"])
        for each in UPSTREAM_STATES:
            upstream.add_metric([each], 1 if each == state else 0)
        families = [upstream]
        for key, name, documentation in self.COUNTERS:
            family = CounterMetricFamily(name, documentation)
            family.add_metric([], totals.get(key, 0))
            families.append(family)
        return families


GATEWAY.register(Hub())


def client_report(client, protocol, events):
    """Count one validated client report (oh_identity.telemetry.parse)."""
    CLIENT_REPORTS.labels(client, protocol).inc()
    for kind, (count, waits) in events.items():
        CLIENT_EVENTS.labels(client, kind).inc(count)
        for wait in waits:
            CLIENT_WAIT.labels(client, kind).observe(wait)

# ---------------------------------------------------------------- policy worker

ROOM_STATES = ("manual", "scheduled", "unlocked", "unavailable")
BOUNDARY_OUTCOMES = ("dispatched", "dispatch_unconfirmed", "already_current", "leave", "missed",
                     "coalesced_active_lock", "stale_revision", "stale_schedule", "shared_resource_conflict",
                     "invalid_policy", "other")
REPAIR_OUTCOMES = ("sent", "unconfirmed")
#: read: a native read for verification failed; stream: the runtime's event
#: feed failed; housekeeping: daily pruning failed; step: a pass raised.
ERROR_STAGES = ("read", "stream", "housekeeping", "step")
LAG_BUCKETS = (1, 2, 5, 10, 30, 60, 120, 300)

POLICY_LOOP = Gauge("thermite_policy_loop_timestamp_seconds", "When the policy worker last finished a pass.",
                    registry=WORKER)
POLICY_ROOMS = Gauge("thermite_policy_rooms", "Routing zones by policy state: locked by hand or by schedule, "
                     "unlocked, or unavailable (a lock that cannot be enforced: an unreadable policy, a conflict "
                     "over shared hardware, openHAB not answering, or a drift not yet repaired).", ["state"], registry=WORKER)
POLICY_ROOM_STATE = Gauge("thermite_policy_room_state", "1 for each routing zone's policy state (as "
                          "thermite_policy_rooms counts them), 0 for its other states.", ["zone", "state"],
                          registry=WORKER)
POLICY_BOUNDARIES = Counter("thermite_policy_boundaries", "Schedule boundaries the worker claimed, by outcome.",
                            ["outcome"], registry=WORKER)
POLICY_LAG = Histogram("thermite_policy_boundary_lag_seconds", "Seconds from a boundary's scheduled time to the "
                       "worker claiming it.", buckets=LAG_BUCKETS, registry=WORKER)
POLICY_REPAIRS = Counter("thermite_policy_repairs", "Repair commands for a locked room that drifted, by outcome.",
                         ["outcome"], registry=WORKER)
POLICY_RECONCILE_AGE = Gauge("thermite_policy_reconcile_age_seconds", "Seconds since the worker last verified a "
                             "locked routing zone, one whose lock cannot be held now (unavailable) included; "
                             "unlocked zones have none.", ["zone"], registry=WORKER)
POLICY_ERRORS = Counter("thermite_policy_errors", "Policy worker failures by stage.", ["stage"], registry=WORKER)

for _state in ROOM_STATES:
    POLICY_ROOMS.labels(_state)
for _outcome in BOUNDARY_OUTCOMES:
    POLICY_BOUNDARIES.labels(_outcome)
for _outcome in REPAIR_OUTCOMES:
    POLICY_REPAIRS.labels(_outcome)
for _stage in ERROR_STAGES:
    POLICY_ERRORS.labels(_stage)


def policy_error(stage):
    POLICY_ERRORS.labels(stage if stage in ERROR_STAGES else "step").inc()

# ---------------------------------------------------------------- callbacks

#: forwarded: the runtime answered a relayed NOTIFY; the rest are
#: oh_access.callbacks NotificationError codes. Anything else is other.
CALLBACK_OUTCOMES = ("forwarded", "client_disconnected", "duplicate_header", "invalid_body", "invalid_headers",
                     "invalid_xml", "length_mismatch", "host_not_allowed", "peer_not_allowed", "not_found",
                     "method_not_allowed", "body_timeout", "length_required", "invalid_notification", "invalid_size",
                     "xml_required", "native_unavailable", "relay_busy", "relay_not_started", "other")
CALLBACK_COUNT = Counter("thermite_callbacks", "UPnP NOTIFY callbacks the relay handled, by outcome.", ["outcome"],
                         registry=CALLBACKS)
for _outcome in CALLBACK_OUTCOMES:
    CALLBACK_COUNT.labels(_outcome)


def callback_receipt(receipt):
    """oh_access.callbacks.CallbackRelay's receipt sink."""
    outcome = receipt.get("outcome")
    if outcome == "native_response":
        outcome = "forwarded"
    CALLBACK_COUNT.labels(outcome if outcome in CALLBACK_OUTCOMES else "other").inc()

# ---------------------------------------------------------------- the textfiles


class Textfile:
    """Writes a registry to <directory>/<name>.prom every `every` seconds."""

    def __init__(self, registry, name, directory, *, every=WRITE_SECONDS, refresh=None):
        self.registry, self.directory, self.every, self.refresh = registry, Path(directory), every, refresh
        self.path = self.directory / f"{name}.prom"
        self._stop = threading.Event()
        self._thread = None
        self._failing = False

    def write(self):
        """One atomic write; False (logged once while it lasts) when it failed."""
        temporary = self.directory / f".{self.path.name}.{os.getpid()}.tmp"
        try:
            if self.refresh:
                self.refresh()
            data = prometheus_client.generate_latest(self.registry)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
            try:
                # node_exporter reads it as the service account or its group.
                os.fchmod(descriptor, 0o640)
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.path)
        except OSError as error:
            if not self._failing:
                LOG.warning("Metrics textfile %s cannot be written (%s); retrying every %ds.",
                            self.path.name, type(error).__name__, self.every)
            self._failing = True
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        self._failing = False
        return True

    def start(self):
        def loop():
            while not self._stop.wait(self.every):
                self.write()
        self.write()
        self._thread = threading.Thread(target=loop, name="metrics-textfile", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Stop writing, after one last write of the final counts."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
            self.write()


def start(registry, name, *, refresh=None, environ=None):
    """Start writing this service's textfile, or None without THERMITE_TEXTFILE_DIR."""
    directory = (os.environ if environ is None else environ).get(ENVIRONMENT)
    if not directory:
        return None
    return Textfile(registry, name, directory, refresh=refresh).start()
