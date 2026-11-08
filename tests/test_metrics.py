"""Offline checks of the metrics stack: the exporter's routing series, the
per-check health rules and alerts, the dashboards' JSON and colours, the
notification adapter's exposition, the access services' textfiles, and
what a deploy adds for the gateway check and deploy annotations."""

import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy  # noqa: E402
from oh_features import routing  # noqa: E402
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_render  # noqa: E402
import oh_units  # noqa: E402
from oh_metrics import alerts, dashboards, exporter, notifications  # noqa: E402

INVENTORY = oh_inventory.load(AREA / "instance/devices.yaml")
HOST = yaml.safe_load((AREA / "instance/host.yaml").read_text())


def rendered(inventory=None):
    return oh_metrics.render(copy.deepcopy(inventory or INVENTORY), HOST)


FILES = rendered()
MAP = json.loads(FILES[oh_metrics.EXPORTER_MAP])
RULES = yaml.safe_load(FILES["prometheus/alerts.yml"])


def boards():
    return {name.rsplit("/", 1)[1].removesuffix(".json"): json.loads(text)
            for name, text in FILES.items() if name.startswith(oh_metrics.GRAFANA_DASHBOARDS)}


def panels(board):
    for panel in board["panels"]:
        yield panel
        yield from panel.get("panels", [])


ROUTING_MAP = {"devices": {"example_matrix": {"room": "example_west", "kind": "hdmi_matrix"},
                           "example_tv": {"room": "example_west", "kind": "tv"}},
               "routing": {"status": "house_routing_io_status", "metrics": "house_routing_metrics",
                           "zones": {"example_west": {"room": "example_west", "label": "Example West"}},
                           "executors": ["example_matrix"]}}


def items(**states):
    return [{"name": name, "state": state} for name, state in states.items()]


class Selections(unittest.TestCase):
    MAPPING = {"items": {"loc_example_west_source": {"room": "example_west", "point": "source", "convert": "enum",
                                                     "options": ["off", "example_player"]}}}

    def test_an_unreadable_selection_is_reported_unavailable(self):
        labels = {"device": "", "room": "example_west", "kind": "", "item": "loc_example_west_source",
                  "point": "source"}
        for state in ("UNDEF", "NULL"):
            samples = exporter._items(self.MAPPING, items(loc_example_west_source=state))
            self.assertNotIn("thermite_item_info", [name for name, _, _ in samples])
            self.assertIn(("thermite_item_unavailable", labels, 1.0), samples)
        shown = exporter._items(self.MAPPING, items(loc_example_west_source="off"))
        self.assertIn(("thermite_item_info", {**labels, "value": "off"}, 1.0), shown)
        self.assertNotIn("thermite_item_unavailable", [name for name, _, _ in shown])
        self.assertIn("thermite_item_unavailable", exporter.FAMILIES)

    def test_every_kept_item_without_a_value_is_unavailable(self):
        """NULL and UNDEF read the same for every conversion, not only for
        selections; a problem Item's declared clear state stays 0."""
        mapping = {"items": {
            "example_tv_power": {"device": "example_tv", "room": "example_west", "kind": "tv", "point": "power",
                                 "convert": "switch"},
            "example_tv_volume": {"device": "example_tv", "room": "example_west", "kind": "tv", "point": "volume",
                                  "convert": "number"},
            "example_tv_problem": {"device": "example_tv", "room": "example_west", "kind": "tv", "point": "problem",
                                   "convert": "problem"},
            "example_display_problem": {"device": "example_display", "room": "example_east", "kind": "tv",
                                        "point": "problem", "convert": "problem", "clear_state": "UNDEF"}}}
        samples = exporter._items(mapping, items(example_tv_power="UNDEF", example_tv_volume="NULL",
                                                 example_tv_problem="UNDEF", example_display_problem="UNDEF"))
        unavailable = {labels["item"]: labels["point"] for name, labels, _ in samples
                       if name == "thermite_item_unavailable"}
        self.assertEqual(unavailable, {"example_tv_power": "power", "example_tv_volume": "volume",
                                       "example_tv_problem": "problem"})
        self.assertIn(("thermite_device_problem", {"device": "example_display", "room": "example_east", "kind": "tv"},
                       0.0), samples)
        self.assertNotIn("thermite_item_value", [name for name, _, _ in samples])
        # An unparsable value is no value, not an unavailable reading.
        samples = exporter._items(mapping, items(example_tv_power="ON", example_tv_volume="loud",
                                                 example_tv_problem="", example_display_problem="UNDEF"))
        self.assertNotIn("thermite_item_unavailable", [name for name, _, _ in samples])

    def test_a_room_that_cannot_be_read_keeps_its_row(self):
        timeline = next(panel for board in boards().values() for panel in panels(board)
                        if panel.get("title") == "Rooms through the day")
        self.assertTrue(timeline["targets"])
        for target in timeline["targets"]:
            item = re.search(r'item="([^"]+)"', target["expr"]).group(1)
            self.assertIn(f'thermite_item_unavailable{{item="{item}"}}', target["expr"])
        texts = {option["text"] for mapping in timeline["fieldConfig"]["defaults"]["mappings"]
                 for option in mapping["options"].values()}
        self.assertIn("Unavailable", texts)

    def test_sources_are_named_as_the_remotes_name_them(self):
        timeline = next(panel for board in boards().values() for panel in panels(board)
                        if panel.get("title") == "Rooms through the day")
        texts = {option["text"] for mapping in timeline["fieldConfig"]["defaults"]["mappings"]
                 for option in mapping["options"].values()}
        self.assertEqual(MAP["sources"], {"example_computer": "Computer", "example_player": "Player"})
        self.assertLessEqual({"Player", "Computer", "Off"}, texts)
        self.assertNotIn("Example Player", texts)

    def test_a_shared_source_label_adds_the_room_then_the_device(self):
        mapping = {**MAP, "sources": {**MAP["sources"], "example_computer": "Player"}}
        self.assertEqual({ident: dashboards._source_words(mapping)[ident] for ident in mapping["sources"]},
                         {"example_computer": "Player (Example East)", "example_player": "Player (Example West)"})
        devices = {**MAP["devices"], "example_computer": {**MAP["devices"]["example_computer"],
                                                          "room": "example_west"}}
        words = dashboards._source_words({**mapping, "devices": devices})
        self.assertEqual((words["example_computer"], words["example_player"]),
                         ("Player (Example Computer)", "Player (Example Player)"))


class RoutingSeries(unittest.TestCase):
    def series(self, **states):
        """The samples of a house with only the routing Items given."""
        keys = {"house_routing_io_status": "status", "house_routing_metrics": "metrics"}
        routing = {"zones": ROUTING_MAP["routing"]["zones"], "executors": ROUTING_MAP["routing"]["executors"],
                   **{keys[name]: name for name in states}}
        return exporter._routing({**ROUTING_MAP, "routing": routing}, items(**states))

    def test_status_counts_and_only_the_houses_quarantined_devices(self):
        status = {"pending": 2, "active": 1, "quarantined": 2, "eventsQueued": 0,
                  "quarantinedKeys": ["example_matrix", "not_a_device"]}
        samples = self.series(house_routing_io_status=json.dumps(status))
        found = {(name, tuple(sorted(labels.items()))): value for name, labels, value in samples}
        self.assertEqual(found[("thermite_routing_executor_up", ())], 1)
        self.assertEqual(found[("thermite_routing_pending", ())], 2)
        self.assertEqual(found[("thermite_routing_quarantined", ())], 2)
        quarantined = [labels for name, labels, _ in samples if name == "thermite_routing_device_quarantined"]
        self.assertEqual(quarantined, [{"device": "example_matrix", "room": "example_west", "kind": "hdmi_matrix"}])

    def test_invalid_status_is_down_without_counts(self):
        for state in ("UNDEF", "[]", json.dumps({"pending": -1, "active": 0, "quarantined": 0, "eventsQueued": 0}),
                      json.dumps({"pending": 0, "active": 0, "quarantined": 0, "eventsQueued": 0, "token": "x"}),
                      json.dumps({"pending": True, "active": 0, "quarantined": 0, "eventsQueued": 0}),
                      "x" * (exporter.ROUTING_BYTES + 1)):
            with self.subTest(state=state[:40]):
                self.assertEqual(self.series(house_routing_io_status=state),
                                 [("thermite_routing_executor_up", {}, 0.0)])

    def test_route_histograms_and_counters_keep_only_known_names(self):
        data = {"version": 1,
                "route": {"example_west": {"confirmed": {"buckets": [1, 3, 4, 4, 4, 4, 4], "count": 4, "sum": 6.5},
                                           "deadline": {"buckets": [0, 0, 0, 0, 1, 1, 1], "count": 1, "sum": 15},
                                           "bogus": {"buckets": [1] * 7, "count": 1, "sum": 1}},
                          "unknown_zone": {"confirmed": {"buckets": [1] * 7, "count": 1, "sum": 1}}},
                "operations": {"applied": 5, "free text": 9}, "failures": {"example_matrix": {"unsent": 2, "x": 1},
                                                                            "stranger": {"unsent": 1}},
                "resends": {"example_west": 3}}
        text = exporter.exposition(self.series(house_routing_metrics=json.dumps(data)))
        self.assertIn("# TYPE thermite_route_seconds histogram", text)
        self.assertIn('thermite_route_seconds_bucket{room="example_west",zone="example_west",outcome="confirmed",'
                      'le="2"} 3', text)
        self.assertIn('thermite_route_seconds_bucket{room="example_west",zone="example_west",outcome="deadline",'
                      'le="+Inf"} 1', text)
        self.assertIn('thermite_route_seconds_sum{room="example_west",zone="example_west",outcome="confirmed"} 6.5',
                      text)
        self.assertIn("# TYPE thermite_routing_operations_total counter", text)
        self.assertIn('thermite_routing_operations_total{outcome="applied"} 5', text)
        self.assertIn('thermite_routing_operations_total{outcome="unsent"} 0', text)
        self.assertIn('thermite_routing_endpoint_failures_total{device="example_matrix",room="example_west",'
                      'kind="hdmi_matrix",reason="unsent"} 2', text)
        self.assertIn('thermite_routing_resends_total{room="example_west",zone="example_west"} 3', text)
        for absent in ("bogus", "unknown_zone", "free text", "stranger", 'reason="x"'):
            self.assertNotIn(absent, text)
        self.assertEqual(text.count("# TYPE thermite_route_seconds "), 1)

    def test_inconsistent_histograms_are_dropped(self):
        for histogram in ({"buckets": [2, 1, 1, 1, 1, 1, 1], "count": 2, "sum": 1},
                          {"buckets": [1, 1, 1, 1, 1, 1, 3], "count": 2, "sum": 1},
                          {"buckets": [1, 1], "count": 1, "sum": 1},
                          {"buckets": [1] * 7, "count": 1, "sum": -1}):
            with self.subTest(histogram=histogram):
                data = {"version": 1, "route": {"example_west": {"confirmed": histogram}}}
                samples = self.series(house_routing_metrics=json.dumps(data))
                outcomes = {labels["outcome"] for name, labels, _ in samples
                            if name.startswith("thermite_route_seconds")}
                self.assertEqual(outcomes, {"deadline", "superseded"})

    def test_every_zone_and_executor_device_starts_at_zero(self):
        """routing.js posts its counts at load, before any choice: every
        series is there at zero, so increase() counts each one's first event."""
        data = {"version": 1, "route": {}, "operations": {}, "failures": {}, "resends": {}}
        samples = self.series(house_routing_metrics=json.dumps(data))
        counts = {(labels["zone"], labels["outcome"]): value for name, labels, value in samples
                  if name == "thermite_route_seconds_count"}
        self.assertEqual(counts, {("example_west", outcome): 0 for outcome in exporter.ROUTE_OUTCOMES})
        buckets = [labels["le"] for name, labels, value in samples
                   if name == "thermite_route_seconds_bucket" and labels["outcome"] == "confirmed"]
        self.assertEqual(len(buckets), len(exporter.ROUTE_BUCKETS) + 1)
        failures = {(labels["device"], labels["reason"]): value for name, labels, value in samples
                    if name == "thermite_routing_endpoint_failures_total"}
        self.assertEqual(failures, {("example_matrix", reason): 0 for reason in exporter.FAILURE_REASONS})

    def test_the_map_names_the_devices_the_executor_commands(self):
        from tests import test_zone_sync as zones
        mapping = json.loads(rendered(zones.house())[oh_metrics.EXPORTER_MAP])
        self.assertEqual(mapping["routing"]["executors"],
                         ["example_avr", "example_matrix", "example_screen", "example_tv", "example_usb"])
        self.assertLessEqual(set(mapping["routing"]["executors"]), set(mapping["devices"]))
        self.assertEqual(MAP["routing"]["executors"], [], "the example house has no routing executor")

    def test_wrong_version_is_down(self):
        self.assertEqual(self.series(house_routing_metrics=json.dumps({"version": 2})),
                         [("thermite_routing_metrics_up", {}, 0.0)])

    def test_map_names_the_routing_items_and_zones(self):
        self.assertEqual(MAP["routing"]["metrics"], "house_routing_metrics")
        self.assertEqual(set(MAP["routing"]["zones"]), {"example_east", "example_west"})

    def test_the_deadline_is_the_houses_and_the_boards_follow_it(self):
        """routing.js's deadline is the house's (routing.hold_ms): 20 s
        with a receiver read every 15 s, not the dashboards' 15 s."""
        from oh_features import routing
        from tests import test_zone_sync as zones
        self.assertEqual(MAP["routing"]["deadline_seconds"], routing.hold_ms(INVENTORY) / 1000)
        files = rendered(zones.house())
        mapping = json.loads(files[oh_metrics.EXPORTER_MAP])
        self.assertEqual(mapping["routing"]["deadline_seconds"], 20)
        found = {name.rsplit("/", 1)[1].removesuffix(".json"): json.loads(text)
                 for name, text in files.items() if name.startswith(oh_metrics.GRAFANA_DASHBOARDS)}
        text = json.dumps(found)
        self.assertNotIn("15 s", text)
        self.assertIn("not confirmed within 20 s", text)
        self.assertIn("it did not within 20 s", text)
        switch = next(panel for panel in panels(found[dashboards.HOUSE]) if panel["title"] == "Switch time by room")
        steps = [prop["value"]["steps"] for override in switch["fieldConfig"]["overrides"]
                 for prop in override["properties"] if prop["id"] == "thresholds"]
        self.assertTrue(steps)
        for each in steps:
            self.assertEqual([(step["color"], step["value"]) for step in each],
                             [(dashboards.OK, None), (dashboards.WARN, 10), (dashboards.FAIL, 20)])

    def test_route_buckets_are_routing_js_and_one_is_above_the_deadline(self):
        from oh_features import routing
        from tests import test_zone_sync as zones
        source = routing.SOURCE.read_text(encoding="utf-8")
        counted = re.search(r"const BUCKETS = \[([^\]]*)\]", source).group(1)
        self.assertEqual(tuple(int(value) for value in counted.split(",")), exporter.ROUTE_BUCKETS)
        for inventory in (INVENTORY, zones.house()):
            self.assertGreater(exporter.ROUTE_BUCKETS[-1], routing.hold_ms(inventory) / 1000)


class Checks(unittest.TestCase):
    def test_rules_are_deterministic_and_record_every_check(self):
        self.assertEqual(FILES, rendered())
        recorded = [rule for rule in RULES["groups"][0]["rules"] if rule.get("record") == alerts.CHECK]
        keys = [tuple(sorted(rule["labels"].items())) for rule in recorded]
        self.assertEqual(len(keys), len(set(keys)), "two rules would write the same series")
        for rule in recorded:
            self.assertLessEqual(set(rule["labels"]), {"room", "zone", "device", "check", "required"})
            self.assertIn(rule["labels"]["check"], alerts.CHECK_WORDS)
        self.assertEqual(RULES["groups"][0]["rules"][-1]["record"], alerts.HEALTH)

    def test_active_path_checks_split_by_demand_and_infrastructure_is_always_required(self):
        recorded = [rule for rule in RULES["groups"][0]["rules"] if rule.get("record") == alerts.CHECK]
        matrix = [rule for rule in recorded if rule["labels"].get("device") == "example_matrix"]
        self.assertTrue(matrix)
        self.assertTrue(all(rule["labels"]["required"] == "true" and "zone" not in rule["labels"] for rule in matrix))
        tv = [rule for rule in recorded if rule["labels"].get("device") == "example_tv"]
        self.assertEqual({rule["labels"]["required"] for rule in tv}, {"true", "false"})
        for rule in tv:
            self.assertIn("and on()" if rule["labels"]["required"] == "true" else "unless on()", rule["expr"])
            self.assertIn("vector(-1)", rule["expr"], "missing telemetry must read unknown")
        sources = [rule for rule in recorded if rule["labels"]["check"] == "source"]
        self.assertEqual({rule["labels"]["zone"] for rule in sources}, set(MAP["routing"]["zones"]))

    def test_optional_devices_are_never_required(self):
        inventory = copy.deepcopy(INVENTORY)
        for device in inventory["devices"]:
            if device["id"] in ("example_tv", "example_matrix"):
                device["monitor"] = "optional"
        self.assertEqual(oh_inventory.validate(inventory), [])
        mapping = json.loads(rendered(inventory)[oh_metrics.EXPORTER_MAP])
        recorded, required = alerts.checks(mapping)
        _, before = alerts.checks(MAP)
        for rule in recorded:
            if rule["labels"].get("device") in ("example_tv", "example_matrix"):
                self.assertEqual(rule["labels"]["required"], "false")
        self.assertLess(required, before)
        bad = copy.deepcopy(INVENTORY)
        bad["devices"][0]["monitor"] = "sometimes"
        self.assertTrue(any("monitor" in problem for problem in oh_inventory.validate(bad)))

    def test_a_rooms_audio_device_is_in_use_with_every_source(self):
        inventory = copy.deepcopy(INVENTORY)
        inventory["devices"].append({"id": "example_amp", "room": "example_east", "kind": "streaming_amp",
                                     "make": "WiiM", "model": "Amp Pro", "ip": "192.0.2.41",
                                     "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}})
        inventory["routing"]["rooms"]["example_east"]["audio"] = "example_amp"
        self.assertEqual(oh_inventory.validate(inventory), [])
        mapping = json.loads(rendered(inventory)[oh_metrics.EXPORTER_MAP])
        east = mapping["paths"]["loc_example_east_source"]["sources"]
        self.assertTrue(east)
        self.assertTrue(all("example_amp" in used for used in east.values()))
        self.assertNotIn("example_amp", json.dumps(MAP["paths"]))
        recorded, _ = alerts.checks(mapping)
        amp = [rule for rule in recorded if rule["labels"].get("device") == "example_amp"]
        self.assertTrue(amp)
        self.assertEqual({rule["labels"]["required"] for rule in amp}, {"true", "false"})

    def test_a_shared_receivers_power_is_checked_per_zone(self):
        from tests import test_zone_sync as zones
        mapping = json.loads(rendered(zones.house())[oh_metrics.EXPORTER_MAP])
        recorded, _ = alerts.checks(mapping)
        power = {rule["labels"]["zone"]: rule["expr"] for rule in recorded
                 if rule["labels"].get("device") == "example_avr" and rule["labels"]["check"] == "power"
                 and rule["labels"]["required"] == "true"}
        cinema, lounge = zones.CINEMA, zones.LOUNGE
        self.assertEqual(set(power), {cinema, lounge})
        # Each room needs only the zone it listens to: the Lounge is not
        # failing while the Cinema's main zone is off, nor the reverse.
        self.assertIn('"example_avr_main_power"', power[cinema])
        self.assertNotIn("zone2", power[cinema])
        self.assertIn('"example_avr_zone2_power"', power[lounge])
        self.assertNotIn("main_power", power[lounge])

    def test_checks_join_the_routing_telemetry_and_the_info_families(self):
        """One zone id in every family, and info families a check joins
        with on(zone), on(room) or on(device) alone, losing none."""
        from tests import test_zone_sync as zones
        files = rendered(zones.house())
        mapping = json.loads(files[oh_metrics.EXPORTER_MAP])
        checks = [rule["labels"] for rule in yaml.safe_load(files["prometheus/alerts.yml"])["groups"][0]["rules"]
                  if rule.get("record") == alerts.CHECK]
        data = {"version": 1, "route": {}, "operations": {}, "failures": {}, "resends": {}}
        answer = [{"name": mapping["routing"]["metrics"], "state": json.dumps(data)}]
        samples = exporter.collect(mapping, lambda path, timeout: answer if "items" in path else [])
        family = {}
        for name, labels, _ in samples:
            family.setdefault(name, []).append(labels)
        zone_info = {labels["zone"]: labels for labels in family["thermite_zone_info"]}
        self.assertEqual(zone_info[zones.LOUNGE], {"zone": zones.LOUNGE, "room": zones.LOUNGE,
                                                   "label": "Example Lounge"})
        routed = {labels["zone"] for labels in family["thermite_route_seconds_count"]}
        self.assertEqual(routed, set(zone_info))
        self.assertEqual({labels["zone"] for labels in family["thermite_routing_resends_total"]}, set(zone_info))
        zoned = {labels["zone"] for labels in checks if "zone" in labels}
        self.assertEqual(zoned, set(zone_info))
        room_info = {labels["room"]: labels["label"] for labels in family["thermite_room_info"]}
        self.assertEqual(room_info, {"house": "House", zones.CINEMA: "Example Cinema", zones.LOUNGE: "Example Lounge"})
        self.assertLessEqual({labels["room"] for labels in checks}, set(room_info))
        # The Lounge's checks of the receiver are the Lounge's, though it is
        # the Cinema's device: device facts join on(device) alone.
        own = {labels["device"]: labels["room"] for labels in family["thermite_device_info"]}
        lounge = [labels for labels in checks if labels.get("device") == "example_avr" and labels["room"] == zones.LOUNGE]
        self.assertTrue(lounge)
        self.assertEqual(own["example_avr"], zones.CINEMA)
        for labels in checks:
            if labels.get("device") in mapping["devices"]:
                self.assertEqual(sum(each["device"] == labels["device"] for each in family["thermite_device_info"]), 1)

    def test_check_values_fail_first_then_need_every_series(self):
        one = alerts._check_value(['thermite_thing_online{thing="a"}'], 0)
        self.assertEqual(one, 'max(thermite_thing_online{thing="a"} == bool 0) or vector(-1)')
        two = alerts._check_value(['probe_success{port="1"}', 'probe_success{port="2"}'], 0)
        self.assertIn("== 1) or (", two)
        self.assertIn("count((probe_success{port=\"1\"} or probe_success{port=\"2\"})) == 2", two)

    def test_control_health_keeps_todays_three_values(self):
        _, required = alerts.checks(MAP)
        expr = alerts.control_health(required)
        self.assertTrue(expr.startswith('clamp_max(count(thermite:check{required="true"} == 1), 1)'))
        self.assertIn(f">= {required})", expr)
        self.assertTrue(expr.endswith("or vector(-1)"))


class Alerting(unittest.TestCase):
    def test_every_alert_is_catalogued_with_a_severity(self):
        names = [rule["alert"] for group in RULES["groups"] for rule in group["rules"] if "alert" in rule]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(set(names), set(alerts.CATALOG))
        self.assertNotIn("ThermiteControlDegraded", names)
        for group in RULES["groups"]:
            for rule in group["rules"]:
                if "alert" in rule:
                    self.assertIn(rule["labels"]["severity"], ("critical", "warning", "heartbeat"))
                    self.assertEqual(rule["annotations"]["summary"], alerts.CATALOG[rule["alert"]][0])
        for title, detail in alerts.CATALOG.values():
            self.assertLessEqual(len(detail), 500)
            self.assertFalse(any(ord(c) < 32 for c in title + detail))
        check = next(rule for rule in RULES["groups"][1]["rules"] if rule.get("alert") == "ThermiteCheckFailing")
        self.assertEqual((check["expr"], check["for"]), ('thermite:check{required="true"} == 1', "5m"))

    def test_routing_groups_by_room_and_inhibits_repeats(self):
        config = yaml.safe_load(FILES["alertmanager/alertmanager.yml"])
        route = config["route"]
        self.assertEqual(route["group_by"], ["alertname", "room"])
        self.assertEqual(route["routes"][0]["receiver"], "heartbeat")
        warning = route["routes"][1]
        self.assertEqual((warning["matchers"], warning["receiver"], warning["repeat_interval"]),
                         (['severity="warning"'], "incidents", "12h"))
        self.assertEqual([r["webhook_configs"][0]["url"].rsplit("/", 1)[1] for r in config["receivers"]],
                         ["alerts", "heartbeat"])
        for source, targets in alerts.INHIBITS:
            self.assertIn(source, alerts.CATALOG)
            self.assertLessEqual(set(targets), set(alerts.CATALOG))
        self.assertEqual(len(config["inhibit_rules"]), len(alerts.INHIBITS))

    def test_a_gateway_cut_off_from_the_runtimes_events_alerts_and_shows_on_the_remotes_tile(self):
        rule = next(rule for rule in RULES["groups"][1]["rules"] if rule.get("alert") == "ThermiteGatewayFeedDown")
        self.assertEqual(rule["expr"], '(thermite_gateway_upstream_state{state="connecting"} == 1) and on() '
                                       f'({alerts.GATEWAY_INSTALLED})')
        self.assertEqual((rule["for"], rule["labels"]["severity"]), ("5m", "critical"))
        silenced = {source for source, targets in alerts.INHIBITS if "ThermiteGatewayFeedDown" in targets}
        self.assertEqual(silenced, {"ThermiteGatewayDown", "ThermiteRuntimeUnavailable"})
        tile = next(panel for panel in panels(boards()[dashboards.HOUSE]) if panel["title"] == "Remotes")
        self.assertIn('thermite_gateway_upstream_state{state="connecting"}[5m]', tile["targets"][0]["expr"])
        words = {key: option["text"] for key, option in tile["fieldConfig"]["defaults"]["mappings"][0]["options"].items()}
        self.assertEqual(words, {"0": "OK", "1": "Timing out", "2": "Not live", "3": "Gateway down"})

    def test_unreadable_routing_telemetry_alerts(self):
        rule = next(rule for rule in RULES["groups"][1]["rules"]
                    if rule.get("alert") == "ThermiteRoutingTelemetryUnavailable")
        self.assertEqual(rule["expr"], "(thermite_routing_executor_up == 0) or (thermite_routing_metrics_up == 0)")
        self.assertEqual((rule["for"], rule["labels"]["severity"]), ("5m", "warning"))

    def test_a_lock_that_cannot_be_held_alerts_unless_the_worker_or_runtime_explains_it(self):
        rule = next(rule for rule in RULES["groups"][1]["rules"] if rule.get("alert") == "ThermitePolicyUnenforced")
        self.assertEqual(rule["expr"], 'thermite_policy_room_state{state="unavailable"} == 1')
        self.assertEqual((rule["for"], rule["labels"]["severity"]), ("10m", "critical"))
        silenced = {source for source, targets in alerts.INHIBITS if "ThermitePolicyUnenforced" in targets}
        self.assertEqual(silenced, {"ThermitePolicyWorkerStalled", "ThermiteRuntimeUnavailable"})

    def test_service_down_watches_every_daemon_with_escaped_names(self):
        rule = next(rule for rule in RULES["groups"][1]["rules"] if rule.get("alert") == "ThermiteServiceDown")
        for unit in oh_units.ACCESS_UNITS:
            self.assertIn(re.escape(unit).replace("\\", "\\\\"), rule["expr"])

    def test_gateway_is_probed_over_https_with_its_trust(self):
        prometheus = yaml.safe_load(FILES[oh_metrics.PROMETHEUS_YML])
        job = next(job for job in prometheus["scrape_configs"] if job["job_name"] == alerts.GATEWAY_JOB)
        target = job["static_configs"][0]
        self.assertEqual(target["targets"], [oh_metrics.gateway_probe_url(HOST)])
        self.assertTrue(target["targets"][0].startswith("https://127.0.0.1:"))
        self.assertEqual(target["labels"]["__param_module"], oh_metrics.HTTPS_GATEWAY)
        module = yaml.safe_load(FILES[oh_metrics.BLACKBOX_YML])["modules"][oh_metrics.HTTPS_GATEWAY]["http"]
        self.assertTrue(module["fail_if_not_ssl"])
        self.assertEqual(module["tls_config"]["ca_file"], "/etc/blackbox/gateway-ca.pem")
        self.assertFalse(module["tls_config"]["insecure_skip_verify"])
        for name in ("prometheus", "alertmanager"):
            job = next(job for job in prometheus["scrape_configs"] if job["job_name"] == name)
            self.assertEqual(job["metric_relabel_configs"][0]["action"], "drop")

    def test_the_gateway_is_judged_only_where_it_is_installed(self):
        """Every house probes the gateway; one without it must not read
        Gateway down: the alert, the Remotes tile and Service probes all ask
        whether its unit exists."""
        alert = next(rule for rule in RULES["groups"][1]["rules"] if rule.get("alert") == "ThermiteGatewayDown")
        self.assertIn(alerts.GATEWAY_INSTALLED, alert["expr"])
        probed = [(name, panel["title"], query["expr"]) for name, board in boards().items()
                  for panel in panels(board) for query in panel.get("targets", [])
                  if alerts.GATEWAY_JOB in query["expr"]]
        self.assertEqual({title for _, title, _ in probed}, {"Remotes", "Service probes"})
        for name, title, expr in probed:
            self.assertIn(f"and on() ({alerts.GATEWAY_INSTALLED})", expr, (name, title))

    def test_session_and_tls_ports_are_not_probed(self):
        """A TCP probe would cost an AVPro a session and a listing, and an
        Android TV remote, Cast or WiiM port a TLS handshake."""
        inventory = copy.deepcopy(INVENTORY)
        inventory["devices"] += [
            {"id": "example_avpro", "room": "example_east", "kind": "hdmi_matrix", "make": "AVPro Edge",
             "model": "AC-MX-44X", "ip": "192.0.2.30",
             "controls": {"observe": ["network_ping"], "control": ["avpro_telnet"]}},
            {"id": "example_shield", "room": "example_east", "kind": "streamer", "make": "NVIDIA",
             "model": "SHIELD TV Pro", "ip": "192.0.2.31",
             "controls": {"observe": ["cast"], "control": ["androidtv_googletv"]}},
            {"id": "example_amp", "room": "example_east", "kind": "streaming_amp", "make": "WiiM",
             "model": "Amp Pro", "ip": "192.0.2.32",
             "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}]
        found, unknown = oh_metrics.probes(inventory)
        self.assertFalse({"example_avpro", "example_shield", "example_amp"} & {probe.device for probe in found})
        self.assertIn(("example_avpro", ("network_ping", "avpro_telnet")), unknown)
        self.assertIn(("example_shield", ("cast", "androidtv_googletv")), unknown)
        self.assertIn(("example_amp", ("wiim_http",)), unknown)

    def test_device_ports_are_probed_every_two_minutes(self):
        prometheus = yaml.safe_load(FILES[oh_metrics.PROMETHEUS_YML])
        intervals = {job["job_name"]: job.get("scrape_interval") for job in prometheus["scrape_configs"]}
        self.assertEqual(intervals.pop("device_tcp"), "2m")
        self.assertEqual(set(intervals.values()), {None})
        self.assertEqual(prometheus["global"]["scrape_interval"], "30s")

    def test_a_sony_projector_is_pinged_not_probed(self):
        """A TCP probe of its ADCP port would cost it a session, reset with its greeting unread."""
        inventory = copy.deepcopy(INVENTORY)
        inventory["devices"].append({"id": "example_projector", "room": "example_east", "kind": "projector",
                                     "make": "Sony", "model": "VPL-VW915ES", "ip": "192.0.2.31",
                                     "controls": {"observe": ["network_ping", "sony_adcp"], "control": ["sony_adcp"]}})
        found, unknown = oh_metrics.probes(inventory)
        self.assertNotIn("example_projector", [probe.device for probe in found])
        self.assertIn(("example_projector", ("network_ping", "sony_adcp")), unknown)

    def test_node_exporter_reads_the_textfiles(self):
        node = next(service for service in oh_metrics.services(HOST) if service.unit == oh_units.NODE_EXPORTER)
        self.assertIn(f"--collector.textfile.directory=/host{oh_deploy.state(HOST).textfile}", node.args)


class Dashboards(unittest.TestCase):
    def test_boards_have_fixed_uids_and_valid_layouts(self):
        found = boards()
        self.assertEqual(set(found), {dashboards.HOUSE, dashboards.SWITCHING, dashboards.DEVICES,
                                      dashboards.OPENHAB, dashboards.HOST, dashboards.USAGE})
        self.assertEqual(found[dashboards.HOUSE]["uid"], "thermite-house-health")
        self.assertEqual(found[dashboards.HOUSE]["title"], "House health")
        self.assertEqual(found[dashboards.OPENHAB]["title"], "openHAB runtime")
        settings = dict(dict(oh_metrics.grafana_settings(HOST))["dashboards"])
        self.assertTrue(settings["default_home_dashboard_path"].endswith("/house.json"))
        for name, board in found.items():
            ids = [panel["id"] for panel in panels(board)]
            self.assertEqual(len(ids), len(set(ids)), name)
            for panel in panels(board):
                grid = panel["gridPos"]
                self.assertLessEqual(grid["x"] + grid["w"], 24, (name, panel["title"]))
                for query in panel.get("targets", []):
                    self.assertEqual(query["datasource"]["uid"], dashboards.DATASOURCE_UID)
                    self.assertEqual(query["expr"].count("("), query["expr"].count(")"), panel["title"])

    def test_house_health_reads_the_recorded_health(self):
        house = boards()[dashboards.HOUSE]
        titles = [panel["title"] for panel in panels(house)]
        self.assertEqual(titles[:6], ["Control", "Rooms ready", "Remotes", "Alerts firing", "Phone delivery", "Host"])
        control = next(panel for panel in panels(house) if panel["title"] == "Control")
        self.assertEqual(control["targets"][0]["expr"], alerts.HEALTH)
        attention = next(panel for panel in panels(house) if panel["title"] == "Needs attention now")
        words = {option["text"] for override in attention["fieldConfig"]["overrides"]
                 for prop in override["properties"] if prop["id"] == "mappings"
                 for mapping in prop["value"] for option in mapping["options"].values()}
        self.assertLessEqual({"Not responding to control", "Control port closed", "Off while in use",
                              "Reports a problem", "Commands quarantined"}, words)
        names = {annotation["name"] for annotation in house["annotations"]["list"]}
        self.assertLessEqual({"Service starts", "Deploys"}, names)
        self.assertNotIn("hour()", json.dumps(boards()), "hour() is UTC only")

    def test_routing_counts_read_unknown_not_zero_while_telemetry_is_unreadable(self):
        from tests import test_zone_sync as zones
        for files, executor in ((FILES, False), (rendered(zones.house()), True)):
            board = json.loads(files[f"{oh_metrics.GRAFANA_DASHBOARDS}/{dashboards.HOUSE}.json"])
            since = next(panel for panel in panels(board) if panel["title"] == "Since yesterday evening")
            exprs = {target["legendFormat"]: target["expr"] for target in since["targets"]}
            for name in ("Source changes", "Slow or missed"):
                self.assertTrue(exprs[name].endswith(
                    f") and on() {dashboards.ROUTING_COUNTS_VALID}) or vector(NaN)"), name)
            self.assertEqual(since["fieldConfig"]["defaults"]["mappings"], dashboards.NAN_UNKNOWN)
            self.assertEqual(dashboards.NAN_UNKNOWN[0]["options"]["result"]["text"], "Unknown")
            if executor:
                self.assertTrue(exprs["Device failures"].endswith(
                    f"and on() {dashboards.ROUTING_COUNTS_VALID} and on() {dashboards.EXECUTOR_VALID}) "
                    "or vector(NaN)"))
            else:
                # Without an executor nothing counts device failures: no 0 that means nothing.
                self.assertNotIn("Device failures", exprs)

    def test_two_zones_of_a_room_and_two_checks_of_a_device_read_apart(self):
        """A room with two zones (a desk and a TV) has rows in Needs
        attention now that differ only by zone, and a device's alerts differ
        only by check."""
        house = boards()[dashboards.HOUSE]
        for title, columns in (("Needs attention now", {"zone": "Zone", "check": "Problem"}),
                               ("Alerts firing", {"zone": "Zone", "check": "Check"})):
            panel = next(panel for panel in panels(house) if panel["title"] == title and panel["type"] == "table")
            organize = panel["transformations"][-1]["options"]
            for label, name in columns.items():
                self.assertIn(label, organize["includeByName"], title)
                self.assertEqual(organize["renameByName"][label], name, title)
            words = {override["matcher"]["options"]: prop["value"][0]["options"]
                     for override in panel["fieldConfig"]["overrides"] for prop in override["properties"]
                     if prop["id"] == "mappings"}
            self.assertEqual(words["zone"]["example_east"]["text"], "Example East", title)
            self.assertEqual(words["check"]["power"]["text"], "Off while in use", title)

    def test_giving_up_means_a_timeout_everywhere(self):
        """A stream_error (an SSE drop, as at every gateway restart) is not a
        remote giving up: the alert, the Remotes tile, Since yesterday
        evening and the Switching board count the same timeout kinds."""
        alert = next(rule for rule in RULES["groups"][1]["rules"] if rule.get("alert") == "ThermiteRemoteTimeouts")
        timeouts = f'kind=~"{dashboards.TIMEOUTS}"'
        self.assertIn(timeouts, alert["expr"])
        found = []
        for name, board in boards().items():
            for panel in panels(board):
                for query in panel.get("targets", []):
                    if re.search(r"thermite_client_(events_total|wait_seconds)", query["expr"]):
                        found.append((panel["title"], query.get("legendFormat"), query["expr"]))
        for title, legend, expr in found:
            wanted = f'kind=~"{dashboards.STREAM_FAILURES}"' if title == "Event stream failures" else timeouts
            self.assertIn(wanted, expr, (title, legend))
        self.assertLessEqual({"Remotes", "Since yesterday evening", "Remotes gave up", "Waited before giving up",
                              "Event stream failures"}, {title for title, _, _ in found})

    def test_colours_have_one_meaning(self):
        for name, board in boards().items():
            for panel in panels(board):
                configs = [panel.get("fieldConfig", {}).get("defaults", {})]
                configs += [{prop["id"]: prop["value"] for prop in override["properties"]}
                            for override in panel.get("fieldConfig", {}).get("overrides", [])]
                for config in configs:
                    for mapping in config.get("mappings", []):
                        for option in mapping.get("options", {}).values():
                            if "color" not in option:
                                continue
                            self.assertIn(option["color"], dashboards.SEMANTIC, (name, panel["title"]))
                            if option["color"] == dashboards.IN_USE:
                                self.assertNotIn(option["text"], ("Up", "Online", "Open", "OK", "Ready"))
                    mapped = [m for m in config.get("mappings", []) if any("color" in o for o in m["options"].values())]
                    if mapped and "thresholds" in config:
                        # The threshold colour at each mapped value is the mapping's colour.
                        steps = config["thresholds"]["steps"]
                        for mapping in mapped:
                            for key, option in mapping["options"].items():
                                if not re.fullmatch(r"-?[0-9.]+", key):
                                    continue
                                at = [s["color"] for s in steps if s["value"] is None or s["value"] <= float(key)]
                                self.assertEqual(at[-1], option["color"], (name, panel["title"], key))
                if panel["type"] == "state-timeline":
                    # Threshold colour mode turns merged values into ranges and drops the mappings.
                    self.assertEqual(panel["fieldConfig"]["defaults"]["color"]["mode"], "fixed",
                                     (name, panel["title"]))
        self.assertEqual(dashboards.UP_DOWN[0]["options"]["1"]["color"], dashboards.OK)
        self.assertEqual(dashboards.ON_OFF[0]["options"]["1"]["color"], dashboards.IN_USE)
        self.assertEqual(dashboards.ON_OFF[0]["options"]["0"]["color"], dashboards.OFF)

    def test_devices_board_has_a_row_per_device_and_no_routing_selections(self):
        board = boards()[dashboards.DEVICES]
        titles = [panel["title"] for panel in panels(board)]
        self.assertEqual(titles[0], "Devices")
        self.assertFalse([title for title in titles if "Routing selections" in title])
        table = next(panel for panel in panels(board) if panel["title"] == "Devices")
        self.assertEqual(table["transformations"][0]["id"], "merge")
        self.assertEqual(len(table["targets"]), 7)

    def test_a_shared_receivers_power_shows_in_the_room_each_zone_plays_in(self):
        from tests import test_zone_sync as zones
        files = rendered(zones.house())
        board = json.loads(files[f"{oh_metrics.GRAFANA_DASHBOARDS}/{dashboards.DEVICES}.json"])
        rows = {panel["title"]: {target["legendFormat"]: target["expr"] for target in panel["targets"]}
                for panel in panels(board) if panel["type"] == "state-timeline"}
        cinema, lounge = rows["Example Cinema"], rows["Example Lounge"]
        avr = "Example Avr (Marantz CINEMA 30)"
        self.assertEqual(cinema[f"{avr} · Main"], 'max(thermite_item_value{item="example_avr_main_power"})')
        self.assertEqual(lounge[f"{avr} · Zone 2"], 'max(thermite_item_value{item="example_avr_zone2_power"})')
        self.assertFalse([label for label in cinema if "Zone 2" in label])
        self.assertFalse([label for label in lounge if avr in label and "Zone 2" not in label])
        # A device with one power Item keeps its name, in the room of the zone that needs it.
        self.assertIn("Example Tv", lounge)
        self.assertIn("Example Screen (Sony Example TV)", cinema)

    def test_devices_table_has_no_row_for_a_house_wide_check(self):
        """The house-wide checks name jobs and services (openhab, node...) as
        their device; merged on device, each would be a row with no room or
        name. Every query that reads thermite:check keeps only devices."""
        house_wide = {rule["labels"]["device"] for rule in RULES["groups"][0]["rules"]
                      if rule.get("record") == alerts.CHECK and rule["labels"]["room"] == "house"}
        self.assertTrue(house_wide)
        self.assertFalse(house_wide & set(MAP["devices"]))
        table = next(panel for panel in panels(boards()[dashboards.DEVICES]) if panel["title"] == "Devices")
        for query in table["targets"]:
            if alerts.CHECK in query["expr"]:
                head, _, _ = query["expr"].partition(" or ")
                self.assertIn(alerts.CHECK, head)
                self.assertTrue(head.endswith("and on(device) thermite_device_info)"), query["expr"])

    def test_items_the_runtime_lacks_and_refused_streams_are_shown(self):
        openhab = boards()[dashboards.OPENHAB]
        missing = next(panel for panel in panels(openhab) if panel["title"] == "Items missing")
        self.assertEqual(missing["targets"][0]["expr"], "thermite_items_missing")
        self.assertEqual(sum(panel["gridPos"]["w"] for panel in openhab["panels"] if panel["gridPos"]["y"] == 0), 24)
        failures = next(panel for panel in panels(boards()[dashboards.SWITCHING])
                        if panel["title"] == "Event stream failures")
        self.assertIn("thermite_gateway_sse_refused_total", json.dumps(failures["targets"]))

    def test_host_board_counts_targets_and_shows_access_units(self):
        board = boards()[dashboards.HOST]
        targets = oh_metrics.scrape_targets(FILES[oh_metrics.PROMETHEUS_YML])
        up = next(panel for panel in panels(board) if panel["title"] == "Scrape targets")
        self.assertEqual(up["fieldConfig"]["defaults"]["unit"], f"suffix:/{targets} up")
        units = next(panel for panel in panels(board) if panel["title"] == "Units active")
        self.assertIn("thermite\\\\-access\\\\-worker\\\\.service", units["targets"][0]["expr"])
        restarts = next(panel for panel in panels(board) if panel["title"] == "Restarts")
        self.assertIn("increase(", restarts["targets"][0]["expr"])
        self.assertTrue(restarts["targets"][0]["expr"].endswith("> 0"))
        openhab = boards()[dashboards.OPENHAB]
        self.assertNotIn("Bundles not ACTIVE", [panel["title"] for panel in panels(openhab)])
        self.assertIn("thingHandler pool", [panel["title"] for panel in panels(openhab)])
        busiest = [panel["targets"][0]["expr"] for panel in panels(openhab) if panel["title"].startswith("Busiest")]
        self.assertEqual(len(busiest), 2)
        for expr in busiest:
            # One set of series for the whole range, not a new top set at every step.
            self.assertRegex(expr, r" and topk\(6, sum by \(\w+\) \(increase\(.+\[\$__range\] @ end\(\)\)\)\)$")

    def test_tables_say_why_they_are_empty_and_dash_a_missing_label(self):
        for name, board in boards().items():
            for panel in panels(board):
                if panel["type"] != "table" or panel["fieldConfig"]["defaults"].get("noValue") in (None, "—"):
                    continue
                dashed = {override["matcher"]["options"] for override in panel["fieldConfig"]["overrides"]
                          if {"id": "noValue", "value": "—"} in override["properties"]}
                self.assertTrue(dashed, (name, panel["title"]))


def without_matrix(idle=None):
    """The example house with its HDMI matrix taken out: each room's source
    is cabled straight to its display."""
    data = copy.deepcopy(INVENTORY)
    data["devices"] = [device for device in data["devices"] if device["id"] != "example_matrix"]
    data["routing"] = {
        "sources": {"example_player": {"label": "Player", **({"idle": idle} if idle else {})},
                    "example_computer": {"label": "Computer"}},
        "rooms": {"example_west": {"sources": ["example_player"],
                                   "display": {"device": "example_tv", "sources": {"example_player": "hdmi1"}}},
                  "example_east": {"sources": ["example_computer"],
                                   "display": {"device": "example_display", "sources": {"example_computer": "hdmi2"}}}}}
    return data


class NoMatrix(unittest.TestCase):
    def test_a_house_without_an_hdmi_matrix_generates_routes_and_is_measured(self):
        for idle in (None, "standby"):
            with self.subTest(idle=idle):
                data = without_matrix(idle)
                self.assertEqual(oh_render.validate(data), [])
                text = oh_render.render(copy.deepcopy(data))[routing.RULE]
                start = text.index("const TABLE = ") + len("const TABLE = ")
                table = json.JSONDecoder().raw_decode(text, start)[0]
                self.assertEqual("lifecycle" in table, idle == "standby")
                west = table["rooms"]["example_west"]
                self.assertIsNone(west["hdmi"])
                self.assertEqual(west["inputs"]["example_player"], [["example_tv_input", "hdmi1"]])
                self.assertEqual(table["rooms"]["example_east"]["inputs"]["example_computer"],
                                 [["example_display_input", "hdmi2"]])
                mapping = json.loads(rendered(data)[oh_metrics.EXPORTER_MAP])
                self.assertEqual(sorted(mapping["routing"]["zones"]), ["example_east", "example_west"])
                self.assertNotIn("example_matrix", mapping["devices"])

    def test_a_source_on_matrix_ports_still_needs_the_matrix(self):
        data = without_matrix()
        data["routing"]["sources"]["example_computer"]["hdmi_input"] = 1
        problems = routing.validate(data)
        self.assertIn("routing.hdmi_matrix: 'None' is not a device id", problems)
        self.assertIn("routing.hdmi_off_input: None is not a positive number", problems)


class Units(unittest.TestCase):
    def test_access_units_are_daemons_but_not_containers(self):
        from oh_access import staging
        self.assertEqual(staging.ACCESS_UNITS, oh_units.ACCESS_UNITS)
        self.assertLessEqual(set(oh_units.ACCESS_UNITS), set(oh_units.DAEMONS))
        self.assertFalse(set(oh_units.ACCESS_UNITS) & set(oh_units.CONTAINERS))
        self.assertEqual(set(oh_deploy.CONTAINER_DROPINS), set(oh_units.CONTAINED))


    def test_staged_units_may_write_only_their_textfiles(self):
        from oh_access import staging
        units = staging.units("/opt/example/release", state="/srv/example/access", callbacks=True,
                              textfile="/srv/example/runtime/textfile")
        self.assertEqual(len(units), 3)
        for name, text in units.items():
            self.assertIn('\nEnvironment="THERMITE_TEXTFILE_DIR=/srv/example/runtime/textfile"\n', text, name)
            self.assertIn('\nReadWritePaths="-/srv/example/runtime/textfile"\n', text, name)
        for text in staging.units("/opt/example/release", state="/srv/example/access").values():
            self.assertNotIn("THERMITE_TEXTFILE_DIR", text)


class Notifications(unittest.TestCase):
    def test_metrics_are_typed(self):
        text = notifications.Relay("http://127.0.0.1:1", {}, now=lambda: 100).metrics()
        for name, kind, _ in notifications.METRICS:
            self.assertIn(f"# TYPE {name} {kind}\n", text)
        self.assertIn("thermite_notification_delivered_total 0\n", text)


class Deploy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host = {**HOST, "root": str(self.root)}

    def test_trust_certificate_only_when_it_is_one(self):
        access = oh_deploy.installation(self.host).access
        self.assertIsNone(oh_deploy.gateway_trust(None))
        self.assertIsNone(oh_deploy.gateway_trust(self.host))
        access.mkdir(parents=True)
        (access / "tls-cert.pem").write_text("not a certificate")
        self.assertIsNone(oh_deploy.gateway_trust(self.host))
        pem = b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        (access / "tls-cert.pem").write_bytes(pem)
        self.assertEqual(oh_deploy.gateway_trust(self.host).data, pem)

    def test_deploy_metrics_are_written_whole_and_never_fail_a_deploy(self):
        layout = oh_deploy.state(self.host)
        self.assertFalse(oh_deploy.deploy_metrics(layout, "deploy"))
        layout.textfile.mkdir(parents=True)
        self.assertTrue(oh_deploy.deploy_metrics(layout, "rollback", clock=lambda: 1234.5))
        written = layout.textfile / oh_deploy.DEPLOY_TEXTFILE
        self.assertIn('thermite_deploy_timestamp_seconds{how="rollback"} 1234.500\n', written.read_text())
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), oh_deploy.FILE_MODE)
        self.assertEqual([p.name for p in layout.textfile.iterdir()], [oh_deploy.DEPLOY_TEXTFILE])
        self.assertIn((oh_deploy.DIR_MODE, [layout.metrics, layout.exporter_app, layout.textfile]),
                      layout.metrics_directories())


class Textfiles(unittest.TestCase):
    def setUp(self):
        try:
            from oh_access import metrics
        except ImportError as exc:  # prometheus_client is a site requirement
            self.skipTest(str(exc))
        self.metrics = metrics
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_written_atomically_with_a_readable_mode(self):
        textfile = self.metrics.Textfile(self.metrics.CALLBACKS, "example", self.directory, every=60)
        self.assertTrue(textfile.write())
        written = self.directory / "example.prom"
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o640)
        self.assertIn("# TYPE thermite_callbacks_total counter", written.read_text())
        self.assertEqual([p.name for p in self.directory.iterdir()], ["example.prom"])

    def test_nothing_is_written_without_a_directory(self):
        self.assertIsNone(self.metrics.start(self.metrics.CALLBACKS, "example", environ={}))
        started = self.metrics.start(self.metrics.CALLBACKS, "example",
                                     environ={self.metrics.ENVIRONMENT: str(self.directory)})
        self.addCleanup(started.stop)
        self.assertTrue((self.directory / "example.prom").exists())

    def test_a_failed_write_is_reported_once_and_not_raised(self):
        textfile = self.metrics.Textfile(self.metrics.CALLBACKS, "example", self.directory / "missing", every=60)
        with self.assertLogs(self.metrics.LOG, "WARNING") as logged:
            self.assertFalse(textfile.write())
            self.assertFalse(textfile.write())
        self.assertEqual(len(logged.records), 1)

    def test_labels_are_finite(self):
        text = self.metrics.prometheus_client.generate_latest(self.metrics.GATEWAY).decode()
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for value in re.findall(r'="([^"]*)"', line):
                self.assertRegex(value, r"^[a-z0-9_./+-]*$|^\+Inf$|^[0-9.e+-]+$", line)

    def test_callbacks_are_counted_without_a_last_received_time(self):
        """Callbacks come when devices change, so the last one's time says
        nothing of the relay's health; the forwarded count says the rest."""
        forwarded = self.metrics.CALLBACK_COUNT.labels("forwarded")
        before = forwarded._value.get()
        self.metrics.callback_receipt({"status": 200, "outcome": "native_response"})
        self.assertEqual(forwarded._value.get(), before + 1)
        text = self.metrics.prometheus_client.generate_latest(self.metrics.CALLBACKS).decode()
        self.assertNotIn("thermite_callbacks_last_received_timestamp_seconds", text)

    def test_every_client_kind_is_a_timeout_or_a_stream_failure(self):
        self.assertEqual(sorted(alerts.TIMEOUT_KINDS + alerts.STREAM_KINDS), sorted(self.metrics.CLIENT_KINDS))

    def test_dashboards_name_only_the_gateways_route_classes(self):
        self.assertLessEqual(set(dashboards.WAITED_ROUTES), set(self.metrics.ROUTES))
        switching = json.dumps(boards()[dashboards.SWITCHING])
        for route in dashboards.WAITED_ROUTES:
            self.assertIn(route, switching)
        self.assertEqual(set(dashboards.FAILED_OPERATIONS),
                         set(exporter.OPERATION_OUTCOMES) & set(exporter.FAILURE_REASONS))

    def test_policy_metrics_name_routing_zones_and_each_zones_state(self):
        from oh_access import worker
        zones = ("example_policy_west", "example_policy_east", "example_policy_den", "example_policy_hall",
                 "example_policy_loft")
        telemetry = worker.Telemetry(zones, monotonic=lambda: 100.0)
        text = self.metrics.prometheus_client.generate_latest(self.metrics.WORKER).decode()
        for zone in zones:
            for state in self.metrics.ROOM_STATES:
                self.assertIn(f'thermite_policy_room_state{{state="{state}",zone="{zone}"}} 0.0', text)
        west, east, den, hall, loft = zones
        telemetry.stepped({
            west: {"phase": "invalid", "reason": "invalid_policy"},
            # Locked, but openHAB did not answer the worker's read.
            east: {"phase": "enforcing", "state": "unknown", "reason": "native_read_unavailable"},
            # Locked with a passive display: held, with nothing else to check.
            den: {"phase": "enforcing", "state": "degraded", "matched": ["example_matrix_output3"],
                  "mismatched": [], "unknown": [], "limitations": ["passive_endpoint:example_den_tv"]},
            # Unlocked beside an unreadable policy: nothing of its own to enforce.
            hall: {"phase": "unlocked", "state": "degraded", "reason": "invalid_policy"},
            # Locked, its receiver's last report too old to trust (it reports
            # only on change): nothing is wrong, so the lock is held.
            loft: {"phase": "enforcing", "state": "unknown", "matched": ["example_matrix_output4"],
                   "mismatched": [], "unknown": ["example_loft_avr_power"]}})
        state = {zone: next(each for each in self.metrics.ROOM_STATES
                            if self.metrics.POLICY_ROOM_STATE.labels(zone, each)._value.get() == 1) for zone in zones}
        self.assertEqual(state, {west: "unavailable", east: "unavailable", den: "scheduled", hall: "unlocked",
                                 loft: "scheduled"})
        text = self.metrics.prometheus_client.generate_latest(self.metrics.WORKER).decode()
        self.assertIn(f'thermite_policy_reconcile_age_seconds{{zone="{den}"}} 0.0', text)
        self.assertNotIn('thermite_policy_reconcile_age_seconds{room=', text)
        # A drift that is not put back is a lock not held.
        telemetry.stepped({den: {"phase": "enforcing", "state": "degraded", "matched": [],
                                 "mismatched": ["example_matrix_output3"], "unknown": [], "limitations": []}})
        self.assertEqual(self.metrics.POLICY_ROOM_STATE.labels(den, "unavailable")._value.get(), 1)

    def test_route_classes_never_carry_the_path(self):
        route = self.metrics.route_class
        self.assertEqual(route("/api/v1/rooms/example_west/commands"), "command")
        self.assertEqual(route("/api/v1/rooms/example_west/policy"), "policy")
        self.assertEqual(route("/rest/items/loc_example_west_source/state"), "items")
        self.assertEqual(route("/static/remote/remote.js"), "asset")
        self.assertEqual(route("/api/v1/events/remote"), "events")
        self.assertEqual(route("/rest/events"), "other")
        self.assertEqual(route("/api/v1/telemetry"), "other")
        self.assertEqual(route("/../../etc/passwd"), "other")
        self.assertEqual(self.metrics.code_class(None), "aborted")
        self.assertEqual(self.metrics.code_class(503), "5xx")


if __name__ == "__main__":
    unittest.main()
