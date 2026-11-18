"""Scenarios through the real health rules: what fires, with which labels.

Each scenario gives the series the fictional example house would export
through some minutes, and promtool, from the Prometheus image versions.json
pins, evaluates the generated alerts.yml over them. The image must be in
podman's local store already: nothing is pulled, and without podman or the
image the scenarios are skipped, saying so.

A check's healthy series come from the exporter map, so a scenario names
only what goes wrong: a device's port closing, a room's readback going
unknown. A readback that is unknown has no thermite_item_info series, as
the exporter leaves it.
"""
import copy
import functools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy  # noqa: E402
from oh_features import routing  # noqa: E402
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_units  # noqa: E402
from oh_metrics import alerts  # noqa: E402

WEST, EAST = "example_west", "example_east"
PLAYER, COMPUTER = "example_player", "example_computer"
TV, DISPLAY, MATRIX = "example_tv", "example_display", "example_matrix"
TEXTFILES = "/host/srv/thermite/runtime/textfile"


@functools.lru_cache(maxsize=None)
def house() -> tuple[str, dict, dict]:
    """The example house's alerts.yml, exporter map and alertmanager.yml."""
    inventory = oh_inventory.load(AREA / "instance/devices.yaml")
    host = yaml.safe_load((AREA / "instance/host.yaml").read_text())
    files = oh_metrics.render(copy.deepcopy(inventory), host)
    return (files["prometheus/alerts.yml"], json.loads(files[oh_metrics.EXPORTER_MAP]),
            yaml.safe_load(files["alertmanager/alertmanager.yml"]))


def image() -> str:
    return oh_deploy.image(json.loads((AREA / "versions.json").read_text()), "prometheus")


def unavailable() -> str | None:
    """Why promtool cannot run here; None when it can."""
    podman = shutil.which("podman")
    if not podman:
        return "needs podman, to run promtool from the pinned Prometheus image"
    if subprocess.run([podman, "image", "exists", image()], capture_output=True, check=False).returncode:
        return f"needs the pinned Prometheus image in podman's local store: podman pull {image()}"
    return None


def series(metric: str, /, **labels: str) -> str:
    return metric + "{" + ",".join(f'{key}="{value}"' for key, value in labels.items()) + "}"


def through(*spans: tuple[int, object]) -> str:
    """promtool's notation for (samples, value) spans, one sample an
    interval; a None value is no sample, and stale where the series ends."""
    out, present = [], False
    for samples, value in spans:
        if value is None:
            if present:
                out.append("stale")
                samples -= 1
            if samples:
                out.append(f"_x{samples}")
        else:
            out.append(f"{value}x{samples - 1}" if samples > 1 else str(value))
        present = value is not None
    return " ".join(out)


def readback(zone: str, *spans: tuple[int, str | None]) -> dict[str, str]:
    """A zone's source readback through (samples, source) spans: None while
    it is unknown, which leaves no thermite_item_info series."""
    out = {}
    for value in dict.fromkeys(value for _, value in spans if value):
        name = series("thermite_item_info", item=f"loc_{zone}_source", point="source", room=zone, zone=zone,
                      value=value)
        out[name] = through(*((samples, 1 if each == value else None) for samples, each in spans))
    return out


def check_series(device: str, check: str) -> str:
    """The one series a device's check reads, from the exporter map."""
    found = [each for each in house()[1]["health_checks"] if (each["device"], each["check"]) == (device, check)]
    assert len(found) == 1, (device, check, found)
    return series(found[0]["metric"], **found[0]["labels"])


def healthy(samples: int) -> dict[str, str]:
    """Every series the house-wide and device checks read, all well."""
    out = {series("thermite_openhab_up", job="thermite"): f"1x{samples}",
           series("node_scrape_collector_success", job="node", collector="systemd"): f"1x{samples}"}
    for job in (*alerts.CORE_JOBS, *alerts.ALERTING_JOBS):
        out[series("up", job=job)] = f"1x{samples}"
    for service in alerts.SERVICES:
        out[series("probe_success", job="service_http", service=service)] = f"1x{samples}"
    for each in house()[1]["health_checks"]:
        out[series(each["metric"], **each["labels"])] = f"{1 - each['bad']}x{samples}"
    return out


def check(room: str, name: str, device: str = "", zone: str = "", scope: str = alerts.CONTROL) -> dict[str, str]:
    """The labels a required check gives its alert."""
    labels = {"room": room, "zone": zone, "device": device, "check": name, "required": "true", "scope": scope}
    return {key: value for key, value in labels.items() if value}


class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = unavailable()
        if reason:
            raise unittest.SkipTest(reason)
        cls.podman, cls.image = shutil.which("podman"), image()

    def run_rules(self, inputs: dict[str, str], *, firing=(), values=(), interval: str = "1m") -> None:
        """Evaluate the example house's rules over `inputs`, a sample each
        `interval`. firing: (time, alert, [labels of each one firing then]),
        exactly; severity and the catalogue's words are added here.
        values: (time, expression, [(series, value)]), exactly."""
        def expected(name, labels):
            title, detail = alerts.CATALOG[name]
            severity = "critical" if name in alerts.CRITICAL else "warning"
            return {"exp_labels": {"severity": severity, **labels},
                    "exp_annotations": {"summary": title, "description": detail}}

        test = {"interval": interval,
                "input_series": [{"series": name, "values": samples} for name, samples in inputs.items()],
                "alert_rule_test": [{"eval_time": when, "alertname": name,
                                     "exp_alerts": [expected(name, labels) for labels in each]}
                                    for when, name, each in firing],
                "promql_expr_test": [{"expr": expr, "eval_time": when,
                                      "exp_samples": [{"labels": name, "value": value} for name, value in each]}
                                     for when, expr, each in values]}
        # The checks before the alerts that read them, as written: promtool
        # otherwise takes the groups in either order, and an alert that
        # reads the last evaluation's checks fires an interval later.
        order = [group["name"] for group in yaml.safe_load(house()[0])["groups"]]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "alerts.yml").write_text(house()[0])
            Path(directory, "scenario.yml").write_text(yaml.safe_dump(
                {"rule_files": ["alerts.yml"], "evaluation_interval": interval, "group_eval_order": order,
                 "tests": [test]}))
            # As root in the container, which rootless podman maps to this
            # user: the image's own user cannot read a private directory.
            ran = subprocess.run([self.podman, "run", "--rm", "--pull=never", "--network=none", "--user=0",
                                  "--security-opt=label=disable", "-v", f"{directory}:/rules:ro",
                                  "--entrypoint=promtool", self.image, "test", "rules", "/rules/scenario.yml"],
                                 capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_a_device_that_fails_in_use_alerts_with_its_room_though_the_readback_went_with_it(self):
        # The West watches the player. At 10 minutes its TV stops answering:
        # port closed, power and problem unread, and the room's readback
        # unknown. At 40 it is back.
        inputs = {**healthy(60),
                  **readback(WEST, (10, PLAYER), (30, None), (21, PLAYER)), **readback(EAST, (61, routing.OFF)),
                  check_series(TV, "port"): through((10, 1), (30, 0), (21, 1)),
                  check_series(TV, "power"): through((10, 1), (30, None), (21, 1)),
                  check_series(TV, "problem"): through((10, 0), (30, None), (21, 0))}
        port = check(WEST, "port", TV, WEST)
        unread = [check(WEST, "source", zone=WEST), check(WEST, "power", TV, WEST), check(WEST, "problem", TV, WEST)]
        self.run_rules(inputs, firing=[
            ("9m", "ThermiteCheckFailing", []),
            ("14m", "ThermiteCheckFailing", []),
            ("15m", "ThermiteCheckFailing", [port]),
            ("39m", "ThermiteCheckFailing", [port]),
            ("19m", "ThermiteControlUnknown", []),
            ("20m", "ThermiteControlUnknown", unread),
            ("39m", "ThermiteControlUnknown", unread),
            # Recovery clears both at once.
            ("40m", "ThermiteCheckFailing", []),
            ("40m", "ThermiteControlUnknown", []),
            ("60m", "ThermiteCheckFailing", []),
        ], values=[
            # The defect this holds off: demand that ends with the readback.
            ("12m", f'thermite:check{{device="{TV}",check="port"}}',
             [(series("thermite:check", **port), 1)]),
            ("12m", f'thermite:zone_source{{zone="{WEST}"}}',
             [(series("thermite:zone_source", item=f"loc_{WEST}_source", value=PLAYER, zone=WEST), 9 * 60)]),
            ("12m", "thermite:control_health", [("thermite:control_health", 1)]),
            ("45m", "thermite:control_health", [("thermite:control_health", 0)]),
        ])

    def test_a_room_in_use_that_stops_reporting_its_source_alerts_as_unknown_with_its_room(self):
        # Every device the West's checks read answers, yet from 10 minutes
        # its source is unknown: a switch in its path with no check, say.
        inputs = {**healthy(60), **readback(WEST, (10, COMPUTER), (51, None)), **readback(EAST, (61, routing.OFF))}
        unread = check(WEST, "source", zone=WEST)
        self.run_rules(inputs, firing=[
            ("19m", "ThermiteControlUnknown", []),
            ("20m", "ThermiteControlUnknown", [unread]),
            ("60m", "ThermiteControlUnknown", [unread]),
            ("60m", "ThermiteCheckFailing", []),
        ], values=[("30m", "thermite:control_health", [("thermite:control_health", -1)])])

    def test_a_room_turned_off_raises_nothing_whatever_its_devices_do_next(self):
        # The West is turned Off at 10 minutes. Its TV then sleeps: port
        # closed from 12, and from 20 nothing of the room can be read at all.
        inputs = {**healthy(90),
                  **readback(WEST, (10, PLAYER), (10, routing.OFF), (71, None)), **readback(EAST, (91, routing.OFF)),
                  check_series(TV, "port"): through((12, 1), (79, 0)),
                  check_series(TV, "power"): through((10, 1), (10, 0), (71, None)),
                  check_series(PLAYER, "power"): through((10, 1), (81, 0))}
        idle = {**check(WEST, "port", TV, WEST), "required": "false"}
        self.run_rules(inputs, firing=[
            (when, name, []) for when in ("18m", "30m", "60m", "90m")
            for name in ("ThermiteCheckFailing", "ThermiteControlUnknown")
        ], values=[
            # Off releases the room's devices at once, not after a hold.
            ("11m", f'thermite:check{{device="{TV}",check="power"}}',
             [(series("thermite:check", **{**check(WEST, "power", TV, WEST), "required": "false"}), 1)]),
            ("30m", f'thermite:check{{device="{TV}",check="port"}}', [(series("thermite:check", **idle), 1)]),
            ("30m", f'thermite:check{{zone="{WEST}",check="source"}}',
             [(series("thermite:check", **{**check(WEST, "source", zone=WEST), "required": "false"}), -1)]),
            ("30m", "thermite:control_health", [("thermite:control_health", 0)]),
        ])

    def test_a_readback_unknown_since_start_up_raises_nothing(self):
        # Nothing has ever read the West's source, and its TV does not answer.
        inputs = {**healthy(60), **readback(EAST, (61, routing.OFF)),
                  check_series(TV, "port"): through((61, 0)),
                  check_series(TV, "power"): through((61, None)),
                  check_series(TV, "problem"): through((61, None))}
        self.run_rules(inputs, firing=[
            (when, name, []) for when in ("15m", "30m", "60m")
            for name in ("ThermiteCheckFailing", "ThermiteControlUnknown")
        ], values=[
            ("30m", f'thermite:zone_source{{zone="{WEST}"}}', []),
            ("30m", 'thermite:check{check="source"}', [
                (series("thermite:check", **{**check(EAST, "source", zone=EAST), "required": "false"}), 0),
                (series("thermite:check", **{**check(WEST, "source", zone=WEST), "required": "false"}), -1)]),
            ("30m", "thermite:control_health", [("thermite:control_health", 0)]),
        ])

    def test_another_source_releases_the_devices_only_the_first_one_used(self):
        # The West changes from the player to the computer at 10 minutes, and
        # the player then sleeps. The TV, which both use, fails at 20.
        inputs = {**healthy(40),
                  **readback(WEST, (10, PLAYER), (31, COMPUTER)), **readback(EAST, (41, routing.OFF)),
                  check_series(PLAYER, "power"): through((12, 1), (29, 0)),
                  check_series(TV, "port"): through((20, 1), (21, 0))}
        self.run_rules(inputs, firing=[
            ("24m", "ThermiteCheckFailing", []),
            ("40m", "ThermiteCheckFailing", [check(WEST, "port", TV, WEST)]),
            ("40m", "ThermiteControlUnknown", []),
        ], values=[
            # Both of the player's checks are released with it: its power,
            # and the problem its own script keeps of its commands.
            ("11m", f'thermite:check{{device="{PLAYER}"}}',
             [(series("thermite:check", **{**check(WEST, name, PLAYER, WEST), "required": "false"}), 0)
              for name in ("power", "problem")]),
        ])

    def test_a_reading_the_room_does_not_offer_is_unknown_and_holds_the_source_before_it(self):
        other = series("thermite_item_info", item=f"loc_{WEST}_source", point="source", room=WEST, zone=WEST,
                       value="other")
        inputs = {**healthy(40), **readback(WEST, (10, PLAYER), (31, None)), **readback(EAST, (41, routing.OFF)),
                  other: through((10, None), (31, 1)),
                  check_series(TV, "port"): through((10, 1), (31, 0))}
        self.run_rules(inputs, firing=[
            ("40m", "ThermiteCheckFailing", [check(WEST, "port", TV, WEST)]),
            ("40m", "ThermiteControlUnknown", [check(WEST, "source", zone=WEST)]),
        ])

    def test_demand_is_held_for_three_days_and_no_longer(self):
        # Ten minutes a sample. The West's TV fails with the readback at one
        # hour and stays so: the room counts as using it for alerts.HELD.
        self.assertEqual(alerts.HELD, "3d")
        day = 24 * 6
        inputs = {**healthy(4 * day),
                  **readback(WEST, (6, PLAYER), (4 * day, None)), **readback(EAST, (4 * day + 1, routing.OFF)),
                  check_series(TV, "port"): through((6, 1), (4 * day - 5, 0))}
        port = check(WEST, "port", TV, WEST)
        self.run_rules(inputs, interval="10m", firing=[
            ("2h", "ThermiteCheckFailing", [port]),
            ("2d", "ThermiteCheckFailing", [port]),
            ("3d", "ThermiteCheckFailing", [port]),
            ("3d2h", "ThermiteCheckFailing", []),
            ("4d", "ThermiteCheckFailing", []),
            ("3d", "ThermiteControlUnknown", [check(WEST, "source", zone=WEST)]),
            ("3d2h", "ThermiteControlUnknown", []),
        ], values=[
            ("4d", f'thermite:check{{device="{TV}",check="port"}}',
             [(series("thermite:check", **{**port, "required": "false"}), 1)]),
        ])

    def test_monitoring_trouble_is_not_a_control_alert(self):
        # From 5 minutes Grafana does not answer, node_exporter cannot read
        # systemd's unit states and openHAB's metrics cannot be scraped.
        grafana = series("probe_success", job="service_http", service="grafana")
        systemd = series("node_scrape_collector_success", job="node", collector="systemd")
        inputs = {**healthy(30), **readback(WEST, (31, PLAYER)), **readback(EAST, (31, routing.OFF)),
                  grafana: through((5, 1), (26, 0)), systemd: through((5, 1), (26, 0)),
                  series("up", job="openhab"): through((5, 1), (26, 0))}
        house_wide = {"room": "house", "required": "true", "scope": alerts.MONITORING}
        self.run_rules(inputs, firing=[
            ("30m", "ThermiteCheckFailing", []),
            ("30m", "ThermiteControlUnknown", []),
            ("9m", "ThermiteMonitoringBroken", []),
            ("10m", "ThermiteMonitoringBroken", [
                {**house_wide, "device": "grafana", "check": "service"},
                {**house_wide, "device": "node", "check": "systemd"}]),
            # The failed scrape has its own alert, once.
            ("10m", "ThermiteScrapeFailed", [{"job": "openhab"}]),
        ], values=[
            ("30m", "thermite:control_health", [("thermite:control_health", 0)]),
            ("30m", 'count(thermite:check{scope="monitoring"} == 1)', [("{}", 3)]),
        ])

    def test_the_alerts_the_gateway_names_carry_the_labels_it_reads(self):
        # alerts.ABOUT, beside the two per-check alerts above: a Thing that
        # never started has its device and room, as the exporter labels a
        # Thing's series, and a lock that cannot be held its zone, as the
        # policy worker labels a zone's state.
        thing = {"device": TV, "room": WEST, "kind": "tv", "thing": f"sony:tv:{TV}", "status": "UNKNOWN"}
        lock = {"zone": WEST, "state": "unavailable"}
        self.assertEqual(set(alerts.ABOUT), {"ThermiteCheckFailing", "ThermiteControlUnknown", "ThermiteThingStuck",
                                             "ThermitePolicyUnenforced"})
        for name, labels in (("ThermiteThingStuck", thing), ("ThermitePolicyUnenforced", lock),
                             ("ThermiteCheckFailing", check(WEST, "port", TV, WEST))):
            self.assertLessEqual(set(alerts.ABOUT[name]), set(labels))
        inputs = {series("thermite_thing_status_info", **thing): "1x20",
                  series("thermite_policy_room_state", **lock): "1x20"}
        self.run_rules(inputs, firing=[
            ("14m", "ThermiteThingStuck", []),
            ("15m", "ThermiteThingStuck", [thing]),
            ("10m", "ThermitePolicyUnenforced", [lock]),
        ])

    def test_a_running_service_that_stops_rewriting_its_metrics_alerts(self):
        # mtime as node_exporter gives it: a file rewritten keeps up with
        # the clock. The gateway's stops at 5 minutes while the gateway
        # runs; the worker's stops too, but the worker is not running; the
        # callback relay is not installed and left a file behind; and a
        # deploy's file is written once.
        def textfile(name):
            return series("node_textfile_mtime_seconds", job="node", file=f"{TEXTFILES}/{name}.prom")

        def written_by(unit):
            return textfile(unit.removesuffix(".service"))

        def active(unit):
            return series("node_systemd_unit_state", job="node", name=unit, state="active")

        stopped = "0+60x5 300x25"
        inputs = {written_by(oh_units.ACCESS): stopped, active(oh_units.ACCESS): "1x30",
                  written_by(oh_units.ACCESS_WORKER): stopped, active(oh_units.ACCESS_WORKER): "1x5 0x25",
                  written_by(oh_units.ACCESS_CALLBACKS): "0x30",
                  textfile("thermite-deploy"): "0x30"}
        gateway = {"job": "node", "name": oh_units.ACCESS, "file": f"{TEXTFILES}/thermite-access.prom"}
        self.run_rules(inputs, firing=[
            # Stale from 120 s after the last write at 5 minutes, for 5 minutes.
            ("12m", "ThermiteServiceMetricsStale", []),
            ("13m", "ThermiteServiceMetricsStale", [gateway]),
            ("30m", "ThermiteServiceMetricsStale", [gateway]),
        ])
        fresh = {**inputs, written_by(oh_units.ACCESS): "0+60x30"}
        self.run_rules(fresh, firing=[("30m", "ThermiteServiceMetricsStale", [])])


class Rules(unittest.TestCase):
    """What the scenarios take for granted, with or without promtool."""

    def setUp(self):
        text, self.map, self.alertmanager = house()
        self.groups = yaml.safe_load(text)["groups"]
        self.recorded = [rule for rule in self.groups[0]["rules"] if rule.get("record") == alerts.CHECK]
        self.alerts = {rule["alert"]: rule for rule in self.groups[1]["rules"] if "alert" in rule}

    def test_every_check_has_a_scope_and_only_what_watches_the_house_is_monitoring(self):
        self.assertEqual({rule["labels"]["scope"] for rule in self.recorded}, {alerts.CONTROL, alerts.MONITORING})
        monitoring = {(rule["labels"]["check"], rule["labels"]["device"]) for rule in self.recorded
                      if rule["labels"]["scope"] == alerts.MONITORING}
        self.assertEqual(monitoring, {*(("scrape", job) for job in alerts.CORE_JOBS), ("service", "grafana"),
                                      ("systemd", "node")})
        for rule in self.recorded:
            if rule["labels"]["scope"] == alerts.MONITORING:
                self.assertEqual(rule["labels"]["room"], "house")

    def test_the_zone_sources_are_recorded_before_the_checks_that_read_them(self):
        first = self.groups[0]["rules"][0]
        self.assertEqual((first["record"], first["expr"]), (alerts.ZONE_SOURCE, alerts.zone_source()))
        self.assertIn(f"[{alerts.HELD}:]", first["expr"])
        self.assertEqual(alerts.OFF, routing.OFF)
        zoned = [rule for rule in self.recorded if rule["labels"].get("zone")]
        self.assertTrue(zoned)
        for rule in zoned:
            self.assertIn(alerts.ZONE_SOURCE + "{", rule["expr"], "a zone's demand is its held source")
            self.assertNotRegex(rule["expr"], r"(and|unless) on\(\) \(count\(thermite_item_info",
                                "demand read from the readback ends with the failure")
        # A house without routing has no zone to hold.
        self.assertNotIn(alerts.ZONE_SOURCE, json.dumps(alerts.rules({})))

    def test_a_zones_source_check_is_required_only_while_the_zone_is_in_use(self):
        sources = [rule for rule in self.recorded if rule["labels"]["check"] == "source"]
        self.assertEqual(sorted((rule["labels"]["zone"], rule["labels"]["required"]) for rule in sources),
                         [(zone, required) for zone in sorted(self.map["routing"]["zones"])
                          for required in ("false", "true")])
        for rule in sources:
            self.assertIn(f'{alerts.ZONE_SOURCE}{{item="loc_{rule["labels"]["zone"]}_source",value!="off"}}',
                          rule["expr"])

    def test_the_two_check_alerts_read_required_control_checks_one_alert_a_check(self):
        required = 'thermite:check{required="true",scope="control"}'
        failing, unknown = self.alerts["ThermiteCheckFailing"], self.alerts["ThermiteControlUnknown"]
        self.assertEqual((failing["expr"], failing["for"], failing["labels"]["severity"]),
                         (f"{required} == 1", "5m", "critical"))
        self.assertEqual((unknown["expr"], unknown["for"], unknown["labels"]["severity"]),
                         (f"{required} == -1", "10m", "warning"))
        for name, rule in self.alerts.items():
            self.assertNotIn(alerts.HEALTH, rule["expr"], f"{name} reads the fold, which names nothing")
        self.assertIn(required, self.groups[0]["rules"][-1]["expr"])

    def test_a_failing_check_holds_back_the_unknown_ones_of_its_own_room_only(self):
        rule = next(rule for rule in self.alertmanager["inhibit_rules"]
                    if rule["source_matchers"] == ['alertname="ThermiteCheckFailing"'])
        self.assertEqual((rule["target_matchers"], rule["equal"]), (['alertname=~"ThermiteControlUnknown"'], ["room"]))
        for rule in self.alertmanager["inhibit_rules"]:
            if rule["source_matchers"] != ['alertname="ThermiteCheckFailing"']:
                self.assertNotIn("equal", rule)
        silenced = {source for source, targets in alerts.INHIBITS if "ThermiteControlUnknown" in targets}
        self.assertEqual(silenced, {"ThermiteRuntimeUnavailable"})

    def test_every_failed_scrape_check_has_one_alert(self):
        scraped = self.alerts["ThermiteScrapeFailed"]["expr"]
        for job in alerts.CORE_JOBS:
            self.assertRegex(scraped, rf'[\"|]{job}[\"|]')
        self.assertIn('thermite:check{scope="monitoring",check!="scrape"} == 1',
                      self.alerts["ThermiteMonitoringBroken"]["expr"])

    def test_the_stale_textfile_alert_names_the_files_the_access_services_write(self):
        try:
            from oh_access import metrics as access
        except ImportError as exc:  # prometheus_client is a site requirement
            self.skipTest(str(exc))
        expr = self.alerts["ThermiteServiceMetricsStale"]["expr"]
        self.assertEqual(expr, alerts.stale_textfiles())
        # The rule takes each file's name from its unit's.
        files = (access.GATEWAY_FILE, access.WORKER_FILE, access.CALLBACKS_FILE)
        self.assertEqual({name + ".service" for name in files}, set(oh_units.ACCESS_UNITS))
        for name in files:
            self.assertIn(name.replace("-", "\\\\-"), expr)
        self.assertNotIn("deploy", expr)
        # Twelve missed writes, and more than a scrape apart.
        self.assertGreaterEqual(alerts.TEXTFILE_STALE, 12 * access.WRITE_SECONDS)
        self.assertIn(f"> {alerts.TEXTFILE_STALE}", expr)
        self.assertEqual((self.alerts["ThermiteServiceMetricsStale"]["for"],
                          self.alerts["ThermiteServiceMetricsStale"]["labels"]["severity"]), ("5m", "warning"))


if __name__ == "__main__":
    unittest.main()
