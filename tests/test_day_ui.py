"""The terminal's Rooms through the day, with fake gateway answers and timers."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class DayUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(AREA / "tests/day_ui_harness.js")], capture_output=True,
                                text=True, timeout=20)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        cls.proof = json.loads(result.stdout)

    def test_answers_are_checked_and_drawn_by_majority_of_time(self):
        self.assertTrue(self.proof["validated"] and self.proof["columns"])

    def test_reads_while_open_refresh_and_give_up_bounded(self):
        self.assertTrue(self.proof["refresh"] and self.proof["bounded"])

    def test_h_opens_it_from_home_and_remotes_and_it_operates_nothing(self):
        self.assertTrue(self.proof["keyboard"])
        self.assertEqual(self.proof["commands"], 0)
