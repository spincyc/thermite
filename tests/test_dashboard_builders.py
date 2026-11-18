"""The dashboards' panels come from builders that know their kind: a chart's
colours are named, an event bar counts exactly its own width, and a bar of
amounts has a stated full scale. These tests fail if a panel is made any
other way."""

import ast
import inspect
import json
from pathlib import Path
import re
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_metrics  # noqa: E402
from oh_metrics import dashboards  # noqa: E402
from oh_metrics.dashboards import Count, Series  # noqa: E402
from tests import test_metrics  # noqa: E402

#: The panel kinds the builders make; a row is the Board's own.
KINDS = {"text", "stat", "table", "state-timeline", "timeseries", "piechart", "bargauge", "heatmap"}
#: Kinds whose marks take a colour from the field's colour mode.
DRAWN = KINDS - {"text", "table", "heatmap"}
#: The query events() writes for a Count, and nothing else.
EVENT_BAR = re.compile(r"round\(sum( by \([a-z_]+(, [a-z_]+)*\))? \(increase\([a-zA-Z_:][a-zA-Z0-9_:]*_(total|count)"
                       r"(\{[^{}()\[\]]*\})?\[\$__interval\] offset -\$__interval\)\)\) > 0")


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
                self.assertRegex(query["expr"], EVENT_BAR, where)
                self.assertTrue(EVENT_BAR.fullmatch(query["expr"]), (where, query["expr"]))
                self.assertEqual(query["interval"], dashboards.EVENT_BUCKET, where)
        # Seven on Switching & remotes and btrfs errors on Host, in each of four houses.
        self.assertEqual(bars, 4 * 8)
        for _, panel in drawn():
            for config in configs(panel):
                self.assertNotIn("custom.drawStyle", config, panel["title"])

    def test_events_take_a_counters_name_not_a_query(self):
        self.assertEqual(Count("thermite_route_seconds_count", "{{outcome}}", dashboards.OK, by="outcome").expr(),
                         "round(sum by (outcome) (increase(thermite_route_seconds_count[$__interval] "
                         "offset -$__interval))) > 0")
        self.assertEqual(Count("thermite_callbacks_total", "Refused", dashboards.WARN,
                               where='outcome!="forwarded"').expr(),
                         'round(sum (increase(thermite_callbacks_total{outcome!="forwarded"}[$__interval] '
                         "offset -$__interval))) > 0")
        for count in (Count("increase(thermite_callbacks_total[1h])", "Calls", dashboards.OK),
                      Count("thermite_callbacks_total[1h]", "Calls", dashboards.OK),
                      Count("thermite_item_value", "A gauge", dashboards.OK),
                      Count("thermite_callbacks_total", "Calls", dashboards.OK,
                            where='outcome="x"}[1h]) or up{job="a"'),
                      Count("thermite_callbacks_total", "Calls", dashboards.OK, by="outcome) (rate(x[5m]")):
            with self.assertRaises(ValueError):
                count.expr()

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


if __name__ == "__main__":
    unittest.main()
