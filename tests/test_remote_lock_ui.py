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

    def test_locked_room_shows_a_padlock_on_its_lit_bubble_and_greys_the_rest(self):
        for key in ("padlockBubble", "othersInert", "noLockRow", "lockedOffNoRemote", "litTapExplains",
                    "unlitLockInWords"):
            self.assertTrue(self.proof[key], key)

    def test_schedule_lock_can_be_kept_until_unlocked_from_its_lit_bubble(self):
        self.assertTrue(self.proof["keepScheduleLock"])

    def test_room_that_is_off_shows_no_remote_until_a_device_is_picked(self):
        for key in ("offNoRemote", "offDevicePick", "offReturn"):
            self.assertTrue(self.proof[key], key)

    def test_lock_on_a_selection_switches_first_and_refuses_what_the_room_did_not_report(self):
        for key in ("lockOnCurrent", "lockOnSwitch", "lockOnMismatch", "lockOnTimeout", "midSwitchRefused",
                    "lockOnRefused"):
            self.assertTrue(self.proof[key], key)

    def test_terminal_lock_menu_opens_by_right_click_and_by_key_for_administrators_only(self):
        for key in ("terminalMenuMouse", "terminalMenuKey", "nonAdminNoMenu"):
            self.assertTrue(self.proof[key], key)


if __name__ == "__main__":
    unittest.main()
