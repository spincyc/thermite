"""The Music page's session and backends (web/remote/music-session.js and
music-backend.js), which the web page and the terminal remote both draw,
and the page's own markup.

The session runs on the mock's invented speakers (two in a kitchen, a Sonos
in a den, a WiiM without AirPlay in a study, a Sonos away on a patio), on
the gateway backend over a fake access client and stream, and access.js's
music calls run on a fake fetch. Every timer runs on the harness's clock.
Nothing leaves the process.
"""

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
        group = out["blocks"][0]
        self.assertEqual((group["group"], [row[1] for row in group["speakers"]]), (True, ["Kitchen Sonos Amp", "Den"]))
        self.assertEqual(len(out["blocks"]), 4)

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

    def test_a_group_change_is_stopped_by_a_lock_on_any_speaker_it_moves(self):
        out = self.report["involved"]
        self.assertEqual(out["formed"], ["kitchen_mini", "den_one"])
        # The Kitchen WiiM Mini leaves its group with the Den's speaker to join: the Den's lock stops it.
        self.assertEqual(out["blocked"], {"sent": [], "toast": ["Kitchen Sonos Amp", "Locked by Den", "bad"]})
        self.assertEqual(out["refused"], {"sent": [["group", "kitchen_amp", "kitchen_mini"]], "toast": ["Group", "Locked by Den", "bad"],
                                          "result": None})
        self.assertEqual(out["leave"], {"sent": [], "toast": ["Den", "Locked by Kitchen", "bad"]})

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

    def test_the_page_is_registered_where_the_remotes_pages_are(self):
        self.assertIn('{ id: "music", screen: "music", key: "Y"', (REMOTE / "navigation.js").read_text())
        self.assertIn('("music", "/music/", "music.html")', (AREA / "lib/oh_routes.py").read_text())
        files = (AREA / "lib/oh_features/remote.py").read_text()
        for name in ("music.html", "music-backend.js", "music-session.js", "music.js", "music.css"):
            self.assertIn(f'"{name}"', files)


if __name__ == "__main__":
    unittest.main()
