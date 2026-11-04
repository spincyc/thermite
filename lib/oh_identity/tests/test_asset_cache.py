"""Asset caching: versioned immutable URLs, validators and HEAD."""

import asyncio
import mimetypes
import tempfile
import warnings
from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.handlers.asgi import ASGIHandler
from django.test import Client, TransactionTestCase

from oh_identity.models import SecurityState
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import bootstrap
from oh_identity.tests.test_core import PASSPHRASE, FakeRuntime
from oh_routes import asset_version

IMMUTABLE = "public, max-age=31536000, immutable"
SCRIPT = b"/* shared program */\n" + b"globalThis.answer = 42;\n" * 64
FILES = {
    "code.js": SCRIPT,
    "theme.css": b":root { color: black; }\n" * 32,
    "icon.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4,
    "access.html": b"<!doctype html><title>sign in</title>" + b"<p>static page</p>" * 32,
    "index.html": b"<!doctype html><title>remotes</title>" + b"<p>private page</p>" * 32,
    "house.js": b"globalThis.house = {};\n" * 32,
    "cameras.json": b'{"version":1,"cameras":[]}\n' * 32,
    "system.json": b'{"version":1,"tools":[]}\n' * 32,
}
#: Only static, non-secret files are ever stored or validated.
UNSHARED = ("index.html", "house.js", "cameras.json", "system.json")


class AssetCache(TransactionTestCase):
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": False})
        temporary = tempfile.TemporaryDirectory(dir=settings.STATE)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in FILES.items():
            (root / name).write_bytes(value)
        specs = {name: {} for name in FILES}
        specs["index.html"] = {"private": True}
        install_adapter(RuntimeAdapter(RuntimeContract({}, assets=specs, asset_root=root), FakeRuntime()))
        self.client = Client()

    def get(self, name, query="", method="get", **headers):
        return getattr(self.client, method)("/static/remote/" + name + query, secure=True, headers=headers)

    def test_only_the_current_version_of_a_shared_file_is_immutable(self):
        version = asset_version(SCRIPT)
        response = self.get("code.js", "?v=" + version)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], IMMUTABLE)
        self.assertEqual(response["ETag"], f'"{version}"')
        self.assertEqual(response["Content-Type"], mimetypes.guess_type("code.js")[0])
        self.assertEqual(response["Content-Length"], str(len(SCRIPT)))
        self.assertEqual(response.content, SCRIPT)
        # Security headers are unchanged by caching.
        self.assertTrue(response["Content-Security-Policy"].startswith("default-src 'self'"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        # A stale page names old bytes: it gets today's, and must ask again.
        for query in ("", "?v=0123456789abcdef", "?v=" + version.upper(), "?v=" + version + "&v=" + version,
                      "?v=" + version + "&room=den", "?version=" + version):
            with self.subTest(query=query):
                response = self.get("code.js", query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Cache-Control"], "no-cache")
                self.assertEqual(response["ETag"], f'"{version}"')
                self.assertEqual(response.content, SCRIPT)
        image = self.get("icon.png", "?v=" + asset_version(FILES["icon.png"]))
        self.assertEqual((image["Cache-Control"], image["Content-Type"]), (IMMUTABLE, "image/png"))

    def test_pages_are_never_immutable_and_revalidate_with_their_etag(self):
        version = asset_version(FILES["access.html"])
        for query in ("", "?v=" + version):
            with self.subTest(query=query):
                response = self.get("access.html", query)
                self.assertEqual(response["Cache-Control"], "no-cache")
                self.assertEqual(response["ETag"], f'"{version}"')
                self.assertEqual(response["Content-Type"], "text/html")
                self.assertEqual(response.content, FILES["access.html"])
        for method in ("get", "head"):
            for tag in (f'"{version}"', f'W/"{version}"', f'"other", "{version}"', "*"):
                with self.subTest(method=method, tag=tag):
                    response = self.get("access.html", method=method, if_none_match=tag)
                    self.assertEqual(response.status_code, 304)
                    self.assertEqual(response.content, b"")
                    self.assertEqual(response["Cache-Control"], "no-cache")
                    self.assertEqual(response["ETag"], f'"{version}"')
                    self.assertEqual(response["X-Frame-Options"], "DENY")
        changed = self.get("access.html", if_none_match='"0123456789abcdef"')
        self.assertEqual((changed.status_code, changed.content), (200, FILES["access.html"]))
        # The page's own versioned files revalidate cheaply too.
        script = self.get("code.js", "?v=" + asset_version(SCRIPT), if_none_match=f'"{asset_version(SCRIPT)}"')
        self.assertEqual((script.status_code, script["Cache-Control"]), (304, IMMUTABLE))

    def test_private_and_data_files_are_never_stored_or_validated(self):
        # An Admin may read every file, administrative system.json included.
        user = bootstrap("owner", PASSPHRASE)
        self.client.force_login(user)
        session = self.client.session
        session["identity_revision"] = user.auth_revision
        session.save()
        for name in UNSHARED:
            for method in ("get", "head"):
                with self.subTest(name=name, method=method):
                    version = asset_version(FILES[name])
                    response = self.get(name, "?v=" + version, method=method, if_none_match=f'"{version}"')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Cache-Control"], "no-store")
                    self.assertFalse(response.has_header("ETag"))
                    self.assertEqual(response["Content-Length"], str(len(FILES[name])))
                    self.assertEqual(response.content, b"" if method == "head" else FILES[name])

    def test_asgi_serves_files_without_a_synchronous_iterator(self):
        """A file-backed response made Django warn on every asset under ASGI."""
        sent, requests = [], [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            if requests:
                return requests.pop()
            await asyncio.Event().wait()  # No disconnect: the handler cancels this.

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "2", "method": "GET", "scheme": "https",
                 "path": "/static/remote/code.js", "raw_path": b"/static/remote/code.js", "query_string": b"",
                 "headers": [(b"host", b"testserver")], "client": ("192.0.2.10", 50000), "server": ("testserver", 443)}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async_to_sync(ASGIHandler())(scope, receive, send)
        self.assertEqual([str(warning.message) for warning in caught], [])
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(b"".join(message.get("body", b"") for message in sent[1:]), SCRIPT)
