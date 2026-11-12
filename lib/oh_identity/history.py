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
#: Seconds the gateway waits for Prometheus, and the bytes it reads of an answer.
TIMEOUT_SECONDS = 5.0
MAX_BYTES = 8 * 1024 * 1024
PATH = "/api/v1/query_range"

_lock = threading.Lock()
_cache = {}
#: Wall-clock seconds; the tests' seam.
clock = time.time


def query(port, expr, start, end, step):
    """Prometheus' range answer for expr on loopback: its result list."""
    with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False, follow_redirects=False) as client:
        with client.stream("POST", f"http://127.0.0.1:{port}{PATH}",
                           data={"query": expr, "start": str(start), "end": str(end), "step": str(step)}) as response:
            if response.status_code != 200:
                raise ValueError("Prometheus refused the query")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise ValueError("Prometheus answer too large")
    answer = json.loads(bytes(body))
    if answer.get("status") != "success" or answer["data"]["resultType"] != "matrix":
        raise ValueError("Prometheus answer is not a range")
    return answer["data"]["result"]


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
        _cache.clear()
        _cache[key] = answer
        return answer
