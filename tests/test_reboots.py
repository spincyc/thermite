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
from oh_conf import House, unfilled  # noqa: E402
from oh_features import reboots  # noqa: E402
from oh_integrations import _io, ukm404_http  # noqa: E402
from tests.test_vrroom_telnet import HOUSE as VRROOM_HOUSE  # noqa: E402
from tests.test_zone_sync import HOUSE as ROUTED  # noqa: E402

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
            "example_tv.reboot: none of its integrations under control can restart it; "
            "these can: avpro_telnet, heos, ukm404_http, vrroom_telnet",
            "example_receiver.reboot: 'daily' is not nightly, when_hung or nightly_and_when_hung"])

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


def routed():
    """A fictional two-room house whose USB matrix (example_usb) switches a
    port in each room, and restarts nightly."""
    data = copy.deepcopy(ROUTED)
    device(data, "example_usb")["reboot"] = "nightly"
    return data


def den():
    """A fictional room shown by output 4 of its own AVPro matrix
    (example_avpro), which restarts nightly."""
    return {
        "version": 1,
        "rooms": {"example_den": {"label": "Example Den", "tag": "Room"}},
        "devices": [
            {"id": "example_avpro", "room": "example_den", "kind": "hdmi_matrix", "make": "AVPro Edge",
             "model": "AC-MX-44X", "ip": "192.0.2.70", "controls": {"control": ["avpro_telnet"]}, "reboot": "nightly"},
            {"id": "example_den_computer", "room": "example_den", "kind": "computer", "model": "Computer",
             "ip": "192.0.2.72"}],
        "routing": {"sources": {"example_den_computer": {"label": "Computer"}},
                    "rooms": {"example_den": {
                        "hdmi_switch": {"device": "example_avpro", "output": 4, "inputs": {"example_den_computer": 1}},
                        "sources": ["example_den_computer"]}}},
    }


class HdmiMatrix(unittest.TestCase):
    def test_an_avpro_restarts_only_while_each_room_showing_its_outputs_is_off(self):
        data = den()
        self.assertEqual(render.validate(data), [])
        self.assertEqual(reboots.table(House(data))["devices"], [{
            "id": "example_avpro", "nightly": True, "item": "example_avpro_reboot",
            "answers": [f"example_avpro_output{output}_input" for output in range(1, 5)], "things": [], "signs": [],
            "idle": [{"item": "loc_example_den_source", "is": "off"}]}])
        items = {name: item for file in render.build(House(data)).yaml.values() for name, item in file.items.items()}
        self.assertEqual(items["example_avpro_reboot"], {
            "type": "String", "label": "Reboot", "groups": ["example_avpro"], "tags": ["Control"], "autoupdate": False,
            "metadata": {"commandDescription": {"config": {"options": "reboot=Reboot"}}}})

    def test_an_avpro_no_room_shows_cannot_restart_nightly(self):
        data = den()
        del data["routing"]
        self.assertEqual(render.validate(data), [
            "example_avpro.reboot: nothing would show it idle; an HDMI matrix restarts nightly only while the rooms "
            "that show its outputs (routing.rooms.<room>.hdmi_switch) are Off, and no room lists it"])
        del device(data, "example_avpro")["reboot"]
        self.assertEqual(render.validate(data), [])

    def test_a_switch_that_reads_its_own_signal_is_not_held_by_its_room(self):
        """A desk left on its computer overnight is idle while the computer
        sleeps, which only the switch sees."""
        entry = next(entry for entry in reboots.table(House(copy.deepcopy(VRROOM_HOUSE)))["devices"]
                     if entry["id"] == "example_switch")
        self.assertEqual([check["item"] for check in entry["idle"]], ["example_switch_signal"])


def hung_desk(value="when_hung"):
    """The fictional desk whose VRROOM (example_switch) shows it, restarting
    when its web API hangs."""
    data = copy.deepcopy(VRROOM_HOUSE)
    device(data, "example_switch")["reboot"] = value
    return data


class WhenHung(unittest.TestCase):
    def test_a_vrroom_restarts_when_its_web_api_stays_offline_while_its_room_is_off(self):
        data = hung_desk()
        self.assertEqual(render.validate(data), [])
        entry = next(entry for entry in reboots.table(House(data))["devices"] if entry["id"] == "example_switch")
        self.assertIs(entry["nightly"], False)
        # Five missed polls of a minute each: a dropped request is no hang.
        self.assertEqual(entry["hung"], {"things": ["http:url:example_switch"], "seconds": 300,
                                         "idle": [{"item": "loc_example_desk_source", "is": "off"}]})
        both = next(entry for entry in reboots.table(House(hung_desk("nightly_and_when_hung")))["devices"]
                    if entry["id"] == "example_switch")
        self.assertEqual((both["nightly"], both["hung"]), (True, entry["hung"]))
        nightly = next(entry for entry in reboots.table(House(hung_desk("nightly")))["devices"]
                       if entry["id"] == "example_switch")
        self.assertNotIn("hung", nightly)

    def test_only_a_device_a_room_shows_and_whose_hang_is_known_restarts_when_hung(self):
        data = hung_desk()
        del data["routing"]
        self.assertEqual(render.validate(data), [
            "example_switch.reboot: nothing would show it idle; a device restarts when hung only while the rooms "
            "that show it (routing.rooms.<room>.hdmi_switch) or whose ports it switches (routing.rooms.<room>.usb) "
            "are Off, and no room lists it"])
        data = house()
        device(data, "example_receiver")["reboot"] = "when_hung"
        self.assertEqual(render.validate(data), [
            "example_receiver.reboot: heos cannot tell when it hangs, so it can only be nightly"])
        # Its web API is what shows the hang.
        data = hung_desk()
        device(data, "example_switch")["controls"]["observe"] = []
        self.assertEqual(render.validate(data), [
            "example_switch.reboot: needs vrroom_http under observe, which tells when the switch's input has no signal"])

    def test_the_rule_looks_for_hangs_only_in_a_house_with_a_device_that_restarts_when_hung(self):
        text = render.render(House(hung_desk()))[reboots.RULE]
        line = text[text.index("const TABLE = "):]
        self.assertEqual(json.loads(line[len("const TABLE = "):line.index(";\n")]), reboots.table(House(hung_desk())))
        self.assertTrue(any("hung" in entry for entry in reboots.table(House(hung_desk()))["devices"]))
        self.assertFalse(any("hung" in entry for entry in reboots.table(House(house()))["devices"]))


def usb_thing(files):
    return yaml.safe_load(files[ukm404_http.FILE])["things"]["http:url:example_usb"]


class UsbMatrix(unittest.TestCase):
    def test_a_usb_matrix_restarts_only_while_each_room_it_switches_is_off(self):
        data = routed()
        self.assertEqual(render.validate(data), [])
        self.assertEqual(reboots.table(House(data))["devices"], [{
            "id": "example_usb", "nightly": True, "item": "example_usb_reboot",
            "answers": ["example_usb_device1_host"], "things": ["http:url:example_usb"], "signs": [],
            "idle": [{"item": "loc_example_cinema_source", "is": "off"},
                     {"item": "loc_example_lounge_source", "is": "off"}],
            "console": {"host": "192.0.2.21", "port": 8000, "ready": "initialization finished!", "polled": 12}}])

    def test_a_usb_matrix_no_room_switches_cannot_restart_nightly(self):
        data = routed()
        for spec in data["routing"]["rooms"].values():
            spec.pop("usb")
            spec.pop("usb_unused", None)
        self.assertEqual(render.validate(data), [
            "example_usb.reboot: nothing would show it idle; a USB matrix restarts nightly only while the rooms "
            "whose ports it switches (routing.rooms.<room>.usb) are Off, and no room lists it"])
        del device(data, "example_usb")["reboot"]
        self.assertEqual(render.validate(data), [])

    def test_its_reboot_item_writes_past_the_routing_actor(self):
        files = render.render(House(routed()))
        self.assertIn("automation/js/thermite_routing_io_example_usb.js", files)
        channel = usb_thing(files)["channels"]["reboot"]
        self.assertEqual(channel["config"]["mode"], "WRITEONLY")
        self.assertEqual(channel["config"]["commandTransformation"], [ukm404_http.REBOOT])
        item = yaml.safe_load(files[ukm404_http.FILE])["items"]["example_usb_reboot"]
        self.assertEqual(item["channel"], "http:url:example_usb:reboot")
        self.assertIs(item["autoupdate"], False)

    def test_the_read_only_copy_polls_nothing_more(self):
        files = render.read_only(render.render(House(routed())))
        thing = usb_thing(files)
        modes = {channel["config"]["mode"] for channel in thing["channels"].values()}
        polls = {(channel["config"].get("stateExtension"), channel["config"].get("stateContent"))
                 for channel in thing["channels"].values()}
        self.assertEqual((modes, polls), ({"READONLY"}, {(None, '{"comhead":"get_matrix_status","language":0}')}))
        self.assertEqual(thing["channels"]["reboot"]["config"]["stateTransformation"], [ukm404_http.NOTHING])
        # A command would still refresh that poll (HttpThingHandler 5.2.3), and
        # the reboots rule runs in the read-only copy too: the Item passes none.
        item = yaml.safe_load(files[ukm404_http.FILE])["items"]["example_usb_reboot"]
        self.assertNotIn("channel", item)
        self.assertEqual(set(item["channels"]), {"http:url:example_usb:reboot"})
        self.assertTrue(render.safe_link("http:url:example_usb:reboot", item["channels"]["http:url:example_usb:reboot"],
                                         set()))

    @unittest.skipUnless(shutil.which("node"), "node is required to run the reboot channel's transformation")
    def test_only_reboot_builds_a_request_and_it_is_the_vendor_pages(self):
        script = ukm404_http.REBOOT[len("JS(|"):-1]
        commands = ["reboot", "REFRESH", "Reboot", "reboot ", "", "set_system_reboot"]
        program = (f"const run = (input) => ({script});\n"
                   f"console.log(JSON.stringify({json.dumps(commands)}.map(run)));")
        result = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['{"comhead":"set_system_reboot"}', None, None, None, None, None])


class Render(unittest.TestCase):
    def test_the_rule_its_item_and_each_reboot_item(self):
        conf = render.build(House(house()))
        files = conf.render()
        text = files[reboots.RULE]
        self.assertNotIn(unfilled("TABLE"), text)
        line = text[text.index("const TABLE = "):]
        self.assertEqual(json.loads(line[len("const TABLE = "):line.index(";\n")]), reboots.table(House(house())))
        self.assertEqual(conf.yaml[reboots.ITEMS].items, {reboots.ITEM: {
            "type": "String", "label": "Reboot a device", "icon": "switch", "autoupdate": False}})
        items = {name: item for file in conf.yaml.values() for name, item in file.items.items()}
        for ident in ("example_receiver", "example_small_receiver", "example_switch"):
            self.assertEqual(items[f"{ident}_reboot"]["metadata"], {"commandDescription": {"config": {"options": "reboot=Reboot"}}})
            self.assertIs(items[f"{ident}_reboot"]["autoupdate"], False)
        # It listens on a USB matrix's console through the bounded I/O; the
        # read-only copy opens none.
        self.assertIn("const thermiteIO = ", text)
        self.assertNotIn(reboots.RULE, render.ITEM_ONLY_RULES)
        self.assertEqual(render.read_only(files)[reboots.RULE],
                         text.replace("\nconst SEND = true;\n", "\nconst SEND = false;\n"))

    def test_a_house_with_nothing_to_restart_has_no_rule(self):
        files = render.render(House(house(devices=[TV])))
        self.assertNotIn(reboots.RULE, files)
        self.assertNotIn(reboots.ITEMS, files)

    def test_the_receivers_rule_knows_their_addresses(self):
        text = render.render(House(house()))["automation/js/heos.js"]
        line = next(line for line in text.splitlines() if line.startswith("const DEVICES = "))
        self.assertEqual([entry["host"] for entry in json.loads(line[len("const DEVICES = "):-1])],
                         ["192.0.2.50", "192.0.2.51"])


@unittest.skipUnless(shutil.which("node"), "node is required for the production reboots rule")
class Rule(unittest.TestCase):
    def test_the_production_rule(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/reboots_harness.js"), str(reboots.SOURCE),
                                 str(_io.SOURCE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 16)

    def test_rooms_off_includes_hdmi_matrix_and_unread_idle_guards_vrroom(self):
        entry = next(entry for entry in reboots.table(House(copy.deepcopy(VRROOM_HOUSE)))["devices"]
                     if entry["id"] == "example_switch")
        self.assertEqual(entry["unreadIdle"], [{"item": "loc_example_desk_source", "is": "off"}])
        data = copy.deepcopy(VRROOM_HOUSE)
        matrix = {"id": "example_matrix", "room": "example_west", "kind": "hdmi_matrix", "ip": "192.0.2.88",
                  "controls": {"control": ["avpro_telnet"]}, "reboot": "nightly"}
        data["devices"].append(matrix)
        data["routing"]["hdmi_matrix"] = "example_matrix"
        data["routing"]["rooms"]["example_desk"]["hdmi_output"] = 1
        data["routing"]["rooms"]["example_east"] = {"hdmi_audio_output": 2, "hdmi_output": 3}
        self.assertEqual(reboots.rooms_off(data, matrix, {"rooms": True}),
                         [{"item": "loc_example_desk_source", "is": "off"},
                          {"item": "loc_example_east_source", "is": "off"}])


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

    def test_the_list_says_which_restart_when_hung_and_whether_the_last_was_a_hang(self):
        def line(value, **devices):
            lines = []
            self.assertEqual(cli.listing(House(hung_desk(value)), FakeOpenHAB(state(**devices)), log=lines.append), 0)
            return next(text for text in lines if "(example_switch)" in text)
        hung = {"at": "2026-11-14T09:00:42.000Z", "by": "hung", "outcome": "back", "seconds": 18}
        local = datetime.datetime(2026, 11, 14, 9, 0, 42, tzinfo=datetime.timezone.utc).astimezone()
        self.assertEqual(line("when_hung", example_switch=hung),
                         f"HDFury VRROOM (example_switch): when hung; last (it had hung): back after 18 s, "
                         f"{local:%Y-%m-%d %H:%M}")
        self.assertEqual(line("nightly_and_when_hung"),
                         "HDFury VRROOM (example_switch): nightly at 03:00 and when hung; not restarted yet")

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

    def test_the_list_and_a_restart_as_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "devices.yaml"
            path.write_text(yaml.safe_dump(house()))

            def verb(http, *argv):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                        mock.patch.object(cli.pair, "openhab", return_value=http):
                    code = cli.main([*argv, "--house", str(path), "--json"])
                self.assertEqual((err.getvalue(), out.getvalue().count("\n")), ("", 1))
                return code, json.loads(out.getvalue())
            last = {"at": "2026-11-14T09:00:42.000Z", "by": "schedule", "outcome": "back", "seconds": 18}
            self.assertEqual(verb(FakeOpenHAB(state(example_switch=last))), (0, {"devices": [
                {"id": "example_receiver", "label": "Marantz CINEMA 60", "nightly": True, "time": "03:00",
                 "when_hung": False, "last": None},
                {"id": "example_small_receiver", "label": "Marantz CINEMA 30", "nightly": False, "time": None,
                 "when_hung": False, "last": None},
                {"id": "example_switch", "label": "HDFury VRROOM", "nightly": True, "time": "03:00",
                 "when_hung": False, "last": last}]}))
            skipped = {"at": now(30), "by": "command", "outcome": "skipped", "detail": "example_switch_signal RX0 is 1080p"}
            http = FakeOpenHAB(state(example_switch=last), state(example_switch=skipped))
            self.assertEqual(verb(http, "example_switch"), (1, {
                "device": "example_switch", "outcome": "skipped", "detail": skipped["detail"], "seconds": None,
                "at": skipped["at"], "timed_out": False}))
            self.assertEqual(http.commands, [("POST", "/rest/items/house_reboot", "example_switch")])
            self.assertEqual(verb(FakeOpenHAB("NULL"), "example_receiver", "--timeout", "0"),
                             (1, {"device": "example_receiver", "timed_out": True, "timeout": 0.0}))


if __name__ == "__main__":
    unittest.main()
