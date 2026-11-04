"""A source cabled to a receiver, offered on two of its zones: a room held on it
leaves the other room free to choose it, never to drive it (the gateway's
cross-room lock check compares the rooms' routes and resources)."""
import json
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

import oh_room_policy
from oh_identity.auth import Guest
from oh_identity.models import RoomPolicy
from oh_identity.runtime import install_adapter
from oh_identity.security import get_state
from oh_identity.services import Problem, bootstrap
from oh_identity.tests.test_core import PASSPHRASE

WEST, EAST, PLAYER, COMPUTER = "example_west", "example_east", "example_player", "example_computer"


def two_zone_house():
    """The example house with a receiver before both screens: its main zone
    the West TV's, its zone 2 the East display's. The player is cabled to the
    receiver, and both rooms offer it on its input there."""
    data = json.loads((Path(__file__).resolve().parents[3] / "instance/devices.yaml").read_text())
    data["devices"].append({"id": "example_avr", "room": WEST, "kind": "avr", "make": "Marantz", "model": "CINEMA 30",
                            "name": "Example AVR", "ip": "192.0.2.15", "zones": 2,
                            "controls": {"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}})
    rooms = data["routing"]["rooms"]
    rooms[WEST]["display"].pop("sources")
    rooms[EAST]["sources"].append(PLAYER)
    for room, zone, feed in ((WEST, {}, "BD"), (EAST, {"zone": 2}, "AUX2")):
        rooms[room]["receiver"] = {"device": "example_avr", **zone, "input": feed, "sources": {PLAYER: "MPLAY"}}
    return data


class SharedZoneSource(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        import oh_render
        import oh_site_contract
        from oh_conf import House
        from oh_identity.runtime import RuntimeAdapter, RuntimeContract
        data = two_zone_house()
        self.assertEqual(oh_render.validate(data), [])
        self.commands = []
        transport = type("Transport", (), {"command": lambda _, item, value, source=None: self.commands.append((item, value)),
                                            "read": lambda _, item: "off"})()
        self.adapter = RuntimeAdapter(RuntimeContract(**oh_site_contract.compile(House(data), oh_render.build(data))),
                                      transport, policy=oh_room_policy)
        install_adapter(self.adapter)

    def hold(self, room, desired):
        config = {"version": 1, "room": room, "timezone": settings.TIME_ZONE, "revision": 1, "enabled": False,
                  "daily_off": [], "locks": [],
                  "manual_lock": {"desired": desired, "started_at": timezone.now().isoformat()}}
        RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": 1})

    def outcome(self, room, action, value):
        try:
            self.adapter.dispatch(Guest(get_state().revision, True), room, action, value)
            return "sent"
        except Problem as refused:
            return refused.code

    def test_a_room_held_on_it_lets_the_other_zone_choose_it_but_not_drive_it(self):
        for held, other in ((WEST, EAST), (EAST, WEST)):
            with self.subTest(held=held):
                RoomPolicy.objects.all().delete()
                self.hold(held, PLAYER)
                for value in (PLAYER, COMPUTER, "off"):
                    self.assertEqual(self.outcome(other, "source", value), "sent", value)
                for action, value in ((f"{PLAYER}.key", "back"), (f"{PLAYER}.power", "OFF")):
                    self.assertEqual(self.outcome(other, action, value), "device_locked", action)
                self.assertEqual(self.outcome(held, "source", "off"), "room_locked")
        self.assertNotIn((f"{PLAYER}_power", "OFF"), self.commands)

    def test_a_room_held_on_another_source_leaves_it_to_the_other_room(self):
        self.hold(WEST, COMPUTER)
        self.assertEqual(self.outcome(EAST, f"{PLAYER}.key", "back"), "sent")
