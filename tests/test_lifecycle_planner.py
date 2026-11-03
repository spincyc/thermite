"""Exercise the production event-driven lifecycle planner, without device I/O."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))


@unittest.skipUnless(shutil.which("node"), "node is required for the production JavaScript planner")
class LifecyclePlanner(unittest.TestCase):
    def test_production_coordinator_invariants_and_failure_boundaries(self):
        harness = Path(__file__).with_name("lifecycle_planner_harness.js")
        result = subprocess.run([shutil.which("node"), str(harness)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertGreaterEqual(report["checks"], 20)
        self.assertEqual(report["failed"], 0)
