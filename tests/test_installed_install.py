"""Installer subprocesses preserve the installed program and private site."""
from __future__ import annotations

from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_paths
from oh_install import steps


class InstalledInstall(unittest.TestCase):
    def setUp(self):
        scratch = AREA.parents[1] / ".scratch/service-access/installed"; scratch.mkdir(parents=True, exist_ok=True)
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

    def test_boot_deploy_and_verification_use_application_interpreter_and_explicit_site(self):
        with patch.object(oh_paths, "PROGRAM", self.program):
            self.assertEqual(steps.BootTest(self.context).actions(self.host)[0].argv, self.command("test", "boot"))
            commands = [action.argv for action in steps.Deploy(self.context).actions(self.host)]
            self.assertEqual(commands, [self.command("generate", "--check"), self.command("validate"),
                                        self.command("lint"), self.command("deploy")])
            self.assertEqual([action.argv for action in steps.Verify(self.context).actions(self.host)],
                             [self.command("health", "--production"), self.command("status")])

    def test_group_refresh_preserves_quoted_program_site_and_interpreter(self):
        self.host.process_groups.return_value = {1000}
        self.host.which.return_value = "/usr/bin/sg"
        self.host.login.return_value = "owner"
        with patch.object(oh_paths, "PROGRAM", self.program):
            command = steps.Deploy(self.context).actions(self.host)[-1]
            self.assertEqual(command.argv[:3], ("sg", "thermite", "-c"))
            self.assertEqual(tuple(shlex.split(command.argv[-1])), self.command("deploy"))
            self.host.which.return_value = None
            fallback = steps.Deploy(self.context).actions(self.host)[-1]
            self.assertTrue(fallback.root)
            self.assertEqual(fallback.argv, ("-u", "owner", *self.command("deploy")))

    def test_source_checkout_keeps_original_boot_and_deploy_checks(self):
        (self.site / oh_paths.SITE_FILE).unlink()
        self.assertEqual(steps.BootTest(self.context).actions(self.host)[0].argv,
                         ("make", "openhab-test", "TEST=boot"))
        self.assertEqual(steps.Deploy(self.context).actions(self.host)[0].argv, ("make", "openhab-deploy"))

    def test_access_staging_uses_installed_resources_and_selected_site_boundary(self):
        from oh_access import staging
        from oh_access.files import atomic_private, json_private
        from oh_features.remote import FILES
        root = Path(self.temporary.name)
        (self.program / "lib").mkdir(parents=True)
        (self.program / "lib/reviewed.py").write_text("# application resource\n")
        (self.program / "bin").mkdir()
        (self.program / "bin/thermite").write_text("# application launcher\n")
        assets = self.site / "conf/html/remote"; assets.mkdir(parents=True)
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
                                    assets=assets, runtime_url="http://127.0.0.1:13601",
                                    runtime_token_file=root / "bearer", tls_cert=root / "cert", tls_key=root / "key",
                                    allowed_hosts=["site.example"], bind_host="127.0.0.1", port=13600,
                                    timezone="Etc/UTC", install_environment=False)
        self.assertEqual((target / "program/lib/reviewed.py").read_text(), "# application resource\n")
        self.assertEqual((target / "program/bin/thermite").read_text(), "# application launcher\n")
        self.assertFalse((self.site / "lib").exists())
        location.assert_called_once_with(root / "identity-state", target, self.site)
        self.assertFalse(receipt["activated"])
        run.assert_not_called()


if __name__ == "__main__": unittest.main()
