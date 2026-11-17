"""Listen on the remote page (web/remote/listen.js and listen.css): the remote
page itself (index.html and remote.js on every script it loads) in
tests/remote_dom.js's small stand-in for a browser, on the mock backends,
and the page's markup and stylesheets.

The house is test_listen_session's with the den's Mini moved into
example_east (a room to watch and to listen in, with two speakers), a patio
of one Mini, the den's Amp Pro a Sonos (so a group can refuse it) and the
owned Amp Pro a power control. There is no real browser here: what the page
draws and sends is read from the stand-in, and the layout from the CSS
rules. Nothing leaves the process.
"""

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from tests.test_listen_session import AMP, MINI, OWNED, ROOM, WIIM, house, model

AREA = Path(__file__).resolve().parents[1]
REMOTE = AREA / "web/remote"
WATCH, DEN, PATIO, PATIO_MINI = "example_west", "fixture_den", "fixture_patio", "fixture_patio_mini"
MOVED = "Fixture Den Mini"


def speaker_house():
    def extra(data):
        data["rooms"][PATIO] = {"label": "Fixture Patio", "tag": "Room"}
        data["devices"][-3]["room"] = ROOM
        data["devices"].append({"id": PATIO_MINI, "room": PATIO, "name": "Fixture Patio Mini", "ip": "192.0.2.44",
                                "kind": "audio_streamer", "model": "Mini", **WIIM})
    built = model(house(extra=extra))
    built["devices"][OWNED]["control"]["power"] = OWNED + "_power"
    built["devices"][AMP]["remote"] = "sonos"
    built["devices"]["example_display"]["control"]["input"] = {"item": "example_display_input",
                                                               "options": [["hdmi1", "HDMI 1"], ["hdmi2", "HDMI 2"]]}
    return built


@unittest.skipUnless(shutil.which("node"), "node needed for the remote page's scripts")
class ListenPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/listen_page_harness.js"), MINI, OWNED, ROOM, WATCH, AMP, DEN,
                              PATIO_MINI, PATIO], input=json.dumps(speaker_house()), text=True, env=env, capture_output=True,
                             timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.out = json.loads(run.stdout)
        cls.errors = {name: part["error"] for name, part in cls.out.items() if "error" in part}

    def test_every_scenario_ran_to_its_end(self):
        self.assertEqual(self.errors, {})

    def test_watchs_rooms_are_those_with_a_screen_and_listens_those_with_speakers(self):
        out = self.out["rooms"]
        self.assertEqual(out["sheet"], [["Example West", "Off", ""], ["Example East", "Off", "♪ Harbour Lights · The Lanterns"]])
        self.assertEqual(out["sidebar"], 2)
        # In Listen each says what its speakers do, and the routed room with speakers comes first.
        self.assertEqual(out["listen"], [[["Example East", "Playing", "♪ Harbour Lights · The Lanterns"],
                                          ["Fixture Den", "Paused", ""], ["Fixture Patio", "Idle", ""]], "Example East", 3])
        # Y on a room of a list it rebuilds keeps the focus on the room on show.
        self.assertEqual(out["focus"], ["button:Example East, Off, playing ♪ Harbour Lights · The Lanterns", "Example East"])

    def test_home_lists_watchs_rooms_then_listens_with_what_plays_and_keeps_the_remotes_choice(self):
        out = self.out["home"]
        self.assertEqual(out["watch"], [[["Example West", "", "Off", "Room off"], ["Example East", "", "Off", "Room off"]], False,
                                        "Reported source", ["remotes?room=example_west", "remotes?room=example_east"]])
        rows, what, stored, status, radios = out["listen"]
        self.assertEqual(rows, [["Example East", "", "Harbour Lights · The Lanterns", "Playing"],
                                ["Fixture Den", "", "Nothing playing", "Paused"], ["Fixture Patio", "", "Nothing playing", "Idle"]])
        # The remote opens on the mode Home shows; the radio on show takes the Tab stop.
        self.assertEqual((what, stored, status, radios), ("Playing", "listen", "Sample speakers · no device commands",
                                                          [["false", "-1"], ["true", "0"]]))
        self.assertEqual(out["y"], ["watch", 2])
        # Home's own stream runs in Watch alone, and Y on a row it rebuilds focuses the switch.
        self.assertEqual(out["streams"], [True, False])
        self.assertEqual(out["focus"], ["button:Watch", "watch", True])
        # Its switch is two icons, as the remote's, each radio named by its label and tooltip.
        self.assertEqual(out["icons"], [["Watch", "Watch (Y)", "", "#i-tv"], ["Listen", "Listen (Y)", "", "#i-note"]])

    def test_the_padlock_has_its_own_column_between_the_room_and_its_source(self):
        self.assertEqual(self.out["rooms"]["columns"], ["room__name", "icon room__lock", "room__source"])
        # Only the room lists have the columns: an input sheet's rows keep their name and "In use".
        self.assertEqual(self.out["rooms"]["lists"], [[True, True], [True, True], [False, False]])
        css = (AREA / "web/remote/remote.css").read_text()
        self.assertIn(".room {\n  display: flex;", css)
        self.assertIn(".rooms--house {\n  grid-template-columns: minmax(0, 1fr) 14px fit-content(42%);", css)

    def test_the_switch_shows_in_every_room_and_y_toggles_it(self):
        out = self.out["modes"]
        self.assertEqual((out["watchOnly"]["switch"], out["watchOnly"]["mode"]), (True, "watch"))
        # Two icons, no words: each radio named by its label and tooltip.
        self.assertEqual(out["icons"], [["Watch", "Watch (Y)", "", "#i-tv"], ["Listen", "Listen (Y)", "", "#i-note"]])
        self.assertEqual(out["watchOnlyY"], [True, "listen", "Example East"])
        self.assertEqual(out["watchAgain"], [True, "watch", "Example East"])
        self.assertEqual((out["mixed"]["switch"], out["mixed"]["checked"], out["mixed"]["listen"]), (True, ["watch"], False))
        pressed, listening = out["y"]
        self.assertTrue(pressed)
        self.assertEqual({key: listening[key] for key in ("checked", "mode", "listen", "bubbles", "speakers", "controls")},
                         {"checked": ["listen"], "mode": "listen", "listen": True, "bubbles": False, "speakers": True, "controls": False})
        # The shortcuts list follows: Listen's keys in Listen, Watch's back in Watch.
        self.assertEqual(listening["shortcuts"], ["Play/pause", "Track"])
        self.assertEqual(out["yAgain"][1]["shortcuts"], ["D-pad", "OK"])
        self.assertEqual((out["yAgain"][1]["mode"], out["yAgain"][1]["bubbles"], out["yAgain"][1]["listen"]), ("watch", True, False))
        self.assertEqual(out["clicked"], "listen")
        # A radio group: the arrows choose and the focus follows; one tab stop.
        self.assertEqual(out["arrow"], [True, "watch", "button:Watch"])
        self.assertEqual(out["tabStops"], ["0", "-1"])
        # A room of speakers alone listens, with no switch; the mode is kept per room.
        self.assertEqual((out["den"]["switch"], out["den"]["mode"], out["den"]["listen"]), (True, "listen", True))
        self.assertEqual(out["kept"], "listen")

    def test_under_1180_px_the_bar_holds_the_room_then_the_switch_and_its_keys_reach_the_page(self):
        out = self.out["bar"]
        self.assertEqual(out["lent"], [["room-switch", "modes"], ["info-open", "log-toggle"], True, True])
        # The arrows and Y from the switch in the bar, as from the head; the shell's own controls keep their keys.
        self.assertEqual(out["arrow"], [True, "listen", "button:Listen"])
        self.assertEqual(out["y"], [True, "watch", "button:Watch"])
        self.assertEqual(out["menu"], [False, "watch"])

    def test_listen_draws_the_speakers_what_plays_its_keys_chips_and_volume(self):
        out = self.out["face"]
        self.assertEqual(out["speakers"], [[MINI, "Mini", "true", "current", "1"], [OWNED, "Amp Pro", None, "idle", "2"]])
        self.assertEqual(out["now"], ["Harbour Lights", "The Lanterns · Harbour Lights", "true", True, False])
        self.assertEqual(out["transport"], [
            ["shuffle", "Shuffle off", False, "false", "#i-shuffle"], ["previous", "Previous track", False, None, "#i-previous"],
            ["play_pause", "Pause", False, None, "#i-pause"], ["next", "Next track", False, None, "#i-next"],
            ["repeat", "Repeat off", False, "false", "#i-repeat"]])
        self.assertEqual(out["repeatOne"], ["Repeat one", "#i-repeat-one"])
        self.assertEqual(out["chips"], [["listen-group", True, "Group"], ["listen-input", True, "Wi-Fi"], ["listen-library", True, "Library"]])
        self.assertEqual((out["caption"], out["mute"], out["level"], out["alert"]), ("Mini · 60", True, ["0", "100", "60"], False))

    def test_volume_held_repeats_and_the_keys_reach_the_speaker(self):
        out = self.out["face"]
        # One press, then one each REPEAT_EVERY after REPEAT_DELAY; the click ending the hold adds none.
        self.assertEqual(out["held"], 4)
        self.assertEqual(out["muted"], [["mute:ON"], "true"])
        self.assertEqual(out["digit"], [None, "true"])
        self.assertEqual(out["keys"], [True, True, True])
        self.assertEqual(out["musicSent"], [f"transport:{MINI}:shuffle", f"transport:{MINI}:pause", f"transport:{MINI}:previous"])
        self.assertFalse(out["enter"])
        self.assertEqual(out["shortcuts"], ["SpacePPlay/pause", "<>Track", "+−Volume", "MMute", "SShuffle", "RRepeat", "GGroup",
                                            "/Search", "1–9Speaker", "[]Room", "YWatch or listen", "LLog"])

    def test_info_holds_the_speakers_settings_facts_and_power(self):
        out = self.out["face"]
        self.assertEqual(out["info"], {"sheet": True, "watch": False, "label": "Speaker settings",
                                       "titles": ["Playback", "Sound", "Device"], "speaker": MOVED + "Mini · Example East",
                                       "switches": ["EQ", "Status Light"], "selects": [MINI + "_play_mode", MINI + "_eq_preset"]})
        self.assertEqual(out["settings"], [MINI + "_eq:ON", MINI + "_eq_preset:Jazz"])
        titles, power, facts, sent = out["amp"]
        self.assertEqual((titles[-1], power, facts), ("About this speaker", 1, ["Subwoofer Connected"]))
        self.assertEqual(sent, [[OWNED, "power", "OFF"]])

    def test_the_input_list_is_remotes_input_sheet(self):
        out = self.out["face"]
        self.assertEqual(out["inputs"], [True, [["wifi", "true"], ["bluetooth", "false"], ["line-in", "false"]]])
        self.assertEqual(out["input"], [False, ["input:line-in"], "Line in"])

    def test_one_speaker_has_no_bubbles_and_its_name_by_the_volume(self):
        self.assertEqual(self.out["alone"]["den"], [True, False, "Amp Pro · 60", False])

    def test_watch_is_unchanged_and_its_audio_caption_listens(self):
        out = self.out["watch"]
        self.assertEqual(out["mixed"], ["watch", False, True, True, True])
        self.assertEqual(out["link"], ["Amp Pro: listen"])
        self.assertEqual(out["listened"], ["listen", "Amp Pro · 60", [None, "true"]])
        # A room without speakers: Watch's bubbles, no Listen, no button in its caption.
        self.assertEqual(out["west"], ["watch", True, False, True])

    def test_a_lock_shows_as_watchs_padlock_disabled_controls_and_alert(self):
        out = self.out["locks"]
        self.assertEqual(out["bubbles"], [[True, "true"], [False, None]])
        self.assertEqual(out["alert"], [True, "Locked by Example East"])
        held = dict(out["disabled"])
        # Browsing the library stays open; everything that would move the speaker is disabled.
        self.assertEqual([name for name, disabled in held.items() if not disabled], ["listen-library"])
        self.assertEqual(out["refused"], [0, ["Example East " + MOVED, "Locked by Example East"]])
        self.assertEqual(out["free"], [False, False])

    def test_the_library_searches_with_its_filters_and_its_item_sheet_plays(self):
        out = self.out["library"]
        self.assertEqual(out["sheet"], [True, True, [["search", "true"], ["browse", "false"], ["queue", "false"]]])
        self.assertEqual((out["kinds"], out["sources"]), (["All", "Tracks", "Albums", "Artists", "Playlists"], ["Everywhere", "Library"]))
        self.assertEqual(out["sections"], ["Tracks", "Albums"])
        self.assertEqual(out["item"], [True, ["Play now", "Play next", "Add to queue", "Go to album", "Go to The Lanterns"],
                                       "On Mini. Play now replaces the queue."])
        self.assertEqual(out["played"], [False, ["Play next", "Harbour Lights plays next on Example East Mini"], ["play", MINI, "next"]])
        self.assertEqual(out["queue"], [True, True])
        # / opens it on Search with the focus in the field.
        self.assertEqual(out["slash"], [True, True, "input:Search music", "search"])
        # From 1200 px it sits beside the remote, not in a sheet.
        self.assertEqual(out["wide"], [True, False, True])
        # Typing is the field's: neither M nor Y reaches the remote.
        self.assertEqual(out["typing"], [False, False, "listen"])

    def test_the_focus_follows_a_folder_opened_back_and_a_sheets_open(self):
        out = self.out["browse"]
        lanterns, nas = "item:0 filesystem_local--flac://folder/The Lanterns", "item:0 filesystem_local--flac://folder/"
        # Browse's roots have no Back; a folder's bar names what Back returns to, and the folder.
        self.assertEqual(out["root"], [False, "", "Library"])
        self.assertEqual(out["opening"], [True, "Library", "NAS", "title"])
        self.assertEqual(out["opened"], [True, "Library", "NAS", lanterns])
        self.assertEqual(out["back"], [False, "", "Library", nas])
        self.assertEqual(out["album"], [True, "The Lanterns", "Harbour Lights",
                                        "item:0 filesystem_local--flac://track/The Lanterns/Harbour Lights/01 Low Tide.flac"])
        self.assertEqual(out["sheet"], [True, "button:Play now"])
        self.assertEqual(out["played"], [False, out["album"][3]])
        self.assertEqual(out["elsewhere"], [True, "Library", "NAS", "title", True, "button:Volume up"])
        # A read that failed leaves the focus on the title once, then lets it be; nothing scrolls the page.
        self.assertEqual(out["failed"], [True, "title", "body", 0])

    def test_an_artist_opens_over_the_search_and_its_pages_lead_to_each_other(self):
        out = self.out["pages"]
        # An artist's row opens its page, no sheet.
        self.assertEqual(out["row"], [None, "Juniper ValeArtistTIDAL"])
        artist = out["artist"]
        self.assertEqual(artist["shown"], [False, True, "library-tab-search"])
        # Back to the search; the header names the page, so the bar has no title.
        self.assertEqual(artist["bar"], [True, "Search", False])
        self.assertEqual(artist["head"], [True, "Juniper Vale", "", "Artist · TIDAL", "artist"])
        self.assertEqual(artist["focus"], "title")
        self.assertEqual(artist["sections"], ["Top tracks", "Albums", "EPs and singles", "Similar artists"])
        self.assertEqual(artist["play"], [["Play Juniper Vale", False], ["Shuffle Juniper Vale", False]])
        album = out["album"]
        self.assertEqual((album["back"], album["head"], album["focus"]),
                         ("Juniper Vale", [True, "Wide Awake", "Juniper Vale", "Single · 2024 · 1 track · 3 min · TIDAL", "album"], "title"))
        # Its tracks by number, each with who plays it.
        self.assertEqual(album["rows"], [["1", "Wide Awake (Edit)", "Juniper Vale"]])
        self.assertEqual(out["headPlayed"], [["tidal--mock://album/902", "replace"],
                                             ["tidal--mock://album/902", "replace", {"shuffle": True}]])
        self.assertEqual(out["more"], [True, ["Play next", "Add to queue", "Go to Juniper Vale"]])
        self.assertEqual(out["track"], [True, ["Play from here", "Play now", "Play next", "Add to queue", "Go to Juniper Vale"],
                                        "button:Play from here"])
        self.assertEqual(out["from"], [False, [["tidal--mock://album/902", "replace", {"start": "tidal--mock://track/9021"}]]])
        self.assertEqual(out["again"], ["Wide Awake", "Juniper Vale"])
        # Home on the tab shown keeps its pages.
        self.assertEqual(out["homeKey"], [True, True, "Juniper Vale"])
        # Back returns the focus to what opened the page left: the header's artist, the single's row, the artist's row.
        self.assertEqual(out["backToSingle"], ["Juniper Vale", "Wide Awake", "head-artist:0"])
        self.assertEqual(out["backToArtist"], ["Search", "Juniper Vale", "item:0 tidal--mock://album/902"])
        self.assertEqual(out["backToSearch"], [True, False, "juniper", True, "item:tidal--mock://artist/900"])
        self.assertEqual(out["home"], [True, False])

    def test_the_group_sheet_says_who_joined_who_could_not_and_who_was_asked(self):
        out = self.out["group"]
        self.assertEqual(out["chip"], ["Group", "G"])
        self.assertEqual(out["open"], [True, "Group with Example East Mini", [[AMP, False], [OWNED, False], [PATIO_MINI, False]]])
        self.assertEqual(out["join"], "Join 3")
        self.assertEqual(out["result"], [
            ["Fixture Patio joined Example East Mini.", None],
            ["Example East Amp Pro was asked to join; Music Assistant has not confirmed it yet.", None],
            ["Fixture Den cannot join Example East Mini: Sonos and WiiM speakers group only over AirPlay, and one of them has none.",
             "listen-result__refused"]])
        self.assertEqual(out["members"], ["Example East MiniLeadsEnd", "Fixture PatioRemove", "Example East Amp ProRemove"])
        self.assertEqual(out["grouped"], ["With Fixture Patio, Example East Amp Pro", True, "Group · 41"])
        # The group's volume key, then a member removed, then the group ended.
        self.assertEqual(out["sent"], [f"volume:{MINI}", f"ungroup:{PATIO_MINI}"])
        self.assertEqual(out["ended"], [f"ungroup:{MINI}", False])

    def test_a_cover_the_gateway_was_too_busy_for_is_asked_for_once_more(self):
        out = self.out["covers"]
        self.assertEqual(out["cover"], [[False, True, 1], [False, False, 2], 2])
        self.assertEqual(out["thumbs"], [True, ["false,true,2"]])
        self.assertGreater(out["jitter"][0], 1)
        self.assertEqual(out["jitter"][1:], [True, True])
        # A position dragged and let go unchanged: the speaker's shows again.
        self.assertEqual((out["dragged"], out["released"]), ("3:20", [True, True]))

    def test_without_music_away_or_as_a_guest_listen_says_so(self):
        out = self.out["music"]
        # No music on the host: the speaker's own report, its input; the line under Info.
        self.assertEqual(out["absent"], [["listen-input"], ["This thermite host has no music."], "Sample Track"])
        self.assertEqual(out["away"], [True, "Music Assistant is not answering yet. It may be starting.", False])
        blind = "Sign in to thermite to search, browse and see the queue."
        # A Guest without titles: whether it plays, and the library's note in place of its panels.
        self.assertEqual(out["guest"], [True, blind, True, 0, "Playing"])
        self.assertEqual(out["told"][:4], [True, blind, True, 0])

    def test_watch_and_listen_each_list_their_own_inputs_in_the_shared_sheet(self):
        out = self.out["inputs"]
        tv, speaker = [True, ["watch:hdmi1", "watch:hdmi2"]], [True, ["listen:wifi", "listen:bluetooth", "listen:line-in"]]
        self.assertEqual([out["watch"], out["listen"], out["watchAgain"], out["listenAgain"]], [tv, speaker, tv, speaker])
        # The TV reports while Listen's list is open: the list stays the speaker's, and a tap goes to the speaker.
        self.assertEqual(out["reported"], speaker)
        self.assertEqual(out["tapped"], [MINI + ":input"])

    def test_the_signal_panel_polls_only_while_it_shows(self):
        out = self.out["signal"]
        # From 1200 px: every SIGNAL_REFRESH in Watch, none in Listen, and again back in Watch.
        self.assertEqual((out["watch"], out["listen"], out["back"]), (2, 0, 2))
        # A room of speakers alone, chosen while it shows, has no signal to read.
        self.assertEqual(out["speakers"], ["listen", []])
        # D in Watch opens the signal; Listen's Info after it is the speaker's and reads none.
        self.assertEqual(out["d"], [True, 1])
        self.assertEqual(out["listenInfo"], [True, 0])

    def test_without_the_right_column_the_library_opens_its_sheet(self):
        self.assertEqual(self.out["sideways"]["library"], [True, True])

    def test_an_admin_signs_in_to_tidal_from_the_library(self):
        self.assertEqual(self.out["music"]["tidal"], [True, "TIDAL is not signed in.", True,
                                                      "Approve this code at link.tidal.com on any device."])


def css_rules(text):
    """(media, selector, declarations) for each rule of a stylesheet, one level of @media deep."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    block = r"@media([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
    rules = []

    def read(body, media):
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            values = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", declarations))
            rules.extend((media, " ".join(one.split()), values) for one in re.split(r",(?![^(]*\))", selector))

    for media, body in re.findall(block, text):
        read(body, " ".join(media.split()))
    read(re.sub(block, "", text), None)
    return rules


def pixels(value, tokens):
    """A length in px: var()s resolved, env() taken as 0, a calc()'s terms added."""
    while "var(" in value:
        value = re.sub(r"var\((--[\w-]+)\)", lambda match: tokens[match.group(1)], value)
    value = re.sub(r"env\([^)]*\)", "0px", value)
    return sum(float(number) for number in re.findall(r"(-?[\d.]+)px", value))


class ListenMarkup(unittest.TestCase):
    page = (REMOTE / "index.html").read_text()
    script = (REMOTE / "listen.js").read_text()
    sheet = (REMOTE / "listen.css").read_text()
    rules = css_rules(sheet)
    shared = css_rules((REMOTE / "remote.css").read_text())

    def test_every_part_the_script_draws_into_is_in_the_page(self):
        parts = set(re.findall(r'(?:\$|all)\("(?:#[\w-]+ )?\[(data-[a-z-]+)', self.script))
        self.assertGreater(len(parts), 40)
        self.assertEqual(sorted(part for part in parts if not re.search(r"\s" + part + r"[\s=>]", self.page)), [])
        ids = set(re.findall(r'\$\("#([\w-]+)', self.script))
        self.assertEqual(sorted(ident for ident in ids if f'id="{ident}"' not in self.page), [])
        icons = set(re.findall(r'icon\("i-([\w-]+)"', self.script)) | {name[2:] for name in re.findall(r'"(i-[\w-]+)"', self.script)}
        icons |= {"play", "pause", "previous", "next", "shuffle", "repeat", "repeat-one", "lock", "mute", "tv", "note"}
        self.assertEqual(sorted(name for name in icons if f'id="i-{name}"' not in self.page), [])

    def test_the_face_keeps_together_above_the_volume_and_the_track_shows_what_is_to_come(self):
        # Seen in a browser: shared out evenly, the parts drifted apart, and the
        # groove, the page's own tone, hid the track's length.
        self.assertIn((None, ".listen-now", {"margin-top": "auto"}), self.rules)
        self.assertIn((None, ".listen-chips + .listen-volume", {"margin-top": "auto"}), self.rules)
        self.assertNotIn("justify-content", dict((sel, d) for media, sel, d in self.rules if media is None)[".listen__face"])
        tracks = [d for media, sel, d in self.rules if sel.startswith(".listen-progress__input::") and "track" in sel]
        self.assertEqual(len(tracks), 2)
        self.assertTrue(all("--key-top" in d["background"] and "--groove" not in d["background"] for d in tracks))
        landscape = [sel for media, sel, d in self.rules if media and "landscape" in media and d.get("margin-top") == "0"]
        self.assertEqual(sorted(landscape), [".listen-chips + .listen-volume", ".listen-now", ".listen-progress"])
        self.assertIn((None, ".listen-chips", {"display": "flex", "flex-wrap": "wrap", "align-items": "center",
                                              "justify-content": "center", "gap": "var(--space-2)"}), self.rules)

    def test_hidden_hides_every_part_of_listen_the_sheets_included(self):
        # .button and .chip set display; one rule reaches every part of Listen marked hidden.
        self.assertIn((None, ":is(.listen, .listen-info, .library, #item-sheet, #group-sheet) [hidden]", {"display": "none"}),
                      self.rules)
        self.assertIn((None, ":is(.modes, .listen, .info)[hidden]", {"display": "none"}), self.rules)
        scopes = ("listen", "listen-info", "library", "item-sheet", "group-sheet")

        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.open, self.hidden = [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                marks = (attrs.get("class") or "").split() + [attrs.get("id") or ""]
                inside = any(scope in each for each in self.open for scope in scopes)
                if "hidden" in attrs and (inside or any(scope in marks for scope in scopes)):
                    self.hidden.append(marks)
                elif "hidden" in attrs and "modes" in marks:
                    self.hidden.append(marks)
                if tag not in ("br", "img", "input", "link", "meta", "source", "use", "path"):
                    self.open.append(marks)

            def handle_endtag(self, tag):
                if tag not in ("use", "path") and self.open:
                    self.open.pop()

        page = Page()
        page.feed(self.page)
        self.assertGreater(len(page.hidden), 15)
        # Remotes' old child pages, and their rule, are gone.
        self.assertNotIn("child-page", (REMOTE / "remote.css").read_text())
        for name in ("audio", "music"):
            for kind in ("html", "js", "css"):
                self.assertFalse((REMOTE / f"{name}.{kind}").exists())

    def test_the_switch_is_one_radiogroup_of_two_icons_beside_the_room_44_px_on_touch(self):
        switch = re.search(r'<header class="remote__top".*?<button class="room-switch"[^>]*>.*?</button>\s*(<!--[^>]*-->\s*)?'
                           r'(<div class="modes"[^>]*>)(.*?)</div>\s*<button class="icon-button" id="info-open"', self.page, re.S)
        self.assertIsNotNone(switch, "the switch sits in the head between the room and Info")
        self.assertIn('role="radiogroup"', switch.group(2))
        self.assertIn('data-thermite-bar="title"', switch.group(2))
        radios = re.findall(r"<button[^>]*>", switch.group(3))
        self.assertEqual(len(radios), 2)
        self.assertTrue(all('role="radio"' in radio and 'aria-keyshortcuts="Y"' in radio for radio in radios))
        self.assertEqual(sum('tabindex="-1"' in radio for radio in radios), 1)
        self.assertEqual(re.sub(r"<[^>]*>|\s", "", switch.group(3)), "", "icons alone, no words")
        fine = "(hover: hover) and (pointer: fine)"
        sizes = {(media, name): values[name] for media, one, values in self.rules if one == ".modes__option"
                 for name in ("width", "min-height") if name in values}
        self.assertEqual(sizes, {(None, "width"): "var(--tap)", (None, "min-height"): "var(--tap)",
                                 (fine, "width"): "36px", (fine, "min-height"): "36px"})
        # The head's free space follows the switch while it shows, and the room's button as before while it does not.
        self.assertIn((None, ".remote__top:has(> .modes:not([hidden])) > .room-switch", {"flex": "0 1 auto"}), self.rules)
        self.assertEqual([values.get("margin-right") for media, one, values in self.rules if one == ".modes"], ["auto"])

    def test_the_bar_fits_the_room_its_chevron_and_the_switch_at_320_px(self):
        # No layout here: the bar's own widths (shell.css), summed as a browser lays them out in one row.
        frame = css_rules((REMOTE / "shell.css").read_text())
        tokens = {"--thermite-inset-top": "0px"}
        for media, selector, values in self.shared:
            if media is None and selector == ":root":
                tokens.update(values)
        bar = "(max-width:1179px)"

        def px(rules, selector, name, media=bar):
            return pixels([values[name] for at, one, values in rules if one == selector and name in values and at == media][-1], tokens)

        # 40 px square keys in a well as tall as the bar: 44 px ones would overflow it.
        key, well = px(frame, ".thermite-title .modes__option", "width"), px(frame, ".thermite-title .modes", "padding")
        self.assertEqual(px(frame, ".thermite-title .modes__option", "min-height"), key)
        self.assertGreaterEqual(key, 40)
        self.assertEqual(2 * well + key, px(frame, "body.thermite-app", "--thermite-header-height"))
        switch = 2 * well + 2 * key + px(self.rules, ".modes", "gap", None)
        # The rest of the row: its sides, ☰, Info and Log and the gaps between; the mark, shown from 360 px
        # without the switch, gives way to it.
        side = pixels(re.search(r"max\(([\d.]+px)", [values["padding"] for at, one, values in frame
                                                    if one == ".thermite-shell" and at == bar][0]).group(1), tokens)
        gap = px(frame, ".thermite-shell", "gap")
        actions = 2 * px(frame, ".thermite-actions .icon-button", "width") + px(frame, ".thermite-actions", "gap")
        fixed = 2 * side + px(frame, ".thermite-shell .thermite-menu", "width") + actions + 2 * gap
        self.assertIn(("(max-width:359px)", ".thermite-shell .thermite-brand", {"display": "none"}), frame)
        self.assertIn((bar, ".thermite-shell:has(.thermite-title .modes:not([hidden])) .thermite-brand", {"display": "none"}),
                      frame)
        # The room's button: its sides, the chevron and the gap before it; the name ellipsizes in what is left.
        padding = [values["padding"] for at, one, values in frame if one == ".thermite-title .room-switch" and at == bar][-1].split()
        room = 2 * pixels(padding[1 % len(padding)], tokens) + px(self.shared, ".room-switch__name .icon", "width", None) \
            + px(self.shared, ".room-switch__name", "gap", None)
        names = {width: width - fixed - switch - room for width in (320, 360, 390)}
        # Enough for a letter or two and the ellipsis of the 18 px name at the narrowest, and a
        # room's whole name, such as "Upper Lounge", at an iPhone's 390 px.
        self.assertGreaterEqual(names[320], 30, names)
        self.assertGreaterEqual(names[390], 100, names)

    def test_listen_takes_the_right_column_from_1200_px_and_info_stays_a_tap_away(self):
        wide = "(min-width: 1200px)"
        self.assertIn((wide, 'body[data-mode="listen"] .panel--library', {"display": "flex", "flex": "1", "min-height": "0",
                                                                           "width": "100%"}), self.rules)
        self.assertIn((wide, 'body[data-mode="listen"] :is(.panel--info, .panel--signal)', {"display": "none"}), self.rules)
        self.assertIn((wide, 'body[data-mode="listen"] #info-open', {"display": "grid"}), self.rules)
        self.assertIn('class="panel panel--library"', self.page)
        self.assertIn("min-width: 1200px", self.script)

    def test_a_phone_on_its_side_puts_what_plays_beside_the_volume(self):
        side = "(orientation: landscape) and (max-height: 500px)"
        columns = {one: values["grid-column"] for media, one, values in self.rules if media == side and "grid-column" in values}
        self.assertEqual(columns, {".listen-now": "1", ".listen-progress": "1", ".listen-transport": "1", ".listen-chips": "2",
                                   ".listen-volume": "2"})

    def test_a_control_a_lock_holds_is_dimmed_as_watchs(self):
        dimmed = [one for media, one, values in self.rules if values.get("opacity") == "0.38" and media is None]
        self.assertIn(".listen :is(.key, .chip, .fader__step):disabled", dimmed)
        self.assertIn((None, ".target:disabled", {"opacity": "0.38", "cursor": "default", "background": "transparent"}), self.shared)
        # Watch's room without audio recedes its own volume keys, not Listen's.
        self.assertIn('.remote[data-audio="idle"] .lower :is(.rocker-wrap--volume .rocker, .key--mute, .fader__bar)',
                      [one for _, one, _ in self.shared])

    def test_the_folder_path_shows_where_the_focus_waits(self):
        self.assertIn((None, ".library-path__text:focus-visible", {"outline": "2px solid var(--focus)", "outline-offset": "0",
                                                                   "border-radius": "4px"}), self.rules)
        self.assertIn('class="library-path__text" tabindex="-1"', self.page)

    def test_only_the_frame_keeps_the_sides_clear_of_a_notch(self):
        # shell.css pads the content by the side insets below 1180 px; Listen adding them again would double them.
        self.assertRegex((REMOTE / "shell.css").read_text(), r"\.thermite-content \{[^}]*env\(safe-area-inset-right")
        for name in ("remote", "listen"):
            with self.subTest(sheet=name):
                self.assertNotRegex((REMOTE / (name + ".css")).read_text(), r"safe-area-inset-(left|right)")

    def test_the_transport_fits_a_320_px_phone_and_every_target_is_44_px(self):
        tokens = {}
        for media, selector, values in self.shared:
            if media is None and selector == ":root":
                tokens.update(values)
        narrow = "(max-width: 359px)"

        def value(selector, name, media=None, rules=None):
            found = [values[name] for at, one, values in (rules or self.rules) if one == selector and name in values and at in (None, media)]
            return pixels(found[-1].split(" ")[-1 if name == "padding" else 0], tokens)

        side = value(".listen", "padding")
        keys = [value(".listen-transport .key--sm", "width", narrow)] * 4 + [value(".listen-transport .key--play", "width", narrow)]
        gaps = 4 * value(".listen-transport", "gap", narrow)
        # With a few pixels to spare, for a font or a border a little wider.
        self.assertLessEqual(sum(keys) + gaps + 8, 320 - 2 * side)
        self.assertGreaterEqual(min(keys), 44)
        # At 360 px the keys are the remote's own size.
        wide = 4 * value(".key--sm", "width", rules=self.shared) + value(".key--play", "width", rules=self.shared)
        self.assertLessEqual(wide + 4 * value(".listen-transport", "gap") + 8, 360 - 2 * side)
        self.assertIn((None, ".listen-transport .key", {"flex": "none"}), self.rules)
        # A sixth key (a Sonos Arc's TV): the narrow sizes at every width, at 320 and 390 px alike.
        six = ".listen-transport:has(> :nth-child(6))"
        for width, media in ((320, narrow), (390, None)):
            with self.subTest(width=width):
                keys = [value(six + " .key--sm", "width", media)] * 5 + [value(six + " .key--play", "width", media)]
                # The media's own gap where it sets one (css_rules lists media rules first).
                gaps = [values["gap"] for at, one, values in self.rules if one == six and "gap" in values and at in (None, media)]
                pad = value(".listen", "padding-inline", narrow) if media else side
                self.assertLessEqual(sum(keys) + 5 * pixels(gaps[0], tokens) + 8, width - 2 * pad)
                self.assertGreaterEqual(min(keys), 44)
        # Every target Listen sizes is at least 44 px tall, but a mouse's switch.
        heights = {one: pixels(values["min-height"], tokens) for media, one, values in self.rules
                   if "min-height" in values and media != "(hover: hover) and (pointer: fine)"}
        self.assertEqual({one: height for one, height in heights.items() if 0 < height < 44}, {})
        self.assertLessEqual({".chip", ".modes__option", ".listen-member .button", ".library-search__input"}, set(heights))

    def test_the_pages_of_their_own_are_gone_and_listens_files_are_served(self):
        routes = (AREA / "lib/oh_routes.py").read_text()
        self.assertNotIn("audio.html", routes)
        self.assertNotIn("music.html", routes)
        self.assertNotRegex((REMOTE / "navigation.js").read_text(), r'id: "(audio|music)"')
        files = (AREA / "lib/oh_features/remote.py").read_text()
        for name in ("listen.js", "listen.css", "listen-session.js", "audio-session.js", "music-backend.js", "music-session.js"):
            self.assertIn(f'"{name}"', files)
            self.assertIn(f'src="{name}"' if name.endswith(".js") else f'href="{name}"', self.page)


if __name__ == "__main__":
    unittest.main()
