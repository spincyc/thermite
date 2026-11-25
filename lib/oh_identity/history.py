"""Each room's confirmed source through the last day: the House board's Rooms
through the day timeline (oh_metrics.dashboards.room_history), as runs of
one value each, for clients that cannot reach Grafana or Prometheus.

The compiled contract names everything (oh_site_contract.history): the
queries, what each number they give means and the loopback port of the
house's Prometheus. No client chooses a query, a range or an upstream. One
range query reads every zone, at most once a STEP_SECONDS: clients asking
within the same step share its answer. Nothing Prometheus says reaches a
client but numbers the contract names; anything else is Unknown, as the
board shows an unmapped number."""

import atexit
import json
import math
import threading
import time

import httpx

from .services import Problem

#: The day a client sees, in steps of a minute: each step's value is the
#: readback at its start, as the board samples it.
RANGE_SECONDS = 24 * 3600
STEP_SECONDS = 60
#: Seconds the gateway waits for Prometheus, and the bytes it reads of an answer
#: (this history, and oh_identity.alerts).
TIMEOUT_SECONDS = 5.0
MAX_BYTES = 8 * 1024 * 1024
PATH = "/api/v1/query_range"

_lock = threading.Lock()
_cache = {}
_client_lock = threading.Lock()
_client = None
_client_factory = None
#: Wall-clock seconds; the tests' seam.
clock = time.time


def close_client():
    """Close the shared loopback Prometheus client if open."""
    global _client, _client_factory
    with _client_lock:
        client, _client, _client_factory = _client, None, None
    if client is not None and not client.is_closed:
        client.close()


atexit.register(close_client)


def _http_client():
    """Shared persistent keepalive httpx.Client for loopback Prometheus reads."""
    global _client, _client_factory
    factory = httpx.Client
    old = None
    with _client_lock:
        if _client is None or _client.is_closed or _client_factory is not factory:
            old = _client
            _client = factory(
                timeout=TIMEOUT_SECONDS,
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
            _client_factory = factory
        client = _client
    if old is not None and not old.is_closed:
        old.close()
    return client


def ask(port, method, path, data=None):
    """The `data` of Prometheus' answer to one request on loopback, read
    whole: at most MAX_BYTES, within TIMEOUT_SECONDS, with no proxy and no
    redirect."""
    client = _http_client()
    with client.stream(method, f"http://127.0.0.1:{port}{path}", data=data) as response:
        if response.status_code != 200:
            raise ValueError("Prometheus refused the request")
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError("Prometheus answer too large")
    answer = json.loads(bytes(body))
    if answer.get("status") != "success":
        raise ValueError("Prometheus did not answer the request")
    return answer["data"]


def query(port, expr, start, end, step):
    """Prometheus' range answer for expr on loopback: its result list."""
    data = ask(port, "POST", PATH, {"query": expr, "start": str(start), "end": str(end), "step": str(step)})
    if data["resultType"] != "matrix":
        raise ValueError("Prometheus answer is not a range")
    return data["result"]


def _value(number, values):
    """The value a sample stands for; None (Unknown) for one the contract does not name."""
    try:
        sample = float(number)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sample) or sample != int(sample):
        return None
    return values.get(str(int(sample)))


def segments(samples, values, start, end, step):
    """[[from, to, value], ...]: runs of one value, each sample holding from
    its time to the next step; a step without a sample is no run (no data)."""
    runs = []
    for at, number in samples:
        at = int(float(at))
        if not start <= at < end or (at - start) % step:
            continue
        value = _value(number, values)
        if runs and runs[-1][1] == at and runs[-1][2] == value:
            runs[-1][1] = at + step
        else:
            runs.append([at, at + step, value])
    return runs


def rooms(contract, *, now=None, fetch=None):
    """The history every client of this contract sees now."""
    spec = contract.history
    if not spec:
        raise Problem("history_unavailable", 503)
    end = int((clock() if now is None else now) // STEP_SECONDS * STEP_SECONDS)
    start = end - RANGE_SECONDS
    key = (id(contract), end)
    with _lock:
        if key in _cache:
            return _cache[key]
    zones = list(spec["zones"])
    series = {}
    try:
        if zones:
            # One query for every zone: each zone's series names its own source Item.
            expr = " or ".join(f"({zone['query']})" for zone in zones)
            # Prometheus' range includes its end: the last step starts a step before now.
            for entry in (fetch or query)(spec["port"], expr, start, end - STEP_SECONDS, STEP_SECONDS):
                item = entry["metric"].get("item")
                if item in series:
                    raise ValueError("Two series for one zone")
                series[item] = entry["values"]
        history = [{"room": zone["room"], "label": zone["label"],
                    "segments": segments(series.get(zone["item"], ()), spec["values"], start, end, STEP_SECONDS)}
                   for zone in zones]
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise Problem("history_unavailable", 503) from None
    answer = {"version": 1, "start": start, "end": end, "step": STEP_SECONDS, "labels": dict(spec["labels"]),
              "rooms": history}
    with _lock:
        _cache.clear()
        _cache[key] = answer
        return answer
