"""Nightly reboots (oh_features.reboots): which devices the house file may
restart, in what order and when, and the rule that restarts them.

A fictional house with documentation addresses. The production rule runs
against stub openHAB and a fake clock in tests/reboots_harness.js.
"""
import contextlib
import copy
import datetime
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_render as render  # noqa: E402
import yaml  # noqa: E402
from oh_cli import reboot as cli  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import reboots  # noqa: E402

SWITCH = {"id": "example_switch", "room": "example_desk", "kind": "hdmi_switch", "make": "HDFury", "model": "VRROOM",
          "ip": "192.0.2.40", "controls": {"observe": ["vrroom_http"], "control": ["vrroom_telnet"]}, "reboot": "nightly"}
RECEIVER = {"id": "example_receiver", "room": "example_lounge", "kind": "avr", "make": "Marantz", "model": "CINEMA 60",
            "ip": "192.0.2.50", "heos_pid": 101, "reboot": "nightly",
            "controls": {"observe": ["denonmarantz_http"], "control": ["denonmarantz_http", "heos"]}}
SMALL = {"id": "example_small_receiver", "room": "example_lounge", "kind": "avr", "make": "Marantz",
         "model": "CINEMA 30", "ip": "192.0.2.51", "heos_pid": 102, "zones": 1,
         "controls": {"observe": ["denonmarantz_http"], "control": ["denonmarantz_http", "heos"]}}
TV = {"id": "example_tv", "room": "example_lounge", "kind": "tv", "make": "Sony", "model": "XR-65A80L",
      "ip": "192.0.2.60", "controls": {"observe": ["network_ping"]}}
HOUSE = {
    "version": 1,
    "rooms": {"example_desk": {"label": "Example Desk", "tag": "Room"},
              "example_lounge": {"label": "Example Lounge", "tag": "Room"}},
    "devices": [SWITCH, TV, RECEIVER, SMALL],
}


def house(**changes):
    data = copy.deepcopy(HOUSE)
    data.update(changes)
    return data


def device(data, ident):
    return next(d for d in data["devices"] if d["id"] == ident)


class Validation(unittest.TestCase):
    def test_the_fictional_house_is_valid(self):
        self.assertEqual(render.validate(house()), [])
        self.assertEqual(render.validate(house(reboots={"time": "23:59"})), [])

    def test_the_time_is_a_24_hour_clock_time(self):
        for setting, problem in (
                ({"time": "3:00"}, "reboots.time: '3:00' is not a 24-hour HH:MM time, such as \"03:00\""),
                ({"time": "24:00"}, "reboots.time: '24:00' is not a 24-hour HH:MM time, such as \"03:00\""),
                ({"time": 300}, "reboots.time: 300 is not a 24-hour HH:MM time, such as \"03:00\""),
                ({"at": "03:00"}, "reboots: takes only time, such as {time: \"03:00\"}"),
                ("03:00", "reboots: takes only time, such as {time: \"03:00\"}")):
            self.assertEqual(render.validate(house(reboots=setting)), [problem])

    def test_only_a_device_an_integration_can_restart_opts_in_and_only_nightly(self):
        data = house()
        device(data, "example_tv")["reboot"] = "nightly"
        device(data, "example_receiver")["reboot"] = "daily"
        self.assertEqual(render.validate(data), [
            "example_tv.reboot: none of its integrations under control can restart it; these can: heos, vrroom_telnet",
            "example_receiver.reboot: 'daily' is not nightly"])

    def test_a_switch_restarts_only_with_its_web_page_to_say_it_is_idle(self):
        data = house()
        device(data, "example_switch")["controls"]["observe"] = []
        self.assertEqual(render.validate(data), [
            "example_switch.reboot: needs vrroom_http under observe, which tells when the switch's input has no signal"])


class Table(unittest.TestCase):
    def test_receivers_restart_before_switches_and_every_capable_device_is_listed(self):
        table = reboots.table(House(house()))
        self.assertEqual([(entry["id"], entry["nightly"]) for entry in table["devices"]],
                         [("example_receiver", True), ("example_small_receiver", False), ("example_switch", True)])
        self.assertEqual({key: table[key] for key in ("item", "command", "time", "cron")},
                         {"item": "house_reboot", "command": "reboot", "time": "03:00", "cron": "0 0 3 * * ?"})
        self.assertEqual(reboots.table(House(house(reboots={"time": "02:30"})))["cron"], "0 30 2 * * ?")

    def test_each_device_says_how_it_restarts_answers_and_shows_it_is_idle(self):
        entries = {entry["id"]: entry for entry in reboots.table(House(house()))["devices"]}
        self.assertEqual(entries["example_receiver"], {
            "id": "example_receiver", "nightly": True, "item": "example_receiver_reboot",
            "answers": ["example_receiver_main_power"], "things": ["denonmarantz:avr:example_receiver"], "signs": [],
            "idle": [{"item": "example_receiver_main_power", "is": "OFF"},
                     {"item": "example_receiver_zone2_power", "is": "OFF"}]})
        self.assertEqual(entries["example_small_receiver"]["idle"],
                         [{"item": "example_small_receiver_main_power", "is": "OFF"}])
        self.assertEqual(entries["example_switch"], {
            "id": "example_switch", "nightly": True, "item": "example_switch_reboot",
            "answers": ["example_switch_input"], "things": [], "signs": [],
            "idle": [{"item": "example_switch_signal", "field": "RX0", "is": "no signal", "maxAge": 125,
                      "unreadIsIdle": True}]})


class Render(unittest.TestCase):
    def test_the_rule_its_item_and_each_reboot_item(self):
        conf = render.build(house())
        files = conf.render()
        text = files[reboots.RULE]
        self.assertNotIn(reboots.MARK, text)
        line = text[text.index("const TABLE = "):]
        self.assertEqual(json.loads(line[len("const TABLE = "):line.index(";\n")]), reboots.table(House(house())))
        self.assertEqual(conf.yaml[reboots.ITEMS].items, {reboots.ITEM: {
            "type": "String", "label": "Reboot a device", "icon": "switch", "autoupdate": False}})
        items = {name: item for file in conf.yaml.values() for name, item in file.items.items()}
        for ident in ("example_receiver", "example_small_receiver", "example_switch"):
            self.assertEqual(items[f"{ident}_reboot"]["metadata"], {"commandDescription": {"config": {"options": "reboot=Reboot"}}})
            self.assertIs(items[f"{ident}_reboot"]["autoupdate"], False)
        # It commands Items only: the read-only copy keeps it as it is.
        self.assertIn(reboots.RULE, render.ITEM_ONLY_RULES)
        self.assertEqual(render.read_only(files)[reboots.RULE], text)

    def test_a_house_with_nothing_to_restart_has_no_rule(self):
        files = render.render(house(devices=[TV]))
        self.assertNotIn(reboots.RULE, files)
        self.assertNotIn(reboots.ITEMS, files)

    def test_the_receivers_rule_knows_their_addresses(self):
        text = render.render(house())["automation/js/heos.js"]
        line = next(line for line in text.splitlines() if line.startswith("const DEVICES = "))
        self.assertEqual([entry["host"] for entry in json.loads(line[len("const DEVICES = "):-1])],
                         ["192.0.2.50", "192.0.2.51"])


@unittest.skipUnless(shutil.which("node"), "node is required for the production reboots rule")
class Rule(unittest.TestCase):
    def test_the_production_rule(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/reboots_harness.js"), str(reboots.SOURCE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 6)


class FakeOpenHAB:
    """house_reboot's state, which the rule moves on once commanded: each
    read after the command takes the next of `states`."""

    def __init__(self, before, *states):
        self.state, self.states, self.commands = before, list(states), []

    def try_get_json(self, path):
        self.reads = getattr(self, "reads", [])
        self.reads.append(path)
        if self.commands and self.states:
            self.state = self.states.pop(0)
        return 200, {"state": self.state}

    def request(self, method, path, body, headers):
        self.commands.append((method, path, body.decode()))


def state(**devices):
    return json.dumps({"version": 1, "devices": devices})


def now(seconds=0):
    at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Command(unittest.TestCase):
    def test_a_reboot_waits_for_the_rules_outcome_after_the_command(self):
        yesterday = {"at": now(-86400), "by": "schedule", "outcome": "back", "seconds": 40}
        http = FakeOpenHAB(state(example_receiver=yesterday), state(example_receiver=yesterday),
                           state(example_receiver={"at": now(), "by": "command", "outcome": "rebooting"}),
                           state(example_receiver={"at": now(30), "by": "command", "outcome": "back", "seconds": 30}))
        lines = []
        self.assertEqual(cli.restart(House(house()), "example_receiver", http, 60, log=lines.append, sleep=lambda _: None), 0)
        self.assertEqual(http.commands, [("POST", "/rest/items/house_reboot", "example_receiver")])
        self.assertEqual(lines[0], "Restarting Marantz CINEMA 60 (example_receiver) once it is idle; "
                                   "waiting for it to answer again.")
        self.assertRegex(lines[1], r"^example_receiver: back after 30 s, \d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_a_skipped_or_lost_reboot_fails(self):
        skipped = FakeOpenHAB("NULL", state(example_switch={"at": now(), "by": "command", "outcome": "skipped",
                                                            "detail": "example_switch_signal RX0 is 1080p, not no signal"}))
        lines = []
        self.assertEqual(cli.restart(House(house()), "example_switch", skipped, 60, log=lines.append, sleep=lambda _: None), 1)
        self.assertTrue(lines[1].startswith("example_switch: skipped (example_switch_signal RX0 is 1080p, not no signal), "))
        ticks = iter(range(100))
        silent = FakeOpenHAB("NULL")
        self.assertEqual(cli.restart(House(house()), "example_switch", silent, 5, log=lines.append,
                                     clock=lambda: next(ticks), sleep=lambda _: None), 1)
        self.assertEqual(lines[-1], "example_switch: no outcome within 5 s; thermite reboot lists it once there is one")

    def test_the_list_says_which_restart_nightly_and_how_each_last_went(self):
        http = FakeOpenHAB(state(example_switch={"at": "2026-11-14T09:00:42.000Z", "by": "schedule", "outcome": "back",
                                                 "seconds": 18}))
        lines = []
        self.assertEqual(cli.listing(House(house()), http, log=lines.append), 0)
        local = datetime.datetime(2026, 11, 14, 9, 0, 42, tzinfo=datetime.timezone.utc).astimezone()
        self.assertEqual(lines, [
            "Marantz CINEMA 60 (example_receiver): nightly at 03:00; not restarted yet",
            "Marantz CINEMA 30 (example_small_receiver): on command only; not restarted yet",
            f"HDFury VRROOM (example_switch): nightly at 03:00; last: back after 18 s, {local:%Y-%m-%d %H:%M}"])

    def test_only_a_device_thermite_can_restart_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "devices.yaml"
            path.write_text(yaml.safe_dump(house()))
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.object(cli.pair, "openhab") as openhab:
                self.assertEqual(cli.main(["example_tv", "--house", str(path)]), 2)
            openhab.assert_not_called()
        self.assertEqual(err.getvalue(), "thermite reboot: not a device thermite can restart: example_tv "
                                         "(those are: example_receiver, example_small_receiver, example_switch)\n")


if __name__ == "__main__":
    unittest.main()
