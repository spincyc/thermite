"""Generic Grafana classic-dashboard builder DSL.

Panels are made only by the builders here (BUILDERS), one per kind, and a
Board takes nothing else. What a kind needs to be right is an argument its
builder requires: a chart's colours, the counter an event bar counts (the
builder writes the bucketing), the end of a bar gauge's scale, and the text
an empty table or chart shows.

Colour means one thing everywhere: green OK, amber warning, red failure,
grey unknown, slate Off, and tan for use. Grafana's own palette is never
used: it hands out green and red by series order, whatever the series means.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Sequence
from typing import Any

DATASOURCE_UID = "thermite-prometheus"
DATASOURCE: dict[str, str] = {"type": "prometheus", "uid": DATASOURCE_UID}
#: Grafana 12/13's classic dashboard JSON; Grafana migrates older ones on load.
SCHEMA_VERSION = 41
TAG = "thermite"

#: Panels that count "since yesterday evening" show 18:00 yesterday to now.
#: Grafana takes a panel's own range only where it starts "now-<n><unit>";
#: any other spelling it ignores without a word and uses the board's range.
SINCE_EVENING = "now-1d/d+18h"

# Colour has one meaning on every board.
OK, WARN, FAIL, UNKNOWN, OFF, IN_USE = "#73A96F", "#E0A33A", "#D8574C", "#808A96", "#394354", "#D7B18A"
#: Every colour a state may have.
SEMANTIC: tuple[str, ...] = (OK, WARN, FAIL, UNKNOWN, OFF, IN_USE)


def value_map(options: dict[Any, tuple[str, str]]) -> list[dict[str, Any]]:
    """A value mapping: {value: (text, colour)}, in the order given."""
    return [{"type": "value", "options": {str(value): {"text": text, "color": color, "index": index}
                                          for index, (value, (text, color)) in enumerate(options.items())}}]


#: NaN, as _known() gives for a count it cannot know, shown as Unknown.
NAN_UNKNOWN: list[dict[str, Any]] = [
    {"type": "special", "options": {"match": "nan", "result": {"text": "Unknown", "color": UNKNOWN, "index": 0}}}
]


def plain(value: str) -> str:
    """A label value in words: "probe_failed" as "Probe failed"."""
    return value.replace("_", " ").capitalize()


class Panel:
    """A finished panel. Only the builders below (BUILDERS) make one, and a
    Board takes nothing else: no panel reaches a board without what its
    kind requires, and none is patched afterwards."""

    __slots__ = ("_json",)

    def __init__(self, json: dict[str, Any]) -> None:
        self._json = json


def _semantic(color: str) -> str:
    if color not in SEMANTIC:
        raise ValueError(f"{color!r} is not one of the six colours a panel may use (SEMANTIC)")
    return color


def _ref(index: int) -> str:
    return chr(ord("A") + index)


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict[str, Any]:
    return {"datasource": DATASOURCE, "refId": ref, "expr": expr, "legendFormat": legend or "__auto",
            "range": not instant, "instant": instant}


def _targets(queries: Sequence[tuple[str, str]], instant: bool = False) -> list[dict[str, Any]]:
    return [target(expr, legend, _ref(index), instant) for index, (expr, legend) in enumerate(queries)]


def _thresholds(*steps: tuple[str, float | None]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"color": color, "value": value} for color, value in steps]}


def mapped_thresholds(mappings: list[dict[str, Any]], below: str = UNKNOWN) -> dict[str, Any]:
    """Threshold steps that give each mapped number its mapping's colour, so
    the threshold colour can never contradict the mapping (Grafana colours
    some panels by threshold even where a mapping matched)."""
    numbers: list[tuple[float, str]] = []
    for entry in mappings:
        for key, value in (entry.get("options") or {}).items() if entry.get("type") == "value" else ():
            try:
                numbers.append((float(key), value["color"]))
            except (ValueError, KeyError):
                continue
    steps: list[tuple[str, float | None]] = [(below, None)]
    steps += [(color, int(number) if number.is_integer() else number) for number, color in sorted(numbers)]
    return _thresholds(*steps)


def _words(options: dict[str, str]) -> list[dict[str, Any]]:
    """A text-to-text mapping: a label value shown in words."""
    return [{"type": "value", "options": {key: {"text": text, "index": index}
                                          for index, (key, text) in enumerate(options.items())}}]


#: A mapping that shows any snake_case label value with spaces, for a
#: vocabulary another module owns ("not_found" as "not found").
SPACED: list[dict[str, Any]] = [
    {"type": "regex", "options": {"pattern": "^" + "_".join(["([a-z0-9]+)"] * count) + "$",
                                  "result": {"text": " ".join(f"${n + 1}" for n in range(count)),
                                             "index": count - 2}}}
    for count in range(2, 6)
]


def board_link(uid: str, title: str, **variables: str) -> dict[str, Any]:
    """A link to a dashboard by uid and title that keeps the time range;
    `variables` set its template variables."""
    query = "".join(f"var-{key}={value}&" for key, value in variables.items())
    return {"title": title, "url": f"/d/{uid}?{query}${{__url_time_range}}", "targetBlank": False}


def _base(kind: str, title: str, description: str, since: bool = False) -> dict[str, Any]:
    panel: dict[str, Any] = {"type": kind, "title": title, "description": description, "datasource": DATASOURCE}
    if since:
        panel.update(timeFrom=SINCE_EVENING, hideTimeOverride=True)
    return panel


def _named(names: dict[str, tuple[str, str]] | None) -> list[dict[str, Any]]:
    """{series name: (name shown, colour)} as overrides: a series whose name
    says its state or outcome, in that colour exactly."""
    return [{"matcher": {"id": "byName", "options": name}, "properties": [
        {"id": "color", "value": {"mode": "fixed", "fixedColor": _semantic(color)}},
        {"id": "displayName", "value": shown}]} for name, (shown, color) in (names or {}).items()]


def note(title: str, content: str) -> Panel:
    return Panel({"type": "text", "title": title, "transparent": True,
                  "options": {"mode": "markdown", "content": content}})


def stat(title: str, expr: str, unit: str = "none", steps: tuple[tuple[str, float | None], ...] | None = None,
         mappings: list[dict[str, Any]] | None = None, description: str = "", *,
         to: dict[str, Any] | None = None) -> Panel:
    """A tile: one value on a background of its state's colour, by `steps`
    or, with `mappings`, as the mapped word. `to`: where a click leads."""
    mappings = mappings or []
    thresholds = _thresholds(*steps) if steps else mapped_thresholds(mappings) if mappings else _thresholds((OK, None))
    defaults: dict[str, Any] = {"unit": unit, "noValue": "Unknown", "color": {"mode": "thresholds"},
                                "thresholds": thresholds, "mappings": mappings}
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
    steps: tuple[tuple[str, float | None], ...] = ((OK, None), (WARN, 1))


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
    mappings: list[dict[str, Any]] | None = None
    #: Value mappings with colours: each value as its word, in its colour.
    states: list[dict[str, Any]] | None = None
    unit: str = ""
    decimals: int | None = None
    #: Threshold steps that colour a number's text.
    steps: dict[str, Any] | None = None
    width: int | None = None
    #: The least width of a column that takes what the others leave.
    least: int | None = None
    #: Where a click on a cell leads.
    to: dict[str, Any] | None = None
    #: A long value takes more lines, not an ellipsis: a phone's column is
    #: narrow. Not with `mappings`: Grafana sizes the row by the unmapped value.
    wrap: bool = False
    #: Values start at the column's left, where a number's start at its
    #: right: for states in several words, so that a cut takes their end.
    left: bool = False

    def properties(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if self.left:
            found.append({"id": "custom.align", "value": "left"})
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
    overrides: list[dict[str, Any]] = []
    for column in columns:
        # `none` speaks for an empty table; a row's missing label is a dash.
        properties = ([{"id": "noValue", "value": "—"}] if none or dash else []) + column.properties()
        if properties:
            overrides.append({"matcher": {"id": "byName", "options": column.field}, "properties": properties})
    transformations: list[dict[str, Any]] = [{"id": "merge", "options": {}}] if len(queries) > 1 else []
    transformations += [{"id": "sortBy", "options": {"fields": {}, "sort": [{"field": field, "desc": descending}]}}
                        for field, descending in order]
    transformations.append({"id": "organize", "options": {
        "excludeByName": {"Time": True, "Value": "Value" not in keep, "__name__": True,
                          "job": "job" not in keep, "instance": "instance" not in keep},
        "indexByName": keep, "includeByName": {column.field: True for column in columns},
        "renameByName": {column.field: column.name for column in columns if column.name != column.field}}})
    options: dict[str, Any] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    if sort:
        options["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return Panel({**_base("table", title, description, since),
                  "targets": [{**target(expr, "", _ref(index), instant=True), "format": "table"}
                              for index, expr in enumerate(queries)],
                  "fieldConfig": {"defaults": {"noValue": none} if none else {}, "overrides": overrides},
                  "options": options, "transformations": transformations})


def state_timeline(title: str, queries: list[tuple[str, str]], mappings: list[dict[str, Any]] | None = None,
                   description: str = "", *, show_values: bool = False, none: str = "Unknown",
                   interval: str = "") -> Panel:
    """Rows of states. Mapped values show the mapping's text in its colour;
    an unmapped value is grey (unknown), never a borrowed meaning.

    The colour mode is fixed, never thresholds: with merged values and
    threshold colours, Grafana replaces each value by its threshold range
    ("9+") and ignores the mappings' text and colours."""
    mappings = mappings or []
    defaults: dict[str, Any] = {"noValue": none, "color": {"mode": "fixed", "fixedColor": UNKNOWN},
                                "thresholds": mapped_thresholds(mappings) if mappings else _thresholds((UNKNOWN, None)),
                                "mappings": mappings, "custom": {"fillOpacity": 85, "lineWidth": 0, "spanNulls": False}}
    panel: dict[str, Any] = {**_base("state-timeline", title, description),
                             "targets": _targets(queries), "fieldConfig": {"defaults": defaults, "overrides": []},
                             "options": {"mergeValues": True, "showValue": "auto" if show_values else "never",
                                         "alignValue": "left", "rowHeight": 0.85,
                                         "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
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
_LINES: dict[str, dict[str, Any] | None] = {
    "solid": None,
    "dashed": {"fill": "dash", "dash": [8, 6]},
    "dotted": {"fill": "dot", "dash": [1, 5]},
}


def _drawn(queries: Sequence[Series | Count]) -> list[dict[str, Any]]:
    """Overrides that give each query's series the query's colour: exactly,
    or in shades of it where several series share it."""
    shared = [query.color for query in queries]
    overrides: list[dict[str, Any]] = []
    for index, query in enumerate(queries):
        several = "{{" in query.legend or shared.count(query.color) > 1
        properties: list[dict[str, Any]] = [{"id": "color", "value": {"mode": "shades" if several else "fixed",
                                                                      "fixedColor": _semantic(query.color)}}]
        style = _LINES[getattr(query, "line", "solid")]
        if style:
            properties.append({"id": "custom.lineStyle", "value": style})
        overrides.append({"matcher": {"id": "byFrameRefID", "options": _ref(index)}, "properties": properties})
    return overrides


#: A chart's legend and tooltip.
_LEGEND: dict[str, Any] = {
    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
    "tooltip": {"mode": "multi", "sort": "desc"},
}


def timeseries(title: str, series: list[Series], unit: str = "short", description: str = "", *,
               stack: bool = False, minimum: float | None = None, maximum: float | None = None,
               names: dict[str, tuple[str, str]] | None = None, limit: float | None = None,
               decimals: int | None = None, none: str = "Unknown") -> Panel:
    """Lines over time, each query in the colour its Series names. `names`
    gives a series its name and colour by the name its legend makes:
    {name: (name shown, colour)}. `limit`: a value from which the chart
    is banded red. `none`: what an empty chart says, where its queries
    leave out what is zero."""
    custom: dict[str, Any] = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 70 if stack else 0,
                              "showPoints": "never", "spanNulls": False, "lineInterpolation": "stepAfter",
                              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    # Whatever no query or name claims is grey: unknown, never a borrowed meaning.
    defaults: dict[str, Any] = {"unit": unit, "noValue": none, "color": {"mode": "fixed", "fixedColor": UNKNOWN},
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
        ends there (_grown() with ahead=False), so negative offsets never
        shift past the selected window end when $__range < 1h."""
        selector = _selector(self.counter, self.where)
        if self.by and not _LABELS.fullmatch(self.by):
            raise ValueError(f"{self.by!r} is not a list of labels")
        by = f" by ({self.by})" if self.by else ""
        return f"round(sum{by} ({_grown(selector, ahead=False)})) > 0"


def events(title: str, counts: list[Count], description: str, *, none: str,
           names: dict[str, tuple[str, str]] | None = None) -> Panel:
    """Counts of events as stacked bars over time, one bar per EVENT_BUCKET.
    The builder writes each query from its counter's name, so a bar cannot
    count anything but its own width; `none` says what an empty chart
    means. `names` as for timeseries()."""
    custom: dict[str, Any] = {"drawStyle": "bars", "barAlignment": -1, "lineWidth": 1, "fillOpacity": 70,
                              "showPoints": "never", "spanNulls": False, "stacking": {"mode": "normal", "group": "A"}}
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
        self.panels: list[dict[str, Any]] = []
        self._next_id = 1
        self._x = 0
        self._y = 0
        self._row_height = 0
        self._row: dict[str, Any] | None = None

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _newline(self) -> None:
        self._y += self._row_height
        self._x = 0
        self._row_height = 0

    def add(self, panel: Panel, width: int, height: int) -> None:
        if not isinstance(panel, Panel):
            raise TypeError("a board takes only what a builder made (builders.BUILDERS)")
        if self._x + width > 24:
            self._newline()
        placed = {"id": self._id(), "gridPos": {"x": self._x, "y": self._y, "w": width, "h": height}, **panel._json}
        self._x += width
        self._row_height = max(self._row_height, height)
        (self._row["panels"] if self._row is not None else self.panels).append(placed)

    def row(self, title: str, collapsed: bool = False) -> None:
        """Start a row; a collapsed one holds the panels added after it."""
        self._newline()
        row: dict[str, Any] = {"type": "row", "id": self._id(), "title": title, "collapsed": collapsed,
                               "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}, "panels": []}
        self.panels.append(row)
        self._row = row if collapsed else None
        self._y += 1


def _choice(name: str, heading: str, labels: dict[str, Any]) -> dict[str, Any]:
    """A variable to choose any number of {id: label} by their labels, All
    at first. A query keeps to those chosen by a matcher on the variable."""
    known = sorted(labels.items(), key=lambda each: (str(each[1]), each[0]))
    # Grafana reads "text : value" pairs, split at commas.
    pairs = [f"{str(label).replace(',', ' ').replace(' : ', ': ')} : {ident}" for ident, label in known]
    return {"type": "custom", "name": name, "label": heading, "query": ",".join(pairs), "multi": True,
            "includeAll": True, "allValue": ".*", "current": {"text": ["All"], "value": ["$__all"]},
            "options": [{"text": "All", "value": "$__all", "selected": True},
                        *({"text": str(label), "value": ident, "selected": False} for ident, label in known)]}


def build_dashboard(uid: str, title: str, board: Board, description: str, *, tag: str = TAG,
                    refresh: str = "1m", time_from: str = "now-24h",
                    annotations: Sequence[dict[str, Any]] = (),
                    variables: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """Assemble a Grafana classic dashboard JSON document from a Board."""
    return {
        "uid": uid, "title": title, "description": description, "tags": [tag],
        "schemaVersion": SCHEMA_VERSION, "editable": False, "graphTooltip": 1, "timezone": "browser",
        "refresh": refresh, "time": {"from": time_from, "to": "now"}, "timepicker": {},
        "templating": {"list": list(variables)},
        "annotations": {"list": list(annotations)},
        "fiscalYearStartMonth": 0, "liveNow": False,
        "links": [{"type": "dashboards", "tags": [tag], "asDropdown": True, "includeVars": False,
                   "keepTime": True, "title": "Dashboards", "icon": "external link", "targetBlank": False}],
        "panels": board.panels,
    }


def _q(value: str) -> str:
    """A PromQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _units(names: Iterable[str]) -> str:
    return _q("|".join(re.escape(name) for name in names))
