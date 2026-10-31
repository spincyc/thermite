"""The thermite exporter: openHAB's Thing statuses and selected Item states
as Prometheus series labelled with the house's devices, rooms and kinds.

Standard library only: it runs in the Apple TV helper's pinned Python image,
and as a script (python3 exporter.py ...) as well as a module. It serves
GET /metrics (text exposition format 0.0.4) and GET /healthz on 127.0.0.1.

What to report comes from the map bin/thermite generate writes
(metrics/exporter/map.json, oh_metrics.exporter_map()): each device's room,
kind, label and Thing UIDs, and each Item kept with its point (power,
volume, input...) and conversion:

    switch   ON 1, OFF 0
    number   the state's leading number (a unit is dropped), ON 1, OFF 0
    enum     an info series with the value as a label: one of the Item's
             options, `other` for any value outside them; without options,
             the value itself when it is short and plain, else `other`
    problem  1 while the problem text is set, 0 when it is empty

NULL, UNDEF and anything unparsable are absent, never stale. The explicit
clear_state descriptor is the sole exception: Sony's problem Item uses a
present UNDEF to mean "nothing wrong", which becomes 0. Missing/NULL never
becomes healthy. A scrape makes
at most one REST round (GET /rest/things and /rest/items, ROUND_SECONDS
for both), shared by scrapes within CACHE_SECONDS. When openHAB does not answer,
thermite_openhab_up is 0 and no Thing or Item series is reported. The API
token is read from its file at every round, so a new one needs no restart;
it is sent only as the Authorization header and never logged.
"""

from __future__ import annotations

import argparse
import http.server
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

EXPORTER_PORT = 9480
LISTEN = f"127.0.0.1:{EXPORTER_PORT}"
OPENHAB = "http://127.0.0.1:8080"
MAP = Path("/etc/thermite-exporter/map.json")
TOKEN = Path("/secrets/openhab-token")
MAP_VERSION = 1
CACHE_SECONDS = 5.0
#: Seconds a round's two REST calls share, under Prometheus' 10 s
#: scrape_timeout: a slow openHAB gives thermite_openhab_up 0, not a failed scrape.
ROUND_SECONDS = 8.0
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
#: openHAB's ThingStatus values.
THING_STATUSES = ("UNINITIALIZED", "INITIALIZING", "UNKNOWN", "ONLINE", "OFFLINE", "REMOVING", "REMOVED")
THINGS_PATH = "/rest/things?summary=true"
ITEMS_PATH = "/rest/items?fields=name,state,type"
NUMBER = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")
#: An enum value without options is kept only if it looks like a code, not prose.
PLAIN = re.compile(r"^[A-Za-z0-9_./:+ -]{1,32}$")
OTHER = "other"
DEVICE_LABELS = ("device", "room", "kind")

#: fetch(path, timeout): GET a path below openHAB and parse its JSON.
Fetch = Callable[[str, float], object]

FAMILIES = {
    "thermite_openhab_up": ("gauge", "1 when openHAB answered both REST requests of the last round."),
    "thermite_openhab_endpoint_up": ("gauge", "1 when openHAB answered this REST request in the last round."),
    "thermite_scrape_duration_seconds": ("gauge", "Seconds the last REST round took."),
    "thermite_items_missing": ("gauge", "Mapped Items openHAB does not have."),
    "thermite_device_info": ("gauge", "Each device of the house file, with its room, kind and label."),
    "thermite_thing_online": ("gauge", "1 when the Thing is ONLINE."),
    "thermite_thing_status": ("gauge", "The Thing's status, as a label; always 1."),
    "thermite_item_value": ("gauge", "A numeric Item state: switches 1 or 0, numbers as they are."),
    "thermite_item_info": ("gauge", "A selection Item's state, as a label; always 1."),
    "thermite_device_problem": ("gauge", "1 while the device's problem Item has a text."),
}


# ---------------------------------------------------------------- exposition


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


Sample = tuple[str, dict[str, str], float]


def exposition(samples: list[Sample]) -> str:
    """Samples as the text format: families in FAMILIES order, each once with
    its HELP and TYPE, its samples in the order given."""
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample[0], []).append(sample)
    lines: list[str] = []
    for name in [*FAMILIES, *sorted(set(grouped) - set(FAMILIES))]:
        if name not in grouped:
            continue
        kind, text = FAMILIES.get(name, ("untyped", ""))
        lines += [f"# HELP {name} {escape_help(text)}", f"# TYPE {name} {kind}"]
        for _, labels, value in grouped[name]:
            body = ",".join(f'{key}="{escape_label(str(labels[key]))}"' for key in labels)
            lines.append(f"{name}{{{body}}} {format_value(value)}" if body else f"{name} {format_value(value)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- conversions


def to_number(state: object, convert: str, clear_state: str | None = None) -> float | None:
    """A state as a number for `switch`, `number` and `problem` Items; None
    when it has none (NULL, UNDEF, unparsable)."""
    if convert == "problem" and clear_state is not None and state == clear_state:
        return 0.0
    if not isinstance(state, str) or state in ("NULL", "UNDEF"):
        return None
    if convert == "problem":
        return 1.0 if state.strip() else 0.0
    if state == "ON":
        return 1.0
    if state == "OFF":
        return 0.0
    if convert == "switch":
        return None
    match = NUMBER.match(state)
    if not match:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) else None


def to_label(state: object, options: list[str] | None) -> str | None:
    """A selection state as a bounded label value; None for NULL and UNDEF."""
    if not isinstance(state, str) or state in ("NULL", "UNDEF") or not state:
        return None
    if options:
        return state if state in options else OTHER
    return state if PLAIN.match(state) else OTHER


# ---------------------------------------------------------------- the collector


def read_token(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def http_fetch(base: str, token_file: Path | None) -> Fetch:
    """GET a path below openHAB's base URL and parse its JSON."""

    def fetch(path: str, timeout: float) -> object:
        headers = {"Accept": "application/json"}
        token = read_token(token_file)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(base.rstrip("/") + path, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    return fetch


def describe(error: BaseException) -> str:
    """An error for the log, without the request's headers."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"unreachable ({type(error.reason).__name__})"
    return type(error).__name__


def load_map(path: Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != MAP_VERSION:
        raise ValueError(f"{path}: not a version {MAP_VERSION} exporter map")
    return document


def device_labels(mapping: dict, ident: str) -> dict[str, str]:
    device = (mapping.get("devices") or {}).get(ident) or {}
    return {"device": ident, "room": str(device.get("room", "")), "kind": str(device.get("kind", ""))}


def collect(mapping: dict, fetch: Fetch, clock: Callable[[], float] = time.monotonic,
            log: Callable[[str], None] | None = None, budget: float = ROUND_SECONDS) -> list[Sample]:
    """One REST round, of at most `budget` seconds, turned into samples."""
    started = clock()
    answers: dict[str, object] = {}
    for endpoint, path in (("things", THINGS_PATH), ("items", ITEMS_PATH)):
        remaining = started + budget - clock()
        if remaining <= 0:
            if log:
                log(f"openHAB {endpoint}: not asked, the round's {budget:g} s are used up")
            continue
        try:
            answer = fetch(path, remaining)
            if not isinstance(answer, list):
                raise ValueError("not a list")
            answers[endpoint] = answer
        except Exception as error:  # noqa: BLE001 - any failure means "openHAB did not answer"
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            if log:
                log(f"openHAB {endpoint}: {describe(error)}")
    duration = max(0.0, clock() - started)
    samples: list[Sample] = [
        ("thermite_openhab_up", {}, 1.0 if len(answers) == 2 else 0.0),
        *(("thermite_openhab_endpoint_up", {"endpoint": e}, 1.0 if e in answers else 0.0) for e in ("things", "items")),
        ("thermite_scrape_duration_seconds", {}, duration),
    ]
    devices = mapping.get("devices") or {}
    for ident in sorted(devices):
        device = devices[ident]
        samples.append(("thermite_device_info", {**device_labels(mapping, ident),
                                                 "label": str(device.get("label", ident))}, 1.0))
    if "things" in answers:
        samples += _things(mapping, answers["things"])
    if "items" in answers:
        samples += _items(mapping, answers["items"])
    return samples


def _things(mapping: dict, answer: list) -> list[Sample]:
    status_of: dict[str, str] = {}
    for thing in answer:
        if isinstance(thing, dict) and isinstance(thing.get("UID"), str):
            status = (thing.get("statusInfo") or {}).get("status")
            if status in THING_STATUSES:
                status_of[thing["UID"]] = status
    samples: list[Sample] = []
    devices = mapping.get("devices") or {}
    for ident in sorted(devices):
        for uid in devices[ident].get("things") or []:
            status = status_of.get(uid)
            if status is None:
                continue
            labels = {**device_labels(mapping, ident), "thing": uid}
            samples.append(("thermite_thing_online", labels, 1.0 if status == "ONLINE" else 0.0))
            samples.append(("thermite_thing_status", {**labels, "status": status}, 1.0))
    return samples


def _items(mapping: dict, answer: list) -> list[Sample]:
    state_of = {item["name"]: item.get("state") for item in answer
                if isinstance(item, dict) and isinstance(item.get("name"), str)}
    samples: list[Sample] = []
    missing = 0
    for name, spec in sorted((mapping.get("items") or {}).items()):
        if name not in state_of:
            missing += 1
            continue
        state = state_of[name]
        device = str(spec.get("device") or "")
        labels = {"device": device, "room": str(spec.get("room", "")), "kind": str(spec.get("kind", ""))}
        convert = spec.get("convert")
        if convert == "problem":
            value = to_number(state, convert, spec.get("clear_state"))
            if value is not None:
                samples.append(("thermite_device_problem", labels, value))
            continue
        labels = {**labels, "item": name, "point": str(spec.get("point", ""))}
        if convert == "enum":
            text = to_label(state, spec.get("options"))
            if text is not None:
                samples.append(("thermite_item_info", {**labels, "value": text}, 1.0))
        elif convert in ("switch", "number"):
            value = to_number(state, convert)
            if value is not None:
                samples.append(("thermite_item_value", labels, value))
    samples.append(("thermite_items_missing", {}, float(missing)))
    return samples


class Collector:
    """The exposition, from at most one REST round per CACHE_SECONDS, with
    the map read again whenever its file changes."""

    def __init__(self, map_path: Path, fetch: Fetch, *, cache_seconds: float = CACHE_SECONDS,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] | None = None) -> None:
        self.map_path = Path(map_path)
        self.fetch = fetch
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._map_stamp: tuple[int, int] | None = None
        self._mapping: dict = {}
        self._text: str | None = None
        self._at = 0.0
        self._last_errors: set[str] = set()
        self._round_errors: set[str] = set()
        self._reload()

    def _reload(self) -> None:
        stat = self.map_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._map_stamp:
            self._mapping = load_map(self.map_path)
            self._map_stamp = stamp

    def _note(self, message: str) -> None:
        # Logged once while it lasts, not at every scrape.
        if message not in self._last_errors:
            self.log(message)
        self._round_errors.add(message)

    def text(self) -> str:
        with self._lock:
            now = self.clock()
            if self._text is not None and now - self._at < self.cache_seconds:
                return self._text
            try:
                self._reload()
            except (OSError, ValueError) as error:
                self.log(f"map {self.map_path}: {type(error).__name__}; keeping the previous one")
            self._round_errors = set()
            samples = collect(self._mapping, self.fetch, self.clock, self._note)
            self._last_errors = self._round_errors
            self._text, self._at = exposition(samples), now
            return self._text


def handler_for(collector: Collector) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "thermite-exporter"
        sys_version = ""

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                self._send(200, collector.text().encode("utf-8"), CONTENT_TYPE)
            elif path == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's signature
            pass

    return Handler


def parse_listen(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"{value!r} is not HOST:PORT")
    return host, int(port)


def serve(collector: Collector, host: str, port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer((host, port), handler_for(collector))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thermite-exporter", description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", type=parse_listen, default=parse_listen(LISTEN), metavar="HOST:PORT",
                        help=f"address to serve on (default {LISTEN})")
    parser.add_argument("--map", type=Path, default=MAP, help=f"exporter map (default {MAP})")
    parser.add_argument("--openhab", default=OPENHAB, metavar="URL", help=f"openHAB (default {OPENHAB})")
    parser.add_argument("--token-file", type=Path, default=TOKEN,
                        help=f"file holding an openHAB API token (default {TOKEN})")
    parser.add_argument("--cache", type=float, default=CACHE_SECONDS, metavar="SECONDS",
                        help=f"seconds scrapes share one REST round (default {CACHE_SECONDS:g})")
    parser.add_argument("--once", action="store_true", help="print one exposition and exit")
    arguments = parser.parse_args(argv)

    def log(message: str) -> None:
        print(f"thermite-exporter: {message}", file=sys.stderr, flush=True)

    if read_token(arguments.token_file) is None:
        log(f"no token in {arguments.token_file}; asking openHAB without one")
    try:
        collector = Collector(arguments.map, http_fetch(arguments.openhab, arguments.token_file),
                              cache_seconds=arguments.cache, log=log)
    except (OSError, ValueError) as error:
        log(f"cannot read the map {arguments.map}: {error}")
        return 2
    if arguments.once:
        sys.stdout.write(collector.text())
        return 0
    host, port = arguments.listen
    server = serve(collector, host, port)
    log(f"serving /metrics on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
