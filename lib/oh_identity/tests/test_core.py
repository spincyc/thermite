"""Actual Django/SQLite/Argon2 boundary tests. No runtime listener or AV calls."""

import json
from datetime import timedelta

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from oh_identity.models import Audit, PersonalToken, Role, RoomPolicy, User
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter, project
from oh_identity.services import Problem, bootstrap, issue_token, save_user

# Synthetic test fixture, not an installed account or reusable deployment secret.
PASSPHRASE = " ".join(("A disconnected", "identity test", "phrase 902!"))


class FakeRuntime:
    def __init__(self):
        self.commands = []
        self.states = {"loc_den_source": "player", "loc_desk_source": "off", "amp_volume": "56.5"}

    def command(self, item, value):
        self.commands.append((item, value))

    def read(self, item):
        return self.states[item]


class Policy:
    def validate(self, config, room, contract):
        if set(config) - {"enabled", "room", "timezone", "revision", "locked"}:
            raise ValueError("Invalid test policy")
        return config

    def authorize(self, config, now):
        return {"allowed": not config.get("locked", False)}


def adapter():
    contract = RuntimeContract({
        "den": {"source_item": "loc_den_source", "sources": ["player"], "actions": {
            "volume": {"item": "amp_volume", "kind": "number", "min": 0, "max": 70, "step": .5},
            "shared_power": {"item": "player_power", "kind": "switch", "values": ["ON", "OFF"], "affected_rooms": ["den", "desk"]}}},
        "desk": {"source_item": "loc_desk_source", "sources": ["player"]}},
        states={"loc_den_source": {"kind": "enum", "values": ["player", "off"]},
                "amp_volume": {"kind": "number", "max": 98},
                "tv_pairing": {"kind": "enum"}, "unrelated_secret": {"kind": "enum"}})
    return RuntimeAdapter(contract, FakeRuntime(), Policy())


@override_settings(ALLOWED_HOSTS=["testserver"])
class Core(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE, role=Role.objects.get(name="User"))
        cls.viewer = User.objects.create_user("viewer", password=PASSPHRASE, role=Role.objects.get(name="Viewer"))

    def setUp(self):
        self.adapter = adapter()
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)

    def request(self, method, path, data=None, **extra):
        csrf = self.client.cookies.get(settings.CSRF_COOKIE_NAME)
        extra.setdefault("HTTP_ORIGIN", "https://testserver")
        return getattr(self.client, method)(path, data=json.dumps(data) if data is not None else None,
                                           content_type="application/json", secure=True,
                                           HTTP_X_CSRFTOKEN=csrf.value if csrf else "", **extra)

    def signin(self, name="owner"):
        self.request("get", "/api/v1/session")
        response = self.request("post", "/api/v1/session", {"username": name, "password": PASSPHRASE})
        self.assertEqual(response.status_code, 200)
        return response

    def test_bootstrap_is_local_one_time_argon2(self):
        self.assertTrue(self.admin.password.startswith("argon2$argon2id$"))
        with self.assertRaises(Problem) as caught:
            bootstrap("intruder", PASSPHRASE)
        self.assertEqual(caught.exception.code, "already_initialized")

    def test_anonymous_and_viewer_cannot_command(self):
        self.assertEqual(self.request("get", "/api/v1/me").status_code, 401)
        self.signin("viewer")
        self.assertEqual(self.request("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "player"}).status_code, 403)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_operator_controls_unlocked_but_cannot_edit_users_roles_policy(self):
        self.signin("operator")
        response = self.request("post", "/api/v1/rooms/den/commands", {"action": "volume", "value": 56.5})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.adapter.transport.commands, [("amp_volume", "56.5")])
        self.assertEqual(self.request("get", "/api/v1/rooms/den/policy").json()["can_edit"], False)
        for path, data in [("/api/v1/users", {"username": "bad"}), ("/api/v1/roles", {"name": "bad"}),
                           ("/api/v1/rooms/den/policy", {"revision": 0, "config": {}})]:
            response = self.request("put" if path.endswith("policy") else "post", path, data)
            self.assertEqual(response.status_code, 403)

    def test_csrf_origin_required_even_login(self):
        self.request("get", "/api/v1/session")
        self.assertEqual(self.client.post("/api/v1/session", {}, secure=True).status_code, 403)
        self.assertEqual(self.request("post", "/api/v1/session", {"username": "owner", "password": PASSPHRASE},
                                      HTTP_ORIGIN="https://evil.invalid").status_code, 403)

    def test_login_throttled_and_no_secret_echo(self):
        self.request("get", "/api/v1/session")
        invalid = "synthetic-sentinel"
        for _ in range(5):
            self.assertEqual(self.request("post", "/api/v1/session", {"username": "nobody", "password": invalid}).status_code, 401)
        response = self.request("post", "/api/v1/session", {"username": "nobody", "password": invalid})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("sentinel", response.content.decode())
        self.assertNotIn("sentinel", str(list(Audit.objects.values())))

    def test_unknown_item_offgrid_cap_and_body_injection_fail_closed(self):
        self.signin("operator")
        for action, value in [("/rest/items/private_pairing", "1234"), ("volume", 70.5), ("volume", 56.25), ("source", "elsewhere")]:
            self.assertGreaterEqual(self.request("post", "/api/v1/rooms/den/commands", {"action": action, "value": value}).status_code, 400)
        self.assertEqual(self.request("get", "/rest/items/tv_pairing/state").status_code, 404)
        self.assertEqual(self.request("get", "/rest/items/unrelated_secret/state").status_code, 404)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_shared_source_cannot_bypass_other_room_lock(self):
        self.signin("operator")
        RoomPolicy.objects.create(room_id="desk", config={"locked": True})
        self.assertEqual(self.request("post", "/api/v1/rooms/den/commands", {"action": "shared_power", "value": "OFF"}).status_code, 423)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_room_grants_and_missing_policy_engine_deny(self):
        self.operator.rooms = ["den"]
        self.operator.save()
        self.signin("operator")
        self.assertEqual(self.request("post", "/api/v1/rooms/desk/commands", {"action": "source", "value": "off"}).status_code, 403)
        RoomPolicy.objects.create(room_id="den", config={"enabled": True})
        self.adapter.policy = None
        self.assertEqual(self.request("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}).status_code, 503)

    def test_token_is_hashed_expiring_revocable_and_role_revision_fenced(self):
        token, raw = issue_token(self.operator, "TUI")
        client = Client(enforce_csrf_checks=True)
        self.assertNotEqual(token.digest, raw)
        self.assertEqual(client.get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 200)
        self.operator.auth_revision += 1
        self.operator.save()
        self.assertEqual(client.get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 401)
        token, raw = issue_token(self.operator, "expiry")
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save()
        self.assertEqual(client.get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 401)

    def test_session_role_change_and_logout_token_revocation(self):
        self.signin("operator")
        self.operator.auth_revision += 1
        self.operator.save()
        self.assertEqual(self.request("get", "/api/v1/me").status_code, 401)
        token, raw = issue_token(self.operator, "logout")
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/api/v1/logout", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 200)
        self.assertEqual(client.get("/api/v1/me", secure=True, HTTP_AUTHORIZATION="Bearer " + raw).status_code, 401)

    def test_last_admin_and_stale_mutation_actor(self):
        with self.assertRaises(Problem) as caught:
            save_user(self.admin, {"active": False}, self.admin.pk)
        self.assertEqual(caught.exception.code, "last_admin")
        User.objects.filter(pk=self.admin.pk).update(auth_revision=self.admin.auth_revision + 1)
        with self.assertRaises(Problem):
            save_user(self.admin, {"active": False}, self.operator.pk)

    def test_policy_cas_injects_trusted_fields(self):
        self.signin()
        response = self.request("put", "/api/v1/rooms/den/policy", {"revision": 0, "config": {"locked": True, "room": "spoof", "timezone": "spoof", "revision": 999}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"], {"locked": True, "room": "den", "timezone": settings.TIME_ZONE, "revision": 1})
        self.assertEqual(self.request("put", "/api/v1/rooms/den/policy", {"revision": 0, "config": {}}).status_code, 409)

    def test_result_projection_discards_pin_cookie_message_extra_fields(self):
        sentinel = "PIN-cookie-sentinel"
        raw = json.dumps({"id": "PIN-cookie-sentinel", "state": "failed", "error": {"code": 1, "api": True, "http": 200,
                           "message": "PIN-cookie-sentinel"}, "cookie": sentinel})
        value = project("tv_pairing_result", raw, {"kind": "pairing_result"})
        self.assertEqual(json.loads(value), {"state": "failed", "error": {"code": 1, "api": True, "http": 200}})
        self.assertNotIn("sentinel", value)
