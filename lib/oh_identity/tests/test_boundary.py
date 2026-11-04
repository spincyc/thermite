"""Adversarial stream, private-asset and concurrent admin invariants."""

import asyncio
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import close_old_connections
from django.http import FileResponse
from django.test import Client, TransactionTestCase, override_settings

from oh_routes import ALIASES, PAGES
from oh_identity.models import PersonalToken, User
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import Problem, bootstrap, issue_token, save_user
from oh_identity.security import get_state
from oh_identity.streams import frame, stream, topics_for
from oh_identity.tests.test_core import FakeRuntime, PASSPHRASE, adapter
from oh_identity.tests.test_transport import HTTPClient, many_topics
from oh_identity.transport import LoopbackTransport


class StreamTransport(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue()
        self.closed = False

    async def events(self, topics):
        try:
            while True:
                event = await self.queue.get()
                if event is None:
                    return
                yield event
        finally:
            self.closed = True


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
                with self.subTest(path=path, method=method), patch("oh_identity.views.FileResponse", wraps=FileResponse) as files:
                    response = getattr(client, method)(path, secure=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("Location", response)
                    self.assertEqual(response["Content-Length"], str((root / name).stat().st_size))
                    self.assertEqual(response["Content-Type"], "text/html")
                    opened = files.call_args.args[0]
                    data = b"".join(response.streaming_content)
                    self.assertEqual(data, b"" if method == "head" else (root / name).read_bytes())
                    response.close()
                    self.assertTrue(opened.closed, "clean asset response retained its file")
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
        contract = RuntimeContract({}, states={"tv_pairing": {"kind": "enum"},
                                  "tv_pairing_result": {"kind": "pairing_result"},
                                  "public_volume": {"kind": "number", "max": 100, "commands": True}})
        allowed = topics_for(contract, "openhab/items/public_volume/*")
        self.assertEqual(len(allowed), 3)
        self.assertFalse(any("pairing" in item for item in allowed))
        with self.assertRaises(Problem):
            topics_for(contract, "openhab/items/tv_pairing/command")
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
        topics = topics_for(contract, "openhab/items/loc_den_source/stateupdated")

        async def check():
            transport = StreamTransport()
            live = RuntimeAdapter(contract, transport)
            frames = stream(live, topics, snapshot)
            await transport.queue.put({"topic": topics[0], "type": "ItemStateUpdatedEvent",
                                       "payload": json.dumps({"type": "String", "value": "player"})})
            first = await anext(frames)
            self.assertIn(b"player", first)
            await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=True)
            await transport.queue.put({"topic": topics[0], "type": "ItemStateUpdatedEvent",
                                       "payload": json.dumps({"type": "String", "value": "off"})})
            with self.assertRaises(StopAsyncIteration):
                await anext(frames)
            self.assertTrue(transport.closed)
            token.revoked = False
            await sync_to_async(token.save)(update_fields=["revoked"])
            transport = StreamTransport()
            frames = stream(RuntimeAdapter(contract, transport), topics, snapshot)
            pending = asyncio.create_task(anext(frames))
            await asyncio.sleep(.03)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(transport.closed)
        async_to_sync(check)()

    def test_revocation_closes_all_real_batched_transport_contexts(self):
        token, _ = issue_token(self.admin, "multi-stream")
        snapshot = {"user": self.admin.pk, "revision": self.admin.auth_revision, "token": str(token.pk), "security_revision": get_state().revision}
        topics = many_topics()
        states = {topic.split("/")[2]: {"kind": "number", "max": 100, "commands": True} for topic in topics}
        contract = RuntimeContract({}, states=states)

        async def check(path):
            client = HTTPClient()
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client):
                transport = LoopbackTransport("http://127.0.0.1:8080", path)
                frames = stream(RuntimeAdapter(contract, transport), topics, snapshot)
                event = {"topic": topics[0], "type": "ItemStateUpdatedEvent", "payload": json.dumps({"type": "Percent", "value": "47"})}
                await client.queue.put(("data: " + json.dumps(event) + "\n\n").encode())
                first = await anext(frames)
                self.assertIn(b"47", first)
                self.assertGreater(len(client.opened), 1)
                await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=True)
                with self.assertRaises(StopAsyncIteration):
                    await anext(frames)
                self.assertCountEqual(client.closed, client.opened)
                self.assertTrue(client.exited)
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            path = Path(temporary) / "upstream-bearer"
            path.write_text("synthetic-not-used")
            path.chmod(0o600)
            async_to_sync(check)(path)

    def test_busy_stream_checks_at_most_once_per_interval_and_fences_quiet_heartbeats(self):
        topics = topics_for(self.adapter.contract, "openhab/items/loc_den_source/stateupdated")
        event = {"topic": topics[0], "type": "ItemStateUpdatedEvent",
                 "payload": json.dumps({"type": "String", "value": "player"})}

        async def check():
            transport = StreamTransport()
            frames = stream(RuntimeAdapter(self.adapter.contract, transport), topics, {})
            clock = [1000.0]
            with patch("oh_identity.streams.valid", return_value=True) as valid, \
                    patch("oh_identity.streams.clock", side_effect=lambda: clock[0]):
                for index in range(100):
                    clock[0] = 1000.0 + index * .05
                    await transport.queue.put(event)
                    self.assertIn(b"player", await anext(frames))
                # One opening check, then one per elapsed second of frames.
                self.assertEqual(valid.call_count, 1 + 4)
                await frames.aclose()
            self.assertTrue(transport.closed)

            transport = StreamTransport()
            frames = stream(RuntimeAdapter(self.adapter.contract, transport), topics, {})
            with patch("oh_identity.streams.HEARTBEAT", .01), patch("oh_identity.streams.valid", side_effect=[True, False]) as valid:
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(anext(frames), .5)
                self.assertEqual(valid.call_count, 2)
            self.assertTrue(transport.closed)
        async_to_sync(check)()
