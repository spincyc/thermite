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

    def test_a_terminal_credential_is_named_after_its_host(self):
        sent = []
        synthetic = "thm_" + "B" * 40
        issued = {"token": synthetic, "id": "two", "expires_at": "2099-01-01T00:00:00+00:00"}
        with patch("oh_access.client.socket.gethostname", return_value="Laptop.home.example"):
            self.client.login(self.url, "operator", "synthetic", config_home=self.home,
                              http=lambda *args, **kwargs: sent.append(kwargs["data"]) or issued)
        self.assertEqual(sent[0]["name"], "laptop")
        self.assertEqual([self.client.token_name(name) for name in ("desk", "bad_host!", "", "-x")],
                         ["desk", "thermite CLI", "thermite CLI", "thermite CLI"])

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


class Audit(unittest.TestCase):
    ROWS = [{"at": "2026-11-04T18:00:00+00:00", "action": "login", "outcome": "denied", "principal": "anonymous",
             "actor": None, "user": None, "target": "2", "peer": "9c0d1e2f", "account": "operator"},
            {"at": "2026-11-04T18:01:00+00:00", "action": "token.create", "outcome": "ok", "principal": "password",
             "actor": 2, "user": "operator", "target": "1a2b3c4d laptop", "peer": "9c0d1e2f"}]

    def test_audit_reads_as_the_service_and_prints_one_line_per_row(self):
        from oh_access import audit
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(audit, "rows", return_value=self.ROWS) as rows:
            self.assertEqual(access.main(["audit", "--config", "/srv/thermite/config.json", "--since", "7d",
                                          "--action", "login", "--action", "token"]), 0)
        rows.assert_called_once_with(Path("/srv/thermite/config.json"), after="7d", actions=["login", "token"])
        self.assertEqual(output.getvalue().splitlines(), [
            "2026-11-04T18:00:00+00:00 audit action=login outcome=denied principal=anonymous actor=- user=- target=2 "
            "peer=9c0d1e2f account=operator",
            '2026-11-04T18:01:00+00:00 audit action=token.create outcome=ok principal=password actor=2 user=operator '
            'target="1a2b3c4d laptop" peer=9c0d1e2f'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(audit, "rows", return_value=self.ROWS):
            self.assertEqual(access.main(["audit", "--config", "/srv/thermite/config.json", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.ROWS)

    def test_audit_refuses_another_account_before_opening_state(self):
        from oh_access import audit
        error = io.StringIO()
        with contextlib.redirect_stderr(error), patch.object(audit, "require_service_account",
                                                             side_effect=AccessError("Run as thermite")), \
                patch.object(service, "configured") as configured:
            self.assertEqual(access.main(["audit", "--config", "/srv/thermite/config.json"]), 2)
        configured.assert_not_called()
        self.assertIn("Run as thermite", error.getvalue())


class StagePort(unittest.TestCase):
    def test_staged_https_port_defaults_to_the_selected_sites_gateway_port(self):
        import oh_ports
        from oh_access import staging
        moved = oh_ports.effective({"gateway": {"port": 16301}})
        required = ["stage", "--destination", "/fictional/release", "--state", "/fictional/state",
                    "--plan-directory", "/fictional/plan", "--assets", "/fictional/assets",
                    "--runtime-url", "http://127.0.0.1:13601", "--runtime-token-file", "/fictional/bearer",
                    "--tls-cert", "/fictional/cert", "--tls-key", "/fictional/key", "--allowed-host", "house.example.test",
                    "--json"]
        for site, extra, expected in ((moved, [], 16301), (None, [], 13600), (moved, ["--port", "14000"], 14000)):
            with self.subTest(site=bool(site), extra=extra), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(access, "_site_ports", return_value=site), \
                    patch.object(staging, "stage", return_value={}) as stage:
                self.assertEqual(access.main([*required, *extra]), 0)
                self.assertEqual((stage.call_args.kwargs["port"], stage.call_args.kwargs["host_ports"]), (expected, site))


class Prune(unittest.TestCase):
    def test_prune_runs_the_service_operation_and_prints_its_result(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(service, "prune_policies", return_value={"pruned": ["attic"]}) as prune:
            self.assertEqual(access.main(["prune", "--config", "/srv/thermite/config.json"]), 0)
        prune.assert_called_once_with(Path("/srv/thermite/config.json"))
        self.assertEqual(json.loads(output.getvalue()), {"pruned": ["attic"]})


if __name__ == "__main__":
    unittest.main()
