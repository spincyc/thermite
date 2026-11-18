"""The dashboards' panels come from builders that know their kind: a chart's
colours are named, an event bar counts exactly its own width, and a bar of
amounts has a stated full scale. These tests fail if a panel is made any
other way.

OnSeries then asks what the panels that once misled say of series made up
for the purpose: promtool, from the Prometheus image versions.json pins,
evaluates each panel's own query, with Grafana's variables filled in, as
tests.test_alert_scenarios does for the rules. Without podman or the image
those tests are skipped, saying so."""

import ast
import inspect
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_metrics  # noqa: E402
from oh_metrics import dashboards  # noqa: E402
from oh_metrics.dashboards import Count, Series  # noqa: E402
from tests import test_alert_scenarios, test_metrics  # noqa: E402

#: The panel kinds the builders make; a row is the Board's own.
KINDS = {"text", "stat", "table", "state-timeline", "timeseries", "piechart", "bargauge", "heatmap"}
#: Kinds whose marks take a colour from the field's colour mode.
DRAWN = KINDS - {"text", "table", "heatmap"}
#: What events() and heatmap() write around the growth of a counter in a bucket.
EVENT_BAR = re.compile(r"round\(sum( by \([a-z_]+(, [a-z_]+)*\))? \((?P<grown>.+)\)\) > 0")
HEATMAP = re.compile(r"sum by \(le\) \((?P<grown>.+)\)")
#: The one counter a bucket's growth is read from: its name and label matchers.
COUNTED = re.compile(r"increase\((?P<selector>[a-zA-Z_:][a-zA-Z0-9_:]*_(total|count|bucket)(\{[^{}()\[\]]*\})?)"
                     r"\[\$__interval\]( offset -\$__interval)?\)$")


def counts_a_bucket(expr: str, shape: re.Pattern, ahead: bool) -> bool:
    """Whether `expr` is the builders' count of one counter's growth in a
    bucket, and nothing else."""
    found = shape.fullmatch(expr)
    counted = COUNTED.search(found["grown"]) if found else None
    return bool(counted) and found["grown"] == dashboards._grown(counted["selector"], ahead=ahead)


def houses():
    """Every board of the example house and of houses with speakers, a
    shared receiver, speakers alone and Yoke observers: {name: board}."""
    from tests import test_wiim, test_zone_sync
    found = {}
    for house, inventory in (("example", None), ("speakers", test_metrics.with_speakers()),
                             ("zones", test_zone_sync.house()), ("wiim", test_wiim.house())):
        for name, text in test_metrics.rendered(inventory).items():
            if name.startswith(oh_metrics.GRAFANA_DASHBOARDS):
                found[f"{house} {name.rsplit('/', 1)[1]}"] = json.loads(text)
    found["yoke"] = dashboards.yoke()
    found["usage with what played"] = dashboards.usage({**test_metrics.MAP,
                                                         "playing": test_metrics.Playing.MAPPING["playing"]})
    return found


BOARDS = houses()


def drawn():
    """(board, panel) of every panel that is not a row."""
    for name, board in BOARDS.items():
        for panel in test_metrics.panels(board):
            if panel["type"] != "row":
                yield name, panel


def configs(panel):
    """The field configuration and each override's properties."""
    config = panel.get("fieldConfig", {})
    yield config.get("defaults", {})
    for override in config.get("overrides", []):
        yield {prop["id"]: prop["value"] for prop in override["properties"]}


def colours(value, key=""):
    """Every colour named anywhere in a panel."""
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from colours(inner, name)
    elif isinstance(value, list):
        for inner in value:
            yield from colours(inner, key)
    elif key in ("color", "fixedColor") and isinstance(value, str):
        yield value


class Builders(unittest.TestCase):
    def test_a_board_takes_only_what_a_builder_made(self):
        board = dashboards.Board()
        with self.assertRaisesRegex(TypeError, "only what a builder made"):
            board.add({"type": "timeseries", "title": "By hand", "targets": []}, 12, 8)
        board.add(dashboards.note("About", "Words."), 24, 2)
        self.assertEqual([panel["type"] for panel in board.panels], ["text"])

    def test_only_the_builders_make_a_panel_and_nothing_patches_one(self):
        """Read from the source: a Panel is constructed only inside a
        builder, and what it holds is read only by the Board that places
        it, so no function can make a panel of its own or change a
        builder's afterwards."""
        tree = ast.parse(Path(dashboards.__file__).read_text())
        builders = {builder.__name__ for builder in dashboards.BUILDERS}
        self.assertEqual(builders, {"note", "stat", "figures", "table", "state_timeline", "timeseries", "events",
                                    "breakdown", "bar_gauge", "heatmap"})
        made, read, placed = set(), set(), set()
        for scope in tree.body:
            for node in ast.walk(scope):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Panel":
                    made.add(scope.name)
                if isinstance(node, ast.Attribute) and node.attr == "_json":
                    read.add(scope.name)
                # A board's list of panels is the Board's to fill.
                if isinstance(node, ast.Attribute) and node.attr == "panels" and not isinstance(node.ctx, ast.Load):
                    placed.add(scope.name)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("append", "extend", "insert", "update")
                        and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "panels"):
                    placed.add(scope.name)
        self.assertEqual(made, builders)
        self.assertEqual(read, {"Panel", "Board"})
        self.assertLessEqual(placed, {"Board"})

    def test_every_panel_on_every_board_is_of_a_kind_a_builder_makes(self):
        self.assertEqual({panel["type"] for _, panel in drawn()}, KINDS)

    def test_a_colour_is_required_and_is_one_of_the_six(self):
        with self.assertRaises(TypeError):
            Series("up", "Up")
        with self.assertRaises(TypeError):
            Count("thermite_callbacks_total", "Callbacks")
        for builder in (dashboards.breakdown, dashboards.bar_gauge):
            self.assertIs(inspect.signature(builder).parameters["color"].default, inspect.Parameter.empty)
        for build in (lambda: dashboards.timeseries("Up", [Series("up", "Up", "#5794F2")]),
                      lambda: dashboards.timeseries("Up", [Series("up", "Up", "green")]),
                      lambda: dashboards.timeseries("Up", [Series("up", "{{job}}", dashboards.OK)],
                                                    names={"node": ("Node", "blue")}),
                      lambda: dashboards.events("Calls", [Count("thermite_callbacks_total", "Calls", "red")],
                                                "Calls.", none="None"),
                      lambda: dashboards.breakdown("Mix", "up", "{{job}}", "palette-classic", "Mix."),
                      lambda: dashboards.bar_gauge("Time", "up", "job", "", "Time.", full="vector(1)")):
            with self.assertRaisesRegex(ValueError, "one of the six"):
                build()

    def test_no_panel_takes_grafanas_palette(self):
        """Whatever a panel draws has a colour mode that names one of the
        six colours or follows thresholds made of them: nothing is left to
        Grafana's default, palette-classic, in which an error can draw
        green."""
        allowed = set(dashboards.SEMANTIC) | {"transparent"}
        for name, panel in drawn():
            where = (name, panel["title"])
            self.assertNotIn("palette", json.dumps(panel), where)
            self.assertLessEqual(set(colours(panel)), allowed, where)
            if panel["type"] == "heatmap":
                self.assertEqual((panel["options"]["color"]["mode"], panel["options"]["color"]["fill"]),
                                 ("opacity", dashboards.IN_USE), where)
            if panel["type"] not in DRAWN:
                continue
            modes = [config["color"] for config in configs(panel) if "color" in config]
            self.assertIn("color", panel["fieldConfig"]["defaults"], where)
            for mode in modes:
                self.assertIn(mode["mode"], ("fixed", "shades", "thresholds"), where)
                if mode["mode"] != "thresholds":
                    self.assertIn(mode["fixedColor"], dashboards.SEMANTIC, where)
            if panel["type"] == "timeseries":
                # Each query's series are coloured by that query, not by the default alone.
                claimed = {override["matcher"]["options"] for override in panel["fieldConfig"]["overrides"]
                           if override["matcher"]["id"] == "byFrameRefID"
                           and any(prop["id"] == "color" for prop in override["properties"])}
                self.assertEqual(claimed, {target["refId"] for target in panel["targets"]}, where)

    def test_an_event_bar_counts_exactly_its_own_width(self):
        """Bars come from events() alone, and events() writes each query
        from a counter's name: no bar is labelled an hour and counts five
        minutes, or the other way round."""
        bars = 0
        for name, panel in drawn():
            where = (name, panel["title"])
            styles = [config.get("custom", {}).get("drawStyle") for config in configs(panel)]
            styles += [config.get("custom.drawStyle") for config in configs(panel)]
            if "bars" not in styles:
                continue
            bars += 1
            self.assertEqual(panel["fieldConfig"]["defaults"]["custom"]["drawStyle"], "bars", where)
            self.assertEqual(panel["fieldConfig"]["defaults"]["custom"]["barAlignment"], 1, where)
            for query in panel["targets"]:
                self.assertTrue(counts_a_bucket(query["expr"], EVENT_BAR, ahead=True), (where, query["expr"]))
                self.assertEqual(query["interval"], dashboards.EVENT_BUCKET, where)
        # Seven on Switching & remotes, the commands on Devices and btrfs errors on Host, in each of four houses.
        self.assertEqual(bars, 4 * 9)
        for _, panel in drawn():
            for config in configs(panel):
                self.assertNotIn("custom.drawStyle", config, panel["title"])

    def test_a_heatmap_cell_counts_exactly_its_own_width(self):
        cells = 0
        for name, panel in drawn():
            if panel["type"] == "heatmap":
                cells += 1
                query, = panel["targets"]
                self.assertTrue(counts_a_bucket(query["expr"], HEATMAP, ahead=False), (name, query["expr"]))
                self.assertTrue(query["interval"], name)
                # A cell that counts one is drawn where no cell counts less.
                self.assertEqual(panel["options"]["color"]["min"], 0, name)
        self.assertEqual(cells, 4)  # Time to switch, in each of four houses

    def test_events_take_a_counters_name_not_a_query(self):
        self.assertEqual(
            dashboards._grown("a_total", ahead=True),
            "((max_over_time(a_total[$__interval] offset -$__interval) - ((last_over_time(a_total[$__interval]) <= "
            "min_over_time(a_total[$__interval] offset -$__interval)) or (last_over_time(a_total[$__interval]) * 0))) "
            "and (resets(a_total[$__interval] offset -$__interval) == 0)) "
            "or increase(a_total[$__interval] offset -$__interval)")
        self.assertEqual(Count("thermite_route_seconds_count", "{{outcome}}", dashboards.OK, by="outcome").expr(),
                         "round(sum by (outcome) ("
                         + dashboards._grown("thermite_route_seconds_count", ahead=True) + ")) > 0")
        self.assertEqual(Count("thermite_callbacks_total", "Refused", dashboards.WARN,
                               where='outcome!="forwarded"').expr(),
                         "round(sum ("
                         + dashboards._grown('thermite_callbacks_total{outcome!="forwarded"}', ahead=True) + ")) > 0")
        for count in (Count("increase(thermite_callbacks_total[1h])", "Calls", dashboards.OK),
                      Count("thermite_callbacks_total[1h]", "Calls", dashboards.OK),
                      Count("thermite_item_value", "A gauge", dashboards.OK),
                      Count("thermite_callbacks_total", "Calls", dashboards.OK,
                            where='outcome="x"}[1h]) or up{job="a"'),
                      Count("thermite_callbacks_total", "Calls", dashboards.OK, by="outcome) (rate(x[5m]")):
            with self.assertRaises(ValueError):
                count.expr()
        for histogram, where in (("sum by (le) (rate(a_bucket[5m]))", ""), ("thermite_route_seconds", 'a="b"} or up{')):
            with self.assertRaises(ValueError):
                dashboards.heatmap("Time", histogram, where, "")

    def test_no_bar_of_amounts_scales_to_its_largest_value(self):
        """A bar gauge's scale ends at a stated amount: left to itself,
        Grafana ends it at the largest bar, so 30 seconds fills the panel."""
        self.assertIs(inspect.signature(dashboards.bar_gauge).parameters["full"].default, inspect.Parameter.empty)
        gauges = [(name, panel) for name, panel in drawn() if panel["type"] in ("gauge", "bargauge")]
        self.assertTrue(gauges)
        for name, panel in gauges:
            where = (name, panel["title"])
            fixed = isinstance(panel["fieldConfig"]["defaults"].get("max"), (int, float))
            scaled = [step["options"] for step in panel.get("transformations", []) if step["id"] == "configFromData"]
            if not fixed:
                self.assertEqual(len(scaled), 1, where)
                self.assertEqual(scaled[0]["mappings"], [{"fieldName": "full", "handlerKey": "max"}], where)
                self.assertEqual(scaled[0]["applyTo"], {"id": "byFrameRefID", "options": "A"}, where)
                full = next(query for query in panel["targets"] if query["refId"] == scaled[0]["configRefId"])
                self.assertEqual(full["legendFormat"], "full", where)
            self.assertEqual(panel["fieldConfig"]["defaults"]["min"], 0, where)

    def test_a_panels_own_range_is_one_grafana_takes(self):
        """Grafana applies a panel's timeFrom only where it starts
        "now-<n><unit>"; any other spelling it ignores without a word, and
        "since yesterday evening" counted the board's whole range."""
        self.assertRegex(dashboards.SINCE_EVENING, r"^now-\d+[smhdwMy]")
        since = [panel["title"] for name, panel in drawn() if name == "example house.json" and "timeFrom" in panel]
        self.assertEqual(since, ["Since yesterday evening", "Switch time by room"])
        for _, panel in drawn():
            self.assertIn(panel.get("timeFrom", dashboards.SINCE_EVENING), dashboards.SINCE_EVENING, panel["title"])


class Words(unittest.TestCase):
    """The boards name the access services' label values in words; each
    table here is a copy, since the module that defines them needs
    prometheus_client, which the generator does not."""

    def test_the_word_tables_cover_what_the_access_services_count(self):
        from oh_access import metrics
        self.assertEqual(set(dashboards.CLIENT_WORDS), set(metrics.CLIENTS))
        self.assertEqual(set(dashboards.KIND_WORDS), set(metrics.CLIENT_KINDS))
        self.assertEqual(set(dashboards.PROTOCOLS), set(metrics.CLIENT_PROTOCOLS))
        self.assertEqual(set(dashboards.STREAM_ENDS), set(metrics.SSE_ENDS))
        self.assertEqual(set(dashboards.LOCKS), set(metrics.ROOM_STATES) - {"unlocked"})
        self.assertEqual(set(dashboards.BOUNDARIES), set(metrics.BOUNDARY_OUTCOMES))
        self.assertEqual(set(dashboards.REPAIRS), set(metrics.REPAIR_OUTCOMES))
        self.assertEqual({unit.removesuffix(".service") for unit in dashboards.TEXTFILES},
                         {metrics.GATEWAY_FILE, metrics.WORKER_FILE, metrics.CALLBACKS_FILE})

    def test_no_series_of_the_remotes_or_the_worker_is_named_by_a_raw_label(self):
        """Every name a legend can make for a client event, a protocol, a
        stream's end, a lock or a worker outcome has words and a colour."""
        from oh_access import metrics
        switching = BOARDS["example switching.json"]
        named = {panel["title"]: {override["matcher"]["options"] for override in panel["fieldConfig"]["overrides"]
                                  if override["matcher"]["id"] == "byName"}
                 for panel in test_metrics.panels(switching) if panel["type"] == "timeseries"}
        kinds = {kind for kind in metrics.CLIENT_KINDS if kind.endswith("_timeout")}
        self.assertEqual(named["Remotes gave up"], {f"{client} {kind}" for client in metrics.CLIENTS for kind in kinds})
        self.assertEqual(named["Event stream failures"], {f"{client} {kind}" for client in metrics.CLIENTS
                                                          for kind in set(metrics.CLIENT_KINDS) - kinds})
        self.assertEqual(named["Protocols remotes use"], set(metrics.CLIENT_PROTOCOLS))
        self.assertEqual(named["Event stream lifetimes"], set(metrics.SSE_ENDS))
        self.assertEqual(named["Locked rooms"], set(metrics.ROOM_STATES) - {"unlocked"})
        self.assertEqual(named["Worker events"], {f"boundary {outcome}" for outcome in metrics.BOUNDARY_OUTCOMES}
                         | {f"repair {outcome}" for outcome in metrics.REPAIR_OUTCOMES})
        self.assertEqual(named["Operations by outcome"], set(dashboards.OPERATION_OUTCOMES))
        for panel in test_metrics.panels(switching):
            for config in configs(panel):
                if "displayName" in config:
                    self.assertNotIn("_", config["displayName"], panel["title"])


class Phone(unittest.TestCase):
    """The owner's first screen is 390 px wide."""

    def test_the_usage_note_is_as_long_as_its_panel_shows_on_a_phone(self):
        """A note three grid rows tall shows two lines of some 44
        characters at 390 px, and cut the rest of a longer one."""
        note, = [panel for panel in test_metrics.panels(BOARDS["example usage.json"])
                 if panel["type"] == "text" and panel["title"] == "How the house is used"]
        self.assertEqual(note["gridPos"]["h"], 3)
        self.assertLessEqual(len(note["options"]["content"].replace("*", "")), 2 * 44)

    def test_the_devices_table_starts_with_what_a_phone_can_show(self):
        """A phone shows the device and two or three states before the
        table scrolls sideways: what says trouble now comes first."""
        table, = [panel for panel in test_metrics.panels(BOARDS["example devices.json"]) if panel["title"] == "Devices"]
        order = table["transformations"][-1]["options"]["indexByName"]
        names = {"label": "Device", **table["transformations"][-1]["options"]["renameByName"]}
        self.assertEqual([names[field] for field in sorted(order, key=order.get)][:5],
                         ["Device", "Thing", "Port", "Reads", "Needed"])


class Rooms(unittest.TestCase):
    """Devices is one board for every room, kept to the rooms chosen in
    its Room variable."""

    def test_devices_has_a_room_variable_and_every_query_keeps_to_it(self):
        board = BOARDS["zones devices.json"]
        (room,) = board["templating"]["list"]
        self.assertEqual((room["name"], room["multi"], room["includeAll"], room["allValue"]),
                         (dashboards.ROOM, True, True, ".*"))
        self.assertEqual([option["value"] for option in room["options"]][:3], ["$__all", "example_cinema",
                                                                               "example_lounge"])
        self.assertIn("Example Cinema : example_cinema", room["query"].split(","))
        for panel in test_metrics.panels(board):
            for query in panel.get("targets", []):
                self.assertIn('room=~"$room"', query["expr"], panel["title"])
        for name, other in BOARDS.items():
            if not name.endswith("devices.json"):
                self.assertEqual(other["templating"]["list"], [], name)
                self.assertNotIn("$room", json.dumps(other), name)

    def test_tiles_and_rooms_lead_to_the_board_that_says_more(self):
        house = BOARDS["speakers house.json"]
        links = {panel["title"]: [link["url"] for link in panel["fieldConfig"]["defaults"].get("links", [])]
                 for panel in test_metrics.panels(house) if panel["type"] == "stat"}
        time = "${__url_time_range}"
        self.assertEqual(links["Rooms ready"], [f"/d/thermite-devices?{time}"])
        self.assertEqual(links["Speakers ready"], [f"/d/thermite-devices?{time}"])
        self.assertEqual(links["Remotes"], [f"/d/thermite-switching?{time}"])
        self.assertEqual(links["Host"], [f"/d/thermite-host?{time}"])
        attention = next(panel for panel in test_metrics.panels(house) if panel["title"] == "Needs attention now")
        room = next(override for override in attention["fieldConfig"]["overrides"]
                    if override["matcher"]["options"] == "room")
        (link,) = next(prop["value"] for prop in room["properties"] if prop["id"] == "links")
        self.assertEqual(link["url"], "/d/thermite-devices?var-room=${__value.raw}&" + time)


def query(board: str, title: str, index: int = 0, *, interval: str = "1h", span: str = "1h", end: int = 0) -> str:
    """A panel's query on the example house's board, as Grafana sends it:
    with the step, the selected range (and where it ends, in seconds) and
    every room filled in."""
    found, = [panel for panel in test_metrics.panels(BOARDS[f"example {board}.json"]) if panel.get("title") == title]
    seconds = {"m": 60, "h": 3600, "d": 86400}[span[-1]] * int(span[:-1])
    return (found["targets"][index]["expr"].replace("$__interval", interval).replace("$__range_s", str(seconds))
            .replace("$__range", span).replace(f"${dashboards.ROOM}", ".*").replace("@ end()", f"@ {end}"))


def counter(events: list[int], samples: int, *, begins_again: int | None = None) -> str:
    """A counter's samples, one an interval: how many of `events` (each the
    number of the sample that first shows it) it has counted, from zero
    again at the sample `begins_again`."""
    values, count = [], 0
    for sample in range(samples):
        count = events.count(sample) if sample == begins_again else count + events.count(sample)
        values.append(str(count))
    return " ".join(values)


class OnSeries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = test_alert_scenarios.unavailable()
        if reason:
            raise unittest.SkipTest(reason)
        cls.podman, cls.image = shutil.which("podman"), test_alert_scenarios.image()

    def shows(self, inputs: dict[str, str], expected: list, *, interval: str = "30s", last: str) -> None:
        """Evaluate over `inputs`, a sample each `interval`. expected:
        (time, query, [(series, value)]), exactly. last: the time of the
        last sample: promtool loads nothing past its last evaluation."""
        expected = [*expected, (last, "vector(1)", [("{}", 1)])]
        test = {"interval": interval,
                "input_series": [{"series": name, "values": samples} for name, samples in inputs.items()],
                "promql_expr_test": [{"expr": expr, "eval_time": when,
                                      "exp_samples": [{"labels": name, "value": value} for name, value in each]}
                                     for when, expr, each in expected]}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "panels.yml").write_text(yaml.safe_dump({"evaluation_interval": interval,
                                                                      "tests": [test]}))
            ran = subprocess.run([self.podman, "run", "--rm", "--pull=never", "--network=none", "--user=0",
                                  "--security-opt=label=disable", "-v", f"{directory}:/rules:ro",
                                  "--entrypoint=promtool", self.image, "test", "rules", "/rules/panels.yml"],
                                 capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_event_bars_side_by_side_count_every_event_once(self):
        """Hours of 120 samples: sample 120 is the last of the first hour,
        121 the first of the second. Sixteen confirmed changes in the second
        hour, the last of them first shown by sample 241, the third hour's
        first: 15 and 1, where increase() of each hour gave 15 and none.
        One missed change two samples into the third hour, where that
        series ends, as the newest bar's does: 1, where increase() gave 2.
        And a counter that begins again at the third hour's first sample
        with one change, then counts another: 2, where increase() gave 1."""
        changes = "thermite_route_seconds_count"
        confirmed = [130, 140, 150, 160, 170, 180, 190, 200, 205, 210, 215, 220, 225, 230, 235, 241]
        inputs = {
            f'{changes}{{zone="example_west",outcome="confirmed"}}': counter(confirmed, 400),
            f'{changes}{{zone="example_east",outcome="deadline"}}': counter([242], 243),
            f'{changes}{{zone="example_west",outcome="superseded"}}': counter([10, 130, 241, 250], 400,
                                                                             begins_again=241)}
        bars = query("switching", "Source changes by outcome")
        plain = f"round(sum by (outcome) (increase({changes}[1h] offset -1h))) > 0"
        self.shows(inputs, [
            ("0s", bars, [('{outcome="superseded"}', 1)]),
            ("1h", bars, [('{outcome="confirmed"}', 15), ('{outcome="superseded"}', 1)]),
            ("2h", bars, [('{outcome="confirmed"}', 1), ('{outcome="deadline"}', 1), ('{outcome="superseded"}', 2)]),
            ("1h", plain, [('{outcome="confirmed"}', 15), ('{outcome="superseded"}', 1)]),
            ("2h", plain, [('{outcome="deadline"}', 2), ('{outcome="superseded"}', 1)]),
        ], last="200m")

    def test_an_event_bar_counts_what_a_restarted_service_counted_before_and_after(self):
        # Two events, the counter begins again inside the hour, one more: increase() knows a reset.
        inputs = {'thermite_routing_resends_total{zone="example_west"}': counter([130, 140, 200], 400,
                                                                                begins_again=150)}
        self.shows(inputs, [("1h", query("switching", "Resends by room"), [('{zone="example_west"}', 3)])],
                   last="200m")

    def test_a_heatmap_cell_counts_the_change_between_two_cells(self):
        """Quarter hours of 30 samples. A change in the first, and one that
        sample 31, the second quarter's first, shows: a cell each."""
        buckets = "thermite_route_seconds_bucket"
        inputs = {f'{buckets}{{zone="example_west",outcome="confirmed",le="4"}}': counter([10, 31], 100),
                  f'{buckets}{{zone="example_west",outcome="confirmed",le="+Inf"}}': counter([10, 31], 100),
                  f'{buckets}{{zone="example_west",outcome="deadline",le="+Inf"}}': counter([12], 100)}
        cells = query("switching", "Time to switch", interval="15m")
        each = [('{le="4"}', 1), ('{le="+Inf"}', 1)]
        self.shows(inputs, [("15m", cells, each), ("30m", cells, each),
                            ("45m", cells, [('{le="4"}', 0), ('{le="+Inf"}', 0)]),
                            ("30m", f'sum by (le) (increase({buckets}{{outcome="confirmed"}}[15m]))',
                             [('{le="4"}', 0), ('{le="+Inf"}', 0)])], last="49m")

    def test_offline_since_is_the_thing_offline_longest_not_the_one_last_online(self):
        """A TV with two Things: one offline for four hours, one that went
        offline a minute ago. The table's review saw "2 minutes" for such a
        device: the newest of its Things. A player whose Thing was never
        online in the week reads 0, which the column words; a display that
        is online has no time."""
        online = "thermite_thing_online"
        inputs = {f'{online}{{device="example_tv",room="example_west",thing="a:tv:1"}}': "1x60 0x240",
                  f'{online}{{device="example_tv",room="example_west",thing="b:tv:1"}}': "1x299 0",
                  f'{online}{{device="example_player",room="example_west",thing="c:player:1"}}': "0x300",
                  f'{online}{{device="example_display",room="example_east",thing="d:display:1"}}': "1x300"}
        self.shows(inputs, [("5h", query("devices", "Devices", 4),
                             [('{device="example_tv"}', 3600 * 1000), ('{device="example_player"}', 0)])],
                   interval="1m", last="5h")
        panel, = [panel for panel in test_metrics.panels(BOARDS["example devices.json"]) if panel["title"] == "Devices"]
        column, = [override for override in panel["fieldConfig"]["overrides"]
                   if override["matcher"]["options"] == "Value #E"]
        words = {prop["id"]: prop["value"] for prop in column["properties"]}
        self.assertEqual(words["mappings"][0]["options"]["0"]["text"], "Not in the last week")
        self.assertEqual(words["unit"], "dateTimeFromNow")

    def test_giving_up_is_counted_from_the_events_and_the_wait_is_their_mean(self):
        """Five give-ups on room policy, two of them with their waits
        (8.1 and 8.3 s), and three on locks with none: 5 and 8.2 s, 3 and no
        wait. The panel once read the 95th percentile off the buckets,
        which for any wait just over a bound is most of the way to the
        next: 15.6 s for 8 s."""
        policy, locks = '{client="web",kind="policy_timeout"}', '{client="web",kind="locks_timeout"}'
        inputs = {f"thermite_client_events_total{policy}": "0x60 5x59", f"thermite_client_events_total{locks}": "0x60 3x59",
                  f"thermite_client_wait_seconds_sum{policy}": "0x60 16.4x59",
                  f"thermite_client_wait_seconds_count{policy}": "0x60 2x59",
                  f"thermite_client_wait_seconds_sum{locks}": "0x120", f"thermite_client_wait_seconds_count{locks}": "0x120"}
        self.shows(inputs, [
            ("1h", query("switching", "Waited before giving up", 0),
             [('{kind="policy_timeout"}', 5), ('{kind="locks_timeout"}', 3)]),
            ("1h", query("switching", "Waited before giving up", 1), [('{kind="policy_timeout"}', 8.2)])],
            last="1h")

    def test_commands_by_device_counts_those_not_taken_and_the_mean_time(self):
        """An hour in which a TV took ten commands in 4 s and two more did
        not arrive after 16 s, a display dropped one itself, and a player
        took none: 12 commands, 2 not taken, 20 s / 12 each; 1 and 0."""
        def tally(device, room, outcome, count, seconds):
            labels = f'{{device="{device}",room="{room}",kind="tv",integration="example",outcome="{outcome}"}}'
            return {f"thermite_device_commands_total{labels}": f"0x60 {count}x59",
                    f"thermite_device_command_seconds_total{labels}": f"0x60 {seconds}x59"}
        inputs = {**tally("example_tv", "example_west", "applied", 10, 4),
                  **tally("example_tv", "example_west", "unsent", 2, 16),
                  **tally("example_display", "example_east", "dropped", 1, 0),
                  **tally("example_display", "example_east", "unsent", 0, 0),
                  **tally("example_player", "example_west", "applied", 0, 0)}
        tv, display = '{device="example_tv"}', '{device="example_display"}'
        self.shows(inputs, [
            ("1h", query("devices", "Commands by device", 0), [(tv, 12), (display, 1)]),
            ("1h", query("devices", "Commands by device", 1), [(tv, 2), (display, 0)]),
            ("1h", query("devices", "Commands by device", 2), [(tv, 20 / 12), (display, 0)]),
            ("0s", query("devices", "Commands by outcome"), [('{outcome="applied"}', 10), ('{outcome="dropped"}', 1),
                                                           ('{outcome="unsent"}', 2)])],
            last="1h")

    def test_the_wait_histogram_has_no_bound_on_the_remotes_timeout(self):
        """A wait just over a bound lands in the bucket that ends at the
        next one. The remotes give up at 8 s (web/remote/access.js)."""
        from oh_access import metrics
        timeouts = set(re.findall(r"config\.(?:policy|command|read)Timeout \|\| (\d+)",
                                  (AREA / "web/remote/access.js").read_text()))
        self.assertEqual(timeouts, {"8000"})
        self.assertNotIn(8, metrics.CLIENT_BUCKETS)
        below, above = max(b for b in metrics.CLIENT_BUCKETS if b < 8), min(b for b in metrics.CLIENT_BUCKETS if b > 8)
        self.assertLessEqual(above - below, 1)

    def test_textfile_ages_are_the_three_services_and_never_a_deploys(self):
        """The deploy's file is written once a deploy: its age of hours on
        the axis of three files written every 10 s laid those flat."""
        directory = test_alert_scenarios.TEXTFILES
        names = ["thermite-access", "thermite-access-worker", "thermite-access-callbacks", "thermite-deploy"]
        inputs = {f'node_textfile_mtime_seconds{{job="node",file="{directory}/{name}.prom"}}': "0x10" for name in names}
        self.shows(inputs, [
            ("5m", query("host", "Metrics textfiles age", index),
             [(f'{{job="node",file="{directory}/{name}.prom"}}', 300)]) for index, name in enumerate(names[:3])],
            interval="1m", last="10m")

    def test_the_things_timeline_has_rows_for_the_longest_away_only(self):
        """Fourteen Things initialise at a start of the runtime, the first
        for one minute, the fourteenth for fourteen, and one is offline all
        along. While all fifteen are away the timeline has THINGS_SHOWN
        rows: not the three that were away shortest."""
        state = 'openhab_thing_state{{job="openhab",thing="example:{:02d}"}}'
        inputs = {state.format(number): f"1x{number} 3x{30 - number}" for number in range(1, 15)}
        inputs[state.format(15)] = "4x30"
        rows = query("openhab", "Things not ONLINE longest", interval="1m", span="30m", end=1800)
        self.assertEqual(dashboards.THINGS_SHOWN, 12)
        self.shows(inputs, [
            ("1m", rows, [(state.format(number).replace("{{", "{").replace("}}", "}"), 1) for number in range(4, 15)]
             + [(state.format(15).replace("{{", "{").replace("}}", "}"), 4)]),
            ("1m", 'count(openhab_thing_state{job="openhab"} != 3)', [("{}", 15)])],
            interval="1m", last="30m")

    def test_the_workers_seconds_and_hours_are_on_charts_of_their_own(self):
        """A pass every few seconds and a check every few hours on one
        axis laid the passes flat on zero."""
        board = {panel["title"]: panel for panel in test_metrics.panels(BOARDS["example switching.json"])
                 if panel.get("title")}
        passes, checks = board["Worker passes"], board["Oldest locked-room check"]
        self.assertEqual(passes["fieldConfig"]["defaults"]["unit"], "s")
        self.assertNotIn("reconcile_age", json.dumps(passes["targets"]))
        self.assertEqual(checks["fieldConfig"]["defaults"]["unit"], "h")
        self.assertEqual([target["expr"] for target in checks["targets"]],
                         ["max(thermite_policy_reconcile_age_seconds) / 3600"])
        self.assertNotIn("Worker", board)


if __name__ == "__main__":
    unittest.main()
