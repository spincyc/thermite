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
from oh_install import bridge, changeover, manifest, ownership, runner, steps  # noqa: E402
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

    @contextmanager
    def planning(self):
        with ExitStack() as scope:
            scope.enter_context(patch.object(ownership, "inspect", return_value=ownership.Owned(recorded=True)))
            scope.enter_context(patch.object(steps.Services, "plan", return_value=([], [], False)))
            scope.enter_context(patch.object(steps.Units, "stale", return_value=()))
            scope.enter_context(patch.object(steps.Token, "saved", return_value=True))
            scope.enter_context(patch.object(steps.oh_apparmor, "enabled", return_value=False))
            for kind in (steps.Target, steps.Packages, steps.NodeConfinement,
                         steps.Preconditions, steps.Account, steps.NativePreparation):
                scope.enter_context(patch.object(kind, "check", return_value=Check(True, "fixture current")))
            scope.enter_context(patch.object(deploy, "unit_version", return_value="5.2.3"))
            scope.enter_context(patch.object(deploy, "unit_image", return_value="fixture-image"))
            scope.enter_context(patch.object(deploy, "image", return_value="fixture-image"))
            yield

    def plan(self):
        with self.planning():
            return steps.plan(self.context, self.system)

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

    def test_new_commit_application_or_configuration_requires_deployment(self):
        current = dict(self.record)
        for changed in ({"commit": "d" * 40},
                        {"application": {**self.application, "digest": "e" * 64}},
                        {"conf_digest": "f" * 64}):
            with self.subTest(changed=changed):
                self.record = {**current, **changed}
                plan = self.plan()
                self.assertFalse(plan.current)
                self.assertTrue(plan.deployment)
                self.assertTrue(plan.restart_runtime)
                self.assertEqual(plan.units, ())
                self.assertEqual(plan.services, ())
                self.assertIn("to deploy:", plan.describe())

    def test_interrupted_restart_is_required_even_after_deploy_record_is_current(self):
        self.record_time = 30
        plan = self.plan()
        self.assertFalse(plan.current)
        self.assertIsNone(plan.deployment)
        self.assertTrue(plan.restart_runtime)
        self.runtime_time = 40
        self.assertTrue(self.plan().current)

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

    def test_planning_names_no_listener_record(self):
        plan = self.plan()
        self.assertTrue(plan.current)
        self.assertFalse(any("listener" in item for item in plan.migration))

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
        self.transaction = scope.enter_context(patch.object(changeover, "upgrade", return_value=0))

    def upgrade(self, dry_run=False, **kwargs):
        with redirect_stderr(io.StringIO()) as stderr:
            code = upgrade_cli.upgrade(self.system, dry_run, **kwargs)
        return code, stderr.getvalue()

    def test_a_change_is_one_install_transaction(self):
        self.assertEqual(self.upgrade(site_before="1" * 40), (0, ""))
        self.confirm.assert_called_once()
        self.transaction.assert_called_once_with(self.context, self.system, installed=self.installed,
                                                 bridged=False, before="1" * 40, first=3)

    def test_an_unchanged_version_is_only_checked(self):
        self.unchanged.return_value = True
        self.assertEqual(self.upgrade()[0], 0)
        self.transaction.assert_not_called()
        self.assertIsInstance(self.run.call_args.args[0][0], steps.Health)

    def test_an_open_transaction_is_resolved_first_and_nothing_else_runs(self):
        self.resolve.return_value = changeover.ROLLED_BACK
        self.assertEqual(self.upgrade()[0], changeover.ROLLED_BACK)
        self.manifest.assert_not_called()
        self.transaction.assert_not_called()
        self.resolve.return_value = None
        self.upgrade(dry_run=True)
        self.assertEqual(self.resolve.call_count, 1, "a dry run resolves nothing")

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


if __name__ == "__main__":
    unittest.main()
