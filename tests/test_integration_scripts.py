"""Exercise production HEOS, IR, CEC, Android TV and AVPro scripts against stub openHAB and Java types."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
SCRIPTS = ("heos.js", "globalcache_ir.js", "bk808_cec.js", "androidtv_googletv/androidtv_googletv.js",
           "avpro_telnet/avpro_telnet.js")


@unittest.skipUnless(shutil.which("node"), "node is required for the production integration scripts")
class IntegrationScripts(unittest.TestCase):
    def test_receiver_wire_format_key_drops_pairing_recovery_and_native_volume(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/integration_scripts_harness.js"),
                                 *(str(AREA / "lib/oh_integrations" / name) for name in SCRIPTS)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 6)
