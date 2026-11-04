"""The optional account boundary remains authoritative at every effect seam."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.sessions.models import Session
from django.db import close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from oh_identity.auth import Guest, revalidate
from oh_identity.models import Audit, BoundaryReceipt, PersonalToken, ReconcileState, Role, RoomPolicy, SecurityState, User
from oh_identity.policy_store import Store, load_policies
from oh_identity.runtime import install_adapter
from oh_identity.security import arm_setup, get_state, transition
from oh_identity.services import Problem, bootstrap, issue_token, password
from oh_identity.streams import stream, topics_for, valid
from oh_identity.tests.test_boundary import StreamTransport
from oh_identity.tests.test_core import PASSPHRASE, adapter


class SecurityClient:
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": False, "revision": 1,
                                                             "setup_digest": "", "setup_expires_at": None})
        self.adapter = adapter()
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/api/v1/security", secure=True)

    def call(self, method, path, data=None, **extra):
        return getattr(self.client, method)(path, data=json.dumps(data) if data is not None else None,
            content_type="application/json", secure=True, HTTP_ORIGIN="https://testserver",
            HTTP_X_CSRFTOKEN=self.client.cookies[settings.CSRF_COOKIE_NAME].value, **extra)

    def toggle(self, enabled, **extra):
        return self.call("post", "/api/v1/security", {"enabled": enabled, "revision": get_state().revision,
                         "username": "owner", "password": PASSPHRASE, **extra})


class OptionalAuthentication(SecurityClient, TestCase):
    def test_empty_site_is_bounded_guest_and_never_creates_an_account(self):
        state = self.call("get", "/api/v1/security").json()
        self.assertEqual({key: state[key] for key in ("enabled", "revision", "bootstrap_required", "setup_available")},
                         {"enabled": False, "revision": 1, "bootstrap_required": True, "setup_available": False})
        me = self.call("get", "/api/v1/me").json()
        for key, value in {"id": 0, "role": 0, "username": "Guest", "rooms": None, "can_edit": False,
                           "auth_enabled": False, "permissions": ["room.control", "site.read"]}.items():
            self.assertEqual(me[key], value)
        self.assertEqual(self.call("get", "/rest/items/loc_den_source/state").content, b"player")
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}).status_code, 202)
        self.assertEqual(self.call("get", "/rest/items/unrelated_secret/state").status_code, 404)
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "volume", "value": 99}).status_code, 400)
        self.assertFalse(User.objects.exists())
        self.assertFalse(Role.objects.exists())

    def test_invalid_credentials_never_fall_back_to_guest(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer invalid").status_code, 401)
        client.cookies[settings.SESSION_COOKIE_NAME] = "expired-session"
        self.assertEqual(client.get("/api/v1/me", secure=True).status_code, 401)
        client.get("/api/v1/session", secure=True)
        self.assertEqual(client.post("/api/v1/logout", secure=True).status_code, 403)
        self.assertEqual(client.post("/api/v1/logout", secure=True, HTTP_ORIGIN="https://testserver",
                         HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value).status_code, 200)
        self.assertEqual(client.get("/api/v1/me", secure=True).json()["username"], "Guest")

    def test_terminal_guest_commands_require_csrf_cookie_token_and_trusted_origin_or_referer(self):
        client = Client(enforce_csrf_checks=True)
        status = client.get("/api/v1/security", secure=True)
        csrf = status.json()["csrf"]
        data = json.dumps({"action": "source", "value": "off"})
        options = {"data": data, "content_type": "application/json", "secure": True,
                   "HTTP_X_CSRFTOKEN": csrf, "HTTP_ORIGIN": "https://testserver"}
        self.assertEqual(client.post("/api/v1/rooms/den/commands", **options).status_code, 202)
        self.assertEqual(Client(enforce_csrf_checks=True).post("/api/v1/rooms/den/commands", **options).status_code, 403)
        del options["HTTP_ORIGIN"]
        self.assertEqual(client.post("/api/v1/rooms/den/commands", **options).status_code, 403)
        options["HTTP_ORIGIN"] = "https://foreign.invalid"
        self.assertEqual(client.post("/api/v1/rooms/den/commands", **options).status_code, 403)
        del options["HTTP_ORIGIN"]
        options["HTTP_REFERER"] = "https://testserver/"
        self.assertEqual(client.post("/api/v1/rooms/den/commands", **options).status_code, 202)
        self.assertEqual(Client(enforce_csrf_checks=True).post("/api/v1/rooms/den/commands", **options).status_code, 403)
        options["HTTP_REFERER"] = "https://foreign.invalid/"
        self.assertEqual(client.post("/api/v1/rooms/den/commands", **options).status_code, 403)
        self.assertEqual(self.adapter.transport.commands, [("loc_den_source", "off"), ("loc_den_source", "off")])

    def test_initial_enable_requires_owner_code_and_csrf_without_secret_echo(self):
        self.assertEqual(self.toggle(True).status_code, 403)
        code = arm_setup()
        state = get_state()
        self.assertNotEqual(state.setup_digest, code)
        self.assertEqual(len(state.setup_digest), 64)
        self.assertTrue(self.call("get", "/api/v1/security").json()["setup_available"])
        self.assertEqual(self.toggle(True, setup_code="wrong").status_code, 403)
        denied = Client(enforce_csrf_checks=True).post("/api/v1/security", data=json.dumps({"enabled": True,
            "revision": state.revision, "username": "owner", "password": PASSPHRASE, "setup_code": code}),
            content_type="application/json", secure=True)
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(User.objects.exists())
        response = self.toggle(True, setup_code=code)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        self.assertFalse(response.json()["bootstrap_required"])
        self.assertFalse(response.json()["setup_available"])
        self.assertEqual(User.objects.get().role.name, "Admin")
        self.assertEqual(self.call("get", "/api/v1/me").status_code, 401)
        self.assertFalse(Session.objects.exists())
        self.assertEqual(get_state().setup_digest, "")
        self.assertNotIn(code, response.content.decode())
        self.assertNotIn(code, str(list(Audit.objects.values())))
        self.assertNotIn(PASSPHRASE, str(list(Audit.objects.values())))
        self.assertEqual(self.toggle(True, revision=state.revision, setup_code=code).status_code, 409)

    def test_setup_rearming_expiry_and_invalid_bootstrap_roll_back(self):
        old = arm_setup()
        code = arm_setup()
        self.assertEqual(self.toggle(True, setup_code=old).status_code, 403)
        self.assertEqual(self.toggle(True, setup_code=code, password="short").status_code, 400)  # lint: allow — synthetic invalid password fixture
        self.assertFalse(User.objects.exists())
        self.assertFalse(Role.objects.exists())
        self.assertEqual(get_state().revision, 1)
        self.assertTrue(get_state().setup_digest)
        SecurityState.objects.update(setup_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(self.call("get", "/api/v1/security").json()["setup_available"])
        self.assertEqual(self.toggle(True, setup_code=code).status_code, 403)
        self.assertEqual(self.toggle(True, setup_code=arm_setup()).status_code, 200)
        with self.assertRaises(Problem):
            arm_setup()

    def test_toggle_requires_active_admin_password_and_current_revision(self):
        owner = bootstrap("owner", PASSPHRASE)
        operator = User.objects.create_user("operator", password=PASSPHRASE, role=Role.objects.get(name="User"))
        self.assertEqual(self.toggle(False, password="incorrect").status_code, 401)  # lint: allow — synthetic invalid password fixture
        self.assertEqual(self.toggle(False, username=operator.username).status_code, 403)
        self.assertEqual(self.toggle(False, revision=get_state().revision - 1).status_code, 409)
        self.assertTrue(get_state().enabled)
        self.assertEqual(self.toggle(False).status_code, 200)
        owner.is_active = False
        owner.save(update_fields=["is_active"])
        self.assertEqual(self.toggle(True).status_code, 401)
        self.assertFalse(get_state().enabled)

    def test_disable_retains_data_revokes_credentials_and_reenables_existing_owner(self):
        owner = bootstrap("owner", PASSPHRASE)
        row = RoomPolicy.objects.create(room_id="den", config={"locked": True, "revision": 4}, revision=4)
        ReconcileState.objects.create(room_id="den", revision=4, state={"state": "verified"})
        token, raw = issue_token(owner, "test")
        self.call("get", "/api/v1/session")
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE}).status_code, 200)
        old_session = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        before = owner.auth_revision
        self.assertEqual(self.toggle(False).status_code, 200)
        owner.refresh_from_db()
        token.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(owner.auth_revision, before + 1)
        self.assertTrue(token.revoked)
        self.assertFalse(Session.objects.exists())
        self.assertFalse(ReconcileState.objects.exists())
        self.assertEqual(row.config, {"locked": True, "revision": 5})
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Role.objects.count(), 3)
        self.assertEqual(Client().get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 401)
        stale = Client()
        stale.cookies[settings.SESSION_COOKIE_NAME] = old_session
        self.assertEqual(stale.get("/api/v1/me", secure=True).status_code, 401)
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}).status_code, 202)
        self.assertEqual(self.toggle(True).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.config, {"locked": True, "revision": 6})
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(Session.objects.exists())

    def test_sensitive_features_and_token_login_are_closed_while_off(self):
        for path in ("/api/v1/users", "/api/v1/roles", "/api/v1/tokens", "/api/v1/rooms/den/policy", "/rest/items?metadata=pairing"):
            with self.subTest(path=path):
                self.assertEqual(self.call("get", path).json()["error"], "authentication_disabled")
        for path, data in (("/api/v1/tokens", {}), ("/api/v1/users", {}), ("/api/v1/roles", {}),
                           ("/api/v1/session", {"username": "owner", "password": PASSPHRASE})):
            self.assertEqual(self.call("post", path, data).status_code, 403)
        self.assertEqual(self.call("put", "/api/v1/rooms/den/policy", {}).status_code, 403)
        token = Client().post("/api/v1/token-login", data=json.dumps({"username": "owner", "password": PASSPHRASE}),
                              content_type="application/json", secure=True)
        self.assertEqual(token.status_code, 403)
        self.assertEqual(self.client.post("/rest/items/tv_pairing", data="REQUEST", content_type="text/plain", secure=True,
            HTTP_ORIGIN="https://testserver", HTTP_X_CSRFTOKEN=self.client.cookies[settings.CSRF_COOKIE_NAME].value).status_code, 403)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_missing_singleton_fails_closed_and_dynamic_config_never_selects_native(self):
        SecurityState.objects.all().delete()
        self.assertEqual(self.call("get", "/api/v1/security").status_code, 503)
        self.assertEqual(self.call("get", "/api/v1/me").status_code, 503)
        response = self.call("get", "/static/remote/access-config.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"mode":"authenticated"', response.content)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_worker_policies_and_receipts_are_dormant_and_old_revisions_are_fenced(self):
        bootstrap("owner", PASSPHRASE)
        row = RoomPolicy.objects.create(room_id="den", revision=4, config={"revision": 4, "locked": True})
        store = Store()
        self.assertTrue(store.claim_boundary("den", 4, "before", timezone.now()))
        self.assertEqual(self.toggle(False).status_code, 200)
        self.assertEqual(store.policies(), [])
        self.assertEqual(load_policies(), [])
        self.assertIsNone(store.current_revision("den"))
        self.assertEqual(store.current_revisions(("den", "desk")), {"den": None, "desk": None})
        self.assertFalse(store.claim_boundary("den", 4, "late", timezone.now()))
        self.assertFalse(store.save_reconcile("den", 4, {"state": "verified"}))
        self.assertEqual(self.toggle(True).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(store.current_revision("den"), 6)
        self.assertEqual(store.policies()[0].changed_at, row.updated_at)
        self.assertFalse(store.claim_boundary("den", 4, "late", timezone.now()))
        self.assertFalse(store.save_reconcile("den", 4, {"state": "verified"}))
        self.assertTrue(BoundaryReceipt.objects.filter(key="before").exists())

    def test_queued_guest_and_stream_snapshot_cannot_cross_security_transition(self):
        guest = Guest(get_state().revision)
        snapshot = {"guest": True, "security_revision": get_state().revision}
        self.assertTrue(valid(snapshot))
        @contextmanager
        def queued(rooms, resources):
            bootstrap("owner", PASSPHRASE)
            yield
        self.adapter.command_lock = queued
        with self.assertRaises(Problem) as caught:
            self.adapter.dispatch(guest, "den", "source", "off")
        self.assertEqual(caught.exception.code, "authorization_changed")
        self.assertEqual(self.adapter.transport.commands, [])
        self.assertFalse(valid(snapshot))
        self.adapter.command_lock = adapter().command_lock
        self.assertEqual(self.toggle(False).status_code, 200)
        self.assertFalse(valid(snapshot), "off/on/off must never revive an old guest stream")

    def test_password_creation_and_login_share_the_1024_character_limit(self):
        value = PASSPHRASE + "x" * (1024 - len(PASSPHRASE))
        owner = bootstrap("owner", value)
        self.assertTrue(owner.check_password(value))
        with self.assertRaises(Problem) as caught:
            password(owner, value + "x")
        self.assertEqual(caught.exception.code, "invalid_password")
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": value}).status_code, 200)
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": value + "x"}).status_code, 401)

    def test_queued_admin_transition_rechecks_original_session_after_lock_wait(self):
        bootstrap("owner", PASSPHRASE)
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE}).status_code, 200)
        @contextmanager
        def logout_during_wait(rooms, resources):
            Session.objects.all().delete()
            yield
        self.adapter.command_lock = logout_during_wait
        response = self.toggle(False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "credential_expired")
        self.assertTrue(get_state().enabled)

    def test_queued_admin_pairing_cannot_run_after_authentication_is_disabled(self):
        bootstrap("owner", PASSPHRASE)
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE}).status_code, 200)
        @contextmanager
        def disable_during_wait(rooms, resources):
            SecurityState.objects.update(enabled=False, revision=get_state().revision + 1)
            yield
        self.adapter.command_lock = disable_during_wait
        response = self.client.post("/rest/items/tv_pairing", data="REQUEST", content_type="text/plain", secure=True,
            HTTP_ORIGIN="https://testserver", HTTP_X_CSRFTOKEN=self.client.cookies[settings.CSRF_COOKIE_NAME].value)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "authentication_disabled")
        self.assertEqual(self.adapter.transport.commands, [])

    def test_login_cannot_mint_a_session_if_disable_precedes_user_authentication(self):
        owner = bootstrap("owner", PASSPHRASE)
        def disable_then_authenticate(request, **credentials):
            with transaction.atomic():
                transition(get_state(), False, owner)
            return authenticate(request, **credentials)
        with patch("oh_identity.auth.authenticate", side_effect=disable_then_authenticate):
            response = self.call("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "authentication_disabled")
        self.assertFalse(Session.objects.exists())
        self.assertFalse(get_state().enabled)

    def test_login_cannot_cross_disable_then_reenable_even_with_current_user_revision(self):
        owner = bootstrap("owner", PASSPHRASE)
        def toggle_then_authenticate(request, **credentials):
            with transaction.atomic():
                transition(get_state(), False, owner)
                transition(get_state(), True, owner)
            return authenticate(request, **credentials)
        with patch("oh_identity.auth.authenticate", side_effect=toggle_then_authenticate):
            response = self.call("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "authorization_changed")
        self.assertFalse(Session.objects.exists())
        self.assertTrue(get_state().enabled)

    def test_authentication_off_rejects_even_current_valid_session_token_and_stream(self):
        owner = bootstrap("owner", PASSPHRASE)
        token, raw = issue_token(owner, "race fixture")
        self.assertEqual(self.toggle(False).status_code, 200)
        owner.refresh_from_db()
        # Model a credential minted after a disable, as the old login race could
        # do. It must never recover authenticated grants while the switch is off.
        client = Client()
        client.force_login(owner)
        session = client.session
        session["identity_revision"] = owner.auth_revision
        session.save()
        self.assertEqual(client.get("/api/v1/me", secure=True).status_code, 403)
        token.revoked = False
        token.auth_revision = owner.auth_revision
        token.save(update_fields=["revoked", "auth_revision"])
        self.assertEqual(Client().get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 403)
        snapshot = {"user": owner.pk, "revision": owner.auth_revision, "token": str(token.pk),
                    "security_revision": get_state().revision}
        self.assertFalse(valid(snapshot))
        owner._identity_context = {"token": token.pk, "security_revision": get_state().revision}
        with self.assertRaises(Problem) as caught:
            revalidate(owner)
        self.assertEqual(caught.exception.code, "authentication_disabled")


class SecurityConcurrency(SecurityClient, TransactionTestCase):
    def test_two_first_enables_create_exactly_one_administrator(self):
        code = arm_setup()
        gate = threading.Barrier(2)
        data = {"enabled": True, "revision": get_state().revision, "username": "owner",
                "password": PASSPHRASE, "setup_code": code}
        def enable():
            close_old_connections()
            gate.wait(timeout=3)
            try:
                return Client().post("/api/v1/security", data=json.dumps(data), content_type="application/json", secure=True).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: enable(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(get_state().enabled)
        self.assertFalse(get_state().setup_digest)

    def test_live_guest_stream_closes_after_bootstrap(self):
        topics = topics_for(self.adapter.contract, "openhab/items/loc_den_source/stateupdated")
        snapshot = {"guest": True, "security_revision": get_state().revision}
        async def check():
            transport = StreamTransport()
            self.adapter.transport = transport
            frames = stream(self.adapter, topics, snapshot)
            event = {"topic": topics[0], "type": "ItemStateUpdatedEvent",
                     "payload": json.dumps({"type": "String", "value": "player"})}
            await transport.queue.put(event)
            self.assertIn(b"player", await anext(frames))
            await sync_to_async(bootstrap)("owner", PASSPHRASE)
            await transport.queue.put(event)
            with self.assertRaises(StopAsyncIteration):
                await anext(frames)
            self.assertTrue(transport.closed)
        async_to_sync(check)()


class SecurityMigration(TransactionTestCase):
    def test_data_migration_preserves_existing_accounts_and_defaults_empty_sites_off(self):
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("oh_identity", "0001_initial")])
            historical = executor.loader.project_state([("oh_identity", "0001_initial")]).apps
            historical.get_model("oh_identity", "User").objects.create(username="existing-owner", password="!")
            executor = MigrationExecutor(connection)
            executor.migrate([("oh_identity", "0002_security_state")])
            self.assertTrue(get_state().enabled)
            self.assertEqual(get_state().revision, 1)
            executor = MigrationExecutor(connection)
            executor.migrate([("oh_identity", "0001_initial")])
            historical.get_model("oh_identity", "User").objects.all().delete()
            executor = MigrationExecutor(connection)
            executor.migrate([("oh_identity", "0002_security_state")])
            self.assertFalse(get_state().enabled)
        finally:
            MigrationExecutor(connection).migrate([("oh_identity", "0002_security_state")])
