"""Installer subprocesses preserve the installed program and private site."""
from __future__ import annotations

from pathlib import Path
import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_paths
import oh_cli
from oh_install import steps
from oh_install.native_auth import NativeAuthError


class InstalledInstall(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA.parents[1] / ".scratch/service-access/installed"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="installed-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.site = Path(self.temporary.name) / "private site"
        self.site.mkdir(); (self.site / oh_paths.SITE_FILE).write_text("{}")
        self.program = Path(self.temporary.name) / "immutable application"
        self.context = steps.Context(self.site, {}, {"gid": 9001}, "a" * 40, "b" * 64, "server", None)
        self.host = Mock()
        self.host.process_groups.return_value = {9001}

    def command(self, *args):
        return (sys.executable, str(self.program / "bin/thermite"), "--site", str(self.site), *args)

    def test_runtime_access_credentials_belong_to_the_private_site(self):
        self.host.read_bytes.return_value = None
        self.host.read_private.side_effect = FileNotFoundError
        self.host.clock.return_value = 10
        self.host.configure_runtime_access.return_value = "oh.synthetic.123"
        self.context.host["http_port"] = 13602
        with patch.object(oh_paths, "PROGRAM", self.program):
            action = steps.Token(self.context).actions(self.host)[0]
            self.assertEqual(action.perform(self.host), 0)
        self.host.configure_runtime_access.assert_called_once_with(
            "http://127.0.0.1:13602", self.site / "var/server/runtime-credentials.json",
            self.site / "var/server/api-token", 10 + steps.WAIT)

    def test_existing_runtime_recovers_only_a_missing_operator_copy(self):
        self.context.host.update(http_port=13602, uid=9001, root=str(self.site / "installation"))
        self.host.clock.return_value = 10
        self.host.read_private.side_effect = [FileNotFoundError(), FileNotFoundError(), "synthetic-recovered-token\n"]
        self.host.configure_runtime_access.side_effect = NativeAuthError("existing_users")
        self.host.native_probe.return_value = Mock(status=200, problem=None, value=[])
        with patch.object(oh_paths, "PROGRAM", self.program), patch("oh_install.runner.perform", return_value=0) as perform:
            token = steps.Token(self.context)
            self.assertEqual(token.save(self.host), 0)
        action, host, area = perform.call_args.args
        self.assertTrue(action.root)
        self.assertEqual(action.argv, ("python3", str(self.program / "lib/oh_install/secrets.py"),
                         str(self.site / "installation/runtime"), "--name", "openhab-token", "--source",
                         str(self.site / "var/server/api-token"), "--uid", "9001", "--gid", "9001",
                         "--recover-runtime-token", "--runtime-url", "http://127.0.0.1:13602"))
        self.assertIs(host, self.host)
        self.assertEqual(area, self.site)
        self.assertEqual(token.validated, "synthetic-recovered-token")
        self.assertNotIn("synthetic-recovered-token", str(self.host.say.call_args_list))

    def test_existing_or_rejected_managed_credentials_never_trigger_service_recovery(self):
        self.context.host["http_port"] = 13602
        self.host.clock.return_value = 10
        for code, saved in (("existing_users", "keep-existing-token"), ("credentials", ""),
                            ("retired", ""), ("identity", "")):
            with self.subTest(code=code), patch("oh_install.runner.perform") as perform:
                self.host.read_private.side_effect = None
                self.host.read_private.return_value = saved
                self.host.configure_runtime_access.side_effect = NativeAuthError(code)
                self.assertEqual(steps.Token(self.context).save(self.host), 1)
                perform.assert_not_called()

    def test_runtime_requiring_authentication_for_uuid_can_recover_without_local_credentials(self):
        self.context.host.update(http_port=13602, uid=9001, root=str(self.site / "installation"))
        self.host.clock.return_value = 10
        self.host.read_private.side_effect = [FileNotFoundError(), FileNotFoundError(), "synthetic-recovered-token\n"]
        self.host.configure_runtime_access.side_effect = NativeAuthError("credentials")
        self.host.native_probe.return_value = Mock(status=200, problem=None, value=[])
        with patch.object(oh_paths, "PROGRAM", self.program), patch("oh_install.runner.perform", return_value=0) as perform:
            self.assertEqual(steps.Token(self.context).save(self.host), 0)
        perform.assert_called_once()

    def test_declined_or_failed_service_recovery_does_not_validate_a_token(self):
        self.context.host.update(http_port=13602, uid=9001, root=str(self.site / "installation"))
        self.host.clock.return_value = 10
        self.host.read_private.side_effect = FileNotFoundError
        self.host.configure_runtime_access.side_effect = NativeAuthError("existing_users")
        for code in (None, 2):
            with self.subTest(code=code), patch.object(oh_paths, "PROGRAM", self.program), \
                    patch("oh_install.runner.perform", return_value=code):
                token = steps.Token(self.context)
                self.assertNotEqual(token.save(self.host), 0)
                self.assertIsNone(token.validated)
        self.host.native_probe.assert_not_called()

    def test_boot_deploy_and_verification_use_application_interpreter_and_explicit_site(self):
        with patch.object(oh_paths, "PROGRAM", self.program):
            self.assertEqual(steps.BootTest(self.context).actions(self.host)[0].argv, self.command("test", "boot"))
            actions = steps.Deploy(self.context).actions(self.host)
            commands = [action.argv for action in actions]
            self.assertEqual(commands, [self.command("generate", "--check"), self.command("validate"),
                                        self.command("lint"), self.command("deploy", "--_configuration-phase")])
            self.assertEqual(actions[-1].approved_argv, self.command("deploy", "--_configuration-phase", "--yes", "--quiet"))
            self.assertTrue(all(action.approved_argv is None for action in actions[:-1]))
            self.assertEqual([action.terminal for action in actions], [False, False, False, True])
            self.assertEqual([action.argv for action in steps.Verify(self.context).actions(self.host)],
                             [self.command("health", "--production", "--installation", "--settle", "120"),
                              self.command("status")])
            verification = steps.Verify(self.context).actions(self.host)
            self.assertTrue(verification[0].show_output)
            self.assertFalse(verification[0].terminal)

    def test_an_upgrades_deploy_and_gateway_activation_reach_the_deploy_history(self):
        deployment = steps.Deploy(self.context)
        deployment.how = "upgrade"
        with patch.object(oh_paths, "PROGRAM", self.program):
            action = deployment.actions(self.host)[-1]
        trail = ("--_how", "upgrade")
        self.assertEqual(action.argv, self.command("deploy", "--_configuration-phase", *trail))
        self.assertEqual(action.approved_argv, self.command("deploy", "--_configuration-phase", *trail, "--yes", "--quiet"))
        event = {"commit": "a" * 40, "release": "/srv/thermite/access/releases/fixture-a1b2c3", "previous_release": None,
                 "application": {"version": "1.2.3"}}
        record = steps.gateway_history(self.context)
        for code, recorded in ((0, True), (1, False), (None, False)):
            with self.subTest(code=code), patch.object(oh_paths, "PROGRAM", self.program), \
                    patch("oh_install.runner.perform", return_value=code) as perform:
                self.assertIs(record(self.host, event), recorded)
            command, host, area = perform.call_args.args
            self.assertEqual((host, area), (self.host, self.site))
            self.assertEqual(command.argv, self.command("history", "--state", "/srv/thermite/runtime",
                                                        "--_record-gateway", json.dumps(event, sort_keys=True)))
            self.assertFalse(command.root)

    def test_group_refresh_preserves_quoted_program_site_and_interpreter(self):
        self.host.process_groups.return_value = {1000}
        self.host.which.return_value = "/usr/bin/sg"
        self.host.login.return_value = "owner"
        with patch.object(oh_paths, "PROGRAM", self.program):
            command = steps.Deploy(self.context).actions(self.host)[-1]
            self.assertEqual(command.argv[:3], ("sg", "thermite", "-c"))
            self.assertEqual(tuple(shlex.split(command.argv[-1])), self.command("deploy", "--_configuration-phase"))
            status = steps.Verify(self.context).actions(self.host)[-1]
            self.assertEqual(status.argv[:3], ("sg", "thermite", "-c"))
            self.assertEqual(tuple(shlex.split(status.argv[-1])), self.command("status"))
            self.assertEqual(command.approved_argv[:3], command.argv[:3])
            self.assertEqual(tuple(shlex.split(command.approved_argv[-1])), self.command("deploy", "--_configuration-phase", "--yes", "--quiet"))
            self.assertTrue(command.terminal)
            self.host.which.return_value = None
            fallback = steps.Deploy(self.context).actions(self.host)[-1]
            self.assertTrue(fallback.root)
            self.assertEqual(fallback.argv, ("-u", "owner", *self.command("deploy", "--_configuration-phase")))
            status = steps.Verify(self.context).actions(self.host)[-1]
            self.assertTrue(status.root)
            self.assertEqual(status.argv, ("-u", "owner", *self.command("status")))
            self.assertEqual(fallback.approved_argv, ("-u", "owner", *self.command("deploy", "--_configuration-phase", "--yes", "--quiet")))
            self.assertTrue(fallback.terminal)

    def test_source_checkout_uses_the_same_configuration_checks_as_an_installed_site(self):
        with patch.object(oh_paths, "PROGRAM", self.program):
            installed = steps.Deploy(self.context).actions(self.host)
        installed_checks = [action.argv[len(self.command()):] for action in installed[:-1]]
        (self.site / oh_paths.SITE_FILE).unlink()
        self.assertEqual(steps.BootTest(self.context).actions(self.host)[0].argv,
                         ("make", "openhab-test", "TEST=boot"))
        *checks, action = steps.Deploy(self.context).actions(self.host)
        self.assertEqual([check.argv for check in checks], [("bin/thermite", "generate", "--check"),
                                                          ("bin/thermite", "validate"), ("bin/thermite", "lint")])
        self.assertEqual([check.argv[1:] for check in checks], installed_checks)
        self.assertTrue(all(check.approved_argv is None and not check.terminal for check in checks))
        self.assertEqual(action.argv, ("bin/thermite", "deploy", "--_configuration-phase"))
        self.assertEqual(action.approved_argv, ("bin/thermite", "deploy", "--_configuration-phase", "--yes", "--quiet"))
        self.assertTrue(action.terminal)

    def test_verbose_source_and_installed_deployment_keep_child_output(self):
        with patch.object(oh_paths, "PROGRAM", self.program), patch.object(oh_cli.switches, "verbose", True):
            for installed in (True, False):
                with self.subTest(installed=installed):
                    if not installed:
                        (self.site / oh_paths.SITE_FILE).unlink()
                    action = steps.Deploy(self.context).actions(self.host)[-1]
                    expected = self.command("deploy", "--_configuration-phase") if installed else ("bin/thermite", "deploy", "--_configuration-phase")
                    self.assertEqual(action.argv, expected)
                    self.assertEqual(action.approved_argv, (*expected, "--yes"))
                    self.assertTrue(action.terminal)

    def stage_access(self, **arguments):
        """Stage the gateway from an installed application: (its root, the
        release, the receipt, the patched state check and subprocess runner)."""
        from oh_access import staging
        from oh_access.files import atomic_private, json_private
        from oh_features.remote import FILES
        root = Path(self.temporary.name)
        (self.program / "lib").mkdir(parents=True, exist_ok=True)
        (self.program / "lib/reviewed.py").write_text("# application resource\n")
        (self.program / "bin").mkdir(exist_ok=True)
        (self.program / "bin/thermite").write_text("# application launcher\n")
        assets = self.site / "conf/html/remote"; assets.mkdir(parents=True, exist_ok=True)
        for name in FILES: (assets / name).write_text("private generated asset\n")
        atomic_private(root / "bearer", b"fixture")
        atomic_private(root / "key", b"fixture")
        (root / "cert").write_text("fixture")
        target = root / "persistent-release"
        def compile_contract(house, asset_root, output): json_private(output, {"rooms": {}})
        with patch.object(oh_paths, "PROGRAM", self.program), patch.object(oh_paths, "SITE", self.site), \
                patch.object(staging, "_disposable", return_value=False), \
                patch.object(staging, "_service_parents"), \
                patch.object(staging, "_state_location") as location, \
                patch("oh_access.service.compile_contract", side_effect=compile_contract), \
                patch("oh_access.service.load_config"), patch.object(staging.subprocess, "run") as run:
            receipt = staging.stage(target, state=root / "identity-state", house=self.site / "instance/devices.yaml",
                                    assets=assets, runtime_url="http://127.0.0.1:13602",
                                    runtime_token_file=root / "bearer", tls_cert=root / "cert", tls_key=root / "key",
                                    allowed_hosts=["site.example"], bind_host="127.0.0.1",
                                    timezone="Etc/UTC", install_environment=False,
                                    plan_directory=root / "operator-plan", **arguments)
        return root, target, receipt, location, run

    def test_access_staging_uses_installed_resources_and_selected_site_boundary(self):
        root, target, receipt, location, run = self.stage_access(
            port=13601, guest_http={"listen": ["192.0.2.10", "192.0.2.10"], "clients": ["192.0.2.0/24"]})
        config = json.loads((target / "config.json").read_text())
        self.assertEqual((config["version"], config["guest_http"]),
                         (2, {"port": 13600, "listen": ["192.0.2.10"], "clients": ["192.0.2.0/24"], "names": []}))
        self.assertEqual((target / "program/lib/reviewed.py").read_text(), "# application resource\n")
        self.assertEqual((target / "program/bin/thermite").read_text(), "# application launcher\n")
        self.assertFalse((self.site / "lib").exists())
        location.assert_called_once_with(root / "identity-state", target, self.site)
        self.assertFalse(receipt["activated"])
        installs = receipt["unit_install_commands"]
        self.assertEqual(len(installs), 2)
        for command in installs:
            source = Path(command[-2])
            self.assertEqual(source.parent, root / "operator-plan/units")
            self.assertEqual(source.read_bytes(), (target / "units" / source.name).read_bytes())
        run.assert_not_called()

    def test_access_staging_writes_a_moved_https_port_beside_guests_on_13601(self):
        import oh_deploy
        import oh_ports
        from oh_access import AccessError
        from oh_access.service import load_config
        host = oh_deploy.validate_host({"timezone": "Etc/UTC", "http_port": 13602, "https_port": 13603, "uid": 9001,
                                        "gid": 9001, "gateway": {"port": 16301},
                                        "guest_http": {"port": 13601, "listen": ["192.0.2.10"],
                                                       "clients": ["192.0.2.0/24"]}})
        with self.assertRaisesRegex(AccessError, "listener port 13601 collides"):
            self.stage_access(port=13601, guest_http=host["guest_http"], host_ports=oh_ports.effective(host))
        self.assertFalse((Path(self.temporary.name) / "persistent-release").exists())
        _, target, _, _, _ = self.stage_access(port=oh_ports.gateway_port(host), guest_http=host["guest_http"],
                                               host_ports=oh_ports.effective(host))
        config = load_config(target / "config.json", tls=False)
        self.assertEqual((config["version"], config["port"], config["guest_http"]),
                         (2, 16301, {"port": 13601, "listen": ["192.0.2.10"], "clients": ["192.0.2.0/24"], "names": []}))

if __name__ == "__main__": unittest.main()
