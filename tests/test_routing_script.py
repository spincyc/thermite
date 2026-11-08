"""Exercise the production routing.js template around a stub planner and executor."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node is required for the production routing script")
class RoutingScript(unittest.TestCase):
    def test_route_claims_first_and_recovers_only_executor_quarantine(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/routing_script_harness.js"),
                                 str(AREA / "lib/oh_features/routing/routing.js")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 12)
