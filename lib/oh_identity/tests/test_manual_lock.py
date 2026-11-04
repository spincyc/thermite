"""Durable manual holds and credential-optional room controls share authority."""
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import secrets
from unittest.mock import patch

from django.conf import settings
from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

import oh_room_policy
from oh_identity.auth import Guest
from oh_identity.models import Audit, PersonalToken, RoomPolicy, User
from oh_identity.policy_store import Store
from oh_identity.runtime import install_adapter
from oh_identity.security import get_state, transition
from oh_identity.services import Problem, bootstrap, issue_token
from oh_identity.streams import valid
from oh_identity.tests.test_core import PASSPHRASE, adapter


class Requests:
    def prepare(self):
        self.adapter = adapter()
        self.adapter.policy = oh_room_policy
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/api/v1/session", secure=True)
        _, self.owner_token = issue_token(self.owner, "manual lock test")

    def request(self, method, path, data=None, *, token=True, client=None):
        client = client or self.client
        headers = {"HTTP_AUTHORIZATION": "Bearer " + (self.owner_token if token is True else token)} if token else {
            "HTTP_ORIGIN": "https://testserver", "HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value}
        return getattr(client, method)(path, data=json.dumps(data) if data is not None else None,
                                       content_type="application/json", secure=True, **headers)

    def lock(self, enabled=True, revision=0, room="den", **options):
        return self.request("put", f"/api/v1/rooms/{room}/lock", {"revision": revision, "locked": enabled}, **options)

    def command(self, room="den", action="source", value="off", **options):
        return self.request("post", f"/api/v1/rooms/{room}/commands", {"action": action, "value": value}, **options)


class ManualLocks(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE)

    def setUp(self):
        self.prepare()

    def test_capture_source_and_off_are_persistent_idempotent_holds(self):
        for room, source in (("den", "player"), ("desk", "off")):
            with self.subTest(room=room):
                result = self.lock(room=room)
                self.assertEqual(result.status_code, 200)
                dto = result.json()
                self.assertEqual(dto["manual_lock"]["desired"], source)
                self.assertTrue(dto["can_lock"])
                self.assertEqual(dto["effective"]["reason"], "manual_lock")
                self.assertTrue(dto["effective"]["locked"])
                self.assertIsNone(dto["effective"]["until"])
                self.assertFalse(dto["config"]["enabled"], "manual holds do not enable schedules")
                with patch.object(self.adapter.transport, "read", side_effect=AssertionError("idempotent hold recaptured")):
                    again = self.lock(room=room, revision=1).json()
                self.assertEqual(again["revision"], 1)
                self.assertEqual(again["manual_lock"], dto["manual_lock"])
        self.assertEqual(len(Store().policies()), 2)
        replacement = adapter()
        replacement.policy = oh_room_policy
        with self.assertRaises(Problem) as denied:
            replacement.dispatch(Guest(get_state().revision, True), "den", "source", "off")
        self.assertEqual(denied.exception.code, "room_locked")
        self.assertEqual(self.adapter.transport.commands, [])
        self.assertEqual(Audit.objects.filter(action="room.lock").count(), 2)

    def test_unlock_clears_only_hold_and_never_sends_native_command(self):
        self.assertEqual(self.lock().status_code, 200)
        result = self.lock(False, revision=1)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json()["manual_lock"])
        self.assertFalse(result.json()["effective"]["locked"])
        self.assertEqual(self.lock(False, revision=2).json()["revision"], 2)
        self.assertEqual(self.adapter.transport.commands, [])
        self.assertEqual(self.command(token=False).status_code, 202)
        self.assertEqual(self.lock(False, room="desk").json()["revision"], 0)
        self.assertFalse(RoomPolicy.objects.filter(room_id="desk").exists())

    def test_guest_remote_controls_and_status_work_with_authentication_enabled(self):
        me = self.request("get", "/api/v1/me", token=False).json()
        self.assertTrue(me["is_guest"])
        self.assertTrue(me["auth_enabled"])
        self.assertFalse(me["can_edit"])
        self.assertEqual(self.command(token=False).status_code, 202)
        self.adapter.transport.commands.clear()
        self.assertEqual(self.lock().status_code, 200)
        dto = self.request("get", "/api/v1/rooms/den/policy", token=False).json()
        self.assertFalse(dto["can_edit"] or dto["can_lock"])
        self.assertTrue(dto["effective"]["locked"])
        self.assertEqual(dto["manual_lock"]["desired"], "player")
        for token in (False, True):
            for action, value in (("source", "off"), ("volume", 45)):
                self.assertEqual(self.command(action=action, value=value, token=token).status_code, 423)
        self.assertEqual(self.adapter.transport.commands, [])
        self.assertTrue(valid({"guest": True, "security_revision": get_state().revision}))

    def test_shared_controls_obey_neighbor_manual_hold_for_guests(self):
        self.assertEqual(self.lock(room="desk").status_code, 200)
        self.assertEqual(self.command(action="shared_power", value="OFF", token=False).status_code, 423)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_only_admin_may_lock_unlock_or_edit_schedules(self):
        _, operator = issue_token(self.operator, "ordinary user")
        schedule = {"revision": 0, "config": {"version": 1, "enabled": False, "daily_off": [], "locks": []}}
        with patch.object(self.adapter.transport, "read", side_effect=AssertionError("unauthorized native read")):
            for token, status in ((False, 401), (operator, 403)):
                self.assertEqual(self.lock(token=token).status_code, status)
                # Guests are asked to sign in for every Admin mutation alike.
                self.assertEqual(self.request("put", "/api/v1/rooms/den/policy", schedule, token=token).status_code, status)
        self.assertFalse(RoomPolicy.objects.exists())
        self.assertEqual(self.lock().status_code, 200)
        self.assertEqual(self.lock(False, revision=1, token=False).status_code, 401)

    def test_session_lock_requires_csrf_and_stale_credentials_do_not_become_guest(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        session = client.session
        session["identity_revision"] = self.owner.auth_revision
        session.save()
        payload = json.dumps({"locked": True, "revision": 0})
        self.assertEqual(client.put("/api/v1/rooms/den/lock", payload, content_type="application/json", secure=True).status_code, 403)
        client.get("/api/v1/session", secure=True)
        self.assertEqual(self.lock(client=client, token=False).status_code, 200)
        client.cookies[settings.SESSION_COOKIE_NAME] = "expired-browser-session"
        self.assertEqual(client.get("/api/v1/me", secure=True).status_code, 401)
        unknown = "thm_" + secrets.token_urlsafe(32)
        self.assertEqual(self.command(token=unknown).status_code, 401)

    def test_unavailable_or_unknown_source_never_creates_a_hold(self):
        for value in ("NULL", "UNDEF", "", "unconfigured-source", None):
            self.adapter.transport.states["loc_den_source"] = value
            response = self.lock()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"], "room_state_unavailable")
        with patch.object(self.adapter.transport, "read", side_effect=TimeoutError):
            self.assertEqual(self.lock().status_code, 502)
        self.assertFalse(RoomPolicy.objects.exists())

    def test_caller_cannot_supply_target_or_invalid_lock_shape(self):
        with patch.object(self.adapter.transport, "read", side_effect=AssertionError("invalid lock read the room")):
            for data in ({"locked": 1, "revision": 0}, {"locked": True, "revision": True}, {"locked": True, "revision": -1}, {},
                         {"locked": False, "revision": 0, "desired": "off"}, {"locked": True, "revision": 0, "desired": None},
                         {"locked": True, "revision": 0, "desired": "unconfigured-source"},
                         {"locked": True, "revision": 0, "desired": ["off"]}, {"locked": True, "revision": 0, "target": "off"}):
                with self.subTest(data=data):
                    response = self.request("put", "/api/v1/rooms/den/lock", data)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"], "invalid_lock")
        self.assertEqual(self.lock(room="missing").status_code, 404)
        self.assertFalse(RoomPolicy.objects.exists())

    def test_desired_locks_what_the_room_still_reads(self):
        for room, source in (("den", "player"), ("desk", "off")):
            with self.subTest(room=room):
                response = self.request("put", f"/api/v1/rooms/{room}/lock", {"revision": 0, "locked": True, "desired": source})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["manual_lock"]["desired"], source)
                self.assertTrue(response.json()["effective"]["locked"])
        self.assertEqual(Audit.objects.filter(action="room.lock").count(), 2)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_desired_mismatch_is_refused_storing_and_auditing_nothing(self):
        response = self.request("put", "/api/v1/rooms/den/lock", {"revision": 0, "locked": True, "desired": "off"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "room_state_changed")
        self.assertFalse(RoomPolicy.objects.exists())
        self.assertFalse(Audit.objects.filter(action__startswith="room.").exists())
        # A hold already on another source is not reported as this lock either.
        held = self.lock().json()
        with patch.object(self.adapter.transport, "read", side_effect=AssertionError("idempotent hold recaptured")):
            response = self.request("put", "/api/v1/rooms/den/lock", {"revision": 1, "locked": True, "desired": "off"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "room_state_changed")
        self.assertEqual(RoomPolicy.objects.get().revision, 1)
        self.assertEqual(RoomPolicy.objects.get().config["manual_lock"], held["manual_lock"])
        self.assertEqual(Audit.objects.filter(action="room.lock").count(), 1)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_cas_rejects_stale_toggle_and_schedule_editor_preserves_hold(self):
        first = self.lock().json()
        self.assertEqual(self.lock(False, revision=0).status_code, 409)
        schedule = {"version": 1, "enabled": True, "daily_off": [{"id": "night", "time": "23:00", "days": [0]}], "locks": []}
        response = self.request("put", "/api/v1/rooms/den/policy", {"revision": 1, "config": schedule})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["manual_lock"], first["manual_lock"])
        for value in (None, {**first["manual_lock"], "desired": "off"}):
            self.assertEqual(self.request("put", "/api/v1/rooms/den/policy", {
                "revision": 2, "config": {**schedule, "manual_lock": value}}).status_code, 400)
        final = self.lock(False, revision=2).json()
        self.assertEqual(final["config"]["daily_off"], schedule["daily_off"])
        self.assertTrue(final["config"]["enabled"])
        self.assertNotIn("manual_lock", final["config"])

    def test_authentication_off_suspends_hold_and_on_resumes_it_for_new_guests(self):
        first = self.lock().json()["manual_lock"]
        old_guest = Guest(get_state().revision, True)
        transition(get_state(), False, self.owner)
        with self.assertRaises(Problem):
            self.adapter.dispatch(old_guest, "den", "source", "off")
        self.assertEqual(self.command(token=False).status_code, 202)
        self.assertEqual(Store().policies(), [])
        self.assertEqual(self.lock(token=False).status_code, 403)
        self.assertEqual(RoomPolicy.objects.get().config["manual_lock"], first)
        transition(get_state(), True, self.owner)
        self.assertEqual(self.command(token=False).status_code, 423)
        self.assertEqual(len(Store().policies()), 1)
        self.assertEqual(RoomPolicy.objects.get().config["manual_lock"], first)

    def test_revision_and_credential_are_rechecked_after_native_read(self):
        original = self.adapter.transport.read
        def revoke(item):
            value = original(item)
            PersonalToken.objects.update(revoked=True)
            return value
        with patch.object(self.adapter.transport, "read", side_effect=revoke):
            self.assertEqual(self.lock().status_code, 401)
        self.assertFalse(RoomPolicy.objects.exists())
        _, self.owner_token = issue_token(self.owner, "replacement")
        def changed(item):
            value = original(item)
            RoomPolicy.objects.create(room_id="den", revision=1, config={"version": 1})
            return value
        with patch.object(self.adapter.transport, "read", side_effect=changed):
            self.assertEqual(self.lock().status_code, 409)
        self.assertNotIn("manual_lock", RoomPolicy.objects.get().config)


class ManualLockTransactions(Requests, TransactionTestCase):
    def setUp(self):
        self.owner = bootstrap("owner", PASSPHRASE)
        self.prepare()

    def test_native_capture_holds_command_lock_without_a_database_transaction(self):
        held = []
        @contextmanager
        def command_lock(rooms, resources):
            held.append(rooms)
            try:
                yield
            finally:
                held.pop()
        original = self.adapter.transport.read
        def read(item):
            self.assertEqual(held, [("den",)])
            self.assertFalse(connection.in_atomic_block)
            return original(item)
        self.adapter.command_lock = command_lock
        with patch.object(self.adapter.transport, "read", side_effect=read):
            self.assertEqual(self.lock().status_code, 200)

    def test_guest_all_off_skips_manual_hold_and_controls_unlocked_room(self):
        self.assertEqual(self.lock().status_code, 200)
        response = self.request("post", "/api/v1/all-off", {}, token=False)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["accepted"], ["desk"])
        self.assertEqual(response.json()["skipped"], [{"room": "den", "reason": "room_locked"}])
        self.assertEqual(self.adapter.transport.commands, [("loc_desk_source", "off")])


class SharedDevices(TestCase):
    """A lock protects what its room holds, not every room sharing a source."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        import oh_render, oh_site_contract
        from oh_conf import House
        from oh_identity.runtime import RuntimeAdapter, RuntimeContract
        data = json.loads((Path(__file__).resolve().parents[3] / "instance/devices.yaml").read_text())
        data["routing"]["rooms"]["example_east"]["sources"].append("example_player")
        self.commands = []
        transport = type("Transport", (), {"command": lambda _, item, value: self.commands.append((item, value)),
                                            "read": lambda _, item: "off"})()
        self.adapter = RuntimeAdapter(RuntimeContract(**oh_site_contract.compile(House(data), oh_render.build(data))),
                                      transport, policy=oh_room_policy)
        install_adapter(self.adapter)

    def hold(self, room, desired):
        config = {"version": 1, "room": room, "timezone": settings.TIME_ZONE, "revision": 1, "enabled": False,
                  "daily_off": [], "locks": [], "manual_lock": {"desired": desired, "started_at": timezone.now().isoformat()}}
        RoomPolicy.objects.update_or_create(room_id=room, defaults={"config": config, "revision": 1})

    def outcome(self, room, action, value):
        try:
            self.adapter.dispatch(Guest(get_state().revision, True), room, action, value)
            return "sent"
        except Problem as refused:
            return refused.code

    def test_a_room_held_off_leaves_shared_sources_to_other_rooms(self):
        self.hold("example_west", "off")
        self.assertEqual(self.outcome("example_east", "source", "example_player"), "sent")
        self.assertEqual(self.outcome("example_east", "example_player.key", "back"), "sent")
        self.assertEqual(self.outcome("example_west", "source", "example_player"), "room_locked")

    def test_a_held_source_may_be_shared_but_not_driven_from_another_room(self):
        self.hold("example_west", "example_player")
        self.assertEqual(self.outcome("example_east", "source", "example_player"), "sent")
        self.assertEqual(self.outcome("example_east", "source", "off"), "sent")
        for action, value in (("example_player.key", "back"), ("example_player.power", "OFF")):
            self.assertEqual(self.outcome("example_east", action, value), "device_locked", action)
        self.assertNotIn(("example_player_power", "OFF"), self.commands)
