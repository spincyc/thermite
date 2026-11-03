"""Recovery snapshots remain bounded while commands and native events flow."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is required for the actual shared backend")
class BackendSnapshots(unittest.TestCase):
    def test_bounded_recovery_cancellation_and_command_dispatch(self):
        run = subprocess.run([NODE, str(AREA / "tests/backend_snapshot_harness.js"), str(AREA)],
                             text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        proof = json.loads(run.stdout)
        self.assertEqual(len(proof["cases"]), 14)
        self.assertEqual((proof["actualNetworkRequests"], proof["actualDeviceCommands"]), (0, 0))
