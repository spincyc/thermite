"""Grafana dashboards for the house, built from the exporter map.

Six default dashboards and an optional Yoke dashboard, each with a fixed
uid, all querying the one provisioned datasource by its fixed uid:

    house      House health, the home page: is the house OK? Control,
               rooms, remotes, alerts, phone delivery and the host at a
               glance, what needs attention now in plain words, every
               room's source through the day, and switching since
               yesterday evening
    switching  Switching & remotes: how long source changes take, the
               routing executor, the gateway's latency, event streams and
               problems, what remotes gave up on, and the policy worker
    devices    Devices: one row per device (Thing, port, problem, whether
               a room needs it now), then each room's power and source
    openhab    openHAB runtime: the runtime's Metrics add-on (JVM, events,
               rules, Things, thread pools) and its thingHandler pool
    host       Host: scrape targets, the Thermite units and their restarts,
               firing alerts, CPU, memory, disk and btrfs
    usage      Usage: how the house is used (zone time, source mix);
               nothing on it is a health signal
    yoke       optional passive endpoint observations and observer health

Colour means one thing everywhere: green OK, amber warning, red failure,
grey unknown, slate Off, and tan only for "in use". A panel whose values
have mapped colours gets threshold steps of the same colours, so whichever
Grafana applies, the colour is the mapping's. State timelines use a fixed
grey, never threshold colours: with those, Grafana merges values into
threshold ranges ("9+") and drops the mappings' words.

House facts (rooms, devices, Items) come only from the map; the code names
none. Panel ids and positions are assigned in order, so the same map gives
the same JSON.
"""

from __future__ import annotations

import re

import oh_units

from . import alerts
from .exporter import FAILURE_REASONS, HOUSE_LABEL, HOUSE_ROOM, OPERATION_OUTCOMES, ROUTE_BUCKETS

DATASOURCE_UID = "thermite-prometheus"
DATASOURCE = {"type": "prometheus", "uid": DATASOURCE_UID}
#: Grafana 12/13's classic dashboard JSON; Grafana migrates older ones on load.
SCHEMA_VERSION = 41
HOUSE, SWITCHING, DEVICES, OPENHAB, HOST, USAGE, YOKE = (
    "house", "switching", "devices", "openhab", "host", "usage", "yoke")
UIDS = {HOUSE: "thermite-house-health", SWITCHING: "thermite-switching", DEVICES: "thermite-devices",
        OPENHAB: "thermite-openhab", HOST: "thermite-host", USAGE: "thermite-usage", YOKE: "thermite-yoke"}
TITLES = {HOUSE: "House health", SWITCHING: "Switching & remotes", DEVICES: "Devices", OPENHAB: "openHAB runtime",
          HOST: "Host", USAGE: "Usage", YOKE: "Yoke"}
TAG = "thermite"

#: The openHAB Metrics add-on's series, as openHAB 5.2.3 serves them at
#: /rest/metrics/prometheus (read from a booted test openHAB; thermite test
#: boot fails when one is missing, unless ON_ACTIVITY has it). The event
#: counter has no openhab_ prefix, and rules have a run counter but no duration.
OPENHAB_METRICS = {
    "thing_state": "openhab_thing_state",
    "event_count": "event_count_total",
    "rule_runs": "openhab_rule_runs_total",
    "pool_threads": "executor_pool_size_threads",
    "pool_active": "executor_active_threads",
    "pool_queued": "executor_queued_tasks",
    "memory_used": "jvm_memory_used_bytes",
    "memory_max": "jvm_memory_max_bytes",
    "gc_pause_sum": "jvm_gc_pause_seconds_sum",
    "gc_pause_count": "jvm_gc_pause_seconds_count",
    "threads_live": "jvm_threads_live_threads",
    "threads_daemon": "jvm_threads_daemon_threads",
    "process_cpu": "process_cpu_usage",
}
#: Of OPENHAB_METRICS, the series that appear only once something happened:
#: a rule's first run, the first garbage collection. Every boot sends events
#: (Things change status), so the event counter is not among them.
ON_ACTIVITY = frozenset({"rule_runs", "gc_pause_sum", "gc_pause_count"})
#: ThingStatus ordinals as the add-on reports them.
THING_STATE_NAMES = ("UNINITIALIZED", "INITIALIZING", "UNKNOWN", "ONLINE", "OFFLINE", "REMOVING", "REMOVED")

#: Device kinds whose volume is a listening level; others (TVs behind a
#: receiver, Cast players at a fixed 100) only draw flat lines.
LISTENING_KINDS = ("avr", "amp", "streaming_amp", "soundbar", "sub")
#: The units the Host dashboard shows.
UNITS = oh_units.DAEMONS
#: Seconds between samples (oh_metrics.SCRAPE_INTERVAL): a sample count
#: times this is observed time.
SAMPLE_SECONDS = 30
#: Panels that count "since yesterday evening" show 18:00 yesterday to now.
SINCE_EVENING = "now/d-6h"

# Colour has one meaning on every board.
OK, WARN, FAIL, UNKNOWN, OFF, IN_USE = "#73A96F", "#E0A33A", "#D8574C", "#808A96", "#394354", "#D7B18A"
#: Every colour a state may have.
SEMANTIC = (OK, WARN, FAIL, UNKNOWN, OFF, IN_USE)


def value_map(options: dict) -> list[dict]:
    """A value mapping: {value: (text, colour)}, in the order given."""
    return [{"type": "value", "options": {str(value): {"text": text, "color": color, "index": index}
                                          for index, (value, (text, color)) in enumerate(options.items())}}]


UP_DOWN = value_map({0: ("Down", FAIL), 1: ("Up", OK)})
ON_OFF = value_map({0: ("Off", OFF), 1: ("On", IN_USE)})
ONLINE = value_map({0: ("Offline", FAIL), 1: ("Online", OK)})
OPEN = value_map({0: ("Closed", FAIL), 1: ("Open", OK)})
PROBLEM = value_map({0: ("None", OK), 1: ("Reported", WARN)})
NEEDED = value_map({0: ("No", OFF), 1: ("Yes", IN_USE)})
CHECK_STATE = value_map({-1: ("Unknown", UNKNOWN), 0: ("OK", OK), 1: ("Failing", FAIL)})
CONTROL = value_map({-1: ("Unknown", UNKNOWN), 0: ("Ready", OK), 1: ("Needs attention", FAIL)})
REMOTES = value_map({0: ("OK", OK), 1: ("Timing out", WARN), 2: ("Not live", FAIL), 3: ("Gateway down", FAIL)})
#: NaN, as _known() gives for a count it cannot know, shown as Unknown.
NAN_UNKNOWN = [{"type": "special", "options": {"match": "nan", "result": {"text": "Unknown", "color": UNKNOWN,
                                                                          "index": 0}}}]
SEVERITY = [{"type": "value", "options": {"critical": {"text": "Critical", "color": FAIL, "index": 0},
                                          "warning": {"text": "Warning", "color": WARN, "index": 1}}}]
#: The plain words of each check, and of a quarantined device.
CHECK_WORDS = {**alerts.CHECK_WORDS, "quarantined": "Commands quarantined"}
HTTP1_LIMIT = alerts.HTTP1_CONNECTIONS
TIMEOUTS = "|".join(alerts.TIMEOUT_KINDS)
STREAM_FAILURES = "|".join(alerts.STREAM_KINDS)
#: Executor outcomes that are a device failing to take an operation.
FAILED_OPERATIONS = tuple(outcome for outcome in OPERATION_OUTCOMES if outcome in FAILURE_REASONS)
#: Gateway route classes a remote waits on (oh_access.metrics.ROUTES).
WAITED_ROUTES = ("command", "policy", "locks", "items")
#: Routing's counts and the executor's status are valid now: a count of 0
#: means none only then.
ROUTING_COUNTS_VALID = "(max(thermite_routing_metrics_up) == 1)"
EXECUTOR_VALID = "(max(thermite_routing_executor_up) == 1)"


# ---------------------------------------------------------------- panels


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    return {"datasource": DATASOURCE, "refId": ref, "expr": expr, "legendFormat": legend or "__auto",
            "range": not instant, "instant": instant}


def _targets(queries: list[tuple[str, str]], instant: bool = False) -> list[dict]:
    return [target(expr, legend, chr(ord("A") + index), instant) for index, (expr, legend) in enumerate(queries)]


def _thresholds(*steps: tuple[str, float | None]) -> dict:
    return {"mode": "absolute", "steps": [{"color": color, "value": value} for color, value in steps]}


def mapped_thresholds(mappings: list[dict], below: str = UNKNOWN) -> dict:
    """Threshold steps that give each mapped number its mapping's colour, so
    the threshold colour can never contradict the mapping (Grafana colours
    some panels by threshold even where a mapping matched)."""
    numbers = []
    for entry in mappings:
        for key, value in (entry.get("options") or {}).items() if entry.get("type") == "value" else ():
            try:
                numbers.append((float(key), value["color"]))
            except (ValueError, KeyError):
                continue
    steps: list[tuple[str, float | None]] = [(below, None)]
    steps += [(color, int(number) if number.is_integer() else number) for number, color in sorted(numbers)]
    return _thresholds(*steps)


def timeseries(title: str, queries: list[tuple[str, str]], unit: str = "short", *, stack: bool = False,
               minimum: float | None = None, maximum: float | None = None, description: str = "",
               bars: bool = False) -> dict:
    defaults: dict = {"unit": unit, "noValue": "Unknown", "color": {"mode": "palette-classic"},
                      "custom": {"drawStyle": "bars" if bars else "line", "lineWidth": 1,
                                 "fillOpacity": 70 if bars else 10 if stack else 0,
                                 "showPoints": "never", "spanNulls": False, "lineInterpolation": "stepAfter",
                                 "stacking": {"mode": "normal" if stack else "none", "group": "A"}}}
    if bars:
        defaults["decimals"] = 0  # bars count events
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    return {"type": "timeseries", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": _targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def state_timeline(title: str, queries: list[tuple[str, str]], mappings: list | None = None,
                   description: str = "", *, show_values: bool = False) -> dict:
    """Rows of states. Mapped values show the mapping's text in its colour;
    an unmapped value is grey (unknown), never a borrowed meaning.

    The colour mode is fixed, never thresholds: with merged values and
    threshold colours, Grafana replaces each value by its threshold range
    ("9+") and ignores the mappings' text and colours."""
    mappings = mappings or []
    defaults = {"noValue": "Unknown", "color": {"mode": "fixed", "fixedColor": UNKNOWN},
                "thresholds": mapped_thresholds(mappings) if mappings else _thresholds((UNKNOWN, None)),
                "mappings": mappings, "custom": {"fillOpacity": 85, "lineWidth": 0, "spanNulls": False}}
    return {"type": "state-timeline", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": _targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"mergeValues": True, "showValue": "auto" if show_values else "never", "alignValue": "left",
                        "rowHeight": 0.85, "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}}}


def stat(title: str, expr: str, unit: str = "none", steps: tuple = ((OK, None),),
         mappings: list | None = None, description: str = "") -> dict:
    mappings = mappings or []
    return {"type": "stat", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": [target(expr, title, "A", instant=True)],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "Unknown", "color": {"mode": "thresholds"},
                                         "thresholds": mapped_thresholds(mappings) if mappings else _thresholds(*steps),
                                         "mappings": mappings},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "justifyMode": "center",
                        "textMode": "value", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}


def table(title: str, expr: str, columns: list[str], description: str = "", *, names: dict | None = None,
          no_value: str = "") -> dict:
    """An instant table of the series `expr` gives, showing only `columns`
    (labels, or Value), renamed by `names`."""
    keep = {name: index for index, name in enumerate(columns)}
    # `no_value` speaks for an empty table; a row's missing label is a dash.
    blanks = [{"matcher": {"id": "byName", "options": name}, "properties": [{"id": "noValue", "value": "—"}]}
              for name in columns] if no_value else []
    return {"type": "table", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": [{**target(expr, "", "A", instant=True), "format": "table"}],
            "fieldConfig": {"defaults": {"noValue": no_value} if no_value else {}, "overrides": blanks},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
            "transformations": [{"id": "organize", "options": {
                "excludeByName": {"Time": True, "Value": "Value" not in keep, "__name__": True, "job": True,
                                  "instance": "instance" not in keep},
                "indexByName": keep, "includeByName": {name: True for name in columns},
                "renameByName": names or {}}}]}


def _table_queries(panel: dict, queries: list[tuple[str, str]]) -> None:
    """Several instant table queries, merged on their common labels."""
    panel["targets"] = [{**target(expr, "", ref, instant=True), "format": "table"} for ref, expr in queries]
    panel["transformations"].insert(0, {"id": "merge", "options": {}})


def note(title: str, content: str) -> dict:
    return {"type": "text", "title": title, "transparent": True,
            "options": {"mode": "markdown", "content": content}}


def bars(title: str, queries: list[tuple[str, str]], description: str, *, unit: str = "dtdurations",
         horizontal: bool = False) -> dict:
    """Compare genuine aggregates, not a separate bar for every current state."""
    panel = timeseries(title, queries, unit, minimum=0, description=description)
    panel.update(type="barchart", targets=_targets(queries, instant=True),
                 transformations=[{"id": "reduce", "options": {"mode": "seriesToRows", "reducers": ["lastNotNull"]}}],
                 options={"xField": "Field", "orientation": "horizontal" if horizontal else "vertical",
                          "barWidth": 0.7, "groupWidth": 0.85, "barRadius": 0.12, "showValue": "auto",
                          "stacking": "none", "xTickLabelRotation": 0, "xTickLabelSpacing": 40,
                          "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}})
    panel["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": IN_USE}
    panel["fieldConfig"]["defaults"]["custom"]["fillOpacity"] = 80
    panel["fieldConfig"]["defaults"]["custom"]["lineWidth"] = 0
    return panel


def heatmap(title: str, expr: str, description: str, *, interval: str = "15m") -> dict:
    """Counts per `interval` ($__interval in `expr`) by bucket: wide enough
    cells that sparse events read as counts, not fractions."""
    return {"type": "heatmap", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": [{**target(expr, "{{le}}", "A"), "format": "heatmap", "interval": interval}],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"calculate": False, "cellGap": 1, "yAxis": {"axisPlacement": "left", "unit": "s"},
                        "rowsFrame": {"layout": "le"},
                        "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64, "exponent": 0.5,
                                  "fill": IN_USE, "reverse": False},
                        "cellValues": {"unit": "short", "decimals": 0}, "filterValues": {"le": 1e-9},
                        "legend": {"show": True}, "tooltip": {"mode": "single", "yHistogram": True}}}


def _color(panel: dict, name: str, color: str, display: str | None = None) -> None:
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]
    if display:
        props.append({"id": "displayName", "value": display})
    panel["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": name}, "properties": props})


def _column(panel: dict, name: str, *properties: dict) -> None:
    panel["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": name},
                                              "properties": list(properties)})


def _mapped_cells(mappings: list[dict]) -> tuple[dict, ...]:
    """Field properties that show a column's mapped text in its colour."""
    return ({"id": "mappings", "value": mappings}, {"id": "thresholds", "value": mapped_thresholds(mappings)},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}})


def _words(options: dict) -> list[dict]:
    """A text-to-text mapping: a label value shown in words."""
    return [{"type": "value", "options": {key: {"text": text, "index": index}
                                          for index, (key, text) in enumerate(options.items())}}]


class Board:
    """A dashboard's panels, laid out left to right in rows of 24 columns."""

    def __init__(self) -> None:
        self.panels: list[dict] = []
        self._next_id = 1
        self._x = 0
        self._y = 0
        self._row_height = 0
        self._row: dict | None = None

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _newline(self) -> None:
        self._y += self._row_height
        self._x = 0
        self._row_height = 0

    def add(self, panel: dict, width: int, height: int) -> None:
        if self._x + width > 24:
            self._newline()
        panel = {"id": self._id(), "gridPos": {"x": self._x, "y": self._y, "w": width, "h": height}, **panel}
        self._x += width
        self._row_height = max(self._row_height, height)
        (self._row["panels"] if self._row is not None else self.panels).append(panel)

    def row(self, title: str, collapsed: bool = False) -> None:
        """Start a row; a collapsed one holds the panels added after it."""
        self._newline()
        row = {"type": "row", "id": self._id(), "title": title, "collapsed": collapsed,
               "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}, "panels": []}
        self.panels.append(row)
        self._row = row if collapsed else None
        self._y += 1


#: Service starts and deploys, marked on the time axes of the boards that use them.
ANNOTATIONS = [
    {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True,
     "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"},
    {"datasource": DATASOURCE, "enable": True, "iconColor": UNKNOWN, "name": "Service starts",
     "expr": 'changes(node_systemd_unit_start_time_seconds{job="node",name=~"thermite-.*\\\\.service"}[2m]) > 0',
     "step": "1m", "titleFormat": "{{name}} started", "textFormat": "", "tagKeys": "name",
     "useValueForTime": False},
    {"datasource": DATASOURCE, "enable": True, "iconColor": IN_USE, "name": "Deploys",
     "expr": "changes(thermite_deploy_timestamp_seconds[2m]) > 0", "step": "1m", "titleFormat": "Deploy",
     "textFormat": "", "tagKeys": "", "useValueForTime": False},
]


def dashboard(name: str, board: Board, description: str, refresh: str = "1m", time_from: str = "now-24h",
              *, annotations: bool = True) -> dict:
    return {
        "uid": UIDS[name], "title": TITLES[name], "description": description, "tags": [TAG],
        "schemaVersion": SCHEMA_VERSION, "editable": False, "graphTooltip": 1, "timezone": "browser",
        "refresh": refresh, "time": {"from": time_from, "to": "now"}, "timepicker": {},
        "templating": {"list": []}, "annotations": {"list": ANNOTATIONS if annotations else ANNOTATIONS[:1]},
        "fiscalYearStartMonth": 0, "liveNow": False,
        "links": [{"type": "dashboards", "tags": [TAG], "asDropdown": False, "includeVars": False,
                   "keepTime": True, "title": "Dashboards", "icon": "external link", "targetBlank": False}],
        "panels": board.panels,
    }


def _q(value: str) -> str:
    """A PromQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _units(names) -> str:
    return _q("|".join(re.escape(name) for name in names))


# ---------------------------------------------------------------- house facts


def _zones(mapping: dict) -> list[tuple[str, str, str]]:
    """(source Item, room, label) of every routing zone, by label."""
    labels = mapping.get("rooms") or {}
    zones = []
    for name, spec in (mapping.get("items") or {}).items():
        if spec.get("point") != "source":
            continue
        room = spec["room"]
        label = (mapping.get("paths", {}).get(name) or {}).get("label", labels.get(room, room))
        suffix = name.removeprefix(f"loc_{room}").removesuffix("_source").strip("_")
        if suffix and name not in mapping.get("paths", {}):
            label += " / " + suffix.replace("_", " ").title()
        zones.append((name, room, label))
    return sorted(zones, key=lambda zone: (zone[2], zone[0]))


def _zone_words(mapping: dict) -> dict:
    """{zone id: label}: each routing zone, as zone labels name it."""
    zones = (mapping.get("routing") or {}).get("zones") or {}
    return {zone: str(spec.get("label", zone)) for zone, spec in zones.items()}


def _source_words(mapping: dict) -> dict:
    """{source id: label}: each source as the remotes name it. Where two
    share a label, each adds its device's room ("Apple TV (Theater)"), and
    where that is shared too, its device's label. A value that is no routed
    source keeps its device's label."""
    devices = mapping.get("devices") or {}
    rooms = _room_words(mapping)
    labels = mapping.get("sources") or {}

    def shared(words):
        texts = list(words.values())
        return [ident for ident, text in words.items() if texts.count(text) > 1 and ident in devices]

    words = dict(labels)
    for ident in shared(words):
        room = devices[ident]["room"]
        words[ident] = f"{labels[ident]} ({rooms.get(room, room)})"
    for ident in shared(words):
        words[ident] = f"{labels[ident]} ({devices[ident].get('label', ident)})"
    return {**{ident: device.get("label", ident) for ident, device in devices.items()}, **words}


#: A source timeline's number for a zone whose readback is NULL or UNDEF.
UNAVAILABLE = -1


def _source_timeline(mapping: dict, zones: list[tuple[str, str, str]]) -> tuple[list[tuple[str, str]], list[dict]]:
    """The queries and the value mapping of a source timeline: each source a
    number, shown as the remotes name it (_source_words) in tan, Off in
    slate, and a readback that could not be read as Unavailable in grey, so
    that a zone whose devices stopped answering keeps its row instead of
    vanishing."""
    words = _source_words(mapping)
    numbers: dict[str, int] = {}
    queries = []
    for name, _, label in zones:
        terms = []
        for value in mapping["items"][name].get("options") or []:
            numbers.setdefault(value, len(numbers))
            terms.append(f'thermite_item_info{{item={_q(name)},value={_q(value)}}} * {numbers[value]}')
        if terms:
            terms.append(f'thermite_item_unavailable{{item={_q(name)}}} * {UNAVAILABLE}')
            queries.append((f'sum without (value) ({" or ".join(terms)})', label))
    names = {number: ("Off", OFF) if value == "off" else (words.get(value, value), IN_USE)
             for value, number in sorted(numbers.items(), key=lambda pair: pair[1])}
    return queries, value_map({UNAVAILABLE: ("Unavailable", UNKNOWN), **names})


def _room_words(mapping: dict) -> dict:
    return {HOUSE_ROOM: HOUSE_LABEL, **(mapping.get("rooms") or {})}


def _device_words(mapping: dict) -> dict:
    return {ident: device.get("label", ident) for ident, device in (mapping.get("devices") or {}).items()}


def _points(mapping: dict, device: str) -> set[str]:
    return {spec["point"] for spec in (mapping.get("items") or {}).values() if spec.get("device") == device}


def _power_rows(mapping: dict) -> list[tuple[str, str, str]]:
    """(room, power Item, row label) of every device power Item. An Item a
    zone's route needs on (its power check) shows in that zone's room, so a
    receiver's zone 2 shows where zone 2 plays; one that several rooms need,
    or none, shows in its device's room. A device with several power Items
    names each row by its Item ("Receiver · Zone 2")."""
    items = mapping.get("items") or {}
    devices = mapping.get("devices") or {}
    needed: dict[str, set[str]] = {}
    for check in mapping.get("health_checks") or []:
        if check.get("check") == "power" and "item" in check.get("labels", {}):
            needed.setdefault(check["labels"]["item"], set()).update(
                str((items.get(demand["item"]) or {}).get("room", "")) for demand in check["required"])
    power = {name: spec for name, spec in items.items() if spec.get("point") == "power" and spec.get("device")}
    rows = []
    for name, spec in power.items():
        ident = spec["device"]
        label = str((devices.get(ident) or {}).get("label", ident))
        if sum(each["device"] == ident for each in power.values()) > 1:
            part = name.removeprefix(f"{ident}_").removesuffix("_power").replace("_", " ")
            label += " · " + re.sub(r"([a-z])([0-9])", r"\1 \2", part).capitalize()
        rooms = needed.get(name, set())
        rows.append((rooms.pop() if len(rooms) == 1 else str(spec.get("room", "")), name, label))
    return sorted(rows, key=lambda row: (row[2], row[1]))


def _deadline(mapping: dict) -> float | None:
    """Seconds routing.js waits for a choice's readback (the map's
    routing.deadline_seconds): one not shown by then counts as `deadline`."""
    return (mapping.get("routing") or {}).get("deadline_seconds")


def _within(mapping: dict) -> str:
    deadline = _deadline(mapping)
    return f"within {deadline:g} s" if deadline else "by its routing deadline"


def _switch_thresholds(mapping: dict) -> dict:
    """A switch time is amber from half the routing deadline, red from the deadline."""
    deadline = _deadline(mapping)
    return _thresholds((OK, None), *(((WARN, round(deadline / 2)), (FAIL, deadline)) if deadline else ()))


# ---------------------------------------------------------------- house health


def _attention(mapping: dict) -> dict:
    """Needs attention now: failing or unknown required checks, and
    quarantined devices, in words."""
    failing = f'{alerts.CHECK}{{required="true"}} != 0'
    quarantined = 'label_replace(thermite_routing_device_quarantined == 1, "check", "quarantined", "", "")'
    panel = table("Needs attention now", f"({failing}) or ({quarantined})",
                  ["room", "zone", "check", "device", "Value"],
                  "Required control checks that fail or have no report, and devices whose routing commands are "
                  "quarantined. A device is required while a room's selected source uses it; routing "
                  "infrastructure always is. A sleeping device that no room uses is never listed. Zone: which of "
                  "the room's source selectors needs it.",
                  names={"room": "Room", "zone": "Zone", "device": "Device", "check": "Problem", "Value": "State"},
                  no_value="Nothing needs attention")
    # Room, zone and problem first: a phone shows those, and scrolls to the rest.
    _column(panel, "room", {"id": "mappings", "value": _words(_room_words(mapping))},
            {"id": "custom.width", "value": 130})
    _column(panel, "zone", {"id": "mappings", "value": _words(_zone_words(mapping))},
            {"id": "custom.width", "value": 130})
    _column(panel, "check", {"id": "mappings", "value": _words(CHECK_WORDS)})
    _column(panel, "device", {"id": "mappings", "value": _words(_device_words(mapping))})
    _column(panel, "Value", *_mapped_cells(CHECK_STATE), {"id": "custom.width", "value": 100})
    panel["options"]["sortBy"] = [{"displayName": "Room", "desc": False}]
    return panel


def _firing(mapping: dict) -> dict:
    panel = table("Alerts firing", 'ALERTS{alertstate="firing",alertname!="ThermiteWatchdog"}',
                  ["alertname", "room", "zone", "device", "check", "severity"], "Alerts firing now. Critical ones "
                  "will push to a phone once phone delivery is set up; warnings will wait for a digest.",
                  names={"alertname": "Alert", "room": "Room", "zone": "Zone", "device": "Device", "check": "Check",
                         "severity": "Severity"},
                  no_value="No alerts")
    _column(panel, "alertname", {"id": "mappings", "value": _words(
        {name: title for name, (title, _) in alerts.CATALOG.items()})})
    _column(panel, "room", {"id": "mappings", "value": _words(_room_words(mapping))},
            {"id": "custom.width", "value": 130})
    _column(panel, "zone", {"id": "mappings", "value": _words(_zone_words(mapping))},
            {"id": "custom.width", "value": 130})
    _column(panel, "device", {"id": "mappings", "value": _words(_device_words(mapping))})
    _column(panel, "check", {"id": "mappings", "value": _words(CHECK_WORDS)})
    _column(panel, "severity", *_mapped_cells(SEVERITY), {"id": "custom.width", "value": 90})
    return panel


def _known(expr: str, valid: str) -> str:
    """`expr` while `valid` holds, else NaN, which NAN_UNKNOWN shows as Unknown."""
    return f"(({expr}) and on() {valid}) or vector(NaN)"


def _since(panel: dict) -> dict:
    panel["timeFrom"] = SINCE_EVENING
    panel["hideTimeOverride"] = True
    return panel


def house(mapping: dict, probes: list[dict]) -> dict:
    board = Board()
    zones = _zones(mapping)
    board.add(stat("Control", alerts.HEALTH, mappings=CONTROL,
                   description="Every check a room's selected source needs, routing infrastructure, the runtime "
                               "and monitoring. A sleeping device no room uses is never a failure; a missing "
                               "report is Unknown. Needs attention now lists the checks. This checks control, not "
                               "picture or sound."), 4, 4)
    ready = (f'count(count by (zone) ({alerts.CHECK}{{check="source"}} == 0) unless on(zone) '
             f'({alerts.CHECK}{{required="true",zone!=""}} != 0)) or vector(0)')
    board.add(stat("Rooms ready", ready, f"suffix: of {len(zones)}", steps=((WARN, None), (OK, len(zones) or 1)),
                   description="Routing zones whose source is known and whose required checks all pass. A zone "
                               "that is Off and reports Off is ready."), 4, 4)
    # Only where the gateway is installed, as for ThermiteGatewayDown: elsewhere its probe always fails.
    down = f'(max(probe_success{{job="{alerts.GATEWAY_JOB}"}}) == bool 0) and on() ({alerts.GATEWAY_INSTALLED})'
    stale = 'max(min_over_time(thermite_gateway_upstream_state{state="connecting"}[5m])) or vector(0)'
    timeouts = f'(sum(increase(thermite_client_events_total{{kind=~"{TIMEOUTS}"}}[15m])) > bool 0) or vector(0)'
    # The worst of the three: 3 down, else 2 not live, else 1 timing out, else 0.
    board.add(stat("Remotes", f"clamp_max(3 * ({down}) + clamp_min(2 * ({stale}), scalar({timeouts})), 3)",
                   mappings=REMOTES,
                   description="Gateway down: the local HTTPS check of the gateway fails. Not live: for 5 minutes "
                               "the gateway has been reconnecting to the runtime's events, so remote pages show "
                               "states as they were. Timing out: remote pages gave up waiting for the gateway in "
                               "the last 15 minutes. Unknown: this host has no gateway, or nothing has checked it "
                               "yet."), 4, 4)
    board.add(stat("Alerts firing", 'count(ALERTS{alertstate="firing",alertname!="ThermiteWatchdog"}) or vector(0)',
                   steps=((OK, None), (WARN, 1)), description="Alerts firing now, without the monitoring heartbeat."),
              4, 4)
    phone = stat("Phone delivery", "", description="Alerts reach a local ntfy that listens on this host only, so "
                 "no phone receives them yet. Subscribers: clients of that ntfy now. Last delivered: since ntfy "
                 "last accepted an alert or the heartbeat.")
    phone["targets"] = _targets([("max(ntfy_subscribers_total) or vector(0)", "Subscribers"),
                                 ("time() - max(thermite_notification_last_success_timestamp_seconds > 0)",
                                  "Last delivered")], instant=True)
    phone["fieldConfig"]["defaults"]["thresholds"] = _thresholds((UNKNOWN, None), (OK, 1))
    phone["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "Last delivered"}, "properties": [
        {"id": "unit", "value": "dtdurations"}, {"id": "decimals", "value": 0},
        {"id": "thresholds", "value": _thresholds((OK, None), (WARN, 1800))}]}]
    phone["options"].update(textMode="value_and_name", colorMode="value", orientation="horizontal",
                            text={"titleSize": 11, "valueSize": 16})
    board.add(phone, 4, 4)
    problems = ('(count(up == 0) or vector(0)) + (count(node_systemd_unit_state{job="node",name=~"thermite-.*",'
                'state="failed"} == 1) or vector(0)) + (count((node_filesystem_avail_bytes{job="node",'
                'fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes < 0.10) and '
                '(node_filesystem_readonly == 0)) or vector(0))')
    host_tile = stat("Host", problems, "suffix: problems", mappings=value_map({0: ("OK", OK)}),
                     description="Scrape targets down, failed Thermite units and file systems under 10% free. "
                                 "The Host board says which.")
    host_tile["fieldConfig"]["defaults"]["thresholds"] = _thresholds((OK, None), (FAIL, 1))
    board.add(host_tile, 4, 4)
    board.add(_attention(mapping), 14, 8)
    # Routing's counts read 0 only while its telemetry is valid, else
    # Unknown; device failures only where an executor counts them, while its
    # status is valid too.
    executor = bool((mapping.get("routing") or {}).get("status"))
    counted = ROUTING_COUNTS_VALID + (f" and on() {EXECUTOR_VALID}" if executor else "")
    since = stat("Since yesterday evening", "", steps=((OK, None), (WARN, 1)),
                 description="Since 18:00 yesterday (browser time). Source changes: choices routing followed. "
                             f"Slow or missed: not confirmed {_within(mapping)}. Remotes gave up: requests remote "
                             "pages stopped waiting for. " + ("Device failures: routing operations a device did not "
                             "take. " if executor else "") + "Restarts: Thermite services systemd restarted. Unknown: "
                             "routing's telemetry is unreadable now.")
    since["targets"] = _targets([
        (_known("round(sum(increase(thermite_route_seconds_count[$__range]))) or vector(0)", ROUTING_COUNTS_VALID),
         "Source changes"),
        (_known('round(sum(increase(thermite_route_seconds_count{outcome="deadline"}[$__range]))) or vector(0)',
                ROUTING_COUNTS_VALID), "Slow or missed"),
        (f'round(sum(increase(thermite_client_events_total{{kind=~"{TIMEOUTS}"}}[$__range]))) or vector(0)',
         "Remotes gave up"),
        *([(_known("round(sum(increase(thermite_routing_endpoint_failures_total[$__range]))) or vector(0)", counted),
            "Device failures")] if executor else []),
        ('round(sum(increase(node_systemd_service_restart_total{job="node",name=~"thermite-.*"}[$__range]))) '
         'or vector(0)', "Restarts")], instant=True)
    since["fieldConfig"]["defaults"]["mappings"] = NAN_UNKNOWN
    _color(since, "Source changes", IN_USE)
    since["options"].update(textMode="value_and_name", colorMode="value", orientation="horizontal",
                            text={"titleSize": 12, "valueSize": 20})
    board.add(_since(since), 10, 8)
    queries, names = _source_timeline(mapping, zones)
    if queries:
        board.add(state_timeline("Rooms through the day", queries, names,
                                 "Each zone's confirmed source readback, sampled every 30 s; not the requested "
                                 "source. Tan: a source is selected. Slate: Off. Grey: Unavailable, the room's "
                                 "devices could not be read. A gap: no data.", show_values=True),
                  24, max(6, len(queries) + 2))
    else:
        board.add(note("No routing zones", "This house has no routed source Items; Devices shows each device."), 24, 3)
    # Every zone's series is there at zero from routing's first report, and the quantile of no changes is
    # NaN: only a room with a confirmed change in the range gets a row, so an idle house reads "No source
    # changes".
    changes = 'round(sum by (zone) (increase(thermite_route_seconds_count{outcome="confirmed"}[$__range]))) > 0'
    quantile = ("histogram_quantile({q}, sum by (le, zone) (increase(thermite_route_seconds_bucket"
                '{{outcome="confirmed"}}[$__range]))) and on (zone) ({changes})')
    switch = table("Switch time by room", "", ["zone", "Value #A", "Value #B", "Value #C"],
                   "Since 18:00 yesterday: how long a source change took until the room's readback showed it, "
                   "median and 95th percentile of confirmed changes, estimated from "
                   f"{', '.join(map(str, ROUTE_BUCKETS[:-1]))} and {ROUTE_BUCKETS[-1]} s buckets. Amber from half "
                   f"the routing deadline, red from the deadline: a change not confirmed {_within(mapping)} "
                   "counts as slow or missed instead.",
                   names={"zone": "Room", "Value #A": "Median", "Value #B": "95th",
                                      "Value #C": "Changes"}, no_value="No source changes")
    _table_queries(switch, [
        ("A", quantile.format(q=0.5, changes=changes)), ("B", quantile.format(q=0.95, changes=changes)),
        ("C", changes)])
    _column(switch, "zone", {"id": "mappings", "value": _words(_zone_words(mapping))})
    for name in ("Value #A", "Value #B"):
        _column(switch, name, {"id": "unit", "value": "s"}, {"id": "decimals", "value": 1},
                {"id": "thresholds", "value": _switch_thresholds(mapping)},
                {"id": "custom.cellOptions", "value": {"type": "color-text"}})
    _column(switch, "Value #C", {"id": "decimals", "value": 0})
    switch["options"]["sortBy"] = [{"displayName": "95th", "desc": True}]  # the slowest room first
    height = max(6, min(len(zones), 12) + 2)
    board.add(_since(switch), 12, height)
    board.add(_firing(mapping), 12, height)
    return dashboard(HOUSE, board, "Is the house OK? Control, rooms, remotes, alerts, phone delivery and the host; "
                     "what needs attention now; each room's source; switching since yesterday evening.")


# ---------------------------------------------------------------- switching and remotes


def switching(mapping: dict) -> dict:
    board = Board()
    zones = _zone_words(mapping)
    board.row("Switching sources")
    board.add(heatmap("Time to switch", 'sum by (le) (increase(thermite_route_seconds_bucket{outcome="confirmed"}'
                      '[$__interval]))', "How long confirmed source changes took, from the choice until the "
                      "room's readback showed it, per quarter hour. Darker cells: more changes."), 12, 8)
    outcomes = timeseries("Source changes by outcome", [
        ("sum by (outcome) (increase(thermite_route_seconds_count[1h]))", "{{outcome}}")], minimum=0, stack=True,
        bars=True, description="Per hour. Confirmed: the readback showed the choice. Deadline: it did not "
                               f"{_within(mapping)}. Superseded: a newer choice came first.")
    for name, color in (("confirmed", OK), ("deadline", FAIL), ("superseded", UNKNOWN)):
        _color(outcomes, name, color, name.capitalize())
    board.add(outcomes, 12, 8)
    executor = timeseries("Routing executor", [
        ("thermite_routing_pending", "Waiting"), ("thermite_routing_active", "Running"),
        ("thermite_routing_events_queued", "Outcomes queued"), ("thermite_routing_quarantined", "Quarantined")],
        minimum=0, description="Device operations the routing executor holds. A quarantined device refuses "
                               "commands until a recovery read succeeds.")
    _color(executor, "Quarantined", FAIL)
    board.add(executor, 8, 7)
    resends = timeseries("Resends by room", [
        ("sum by (zone) (increase(thermite_routing_resends_total[1h])) > 0", "{{zone}}")], minimum=0, bars=True,
        description="Per hour: rounds in which routing sent a room's commands again because a device had not "
                    "taken them (often one still waking).")
    resends["fieldConfig"]["defaults"]["mappings"] = []
    for zone, label in zones.items():
        resends["fieldConfig"]["overrides"].append(
            {"matcher": {"id": "byName", "options": zone}, "properties": [{"id": "displayName", "value": label}]})
    board.add(resends, 8, 7)
    operations = timeseries("Operations by outcome", [
        ("sum by (outcome) (increase(thermite_routing_operations_total[1h])) > 0", "{{outcome}}")], minimum=0,
        bars=True, stack=True, description="Per hour: what became of each executor operation. Applied and sampled "
                                           "are normal; the red ones are device failures.")
    _color(operations, "applied", OK)
    for name in FAILED_OPERATIONS:
        _color(operations, name, FAIL)
    board.add(operations, 8, 7)
    failures = table("Device failures", "sum by (device, reason) (increase(thermite_routing_endpoint_failures_total"
                     "[$__range])) > 0", ["device", "reason", "Value"], "Routing operations a device did not take "
                     "in the selected range, by reason.",
                     names={"device": "Device", "reason": "Reason", "Value": "Failures"}, no_value="No device failures")
    _column(failures, "device", {"id": "mappings", "value": _words(_device_words(mapping))})
    _column(failures, "reason", {"id": "custom.width", "value": 140})
    _column(failures, "Value", {"id": "decimals", "value": 0}, {"id": "custom.width", "value": 90})
    board.add(failures, 12, 6)
    quarantined = table("Quarantined now", "thermite_routing_device_quarantined == 1", ["room", "device"],
                        "Devices whose routing commands are refused now.", names={"room": "Room", "device": "Device"},
                        no_value="No device is quarantined")
    _column(quarantined, "room", {"id": "mappings", "value": _words(_room_words(mapping))})
    _column(quarantined, "device", {"id": "mappings", "value": _words(_device_words(mapping))})
    board.add(quarantined, 12, 6)
    board.row("Remotes and the gateway")
    streams = timeseries("Event streams", [
        ("sum by (http_version) (thermite_gateway_sse_streams)", "HTTP/{{http_version}} streams"),
        ('max(thermite_gateway_sse_peer_streams_max{http_version="1.1"})', "Most on one HTTP/1.1 client")],
        minimum=0, description=f"Open event streams by HTTP version. Over HTTP/1.1 a browser opens at most "
                               f"{HTTP1_LIMIT} connections to the gateway, so one client holding {HTTP1_LIMIT} "
                               "streams starves its own requests (the red band).")
    streams["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] = {"mode": "line+area"}
    streams["fieldConfig"]["defaults"]["thresholds"] = _thresholds(("transparent", None), (FAIL, HTTP1_LIMIT))
    board.add(streams, 8, 8)
    dropped = timeseries("Event stream failures", [
        (f'sum by (client, kind) (increase(thermite_client_events_total{{kind=~"{STREAM_FAILURES}"}}[1h])) > 0',
         "{{client}} {{kind}}"),
        ("sum by (limit) (increase(thermite_gateway_sse_refused_total[1h])) > 0", "refused at the {{limit}} limit")],
        minimum=0, bars=True, stack=True,
        description="Per hour: event streams remote pages and the terminal remote saw fail (stream_error, as at "
                    "every gateway restart) or refused (stream_refused), and streams the gateway refused at its "
                    "per-client (peer) or total limit. Remotes reconnect; it is not giving up.")
    dropped["fieldConfig"]["defaults"]["color"] = {"mode": "shades", "fixedColor": UNKNOWN}
    board.add(dropped, 8, 8)
    gave_up = timeseries("Remotes gave up", [
        (f'sum by (client, kind) (increase(thermite_client_events_total{{kind=~"{TIMEOUTS}"}}[1h])) > 0',
         "{{client}} {{kind}}")],
        minimum=0, bars=True, stack=True,
        description="Per hour: requests remote pages and the terminal remote stopped waiting for (timeouts), as "
                    "they reported them on their next answered request.")
    gave_up["fieldConfig"]["defaults"]["color"] = {"mode": "shades", "fixedColor": WARN}
    board.add(gave_up, 8, 8)
    board.add(timeseries("Gateway latency", [
        ('histogram_quantile(0.95, sum by (le, route) (rate(thermite_gateway_request_seconds_bucket'
         f'{{route=~"{"|".join(WAITED_ROUTES)}"}}[5m]))) > 0', "{{route}} 95th"),
        ('histogram_quantile(0.95, sum by (le) (rate(thermite_gateway_upstream_request_seconds_bucket[5m]))) > 0',
         "runtime 95th")], "s", minimum=0,
        description="95th percentile time to the first byte of the answer for the requests remotes wait on, "
                    "and of the gateway's own requests to the runtime."), 12, 8)
    board.add(timeseries("Waited before giving up", [
        ('histogram_quantile(0.95, sum by (le, kind) (rate(thermite_client_wait_seconds_bucket'
         f'{{kind=~"{TIMEOUTS}"}}[1h]))) > 0', "{{kind}} 95th")], "s", minimum=0,
        description="How long remotes had waited, queued and in flight, when they gave up."), 12, 8)
    requests = timeseries("Requests by answer", [
        ("sum by (code_class) (rate(thermite_gateway_requests_total[5m])) > 0", "{{code_class}}")], "reqps",
        minimum=0, description="Gateway requests per second by status class, on both listeners; "
                                           "aborted ones ended before an answer. Includes requests the admission "
                                           "limit refused.")
    for name, color in (("2xx", OK), ("3xx", UNKNOWN), ("4xx", WARN), ("5xx", FAIL), ("aborted", OFF)):
        _color(requests, name, color)
    board.add(requests, 8, 8)
    problems = table("Problem answers", "sum by (code) (increase(thermite_gateway_problems_total[$__range])) > 0",
                     ["code", "Value"], "The gateway's problem codes in the selected range.",
                     names={"code": "Problem", "Value": "Answers"}, no_value="No problems")
    _column(problems, "Value", {"id": "decimals", "value": 0})
    problems["options"]["sortBy"] = [{"displayName": "Answers", "desc": True}]
    board.add(problems, 8, 8)
    board.add(timeseries("Protocols remotes use", [
        ("sum by (protocol) (increase(thermite_client_reports_total[1h])) > 0", "{{protocol}} reports")],
        minimum=0, bars=True, stack=True,
        description="Per hour, the HTTP protocol of remotes that reported: h2 shares one connection, while "
                    "http/1.1 allows a browser six."), 8, 8)
    board.row("Room policy worker")
    policies = timeseries("Locked rooms", [('thermite_policy_rooms{state!="unlocked"}', "{{state}}")], minimum=0,
                          stack=True, description="Rooms locked by hand (manual) or by a schedule (scheduled), and "
                                                  "rooms whose lock cannot be held now (unavailable): an unreadable "
                                                  "policy, a conflict over shared hardware, openHAB not answering, "
                                                  "or a change not yet put back. The other rooms are unlocked.")
    policies["fieldConfig"]["defaults"]["decimals"] = 0
    _color(policies, "manual", IN_USE)
    _color(policies, "unavailable", FAIL)
    board.add(policies, 8, 7)
    board.add(timeseries("Worker", [
        ("time() - thermite_policy_loop_timestamp_seconds", "Since last pass"),
        ("max(thermite_policy_reconcile_age_seconds)", "Oldest locked-room check"),
        ("histogram_quantile(0.95, sum by (le) (rate(thermite_policy_boundary_lag_seconds_bucket[1h]))) > 0",
         "Schedule lag 95th")], "s", minimum=0,
        description="Seconds since the worker's last pass (it runs every 5 s), since it last verified the "
                    "longest-unchecked locked room, and how late schedule boundaries were applied."), 8, 7)
    board.add(timeseries("Worker events", [
        ("sum by (outcome) (increase(thermite_policy_boundaries_total[1h])) > 0", "boundary {{outcome}}"),
        ("sum by (outcome) (increase(thermite_policy_repairs_total[1h])) > 0", "repair {{outcome}}"),
        ("sum by (stage) (increase(thermite_policy_errors_total[1h])) > 0", "error in {{stage}}"),
        ('sum by (outcome) (increase(thermite_callbacks_total{outcome!="forwarded"}[1h])) > 0',
         "callback {{outcome}}")], minimum=0,
        bars=True, description="Per hour: schedule boundaries by outcome, repairs of drifted locked rooms, "
                               "worker errors, and device callbacks the relay refused."), 8, 7)
    return dashboard(SWITCHING, board, "How source changes go, the routing executor, the gateway and its event "
                     "streams, what remotes gave up on, and the room policy worker.")


# ---------------------------------------------------------------- devices


def devices(mapping: dict, probes: list[dict]) -> dict:
    board = Board()
    known = mapping.get("devices") or {}
    panel = table("Devices", "", ["room", "label", "Value #B", "Value #C", "Value #D", "Value #E", "Value #F",
                                  "Value #G"],
                  "One row per device. Thing: the runtime's connection to it. Port: its control port accepts a TCP "
                  "connection; Probe: how long that took. Offline since: when its Thing was last online, within "
                  "a week. Needed now: a room's selected source uses it, or it is routing infrastructure. An "
                  "offline device no room needs is usually asleep.",
                  names={"room": "Room", "label": "Device", "Value #B": "Thing", "Value #C": "Port",
                         "Value #D": "Probe", "Value #E": "Offline since", "Value #F": "Problem",
                         "Value #G": "Needed now"})
    # House-wide checks name a job or service as their device: no row of their own.
    needed = (f'((count by (device) ({alerts.CHECK}{{required="true",device!=""}}) * 0 + 1) '
              'and on(device) thermite_device_info) or (count by (device) (thermite_device_info) * 0)')
    last_online = ("max by (device) (max_over_time(timestamp(thermite_thing_online == 1)[7d:5m])) * 1000 "
                   "and on(device) (min by (device) (thermite_thing_online) == 0)")
    _table_queries(panel, [
        ("A", "max by (device, room, label) (thermite_device_info)"),
        ("B", "min by (device) (thermite_thing_online)"),
        ("C", 'min by (device) (probe_success{job="device_tcp"})'),
        ("D", 'max by (device) (probe_duration_seconds{job="device_tcp"})'),
        ("E", last_online),
        ("F", "max by (device) (thermite_device_problem)"),
        ("G", needed)])
    panel["fieldConfig"]["defaults"]["noValue"] = "—"
    _column(panel, "room", {"id": "mappings", "value": _words(_room_words(mapping))},
            {"id": "custom.width", "value": 150})
    for name, words in (("Value #B", ONLINE), ("Value #C", OPEN), ("Value #F", PROBLEM), ("Value #G", NEEDED)):
        _column(panel, name, *_mapped_cells(words), {"id": "custom.width", "value": 100})
    _column(panel, "Value #D", {"id": "unit", "value": "s"}, {"id": "decimals", "value": 3},
            {"id": "custom.width", "value": 90})
    _column(panel, "Value #E", {"id": "unit", "value": "dateTimeFromNow"}, {"id": "custom.width", "value": 130})
    panel["options"]["sortBy"] = [{"displayName": "Room", "desc": False}]
    board.add(panel, 24, min(30, max(8, len(known) + 2)))
    zones = _zones(mapping)
    queries, names = _source_timeline(mapping, zones)
    by_room: dict[str, list[tuple[str, str]]] = {}
    sources: set[str] = set()
    for (name, room, label), (query, _) in zip(zones, queries):
        by_room.setdefault(room, []).append((query, "Source · " + label))
        sources.add("Source · " + label)
    for room, item, label in _power_rows(mapping):
        by_room.setdefault(room, []).append((f"max(thermite_item_value{{item={_q(item)}}})", label))
    rooms = mapping.get("rooms") or {}
    if by_room:
        board.row("Power and source by room")
    for room in sorted(by_room, key=lambda room: (rooms.get(room, room), room)):
        rows = by_room[room]
        timeline = state_timeline(rooms.get(room, room), rows, ON_OFF, show_values=True,
                                  description="Source: the room's confirmed source. Other rows: device power, On "
                                              "in tan, Off in slate; Off is normal. A gap: unknown.")
        for index, (_, label) in enumerate(rows):
            if label in sources:
                timeline["fieldConfig"]["overrides"].append({
                    "matcher": {"id": "byFrameRefID", "options": chr(ord("A") + index)},
                    "properties": [{"id": "mappings", "value": names},
                                   {"id": "thresholds", "value": mapped_thresholds(names)}]})
        board.add(timeline, 12, max(4, len(rows) + 2))
    volumes = [(f'thermite_item_value{{device={_q(ident)},point="volume"}}', device.get("label", ident))
               for ident, device in sorted(known.items()) if device.get("kind") in LISTENING_KINDS
               and "volume" in _points(mapping, ident)]
    if volumes:
        board.row("Listening levels")
        board.add(timeseries("Listening levels", volumes, minimum=0,
                             description="Receivers', amplifiers' and speakers' volume settings in their own "
                                         "scales: not percentages, not loudness. Unknown stays a gap."), 24, 8)
    return dashboard(DEVICES, board, "Every device's connection, control port, problem and whether a room needs it "
                     "now; then each room's power and source.")


# ---------------------------------------------------------------- openHAB and the host


def _thing_colour(status: str) -> str:
    """Amber, not red, for a Thing that is not ONLINE: a sleeping device's
    Thing is OFFLINE, and House health says whether a room needs it."""
    return OK if status == "ONLINE" else UNKNOWN if status in ("UNINITIALIZED", "INITIALIZING", "UNKNOWN") else WARN


def _busiest(by: str, series: str, scale: int = 1, count: int = 6) -> str:
    """The 5-minute rate of `series` summed by `by`, for the `count` values
    that grew most over the whole selected range: one stable set of series,
    not a new top set at every step."""
    rate = f"sum by ({by}) (rate({series}[5m]))" + (f" * {scale}" if scale != 1 else "")
    return f"{rate} and topk({count}, sum by ({by}) (increase({series}[$__range] @ end())))"


def openhab(mapping: dict | None = None) -> dict:
    m = OPENHAB_METRICS
    job = 'job="openhab"'
    board = Board()
    board.add(stat("Scrape", f"up{{{job}}}", mappings=UP_DOWN), 4, 4)
    board.add(stat("Things ONLINE", f"count({m['thing_state']}{{{job}}} == 3) or vector(0)"), 4, 4)
    board.add(stat("Things not ONLINE", f"count({m['thing_state']}{{{job}}} != 3) or vector(0)",
                   steps=((OK, None), (WARN, 1)),
                   description="Includes devices that are asleep or off; Devices says which are needed now."), 4, 4)
    board.add(stat("Items missing", "thermite_items_missing", steps=((OK, None), (WARN, 1)),
                   description="Items the generated configuration has that the runtime does not: a deploy it has "
                               "not loaded, or an Item file it refused. Their checks read Unknown."), 4, 4)
    board.add(stat("Heap used",
                   f'sum({m["memory_used"]}{{{job},area="heap"}}) / sum({m["memory_max"]}{{{job},area="heap"}} > 0)',
                   "percentunit", ((OK, None), (WARN, 0.8), (FAIL, 0.9))), 4, 4)
    handler = bool((mapping or {}).get("handler_pool"))
    board.add(stat("thingHandler busy", "max(thermite_handler_pool_active_threads / "
                   "(thermite_handler_pool_core_threads > 0))", "percentunit", ((OK, None), (WARN, 0.75), (FAIL, 1)),
                   description="Busy share of the thingHandler pool, where bindings do device I/O. At 100% one "
                               "blocked connection delays every device." + ("" if handler else
                               " This house does not report it.")), 4, 4)
    board.row("JVM and thread pools")
    board.add(timeseries("Heap", [(f'sum({m["memory_used"]}{{{job},area="heap"}})', "used"),
                                  (f'sum({m["memory_max"]}{{{job},area="heap"}} > 0)', "max")], "bytes",
                         minimum=0), 8, 8)
    board.add(timeseries("GC pause time per second", [
        (f'sum by (gc) (rate({m["gc_pause_sum"]}{{{job}}}[5m]))', "{{gc}}")], "percentunit", minimum=0), 8, 8)
    board.add(timeseries("Threads", [(f"{m['threads_live']}{{{job}}}", "live"),
                                     (f"{m['threads_daemon']}{{{job}}}", "daemon")], minimum=0), 8, 8)
    board.add(timeseries("Process CPU", [(f"{m['process_cpu']}{{{job}}}", "cpu")], "percentunit", minimum=0), 8, 8)
    board.add(timeseries("Thread pool use", [
        (f"max by (pool) ({m['pool_active']}{{{job}}} / ({m['pool_threads']}{{{job}}} > 0)) > 0", "{{pool}}")],
        "percentunit", minimum=0, maximum=1,
        description="Busy share of each runtime thread pool (active threads over its size); idle pools are "
                    "left out."), 8, 8)
    board.add(timeseries("Queued pool tasks", [
        (f"sum by (pool) ({m['pool_queued']}{{{job}}}) > 0", "{{pool}}")], minimum=0,
        description="Tasks waiting for a pool thread; pools with none queued are left out."), 8, 8)
    board.add(timeseries("thingHandler pool", [
        ("thermite_handler_pool_active_threads", "busy"), ("thermite_handler_pool_core_threads", "size"),
        ("thermite_handler_pool_queued_tasks", "queued"),
        ("thermite_handler_pool_probe_overdue_seconds", "probe overdue (s)")], minimum=0,
        description="Where bindings do device I/O. A probe overdue while every thread is busy means a blocked "
                    "connection is delaying other devices."), 24, 8)
    board.row("Events and rules")
    board.add(timeseries("Events per second", [(f"sum(rate({m['event_count']}{{{job}}}[5m]))", "all")],
                         "ops", minimum=0), 8, 8)
    board.add(timeseries("Busiest topics", [
        (_busiest("topic", f"{m['event_count']}{{{job}}}"), "{{topic}}")], "ops", minimum=0,
        description="The topics with the most events in the selected range."), 16, 8)
    board.add(timeseries("Rule runs per minute", [
        (f"sum(rate({m['rule_runs']}{{{job}}}[5m])) * 60", "all")], minimum=0), 8, 8)
    board.add(timeseries("Busiest rules, runs per minute", [
        (_busiest("rule", f"{m['rule_runs']}{{{job}}}", 60), "{{rule}}")], minimum=0,
        description="The rules that ran most in the selected range. The runtime counts rule runs; it does not "
                    "time them."), 16, 8)
    board.row("Things")
    names = value_map({index: (name, _thing_colour(name)) for index, name in enumerate(THING_STATE_NAMES)})
    statuses = timeseries("Things by status", [
        (f'count_values("state", {m["thing_state"]}{{{job}}})', "{{state}}")], minimum=0,
        description="Things per ThingStatus.")
    for index, name in enumerate(THING_STATE_NAMES):
        _color(statuses, str(index), _thing_colour(name), name)
    board.add(statuses, 12, 8)
    board.add(state_timeline("Things not ONLINE", [(f"{m['thing_state']}{{{job}}} != 3", "{{thing}}")], names,
                             "Things while they are not ONLINE; a sleeping device's Thing is often OFFLINE.",
                             show_values=True), 12, 8)
    return dashboard(OPENHAB, board, "The runtime's Metrics add-on: JVM, thread pools, the thingHandler pool, "
                     "events, rules and Things.", annotations=False)


def host(targets: int = 0, mapping: dict | None = None) -> dict:
    board = Board()
    node = 'job="node"'
    units = _units(UNITS)
    board.add(stat("Scrape targets", "count(up == 1) or vector(0)", f"suffix:/{targets} up" if targets else "none",
                   steps=((FAIL, None), (OK, targets or 1)),
                   description="Prometheus targets that answered their last scrape. Each device and service "
                               "probe is a target; a probe that fails still answers."), 6, 4)
    board.add(stat("CPU busy", f'1 - avg(rate(node_cpu_seconds_total{{{node},mode="idle"}}[5m]))', "percentunit",
                   ((OK, None), (WARN, 0.7), (FAIL, 0.9))), 4, 4)
    board.add(stat("Memory available",
                   f"node_memory_MemAvailable_bytes{{{node}}} / node_memory_MemTotal_bytes{{{node}}}", "percentunit",
                   ((FAIL, None), (WARN, 0.1), (OK, 0.2))), 5, 4)
    board.add(stat("Root free", f'node_filesystem_avail_bytes{{{node},mountpoint="/"}} / '
                                f'node_filesystem_size_bytes{{{node},mountpoint="/"}}', "percentunit",
                   ((FAIL, None), (WARN, 0.1), (OK, 0.2))), 4, 4)
    board.add(stat("Metrics storage", "sum(prometheus_tsdb_storage_blocks_bytes) + "
                                      "sum(prometheus_tsdb_wal_storage_size_bytes)", "bytes",
                   steps=((UNKNOWN, None),)), 5, 4)
    board.add(table("Targets down", "up == 0", ["job", "instance"], "Scrape targets that did not answer.",
                    names={"job": "Job", "instance": "Target"}, no_value="Every target is up"), 12, 6)
    board.add(_firing(mapping or {}), 12, 6)
    board.row("Services")
    board.add(state_timeline("Units active", [
        (f'node_systemd_unit_state{{{node},name=~{units},state="active"}}', "{{name}}")], UP_DOWN,
        "Thermite's long-running services, including the gateway, its policy worker and its callback relay "
        "where they are installed."), 16, max(8, len(UNITS) + 2))
    restarts = table("Restarts", f'round(sum by (name) (increase(node_systemd_service_restart_total{{{node},'
                     f'name=~"thermite-.*"}}[$__range]))) > 0', ["name", "Value"],
                     "Restarts systemd made in the selected range; units without any are left out.",
                     names={"name": "Unit", "Value": "Restarts"}, no_value="No restarts")
    _column(restarts, "Value", {"id": "decimals", "value": 0})
    board.add(restarts, 8, max(8, len(UNITS) + 2))
    board.add(state_timeline("Service probes", [
        ('probe_success{job=~"service_http|alerting_http"}', "{{service}}"),
        (f'probe_success{{job="{alerts.GATEWAY_JOB}"}} and on() ({alerts.GATEWAY_INSTALLED})', "{{service}}")],
        UP_DOWN, "Local services answering their checks; the gateway only where it is installed."), 12, 7)
    board.add(timeseries("Metrics textfiles age", [("time() - node_textfile_mtime_seconds", "{{file}}")], "s",
                         minimum=0, description="Seconds since each access service last wrote its metrics; it "
                                                "writes every 10 s while it runs."), 12, 7)
    board.row("Load")
    board.add(timeseries("CPU", [(f'sum by (mode) (rate(node_cpu_seconds_total{{{node},mode!="idle"}}[5m])) / '
                                  f'scalar(count(node_cpu_seconds_total{{{node},mode="idle"}}))', "{{mode}}")],
                         "percentunit", stack=True, minimum=0), 12, 8)
    board.add(timeseries("Load", [(f"node_load1{{{node}}}", "1m"), (f"node_load5{{{node}}}", "5m"),
                                  (f"node_load15{{{node}}}", "15m")], minimum=0), 12, 8)
    board.add(timeseries("Memory", [
        (f"node_memory_MemTotal_bytes{{{node}}} - node_memory_MemAvailable_bytes{{{node}}}", "used"),
        (f"node_memory_MemAvailable_bytes{{{node}}}", "available")], "bytes", stack=True, minimum=0), 12, 8)
    board.add(timeseries("Temperatures", [(f"node_hwmon_temp_celsius{{{node}}}", "{{chip}} {{sensor}}")],
                         "celsius"), 12, 8)
    board.row("Disk")
    board.add(timeseries("Free space", [
        (f'node_filesystem_avail_bytes{{{node},fstype!~"tmpfs|overlay|squashfs"}}', "{{mountpoint}}")],
        "bytes", minimum=0), 8, 8)
    btrfs = timeseries("btrfs", [
        (f"sum by (device) (node_btrfs_device_unused_bytes{{{node}}})", "{{device}} unallocated"),
        (f"sum by (block_group_type) (node_btrfs_size_bytes{{{node}}} - node_btrfs_used_bytes{{{node}}})",
         "{{block_group_type}} free in its chunks"),
        (f"sum by (device) (increase(node_btrfs_device_errors_total{{{node}}}[1h])) > 0", "{{device}} errors")],
        "bytes", minimum=0, description="Unallocated: what btrfs can still turn into data or metadata chunks; "
                                        "when it runs out, metadata can fill while df still shows free space. "
                                        "Errors (per hour): new device read, write, flush, checksum or "
                                        "generation errors.")
    btrfs["fieldConfig"]["overrides"].append({"matcher": {"id": "byRegexp", "options": ".* errors"}, "properties": [
        {"id": "unit", "value": "short"}, {"id": "color", "value": {"mode": "fixed", "fixedColor": FAIL}},
        {"id": "custom.drawStyle", "value": "bars"}]})
    board.add(btrfs, 8, 8)
    board.add(timeseries("Disk I/O", [
        (f"rate(node_disk_read_bytes_total{{{node}}}[5m])", "{{device}} read"),
        (f"rate(node_disk_written_bytes_total{{{node}}}[5m])", "{{device}} write")], "Bps", minimum=0), 8, 8)
    return dashboard(HOST, board, "This host: its scrape targets, the Thermite units and their restarts, firing "
                     "alerts, CPU, memory, disk and btrfs.")


# ---------------------------------------------------------------- usage


def usage(mapping: dict) -> dict:
    board = Board()
    zones = _zones(mapping)
    active = 'thermite_item_info{point="source",value!~"off|other"}'
    observed = 'thermite_item_info{point="source",value!="other"}'
    available = "thermite_openhab_up == 1"
    # A known zero is valid only while the exporter can read the runtime.
    active_count = f"(sum({active}) or vector(0)) and on() ({available})"
    unknown_count = f"({len(zones)} - (count({observed}) or vector(0))) and on() ({available})"
    board.add(note("How the house is used", "Selected sources through the day, **not** a health signal: House "
                   "health is. Off is normal; gaps mean unknown. History builds from installation, with no "
                   "backfill."), 24, 2)
    if not zones:
        board.add(note("No source observations configured", "This house has no routed source Items; no use is "
                       "inferred from power or reachability."), 24, 3)
        return dashboard(USAGE, board, "Observed room/source use; Off is not a failure.", annotations=False)
    use = timeseries("The house through the day", [
        (active_count, "In use"),
        (f'(sum(thermite_item_info{{point="source",value="off"}}) or vector(0)) and on() ({available})', "Off"),
        (unknown_count, "Unobserved")], stack=True, minimum=0, maximum=len(zones),
        description="Routing zones by observed state. Off is normal; unknown source state is shown separately. "
                    "Runtime outages remain gaps. No presence, playback or exact session is inferred.")
    for name, color in (("In use", IN_USE), ("Off", OFF), ("Unobserved", UNKNOWN)):
        _color(use, name, color)
    use["fieldConfig"]["defaults"]["custom"]["fillOpacity"] = 85
    board.add(use, 24, 7)
    duration = []
    for name, _, label in zones:
        used = f'sum(count_over_time(thermite_item_info{{item={_q(name)},value!~"off|other"}}[$__range]))'
        known = f'sum(count_over_time(thermite_item_info{{item={_q(name)},value!="other"}}[$__range]))'
        duration.append(f'label_replace((({used}) or ({known}) * 0) * {SAMPLE_SECONDS}, "zone", {_q(label)}, "", "")')
    board.add(bars("Where time was spent", [(" or ".join(duration), "{{zone}}")], "Observed time each zone had a "
                   "source selected in the selected range: samples times the 30-second scrape interval. "
                   "Overlapping zones add; gaps are not extrapolated. Selected-source time, not people or playback.",
                   horizontal=True), 12, 9)
    words = _source_words(mapping)
    mix_expr = f"sum by (value) (count_over_time({active}[$__range])) * {SAMPLE_SECONDS} >= 60"
    mix = timeseries("Source mix", [(mix_expr, "{{value}}")], "dtdurations",
                     description="Share of observed in-use zone time by source. The same source in two zones "
                                 "counts twice; Off, unknown and sources used under a minute are left out.")
    mix["fieldConfig"]["defaults"]["decimals"] = 1  # hours and minutes
    mix.update(type="piechart", targets=_targets([(mix_expr, "{{value}}")], instant=True),
               options={"pieType": "donut", "displayLabels": ["percent"],
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "legend": {"showLegend": True, "displayMode": "table", "placement": "right",
                                   "values": ["value", "percent"]}, "tooltip": {"mode": "single", "sort": "desc"}})
    for value in sorted({v for name, _, _ in zones for v in mapping["items"][name].get("options") or []} - {"off"}):
        mix["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": value}, "properties": [
            {"id": "displayName", "value": words.get(value, value)}]})
    board.add(mix, 12, 9)
    return dashboard(USAGE, board, "How the house is used: zones in use, where time went and the source mix. "
                     "Off is normal; missing observations stay unknown.", annotations=False)


def yoke() -> dict:
    """Passive control observations, separate from core home-control health."""
    board = Board()
    job = 'job="yoke"'
    device = "{{instance}} {{node_id}}"
    board.add(note("About these observations",
                   "Yoke observers passively read existing announcements. Device up means a recent "
                   "announcement, not verified hardware or media health. State is the last accepted "
                   "announcement, even after a peer becomes silent. Counters cover retained observations "
                   "and reset on observer restart or peer eviction. Thermite scrapes every 30 seconds; "
                   "the observer samples once per second by default. Missing series remain unknown."), 24, 4)
    board.add(state_timeline("Observer scrape health", [(f"up{{{job}}}", "{{instance}}")], UP_DOWN), 12, 7)
    board.add(state_timeline("Announcement liveness", [(f"yoke_device_up{{{job}}}", device)], UP_DOWN), 12, 7)
    board.add(table("Last announced state", f"yoke_device_state{{{job}}} == 1",
                    ["instance", "node_id", "state"],
                    "Last accepted announcement; consult liveness before treating it as current."), 12, 7)
    board.add(timeseries("Time since announcement", [(f"yoke_device_last_seen_age_seconds{{{job}}}", device)],
                         "s", minimum=0), 12, 7)
    board.add(timeseries("Observed control frames", [(f"rate(yoke_device_control_frames_total{{{job}}}[5m])", device)],
                         "ops", minimum=0), 12, 7)
    board.add(timeseries("Observed control bytes", [(f"rate(yoke_device_control_bytes_total{{{job}}}[5m])", device)],
                         "Bps", minimum=0, description="Control datagrams only; this is not media throughput."), 12, 7)
    board.add(timeseries("Observed device events", [(f"rate(yoke_device_events_total{{{job}}}[5m])",
                                                     device + " {{event}}")], "ops", minimum=0), 12, 7)
    board.add(timeseries("Observer datagrams", [(f"rate(yoke_observer_datagrams_total{{{job}}}[5m])",
                                                "{{instance}} {{verdict}}")], "ops", minimum=0), 12, 7)
    return dashboard(YOKE, board, "Optional Yoke observers: announcement liveness, last state and control traffic.",
                     refresh="30s", time_from="now-1h", annotations=False)


def render(mapping: dict, probes: list[dict], *, yoke_enabled: bool = False, targets: int = 0) -> dict[str, dict]:
    """{name: dashboard} for the exporter map and the device probes; `targets`
    is how many scrape targets prometheus.yml has."""
    result = {HOUSE: house(mapping, probes), SWITCHING: switching(mapping), DEVICES: devices(mapping, probes),
              OPENHAB: openhab(mapping), HOST: host(targets, mapping), USAGE: usage(mapping)}
    if yoke_enabled:
        result[YOKE] = yoke()
    return result
