"""The terminal remote's Remotes screen and its other screens
(tui/remote/controller.js, view.js), driven by keys and the mouse as a user
drives them (tests/remote_tui_harness.js).

The house is test_listen_tui's: the public example's two routed rooms, the
east one watching and listening, and two rooms of speakers alone, the den
and the lounge; the west room's TV also lists its inputs, and the room has
a control (not connected) to raise or lower its screen. On the mock, on openHAB itself (--native) and on the
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


def fixture():
    built = model()
    built["devices"]["example_tv"]["control"]["input"] = {"item": "example_tv_input", "options": [["hdmi1", "HDMI 1"], ["hdmi2", "HDMI 2"]]}
    west = next(room for room in built["rooms"] if room["id"] == WEST)
    west["controls"] = [{"id": "screen", "label": "Screen", "device": "example_matrix", "actions": [["up", "Raise"], ["down", "Lower"]]}]
    return built


@unittest.skipUnless(shutil.which("node"), "node needed for the terminal remote")
class RemoteTUI(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as root:
            house = Path(root) / "house.js"
            house.write_text("globalThis.REMOTE_HOUSE = " + json.dumps(fixture()) + ";\n")
            env = dict(os.environ, THERMITE_REMOTE_HOUSE_FILE=str(house))
            run = subprocess.run([shutil.which("node"), str(AREA / "tests/remote_tui_harness.js"), WEST, EAST, DEN, LOUNGE],
                                 text=True, env=env, capture_output=True, timeout=180)
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout)
        cls.proof = json.loads(run.stdout)

    def test_tab_goes_round_the_panes_and_the_rooms_arrows_choose_a_room_and_a_source(self):
        watch = self.proof["watch"]
        self.assertEqual(watch["start"], ["rooms", "room:" + WEST])
        self.assertEqual(watch["panes"], [["sources", "remote", "info", "signal", "log", "rooms"], "log"])
        self.assertEqual(watch["rooms"], {"down": EAST, "up": WEST, "cursor": "bubble:example_player",
                                          "sent": [[WEST, "source", "example_player"]]})
        for words in ("r Rooms", "d Signal", "h Day", "L Hide log", "v Volume", "t Theme", "? Help", "q Quit"):
            self.assertIn(words, watch["footer"])

    def test_the_digits_choose_a_source_and_the_remotes_keys_press_its_buttons(self):
        watch = self.proof["watch"]
        # 7: the room has no seventh source.
        self.assertEqual(watch["digits"], [[WEST, "source", "example_computer"], [WEST, "source", "off"]])
        self.assertEqual(watch["remote"], ["up", "down", "left", "right", "ok", "ok", "back", "back",
                                           "play_pause", "volume_up", "volume_down", "mute", "previous", "next"])
        self.assertEqual(watch["quiet"], [], "Esc and Backspace off the remote press nothing")

    def test_each_sheet_opens_on_its_key_and_esc_gives_the_pane_back(self):
        sheets = self.proof["sheets"]
        self.assertEqual(sheets["r"], ["rooms", "sheet:rooms", "room:" + WEST, None, "rooms"])
        self.assertEqual(sheets["i"], ["info", "sheet:info", "signal-open", None, "rooms"])
        self.assertEqual(sheets["d"], ["signal", "sheet:signal", None, None, "rooms"])
        self.assertEqual(sheets["h"], ["day", "sheet:day", "day:" + WEST, None, "rooms"])
        self.assertEqual(sheets["?"], ["help", "sheet:help", None, None, "rooms"])
        self.assertEqual(sheets["rooms"], [EAST, None, "remote"])
        self.assertEqual(sheets["help"], [], "missing from the help")

    def test_v_t_a_and_l_switch_the_slider_theme_motion_and_log_and_keep_them_and_q_quits(self):
        sheets = self.proof["sheets"]
        before, after, kept = sheets["toggles"]
        self.assertEqual(after, [{"slider": "buttons", "buttons": "slider"}[before[0]], {"dark": "light", "light": "dark"}[before[1]],
                                 not before[2], not before[3]])
        self.assertEqual(kept, after)
        self.assertTrue(sheets["quit"])

    def test_a_click_on_a_bubble_a_key_an_input_the_slider_or_all_off_does_what_it_says(self):
        mouse = self.proof["mouse"]
        self.assertEqual(mouse["bubbles"], [[[WEST, "source", "example_player"]], "example_tv"])
        self.assertEqual(mouse["inputs"], ["inputs", "input:hdmi1", None, [["example_tv", "input", "hdmi2"]]])
        self.assertEqual(mouse["held"], [True, True], "a held key repeats until let go")
        # Dragged to its end (100), then a step down on the wheel.
        self.assertEqual(mouse["slider"], ["slider", [WEST, "volume", 99]])
        self.assertEqual(mouse["allOff"], [[True, True, 0], [["all", "off"]]])

    def test_the_headers_theme_tabs_and_remotes_menu_and_homes_rooms_go_where_they_say(self):
        self.assertEqual(self.proof["mouse"]["header"], [True, "home", "remote", DEN, "listen", "remotes-menu", "pairing", "remote"])

    def test_narrow_the_headers_chips_open_the_rooms_and_info_each_drawn_whole(self):
        narrow = self.proof["narrow"]
        self.assertEqual(narrow["header"], {"60": [["F5 Site", "≡ Rooms"], ["open:rooms"]],
                                            "70": [["≡ Rooms", "i Info"], ["open:rooms", "open:info"]],
                                            "90": [["", "○ mock  ◐"], ["open:info"]]})
        self.assertEqual((narrow["start"], narrow["info"], narrow["rooms"], narrow["chosen"]), ("rooms", "info", "rooms", [EAST, None]))
        # The sources' row, wider than the terminal: › scrolls it on, the wheel back.
        self.assertEqual(narrow["bubbles"], [0, True, True])

    def test_homes_arrows_and_enter_open_remotes_on_a_room_and_h_the_day_and_esc_stays_home(self):
        self.assertEqual(self.proof["pages"]["home"], ["home", "home:remote", "remote", LOUNGE, "listen", "remote", WEST, "watch", "day", "home"])

    def test_cameras_arrows_choose_one_and_enter_opens_it_in_the_browser(self):
        self.assertEqual(self.proof["pages"]["cameras"], [True, True, "Camera web view", "home"])

    def test_site_from_a_room_of_speakers_alone_opens_on_a_routed_room_and_steps_sorts_and_opens_the_map(self):
        topology = self.proof["pages"]["topology"]
        self.assertEqual(topology["start"], [EAST, "example_computer"])
        self.assertTrue(topology["moved"] and topology["source"])
        self.assertEqual(topology["sort"], [[None, None], ["matrix", True]])
        self.assertIn("/site/?room=" + WEST + "&source=example_player", topology["opened"])

    def test_f6_and_ctrl_p_open_pair_devices_only_from_remotes_and_esc_goes_back(self):
        self.assertEqual(self.proof["pages"]["pairing"], ["topology", "pairing", "remote", LOUNGE, "listen"])
        # The player chosen with ↓, asked for a code with Enter, 123x5 typed (x refused), ⌫ and 4: the mock's code pairs it.
        self.assertEqual(self.proof["pages"]["paired"], ["1234", "paired", "device", True])

    def test_kept_from_the_last_run_a_room_of_speakers_alone_starts_on_listen_and_reads_no_signal(self):
        startup = self.proof["startup"]
        self.assertEqual(startup["start"], [DEN, "listen", []])
        self.assertEqual(startup["stream"], [DEN, "listen", True], "a routed room's Source, changed meanwhile, in the list")
        # d has no signal to show in Listen, beside the remote or as a sheet.
        self.assertEqual(startup["signal"], [None, "rooms", None, None, "rooms", []])
        self.assertEqual(startup["walk"], [WEST, "watch", [WEST]])
        self.assertEqual(startup["settings"], ["settings", True, ["GET /api/v1/rooms/" + WEST + "/policy"]])

    def test_a_speakers_input_not_read_yet_reads_as_reading_not_null(self):
        self.assertEqual(self.proof["startup"]["input"], ["Reading… ▾", "Input: Reading… (Enter: choose)"])

    def test_an_administrators_security_users_and_account_pages_from_the_keys(self):
        admin = self.proof["admin"]
        self.assertEqual(admin["security"], "security")
        self.assertEqual(admin["users"], ["users", "access-form", True, None])
        self.assertEqual(admin["account"], "account")

    def test_a_sign_in_that_ends_puts_the_remote_on_account_and_ctrl_c_quits(self):
        admin = self.proof["admin"]
        self.assertEqual(admin["ended"], ["account", "Your sign-in has ended. Run thermite access login again.", True])
        self.assertTrue(admin["quit"])

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

    def test_infos_buttons_and_switches_are_chosen_with_the_arrows(self):
        self.assertEqual(self.proof["sheets"]["info"], {"mode": ["buttons", "slider"], "sent": [[WEST, "screen", "down"]],
                                                        "answering": False, "sheet": "info"})


if __name__ == "__main__":
    unittest.main()
