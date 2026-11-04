"""System navigation and explicit specialist-tool access use finite fake I/O."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class SystemUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(AREA / "tests/system_ui_harness.js")], capture_output=True,
                                text=True, timeout=20)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        cls.proof = json.loads(result.stdout)

    def test_endpoints_are_credential_free_and_follow_site_port_overrides(self):
        self.assertTrue(self.proof["safeEndpoints"])

    def test_system_metadata_is_admin_or_explicit_native_only_and_late_reads_are_fenced(self):
        self.assertTrue(self.proof["gatedMetadata"] and self.proof["cancelledReads"])

    def test_tool_launch_requires_explicit_selection_and_loopback_tools_stay_local(self):
        self.assertTrue(self.proof["noAutomaticLaunch"] and self.proof["localToolsStayLocal"])
        self.assertEqual(self.proof["commands"], 0)

    def test_f10_and_empty_room_sites_can_reach_system(self):
        self.assertTrue(self.proof["systemKeyboard"] and self.proof["emptyRooms"])

    def test_browser_disclosure_starts_closed_and_metadata_resumes_once_after_hiding(self):
        self.assertTrue(self.proof["browserLifecycle"])
