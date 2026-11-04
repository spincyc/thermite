"""Room lock display and control gating in the shared web/terminal remote code."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class RemoteLockUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(AREA / "tests/remote_lock_harness.js")],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        cls.proof = json.loads(result.stdout)

    def test_terminal_k_key_locks_the_room_and_session_refuses_ordinary_controls(self):
        self.assertTrue(self.proof["terminalLockKey"] and self.proof["sharedGating"])

    def test_schedule_end_and_refused_toggle_stay_visible(self):
        self.assertTrue(self.proof["scheduleEnd"] and self.proof["inlineError"])

    def test_guest_refusal_keeps_access_and_failed_poll_keeps_last_answer(self):
        self.assertTrue(self.proof["guestRefusalKeepsAccess"] and self.proof["policyRefresh"])


if __name__ == "__main__":
    unittest.main()
