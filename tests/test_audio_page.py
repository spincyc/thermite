"""The Audio page's session and its backend (web/remote/audio-session.js and
backend.js command(), audioOf()), which the web page and the terminal remote
both draw.

The public example house with two WiiMs: one in a den no routed room
covers, one example_east's `audio` (so that room's lock is its own). They
are driven through the mock backend, then through the openHAB backend on a
fake fetch and stream. Nothing leaves the process.
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
from oh_integrations import sonos  # noqa: E402
from tests.test_playing import SPEAKER as ARC  # noqa: E402

FREE, OWNED, ROOM = "fixture_den_amp", "fixture_amp", "example_east"
WIIM = {"kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro",
        "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}


def model():
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["rooms"]["fixture_den"] = {"label": "Fixture Den", "tag": "Room"}
    data["devices"] += [{"id": FREE, "room": "fixture_den", "name": "Fixture Den Amp", "ip": "192.0.2.42", **WIIM},
                        {"id": OWNED, "room": ROOM, "ip": "192.0.2.41", **WIIM}]
    data["routing"]["rooms"][ROOM]["audio"] = OWNED
    return remote_model.build(House(data))


def bar():
    """The example house with a controlled Sonos Arc in a den."""
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["rooms"]["fixture_den"] = {"label": "Fixture Den", "tag": "Room"}
    data["devices"].append({**ARC, "room": "fixture_den"})
    return remote_model.build(House(data))


def routed():
    """The desk: example_east's amplifier is its `audio`, on the input its
    route feeds (`audio_input`), and its computer carries a picture alone."""
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["devices"].append({"id": OWNED, "room": ROOM, "ip": "192.0.2.41", **WIIM})
    east = data["routing"]["rooms"][ROOM]
    east["audio"] = OWNED
    east["carries"] = {"example_computer": "display"}
    east["audio_input"] = "optical"
    return remote_model.build(House(data))


def settings(view):
    return {setting["item"].removeprefix(FREE + "_"): setting for section in view["sections"]
            for setting in section["settings"]}


@unittest.skipUnless(shutil.which("node"), "node needed for the shared web/TUI session")
class AudioPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        with tempfile.TemporaryDirectory() as scratch:
            arc = Path(scratch) / "arc.json"
            arc.write_text(json.dumps(bar()))
            route = Path(scratch) / "route.json"
            route.write_text(json.dumps(routed()))
            run = subprocess.run([shutil.which("node"), str(AREA / "tests/audio_page_harness.js"), OWNED, FREE, ROOM,
                                  str(arc), str(route)], input=json.dumps(model()), text=True, env=env,
                                 capture_output=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.report = json.loads(run.stdout)
        cls.mock = cls.report["mock"]

    def test_an_arc_on_a_silent_tv_input_plays_nothing_unless_it_names_a_track(self):
        out = self.report["arc"]
        arc = ARC["id"]
        self.assertEqual(out["silent"], {"item": f"{arc}_codec", "is": list(sonos.TV_SILENT)})
        # Its TV audio format is read with the page's Items, a fact or not.
        self.assertTrue(out["watched"])
        # Its TV is off but holds its link open, or sends nothing at all: silent,
        # which Listen trusts over Music Assistant.
        self.assertEqual(out["silence"], [False, "Nothing playing", True])
        self.assertEqual(out["noSignal"], [False, "Nothing playing", True])
        # TV sound is TV audio as before; a format not read yet (a runtime just
        # started reads the state first) is not known to be either.
        self.assertEqual(out["sound"], [True, "Playing", False])
        self.assertEqual(out["unread"], [None, "Reading…", None])
        # A track is a track whatever the TV input carries.
        self.assertEqual(out["track"], [True, "Sample Track", False])
        self.assertEqual(out["stopped"], [False, "Nothing playing", False])
        # A silent TV input has nothing to play or pause: Play is off and sends nothing.
        self.assertEqual(out["silentKey"], {"key": {"id": "play_pause", "label": "Play", "icon": "play", "disabled": True},
                                            "sent": [], "playing": False})
        self.assertEqual(out["soundKey"], {"id": "play_pause", "label": "Pause", "icon": "pause"})

    def test_an_amp_on_the_input_a_room_feeds_plays_nothing_while_it_shows_a_source_of_picture_alone(self):
        out = self.report["route"]
        self.assertEqual(out["route"], [{"room": ROOM, "item": f"loc_{ROOM}_source", "is": ["example_computer"],
                                         "input": {"item": f"{OWNED}_input", "is": ["optical"]}}])
        # The room shows the computer and the amp is on the input its route
        # feeds: nothing plays, and Play is off, as a silent TV input's is.
        self.assertEqual(out["quiet"], [False, "Nothing playing", True])
        self.assertEqual(out["quietKey"], {"id": "play_pause", "label": "Play", "icon": "play", "disabled": True})
        # The room switching off, and back, is heard.
        self.assertEqual(out["off"], [True, "Playing from Optical", False])
        self.assertEqual(out["again"], [False, "Nothing playing", True])
        # On another input its sound is its own.
        self.assertEqual(out["otherInput"], [True, "Playing from Bluetooth", False])
        # What tells (the room's Source) not read yet: not known to be either.
        self.assertEqual(out["unread"], [None, "Reading…", None])
        # A track is a track whatever the route carries.
        self.assertEqual(out["track"], [True, "Sample Track", False])

    def test_each_place_lists_its_devices_and_what_they_play(self):
        places = {place["place"]: place["devices"] for place in self.mock["devices"]}
        self.assertEqual(list(places), ["Fixture Den", "Example East"])
        den = places["Fixture Den"][0]
        self.assertEqual((den["id"], den["name"], den["current"], den["playing"], den["title"]),
                         (FREE, "Fixture Den", True, True, "Sample Track"))

    def test_the_page_shows_the_track_its_controls_settings_and_facts(self):
        view = self.mock["first"]
        self.assertEqual(view["device"]["name"], "Fixture Den")
        self.assertEqual(view["track"], {"title": "Sample Track", "line": "Sample Artist · Sample Album",
                                         "service": "Preview", "duration": "4:05", "format": "44.1 kHz · 16-bit"})
        self.assertEqual([button["label"] for button in view["transport"]], ["Previous", "Pause", "Next"])
        self.assertEqual((view["volume"]["value"], view["volume"]["text"], view["muted"], view["input"]["label"]),
                         (60, "60", False, "Wi-Fi"))
        self.assertIsNone(view["power"])
        self.assertEqual([section["id"] for section in view["sections"]], ["playback", "sound", "device"])
        shown = settings(view)
        self.assertEqual({name: (setting["kind"], setting["text"]) for name, setting in shown.items()},
                         {"play_mode": ("enum", "In order"), "eq": ("switch", "Off"), "eq_preset": ("enum", "Flat"),
                          "sub_level": ("number", "0 dB"), "light": ("switch", "Off")})
        # Facts show only once read; the preview reads only what a `when` needs.
        self.assertEqual(view["facts"], [{"label": "Subwoofer Connected", "text": "Yes"}])

    def test_a_setting_shows_what_was_sent_until_it_is_read_back(self):
        held, read = self.mock["presetHeld"], self.mock["presetRead"]
        self.assertEqual((held["text"], held["pending"]), ("Bass Reducer", True))
        self.assertEqual([option["value"] for option in held["options"] if option["current"]], ["Bass Reducer"])
        self.assertEqual((read["text"], read["pending"]), ("Bass Reducer", False))
        self.assertEqual(self.mock["stepped"], ["On", "−1 dB"])

    def test_a_level_stops_at_its_end(self):
        at_min = self.mock["atMin"]
        self.assertEqual((at_min["setting"]["text"], at_min["setting"]["atMin"], at_min["sentMore"]), ("−15 dB", True, 0))

    def test_play_pause_shows_what_it_asked_for(self):
        self.assertEqual(self.mock["paused"], {"playing": False, "transport": ["Previous", "Play", "Next"]})
        self.assertFalse(self.mock["pausedRead"])

    def test_a_setting_shows_only_while_its_when_holds(self):
        self.assertEqual(self.mock["bluetooth"], {"input": "Bluetooth", "sections": ["sound", "device"]})

    def test_a_dragged_volume_sends_its_first_and_last(self):
        sliding = self.mock["sliding"]
        self.assertEqual((sliding["shown"], sliding["pending"], sliding["sent"]), (64, True, ["61"]))
        self.assertEqual(self.mock["slid"], ["61", "64"])

    def test_every_command_goes_to_the_device_by_its_action(self):
        self.assertEqual(self.mock["sent"][:6], [
            [FREE, f"{FREE}_eq_preset", "Bass Reducer"], [FREE, f"{FREE}_eq", "ON"], [FREE, f"{FREE}_sub_level", "-1"],
            [FREE, f"{FREE}_sub_level", "-15"], [FREE, "key", "play_pause"], [FREE, "input", "bluetooth"]])

    def test_a_session_opened_again_draws_and_sends(self):
        self.assertEqual(self.mock["restarted"], {"stopped": 0, "selected": OWNED, "changes": 2, "title": "Another Track",
                                                  "sent": 1})

    def test_a_locked_room_that_owns_the_device_locks_its_page(self):
        locked = self.report["locked"]
        # Locks are watched once access is ready, and never by a session already stopped.
        self.assertEqual((locked["watchedAfterEarlyStop"], locked["watchingAtStart"]), (False, False))
        self.assertEqual(locked["view"], {"rooms": ["Example East"], "text": "Locked by Example East"})
        self.assertEqual(dict(locked["list"]), {FREE: False, OWNED: True})
        self.assertEqual(locked["refused"], {"toasts": [["Locked by Example East", "muted"]] * 3, "sent": 0})
        self.assertEqual(locked["free"], {"locked": None, "sent": [f"{FREE}_light"]})
        self.assertEqual((locked["watching"], locked["unwatched"]), (True, False))

    def test_a_refusal_or_silence_shows_what_the_device_reads(self):
        unheard = self.report["unheard"]
        self.assertEqual(unheard["refused"]["toasts"], [["Status Light", "Refused by the gateway", "bad"]])
        self.assertEqual((unheard["refused"]["light"]["text"], unheard["refused"]["light"]["pending"]), ("Off", False))
        self.assertEqual((unheard["held"], unheard["expired"]), (True, False))

    def test_a_volume_the_device_moved_off_is_sent_again_and_each_step_is_said(self):
        review = self.report["review"]
        self.assertEqual(review["resent"], ["61", "61"])
        self.assertEqual(review["announced"], ["Fixture Den: Volume 61"] * 2)

    def test_a_second_play_pause_undoes_the_first_and_a_refused_one_shows_what_plays(self):
        review = self.report["review"]
        self.assertTrue(review["twice"])
        self.assertEqual(review["refusedPress"], {"playing": True, "label": "Pause"})

    def test_another_device_chosen_mid_slide_still_gets_its_last_volume(self):
        self.assertEqual(self.report["review"]["switched"], [[FREE, "volume", "30"], [FREE, "volume", "35"],
                                                             [OWNED, "volume", "20"]])

    def test_the_headline_is_the_title_else_whether_it_plays(self):
        review = self.report["review"]
        self.assertEqual(review["headline"], "Sample Track")
        # A WiiM names what it does not know Unknown: no track, but it plays.
        self.assertEqual(review["unknown"], {"track": None, "now": "Playing from Wi-Fi", "title": None})
        self.assertEqual(review["paused"], "Nothing playing")

    def test_a_stopped_session_leaves_no_timer(self):
        review = self.report["review"]
        self.assertGreater(review["timersBeforeStop"], 0)
        self.assertEqual(review["timersAfterStop"], 0)

    def test_a_session_stopped_and_started_again_loses_and_repeats_nothing(self):
        restart = self.report["restart"]
        # Left mid-slide, the last step is sent; back after the device moved, the same step is sent again.
        self.assertEqual((restart["leftMidSlide"], restart["back"]), (["61", "62"], ["62"]))
        self.assertEqual(restart["listeners"], [1, 1])
        self.assertTrue(restart["connected"] is False)

    def test_a_volume_not_known_is_not_moved_and_late_echoes_send_nothing_twice(self):
        restart = self.report["restart"]
        self.assertEqual(restart["unknownVolume"], 0)
        self.assertEqual(restart["slow"], ["61", "66"])

    def test_openhab_commands_go_through_the_gateway_or_natively_to_the_item(self):
        openhab = self.report["openhab"]
        self.assertEqual(openhab["asked"], [[FREE, f"{FREE}_eq_preset", "Rock"], [FREE, "volume", 30]])
        self.assertEqual(openhab["posted"], [[f"/rest/items/{FREE}_key", "play_pause"], [f"/rest/items/{FREE}_light", "OFF"]])
        self.assertEqual(openhab["unknown"], "The Fixture Den Amp has no reboot")

    def test_openhab_reads_a_pages_items_from_the_snapshot_and_the_stream(self):
        openhab = self.report["openhab"]
        page = model()["devices"][FREE]["audio"]
        reads = {page["state"]["item"], *page["track"].values(), *(setting["item"] for setting in page["settings"]),
                 *(fact["item"] for fact in page["facts"])}
        self.assertLessEqual(reads, set(openhab["watched"]))
        self.assertEqual(openhab["unread"], "NULL")
        # The same value twice is heard once.
        self.assertEqual(openhab["heard"], [[FREE, f"{FREE}_title"], [FREE, f"{FREE}_artist"]])
        self.assertEqual((openhab["audio"][f"{FREE}_title"], openhab["audio"][f"{FREE}_artist"]),
                         ("From the snapshot", "From the stream"))


if __name__ == "__main__":
    unittest.main()
