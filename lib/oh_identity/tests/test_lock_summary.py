"""Every room's lock in one read: the room lists' view of what policy status reports."""
from datetime import datetime, timezone
from unittest.mock import patch

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

import oh_room_policy
from oh_identity.models import RoomPolicy, SecurityState, User
from oh_identity.runtime import install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, adapter

UNLOCKED = {"locked": False, "manual": False, "until": None, "reason": "unlocked"}


class LockSummary(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE)

    def setUp(self):
        self.now = datetime(2026, 11, 2, 10, 30, tzinfo=timezone.utc)  # Monday
        patcher = patch("django.utils.timezone.now", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = adapter()
        self.adapter.policy = oh_room_policy
        install_adapter(self.adapter)
        _, self.admin = issue_token(self.owner, "lock summary test")
        _, self.member = issue_token(self.operator, "lock summary test")

    def tearDown(self):
        self.assertEqual(self.adapter.transport.commands, [], "reading locks never routes or enforces")

    def save(self, room, config, revision=1):
        return RoomPolicy.objects.create(room_id=room, config={"room": room, "timezone": "UTC", "revision": revision, **config},
                                         revision=revision)

    def schedule(self, room="den"):
        return self.save(room, {"version": 1, "locks": [
            {"id": "film", "start": "10:00", "end": "11:00", "days": [0], "desired": "player"}]})

    def hold(self, room="desk"):
        return self.save(room, {"version": 1, "enabled": False, "daily_off": [], "locks": [],
                                "manual_lock": {"desired": "player", "started_at": "2026-11-02T09:00:00+00:00"}})

    def read(self, token=None, status=200):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token} if token else {}
        response = Client().get("/api/v1/locks", secure=True, **headers)
        self.assertEqual(response.status_code, status)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        return response.json()

    def test_guests_members_and_admins_read_the_same_answer(self):
        self.hold()
        answers = [self.read(token) for token in (None, self.member, self.admin)]
        self.assertEqual(answers[0], {"rooms": {"den": UNLOCKED, "desk": {
            "locked": True, "manual": True, "until": None, "reason": "manual_lock"}}})
        self.assertEqual(answers[1:], answers[:1] * 2)

    def test_schedule_lock_reports_its_end_as_policy_status_does(self):
        self.schedule()
        self.hold()
        rooms = self.read()["rooms"]
        self.assertEqual(rooms["den"], {"locked": True, "manual": False, "until": "2026-11-02T11:00:00Z", "reason": "room_locked"})
        for room in ("den", "desk"):
            status = Client().get(f"/api/v1/rooms/{room}/policy", secure=True).json()["effective"]
            self.assertEqual({key: rooms[room][key] for key in ("locked", "until", "reason")},
                             {key: status[key] for key in ("locked", "until", "reason")}, room)

    def test_one_query_reads_every_policy_and_ignores_rooms_the_house_lacks(self):
        self.schedule()
        self.hold()
        self.save("attic", {"version": 1, "enabled": True, "locks": []})
        with CaptureQueriesContext(connection) as queries:
            rooms = self.read()["rooms"]
        self.assertEqual(set(rooms), {"den", "desk"})
        self.assertEqual(sum("oh_identity_roompolicy" in query["sql"] for query in queries.captured_queries), 1)

    def test_ended_or_disabled_schedules_are_unlocked(self):
        self.save("den", {"version": 1, "enabled": False, "locks": [
            {"id": "film", "start": "10:00", "end": "11:00", "days": [0], "desired": "player"}]})
        self.save("desk", {"version": 1, "locks": [
            {"id": "night", "start": "22:00", "end": "23:00", "days": [0], "desired": "off"}]})
        self.assertEqual(self.read()["rooms"], {"den": UNLOCKED, "desk": UNLOCKED})

    def test_unusable_policy_locks_its_room_as_policy_unavailable(self):
        self.save("den", {"version": 99, "enabled": True})
        unavailable = {"locked": True, "manual": False, "until": None, "reason": "policy_unavailable"}
        self.assertEqual(self.read()["rooms"]["den"], unavailable)
        self.assertEqual(Client().get("/api/v1/rooms/den/policy", secure=True).json()["effective"]["reason"], "policy_unavailable")
        self.hold()
        self.adapter.policy = None
        self.assertEqual(self.read()["rooms"], {"den": unavailable, "desk": unavailable})

    def test_authentication_off_unlocks_every_room_without_reading_policies(self):
        self.schedule()
        self.hold()
        SecurityState.objects.filter(pk=1).update(enabled=False)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.read()["rooms"], {"den": UNLOCKED, "desk": UNLOCKED})
        self.assertFalse(any("oh_identity_roompolicy" in query["sql"] for query in queries.captured_queries))
        self.assertEqual(self.read(self.admin, 403)["error"], "authentication_disabled")

    def test_only_get_is_allowed(self):
        response = Client().post("/api/v1/locks", secure=True)
        self.assertEqual((response.status_code, response["Allow"], response.json()["error"]), (405, "GET", "method_not_allowed"))
