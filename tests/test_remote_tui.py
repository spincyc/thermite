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

    def test_n_and_shift_n_walk_the_remotes_buttons_and_space_presses_the_one_reached(self):
        walk = self.proof["walk"]
        self.assertEqual(walk["start"], "key:ok")
        self.assertIn("←↑→↓ D-pad · Enter OK · n/N button · Space press", walk["status"])
        # Space pressed the key walked to; with the focus still on it, Enter is OK and the arrows the D-pad.
        self.assertEqual(walk["pressed"], ["key:netflix", [["example_player", "netflix"], ["example_player", "ok"], ["example_player", "up"]]])
        self.assertEqual(walk["round"][:2], [True, True], "N from the first button is the last, and n the first again")
        self.assertEqual(walk["elsewhere"], [True, True], "n moves nothing off the remote's pane")

    def test_the_walk_reaches_the_volume_slider_whose_arrows_step_the_volume(self):
        focus, sent, status = self.proof["walk"]["slider"]
        self.assertEqual((focus, sent), ("slider", [["volume", 1]]))
        self.assertIn("Volume: ←→ step · Home/End least/most", status)

    def test_every_button_of_every_remote_is_reached_and_pressed_from_the_keyboard_in_80_by_24(self):
        remotes = self.proof["remotes"]
        self.assertGreaterEqual(len(remotes), 14)
        self.assertEqual(sum(remote["buttons"] for remote in remotes.values()), 249)
        for name, remote in remotes.items():
            with self.subTest(remote=name):
                self.assertEqual(remote["undrawn"], [], "buttons of the layout the terminal does not draw")
                self.assertEqual(remote["unwalked"], [], "buttons n never brings the focus to")
                self.assertEqual(remote["unpressed"], [], "buttons Space did not press with the focus on them")
                self.assertEqual(remote["unseen"], [], "buttons the panel did not scroll into sight when focused")

    def test_a_remote_too_tall_for_its_pane_has_one_row_keys_and_says_how_to_scroll_what_is_left(self):
        remotes = self.proof["remotes"]
        # In 80 by 24 the pane shows 16 rows. Three-row keys fit for these three; the others go to one row a key.
        self.assertEqual(sorted(name for name, remote in remotes.items() if not remote["flat"]), ["pc", "ps5", "wiim"])
        # Three remotes still hide rows (three-row keys hid up to 51 of eleven remotes).
        self.assertEqual({name: remote["hidden"] for name, remote in remotes.items() if remote["hidden"]},
                         {"sony_bluray": 5, "sony_bluray_cec": 7, "sony_tv": 11})
        for name, remote in remotes.items():
            with self.subTest(remote=name):
                hidden = bool(remote["hidden"])
                self.assertEqual(remote["hint"], hidden, "PgUp/Dn scroll in the pane's bottom edge, with the focus on the rooms")
                self.assertEqual(remote["status"], hidden, "PgUp/PgDn scroll in the status line, with the focus on the remote")
                self.assertEqual(remote["unpaged"], [], "buttons PgDn never brings on screen")

    def test_a_remote_that_fits_its_pane_keeps_its_three_row_keys(self):
        # In 110 by 60 the pane shows 42 rows: only the three Sony remotes, 50 rows and more, need one-row keys, and nothing is hidden.
        self.assertEqual({name: remote["roomy"] for name, remote in self.proof["remotes"].items() if remote["roomy"] != [False, 0]},
                         {name: [True, 0] for name in ("sony_bluray", "sony_bluray_cec", "sony_tv")})

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
        # Home's switch, as the remote's: Watch's rooms, then Listen's.
        self.assertEqual(self.proof["mouse"]["home"], ["watch", [WEST, EAST], "listen", [EAST, DEN, LOUNGE]])

    def test_narrow_the_headers_chips_open_the_rooms_and_info_each_drawn_whole(self):
        narrow = self.proof["narrow"]
        self.assertEqual(narrow["header"], {"60": [["F5 Site", "≡ Rooms"], ["open:rooms"]],
                                            "70": [["≡ Rooms", "i Info"], ["open:rooms", "open:info"]],
                                            "90": [["", "○ mock  ◐"], ["open:info"]]})
        self.assertEqual((narrow["start"], narrow["info"], narrow["rooms"], narrow["chosen"]), ("rooms", "info", "rooms", [EAST, None]))
        # The sources' row, wider than the terminal: › scrolls it on, the wheel back.
        self.assertEqual(narrow["bubbles"], [0, True, True])

    def test_the_rooms_rows_keep_name_padlock_source_and_light_in_columns_at_every_width(self):
        # The sheet's rows at 40 and 60 columns, the panel's at 80 and 120: the name, in the row's spare
        # columns, cut where they are too few; the source column as wide as "Computer".
        names = {"40": ("Example East, the lo…", 21), "60": ("Example East, the long way round", 33), "80": ("Example East…", 13),
                 "120": ("Example East…", 13)}
        self.assertEqual(sorted(self.proof["columns"], key=int), list(names))
        for width, rows in self.proof["columns"].items():
            with self.subTest(width=width):
                (west, computer), (east, off) = rows
                self.assertEqual((west, east), (WEST, EAST))
                name, spare = names[width]
                self.assertEqual((off[1:1 + spare].rstrip(), computer[1:1 + spare].rstrip()), (name, "Example West"))
                # The padlock in the column after the name's; the sources against the lights at the right edge.
                lock = 1 + spare + 1
                self.assertEqual((off[lock - 1:lock + 2], computer[lock - 1:lock + 2]), (" ⚿ ", "   "))
                self.assertEqual((computer[lock + 2:], off[lock + 2:]), ("Computer ● ", "     Off ○ "))

    def test_homes_arrows_and_enter_open_remotes_on_a_room_and_h_the_day_and_esc_stays_home(self):
        self.assertEqual(self.proof["pages"]["home"], ["home", "home:remote", "remote", LOUNGE, "listen", "watch", "remote", WEST, "watch", "day", "home"])

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
        self.assertEqual(startup["stream"], [DEN, "listen", [EAST, DEN, LOUNGE]])
        # d has no signal to show in Listen, beside the remote or as a sheet.
        self.assertEqual(startup["signal"], [None, "rooms", None, None, "rooms", []])
        self.assertEqual(startup["walk"], [WEST, "watch", [WEST], True], "a routed room's Source, changed meanwhile, in Watch's list")
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
        # Watch's ↓ stays in its rooms; y, Listen's ↓ and on, ], the rooms sheet's ↓ and Enter, and a click
        # ask none of Listen's rooms for a signal; y again is Watch on the room last in it.
        self.assertEqual(signal["walk"], [[WEST, "watch"], [EAST, "listen"], [DEN, "listen"], [LOUNGE, "listen"], [EAST, "listen"],
                                          [DEN, "listen"], [LOUNGE, "listen"], [DEN, "listen"], [WEST, "watch"]])
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

    def test_a_controller_without_fault_is_refused_by_name_before_the_terminal_is_touched(self):
        message, touched = self.proof["terminal"]["refused"]
        self.assertEqual(message, "The terminal's controller lacks fault(), which says on the screen that a key or a click failed: "
                                  "fault(error). Define it: tui/remote/app.js lists what a controller needs.")
        self.assertEqual(touched, 0, "blessed's program was made, or bytes written")

    def test_every_controller_under_tui_is_one_the_terminal_takes(self):
        self.assertEqual(self.proof["terminal"]["controllers"], {"installer": "ok", "remote": "ok"})

    def test_infos_buttons_and_switches_are_chosen_with_the_arrows(self):
        self.assertEqual(self.proof["sheets"]["info"], {"mode": ["buttons", "slider"], "sent": [[WEST, "screen", "down"]],
                                                        "answering": False, "sheet": "info"})


if __name__ == "__main__":
    unittest.main()
