"""The terminal remote's Audio screen (tui/remote), which draws the Audio
page's session (web/remote/audio-session.js) and opens with u from Remotes.

The model is the public example with two fixture WiiM amplifiers: one is
example_east's `audio`, so that room's lock is its own, and one is in a room
no routed room covers, its Play Mode cut to four short choices so that a
choice also draws as a row of options. A second run uses the public example
alone, which has no audio device. Mock backend only; nothing leaves the
process.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
from oh_features import remote_model  # noqa: E402

OWNED, FREE, ROOM, DEN = "fixture_amp", "fixture_den_amp", "example_east", "fixture_den"
WIIM = {"kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro",
        "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}


def model():
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["devices"].append({"id": OWNED, "room": ROOM, "ip": "192.0.2.41", **WIIM})
    data["routing"]["rooms"][ROOM]["audio"] = OWNED
    data["rooms"][DEN] = {"label": "Fixture Den", "tag": "Room"}
    data["devices"].append({"id": FREE, "room": DEN, "ip": "192.0.2.42", **WIIM})
    built = remote_model.build(House(data))
    mode = next(setting for setting in built["devices"][FREE]["audio"]["settings"]
                if setting["item"] == FREE + "_play_mode")
    mode["options"] = [option for option in mode["options"] if option[0] in ("4", "0", "1", "3")]
    return built


def harness(mode, house_file=None):
    env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
    if house_file:
        env["THERMITE_REMOTE_HOUSE_FILE"] = str(house_file)
    run = subprocess.run([shutil.which("node"), str(AREA / "tests/audio_tui_harness.js"), mode, OWNED, FREE, ROOM],
                         text=True, env=env, capture_output=True, timeout=60)
    if run.returncode:
        raise AssertionError(run.stderr or run.stdout)
    return json.loads(run.stdout)


@unittest.skipUnless(shutil.which("node"), "node needed for the terminal remote")
class AudioTUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as root:
            house = Path(root) / "house.js"
            house.write_text("globalThis.REMOTE_HOUSE = " + json.dumps(model()) + ";\n")
            cls.proof = harness("fixture", house)
        cls.plain = harness("plain")

    def test_u_opens_audio_from_remotes_only(self):
        reach = self.proof["reach"]
        self.assertEqual(reach["remote"], "audio")
        self.assertEqual((reach["home"], reach["security"], reach["cameras"], reach["usersU"], reach["pairingU"]),
                         ("home", "security", "cameras", "users", "pairing"))
        self.assertEqual(reach["remoteCtrlU"], "remote")
        self.assertEqual(reach["audioU"], "audio")
        self.assertEqual(reach["again"], "audio")

    def test_f6_stays_with_remotes_and_f8_reaches_users_from_anywhere(self):
        reach = self.proof["reach"]
        self.assertEqual((reach["remoteF6"], reach["remoteCtrlP"], reach["pairingEsc"]), ("pairing", "pairing", "remote"))
        self.assertEqual((reach["homeF6"], reach["audioF6"]), ("home", "audio"))
        self.assertEqual((reach["homeF8"], reach["remoteF8"], reach["audioF8"]), ("users", "users", "users"))

    def test_esc_returns_to_remotes(self):
        self.assertEqual(self.proof["reach"]["audioEsc"], "remote")
        self.assertEqual(self.plain["back"], "remote")

    def test_remotes_offers_u_only_with_an_audio_device(self):
        self.assertTrue(self.proof["hint"])
        self.assertFalse(self.plain["hint"])

    def test_it_opens_on_the_rooms_audio_device_and_draws_its_page(self):
        self.assertEqual(self.proof["opened"], OWNED)
        self.assertEqual(self.proof["drawn"], [], "missing from the screen")
        self.assertTrue(self.proof["places"], "each place heads its devices")
        self.assertEqual(self.proof["focus"], "audio-key:play_pause")
        for kind in ("audio-device", "audio-key", "audio-volume", "audio-mute", "audio-input", "audio-switch",
                     "audio-level", "audio-choose"):
            self.assertIn(kind, self.proof["kinds"])
        self.assertTrue(self.proof["nextDrawn"], "a short choice draws its options")
        self.assertTrue(self.proof["help"])

    def test_keys_send_the_page_commands_and_the_screen_shows_what_was_read_back(self):
        keys = self.proof["keys"]
        self.assertTrue(keys["volume"] and keys["muted"] and keys["played"], keys)
        self.assertEqual(keys["set"], {"eq": True, "level": True, "preset": True, "volume": True, "input": True,
                                       "whenHides": True})
        self.assertEqual(self.proof["sheet"], {"kind": "audio-choice", "focus": "audio-option:" + OWNED + "_eq_preset:Flat",
                                               "lists": True, "closed": True, "focusBack": "audio-set:" + OWNED + "_eq_preset"})
        self.assertEqual(self.proof["spaceOnLevel"], [])
        self.assertEqual(self.proof["sent"], [
            [OWNED, "volume", "61"], [OWNED, "mute", "ON"], [OWNED, "key", "play_pause"],
            [OWNED, OWNED + "_eq", "ON"],
            [OWNED, OWNED + "_sub_level", "1"], [OWNED, OWNED + "_sub_level", "2"], [OWNED, OWNED + "_sub_level", "1"],
            [OWNED, "volume", "60"], [OWNED, OWNED + "_eq_preset", "Acoustic"], [OWNED, "input", "bluetooth"],
            [FREE, FREE + "_play_mode", "0"]])
        self.assertEqual((self.proof["next"], self.proof["back"]), (FREE, OWNED))
        self.assertTrue(self.proof["nextChose"])

    def test_a_locked_room_greys_its_device_and_its_commands_only_toast(self):
        lock = self.proof["lock"]
        self.assertTrue(lock["said"] and lock["padlock"], lock)
        self.assertEqual(lock["toasts"], ["Example East: Locked by Example East"] * 4)
        self.assertEqual(lock["sent"], [])
        self.assertEqual(lock["other"], [[FREE, "volume", "61"]])

    def test_without_an_audio_device_the_screen_says_so_and_sends_nothing(self):
        self.assertEqual(self.plain["screen"], "audio")
        self.assertTrue(self.plain["empty"])
        self.assertEqual(self.plain["sent"], [])


if __name__ == "__main__":
    unittest.main()
