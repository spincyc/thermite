"""Requests through the plain-HTTP Guest listener, through Django's ASGI
handler as the gateway serves them, and the listener's sockets in the
gateway's hypercorn server."""

import asyncio
import gzip
import json
import re
import socket
import tempfile
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import AsyncClient, Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import resolve

from oh_access import service
from oh_access.guest import SCOPE, Listener
from oh_features.remote import FILES
from oh_features.remote_system import ADMIN_ASSETS
from oh_identity import guest, streams
from oh_identity.asgi import application
from oh_identity.batches import commands
from oh_identity.models import RoomPolicy, SecurityState
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, FakeRuntime, Policy, adapter
from oh_identity.tests.test_gateway_server import HOST as LOOPBACK, EventRuntime, Running, Seen
from oh_identity.urls import urlpatterns
from oh_routes import asset_version

LISTENER = Listener(frozenset({"http://192.0.2.10:13600", "http://[2001:db8::10]:13600",
                               "http://remote.guest.example:13600"}), "https://house.example.test:13601")
HOST = "192.0.2.10:13600"
ORIGIN = "http://" + HOST
WEB = Path(__file__).resolve().parents[3] / "web/remote"
#: Pages that stay on the HTTPS site, as routes and as files.
REFUSED_PAGES = ("/security/", "/account/", "/cameras/", "/pairing/", "/site/", "/system/", "/room-settings/",
                 "/users/", "/security", "/account", "/cameras", "/pairing")
REFUSED_FILES = ("security.html", "access.html", "access-ui.js", "access.css", "users.html", "settings.html",
                 "system.html", "system.js", "system.json", "pair.html", "pair.js", "pair.css", "cameras.html",
                 "cameras.js", "cameras.css", "cameras.json", "topology.html", "topology.js", "topology.css",
                 "topology.json", "README.md", "../secrets")
#: The files a page's markup loads.
LOADS = r'<(?:script|link|img)\b[^>]*?\b(?:src|href)="([^"#]+)"'


class GuestClient(AsyncClient):
    """Requests as the guest listener's sockets hand them to the application:
    the listener's mark in the scope, a peer on the guest network, plain HTTP."""

    def __init__(self, host=HOST):
        super().__init__(enforce_csrf_checks=True)
        self.host = host

    def _base_scope(self, **request):
        scope = super()._base_scope(**request)
        # This client never stores cookies; a test sends its own Cookie header.
        headers = [(name, value) for name, value in scope["headers"] if name != b"host" and (name, value) != (b"cookie", b"")]
        if self.host is not None:
            headers.append((b"host", self.host.encode()))
        return {**scope, "headers": headers, "client": ["192.0.2.50", 50123], "server": ("192.0.2.10", 13600),
                "scheme": "http", SCOPE: LISTENER}


def call(method, path, data=None, *, client=None, content_type="application/json", origin=ORIGIN, **headers):
    client = client or GuestClient()
    if method in ("post", "put", "delete"):
        headers = {**({"Origin": origin} if origin else {}), **headers}
        options = {"data": json.dumps(data) if not isinstance(data, str) else data, "content_type": content_type}
    else:
        options = {"data": data} if data else {}
    return async_to_sync(getattr(client, method))(path, headers=headers, **options)


def body(response):
    return json.loads(response.content)


def guest_application(answer_status):
    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": answer_status, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return application


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(message):
    pass


class Listening(TestCase):
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": False, "revision": 1})
        temporary = tempfile.TemporaryDirectory(dir=settings.STATE)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in FILES:
            (root / name).write_text("fixture " + name)
        self.adapter = adapter()
        self.adapter.contract.assets = {name: {"private": name.endswith(".html")} for name in FILES}
        self.adapter.contract.asset_root = root
        install_adapter(self.adapter)

    def https(self):
        return Client(enforce_csrf_checks=True)

    def test_home_remotes_and_their_files_load_and_nothing_else_is_found(self):
        for path in ("/", "/remotes/", "/static/remote/", *("/static/remote/" + name for name in guest.ASSETS)):
            with self.subTest(path=path):
                response = call("get", path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(call("head", path).status_code, 200)
        for path, target in (("/remotes", "/remotes/"), ("/overview", "/"), ("/overview/", "/")):
            with self.subTest(path=path):
                self.assertEqual(call("get", path)["Location"], target)
        refused = [*REFUSED_PAGES, *("/static/remote/" + name for name in REFUSED_FILES), "/missing", "/unifi/media/x",
                   "/api/v1/session", "/api/v1/tokens", "/api/v1/users", "/api/v1/users/1", "/rest/items",
                   "/rest/items/tv_pairing", "/rest/items?metadata=pairing"]
        for path in refused:
            with self.subTest(path=path):
                response = call("get", path)
                self.assertEqual((response.status_code, body(response)["error"]), (404, "not_found"))
                self.assertEqual(response["Cache-Control"], "no-store")
        for path in ("/api/v1/session", "/api/v1/token-login", "/api/v1/logout", "/api/v1/tokens", "/api/v1/users",
                     "/rest/items/tv_pairing", "/unifi/media/x"):
            with self.subTest(path=path):
                self.assertEqual(call("post", path, {"username": "owner", "password": PASSPHRASE}).status_code, 404)
        # Known routes refuse other methods with the methods guests may use.
        for method, path, allow in (("put", "/api/v1/rooms/den/policy", "GET"), ("post", "/api/v1/security", "GET"),
                                    ("put", "/api/v1/rooms/den/lock", None), ("delete", "/api/v1/tokens/1", None),
                                    ("post", "/rest/items/loc_den_source/state", "GET")):
            with self.subTest(method=method, path=path):
                response = call(method, path, {"revision": 0, "locked": True, "enabled": True})
                self.assertEqual(response.status_code, 405 if allow else 404)
                if allow:
                    self.assertEqual(response["Allow"], allow)
        self.assertEqual(self.adapter.transport.commands, [])
        self.assertEqual(self.https().get("/security/", secure=True).status_code, 200)

    def test_versioned_files_load_and_cache_as_on_https(self):
        root = self.adapter.contract.asset_root
        for name in sorted(guest.ASSETS):
            data = (root / name).read_bytes()
            version, gated = asset_version(data), name in ("house.js", "access-config.js") or name.endswith((".html", ".json"))
            with self.subTest(name=name):
                response = call("get", f"/static/remote/{name}?v={version}", **{"Accept-Encoding": "gzip"})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.cookies)
                stale = call("get", f"/static/remote/{name}?v=0123456789abcdef")
                if gated:
                    self.assertEqual((response["Cache-Control"], stale["Cache-Control"]), ("no-store", "no-store"))
                    self.assertFalse(response.has_header("ETag") or response.has_header("Content-Encoding"))
                    continue
                self.assertEqual(response["Cache-Control"], "public, max-age=31536000, immutable")
                self.assertEqual(stale["Cache-Control"], "no-cache")
                text = not name.endswith(".png")
                self.assertEqual(response.content, gzip.compress(data, mtime=0) if text else data)
                tag = response["ETag"]
                self.assertEqual(tag, f'"{version}-gzip"' if text else f'"{version}"')
                again = call("get", f"/static/remote/{name}?v={version}", **{"Accept-Encoding": "gzip", "If-None-Match": tag})
                self.assertEqual((again.status_code, again.content), (304, b""))
        # A valid hash opens nothing the listener refuses.
        for name in ("cameras.js", "access.html", "system.json"):
            with self.subTest(name=name):
                version = asset_version((root / name).read_bytes())
                response = call("get", f"/static/remote/{name}?v={version}")
                self.assertEqual((response.status_code, body(response)["error"]), (404, "not_found"))

    def test_reads_and_events_reach_their_views(self):
        self.assertEqual(call("get", "/rest/items/loc_den_source/state").content, b"player")
        self.assertEqual(call("get", "/rest/items/unrelated_secret/state").status_code, 404)
        # The Home and Remotes streams; Pairing's stays on the HTTPS site.
        for view in ("remote", "home"):
            response = call("get", "/api/v1/events/" + view)
            self.assertEqual((response.status_code, response["Content-Type"]), (200, "text/event-stream"))
        self.assertEqual(call("get", "/api/v1/events/pair").status_code, 404)
        self.assertEqual(call("get", "/rest/events", {"topics": "openhab/items/loc_den_source/stateupdated"}).status_code, 404)
        SecurityState.objects.filter(pk=1).update(enabled=True)
        policy = body(call("get", "/api/v1/rooms/den/policy"))
        self.assertEqual((policy["room"], policy["can_lock"], policy["effective"]["allowed"]), ("den", False, True))

    def test_room_lists_read_every_rooms_lock_as_on_https(self):
        SecurityState.objects.filter(pk=1).update(enabled=True)
        RoomPolicy.objects.create(room_id="den", config={"locked": True})
        locks = call("get", "/api/v1/locks")
        self.assertEqual((locks.status_code, locks["Cache-Control"]), (200, "no-store"))
        self.assertEqual(body(locks), self.https().get("/api/v1/locks", secure=True).json())
        self.assertEqual({room: lock["locked"] for room, lock in body(locks)["rooms"].items()}, {"den": True, "desk": False})
        self.assertEqual(call("head", "/api/v1/locks").status_code, 405)
        self.assertEqual(call("post", "/api/v1/locks", {}).status_code, 405)

    def test_every_answer_is_a_guest_in_both_modes_and_cameras_stay_closed(self):
        for enabled in (False, True):
            SecurityState.objects.filter(pk=1).update(enabled=enabled)
            with self.subTest(enabled=enabled):
                me = body(call("get", "/api/v1/me"))
                self.assertEqual((me["id"], me["is_guest"], me["auth_enabled"], me["can_edit"]), (0, True, enabled, False))
                self.assertEqual("camera.view" in me["permissions"], not enabled)
                for path in ("/unifi/media/camera/snapshot", "/cameras/", "/static/remote/cameras.json"):
                    self.assertEqual(call("get", path).status_code, 404)
                status = body(call("get", "/api/v1/security"))
                self.assertEqual(status["enabled"], enabled)
                self.assertNotIn("csrf", status)

    def test_access_config_makes_the_page_guest_only_with_the_secure_site(self):
        script = call("get", "/static/remote/access-config.js").content.decode()
        self.assertEqual(script, 'globalThis.THERMITE_ACCESS_CONFIG = Object.freeze('
                                 '{"version":1,"mode":"guest","secure":"https://house.example.test:13601"});\n')
        self.assertIn(b'"mode":"authenticated"', self.https().get("/static/remote/access-config.js", secure=True).content)

    def test_credentials_are_refused_and_never_used(self):
        SecurityState.objects.filter(pk=1).update(enabled=True)
        owner = bootstrap("owner", PASSPHRASE)
        _, token = issue_token(owner, "guest listener fixture")
        browser = self.https()
        browser.get("/api/v1/session", secure=True)
        signed_in = browser.post("/api/v1/session", json.dumps({"username": "owner", "password": PASSPHRASE}),
                                 content_type="application/json", secure=True, HTTP_ORIGIN="https://testserver",
                                 HTTP_X_CSRFTOKEN=browser.cookies[settings.CSRF_COOKIE_NAME].value)
        self.assertEqual(signed_in.status_code, 200)
        session = browser.cookies[settings.SESSION_COOKIE_NAME].value
        for cookie in (settings.SESSION_COOKIE_NAME + "=" + session, settings.CSRF_COOKIE_NAME + "=x",
                       "other=1; " + settings.SESSION_COOKIE_NAME + "=" + session):
            for method, path in (("get", "/api/v1/me"), ("post", "/api/v1/rooms/den/commands"), ("get", "/")):
                with self.subTest(cookie=cookie, path=path):
                    response = call(method, path, {"action": "source", "value": "off"}, Cookie=cookie)
                    self.assertEqual((response.status_code, body(response)["error"]), (403, "secure_connection_required"))
        for header in ("Bearer " + token, "Bearer invalid", "Basic b3duZXI6eA=="):
            with self.subTest(header=header[:6]):
                response = call("get", "/api/v1/me", Authorization=header)
                self.assertEqual((response.status_code, body(response)["error"]), (403, "secure_connection_required"))
        # Another service's cookie on this host is neither refused nor read.
        unrelated = "grafana_session=fixture"
        me = body(call("get", "/api/v1/me", Cookie=unrelated))
        self.assertEqual((me["id"], me["is_guest"]), (0, True))
        self.assertEqual(self.adapter.transport.commands, [])

    def test_host_must_name_the_listener_with_its_port(self):
        for host in (HOST, "[2001:db8::10]:13600", "remote.guest.example:13600", "REMOTE.Guest.Example:13600"):
            with self.subTest(host=host):
                self.assertEqual(call("get", "/api/v1/me", client=GuestClient(host)).status_code, 200)
        for host in ("attacker.example:13600", "192.0.2.10", "192.0.2.10:13601", "house.example.test:13601",
                     "localhost:13600", "192.0.2.10:13600.attacker.example", "", None):
            with self.subTest(host=host):
                response = call("get", "/api/v1/me", client=GuestClient(host))
                self.assertEqual((response.status_code, body(response)["error"]), (400, "invalid_host"))
                response = call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}, client=GuestClient(host))
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.adapter.transport.commands, [])

    def test_controls_need_the_listeners_own_origin_and_json(self):
        command = {"action": "source", "value": "off"}
        for origin in (None, "null", "http://attacker.example", "https://192.0.2.10:13600", "http://192.0.2.10:13601",
                       "http://192.0.2.10", ORIGIN + "/", "https://house.example.test:13601"):
            with self.subTest(origin=origin):
                response = call("post", "/api/v1/rooms/den/commands", command, origin=origin)
                self.assertEqual((response.status_code, body(response)["error"]), (403, "invalid_origin"))
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
            with self.subTest(content_type=content_type):
                response = call("post", "/api/v1/rooms/den/commands", "{}", content_type=content_type)
                self.assertEqual((response.status_code, body(response)["error"]), (415, "json_required"))
        self.assertEqual(self.adapter.transport.commands, [])
        for origin in LISTENER.origins:
            with self.subTest(origin=origin):
                self.assertEqual(call("post", "/api/v1/rooms/den/commands", command, origin=origin).status_code, 202)
        self.assertEqual(self.adapter.transport.commands, [("loc_den_source", "off")] * 3)

    def test_guests_report_telemetry_with_the_listeners_origin_and_json(self):
        from oh_access import metrics
        from oh_identity import telemetry
        self.addCleanup(setattr, telemetry, "limits", telemetry.limits)
        telemetry.limits = telemetry.Limits(per_peer=100)
        report = {"client": "web", "protocol": "http/1.1", "events": {"policy_timeout": {"count": 2, "waits": [8.5]}}}
        events = metrics.CLIENT_EVENTS.labels("web", "policy_timeout")
        guests = metrics.REQUESTS.labels("guest", "other", "2xx")
        before, requests = events._value.get(), guests._value.get()
        for origin in (None, "http://attacker.example", "https://house.example.test:13601"):
            with self.subTest(origin=origin):
                response = call("post", "/api/v1/telemetry", report, origin=origin)
                self.assertEqual((response.status_code, body(response)["error"]), (403, "invalid_origin"))
        refused = call("post", "/api/v1/telemetry", json.dumps(report), content_type="text/plain")
        self.assertEqual((refused.status_code, body(refused)["error"]), (415, "json_required"))
        self.assertEqual(call("get", "/api/v1/telemetry").status_code, 405)
        self.assertEqual(events._value.get(), before)
        answer = call("post", "/api/v1/telemetry", report)
        self.assertEqual(answer.status_code, 204)
        self.assertFalse(answer.cookies)
        self.assertEqual(events._value.get(), before + 2)
        # Django's test client calls the application, not Metered; count one through it.
        application = metrics.Metered(guest_application(answer_status=204))
        async_to_sync(application)({"type": "http", "path": "/api/v1/telemetry", SCOPE: LISTENER},
                                   _receive, _discard)
        self.assertEqual(guests._value.get(), requests + 1)

    def test_room_locks_refuse_guest_commands_as_on_https(self):
        SecurityState.objects.filter(pk=1).update(enabled=True)
        RoomPolicy.objects.create(room_id="den", config={"locked": True})
        command = {"action": "source", "value": "off"}
        locked = call("post", "/api/v1/rooms/den/commands", command)
        self.assertEqual((locked.status_code, body(locked)["error"]), (423, "room_locked"))
        self.assertEqual(call("post", "/api/v1/rooms/desk/commands", {"action": "source", "value": "player"}).status_code, 202)
        self.assertEqual(call("post", "/api/v1/rooms/den/commands", {"action": "volume", "value": 40}).status_code, 423)
        self.assertEqual(self.adapter.transport.commands, [("loc_desk_source", "player")])

    def test_no_answer_sets_a_cookie_and_headers_match_https_without_its_extras(self):
        answers = [call("get", path) for path in ("/", "/remotes/", "/static/remote/remote.js", "/api/v1/me",
                                                  "/api/v1/security", "/missing", "/security/")]
        answers += [call("get", "/api/v1/me", client=GuestClient("attacker.example:13600")),
                    call("get", "/api/v1/me", Cookie=settings.SESSION_COOKIE_NAME + "=x"),
                    call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}),
                    call("post", "/api/v1/rooms/den/commands", {"action": "source", "value": "off"}, origin=None)]
        for response in answers:
            with self.subTest(status=response.status_code, url=getattr(response, "request", {}).get("path")):
                self.assertNotIn("Set-Cookie", str(response.serialize_headers()))
                self.assertFalse(response.cookies)
                self.assertTrue(response["Content-Security-Policy"].startswith("default-src 'self'"))
                self.assertEqual(response["X-Frame-Options"], "DENY")
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response["Referrer-Policy"], "same-origin")
                self.assertFalse(response.has_header("Strict-Transport-Security"))
                self.assertFalse(response.has_header("Cross-Origin-Opener-Policy"))
                self.assertNotEqual(response.status_code, 301)
        self.assertEqual(answers[0]["Cache-Control"], "no-store")
        self.assertEqual(answers[2]["Cache-Control"], "no-cache")
        https = self.https().get("/api/v1/security", secure=True)
        self.assertIn(settings.CSRF_COOKIE_NAME, https.cookies)
        self.assertIn("csrf", https.json())
        self.assertEqual(https["Cross-Origin-Opener-Policy"], "same-origin")

    def test_https_requests_are_unaffected(self):
        # A header cannot mark a request, and plain HTTP elsewhere still redirects.
        forged = self.https().get("/api/v1/me", secure=True, HTTP_THERMITE_GUEST_HTTP="1", **{SCOPE: LISTENER})
        self.assertEqual(forged.status_code, 200)
        self.assertEqual(self.https().get("/security/", secure=True).status_code, 200)
        self.assertEqual(self.https().get("/api/v1/me").status_code, 301)
        csrf = self.https().post("/api/v1/rooms/den/commands", json.dumps({"action": "source", "value": "off"}),
                                 content_type="application/json", secure=True, HTTP_ORIGIN="https://testserver")
        self.assertEqual((csrf.status_code, csrf.json()["error"]), (403, "csrf_failed"))


class AllOff(TransactionTestCase):
    def setUp(self):
        commands.shutdown()
        self.addCleanup(commands.shutdown)
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True, "revision": 1})
        rooms = {name: {"source_item": "loc_" + name + "_source", "sources": ["player"]} for name in ("den", "desk")}
        self.adapter = RuntimeAdapter(RuntimeContract(rooms), FakeRuntime(), Policy())
        install_adapter(self.adapter)

    def test_all_off_turns_off_unlocked_rooms(self):
        RoomPolicy.objects.create(room_id="desk", config={"locked": True})
        response = call("post", "/api/v1/all-off", {})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(body(response), {"accepted": ["den"], "skipped": [{"room": "desk", "reason": "room_locked"}]})
        self.assertEqual(self.adapter.transport.commands, [("loc_den_source", "off")])
        self.assertEqual(call("post", "/api/v1/all-off", {}, origin="http://attacker.example").status_code, 403)


class Allowlist(SimpleTestCase):
    def test_routes_are_real_and_files_are_what_guest_pages_load(self):
        routes = {str(pattern.pattern) for pattern in urlpatterns}
        self.assertLessEqual(set(guest.ROUTES), routes)
        self.assertLessEqual(guest.ASSETS, set(FILES))
        self.assertFalse(guest.ASSETS & (ADMIN_ASSETS | set(REFUSED_FILES)))
        marked = set()
        for page in ("home.html", "index.html"):
            marked |= set(re.findall(LOADS, (WEB / page).read_text()))
        loaded = set(marked)
        for sheet in ("theme.css", "shell.css", "remote.css", "home.css"):
            loaded |= set(re.findall(r'url\("([^"#]+)"\)', (WEB / sheet).read_text()))
        # Scripts fetch these themselves.
        loaded |= {"activity.json", "diagnostics.json", "mark.svg"}
        self.assertEqual(loaded | {"home.html", "index.html"}, set(guest.ASSETS))
        # Generated pages, and the manifest they link, load each file as
        # /static/remote/<name>?v=<hash of its bytes>. The query never reaches
        # the allowlist: the route's bare name does, and the listener admits it.
        import oh_render
        files = oh_render.render({"version": 1, "rooms": {}, "devices": []},
                                 system_metadata={"version": 1, "runtime": {"version": "test"}, "tools": []})

        def content(name):
            value = files["html/remote/" + name]
            return value.encode() if isinstance(value, str) else value
        served = set()
        for page in ("home.html", "index.html"):
            urls = re.findall(LOADS, content(page).decode())
            manifest = next(url for url in urls if urlsplit(url).path.endswith("/manifest.webmanifest"))
            urls += [urljoin(manifest, icon["src"]) for icon in json.loads(content("manifest.webmanifest"))["icons"]]
            for url in urls:
                with self.subTest(page=page, url=url):
                    target = urlsplit(url)
                    match = resolve(target.path)
                    self.assertEqual(match.route, "static/remote/<str:name>")
                    self.assertIn(match.kwargs["name"], guest.ASSETS)
                    self.assertEqual(parse_qs(target.query), {"v": [asset_version(content(match.kwargs["name"]))]})
                    self.assertIsNone(guest.refusal(RequestFactory().get(url, HTTP_HOST=HOST), LISTENER))
                    served.add(match.kwargs["name"])
        self.assertEqual(served, marked)


class Serving:
    """service._serve on its own thread and loop, as the unit runs it."""

    def __init__(self, pairs):
        for _, config in pairs:
            config.graceful_timeout = 1
        self.bound = [(application, config, config.create_sockets()) for application, config in pairs]

    def __enter__(self):
        ready = threading.Event()

        async def main():
            self.loop, self.stop = asyncio.get_running_loop(), asyncio.Event()
            ready.set()
            await service._serve(self.bound, stop=self.stop)
        self.thread = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
        self.thread.start()
        ready.wait(5)
        return self

    def __exit__(self, *_):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)
        assert not self.thread.is_alive()


def fetch(port, source, address="127.0.0.1", path="/"):
    """(status, body) of one HTTP/1.1 request from source, or None when the
    connection is closed unanswered."""
    with socket.create_connection((address, port), timeout=5, source_address=(source, 0)) as connection:
        connection.sendall(f"GET {path} HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n".encode())
        answer = b""
        try:
            while chunk := connection.recv(65536):
                answer += chunk
        except ConnectionResetError:
            pass
    if not answer:
        return None
    head, _, content = answer.partition(b"\r\n\r\n")
    return int(head.split()[1]), content


def listening(port, source, address="127.0.0.1"):
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection((address, port), timeout=1, source_address=(source, 0)).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class Sockets(SimpleTestCase):
    """The Guest listeners beside the gateway's own in one hypercorn loop."""

    @staticmethod
    def port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def setUp(self):
        self.release = threading.Event()

        async def app(scope, receive, send):
            if scope["path"] == "/hold":
                while not self.release.is_set():
                    await asyncio.sleep(0.02)
            body = json.dumps([scope["scheme"], scope.get(SCOPE) is not None, scope["client"][0]]).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
        self.app = app
        self.main, self.guest = self.port(), self.port()
        self.config = {"allowed_hosts": ["house.example.test", "192.0.2.10"], "port": self.main,
                       "guest_http": {"port": self.guest, "listen": ["127.0.0.1", "127.0.0.5"], "clients": ["127.0.0.2/32"],
                                      "names": ["remote.guest.example"]}}

    def listeners(self, admission):
        return [(admission, service.listener("127.0.0.1", self.main)), *service.guest_listeners(self.config, admission)]

    def test_one_plain_listener_per_address_marked_for_the_secure_site(self):
        self.assertEqual(service.guest_listeners({**self.config, "guest_http": None}, None), [])
        admission = service.Admission(self.app)
        pairs = service.guest_listeners(self.config, admission)
        self.assertEqual([config.bind for _, config in pairs], [[f"127.0.0.1:{self.guest}"], [f"127.0.0.5:{self.guest}"]])
        self.assertFalse(any(config.ssl_enabled for _, config in pairs))
        defaults = service.listener("127.0.0.1", self.main)
        for _, config in pairs:
            self.assertEqual((config.keep_alive_timeout, config.graceful_timeout, config.accesslog, config.include_server_header),
                             (defaults.keep_alive_timeout, defaults.graceful_timeout, None, False))
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope[SCOPE])
        application = service.guest_listeners(self.config, inner)[0][0]
        self.assertIs(pairs[0][0], pairs[1][0])
        asyncio.run(application({"type": "http"}, None, None))
        self.assertEqual(seen[0].secure, f"https://house.example.test:{self.main}")
        self.assertEqual(seen[0].origins, {f"http://127.0.0.1:{self.guest}", f"http://127.0.0.5:{self.guest}",
                                           f"http://remote.guest.example:{self.guest}"})
        application = service.guest_listeners({**self.config, "allowed_hosts": ["[2001:db8::10]"]}, inner)[0][0]
        asyncio.run(application({"type": "http"}, None, None))
        self.assertEqual(seen[1].secure, f"https://[2001:db8::10]:{self.main}")

    def test_the_sign_in_link_names_the_configured_https_port(self):
        # host.yaml's gateway.port moved HTTPS off 13601, and Guests took it.
        config = {**self.config, "port": 16301, "guest_http": {**self.config["guest_http"], "port": 13601}}
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope[SCOPE])
        pairs = service.guest_listeners(config, inner)
        self.assertEqual([listener.bind for _, listener in pairs], [["127.0.0.1:13601"], ["127.0.0.5:13601"]])
        asyncio.run(pairs[0][0]({"type": "http"}, None, None))
        self.assertEqual(seen[0].secure, "https://house.example.test:16301")
        script = guest.access_config(type("Request", (), {"scope": {SCOPE: seen[0]}})())
        self.assertEqual(script, 'globalThis.THERMITE_ACCESS_CONFIG = Object.freeze('
                                 '{"version":1,"mode":"guest","secure":"https://house.example.test:16301"});\n')

    def test_guest_listeners_bind_only_their_addresses_and_close_other_peers_unread(self):
        with self.assertLogs("oh_access", "WARNING") as logs, Serving(self.listeners(service.Admission(self.app))):
            listening(self.main, "127.0.0.1")
            for address in ("127.0.0.1", "127.0.0.5"):
                listening(self.guest, "127.0.0.2", address)
            results = {"guest": fetch(self.guest, "127.0.0.2"), "second address": fetch(self.guest, "127.0.0.2", "127.0.0.5"),
                       "outside": fetch(self.guest, "127.0.0.3"), "listener itself": fetch(self.guest, "127.0.0.1"),
                       "main": fetch(self.main, "127.0.0.3")}
            with self.assertRaises(ConnectionRefusedError):
                fetch(self.guest, "127.0.0.2", "127.0.0.4")
        self.assertEqual(results, {"guest": (200, b'["http", true, "127.0.0.2"]'),
                                   "second address": (200, b'["http", true, "127.0.0.2"]'),
                                   "outside": None, "listener itself": None, "main": (200, b'["http", false, "127.0.0.3"]')})
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Guest HTTP listener closed 1 connection(s) from outside its client networks"])

    def test_guests_share_the_gateways_request_budget(self):
        admission = service.Admission(self.app, limit=1)
        with Serving(self.listeners(admission)):
            listening(self.main, "127.0.0.1")
            listening(self.guest, "127.0.0.2")
            answers = []
            held = threading.Thread(target=lambda: answers.append(fetch(self.main, "127.0.0.1", path="/hold")))
            held.start()
            deadline = time.monotonic() + 5
            while not admission.active and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(fetch(self.guest, "127.0.0.2"), (503, b"Service Unavailable"))
            self.release.set()
            held.join(5)
            self.assertEqual(answers, [(200, b'["http", false, "127.0.0.1"]')])
            self.assertEqual(fetch(self.guest, "127.0.0.2")[0], 200)
        self.assertEqual(admission.active, 0)

    def test_a_guest_listener_that_cannot_bind_stops_startup(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.5", self.guest))
            taken.listen()
            with self.assertRaises(SystemExit) as stopped, self.assertLogs("hypercorn.error", "ERROR") as logs:
                service._run_server(self.listeners(service.Admission(self.app)))
        self.assertEqual(stopped.exception.code, service.STARTUP_FAILURE)
        self.assertIn("Access server could not bind", logs.output[0])


#: The Guest browser's address: the listener closes connections from its own.
PEER = "127.0.0.2"


class GuestListener(Running):
    """The plain-HTTP Guest listener alone on loopback, bound as service.serve
    binds it, admitting the one peer address PEER."""

    def __init__(self, application, *, graceful=2):
        with socket.socket() as probe:
            probe.bind((LOOPBACK, 0))
            self.port = probe.getsockname()[1]
        config = {"allowed_hosts": [LOOPBACK], "port": self.port + 1,
                  "guest_http": {"port": self.port, "listen": [LOOPBACK], "clients": [PEER + "/32"], "names": []}}
        (marked, listener), = service.guest_listeners(config, application)
        listener.graceful_timeout = graceful
        self.config, self.sockets = listener, listener.create_sockets()
        self.bound = [(marked, listener, self.sockets)]
        self.drained = threading.Event()
        self.started = threading.Event()


@override_settings(ALLOWED_HOSTS=[LOOPBACK])
class GuestHttp11Gateway(TransactionTestCase):
    """A Guest's browser on the plain-HTTP listener: HTTP/1.1, and at most six
    connections to the origin shared by all of its tabs, each event stream
    holding one for as long as its page is shown."""

    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        self.runtime = EventRuntime()
        install_adapter(RuntimeAdapter(adapter().contract, self.runtime, Policy()))

    def test_one_stream_per_page_leaves_a_browsers_six_connections_room_for_requests(self):
        # Three Remotes tabs, Home and Topology: one stream each
        # (web/remote/backend.js). The remote's earlier three streams a page
        # filled all six connections with two Remotes tabs.
        pages = ("remote", "remote", "remote", "home", "home")
        seen = Seen(application)

        async def exercise(port):
            limits = httpx.Limits(max_connections=6, max_keepalive_connections=6)
            transport = httpx.AsyncHTTPTransport(local_address=PEER, limits=limits)
            async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=10,
                                         base_url=f"http://{LOOPBACK}:{port}") as client, AsyncExitStack() as stack:
                held = [await stack.enter_async_context(client.stream("GET", "/api/v1/events/" + view)) for view in pages]
                self.assertEqual({(each.status_code, each.http_version) for each in held}, {(200, "HTTP/1.1")})
                # A page keeps reading its stream; a dropped line iterator would close it.
                lines = [each.aiter_lines() for each in held]

                async def snapshot(page):
                    while not (line := await anext(page)).startswith("data:"):
                        pass
                    return json.loads(line[5:])
                frames = await asyncio.gather(*(snapshot(page) for page in lines))
                self.assertEqual([(frame["type"], frame["view"]) for frame in frames], [("snapshot", view) for view in pages])
                self.assertEqual(streams.slots.count(), len(pages))
                # Every page's reads and a command share the sixth connection.
                started = time.monotonic()
                answers = await asyncio.gather(*(client.get(path) for path in ("/api/v1/me", "/api/v1/locks", "/api/v1/rooms/den/policy") * 3),
                                               client.post("/api/v1/rooms/den/commands", json={"action": "source", "value": "player"},
                                                           headers={"Origin": f"http://{LOOPBACK}:{port}"}))
                elapsed = time.monotonic() - started
                # A sixth stream takes the last connection: nothing else is served.
                await stack.enter_async_context(client.stream("GET", "/api/v1/events/home"))
                with self.assertRaises(httpx.PoolTimeout):
                    await client.get("/api/v1/locks", timeout=httpx.Timeout(10, pool=.5))
                self.assertEqual(streams.slots.count(), len(pages) + 1)
                del lines
                return answers, elapsed

        with GuestListener(service.Admission(seen)) as server:
            answers, elapsed = asyncio.run(exercise(server.port))
        self.assertEqual([answer.status_code for answer in answers], [200] * 9 + [202])
        self.assertEqual({answer.json().get("username") for answer in answers[:9:3]}, {"Guest"})
        self.assertLess(elapsed, 2)
        connections = {client for kind, _, client in seen.scopes if kind == "http"}
        self.assertEqual({version for kind, version, _ in seen.scopes if kind == "http"}, {"1.1"})
        self.assertEqual(len(connections), 6)
        self.assertEqual({client[0] for client in connections}, {PEER})
        # Every stream, of either view, is served from one upstream subscription.
        self.assertEqual(self.runtime.subscriptions, 1)
        self.assertEqual(streams.slots.count(), 0)
