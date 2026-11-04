"""Config-only installation consumes one committed, pinned configuration.

All filesystem work stays in private scratch fixtures. No native process,
service command, network request or privileged operation is executed.
"""
from __future__ import annotations

from contextlib import nullcontext
import dataclasses
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_paths  # noqa: E402
import oh_release  # noqa: E402
from oh_cli import deploy as deploy_cli, install as install_cli, rollback as rollback_cli, health, status  # noqa: E402
from oh_install import runner, steps  # noqa: E402
from oh_testing import boot  # noqa: E402


class SplitInstaller(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/split_installer"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="site-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.site = Path(self.temporary.name) / "private site"
        self.site.mkdir(mode=0o700)
        self.program = Path(self.temporary.name) / "installed application"
        files = {"bin/thermite": b"# synthetic application fixture\n"}
        release = oh_release.create(files)
        for name, data in files.items():
            path = self.program / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.program / oh_release.FILE).write_text(json.dumps(release))
        selected_program = patch.object(oh_paths, "PROGRAM", self.program)
        selected_program.start()
        self.addCleanup(selected_program.stop)
        self.requirement = {"version": 2, "application": release["application"], "release": release["digest"]}
        (self.site / oh_paths.SITE_FILE).write_text(json.dumps(self.requirement))
        self.versions = {"openhab": {"version": "5.2.3"}, "java": {"version": "21"}}
        self.host = {"uid": 9001, "gid": 9001, "state_root": str(self.site / "native-state"), "http_port": 13601}
        self.files = {name: deploy.Blob(("frozen " + name).encode()) for name in deploy.INSTALLED}
        self.files.update({"conf/services/addons.cfg": deploy.Blob(b"binding = http,network\nmisc = metrics\n"),
                           "conf/automation/js/owned.js": deploy.Blob(b"// committed generated rule\n"),
                           "metrics/prometheus/prometheus.yml": deploy.Blob(b"# frozen metrics\n"),
                           "lib/helper.py": deploy.Blob(b"# release helper, not a local build input\n")})
        conf = {name.removeprefix("conf/"): blob for name, blob in self.files.items() if name.startswith("conf/")}
        self.application = {"version": "0.1.0.dev0", "digest": self.requirement["release"]}
        self.context = steps.Context(self.site, self.versions, self.host, "b" * 40, deploy.tree_digest(conf),
                                     "fixture-host", None, prepared=self.files, application=self.application)
        self.system = Mock()
        self.system.hostname.return_value = "fixture-host"
        self.system.address.return_value = None
        self.system.process_groups.return_value = {9001}

    def prepare(self):
        with patch.object(deploy, "resolve", return_value=self.context.head), patch.object(deploy, "uncommitted", return_value=[]):
            return steps.DerivedConfiguration(self.context).prepare(self.system)

    def test_descriptor_accepts_both_schemas_and_rejects_unknown_or_boolean_versions(self):
        for version in (1, 2):
            data = {**self.requirement, "version": version}
            (self.site / oh_paths.SITE_FILE).write_text(json.dumps(data))
            self.assertEqual(oh_paths.descriptor(self.site), data)
        for version in (0, 3, True, "2", 2.0):
            (self.site / oh_paths.SITE_FILE).write_text(json.dumps({**self.requirement, "version": version}))
            with self.subTest(version=version), self.assertRaises(oh_paths.SiteError):
                oh_paths.descriptor(self.site)

    def test_context_ignores_working_inputs_and_missing_generated_trees(self):
        (self.site / "versions.json").write_text('{"openhab":{"version":"wrong working pin"}}')
        with patch.object(deploy, "resolve", return_value=self.context.head), \
                patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, self.requirement)) as inputs, \
                patch.object(deploy, "prepared_files", return_value=(self.files, self.application)) as rendered, \
                patch.object(deploy, "load", side_effect=AssertionError("working inputs became authority")):
            actual = steps.committed_context(self.site, self.system)
        inputs.assert_called_once_with(self.site, self.context.head)
        rendered.assert_called_once_with(self.site, self.context.head, self.site / "var")
        self.assertEqual(actual, self.context)
        self.assertFalse((self.site / "var").exists())
        self.assertFalse((self.site / "conf").exists())

    def test_mismatched_working_requirement_or_missing_release_fails_before_materialization(self):
        with patch.object(deploy, "resolve", return_value=self.context.head), \
                patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, {**self.requirement, "release": "c" * 64})), \
                patch.object(deploy, "prepared_files") as prepared:
            with self.assertRaisesRegex(deploy.DeployError, "commit the site's application requirement"):
                steps.committed_context(self.site, self.system)
            prepared.assert_not_called()
        with patch.object(deploy, "resolve", return_value=self.context.head), \
                patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, self.requirement)), \
                patch.object(deploy, "prepared_files", side_effect=deploy.DeployError("missing exact release")):
            with self.assertRaisesRegex(deploy.DeployError, "missing exact release"):
                steps.committed_context(self.site, self.system)
        self.assertFalse((self.site / "var").exists())

    def test_approved_materialization_is_exact_and_never_uses_top_level_output(self):
        self.assertFalse(steps.DerivedConfiguration(self.context).check(self.system).done)
        self.assertEqual(self.prepare(), 0)
        root = self.context.configuration
        self.assertEqual(deploy.read_tree(root), self.context.generated())
        self.assertEqual(json.loads((root / "versions.json").read_bytes()), self.versions)
        self.assertTrue(steps.DerivedConfiguration(self.context).check(self.system).done)
        self.assertEqual(oh_release.manifest(self.context.program)["digest"], self.application["digest"])
        self.assertEqual(self.prepare(), 0)
        for name in ("conf", "metrics", "deploy", "lib"):
            self.assertFalse((self.site / name).exists())
        self.assertFalse((root / "lib").exists())
        self.assertEqual(list(root.parent.glob(".build-*")), [])

    def test_dry_run_and_declined_install_do_not_materialize_the_build(self):
        runner.run([steps.DerivedConfiguration(self.context)], self.system, self.site, dry_run=True)
        self.assertFalse(self.context.configuration.parent.exists())
        self.assertFalse((self.site / "var").exists())
        with patch.object(install_cli, "AREA", self.site), \
                patch.object(steps, "committed_context", return_value=self.context), \
                patch.object(steps, "install_preflight", return_value=[]), \
                patch.object(runner, "confirm_run", return_value=False) as approval:
            self.assertEqual(install_cli.install(self.system, False), 1)
        approval.assert_called_once()
        self.assertFalse((self.site / "var").exists())

    def test_existing_build_with_missing_release_is_repaired_after_approval(self):
        self.prepare()
        import shutil
        shutil.rmtree(self.context.program)
        step = steps.DerivedConfiguration(self.context)
        self.assertTrue(step.matches())
        self.assertFalse(step.check(self.system).done)
        self.assertEqual(self.prepare(), 0)
        self.assertTrue(step.check(self.system).done)

    def test_damaged_release_is_not_overwritten_during_preparation(self):
        self.prepare()
        program = self.context.program / "bin/thermite"
        program.chmod(0o600)
        program.write_bytes(b"changed application")
        self.assertIn("differs", steps.DerivedConfiguration(self.context).check(self.system).problem)
        with self.assertRaises(oh_release.ReleaseError):
            self.prepare()
        self.assertEqual(program.read_bytes(), b"changed application")

    def test_changed_commit_or_uncommitted_inputs_cannot_replace_the_reviewed_plan(self):
        for head, dirty in (("c" * 40, []), (self.context.head, [" M instance/devices.yaml"])):
            with self.subTest(head=head, dirty=dirty), patch.object(deploy, "resolve", return_value=head), \
                    patch.object(deploy, "uncommitted", return_value=dirty):
                with self.assertRaisesRegex(deploy.DeployError, "site changed after planning"):
                    steps.DerivedConfiguration(self.context).prepare(self.system)
        self.assertFalse(self.context.configuration.parent.exists())

    def test_existing_cache_symlinks_extra_files_and_hardlinks_fail_closed(self):
        self.prepare()
        root = self.context.configuration
        extra = root / "unexpected.rules"
        extra.write_text("unreviewed rule")
        self.assertIn("differs", steps.DerivedConfiguration(self.context).check(self.system).problem)
        extra.unlink()
        extra.mkdir()
        self.assertIn("differs", steps.DerivedConfiguration(self.context).check(self.system).problem)
        extra.rmdir()
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("leave unchanged")
        (root / "linked").symlink_to(outside)
        self.assertIn("unsafe entry", steps.DerivedConfiguration(self.context).check(self.system).problem)
        (root / "linked").unlink()
        os.link(root / "versions.json", outside.with_name("hardlink"))
        self.assertIn("hard-linked", steps.DerivedConfiguration(self.context).check(self.system).problem)
        self.assertEqual(outside.read_text(), "leave unchanged")

    def test_boot_readiness_units_deploy_and_verification_share_the_frozen_build(self):
        root = self.context.configuration
        cached_program = self.site / "var/application-releases" / self.application["digest"] / "bin/thermite"
        prefix = (sys.executable, str(cached_program), "--site", str(self.site))
        self.assertEqual(steps.BootTest(self.context).actions(self.system)[0].argv,
                         (*prefix, "test", "boot", "--conf", str(root / "conf"), "--versions", str(root / "versions.json")))
        self.assertEqual(steps.NativeReady(self.context).required, {"binding-http", "binding-network", "misc-metrics"})
        self.system.read_bytes.side_effect = lambda path: next((self.files[name].data for name, target in deploy.INSTALLED.items() if path == target), None)
        self.assertEqual(steps.Units(self.context).stale(self.system), [])
        self.system.read_bytes.side_effect = None
        self.system.read_bytes.return_value = None
        self.system.stat.return_value = None
        installs = [command for command in steps.Units(self.context).actions(self.system) if command.argv[:3] == ("install", "-m", "0644")]
        self.assertTrue(installs)
        self.assertEqual({name for command in installs for name in command.argv[5:]},
                         {str(root / name) for name in deploy.INSTALLED})
        deploy_actions = steps.Deploy(self.context).actions(self.system)
        self.assertEqual(len(deploy_actions), 1)
        self.assertEqual(deploy_actions[0].argv, (*prefix, "deploy", self.context.head, "--state", self.host["state_root"], "--_configuration-phase"))
        self.assertIn("--yes", deploy_actions[0].approved_argv)
        self.assertTrue(deploy_actions[0].terminal)
        verification = steps.Verify(self.context).actions(self.system)[0]
        self.assertIn(str(root / "conf/services/addons.cfg"), verification.argv)
        self.assertEqual(verification.argv[-2:], ("--revision", self.context.head))
        self.assertEqual(steps.Verify(self.context).actions(self.system)[1].argv[-2:], ("--revision", self.context.head))
        self.assertTrue(verification.show_output)
        sequence = steps.install(self.context, preflight=False)
        self.assertIsInstance(sequence[0], steps.DerivedConfiguration)
        self.assertLess(next(i for i, step in enumerate(sequence) if isinstance(step, steps.DerivedConfiguration)),
                        next(i for i, step in enumerate(sequence) if isinstance(step, steps.BootTest)))

    def test_boot_uses_explicit_frozen_versions_and_records_that_conf_digest(self):
        self.prepare()
        args = SimpleNamespace(json=True, versions=self.context.configuration / "versions.json",
                               conf=self.context.configuration / "conf")
        with patch.object(boot.lib, "load_versions", return_value=self.versions) as pins, \
                patch.object(boot.lib, "boot_check_instance"), patch.object(boot.lib, "InstanceLock", return_value=nullcontext()), \
                patch.object(boot.lib, "running_pgid", return_value=None), patch.object(boot.lib, "mirror_conf"), \
                patch.object(boot.lib, "fetch"), patch.object(boot, "run_check", return_value={"failures": []}), \
                patch.object(deploy, "record_boot_check") as receipt, patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(boot.run(args), 0)
        pins.assert_called_once_with(args.versions)
        self.assertEqual(receipt.call_args.args[1], self.context.conf_digest)

    def test_schema_one_and_source_checkouts_keep_their_previous_installer_commands(self):
        legacy = dataclasses.replace(self.context, prepared=None, application=None)
        with patch.object(oh_paths, "PROGRAM", self.site / "installed app"):
            self.assertEqual(steps.BootTest(legacy).actions(self.system)[0].argv[-2:], ("test", "boot"))
            self.assertEqual(len(steps.Deploy(legacy).actions(self.system)), 4)
        self.assertEqual(steps.configuration_steps(legacy), [])

    def test_standalone_deploy_destination_uses_selected_committed_host(self):
        with patch.object(deploy_cli, "AREA", self.site), patch.object(deploy, "resolve", return_value=self.context.head), \
                patch.object(deploy, "export", return_value={oh_paths.SITE_FILE: deploy.Blob(b"manifest")}), \
                patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, self.requirement)) as inputs, \
                patch.object(deploy, "load", side_effect=AssertionError("mutable destination used")):
            self.assertEqual(deploy_cli.deployment_host("reviewed-commit"), self.host)
        inputs.assert_called_once_with(self.site, self.context.head)

    def test_rollback_history_waives_boot_only_for_the_exact_rendered_digest(self):
        args = SimpleNamespace(commit=self.context.head, force=False, dry_run=True)
        result = SimpleNamespace(commit=self.context.head, subject="fixture", conf=deploy.Changes(),
                                 atv_app=deploy.Changes(), with_metrics=False)
        for digest, accepted in ((self.context.conf_digest, True), ("different output", False), (None, False)):
            entry = {"commit": self.context.head, "number": 1, "deployed_at": "earlier", "conf_digest": digest}
            with self.subTest(digest=digest), patch.object(rollback_cli, "AREA", self.site), \
                    patch.object(deploy, "resolve", return_value=self.context.head), \
                    patch.object(deploy, "read_record", return_value={"commit": "c" * 40}), \
                    patch.object(deploy, "deploys", return_value=[entry]), \
                    patch.object(deploy, "export", return_value={oh_paths.SITE_FILE: deploy.Blob(b"manifest")}), \
                    patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, self.requirement)), \
                    patch.object(deploy, "prepared_files", return_value=(self.files, self.application)), \
                    patch.object(rollback_cli, "warn_units", return_value=[]), \
                    patch.object(deploy, "deploy", return_value=result) as applied, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(rollback_cli.roll_back(args, deploy.State(Path(self.host["state_root"])), host=self.host), 0)
                self.assertEqual(applied.call_args.kwargs["force"], accepted)
                self.assertEqual(applied.call_args.kwargs.get("expected_conf_digest"), self.context.conf_digest if accepted else None)

    def test_schema2_unit_status_reads_the_selected_snapshot_without_top_level_deploy(self):
        name = next(iter(deploy.INSTALLED))
        target = Path(self.temporary.name) / "installed.unit"
        target.write_bytes(self.files[name].data)
        with patch.object(deploy, "resolve", return_value=self.context.head) as selected, \
                patch.object(deploy, "export", return_value={oh_paths.SITE_FILE: deploy.Blob(json.dumps(self.requirement).encode())}), \
                patch.object(deploy, "prepared_files", return_value=(self.files, self.application)) as prepared:
            self.assertEqual(deploy.installed_units(self.site, {name: target}, rev="reviewed"), [(name, target, "current")])
            selected.assert_called_with(self.site, "reviewed")
            prepared.assert_called_with(self.site, self.context.head, self.site / "var")
            target.write_text("different installed bytes")
            self.assertEqual(deploy.installed_units(self.site, {name: target})[0][2], "differs")
            target.unlink()
            self.assertEqual(deploy.installed_units(self.site, {name: target})[0][2], "missing")
        self.assertFalse((self.site / "deploy").exists())

    def test_final_health_uses_frozen_host_and_required_addons_without_writing_a_cache(self):
        token = Path(self.temporary.name) / "operator-token"
        token.write_text("fixture")
        args = SimpleNamespace(token_file=token, installation=True, addons=None, revision=self.context.head)
        with patch.object(health, "AREA", self.site), \
                patch.object(deploy, "resolve", return_value=self.context.head), \
                patch.object(deploy, "export", return_value={oh_paths.SITE_FILE: deploy.Blob(b"manifest")}), \
                patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, self.requirement)), \
                patch.object(deploy, "prepared_files", return_value=(self.files, self.application)) as prepared, \
                patch.object(deploy, "load", side_effect=AssertionError("mutable host used")):
            health.production(args)
        self.assertEqual(args.url, "http://127.0.0.1:13601")
        self.assertEqual(args.host, self.host)
        self.assertEqual(args._addons_text, self.files["conf/services/addons.cfg"].data.decode())
        prepared.assert_called_once_with(self.site, self.context.head, self.site / "var")
        self.assertFalse(self.context.configuration.exists())

    def test_status_uses_one_selected_revision_for_host_and_unit_comparison(self):
        with patch.object(status, "AREA", self.site), \
                patch.object(deploy, "resolve", return_value=self.context.head), \
                patch.object(deploy, "export", return_value={oh_paths.SITE_FILE: deploy.Blob(b"manifest")}), \
                patch.object(deploy, "committed_inputs", return_value=(self.versions, self.host, self.requirement)), \
                patch.object(deploy, "load", side_effect=AssertionError("mutable host used")), \
                patch.object(status, "show", return_value=0) as show:
            self.assertEqual(status.main(["--revision", self.context.head]), 0)
            args, host = show.call_args.args
            self.assertEqual((args.revision, host), (self.context.head, self.host))
        with patch.object(deploy, "read_record", return_value={}), patch.object(deploy, "installed_units", return_value=[]) as units, \
                patch.object(deploy, "service_states", return_value={}), patch.object(deploy, "openhab_version", return_value=None), \
                patch.object(deploy, "lan_address", return_value=None), patch.object(deploy, "hostname", return_value="fixture"), \
                patch.object(status, "show_metrics"), patch.object(deploy, "deploys", return_value=[]), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(status.show(args, host), 0)
            units.assert_called_once_with(status.AREA, rev=self.context.head)

    def test_symbolic_deploy_and_rollback_targets_are_pinned_before_the_body(self):
        with patch.object(deploy_cli, "deployment_selection", return_value=(self.context.head, self.host, self.requirement)), \
                patch.object(deploy_cli, "apply", return_value=0) as apply, patch.object(oh_paths, "require_compatible"):
            self.assertEqual(deploy_cli.main(["HEAD", "--dry-run", "--_configuration-phase"]), 0)
            self.assertEqual(apply.call_args.args[0].rev, self.context.head)
        with patch.object(rollback_cli, "deployment_selection", return_value=(self.context.head, self.host, self.requirement)), \
                patch.object(rollback_cli, "roll_back", return_value=0) as apply:
            self.assertEqual(rollback_cli.main(["previous", "--dry-run"]), 0)
            self.assertEqual(apply.call_args.args[0].commit, self.context.head)
            self.assertEqual(apply.call_args.args[1].root, Path(self.host["state_root"]))

    def test_privileged_helper_paths_use_the_frozen_application(self):
        program = self.site / "var/application-releases" / self.application["digest"]
        with patch.object(oh_paths, "PROGRAM", self.site / "wrong mutable installation"), \
                patch.object(steps.ownership, "inspect", return_value=steps.ownership.Owned()), \
                patch.object(deploy, "prepare_openhab_argv", return_value=("fixture-prepare",)):
            self.assertEqual(self.context.program, program)
            actions = steps.NativePreparation(self.context).actions(self.system)
            helpers = [part for action in actions for part in action.argv if part.endswith(".py")]
            self.assertTrue(helpers)
            self.assertTrue(all(part.startswith(str(program / "lib") + "/") for part in helpers))
            secret = steps._install_secret(self.context, Path("source"), Path("destination"))
            self.assertEqual(secret.argv[1], str(program / "lib/oh_install/secrets.py"))

    def test_explicit_restart_is_one_approved_action_then_normal_readiness(self):
        context = dataclasses.replace(self.context, restart_runtime=True)
        service = steps.Services(context, restart_runtime=True)
        self.system.unit.return_value = SimpleNamespace(active="active", enabled="enabled", since=10)
        self.system.stat.return_value = SimpleNamespace(mtime=0)
        self.system.interactive.return_value = True
        self.assertEqual(steps.Services(context).plan(self.system), ([], [], False),
                         "the rollout flag must not make native-preparation's internal plan request another restart")
        with patch.object(service, "prefetch", side_effect=lambda *_: []):
            self.assertFalse(service.check(self.system).done)
            actions = service.actions(self.system)
            restart, receipt = actions
            self.assertEqual(restart.argv, ("systemctl", "restart", "thermite-openhab.service"))
            self.assertFalse(service.runtime_restarted, "listing a dry-run action must not complete it")
            self.system.run.return_value = 1
            self.assertEqual(runner.perform(restart, self.system, self.site, root_confirmed=True), 1)
            self.assertFalse(service.check(self.system).done)
            self.system.run.return_value = 0
            self.assertEqual(runner.perform(restart, self.system, self.site, root_confirmed=True), 0)
            self.assertEqual(runner.perform(receipt, self.system, self.site, root_confirmed=True), 0)
            self.assertTrue(service.check(self.system).done)
            self.assertEqual(service.actions(self.system), [])
            self.system.ask.assert_not_called()
        sequence = steps.install(context, preflight=False)
        location = lambda kind: next(i for i, step in enumerate(sequence) if type(step) is kind)
        self.assertLess(location(steps.Deploy), location(steps.Services))
        self.assertLess(location(steps.Services), location(steps.Answers))
        self.assertLess(location(steps.Services), location(steps.NativeReady))
        self.assertLess(location(steps.Services), location(steps.Verify))
        with patch.object(install_cli.os, "geteuid", return_value=1000), \
                patch.object(install_cli, "install", return_value=0) as run, patch.object(oh_paths, "require_compatible"):
            self.assertEqual(install_cli.main(["--restart-runtime", "--dry-run", "--_host-phase"]), 0)
            self.assertTrue(run.call_args.kwargs["restart_runtime"])


if __name__ == "__main__":
    unittest.main()
