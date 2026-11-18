"""Who may do what (oh_identity.security): the Guest, a Member and an Admin,
and three settings that do not move one another. There is no mode."""

import itertools
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from oh_identity.auth import PROBLEMS
from oh_identity.models import Audit, ReconcileState, RoomPolicy, SecurityState, User
from oh_identity.policy_store import Store
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.security import ADMIN, GUEST, MEMBER, arm_setup, get_state, set_guests
from oh_identity.services import Problem, bootstrap, password
from oh_identity.streams import stream, valid
from oh_identity.tests.test_event_hub import Upstream, event
from oh_identity.tests.test_core import PASSPHRASE, FakeRuntime, Policy

SNAPSHOT = "/unifi/media/image/camera"


class Cameras(FakeRuntime):
    """The fake runtime, with one camera."""

    def __init__(self):
        super().__init__()
        self.media_calls = []

    def media(self, resource, method, body, maximum):
        self.media_calls.append((resource, method))
        return "image/jpeg", b"\xff\xd8\xff\xd9"


def house():
    """Two rooms, a pairing and a camera."""
    contract = RuntimeContract(
        {"den": {"source_item": "loc_den_source", "sources": ["player"], "actions": {
            "volume": {"item": "amp_volume", "kind": "number", "min": 0, "max": 98, "step": .5}}},
         "desk": {"source_item": "loc_desk_source", "sources": ["player"]}},
        states={"loc_den_source": {"kind": "enum", "values": ["player", "off"]},
                "amp_volume": {"kind": "number", "min": 0, "max": 98},
                "tv_pairing": {"kind": "enum"}, "unrelated_secret": {"kind": "enum"}},
        pairings={"tv_pairing": {"value": "sony_ip_control", "config": {"length": 4, "chars": "digits"}}},
        media={SNAPSHOT: {"kind": "snapshot"}})
    return RuntimeAdapter(contract, Cameras(), Policy())


class Site:
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"setup_digest": "", "setup_expires_at": None,
                                                             "guest_text": True, "guest_cameras": False})
        self.adapter = house()
        install_adapter(self.adapter)
        self.client = self.browser()

    def browser(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/api/v1/security", secure=True)
        return client

    def call(self, method, path, data=None, client=None, **extra):
        client = client or self.client
        return getattr(client, method)(path, data=json.dumps(data) if data is not None else None,
            content_type="application/json", secure=True, HTTP_ORIGIN="https://testserver",
            HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value, **extra)

    def first_admin(self, **extra):
        return self.call("post", "/api/v1/security", {"username": "owner", "password": PASSPHRASE, **extra})

    def signed_in(self, name):
        client = self.browser()
        answer = self.call("post", "/api/v1/session", {"username": name, "password": PASSPHRASE}, client)
        self.assertEqual(answer.status_code, 200)
        return client

    def principals(self):
        """One browser for each kind of principal; the accounts are made here."""
        bootstrap("owner", PASSPHRASE)
        User.objects.create_user("member", password=PASSPHRASE)
        return {"guest": self.browser(), "member": self.signed_in("member"), "admin": self.signed_in("owner")}

    def pair(self, client):
        return client.post("/rest/items/tv_pairing", data="REQUEST", content_type="text/plain", secure=True,
                           HTTP_ORIGIN="https://testserver",
                           HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value)


class AccessModel(Site, TestCase):
    def test_a_site_with_no_account_serves_a_guest_and_never_creates_one(self):
        state = self.call("get", "/api/v1/security").json()
        self.assertTrue(state.pop("csrf"))
        self.assertEqual(state, {"bootstrap_required": True, "setup_available": False, "guest_text": True,
                                 "guest_cameras": False})
        me = self.call("get", "/api/v1/me").json()
        self.assertEqual({key: me[key] for key in ("id", "admin", "username", "can_edit", "is_guest", "permissions")},
                         {"id": 0, "admin": False, "username": "Guest", "can_edit": False, "is_guest": True,
                          "permissions": ["room.control", "site.read"]})
        self.assertEqual(self.call("get", "/rest/items/loc_den_source/state").content, b"player")
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}).status_code, 202)
        self.assertEqual(self.call("get", "/rest/items/unrelated_secret/state").status_code, 404)
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "volume", "value": 99}).status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_each_principal_is_given_its_part_and_no_setting_gives_a_guest_more_than_the_cameras(self):
        clients = self.principals()
        self.assertEqual((GUEST, MEMBER[len(GUEST):], ADMIN[len(MEMBER):]),
                         (("site.read", "room.control"), ("camera.view",),
                          ("settings.manage", "users.manage", "pairing.manage")))
        # What each is refused, as (cameras, accounts, tokens, pairing, guest settings).
        expected = {"guest": (401, 401, 401, 401, 401), "member": (200, 403, 200, 403, 403),
                    "admin": (200, 200, 200, 202, 200)}
        for cameras in (False, True):
            SecurityState.objects.filter(pk=1).update(guest_cameras=cameras)
            for name, client in clients.items():
                with self.subTest(principal=name, guest_cameras=cameras):
                    me = self.call("get", "/api/v1/me", client=client).json()
                    part = MEMBER if name == "member" or name == "guest" and cameras else ADMIN if name == "admin" else GUEST
                    self.assertEqual(me["permissions"], sorted(part))
                    self.assertEqual((me["is_guest"], me["admin"], me["can_edit"]),
                                     (name == "guest", name == "admin", name == "admin"))
                    answers = (self.call("get", SNAPSHOT, client=client).status_code,
                               self.call("get", "/api/v1/users", client=client).status_code,
                               self.call("get", "/api/v1/tokens", client=client).status_code,
                               self.pair(client).status_code,
                               self.call("put", "/api/v1/security/guests", {"guest_cameras": cameras}, client).status_code)
                    self.assertEqual(answers, (200, *expected[name][1:]) if cameras else expected[name])
                    # Everyone reads the site and commands a room that is not locked.
                    self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"},
                                               client).status_code, 202)
                    self.assertEqual(self.call("get", "/api/v1/locks", client=client).status_code, 200)
        refused = self.call("get", SNAPSHOT, client=self.browser())
        self.assertEqual(refused.status_code, 200)
        SecurityState.objects.filter(pk=1).update(guest_cameras=False)
        refused = self.call("get", SNAPSHOT, client=self.browser())
        self.assertEqual((refused.status_code, refused.json()), (401, {"error": "login_required"}))

    def test_camera_media_needs_an_account_in_every_configuration_unless_guests_see_cameras(self):
        # The defect this model replaced: cameras open to a client with no
        # account because of something other than the cameras' own setting.
        for accounts, text, locked in itertools.product((False, True), repeat=3):
            with self.subTest(accounts=accounts, guest_text=text, locked=locked):
                User.objects.all().delete()
                RoomPolicy.objects.all().delete()
                if accounts:
                    bootstrap("owner", PASSPHRASE)
                if locked:
                    RoomPolicy.objects.create(room_id="den", config={"locked": True})
                SecurityState.objects.filter(pk=1).update(guest_text=text, guest_cameras=False)
                self.adapter.transport.media_calls.clear()
                guest = self.browser()
                self.assertNotIn("camera.view", self.call("get", "/api/v1/me", client=guest).json()["permissions"])
                self.assertEqual(self.call("get", SNAPSHOT, client=guest).status_code, 401)
                self.assertEqual(self.adapter.transport.media_calls, [])
                SecurityState.objects.filter(pk=1).update(guest_cameras=True)
                self.assertEqual(self.call("get", SNAPSHOT, client=guest).status_code, 200)

    def test_no_setting_moves_another_and_accounts_move_none(self):
        def observed():
            guest = self.browser()
            return {"cameras": self.call("get", SNAPSHOT, client=guest).status_code == 200,
                    "text": valid({"guest": True, "guest_text": True}),
                    "den": self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}, guest).status_code,
                    "desk": self.call("post", "/api/v1/rooms/desk/commands", {"action": "source", "value": "off"}, guest).status_code}

        before = observed()
        self.assertEqual(before, {"cameras": False, "text": True, "den": 202, "desk": 202})
        # The first account, then a second: a Guest is given what it was.
        owner = bootstrap("owner", PASSPHRASE)
        User.objects.create_user("member", password=PASSPHRASE)
        self.assertEqual(observed(), before)
        set_guests(owner, {"guest_cameras": True})
        self.assertEqual(observed(), {**before, "cameras": True})
        set_guests(owner, {"guest_text": False})
        self.assertEqual(observed(), {**before, "cameras": True, "text": False})
        RoomPolicy.objects.create(room_id="den", config={"locked": True})
        self.assertEqual(observed(), {"cameras": True, "text": False, "den": 423, "desk": 202})
        set_guests(owner, {"guest_cameras": False, "guest_text": True})
        self.assertEqual(observed(), {**before, "den": 423})
        self.assertEqual(len(Store().policies()), 1)

    def test_a_lock_holds_whoever_asks_and_whether_or_not_an_account_exists(self):
        RoomPolicy.objects.create(room_id="den", config={"locked": True})
        command = {"action": "source", "value": "off"}
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", command).status_code, 423)
        self.assertEqual(self.call("get", "/api/v1/locks").json()["rooms"]["den"]["locked"], True)
        self.assertEqual(self.call("get", "/api/v1/rooms/den/policy").json()["effective"]["locked"], True)
        for name, client in self.principals().items():
            with self.subTest(principal=name):
                self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", command, client).status_code, 423)
                self.assertEqual(self.call("post", "/api/v1/rooms/desk/commands", command, client).status_code, 202)
        self.assertEqual(self.adapter.transport.commands, [("loc_desk_source", "off")] * 3)

    def test_account_features_ask_a_guest_to_sign_in_even_before_any_account_exists(self):
        for path in ("/api/v1/users", "/api/v1/tokens", "/rest/items?metadata=pairing"):
            with self.subTest(path=path):
                answer = self.call("get", path)
                self.assertEqual((answer.status_code, answer.json()["error"]), (401, "login_required"))
        for path, data in (("/api/v1/tokens", {}), ("/api/v1/users", {})):
            self.assertEqual(self.call("post", path, data).status_code, 401)
        self.assertEqual(self.call("put", "/api/v1/rooms/den/policy", {}).status_code, 401)
        self.assertEqual(self.call("put", "/api/v1/rooms/den/lock", {}).status_code, 401)
        self.assertEqual(self.pair(self.client).status_code, 401)
        # Nobody can sign in where nobody has an account.
        login = self.call("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE})
        self.assertEqual((login.status_code, login.json()["error"]), (401, "invalid_login"))
        token = Client().post("/api/v1/token-login", data=json.dumps({"username": "owner", "password": PASSPHRASE}),
                              content_type="application/json", secure=True)
        self.assertEqual(token.status_code, 401)
        self.assertEqual(self.adapter.transport.commands, [])
        self.assertNotIn("authentication_disabled", PROBLEMS)

    def test_only_an_admin_changes_what_guests_see_one_setting_at_a_time_or_both(self):
        clients = self.principals()
        admin = clients["admin"]
        for body in ({}, {"guest_cameras": 1}, {"guest_text": "yes"}, {"enabled": True}, {"guest_cameras": True, "revision": 1}):
            with self.subTest(body=body):
                answer = self.call("put", "/api/v1/security/guests", body, admin)
                self.assertEqual((answer.status_code, answer.json()["error"]), (400, "invalid_security"))
        self.assertEqual((get_state().guest_text, get_state().guest_cameras), (True, False))
        answer = self.call("put", "/api/v1/security/guests", {"guest_cameras": True}, admin).json()
        self.assertEqual((answer["guest_text"], answer["guest_cameras"]), (True, True))
        answer = self.call("put", "/api/v1/security/guests", {"guest_text": False}, admin).json()
        self.assertEqual((answer["guest_text"], answer["guest_cameras"]), (False, True))
        # An Admin demoted since signing in changes nothing.
        User.objects.filter(username="owner").update(admin=False)
        self.assertEqual(self.call("put", "/api/v1/security/guests", {"guest_cameras": False}, admin).status_code, 403)
        self.assertTrue(get_state().guest_cameras)
        self.assertEqual(list(Audit.objects.filter(action__startswith="security.guest").values_list("action", flat=True)),
                         ["security.guest_cameras_on", "security.guest_text_off"])

    def test_house_wide_alerts_and_the_system_page_are_an_admins_so_nobodys_without_an_account(self):
        feed = {"watching": "yes", "firing": 0, "alerts": []}
        with patch("oh_identity.alerts.answer", side_effect=lambda contract, house: {**feed, "house": house}):
            self.assertFalse(self.call("get", "/api/v1/alerts").json()["house"])
            clients = self.principals()
            self.assertEqual({name: self.call("get", "/api/v1/alerts", client=client).json()["house"]
                              for name, client in clients.items()}, {"guest": False, "member": False, "admin": True})

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

    def test_the_first_admin_requires_the_owners_code_and_csrf_without_secret_echo(self):
        self.assertEqual(self.first_admin().status_code, 403)
        code = arm_setup()
        state = get_state()
        self.assertNotEqual(state.setup_digest, code)
        self.assertEqual(len(state.setup_digest), 64)
        self.assertTrue(self.call("get", "/api/v1/security").json()["setup_available"])
        self.assertEqual(self.first_admin(setup_code="wrong").status_code, 403)
        denied = Client(enforce_csrf_checks=True).post("/api/v1/security", data=json.dumps({
            "username": "owner", "password": PASSPHRASE, "setup_code": code}), content_type="application/json", secure=True)
        self.assertEqual(denied.status_code, 403)
        # The switch that is gone is not a field any more.
        self.assertEqual(self.first_admin(setup_code=code, enabled=True).json(), {"error": "unknown_field"})
        self.assertFalse(User.objects.exists())
        response = self.call("post", "/api/v1/security", {"username": "owner", "password": PASSPHRASE, "setup_code": code},
                             REMOTE_ADDR="192.0.2.80")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({key: response.json()[key] for key in ("bootstrap_required", "setup_available", "guest_cameras")},
                         {"bootstrap_required": False, "setup_available": False, "guest_cameras": False})
        self.assertTrue(User.objects.get().admin)
        self.assertTrue(self.call("get", "/api/v1/me").json()["is_guest"])
        self.assertFalse(Session.objects.exists())
        self.assertEqual(get_state().setup_digest, "")
        self.assertNotIn(code, response.content.decode())
        self.assertNotIn(code, str(list(Audit.objects.values())))
        self.assertNotIn(PASSPHRASE, str(list(Audit.objects.values())))
        created = Audit.objects.get(action="bootstrap")
        self.assertEqual((created.principal, bool(created.peer), created.actor_id), ("setup", True, User.objects.get().pk))
        self.assertEqual(self.first_admin(setup_code=code).status_code, 409)
        # The account works at once: nothing else has to be turned on.
        self.assertTrue(self.call("get", "/api/v1/me", client=self.signed_in("owner")).json()["admin"])

    def test_setup_rearming_expiry_and_invalid_bootstrap_roll_back(self):
        old = arm_setup()
        code = arm_setup()
        self.assertEqual(self.first_admin(setup_code=old).status_code, 403)
        self.assertEqual(self.first_admin(setup_code=code, password=PASSPHRASE[:5]).status_code, 400)
        self.assertFalse(User.objects.exists())
        self.assertTrue(get_state().setup_digest)
        SecurityState.objects.update(setup_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(self.call("get", "/api/v1/security").json()["setup_available"])
        self.assertEqual(self.first_admin(setup_code=code).status_code, 403)
        self.assertEqual(self.first_admin(setup_code=arm_setup()).status_code, 200)
        with self.assertRaises(Problem):
            arm_setup()

    def test_a_local_bootstrap_spends_an_armed_setup_code(self):
        code = arm_setup()
        bootstrap("owner", PASSPHRASE)
        self.assertEqual(get_state().setup_digest, "")
        self.assertEqual(self.first_admin(setup_code=code).status_code, 409)
        self.assertEqual(Audit.objects.get(action="bootstrap").principal, "local")

    def test_missing_singleton_fails_closed_and_dynamic_config_never_selects_native(self):
        SecurityState.objects.all().delete()
        self.assertEqual(self.call("get", "/api/v1/security").status_code, 503)
        self.assertEqual(self.call("get", "/api/v1/me").status_code, 503)
        self.assertEqual(self.call("get", SNAPSHOT).status_code, 503)
        response = self.call("get", "/static/remote/access-config.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"mode":"authenticated"', response.content)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_password_creation_and_login_share_the_1024_character_limit(self):
        value = PASSPHRASE + "x" * (1024 - len(PASSPHRASE))
        owner = bootstrap("owner", value)
        self.assertTrue(owner.check_password(value))
        with self.assertRaises(Problem) as caught:
            password(owner, value + "x")
        self.assertEqual(caught.exception.code, "invalid_password")
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": value}).status_code, 200)
        # Longer than any password can be: a malformed request, not a guess.
        self.assertEqual(self.call("post", "/api/v1/session", {"username": "owner", "password": value + "x"}).status_code, 400)


class Concurrency(Site, TransactionTestCase):
    def test_two_first_setups_create_exactly_one_administrator(self):
        code = arm_setup()
        gate = threading.Barrier(2)
        data = {"username": "owner", "password": PASSPHRASE, "setup_code": code}

        def create():
            close_old_connections()
            gate.wait(timeout=3)
            try:
                return Client().post("/api/v1/security", data=json.dumps(data), content_type="application/json", secure=True).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(get_state().setup_digest)

    @patch("oh_identity.streams.REVALIDATE", 0)
    def test_a_live_guest_stream_goes_on_when_the_first_account_is_created(self):
        snapshot = {"guest": True, "guest_text": True}

        async def check():
            upstream = Upstream()
            frames = stream(RuntimeAdapter(self.adapter.contract, upstream), "remote", snapshot)
            self.assertIn(b"snapshot", await anext(frames))
            await sync_to_async(bootstrap)("owner", PASSPHRASE)
            upstream.send(event("loc_den_source", "player"))
            self.assertIn(b"player", await anext(frames))
            await frames.aclose()
        async_to_sync(check)()


def hold(room):
    return {"version": 1, "room": room, "timezone": "UTC", "revision": 4, "enabled": False, "daily_off": [], "locks": [],
            "manual_lock": {"desired": "off", "started_at": "2026-01-01T00:00:00+00:00"}}


def schedule(room, enabled):
    return {"version": 1, "room": room, "timezone": "UTC", "revision": 4, "enabled": enabled,
            "daily_off": [{"id": "night", "time": "23:00", "days": [0]}], "locks": []}


class Migrations(TransactionTestCase):
    BEFORE, LEAF = "0004_security_guest_text", "0005_independent_settings"

    def migrate(self, target):
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        executor.migrate([("oh_identity", target)])
        return executor.loader.project_state([("oh_identity", target)]).apps

    def tearDown(self):
        from django.db.migrations.executor import MigrationExecutor
        MigrationExecutor(connection).migrate([("oh_identity", self.LEAF)])

    def upgraded(self, *, enabled, accounts, guest_text, policies=()):
        """The site an upgrade leaves from this stored state of the release
        before, through the real migration."""
        from django.contrib.auth.hashers import make_password
        old = self.migrate(self.BEFORE)
        for name in ("ReconcileState", "RoomPolicy", "User"):
            old.get_model("oh_identity", name).objects.all().delete()
        old.get_model("oh_identity", "SecurityState").objects.update_or_create(
            pk=1, defaults={"enabled": enabled, "revision": 7, "guest_text": guest_text})
        if accounts:
            old.get_model("oh_identity", "User").objects.create(username="owner", password=make_password(PASSPHRASE),
                                                                admin=True, auth_revision=3)
        for room, config in policies:
            old.get_model("oh_identity", "RoomPolicy").objects.create(room_id=room, config=config, revision=4)
            old.get_model("oh_identity", "ReconcileState").objects.create(room_id=room, revision=4, state={"phase": "verified"})
        self.migrate(self.LEAF)
        return get_state()

    def test_an_upgrade_closes_cameras_to_guests_and_changes_nothing_else_for_any_stored_state(self):
        policies = (("den", hold("den")), ("desk", schedule("desk", True)), ("attic", schedule("attic", False)))
        for enabled, accounts, guest_text in itertools.product((False, True), repeat=3):
            with self.subTest(enabled=enabled, accounts=accounts, guest_text=guest_text):
                state = self.upgraded(enabled=enabled, accounts=accounts, guest_text=guest_text, policies=policies)
                # The one decided change: off everywhere, also where the
                # switch being off had opened the cameras to every client.
                self.assertFalse(state.guest_cameras)
                # Off, Guests saw text whatever was stored; on, the stored choice held.
                self.assertEqual(state.guest_text, guest_text if enabled else True)
                self.assertEqual(User.objects.count(), int(accounts))
                self.assertEqual(set(User.objects.values_list("auth_revision", flat=True)), {3} if accounts else set())
                configs = {row.room_id: (row.revision, row.config) for row in RoomPolicy.objects.all()}
                if enabled:
                    # Locks applied and still do, untouched.
                    self.assertEqual(configs, {"den": (4, hold("den")), "desk": (4, schedule("desk", True)),
                                               "attic": (4, schedule("attic", False))})
                    self.assertEqual(ReconcileState.objects.count(), 3)
                else:
                    # No lock applied and none does: schedules kept, disabled;
                    # the hold released; each changed policy a new revision.
                    self.assertEqual(configs["den"], (5, {**{key: value for key, value in hold("den").items()
                                                             if key != "manual_lock"}, "revision": 5}))
                    self.assertEqual(configs["desk"], (5, {**schedule("desk", False), "revision": 5}))
                    self.assertEqual(configs["attic"], (4, schedule("attic", False)))
                    self.assertFalse(ReconcileState.objects.exists())
                from oh_room_policy import active
                self.assertEqual({room: active(config) for room, (_, config) in configs.items()},
                                 {"den": enabled, "desk": enabled, "attic": False})
        columns = {column.name for column in connection.introspection.get_table_description(
            connection.cursor(), "oh_identity_securitystate")}
        self.assertEqual(columns, {"id", "setup_digest", "setup_expires_at", "guest_text", "guest_cameras"})

    def test_an_upgraded_site_answers_as_before_except_cameras_for_guests(self):
        for enabled, accounts in itertools.product((False, True), repeat=2):
            with self.subTest(enabled=enabled, accounts=accounts):
                self.upgraded(enabled=enabled, accounts=accounts, guest_text=False,
                              policies=(("den", {"locked": True, "enabled": True}),))
                runtime = house()
                install_adapter(runtime)
                guest = Client()
                self.assertEqual(guest.get(SNAPSHOT, secure=True).status_code, 401)
                me = guest.get("/api/v1/me", secure=True).json()
                self.assertEqual(me["permissions"], ["room.control", "site.read"])
                # A room locked under the switch stays locked; one the switch
                # being off left open stays open.
                locked = guest.get("/api/v1/locks", secure=True).json()["rooms"]["den"]["locked"]
                self.assertEqual(locked, enabled)
                self.assertEqual(valid({"guest": True, "guest_text": True}), not enabled)
                status = guest.get("/api/v1/security", secure=True).json()
                self.assertEqual((status["bootstrap_required"], status["guest_cameras"]), (not accounts, False))
                if accounts:
                    # The account signs in with the password it had.
                    browser = Client(enforce_csrf_checks=True)
                    browser.get("/api/v1/session", secure=True)
                    login = browser.post("/api/v1/session", json.dumps({"username": "owner", "password": PASSPHRASE}),
                                         content_type="application/json", secure=True, HTTP_ORIGIN="https://testserver",
                                         HTTP_X_CSRFTOKEN=browser.cookies[settings.CSRF_COOKIE_NAME].value)
                    self.assertEqual(login.status_code, 200)
                    self.assertEqual(browser.get(SNAPSHOT, secure=True).status_code, 200)

    def test_a_site_without_its_settings_row_stays_refused(self):
        old = self.migrate(self.BEFORE)
        old.get_model("oh_identity", "SecurityState").objects.all().delete()
        self.migrate(self.LEAF)
        with self.assertRaises(Problem) as caught:
            get_state()
        self.assertEqual(caught.exception.code, "security_unavailable")

    def test_security_state_preserves_existing_accounts_and_defaults_empty_sites_off(self):
        from django.contrib.auth.hashers import make_password
        historical = self.migrate("0001_initial")
        historical.get_model("oh_identity", "User").objects.create(username="existing-owner", password=make_password(None))
        # The state as that migration left it: later ones add and drop columns.
        state = self.migrate("0002_security_state").get_model("oh_identity", "SecurityState").objects.get(pk=1)
        self.assertTrue(state.enabled)
        self.assertEqual(state.revision, 1)
        historical = self.migrate("0001_initial")
        historical.get_model("oh_identity", "User").objects.all().delete()
        self.assertFalse(self.migrate("0002_security_state").get_model("oh_identity", "SecurityState").objects.get(pk=1).enabled)

    def test_released_roles_become_one_admin_flag_and_audit_gains_labels(self):
        from django.contrib.auth.hashers import make_password
        # The schema the previous release created: reserved roles, a custom
        # role carrying every permission, room grants and a role-less account.
        historical = self.migrate("0002_security_state")
        roles, users = historical.get_model("oh_identity", "Role"), historical.get_model("oh_identity", "User")
        everything = ["site.read", "room.control", "settings.manage", "users.manage", "roles.manage", "pairing.manage"]
        reserved = {name: roles.objects.create(name=name, reserved=True, permissions=grants) for name, grants in
                    (("Admin", everything), ("User", everything[:2]), ("Viewer", everything[:1]))}
        custom = roles.objects.create(name="Everything but reserved", permissions=everything)
        for name, role, rooms in (("owner", reserved["Admin"], None), ("operator", reserved["User"], ["den"]),
                                  ("viewer", reserved["Viewer"], []), ("imposter", custom, None), ("orphan", None, None)):
            users.objects.create(username=name, password=make_password(None), role=role, rooms=rooms, auth_revision=4)
        historical.get_model("oh_identity", "Audit").objects.create(action="bootstrap", outcome="ok")
        self.migrate("0003_principals_and_audit")
        self.assertEqual(dict(User.objects.values_list("username", "admin")),
                         {"owner": True, "operator": False, "viewer": False, "imposter": False, "orphan": False})
        self.assertEqual(set(User.objects.values_list("auth_revision", flat=True)), {4})
        self.assertEqual(list(Audit.objects.values_list("principal", "peer")), [("", "")])
        self.assertNotIn("oh_identity_role", connection.introspection.table_names())
        columns = {column.name for column in connection.introspection.get_table_description(connection.cursor(), "oh_identity_user")}
        self.assertTrue({"admin", "auth_revision"} <= columns)
        self.assertFalse({"role_id", "rooms"} & columns)
