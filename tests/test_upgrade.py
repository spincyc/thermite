"""Application-only upgrades use the same checked runtime workflow.

The host and deployment are in-memory fixtures; no native commands run.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
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
from oh_install import ownership, runner, steps  # noqa: E402
from oh_install.model import Check, Task  # noqa: E402
from oh_install.model import Command  # noqa: E402
from oh_install.system import Stat, System, Unit  # noqa: E402


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

    def test_current_native_deployment_still_verifies_gateway_and_ingress(self):
        plan = self.plan()
        self.assertTrue(plan.current)
        self.assertIsNone(plan.deployment)
        self.assertFalse(plan.restart_runtime)
        with self.planning(), patch.object(upgrade_cli, "AREA", self.site), \
                patch.object(steps, "committed_context", return_value=self.context), \
                patch.object(upgrade_cli, "installed", return_value=True), \
                patch.object(runner, "run", return_value=0) as run, \
                patch.object(runner, "confirm_run") as confirm:
            self.assertEqual(upgrade_cli.upgrade(self.system, False), 0)
        self.assertEqual(run.call_count, 2, "preflight and complete host verification are required")
        sequence = run.call_args.args[0]
        self.assertTrue(any(isinstance(step, steps.Gateway) for step in sequence))
        self.assertTrue(any(isinstance(step, steps.NativeIngress) for step in sequence))
        self.assertIsInstance(sequence[-1], steps.Verify)
        self.assertTrue(run.call_args.kwargs["root_confirmed"])
        confirm.assert_called_once()
        self.system.run.assert_not_called()
        deployment = next(step for step in sequence if isinstance(step, steps.Deploy))
        self.assertEqual((deployment.how, deployment.backup),
                         ("upgrade", next(step for step in sequence if isinstance(step, steps.Backup))),
                         "the history records an upgrade with the state backup it took")

    def test_current_native_deployment_propagates_gateway_failure(self):
        with self.planning(), patch.object(upgrade_cli, "AREA", self.site), \
                patch.object(steps, "committed_context", return_value=self.context), \
                patch.object(upgrade_cli, "installed", return_value=True), \
                patch.object(runner, "run", side_effect=[0, 7]) as run, \
                patch.object(upgrade_cli, "going_back"), \
                patch.object(runner, "confirm_run", return_value=True) as confirm:
            self.assertEqual(upgrade_cli.upgrade(self.system, False), 7)
        self.assertTrue(any(isinstance(step, steps.Gateway) for step in run.call_args.args[0]))
        confirm.assert_called_once()
        self.system.run.assert_not_called()

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

    def test_invalid_deployment_record_requires_redeployment(self):
        self.record = []
        self.assertFalse(steps.Deploy(self.context).check(self.system).done)

    def workflow(self, *, dry_run=False, fail=None):
        self.record["commit"] = "d" * 40
        plan = self.plan()
        sequence = steps.upgrade(self.context, plan, "fixture-stamp")
        visited, completed = [], set()

        def work(name):
            visited.append(name)
            if fail == name:
                return 7
            completed.add(name)
            if name == "deploy":
                self.record["commit"] = self.context.head
                self.record_time = 30
            return 0

        def run(argv, area):
            self.assertEqual(area, self.site)
            self.assertEqual(argv, ("sudo", "systemctl", "restart", oh_units.RUNTIME))
            result = work("restart")
            if not result:
                self.runtime_time = 40
            return result

        self.system.run.side_effect = run
        with ExitStack() as scope:
            scope.enter_context(patch.object(upgrade_cli, "AREA", self.site))
            scope.enter_context(patch.object(steps, "committed_context", return_value=self.context))
            scope.enter_context(patch.object(ownership, "inspect", return_value=ownership.Owned(recorded=True)))
            scope.enter_context(patch.object(upgrade_cli, "installed", return_value=True))
            scope.enter_context(patch.object(steps, "plan", return_value=plan))
            scope.enter_context(patch.object(steps, "upgrade", return_value=sequence))
            scope.enter_context(patch.object(steps.Preconditions, "check", return_value=Check(True, "fixture")))
            scope.enter_context(patch.object(steps.Committed, "check", return_value=Check(True, "fixture")))
            confirm = scope.enter_context(patch.object(runner, "confirm_run", return_value=True))
            scope.enter_context(patch.object(upgrade_cli, "going_back"))
            for step in sequence:
                if isinstance(step, steps.Restart):
                    scope.enter_context(patch.object(step, "prefetch", return_value=[]))
                    restart = step
                    continue
                if isinstance(step, steps.Backup):
                    scope.enter_context(patch.object(step, "completed", return_value=False))
                name = {steps.BootTest: "boot", steps.Deploy: "deploy", steps.NativeReady: "readiness",
                        steps.Verify: "verify"}.get(type(step))
                if name:
                    scope.enter_context(patch.object(step, "check", side_effect=lambda _, name=name:
                                                    Check(name in completed, "fixture " + name)))
                    scope.enter_context(patch.object(step, "actions", side_effect=lambda _, name=name:
                                                    [Task("fixture " + name, lambda _: work(name))]))
                else:
                    scope.enter_context(patch.object(step, "check", return_value=Check(True, "fixture current")))
                    scope.enter_context(patch.object(step, "actions", return_value=[]))
            code = upgrade_cli.upgrade(self.system, dry_run, when="fixture-stamp")
        return code, visited, restart, confirm

    def test_application_only_upgrade_boots_deploys_restarts_once_and_checks_readiness(self):
        code, visited, restart, confirm = self.workflow()
        self.assertEqual(code, 0)
        self.assertEqual(visited, ["boot", "deploy", "restart", "readiness", "verify"])
        confirm.assert_called_once()
        self.system.run.assert_called_once()
        self.assertTrue(restart.runtime_restarted)
        self.assertTrue(restart.check(self.system).done)
        self.assertTrue(self.plan().current)

    def test_dry_run_does_not_deploy_restart_or_request_approval(self):
        code, visited, restart, confirm = self.workflow(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(visited, [])
        self.assertEqual(self.record["commit"], "d" * 40)
        self.assertFalse(restart.runtime_restarted)
        confirm.assert_not_called()
        self.system.run.assert_not_called()
        self.assertTrue(any("would" in call.args[0] for call in self.system.say.call_args_list))

    def test_failed_stage_stops_before_later_deploy_restart_or_readiness(self):
        order = ["boot", "deploy", "restart", "readiness"]
        for failed in order:
            with self.subTest(failed=failed):
                self.setUp()
                code, visited, restart, _ = self.workflow(fail=failed)
                self.assertEqual(code, 7)
                self.assertEqual(visited, order[:order.index(failed) + 1])
                if failed == "restart":
                    self.assertFalse(restart.runtime_restarted)
                    self.assertTrue(self.plan().restart_runtime, "retry must finish the interrupted restart")


if __name__ == "__main__":
    unittest.main()
