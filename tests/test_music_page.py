"""The Music page's session and backends (web/remote/music-session.js and
music-backend.js), which the web page and the terminal remote both draw,
the page's script (music.js) in a small stand-in for a browser, and the
page's own markup and stylesheet.

The session runs on the mock's invented speakers (two in a kitchen, a Sonos
in a den, a WiiM without AirPlay in a study, a Sonos away on a patio), on
the gateway backend over a fake access client and stream, and access.js's
music calls run on a fake fetch. Every timer runs on the harness's clock.
Nothing leaves the process.
"""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
REMOTE = AREA / "web/remote"
AIRPLAY = "Study cannot join Kitchen Sonos Amp: Sonos and WiiM speakers group only over AirPlay, and one of them has none."
LOCKED = "A locked room is using this device or route, so it cannot change now."
BLIND = "Sign in to thermite to search, browse and see the queue."


@unittest.skipUnless(shutil.which("node"), "node needed for the shared web/TUI session")
class MusicPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/music_page_harness.js")],
                             text=True, capture_output=True, timeout=60)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.report = json.loads(run.stdout)

    def test_speakers_are_named_by_room_and_the_one_playing_is_chosen(self):
        out = self.report["overview"]
        self.assertEqual(out["reading"]["text"], "Reading the speakers…")
        self.assertEqual(out["status"], {"state": "ready", "text": ""})
        rows = [row for block in out["blocks"] for row in block["speakers"]]
        self.assertEqual([row[1] for row in rows], ["Kitchen Sonos Amp", "Kitchen WiiM Mini", "Den", "Study", "Patio"])
        self.assertEqual([row[2] for row in rows], ["Harbour Lights · The Lanterns", "Nothing playing", "Nothing playing",
                                                    "Slow Rivers · Northbound Quartet", "Not available"])
        self.assertEqual(out["selected"], "kitchen_amp")
        view = out["view"]
        self.assertEqual(view["speaker"]["provider"], "Sonos")
        self.assertEqual((view["title"], view["line"], view["playLabel"], view["progress"], view["volume"]),
                         ("Harbour Lights", "The Lanterns · Harbour Lights", "Pause", "1:03 of 4:05", [32, False]))
        self.assertEqual(out["reads"], ["overview", "tidal"])

    def test_progress_moves_while_playing_and_holds_while_paused(self):
        out = self.report["overview"]
        self.assertEqual(out["progress"], [63, 245, "4:05"])
        self.assertEqual(out["advanced"], 68)
        self.assertEqual((out["paused"]["before"], out["paused"]["after"]), (120, 120))
        self.assertEqual((out["paused"]["view"]["playLabel"], out["paused"]["view"]["repeat"]), ("Play", "Repeat all"))
        # Play pressed on the paused speaker: it moves once the speaker plays, not before.
        self.assertEqual(out["pressed"]["play"], [True, 120])
        # Pause pressed: the position stops then, and moves on again if the speaker ignores it.
        self.assertEqual(out["pressed"]["pause"], [False, 0])
        self.assertEqual(out["pressed"]["ignored"], [True, 9])
        self.assertEqual(out["stepped"], ["kitchen_mini", "den_one", "kitchen_mini", "kitchen_amp", "patio_roam"])
        away = out["away"]
        self.assertEqual((away["now"], away["progress"], away["volume"], away["canSkip"]), ("Not available", None, [None, False], False))

    def test_transport_shows_what_was_sent_until_the_speaker_reads_it_back(self):
        out = self.report["transport"]
        self.assertEqual(out["held"], {"playing": False, "label": "Play", "sent": [["transport", "kitchen_amp", "pause", None]]})
        self.assertEqual(out["read"], {"playing": False, "state": "paused", "announced": ["Kitchen Sonos Amp: Pause"]})
        self.assertEqual(out["shuffle"], {"held": ["Shuffle on", True], "read": ["Shuffle on", False]})
        self.assertEqual(out["repeat"], "Repeat one")
        self.assertEqual(out["next"], "Signal Fires")
        self.assertEqual(out["seek"], {"held": [30, True], "read": [30, False], "by": 40})
        self.assertEqual([entry[2:] for entry in out["sent"]], [["shuffle", True], ["repeat", "all"], ["repeat", "one"], ["next", None],
                                                         ["seek", 30], ["seek", 40]])
        # Nothing read back in HOLD_MS: what the speaker last said shows again.
        self.assertEqual(out["deaf"], ["Shuffle off", True, "Shuffle on", False])
        self.assertEqual(out["toasts"], [])

    def test_a_volume_slide_sends_its_first_and_last_level_only(self):
        out = self.report["transport"]
        self.assertEqual(out["slide"]["shown"], [36, True])
        self.assertEqual(out["slide"]["first"], [["volume", "kitchen_amp", "level", 33]])
        self.assertEqual([entry[3] for entry in out["slide"]["sent"]], [33, 36])
        self.assertEqual(out["slide"]["after"], out["slide"]["sent"])
        self.assertEqual(out["slide"]["read"], [36, False])
        self.assertEqual(out["step"], {"volume": 38, "said": "Kitchen Sonos Amp: Volume 38"})
        self.assertEqual(out["elsewhere"], 12)
        # Woken to a level moved elsewhere, a step to the level last sent goes again.
        self.assertEqual(out["wake"], {"woke": 38, "sent": [["volume", "kitchen_amp", "level", 40]], "read": [40, False, 1]})
        # Let go after a rest, nothing goes twice; let go while moving, the last level goes at once.
        self.assertEqual(out["letGo"], [[30, 32, 34, 36], 4, 36])

    def test_search_waits_for_the_typing_and_filters_by_kind(self):
        out = self.report["library"]
        self.assertEqual(out["typing"], "idle")
        self.assertEqual(out["searches"], [["search", "harbour", "", ""]])
        self.assertEqual(out["sections"], [["Tracks", 4, False, ["Low Tide", "Harbour Lights", "Signal Fires", "Drift"]],
                                           ["Albums", 1, False, ["Harbour Lights"]]])
        self.assertEqual(out["sources"], ["Everywhere", "Library"])
        self.assertEqual(out["kinds"][0], ["All", True])
        self.assertEqual((out["track"]["line"], out["track"]["source"], out["track"]["duration"]),
                         ("The Lanterns · Harbour Lights", "Library", "3:41"))
        self.assertEqual(out["albums"], [["Albums", ["Harbour Lights"]]])
        self.assertEqual(out["nothing"], "Nothing found for “zzz”.")
        self.assertEqual(out["cleared"], ["idle", 0])

    def test_browse_walks_folders_and_back_without_reading_again(self):
        out = self.report["library"]
        self.assertEqual(out["roots"], [["Library"], ["NAS"], [[None, "Folder"]]])
        self.assertEqual(out["artists"], ["The Lanterns", "Mira Okafor", "Northbound Quartet"])
        self.assertEqual(out["album"]["path"], ["Library", "NAS", "The Lanterns", "Harbour Lights"])
        self.assertEqual(out["album"]["tracks"], ["Low Tide", "Harbour Lights", "Signal Fires", "Drift"])
        self.assertEqual(out["album"]["first"]["source"], "NAS")
        self.assertEqual(out["back"], {"path": ["Library", "NAS", "The Lanterns"], "items": ["Harbour Lights"], "read": 0})
        self.assertEqual(out["actions"], {"folder": ["Open"], "track": ["Play now", "Play next", "Add to queue"]})
        self.assertEqual(out["playlist"], ["Kitchen Radio", "Library", ["Open", "Play now", "Play next", "Add to queue"]])

    def test_play_now_next_and_add_each_say_what_they_did(self):
        out = self.report["library"]["played"]
        self.assertEqual([entry[3] for entry in out["sent"]], ["next", "add", "replace"])
        self.assertEqual(out["toasts"], [["Play next", "Low Tide plays next on Kitchen Sonos Amp", ""],
                                         ["Add to queue", "Low Tide added to the queue on Kitchen Sonos Amp", ""]])
        self.assertEqual(out["said"], "Kitchen Sonos Amp: Playing Kitchen Radio")
        member = self.report["grouping"]["memberPlay"]
        self.assertEqual(member[0], ["play", "den_one", "library://track/101", "add"])
        self.assertEqual(member[1][1], "Low Tide added to the queue on Den (Kitchen Sonos Amp's group)")

    def test_the_queue_marks_the_current_track_and_is_read_again_when_one_begins(self):
        out = self.report["library"]["queue"]
        self.assertEqual(out["items"], [["Low Tide", True], ["Paper Moons", False], ["Rain on Tin", False], ["Signal Fires", False]])
        self.assertEqual((out["total"], out["text"]), (4, ""))
        self.assertEqual(out["reread"], [["queue", "kitchen_amp"]])
        self.assertEqual(out["current"], ["Paper Moons"])
        self.assertEqual(out["away"], "Patio is not available.")

    def test_grouping_says_who_joined_and_why_the_others_could_not(self):
        out = self.report["grouping"]
        self.assertEqual(out["candidates"], [["kitchen_mini", "Kitchen WiiM Mini", ""], ["den_one", "Den", ""],
                                             ["study_amp", "Study", ""], ["patio_roam", "Patio", "Not available"]])
        self.assertTrue(out["pending"])
        self.assertEqual(out["result"]["joined"], ["Den"])
        self.assertEqual(out["result"]["text"], "Den joined Kitchen Sonos Amp.")
        self.assertEqual([entry["reason"] for entry in out["result"]["refused"]], [AIRPLAY, "Patio is not available."])
        self.assertEqual(out["toast"], ["2 speakers could not join", AIRPLAY + " Patio is not available.", "bad"])
        # The toast says it, and the group sheet's result: nothing more for a screen reader.
        self.assertEqual(out["announced"], [])
        group = out["blocks"][0]
        self.assertEqual((group["group"], [row[1] for row in group["speakers"]]), (True, ["Kitchen Sonos Amp", "Den"]))
        self.assertEqual(len(out["blocks"]), 4)

    def test_a_grouping_no_read_back_confirmed_says_who_was_asked(self):
        out = self.report["unconfirmed"]
        asked = "Den and Study were asked to join; Music Assistant has not confirmed it yet."
        self.assertEqual(out["all"]["sent"], ["group", "kitchen_amp", "den_one,study_amp"])
        self.assertEqual((out["all"]["result"]["joined"], out["all"]["result"]["pending"], out["all"]["result"]["refused"]),
                         ([], ["Den", "Study"], []))
        self.assertEqual((out["all"]["result"]["text"], out["all"]["result"]["waiting"]), ("", asked))
        # Nothing the page knows joined: sent, not grouped.
        self.assertEqual(out["all"]["toast"], ["Group sent", asked, ""])
        waiting = "Study was asked to join; Music Assistant has not confirmed it yet."
        self.assertEqual((out["mixed"]["result"]["text"], out["mixed"]["result"]["waiting"]), ("Den joined Kitchen Sonos Amp.", waiting))
        self.assertEqual(out["mixed"]["toast"], ["Grouped", "Den joined Kitchen Sonos Amp. " + waiting, ""])
        self.assertEqual(out["refused"]["toast"], ["Patio could not join", "Patio is not available. " + waiting, "bad"])
        self.assertEqual((out["after"]["pending"], out["after"]["waiting"]), ([], ""))
        self.assertEqual(out["announced"], [])

    def test_a_member_plays_its_leaders_queue_and_the_group_volume_moves_all(self):
        out = self.report["grouping"]
        self.assertEqual(out["member"]["group"], {"text": "With Kitchen Sonos Amp", "leader": "kitchen_amp", "volume": 32})
        self.assertEqual((out["member"]["queueOf"], out["member"]["title"]), ("Kitchen Sonos Amp", "Harbour Lights"))
        self.assertEqual(out["memberGrouping"], {"leader": "Kitchen Sonos Amp", "members": [["Kitchen Sonos Amp", True], ["Den", False]]})
        self.assertEqual(out["groupVolume"], [50, 36])
        self.assertEqual(out["groupStep"], 48)
        self.assertEqual(out["left"], {"den": None, "blocks": [False] * 5})
        self.assertEqual(out["again"], ["Grouped", "Kitchen WiiM Mini joined Kitchen Sonos Amp.", ""])
        self.assertEqual(out["ended"], [None, "Kitchen Sonos Amp: Group ended"])
        self.assertEqual(out["sent"], [["group", "kitchen_amp", "den_one,study_amp,patio_roam"], ["play", "den_one", "library://track/101", "add"],
                                       ["volume", "den_one", "group", 50],
                                       ["volume", "den_one", "group", 48], ["ungroup", "den_one"],
                                       ["group", "kitchen_amp", "kitchen_mini"], ["ungroup", "kitchen_amp"]])

    def test_a_refused_command_drops_what_it_held_and_says_why(self):
        out = self.report["locks"]
        self.assertFalse(out["held"])
        self.assertEqual(out["refused"], {"playing": True, "toast": ["Kitchen Sonos Amp: Pause", LOCKED, "bad"]})
        self.assertEqual(out["volume"], [32, False, ["Kitchen Sonos Amp: Volume", LOCKED, "bad"]])

    def test_a_locked_room_known_to_the_page_sends_nothing(self):
        out = self.report["locks"]
        self.assertEqual(out["view"]["locked"], "Locked by Kitchen")
        self.assertEqual([row[4] for block in out["rows"] for row in block["speakers"]], [True, True, False, False, False])
        self.assertEqual(out["blocked"]["sent"], [])
        self.assertEqual(out["blocked"]["toasts"], [["Kitchen Sonos Amp", "Locked by Kitchen", "bad"]] * 3
                         + [["Den", "Locked by Kitchen", "bad"]])

    def test_a_group_members_lock_holds_only_what_moves_the_group(self):
        out = self.report["locks"]["member"]
        self.assertEqual((out["locked"], out["group"]), (None, {"rooms": ["Den"], "text": "Group locked by Den"}))
        # The Kitchen speaker's own volume goes; Play and the group's volume wait for the Den.
        self.assertEqual(out["sent"], [["volume", "kitchen_amp", "level", 34]])
        self.assertEqual(out["toasts"], [["Kitchen Sonos Amp", "Locked by Den", "bad"]] * 2)
        self.assertEqual(out["den"], ["Locked by Den", "Locked by Den"])

    def test_a_group_change_is_stopped_by_a_lock_on_any_speaker_it_moves(self):
        out = self.report["involved"]
        self.assertEqual(out["formed"], ["kitchen_mini", "den_one"])
        # The Kitchen WiiM Mini leaves its group with the Den's speaker to join: the Den's lock stops it.
        self.assertEqual(out["blocked"], {"sent": [], "toast": ["Kitchen Sonos Amp", "Locked by Den", "bad"]})
        self.assertEqual(out["refused"], {"sent": [["group", "kitchen_amp", "kitchen_mini"]], "toast": ["Group", "Locked by Den", "bad"],
                                          "result": None})
        self.assertEqual(out["leave"], {"sent": [], "toast": ["Den", "Locked by Kitchen", "bad"]})

    def test_a_read_cut_short_or_refused_is_read_again(self):
        out = self.report["resuming"]
        # Left while the typing settled, or while a search was on its way: opened again, it searches.
        self.assertEqual(out["debounced"], [["harbour"], "done", True])
        self.assertEqual(out["searching"][:2], [["harbour", "lantern", "lantern"], "done"])
        self.assertIn(["Artists", ["The Lanterns"]], out["searching"][2])
        # The answer to harbour comes after signal is typed: it never shows.
        self.assertEqual(out["late"], ["idle", [], ["harbour", "signal"], "done", True])
        self.assertEqual(out["folder"], ["reading", ["Library", "NAS"], "done", True])
        # Refused as a Guest, then signed in: the next visit, or access saying so, reads again.
        self.assertEqual(out["guest"], [BLIND, None, "done"])
        self.assertEqual(out["told"], [BLIND, 0, None, 1, "done"])
        # The queue kept from a Guest by access, or refused by the gateway, is read once they may.
        self.assertEqual(out["queued"], [BLIND, 0, "done", 1, BLIND, 1, "done", 2, True])
        # Started again with the overview failing, the stream's snapshot reads again what was under way.
        self.assertEqual(out["streamed"], [2, "done", ["Library", "NAS"], "done"])
        self.assertEqual(out["streamedRoot"], [["Library"], "done"])
        # Who is signed in changed before the overview came: still one search.
        self.assertEqual(out["early"], [["harbour"], "done"])

    def test_a_guest_without_free_text_sees_whether_it_plays_but_no_library(self):
        out = self.report["guests"]
        self.assertEqual((out["view"]["title"], out["view"]["now"], out["view"]["playLabel"]), (None, "Playing", "Pause"))
        self.assertEqual([block["line"] for block in out["rows"]], ["Playing", "Nothing playing", "Nothing playing", "Paused", "Not available"])
        self.assertEqual(out["library"]["blind"], BLIND)
        self.assertNotIn("search", out["library"])
        # The gateway's 401 closes the library; access saying so first asks nothing.
        self.assertEqual(out["searched"], 1)
        self.assertEqual(out["told"], [BLIND, 0])

    def test_without_music_the_page_says_so_in_one_line(self):
        out = self.report["absent"]
        self.assertEqual(out["unconfigured"], {"status": {"state": "absent", "text": "This thermite host has no music."},
                                               "view": None, "library": None, "speakers": []})
        self.assertEqual(out["native"], {"state": "absent", "text": "Music needs thermite's gateway."})

    def test_music_assistant_away_says_why_and_the_stream_resyncs(self):
        out = self.report["unavailable"]
        self.assertEqual(out["gone"]["status"], {"state": "unavailable",
                                                 "text": "Music Assistant belongs to someone else: reset it so thermite can claim it."})
        self.assertEqual((out["gone"]["view"], out["gone"]["library"]), (None, None))
        self.assertEqual(out["gone"]["lines"], ["Not available"] * 5)
        self.assertEqual(out["back"], {"status": {"state": "ready", "text": ""}, "title": "Harbour Lights"})
        self.assertEqual(out["dropped"], {"state": "ready", "text": "Not connected to thermite. Reconnecting…"})
        self.assertEqual(out["resynced"], {"status": {"state": "ready", "text": ""}, "connected": True})
        self.assertEqual(out["stopped"], 0)
        self.assertEqual(out["restarted"], ["ready", "Signal Fires"])

    def test_an_admin_signs_in_to_tidal_with_a_code(self):
        out = self.report["tidal"]
        self.assertEqual((out["before"]["state"], out["before"]["text"]), ("idle", "TIDAL is not signed in."))
        self.assertEqual(out["checked"], 1)
        self.assertTrue(out["starting"])
        waiting = out["waiting"]
        self.assertEqual((waiting["state"], waiting["code"], waiting["url"]), ("waiting", "MOCK7QX2", "https://link.tidal.com/MOCK7QX2"))
        self.assertEqual(waiting["text"], "Approve this code at link.tidal.com on any device.")
        self.assertEqual(out["said"], "TIDAL code M O C K 7 Q X 2")
        self.assertEqual(out["still"], "waiting")
        self.assertEqual(out["done"], {"tidal": None, "toast": ["TIDAL", "Signed in. TIDAL is in Search and Browse.", ""],
                                       "sent": [["tidal", "start"], ["tidal", "finish"]]})
        self.assertEqual(out["search"]["sources"], ["Everywhere", "Library", "TIDAL"])
        self.assertEqual({section[0]: section[2] for section in out["search"]["found"]}, {"Tracks": "TIDAL", "Albums": "TIDAL", "Artists": "TIDAL"})
        self.assertEqual(out["roots"], ["NAS", "TIDAL"])
        self.assertEqual((out["cancelled"]["tidal"]["state"], out["cancelled"]["sent"]), ("idle", [["tidal", "start"], ["tidal", "finish"]]))
        self.assertEqual(out["member"], [None, 0])

    def test_the_gateway_backend_asks_the_api_and_follows_its_stream(self):
        out = self.report["gateway"]
        self.assertEqual(out["failed"], {"state": "failed", "text": "thermite did not answer in time. Try again in a moment."})
        self.assertEqual(out["stream"], ["https://site.example/api/v1/events/music"])
        # The stream says Music Assistant is away; the overview read again says why.
        self.assertEqual(out["away"], [{"state": "unavailable", "text": "Music Assistant did not answer. thermite keeps trying."},
                                       ["Not available"]])
        self.assertEqual(out["ready"], ["ready", "Low Tide", True])
        self.assertEqual(out["art"], ["https://site.example/api/v1/music/art/" + "a" * 64 + "?size=80", None, None])
        self.assertEqual(out["speakerFrame"], 44)
        self.assertEqual(out["asked"], [
            ["GET", ""],
            ["GET", ""],
            ["POST", "kitchen_amp/transport", {"action": "pause"}],
            ["POST", "kitchen_amp/transport", {"action": "seek", "value": 42}],
            ["POST", "kitchen_amp/volume", {"level": 30}],
            ["POST", "kitchen_amp/transport", {"action": "shuffle", "value": True}],
            ["POST", "kitchen_amp/transport", {"action": "repeat", "value": "all"}],
            ["POST", "kitchen_amp/play", {"ref": "library://track/1", "option": "next"}],
            ["GET", "/browse?offset=0&limit=100", 25000],
            ["GET", "/browse?offset=100&limit=100", 25000],
            ["GET", "/search?q=low+tide", 25000]])
        self.assertEqual(out["played"], [["Play next", "Low Tide plays next on Kitchen", ""]])
        self.assertEqual((out["page"], out["paged"]), ([100, True], [150, False]))
        self.assertEqual(out["guest"], BLIND)

    def test_the_gateway_stream_reopens_when_closed_and_lets_go_when_hidden(self):
        out = self.report["gateway"]
        self.assertEqual(out["blip"], ["Not connected to thermite. Reconnecting…", 1])
        self.assertEqual((out["waited"], out["reopened"]), (1, 2))
        self.assertEqual(out["unavailable"]["state"], "unavailable")
        self.assertEqual((out["hidden"], out["shown"], out["stopped"]), ([True, 2], 3, True))

    def test_access_sends_music_commands_with_csrf_and_names_their_problems(self):
        out = self.report["client"]
        self.assertEqual(out["overview"]["problem"], "music_unavailable")
        self.assertEqual(out["search"], ["login_required", 401, "Sign in to thermite."])
        self.assertEqual(out["locked"], ["device_locked", 423, LOCKED])
        self.assertEqual(out["unclaimed"], ["music_unclaimed", 503, "Music Assistant belongs to someone else: reset it so thermite can claim it."])
        self.assertEqual(out["calls"], [["GET", "/api/v1/music", None, None], ["GET", "/api/v1/music/search?q=low+tide", None, None],
                                        ["POST", "/api/v1/music/kitchen_amp/transport", {"action": "pause"}, "C" * 32],
                                        ["POST", "/api/v1/music/den_one/volume", {"level": 3}, "C" * 32]])
        self.assertEqual([code for code, known in out["messages"] if code != known], [])


@unittest.skipUnless(shutil.which("node"), "node needed for the page's script")
class MusicPageScript(unittest.TestCase):
    """music.js on music.html in tests/music_dom_harness.js's small stand-in for a browser."""

    @classmethod
    def setUpClass(cls):
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/music_dom_harness.js")],
                             text=True, capture_output=True, timeout=60)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.report = json.loads(run.stdout)

    def test_the_card_names_only_what_the_speaker_is_beside_the_bars_switch(self):
        # The switch and the card's place both hold the name; the CSS shows the place only beside no switch.
        self.assertEqual(self.report["now"]["place"], ["Kitchen Sonos Amp", "Kitchen Sonos Amp", "Sonos"])

    def test_a_position_dragged_and_let_go_unchanged_shows_the_speakers_again(self):
        self.assertEqual(self.report["now"]["dragged"], "3:20")
        self.assertEqual(self.report["now"]["released"], [True, True])

    def test_a_volume_let_go_after_a_rest_sends_its_level_once(self):
        self.assertEqual(self.report["now"]["letGo"], [["volume", "kitchen_amp", 30], ["volume", "kitchen_amp", 32]])

    def test_the_focus_follows_a_folder_opened_back_and_a_sheets_open(self):
        out = self.report["browse"]
        lanterns, nas = "item:filesystem_local--flac://folder/The Lanterns", "item:filesystem_local--flac://folder/"
        # On the path while the folder reads, then on its first entry.
        self.assertEqual(out["opening"], ["Library › NAS", "path"])
        self.assertEqual(out["opened"], ["Library › NAS", lanterns])
        # Back to the top disables Back: the focus goes to the first root.
        self.assertEqual(out["back"], ["Library", nas, True])
        self.assertEqual(out["album"][1], "item:filesystem_local--flac://track/The Lanterns/Harbour Lights/01 Low Tide.flac")
        # A sheet that plays hands the focus back to its entry; one that opens, to the folder's.
        self.assertEqual(out["sheet"], [True, "button:Play now"])
        self.assertEqual(out["played"], [False, out["album"][1]])
        self.assertEqual(out["found"], [True, "button:Open"])
        self.assertEqual(out["sheetOpening"][::2], [False, "path"])
        self.assertEqual(out["sheetOpened"], "item:library://track/101")
        # Taken elsewhere, to a control in sight, while a folder reads, the focus stays there.
        self.assertEqual(out["elsewhere"], ["Library › NAS", "path", True, "button:Volume up"])
        # The stand-in browser gives no focus to what is hidden, or behind an open sheet.
        self.assertEqual(out["inert"], ["button:Volume up", "button:Volume up", "button:Search"])

    def test_a_cover_the_gateway_was_too_busy_for_is_asked_for_once_more(self):
        out = self.report["covers"]
        # Hidden while it failed, shown once asked again; never asked a third time.
        self.assertEqual(out["cover"], [[True, 1], [False, 2], 2])
        # A thumbnail that fails twice stays hidden over its kind's icon.
        self.assertEqual(out["thumbs"], [True, ["true,2"]])

    def test_a_folder_takes_the_focus_only_while_browse_shows_and_it_reads(self):
        out = self.report["following"]
        self.assertEqual(out["reading"], "path")
        # Search shown meanwhile: back on Browse, the folder read has not moved the focus to its entries.
        self.assertEqual(out["tabbed"], ["Library › NAS", "path"])
        # A read that failed leaves the focus on the path once, then lets it be.
        self.assertEqual(out["failed"], [True, "path", "body"])
        # Following a folder never scrolls the page.
        self.assertEqual(out["scrolls"], [0, 0])

    def test_the_group_sheet_says_who_was_asked_and_not_confirmed(self):
        self.assertEqual(self.report["grouped"]["result"], [False, [
            ["Den joined Kitchen Sonos Amp.", None], ["Study was asked to join; Music Assistant has not confirmed it yet.", None]]])


def css_rules(text):
    """(media, selector, declarations) for each rule of a stylesheet, one level of @media deep."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    block = r"@media([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
    rules = []

    def read(body, media):
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            values = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", declarations))
            rules.extend((media, " ".join(one.split()), values) for one in selector.split(","))

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


class MusicMarkup(unittest.TestCase):
    def test_every_part_the_script_draws_into_is_in_the_page(self):
        script = (REMOTE / "music.js").read_text()
        page = (REMOTE / "music.html").read_text()
        parts = set(re.findall(r'(?:\$|querySelectorAll)\("(?:#[\w-]+ )?\[(data-[a-z-]+)', script))
        self.assertGreater(len(parts), 40)
        self.assertEqual(sorted(part for part in parts if not re.search(r"\s" + part + r"[\s=>]", page)), [])
        ids = set(re.findall(r'\$\("#([\w-]+)', script))
        self.assertEqual(sorted(ident for ident in ids if f'id="{ident}"' not in page), [])
        icons = set(re.findall(r'icon\("([\w-]+)"\)', script)) | set(re.findall(r'KIND_ICON = \{([^}]*)\}', script)[0].split('"')[1::2])
        icons |= {"play", "pause", "repeat", "repeat-one", "warn", "lock"}
        self.assertEqual(sorted(name for name in icons if f'id="i-{name}"' not in page), [])

    def test_hidden_hides_every_part_of_the_page_the_sheets_included(self):
        # .button and other classes set display; the page's rule must reach every part marked hidden.
        rule = re.search(r"^\.([\w-]+) \[hidden\] \{\s*display: none !important;", (REMOTE / "music.css").read_text(), re.M)
        self.assertIsNotNone(rule)
        scope = rule.group(1)

        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.open, self.hidden = [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                classes = (attrs.get("class") or "").split()
                if "hidden" in attrs:
                    self.hidden.append(("data-group-end" in attrs, any(scope in each for each in self.open + [classes])))
                if tag not in ("br", "img", "input", "link", "meta", "source", "use", "path"):
                    self.open.append(classes)

            def handle_endtag(self, tag):
                if tag not in ("use", "path") and self.open:
                    self.open.pop()

        page = Page()
        page.feed((REMOTE / "music.html").read_text())
        self.assertGreater(len(page.hidden), 10)
        self.assertIn((True, True), page.hidden)
        self.assertEqual([covered for _, covered in page.hidden if not covered], [])

    def test_what_shows_first_sits_12_px_under_the_bar(self):
        # The head lent to the shell's bar, the first block shown sits at the page's padding: no margin above it.
        shared, rules = css_rules((REMOTE / "remote.css").read_text()), css_rules((REMOTE / "music.css").read_text())
        padding = [values["padding"] for media, one, values in shared if one == ".child-page"][0]
        self.assertEqual(padding.split()[0], "var(--space-3)")
        music = {name: value for media, one, values in rules if one == ".music" and media is None for name, value in values.items()}
        self.assertEqual((music["display"], music["flex-direction"], music["gap"]), ("flex", "column", "var(--space-3)"))
        zeroed = {one for media, one, values in rules if values.get("margin") == "0" and media is None}
        self.assertLessEqual({".music > .child-page__mock", ".music > .child-page__alert"}, zeroed)
        self.assertEqual([one for media, one, values in rules if one.startswith(".music__layout") and "margin-top" in values], [])
        self.assertIn((None, ".thermite-app .music__head[data-lent]", {"display": "none"}), rules)

    def test_a_control_a_locked_room_holds_is_dimmed_once(self):
        shared = css_rules((REMOTE / "remote.css").read_text())
        self.assertIn((None, ".child-page__controls:disabled", {"opacity": "0.45"}), shared)
        # Inside the dimmed fieldset a control's own :disabled (a page's, of three classes) adds nothing.
        self.assertIn((None, ".child-page .child-page__controls:disabled :disabled", {"opacity": "1"}), shared)
        # Disabled, a key is as dim as the remote's.
        page = (REMOTE / "music.css").read_text()
        self.assertRegex(page, r":is\(\.music-transport \.key, \.music-path \.icon-button\):disabled \{\s*opacity: 0\.38;")
        self.assertIn((None, ".target:disabled", {"opacity": "0.38", "cursor": "default", "background": "transparent"}), shared)

    def test_the_transport_fits_a_320_px_phone_and_every_target_is_44_px(self):
        shared, rules = css_rules((REMOTE / "remote.css").read_text()), css_rules((REMOTE / "music.css").read_text())
        tokens = {}
        for media, selector, values in shared:
            if media is None and selector == ":root":
                tokens.update(values)

        def value(selector, name, media=None):
            found = [values[name] for at, one, values in rules if one == selector and name in values and at in (None, media)]
            return pixels(found[-1].split(" ")[0] if name != "padding-inline" else found[-1], tokens)

        narrow = "(max-width: 374px)"
        # The page's own padding at the sides (remote.css's, shared with Audio).
        page = pixels(re.split(r"\s+(?![^(]*\))", [values["padding"] for media, one, values in shared if one == ".child-page"][0])[1], tokens)
        card = value(".music-now", "padding", narrow)
        keys = [value(".music-key", "width")] * 2 + [value(".music-transport .key--sm", "width", narrow)] * 2 + [value(".music-transport .key--play", "width", narrow)]
        gaps = 4 * value(".music-transport", "gap")
        self.assertLessEqual(sum(keys) + gaps, 320 - 2 * page - 2 * card)
        self.assertGreaterEqual(min(keys), 44)
        self.assertIn(("flex", "none"), [(name, values[name]) for _, one, values in rules if one == ".music-transport .key" for name in values])
        # Every target the page sizes is at least 44 px tall.
        heights = {one: pixels(values["min-height"], tokens) for _, one, values in rules if "min-height" in values}
        self.assertEqual({one: height for one, height in heights.items() if height < 44}, {})
        self.assertIn(".music-member .button", heights)

    def test_the_page_is_registered_where_the_remotes_pages_are(self):
        self.assertIn('{ id: "music", screen: "music", key: "Y"', (REMOTE / "navigation.js").read_text())
        self.assertIn('("music", "/music/", "music.html")', (AREA / "lib/oh_routes.py").read_text())
        files = (AREA / "lib/oh_features/remote.py").read_text()
        for name in ("music.html", "music-backend.js", "music-session.js", "music.js", "music.css"):
            self.assertIn(f'"{name}"', files)


if __name__ == "__main__":
    unittest.main()
