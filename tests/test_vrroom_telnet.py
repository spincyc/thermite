"""The HDFury VRROOM: controlled over telnet (oh_integrations.vrroom_telnet),
observed over its web API for diagnostics only (vrroom_http), and its
telnet stand-in (oh_matrix_sim.VRROOM).

A fictional house with documentation addresses. The production rule runs
against a scripted switch in tests/vrroom_telnet_harness.js.
"""
import copy
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_integrations as integrations  # noqa: E402
import oh_matrix_sim as sim  # noqa: E402
import oh_render as render  # noqa: E402
from oh_conf import RULE_SENDS, RULE_SILENT, Conf, House, YamlFile  # noqa: E402
from oh_features import routing  # noqa: E402
from oh_features.routing import lifecycle_io  # noqa: E402
from oh_integrations import _io, vrroom_http, vrroom_telnet  # noqa: E402
from oh_room_policy import expected_plan  # noqa: E402
from oh_testing import routing as routing_check  # noqa: E402

DESK = "example_desk"
SWITCH = {"id": "example_switch", "room": DESK, "kind": "hdmi_switch", "make": "HDFury", "model": "VRROOM",
          "ip": "192.0.2.40", "controls": {"observe": ["vrroom_http"], "control": ["vrroom_telnet"]}}
HOUSE = {
    "version": 1,
    "rooms": {DESK: {"label": "Example Desk", "tag": "Room"}},
    "devices": [
        {"id": "example_matrix", "room": DESK, "kind": "hdmi_matrix", "make": "BZBGEAR", "model": "BK-808",
         "ip": "192.0.2.41", "controls": {"control": ["bk808_http"]}},
        SWITCH,
        {"id": "example_computer", "room": DESK, "kind": "computer", "model": "Computer", "ip": "192.0.2.42"},
        {"id": "example_laptop", "room": DESK, "kind": "computer", "model": "Computer", "ip": "192.0.2.43"},
    ],
    "routing": {
        "hdmi_matrix": "example_matrix", "hdmi_off_input": 8, "usb_off_host": 4,
        "sources": {"example_computer": {"label": "Computer", "hdmi_input": 1},
                    "example_laptop": {"label": "Laptop"}},
        "rooms": {DESK: {"hdmi_output": 1, "sources": ["example_computer", "example_laptop"],
                         "hdmi_switch": {"device": "example_switch", "matrix_input": 1,
                                         "inputs": {"example_laptop": 0}}}},
    },
}


def house():
    return copy.deepcopy(HOUSE)


@unittest.skipUnless(shutil.which("node"), "node is required for the production VRROOM rule")
class Rule(unittest.TestCase):
    def test_the_production_rule_against_a_scripted_switch(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/vrroom_telnet_harness.js"),
                                 str(vrroom_telnet.SOURCE), str(_io.DEVICE)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 10)


class Rendering(unittest.TestCase):
    def test_the_house_validates_and_the_switch_is_the_telnet_integrations(self):
        data = house()
        self.assertEqual(render.validate(data), [])
        self.assertIs(routing.hdmi_switcher(SWITCH), vrroom_telnet)
        self.assertEqual(routing.table(data)["rooms"][DESK]["switch"]["item"], "example_switch_input")

    def test_control_lives_only_in_telnet(self):
        data = house()
        data["devices"][1]["controls"] = {"control": ["vrroom_http"]}
        self.assertTrue(any("vrroom_http" in problem for problem in render.validate(data)))
        for name in ("input_item", "fields", "INPUTS", "COMHEADS", "url", "CONTROL"):
            self.assertFalse(hasattr(vrroom_http, name), name)
        self.assertEqual(vrroom_http.PHASE, "observe")

    def test_the_web_api_is_a_read_only_snapshot_polled_about_once_a_minute(self):
        out = YamlFile(())
        vrroom_http.render(SWITCH, House(house()), out)
        self.assertEqual(set(out.items), {"example_switch_signal"})
        (thing,) = out.things.values()
        self.assertEqual(thing["config"]["refresh"], 60)
        self.assertEqual({channel["config"]["mode"] for channel in thing["channels"].values()}, {"READONLY"})
        window = vrroom_http.diagnostics(SWITCH)["maxAgeSeconds"]
        self.assertLess(2 * thing["config"]["refresh"] + thing["config"]["timeout"] / 1000, window)
        keys = {reading["path"][1] for reading in vrroom_http.diagnostics(SWITCH)["readings"]}
        self.assertLessEqual({"rx0in5v", "rx3in5v", "tx0hpd", "tx1hpd", "earcst"}, keys)
        self.assertLessEqual(keys, set(vrroom_http.FIELDS))

    def test_the_rule_and_its_items(self):
        data = house()
        out = YamlFile(())
        vrroom_telnet.render(SWITCH, House(data), out)
        self.assertEqual(set(out.items), {"example_switch_input", "example_switch_reboot"})
        self.assertTrue(all(item["autoupdate"] is False and "channel" not in item for item in out.items.values()))
        files = vrroom_telnet.files([SWITCH], House(data))
        text = files["automation/js/vrroom_telnet_example_switch.js"]
        self.assertEqual(text.count(RULE_SENDS), 1)
        self.assertNotIn(_io.MARK, text)
        self.assertNotIn(_io.DEVICE_MARK, text)
        self.assertEqual(text.count("const thermiteDevice = "), 1)
        line = next(line for line in text.splitlines() if line.startswith("const DEVICES = "))
        self.assertEqual(json.loads(line[len("const DEVICES = "):-1]),
                         [{"id": "example_switch", "host": "192.0.2.40", "port": 2222, "inputs": 4}])
        aimed = vrroom_telnet.aim(files, SWITCH, "127.0.0.1:40123")
        self.assertIn('"host":"127.0.0.1","port":40123', aimed["automation/js/vrroom_telnet_example_switch.js"])
        self.assertNotIn("192.0.2.40", aimed["automation/js/vrroom_telnet_example_switch.js"])
        silenced = render.read_only(files)["automation/js/vrroom_telnet_example_switch.js"]
        self.assertIn(RULE_SILENT, silenced)
        self.assertNotIn(RULE_SENDS, silenced)

    def test_a_poll_keeps_the_reading_inside_the_policy_window(self):
        source = vrroom_telnet.SOURCE.read_text(encoding="utf-8")
        self.assertIn(f"const POLL = '0/{vrroom_telnet.POLL_S} * * * * ?';", source)
        answer_s = int(re.search(r"const ANSWER_MS = (\d+);", source)[1]) / 1000
        fields = expected_plan(house(), DESK, "example_laptop").fields
        field = next(field for field in fields if field.item == "example_switch_input")
        self.assertLess(vrroom_telnet.POLL_S + answer_s, field.max_age)

    def test_the_routing_executor_drives_it_through_its_own_rule(self):
        data = house()
        conf, built = Conf(), House(data)
        vrroom_telnet.render(SWITCH, built, conf.yaml_file(vrroom_telnet.FILE, ()))
        conf.files.update(vrroom_telnet.files([SWITCH], built))
        capabilities = lifecycle_io.capabilities(data)
        self.assertEqual(capabilities["example_switch_input"]["field"],
                         {"kind": "vrroom", "allowed": ["0", "1", "2", "3"]})
        self.assertEqual(capabilities["example_switch_input"]["manual"], 2)
        lifecycle_io.configure(built, conf, {"resources": {"example_switch_input": {}}})
        text = conf.files[vrroom_telnet.rule_path(SWITCH)]
        self.assertNotIn(lifecycle_io.CUSTOM_MARK, text)
        self.assertIn("routingLifecycleActor.legacy(", text)
        config = next(line for line in text.splitlines() if line.startswith("const ROUTING_EXECUTOR = "))
        self.assertNotIn(SWITCH["ip"], config)
        aimed = vrroom_telnet.aim(conf.files, SWITCH, "127.0.0.1:40123")[vrroom_telnet.rule_path(SWITCH)]
        self.assertEqual(next(line for line in aimed.splitlines() if line.startswith("const ROUTING_EXECUTOR = ")), config)

    def test_the_routing_check_aims_the_rule_at_the_stand_in_and_counts_no_setup_as_a_write(self):
        check = routing_check.Check(house(), log=lambda _: None)
        self.addCleanup(lambda: [server.stop() for server in check.servers.values()])
        self.assertIsInstance(check.sims["example_switch"], sim.VRROOM)
        files = check.conf()
        text = files[vrroom_telnet.rule_path(SWITCH)]
        self.assertIn(f'"port":{check.servers["example_switch"].address.rpartition(":")[2]}', text)
        matrix = check.sims["example_switch"]
        for line in ("get ipinterrupt", "set ipinterrupt on", "get insel"):
            matrix.answer(matrix.parse_line(line))
        self.assertEqual(check._writes("example_switch"), 0)
        matrix.answer(matrix.parse_line("set insel 1 4"))
        self.assertEqual(check._writes("example_switch"), 1)
        self.assertLessEqual(set(matrix.comheads()), vrroom_telnet.COMHEADS)


class Lines:
    """One telnet connection to a stand-in, read line by line."""

    def __init__(self, address: str) -> None:
        host, _, port = address.rpartition(":")
        self.conn = socket.create_connection((host, int(port)), timeout=2)
        self.pending = b""

    def send(self, line: str) -> None:
        self.conn.sendall(line.encode("ascii") + b"\n")

    def read(self, timeout: float = 0.3) -> list[str]:
        """Every line that comes until none has for `timeout` seconds."""
        self.conn.settimeout(timeout)
        while True:
            try:
                chunk = self.conn.recv(4096)
            except (TimeoutError, OSError):
                chunk = None
            if not chunk:
                lines = self.pending.split(b"\r\n")
                self.pending = lines.pop()
                return [line.decode("ascii") for line in lines]
            self.pending += chunk

    def closed(self) -> bool:
        """Whether the stand-in closes the connection within 2 s, after
        whatever it still had to say."""
        self.conn.settimeout(2)
        try:
            while self.conn.recv(4096):
                pass
            return True
        except TimeoutError:
            return False
        except OSError:
            return True

    def close(self) -> None:
        self.conn.close()


class StandIn(unittest.TestCase):
    def setUp(self):
        self.vrroom = sim.VRROOM()
        self.vrroom.reboot_s = 0.5
        self.server = sim.serve(self.vrroom).start()
        self.addCleanup(self.server.stop)

    def connect(self) -> Lines:
        lines = Lines(self.server.address)
        self.addCleanup(lines.close)
        return lines

    def test_it_answers_what_it_knows_refuses_with_err_and_says_nothing_to_anything_else(self):
        _, answers = sim.exchange(self.server.address, [
            "get insel", "set insel 2 4", "get ipinterrupt", "set ipinterrupt on", "get ipinterrupt",
            "SET INSEL 1 4", "set insel 5 4", "set insel 4 4", "set ipinterrupt maybe", "get status tx0", "hello"])
        self.assertEqual(answers, [["insel 0 4"], ["insel 2 4"], ["ipinterrupt off"], ["ipinterrupt on"],
                                   ["ipinterrupt on"], [], ["insel err"], ["insel err"], ["ipinterrupt err"], [], []])
        self.assertEqual(self.vrroom.get(0), 2)
        self.assertEqual(self.vrroom.unknown, ["SET INSEL 1 4", "get status tx0", "hello"])

    def test_with_ipinterrupt_on_every_connection_hears_int_after_a_change_by_anyone(self):
        first, second = self.connect(), self.connect()
        first.send("set insel 1 4")
        self.assertEqual(first.read(), ["insel 1 4"])
        self.assertEqual(second.read(), [])
        first.send("set ipinterrupt on")
        self.assertEqual(first.read(), ["ipinterrupt on"])
        first.send("set insel 2 4")
        self.assertEqual(first.read(), ["insel 2 4", "int"])
        self.assertEqual(second.read(), ["int"])
        self.vrroom.set(0, 3)
        self.assertEqual(first.read(), ["int"])
        first.send("set insel 3 4")
        self.assertEqual(first.read(), ["insel 3 4"])

    def test_set_reboot_closes_every_connection_and_keeps_its_settings(self):
        first, second = self.connect(), self.connect()
        first.send("set ipinterrupt on")
        first.read()
        first.send("set insel 1 4")
        first.read()
        first.send("set reboot")
        self.assertEqual(first.read(), ["reboot"])
        self.assertTrue(first.closed())
        self.assertTrue(second.closed())
        self.assertEqual(sim.exchange(self.server.address, ["get insel"])[1], [[]])
        time.sleep(0.6)
        self.assertEqual(sim.exchange(self.server.address, ["get insel", "get ipinterrupt"])[1],
                         [["insel 1 4"], ["ipinterrupt on"]])
        self.assertEqual(self.vrroom.reboots, 1)

    def test_muted_it_closes_a_connection_at_its_next_line(self):
        lines = self.connect()
        lines.send("get insel")
        self.assertEqual(lines.read(), ["insel 0 4"])
        self.vrroom.muted = True
        lines.send("get insel")
        self.assertTrue(lines.closed())
        self.vrroom.muted = False
        self.assertEqual(sim.exchange(self.server.address, ["get insel"])[1], [["insel 0 4"]])


class Registry(unittest.TestCase):
    def test_the_routing_check_has_its_stand_in(self):
        self.assertIs(sim.SIMS[vrroom_telnet.NAME], sim.VRROOM)
        self.assertNotIn(vrroom_http.NAME, sim.SIMS)
        self.assertIn(vrroom_telnet, integrations.REGISTRY)
        self.assertLess(integrations.REGISTRY.index(vrroom_http), integrations.REGISTRY.index(vrroom_telnet))


if __name__ == "__main__":
    unittest.main()
