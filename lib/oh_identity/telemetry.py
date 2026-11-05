"""POST /api/v1/telemetry: what clients saw that the gateway could not.

A browser that gives up on a request it never managed to send (a full
HTTP/1.1 connection pool, a stalled stream) leaves no trace on the server.
Clients therefore count their own give-ups and stream failures and report
them on their next successful request, or with sendBeacon when a page is
hidden. Reports become oh_access.metrics series and one journal line each.
Guest-listener pages report too (oh_identity.guest.ROUTES), under that
listener's Origin and JSON checks.

The schema is finite and carries no free text: at most REPORT_BYTES of
{"client": "web"|"tui", "protocol": <the HTTP protocol last used>,
"events": {<kind>: {"count": 1..1000, "waits": [seconds, ...]}}}. Guests
report too, so the endpoint needs no identity, and sendBeacon cannot send
a CSRF header, so it is CSRF-exempt: it changes nothing but counters,
accepts only application/json (which a cross-site page cannot send without
a preflight this site never answers) and refuses a foreign Origin. Each
peer (oh_identity.addresses.peer_bucket) may report PEER_REPORTS times per
PEER_WINDOW seconds; all peers together GLOBAL_REPORTS times.
"""

import hashlib
import hmac
import json
import logging
import math
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from oh_access import metrics

from .addresses import peer_bucket
from .auth import failure, not_allowed
from .guest import listener

LOG = logging.getLogger(__name__)
REPORT_BYTES = 2048
MAX_COUNT = 1000
MAX_WAITS = 16
MAX_WAIT = 600
PEER_REPORTS = 6
PEER_WINDOW = 60
GLOBAL_REPORTS = 120
#: Peers remembered at once; the oldest are forgotten first.
PEERS = 4096


class Invalid(ValueError):
    pass


def parse(raw):
    """(client, protocol, {kind: (count, waits)}) of a report body, or Invalid."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= REPORT_BYTES:
        raise Invalid()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise Invalid() from None
    if not isinstance(data, dict) or not {"client", "events"} <= set(data) <= {"client", "protocol", "events"}:
        raise Invalid()
    client, protocol, events = data["client"], data.get("protocol", "unknown"), data["events"]
    if client not in metrics.CLIENTS or protocol not in metrics.CLIENT_PROTOCOLS:
        raise Invalid()
    if not isinstance(events, dict) or not events or not set(events) <= set(metrics.CLIENT_KINDS):
        raise Invalid()
    parsed = {}
    for kind in metrics.CLIENT_KINDS:
        if kind not in events:
            continue
        entry = events[kind]
        if not isinstance(entry, dict) or not {"count"} <= set(entry) <= {"count", "waits"}:
            raise Invalid()
        count, waits = entry["count"], entry.get("waits", [])
        if type(count) is not int or not 1 <= count <= MAX_COUNT:
            raise Invalid()
        if not isinstance(waits, list) or len(waits) > min(count, MAX_WAITS):
            raise Invalid()
        for wait in waits:
            if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= MAX_WAIT:
                raise Invalid()
        parsed[kind] = (count, [float(wait) for wait in waits])
    return client, protocol, parsed


class Limits:
    """Token buckets per peer and in total: a report spends one token."""

    def __init__(self, per_peer=PEER_REPORTS, window=PEER_WINDOW, total=GLOBAL_REPORTS, peers=PEERS,
                 clock=time.monotonic):
        self.per_peer, self.window, self.total, self.peers, self.clock = per_peer, window, total, peers, clock
        self._lock = threading.Lock()
        self._buckets = {}
        self._global = (float(total), clock())

    @staticmethod
    def _refill(bucket, capacity, rate, now):
        tokens, at = bucket
        return min(float(capacity), tokens + (now - at) * rate)

    def take(self, peer):
        """0 when the report may go ahead, else seconds until it may."""
        with self._lock:
            now = self.clock()
            peer_rate, global_rate = self.per_peer / self.window, self.total / self.window
            mine = self._refill(self._buckets.pop(peer, (float(self.per_peer), now)), self.per_peer, peer_rate, now)
            everyone = self._refill(self._global, self.total, global_rate, now)
            wait = max((1 - mine) / peer_rate if mine < 1 else 0, (1 - everyone) / global_rate if everyone < 1 else 0)
            if not wait:
                mine, everyone = mine - 1, everyone - 1
            # Reinserted last: the dict's order is least recently seen first.
            self._buckets[peer] = (mine, now)
            self._global = (everyone, now)
            while len(self._buckets) > self.peers:
                del self._buckets[next(iter(self._buckets))]
            return wait


limits = Limits()


def _same_origin(request):
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # Terminal clients send none; browsers always do on POST.
    guest = listener(request)
    if guest is not None:
        # oh_identity.guest has checked it already, against the listener's own.
        return origin in guest.origins
    try:
        host = request.get_host()
    except DisallowedHost:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host.lower()


def _peer_label(bucket):
    """A short keyed digest that ties one peer's lines together in the journal."""
    return hmac.new(settings.SECRET_KEY.encode(), ("telemetry\0" + bucket).encode(), hashlib.sha256).hexdigest()[:8]


@csrf_exempt
def report(request):
    if request.method != "POST":
        return not_allowed("POST")
    if request.content_type != "application/json":
        return failure("json_required", 415)
    if not _same_origin(request):
        return failure("invalid_origin")
    length = request.META.get("CONTENT_LENGTH")
    if length and (not length.isdigit() or int(length) > REPORT_BYTES):
        return failure("invalid_telemetry", 400)
    peer = peer_bucket(request.META.get("REMOTE_ADDR"))
    wait = limits.take(peer)
    if wait:
        return failure("telemetry_throttled", 429, retry_after=wait)
    try:
        client, protocol, events = parse(request.body)
    except Invalid:
        return failure("invalid_telemetry", 400)
    metrics.client_report(client, protocol, events)
    LOG.warning("Client report: client=%s protocol=%s peer=%s %s", client, protocol, _peer_label(peer),
                " ".join(f"{kind}={count}" + (f"(max {max(waits):.1f}s)" if waits else "")
                         for kind, (count, waits) in events.items()))
    response = HttpResponse(status=204)
    response["Cache-Control"] = "no-store"
    return response
