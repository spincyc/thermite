"""Local commissioning and terminal startup preserve the remote auth boundary."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_brand
from oh_access import AccessError, service
from oh_cli import access, remote


class Commissioning(unittest.TestCase):
    def test_setup_refuses_redirected_output_before_arming_a_code(self):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch.object(service, "prepare_setup") as prepare:
            result = access.main(["setup", "--config", "/unused/config.json"])
        self.assertEqual(result, 2)
        prepare.assert_not_called()
        self.assertNotIn("setup code", output.getvalue())
        self.assertIn("interactive terminal", error.getvalue())

    def test_terminal_without_credential_defers_to_gateway_discovery(self):
        scratch = AREA / ".scratch/auth-root"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            path = Path(temporary) / "credential.json"
            args = argparse.Namespace(mock=False, native=False, url="https://site.example.invalid", sandbox=False)
            with patch.object(remote, "model_file", return_value=None), \
                    patch("oh_access.client.credential_path", return_value=path), \
                    patch("oh_access.client.saved_ca", return_value="/trusted/site-ca.pem"), \
                    patch("oh_access.client.load") as load, \
                    patch.dict(os.environ, {"THERMITE_REMOTE_TOKEN_FILE": "/another/site.json"}):
                environment = remote.launch_environment(args, oh_brand.load())
            self.assertNotIn("THERMITE_REMOTE_TOKEN_FILE", environment)
            self.assertEqual(environment["NODE_EXTRA_CA_CERTS"], "/trusted/site-ca.pem")
            self.assertFalse(remote.native_mode(args))
            load.assert_not_called()


class TerminalCredentials(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/auth-root"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "config"
        self.url = "https://site.example.invalid"
        from oh_access import client
        self.client = client
        synthetic = "thm_" + "A" * 40
        issued = {"token": synthetic, "id": "one", "expires_at": "2099-01-01T00:00:00+00:00"}
        result = client.login(self.url, "operator", "synthetic", config_home=self.home, http=lambda *args, **kwargs: issued)
        self.path = Path(result["credential_file"])

    def test_logout_removes_a_credential_the_site_already_rejects(self):
        def rejected(*args, **kwargs):
            raise self.client.Refused(401)
        self.assertTrue(self.client.logout(self.url, config_home=self.home, http=rejected)["logged_out"])
        self.assertFalse(self.path.exists())

    def test_logout_keeps_the_credential_when_the_site_fails_otherwise(self):
        def unavailable(*args, **kwargs):
            raise self.client.Refused(503)
        with self.assertRaises(self.client.Refused):
            self.client.logout(self.url, config_home=self.home, http=unavailable)
        self.assertTrue(self.path.exists())

    def test_status_reports_the_principal_shape_and_checks_room_ids(self):
        me = {"id": 2, "username": "operator", "active": True, "admin": False, "is_guest": False, "auth_enabled": True,
              "can_edit": False, "site": "site", "permissions": ["camera.view", "room.control", "site.read"], "extra": "x"}
        result = self.client.status(self.url, config_home=self.home, http=lambda *args, **kwargs: me)
        self.assertEqual(result["user"], {key: value for key, value in me.items() if key != "extra"})
        for room in ("Den", "den/../me", "1den"):
            with self.assertRaises(AccessError):
                self.client.status(self.url, config_home=self.home, http=lambda *args, **kwargs: me, rooms=[room])
        with self.assertRaises(AccessError):
            self.client.request(self.url, "/api/v1/rooms/Den/policy")


class Prune(unittest.TestCase):
    def test_prune_runs_the_service_operation_and_prints_its_result(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(service, "prune_policies", return_value={"pruned": ["attic"]}) as prune:
            self.assertEqual(access.main(["prune", "--config", "/srv/thermite/config.json"]), 0)
        prune.assert_called_once_with(Path("/srv/thermite/config.json"))
        self.assertEqual(json.loads(output.getvalue()), {"pruned": ["attic"]})


if __name__ == "__main__":
    unittest.main()
