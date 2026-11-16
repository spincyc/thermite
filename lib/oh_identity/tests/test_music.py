"""Music through the gateway: the contract names a music host's speakers and
libraries; the gateway matches each speaker to its Music Assistant player,
serves finite projections of what they play and commands them with a
device's locks and trail, against the stateful stand-in (oh_music_sim).
Fictional devices, documentation addresses and demo MACs only."""
import asyncio
import collections
import copy
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

import oh_music_sim
import oh_room_policy
from oh_identity import music, music_hub, streams, topics
from oh_identity.auth import Guest
from oh_identity.models import RoomPolicy, SecurityState
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter, plain
from oh_identity.security import get_state, set_guest_text
from oh_identity.services import Problem, bootstrap
from oh_identity.tests.test_core import PASSPHRASE
from oh_identity.tests.test_trail import Requests, lines
from oh_music import provision
from oh_music.client import SLOW, Failed

WEST, EAST, KITCHEN, ESTATE = "example_west", "example_east", "example_kitchen", "example_estate"
WIIM, MINI, AMP = "example_desk_wiim", "example_kitchen_mini", "example_kitchen_amp"
ALBUM, TRACK = "library://album/100", "library://track/102"


def mac(n):
    return bytes((2, 0, 0, 0, 0, n)).hex(":")


def house():
    """The example house with a WiiM Amp Pro as the East room's sound, and a
    WiiM Mini (no MAC listed) and a Sonos Amp in a kitchen no routed room
    covers."""
    from oh_integrations import sonos
    data = json.loads((Path(__file__).resolve().parents[3] / "instance/devices.yaml").read_text())
    data["rooms"][KITCHEN] = {"label": "Example Kitchen", "tag": "Kitchen"}
    wiim = {"observe": ["wiim_http"], "control": ["wiim_http"]}
    data["devices"] += [
        {"id": WIIM, "room": EAST, "kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro", "ip": "192.0.2.32",
         "mac": mac(32), "controls": wiim},
        {"id": MINI, "room": KITCHEN, "kind": "audio_streamer", "make": "WiiM", "model": "Mini", "ip": "192.0.2.33",
         "controls": wiim},
        {"id": AMP, "room": KITCHEN, "kind": "amp", "make": "Sonos", "model": "Amp", "ip": "192.0.2.21", "mac": mac(21),
         "sonos_udn": sonos.udn(mac(21)), "controls": {"observe": ["sonos"], "control": ["sonos"]}}]
    data["routing"]["rooms"][EAST]["audio"] = WIIM
    return data


def compiled(data, port=13627, libraries=("flac",)):
    import oh_render
    import oh_site_contract
    from oh_conf import House
    host = None if port is None else {"port": port, "libraries": list(libraries)}
    return oh_site_contract.compile(House(data), oh_render.build(data), music_host=host)


class Contract(SimpleTestCase):
    def test_a_music_host_gets_its_port_libraries_and_every_speaker(self):
        from oh_integrations import sonos
        authority = compiled(house(), libraries=("mp3", "flac"))
        self.assertEqual(authority["music"], {"port": 13627, "libraries": ["flac", "mp3"], "speakers": {
            WIIM: {"label": "WiiM Amp Pro", "room": EAST, "room_label": "Example East", "provider": "wiim",
                   "mac": mac(32), "ip": "192.0.2.32", "rooms": {EAST: "own"}},
            MINI: {"label": "WiiM Mini", "room": KITCHEN, "room_label": "Example Kitchen", "provider": "wiim",
                   "ip": "192.0.2.33", "rooms": {}},
            AMP: {"label": "Sonos Amp", "room": KITCHEN, "room_label": "Example Kitchen", "provider": "sonos",
                  "udn": sonos.udn(mac(21)), "ip": "192.0.2.21", "rooms": {}}}})
        self.assertEqual(RuntimeContract(**authority).music["speakers"][WIIM]["rooms"], {EAST: "own"})

    def test_a_host_without_music_has_none(self):
        authority = compiled(house(), port=None)
        self.assertNotIn("music", authority)
        self.assertIsNone(RuntimeContract(**authority).music)

    def test_a_contract_naming_what_it_lacks_is_refused(self):
        authority = compiled(house())
        for change in (lambda spec: spec["speakers"][WIIM]["rooms"].update({"example_nowhere": "own"}),
                       lambda spec: spec["speakers"][WIIM]["rooms"].update({EAST: "owner"}),
                       lambda spec: spec["speakers"][AMP].update(udn="uuid:RINCON_0200000000210"),
                       lambda spec: spec["speakers"][MINI].pop("ip"),
                       lambda spec: spec["speakers"][MINI].update(ip="not-an-address"),
                       lambda spec: spec["speakers"][WIIM].update(provider="chromecast"),
                       lambda spec: spec["speakers"][WIIM].update(label="Desk\n"),
                       lambda spec: spec["libraries"].append("../etc"),
                       lambda spec: spec.update(port=80),
                       lambda spec: spec.update(extra=True)):
            broken = copy.deepcopy(authority)
            change(broken["music"])
            with self.assertRaisesRegex(ValueError, "music contract"):
                RuntimeContract(**broken)

    def test_the_service_compiles_a_staged_hosts_music(self):
        from oh_access.service import compile_contract
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        os.chmod(root, 0o700)
        (root / "house.yaml").write_text(json.dumps(house()))
        (root / "assets").mkdir()
        contract = compile_contract(root / "house.yaml", root / "assets", root / "contract.json",
                                    music_host={"port": 14000, "libraries": ["flac"]})
        self.assertEqual((contract["music"]["port"], contract["music"]["libraries"]), (14000, ["flac"]))
        self.assertEqual(json.loads((root / "contract.json").read_text())["music"], contract["music"])
        self.assertNotIn("music", compile_contract(root / "house.yaml", root / "assets", root / "plain.json"))


class FakeAdapter:
    def __init__(self, authority):
        self.contract = RuntimeContract(**authority)


class Matching(SimpleTestCase):
    def setUp(self):
        self.music = music.Music(FakeAdapter(compiled(house())), "/nonexistent")

    def player(self, provider, player_id, **identifiers):
        return {"player_id": player_id, "provider": provider, "type": "player",
                "device_info": {"identifiers": identifiers}}

    def test_a_sonos_by_its_udn_and_a_wiim_by_its_mac_else_its_address(self):
        from oh_integrations import sonos
        self.assertEqual(self.music.match(self.player("sonos", sonos.udn(mac(21)))), AMP)
        self.assertIsNone(self.music.match(self.player("sonos", sonos.udn(mac(99)))))
        # A MAC in any case or separator; the address does not matter then.
        self.assertEqual(self.music.match(self.player("wiim", "wiim_a", mac_address=mac(32).upper(),
                                                      ip_address="192.0.2.99")), WIIM)
        self.assertEqual(self.music.match(self.player("wiim", "wiim_b", mac_address=mac(32).replace(":", "-"))), WIIM)
        # The Mini lists no MAC: its address matches it, whatever MAC it reports.
        self.assertEqual(self.music.match(self.player("wiim", "wiim_c", mac_address=mac(33), ip_address="192.0.2.33")), MINI)
        self.assertEqual(self.music.match(self.player("wiim", "wiim_d", ip_address="192.0.2.32")), WIIM)
        # Another WiiM now at the Amp Pro's address is not the Amp Pro.
        self.assertIsNone(self.music.match(self.player("wiim", "wiim_e", mac_address=mac(40), ip_address="192.0.2.32")))

    def test_protocol_and_group_players_and_other_providers_are_never_speakers(self):
        from oh_integrations import sonos
        for kind in ("protocol", "group"):
            self.assertIsNone(self.music.match({**self.player("sonos", sonos.udn(mac(21))), "type": kind}))
        self.assertIsNone(self.music.match(self.player("airplay", "ap020000000020", mac_address=mac(32))))
        self.assertIsNone(self.music.match(self.player("dlna", sonos.udn(mac(21)))))
        self.assertIsNone(self.music.match("not a player"))

    def test_free_text_is_printable_bounded_and_never_a_credential(self):
        self.assertEqual(music.text("  Opening  "), "Opening")
        self.assertEqual(len(music.text("x" * 500)), music.TEXT_LENGTH)
        for value in ("bad\x07bell", "my password", "", None, 7, "bad\x85next", "lone \ud800", "\u202e"):
            self.assertIsNone(music.text(value))
        # A title's spaces, joiners and marks stay; what would reorder the page after it goes.
        for value in ("Ar\u00a0tist", "\u66f2\u3000\u4e00", "\U0001f469\u200d\U0001f467", "\u200f\u05e9\u05d9\u05e8"):
            self.assertEqual(music.text(value), value)
        self.assertEqual(music.text("\u202eevil\u202c and \u2067isolated\u2069"), "evil and isolated")
        # Zero widths, soft hyphens, stray tags, private-use code points and noncharacters go; a line
        # separator is a space.
        for value, cleaned in (("\u200bA\u2060B\ufeffC\u00adD", "ABCD"), ("Flag\U000e0067\U000e007f", "Flag"),
                               ("x\ue000\ufdd0y\u206a\ufffe\U0001ffff", "xy"),
                               ("one\u2028two\u2029three", "one two three"),
                               ("\U0001f3f4\U000e0067\U000e0062", "\U0001f3f4")):
            self.assertEqual(music.text(value), cleaned, value)
        # What scripts and emoji are written with stays: joiners and marks, a subdivision flag, Arabic number
        # signs and end of ayah, the Mongolian vowel separator, Kaithi number signs, and a code point this
        # interpreter's Unicode has not assigned (a newer emoji, maybe).
        england = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
        for value in ("\u2764\ufe0f\u200c\u061c", "Fans " + england + " and " + england,
                      "\u0600\u0661\u0662 \u06dd\u0663", "\u0645\u08e2", "\u1820\u180e\u1821",
                      "\U000110bd\U000110cd", "Face \U0001fae9 \U0001fae9", "Newer \U0001fbff\U0003ffef"):
            self.assertEqual(music.text(value), value, value)
        # A credential word is found whatever hides it: a removed character, or a kept joiner, mark or
        # invisible combining mark (variation selectors, the combining grapheme joiner, Mongolian free
        # variation selectors), or another form of its letters.
        for value in ("pass\u200bword", "pass\u00adword", "tok\u2060en", "pass\u200dword", "pass\u200cword",
                      "pass\u200eword", "pass\u200fword", "pass\u061cword", "tok\U000e0100en", "tok\ufe0fen",
                      "pass\u034fword", "pass\u180bword", "\uff50\uff41\uff53\uff53word", "the S\u0301ecret"):
            self.assertIsNone(music.text(value), value)
        # The text shown keeps them.
        self.assertEqual(music.text("Caf\u0065\u0301 \u2764\ufe0f"), "Caf\u0065\u0301 \u2764\ufe0f")
        # Only the text shown is cut and cleaned, however long the value.
        self.assertEqual(music.text("\u200b" * 10 + "A" * (8 * 1024 * 1024)), "A" * music.TEXT_LENGTH)

    def test_a_reference_names_a_known_provider_and_an_item_or_folder(self):
        sources = music.Sources([{"domain": "tidal", "instance_id": "tidal--a1", "available": True},
                                 {"domain": "filesystem_local", "instance_id": "filesystem_local--b2",
                                  "name": "thermite flac", "available": True},
                                 {"domain": "filesystem_local", "instance_id": "filesystem_local--c3",
                                  "name": "someone else", "available": True}], ["flac"])
        for good in ("library://album/5", "tidal--a1://track/123", "tidal--a1://playlist/" + oh_music_sim.TIDAL_PLAYLIST_ID,
                     "tidal--a1://folder/", "tidal--a1://folder/playlists", "filesystem_local--b2://folder/",
                     "filesystem_local--b2://folder/Artist", "filesystem_local--b2://track/Artist/Album/01 - Song?.flac",
                     # A file name's spaces of any kind: a no-break space, an ideographic space, a joiner.
                     "filesystem_local--b2://track/Ar\u00a0tist/\u66f2\u3000\u4e00.flac",
                     "filesystem_local--b2://folder/\u200dBand"):
            self.assertEqual(music.parse_ref(good, sources)[0], good.split("://")[0])
        for bad in ("filesystem_local--c3://track/x", "spotify://track/1", "library://folder/x", "library://radio/1",
                    "library://track/", "filesystem_local--b2://folder/../../etc", "tidal--a1://track/1\n",
                    "filesystem_local--b2://track/a\x7fb", "filesystem_local--b2://track/a\x85b",
                    "filesystem_local--b2://track/a\tb", "filesystem_local--b2://track/a\ud800b",
                    "tidal--a1://track/1\udfff",
                    "library://track/" + "1" * 1100, 5, None,
                    # Each provider's own IDs: Music Assistant builds TIDAL's API paths from them.
                    "library://track/5/../6", "library://album/five", "tidal--a1://track/123/../../users",
                    "tidal--a1://track/1?countryCode=x", "tidal--a1://album/1%2F2", "tidal--a1://folder/../x",
                    "filesystem_local--b2://track//etc/passwd", "filesystem_local--b2://track/Artist/./x",
                    "filesystem_local--b2://folder/Artist/", "filesystem_local--b2://track/"):
            with self.assertRaises(Problem):
                music.parse_ref(bad, sources)
        # Without the sources, only the form: any instance of a provider thermite uses.
        self.assertEqual(music.parse_ref("filesystem_local--c3://track/x")[0], "filesystem_local--c3")


class Stand_in:
    """A stand-in Music Assistant for the house, claimed and configured by
    the worker's provisioning, and the gateway's music on it."""

    def start_music(self, data=None, libraries=("flac",)):
        self.data = data or house()
        self.sim = oh_music_sim.Sim(oh_music_sim.speakers_from_house(self.data))
        self.server = oh_music_sim.Server(self.sim).start()
        self.addCleanup(self.server.stop)
        self.authority = compiled(self.data, port=self.server.port, libraries=libraries)
        self.commands = []
        transport = type("Transport", (), {
            "command": lambda _, item, value, source=None: self.commands.append((item, value)),
            "read": lambda _, item: "off"})()
        self.adapter = RuntimeAdapter(RuntimeContract(**self.authority), transport, policy=oh_room_policy)
        install_adapter(self.adapter)
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        os.chmod(self.state, 0o700)
        self.assertEqual(provision.Provisioner(self.state, plain(self.adapter.contract.music)).step(), "ready")
        self.music = music.Music(self.adapter, self.state, settle=0.5)
        music.install(self.adapter, self.music)

    def player_id(self, speaker):
        return self.music.players()[speaker]["player_id"]


class Gateway(Stand_in, Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.start_music()
        self.client = Client(enforce_csrf_checks=True)
        self.request("get", "/api/v1/session")

    def get(self, path, **extra):
        return self.request("get", path, **extra)

    def post(self, path, data, **extra):
        return self.request("post", path, data, **extra)

    def speakers(self):
        return {speaker["id"]: speaker for speaker in self.get("/api/v1/music").json()["speakers"]}

    def hold(self, room, desired):
        config = {"version": 1, "room": room, "timezone": settings.TIME_ZONE, "revision": 1, "enabled": False,
                  "daily_off": [], "locks": [],
                  "manual_lock": {"desired": desired, "started_at": timezone.now().isoformat()}}
        RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": 1})

    def test_the_overview_lists_every_speaker_and_the_sources_only(self):
        self.sim.add_player("RINCON_020000000063014", "sonos", "Stranger")
        response = self.get("/api/v1/music")
        self.assertEqual((response.status_code, response["Cache-Control"]), (200, "no-store"))
        answer = response.json()
        self.assertEqual({key: answer[key] for key in ("version", "available", "problem", "sources")},
                         {"version": 1, "available": True, "problem": None,
                          "sources": {"tidal": False, "libraries": {"flac": True}}})
        self.assertEqual([speaker["id"] for speaker in answer["speakers"]], [WIIM, MINI, AMP])
        self.assertEqual(answer["speakers"][2], {
            "id": AMP, "label": "Sonos Amp", "room": KITCHEN, "room_label": "Example Kitchen", "provider": "sonos",
            "available": True, "state": "idle", "volume": 20, "muted": False, "group": None, "now_playing": None,
            "shuffle": False, "repeat": "off"})
        self.assertNotIn("Stranger", json.dumps(answer))
        self.assertEqual(self.get("/api/v1/music?x=1").status_code, 400)

    def test_a_speaker_plays_each_option_and_its_queue_reads_back(self):
        response = self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM, "option": "replace"})
        self.assertEqual((response.status_code, response.json()),
                         (202, {"accepted": True, "speaker": AMP, "action": "play", "queue": AMP}))
        now = self.speakers()[AMP]
        self.assertEqual((now["state"], now["now_playing"]["title"], now["now_playing"]["artist"],
                          now["now_playing"]["album"], now["now_playing"]["duration"]),
                         ("playing", "Opening", "Example Quartet", "First Light", 241))
        self.assertRegex(now["now_playing"]["art"], "^[0-9a-f]{64}$")
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": TRACK, "option": "next"}).status_code, 202)
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": "library://album/200", "option": "add"}).status_code, 202)
        queue = self.get(f"/api/v1/music/{AMP}/queue").json()
        self.assertEqual({key: queue[key] for key in ("speaker", "queue", "offset", "limit", "total", "current")},
                         {"speaker": AMP, "queue": AMP, "offset": 0, "limit": 100, "total": 6, "current": 0})
        self.assertEqual([item["name"] for item in queue["items"]],
                         ["Opening", "Morning Rain", "Morning Rain", "Long Road", "Example Net", "Test Pattern"])
        self.assertEqual(queue["items"][0], {"ref": "library://track/101", "kind": "track", "name": "Opening",
                                             "source": "library", "artists": ["Example Quartet"],
                                             "album": "First Light", "year": None, "duration": 241,
                                             "art": queue["items"][0]["art"], "index": 0})
        page = self.get(f"/api/v1/music/{AMP}/queue?offset=4&limit=1").json()
        self.assertEqual([item["name"] for item in page["items"]], ["Example Net"])
        # Replacing starts over.
        self.post(f"/api/v1/music/{AMP}/play", {"ref": TRACK})
        self.assertEqual(self.get(f"/api/v1/music/{AMP}/queue").json()["total"], 1)
        for query in ("offset=-1", "limit=0", "limit=201", "offset=x", "other=1", "limit=1&limit=2"):
            self.assertEqual(self.get(f"/api/v1/music/{AMP}/queue?{query}").json(), {"error": "invalid_query"}, query)

    def test_a_group_reports_who_joined_and_who_cannot(self):
        response = self.post(f"/api/v1/music/{AMP}/group", {"members": [MINI, WIIM]})
        # The Amp Pro has no AirPlay: Music Assistant drops it from a Sonos's group without an error.
        self.assertEqual((response.status_code, response.json()),
                         (200, {"accepted": True, "speaker": AMP, "joined": [MINI], "refused": [WIIM], "pending": []}))
        speakers = self.speakers()
        self.assertEqual(speakers[AMP]["group"], {"leader": AMP, "members": [AMP, MINI], "volume": 20})
        self.assertEqual(speakers[MINI]["group"], speakers[AMP]["group"])
        self.assertIsNone(speakers[WIIM]["group"])
        # A member plays its leader's queue; Music Assistant would pull it out otherwise.
        response = self.post(f"/api/v1/music/{MINI}/play", {"ref": ALBUM})
        self.assertEqual(response.json()["queue"], AMP)
        speakers = self.speakers()
        self.assertEqual((speakers[MINI]["state"], speakers[MINI]["now_playing"]["title"]), ("playing", "Opening"))
        self.assertEqual(speakers[MINI]["group"]["members"], [AMP, MINI])
        self.assertEqual(self.get(f"/api/v1/music/{MINI}/queue").json()["queue"], AMP)
        # A group's volume moves every member; it reports its loudest.
        self.assertEqual(self.post(f"/api/v1/music/{MINI}/volume", {"level": 10}).status_code, 202)
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/volume", {"group_level": 40}).status_code, 202)
        speakers = self.speakers()
        self.assertEqual((speakers[AMP]["volume"], speakers[MINI]["volume"], speakers[AMP]["group"]["volume"]), (40, 20, 40))
        self.assertEqual(self.post(f"/api/v1/music/{MINI}/ungroup", {}).status_code, 202)
        speakers = self.speakers()
        self.assertEqual((speakers[AMP]["group"], speakers[MINI]["group"], speakers[MINI]["state"]), (None, None, "idle"))

    def test_a_group_of_wiims_and_a_member_that_is_not_there(self):
        self.sim.players[self.player_id(MINI)]["available"] = False
        response = self.post(f"/api/v1/music/{WIIM}/group", {"members": [MINI]})
        self.assertEqual(response.json(), {"accepted": True, "speaker": WIIM, "joined": [], "refused": [MINI],
                                           "pending": []})
        self.sim.players[self.player_id(MINI)]["available"] = True
        response = self.post(f"/api/v1/music/{WIIM}/group", {"members": [MINI]})
        self.assertEqual(response.json(), {"accepted": True, "speaker": WIIM, "joined": [MINI], "refused": [],
                                           "pending": []})
        for members in ([], [WIIM], [MINI, MINI], "x", [7]):
            self.assertEqual(self.post(f"/api/v1/music/{WIIM}/group", {"members": members}).json(),
                             {"error": "invalid_command"}, members)
        self.assertEqual(self.post(f"/api/v1/music/{WIIM}/group", {"members": ["example_nowhere"]}).status_code, 404)

    def test_transport_reaches_the_queue_and_seek_shows_on_the_speaker(self):
        self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM, "option": "replace"})
        for action, value in (("pause", None), ("play", None), ("next", None), ("seek", 90), ("shuffle", True),
                              ("repeat", "all"), ("previous", None), ("stop", None)):
            body = {"action": action} if value is None else {"action": action, "value": value}
            response = self.post(f"/api/v1/music/{AMP}/transport", body)
            self.assertEqual((response.status_code, response.json()["queue"]), (202, AMP), action)
            if action == "pause":
                self.assertEqual(self.speakers()[AMP]["state"], "paused")
            if action == "next":
                self.assertEqual(self.speakers()[AMP]["now_playing"]["title"], "Morning Rain")
            if action == "seek":
                self.assertEqual(self.speakers()[AMP]["now_playing"]["elapsed"], 90)
        speaker = self.speakers()[AMP]
        self.assertEqual((speaker["state"], speaker["shuffle"], speaker["repeat"]), ("idle", True, "all"))
        for body in ({"action": "seek", "value": -1}, {"action": "seek", "value": "1"}, {"action": "seek"},
                     {"action": "shuffle", "value": 1}, {"action": "repeat", "value": "twice"},
                     {"action": "pause", "value": 1}, {"action": "skip"}, {"action": "play", "other": 1}, {}):
            self.assertEqual(self.post(f"/api/v1/music/{AMP}/transport", body).json(), {"error": "invalid_command"}, body)

    def test_what_a_command_names_is_checked_before_music_assistant_sees_it(self):
        calls = len(self.sim.calls)
        for path, body, code in (
                ("example_nowhere/play", {"ref": ALBUM}, "unknown_speaker"),
                (f"{AMP}/play", {"ref": ALBUM, "option": "now"}, "invalid_command"),
                (f"{AMP}/play", {"ref": ALBUM, "extra": 1}, "invalid_command"),
                (f"{AMP}/play", {"ref": "spotify://track/1"}, "invalid_ref"),
                (f"{AMP}/play", {"ref": "library://track/1\u0007"}, "invalid_ref"),
                (f"{AMP}/play", {"ref": "library://track/1\ud800"}, "invalid_ref"),
                (f"{AMP}/play", {"ref": "library://folder/x"}, "invalid_ref"),
                (f"{AMP}/play", {"ref": "tidal--abcd1234://track/1"}, "invalid_ref"),
                (f"{AMP}/volume", {"level": 101}, "invalid_command"),
                (f"{AMP}/volume", {"level": 5, "group_level": 5}, "invalid_command"),
                (f"{AMP}/volume", {"level": True}, "invalid_command"),
                (f"{AMP}/ungroup", {"now": True}, "invalid_command"),
                (f"{AMP}/transport", {"action": ["pause"]}, "invalid_command"),
                (f"{AMP}/transport", {"action": {"pause": 1}}, "invalid_command")):
            self.assertEqual(self.post("/api/v1/music/" + path, body).json(), {"error": code}, (path, body))
        # Only the providers were read, for a TIDAL instance Music Assistant does not have; never a command sent.
        self.assertEqual({command for command, _ in self.sim.calls[calls:]}, {"providers"})
        self.assertEqual(self.request("get", f"/api/v1/music/{AMP}/play").status_code, 405)

    def test_a_value_music_assistant_cannot_be_sent_is_refused_not_a_server_error(self):
        with self.assertRaises(Problem) as refused:
            self.music.call("players/all", name="\ud800")
        self.assertEqual((refused.exception.code, refused.exception.status), ("invalid_command", 400))

    def test_a_failure_is_music_failed_and_never_music_assistants_text(self):
        response = self.post(f"/api/v1/music/{AMP}/play", {"ref": "library://track/999"})
        self.assertEqual((response.status_code, response.json()), (502, {"error": "music_failed"}))
        self.sim.players[self.player_id(AMP)]["available"] = False
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": TRACK}).json(), {"error": "music_failed"})
        self.assertEqual(self.speakers()[AMP]["state"], "unavailable")

    def test_a_locked_room_refuses_what_it_owns_and_a_group_checks_every_speaker(self):
        self.hold(EAST, "off")
        self.assertEqual(self.post(f"/api/v1/music/{WIIM}/play", {"ref": ALBUM}).json(), {"error": "device_locked"})
        self.assertEqual(self.post(f"/api/v1/music/{WIIM}/volume", {"level": 5}).status_code, 423)
        self.assertEqual(self.post(f"/api/v1/music/{MINI}/group", {"members": [WIIM]}).status_code, 423)
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM}).status_code, 202)
        self.assertNotIn("players/cmd/set_members", [command for command, _ in self.sim.calls])

    def test_grouping_checks_the_rooms_of_every_speaker_whose_group_it_changes(self):
        self.assertEqual(self.post(f"/api/v1/music/{MINI}/group", {"members": [WIIM]}).json()["joined"], [WIIM])
        self.hold(EAST, "off")
        calls = len(self.sim.calls)
        # The Mini leads the East room's WiiM: moving it into the Amp's group would take the WiiM's leader away.
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/group", {"members": [MINI]}).json(), {"error": "device_locked"})
        self.assertNotIn("players/cmd/set_members", [command for command, _ in self.sim.calls[calls:]])

    def test_a_group_that_changes_while_a_command_waits_is_read_again_under_the_locks(self):
        self.hold(EAST, "off")
        lock, raced = self.adapter.command_lock, []

        def racing(rooms, resources):
            if not raced:
                # Another command groups the East room's WiiM under the Mini meanwhile.
                raced.append(rooms)
                self.music.call("players/cmd/set_members", target_player=self.player_id(MINI),
                                player_ids_to_add=[self.player_id(WIIM)])
            return lock(rooms, resources)
        with patch.object(self.adapter, "command_lock", racing), self.assertLogs("oh_identity.trail", "INFO") as logs:
            response = self.post(f"/api/v1/music/{MINI}/play", {"ref": ALBUM})
        # The first locks were the Mini's alone: no routed room has it.
        self.assertEqual((raced, response.json()), ([()], {"error": "device_locked"}))
        self.assertNotIn("player_queues/play_media", [command for command, _ in self.sim.calls])
        self.assertEqual([line["outcome"] for line in lines(logs, "cmd")], ["device_locked"])

    def test_reading_a_group_back_ends_by_its_deadline_whatever_music_assistant_does(self):
        from oh_music.client import TIMEOUT
        now, timeouts = [0.0], []

        class Hung:
            def __init__(self, port, token):
                self.token = token

            def call(self, command, timeout=None, /, **args):
                # Music Assistant answers at the last moment, never with the members.
                timeouts.append(timeout)
                now[0] += timeout
                return []
        slow = music.Music(self.adapter, self.state, connect=Hung, settle=3.0, clock=lambda: now[0],
                           sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertEqual(slow.settled(AMP, [MINI]), set())
        self.assertLessEqual(now[0], 3.5)
        self.assertTrue(all(timeout <= TIMEOUT for timeout in timeouts))

    def test_play_and_group_wait_longer_for_music_assistant_than_other_commands(self):
        call, sent = music.Music.call, []

        def recording(music_, command, timeout=None, /, **args):
            sent.append((command, timeout))
            return call(music_, command, timeout, **args)
        with patch.object(music.Music, "call", recording):
            self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM})
            self.post(f"/api/v1/music/{AMP}/volume", {"level": 33})
            self.post(f"/api/v1/music/{AMP}/group", {"members": [MINI]})
            self.post(f"/api/v1/music/{AMP}/transport", {"action": "pause"})
            reads = len(sent)
            self.get(f"/api/v1/music/{AMP}/queue")
        self.assertEqual([(command, timeout) for command, timeout in sent if "/cmd/" in command or "play_media" in command],
                         [("player_queues/play_media", 15.0), ("players/cmd/volume_set", None),
                          ("players/cmd/set_members", 15.0), ("players/cmd/pause", None)])
        # A queue's reads hold no lock, and Music Assistant may fetch its TIDAL items' details first.
        self.assertEqual([(command, timeout) for command, timeout in sent[reads:] if command.startswith("player_queues/")],
                         [("player_queues/get", SLOW), ("player_queues/items", SLOW)])

    def test_a_group_read_back_that_fails_answers_what_it_last_read_or_pending(self):
        call, sent, reads = music.Music.call, [], [1]

        def failing(music_, command, timeout=None, /, **args):
            sent.append(command)
            after = sent[sent.index("players/cmd/set_members") + 1:] if "players/cmd/set_members" in sent else []
            if after.count("players/all") > reads[0]:
                raise Problem("music_unavailable", 503)
            return call(music_, command, timeout, **args)
        wiim = self.player_id(WIIM)
        # The Amp Pro has no AirPlay: Music Assistant drops it from a Sonos's group, so the read-back waits for it.
        with patch.object(music.Music, "call", failing):
            response = self.post(f"/api/v1/music/{AMP}/group", {"members": [MINI, WIIM]})
            self.assertEqual((response.status_code, response.json()),
                             (200, {"accepted": True, "speaker": AMP, "joined": [MINI], "refused": [WIIM],
                                    "pending": []}))
            # No read-back answers at all: neither joined nor refused, but pending; one never sent is refused.
            sent.clear()
            self.post(f"/api/v1/music/{AMP}/ungroup", {})
            self.sim.players[wiim]["available"] = False
            reads[0] = 0
            response = self.post(f"/api/v1/music/{AMP}/group", {"members": [MINI, WIIM]})
        self.assertEqual(response.json(), {"accepted": True, "speaker": AMP, "joined": [], "refused": [WIIM],
                                           "pending": [MINI]})

    def test_a_speaker_called_art_keeps_its_routes(self):
        from django.urls import resolve
        from oh_identity import music_api
        self.assertIs(resolve("/api/v1/music/art/play").func, music_api.play)
        self.assertIs(resolve("/api/v1/music/art/" + "a" * 64).func, music_api.art)

    def test_a_room_that_shares_a_speaker_leaves_it_what_does_not_disturb_its_route(self):
        data = house()
        data["rooms"][ESTATE] = {"label": "Example Estate", "tag": "Building"}
        data["rooms"][EAST]["in"] = ESTATE
        data["routing"]["rooms"][ESTATE] = {"hdmi_output": 3, "sources": ["example_computer"]}
        self.start_music(data)
        self.assertEqual(self.music.spec["speakers"][WIIM]["rooms"], {EAST: "own", ESTATE: "shared"})
        self.hold(ESTATE, "off")
        self.assertEqual(self.post(f"/api/v1/music/{WIIM}/volume", {"level": 5}).status_code, 202)

    def test_each_command_is_one_trail_line_without_what_was_played(self):
        self.signin("owner")
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM, "option": "add"})
            self.post(f"/api/v1/music/{AMP}/volume", {"level": 33})
            self.post(f"/api/v1/music/{AMP}/group", {"members": [MINI]})
            # A valid command Music Assistant cannot take is a line too; an invalid one is none.
            self.sim.players[self.player_id(WIIM)]["available"] = False
            self.post(f"/api/v1/music/{WIIM}/transport", {"action": "pause"})
            self.post(f"/api/v1/music/{WIIM}/transport", {"action": "pause", "value": 1})
            # Nor one whose reference names a source Music Assistant does not have, found once it answers.
            self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": "tidal--abcd1234://track/1"}).status_code, 400)
        found = [(line["device"], line["action"], line["item"], line["value"], line["outcome"], line["user"])
                 for line in lines(logs, "cmd")]
        self.assertEqual(found, [(AMP, "music.play", "-", "add", "accepted", "owner"),
                                 (AMP, "music.volume", "-", "33", "accepted", "owner"),
                                 (AMP, "music.group", "-", MINI, "accepted", "owner"),
                                 (WIIM, "music.pause", "-", "-", "music_failed", "owner")])
        self.assertFalse(any("library" in record.getMessage() for record in logs.records))

    def test_search_and_browse_show_items_thermite_can_play(self):
        response = self.get("/api/v1/music/search?q=light")
        answer = response.json()
        self.assertEqual([album["name"] for album in answer["albums"]], ["First Light"])
        self.assertEqual(answer["albums"][0]["ref"], ALBUM)
        self.assertEqual((answer["query"], answer["tracks"], answer["playlists"]), ("light", [], []))
        self.assertEqual([track["name"] for track in self.get("/api/v1/music/search?q=rain&kind=track").json()["tracks"]],
                         ["Morning Rain"])
        self.assertEqual(self.get("/api/v1/music/search?q=rain&source=tidal").json()["tracks"], [])
        # A query is bounded and printable before Music Assistant sees it.
        self.get("/api/v1/music/search?q=" + "r\x01ain" + "x" * 200)
        query = self.sim.calls[-1][1]["search_query"]
        self.assertEqual((query[:4], len(query)), ("rain", 100))
        for bad in ("q=", "q=%20", "q=a&kind=radio", "q=a&source=spotify", "q=a&q=b", "q=a&other=1"):
            self.assertEqual(self.get("/api/v1/music/search?" + bad).json(), {"error": "invalid_query"}, bad)
        root = self.get("/api/v1/music/browse").json()
        [library] = root["items"]
        self.assertEqual((library["kind"], library["name"], library["library"]), ("folder", "flac", "flac"))
        artists = self.get("/api/v1/music/browse?ref=" + library["ref"]).json()["items"]
        self.assertEqual([artist["name"] for artist in artists], ["Example Quartet", "Documentation Band"])
        albums = self.get("/api/v1/music/browse?ref=" + artists[0]["ref"]).json()["items"]
        tracks = self.get("/api/v1/music/browse?ref=" + albums[0]["ref"]).json()
        self.assertEqual(([track["name"] for track in tracks["items"]], tracks["total"]),
                         (["Opening", "Morning Rain", "Long Road"], 3))
        self.assertEqual(tracks["items"][0]["library"], "flac")
        page = self.get("/api/v1/music/browse?ref=" + albums[0]["ref"] + "&offset=1&limit=1").json()
        self.assertEqual(([track["name"] for track in page["items"]], page["total"]), (["Morning Rain"], 3))
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": tracks["items"][2]["ref"]}).status_code, 202)
        self.assertEqual(self.speakers()[AMP]["now_playing"]["title"], "Long Road")
        album = self.get("/api/v1/music/browse?ref=" + ALBUM).json()["items"]
        self.assertEqual([track["name"] for track in album], ["Opening", "Morning Rain", "Long Road"])
        self.assertEqual(self.get("/api/v1/music/browse?ref=" + TRACK).json(), {"error": "invalid_query"})
        self.assertEqual(self.get("/api/v1/music/browse?ref=spotify://folder/x").json(), {"error": "invalid_ref"})

    def test_art_is_a_bounded_image_cached_privately(self):
        self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM})
        art = self.speakers()[AMP]["now_playing"]["art"]
        calls = len(self.sim.calls)
        response = self.get(f"/api/v1/music/art/{art}?size=80")
        self.assertEqual((response.status_code, response["Content-Type"], response["Cache-Control"]),
                         (200, "image/jpeg", "private, max-age=86400"))
        self.assertTrue(response.content.startswith(b"\xff\xd8\xff"))
        self.assertEqual(len(self.sim.calls), calls)  # The image proxy, not a command.
        self.assertTrue(self.get(f"/api/v1/music/art/{art}?size=512").content.startswith(b"\xff\xd8\xff"))
        for path in (f"{art}?size=1024", f"{art}?size=x", "0" * 63, "g" * 64, "0" * 64):
            self.assertEqual(self.get("/api/v1/music/art/" + path).status_code, 404, path)
        self.assertEqual(self.get(f"/api/v1/music/art/{art}?other=1").json(), {"error": "invalid_query"})

    def test_art_takes_at_most_its_share_of_the_gateways_requests(self):
        from oh_access.service import MAX_REQUESTS
        fetching, answer, answered = threading.Semaphore(0), threading.Event(), []

        class Stalled:
            def __init__(self, port, token):
                self.token = token

            def image(self, proxy_id, size):
                fetching.release()
                answer.wait(10)
                return "image/png", b"x"
        stalled = music.Music(self.adapter, self.state, connect=Stalled)
        asked, seen = collections.Counter(), threading.Condition()

        class Counted:
            """Slots that tell the test of each request that asks for one."""

            def __init__(self, name):
                self.name, self.slots = name, getattr(stalled, name)

            def acquire(self, timeout):
                with seen:
                    asked[self.name] += 1
                    seen.notify_all()
                return self.slots.acquire(timeout=timeout)

            def release(self):
                self.slots.release()

        def reached(name, count):
            with seen:
                self.assertTrue(seen.wait_for(lambda: asked[name] >= count, 5), (name, asked))
        for name in ("_art", "_art_queue", "_art_requests"):
            setattr(stalled, name, Counted(name))

        def fetch():
            answered.append(stalled.image("0" * 64, 80))

        def refused(**waits):
            started = time.monotonic()
            with patch.multiple(music, **waits) if waits else nullcontext(), self.assertRaises(Problem) as problem:
                stalled.image("0" * 64, 80)
            self.assertEqual((problem.exception.code, problem.exception.status, problem.exception.retry_after),
                             ("music_unavailable", 503, 1))
            return time.monotonic() - started
        running = [threading.Thread(target=fetch) for _ in range(music.ART_FETCHES)]
        for thread in running:
            thread.start()
        for _ in running:
            self.assertTrue(fetching.acquire(timeout=5))
        # A request waits ART_WAIT for a fetch to end, then gives up.
        self.assertGreaterEqual(refused(ART_WAIT=0.1), 0.1)
        waiting = [threading.Thread(target=fetch) for _ in range(music.ART_WAITING)]
        for thread in waiting:
            thread.start()
        reached("_art", music.ART_FETCHES + 1 + music.ART_WAITING)
        # With the queue full, one waits ART_ARRIVING_WAIT for a place in it, then gives up.
        self.assertGreaterEqual(refused(ART_ARRIVING_WAIT=0.1), 0.1)
        with patch.object(music, "ART_ARRIVING_WAIT", 10):
            arriving = [threading.Thread(target=fetch) for _ in range(music.ART_ARRIVING)]
            for thread in arriving:
                thread.start()
            reached("_art_queue", music.ART_FETCHES + 1 + music.ART_WAITING + 1 + music.ART_ARRIVING)
            # Past all of those, one is refused at once.
            self.assertLess(refused(), 1)
            answer.set()
            for thread in running + waiting + arriving:
                thread.join(5)
        self.assertEqual(answered, [("image/png", b"x")] * (music.ART_FETCHES + music.ART_WAITING + music.ART_ARRIVING))
        self.assertLessEqual(4 * (music.ART_FETCHES + music.ART_WAITING), MAX_REQUESTS)
        self.assertLessEqual(5 * (music.ART_FETCHES + music.ART_WAITING + music.ART_ARRIVING), 2 * MAX_REQUESTS)

    def test_a_failed_art_fetch_gives_its_place_back(self):
        class Failing:
            def __init__(self, port, token):
                self.token = token

            def image(self, proxy_id, size):
                raise Failed(404)
        failing = music.Music(self.adapter, self.state, connect=Failing)
        for _ in range(music.ART_FETCHES + music.ART_WAITING + 1):
            with self.assertRaises(Problem) as missing:
                failing.image("0" * 64, 80)
            self.assertEqual(missing.exception.status, 404)

    def test_a_guest_who_may_not_see_text_sees_speakers_but_no_titles(self):
        self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM})
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        set_guest_text(self.owner, False)
        now = self.speakers()[AMP]["now_playing"]
        self.assertEqual(set(now), {"duration", "elapsed", "at"})
        art = self.music.read()["speakers"][2]["now_playing"]["art"]
        for path in ("/api/v1/music/search?q=light", "/api/v1/music/browse", f"/api/v1/music/{AMP}/queue",
                     f"/api/v1/music/art/{art}"):
            self.assertEqual((self.get(path).status_code, self.get(path).json()), (401, {"error": "login_required"}), path)
        # Commands are a room's: a Guest still sends them.
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/transport", {"action": "pause"}).status_code, 202)
        self.signin("owner")
        self.assertEqual(self.speakers()[AMP]["now_playing"]["title"], "Opening")

    def test_without_music_assistant_music_is_unavailable_and_says_why(self):
        self.server.stop()
        answer = self.get("/api/v1/music").json()
        self.assertEqual((answer["available"], answer["problem"], answer["sources"]), (False, "music_unavailable", None))
        self.assertEqual({speaker["state"] for speaker in answer["speakers"]}, {"unavailable"})
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM}).status_code, 503)
        provision.write_claim(self.state, {"problem": provision.UNCLAIMED})
        self.assertEqual(self.get("/api/v1/music").json()["problem"], "music_unclaimed")
        self.assertEqual(self.post(f"/api/v1/music/{AMP}/play", {"ref": ALBUM}).json(), {"error": "music_unclaimed"})
        provision.token_file(self.state).unlink()
        self.assertEqual(self.get("/api/v1/music").json()["problem"], "music_unavailable")
        # A server that has refused thermite's password for a day's passes is no longer thermite's.
        provision.write_claim(self.state, {"password": "x", "token": "x", "issued_at": 1, "expires_at": 2 ** 40,
                                           "refusals": provision.REFUSALS})
        self.assertEqual(self.get("/api/v1/music").json()["problem"], "music_unclaimed")

    def test_a_read_music_assistant_fails_is_still_the_speakers_and_why(self):
        self.sim.failing["player_queues/all"] = 1
        response = self.get("/api/v1/music")
        self.assertEqual((response.status_code, response.json()["available"], response.json()["problem"]),
                         (200, False, "music_failed"))
        self.assertEqual(len(response.json()["speakers"]), 3)
        self.assertTrue(self.get("/api/v1/music").json()["available"])

    def test_a_renewed_token_gets_a_new_client_and_the_old_one_closes_once_unused(self):
        made = []

        class Recorded:
            def __init__(self, port, token):
                self.token, self.closed = token, False
                made.append(self)

            def call(self, command, timeout=None, /, **args):
                return []

            def close(self):
                self.closed = True
        tracked = music.Music(self.adapter, self.state, connect=Recorded)
        claim = provision.read_claim(self.state)
        with tracked.connected() as first:
            provision.write_claim(self.state, {**claim, "token": "xxx"})
            with tracked.connected() as second:
                # The first is still in use by the outer block.
                self.assertEqual((first.closed, second.token), (False, "xxx"))
            self.assertFalse(first.closed)
        self.assertTrue(first.closed)
        tracked.call("players/all")
        self.assertEqual((len(made), second.closed), (2, False))

    def test_a_host_without_music_has_no_music(self):
        install_adapter(RuntimeAdapter(RuntimeContract(**compiled(house(), port=None)), self.adapter.transport))
        self.assertEqual(self.get("/api/v1/music").json(), {"error": "music_not_configured"})
        self.assertEqual(self.get("/api/v1/events/music").status_code, 404)

    def test_tidal_sign_in_is_an_admins_and_shows_only_the_code(self):
        self.assertEqual(self.post("/api/v1/music/tidal/start", {}).status_code, 401)
        self.signin("owner")
        self.assertEqual(self.get("/api/v1/music/tidal").json(), {"signed_in": False, "sign_in": None})
        started = self.post("/api/v1/music/tidal/start", {}).json()
        code = self.sim.tidal_code
        self.assertEqual(started["sign_in"]["state"], "waiting")
        self.assertEqual((started["sign_in"]["code"], started["sign_in"]["url"]), (code, "https://link.tidal.com/" + code))
        self.sim.expire_tidal()
        self.assertEqual(self.get("/api/v1/music/tidal").json()["sign_in"]["code"], self.sim.tidal_code)
        self.sim.approve_tidal()
        self.assertEqual(self.get("/api/v1/music/tidal").json(), {"signed_in": True, "sign_in": {
            "state": "done", "code": None, "url": None, "expires_at": None}})
        self.assertEqual(self.post("/api/v1/music/tidal/finish", {}).json()["signed_in"], True)
        self.assertEqual(self.post("/api/v1/music/tidal/finish", {}).json(), {"error": "tidal_not_started"})
        self.assertTrue(self.get("/api/v1/music").json()["sources"]["tidal"])
        found = self.get("/api/v1/music/search?q=overture&source=tidal").json()["tracks"]
        self.assertEqual([(track["name"], track["source"]) for track in found], [("Overture", "tidal")])
        root = self.get("/api/v1/music/browse").json()["items"]
        self.assertEqual([item["name"] for item in root], ["flac", "TIDAL"])
        playlists = self.get("/api/v1/music/browse?ref=" + root[1]["ref"]).json()["items"]
        self.assertEqual([item["name"] for item in playlists], ["Albums", "Playlists"])
        # A second sign-in reconfigures the instance; finishing it early cancels it.
        self.post("/api/v1/music/tidal/start", {})
        self.assertEqual(self.post("/api/v1/music/tidal/finish", {}).json()["sign_in"]["state"], "cancelled")
        self.assertEqual(self.sim.flows, {})

    def test_the_event_stream_joins_the_view_a_principal_may_see(self):
        views = []

        def fake(adapter, view, snapshot, release=None, meta=None, hub=None):
            views.append((view, callable(hub)))
            return (b"" for _ in ())
        with patch("oh_identity.streams.stream", fake):
            response = self.get("/api/v1/events/music")
            self.assertEqual((response.status_code, response["Content-Type"], response["Cache-Control"]),
                             (200, "text/event-stream", "no-store"))
            SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
            set_guest_text(self.owner, False)
            self.get("/api/v1/events/music")
            self.signin("owner")
            self.get("/api/v1/events/music")
        self.assertEqual(views, [("music", True), ("music" + topics.WITHOUT_TEXT, True), ("music", True)])
        self.assertEqual(self.get("/api/v1/events/music?x=1").status_code, 404)
        self.assertEqual(self.request("post", "/api/v1/events/music")["Allow"], "GET")

    def test_the_worker_stops_and_joins_its_music_thread_before_the_block_holding_its_lease_ends(self):
        import functools

        import httpx

        from oh_access import worker
        from oh_access.staging import units
        from oh_music.client import Client as MusicClient
        locks = oh_room_policy.FileLocks(Path(self.state) / "locks")
        lease = functools.partial(locks.hold, ["room-policy-worker"], blocking=False)
        stop = threading.Event()
        with lease():
            with worker.music_running(self.state, self.adapter.contract.music, stop) as thread:
                self.assertTrue(thread.is_alive())
                with self.assertRaises(BlockingIOError), lease():
                    pass
            self.assertTrue(stop.is_set())
            self.assertFalse(thread.is_alive())
        stop = threading.Event()
        with self.assertRaises(KeyError), worker.music_running(self.state, self.adapter.contract.music, stop) as thread:
            raise KeyError()
        self.assertFalse(thread.is_alive())
        with worker.music_running(self.state, None, threading.Event()) as thread:
            self.assertIsNone(thread)
        # A request in flight outlasts the join: the warning is logged, the
        # lease given up, and the thread makes no request after it.
        asked, answer, requests = threading.Event(), threading.Event(), []

        def handler(request):
            requests.append(request.url.path)
            asked.set()
            answer.wait(10)
            return httpx.Response(200, json={"server_id": "x", "status": "running"})

        def connect(port, **options):
            return MusicClient(port, transport=httpx.MockTransport(handler), **options)
        stop = threading.Event()
        with patch.object(provision, "run", functools.partial(provision.run, connect=connect)), \
                self.assertLogs("oh_access.worker", "WARNING") as logs:
            with lease():
                # What is left of the worker's stop budget bounds the join, below MUSIC_STOP.
                with worker.music_running(self.state, self.adapter.contract.music, stop, left=lambda: 0.1) as thread:
                    self.assertTrue(asked.wait(10))
                    started = time.monotonic()
                self.assertLess(time.monotonic() - started, 5)
                self.assertTrue(thread.is_alive())
        with lease():
            pass
        self.assertEqual(logs.output, ["WARNING:oh_access.worker:The music thread did not stop; it ends with the process."])
        answer.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(requests, ["/info"])
        # Both joins fit inside the unit's stop timeout, so systemd never kills the worker.
        unit = units("/srv/thermite/access/releases/example",
                     state="/srv/thermite/access/state")["thermite-access-worker.service"]
        timeout = int(next(line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")).split("=")[1])
        self.assertLess(SLOW, worker.MUSIC_STOP)
        self.assertLess(worker.MUSIC_STOP, worker.STOP_SECONDS)
        self.assertLessEqual(worker.STOP_SECONDS + 3, timeout)

    def test_a_stop_during_a_step_stops_music_at_once_and_the_observer_once_the_step_ends(self):
        from oh_access import worker
        happened, shutdowns, music_stops, stop = [], [], [], threading.Event()

        class Recorded(worker.Shutdown):
            def __init__(self, stop):
                super().__init__(stop, poll=0.01)
                shutdowns.append(self)
        test = self

        class Stepping:
            """A step during which a signal comes: its handler records the request alone."""

            def step(self):
                happened.append("step")
                shutdowns[0].request()
                test.assertTrue(music_stops[0].wait(5))
                happened.append("music stopped")
                time.sleep(0.1)  # Nothing else is stopped meanwhile.
                happened.append(("step ended", stop.is_set()))
                return {}

            def wake(self):
                pass

            def wait(self, maximum):
                pass

        async def observe(transport, cache, stop):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                happened.append("observer cancelled")
                raise
            finally:
                cache.clear()

        @contextmanager
        def music_running(state, music, stop, *, start, left):
            music_stops.append(stop)
            yield None

        class Locks:
            @contextmanager
            def hold(self, keys, blocking):
                yield
        adapter = SimpleNamespace(transport=SimpleNamespace(), contract=SimpleNamespace(music=None))
        with patch.object(worker, "Shutdown", Recorded), patch.object(worker, "observe", observe), \
                patch.object(worker, "music_running", music_running), patch.object(worker, "maintain", lambda store: None):
            worker.supervise(Stepping(), adapter, {"state": None}, locks=Locks(), store=None,
                             telemetry=SimpleNamespace(stepped=lambda result: None),
                             cache=SimpleNamespace(clear=lambda: happened.append("cleared")), stop=stop, interval=0.01)
        # The music thread's own stop is set at the request; the shared one, which ends the observer and
        # clears what it saw, once the step has ended.
        self.assertIsNot(music_stops[0], stop)
        self.assertEqual(happened, ["step", "music stopped", ("step ended", False), "observer cancelled", "cleared",
                                    "cleared"])

    def test_the_worker_joins_its_threads_within_one_deadline_from_its_stop_request(self):
        from oh_access import worker
        now, music_stop, woken = [100.0], threading.Event(), threading.Event()
        stop = threading.Event()
        shutdown = worker.Shutdown(stop, clock=lambda: now[0], poll=0.01)
        self.assertEqual(shutdown.left(), worker.STOP_SECONDS)
        watcher = shutdown.watch(music_stop.set, woken.set)
        # A signal while a step runs: a flag and a time. The music thread stops and the loop
        # wakes at once; the observer's stop, which clears what it saw, waits for the step.
        shutdown.request()
        self.assertTrue(music_stop.wait(5))
        self.assertTrue(woken.wait(5))
        self.assertFalse(stop.is_set())
        # The step ends 20 seconds on: what is left counts from the request, not from then.
        now[0] += 20
        shutdown.request()
        self.assertEqual(shutdown.left(), worker.STOP_SECONDS - 20)
        now[0] += 60
        self.assertEqual(shutdown.left(), 0.0)
        watcher.join(5)
        # The stop event set by another starts the budget when the watcher sees it.
        stop, music_stop = threading.Event(), threading.Event()
        shutdown = worker.Shutdown(stop, clock=lambda: now[0], poll=0.01)
        watcher = shutdown.watch(music_stop.set)
        stop.set()
        watcher.join(5)
        self.assertEqual((shutdown.requested, music_stop.is_set(), shutdown.left()), (True, True, worker.STOP_SECONDS))

    def test_the_guest_listener_serves_music_but_not_tidal(self):
        from oh_identity.guest import ROUTES
        self.assertEqual(ROUTES["api/v1/music/<slug:speaker>/play"], ("POST",))
        self.assertEqual(ROUTES["api/v1/events/music"], ("GET",))
        self.assertFalse([route for route in ROUTES if "tidal" in route])


class Events(Stand_in, TransactionTestCase):
    """The music event stream's hub on a running loop."""

    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        self.revision = get_state().revision
        self.guest = Guest(self.revision, True)
        self.start_music()

    def check(self, body):
        async def run():
            hub = music_hub.MusicHub(self.music, backoff=(0.05,), linger=0.1)
            try:
                await body(hub)
            finally:
                if hub.task:
                    hub.task.cancel()
                    await asyncio.gather(hub.task, return_exceptions=True)
        async_to_sync(run)()

    async def next(self, subscriber, kind=None):
        while True:
            data = await asyncio.wait_for(subscriber.next(5), 5)
            if data is None:
                continue
            frame = json.loads(data.decode()[len("data: "):])
            if kind is None or frame["type"] == kind:
                return frame

    def test_a_snapshot_then_each_speaker_that_changes(self):
        async def body(hub):
            seen = hub.join(music_hub.VIEW)
            hidden = hub.join(music_hub.VIEW + topics.WITHOUT_TEXT)
            snapshot = await self.next(seen)
            self.assertEqual((snapshot["type"], snapshot["view"]), ("snapshot", "music"))
            self.assertEqual([speaker["id"] for speaker in snapshot["music"]["speakers"]], [WIIM, MINI, AMP])
            await self.next(hidden)
            await asyncio.to_thread(self.music.play, self.guest, AMP, ALBUM, "replace")
            frame = await self.next(seen, "speaker")
            while frame["speaker"]["now_playing"] is None:
                frame = await self.next(seen, "speaker")
            self.assertEqual((frame["speaker"]["id"], frame["speaker"]["now_playing"]["title"]), (AMP, "Opening"))
            frame = await self.next(hidden, "speaker")
            while frame["speaker"]["now_playing"] is None:
                frame = await self.next(hidden, "speaker")
            self.assertNotIn("title", frame["speaker"]["now_playing"])
            # A seek is reported on the queue: the speaker shows it.
            await asyncio.to_thread(self.music.transport, self.guest, AMP, "seek", 120)
            frame = await self.next(seen, "speaker")
            while frame["speaker"]["now_playing"]["elapsed"] != 120:
                frame = await self.next(seen, "speaker")
            # A player no speaker is never streamed.
            self.sim.add_player("RINCON_020000000063014", "sonos", "Stranger")
            await asyncio.to_thread(self.music.volume, self.guest, AMP, {"level": 44})
            frame = await self.next(seen, "speaker")
            while frame["speaker"]["volume"] != 44:
                frame = await self.next(seen, "speaker")
            self.assertEqual(hub.status()["streams"], {"music": 2})
            hub.leave(seen)
            hub.leave(hidden)
        self.check(body)

    def test_a_client_stream_of_the_music_view_reads_the_music_hub(self):
        async def body(hub):
            frames = streams.stream(self.adapter, music_hub.VIEW, {"guest": True, "security_revision": self.revision},
                                    hub=lambda: hub)
            first = json.loads((await asyncio.wait_for(anext(frames), 5)).decode()[len("data: "):])
            self.assertEqual((first["type"], first["view"], len(first["music"]["speakers"])), ("snapshot", "music", 3))
            await frames.aclose()
            self.assertEqual(hub.subscribers[music_hub.VIEW], set())
        self.check(body)

    def test_the_stream_says_unavailable_while_music_assistant_is_down_and_resyncs(self):
        async def body(hub):
            subscriber = hub.join(music_hub.VIEW)
            await self.next(subscriber, "snapshot")
            port = self.server.port
            await asyncio.to_thread(self.server.stop)
            self.assertEqual(await self.next(subscriber), {"type": "unavailable"})
            self.server = oh_music_sim.Server(self.sim, port).start()
            self.addCleanup(self.server.stop)
            snapshot = await self.next(subscriber, "snapshot")
            self.assertTrue(snapshot["music"]["available"])
            self.assertGreaterEqual(hub.counters["upstream_connects"], 2)
            hub.leave(subscriber)
        self.check(body)

    def test_a_stream_that_joins_or_waits_while_music_assistant_is_down_hears_it_at_once(self):
        async def body(hub):
            port = self.server.port
            await asyncio.to_thread(self.server.stop)
            waiting = hub.join(music_hub.VIEW)
            self.assertEqual(await self.next(waiting), {"type": "unavailable"})
            late = hub.join(music_hub.VIEW)
            self.assertEqual(await self.next(late), {"type": "unavailable"})
            self.server = oh_music_sim.Server(self.sim, port).start()
            self.addCleanup(self.server.stop)
            for subscriber in (waiting, late):
                self.assertTrue((await self.next(subscriber, "snapshot"))["music"]["available"])
                hub.leave(subscriber)
        self.check(body)

    def test_a_stream_that_joins_after_the_hub_closed_hears_only_what_music_assistant_is_now(self):
        async def body(hub):
            port = self.server.port
            await asyncio.to_thread(self.server.stop)
            gone = hub.join(music_hub.VIEW)
            self.assertEqual(await self.next(gone), {"type": "unavailable"})
            hub.leave(gone)
            # The hub closes once it has lingered; Music Assistant comes back long after.
            while hub.task and not hub.task.done():
                await asyncio.sleep(0.02)
            self.server = oh_music_sim.Server(self.sim, port).start()
            self.addCleanup(self.server.stop)
            later = hub.join(music_hub.VIEW)
            self.assertEqual((await self.next(later))["type"], "snapshot")
            hub.leave(later)
        self.check(body)

    def test_providers_that_change_before_the_first_snapshot_are_kept_not_streamed(self):
        async def body(hub):
            hub.task = asyncio.get_running_loop().create_future()  # Its connection is not opened here.
            subscriber = hub.join(music_hub.VIEW)
            providers = [{"domain": "tidal", "instance_id": "tidal--abcd1234", "available": True}]
            self.assertFalse(hub.event({"event": "providers_updated", "data": providers}))
            self.assertEqual(hub.sources.tidal, ["tidal--abcd1234"])
            self.assertEqual(list(subscriber.frames), [])
            hub.task.cancel()
            hub.task = None
            hub.leave(subscriber)
        self.check(body)

    def test_a_renewed_token_reconnects_the_stream(self):
        async def body(hub):
            subscriber = hub.join(music_hub.VIEW)
            await self.next(subscriber, "snapshot")
            claim = provision.read_claim(self.state)
            provision.write_claim(self.state, {**claim, "expires_at": 0})
            await asyncio.to_thread(provision.Provisioner(self.state, plain(self.adapter.contract.music)).step)
            self.assertEqual(await self.next(subscriber), {"type": "unavailable"})
            self.assertEqual((await self.next(subscriber, "snapshot"))["type"], "snapshot")
            hub.leave(subscriber)
        self.check(body)
