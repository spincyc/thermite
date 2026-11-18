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
            result = access.main(["setup", "--descriptor", "/unused/config.json"])
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
        me = {"id": 2, "username": "operator", "active": True, "admin": False, "is_guest": False,
              "can_edit": False, "site": "site", "permissions": ["camera.view", "room.control", "site.read"], "extra": "x"}
        result = self.client.status(self.url, config_home=self.home, http=lambda *args, **kwargs: me)
        self.assertEqual(result["user"], {key: value for key, value in me.items() if key != "extra"})
        for room in ("Den", "den/../me", "1den"):
            with self.assertRaises(AccessError):
                self.client.status(self.url, config_home=self.home, http=lambda *args, **kwargs: me, rooms=[room])
        with self.assertRaises(AccessError):
            self.client.request(self.url, "/api/v1/rooms/Den/policy")


class TerminalWithoutSite(unittest.TestCase):
    """login, logout and status reach a gateway as thermite tui does, with no
    private site; the server's subcommands still need one."""

    def run_access(self, argv):
        import oh_paths
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch.object(oh_paths, "require_compatible", side_effect=oh_paths.SiteError("no site")) as check:
            status = access.main(argv)
        return status, check, error.getvalue()

    def test_login_takes_the_address_and_trust_as_thermite_tui_does_without_a_site(self):
        import oh_cli
        from oh_access import client
        from oh_cli import tui
        self.assertFalse(oh_cli.VERBS["access"].site)
        for given, url in (("thermite.example:13601", "https://thermite.example:13601"),
                           ("192.0.2.50", "https://192.0.2.50:16301")):
            with patch.object(tui, "installed_profile", return_value=None), \
                    patch.object(tui, "default_port", return_value=16301), \
                    patch.object(tui, "trust", return_value=("/trusted/site-ca.pem", "saved")) as trust, \
                    patch.object(access, "hidden_password", return_value="synthetic"), \
                    patch.object(client, "login", return_value={"user": "operator"}) as login:
                status, check, error = self.run_access(["login", "--url", given, "--user", "operator"])
            self.assertEqual(status, 0, error)
            check.assert_not_called()
            trust.assert_called_once_with(url, None)
            login.assert_called_once_with(url, "operator", "synthetic", ca_file="/trusted/site-ca.pem")

    def test_status_and_logout_need_no_site_either(self):
        from oh_access import client
        from oh_cli import tui
        with patch.object(tui, "installed_profile", return_value=None), \
                patch.object(client, "status", return_value={}) as status_call, \
                patch.object(client, "logout", return_value={}) as logout:
            self.assertEqual(self.run_access(["status", "--url", "thermite.example:13601"])[0], 0)
            self.assertEqual(self.run_access(["logout", "--url", "thermite.example:13601"])[0], 0)
        status_call.assert_called_once_with("https://thermite.example:13601", rooms=[])
        logout.assert_called_once_with("https://thermite.example:13601")

    def test_a_selected_sites_installed_gateway_is_the_default(self):
        from oh_access import client
        from oh_cli import tui
        profile = {"url": "https://thermite.example:13601", "ca_file": "/site/tls-cert.pem"}
        with patch.object(tui, "installed_profile", return_value=profile), \
                patch.object(client, "status", return_value={}) as status_call:
            self.assertEqual(self.run_access(["status"])[0], 0)
        status_call.assert_called_once_with("https://thermite.example:13601", rooms=[])

    def test_a_wrong_address_is_refused_before_anything_is_sent(self):
        from oh_access import client
        from oh_cli import tui
        with patch.object(tui, "installed_profile", return_value=None), \
                patch.object(client, "login") as login:
            status, _, error = self.run_access(["login", "--url", "http://thermite.example", "--user", "operator"])
        self.assertEqual(status, 2)
        self.assertIn("plain http only reaches this computer", error)
        login.assert_not_called()

    def test_the_servers_subcommands_still_need_a_compatible_site(self):
        with patch.object(service, "prune_policies") as prune:
            status, check, error = self.run_access(["prune", "--descriptor", "/unused/config.json"])
        self.assertEqual(status, 2)
        check.assert_called_once_with()
        prune.assert_not_called()
        self.assertIn("no site", error)


class Audit(unittest.TestCase):
    ROWS = [{"at": "2026-11-04T18:00:00+00:00", "action": "login", "outcome": "denied", "principal": "anonymous",
             "actor": None, "user": None, "target": "2", "peer": "9c0d1e2f", "account": "operator"},
            {"at": "2026-11-04T18:01:00+00:00", "action": "token.create", "outcome": "ok", "principal": "password",
             "actor": 2, "user": "operator", "target": "1a2b3c4d laptop", "peer": "9c0d1e2f"}]

    def test_audit_reads_as_the_service_and_prints_one_line_per_row(self):
        from oh_access import audit
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(audit, "rows", return_value=self.ROWS) as rows:
            self.assertEqual(access.main(["audit", "--descriptor", "/srv/thermite/config.json", "--since", "7d",
                                          "--action", "login", "--action", "token"]), 0)
        rows.assert_called_once_with(Path("/srv/thermite/config.json"), after="7d", actions=["login", "token"])
        self.assertEqual(output.getvalue().splitlines(), [
            "2026-11-04T18:00:00+00:00 audit action=login outcome=denied principal=anonymous actor=- user=- target=2 "
            "peer=9c0d1e2f account=operator",
            '2026-11-04T18:01:00+00:00 audit action=token.create outcome=ok principal=password actor=2 user=operator '
            'target="1a2b3c4d laptop" peer=9c0d1e2f'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(audit, "rows", return_value=self.ROWS):
            self.assertEqual(access.main(["audit", "--descriptor", "/srv/thermite/config.json", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.ROWS)

    def test_audit_refuses_another_account_before_opening_state(self):
        from oh_access import audit
        error = io.StringIO()
        with contextlib.redirect_stderr(error), patch.object(audit, "require_service_account",
                                                             side_effect=AccessError("Run as thermite")), \
                patch.object(service, "configured") as configured:
            self.assertEqual(access.main(["audit", "--descriptor", "/srv/thermite/config.json"]), 2)
        configured.assert_not_called()
        self.assertIn("Run as thermite", error.getvalue())


class ResetAdmin(unittest.TestCase):
    ARGV = ["reset-admin", "--descriptor", "/srv/thermite/config.json", "--user", "owner"]

    def run_verb(self, *, account=None, terminal=True, entered=("a phrase nobody had", "a phrase nobody had")):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch("oh_access.files.require_service_account", side_effect=account) as required, \
                patch.object(sys.stdin, "isatty", return_value=terminal), \
                patch.object(access.getpass, "getpass", side_effect=list(entered)) as asked, \
                patch.object(service, "reset_admin", return_value={"reset": True, "username": "owner"}) as reset:
            status = access.main(self.ARGV)
        return status, output.getvalue(), error.getvalue(), required, asked, reset

    def test_it_asks_the_terminal_for_the_password_and_resets_the_named_account(self):
        status, output, _, required, asked, reset = self.run_verb()
        self.assertEqual((status, json.loads(output)), (0, {"reset": True, "username": "owner"}))
        required.assert_called_once_with()
        self.assertEqual([call.args[0] for call in asked.call_args_list], ["New administrator password: ", "Confirm password: "])
        reset.assert_called_once_with(Path("/srv/thermite/config.json"), "owner", "a phrase nobody had")
        self.assertNotIn("a phrase nobody had", output)

    def test_another_account_is_refused_before_a_password_is_asked_for(self):
        status, _, error, _, asked, reset = self.run_verb(account=AccessError("Run this access service as user thermite"))
        self.assertEqual(status, 2)
        self.assertIn("user thermite", error)
        asked.assert_not_called()
        reset.assert_not_called()

    def test_it_needs_a_terminal_and_two_matching_entries_of_twelve_characters(self):
        for terminal, entered, message in ((False, (), "interactive terminal"),
                                           (True, ("a phrase nobody had", "another phrase"), "must match"),
                                           (True, ("short",), "at least 12 characters")):
            with self.subTest(message=message):
                status, _, error, _, _, reset = self.run_verb(terminal=terminal, entered=entered)
                self.assertEqual(status, 2)
                self.assertIn(message, error)
                reset.assert_not_called()

    def test_the_name_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            access.main(self.ARGV[:3])
        self.assertEqual(refused.exception.code, 2)


class Stage(unittest.TestCase):
    def test_no_action_stages_a_release(self):
        """Only the install transaction stages one (oh_install.access.Gateway)."""
        with contextlib.redirect_stderr(io.StringIO()) as refused, self.assertRaises(SystemExit) as stopped:
            access.main(["stage", "--help"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("invalid choice: 'stage'", refused.getvalue())


class Prune(unittest.TestCase):
    def test_prune_runs_the_service_operation_and_prints_its_result(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(service, "prune_policies", return_value={"pruned": ["attic"]}) as prune:
            self.assertEqual(access.main(["prune", "--descriptor", "/srv/thermite/config.json"]), 0)
        prune.assert_called_once_with(Path("/srv/thermite/config.json"))
        self.assertEqual(json.loads(output.getvalue()), {"pruned": ["attic"]})


if __name__ == "__main__":
    unittest.main()
