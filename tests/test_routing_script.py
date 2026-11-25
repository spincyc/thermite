"""Exercise the production routing.js template around a stub planner and executor."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
from oh_features import routing  # noqa: E402
from oh_integrations import _instr as instr, avpro_telnet, bk808_http, ukm404_http, vrroom_telnet  # noqa: E402
from tests.test_hotplug import house as hotplug_house  # noqa: E402


class MissedReads(unittest.TestCase):
    """routing.js holds a room's input through one missed read of its HDMI
    output or HDMI switch, for one and a half of its device's polls."""

    def test_each_room_holds_its_output_and_switch_for_one_and_a_half_polls(self):
        table = routing.table(House(hotplug_house()))
        self.assertEqual(table["reads"], {"example_matrix_output1_input": 15000, "example_switch_input": 15000,
                                          "example_avpro_output4_input": 15000})

    def test_the_polls_are_the_devices_own(self):
        self.assertEqual((bk808_http.POLL_S, ukm404_http.POLL_S), (instr.REFRESH, instr.REFRESH))
        for module in (avpro_telnet, vrroom_telnet):
            self.assertIn(f"const POLL = '0/{module.POLL_S} * * * * ?';", module.SOURCE.read_text())


@unittest.skipUnless(shutil.which("node"), "node is required for the production routing script")
class RoutingScript(unittest.TestCase):
    def test_route_claims_first_and_hands_recovery_proof_to_the_planner(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/routing_script_harness.js"),
                                 str(AREA / "lib/oh_features/routing/routing.js")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 22)


class RoomActions(unittest.TestCase):
    """Room on_actions/off_actions stay distinct from readback problem ACTIONS."""

    def test_room_actions_are_validated_and_rendered_without_clobbering_readback_actions(self):
        self.assertEqual(routing.ROOM_ACTIONS, ("on_actions", "off_actions"))
        self.assertEqual(routing.ACTIONS, ("read selected input", "read matrix output input", "read power", routing.DETECT))
        data = hotplug_house()
        data["devices"].append({"id": "example_curtain", "room": "example_desk", "kind": "curtain", "ip": "192.0.2.49"})
        room = data["routing"]["rooms"]["example_desk"]
        room["on_actions"] = {"example_curtain": "close"}
        room["off_actions"] = {"example_curtain": "open"}
        self.assertEqual(routing.validate(data), [])
        house = House(data)
        self.assertEqual(routing.after(house, "example_desk", "example_computer"),
                         [["example_curtain_action", "close"]])
        self.assertIn(["example_curtain_action", "open"], routing.after(house, "example_desk", routing.OFF))
        table = routing.table(house)["rooms"]["example_desk"]["after"]
        self.assertEqual(table["example_computer"], [["example_curtain_action", "close"]])
        self.assertIn(["example_curtain_action", "open"], table[routing.OFF])
        # Validation checks device ids and rejects unquoted YAML booleans.
        room["on_actions"] = {"missing_device": True}
        room["off_actions"] = {}
        problems = routing.validate(data)
        self.assertTrue(any("on_actions.missing_device" in p and "not a device id" in p for p in problems), problems)
        self.assertTrue(any("on_actions.missing_device" in p and "quote it" in p for p in problems), problems)
        self.assertTrue(any("off_actions: must map devices" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
