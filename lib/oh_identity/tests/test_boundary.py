"""Adversarial stream, private-asset and concurrent admin invariants."""

import asyncio
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlencode

import httpx
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings

from oh_routes import ALIASES, PAGES
from oh_identity.models import PersonalToken, User
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import Problem, bootstrap, issue_token, save_user
from oh_identity.security import get_state
from oh_identity import hub as hubs
from oh_identity.streams import stream
from oh_identity.tests.test_core import PASSPHRASE, adapter
from oh_identity.tests.test_event_hub import Upstream, event as native_event
from oh_identity.tests.test_transport import HTTPClient
from oh_identity.topics import available, frame
from oh_identity.transport import LoopbackTransport


@override_settings(ALLOWED_HOSTS=["testserver"])
class Boundary(TransactionTestCase):
    def setUp(self):
        self.admin = bootstrap("owner", PASSPHRASE)
        self.adapter = adapter()
        install_adapter(self.adapter)

    def test_simultaneous_admin_removals_keep_one(self):
        other = User.objects.create_user("second", password=PASSPHRASE, admin=True)
        barrier = threading.Barrier(2)

        def demote(ident):
            close_old_connections()
            actor = User.objects.get(pk=ident)
            barrier.wait(timeout=3)
            try:
                save_user(actor, {"active": False}, ident)
                return "ok"
            except Problem as exc:
                return exc.code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = list(executor.map(demote, [self.admin.pk, other.pk]))
        self.assertCountEqual(result, ["ok", "last_admin"])
        self.assertEqual(User.objects.filter(admin=True, is_active=True).count(), 1)

    def test_token_login_has_no_cookie_and_rejects_browser_origin(self):
        client = Client(enforce_csrf_checks=True)
        data = {"username": "owner", "password": PASSPHRASE, "name": "terminal"}
        response = client.post("/api/v1/token-login", json.dumps(data), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.cookies)
        raw = response.json()["token"]
        self.assertEqual(client.get("/api/v1/me", HTTP_AUTHORIZATION="Bearer " + raw, secure=True).status_code, 200)
        self.assertEqual(client.post("/api/v1/token-login", json.dumps(data), content_type="application/json", secure=True,
                                     HTTP_ORIGIN="https://evil.invalid").status_code, 403)
        self.assertTrue(settings.SESSION_COOKIE_NAME.startswith(settings.COOKIE_PREFIX))
        self.assertTrue(settings.CSRF_COOKIE_NAME.startswith(settings.COOKIE_PREFIX))

    def test_private_static_and_state_projection(self):
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            root = Path(temporary)
            (root / "house.js").write_text("private configured inventory")
            (root / "code.js").write_text("public program")
            self.adapter.contract = RuntimeContract({}, assets={"house.js": {}, "code.js": {}}, asset_root=root)
            client = Client()
            response = client.get("/static/remote/house.js", secure=True)
            self.assertEqual(response.status_code, 200)
            response.close()
            response = client.get("/static/remote/code.js", secure=True)
            self.assertEqual(response.status_code, 200)
            response.close()
            self.assertEqual(client.get("/static/remote/../secret-key", secure=True).status_code, 404)

    def system_assets(self):
        temporary = tempfile.TemporaryDirectory(dir=settings.STATE)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ("system.html", "system.json", "home.html", "access.html", "admin-note.html"):
            (root / name).write_text('{"version":1,"tools":[]}' if name.endswith(".json") else "private System page")
        # Empty legacy specs cannot weaken the finite System basename policy.
        self.adapter.contract = RuntimeContract({}, assets={"system.html": {}, "system.json": {},
            "home.html": {"private": True}, "access.html": {}, "admin-note.html": {"admin": True}}, asset_root=root)

    def session_client(self, user):
        client = Client()
        client.force_login(user)
        session = client.session
        session["identity_revision"] = user.auth_revision
        session.save()
        return client

    def test_finite_aliases_redirect_only_safe_methods_without_reflecting_query(self):
        self.system_assets()
        client = Client()
        for path, target in ALIASES.items():
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path + "?next=https://foreign.invalid&token=private", secure=True)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response["Location"], target)
                    self.assertEqual(response["Cache-Control"], "no-store")
            for method in ("post", "put", "delete"):
                response = getattr(client, method)(path, secure=True)
                self.assertEqual(response.status_code, 405)
                self.assertNotIn("Location", response)
        for path in ("/overview/other", "/Overview", "/overview//"):
            self.assertEqual(client.get(path, secure=True).status_code, 404)
        response = client.get("/static/remote/home.html", secure=True)
        self.assertEqual(response.status_code, 200)
        response.close()

    def route_assets(self):
        temporary = tempfile.TemporaryDirectory(dir=settings.STATE)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        specs = {}
        for ident, _, name in PAGES:
            (root / name).write_text("<!doctype html><title>" + ident + " fixture</title>")
            specs[name] = {} if ident == "access" else {"private": True}
        # The finite System policy still protects a pre-policy exported contract.
        specs["system.html"] = {}
        self.adapter.contract = RuntimeContract({}, assets=specs, asset_root=root)
        return root

    def test_all_clean_routes_serve_their_own_asset_and_head_metadata_without_a_body(self):
        root = self.route_assets()
        client = self.session_client(self.admin)
        for _, path, name in PAGES:
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path, secure=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("Location", response)
                    self.assertEqual(response["Content-Length"], str((root / name).stat().st_size))
                    self.assertEqual(response["Content-Type"], "text/html")
                    # Read whole: no file handle or synchronous iterator outlives the view.
                    self.assertFalse(response.streaming)
                    self.assertEqual(response.content, b"" if method == "head" else (root / name).read_bytes())
        self.assertEqual(self.adapter.transport.commands, [])

    def test_guest_pages_work_and_admin_login_uses_fixed_canonical_next(self):
        self.route_assets()
        client = Client()
        for ident, path, _ in PAGES:
            for method in ("get", "head"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path + "?next=https://foreign.invalid&token=private", secure=True)
                    if ident != "system":
                        self.assertEqual(response.status_code, 200)
                    else:
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response["Location"], "/account/?" + urlencode({"next": path}))
                        self.assertEqual(response["Cache-Control"], "no-store")
                    response.close()
            for method in ("post", "put", "patch", "delete"):
                response = getattr(client, method)(path, secure=True)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], "GET, HEAD")
                self.assertNotIn("Location", response)
        for path in ("/system/child", "/System/", "/settings/", "/remotes//"):
            self.assertEqual(client.get(path, secure=True).status_code, 404)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_clean_system_and_users_aliases_keep_existing_asset_and_api_authorization(self):
        root = self.route_assets()
        member = User.objects.create_user("member", password=PASSPHRASE)
        client = self.session_client(member)
        for path in ("/system/", "/static/remote/system.html"):
            self.assertEqual(client.get(path, secure=True).status_code, 403)
        # Users HTML remains ordinary private presentation; its API requires
        # an Admin. An explicit admin asset policy is also honored by aliases.
        response = client.get("/users/", secure=True)
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(client.get("/api/v1/users", secure=True).status_code, 403)
        specs = {name: dict(spec) for name, spec in self.adapter.contract.assets.items()}
        specs["users.html"]["admin"] = True
        self.adapter.contract = RuntimeContract({}, assets=specs, asset_root=root)
        for path in ("/users/", "/static/remote/users.html"):
            self.assertEqual(client.get(path, secure=True).status_code, 403)
        admin = self.session_client(self.admin)
        for path in ("/system/", "/users/"):
            response = admin.get(path, secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "no-store")
            response.close()
        User.objects.filter(pk=self.admin.pk).update(admin=False)
        self.assertEqual(admin.get("/system/", secure=True).status_code, 403)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_system_assets_require_an_authoritative_admin_session(self):
        self.system_assets()
        anonymous = Client()
        response = anonymous.get("/static/remote/system.html?next=https://foreign.invalid", secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/account/?next=%2Fsystem%2F")
        self.assertEqual(anonymous.get("/static/remote/system.json", secure=True).status_code, 401)
        member = User.objects.create_user("operator", password=PASSPHRASE)
        for actor in (member,):
            client = self.session_client(actor)
            for name in ("system.html", "system.json", "admin-note.html"):
                self.assertEqual(client.get("/static/remote/" + name, secure=True).status_code, 403)
        admin = self.session_client(self.admin)
        for name in ("system.html", "system.json", "admin-note.html"):
            response = admin.get("/static/remote/" + name, secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "no-store")
            response.close()
        User.objects.filter(pk=self.admin.pk).update(admin=False)
        self.assertEqual(admin.get("/static/remote/system.json", secure=True).status_code, 403)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_system_assets_enforce_personal_token_revocation_and_token_channel(self):
        self.system_assets()
        member = User.objects.create_user("member", password=PASSPHRASE)
        _, viewer_raw = issue_token(member, "member")
        token, admin_raw = issue_token(self.admin, "admin")
        client = Client(enforce_csrf_checks=True)
        for name in ("system.html", "system.json"):
            path = "/static/remote/" + name
            self.assertEqual(client.get(path, secure=True, HTTP_AUTHORIZATION="Bearer " + viewer_raw).status_code, 403)
            response = client.get(path, secure=True, HTTP_AUTHORIZATION="Bearer " + admin_raw)
            self.assertEqual(response.status_code, 200)
            response.close()
        self.assertEqual(client.get("/static/remote/system.json?token=" + admin_raw, secure=True).status_code, 401)
        self.assertEqual(client.get("/static/remote/system.json", secure=True, HTTP_AUTHORIZATION="Bearer " + admin_raw,
                                    HTTP_ORIGIN="https://testserver").status_code, 403)
        token.revoked = True
        token.save(update_fields=["revoked"])
        self.assertEqual(client.get("/static/remote/system.json", secure=True, HTTP_AUTHORIZATION="Bearer " + admin_raw).status_code, 401)

    def test_private_topic_rejected_before_payload_parse_and_native_type_exact(self):
        contract = RuntimeContract({}, states={"tv_pairing": {"kind": "enum", "commands": True},
                                  "tv_pairing_result": {"kind": "pairing_result"},
                                  "public_volume": {"kind": "number", "max": 100, "commands": True}})
        allowed = available(contract)
        self.assertEqual(len([topic for topic in allowed if "public_volume" in topic]), 3)
        self.assertFalse(any("/tv_pairing/" in topic or topic.endswith("pairing_result/command") for topic in allowed))
        sentinel = "synthetic-private-sentinel"
        event = {"topic": "openhab/items/public_volume/command", "type": "ItemCommandEvent",
                 "payload": json.dumps({"type": "Decimal", "value": "47", "cookie": sentinel})}
        self.assertNotIn("sentinel", json.dumps(frame(contract, allowed, event)))
        self.assertIsNone(frame(contract, allowed, {**event, "type": "ItemStateEvent"}))
        self.assertIsNone(frame(contract, allowed, {"topic": "openhab/items/tv_pairing/command", "payload": object()}))

    @patch("oh_identity.streams.REVALIDATE", 0)
    def test_stream_revocation_and_disconnect_cleanup(self):
        token, _ = issue_token(self.admin, "stream")
        snapshot = {"user": self.admin.pk, "revision": self.admin.auth_revision, "token": str(token.pk), "security_revision": get_state().revision}
        contract = self.adapter.contract

        async def check():
            upstream = Upstream()
            live = RuntimeAdapter(contract, upstream)
            released = []
            frames = stream(live, "remote", snapshot, release=lambda: released.append(True))
            self.assertIn(b'"loc_den_source":"player"', await anext(frames))
            upstream.send(native_event("loc_den_source", "off"))
            self.assertIn(b'"value\\":\\"off', await anext(frames))
            await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=True)
            upstream.send(native_event("loc_den_source", "player"))
            with self.assertRaises(StopAsyncIteration):
                await anext(frames)
            self.assertEqual(released, [True])
            token.revoked = False
            await sync_to_async(token.save)(update_fields=["revoked"])
            hub = hubs.hub_for(live)
            frames = stream(live, "remote", snapshot, release=lambda: released.append(True))
            await anext(frames)
            pending = asyncio.create_task(anext(frames))
            await asyncio.sleep(.03)
            self.assertEqual(hub.status()["streams"]["remote"], 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            # A cancelled client leaves the hub; the shared upstream stays for others.
            self.assertEqual((hub.status()["streams"]["remote"], released), (0, [True, True]))
            self.assertEqual(len(upstream.connections), 1)
        async_to_sync(check)()

    @patch("oh_identity.streams.REVALIDATE", 0)
    @patch("oh_identity.hub.LINGER", 0)
    def test_one_real_wildcard_upstream_is_read_once_and_closed_with_its_last_stream(self):
        token, _ = issue_token(self.admin, "multi-stream")
        snapshot = {"user": self.admin.pk, "revision": self.admin.auth_revision, "token": str(token.pk), "security_revision": get_state().revision}
        contract = RuntimeContract({"den": {"source_item": "loc_den_source", "sources": ["player"]}},
                                   states={"public_volume": {"kind": "number", "max": 100, "commands": True}})
        reads = []

        def reply(request):
            reads.append(str(request.url))
            return httpx.Response(200, json=[{"name": "loc_den_source", "state": "player"}, {"name": "public_volume", "state": "47"},
                                             {"name": "private_token", "state": "synthetic-private-sentinel"}])
        original = httpx.Client

        async def check(path):
            client = HTTPClient()
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client), \
                    patch("oh_identity.transport.httpx.Client", side_effect=lambda **options: original(transport=httpx.MockTransport(reply), **options)):
                transport = LoopbackTransport("http://127.0.0.1:8080", path)
                live = RuntimeAdapter(contract, transport)
                first, second = stream(live, "remote", snapshot), stream(live, "home", snapshot)
                opening = [await anext(first), await anext(second)]
                self.assertIn(b'"public_volume":"47"', opening[0])
                self.assertEqual(opening[1], b'data: {"type":"snapshot","view":"home","states":{"loc_den_source":"player"}}\n\n')
                self.assertNotIn(b"sentinel", b"".join(opening))
                self.assertEqual(reads, ["http://127.0.0.1:8080/rest/items?fields=name%2Cstate"])
                self.assertEqual([unquote(url.split("?topics=")[1]) for url in client.opened],
                                 ["openhab/items/*/stateupdated,openhab/items/*/statechanged,openhab/items/*/command"])
                for item, value in (("private_token", "synthetic-private-sentinel"), ("public_volume", "48")):
                    event = native_event(item, value, "statechanged", old="47")
                    await client.queue.put(("data: " + json.dumps(event) + "\n\n").encode())
                self.assertIn(b"48", await anext(first))
                await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=True)
                await client.queue.put(("data: " + json.dumps(native_event("public_volume", "49", "statechanged", old="48")) + "\n\n").encode())
                with self.assertRaises(StopAsyncIteration):
                    await anext(first)
                self.assertEqual(client.closed, [])
                with patch("oh_identity.streams.HEARTBEAT", .01), self.assertRaises(StopAsyncIteration):
                    await anext(second)
                for _ in range(100):
                    if client.exited:
                        break
                    await asyncio.sleep(.01)
                self.assertCountEqual(client.closed, client.opened)
                self.assertTrue(client.exited)
                transport.close()
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            path = Path(temporary) / "upstream-bearer"
            path.write_text("synthetic-not-used")
            path.chmod(0o600)
            async_to_sync(check)(path)

    def test_busy_stream_checks_at_most_once_per_interval_and_fences_quiet_heartbeats(self):
        async def check():
            upstream = Upstream()
            live = RuntimeAdapter(self.adapter.contract, upstream)
            frames = stream(live, "remote", {})
            clock = [1000.0]
            with patch("oh_identity.streams.valid", return_value=True) as valid, \
                    patch("oh_identity.streams.clock", side_effect=lambda: clock[0]):
                await anext(frames)
                for index in range(100):
                    clock[0] = 1000.0 + index * .05
                    upstream.send(native_event("loc_den_source", "player"))
                    self.assertIn(b"player", await anext(frames))
                # One opening check, then one per elapsed second of frames.
                self.assertEqual(valid.call_count, 1 + 4)
                await frames.aclose()

            frames = stream(live, "remote", {})
            with patch("oh_identity.streams.HEARTBEAT", .01), patch("oh_identity.streams.valid", side_effect=[True, False]) as valid:
                await anext(frames)
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(anext(frames), .5)
                self.assertEqual(valid.call_count, 2)
            self.assertEqual(hubs.hub_for(live).status()["streams"]["remote"], 0)
        async_to_sync(check)()
