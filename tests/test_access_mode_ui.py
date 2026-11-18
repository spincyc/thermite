"""The shared web/terminal access client and its account pages use the finite gateway boundary."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class AccessModeUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(AREA / "tests/access_mode_harness.js")],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        cls.proof = json.loads(result.stdout)

    def test_guest_terminal_uses_gateway_csrf_without_credentials_or_native_fallback(self):
        self.assertTrue(self.proof["guestGateway"] and self.proof["terminalCsrf"] and self.proof["noFallback"])

    def test_guest_remote_controls_stay_available_once_accounts_exist(self):
        self.assertTrue(self.proof["enabledGuest"])

    def test_the_first_administrator_clears_authority_and_rejects_inflight_old_responses(self):
        self.assertTrue(self.proof["transitionFence"])

    def test_bootstrap_elevation_and_expired_cookie_recovery_are_explicit(self):
        self.assertTrue(self.proof["bootstrapForms"] and self.proof["staleRecovery"])

    def test_terminal_security_and_schedules_explain_the_same_authority(self):
        self.assertTrue(self.proof["terminalSecurity"])

    def test_guest_listener_pages_are_guest_only_and_sign_in_on_https(self):
        self.assertTrue(self.proof["guestListener"])
