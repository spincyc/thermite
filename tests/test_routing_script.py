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
        self.assertEqual(json.loads(result.stdout)["passed"], 20)


if __name__ == "__main__":
    unittest.main()
