"""Policy status reports current enforcement, never a historical green state."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import Client, TestCase

import oh_room_policy
from oh_identity.models import ReconcileState, Role, RoomPolicy, User
from oh_identity.runtime import install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, adapter


class PolicyStatus(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE, role=Role.objects.get(name="User"))
        cls.viewer = User.objects.create_user("viewer", password=PASSPHRASE, role=Role.objects.get(name="Viewer"))

    def setUp(self):
        self.now = datetime(2026, 11, 2, 10, 30, tzinfo=timezone.utc)  # Monday
        patcher = patch("django.utils.timezone.now", return_value=self.now)
        self.clock = patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = adapter()
        self.adapter.policy = oh_room_policy
        install_adapter(self.adapter)
        _, self.token = issue_token(self.operator, "policy status test")
        self.client = Client(enforce_csrf_checks=True)

    def tearDown(self):
        self.assertEqual(self.adapter.transport.commands, [], "status GET must never enforce or route")

    def policy(self, desired="player", locks=None):
        config = oh_room_policy.validate({"version": 1, "room": "den", "timezone": "UTC", "revision": 1,
            "locks": locks if locks is not None else [
                {"id": "hold", "start": "10:00", "end": "11:00", "days": [0], "desired": desired}]},
            "den", self.adapter.contract.policy_contract())
        return RoomPolicy.objects.create(room_id="den", config=config, revision=1)

    def report(self, desired="player", age=5, revision=1):
        state = {"phase": "verified", "state": "verified", "desired": desired,
                 "matched": ["loc_den_source"], "mismatched": [], "unknown": [],
                 "scope": "configured_native_controls"}
        report = ReconcileState.objects.create(room_id="den", revision=revision, state=state)
        ReconcileState.objects.filter(pk=report.pk).update(updated_at=self.clock.return_value - timedelta(seconds=age))
        report.refresh_from_db()
        return report

    def get(self, token=None):
        response = self.client.get("/api/v1/rooms/den/policy", secure=True,
                                   HTTP_AUTHORIZATION="Bearer " + (token or self.token))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def unknown(self, reason, report=None):
        dto = self.get()
        self.assertEqual(dto["enforcement"]["state"], "unknown")
        self.assertEqual(dto["enforcement"]["reason"], reason)
        self.assertNotEqual(dto["enforcement"].get("phase"), "verified")
        if report:
            before = report.state.copy()
            report.refresh_from_db()
            self.assertEqual(report.state, before, "a status projection must preserve historical reports")
            self.assertIsNotNone(dto["enforcement"]["updated_at"])
        return dto

    def test_active_lock_without_matching_revision_waits_for_worker(self):
        self.policy()
        self.assertTrue(self.unknown("awaiting_worker")["effective"]["locked"])
        self.report(revision=2)
        self.assertIsNone(self.unknown("awaiting_worker")["enforcement"]["updated_at"])

    def test_aged_or_future_worker_record_is_not_verified(self):
        self.policy()
        for age in (16, 86400, -1):
            with self.subTest(age=age):
                ReconcileState.objects.all().delete()
                report = self.report(age=age)
                self.unknown("stale_worker", report)

    def test_fresh_wrong_target_is_not_verified(self):
        self.policy(desired="off")
        dto = self.unknown("target_mismatch", self.report(desired="player", age=1))
        self.assertEqual(dto["effective"]["desired"], "off")

    def test_same_target_previous_occurrence_is_not_current_verification(self):
        self.clock.return_value = self.now.replace(hour=10, minute=0, second=1)
        self.policy(locks=[
            {"id": "earlier", "start": "09:00", "end": "10:00", "days": [0], "desired": "player"},
            {"id": "current", "start": "10:00", "end": "11:00", "days": [0], "desired": "player"}])
        self.unknown("stale_occurrence", self.report(age=2))

    def test_adjacent_new_target_does_not_inherit_verified_state(self):
        self.clock.return_value = self.now.replace(hour=10, minute=0, second=0)
        self.policy(locks=[
            {"id": "earlier", "start": "09:00", "end": "10:00", "days": [0], "desired": "player"},
            {"id": "current", "start": "10:00", "end": "11:00", "days": [0], "desired": "off"}])
        dto = self.unknown("stale_occurrence", self.report(age=1))
        self.assertEqual(dto["effective"]["desired"], "off")

    def test_young_matching_record_preserves_full_current_dto(self):
        self.policy()
        report = self.report(age=5)
        dto = self.get()
        self.assertEqual({key: dto["enforcement"][key] for key in report.state}, report.state)
        self.assertTrue(dto["effective"]["locked"])
        self.assertFalse(dto["effective"]["allowed"])
        self.assertFalse(dto["can_edit"])

    def test_report_at_current_start_and_age_limit_is_valid(self):
        self.clock.return_value = self.now.replace(hour=10, minute=0, second=15)
        self.policy()
        self.report(age=15)
        self.assertEqual(self.get()["enforcement"]["state"], "verified")

    def test_matching_off_target_can_be_verified(self):
        self.policy(desired="off")
        self.report(desired="off")
        self.assertEqual(self.get()["enforcement"]["state"], "verified")

    def test_ended_or_disabled_policy_has_no_current_verification(self):
        saved = self.policy()
        self.clock.return_value = self.now.replace(hour=11, minute=1)
        report = self.report()
        self.assertFalse(self.unknown("unlocked", report)["effective"]["locked"])
        self.clock.return_value = self.now
        saved.config["enabled"] = False
        saved.save(update_fields=["config"])
        self.assertFalse(self.unknown("unlocked", report)["effective"]["locked"])

    def test_no_policy_is_honestly_unlocked(self):
        dto = self.unknown("unlocked")
        self.assertIsNone(dto["config"])
        self.assertEqual(dto["revision"], 0)
        self.assertFalse(dto["effective"]["locked"])

    def test_missing_engine_cannot_reuse_a_verified_record(self):
        self.policy()
        report = self.report()
        self.adapter.policy = None
        self.assertFalse(self.unknown("policy_unavailable", report)["effective"]["allowed"])

    def test_viewer_permissions_do_not_change_physical_enforcement_projection(self):
        self.policy()
        self.report()
        _, token = issue_token(self.viewer, "read only status")
        dto = self.get(token)
        self.assertFalse(dto["effective"]["allowed"])
        self.assertEqual(dto["effective"]["reason"], "room_read_only")
        self.assertEqual(dto["enforcement"]["state"], "verified")
