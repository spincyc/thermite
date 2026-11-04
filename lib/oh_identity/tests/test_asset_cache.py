"""Asset caching: versioned immutable URLs, validators, compression and HEAD."""

import asyncio
import gzip
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
from oh_identity.views import accepts_gzip
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
#: Only static, non-secret files are ever stored, validated or compressed.
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
        self.assertEqual(response["Vary"], "Accept-Encoding")
        self.assertEqual(response["Content-Type"], mimetypes.guess_type("code.js")[0])
        self.assertEqual(response["Content-Length"], str(len(SCRIPT)))
        self.assertEqual(response.content, SCRIPT)
        self.assertFalse(response.has_header("Content-Encoding"))
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
                    self.assertEqual(response["Vary"], "Accept-Encoding")
                    self.assertEqual(response["X-Frame-Options"], "DENY")
        changed = self.get("access.html", if_none_match='"0123456789abcdef"')
        self.assertEqual((changed.status_code, changed.content), (200, FILES["access.html"]))
        # The page's own versioned files revalidate cheaply too.
        script = self.get("code.js", "?v=" + asset_version(SCRIPT), if_none_match=f'"{asset_version(SCRIPT)}"')
        self.assertEqual((script.status_code, script["Cache-Control"]), (304, IMMUTABLE))

    def test_shared_text_is_gzipped_only_when_accepted(self):
        version = asset_version(SCRIPT)
        for method in ("get", "head"):
            with self.subTest(method=method):
                response = self.get("code.js", "?v=" + version, method=method, accept_encoding="gzip, deflate, br, zstd")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Encoding"], "gzip")
                self.assertEqual(response["ETag"], f'"{version}-gzip"')
                self.assertEqual(response["Vary"], "Accept-Encoding")
                self.assertEqual(response["Cache-Control"], IMMUTABLE)
                compressed = gzip.compress(SCRIPT, mtime=0)
                self.assertLess(len(compressed), len(SCRIPT))
                self.assertEqual(response["Content-Length"], str(len(compressed)))
                self.assertEqual(gzip.decompress(response.content) if method == "get" else response.content,
                                 SCRIPT if method == "get" else b"")
        revalidated = self.get("code.js", accept_encoding="gzip", if_none_match=f'"{version}-gzip"')
        self.assertEqual((revalidated.status_code, revalidated["ETag"]), (304, f'"{version}-gzip"'))
        # An identity copy is a different representation from the gzip one.
        other = self.get("code.js", accept_encoding="gzip", if_none_match=f'"{version}"')
        self.assertEqual((other.status_code, other["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(self.get("theme.css", accept_encoding="gzip").content), FILES["theme.css"])
        for header in ("identity", "gzip;q=0", "br, *;q=0", "*;q=0, gzip;q=0", "deflate"):
            with self.subTest(accept_encoding=header):
                response = self.get("code.js", accept_encoding=header)
                self.assertFalse(response.has_header("Content-Encoding"))
                self.assertEqual(response["ETag"], f'"{version}"')
                self.assertEqual(response.content, SCRIPT)
        image = self.get("icon.png", accept_encoding="gzip")
        self.assertFalse(image.has_header("Content-Encoding"))
        self.assertFalse(image.has_header("Vary"))
        self.assertEqual(image.content, FILES["icon.png"])

    def test_private_and_data_files_are_never_stored_validated_or_compressed(self):
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
                    response = self.get(name, "?v=" + version, method=method, accept_encoding="gzip",
                                        if_none_match=f'"{version}"')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Cache-Control"], "no-store")
                    self.assertFalse(response.has_header("ETag"))
                    self.assertFalse(response.has_header("Content-Encoding"))
                    self.assertNotIn("Accept-Encoding", response.get("Vary", ""))
                    self.assertEqual(response["Content-Length"], str(len(FILES[name])))
                    self.assertEqual(response.content, b"" if method == "head" else FILES[name])

    def test_gzip_negotiation_follows_quality_values(self):
        for header, accepted in (("gzip", True), ("GZIP;q=0.5", True), ("*", True), ("br;q=1, *;q=0.1", True),
                                 ("", False), ("identity", False), ("gzip;q=0", False), ("gzip;q=0.0, *", False),
                                 ("*;q=0", False), ("gzip;q=bad", False)):
            with self.subTest(header=header):
                self.assertEqual(accepts_gzip(header), accepted)

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
