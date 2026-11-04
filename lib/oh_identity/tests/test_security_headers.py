"""Exercise actual middleware and HTML responses, without a network listener."""
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import Client, TransactionTestCase

from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.models import SecurityState
from oh_identity.tests.test_core import FakeRuntime


class SecurityHeaders(TransactionTestCase):
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})

    def test_private_html_redirects_to_public_login_without_exposing_house(self):
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            root = Path(temporary)
            (root / "home.html").write_text("private home fixture")
            (root / "index.html").write_text("private remote fixture")
            (root / "access.html").write_text("sign in fixture")
            adapter = RuntimeAdapter(RuntimeContract({}, assets={"home.html": {"private": True},
                                                     "index.html": {"private": True}, "access.html": {}},
                                                     asset_root=root), FakeRuntime())
            install_adapter(adapter)
            response = Client().get("/static/remote/home.html", secure=True)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], "/account/?next=%2F")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertNotIn(b"private home fixture", response.content)
            # The directory bookmark and Remotes navigation share the same
            # private asset gate as the explicit index; neither is a 404 or bypass.
            for path in ("/static/remote/", "/static/remote/index.html", "/remotes/"):
                remote = Client().get(path, secure=True)
                self.assertEqual(remote.status_code, 302)
                self.assertEqual(remote["Location"], "/account/?next=%2Fremotes%2F")
                self.assertEqual(remote["Cache-Control"], "no-store")
                self.assertNotIn(b"private remote fixture", remote.content)
            login = Client().get(response["Location"], secure=True)
            try:
                self.assertEqual(login.status_code, 200)
                self.assertEqual(adapter.transport.commands, [])
            finally:
                login.close()

    def test_html_asset_has_framing_protection(self):
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            root = Path(temporary)
            (root / "index.html").write_text("<!doctype html><title>thermite</title><p>HTML fixture</p>")
            adapter = RuntimeAdapter(RuntimeContract({}, assets={"index.html": {}}, asset_root=root), FakeRuntime())
            install_adapter(adapter)
            response = Client().get("/static/remote/index.html", secure=True)
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Frame-Options"], "DENY")
                self.assertIn(b"HTML fixture", b"".join(response.streaming_content))
                self.assertEqual(adapter.transport.commands, [])
            finally:
                response.close()

    def test_session_and_private_asset_rejection_also_have_header(self):
        with tempfile.TemporaryDirectory(dir=settings.STATE) as temporary:
            root = Path(temporary)
            (root / "house.js").write_text("private fixture")
            install_adapter(RuntimeAdapter(RuntimeContract({}, assets={"house.js": {}}, asset_root=root), FakeRuntime()))
            client = Client(enforce_csrf_checks=True)
            response = client.get("/api/v1/session", secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]["secure"])
            response = client.get("/static/remote/house.js", secure=True)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertEqual(response["Cache-Control"], "no-store")
