"""Truthful per-hop signal reports in the real shared clients, entirely offline."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is required for the shared signal clients")
class SignalReporting(unittest.TestCase):
    def test_projection_acquisition_session_and_terminal(self):
        result = subprocess.run([NODE, str(AREA / "tests/signal_reporting_harness.js")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        proof = json.loads(result.stdout)
        self.assertEqual(proof["passed"], 15)
        self.assertEqual(len(set(proof["cases"])), proof["passed"])


if __name__ == "__main__":
    unittest.main()
