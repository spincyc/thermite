"""thermite upgrade's host phase, and the checks its steps make.

The host and deployment are in-memory fixtures; no native commands run.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager, ExitStack, redirect_stderr
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_units  # noqa: E402
from oh_cli import upgrade as upgrade_cli  # noqa: E402
from oh_install import bridge, changeover, manifest, runner, steps  # noqa: E402
from oh_install.model import Check  # noqa: E402
from oh_install.system import Stat, System, Unit, UnitOrigin  # noqa: E402


class Upgrade(unittest.TestCase):
    def setUp(self):
        self.site = AREA / ".scratch/update-site/fictional-site"
        self.application = {"version": "0.1.0.dev0", "digest": "a" * 64}
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}},
            {"uid": 9001, "gid": 9001, "root": str(self.site / "installation")},
            "b" * 40, "c" * 64, "fixture-host", None,
            prepared={"conf/services/addons.cfg": deploy.Blob(b"binding = http,network\n")},
            application=self.application)
        self.record = {"commit": self.context.head, "application": self.application,
                       "conf_digest": self.context.conf_digest, "metrics": {}}
        self.record_time = 10
        self.runtime_time = 20
        self.system = Mock(spec=System)
        self.system.read_bytes.side_effect = lambda path: (
            json.dumps(self.record).encode() if path == self.context.layout.record else b"fixture unit")
        self.system.stat.side_effect = lambda path: Stat(
            9001, 9001, 0o750, path != self.context.layout.record,
            self.record_time if path == self.context.layout.record else 0)
        self.system.unit.side_effect = lambda name: Unit("active", "enabled", self.runtime_time)
        self.system.interactive.return_value = True
        self.system.run.side_effect = AssertionError("unapproved fixture command")

    def test_current_verification_needs_no_build_cache_and_preserves_group_approval(self):
        import oh_release
        program = self.site.parent / "installed-application"
        self.system.process_groups.return_value = set()
        self.system.which.return_value = None
        self.system.login.return_value = "fixture-owner"
        with patch.object(oh_release, "resolve", return_value=program) as resolve:
            actions = steps.Verify(self.context, materialized=False).actions(self.system)
        resolve.assert_called_once_with(self.context.var / "application-releases",
                                        self.application["digest"], self.application["version"])
        health, status = actions
        self.assertFalse(health.root)
        self.assertNotIn("--addons", health.argv)
        self.assertIn(str(program / "bin/thermite"), health.argv)
        self.assertIn(self.context.head, health.argv)
        self.assertTrue(status.root)
        self.assertEqual(status.argv[:2], ("-u", "fixture-owner"))
        self.system.ask.return_value = "n"
        self.assertIsNone(runner.perform(status, self.system, self.site))
        self.system.run.assert_not_called()

    @contextmanager
    def quiescing(self, *, change=None, running=True):
        """A host where Thermite runs (or not) and this run changes `change` (or nothing)."""
        changes = {"units": patch.object(steps.Units, "stale", return_value=[deploy.QUADLET] if change == "units" else []),
                   "native": patch.object(steps.NativePreparation, "check",
                                          return_value=Check(change != "native", "fixture")),
                   "ingress": patch.object(steps.NativeIngress, "current", return_value=change != "ingress"),
                   "services": patch.object(steps.Services, "plan", side_effect=lambda system: (
                       [], [oh_units.ATV] if change == "services" else [], False))}
        if change == "deploy":
            self.record["commit"] = "d" * 40
        self.system.unit_origins.side_effect = lambda names: {
            name: UnitOrigin(active="active" if running else "inactive") for name in names}
        with ExitStack() as scope:
            for patcher in changes.values():
                scope.enter_context(patcher)
            yield steps.Quiesce(self.context)

    def test_an_up_to_date_rerun_stops_nothing(self):
        with self.quiescing() as quiesce:
            found = quiesce.check(self.system)
        self.assertTrue(found.done)
        self.assertIn("keeps running", found.detail)

    def test_any_change_stops_all_of_thermite_then_confirms_the_slice_is_empty(self):
        for change, words in (("units", "service units"), ("native", "openHAB's native state"),
                              ("ingress", "native-port firewall"), ("deploy", "the deployment"),
                              ("services", "started before their units changed")):
            with self.subTest(change=change):
                self.setUp()
                with self.quiescing(change=change) as quiesce:
                    found = quiesce.check(self.system)
                    self.assertFalse(found.done)
                    self.assertIn(words, found.detail)
                    stop, confirm = quiesce.actions(self.system)
                    self.assertEqual(stop.argv, ("systemctl", "stop", oh_units.TARGET, oh_units.SLICE))
                    self.assertTrue(stop.root)
                    self.assertEqual(stop.stops, (oh_units.TARGET,), "a failed run starts the target as a whole")
                    self.system.unit_origins.side_effect = lambda names: {
                        name: UnitOrigin(active="inactive") for name in names}
                    self.assertEqual(confirm.perform(self.system), 0)
                    self.assertTrue(quiesce.after_actions(self.system).done)
                self.assertIn(oh_units.SLICE, self.system.unit_origins.call_args.args[0])

    def test_a_change_with_nothing_running_stops_nothing(self):
        with self.quiescing(change="deploy", running=False) as quiesce:
            self.assertTrue(quiesce.check(self.system).done)

    def test_anything_still_in_the_slice_after_the_stop_fails_clearly(self):
        with self.quiescing(change="units") as quiesce:
            self.system.unit_origins.side_effect = lambda names: {
                name: UnitOrigin(active="deactivating" if name == oh_units.SLICE else "inactive") for name in names}
            self.assertEqual(quiesce.stopped(self.system), 1)
            said = self.system.say.call_args.args[0]
            self.assertIn(f"{oh_units.SLICE} (deactivating)", said)
            self.assertIn(f"systemctl status {oh_units.SLICE}", said)
            self.system.unit_origins.side_effect = lambda names: {
                name: UnitOrigin(known=False, load_state="", active="unknown") for name in names}
            self.assertEqual(quiesce.stopped(self.system), 1, "an unknown state is never proof of a stop")

    def test_invalid_deployment_record_requires_redeployment(self):
        self.record = []
        self.assertFalse(steps.Deploy(self.context).check(self.system).done)


class HostPhase(unittest.TestCase):
    """thermite upgrade's host phase: what it resolves, refuses, or hands
    the install transaction (tests/test_txn_changeover.py runs that)."""

    def setUp(self):
        self.site = AREA / ".scratch/update-site/fictional-site"
        self.context = steps.Context(self.site, {"openhab": {"version": "5.2.3"}},
                                     {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"},
                                     "b" * 40, "c" * 64, "fixture-host", None,
                                     application={"version": "0.1.0.dev0", "digest": "a" * 64})
        self.system = Mock(spec=System)
        self.installed = {"transaction": "20261105T101500Z-3f9a2c"}
        scope = ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(patch.object(upgrade_cli, "AREA", self.site))
        scope.enter_context(patch.object(steps, "committed_context", return_value=self.context))
        self.resolve = scope.enter_context(patch.object(changeover, "resolve", return_value=None))
        self.manifest = scope.enter_context(patch.object(manifest, "installed", return_value=self.installed))
        self.bridge = scope.enter_context(patch.object(bridge, "needed", return_value=False))
        self.run = scope.enter_context(patch.object(runner, "run", return_value=0))
        self.confirm = scope.enter_context(patch.object(runner, "confirm_run", return_value=True))
        self.unchanged = scope.enter_context(patch.object(changeover, "unchanged", return_value=False))
        self.transaction = scope.enter_context(patch.object(changeover, "run", return_value=0))
        scope.enter_context(patch.object(changeover, "preview", return_value=0))
        scope.enter_context(patch.object(changeover, "install_work"))

    def upgrade(self, dry_run=False, **kwargs):
        with redirect_stderr(io.StringIO()) as stderr:
            code = upgrade_cli.upgrade(self.system, dry_run, **kwargs)
        return code, stderr.getvalue()

    def test_a_change_is_one_install_transaction(self):
        self.assertEqual(self.upgrade(site_before="1" * 40), (0, ""))
        self.confirm.assert_called_once()
        [(context, system, build)] = [self.transaction.call_args.args]
        self.assertEqual((context, system), (self.context, self.system))
        self.assertEqual(self.transaction.call_args.kwargs, {"installed": self.installed, "bridged": False,
                                                             "before": "1" * 40, "first": 3})
        with patch.object(changeover, "install_work") as work:
            build("20261105T101500Z-3f9a2c")
        work.assert_called_once_with(self.context, "20261105T101500Z-3f9a2c", kind="upgrade")

    def test_an_unchanged_version_is_only_checked(self):
        self.unchanged.return_value = True
        self.assertEqual(self.upgrade()[0], 0)
        self.transaction.assert_not_called()
        self.assertIsInstance(self.run.call_args.args[0][0], steps.Health)

    def test_an_open_transaction_is_resolved_first_and_nothing_else_runs(self):
        for outcome, code in ((changeover.ROLLED_BACK, changeover.ROLLED_BACK), (changeover.STUCK, changeover.STUCK),
                              (changeover.COMMITTED, changeover.REFUSED)):
            with self.subTest(outcome=outcome):
                self.resolve.return_value = outcome
                self.assertEqual(self.upgrade()[0], code, "this run installed nothing")
        self.manifest.assert_not_called()
        self.transaction.assert_not_called()
        self.resolve.return_value = None
        self.upgrade(dry_run=True)
        self.assertEqual(self.resolve.call_count, 3, "a dry run resolves nothing")

    def test_without_an_installed_manifest_it_refuses(self):
        self.manifest.return_value = None
        with patch.object(manifest, "thermite_present", return_value=[deploy.QUADLET]):
            code, said = self.upgrade()
        self.assertEqual(code, 2)
        self.assertIn(manifest.missing_recovery(self.context), said)
        with patch.object(manifest, "thermite_present", return_value=[]):
            code, said = self.upgrade()
        self.assertEqual((code, said), (2, "thermite upgrade: Thermite is not installed on this host yet: "
                                           "thermite install\n"))
        self.manifest.side_effect = manifest.ManifestError("/etc/thermite/installed.json is damaged")
        code, said = self.upgrade()
        self.assertEqual(code, 2)
        self.assertIn("is damaged", said)
        self.transaction.assert_not_called()

    def test_units_json_alone_is_bridged(self):
        self.manifest.return_value = None
        self.bridge.return_value = True
        self.assertEqual(self.upgrade()[0], 0)
        self.assertTrue(self.transaction.call_args.kwargs["bridged"])

    def test_a_rerun_that_resolves_undoes_its_own_pin_commit_first(self):
        import oh_source_upgrade
        arguments = Namespace(dry_run=False, runtime_token_file=None, _run_approved=True, native_timeout=None,
                              _site_before="1" * 40)
        order = []
        self.resolve.side_effect = lambda *args: order.append("resolve") or changeover.ROLLED_BACK
        with patch.object(oh_source_upgrade, "git", return_value="2" * 40), \
                patch.object(oh_source_upgrade, "reset_pin",
                             side_effect=lambda *args, **kwargs: order.append(kwargs) or True), \
                patch.object(changeover, "open_transaction", return_value="20261105T101500Z-3f9a2c"), \
                patch("oh_paths.require_compatible"):
            self.assertEqual(upgrade_cli.host_phase(self.system, arguments), changeover.ROLLED_BACK)
        self.assertEqual(order, [{"expected": "2" * 40, "previous": "1" * 40}, "resolve"])


class InstallHostPhase(unittest.TestCase):
    """thermite install's host phase: the same install transaction, with
    --clean inside it, the bridge, and the refusal without a manifest."""

    def setUp(self):
        from oh_cli import install as install_cli
        self.cli = install_cli
        self.site = AREA / ".scratch/update-site/fictional-site"
        self.context = steps.Context(self.site, {"openhab": {"version": "5.2.3"}},
                                     {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"},
                                     "b" * 40, "c" * 64, "fixture-host", None,
                                     application={"version": "0.1.0.dev0", "digest": "a" * 64})
        self.system = Mock(spec=System)
        self.system.login.return_value = "fixture-owner"
        self.system.process_groups.return_value = {9001}
        self.installed = {"transaction": "20261105T101500Z-3f9a2c", "installed": True}
        scope = ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(patch.object(install_cli, "AREA", self.site))
        scope.enter_context(patch.object(steps, "committed_context", return_value=self.context))
        self.resolve = scope.enter_context(patch.object(changeover, "resolve", return_value=None))
        self.manifest = scope.enter_context(patch.object(manifest, "installed", return_value=self.installed))
        self.bridge = scope.enter_context(patch.object(bridge, "needed", return_value=False))
        self.present = scope.enter_context(patch.object(manifest, "thermite_present", return_value=[]))
        self.run = scope.enter_context(patch.object(runner, "run", return_value=0))
        self.confirm = scope.enter_context(patch.object(runner, "confirm_run", return_value=True))
        self.unchanged = scope.enter_context(patch.object(changeover, "unchanged", return_value=False))
        self.transaction = scope.enter_context(patch.object(changeover, "run", return_value=0))

    def install(self, dry_run=False, **kwargs):
        return self.cli.install(self.system, dry_run, **kwargs)

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)

    def kind(self):
        build = self.transaction.call_args.args[2]
        with patch.object(changeover, "install_work") as work:
            build("20261105T101500Z-3f9a2c")
        return work.call_args.kwargs["kind"]

    def test_an_install_is_one_install_transaction_from_the_pin_it_made(self):
        self.assertEqual(self.install(site_before="1" * 40), 0)
        self.assertEqual(self.transaction.call_args.kwargs, {"installed": self.installed, "bridged": False,
                                                             "before": "1" * 40, "first": 3})
        self.assertEqual(self.kind(), "install")
        self.assertIn("Thermite's HTTPS gateway is installed", self.said())

    def test_clean_is_its_own_kind_in_the_same_transaction(self):
        self.assertEqual(self.install(clean=True), 0)
        self.assertEqual(self.kind(), "clean")
        self.assertEqual(self.confirm.call_args.args[1], "clean install")

    def test_an_unchanged_version_is_only_checked_unless_clean_or_restarted(self):
        self.unchanged.return_value = True
        self.assertEqual(self.install(), 0)
        self.transaction.assert_not_called()
        self.assertIsInstance(self.run.call_args.args[0][0], steps.Health)
        self.assertEqual(self.install(clean=True), 0)
        self.assertEqual(self.install(restart_runtime=True), 0)
        self.assertEqual(self.transaction.call_count, 2)

    def test_a_fresh_host_installs_and_units_json_alone_is_bridged(self):
        self.manifest.return_value = None
        self.assertEqual(self.install(), 0)
        self.assertFalse(self.transaction.call_args.kwargs["bridged"])
        self.bridge.return_value = True
        self.assertEqual(self.install(), 0)
        self.assertTrue(self.transaction.call_args.kwargs["bridged"])
        self.assertIn("first install transaction here", self.said())

    def test_thermite_files_without_a_manifest_refuse_with_the_recovery(self):
        self.manifest.return_value = None
        self.present.return_value = [deploy.QUADLET]
        self.assertEqual(self.install(), 2)
        self.assertIn(manifest.missing_recovery(self.context), self.said())
        self.manifest.side_effect = manifest.ManifestError("/etc/thermite/installed.json is damaged")
        self.assertEqual(self.install(), 2)
        self.assertIn("is damaged", self.said())
        self.transaction.assert_not_called()

    def test_an_open_transaction_is_resolved_first_and_nothing_else_runs(self):
        self.resolve.return_value = changeover.COMMITTED
        self.assertEqual(self.install(), changeover.REFUSED)
        self.resolve.assert_called_once_with(self.site, self.system, "install")
        self.manifest.assert_not_called()

    def test_a_dry_run_previews_the_transaction(self):
        with patch.object(changeover, "preview", return_value=0) as preview, \
                patch.object(changeover, "install_work") as work:
            self.assertEqual(self.install(dry_run=True, clean=True), 0)
        preview.assert_called_once()
        self.assertEqual(work.call_args.kwargs, {"kind": "clean"})
        self.resolve.assert_not_called()
        self.transaction.assert_not_called()


class RollbackHostPhase(unittest.TestCase):
    """thermite rollback: an install transaction of an earlier site commit,
    the site's branch left where it is (Q16)."""

    def setUp(self):
        from oh_cli import rollback as rollback_cli
        self.cli = rollback_cli
        self.site = AREA / ".scratch/update-site/fictional-site"
        application = {"version": "0.1.0.dev0", "digest": "a" * 64}
        self.head = steps.Context(self.site, {"openhab": {"version": "5.2.3"}},
                                  {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"},
                                  "b" * 40, "c" * 64, "fixture-host", None, application=application)
        self.earlier = steps.Context(**{**self.head.__dict__, "head": "e" * 40})
        self.system = Mock(spec=System)
        self.installed = {"transaction": "20261105T101500Z-3f9a2c", "installed": True,
                          "release": {"site": "b" * 40}}
        self.entries = [{"commit": "e" * 40, "number": 1, "deployed_at": "earlier", "current": False},
                        {"commit": "b" * 40, "number": 2, "deployed_at": "later", "current": True}]
        scope = ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(patch.object(rollback_cli, "AREA", self.site))
        self.contexts = scope.enter_context(patch.object(
            steps, "committed_context", side_effect=lambda area, system, revision=None: (
                self.earlier if revision else self.head)))
        scope.enter_context(patch.object(deploy, "deploys", return_value=self.entries))
        scope.enter_context(patch.object(deploy, "read_record", return_value={"commit": "b" * 40}))
        self.resolve = scope.enter_context(patch.object(changeover, "resolve", return_value=None))
        self.manifest = scope.enter_context(patch.object(manifest, "installed", return_value=self.installed))
        scope.enter_context(patch.object(bridge, "needed", return_value=False))
        scope.enter_context(patch.object(manifest, "thermite_present", return_value=[]))
        self.run = scope.enter_context(patch.object(runner, "run", return_value=0))
        self.confirm = scope.enter_context(patch.object(runner, "confirm_run", return_value=True))
        self.transaction = scope.enter_context(patch.object(changeover, "run", return_value=0))

    def test_the_commit_before_is_installed_without_moving_the_site(self):
        self.assertEqual(self.cli.roll_back(self.system, None, False), 0)
        self.assertEqual(self.contexts.call_args.kwargs, {"revision": "e" * 40})
        (context, _, build), options = self.transaction.call_args
        self.assertIs(context, self.earlier)
        self.assertEqual(options, {"installed": self.installed, "bridged": False, "before": "b" * 40,
                                   "after": "e" * 40, "first": 3, "pinned": False})
        with patch.object(changeover, "install_work") as work:
            build("20261105T101500Z-3f9a2c")
        self.assertEqual(work.call_args.kwargs, {"verb": "rollback"})
        committed = self.run.call_args.args[0][1]
        self.assertEqual((committed.head, committed.context.head), ("b" * 40, "e" * 40),
                         "HEAD stays clean and is not what is installed")

    def test_another_application_release_or_nothing_installed_refuses(self):
        self.earlier = steps.Context(**{**self.earlier.__dict__, "application": {"digest": "f" * 64}})
        with self.assertRaisesRegex(deploy.DeployError, "pins another application release"):
            self.cli.roll_back(self.system, None, False)
        self.earlier = steps.Context(**{**self.head.__dict__, "head": "e" * 40})
        self.manifest.return_value = None
        self.assertEqual(self.cli.roll_back(self.system, None, False), 2)
        self.transaction.assert_not_called()

    def test_an_open_transaction_is_resolved_first(self):
        self.resolve.return_value = changeover.ROLLED_BACK
        self.assertEqual(self.cli.roll_back(self.system, None, False), changeover.ROLLED_BACK)
        self.resolve.assert_called_once_with(self.site, self.system, "rollback")
        self.contexts.assert_not_called()

    def test_deploy_history_records_it_as_a_rollback(self):
        with patch.object(changeover, "release", return_value={}), patch.object(steps.NativeDeadline, "of"):
            work = changeover.install_work(self.earlier, "20261105T101500Z-3f9a2c", verb="rollback")
        deployment = next(step for step in work.switching if isinstance(step, steps.Deploy))
        self.assertEqual(deployment.trail(), ("--_how", "rollback"))
        self.assertEqual((work.kind, work.verb), ("install", "rollback"))


if __name__ == "__main__":
    unittest.main()
