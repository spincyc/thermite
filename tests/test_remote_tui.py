"""The terminal remote's Remotes screen and its other screens
(tui/remote/controller.js, view.js), driven by keys and the mouse as a user
drives them (tests/remote_tui_harness.js).

The house is test_listen_tui's: the public example's two routed rooms, the
east one watching and listening, and two rooms of speakers alone, the den
and the lounge. On the mock, on openHAB itself (--native) and on the
gateway, each through a stand-in. Nothing leaves the process.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_listen_tui import DEN, LOUNGE, ROOM as EAST, model

AREA = Path(__file__).resolve().parents[1]
WEST = "example_west"


@unittest.skipUnless(shutil.which("node"), "node needed for the terminal remote")
class RemoteTUI(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as root:
            house = Path(root) / "house.js"
            house.write_text("globalThis.REMOTE_HOUSE = " + json.dumps(model()) + ";\n")
            env = dict(os.environ, THERMITE_REMOTE_HOUSE_FILE=str(house))
            run = subprocess.run([shutil.which("node"), str(AREA / "tests/remote_tui_harness.js"), WEST, EAST, DEN, LOUNGE],
                                 text=True, env=env, capture_output=True, timeout=180)
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout)
        cls.proof = json.loads(run.stdout)

    def test_a_room_of_speakers_alone_reached_with_the_signal_open_reads_none(self):
        signal = self.proof["signal"]
        # ↓ through every room and on, ], the rooms sheet's ↓ and Enter, and a click: none asks a room of speakers for its signal.
        self.assertEqual(signal["walk"], [[DEN, "listen"], [LOUNGE, "listen"], [WEST, "watch"], [EAST, "watch"],
                                          [DEN, "listen"], [LOUNGE, "listen"], [DEN, "listen"]])
        self.assertEqual(signal["diagnosed"], [EAST, WEST])
        self.assertTrue(signal["shown"])

    def test_a_signal_read_that_fails_at_once_never_throws(self):
        signal = self.proof["signal"]
        self.assertEqual(signal["unknown"], [True, "unknown room: " + DEN])
        self.assertEqual(signal["thrown"], [False, EAST, "watch"])

    def test_room_settings_from_a_room_of_speakers_alone_edit_a_routed_rooms_schedule(self):
        settings = self.proof["settings"]
        self.assertEqual(settings["room"], [True, DEN])
        self.assertEqual(settings["read"], ["GET /api/v1/rooms/" + WEST + "/policy"])
        self.assertEqual((settings["sheet"], settings["form"], settings["closed"]), ("access-form", True, None))

    def test_a_key_a_click_or_a_frame_that_fails_does_not_end_the_terminal(self):
        terminal = self.proof["terminal"]
        self.assertEqual(terminal["key"], ["That did not work", "no step"])
        self.assertEqual(terminal["after"], EAST, "the next key still goes")
        self.assertEqual(terminal["click"], ["That did not work", "no room"])
        self.assertEqual(terminal["kept"], ["no step", "no room", "no frame"])
        self.assertEqual(terminal["draws"], 1)


if __name__ == "__main__":
    unittest.main()
