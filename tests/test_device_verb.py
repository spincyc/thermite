"""thermite device ID: one device's facts, each read from the function or
series that owns it.

The verb is given a stand-in for the one thing it asks, the installation's
Prometheus, at the function it asks through (oh_deploy.http_get). What the
stand-in holds is made by the real exporter from stand-in openHAB answers,
and each check's labels by the real rules, so the names the verb reads are
the ones an installation has. No test here lets the verb reach a host:
setUp replaces the reader for every one.
"""

import contextlib
import copy
import io
import json
import re
import shutil
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import yaml  # noqa: E402

import oh_cli  # noqa: E402
import oh_deploy  # noqa: E402
import oh_host  # noqa: E402
import oh_metrics  # noqa: E402
import oh_paths  # noqa: E402
import oh_render  # noqa: E402
from oh_cli import device  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import remote_diagnostics  # noqa: E402
from oh_integrations import _commands  # noqa: E402
from oh_metrics import alerts, exporter  # noqa: E402

HOUSE = AREA / "instance/devices.yaml"
HOST = oh_host.load(AREA / "instance/host.yaml")
#: The instant every stand-in answer is for.
NOW = 1_800_000_000.0
TV, PLAYER, MATRIX, COMPUTER = "example_tv", "example_player", "example_matrix", "example_computer"
WEST = "example_west"


def refuse(*_args, **_kwargs):
    raise AssertionError("the verb asked a host for something this test did not give it")


class Prometheus:
    """Prometheus' query API over fixed series, for the three questions the
    verb asks. Each must name one device: nothing else is ever selected."""

    def __init__(self):
        self.series: list[tuple[str, dict, float, float]] = []
        self.asked: list[str] = []
        self.clear: float | None = None
        self.down = False
        self.broken = False

    def add(self, name, labels, value, age=10):
        self.series.append((name, {key: str(text) for key, text in labels.items()}, float(value), NOW - age))

    def match(self, name, labels):
        return [row for row in self.series
                if name in ("", row[0]) and all(row[1].get(key) == value for key, value in labels.items())]

    def drop(self, name, **labels):
        gone = self.match(name, labels)
        self.series = [row for row in self.series if row not in gone]

    def put(self, name, value, **labels):
        """Give the series there a new value."""
        found = self.match(name, labels)
        assert found, (name, labels)
        self.series = [(row[0], row[1], float(value), row[3]) if row in found else row for row in self.series]

    def select(self, selector):
        name, inner = re.fullmatch(r"([A-Za-z_:][A-Za-z0-9_:]*)?\{(.*)\}", selector).groups()
        labels = dict(re.findall(r'(\w+)="([^"]*)"', inner))
        assert labels, selector
        return self.match(name or "", labels)

    def get(self, url, timeout=5.0):
        self.asked.append(url)
        if self.down:
            return None, b""
        if self.broken:
            return 200, b"<html>not Prometheus</html>"
        split = urllib.parse.urlsplit(url)
        assert (split.scheme, split.hostname, split.port, split.path) == (
            "http", "127.0.0.1", oh_metrics.ports(HOST)["prometheus"], "/api/v1/query"), url
        query = urllib.parse.parse_qs(split.query)
        assert float(query["time"][0]) == NOW, url
        expr = query["query"][0]
        history = re.fullmatch(r'max\(max_over_time\(timestamp\(thermite_device_problem\{device="[a-z][a-z0-9_]*"\} '
                               r'== 0\)\[14d:5m\]\)\)', expr)
        if history:
            kind = "vector"
            result = [] if self.clear is None else [{"metric": {}, "value": [NOW, str(self.clear)]}]
        elif expr.endswith("[5m]"):
            kind = "matrix"
            result = [{"metric": {"__name__": name, **labels}, "values": [[at - 30, str(value)], [at, str(value)]]}
                      for name, labels, value, at in self.select(expr.removesuffix("[5m]"))]
        else:
            # The device's own series first, and the runtime's check last.
            terms = expr.split(" or ")
            assert re.fullmatch(r'\{device="[a-z][a-z0-9_]*"\}', terms[0]), expr
            assert terms[-1] == 'thermite:check{check="runtime",room="house"}', expr
            rows = []
            for term in terms:
                rows += [row for row in self.select(term) if row not in rows]
            kind = "vector"
            result = [{"metric": {"__name__": name, **labels}, "value": [NOW, str(value)]}
                      for name, labels, value, _ in rows]
        return 200, json.dumps({"status": "success", "data": {"resultType": kind, "result": result}}).encode()


def installation(inventory: dict, *, things: dict | None = None, states: dict | None = None) -> Prometheus:
    """What a healthy installation of `inventory` holds: the real exporter's
    samples for openHAB answering every Thing ONLINE and every kept Item
    with a value (`things` and `states` say otherwise for some), a probe of
    each control port, and every check the real rules record, OK and needed
    now."""
    conf = oh_render.build(House(copy.deepcopy(inventory)))
    mapping = oh_metrics.exporter_map(House(copy.deepcopy(inventory)), conf)
    held = Prometheus()
    status = {uid: "ONLINE" for entry in mapping["devices"].values() for uid in entry["things"]} | (things or {})
    values = {}
    for name, spec in mapping["items"].items():
        values[name] = {"switch": "ON", "number": "30", "problem": _commands.CLEAR,
                        "playing": (spec.get("playing") or [""])[0],
                        "enum": (spec.get("options") or ["hdmi1"])[0]}[spec["convert"]]
        if spec.get("title"):
            values[spec["title"]] = "A Song"
    for name in mapping.get("commands") or {}:
        values[name] = json.dumps({"version": 1, "outcomes": {"applied": {"count": 7, "ms": 700}}, "reads": "ok"})
    values |= states or {}
    answers = {exporter.THINGS_PATH: [{"UID": uid, "statusInfo": {"status": text}} for uid, text in status.items()],
               exporter.ITEMS_PATH: [{"name": name, "state": state} for name, state in values.items()]}
    for name, labels, value in exporter.collect(mapping, lambda path, _timeout: answers[path]):
        held.add(name, labels, value)
    scrape = yaml.safe_load(oh_metrics.prometheus_yml(House(copy.deepcopy(inventory)), HOST, conf))
    job = next(job for job in scrape["scrape_configs"] if job["job_name"] == oh_metrics.DEVICE_JOB)
    for target in job["static_configs"]:
        labels = {key: value for key, value in target["labels"].items() if not key.startswith("__")}
        held.add("probe_success", {"job": oh_metrics.DEVICE_JOB, "instance": target["targets"][0], **labels}, 1, age=70)
    recorded, _ = alerts.checks(mapping)
    seen = set()
    for rule in recorded:
        labels = rule["labels"]
        place = (labels.get("device", ""), labels["check"], labels.get("zone", ""), labels["room"])
        # A check a room may need has two rules, of which one has a result
        # at a time: here the room needs it.
        if place not in seen:
            seen.add(place)
            held.add(alerts.CHECK, labels, 0, age=5)
    return held


def run(*argv, reader=refuse, installed=False):
    """(exit status, stdout, stderr) of `thermite ARGV` on a host with or
    without the monitoring services, reading through `reader` alone."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(oh_paths, "require_compatible"), \
            mock.patch.object(device, "installation", return_value=(HOST, installed)), \
            mock.patch.object(oh_deploy, "http_get", reader), \
            mock.patch.object(oh_deploy, "lan_address", return_value="192.0.2.50"), \
            mock.patch.object(device, "clock", lambda: NOW):
        code = oh_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def facts(inventory: dict, ident: str, held: Prometheus | None = None) -> dict:
    """The report for a device of `inventory`, offline or over `held`."""
    with mock.patch.object(oh_deploy, "lan_address", return_value="192.0.2.50"), \
            mock.patch.object(device, "clock", lambda: NOW):
        return device.report(House(copy.deepcopy(inventory)), ident, HOST, held is not None, 5,
                             get=held.get if held else refuse)


def houses() -> dict[str, dict]:
    """Fixture houses with every shape a device has: on a display path, a
    matrix always needed, a receiver, a speaker needed while it plays, a
    source commanded through another device's Thing, a pinged device."""
    from tests import test_generated_output, test_metrics, test_read_only
    return {"example": copy.deepcopy(test_metrics.INVENTORY), "speakers": test_metrics.with_speakers(),
            "routed": copy.deepcopy(test_generated_output.ROUTED),
            "cec": test_read_only.house("bk808_cec", "control"),
            "ping": test_read_only.house("network_ping", "observe"),
            "ir": test_read_only.house("ir", "control")}


class Guarded(unittest.TestCase):
    def setUp(self):
        # Whatever a test forgets, the verb reaches no host from here.
        for target, name, stand_in in ((oh_deploy, "http_get", refuse), (oh_deploy, "metrics_installed", lambda: False)):
            patch = mock.patch.object(target, name, stand_in)
            patch.start()
            self.addCleanup(patch.stop)


class Offline(Guarded):
    def test_the_house_file_alone_says_what_it_is_and_what_watches_it_and_that_now_needs_the_installation(self):
        code, out, err = run("device", TV)
        self.assertEqual((code, err), (0, ""))
        for line in ("Example Tv (example_tv)", "  tv in Example West, at 192.0.2.10",
                     "  observed through sony_rest; commanded through sony_ip_control",
                     "  Example West: display for Player (input hdmi1), Computer (input hdmi2)",
                     "Things generated for it (1)", "  Readings that say whether it answers",
                     "    port: TCP port 80. Failing, it is shown as \"Control port closed\".",
                     "      needed while Example West uses Player or Computer",
                     "  This host does not run the house's monitoring, so what holds now is not known here. Run",
                     "    journalctl -u thermite-openhab.service -g 'command device=example_tv '"):
            self.assertIn(line + "\n", out)
        self.assertRegex(out, r"example_tv_power +Switch +Power +kept as power\n")
        self.assertIn("Where to look next (the commands run on the host the house is installed on)\n", out)
        # No page of a host this one is not, and nothing said of now.
        self.assertNotIn("http://", out)
        self.assertNotRegex(out, r"\bOK\b|FAILING|ago\b")

    def test_a_path_that_carries_a_picture_alone_says_so_and_uses_no_speaker(self):
        from tests import test_metrics
        inventory = copy.deepcopy(test_metrics.INVENTORY)
        inventory["devices"].append({"id": "example_amp", "room": "example_east", "kind": "streaming_amp",
                                     "make": "WiiM", "model": "Amp Pro", "ip": "192.0.2.41",
                                     "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}})
        east = inventory["routing"]["rooms"]["example_east"]
        east["sources"].append("example_player")
        east["display"]["sources"] = {"example_player": "hdmi1"}
        east["audio"] = "example_amp"
        east["carries"] = {"example_computer": "display"}
        with mock.patch.object(House, "load", classmethod(lambda cls, path: House(copy.deepcopy(inventory)))):
            code, out, err = run("device", "example_display")
            self.assertEqual((code, err), (0, ""))
            self.assertIn("  Example East: display for Computer (input hdmi2, display only), Player (input hdmi1)\n",
                          out)
            code, amp, err = run("device", "example_amp")
            self.assertEqual((code, err), (0, ""))
        # The room's Volume and Mute drive it only where the source carries sound.
        self.assertIn("  Example East: volume for Player\n", amp)

    def test_json_is_the_same_facts_as_data(self):
        code, out, _ = run("device", TV, "--json")
        found = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(found, json.loads(json.dumps(facts(houses()["example"], TV))))
        self.assertEqual(found["now"]["state"], device.ELSEWHERE)
        self.assertEqual(sorted(found), ["id", "is", "next", "now", "watched"])
        # Text is that report and nothing else.
        self.assertEqual(run("device", TV)[1], device.text(found))

    def test_a_device_nothing_watches_is_told_so_plainly(self):
        _, out, _ = run("device", COMPUTER)
        self.assertIn("  Readings that say whether it answers\n    none\n", out)
        self.assertIn("    none: nothing alerts when this device stops answering\n", out)
        self.assertIn("  Note: example_computer: it has an address and nothing reads whether it answers; list", out)
        self.assertIn("  Its problem Item does not exist: no rule of thermite's commands it.\n", out)
        self.assertIn("  Example West: source for Computer\n", out)

    def test_an_unknown_id_exits_2_naming_the_nearest(self):
        code, out, err = run("device", "example_tvv")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, f"thermite device: example_tvv is not a device id in {HOUSE}; did you mean example_tv?\n")
        code, _, err = run("device", "zzz")
        self.assertEqual(code, 2)
        self.assertIn("; its devices are example_tv, example_player,", err)

    def test_a_broken_house_file_exits_2_naming_the_file(self):
        code, _, err = run("device", TV, "--house", str(AREA / "no-such.yaml"))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith(f"thermite device: {AREA / 'no-such.yaml'}"), err)

    def test_the_installation_is_asked_only_where_the_monitoring_services_are(self):
        with mock.patch.object(oh_deploy, "metrics_installed", return_value=True):
            self.assertEqual(device.installation(), (HOST, True))
        self.assertEqual(device.installation(), (HOST, False))
        with mock.patch.object(oh_host, "load", side_effect=oh_host.HostError("no host facts")), \
                mock.patch.object(oh_deploy, "metrics_installed", return_value=True):
            self.assertEqual(device.installation(), (None, False))
        held = facts(houses()["example"], TV)["now"]
        self.assertEqual(sorted(held), ["state", "text"])


class Now(Guarded):
    def test_a_healthy_device_reads_each_value_its_age_and_each_checks_state(self):
        held = installation(houses()["example"])
        code, out, err = run("device", TV, reader=held.get, installed=True)
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"What holds now \(Prometheus on this host, 20\d\d-\d\d-\d\d \d\d:\d\d; each age")
        for pattern in (r"Thing http:url:example_tv +ONLINE +10 s ago\n",
                        r"sony_ip_control's own reads of it +it answers +10 s ago\n",
                        r"TCP port 80 +accepts connections +70 s ago\n",
                        r"Power \(example_tv_power\) +ON +10 s ago\n",
                        r"Volume \(example_tv_volume\) +30 +10 s ago\n",
                        r"Input \(example_tv_input\) +\S+ +10 s ago\n",
                        r"port +OK +needed now by Example West +5 s ago\n",
                        r"reads +OK +needed now by Example West +5 s ago\n",
                        r"  Alerts about it\n    none firing or pending\n",
                        r"  Problem\n    none \(10 s ago\)\n",
                        r"  Commands its rules took, since each rule last loaded\n    sony_ip_control  applied 7\n"):
            self.assertRegex(out, pattern)
        self.assertIn("    http://192.0.2.50:%d/d/thermite-devices?var-room=example_west&var-device=example_tv\n"
                      % oh_metrics.ports(HOST)["grafana"], out)
        self.assertIn("    http://192.0.2.50:%d/settings/things/http:url:example_tv\n" % HOST["http_port"], out)
        self.assertNotIn("routing ticket", out, "this house has no routing executor to log one")
        # Two questions (what exists now, and when each sample was taken), each
        # a GET the stand-in checked; a third only while it holds a problem.
        self.assertEqual(len(held.asked), 2)

    def test_a_failing_check_a_room_needs_and_its_alert_are_the_installations_own_and_exit_1(self):
        held = installation(houses()["example"], things={"http:url:example_tv": "OFFLINE"})
        held.drop("probe_success", device=TV)
        held.add("probe_success", {"job": oh_metrics.DEVICE_JOB, "device": TV, "port": 80}, 0, age=95)
        held.drop(alerts.CHECK, device=TV, check="port")
        place = {"room": WEST, "zone": WEST, "device": TV, "check": "port", "required": "true", "scope": "control"}
        held.add(alerts.CHECK, place, 1, age=5)
        held.add("ALERTS", {**place, "alertname": "ThermiteCheckFailing", "alertstate": "firing",
                            "severity": "critical"}, 1)
        held.add("ALERTS_FOR_STATE", {**place, "alertname": "ThermiteCheckFailing", "severity": "critical"},
                 NOW - 3600)
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 1)
        title, text = alerts.CATALOG["ThermiteCheckFailing"]
        self.assertRegex(out, r"Thing http:url:example_tv +not ONLINE \(OFFLINE\) +10 s ago\n")
        self.assertRegex(out, r"TCP port 80 +closed, or nothing answers there +95 s ago\n")
        self.assertRegex(out, r"port +FAILING \(Control port closed\) +needed now by Example West +5 s ago\n")
        self.assertRegex(out, r"    FIRING \(critical\) since 20\d\d-\d\d-\d\d \d\d:\d\d: %s \(Control port closed, "
                              r"Example West\)\n" % re.escape(title))
        self.assertIn(text.split(",")[0][:60], " ".join(out.split()))
        found = facts(houses()["example"], TV, held)
        self.assertEqual(found["now"]["alerts"], [{
            "name": "ThermiteCheckFailing", "state": "firing", "severity": alerts.severity("ThermiteCheckFailing"),
            "title": title, "text": text, "check": "port", "check_text": alerts.CHECK_WORDS["port"],
            "room": "Example West", "zone": "Example West", "since": int(NOW - 3600)}])
        # A check that fails while no room needs it is shown, and is no failure of the verb's.
        held.drop(alerts.CHECK, device=TV, check="port")
        held.add(alerts.CHECK, {**place, "required": "false"}, 1)
        held.drop("ALERTS", device=TV)
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"port +failing +not needed now by Example West, so no failure")
        self.assertNotIn("FAILING", out)
        # A pending alert is shown as pending, and fires nothing yet.
        held.add("ALERTS", {**place, "alertname": "ThermiteControlUnknown", "alertstate": "pending",
                            "severity": "warning"}, 1)
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 0)
        self.assertIn("    PENDING (warning): %s (Control port closed, Example West)\n"
                      % alerts.CATALOG["ThermiteControlUnknown"][0], out)

    def test_its_problem_is_said_in_the_codes_words_with_when_it_last_read_clear_and_that_it_may_be_old(self):
        held = installation(houses()["example"], states={"example_tv_problem": "refused"})
        held.clear = NOW - 5 * 3600
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        flat = " ".join(out.split())
        self.assertEqual(code, 0, "what a command left behind fails nothing")
        self.assertRegex(flat, r"Problem Example Tv %s \(code refused\); it last read clear 20\d\d-\d\d-\d\d \d\d:\d\d\. "
                               r"A command's problem stays until the device takes a command, so this may be old"
                               % _commands.CODES["refused"])
        self.assertEqual(len(held.asked), 3, "its history is asked only while it holds a problem")
        self.assertEqual(facts(houses()["example"], TV, held)["now"]["problem"], {
            "holds": True, "code": "refused", "words": _commands.CODES["refused"], "clear_at": int(NOW - 5 * 3600),
            "window": "14d", "age": 10})
        held.clear = None
        self.assertIn("it has not read clear in the last 14d.", " ".join(run("device", TV, reader=held.get,
                                                                             installed=True)[1].split()))
        # Text a rule should never write there is no label: it reads as the code for anything else.
        held = installation(houses()["example"], states={"example_tv_problem": "free text <b>"})
        self.assertEqual(facts(houses()["example"], TV, held)["now"]["problem"]["code"], "other")
        # An exporter from before the code was exported: said, not guessed.
        held.drop("thermite_device_problem_info")
        self.assertIn("it holds a problem code this installation's exporter does not name yet",
                      " ".join(run("device", TV, reader=held.get, installed=True)[1].split()))

    def test_a_reading_that_is_not_there_is_unknown_and_never_fine(self):
        house = houses()["example"]
        held = installation(house, states={"example_tv_power": "OFF", "example_tv_volume": "UNDEF",
                                           "example_tv_input": "NULL"})
        # Its rule asks a TV in standby nothing: no reading of its reads.
        held.drop("thermite_device_read_ok", device=TV)
        held.drop(alerts.CHECK, device=TV, check="reads")
        held.add(alerts.CHECK, {"room": WEST, "zone": WEST, "device": TV, "check": "reads", "required": "true",
                                "scope": "control"}, -1)
        held.drop(alerts.CHECK, device=TV, check="power")
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"sony_ip_control's own reads of it +no reading: unknown\n")
        self.assertRegex(out, r"Power \(example_tv_power\) +OFF +10 s ago\n")
        self.assertRegex(out, r"Volume \(example_tv_volume\) +none: the device is off\n")
        self.assertRegex(out, r"reads +unknown +needed now by Example West")
        self.assertRegex(out, r"power +unknown +no result in Prometheus: the deployed rules do not have it")
        found = facts(house, TV, held)["now"]
        self.assertEqual([(row["kind"], row["answers"], row["words"]) for row in found["signals"]][1],
                         ("reads", None, None))
        self.assertEqual({row["check"]: (row["state"], row["result"]) for row in found["checks"]},
                         {"port": ("ok", True), "reads": ("unknown", True), "power": ("unknown", False)})
        # A check the deployed rules have and this checkout does not is shown
        # by its name, with its state and no words made up for it.
        held.add(alerts.CHECK, {"room": WEST, "zone": WEST, "device": TV, "check": "older", "required": "true",
                                "scope": "control"}, 1)
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"older +FAILING +needed now by Example West")
        self.assertNotIn("None", out)

    def test_while_prometheus_does_not_answer_everything_now_is_unknown_and_the_verb_exits_2(self):
        for name in ("down", "broken"):
            held = installation(houses()["example"])
            setattr(held, name, True)
            with self.subTest(prometheus=name):
                code, out, _ = run("device", TV, reader=held.get, installed=True)
                self.assertEqual(code, 2)
                now = out.split("What holds now\n", 1)[1].split("\nWhere to look next", 1)[0]
                self.assertIn("did not answer", now)
                self.assertIn("so what holds now is unknown: no reading,", " ".join(now.split()))
                self.assertNotRegex(now, r"\bOK\b|none firing|ONLINE")
                # What the house file says is still all there.
                self.assertIn("  Readings that say whether it answers\n", out)
                self.assertEqual(sorted(facts(houses()["example"], TV, held)["now"]), ["state", "text", "url"])

    def test_with_no_check_results_at_all_checks_and_alerts_are_unknown_and_the_verb_exits_1(self):
        held = installation(houses()["example"])
        held.drop(alerts.CHECK)
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 1)
        self.assertIn("Prometheus has no check results at all: its rules are not loaded or are not being", out)
        self.assertRegex(out, r"port +unknown +no result in Prometheus")
        self.assertIn("  Alerts about it\n    unknown\n", out)
        self.assertNotRegex(out.split("What holds now", 1)[1], r"\bOK\b|none firing")
        # The exporter not reading the runtime is the house-wide check's to say.
        held = installation(houses()["example"])
        held.drop(alerts.CHECK, check="runtime")
        held.add(alerts.CHECK, {"room": "house", "device": "openhab", "check": "runtime", "required": "true"}, 1)
        self.assertIn("The exporter's reading of the runtime is failing (the house-wide runtime check)",
                      " ".join(run("device", TV, reader=held.get, installed=True)[1].split()))

    def test_a_device_the_running_installation_does_not_have_is_said_so(self):
        held = installation(houses()["example"])
        held.series = [row for row in held.series if row[1].get("device") != TV]
        code, out, _ = run("device", TV, reader=held.get, installed=True)
        self.assertEqual(code, 0)
        self.assertIn("The running installation reports no such device: this house file has it", out)
        self.assertRegex(out, r"Thing http:url:example_tv +no reading: unknown\n")
        self.assertRegex(out, r"  Problem\n    no reading: unknown\n")

    def test_what_routing_met_at_a_device_it_commands_is_routings_own_count(self):
        routed = houses()["routed"]
        held = installation(routed)
        place = {"device": "example_avr", "room": "example_cinema", "kind": "avr"}
        for name in ("thermite_routing_endpoint_failures_total", "thermite_routing_device_quarantined"):
            held.drop(name)
        for reason, count in (("refused", 3), ("unsent", 0), ("probe_failed", 1)):
            held.add("thermite_routing_endpoint_failures_total", {**place, "reason": reason}, count)
        held.add("thermite_routing_device_quarantined", place, 1)
        _, out, _ = run_house(routed, "example_avr", held)
        self.assertIn("  Routing operations that failed at it, by reason, since the routing script last loaded\n"
                      "    probe failed 1, refused 3\n"
                      "  Routing has quarantined its commands after an uncertain outcome.\n", out)
        self.assertIn("    journalctl -u thermite-openhab.service -g 'routing ticket .*resource=example_avr_'\n", out)
        found = facts(routed, "example_avr", held)["now"]
        self.assertEqual((found["routing_failures"], found["quarantined"]), ({"refused": 3, "probe_failed": 1}, True))
        # Nothing of routing is said of a device it does not command.
        held = installation(houses()["example"])
        self.assertNotIn("outing", run("device", TV, reader=held.get, installed=True)[1])
        self.assertIsNone(facts(houses()["example"], TV, held)["now"]["routing_failures"])

    def test_routing_infrastructure_is_needed_always_and_a_speaker_while_it_plays(self):
        held = installation(houses()["speakers"])
        _, out, _ = run_house(houses()["speakers"], MATRIX, held)
        self.assertIn("      needed always: routing depends on it\n", out)
        self.assertRegex(out, r"thing +OK +needed now +5 s ago\n")
        # A source commanded through the matrix's Thing has its reading, though none is named after it.
        cec = houses()["cec"]
        _, out, _ = run_house(cec, "den_device", installation(cec))
        self.assertIn("Things generated for it (0)\n  Its commands go through http:url:den_device_cec:den_matrix, "
                      "a Thing named after another device.\n", out)
        self.assertRegex(out, r"Thing http:url:den_device_cec:den_matrix +ONLINE +10 s ago\n")
        self.assertIn("/settings/things/http:url:den_device_cec:den_matrix\n", out)
        _, out, _ = run_house(houses()["speakers"], "example_speaker", held)
        self.assertIn("      needed while it plays, or did in the last %s\n" % alerts.IN_USE, out)
        self.assertIn("  none: no room's source passes through it\n", out)
        self.assertRegex(out, r"\(example_speaker_\w+\) +a track +10 s ago\n")


def run_house(inventory: dict, ident: str, held: Prometheus):
    """run() for a device of another fixture house."""
    with mock.patch.object(House, "load", classmethod(lambda cls, path: House(copy.deepcopy(inventory)))):
        return run("device", ident, reader=held.get, installed=True)


def leaves(found: dict) -> set[str]:
    return {key if not isinstance(value, dict) else f"{key}.{inner}"
            for key, value in found.items() for inner in (value if isinstance(value, dict) else [None])}


class ClosedWorld(Guarded):
    """Every fact the verb gives is looked up from the function, table or
    series that owns it. A fact computed here a second time would sooner or
    later differ from what alerts: these tests are what stops one."""

    def test_every_field_of_every_report_has_an_owner_and_every_owner_a_field(self):
        seen = set()
        for name, inventory in houses().items():
            held = installation(inventory)
            down = Prometheus()
            down.down = True
            for entry in inventory["devices"]:
                for reader in (None, held, down):
                    found = facts(inventory, entry["id"], reader)
                    with self.subTest(house=name, device=entry["id"]):
                        self.assertLessEqual(leaves(found), set(device.OWNERS))
                    seen |= leaves(found)
        self.assertEqual(seen, set(device.OWNERS))

    def test_what_the_house_says_of_each_fixture_device_is_what_its_owner_says(self):
        for name, inventory in houses().items():
            house = House(copy.deepcopy(inventory))
            conf = oh_render.build(House(copy.deepcopy(inventory)))
            mapping = oh_metrics.exporter_map(House(copy.deepcopy(inventory)), conf)
            probes, _ = oh_metrics.probes(House(inventory), conf)
            manifest = remote_diagnostics.manifest(House(copy.deepcopy(inventory)), conf)
            unread = oh_metrics.unobserved(House(inventory), conf)
            for entry in house.devices:
                ident = entry["id"]
                found = facts(inventory, ident)
                what, watched = found["is"], found["watched"]
                with self.subTest(house=name, device=ident):
                    self.assertEqual((what["label"], what["room"], what["kind"], what["address"]),
                                     (house.label(entry), {"id": entry["room"], "label": house.room_label(entry["room"])},
                                      entry["kind"], entry.get("ip")))
                    controls = entry.get("controls") or {}
                    self.assertEqual((what["observe"], what["control"]),
                                     (controls.get("observe") or [], controls.get("control") or []))
                    self.assertEqual([thing["uid"] for thing in what["things"]],
                                     [uid for uid, _ in oh_metrics.device_things(conf, ident)])
                    self.assertEqual([(item["name"], item["kept"]) for item in what["items"]],
                                     [(item, (mapping["items"].get(item) or {}).get("point"))
                                      for item, _ in conf.points(ident)])
                    # On a path exactly where health_paths() has it, as what the Signal view shows it.
                    self.assertEqual({(use["zone"], use["source"]) for use in what["paths"]},
                                     {(path["zone"], source) for path in mapping["paths"].values()
                                      for source, used in path["sources"].items() if ident in used})
                    for use in what["paths"]:
                        path = manifest["rooms"][use["zone"]]["sources"][use["source"]]
                        roles = {node["role"] for node in [*path["nodes"], *path.get("audioNodes", [])]
                                 if node["device"] == ident}
                        self.assertIn(use["role"], roles or {"volume"})
                    signals = oh_metrics.signals(entry, conf, probes)
                    self.assertEqual(
                        [(row["kind"], row["what"], row["through"], row["check"], row["series"]["metric"],
                          row["series"]["labels"]) for row in watched["signals"]],
                        [(signal.kind, oh_metrics.signal_words(signal)[0], list(signal.integrations), signal.control,
                          signal.metric, dict(signal.labels)) for signal in signals])
                    checks = [check for check in mapping["health_checks"] if check["device"] == ident]
                    self.assertEqual(
                        [(row["check"], row["fails_as"], row["series"]["metric"], row["series"]["labels"],
                          row["always"], row["optional"], len(row["needed"])) for row in watched["checks"]],
                        [(check["check"], alerts.CHECK_WORDS[check["check"]], check["metric"], check["labels"],
                          check["always"], check["optional"], len(check["required"])) for check in checks])
                    self.assertEqual(watched["notes"], [oh_metrics.unobserved_text(*row) for row in unread
                                                        if row[0] == ident])
                    self.assertEqual(watched["tallies"], sorted(
                        row["integration"] for row in (mapping.get("commands") or {}).values()
                        if row["device"] == ident))
                    for row in (*watched["signals"], *watched["checks"]):
                        self.assertTrue(row["series"]["meaning"], row)

    def test_what_holds_now_is_each_series_value_in_its_owners_words(self):
        for name, inventory in houses().items():
            conf = oh_render.build(House(copy.deepcopy(inventory)))
            probes, _ = oh_metrics.probes(House(inventory), conf)
            held = installation(inventory)
            # Every other signal reads that the device does not answer.
            flipped = set()
            for entry in inventory["devices"]:
                for index, signal in enumerate(oh_metrics.signals(entry, conf, probes)):
                    if index % 2:
                        held.put(signal.metric, signal.bad, **dict(signal.labels))
                        flipped.add((signal.metric, signal.labels))
            for entry in inventory["devices"]:
                found = facts(inventory, entry["id"], held)["now"]
                signals = oh_metrics.signals(entry, conf, probes)
                with self.subTest(house=name, device=entry["id"]):
                    self.assertEqual(
                        [(row["kind"], row["what"], row["answers"], row["words"]) for row in found["signals"]],
                        [(signal.kind, oh_metrics.signal_words(signal)[0],
                          (signal.metric, signal.labels) not in flipped,
                          oh_metrics.signal_words(signal)[2 if (signal.metric, signal.labels) in flipped else 1])
                         for signal in signals])
                    results = [row for row in held.series if row[0] == alerts.CHECK
                               and row[1].get("device") == entry["id"]]
                    self.assertEqual(sorted((row["check"], row["state"], row["needed"]) for row in found["checks"]),
                                     sorted((labels["check"], device.CHECK_STATES[value], labels["required"] == "true")
                                            for _, labels, value, _ in results))
                    self.assertEqual(found["runtime"], device.OK)
                    self.assertTrue(found["deployed"])

    def test_a_checks_states_are_the_values_the_alert_rules_compare_with(self):
        rules = {rule["alert"]: rule["expr"] for group in alerts.rules({})["groups"] for rule in group["rules"]
                 if "alert" in rule}
        self.assertTrue(rules["ThermiteCheckFailing"].endswith(" == 1"))
        self.assertTrue(rules["ThermiteControlUnknown"].endswith(" == -1"))
        self.assertEqual(device.CHECK_STATES, {1.0: device.FAILING, 0.0: device.OK, -1.0: device.UNKNOWN})

    def test_the_verb_reads_only_series_the_installation_has_and_writes_no_rule_of_its_own(self):
        source = (AREA / "lib/oh_cli/device.py").read_text()
        code = source.split('"""', 2)[2].split("OWNERS = {", 1)[0] + source.split("\n}\n", 1)[1]
        named = set(re.findall(r'"(thermite_[a-z_]+|probe_success|ALERTS(?:_FOR_STATE)?)"', code))
        self.assertLessEqual({name for name in named if name.startswith("thermite_")}, set(exporter.FAMILIES))
        self.assertEqual({name for name in named if not name.startswith("thermite_")},
                         {"probe_success", "ALERTS", "ALERTS_FOR_STATE"})
        # It asks Prometheus in three places and compares no reading with a threshold of its own.
        self.assertEqual(len(re.findall(r"\bask\(get, base, ", code)), 3)
        self.assertNotRegex(code, r"probe_success\s*[=!<>]|for:\s|\bincrease\(|\brate\(")
        # No series is named to a person: only what it means.
        for inventory, ident in ((houses()["example"], TV), (houses()["speakers"], "example_speaker")):
            text = device.text(facts(inventory, ident, installation(inventory)))
            self.assertNotRegex(text, r"thermite_[a-z]|thermite:|probe_success|ALERTS")


class AskedOfPrometheus(unittest.TestCase):
    """The verb's own questions, put to the pinned Prometheus' evaluator
    beside the example house's rules: what the stand-in above answers by
    hand is what Prometheus answers."""

    @classmethod
    def setUpClass(cls):
        from tests import test_alert_scenarios as scenarios
        reason = scenarios.unavailable()
        if reason:
            raise unittest.SkipTest(reason)
        cls.scenarios, cls.podman, cls.image = scenarios, shutil.which("podman"), scenarios.image()

    def evaluate(self, inputs, values):
        self.scenarios.Scenarios.run_rules(self, inputs, values=values)

    def test_one_question_gives_every_series_of_the_device_under_its_name_and_the_runtimes_check(self):
        series = self.scenarios.series
        inputs = {**self.scenarios.healthy(10),
                  # A probe's own result and its scrape differ in nothing but the name.
                  series("up", job=oh_metrics.DEVICE_JOB, device=TV, port="80"): "1x10",
                  series("thermite_device_problem", device=TV): "0x10",
                  series("thermite_device_problem", device=PLAYER): "1x10",
                  series("thermite_device_info", device=TV, room=WEST, kind="tv", label="Example Tv"): "1x10"}
        # A Thing named after another device, as elsewhere() asks for one: its
        # two series differ in more than the name, or `or` would keep one.
        uid = "http:url:example_matrix"
        thing = next(each["labels"] for each in self.scenarios.house()[1]["health_checks"]
                     if each["labels"].get("thing") == uid)
        inputs[series("thermite_thing_status_info", **thing, status="ONLINE")] = "1x10"
        more = [f'{name}{{thing="{uid}"}}' for name in ("thermite_thing_online", "thermite_thing_status_info")]
        counted = {"up": 1, "probe_success": 1, "thermite_device_read_ok": 1, "thermite_device_problem": 1,
                   "thermite_device_info": 1,
                   # Its three checks, which no room needs now, and the runtime's.
                   alerts.CHECK: 4}
        self.evaluate(inputs, [
            ("5m", f"count by (__name__) ({device.everything(TV, [])})",
             [(f'{{__name__="{name}"}}', count) for name, count in counted.items()]),
            ("5m", f"count by (__name__) ({device.everything(TV, more)})",
             [(f'{{__name__="{name}"}}', count)
              for name, count in {**counted, "thermite_thing_online": 1, "thermite_thing_status_info": 1}.items()]),
            ("5m", f'count(({device.everything(TV, [])}) unless {device.own(TV)})', [("{}", 1)]),
        ])

    def test_when_its_problem_last_read_clear_is_the_last_step_its_gauge_was_0(self):
        series = self.scenarios.series
        through = self.scenarios.through
        inputs = {**self.scenarios.healthy(60),
                  # The TV's room was renamed after two minutes: its gauge has
                  # two series in the window, and one answer.
                  series("thermite_device_problem", device=TV, room="before"): through((2, 0), (59, None)),
                  series("thermite_device_problem", device=TV, room=WEST): through((2, None), (8, 0), (51, 1)),
                  series("thermite_device_problem", device=PLAYER): "1x60",
                  series("thermite_device_problem", device=MATRIX): "0x60"}
        self.evaluate(inputs, [
            ("60m", device.clear_question(TV), [("{}", 300)]),
            ("60m", device.clear_question(PLAYER), []),
            ("60m", device.clear_question(MATRIX), [("{}", 3600)]),
        ])


class Registered(unittest.TestCase):
    def test_it_is_a_devices_verb_of_the_selected_site(self):
        verb = oh_cli.VERBS["device"]
        self.assertEqual((verb.group, verb.site), ("devices", True))
        usage = oh_cli.parser_of("device").format_help()
        self.assertIn("thermite device [-h] [--config DIR] [--house FILE] [--timeout SECONDS] [--json]", usage)
        self.assertIn("is unknown", usage)


if __name__ == "__main__":
    unittest.main()
