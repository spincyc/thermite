"""Watching and listening in one remote: the rooms the remote lists
(remote_model speakers and speakerRooms), and RemoteSession's modes over
RemoteListen (web/remote/listen-session.js), which both the web page and the
terminal remote draw.

The public example house with three WiiMs: a Mini and an Amp Pro in a den
no routed room covers, and an Amp Pro example_east names its `audio`. The
sessions run on the mock backends; nothing leaves the process.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
from oh_features import remote_model  # noqa: E402

MINI, AMP, OWNED, ROOM = "fixture_den_mini", "fixture_den_amp", "fixture_amp", "example_east"
WIIM = {"make": "WiiM", "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}


def house(**changes):
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["rooms"]["fixture_den"] = {"label": "Fixture Den", "tag": "Room"}
    data["devices"] += [
        {"id": MINI, "room": "fixture_den", "name": "Fixture Den Mini", "ip": "192.0.2.43", "kind": "audio_streamer", "model": "Mini", **WIIM},
        {"id": AMP, "room": "fixture_den", "name": "Fixture Den Amp", "ip": "192.0.2.42", "kind": "streaming_amp", "model": "Amp Pro", **WIIM},
        {"id": OWNED, "room": ROOM, "ip": "192.0.2.41", "kind": "streaming_amp", "model": "Amp Pro", **WIIM}]
    data["routing"]["rooms"][ROOM]["audio"] = OWNED
    for key, value in changes.items():
        value(data) if callable(value) else data.__setitem__(key, value)
    return data


def model(data=None):
    return remote_model.build(House(data or house()))


class Rooms(unittest.TestCase):
    def test_a_routed_room_lists_the_speakers_it_owns_and_the_rest_have_rooms_of_their_own(self):
        built = model()
        self.assertEqual([(room["id"], room["speakers"]) for room in built["rooms"]],
                         [("example_west", []), (ROOM, [OWNED])])
        self.assertEqual(built["speakerRooms"], [{"id": "fixture_den", "label": "Fixture Den", "speakers": [MINI, AMP]}])

    def test_a_speaker_no_routed_room_owns_in_a_routed_rooms_home_is_that_rooms(self):
        def move(data):
            data["devices"][-3]["room"] = "example_west"
        built = model(house(move=move))
        self.assertEqual(dict((room["id"], room["speakers"]) for room in built["rooms"])["example_west"], [MINI])
        self.assertEqual(built["speakerRooms"], [{"id": "fixture_den", "label": "Fixture Den", "speakers": [AMP]}])

    def test_rooms_of_speakers_alone_follow_the_house_files_order(self):
        def porch(data):
            data["rooms"] = {"fixture_porch": {"label": "Fixture Porch", "tag": "Room"}, **data["rooms"]}
            data["devices"][-2]["room"] = "fixture_porch"
        built = model(house(porch=porch))
        self.assertEqual([room["id"] for room in built["speakerRooms"]], ["fixture_porch", "fixture_den"])

    def test_a_house_without_speakers_lists_none(self):
        data = json.loads((AREA / "instance/devices.yaml").read_text())
        built = remote_model.build(House(data))
        self.assertEqual(built["speakerRooms"], [])
        self.assertTrue(all(room["speakers"] == [] for room in built["rooms"]))


@unittest.skipUnless(shutil.which("node"), "node needed for the shared web/TUI session")
class Listen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/listen_session_harness.js"), MINI, AMP, OWNED, ROOM],
                             input=json.dumps(model()), text=True, env=env, capture_output=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.out = json.loads(run.stdout)

    def test_every_room_is_listed_with_the_modes_it_has(self):
        self.assertEqual([room[:3] + [room[4]] for room in self.out["rooms"]], [
            ["example_west", "Example West", ["watch"], False],
            [ROOM, "Example East", ["watch", "listen"], False],
            ["fixture_den", "Fixture Den", ["listen"], True]])

    def test_a_room_of_speakers_says_what_they_play_as_a_routed_room_says_its_source(self):
        listed = dict(self.out["listed"])
        self.assertEqual(listed["fixture_den"], {"led": "ok", "short": "Playing", "spoken": "playing Harbour Lights · The Lanterns",
                                                 "playing": "♪ Harbour Lights · The Lanterns"})
        self.assertEqual(listed[ROOM]["short"], "Off")

    def test_a_room_with_both_opens_on_watch_and_keeps_listen_once_chosen(self):
        self.assertEqual(self.out["mixedMode"], "watch")
        self.assertEqual(self.out["mixedListen"]["mode"], "listen")
        self.assertEqual(self.out["back"], {"mode": "listen", "selected": OWNED})
        self.assertEqual(self.out["watchOnly"], {"mode": "watch", "modes": ["watch"], "toggled": False, "face": None})

    def test_a_room_of_speakers_opens_on_the_one_that_plays(self):
        den = self.out["den"]
        self.assertEqual((den["mode"], den["modes"]), ("listen", ["listen"]))
        self.assertEqual([(s["id"], s["name"], s["playing"], s["current"], s["digit"]) for s in den["speakers"]],
                         [(MINI, "Mini", True, True, "1"), (AMP, "Amp Pro", False, False, "2")])

    def test_music_assistant_plays_the_face_and_the_device_its_volume(self):
        self.assertEqual(self.out["face"], {"source": "music", "now": "Harbour Lights", "line": "The Lanterns · Harbour Lights",
                                            "transport": ["shuffle", "previous", "play_pause", "next", "repeat"],
                                            "volume": [0, 100, 1, 60], "speaker": "Mini", "library": True, "canGroup": True,
                                            "progress": True})
        self.assertEqual(self.out["paused"], {"music": [["transport", MINI, "pause", None]], "playing": False})
        self.assertEqual((self.out["volumeKey"], self.out["volumeSent"]), ("volume_up", [[MINI, "volume", "61"]]))

    def test_a_digit_chooses_a_speaker_in_listen_and_both_sessions_follow(self):
        self.assertEqual(self.out["digit"], AMP)
        self.assertEqual(self.out["chosen"], {"listen": AMP, "audio": AMP, "music": AMP, "speaker": "Amp Pro"})
        self.assertIsNone(self.out["badDigit"])

    def test_without_music_a_speaker_plays_what_it_reports_itself(self):
        self.assertEqual(self.out["plain"], {"source": "device", "transport": ["previous", "play_pause", "next"],
                                             "library": False, "canGroup": False})
        self.assertEqual(self.out["plainSent"], [[MINI, "key", "play_pause"]])

    def test_a_locked_routed_room_locks_the_speaker_it_owns_and_no_other(self):
        self.assertEqual(self.out["lockedOwned"], {"rooms": ["Example East"], "text": "Locked by Example East"})
        self.assertIsNone(self.out["lockedFree"])


if __name__ == "__main__":
    unittest.main()
