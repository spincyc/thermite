"""An audio device's own page: the gateway's device route commands what its
remote and its audio page declare, each only a value it takes; a locked room
that owns the device locks it there too, a room that only shows it as a
source refuses only what would disturb its route. Like a room's commands,
it needs no account. Fictional devices and documentation addresses only."""
import copy
import json
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.utils import timezone

import oh_room_policy
from oh_identity.auth import Guest
from oh_identity.models import RoomPolicy
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.security import get_state
from oh_identity.services import Problem, bootstrap
from oh_identity.tests.test_core import PASSPHRASE
from oh_identity.tests.test_trail import Requests, lines

WEST, EAST, PLAYER = "example_west", "example_east", "example_player"
WIIM, AMP, KITCHEN = "example_desk_wiim", "example_kitchen_amp", "example_kitchen"
ESTATE = "example_estate"


def house():
    """The example house with a WiiM as the East room's sound and a Sonos Amp
    in a kitchen no routed room covers."""
    from oh_integrations import sonos
    data = json.loads((Path(__file__).resolve().parents[3] / "instance/devices.yaml").read_text())
    data["rooms"][KITCHEN] = {"label": "Example Kitchen", "tag": "Kitchen"}
    mac = bytes((2, 0, 0, 0, 0, 21)).hex(":")
    data["devices"] += [
        {"id": WIIM, "room": EAST, "kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro", "ip": "192.0.2.32",
         "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}},
        {"id": AMP, "room": KITCHEN, "kind": "amp", "make": "Sonos", "model": "Amp", "ip": "192.0.2.21", "mac": mac,
         "sonos_udn": sonos.udn(mac), "controls": {"observe": ["sonos"], "control": ["sonos"]}}]
    data["routing"]["rooms"][EAST]["audio"] = WIIM
    return data


def compiled(data):
    import oh_render
    import oh_site_contract
    from oh_conf import House
    return oh_site_contract.compile(House(data), oh_render.build(data))


class Contract(TestCase):
    def test_each_audio_device_gets_its_remotes_controls_and_its_settings(self):
        devices = compiled(house())["devices"]
        self.assertEqual(set(devices), {WIIM, AMP})
        wiim = devices[WIIM]
        self.assertEqual(wiim["rooms"], {EAST: "own"})
        self.assertEqual(wiim["actions"]["volume"], {"kind": "number", "min": 0, "max": 100, "step": 1,
                                                     "item": f"{WIIM}_volume", "resources": [f"device:{WIIM}"]})
        self.assertEqual(wiim["actions"]["key"]["values"], ["next", "play_pause", "previous"])
        self.assertEqual(wiim["actions"][f"{WIIM}_eq_preset"]["kind"], "enum")
        self.assertIn("R&B", wiim["actions"][f"{WIIM}_eq_preset"]["values"])
        self.assertEqual(wiim["actions"][f"{WIIM}_play_mode"]["values"], ["4", "0", "1", "3", "2"])
        self.assertEqual({key: wiim["actions"][f"{WIIM}_sub_level"][key] for key in ("min", "max", "step")},
                         {"min": -15, "max": 15, "step": 1})
        # Read-only facts are never actions.
        self.assertFalse({f"{WIIM}_firmware", f"{WIIM}_cpu_temperature"} & set(wiim["actions"]))
        amp = devices[AMP]
        self.assertEqual(amp["rooms"], {})
        self.assertIn(f"{AMP}_bass", amp["actions"])
        self.assertNotIn(f"{AMP}_sub_gain", amp["actions"])

    def test_an_audio_streamer_where_a_room_is_routed_is_that_rooms_sound(self):
        """A WiiM Mini beside a routed room's own sound is the room's too, as a
        streaming amp is, and offers only the inputs it has."""
        data = house()
        mini = "example_east_mini"
        data["devices"].append({"id": mini, "room": EAST, "kind": "audio_streamer", "make": "WiiM", "model": "Mini",
                                "ip": "192.0.2.33", "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}})
        device = compiled(data)["devices"][mini]
        self.assertEqual(device["rooms"], {EAST: "own"})
        self.assertEqual(sorted(device["actions"]["input"]["values"]), ["bluetooth", "line-in", "wifi"])
        self.assertNotIn(f"{mini}_sub_level", device["actions"])

    def test_a_play_mode_reads_what_it_is_never_sent_and_the_remote_view_carries_the_page(self):
        authority = compiled(house())
        self.assertEqual(authority["states"][f"{WIIM}_play_mode"]["values"], ["4", "0", "1", "3", "2", "5"])
        self.assertTrue(authority["states"][f"{WIIM}_eq"]["commands"])
        read = set(authority["views"]["remote"]["statechanged"])
        for suffix in ("status", "album", "duration", "eq_preset", "sub", "sub_level", "cpu_temperature", "firmware"):
            self.assertIn(f"{WIIM}_{suffix}", read)
        self.assertIn(f"{AMP}_treble", read)

    def test_a_device_two_rooms_own_is_locked_by_either(self):
        data = house()
        data["routing"]["rooms"][WEST]["audio"] = WIIM
        rooms = compiled(data)["rooms"]
        self.assertEqual(rooms[WEST]["actions"][f"{WIIM}.volume"]["own_rooms"], [EAST])
        self.assertEqual(rooms[EAST]["actions"][f"{WIIM}.volume"]["own_rooms"], [WEST])
        self.assertNotIn("own_rooms", rooms[WEST]["actions"][f"{PLAYER}.key"])

    def test_a_contract_naming_a_room_it_lacks_is_refused(self):
        authority = compiled(house())
        broken = copy.deepcopy(authority)
        broken["devices"][WIIM]["rooms"] = {"example_nowhere": "own"}
        with self.assertRaisesRegex(ValueError, "device contract"):
            RuntimeContract(**broken)
        broken["devices"][WIIM]["rooms"] = {EAST: "owner"}
        with self.assertRaisesRegex(ValueError, "device contract"):
            RuntimeContract(**broken)


class Dispatch(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.data = house()
        self.commands = []
        self.transport = type("Transport", (), {
            "command": lambda _, item, value, source=None: self.commands.append((item, value)),
            "read": lambda _, item: "off"})()
        self.install(compiled(self.data))

    def install(self, authority):
        self.adapter = RuntimeAdapter(RuntimeContract(**authority), self.transport, policy=oh_room_policy)
        install_adapter(self.adapter)

    def hold(self, room, desired):
        config = {"version": 1, "room": room, "timezone": settings.TIME_ZONE, "revision": 1, "enabled": False,
                  "daily_off": [], "locks": [],
                  "manual_lock": {"desired": desired, "started_at": timezone.now().isoformat()}}
        RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": 1})

    def outcome(self, device, action, value, user=None):
        try:
            self.adapter.dispatch_device(user or self.owner, device, action, value)
            return "sent"
        except Problem as refused:
            return refused.code

    def test_an_account_sends_what_the_page_declares_and_nothing_else(self):
        self.assertEqual(self.outcome(WIIM, f"{WIIM}_eq_preset", "R&B"), "sent")
        self.assertEqual(self.outcome(WIIM, f"{WIIM}_sub_level", -4), "sent")
        self.assertEqual(self.outcome(AMP, "key", "play_pause"), "sent")
        self.assertEqual(self.outcome(WIIM, f"{WIIM}_sub_level", 16), "invalid_command")
        self.assertEqual(self.outcome(WIIM, f"{WIIM}_play_mode", "5"), "invalid_command")
        self.assertEqual(self.outcome(WIIM, f"{WIIM}_firmware", "x"), "unknown_action")
        self.assertEqual(self.outcome(WIIM, f"{AMP}_bass", 1), "unknown_action")
        self.assertEqual(self.outcome("example_nowhere", "volume", 1), "unknown_action")
        self.assertEqual(self.commands, [(f"{WIIM}_eq_preset", "R&B"), (f"{WIIM}_sub_level", "-4"),
                                         (f"{AMP}_key", "play_pause")])

    def test_a_guest_sends_as_an_account_does(self):
        self.assertTrue(get_state().enabled)
        self.assertEqual(self.outcome(AMP, "volume", 30, Guest(get_state().revision, True)), "sent")
        self.hold(EAST, "off")
        self.assertEqual(self.outcome(WIIM, "volume", 30, Guest(get_state().revision, True)), "device_locked")
        self.assertEqual(self.commands, [(f"{AMP}_volume", "30")])

    def test_a_room_locked_locks_what_it_owns_and_nothing_it_does_not(self):
        self.hold(EAST, "off")
        self.assertEqual(self.outcome(WIIM, "volume", 30), "device_locked")
        self.assertEqual(self.outcome(WIIM, f"{WIIM}_light", "OFF"), "device_locked")
        self.assertEqual(self.outcome(AMP, "volume", 30), "sent")
        self.assertEqual(self.commands, [(f"{AMP}_volume", "30")])

    def test_another_rooms_remote_cannot_drive_a_device_a_locked_room_owns(self):
        self.data["routing"]["rooms"][WEST]["audio"] = WIIM
        self.install(compiled(self.data))
        self.hold(EAST, "off")
        try:
            self.adapter.dispatch(self.owner, WEST, f"{WIIM}.volume", 30)
            outcome = "sent"
        except Problem as refused:
            outcome = refused.code
        # East is not the room commanding it, so the refusal is not "this room is locked".
        self.assertEqual(outcome, "device_locked")
        self.assertEqual(self.commands, [])

    def test_a_building_shares_its_rooms_sound_and_its_lock_leaves_their_volume(self):
        """A zone over a Building commands the streaming amps and soundbars
        of the rooms in it, and only shares them: locked, it leaves East's
        own remote and the device's page their volume."""
        self.data["rooms"][ESTATE] = {"label": "Example Estate", "tag": "Building"}
        self.data["rooms"][EAST]["in"] = ESTATE
        self.data["routing"]["rooms"][ESTATE] = {"hdmi_output": 3, "sources": ["example_computer"]}
        authority = compiled(self.data)
        self.assertEqual(authority["devices"][WIIM]["rooms"], {EAST: "own", ESTATE: "shared"})
        self.install(authority)
        self.hold(ESTATE, "off")
        self.adapter.dispatch(self.owner, EAST, f"{WIIM}.volume", 30)
        self.assertEqual(self.outcome(WIIM, "volume", 31), "sent")
        self.assertEqual(self.commands, [(f"{WIIM}_volume", "30"), (f"{WIIM}_volume", "31")])

    def test_a_room_that_only_shows_a_device_refuses_only_what_disturbs_its_route(self):
        authority = compiled(self.data)
        # A page for the West room's source, as a streamer with audio() would have.
        spec = {"item": f"{PLAYER}_key", "kind": "key", "values": ["play_pause"], "resources": [f"device:{PLAYER}"]}
        authority["devices"][PLAYER] = {"actions": {"key": spec}, "rooms": {WEST: "shared"}}
        self.install(authority)
        self.hold(WEST, PLAYER)
        self.assertEqual(self.outcome(PLAYER, "key", "play_pause"), "device_locked")
        self.hold(WEST, "off")
        self.assertEqual(self.outcome(PLAYER, "key", "play_pause"), "sent")


class Route(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.commands = []
        transport = type("Transport", (), {
            "command": lambda _, item, value, source=None: self.commands.append((item, value)),
            "read": lambda _, item: "off"})()
        install_adapter(RuntimeAdapter(RuntimeContract(**compiled(house())), transport, policy=oh_room_policy))
        self.client = Client(enforce_csrf_checks=True)
        self.request("get", "/api/v1/session")

    def device_command(self, device, action, value):
        return self.request("post", f"/api/v1/devices/{device}/commands", {"action": action, "value": value})

    def test_a_signed_in_command_is_accepted_and_its_trail_names_the_device(self):
        self.signin("owner")
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            response = self.device_command(WIIM, f"{WIIM}_eq", "OFF")
        self.assertEqual((response.status_code, response.json()),
                         (202, {"accepted": True, "device": WIIM, "action": f"{WIIM}_eq"}))
        [line] = lines(logs, "cmd")
        self.assertEqual((line["room"], line["device"], line["item"], line["value"], line["outcome"]),
                         ("-", WIIM, f"{WIIM}_eq", "OFF", "accepted"))
        self.assertEqual(self.commands, [(f"{WIIM}_eq", "OFF")])

    def test_a_guest_is_accepted_and_a_malformed_body_is_refused(self):
        self.assertEqual(self.device_command(AMP, "volume", 30).status_code, 202)
        response = self.request("post", f"/api/v1/devices/{AMP}/commands", {"action": "volume"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.commands, [(f"{AMP}_volume", "30")])

    def test_the_guest_listener_serves_the_route(self):
        from oh_identity.guest import ROUTES
        self.assertEqual(ROUTES["api/v1/devices/<slug:device>/commands"], ("POST",))
