"""Grafana dashboards for the house, built from the exporter map.

Four dashboards, each with a fixed uid, all querying the one provisioned
datasource by its fixed uid:

    house     one control-system health result, then observed room/source
              use, source mix and hour-of-day rhythms
    devices   power, listening levels and routing behavior by device kind;
              connection observations remain a separate, collapsed detail
    openhab   openHAB's own metrics (JVM, events, rules, Things, bundles,
              thread pools) from its Metrics add-on
    host      CPU, memory, disk, temperatures, the openHAB and metrics
              units, and Prometheus' own storage

House facts (rooms, devices, Items) come only from the map; the code names
none. Panel ids and positions are assigned in order, so the same map gives
the same JSON.
"""

from __future__ import annotations

import re

DATASOURCE_UID = "thermite-prometheus"
DATASOURCE = {"type": "prometheus", "uid": DATASOURCE_UID}
#: Grafana 12/13's classic dashboard JSON; Grafana migrates older ones on load.
SCHEMA_VERSION = 41
HOUSE, DEVICES, OPENHAB, HOST = "house", "devices", "openhab", "host"
UIDS = {HOUSE: "thermite-house", DEVICES: "thermite-devices", OPENHAB: "thermite-openhab", HOST: "thermite-host"}
TITLES = {HOUSE: "House rhythm", DEVICES: "Device use", OPENHAB: "thermite runtime", HOST: "Host"}
TAG = "thermite"

#: The openHAB Metrics add-on's series, as openHAB 5.2.3 serves them at
#: /rest/metrics/prometheus (read from a booted test openHAB; thermite test
#: boot fails when one is missing, unless ON_ACTIVITY has it). The event
#: counter has no openhab_ prefix, and rules have a run counter but no duration.
OPENHAB_METRICS = {
    "thing_state": "openhab_thing_state",
    "bundle_state": "openhab_bundle_state",
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
#: An OSGi bundle's state value when it is ACTIVE.
BUNDLE_ACTIVE = 32
#: ThingStatus ordinals as the add-on reports them.
THING_STATE_NAMES = ("UNINITIALIZED", "INITIALIZING", "UNKNOWN", "ONLINE", "OFFLINE", "REMOVING", "REMOVED")

#: (key, title, device kinds) of the Devices dashboard's sections, in order;
#: a kind in none of them goes under Other.
KIND_GROUPS = (
    ("receivers", "Receivers and amplifiers", ("avr", "amp", "streaming_amp")),
    ("tvs", "TVs", ("tv",)),
    ("projectors", "Projectors", ("projector",)),
    ("matrices", "Matrices and switches", ("hdmi_matrix", "hdmi_switch", "usb_matrix")),
    ("speakers", "Speakers", ("soundbar", "sub")),
    ("streamers", "Streamers and players", ("streamer", "disc_player", "game_console")),
)
#: The units the Host dashboard shows.
UNITS = ("openhab.service", "openhab-atv.service", "openhab-prometheus.service", "openhab-node-exporter.service",
         "openhab-blackbox.service", "openhab-metrics-exporter.service", "openhab-grafana.service")

SWITCH_POINTS = ("power", "mute", "reachable")
NUMBER_POINTS = ("volume", "input", "routing", "edid")
ENUM_POINTS = ("input", "power_state", "source")

UP_DOWN = [{"type": "value", "options": {"0": {"text": "down", "color": "red", "index": 0},
                                         "1": {"text": "up", "color": "green", "index": 1}}}]
ON_OFF = [{"type": "value", "options": {"0": {"text": "Off", "color": "#394354", "index": 0},
                                        "1": {"text": "On", "color": "#D7B18A", "index": 1}}}]
PALETTE = ("#D7B18A", "#9FAF9A", "#99AFBF", "#BEA2BA", "#C4BD90", "#93B9B3", "#B89E96",
           "#A4A0C2", "#A7BFA2", "#C6A7A5", "#8FA9C2", "#C3B598")
QUIET = "#394354"
CORE_JOBS = 'job=~"openhab|thermite|node|prometheus|blackbox"'
SERVICE_PROBES = 'probe_success{job="service_http"}'
SYSTEMD = 'node_scrape_collector_success{job="node",collector="systemd"}'


def control_health(mapping: dict | None = None) -> str:
    """0 ready / 1 an observed failure / -1 incomplete telemetry.

    An inactive device may sleep. Required checks come from exact active
    paths plus routing infrastructure, not every device in a room.
    Missing checks must never disappear into a reassuring minimum or zero.
    """
    checks = [f"up{{{CORE_JOBS}}}", "thermite_openhab_up", SERVICE_PROBES, SYSTEMD]
    failures = [f"(count({expr} == 0) or vector(0))" for expr in checks]
    complete = [f"count(count by (job) (up{{{CORE_JOBS}}})) == 5",
                "count(thermite_openhab_up) == 1", f"count({SERVICE_PROBES}) == 3", f"count({SYSTEMD}) == 1"]
    for check in (mapping or {}).get("health_checks", []):
        selector = ",".join(f"{key}={_q(value)}" for key, value in check["labels"].items())
        expr = f'{check["metric"]}{{{selector}}}'
        required: dict[str, list[str]] = {}
        for demand in check["required"]:
            required.setdefault(demand["item"], []).append(demand["source"])
        demanded = " or ".join(f'thermite_item_info{{item={_q(item)},value=~{_q("|".join(re.escape(v) for v in values))}}}'
                                 for item, values in required.items())
        gate = "" if check["always"] else f" and on() (count({demanded}) > 0)"
        failures.append(f'(count({expr} == {check["bad"]}){gate} or vector(0))')
        # A missing expected check is unknown while needed, not zero/healthy.
        complete.append(f'(count(absent({expr}){gate}) or vector(0)) == 0')
    if mapping:
        sources = [s for s in mapping.get("items", {}).values() if s.get("point") == "source"]
        if sources:
            complete.append(f'count(thermite_item_info{{point="source",value!="other"}}) == {len(sources)}')
    ready = "vector(0)" + "".join(f" and on() ({expr})" for expr in complete)
    return f'(clamp_max({" + ".join(failures)}, 1) > 0) or ({ready}) or vector(-1)'


# ---------------------------------------------------------------- panels


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    return {"datasource": DATASOURCE, "refId": ref, "expr": expr, "legendFormat": legend or "__auto",
            "range": not instant, "instant": instant}


def _targets(queries: list[tuple[str, str]], instant: bool = False) -> list[dict]:
    return [target(expr, legend, chr(ord("A") + index), instant) for index, (expr, legend) in enumerate(queries)]


def _thresholds(*steps: tuple[str, float | None]) -> dict:
    return {"mode": "absolute", "steps": [{"color": color, "value": value} for color, value in steps]}


def timeseries(title: str, queries: list[tuple[str, str]], unit: str = "short", *, stack: bool = False,
               minimum: float | None = None, maximum: float | None = None, description: str = "") -> dict:
    defaults: dict = {"unit": unit, "noValue": "Unknown", "color": {"mode": "palette-classic"},
                      "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10 if stack else 0,
                                 "showPoints": "never", "spanNulls": False, "lineInterpolation": "stepAfter",
                                 "stacking": {"mode": "normal" if stack else "none", "group": "A"}}}
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    return {"type": "timeseries", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": _targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "none"}}}


def state_timeline(title: str, queries: list[tuple[str, str]], mappings: list | None = None,
                   description: str = "") -> dict:
    defaults = {"noValue": "Unknown", "color": {"mode": "thresholds"},
                "thresholds": _thresholds((QUIET, None), (PALETTE[0], 1)),
                "mappings": mappings or [], "custom": {"fillOpacity": 85, "lineWidth": 0, "spanNulls": False}}
    return {"type": "state-timeline", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": _targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"mergeValues": True, "showValue": "never", "alignValue": "left", "rowHeight": 0.85,
                        "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}}}


def stat(title: str, expr: str, unit: str = "none", steps: tuple = (("green", None),),
         mappings: list | None = None, description: str = "") -> dict:
    return {"type": "stat", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": [target(expr, title, "A", instant=True)],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "Unknown", "color": {"mode": "thresholds"},
                                         "thresholds": _thresholds(*steps), "mappings": mappings or []},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "justifyMode": "center",
                        "textMode": "value", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}


def table(title: str, expr: str, columns: list[str], description: str = "") -> dict:
    """An instant table of the series `expr` gives, showing only `columns` (labels)."""
    keep = {name: index for index, name in enumerate(columns)}
    return {"type": "table", "title": title, "description": description, "datasource": DATASOURCE,
            "targets": [{**target(expr, "", "A", instant=True), "format": "table"}],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
            "transformations": [{"id": "organize", "options": {
                "excludeByName": {"Time": True, "Value": "Value" not in keep, "__name__": True, "job": True,
                                  "instance": True},
                "indexByName": keep, "includeByName": {name: True for name in columns}}}]}


def note(title: str, content: str) -> dict:
    return {"type": "text", "title": title, "transparent": True,
            "options": {"mode": "markdown", "content": content}}


def bars(title: str, queries: list[tuple[str, str]], description: str, *, horizontal: bool = False) -> dict:
    """Compare genuine aggregates, not a separate bar for every current state."""
    panel = timeseries(title, queries, "h", minimum=0, description=description)
    panel.update(type="barchart", targets=_targets(queries, instant=True),
                 transformations=[{"id": "reduce", "options": {"mode": "seriesToRows", "reducers": ["lastNotNull"]}}],
                 options={"xField": "Field", "orientation": "horizontal" if horizontal else "vertical",
                          "barWidth": 0.7, "groupWidth": 0.85, "barRadius": 0.12, "showValue": "auto",
                          "stacking": "none", "xTickLabelRotation": 0, "xTickLabelSpacing": 40,
                          "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}})
    panel["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": PALETTE[0]}
    panel["fieldConfig"]["defaults"]["custom"]["fillOpacity"] = 80
    panel["fieldConfig"]["defaults"]["custom"]["lineWidth"] = 0
    return panel


def _color(panel: dict, name: str, color: str, display: str | None = None) -> None:
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]
    if display:
        props.append({"id": "displayName", "value": display})
    panel["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": name}, "properties": props})


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


def dashboard(name: str, board: Board, description: str, refresh: str = "1m", time_from: str = "now-24h") -> dict:
    return {
        "uid": UIDS[name], "title": TITLES[name], "description": description, "tags": [TAG],
        "schemaVersion": SCHEMA_VERSION, "editable": False, "graphTooltip": 1, "timezone": "browser",
        "refresh": refresh, "time": {"from": time_from, "to": "now"}, "timepicker": {},
        "templating": {"list": []}, "annotations": {"list": []}, "fiscalYearStartMonth": 0, "liveNow": False,
        "links": [{"type": "dashboards", "tags": [TAG], "asDropdown": False, "includeVars": False,
                   "keepTime": True, "title": "Dashboards", "icon": "external link", "targetBlank": False}],
        "panels": board.panels,
    }


def _q(value: str) -> str:
    """A PromQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------- the house


def _points(mapping: dict, device: str) -> set[str]:
    return {spec["point"] for spec in (mapping.get("items") or {}).values() if spec.get("device") == device}


def house(mapping: dict, probes: list[dict]) -> dict:
    board = Board()
    sources = [(name, spec) for name, spec in (mapping.get("items") or {}).items() if spec.get("point") == "source"]
    labels = mapping.get("rooms") or {}
    devices = mapping.get("devices") or {}
    active = 'thermite_item_info{point="source",value!~"off|other"}'
    observed = 'thermite_item_info{point="source",value!="other"}'
    available = 'thermite_openhab_up == 1'
    # A known zero is valid only while the exporter can read the runtime.
    active_count = f'(sum({active}) or vector(0)) and on() ({available})'
    unknown_count = f'({len(sources)} - (count({observed}) or vector(0))) and on() ({available})'
    board.add(note("thermite / house rhythm", "What the house is doing, and how its rhythms change. "
                   "**Off is normal.** Gaps mean unknown."), 24, 3)
    health = stat("Control system", control_health(mapping), steps=((PALETTE[2], None),),
        mappings=[{"type": "value", "options": {
        "-1": {"text": "Unknown", "color": "#99AFBF", "index": 0},
        "0": {"text": "Ready", "color": "#9FAF9A", "index": 1},
        "1": {"text": "Needs attention", "color": "#D7B18A", "index": 2}}}],
        description="Runtime and monitoring, routing infrastructure, and control checks on confirmed active paths. "
                    "Inactive devices may sleep. Missing source/path reports are Unknown; a known failure takes priority. "
                    "This checks control availability, not physical picture or sound quality.")
    health["options"]["colorMode"] = "value"
    board.add(health, 8, 4)
    glance = stat("At a glance", active_count, "short", steps=((PALETTE[2], None),),
        description="In use counts confirmed active routing zones, not people. Unobserved zones have missing or "
                    "unrecognized source state. History is actual retained data, not the configured limit.")
    glance["targets"] = _targets([(active_count, "In use"), (unknown_count, "Unobserved"),
        ("time() - prometheus_tsdb_lowest_timestamp_seconds", "History")], instant=True)
    glance["options"].update(colorMode="value", textMode="value_and_name", orientation="vertical", wideLayout=False,
                             text={"titleSize": 12, "valueSize": 28})
    glance["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "History"},
                                           "properties": [{"id": "unit", "value": "s"}]}]
    board.add(glance, 16, 4)
    if not sources:
        board.add(note("No source observations configured", "This house has no routed source Items. Device use "
                       "is available on **Device use**; no source use is inferred from power or reachability."), 24, 5)
        return dashboard(HOUSE, board, "Control-system readiness and observed house use; Off is not a failure.",
                         time_from="now-24h")
    use = timeseries("The house through the day", [(active_count, "In use"),
        (f'(sum(thermite_item_info{{point="source",value="off"}}) or vector(0)) and on() ({available})', "Off"),
        (unknown_count, "Unobserved")], stack=True, minimum=0, maximum=len(sources),
        description="Observed routing zones. Off is normal; unknown source state is shown separately. "
                    "Runtime outages remain gaps. No presence, playback or exact session duration is inferred.")
    for name, color in [("In use", PALETTE[0]), ("Off", QUIET), ("Unobserved", PALETTE[2])]:
        _color(use, name, color)
    use["fieldConfig"]["defaults"]["custom"]["fillOpacity"] = 55
    board.add(use, 24, 7)
    timeline, duration = [], []
    source_names: dict[str, int] = {}
    for name, spec in sources:
        room = spec["room"]
        label = (mapping.get("paths", {}).get(name) or {}).get("label", labels.get(room, room))
        suffix = name.removeprefix(f"loc_{room}").removesuffix("_source").strip("_")
        if suffix and name not in mapping.get("paths", {}):
            label += " / " + suffix.replace("_", " ").title()
        options = spec.get("options") or []
        terms = []
        for value in options:
            if value not in source_names:
                source_names[value] = len(source_names)
            terms.append(f'thermite_item_info{{item={_q(name)},value={_q(value)}}} * {source_names[value]}')
        if terms:
            timeline.append((f'sum without (value) ({" or ".join(terms)})', label))
        used = f'sum(count_over_time(thermite_item_info{{item={_q(name)},value!~"off|other"}}[$__range]))'
        known = f'sum(count_over_time(thermite_item_info{{item={_q(name)},value!="other"}}[$__range]))'
        duration.append(f'label_replace((({used}) or ({known}) * 0) / 120, "zone", {_q(label)}, "", "")')
    names = {str(index): {"text": "Off" if value == "off" else devices.get(value, {}).get("label", value),
             "color": QUIET if value == "off" else PALETTE[index % len(PALETTE)], "index": index}
             for value, index in source_names.items()}
    if timeline:
        board.add(state_timeline("On screen / selected source", timeline, [{"type": "value", "options": names}],
                  "One continuous line per routing zone. Values are confirmed source readback, not requested source. "
                  "Off is quiet slate; absent or unknown readback remains a gap."), 24, max(6, len(timeline) + 2))
    board.add(bars("Where time was spent", [(" or ".join(duration), "{{zone}}")], "Observed active zone-hours in the selected range: source samples "
                   "× the configured 30-second scrape interval. Overlapping zones add; gaps are not extrapolated. "
                   "This estimates selected-source time, not people, playback or exact sessions.", horizontal=True), 12, 9)
    mix = timeseries("Source mix", [(f'sum by (value) (count_over_time({active}[$__range])) / 120', "{{value}}")],
                     "h", description="Share of observed active zone-hours. The same source in two zones counts twice; "
                     "Off and unknown are excluded. Sample estimates, with no backfill through gaps.")
    mix.update(type="piechart", targets=_targets([(f'sum by (value) (count_over_time({active}[$__range])) / 120',
                                                "{{value}}")], instant=True),
               options={"pieType": "donut", "displayLabels": ["percent"],
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "legend": {"showLegend": True, "displayMode": "table", "placement": "right",
                                   "values": ["value", "percent"]}, "tooltip": {"mode": "single", "sort": "desc"}})
    for value, index in source_names.items():
        _color(mix, value, PALETTE[index % len(PALETTE)], devices.get(value, {}).get("label", value))
    board.add(mix, 12, 9)
    hourly = []
    for hour in range(24):
        sample = f'({active_count}) and on() (hour() == {hour}) and on() (count({observed}) > 0)'
        # Explicit NaN keeps an unobserved clock-hour blank rather than inventing idle time.
        hourly.append((f'(sum_over_time(({sample})[$__range:1m]) / 60) or vector(0/0)', f"{hour:02}:00"))
    board.add(bars("Use by hour of day · UTC", hourly, "Observed active zone-hours, grouped by UTC clock hour over "
                   "the selected range, sampled each minute. Blank hours have no observations; a recorded zero "
                   "means observed idle. Selected-source use, not command volume or presence. Hour labels use UTC; "
                   "timeline axes use your browser timezone."), 24, 9)
    board.add(note("Reading the rhythms", "**History builds from installation; there is no backfill.** "
                   "Source use estimates selected-source time, not presence or playback. Shared sources can overlap. "
                   "Device use, runtime and host details are available in the dashboard links."), 24, 3)
    return dashboard(HOUSE, board, "Observed room/source use and hour-of-day rhythms, with one separate control-system "
                     "health result. Off is normal; missing observations stay unknown.", time_from="now-24h")


def _group_of(kind: str) -> str:
    return next((key for key, _, kinds in KIND_GROUPS if kind in kinds), "other")


def devices(mapping: dict, probes: list[dict]) -> dict:
    board = Board()
    known = mapping.get("devices") or {}
    board.add(note("thermite / device use", "Power and levels describe behavior, **not health**. An intentionally "
                   "off TV or sleeping player is normal. Connection details are observations, without enough "
                   "information to decide whether a device was supposed to be on."), 24, 3)
    sections = [*KIND_GROUPS, ("other", "Other", ())]
    for key, title, _ in sections:
        members = sorted(ident for ident, device in known.items() if _group_of(device.get("kind", "")) == key)
        if not members:
            continue
        powers, volumes, routes = [], [], []
        for ident in members:
            label = known[ident].get("label", ident)
            for point, out in [("power", powers), ("volume", volumes), ("routing", routes)]:
                if point in _points(mapping, ident):
                    out.append((f'thermite_item_value{{device={_q(ident)},point={_q(point)}}}',
                                label + " · {{item}}"))
        if powers or volumes or routes:
            board.row(title)
        if powers:
            board.add(state_timeline("Power · " + title.lower(), powers, ON_OFF,
                                    "Off is normal and is never counted as a control-system failure. Unknown is a gap."),
                      14 if volumes else 24, max(5, len(powers) + 2))
        if volumes:
            board.add(timeseries("Listening levels · " + title.lower(), volumes, "short", minimum=0,
                      description="Raw device volume settings in their native scale, not percentages or measured loudness. "
                                  "Different models may have different scales. Unknown stays a gap."),
                      10 if powers else 24, max(7, len(powers) + 2))
        if routes:
            board.add(state_timeline("Routing selections · " + title.lower(), routes,
                      description="Reported port selections. Port numbers are categories, not a level or a health score."),
                      24, max(6, len(routes) * 2))
    board.row("Connection observations · not a verdict on sleeping devices", collapsed=True)
    board.add(table("Reported Thing state", 'thermite_thing_status * on(device) group_left(label) thermite_device_info',
                    ["room", "label", "thing", "status"],
                    "OFFLINE can accompany an intentionally off device. Intent is not available from a Thing status."), 24, 12)
    board.add(table("Ports not answering", 'probe_success{job="device_tcp"} == 0',
                    ["room", "device", "port", "integration"],
                    "A sleeping device may close its control port. This list is context, not an outage count."), 12, 8)
    board.add(table("Reported device problems", 'thermite_device_problem == 1', ["room", "device"],
                    "Reported problem flags, not proof that an unused device was required. No raw problem text is stored."),
              12, 8)
    return dashboard(DEVICES, board, "Power, listening levels and routing behavior by kind; connection observations "
                     "are separate from intent and health.")


# ---------------------------------------------------------------- openHAB and the host


def openhab() -> dict:
    m = OPENHAB_METRICS
    job = 'job="openhab"'
    board = Board()
    board.add(stat("Scrape", f"up{{{job}}}", mappings=UP_DOWN, steps=(("red", None), ("green", 1))), 4, 4)
    board.add(stat("Things ONLINE", f"count({m['thing_state']}{{{job}}} == 3) or vector(0)"), 5, 4)
    board.add(stat("Things not ONLINE", f"count({m['thing_state']}{{{job}}} != 3) or vector(0)",
                   steps=(("green", None), ("orange", 1))), 5, 4)
    board.add(stat("Bundles not ACTIVE", f"count({m['bundle_state']}{{{job}}} != {BUNDLE_ACTIVE}) or vector(0)",
                   description="Fragments stay RESOLVED, so a few are normal."), 5, 4)
    board.add(stat("Heap used",
                   f'sum({m["memory_used"]}{{{job},area="heap"}}) / sum({m["memory_max"]}{{{job},area="heap"}} > 0)',
                   "percentunit", (("green", None), ("orange", 0.8), ("red", 0.9))), 5, 4)
    board.row("JVM")
    board.add(timeseries("Heap", [(f'sum({m["memory_used"]}{{{job},area="heap"}})', "used"),
                                  (f'sum({m["memory_max"]}{{{job},area="heap"}} > 0)', "max")], "bytes",
                         minimum=0), 8, 8)
    board.add(timeseries("GC pause time per second", [
        (f'sum by (gc) (rate({m["gc_pause_sum"]}{{{job}}}[5m]))', "{{gc}}")], "percentunit", minimum=0), 8, 8)
    board.add(timeseries("Threads", [(f"{m['threads_live']}{{{job}}}", "live"),
                                     (f"{m['threads_daemon']}{{{job}}}", "daemon")], minimum=0), 8, 8)
    board.add(timeseries("Process CPU", [(f"{m['process_cpu']}{{{job}}}", "cpu")], "percentunit", minimum=0), 8, 8)
    board.add(timeseries("Thread pools", [
        (f"{m['pool_active']}{{{job}}}", "{{pool}} active"),
        (f"{m['pool_queued']}{{{job}}}", "{{pool}} queued")], minimum=0), 16, 8)
    board.row("Events and rules")
    board.add(timeseries("Events per second", [(f"sum(rate({m['event_count']}{{{job}}}[5m]))", "all")],
                         "ops", minimum=0), 8, 8)
    board.add(timeseries("Busiest topics", [
        (f"topk(10, rate({m['event_count']}{{{job}}}[5m]))", "{{topic}}")], "ops", minimum=0), 16, 8)
    board.add(timeseries("Rule runs per minute", [
        (f"sum(rate({m['rule_runs']}{{{job}}}[5m])) * 60", "all")], minimum=0), 8, 8)
    board.add(timeseries("Busiest rules, runs per minute", [
        (f"topk(10, sum by (rule) (rate({m['rule_runs']}{{{job}}}[5m])) * 60)", "{{rule}}")], minimum=0,
        description="The runtime counts rule runs; it does not time them."), 16, 8)
    board.row("Things and bundles")
    names = {str(index): {"text": name, "index": index, "color": "green" if name == "ONLINE" else "orange"}
             for index, name in enumerate(THING_STATE_NAMES)}
    board.add(timeseries("Things by status", [
        (f'count_values("state", {m["thing_state"]}{{{job}}})', "{{state}}")], minimum=0,
        description="Things per ThingStatus ordinal (3 is ONLINE)."), 12, 8)
    board.add(state_timeline("Things not ONLINE", [(f"{m['thing_state']}{{{job}}} != 3", "{{thing}}")],
                             [{"type": "value", "options": names}]), 12, 8)
    board.add(table("Bundles not ACTIVE", f"{m['bundle_state']}{{{job}}} != {BUNDLE_ACTIVE}", ["bundle"]), 24, 6)
    return dashboard(OPENHAB, board, "The runtime's Metrics add-on: JVM, events, rules, Things and bundles.")


def host() -> dict:
    board = Board()
    node = 'job="node"'
    units = "|".join(name.replace(".", "\\\\.") for name in UNITS)
    board.add(stat("CPU busy", f'1 - avg(rate(node_cpu_seconds_total{{{node},mode="idle"}}[5m]))', "percentunit",
                   (("green", None), ("orange", 0.7), ("red", 0.9))), 6, 4)
    board.add(stat("Memory available",
                   f"node_memory_MemAvailable_bytes{{{node}}} / node_memory_MemTotal_bytes{{{node}}}", "percentunit",
                   (("red", None), ("orange", 0.1), ("green", 0.2))), 6, 4)
    board.add(stat("Root free", f'node_filesystem_avail_bytes{{{node},mountpoint="/"}} / '
                                f'node_filesystem_size_bytes{{{node},mountpoint="/"}}', "percentunit",
                   (("red", None), ("orange", 0.1), ("green", 0.2))), 6, 4)
    board.add(stat("Metrics storage", "sum(prometheus_tsdb_storage_blocks_bytes) + "
                                      "sum(prometheus_tsdb_wal_storage_size_bytes)", "bytes"), 6, 4)
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
        "bytes", minimum=0), 12, 8)
    board.add(timeseries("Disk I/O", [
        (f"rate(node_disk_read_bytes_total{{{node}}}[5m])", "{{device}} read"),
        (f"rate(node_disk_written_bytes_total{{{node}}}[5m])", "{{device}} write")], "Bps", minimum=0), 12, 8)
    board.row("Services")
    board.add(state_timeline("Units active", [
        (f'node_systemd_unit_state{{{node},name=~"{units}",state="active"}}', "{{name}}")], UP_DOWN), 16, 8)
    board.add(table("Unit restarts", f'node_systemd_service_restart_total{{{node},name=~"{units}"}}',
                    ["name", "Value"]), 8, 8)
    board.add(state_timeline("Scrape targets", [("up", "{{job}} {{instance}}")], UP_DOWN), 12, 8)
    board.add(state_timeline("Service probes", [('probe_success{job="service_http"}', "{{service}}")], UP_DOWN),
              12, 8)
    return dashboard(HOST, board, "This host: CPU, memory, disk, temperatures and the runtime units.")


def render(mapping: dict, probes: list[dict]) -> dict[str, dict]:
    """{name: dashboard} for the exporter map and the device probes."""
    return {HOUSE: house(mapping, probes), DEVICES: devices(mapping, probes), OPENHAB: openhab(), HOST: host()}
