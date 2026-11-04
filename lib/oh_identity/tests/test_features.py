"""Actual finite pairing, camera and All Off APIs; no real runtime transport."""

import json
import tempfile
from pathlib import Path
from contextlib import contextmanager

from django.conf import settings
from django.test import Client, TestCase

from oh_identity.models import Audit, BoundaryReceipt, PersonalToken, Role, RoomPolicy, User
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter, project
from oh_identity.policy_store import Store, rebase_timezone
from oh_identity.services import Problem
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import FakeRuntime, PASSPHRASE, Policy
from oh_identity.transport import LoopbackTransport


class Transport(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.states.update(tv_pairing_status="failed", tv_pairing_result=json.dumps({"state": "failed", "id": "private-cookie-sentinel",
                          "error": {"code": 1, "api": True, "http": 200, "message": "private-cookie-sentinel"}}))
        self.media_calls = []

    def group(self, name):
        return {"members": [{"name": "camera_online", "state": "ON"}, {"name": "camera_motion", "state": "OPEN"},
                            {"name": "unrelated_pairing", "state": "private-cookie-sentinel"}]}

    def media(self, resource, method, body, maximum):
        self.media_calls.append((resource, method))
        return ("image/jpeg", b"\xff\xd8\xff\xd9") if method == "GET" else ("application/sdp", b"v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=sendonly\r\n")


class Features(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.user = User.objects.create_user("user", password=PASSPHRASE, role=Role.objects.get(name="User"))

    def setUp(self):
        pair = {"value": "sony_ip_control", "config": {"device": "tv", "label": "TV", "room": "Den", "length": 4,
                "chars": "digits", "status": "tv_pairing_status", "result": "tv_pairing_result"}}
        contract = RuntimeContract({"den": {"source_item": "loc_den_source", "sources": ["player"]},
                                    "desk": {"source_item": "loc_desk_source", "sources": ["player"]}},
                                  states={"tv_pairing_status": {"kind": "pairing_status"}, "tv_pairing_result": {"kind": "pairing_result"},
                                          "camera_online": {"kind": "switch"}, "camera_motion": {"kind": "enum", "values": ["OPEN", "CLOSED"]}},
                                  pairings={"tv_pairing": pair}, groups={"camera_status": ["camera_online", "camera_motion"]},
                                  media={"/unifi/media/image/camera": {"kind": "snapshot"}, "/unifi/media/play/camera:high": {"kind": "sdp"}})
        self.adapter = RuntimeAdapter(contract, Transport(), Policy())
        install_adapter(self.adapter)
        _, self.owner_token = issue_token(self.owner, "test")
        _, self.user_token = issue_token(self.user, "test")
        self.client = Client(enforce_csrf_checks=True)

    def call(self, method, path, data=None, owner=False, content_type="application/json"):
        value = json.dumps(data) if content_type == "application/json" and data is not None else data
        return getattr(self.client, method)(path, value, content_type=content_type, secure=True,
                                           HTTP_AUTHORIZATION="Bearer " + (self.owner_token if owner else self.user_token))

    def test_pairing_preflight_and_commands_are_admin_only_and_never_echo_code(self):
        path = "/rest/items?metadata=pairing&fields=name,state,metadata"
        self.assertEqual(self.call("get", path).status_code, 403)
        response = self.call("get", path, owner=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["state"], "NULL")
        self.assertNotIn("sentinel", response.content.decode())
        self.assertEqual(self.call("post", "/rest/items/tv_pairing", "1234", content_type="text/plain").status_code, 403)
        for value in ("REQUEST", "AGAIN", "CANCEL", "1234"):
            response = self.call("post", "/rest/items/tv_pairing", value, owner=True, content_type="text/plain")
            self.assertEqual(response.status_code, 202)
            self.assertNotIn(value, response.content.decode())
        self.assertNotIn("1234", str(list(Audit.objects.values())))
        self.assertEqual(self.call("post", "/rest/items/tv_pairing", "bad-private-code", owner=True, content_type="text/plain").status_code, 400)

    def test_camera_group_excludes_unrelated_items(self):
        response = self.call("get", "/rest/items/camera_status?fields=members(name,state)")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["members"], [{"name": "camera_online", "state": "ON"}, {"name": "camera_motion", "state": "OPEN"}])
        self.assertNotIn("sentinel", response.content.decode())

    def test_camera_media_exact_origin_auth_recvonly_and_unknown_paths(self):
        self.assertEqual(Client().get("/unifi/media/image/camera", secure=True).status_code, 401)
        response = self.call("get", "/unifi/media/image/camera")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", response.headers)
        before = len(self.adapter.transport.media_calls)
        self.assertEqual(self.call("get", "/unifi/media/image/unknown").status_code, 404)
        self.assertEqual(len(self.adapter.transport.media_calls), before)
        offer = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\n"
        self.assertEqual(self.call("post", "/unifi/media/play/camera:high", offer, content_type="application/sdp").status_code, 200)
        self.assertEqual(self.call("post", "/unifi/media/play/camera:high", offer.replace("recvonly", "sendrecv"), content_type="application/sdp").status_code, 400)
        self.assertEqual(self.call("post", "/unifi/media/play/camera:high", "v=0\r\na=recvonly\r\n", content_type="application/sdp").status_code, 400)

    def test_reserved_roles_and_privilege_escalation_protected(self):
        role = Role.objects.get(name="Admin")
        self.assertEqual(self.call("patch", "/api/v1/roles/" + str(role.pk), {"name": "weakened"}, owner=True).status_code, 409)
        self.assertEqual(self.call("post", "/api/v1/roles", {"name": "operator_admin", "permissions": ["users.manage"]}, owner=True).status_code, 400)
        response = self.call("post", "/api/v1/roles", {"name": "Room operator", "permissions": ["site.read", "room.control"]}, owner=True)
        self.assertEqual(response.status_code, 201)

    def test_transport_rejects_external_origins_and_unsafe_credential_files(self):
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            path = Path(temporary) / "upstream-token"
            path.write_text("synthetic-never-used")
            path.chmod(0o600)
            for url in ("http://elsewhere.invalid:8080", "http://127.0.0.1:8080/rest", "http://user@127.0.0.1:8080"):
                with self.assertRaises(ValueError):
                    LoopbackTransport(url, path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                LoopbackTransport("http://127.0.0.1:8080", path)

    def test_policy_revisions_are_one_query_and_include_absent_neighbors(self):
        row = RoomPolicy.objects.create(room_id="den", config={}, revision=2)
        store = Store()
        with self.assertNumQueries(1):
            self.assertEqual(store.current_revisions(("den", "desk")), {"den": 2, "desk": None})
        RoomPolicy.objects.create(room_id="desk", config={}, revision=1)
        row.revision = 3
        row.save(update_fields=["revision"])
        with self.assertNumQueries(1):
            self.assertEqual(store.current_revisions(("den", "desk")), {"den": 3, "desk": 1})

    def test_json_projection_keeps_finite_provider_fields_not_raw_bodies(self):
        schema = {"kind": "json", "fields": {"observedAt": {"kind": "number", "min": 0},
                  "values": {"kind": "object", "fields": {"power": {"kind": "enum", "values": ["on", "off"]},
                  "video": {"kind": "text", "pattern": "[A-Za-z0-9 ]{1,40}"}, "cookie": {"kind": "text", "pattern": ".*"}}}}}
        sentinel = "private-sentinel"
        raw = {"observedAt": 12345, "values": {"power": "off", "video": "4K120 RGB", "cookie": sentinel},
               "error": "private-sentinel", "id": "private-sentinel"}
        self.assertEqual(json.loads(project("avr_signal", json.dumps(raw), schema)), {"observedAt": 12345, "values": {"power": "off", "video": "4K120 RGB"}})
        raw["values"]["video"] = "Cookie private-sentinel"
        self.assertNotIn("sentinel", project("avr_signal", json.dumps(raw), schema))
        self.assertEqual(project("read_text", "arbitrary", {"kind": "text"}), "UNDEF")

    def test_lock_rechecks_policy_after_acquiring_and_value_specific_affected_rooms(self):
        touched = []

        @contextmanager
        def lock(rooms, resources):
            touched.append((rooms, resources))
            RoomPolicy.objects.update_or_create(room_id="den", defaults={"config": {"locked": True}})
            yield
        self.adapter.command_lock = lock
        with self.assertRaises(Problem) as caught:
            self.adapter.dispatch(self.user, "den", "source", "player")
        self.assertEqual(caught.exception.code, "room_locked")
        self.assertEqual(touched, [(("den",), ("loc_den_source",))])
        self.assertEqual(self.adapter.transport.commands, [])
        contract = RuntimeContract({"den": {"source_item": "loc_den_source", "sources": ["local", "shared"], "actions": {
            "source": {"affected_by_value": {"local": ["den"], "shared": ["den", "desk"]}}}},
            "desk": {"source_item": "loc_desk_source", "sources": ["shared"]}})
        self.assertEqual(contract.command("den", "source", "local")[2], ("den",))
        self.assertEqual(contract.command("den", "source", "shared")[2], ("den", "desk"))

    def test_historical_receipt_finishes_after_revision_change_and_timezone_rebases(self):
        policy = RoomPolicy.objects.create(room_id="den", config={"room": "den", "timezone": "UTC", "revision": 1})
        store = Store()
        self.assertTrue(store.claim_boundary("den", 1, "one-boundary", None))
        self.assertEqual(rebase_timezone("America/Chicago"), 1)
        self.assertEqual(store.current_revision("den"), 2)
        store.finish_boundary("den", 1, "one-boundary", {"status": "skipped", "reason": "stale_revision"})
        self.assertEqual(BoundaryReceipt.objects.get(key="one-boundary").status, "skipped")
        self.assertFalse(store.save_reconcile("den", 1, {"phase": "verified", "state": "verified"}))
        self.assertTrue(store.save_reconcile("den", 2, {"phase": "enforcing", "state": "degraded", "desired": "player",
                         "matched": ["loc_den_source", "private_cookie_sentinel"], "reason": "dispatch_unconfirmed", "raw": "private-sentinel"}))
        from oh_identity.models import ReconcileState
        self.assertNotIn("sentinel", json.dumps(ReconcileState.objects.get(room_id="den").state))
        self.assertIsNone(store.current_revision("absent"))

    def test_queued_credential_revocation_cannot_dispatch(self):
        @contextmanager
        def revoke(rooms, resources):
            PersonalToken.objects.filter(user=self.user).update(revoked=True)
            yield
        self.adapter.command_lock = revoke
        self.assertEqual(self.call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}).status_code, 401)
        self.assertEqual(self.adapter.transport.commands, [])
