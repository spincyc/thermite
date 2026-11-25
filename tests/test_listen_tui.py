"""Listen on the terminal remote's Remotes screen (tui/remote/listen.js),
which draws a room's speakers from the shared Listen session
(web/remote/listen-session.js) over the device's own (audio-session.js)
and Music Assistant's (music-session.js).

The public example house with four fixture speakers: a WiiM Amp Pro that
example_east names its `audio` (a room that watches and listens, whose
lock is the Amp's), a WiiM Mini and an Amp Pro in a den no routed room
covers (the Amp's Play Mode cut to four short choices, so that a choice
also draws as a row of options), and a Sonos Arc alone in a lounge. The
music mock plays the house's speakers. A second run uses the public
example alone, which has none. The harness waits for what it expects to
draw, never for a fixed time. Mock backends only; nothing leaves the
process.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
import oh_inventory  # noqa: E402
from oh_features import remote_model  # noqa: E402

ROOM, OWNED, DEN, MINI, FREE, LOUNGE, ARC = (
    "example_east", "fixture_amp", "fixture_den", "fixture_den_mini", "fixture_den_amp", "fixture_lounge", "fixture_arc")
WIIM = {"make": "WiiM", "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}


def model():
    data = oh_inventory.load(AREA / "instance/devices.yaml")
    data["rooms"][DEN] = {"label": "Fixture Den", "tag": "Room"}
    data["rooms"][LOUNGE] = {"label": "Fixture Lounge", "tag": "Room"}
    data["devices"] += [
        {"id": MINI, "room": DEN, "name": "Fixture Den Mini", "ip": "192.0.2.43", "kind": "audio_streamer", "model": "Mini", **WIIM},
        {"id": FREE, "room": DEN, "name": "Fixture Den Amp", "ip": "192.0.2.42", "kind": "streaming_amp", "model": "Amp Pro", **WIIM},
        {"id": OWNED, "room": ROOM, "ip": "192.0.2.41", "kind": "streaming_amp", "model": "Amp Pro", **WIIM},
        {"id": ARC, "room": LOUNGE, "name": "Fixture Lounge Arc", "ip": "192.0.2.44", "kind": "soundbar", "make": "Sonos", "model": "Arc",
         # The UDN a player derives from its mac (sonos.udn); the mac in two parts, for the public guard.
         "mac": "02:00:00:00:00:" + "44", "config": {"sonos": {"udn": "RINCON_02000000004401400"}},
         "controls": {"observe": ["sonos"], "control": ["sonos"]}}]
    data["routing"]["rooms"][ROOM]["audio"] = OWNED
    built = remote_model.build(House(data))
    mode = next(setting for setting in built["devices"][FREE]["audio"]["settings"] if setting["item"] == FREE + "_play_mode")
    mode["options"] = [option for option in mode["options"] if option[0] in ("4", "0", "1", "3")]
    return built


def harness(mode, house_file=None):
    env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
    if house_file:
        env["THERMITE_REMOTE_HOUSE_FILE"] = str(house_file)
    run = subprocess.run([shutil.which("node"), str(AREA / "tests/listen_tui_harness.js"), mode, ROOM, OWNED, DEN, MINI, FREE, LOUNGE, ARC],
                         text=True, env=env, capture_output=True, timeout=180)
    if run.returncode:
        raise AssertionError(run.stderr or run.stdout)
    return json.loads(run.stdout)


def columns(text):
    """The columns a terminal gives text: two for wide, none for a mark or a format character."""
    return sum(0 if unicodedata.category(ch) in ("Mn", "Me", "Cf") else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


@unittest.skipUnless(shutil.which("node"), "node needed for the terminal remote")
class ListenTUI(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as root:
            house = Path(root) / "house.js"
            house.write_text("globalThis.REMOTE_HOUSE = " + json.dumps(model()) + ";\n")
            cls.proof = harness("fixture", house)
        cls.plain = harness("plain")

    def test_a_room_that_watches_and_listens_opens_on_watch_and_y_switches(self):
        self.assertEqual(self.proof["watch"], {"mode": "watch", "switch": True, "footer": True, "help": True})
        self.assertEqual(self.proof["caption"], ["watch", "listen"])

    def test_no_audio_or_music_screen_is_left_and_the_page_keys_still_go(self):
        reach = self.proof["reach"]
        self.assertEqual((reach["u"], reach["Y"], reach["again"]), ("remote", "listen", "watch"))
        self.assertEqual((reach["f6"], reach["back"], reach["kept"]), ("pairing", "remote", "listen"))
        self.assertEqual((reach["homeY"], reach["f8"]), (["home", "watch", "listen"], "users"))

    def test_listen_draws_the_speaker_its_transport_chips_and_volume_as_watch(self):
        self.assertEqual(self.proof["drawn"], [], "missing from the screen")
        self.assertEqual(self.proof["focus"], "listen-key:play_pause")
        self.assertIn("y Watch   r Rooms", self.proof["footer"])
        self.assertEqual(self.proof["help"], [])
        self.assertEqual(self.proof["helpKeys"], {"r": ["Rooms"], "R": ["Repeat"], "space": ["Space P"]})

    def test_the_keys_reach_the_speaker_its_volume_and_mute_its_own(self):
        self.assertEqual(self.proof["music"], [["transport", OWNED, "play", None]])
        self.assertEqual(self.proof["spaceOnLevel"], [])
        sent = self.proof["sent"]
        self.assertEqual(sent[:4], [[OWNED, "volume", "61"], [OWNED, "mute", "ON"], [OWNED, "volume", "60"], [OWNED, "volume", "0"]])
        # Held, + presses again and again.
        self.assertGreaterEqual(len([entry for entry in sent[4:] if entry[1] == "volume"]), 2)
        self.assertIn([OWNED, "input", "bluetooth"], self.proof["inputSent"])

    def test_the_input_is_a_list_from_its_chip(self):
        self.assertEqual(self.proof["inputs"], {"kind": "audio-inputs", "focus": "audio-input:wifi", "closed": [None, "listen-inputs"]})

    def test_info_shows_the_speakers_settings_and_facts_each_reached_with_the_arrows(self):
        self.assertEqual(self.proof["info"], [])
        self.assertEqual(self.proof["choice"], {"kind": "audio-choice", "focus": "audio-option:" + OWNED + "_eq_preset:Flat", "lists": True,
                                                "back": ["info", "audio-set:" + OWNED + "_eq_preset"],
                                                "set": {"eq": True, "level": True}})
        self.assertEqual(self.proof["settings"], [
            [OWNED, OWNED + "_eq", "ON"], [OWNED, OWNED + "_sub_level", "1"], [OWNED, OWNED + "_sub_level", "2"],
            [OWNED, OWNED + "_sub_level", "1"], [OWNED, OWNED + "_eq_preset", "Acoustic"]])

    def test_a_room_of_speakers_alone_shows_their_bubbles_and_the_digits_choose(self):
        den = self.proof["den"]
        self.assertEqual({key: den[key] for key in ("mode", "switch", "footer", "bubbles", "listed", "drawn")},
                         {"mode": "listen", "switch": True, "footer": True, "bubbles": True, "listed": True, "drawn": []})
        self.assertEqual((den["digit"], den["arrow"], den["room"]), (FREE, MINI, LOUNGE))
        self.assertEqual(den["kinds"], [], "Info draws each kind of control")
        self.assertEqual(den["info"], [[FREE, FREE + "_play_mode", "0"]])

    def test_the_position_moves_by_keys_and_a_click(self):
        self.assertEqual(self.proof["seek"][:2], [10, "listen-progress"])
        self.assertEqual(self.proof["seek"][2], 0)
        self.assertEqual(self.proof["transport"], [["transport", MINI, "shuffle", True], ["transport", MINI, "repeat", "all"],
                                                   ["transport", MINI, "repeat", "one"], ["transport", MINI, "next", None]])

    def test_r_opens_the_rooms_in_listen_as_in_watch_and_shift_r_repeats(self):
        self.assertEqual(self.proof["rooms"], {"sheet": "rooms", "focus": "room:" + DEN, "repeat": "off", "closed": [None, DEN]})
        # Only R reached Music Assistant's repeat: all, then one.
        self.assertEqual([entry for entry in self.proof["transport"] if entry[2] == "repeat"],
                         [["transport", MINI, "repeat", "all"], ["transport", MINI, "repeat", "one"]])

    def test_typing_goes_into_the_librarys_search_and_a_result_offers_how_to_play_it(self):
        search = self.proof["search"]
        self.assertEqual((search["kind"], search["focus"], search["typed"], search["results"]),
                         ("music-library", "music-search", ["harbq", False], []))
        item = self.proof["item"]
        self.assertEqual((item["kind"], item["title"], item["offers"]), ("music-item", "Low Tide", []))
        self.assertEqual(item["played"], {"sent": [["play", MINI, "library://track/101", "next"]],
                                          "toast": ["Play next", "Low Tide plays next on Fixture Den Mini"],
                                          "back": ["music-library", "music-item:search:track:0"]})
        self.assertEqual(self.proof["cleared"], ["", "music-library"])

    def test_browse_opens_a_folder_and_backspace_goes_back_and_the_queue_marks_what_plays(self):
        # Back to Browse's own list returns to the folder that opened; Backspace there keeps the focus.
        self.assertEqual((self.proof["browsed"], self.proof["browsedBack"]),
                         (["Library", "NAS"], [["Library"], "music-item:page:0", "music-tab:browse"]))
        self.assertEqual(len(self.proof["queue"]), 1)

    def test_an_artist_opens_over_the_search_and_its_pages_lead_to_each_other(self):
        # Its Back is focused at once, while it reads.
        self.assertEqual(self.proof["opened"], ["music-library", "music-back"])
        self.assertEqual(self.proof["artist"], [])
        self.assertEqual(self.proof["album"], [])
        self.assertEqual(self.proof["track"], ["music-item", "music-action:from", []])
        self.assertEqual(self.proof["played"], [["tidal--mock://album/902", "replace", {"shuffle": True}],
                                              ["tidal--mock://album/902", "replace", {"start": "tidal--mock://track/9021"}]])
        self.assertEqual(self.proof["again"], "music-back")
        # Backspace walks back a page at a time, each time to what opened the page left: the header's
        # artist, the single's row, the search's artist; with nothing left, the focus stays.
        self.assertEqual(self.proof["back"], [[["Wide Awake", "music-artist:0"], ["Juniper Vale", "music-item:section:singles:0"],
                                               [None, "music-item:search:artist:0"]], "music-item:search:track:0", "juniper"])

    def test_an_admin_sees_the_tidal_code_to_approve(self):
        self.assertEqual(self.proof["tidal"], [])
        self.assertIsNone(self.proof["closed"])

    def test_a_refusal_by_the_gateway_says_why(self):
        refused = self.proof["refused"]
        self.assertEqual(refused["toast"], ["Fixture Den Mini: Pause", "A locked room is using this device or route, so it cannot change now."])
        self.assertEqual(refused["tried"], [["transport", MINI, "pause", None]])

    def test_the_group_sheet_joins_says_why_one_could_not_and_sets_the_groups_volume(self):
        group = self.proof["group"]
        self.assertEqual((group["kind"], group["title"], group["ticked"]), ("music-group", "Group with Example East", True))
        self.assertEqual(group["result"], [])
        self.assertEqual(group["toast"], ["Fixture Lounge could not join", "Fixture Lounge cannot join Example East: speakers from different "
                                          "providers cannot group without a shared protocol."])
        level, sent = group["stepped"]
        self.assertEqual(sent, ["volume", OWNED, "group", level + 2])
        self.assertEqual(group["drawn"], [])
        self.assertEqual(group["dragged"][-1], [100, True])
        self.assertEqual(group["dragged"][0], [0, False])

    def test_the_group_sheet_says_who_was_asked_and_not_confirmed(self):
        asked = "Fixture Den Amp Pro was asked to join; Music Assistant has not confirmed it yet."
        self.assertEqual(self.proof["unconfirmed"], {"toast": ["Group sent", asked], "refused": False, "said": True})

    def test_a_locked_room_shows_its_lock_as_watch_and_refuses_each_command(self):
        lock = self.proof["lock"]
        self.assertTrue(lock["said"] and lock["padlock"], lock)
        self.assertEqual(lock["toasts"], ["Example East: Locked by Example East"] * 4)
        self.assertEqual(lock["pressed"], [])
        self.assertEqual((lock["sent"], lock["music"]), ([], []))
        self.assertTrue(lock["den"])
        self.assertEqual(lock["other"], [[MINI, "volume", "61"]])

    def test_a_group_members_lock_holds_what_moves_the_group_not_the_speakers_own_volume(self):
        member = self.proof["member"]
        self.assertEqual(member["sent"], [[MINI, "volume", "62"]])
        self.assertEqual((member["toast"], member["played"]), (["Fixture Den Mini", "Locked by Example East"], []))
        self.assertEqual((member["pressed"], member["sheet"], member["groupToast"]), ([False, False], None, ["Fixture Den Mini", "Locked by Example East"]))

    def test_a_q_typed_after_the_search_field_went_does_not_quit(self):
        vanished = self.proof["vanished"]
        # Music Assistant went away under the typing: the field went, and q went nowhere; back, the
        # field has the focus again and takes q, until another key. Two seconds on, q still goes nowhere;
        # four more without a key, it quits.
        self.assertNotEqual(vanished["focus"], "music-search")
        self.assertEqual({key: value for key, value in vanished.items() if key != "focus"},
                         {"typed": [False, "remote"], "back": "music-search", "query": [False, "loq"], "after": True,
                          "account": True, "lapsed": [False, True]})

    def test_a_title_in_wide_characters_keeps_its_rows_on_the_panel(self):
        widths = self.proof["widths"]
        self.assertTrue(all(columns(row) == widths["cells"] == 110 for row in widths["rows"]), widths["rows"])
        self.assertEqual(widths["cut"], ["東京…", "東… ", ["東京", "東京", "東京"], "ab X京cd"])

    def test_it_fits_80_by_24_and_a_narrow_terminal(self):
        sizes = self.proof["sizes"]
        self.assertEqual(sizes["80"], {"keys": True, "chips": True, "volume": True, "mute": True, "switch": True, "wide": 0})
        self.assertEqual(sizes["44"], {"keys": True, "chips": True, "volume": True, "mute": True, "switch": True, "wide": 0,
                                       "caret": [True, True]})

    def test_without_music_one_line_says_so_and_the_speaker_plays_what_it_reports(self):
        self.assertEqual(self.proof["absent"], {"said": True, "titles": False, "chips": [], "playing": True, "sheet": None,
                                                "sent": [], "device": [[MINI, "key", "play_pause"], [MINI, "volume", "61"]]})

    def test_a_guest_without_free_text_sees_what_plays_but_no_titles_or_library(self):
        self.assertEqual(self.proof["guest"], {"said": True, "titles": False, "chips": ["listen-group", "listen-library"], "playing": True, "sheet": None,
                                               "sent": [], "device": []})

    def test_wide_infos_controls_take_the_keys_not_the_room_or_the_speaker(self):
        info = self.proof["edges"]["info"]
        self.assertEqual({key: info[key] for key in ("pane", "moved", "room", "mode", "music")},
                         {"pane": "info", "moved": True, "room": ROOM, "mode": "listen", "music": []})
        self.assertEqual(info["sent"], [[OWNED, OWNED + "_sub_level", "1"], [OWNED, OWNED + "_eq", "ON"], [OWNED, OWNED + "_eq", "OFF"]])

    def test_walking_rooms_keeps_to_the_modes_rooms_and_one_speaker_lands_on_the_rooms(self):
        self.assertEqual(self.proof["edges"]["walk"], ["example_west", "sources", DEN, "speakers", LOUNGE, "rooms"])

    def test_the_switch_leaves_an_active_titles_border(self):
        self.assertIn("┤ ▸ Example East ├─┤  Watch │ ♪ Listen  ├", self.proof["edges"]["title"])

    def test_a_click_or_the_wheel_on_watchs_volume_drawn_before_y_does_not_reach_it(self):
        self.assertEqual(self.proof["edges"]["stale"], {"mode": "listen", "slid": [], "sent": []})

    def test_a_locked_speakers_volume_dragged_is_refused_once(self):
        self.assertEqual(self.proof["edges"]["drag"], {"toasts": ["Example East: Locked by Example East"], "sent": []})

    def test_k_in_a_room_of_speakers_alone_says_it_has_no_lock(self):
        self.assertEqual(self.proof["edges"]["lock"], ["Room lock", "Fixture Den has no room lock."])

    def test_a_narrow_speakers_row_shows_whole_bubbles_and_the_chosen_one(self):
        self.assertEqual(self.proof["edges"]["bubbles"], ["│‹ 1 ● Mini             ›│", "│‹ 2 ● Amp Pro          ›│"])

    def test_a_house_without_speakers_only_watches(self):
        self.assertEqual(self.plain["rooms"], ["example_west", "example_east"])
        self.assertIsNone(self.plain["music"])
        self.assertNotIn(" y ", self.plain["footer"])
        self.assertFalse(self.plain["switch"])
        self.assertEqual((self.plain["mode"], self.plain["sheet"]), ("watch", None))


if __name__ == "__main__":
    unittest.main()
