"""Gateway hardening: throttling, audit, logging, streams, HTTP semantics,
retired policy targets and housekeeping. Synthetic accounts and runtimes only."""

from contextlib import contextmanager
from datetime import timedelta
import json
import logging
import secrets
import sys
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import oh_room_policy
from oh_identity import streams
from oh_identity.hardening import CONTENT_SECURITY_POLICY, PrivateFormatter, PrivateRecord
from oh_identity.models import Audit, BoundaryReceipt, LoginThrottle, PersonalToken, ReconcileState, RoomPolicy, User
from oh_identity.policy_store import next_revision, prune_history, prune_unknown_rooms
from oh_identity.runtime import RuntimeContract, install_adapter
from oh_identity.security import arm_setup
from oh_identity.services import AUDIT_CAP, DENIED_CAP, bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, adapter


class Requests:
    def browser(self, address="192.0.2.10"):
        client = Client(enforce_csrf_checks=True, REMOTE_ADDR=address)
        client.get("/api/v1/session", secure=True)
        return client

    def post(self, client, path, data, **extra):
        return client.post(path, json.dumps(data), content_type="application/json", secure=True, HTTP_ORIGIN="https://testserver",
                           HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value, **extra)

    def login(self, username="owner", secret=PASSPHRASE, address="192.0.2.10"):
        return self.post(self.browser(address), "/api/v1/session", {"username": username, "password": secret})


class Throttling(Requests, TestCase):
    WRONG = "not the passphrase at all"

    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        install_adapter(adapter())

    def test_failures_from_one_peer_never_lock_the_owner_out_elsewhere(self):
        for _ in range(5):
            self.assertEqual(self.login(secret=self.WRONG, address="192.0.2.66").status_code, 401)
        locked = self.login(address="192.0.2.66")
        self.assertEqual(locked.status_code, 429)
        self.assertTrue(1 <= int(locked["Retry-After"]) <= 300)
        self.assertEqual(self.login(address="192.0.2.10").status_code, 200)

    def test_successful_sign_ins_are_never_counted(self):
        for _ in range(8):
            self.assertEqual(self.login().status_code, 200)
        self.assertFalse(LoginThrottle.objects.exists())

    def test_ipv6_peers_count_per_64_and_mapped_ipv4_as_ipv4(self):
        for index in range(5):
            self.login(secret=self.WRONG, address=f"2001:db8::{index + 1}")
        self.assertEqual(self.login(address="2001:db8::ff").status_code, 429)
        self.assertEqual(self.login(address="2001:db8:0:1::1").status_code, 200)
        for _ in range(5):
            self.login(secret=self.WRONG, address="::ffff:192.0.2.77")
        self.assertEqual(self.login(address="192.0.2.77").status_code, 429)

    def test_an_account_attacked_from_many_peers_is_slowed_not_locked(self):
        now = timezone.now()
        with patch("oh_identity.auth.timezone.now", return_value=now):
            for index in range(11):
                self.assertEqual(self.login(secret=self.WRONG, address=f"192.0.2.{100 + index}").status_code, 401)
            slowed = self.login(address="192.0.2.200")
            self.assertEqual(slowed.status_code, 429)
            self.assertTrue(1 <= int(slowed["Retry-After"]) <= 60)
        with patch("oh_identity.auth.timezone.now", return_value=now + timedelta(seconds=61)):
            self.assertEqual(self.login(address="192.0.2.200").status_code, 200)

    def test_one_peer_is_bounded_across_usernames_and_rows_stay_bounded(self):
        for index in range(30):
            self.login(username=f"nobody-{index}", secret=self.WRONG, address="192.0.2.90")
        self.assertEqual(self.login(username="nobody-new", address="192.0.2.90").status_code, 429)
        # Unknown names get no account bucket: one pair row each plus the peer.
        self.assertEqual(LoginThrottle.objects.count(), 31)

    def test_malformed_sign_in_is_a_bad_request_not_a_failed_guess(self):
        client = self.browser()
        for data in ({"username": "owner"}, {"username": "owner", "password": 5}, {"username": ["owner"], "password": "x"}):
            response = self.post(client, "/api/v1/session", data)
            self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_login"))
        self.assertFalse(LoginThrottle.objects.exists())


class Auditing(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.adapter = adapter()
        install_adapter(self.adapter)

    def test_records_name_principal_kind_and_a_keyed_peer_label(self):
        self.assertEqual(self.login(address="192.0.2.44").status_code, 200)
        self.assertEqual(self.login(secret=Throttling.WRONG, address="192.0.2.44").status_code, 401)
        _, raw = issue_token(self.owner, "audit")
        Client().post("/api/v1/logout", secure=True, HTTP_AUTHORIZATION="Bearer " + raw, REMOTE_ADDR="192.0.2.45")
        rows = list(Audit.objects.filter(action__in=("login", "logout")).order_by("id").values("action", "outcome", "principal", "peer"))
        self.assertEqual([(row["action"], row["outcome"], row["principal"]) for row in rows],
                         [("login", "ok", "password"), ("login", "denied", "anonymous"), ("logout", "ok", "token")])
        self.assertEqual(rows[0]["peer"], rows[1]["peer"])
        self.assertNotEqual(rows[0]["peer"], rows[2]["peer"])
        self.assertTrue(all(len(row["peer"]) == 32 and "192.0.2" not in row["peer"] for row in rows))

    def test_denied_sign_ins_cannot_evict_other_security_events(self):
        Audit.objects.bulk_create([Audit(action="login", outcome="denied") for _ in range(DENIED_CAP + 5)])
        Audit.objects.bulk_create([Audit(action="user.update", outcome="ok") for _ in range(10)])
        self.login(secret=Throttling.WRONG, address="192.0.2.50")
        self.assertEqual(Audit.objects.filter(outcome="denied").count(), DENIED_CAP)
        self.assertEqual(Audit.objects.filter(action="user.update").count(), 10)
        self.assertTrue(Audit.objects.filter(action="bootstrap").exists())
        self.assertLess(DENIED_CAP, AUDIT_CAP)


class CommandAudit(Requests, TransactionTestCase):
    def setUp(self):
        bootstrap("owner", PASSPHRASE)
        install_adapter(adapter())

    def test_room_commands_are_not_security_events(self):
        client = self.browser()
        before = Audit.objects.count()
        for _ in range(3):
            self.assertEqual(self.post(client, "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}).status_code, 202)
        self.assertEqual(self.post(client, "/api/v1/all-off", {}).status_code, 202)
        self.assertEqual(self.post(client, "/api/v1/logout", {}).status_code, 200)
        self.assertEqual(Audit.objects.count(), before)


class Logging(TestCase):
    def test_server_errors_reach_the_journal_without_request_or_exception_text(self):
        config = settings.LOGGING if "test_settings" not in settings.SETTINGS_MODULE else sys.modules["oh_identity.settings"].LOGGING
        self.assertEqual(config["loggers"]["django.request"]["handlers"], ["journal"])
        self.assertEqual(config["loggers"]["django.request"]["level"], "ERROR")
        for name in ("oh_identity", "oh_access", "oh_room_policy", "uvicorn.error", "asyncio"):
            self.assertEqual(config["loggers"][name]["handlers"], ["journal"])
        self.assertEqual(config["handlers"]["journal"]["stream"], "ext://sys.stderr")
        try:
            raise ValueError("Bearer private-sentinel from a request")
        except ValueError:
            record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Internal Server Error: %s",
                                       ("/api/v1/me",), sys.exc_info())
        record.request = object()
        PrivateRecord().filter(record)
        self.assertIsNone(record.request)
        text = PrivateFormatter("%(levelname)s %(name)s: %(message)s").format(record)
        self.assertIn("Internal Server Error: /api/v1/me", text)
        self.assertIn("builtins.ValueError", text)
        self.assertIn("test_hardening.py", text)
        self.assertNotIn("sentinel", text)


class Streams(TransactionTestCase):
    def setUp(self):
        SecurityState = __import__("oh_identity.models", fromlist=["SecurityState"]).SecurityState
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        states = {f"item_{index:04d}": {"kind": "enum", "values": ["ON"], "commands": True} for index in range(400)}
        self.contract = RuntimeContract({"den": {"source_item": "loc_den_source", "sources": ["player"]}}, states=states)

    def test_topic_queries_are_deduplicated_capped_and_exact_topics_need_no_scan(self):
        pattern = "openhab/items/item_000*/*"
        self.assertEqual(len(streams.topics_for(self.contract, ",".join([pattern] * 300))), 30)
        distinct = ",".join(f"openhab/items/item_{index:04d}/stateupdated" for index in range(65))
        with self.assertRaises(streams.Problem):
            streams.topics_for(self.contract, distinct)
        with self.assertRaises(streams.Problem):
            streams.topics_for(self.contract, "openhab/items/item_*/*")  # 1200 topics
        with patch("oh_identity.streams.fnmatch.fnmatchcase", side_effect=AssertionError("scanned")):
            self.assertEqual(streams.topics_for(self.contract, "openhab/items/item_0001/command"), ["openhab/items/item_0001/command"])

    def test_streams_are_admitted_per_peer_and_in_total(self):
        install_adapter(adapter())
        path = "/rest/events?topics=openhab/items/loc_den_source/stateupdated"
        with patch("oh_identity.streams.slots", streams.Slots(per_peer=2, total=3)) as slots:
            first = [Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True) for _ in range(2)]
            self.assertEqual([response.status_code for response in first], [200, 200])
            refused = Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True)
            self.assertEqual((refused.status_code, refused.json()["error"]), (429, "too_many_streams"))
            self.assertIn("Retry-After", refused)
            other = Client(REMOTE_ADDR="2001:db8::1").get(path, secure=True)
            self.assertEqual(other.status_code, 200)
            self.assertEqual(Client(REMOTE_ADDR="2001:db8:1::1").get(path, secure=True).status_code, 429)
            for response in (*first, other):
                response.close()
            self.assertEqual(slots.count(), 0)
            release = slots.acquire("peer")
            release()
            release()
            self.assertEqual(slots.count(), 0)


class HttpSemantics(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.adapter = adapter()
        self.adapter.policy = oh_room_policy
        install_adapter(self.adapter)
        _, self.token = issue_token(self.owner, "http")

    def bearer(self, method, path, data=None, scheme="Bearer"):
        return getattr(Client(), method)(path, json.dumps(data) if data is not None else None, content_type="application/json",
                                         secure=True, HTTP_AUTHORIZATION=scheme + " " + self.token)

    def test_method_errors_name_the_allowed_methods(self):
        client = Client()
        for method, path, allow in (("delete", "/api/v1/me", "GET"), ("put", "/api/v1/session", "GET, POST"),
                                    ("get", "/api/v1/logout", "POST"), ("get", "/api/v1/token-login", "POST"),
                                    ("delete", "/api/v1/security", "GET, POST"), ("post", "/rest/events", "GET"),
                                    ("delete", "/api/v1/tokens", "GET, POST")):
            response = getattr(client, method)(path, secure=True)
            self.assertEqual((response.status_code, response["Allow"], response.json()["error"]), (405, allow, "method_not_allowed"))

    def test_bearer_scheme_is_case_insensitive_and_invalid_logout_is_idempotent(self):
        for scheme in ("Bearer", "bearer", "BEARER"):
            self.assertEqual(self.bearer("get", "/api/v1/me", scheme=scheme).json()["username"], "owner")
        self.assertEqual(self.bearer("get", "/api/v1/me", scheme="Basic").status_code, 401)
        unknown = "thm_" + secrets.token_urlsafe(32)
        for _ in range(2):
            response = Client().post("/api/v1/logout", secure=True, HTTP_AUTHORIZATION="Bearer " + unknown)
            self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(Client().get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + unknown).status_code, 401)

    def test_invalid_policy_names_a_fixed_reason_and_field(self):
        overlap = {"version": 1, "enabled": True, "daily_off": [], "locks": [
            {"id": "a", "start": "18:00", "end": "20:00", "days": [0], "desired": "player"},
            {"id": "b", "start": "19:00", "end": "21:00", "days": [0], "desired": "player"}]}
        response = self.bearer("put", "/api/v1/rooms/den/policy", {"revision": 0, "config": overlap})
        self.assertEqual(response.json(), {"error": "invalid_policy", "reason": "overlapping_windows", "field": "locks.a.b.day0"})
        bad_time = {**overlap, "locks": [], "daily_off": [{"id": "night", "time": "25:00", "days": [0]}]}
        response = self.bearer("put", "/api/v1/rooms/den/policy", {"revision": 0, "config": bad_time})
        self.assertEqual(response.json(), {"error": "invalid_policy", "reason": "invalid_local_time", "field": "daily_off.0.time"})
        self.assertEqual(oh_room_policy.public_error(ValueError("private text")), {})
        self.assertEqual(oh_room_policy.public_error(oh_room_policy.PolicyError("invalid_rule_id", "locks.0 <script>")),
                         {"reason": "invalid_rule_id"})

    def test_room_routes_accept_only_inventory_room_ids(self):
        for room in ("Den", "1den", "den-room", "d" * 101):
            self.assertEqual(self.bearer("get", f"/api/v1/rooms/{room}/policy").status_code, 404)
        with self.assertRaises(ValueError):
            RuntimeContract({"Den": {"source_item": "loc_den_source", "sources": []}})

    def test_every_response_carries_the_content_security_policy(self):
        self.assertIn("frame-ancestors 'none'", CONTENT_SECURITY_POLICY)
        self.assertNotIn("unsafe-inline", CONTENT_SECURITY_POLICY)
        for response in (Client().get("/api/v1/me", secure=True), Client().get("/api/v1/me"), Client().delete("/api/v1/me", secure=True)):
            self.assertEqual(response["Content-Security-Policy"], CONTENT_SECURITY_POLICY)
        self.assertNotIn("Strict-Transport-Security", Client().get("/api/v1/me", secure=True))

    def test_the_prologue_reads_the_security_switch_once(self):
        for headers in ({}, {"HTTP_AUTHORIZATION": "Bearer " + self.token}):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(Client().get("/api/v1/me", secure=True, **headers).status_code, 200)
            self.assertEqual(sum("oh_identity_securitystate" in query["sql"] for query in queries.captured_queries), 1)

    def test_database_uses_wal_and_requests_are_not_wrapped_in_transactions(self):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            self.assertEqual(cursor.fetchone()[0], "wal")
        self.assertFalse(settings.DATABASES["default"].get("ATOMIC_REQUESTS", False))

    def test_token_list_omits_revoked_and_long_expired_tokens(self):
        live, _ = issue_token(self.owner, "live")
        revoked, _ = issue_token(self.owner, "revoked")
        PersonalToken.objects.filter(pk=revoked.pk).update(revoked=True)
        old, _ = issue_token(self.owner, "old")
        PersonalToken.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(days=8))
        names = {token["name"] for token in self.bearer("get", "/api/v1/tokens").json()["tokens"]}
        self.assertEqual(names, {"http", "live"})


class RetiredTargets(TestCase):
    """A stored hold or rule naming a source the house no longer offers."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.adapter = adapter()
        self.adapter.policy = oh_room_policy
        install_adapter(self.adapter)
        _, self.token = issue_token(self.owner, "retired")
        RoomPolicy.objects.create(room_id="den", revision=3, config={
            "version": 1, "room": "den", "timezone": settings.TIME_ZONE, "revision": 3, "enabled": True, "daily_off": [],
            "locks": [{"id": "eve", "start": "18:00", "end": "19:00", "days": [0], "desired": "retired", "unlock": "leave"}],
            "manual_lock": {"desired": "retired", "started_at": "2026-01-01T00:00:00+00:00"}})

    def call(self, method, path, data=None):
        return getattr(Client(), method)(path, json.dumps(data) if data is not None else None, content_type="application/json",
                                         secure=True, HTTP_AUTHORIZATION="Bearer " + self.token)

    def command(self):
        return self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"})

    def test_unlock_always_succeeds_and_disables_an_unreadable_schedule(self):
        self.assertEqual(self.command().status_code, 503, "an unreadable policy fails closed")
        response = self.call("put", "/api/v1/rooms/den/lock", {"revision": 3, "locked": False})
        self.assertEqual(response.status_code, 200)
        dto = response.json()
        self.assertEqual((dto["revision"], dto["manual_lock"], dto["effective"]["locked"]), (4, None, False))
        self.assertFalse(dto["config"]["enabled"])
        self.assertEqual(dto["config"]["locks"][0]["desired"], "retired", "kept for review")
        self.assertEqual(Audit.objects.filter(action="room.unlock").get().outcome, "schedule_disabled")
        self.assertEqual(self.command().status_code, 202)
        self.assertEqual(oh_room_policy.active(dto["config"]), False)

    def test_a_schedule_edit_releases_an_unreadable_hold_but_keeps_a_valid_one(self):
        schedule = {"version": 1, "enabled": True, "daily_off": [], "locks": []}
        response = self.call("put", "/api/v1/rooms/den/policy", {"revision": 3, "config": schedule})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("manual_lock", response.json()["config"])
        self.assertEqual(Audit.objects.filter(action="policy.update").get().outcome, "hold_released")
        self.assertEqual(self.command().status_code, 202)
        self.assertEqual(self.call("put", "/api/v1/rooms/den/lock", {"revision": 4, "locked": True}).status_code, 200)
        bad = {**schedule, "locks": [{"id": "eve", "start": "18:00", "end": "19:00", "days": [0], "desired": "retired"}]}
        response = self.call("put", "/api/v1/rooms/den/policy", {"revision": 5, "config": bad})
        self.assertEqual((response.status_code, response.json()["reason"]), (400, "invalid_desired_source"))
        self.assertEqual(RoomPolicy.objects.get().config["manual_lock"]["desired"], "player")


class Housekeeping(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def test_policies_for_removed_rooms_are_pruned_and_new_rows_never_reuse_revisions(self):
        RoomPolicy.objects.create(room_id="attic", revision=6, config={"room": "attic", "revision": 6})
        RoomPolicy.objects.create(room_id="den", revision=2, config={"room": "den", "revision": 2})
        ReconcileState.objects.create(room_id="attic", revision=6, state={})
        BoundaryReceipt.objects.create(key="attic:6:eve:2026-11-02:start", room_id="attic", revision=6)
        self.assertEqual(prune_unknown_rooms(("den", "desk")), ["attic"])
        self.assertEqual(list(RoomPolicy.objects.values_list("room_id", flat=True)), ["den"])
        self.assertFalse(ReconcileState.objects.filter(room_id="attic").exists())
        self.assertEqual(Audit.objects.filter(action="policy.prune").get().target, "attic")
        self.assertEqual(next_revision("attic", 0), 7)
        self.assertEqual(next_revision("desk", 0), 1)
        self.assertEqual(next_revision("den", 2), 3)

    def test_daily_pruning_removes_only_records_that_can_no_longer_matter(self):
        now = timezone.now()
        old = BoundaryReceipt.objects.create(key="old", room_id="den", revision=1)
        BoundaryReceipt.objects.filter(pk=old.pk).update(updated_at=now - timedelta(days=4))
        BoundaryReceipt.objects.create(key="recent", room_id="den", revision=1)
        Session.objects.create(session_key="expired", session_data="", expire_date=now - timedelta(seconds=1))
        Session.objects.create(session_key="current", session_data="", expire_date=now + timedelta(hours=1))
        for name, revoked, expiry in (("live", False, now + timedelta(days=1)), ("revoked", True, now + timedelta(days=1)),
                                      ("lapsed", False, now - timedelta(days=1)), ("ancient", False, now - timedelta(days=8))):
            token, _ = issue_token(self.owner, name)
            PersonalToken.objects.filter(pk=token.pk).update(revoked=revoked, expires_at=expiry)
        LoginThrottle.objects.create(key="old", attempts=3, until=now - timedelta(seconds=1))
        counts = prune_history(now)
        self.assertEqual(counts, {"receipts": 1, "sessions": 1, "tokens": 2, "throttles": 1})
        self.assertEqual(list(BoundaryReceipt.objects.values_list("key", flat=True)), ["recent"])
        self.assertEqual(set(PersonalToken.objects.values_list("name", flat=True)), {"live", "lapsed"})

    def test_timezone_rebase_runs_under_every_room_lock(self):
        from oh_access.service import rebase_timezone
        held = []

        @contextmanager
        def locks(rooms, resources):
            held.append(rooms)
            yield
        runtime = adapter()
        runtime.command_lock = locks
        RoomPolicy.objects.create(room_id="den", revision=2, config={"room": "den", "timezone": "UTC", "revision": 2})
        self.assertEqual(rebase_timezone(runtime, "America/Chicago"), 1)
        self.assertEqual(held, [("den", "desk")])
        row = RoomPolicy.objects.get()
        self.assertEqual((row.revision, row.config["timezone"], row.config["revision"]), (3, "America/Chicago", 3))
        self.assertEqual(rebase_timezone(runtime, "America/Chicago"), 0)


class FirstActivation(TestCase):
    def test_a_wrong_setup_code_is_refused_before_any_room_lock(self):
        held = []

        @contextmanager
        def locks(rooms, resources):
            held.append(rooms)
            yield
        runtime = adapter()
        runtime.command_lock = locks
        install_adapter(runtime)
        __import__("oh_identity.models", fromlist=["SecurityState"]).SecurityState.objects.update_or_create(
            pk=1, defaults={"enabled": False, "revision": 1})
        code = arm_setup()
        client = Client(enforce_csrf_checks=True)
        csrf = client.get("/api/v1/security", secure=True).json()["csrf"]
        def enable(setup_code):
            return client.post("/api/v1/security", json.dumps({"enabled": True, "revision": 1, "username": "owner",
                               "password": PASSPHRASE, "setup_code": setup_code}), content_type="application/json",
                               secure=True, HTTP_ORIGIN="https://testserver", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(enable("wrong").status_code, 403)
        self.assertEqual(held, [])
        self.assertEqual(enable(code).status_code, 200)
        self.assertEqual(held, [("den", "desk")])
        self.assertTrue(User.objects.get().admin)


class PolicyPredicates(TestCase):
    def test_one_active_predicate_and_a_staleness_bound_above_the_heartbeat(self):
        from oh_room_policy.worker import RECONCILE_HEARTBEAT
        self.assertGreater(oh_room_policy.STALE_AFTER, RECONCILE_HEARTBEAT)
        for config, expected in (({"enabled": False}, False), ({}, True), ({"enabled": True}, True),
                                 ({"enabled": False, "manual_lock": {"desired": "off"}}, True), (["enabled"], False)):
            self.assertEqual(oh_room_policy.active(config), expected)
        empty = oh_room_policy.empty("den", "UTC", 1)
        self.assertFalse(oh_room_policy.active(empty))
        held = oh_room_policy.with_manual_hold(empty, {"desired": "off"})
        self.assertEqual(oh_room_policy.manual_hold(held), {"desired": "off"})
        self.assertNotIn("manual_lock", oh_room_policy.with_manual_hold(held, None))
