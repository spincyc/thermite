"""Grafana dashboards for the house, built from the exporter map.

Six default dashboards and an optional Yoke dashboard, each with a fixed
uid, all querying the one provisioned datasource by its fixed uid:

    house      House health, the home page: is the house OK? Control,
               rooms, speakers and remotes at a glance and what needs
               attention now in plain words; then alerts and the
               host, the alerts firing, and switching since
               yesterday evening. No usage: that is the Usage board's
    switching  Switching & remotes: how long source changes take, the
               routing executor, the gateway's latency, event streams and
               problems, what remotes gave up on, and the policy worker
    devices    Devices: one row per device, trouble first (Thing, port,
               problem, whether a room needs it now), the Things not
               online, then every source, power and speaker through the
               day; its Room variable narrows all of it to some rooms
    openhab    openHAB runtime: the runtime's Metrics add-on (JVM, events,
               rules, Things, thread pools) and its thingHandler pool
    host       Host: scrape targets, the Thermite units and their restarts,
               firing alerts, CPU, memory, disk and btrfs
    usage      Usage: how the house is used (each room's source and
               speakers through the day, zone time, source mix, what
               played); nothing on it is a health signal
    yoke       optional passive endpoint observations and observer health

Colour means one thing everywhere: green OK, amber warning, red failure,
grey unknown, slate Off, and tan for use. A panel whose values
have mapped colours gets threshold steps of the same colours, so whichever
Grafana applies, the colour is the mapping's. State timelines use a fixed
grey, never threshold colours: with those, Grafana merges values into
threshold ranges ("9+") and drops the mappings' words.

On a chart a series takes the colour of its state or outcome. One that only
measures what is used or done (requests, memory, time) is tan, several of a
kind in shades of it; dashes and dots tell a chart's few lines apart, or
mark a limit or a total the others are read against. Grafana's own palette
is never used: it hands out green and red by series order, whatever the
series means.

Panels are made only by the builders below (BUILDERS), one per kind, and a
Board takes nothing else. What a kind needs to be right is an argument its
builder requires: a chart's colours, the counter an event bar counts (the
builder writes the bucketing), the end of a bar gauge's scale, the text an
empty table or bar chart shows.

House facts (rooms, devices, Items) come only from the map; the code names
none. Panel ids and positions are assigned in order, so the same map gives
the same JSON.
"""

from __future__ import annotations

import dataclasses
import re

import oh_units

from . import alerts
from .exporter import FAILURE_REASONS, HOUSE_LABEL, HOUSE_ROOM, OPERATION_OUTCOMES, ROUND_SECONDS, ROUTE_BUCKETS

DATASOURCE_UID = "thermite-prometheus"
DATASOURCE = {"type": "prometheus", "uid": DATASOURCE_UID}
#: Grafana 12/13's classic dashboard JSON; Grafana migrates older ones on load.
SCHEMA_VERSION = 41
HOUSE, SWITCHING, DEVICES, OPENHAB, HOST, USAGE, YOKE = (
    "house", "switching", "devices", "openhab", "host", "usage", "yoke")
#: Fixed for good: Grafana's file provisioning ties a board's internal id to
#: its file and may not delete (disableDeletion), so a new uid for a file it
#: already provisioned can never be saved ("deprecatedInternalID ... is
#: already in use"), and that board stops updating.
UIDS = {HOUSE: "thermite-house", SWITCHING: "thermite-switching", DEVICES: "thermite-devices",
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
#: The runtime pool whose one thread waits for a language-server client all
#: day: busy by design, so it says nothing about load.
LISTENING_POOL = "lsp"

#: Device kinds whose volume is a listening level; others (TVs behind a
#: receiver, Cast players at a fixed 100) only draw flat lines.
LISTENING_KINDS = ("avr", "amp", "streaming_amp", "audio_streamer", "soundbar", "sub")
#: The units the Host dashboard shows.
UNITS = oh_units.DAEMONS
#: Seconds between samples (oh_metrics.SCRAPE_INTERVAL): a sample count
#: times this is observed time.
SAMPLE_SECONDS = 30
#: Panels that count "since yesterday evening" show 18:00 yesterday to now.
#: Grafana takes a panel's own range only where it starts "now-<n><unit>";
#: any other spelling it ignores without a word and uses the board's range.
SINCE_EVENING = "now-1d/d+18h"
#: The Devices board's template variable, and the label matcher that keeps a
#: query to the rooms chosen in it.
ROOM = "room"
ROOMS = f'room=~"${ROOM}"'

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
#: A device's `<id>_online` (point online). No response is not a failure by
#: itself: a device that sleeps answers no ping.
REACHABLE = value_map({0: ("No response", OFF), 1: ("Reachable", OK)})
NEEDED = value_map({0: ("No", OFF), 1: ("Yes", IN_USE)})
#: A speaker's playing state (exporter_map() `playing` points).
PLAYBACK = value_map({0: ("Idle", OFF), 1: ("TV or line-in", IN_USE), 2: ("Playing", IN_USE)})
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
#: HTTP requests the gateway serves at once before it answers 503
#: (oh_access.service.MAX_REQUESTS); each open event stream is one.
GATEWAY_MAX_REQUESTS = 100
#: Seconds without a pass after which the policy worker counts as stalled
#: (the ThermitePolicyWorkerStalled alert).
WORKER_STALLED_SECONDS = 120
#: Routing's counts and the executor's status are valid now: a count of 0
#: means none only then.
ROUTING_COUNTS_VALID = "(max(thermite_routing_metrics_up) == 1)"
EXECUTOR_VALID = "(max(thermite_routing_executor_up) == 1)"

#: The remotes that report to the gateway (oh_access.metrics.CLIENTS), and
#: what each client event is in words (its CLIENT_KINDS).
CLIENT_WORDS = {"web": "Web remote", "tui": "Terminal remote"}
KIND_WORDS = {"policy_timeout": "room policy", "locks_timeout": "locks", "command_timeout": "a command",
              "snapshot_timeout": "the snapshot", "stream_error": "stream failed", "stream_refused": "stream refused"}
#: The HTTP protocols remotes report (oh_access.metrics.CLIENT_PROTOCOLS).
#: Over HTTP/1.1 a browser's six connections can starve its own requests.
PROTOCOLS = {"h2": ("HTTP/2", OK), "h3": ("HTTP/3", OK), "http/1.1": ("HTTP/1.1", WARN),
             "unknown": ("Unknown", UNKNOWN)}
#: How an event stream ended (oh_access.metrics.SSE_ENDS).
STREAM_ENDS = {"client": ("Closed by the page", IN_USE), "revoked": ("Sign-in revoked", WARN), "error": ("Error", FAIL)}
#: What became of a schedule boundary and of a repair (every one of
#: oh_access.metrics BOUNDARY_OUTCOMES and REPAIR_OUTCOMES, so that each has
#: a name in words): green went well, amber needs a look, red went wrong,
#: grey changed nothing.
BOUNDARIES = {"dispatched": OK, "already_current": OK, "leave": UNKNOWN, "coalesced_active_lock": UNKNOWN,
              "stale_revision": UNKNOWN, "stale_schedule": UNKNOWN, "other": UNKNOWN, "dispatch_unconfirmed": WARN,
              "shared_resource_conflict": WARN, "missed": FAIL, "invalid_policy": FAIL}
REPAIRS = {"sent": WARN, "unconfirmed": FAIL}
#: The policy worker's rooms that are not unlocked (oh_access.metrics.ROOM_STATES).
LOCKS = {"manual": ("Locked by hand", IN_USE), "scheduled": ("Locked by a schedule", IN_USE),
         "unavailable": ("Lock cannot be held", FAIL)}


def plain(value: str) -> str:
    """A label value in words: "probe_failed" as "Probe failed"."""
    return value.replace("_", " ").capitalize()


# ---------------------------------------------------------------- panels


class Panel:
    """A finished panel. Only the builders below (BUILDERS) make one, and a
    Board takes nothing else: no panel reaches a board without what its
    kind requires, and none is patched afterwards."""

    __slots__ = ("_json",)

    def __init__(self, json: dict) -> None:
        self._json = json


def _semantic(color: str) -> str:
    if color not in SEMANTIC:
        raise ValueError(f"{color!r} is not one of the six colours a panel may use (SEMANTIC)")
    return color


def _ref(index: int) -> str:
    return chr(ord("A") + index)


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    return {"datasource": DATASOURCE, "refId": ref, "expr": expr, "legendFormat": legend or "__auto",
            "range": not instant, "instant": instant}


def _targets(queries: list[tuple[str, str]], instant: bool = False) -> list[dict]:
    return [target(expr, legend, _ref(index), instant) for index, (expr, legend) in enumerate(queries)]


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


def _words(options: dict) -> list[dict]:
    """A text-to-text mapping: a label value shown in words."""
    return [{"type": "value", "options": {key: {"text": text, "index": index}
                                          for index, (key, text) in enumerate(options.items())}}]


#: A mapping that shows any snake_case label value with spaces, for a
#: vocabulary another module owns ("not_found" as "not found").
SPACED = [{"type": "regex", "options": {"pattern": "^" + "_".join(["([a-z0-9]+)"] * count) + "$",
                                        "result": {"text": " ".join(f"${n + 1}" for n in range(count)),
                                                   "index": count - 2}}}
          for count in range(2, 6)]


def link(name: str, **variables: str) -> dict:
    """A link to the board `name` that keeps the time range; `variables`
    set its template variables."""
    query = "".join(f"var-{key}={value}&" for key, value in variables.items())
    return {"title": TITLES[name], "url": f"/d/{UIDS[name]}?{query}${{__url_time_range}}", "targetBlank": False}


def _base(kind: str, title: str, description: str, since: bool = False) -> dict:
    panel = {"type": kind, "title": title, "description": description, "datasource": DATASOURCE}
    if since:
        panel.update(timeFrom=SINCE_EVENING, hideTimeOverride=True)
    return panel


def _named(names: dict[str, tuple[str, str]] | None) -> list[dict]:
    """{series name: (name shown, colour)} as overrides: a series whose name
    says its state or outcome, in that colour exactly."""
    return [{"matcher": {"id": "byName", "options": name}, "properties": [
        {"id": "color", "value": {"mode": "fixed", "fixedColor": _semantic(color)}},
        {"id": "displayName", "value": shown}]} for name, (shown, color) in (names or {}).items()]


def note(title: str, content: str) -> Panel:
    return Panel({"type": "text", "title": title, "transparent": True,
                  "options": {"mode": "markdown", "content": content}})


def stat(title: str, expr: str, unit: str = "none", steps: tuple | None = None, mappings: list | None = None,
         description: str = "", *, to: dict | None = None) -> Panel:
    """A tile: one value on a background of its state's colour, by `steps`
    or, with `mappings`, as the mapped word. `to`: where a click leads."""
    mappings = mappings or []
    thresholds = _thresholds(*steps) if steps else mapped_thresholds(mappings) if mappings else _thresholds((OK, None))
    defaults = {"unit": unit, "noValue": "Unknown", "color": {"mode": "thresholds"}, "thresholds": thresholds,
                "mappings": mappings}
    if to:
        defaults["links"] = [to]
    return Panel({**_base("stat", title, description), "targets": [target(expr, title, "A", instant=True)],
                  "fieldConfig": {"defaults": defaults, "overrides": []},
                  "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                              "colorMode": "background", "graphMode": "none", "justifyMode": "center",
                              "textMode": "value", "orientation": "auto", "wideLayout": True,
                              "showPercentChange": False}})


@dataclasses.dataclass(frozen=True)
class Figure:
    """One named number of a figures() panel, coloured by `steps`."""

    expr: str
    name: str
    steps: tuple = ((OK, None), (WARN, 1))


def figures(title: str, values: list[Figure], description: str, *, since: bool = False) -> Panel:
    """Several named counts, one under another, each in the colour of its
    own steps. A count that cannot be known (_known()) reads Unknown."""
    return Panel({**_base("stat", title, description, since),
                  "targets": _targets([(value.expr, value.name) for value in values], instant=True),
                  "fieldConfig": {"defaults": {"unit": "none", "noValue": "Unknown", "color": {"mode": "thresholds"},
                                               "thresholds": _thresholds((UNKNOWN, None)), "mappings": NAN_UNKNOWN},
                                  "overrides": [{"matcher": {"id": "byName", "options": value.name}, "properties": [
                                      {"id": "thresholds", "value": _thresholds(*value.steps)}]} for value in values]},
                  "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                              "colorMode": "value", "graphMode": "none", "justifyMode": "center",
                              "textMode": "value_and_name", "orientation": "horizontal", "wideLayout": True,
                              "showPercentChange": False, "text": {"titleSize": 12, "valueSize": 20}}})


@dataclasses.dataclass(frozen=True)
class Column:
    """A table column: a label of the queries' series, or a query's value
    ("Value" of one query, "Value #A", "Value #B" of several)."""

    field: str
    name: str
    #: Value mappings shown as plain words (_words(), SPACED).
    mappings: list | None = None
    #: Value mappings with colours: each value as its word, in its colour.
    states: list | None = None
    unit: str = ""
    decimals: int | None = None
    #: Threshold steps that colour a number's text.
    steps: dict | None = None
    width: int | None = None
    #: The least width of a column that takes what the others leave.
    least: int | None = None
    #: Where a click on a cell leads.
    to: dict | None = None
    #: A long value takes more lines, not an ellipsis: a phone's column is
    #: narrow. Not with `mappings`: Grafana sizes the row by the unmapped value.
    wrap: bool = False

    def properties(self) -> list[dict]:
        found: list[dict] = []
        if self.wrap:
            found.append({"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}})
        if self.mappings:
            found.append({"id": "mappings", "value": self.mappings})
        if self.states:
            found += [{"id": "mappings", "value": self.states},
                      {"id": "thresholds", "value": mapped_thresholds(self.states)},
                      {"id": "custom.cellOptions", "value": {"type": "color-text"}}]
        if self.unit:
            found.append({"id": "unit", "value": self.unit})
        if self.decimals is not None:
            found.append({"id": "decimals", "value": self.decimals})
        if self.steps:
            found += [{"id": "thresholds", "value": self.steps},
                      {"id": "custom.cellOptions", "value": {"type": "color-text"}}]
        if self.width:
            found.append({"id": "custom.width", "value": self.width})
        if self.least:
            found.append({"id": "custom.minWidth", "value": self.least})
        if self.to:
            found.append({"id": "links", "value": [self.to]})
        return found


def table(title: str, queries: str | list[str], columns: list[Column], description: str = "", *, none: str = "",
          dash: bool = False, sort: tuple[str, bool] | None = None, order: tuple[tuple[str, bool], ...] = (),
          since: bool = False) -> Panel:
    """An instant table of the series the queries give, several merged on
    their common labels, showing only `columns`. `none` speaks for an empty
    table. `sort`: the heading to sort by, and whether descending. `order`
    sorts the rows by fields that need not be columns, the last one first:
    (field, descending) each."""
    queries = [queries] if isinstance(queries, str) else queries
    keep = {column.field: index for index, column in enumerate(columns)}
    overrides = []
    for column in columns:
        # `none` speaks for an empty table; a row's missing label is a dash.
        properties = ([{"id": "noValue", "value": "—"}] if none or dash else []) + column.properties()
        if properties:
            overrides.append({"matcher": {"id": "byName", "options": column.field}, "properties": properties})
    transformations: list[dict] = [{"id": "merge", "options": {}}] if len(queries) > 1 else []
    transformations += [{"id": "sortBy", "options": {"fields": {}, "sort": [{"field": field, "desc": descending}]}}
                        for field, descending in order]
    transformations.append({"id": "organize", "options": {
        "excludeByName": {"Time": True, "Value": "Value" not in keep, "__name__": True, "job": True,
                          "instance": "instance" not in keep},
        "indexByName": keep, "includeByName": {column.field: True for column in columns},
        "renameByName": {column.field: column.name for column in columns if column.name != column.field}}})
    options: dict = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    if sort:
        options["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return Panel({**_base("table", title, description, since),
                  "targets": [{**target(expr, "", _ref(index), instant=True), "format": "table"}
                              for index, expr in enumerate(queries)],
                  "fieldConfig": {"defaults": {"noValue": none} if none else {}, "overrides": overrides},
                  "options": options, "transformations": transformations})


def state_timeline(title: str, queries: list[tuple[str, str]], mappings: list | None = None,
                   description: str = "", *, show_values: bool = False, none: str = "Unknown",
                   interval: str = "") -> Panel:
    """Rows of states. Mapped values show the mapping's text in its colour;
    an unmapped value is grey (unknown), never a borrowed meaning.

    The colour mode is fixed, never thresholds: with merged values and
    threshold colours, Grafana replaces each value by its threshold range
    ("9+") and ignores the mappings' text and colours."""
    mappings = mappings or []
    defaults = {"noValue": none, "color": {"mode": "fixed", "fixedColor": UNKNOWN},
                "thresholds": mapped_thresholds(mappings) if mappings else _thresholds((UNKNOWN, None)),
                "mappings": mappings, "custom": {"fillOpacity": 85, "lineWidth": 0, "spanNulls": False}}
    panel = {**_base("state-timeline", title, description),
             "targets": _targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": {"mergeValues": True, "showValue": "auto" if show_values else "never", "alignValue": "left",
                         "rowHeight": 0.85, "legend": {"showLegend": False, "displayMode": "list",
                                                       "placement": "bottom"},
                         "tooltip": {"mode": "single", "sort": "none"}}}
    if interval:
        panel["interval"] = interval
    return Panel(panel)


@dataclasses.dataclass(frozen=True)
class Series:
    """One query of a time series chart and the colour of what it draws,
    one of the six: its state's, or tan for a plain measure. Queries of one
    colour, and the series of a legend that names a label ("{{route}}"),
    draw in shades of it, which are close: `line` tells the lines of one
    chart apart, and marks a limit or a total the others are read against."""

    expr: str
    legend: str
    color: str
    line: str = "solid"


#: How a Series' line is drawn.
_LINES = {"solid": None, "dashed": {"fill": "dash", "dash": [8, 6]}, "dotted": {"fill": "dot", "dash": [1, 5]}}


def _drawn(queries: list) -> list[dict]:
    """Overrides that give each query's series the query's colour: exactly,
    or in shades of it where several series share it."""
    shared = [query.color for query in queries]
    overrides = []
    for index, query in enumerate(queries):
        several = "{{" in query.legend or shared.count(query.color) > 1
        properties: list[dict] = [{"id": "color", "value": {"mode": "shades" if several else "fixed",
                                                            "fixedColor": _semantic(query.color)}}]
        style = _LINES[getattr(query, "line", "solid")]
        if style:
            properties.append({"id": "custom.lineStyle", "value": style})
        overrides.append({"matcher": {"id": "byFrameRefID", "options": _ref(index)}, "properties": properties})
    return overrides


#: A chart's legend and tooltip.
_LEGEND = {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
           "tooltip": {"mode": "multi", "sort": "desc"}}


def timeseries(title: str, series: list[Series], unit: str = "short", description: str = "", *,
               stack: bool = False, minimum: float | None = None, maximum: float | None = None,
               names: dict[str, tuple[str, str]] | None = None, limit: float | None = None,
               decimals: int | None = None, none: str = "Unknown") -> Panel:
    """Lines over time, each query in the colour its Series names. `names`
    gives a series its name and colour by the name its legend makes:
    {name: (name shown, colour)}. `limit`: a value from which the chart
    is banded red. `none`: what an empty chart says, where its queries
    leave out what is zero."""
    custom: dict = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 70 if stack else 0, "showPoints": "never",
                    "spanNulls": False, "lineInterpolation": "stepAfter",
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    # Whatever no query or name claims is grey: unknown, never a borrowed meaning.
    defaults: dict = {"unit": unit, "noValue": none, "color": {"mode": "fixed", "fixedColor": UNKNOWN},
                      "custom": custom}
    for key, value in (("min", minimum), ("max", maximum), ("decimals", decimals)):
        if value is not None:
            defaults[key] = value
    if limit is not None:
        custom["thresholdsStyle"] = {"mode": "line+area"}
        defaults["thresholds"] = _thresholds(("transparent", None), (FAIL, limit))
    return Panel({**_base("timeseries", title, description),
                  "targets": _targets([(each.expr, each.legend) for each in series]),
                  "fieldConfig": {"defaults": defaults, "overrides": _drawn(series) + _named(names)},
                  "options": dict(_LEGEND)})


#: The width of an event bar. Each bar counts exactly its own width: an hour,
#: or what Grafana's step is where a long range makes that longer.
EVENT_BUCKET = "1h"
_COUNTER = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*_(total|count|bucket)")
_MATCHERS = re.compile(r'[a-z_]+(=|!=|=~|!~)"[^"{}()\[\]]*"(,[a-z_]+(=|!=|=~|!~)"[^"{}()\[\]]*")*')
_LABELS = re.compile(r"[a-z_]+(, [a-z_]+)*")


def _selector(counter: str, where: str) -> str:
    """A counter's series by its name and label matchers, and nothing else:
    the builders write the query around it."""
    if not _COUNTER.fullmatch(counter):
        raise ValueError(f"{counter!r} is not a counter's name: the builder writes the query itself")
    if where and not _MATCHERS.fullmatch(where):
        raise ValueError(f"{where!r} is not a list of label matchers")
    return f"{counter}{{{where}}}" if where else counter


def _grown(selector: str, ahead: bool) -> str:
    """By how much each counter of `selector` grew in a bucket $__interval
    wide: the bucket that ends at the query's time or, `ahead`, the one that
    starts there, which may still be filling.

    Its last sample in the bucket less its last sample before the bucket,
    so buckets side by side count every event once. increase() alone would
    not: it reads between the bucket's own samples, so the event that falls
    between one bucket's last sample and the next one's first is in neither
    (one in 120 at an hour of 30-second samples), and it stretches what it
    read to the bucket's width, which makes two of one event while a bucket
    is two samples old. A counter that began again before the bucket's
    first sample counts from zero. One that began again inside the bucket
    is left to increase(), the one function that knows a reset. A series'
    first sample is never counted, here or by increase(): exporters start
    their counters at zero for that reason."""
    bucket = f"{selector}[$__interval]" + (" offset -$__interval" if ahead else "")
    before = f"last_over_time({selector}[$__interval]" + ("" if ahead else " offset $__interval") + ")"
    start = f"(({before} <= min_over_time({bucket})) or ({before} * 0))"
    return f"((max_over_time({bucket}) - {start}) and (resets({bucket}) == 0)) or increase({bucket})"


@dataclasses.dataclass(frozen=True)
class Count:
    """Events for events() to count: a counter by its name, never an
    expression, the labels to split it `by`, and the label matchers that
    say `where`. Its colour as for a Series."""

    counter: str
    legend: str
    color: str
    by: str = ""
    where: str = ""

    def expr(self) -> str:
        """The events in each bar. The bar at a time counts the bucket that
        starts there (_grown() ahead), so the newest bar is the bucket still
        filling: otherwise what happened since the last full bucket would
        not show."""
        selector = _selector(self.counter, self.where)
        if self.by and not _LABELS.fullmatch(self.by):
            raise ValueError(f"{self.by!r} is not a list of labels")
        by = f" by ({self.by})" if self.by else ""
        return f"round(sum{by} ({_grown(selector, ahead=True)})) > 0"


def events(title: str, counts: list[Count], description: str, *, none: str,
           names: dict[str, tuple[str, str]] | None = None) -> Panel:
    """Counts of events as stacked bars over time, one bar per EVENT_BUCKET.
    The builder writes each query from its counter's name, so a bar cannot
    count anything but its own width; `none` says what an empty chart
    means. `names` as for timeseries()."""
    custom = {"drawStyle": "bars", "barAlignment": 1, "lineWidth": 1, "fillOpacity": 70, "showPoints": "never",
              "spanNulls": False, "stacking": {"mode": "normal", "group": "A"}}
    return Panel({**_base("timeseries", title, description),
                  "targets": [{**target(count.expr(), count.legend, _ref(index)), "interval": EVENT_BUCKET}
                              for index, count in enumerate(counts)],
                  "fieldConfig": {"defaults": {"unit": "short", "decimals": 0, "min": 0, "noValue": none,
                                               "color": {"mode": "fixed", "fixedColor": UNKNOWN}, "custom": custom},
                                  "overrides": _drawn(counts) + _named(names)},
                  "options": dict(_LEGEND)})


def breakdown(title: str, expr: str, legend: str, color: str, description: str, *, unit: str = "dtdurations",
              names: dict[str, str] | None = None, none: str = "Unknown") -> Panel:
    """A whole and its parts, as a donut: each part of `expr` a slice in a
    shade of `color`, named beside it with its amount and share. `names`:
    {series name: name shown}."""
    overrides = [{"matcher": {"id": "byName", "options": name}, "properties": [{"id": "displayName", "value": shown}]}
                 for name, shown in (names or {}).items()]
    return Panel({**_base("piechart", title, description), "targets": _targets([(expr, legend)], instant=True),
                  "fieldConfig": {"defaults": {"unit": unit, "decimals": 1, "noValue": none,
                                               "color": {"mode": "shades", "fixedColor": _semantic(color)}},
                                  "overrides": overrides},
                  "options": {"pieType": "donut", "displayLabels": [],
                              "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                              "legend": {"showLegend": True, "displayMode": "table", "placement": "right",
                                         "values": ["value", "percent"]},
                              "tooltip": {"mode": "single", "sort": "desc"}}})


def bar_gauge(title: str, expr: str, label: str, color: str, description: str, *, full: str,
              unit: str = "dtdurations", none: str = "Unknown") -> Panel:
    """Amounts to compare, one bar each, longest first, each named by its
    `label`, on a scale that ends at `full`: a query for the amount a full
    bar stands for. A scale that ended at the largest bar instead would draw
    30 seconds as large as 20 hours."""
    return Panel({**_base("bargauge", title, description),
                  "targets": [target(f"sort_desc({expr})", f"{{{{{label}}}}}", "A", instant=True),
                              target(full, "full", "B", instant=True)],
                  # The scale's end is query B's value, and B draws no bar.
                  "transformations": [
                      {"id": "configFromData", "options": {"configRefId": "B", "applyTo": {"id": "byFrameRefID",
                                                                                           "options": "A"},
                                                           "mappings": [{"fieldName": "full", "handlerKey": "max"}]}}],
                  # Named from the label, not the legend: Grafana leaves a lone bar's legend name out.
                  "fieldConfig": {"defaults": {"unit": unit, "min": 0, "noValue": none,
                                               "displayName": f"${{__field.labels.{label}}}",
                                               "color": {"mode": "fixed", "fixedColor": _semantic(color)}},
                                  "overrides": []},
                  "options": {"orientation": "horizontal", "displayMode": "basic", "valueMode": "text",
                              "text": {"valueSize": 13},
                              "showUnfilled": True, "namePlacement": "left", "sizing": "manual",
                              "minVizHeight": 22, "minVizWidth": 8, "maxVizHeight": 30,
                              "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                              "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom",
                                         "calcs": []}}})


def heatmap(title: str, histogram: str, where: str, description: str, *, interval: str = "15m") -> Panel:
    """How many of a histogram's observations fell in each of its buckets,
    per `interval`: wide enough cells that sparse events read as counts. The
    builder writes the query from the histogram's name and the label
    matchers that say `where`, as events() does, so a cell counts exactly
    its own width. A cell is tan, the more opaque the more it counts.
    Grafana draws a cell once its interval has ended."""
    expr = f"sum by (le) ({_grown(_selector(f'{histogram}_bucket', where), ahead=False)})"
    return Panel({**_base("heatmap", title, description),
                  "targets": [{**target(expr, "{{le}}", "A"), "format": "heatmap", "interval": interval}],
                  "fieldConfig": {"defaults": {}, "overrides": []},
                  "options": {"calculate": False, "cellGap": 1, "yAxis": {"axisPlacement": "left", "unit": "s"},
                              "rowsFrame": {"layout": "le"},
                              # From zero: a scale that began at the least count drew a count of one,
                              # where no cell counts less, as no cell at all.
                              "color": {"mode": "opacity", "fill": IN_USE, "scale": "exponential", "exponent": 0.5,
                                        "steps": 64, "min": 0},
                              "cellValues": {"unit": "short", "decimals": 0}, "filterValues": {"le": 1e-9},
                              "legend": {"show": True}, "tooltip": {"mode": "single", "yHistogram": True}}})


#: Every builder of a panel: nothing else makes a Panel.
BUILDERS = (note, stat, figures, table, state_timeline, timeseries, events, breakdown, bar_gauge, heatmap)


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

    def add(self, panel: Panel, width: int, height: int) -> None:
        if not isinstance(panel, Panel):
            raise TypeError("a board takes only what a builder made (dashboards.BUILDERS)")
        if self._x + width > 24:
            self._newline()
        placed = {"id": self._id(), "gridPos": {"x": self._x, "y": self._y, "w": width, "h": height}, **panel._json}
        self._x += width
        self._row_height = max(self._row_height, height)
        (self._row["panels"] if self._row is not None else self.panels).append(placed)

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


def room_variable(mapping: dict) -> dict:
    """The Room variable: every room of the house by its label, any number
    of them, All at first. Queries keep to the rooms chosen with ROOMS."""
    rooms = sorted((mapping.get("rooms") or {}).items(), key=lambda room: (str(room[1]), room[0]))
    # Grafana reads "text : value" pairs, split at commas.
    pairs = [f"{str(label).replace(',', ' ').replace(' : ', ': ')} : {room}" for room, label in rooms]
    return {"type": "custom", "name": ROOM, "label": "Room", "query": ",".join(pairs), "multi": True,
            "includeAll": True, "allValue": ".*", "current": {"text": ["All"], "value": ["$__all"]},
            "options": [{"text": "All", "value": "$__all", "selected": True},
                        *({"text": str(label), "value": room, "selected": False} for room, label in rooms)]}


def dashboard(name: str, board: Board, description: str, refresh: str = "1m", time_from: str = "now-24h",
              *, annotations: bool = True, variables: tuple[dict, ...] = ()) -> dict:
    """`annotations`: whether the board has time axes to mark service starts
    and deploys on. The other boards are one menu, not a chip each: on a
    phone the chips took two rows above the first panel."""
    return {
        "uid": UIDS[name], "title": TITLES[name], "description": description, "tags": [TAG],
        "schemaVersion": SCHEMA_VERSION, "editable": False, "graphTooltip": 1, "timezone": "browser",
        "refresh": refresh, "time": {"from": time_from, "to": "now"}, "timepicker": {},
        "templating": {"list": list(variables)},
        "annotations": {"list": ANNOTATIONS if annotations else ANNOTATIONS[:1]},
        "fiscalYearStartMonth": 0, "liveNow": False,
        "links": [{"type": "dashboards", "tags": [TAG], "asDropdown": True, "includeVars": False,
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
NO_READBACK = -1


def source_timeline(mapping: dict, zones: list[tuple[str, str, str]]) -> tuple[list[dict], dict[int, str | None]]:
    """Each zone's row of a source timeline, {item, room, label, query}, and
    the source value each number a query gives stands for: a source the same
    number in every zone, and NO_READBACK (None) while the zone's readback
    is NULL or UNDEF, so that a zone whose devices stopped answering keeps
    its row instead of vanishing. A value outside the options gives nothing.

    max, not sum: Prometheus writes no staleness marker across its own
    restart, so for up to its lookback a series that ended keeps its last
    sample beside the one that replaced it. A sum of the two numbers would
    name a third source; max names one the readback reported."""
    numbers: dict[str, int] = {}
    rows = []
    for name, room, label in zones:
        terms = []
        for value in mapping["items"][name].get("options") or []:
            numbers.setdefault(value, len(numbers))
            terms.append(f'thermite_item_info{{item={_q(name)},value={_q(value)}}} * {numbers[value]}')
        if terms:
            terms.append(f'thermite_item_unknown{{item={_q(name)}}} * {NO_READBACK}')
            rows.append({"item": name, "room": room, "label": label,
                         "query": f'max without (value) ({" or ".join(terms)})'})
    return rows, {NO_READBACK: None, **{number: value for value, number in numbers.items()}}


def room_history(mapping: dict) -> dict:
    """Rooms through the day as data, for the gateway's room history
    (oh_identity.history): each zone's source Item, label and query; the
    value each number stands for (None: Unknown); and each value in the
    words the Usage board shows it in."""
    rows, values = source_timeline(mapping, _zones(mapping))
    words = _source_words(mapping)
    return {"zones": [{key: row[key] for key in ("item", "label", "query")} for row in rows],
            "values": {str(number): value for number, value in values.items()},
            "labels": {value: "Off" if value == "off" else str(words.get(value, value))
                       for value in values.values() if value is not None}}


def _source_timeline(mapping: dict, zones: list[tuple[str, str, str]]) -> tuple[list[tuple[str, str]], list[dict]]:
    """The queries and the value mapping of a source timeline: each source
    shown as the remotes name it (_source_words) in tan, Off in slate, and a
    readback with no value it knows as Unknown in grey."""
    words = _source_words(mapping)
    rows, values = source_timeline(mapping, zones)
    names = {number: ("Unknown", UNKNOWN) if value is None else ("Off", OFF) if value == "off"
             else (words.get(value, value), IN_USE) for number, value in values.items()}
    return [(row["query"], row["label"]) for row in rows], value_map(names)


def _room_words(mapping: dict) -> dict:
    return {HOUSE_ROOM: HOUSE_LABEL, **(mapping.get("rooms") or {})}


def _device_words(mapping: dict) -> dict:
    return {ident: device.get("label", ident) for ident, device in (mapping.get("devices") or {}).items()}


def _speakers(mapping: dict) -> list[tuple[str, str, str]]:
    """(playing Item, device, room) of each speaker (a `playing` point),
    by room label, then device label."""
    rooms, devices = mapping.get("rooms") or {}, mapping.get("devices") or {}
    found = [(name, spec["device"], str(spec.get("room", ""))) for name, spec in (mapping.get("items") or {}).items()
             if spec.get("point") == "playing" and spec.get("device")]
    return sorted(found, key=lambda row: (rooms.get(row[2], row[2]), (devices.get(row[1]) or {}).get("label", row[1])))


def _speaker_rooms(mapping: dict) -> list[str]:
    """The rooms of the speakers, in _speakers()' order."""
    return list(dict.fromkeys(room for _, _, room in _speakers(mapping)))


def _listening_timeline(mapping: dict) -> Panel:
    """Each speaker room's playing state through the range."""
    rooms = mapping.get("rooms") or {}
    queries = [(f'max(thermite_item_value{{point="playing",room={_q(room)}}})', rooms.get(room, room))
               for room in _speaker_rooms(mapping)]
    return state_timeline("Listening through the day", queries, PLAYBACK,
                          "Each room's speakers as they report themselves every 30 s. Playing: one plays a track. TV "
                          "or line-in: one plays sound with no track named, such as a soundbar's TV audio. Idle: "
                          "paused or stopped. A gap: none answered.", show_values=True)


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

#: Grid rows a tile is tall.
TILE = 3


def _room_column(mapping: dict, last: bool = False) -> Column:
    """A Room column; a click on a room opens Devices for that room. As a
    table's first column it shares the width with the others, down to what
    a phone can spare; as its `last` it is as wide as the longest room's
    name, and the columns before it get the rest."""
    words = _room_words(mapping)
    longest = max((len(str(label)) for label in words.values()), default=0)
    width = {"width": max(110, min(230, 24 + 8 * longest))} if last else {"least": 110}
    return Column("room", "Room", _words(words), to=link(DEVICES, **{ROOM: "${__value.raw}"}), **width)


def _attention(mapping: dict) -> Panel:
    """Needs attention now: failing or unknown required checks, and
    quarantined devices, in words."""
    failing = f'{alerts.CHECK}{{required="true"}} != 0'
    quarantined = 'label_replace(thermite_routing_device_quarantined == 1, "check", "quarantined", "", "")'
    # The room and the problem first: a phone shows those, and scrolls to the rest.
    return table("Needs attention now", f"({failing}) or ({quarantined})", [
        _room_column(mapping),
        Column("check", "Problem", _words(CHECK_WORDS), least=170),
        Column("zone", "Zone", _words(_zone_words(mapping)), width=130),
        Column("device", "Device", _words(_device_words(mapping))),
        Column("Value", "State", states=CHECK_STATE, width=100)],
        "Required control checks that fail or have no report, and devices whose routing commands are "
        "quarantined. A device is required while a room's selected source uses it; routing infrastructure "
        f"always is. A room that stops reporting its source keeps the one it last reported, for {alerts.HELD} "
        "at most, so a device that fails and takes the report with it stays listed. A sleeping device that "
        "no room uses is never listed. Monitoring's own checks are listed too. Zone: which of the room's source "
        "selectors needs it. A click on a room opens Devices for it.",
        none="Nothing needs attention", sort=("Room", False))


def _firing(mapping: dict) -> Panel:
    # The alert and its severity first: the rest says where, and a narrow panel scrolls to it.
    return table("Alerts firing", 'ALERTS{alertstate="firing"}', [
        Column("alertname", "Alert", _words({name: title for name, (title, _) in alerts.CATALOG.items()}), least=180),
        Column("severity", "Severity", states=SEVERITY, width=90),
        _room_column(mapping),
        Column("zone", "Zone", _words(_zone_words(mapping)), width=130),
        Column("device", "Device", _words(_device_words(mapping))),
        Column("check", "Check", _words(CHECK_WORDS))],
        "Alerts firing now, every one. The thermite app's Home shows them too, critical ones first, without "
        "those another firing alert already explains.", none="No alerts")


def _known(expr: str, valid: str) -> str:
    """`expr` while `valid` holds, else NaN, which figures() shows as Unknown."""
    return f"(({expr}) and on() {valid}) or vector(NaN)"


def house(mapping: dict, probes: list[dict]) -> dict:
    board = Board()
    zones = _zones(mapping)
    speakers = _speakers(mapping)
    # Two rows of tiles: in one row a tile is too narrow for its title on a
    # laptop with Grafana's menu docked. The house's tiles, what they point
    # at, then monitoring's: a phone stacks the panels in this order, and
    # what needs attention must not wait below seven tiles.
    wide = 6 if speakers else 8
    board.add(stat("Control", alerts.HEALTH, mappings=CONTROL,
                   description="Every check a room's selected source needs, routing infrastructure, each speaker "
                               f"in use (it played in the last {alerts.IN_USE}, or was playing when it stopped "
                               "answering) and the runtime. A room that stops reporting its source keeps the "
                               f"one it last reported, for {alerts.HELD} at most. A sleeping device no room uses, "
                               "or an idle speaker, is never a failure; a missing report is Unknown. "
                               "Monitoring's own checks do not count here. Needs attention now lists the "
                               "checks. This checks control, not picture or sound."), wide, TILE)
    ready = (f'count(count by (zone) ({alerts.CHECK}{{check="source"}} == 0) unless on(zone) '
             f'({alerts.CHECK}{{required="true",zone!=""}} != 0)) or vector(0)')
    board.add(stat("Rooms ready", ready, f"suffix: of {len(zones)}", steps=((WARN, None), (OK, len(zones) or 1)),
                   description="Routing zones whose source is known and whose required checks all pass. A zone "
                               "that is Off and reports Off is ready. A click opens Devices.", to=link(DEVICES)),
              wide, TILE)
    if speakers:
        names = "|".join(re.escape(device) for _, device, _ in speakers)
        answering = f'count(max by (device) (abs({alerts.CHECK}{{device=~{_q(names)}}})) == 0) or vector(0)'
        board.add(stat("Speakers ready", answering, f"suffix: of {len(speakers)}",
                       steps=((WARN, None), (OK, len(speakers))),
                       description="Speakers whose checks all pass: the runtime reaches each, playing or idle. One "
                                   "that does not answer is a failure only while in use (Control): it played in "
                                   f"the last {alerts.IN_USE}, or was playing when it stopped answering. A click "
                                   "opens Devices.", to=link(DEVICES)), wide, TILE)
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
                               "yet. A click opens Switching & remotes.", to=link(SWITCHING)), wide, TILE)
    board.add(_attention(mapping), 24, 6)
    board.add(stat("Alerts firing", 'count(ALERTS{alertstate="firing"}) or vector(0)',
                   steps=((OK, None), (WARN, 1)),
                   description="Alerts firing now. The thermite app's Home shows them; nothing is sent to a phone."),
              12, TILE)
    problems = ('(count(up == 0) or vector(0)) + (count(node_systemd_unit_state{job="node",name=~"thermite-.*",'
                'state="failed"} == 1) or vector(0)) + (count((node_filesystem_avail_bytes{job="node",'
                'fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes < 0.10) and '
                '(node_filesystem_readonly == 0)) or vector(0))')
    board.add(stat("Host", problems, "suffix: problems", steps=((OK, None), (FAIL, 1)),
                   mappings=value_map({0: ("OK", OK)}),
                   description="Scrape targets down, failed Thermite units and file systems under 10% free. "
                               "A click opens Host, which says which.", to=link(HOST)), 12, TILE)
    board.add(_firing(mapping), 14, 7)
    # Routing's counts read 0 only while its telemetry is valid, else
    # Unknown; device failures only where an executor counts them, while its
    # status is valid too.
    executor = bool((mapping.get("routing") or {}).get("status"))
    counted = ROUTING_COUNTS_VALID + (f" and on() {EXECUTOR_VALID}" if executor else "")
    board.add(figures("Since yesterday evening", [
        Figure(_known("round(sum(increase(thermite_route_seconds_count[$__range]))) or vector(0)",
                      ROUTING_COUNTS_VALID), "Source changes", ((IN_USE, None),)),
        Figure(_known('round(sum(increase(thermite_route_seconds_count{outcome="deadline"}[$__range]))) or vector(0)',
                      ROUTING_COUNTS_VALID), "Slow or missed"),
        Figure(f'round(sum(increase(thermite_client_events_total{{kind=~"{TIMEOUTS}"}}[$__range]))) or vector(0)',
               "Remotes gave up"),
        *([Figure(_known("round(sum(increase(thermite_routing_endpoint_failures_total[$__range]))) or vector(0)",
                         counted), "Device failures")] if executor else []),
        Figure('round(sum(increase(node_systemd_service_restart_total{job="node",name=~"thermite-.*"}[$__range]))) '
               'or vector(0)', "Restarts")],
        "Since 18:00 yesterday (browser time). Source changes: choices routing followed. "
        f"Slow or missed: not confirmed {_within(mapping)}. Remotes gave up: requests remote "
        "pages stopped waiting for. " + ("Device failures: routing operations a device did not "
        "take. " if executor else "") + "Restarts: Thermite services systemd restarted. Unknown: "
        "routing's telemetry is unreadable now.", since=True), 10, 7)
    # Every zone's series is there at zero from routing's first report, and the quantile of no changes is
    # NaN: only a room with a confirmed change in the range gets a row, so an idle house reads "No source
    # changes".
    changes = 'round(sum by (zone) (increase(thermite_route_seconds_count{outcome="confirmed"}[$__range]))) > 0'
    quantile = ("histogram_quantile({q}, sum by (le, zone) (increase(thermite_route_seconds_bucket"
                '{{outcome="confirmed"}}[$__range]))) and on (zone) ({changes})')
    seconds = {"unit": "s", "decimals": 1, "steps": _switch_thresholds(mapping), "width": 110}
    board.add(table("Switch time by room", [quantile.format(q=0.5, changes=changes),
                                            quantile.format(q=0.95, changes=changes), changes], [
        Column("zone", "Room", _words(_zone_words(mapping))),
        Column("Value #A", "Median", **seconds), Column("Value #B", "95th", **seconds),
        Column("Value #C", "Changes", decimals=0, width=110)],
        "Since 18:00 yesterday: how long a source change took until the room's readback showed it, "
        "median and 95th percentile of confirmed changes, estimated from "
        f"{', '.join(map(str, ROUTE_BUCKETS[:-1]))} and {ROUTE_BUCKETS[-1]} s buckets. Amber from half "
        f"the routing deadline, red from the deadline: a change not confirmed {_within(mapping)} "
        "counts as slow or missed instead.",
        none="No source changes", sort=("95th", True), since=True), 24, 6)  # the slowest room first
    # No panel here has a time axis to mark service starts and deploys on.
    return dashboard(HOUSE, board, "Is the house OK? Control, rooms, speakers, remotes, alerts and "
                     "the host; what needs attention now; the alerts firing; switching since yesterday evening.",
                     annotations=False)


# ---------------------------------------------------------------- switching and remotes


def _per_bar(what: str) -> str:
    """How an events() panel's description begins."""
    return f"{what}, each bar an hour (longer on a range of weeks)"


def switching(mapping: dict) -> dict:
    board = Board()
    zones = _zone_words(mapping)
    board.row("Switching sources")
    board.add(heatmap("Time to switch", "thermite_route_seconds", 'outcome="confirmed"',
                      "How long confirmed source changes took, from the choice until the room's readback showed "
                      "it, per quarter hour. More opaque cells: more changes. A quarter hour shows once it has "
                      "ended; Source changes by outcome counts the hour still running."), 12, 8)
    board.add(events("Source changes by outcome", [
        Count("thermite_route_seconds_count", "{{outcome}}", UNKNOWN, by="outcome")],
        _per_bar("Source changes") + ". Confirmed: the readback showed the choice. Deadline: it did not "
        f"{_within(mapping)}. Superseded: a newer choice came first.", none="No source changes",
        names={"confirmed": ("Confirmed", OK), "deadline": ("Deadline", FAIL), "superseded": ("Superseded", UNKNOWN)}),
        12, 8)
    board.add(timeseries("Routing executor", [
        Series("thermite_routing_pending", "Waiting", IN_USE, line="dashed"),
        Series("thermite_routing_active", "Running", IN_USE),
        Series("thermite_routing_events_queued", "Outcomes queued", IN_USE, line="dotted"),
        Series("thermite_routing_quarantined > 0", "Quarantined", FAIL)], minimum=0, decimals=0,
        description="Device operations the routing executor holds. Quarantined, in red while there are any: "
                    "devices that refuse commands until a recovery read succeeds."), 8, 7)
    board.add(events("Resends by room", [Count("thermite_routing_resends_total", "{{zone}}", WARN, by="zone")],
                     _per_bar("Rounds in which routing sent a room's commands again because a device had not "
                              "taken them (often one still waking)"), none="No resends",
                     names={zone: (label, WARN) for zone, label in zones.items()}), 8, 7)
    board.add(events("Operations by outcome", [
        Count("thermite_routing_operations_total", "{{outcome}}", UNKNOWN, by="outcome")],
        _per_bar("What became of each executor operation") + ". Applied and sampled are normal; the red ones are "
        "device failures.", none="No operations",
        names={outcome: (plain(outcome), OK if outcome in ("applied", "sampled") else
                         FAIL if outcome in FAILED_OPERATIONS else UNKNOWN) for outcome in OPERATION_OUTCOMES}), 8, 7)
    board.add(table("Device failures", "sum by (device, reason) (increase(thermite_routing_endpoint_failures_total"
                    "[$__range])) > 0", [
        Column("device", "Device", _words(_device_words(mapping))),
        Column("reason", "Reason", _words({reason: plain(reason) for reason in FAILURE_REASONS}), width=160),
        Column("Value", "Failures", decimals=0, width=90)],
        "Routing operations a device did not take in the selected range, by reason.",
        none="No device failures"), 12, 6)
    board.add(table("Quarantined now", "thermite_routing_device_quarantined == 1", [
        _room_column(mapping), Column("device", "Device", _words(_device_words(mapping)))],
        "Devices whose routing commands are refused now. A click on a room opens Devices for it.",
        none="No device is quarantined"), 12, 6)
    board.row("Remotes and the gateway")
    board.add(timeseries("Event streams", [
        Series("sum by (http_version) (thermite_gateway_sse_streams)", "HTTP/{{http_version}} streams", IN_USE),
        Series('max(thermite_gateway_sse_peer_streams_max{http_version="1.1"})', "Most on one HTTP/1.1 client",
               IN_USE, line="dashed")],
        minimum=0, decimals=0, limit=HTTP1_LIMIT,
        description=f"Open event streams by HTTP version. Over HTTP/1.1 a browser opens at most {HTTP1_LIMIT} "
                    f"connections to the gateway, so one client holding {HTTP1_LIMIT} streams starves its own "
                    "requests (the dashed line reaching the red band)."), 8, 8)
    board.add(events("Event stream failures", [
        Count("thermite_client_events_total", "{{client}} {{kind}}", WARN, by="client, kind",
              where=f'kind=~"{STREAM_FAILURES}"'),
        Count("thermite_gateway_sse_refused_total", "Refused at the {{limit}} limit", FAIL, by="limit"),
        Count("thermite_gateway_sse_overflows_total", "Fell behind, sent a fresh snapshot", WARN),
        Count("thermite_gateway_upstream_failures_total", "The gateway's runtime feed failed", FAIL)],
        _per_bar("Event streams that failed") + ": those remote pages and the terminal remote saw fail (as at "
        "every gateway restart) or refused; streams the gateway refused at its per-client (peer) or total limit; "
        "streams that fell so far behind that the gateway dropped their queued changes and sent a fresh "
        "snapshot; and the gateway's one subscription to the runtime's events failing, after which it reconnects "
        "and every page gets a fresh snapshot. Remotes reconnect; it is not giving up.",
        none="No stream failed",
        names={f"{client} {kind}": (f"{shown}: {KIND_WORDS[kind]}", FAIL if kind == "stream_refused" else WARN)
               for client, shown in CLIENT_WORDS.items() for kind in alerts.STREAM_KINDS}), 8, 8)
    board.add(events("Remotes gave up", [
        Count("thermite_client_events_total", "{{client}} {{kind}}", WARN, by="client, kind",
              where=f'kind=~"{TIMEOUTS}"')],
        _per_bar("Requests remote pages and the terminal remote stopped waiting for (timeouts)") + ", as they "
        "reported them on their next answered request, by what they waited for.", none="No remote gave up",
        names={f"{client} {kind}": (f"{shown}: {KIND_WORDS[kind]}", WARN)
               for client, shown in CLIENT_WORDS.items() for kind in alerts.TIMEOUT_KINDS}), 8, 8)
    board.add(timeseries("Gateway latency", [
        Series('histogram_quantile(0.95, sum by (le, route) (rate(thermite_gateway_request_seconds_bucket'
               f'{{route=~"{"|".join(WAITED_ROUTES)}"}}[5m]))) > 0', "{{route}} 95th", IN_USE),
        Series('histogram_quantile(0.95, sum by (le) (rate(thermite_gateway_upstream_request_seconds_bucket[5m]))) > 0',
               "runtime 95th", IN_USE)], "s", minimum=0, none="No requests",
        description="95th percentile time to the first byte of the answer for the requests remotes wait on, "
                    "and of the gateway's own requests to the runtime."), 12, 8)
    # How often from the event counter: a report carries every give-up but at most 16 of the waits. And
    # the mean of those, not a quantile: a quantile reads between a bucket's bounds whatever was waited.
    gave_up = f'round(sum by (kind) (increase(thermite_client_events_total{{kind=~"{TIMEOUTS}"}}[$__range]))) > 0'
    waited = [f'sum by (kind) (increase(thermite_client_wait_seconds_{part}{{kind=~"{TIMEOUTS}"}}[$__range]))'
              for part in ("sum", "count")]
    board.add(table("Waited before giving up", [
        gave_up, f"({waited[0]} / ({waited[1]} > 0)) and on (kind) ({gave_up})"], [
        Column("kind", "Waited for", _words({kind: plain(KIND_WORDS[kind]) for kind in alerts.TIMEOUT_KINDS})),
        Column("Value #A", "Gave up", decimals=0, width=100),
        Column("Value #B", "Mean wait", unit="s", decimals=1, width=110)],
        "In the selected range: how often remotes gave up on each kind of request, and how long they had "
        "waited by then on average, queued and in flight. A remote reports every give-up and the waits of at "
        "most 16 of each kind at a time; the mean is of the waits reported.", none="No remote gave up",
        sort=("Gave up", True)), 12, 8)
    board.add(timeseries("Requests by answer", [
        Series("sum by (code_class) (rate(thermite_gateway_requests_total[5m])) > 0", "{{code_class}}", UNKNOWN)],
        "reqps", minimum=0, none="No requests",
        names={"2xx": ("2xx", OK), "3xx": ("3xx", UNKNOWN), "4xx": ("4xx", WARN), "5xx": ("5xx", FAIL),
               "aborted": ("Aborted", OFF)},
        description="Gateway requests per second by status class, on both listeners; aborted ones ended before "
                    "an answer. Includes requests the admission limit refused."), 8, 8)
    board.add(table("Problem answers", "sum by (code) (increase(thermite_gateway_problems_total[$__range])) > 0", [
        Column("code", "Problem", SPACED), Column("Value", "Answers", decimals=0, width=100)],
        "The gateway's problem codes in the selected range.", none="No problems", sort=("Answers", True)), 8, 8)
    board.add(events("Protocols remotes use", [
        Count("thermite_client_reports_total", "{{protocol}}", UNKNOWN, by="protocol")],
        _per_bar("Reports from remotes by the HTTP protocol they last used") + ": HTTP/2 shares one connection, "
        "while HTTP/1.1 allows a browser six.", none="No remote reported", names=PROTOCOLS), 8, 8)
    board.add(timeseries("Event stream lifetimes", [
        Series("histogram_quantile(0.5, sum by (le, end) (increase(thermite_gateway_sse_stream_seconds_bucket[1h]))) "
               "> 0", "{{end}}", UNKNOWN)], "s", minimum=0, none="No stream ended", names=STREAM_ENDS,
        description="The median time event streams that ended in the hour before stayed open, by how they ended: "
                    "the page closed it or the gateway stopped, its sign-in was revoked, or an error. Streams that "
                    "keep ending after the same short time point to something cutting them, such as an idle "
                    "timeout on the way. Hours in which no stream ended are left out."), 8, 8)
    board.add(timeseries("Requests in flight", [
        Series("sum by (route) (thermite_gateway_requests_in_flight) > 0", "{{route}}", IN_USE)], minimum=0,
        stack=True, decimals=0, limit=GATEWAY_MAX_REQUESTS, none="No requests in flight",
        description="Requests the gateway was serving at its last 10-second sample, by route class; an open event "
                    f"stream is one for as long as it lasts. From {GATEWAY_MAX_REQUESTS} at once (the red band) "
                    "it answers 503 to any more."), 8, 8)
    board.add(timeseries("Live updates", [
        Series("sum(rate(thermite_gateway_upstream_events_total[5m])) * 60", "Runtime changes taken", IN_USE),
        Series("sum(rate(thermite_gateway_sse_frames_total[5m])) * 60", "Frames sent to pages", IN_USE,
               line="dashed")], minimum=0,
        description="Per minute: the runtime's changes the gateway took on its one subscription, which is open "
                    "only while a remote page is, and the frames it sent on pages' event streams: each change once "
                    "to every open page that shows it, and a snapshot to each page that joins or reconnects."),
              8, 8)
    board.row("Room policy worker")
    board.add(timeseries("Locked rooms", [
        Series('thermite_policy_rooms{state!="unlocked"} > 0', "{{state}}", UNKNOWN)],
                         minimum=0, stack=True, decimals=0, names=LOCKS, none="No room is locked",
                         description="Rooms locked by hand or by a schedule, and rooms whose lock cannot be held "
                                     "now: an unreadable policy, a conflict over shared hardware, openHAB not "
                                     "answering, or a change not yet put back. The other rooms are unlocked."), 12, 7)
    outcomes = {f"{kind} {outcome}": (f"{kind.capitalize()} {plain(outcome).lower()}", color)
                for kind, known in (("boundary", BOUNDARIES), ("repair", REPAIRS)) for outcome, color in known.items()}
    board.add(events("Worker events", [
        Count("thermite_policy_boundaries_total", "boundary {{outcome}}", UNKNOWN, by="outcome"),
        Count("thermite_policy_repairs_total", "repair {{outcome}}", WARN, by="outcome"),
        Count("thermite_policy_errors_total", "Error in {{stage}}", FAIL, by="stage"),
        Count("thermite_callbacks_total", "Device callbacks refused", WARN, where='outcome!="forwarded"')],
        _per_bar("Schedule boundaries by outcome, repairs of drifted locked rooms, worker errors by the stage "
                 "that failed, and device callbacks the relay refused, whatever the reason"),
        none="No worker events", names=outcomes), 12, 7)
    # Each on an axis of its own scale: a pass every few seconds, a check every few hours.
    board.add(timeseries("Worker passes", [
        Series("time() - thermite_policy_loop_timestamp_seconds", "Since last pass", IN_USE),
        Series("histogram_quantile(0.95, sum by (le) (rate(thermite_policy_boundary_lag_seconds_bucket[1h]))) > 0",
               "Schedule lag 95th", IN_USE, line="dashed")], "s", minimum=0, limit=WORKER_STALLED_SECONDS,
        description="Seconds since the worker's last pass (it runs every 5 s), and how late schedule boundaries "
                    f"were applied. Without a pass for {WORKER_STALLED_SECONDS} s (the red band) the worker "
                    "counts as stalled."), 12, 7)
    board.add(timeseries("Oldest locked-room check", [
        Series("max(thermite_policy_reconcile_age_seconds) / 3600", "Since the longest-unchecked room", IN_USE)],
        "h", minimum=0, none="No room is locked",
        description="Hours since the worker last verified the locked room it has left unchecked longest, a lock "
                    "it cannot hold now included."), 12, 7)
    return dashboard(SWITCHING, board, "How source changes go, the routing executor, the gateway and its event "
                     "streams, what remotes gave up on, and the room policy worker.")


# ---------------------------------------------------------------- devices


def _shown(expr: str, room: str) -> str:
    """`expr` while `room` is among the rooms chosen on the board: a row
    belongs to the room it is shown in, which a shared receiver's zone 2
    has and its series does not."""
    return f"({expr}) and on() thermite_room_info{{room={_q(room)},{ROOMS}}}"


#: Characters of a timeline row's name. Grafana sizes the names' column by
#: the name with the most characters, a little short, and cuts what does not
#: fit at the name's start: the longer the names, the more of a room's first
#: letters go.
ROW_NAME = 40


def _in_room(room: str, label: str) -> str:
    """A row's name in a timeline of every room: its room, then what it is,
    unless its own name starts with the room. One too long for the row
    (ROW_NAME) loses its model in brackets, and then its middle: a short
    last part may be all that tells a receiver's zones apart."""
    name = label if not room or label.lower().startswith(room.lower()) else f"{room} · {label}"
    if len(name) > ROW_NAME:
        name = re.sub(r" \([^()]*\)", "", name)
    if len(name) <= ROW_NAME:
        return name
    _, dot, last = name.rpartition(" · ")
    kept = dot + last if dot and len(last) <= 12 else ""
    return name[:ROW_NAME - 1 - len(kept)].rstrip(" ·") + "…" + kept


def _things_not_online(mapping: dict) -> Panel:
    """The house's device Things that are not ONLINE, with their status in
    words: OFFLINE is often a device asleep, UNINITIALIZED a setup fault."""
    statuses = value_map({name: (name.capitalize(), _thing_colour(name)) for name in THING_STATE_NAMES})
    # The device and its status first: a phone shows those, and scrolls to the rest.
    return table("Things not online", f'thermite_thing_status_info{{status!="ONLINE",{ROOMS}}}', [
        Column("device", "Device", _words(_device_words(mapping)), least=130),
        Column("status", "Status", states=statuses, width=120),
        Column("thing", "Thing"), _room_column(mapping, last=True)],
        "Each device's Things the runtime does not have ONLINE, and in what status. Offline: the device "
        "does not answer, as a sleeping one often does. Unknown or initializing: the runtime is still "
        "connecting. Uninitialized: its binding or settings are missing or wrong, which waking the "
        "device will not mend. Room: the device's own.",
        none="Every Thing is online", sort=("Device", False))


def devices(mapping: dict, probes: list[dict]) -> dict:
    board = Board()
    known = mapping.get("devices") or {}
    rooms = mapping.get("rooms") or {}
    info = f"thermite_device_info{{{ROOMS}}}"
    online = f"thermite_thing_online{{{ROOMS}}}"
    probe = f'job="device_tcp",{ROOMS}'
    # House-wide checks name a job or service as their device: no row of their own.
    needed = (f'((count by (device) ({alerts.CHECK}{{required="true",device!=""}}) * 0 + 1) '
              f"and on(device) {info}) or (count by (device) ({info}) * 0)")
    # Of the Things that are offline now, the one offline longest: when each was last online, and 0
    # (Not in the last week) for one that was at none of the week's five-minute steps.
    offline = f"({online} == 0)"
    last_online = (f"min by (device) (max by (device, thing) (max_over_time(timestamp({online} == 1)[7d:5m])) "
                   f"and on(device, thing) {offline} or on(device, thing) {offline} * 0) * 1000")
    # How many of a device's signals say trouble, 0 for one with none: the order of the rows.
    signals = (("thing", f"min by (device) ({online}) == bool 0"),
               ("port", f"min by (device) (probe_success{{{probe}}}) == bool 0"),
               ("problem", f"max by (device) (thermite_device_problem{{{ROOMS}}}) > bool 0"),
               ("none", f"count by (device) ({info}) * 0"))
    trouble = "sum by (device) (" + " or ".join(
        f'label_replace({expr}, "signal", "{name}", "", "")' for name, expr in signals) + ")"
    state = {"width": 78}
    # The device and its states first: a phone shows those, and scrolls to the rest.
    board.add(table("Devices", [
        f"max by (device, room, label) ({info})",
        f"min by (device) ({online})",
        f"min by (device) (probe_success{{{probe}}})",
        f"max by (device) (probe_duration_seconds{{{probe}}}) * 1000",
        last_online,
        f"max by (device) (thermite_device_problem{{{ROOMS}}})",
        needed,
        trouble,
        f'min by (device) (thermite_item_value{{point="online",{ROOMS}}})'], [
        Column("label", "Device", least=130, wrap=True),
        Column("Value #B", "Thing", states=ONLINE, **state), Column("Value #C", "Port", states=OPEN, **state),
        Column("Value #G", "Needed", states=NEEDED, **state),
        Column("Value #F", "Problem", states=PROBLEM, width=92),
        Column("Value #I", "Ping", states=REACHABLE, width=104),
        Column("Value #D", "Probe", unit="ms", decimals=1, width=86),
        Column("Value #E", "Offline since", _words({"0": "Not in the last week"}), unit="dateTimeFromNow", width=150),
        _room_column(mapping, last=True)],
        "One row per device, those in trouble first, then those needed now. Thing: the runtime's connection to "
        "it (Things not online says in what status). Port: its control port accepts a TCP connection; Probe: "
        "how long that took. Ping: it answers a ping, or its controller reports it connected; a device that "
        "sleeps answers none, so this alone is no trouble and no alert. "
        "Needed: a room's selected source uses it (the source the room last reported, "
        f"for {alerts.HELD} at most, while it reports none), it is routing infrastructure, or it is "
        f"a speaker in use: it played in the last {alerts.IN_USE}, or was playing when it stopped answering. "
        "An offline device no room needs is usually asleep. Offline since: when the Thing of it that has been "
        "offline longest was last online, to five minutes. A click on a room keeps the board to that room.",
        none="No device in the chosen rooms",
        order=(("room", False), ("Value #G", True), ("Value #H", True))),
        24, min(20, max(8, len(known) + 2)))
    board.add(_things_not_online(mapping), 24, 6)
    # One timeline of every room per signal: the time axes line up, and a row is as tall in one as in another.
    zones = _zones(mapping)
    queries, names = _source_timeline(mapping, zones)
    sources = [(_shown(query, room), label) for (_, room, _), (query, label) in zip(zones, queries)]
    power = sorted((_in_room(str(rooms.get(room, room)), label), _shown(f"max(thermite_item_value{{item={_q(item)}}})",
                                                                        room))
                   for room, item, label in _power_rows(mapping))
    speakers = [(_shown(f"max(thermite_item_value{{item={_q(item)}}})", room),
                 _in_room(str(rooms.get(room, room)), str((known.get(ident) or {}).get("label", ident))))
                for item, ident, room in _speakers(mapping)]
    if sources or power or speakers:
        board.row("Through the day")
    gap = "A gap: unknown. Only the rooms chosen at the top are shown."
    if sources:
        board.add(state_timeline("Sources", sources, names, "Each zone's confirmed source, in tan, and Off in "
                                 "slate. Grey: the room's devices gave no readback. " + gap, show_values=True,
                                 none="No zone in the chosen rooms"), 24, len(sources) + 2)
    if power:
        board.add(state_timeline("Power", [(query, label) for label, query in power], ON_OFF, "Each device's "
                                 "power, On in tan and Off in slate; Off is normal. A device is shown in the "
                                 "room that needs it on. " + gap, show_values=True,
                                 none="No device with a power state in the chosen rooms"), 24, len(power) + 2)
    if speakers:
        board.add(state_timeline("Speakers", speakers, PLAYBACK, "What each speaker plays: a track, or TV or "
                                 "line-in, in tan, and idle in slate. " + gap, show_values=True,
                                 none="No speaker in the chosen rooms"), 24, len(speakers) + 2)
    volumes = [Series(f'thermite_item_value{{device={_q(ident)},point="volume",{ROOMS}}}',
                      str(device.get("label", ident)), IN_USE)
               for ident, device in sorted(known.items()) if device.get("kind") in LISTENING_KINDS
               and "volume" in _points(mapping, ident)]
    if volumes:
        board.row("Listening levels")
        board.add(timeseries("Listening levels", volumes, minimum=0, none="No volume in the chosen rooms",
                             description="Receivers', amplifiers' and speakers' volume settings in their own "
                                         "scales: not percentages, not loudness. Unknown stays a gap. Choose a "
                                         "room at the top to tell its lines apart."), 24, 8)
    return dashboard(DEVICES, board, "Every device's connection, control port, problem and whether a room needs it "
                     "now, trouble first; then every source, power and speaker through the day. Room narrows it "
                     "to the rooms you choose.", variables=(room_variable(mapping),))


# ---------------------------------------------------------------- openHAB and the host


def _thing_colour(status: str) -> str:
    """Amber, not red, for a Thing that is not ONLINE: a sleeping device's
    Thing is OFFLINE, and House health says whether a room needs it."""
    return OK if status == "ONLINE" else UNKNOWN if status in ("UNINITIALIZED", "INITIALIZING", "UNKNOWN") else WARN


#: Rows of a board's table or timeline of the busiest or longest of many.
MOST = 6
#: Things the openHAB board's timeline has rows for.
THINGS_SHOWN = 12


def _busiest(title: str, by: str, heading: str, series: str, description: str) -> Panel:
    """The MOST values of `by` that `series` counted most in the selected
    range, with how often per minute: a table, since on a chart those with
    one rate draw one line."""
    most = f"topk({MOST}, sum by ({by}) (increase({series}[$__range])))"
    return table(title, [most, f"{most} / ($__range_s / 60)"], [
        Column(by, heading), Column("Value #A", "In the range", decimals=0, width=120),
        Column("Value #B", "Per minute", decimals=1, width=110)], description, none="None",
        sort=("In the range", True))


def openhab(mapping: dict | None = None) -> dict:
    m = OPENHAB_METRICS
    job = 'job="openhab"'
    board = Board()
    board.add(stat("Scrape", f"up{{{job}}}", mappings=UP_DOWN), 8, TILE)
    board.add(stat("Things ONLINE", f"count({m['thing_state']}{{{job}}} == 3) or vector(0)"), 8, TILE)
    board.add(stat("Things not ONLINE", f"count({m['thing_state']}{{{job}}} != 3) or vector(0)",
                   steps=((OK, None), (WARN, 1)),
                   description="Includes devices that are asleep or off; Devices says which are needed now."), 8, TILE)
    board.add(stat("Items missing", "thermite_items_missing", steps=((OK, None), (WARN, 1)),
                   description="Items the generated configuration has that the runtime does not: a deploy it has "
                               "not loaded, or an Item file it refused. Their checks read Unknown."), 8, TILE)
    board.add(stat("Heap used",
                   f'sum({m["memory_used"]}{{{job},area="heap"}}) / sum({m["memory_max"]}{{{job},area="heap"}} > 0)',
                   "percentunit", ((OK, None), (WARN, 0.8), (FAIL, 0.9))), 8, TILE)
    handler = bool((mapping or {}).get("handler_pool"))
    board.add(stat("thingHandler busy", "max(thermite_handler_pool_active_threads / "
                   "(thermite_handler_pool_core_threads > 0))", "percentunit", ((OK, None), (WARN, 0.75), (FAIL, 1)),
                   description="Busy share of the thingHandler pool, where bindings do device I/O. At 100% one "
                               "blocked connection delays every device." + ("" if handler else
                               " This house does not report it.")), 8, TILE)
    board.row("JVM and thread pools")
    board.add(timeseries("Heap", [
        Series(f'sum({m["memory_used"]}{{{job},area="heap"}})', "Used", IN_USE),
        Series(f'sum({m["memory_max"]}{{{job},area="heap"}} > 0)', "Max", IN_USE, line="dashed")], "bytes",
        minimum=0), 8, 8)
    board.add(timeseries("GC pause time per second", [
        Series(f'sum by (gc) (rate({m["gc_pause_sum"]}{{{job}}}[5m]))', "{{gc}}", IN_USE)], "percentunit",
        minimum=0), 8, 8)
    board.add(timeseries("Threads", [
        Series(f"{m['threads_live']}{{{job}}}", "Live", IN_USE),
        Series(f"{m['threads_daemon']}{{{job}}}", "Of them daemon", IN_USE, line="dashed")], minimum=0,
        decimals=0), 8, 8)
    board.add(timeseries("Process CPU", [Series(f"{m['process_cpu']}{{{job}}}", "CPU", IN_USE)], "percentunit",
                         minimum=0), 8, 8)
    pools = f'{job},pool!="{LISTENING_POOL}"'
    board.add(timeseries("Thread pool use", [
        Series(f"max by (pool) ({m['pool_active']}{{{pools}}} / ({m['pool_threads']}{{{pools}}} > 0)) > 0",
               "{{pool}}", IN_USE)], "percentunit", minimum=0, maximum=1, none="Every pool is idle",
        description="Busy share of each runtime thread pool (active threads over its size); idle pools are "
                    f"left out, and so is {LISTENING_POOL}, whose one thread waits for a client all day. A pool "
                    "that stays at 100% makes its tasks queue (Queued pool tasks)."), 8, 8)
    board.add(timeseries("Queued pool tasks", [
        Series(f"sum by (pool) ({m['pool_queued']}{{{job}}}) > 0", "{{pool}}", WARN)], minimum=0, decimals=0,
        none="No task is queued",
        description="Tasks waiting for a pool thread; pools with none queued are left out."), 8, 8)
    # Threads and tasks on one chart, seconds on another: one axis cannot show both.
    board.add(timeseries("thingHandler pool", [
        Series("thermite_handler_pool_active_threads", "Busy", IN_USE),
        Series("thermite_handler_pool_core_threads", "Size", IN_USE, line="dashed"),
        Series("thermite_handler_pool_queued_tasks", "Queued", WARN)], minimum=0, decimals=0,
        description="Where bindings do device I/O: its busy threads against its size, and the tasks waiting "
                    "for one."), 8, 8)
    board.add(timeseries("thingHandler probe", [
        Series("thermite_handler_pool_probe_overdue_seconds", "Probe overdue", WARN),
        Series("thermite_handler_pool_observation_age_seconds", "Sample age", IN_USE)], "s", minimum=0,
        description="A probe overdue while every thread is busy means a blocked connection is delaying other "
                    "devices. Sample age: how old the runtime's last report of the pool is; over 60 s the pool's "
                    "numbers are left out as stale."), 8, 8)
    board.add(timeseries("REST answer time", [
        Series("max(thermite_scrape_duration_seconds)", "Things and Items", IN_USE)], "s", minimum=0,
        limit=ROUND_SECONDS,
        description="How long the runtime took to answer the exporter's two REST reads, its Things and its "
                    f"Items, every 30 s. At {ROUND_SECONDS:g} s (the red band) the exporter stops waiting and "
                    "counts the runtime as not answering."), 8, 8)
    board.row("Events and rules")
    board.add(timeseries("Events per second", [Series(f"sum(rate({m['event_count']}{{{job}}}[5m]))", "All", IN_USE)],
                         "ops", minimum=0), 8, 8)
    board.add(_busiest("Busiest topics", "topic", "Topic", f"{m['event_count']}{{{job}}}",
                       "The topics with the most events in the selected range."), 16, 8)
    board.add(timeseries("Rule runs per minute", [
        Series(f"sum(rate({m['rule_runs']}{{{job}}}[5m])) * 60", "All", IN_USE)], minimum=0), 8, 8)
    board.add(_busiest("Busiest rules", "rule", "Rule", f"{m['rule_runs']}{{{job}}}",
                       "The rules that ran most in the selected range. The runtime counts rule runs; it does not "
                       "time them."), 16, 8)
    board.row("Things")
    board.add(timeseries("Things by status", [
        Series(f'count_values("state", {m["thing_state"]}{{{job}}})', "{{state}}", UNKNOWN)], minimum=0, decimals=0,
        names={str(index): (name, _thing_colour(name)) for index, name in enumerate(THING_STATE_NAMES)},
        description="Things per ThingStatus."), 12, 10)
    # Every Thing passes through INITIALIZING at each start of the runtime: a row for each that was ever
    # not ONLINE would be all of them, one over another.
    away = f"{m['thing_state']}{{{job}}} != 3"
    # Sampled as coarsely as the chart draws: a range of weeks is not read a minute at a time.
    longest = f"topk({THINGS_SHOWN}, count_over_time(({away})[$__range:$__interval] @ end()))"
    board.add(state_timeline("Things not ONLINE longest", [(f"({away}) and on(thing) {longest}", "{{thing}}")],
        value_map({index: (name, _thing_colour(name)) for index, name in enumerate(THING_STATE_NAMES)}),
        f"The {THINGS_SHOWN} Things that were not ONLINE longest in the selected range, while they were not; a "
        "sleeping device's Thing is often OFFLINE. Devices lists every device's Things that are not online now.",
        show_values=True, none="Every Thing was ONLINE"), 12, 10)
    return dashboard(OPENHAB, board, "The runtime's Metrics add-on: JVM, thread pools, the thingHandler pool, "
                     "events, rules and Things.", annotations=False)


#: What each access service's metrics file is called on the board
#: (oh_access.metrics names each file after its unit).
TEXTFILES = {oh_units.ACCESS: "Gateway", oh_units.ACCESS_WORKER: "Policy worker",
             oh_units.ACCESS_CALLBACKS: "Callback relay"}


def host(targets: int = 0, mapping: dict | None = None) -> dict:
    board = Board()
    node = 'job="node"'
    units = _units(UNITS)
    board.add(stat("Scrape targets", "count(up == 1) or vector(0)", f"suffix:/{targets} up" if targets else "none",
                   steps=((FAIL, None), (OK, targets or 1)),
                   description="Prometheus targets that answered their last scrape. Each device and service "
                               "probe is a target; a probe that fails still answers."), 8, TILE)
    board.add(stat("CPU busy", f'1 - avg(rate(node_cpu_seconds_total{{{node},mode="idle"}}[5m]))', "percentunit",
                   ((OK, None), (WARN, 0.7), (FAIL, 0.9))), 8, TILE)
    board.add(stat("Memory available",
                   f"node_memory_MemAvailable_bytes{{{node}}} / node_memory_MemTotal_bytes{{{node}}}", "percentunit",
                   ((FAIL, None), (WARN, 0.1), (OK, 0.2))), 8, TILE)
    board.add(stat("Root free", f'node_filesystem_avail_bytes{{{node},mountpoint="/"}} / '
                                f'node_filesystem_size_bytes{{{node},mountpoint="/"}}', "percentunit",
                   ((FAIL, None), (WARN, 0.1), (OK, 0.2))), 12, TILE)
    board.add(stat("Metrics storage", "sum(prometheus_tsdb_storage_blocks_bytes) + "
                                      "sum(prometheus_tsdb_wal_storage_size_bytes)", "bytes",
                   steps=((UNKNOWN, None),)), 12, TILE)
    board.add(table("Targets down", "up == 0", [Column("job", "Job"), Column("instance", "Target")],
                    "Scrape targets that did not answer.", none="Every target is up"), 12, 6)
    board.add(_firing(mapping or {}), 12, 6)
    board.row("Services")
    board.add(state_timeline("Units active", [
        (f'node_systemd_unit_state{{{node},name=~{units},state="active"}}', "{{name}}")], UP_DOWN,
        "Thermite's long-running services, including the gateway, its policy worker and its callback relay "
        "where they are installed."), 16, max(8, len(UNITS) + 2))
    board.add(table("Restarts", f'round(sum by (name) (increase(node_systemd_service_restart_total{{{node},'
                    f'name=~"thermite-.*"}}[$__range]))) > 0', [
        Column("name", "Unit"), Column("Value", "Restarts", decimals=0, width=100)],
        "Restarts systemd made in the selected range; units without any are left out.", none="No restarts"),
        8, max(8, len(UNITS) + 2))
    board.add(state_timeline("Service probes", [
        ('probe_success{job="service_http"}', "{{service}}"),
        (f'probe_success{{job="{alerts.GATEWAY_JOB}"}} and on() ({alerts.GATEWAY_INSTALLED})', "{{service}}")],
        UP_DOWN, "Local services answering their checks; the gateway only where it is installed."), 12, 7)
    # The three files the services rewrite, by name: a deploy's own file is written once per deploy, and
    # its age of hours on the same axis laid these flat on zero.
    board.add(timeseries("Metrics textfiles age", [
        Series(f'time() - node_textfile_mtime_seconds{{file=~".*/{unit.removesuffix(".service")}\\\\.prom"}}',
               shown, IN_USE, line=line)
        for (unit, shown), line in zip(TEXTFILES.items(), _LINES)], "s", minimum=0,
        limit=alerts.TEXTFILE_STALE, none="No access service has written its metrics",
        description="Seconds since each access service last wrote its metrics; it writes every 10 s while it "
                    f"runs. From {alerts.TEXTFILE_STALE} s (the red band) what the boards show of it is "
                    "stale, and five minutes of that is the warning "
                    f"\"{alerts.CATALOG['ThermiteServiceMetricsStale'][0]}\"."), 12, 7)
    board.row("Load")
    board.add(timeseries("CPU", [
        Series(f'sum by (mode) (rate(node_cpu_seconds_total{{{node},mode!="idle"}}[5m])) / '
               f'scalar(count(node_cpu_seconds_total{{{node},mode="idle"}}))', "{{mode}}", IN_USE)],
        "percentunit", stack=True, minimum=0), 12, 8)
    board.add(timeseries("Load", [
        Series(f"node_load1{{{node}}}", "1m", IN_USE), Series(f"node_load5{{{node}}}", "5m", IN_USE, line="dashed"),
        Series(f"node_load15{{{node}}}", "15m", IN_USE, line="dotted")], minimum=0), 12, 8)
    board.add(timeseries("Memory", [
        Series(f"node_memory_MemTotal_bytes{{{node}}} - node_memory_MemAvailable_bytes{{{node}}}", "Used", IN_USE),
        Series(f"node_memory_MemAvailable_bytes{{{node}}}", "Available", OFF)], "bytes", stack=True, minimum=0),
        12, 8)
    board.add(timeseries("Temperatures", [
        Series(f"node_hwmon_temp_celsius{{{node}}}", "{{chip}} {{sensor}}", IN_USE)], "celsius"), 12, 8)
    board.row("Disk")
    board.add(timeseries("Free space", [
        Series(f'node_filesystem_avail_bytes{{{node},fstype!~"tmpfs|overlay|squashfs"}}', "{{mountpoint}}", IN_USE)],
        "bytes", minimum=0), 6, 8)
    board.add(timeseries("btrfs", [
        Series(f"sum by (device) (node_btrfs_device_unused_bytes{{{node}}})", "{{device}} unallocated", IN_USE),
        Series(f"sum by (block_group_type) (node_btrfs_size_bytes{{{node}}} - node_btrfs_used_bytes{{{node}}})",
               "{{block_group_type}} free in its chunks", IN_USE, line="dashed")],
        "bytes", minimum=0, none="No btrfs file system",
        description="Unallocated: what btrfs can still turn into data or metadata chunks; when it runs out, "
                    "metadata can fill while df still shows free space."), 6, 8)
    board.add(events("btrfs errors", [
        Count("node_btrfs_device_errors_total", "{{device}}", FAIL, by="device", where=node)],
        _per_bar("New device read, write, flush, checksum or generation errors"), none="No btrfs errors"), 6, 8)
    board.add(timeseries("Disk I/O", [
        Series(f"rate(node_disk_read_bytes_total{{{node}}}[5m])", "{{device}} read", IN_USE),
        Series(f"rate(node_disk_written_bytes_total{{{node}}}[5m])", "{{device}} write", IN_USE, line="dashed")],
        "Bps",
        minimum=0), 6, 8)
    return dashboard(HOST, board, "This host: its scrape targets, the Thermite units and their restarts, firing "
                     "alerts, CPU, memory, disk and btrfs.")


# ---------------------------------------------------------------- usage


#: The amount a bar of observed time is full at: the selected range.
RANGE = "vector($__range_s)"


def usage(mapping: dict) -> dict:
    board = Board()
    zones = _zones(mapping)
    active = 'thermite_item_info{point="source",value!~"off|other"}'
    observed = 'thermite_item_info{point="source",value!="other"}'
    available = "thermite_openhab_up == 1"
    # A known zero is valid only while the exporter can read the runtime.
    active_count = f"(sum({active}) or vector(0)) and on() ({available})"
    unknown_count = f"({len(zones)} - (count({observed}) or vector(0))) and on() ({available})"
    board.add(note("How the house is used", "Selected sources and what speakers play through the day, **not** a "
                   "health signal: House health is. Off and idle are normal; gaps mean unknown. History builds "
                   "from installation, with no backfill."), 24, 2)
    if not zones:
        if _speakers(mapping):
            if mapping.get("playing"):
                _played(board, mapping)
            _listened(board, mapping)
        else:
            board.add(note("No source observations configured", "This house has no routed source Items or "
                           "speakers; no use is inferred from power or reachability."), 24, 3)
        return dashboard(USAGE, board, "Observed room/source use and listening; Off is not a failure.",
                         annotations=False)
    board.add(timeseries("The house through the day", [
        Series(active_count, "In use", IN_USE),
        Series(f'(sum(thermite_item_info{{point="source",value="off"}}) or vector(0)) and on() ({available})',
               "Off", OFF),
        Series(unknown_count, "Unobserved", UNKNOWN)], stack=True, minimum=0, maximum=len(zones), decimals=0,
        description="Routing zones by observed state. Off is normal; unknown source state is shown separately. "
                    "Runtime outages remain gaps. No presence or exact session is inferred."), 24, 7)
    queries, names = _source_timeline(mapping, zones)
    board.add(state_timeline("Rooms through the day", queries, names,
                             "Each zone's confirmed source readback, sampled every 30 s; not the requested "
                             "source. Tan: a source is selected. Slate: Off. Grey: Unknown, the room's "
                             "devices gave no readback. A gap: no data.", show_values=True),
              24, max(6, len(queries) + 2))
    duration = []
    for name, _, label in zones:
        used = f'sum(count_over_time(thermite_item_info{{item={_q(name)},value!~"off|other"}}[$__range]))'
        known = f'sum(count_over_time(thermite_item_info{{item={_q(name)},value!="other"}}[$__range]))'
        duration.append(f'label_replace((({used}) or ({known}) * 0) * {SAMPLE_SECONDS}, "zone", {_q(label)}, "", "")')
    board.add(bar_gauge("Where time was spent", " or ".join(duration), "zone", IN_USE, "Observed time each "
                        "zone had a source selected in the selected range: samples times the 30-second scrape "
                        "interval. A full bar is the whole range. Overlapping zones add; gaps are not "
                        "extrapolated. Selected-source time, not people or playback.", full=RANGE),
              12, max(9, len(zones) + 2))
    labels = _source_words(mapping)
    board.add(breakdown("Source mix", f"sum by (value) (count_over_time({active}[$__range])) * {SAMPLE_SECONDS} >= 60",
                        "{{value}}", IN_USE, "Share of observed in-use zone time by source. The same source in "
                        "two zones counts twice; Off, unknown and sources used under a minute are left out.",
                        names={value: str(labels.get(value, value)) for value in sorted(
                            {v for name, _, _ in zones for v in mapping["items"][name].get("options") or []}
                            - {"off"})}, none="No source was in use"), 12, max(9, len(zones) + 2))
    if mapping.get("playing"):
        _played(board, mapping)
    if _speakers(mapping):
        _listened(board, mapping)
    return dashboard(USAGE, board, "How the house is used: zones in use, each room's source through the day, "
                     "where time went, the source mix, what played and where speakers played. Off is normal; "
                     "missing observations stay unknown.", annotations=False)


def _listened(board: Board, mapping: dict) -> None:
    """Where the speakers played: through the day, and for how long."""
    rooms = mapping.get("rooms") or {}
    board.row("Listening")
    board.add(_listening_timeline(mapping), 24, max(5, len(_speaker_rooms(mapping)) + 2))
    duration = []
    for room in _speaker_rooms(mapping):
        # A room's speakers sampled as one, so two playing at once count once.
        series = f'max(thermite_item_value{{point="playing",room={_q(room)}}})'
        # Only a room that played: twenty rooms at 0 seconds bury the one that did.
        played = f"count_over_time(({series} == 2)[$__range:{SAMPLE_SECONDS}s])"
        duration.append(f'label_replace({played} * {SAMPLE_SECONDS}, "room", {_q(rooms.get(room, room))}, "", "")')
    board.add(bar_gauge("Where listening time went", " or ".join(duration), "room", IN_USE, "Time each room's "
                        "speakers reported playing a track in the selected range, sampled every 30 s, the room "
                        "that played longest first; a full bar is the whole range, and a room that played "
                        "nothing is left out. Two speakers playing in one room count once, and TV audio and "
                        "line-in not at all. Gaps are not extrapolated. Playback time, not people.", full=RANGE,
                        none="No speaker played a track"), 24, max(5, min(8, len(_speaker_rooms(mapping)) + 2)))


#: The playing series: the Unix time each device was read playing its app
#: and title.
PLAYING = "thermite_playing_timestamp_seconds"


def _what(expr: str, most: int) -> str:
    """expr with `what`: its app and title, joined by " · ", or whichever of
    them it has, its end cut after `most` characters."""
    what = (f'label_replace(label_replace(label_join({expr}, "what", " · ", "app", "title"), '
            '"what", "$1", "what", " · (.*)"), "what", "$1", "what", "(.*) · ")')
    return f'label_replace({what}, "what", "$1…", "what", "(.{{{most}}}).+")'


def _played(board: Board, mapping: dict) -> None:
    """What each device played: when, and for how long in which rooms."""
    # A row is named by what played alone, within ROW_NAME: with the device's name too, the timeline
    # cut the row's name at its start, which is the app and the title. The table names the device.
    shown = f'count_over_time({PLAYING}{{zones!=""}}[$__interval]) > bool 0'
    board.add(state_timeline(
        "What rooms played", [(f"max by (what) ({_what(shown, ROW_NAME)})", "{{what}}")],
        value_map({1: ("Playing", IN_USE)}),
        "Each app and title played while its device was a room's source, one row each, as the app and title "
        "Items read every 30 s: a title where the app sends one (Netflix sends none), a track on a speaker. "
        "The table below says on which device and in which rooms, and has what played where no room showed "
        "it, such as music on a speaker of its own.", none="Nothing played in a room", interval="1m"), 24, 10)
    key = "device, zones, app, title"
    board.add(table("What played, where and for how long", [
        f"min by ({key}) (min_over_time({PLAYING}[$__range])) * 1000",
        f"max by ({key}) (max_over_time({PLAYING}[$__range])) * 1000",
        f"sum by ({key}) (count_over_time({PLAYING}[$__range])) * {SAMPLE_SECONDS}"], [
        Column("device", "Device", _words(_device_words(mapping))),
        Column("zones", "Rooms", _words(_zone_words(mapping))),
        Column("app", "App"), Column("title", "Title"),
        Column("Value #A", "First", unit="dateTimeAsLocal"), Column("Value #B", "Last", unit="dateTimeAsLocal"),
        Column("Value #C", "Time", unit="dtdurations", decimals=0)],
        "Each app and title in the selected range: the rooms whose source the device was (none: no room "
        "showed it), when it was first and last read playing, and for how long: readings times the "
        "30-second scrape interval. Playing means on screen or in a speaker, paused or not.",
        none="Nothing played", sort=("Last", True)), 24, 10)  # the latest first


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
    board.add(table("Last announced state", f"yoke_device_state{{{job}}} == 1", [
        Column("instance", "instance"), Column("node_id", "node_id"), Column("state", "state")],
        "Last accepted announcement; consult liveness before treating it as current."), 12, 7)
    board.add(timeseries("Time since announcement", [
        Series(f"yoke_device_last_seen_age_seconds{{{job}}}", device, IN_USE)], "s", minimum=0), 12, 7)
    board.add(timeseries("Observed control frames", [
        Series(f"rate(yoke_device_control_frames_total{{{job}}}[5m])", device, IN_USE)], "ops", minimum=0), 12, 7)
    board.add(timeseries("Observed control bytes", [
        Series(f"rate(yoke_device_control_bytes_total{{{job}}}[5m])", device, IN_USE)], "Bps", minimum=0,
        description="Control datagrams only; this is not media throughput."), 12, 7)
    board.add(timeseries("Observed device events", [
        Series(f"rate(yoke_device_events_total{{{job}}}[5m])", device + " {{event}}", IN_USE)], "ops",
        minimum=0), 12, 7)
    board.add(timeseries("Observer datagrams", [
        Series(f"rate(yoke_observer_datagrams_total{{{job}}}[5m])", "{{instance}} {{verdict}}", IN_USE)], "ops",
        minimum=0), 12, 7)
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
