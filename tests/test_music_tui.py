"""The terminal remote's Music screen (tui/remote/music.js), which draws the
Music page's session (web/remote/music-session.js) and opens with y from
Remotes.

The public example house with the mock's invented speakers: two in a
kitchen, a Sonos in a den, a WiiM without AirPlay in a study and a Sonos
away on a patio. Then a host without music, and a Guest who may not see
free text. The harness waits for what it expects to draw, never for a fixed
time. Mock backend only; nothing leaves the process.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node needed for the terminal remote")
class MusicScreen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/music_tui_harness.js")],
                             text=True, env=env, capture_output=True, timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.proof = json.loads(run.stdout)

    def test_y_opens_music_from_remotes_only_and_esc_goes_back(self):
        reach = self.proof["reach"]
        self.assertEqual((reach["home"], reach["pairing"], reach["remote"]), ("home", "pairing", "music"))
        self.assertEqual((reach["esc"], reach["again"], reach["f8"]), ("remote", "music", "users"))
        self.assertTrue(self.proof["hint"])
        self.assertTrue(self.proof["help"])

    def test_the_screen_draws_the_speakers_what_plays_and_the_library(self):
        self.assertEqual(self.proof["drawn"], [])
        self.assertEqual(self.proof["focus"], "music-play")

    def test_the_pages_keys_reach_the_speaker(self):
        self.assertEqual(self.proof["sent"], [["volume", "kitchen_amp", "level", 34], ["transport", "kitchen_amp", "pause", None],
                                              ["transport", "kitchen_amp", "shuffle", True], ["transport", "kitchen_amp", "repeat", "all"],
                                              ["transport", "kitchen_amp", "next", None]])
        self.assertEqual((self.proof["next"], self.proof["back"]), ("kitchen_mini", "kitchen_amp"))
        self.assertEqual(self.proof["seek"], [10, "music-progress"])

    def test_typing_goes_into_the_search_and_a_result_offers_how_to_play_it(self):
        self.assertEqual(self.proof["searchFocus"], "music-search")
        self.assertEqual(self.proof["typed"], ["harbq", "music"])
        self.assertEqual(self.proof["results"], [])
        self.assertEqual(self.proof["sheet"], {"kind": "music-item", "title": "Low Tide", "offers": []})
        self.assertEqual(self.proof["played"], {"sent": [["play", "kitchen_amp", "library://track/101", "next"]],
                                                "toast": ["Play next", "Low Tide plays next on Kitchen Sonos Amp"], "closed": True})
        self.assertEqual(self.proof["cleared"], ["", "music"])

    def test_browse_opens_a_folder_and_backspace_goes_back(self):
        self.assertEqual((self.proof["browsed"], self.proof["browsedBack"]), (["Library", "NAS"], ["Library"]))
        self.assertEqual(self.proof["queue"], ["Signal Fires"])

    def test_the_group_sheet_joins_and_says_why_a_speaker_could_not(self):
        group = self.proof["group"]
        self.assertEqual((group["kind"], group["title"], group["ticked"]), ("music-group", "Group with Kitchen Sonos Amp", True))
        self.assertEqual(group["result"], [])
        self.assertEqual(group["toast"], ["Study could not join", "Study cannot join Kitchen Sonos Amp: Sonos and WiiM speakers "
                                          "group only over AirPlay, and one of them has none."])
        self.assertEqual(group["drawn"], [])

    def test_a_locked_speaker_is_refused_with_a_toast(self):
        locked = self.proof["locked"]
        self.assertEqual(locked["toast"], ["Kitchen Sonos Amp: Pause", "A locked room is using this device or route, so it cannot change now."])
        self.assertEqual(locked["tried"], [["transport", "kitchen_amp", "pause", None]])
        self.assertTrue(locked["drawn"])

    def test_a_group_members_lock_leaves_the_speakers_own_volume(self):
        self.assertEqual(self.proof["member"], {"sent": [["volume", "kitchen_amp", "level", 34]], "toast": ["Kitchen Sonos Amp", "Locked by Den"]})

    def test_an_admin_sees_the_tidal_code_to_approve(self):
        self.assertEqual(self.proof["tidal"], [])

    def test_a_narrow_terminal_still_draws_it(self):
        self.assertEqual(self.proof["narrow"], {"header": True, "keys": True, "volume": True})

    def test_without_music_one_line_says_so_and_no_key_sends(self):
        self.assertEqual(self.proof["absent"], {"said": True, "speakers": False, "titles": False, "playing": False, "sent": []})

    def test_a_guest_without_free_text_sees_what_plays_but_no_titles_or_library(self):
        self.assertEqual(self.proof["guest"], {"said": True, "speakers": True, "titles": False, "playing": True, "sent": []})


if __name__ == "__main__":
    unittest.main()
