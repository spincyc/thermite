"""The public commands always enter the whole application/host workflow."""
from __future__ import annotations

import os
import json
import argparse
import secrets
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_application_update
import oh_paths
from oh_cli import deploy, install, upgrade
from oh_install import runner, steps
from oh_install.access import Gateway
from oh_install.ingress import NativeIngress, NativeIngressPrerequisites, unit_text
from oh_install.model import Check, Task
from oh_install.system import NativeProbe, System, Unit


class UnifiedInstall(unittest.TestCase):
    def test_local_gateway_profile_connects_terminal_without_granting_a_credential(self):
        from oh_cli import remote
        from oh_access import client
        arguments = argparse.Namespace(mock=False, native=False, url=None, sandbox=False)
        profile = {"url": "https://fixture.example:13600", "ca_file": "/fictional/public-cert.pem"}
        credential = AREA / ".scratch/unified-root/absent-personal-token.json"
        with patch.object(client, "deployment", return_value=profile), \
                patch.object(client, "credential_path", return_value=credential), \
                patch.object(client, "saved_ca", return_value=None), \
                patch.object(remote, "model_file", return_value=None), \
                patch.object(remote, "look", return_value="fixture"), \
                patch.dict(os.environ, {}, clear=True):
            self.assertFalse(remote.native_mode(arguments))
            self.assertEqual(remote.target(arguments), ["--url", profile["url"]])
            environment = remote.launch_environment(arguments, None)
            self.assertEqual(environment["NODE_EXTRA_CA_CERTS"], profile["ca_file"])
            self.assertNotIn("THERMITE_REMOTE_TOKEN_FILE", environment)
            with patch.object(client, "saved_ca", side_effect=ValueError("retired old certificate")):
                self.assertEqual(remote.launch_environment(arguments, None)["NODE_EXTRA_CA_CERTS"], profile["ca_file"])
            arguments.url = "https://elsewhere.example:13600"
            self.assertEqual(remote.target(arguments), ["--url", arguments.url])
            self.assertNotIn("NODE_EXTRA_CA_CERTS", remote.launch_environment(arguments, None))

    def test_local_profile_rejects_native_urls_invalid_certificates_and_extra_credentials(self):
        from oh_access import client, AccessError
        profile = dict(version=1, url="https://fixture.example:13600", ca_file="/fictional/public-cert.pem",
                       release="/fictional/release", state="/fictional/state", versions={}, journal="/fictional/journal")
        with patch.object(client, "read_private", return_value=json.dumps(profile).encode()), \
                patch.object(client.ssl, "create_default_context"):
            self.assertEqual(client.deployment(AREA), {"url": profile["url"], "ca_file": profile["ca_file"]})
        for changed in ({**profile, "url": "http://fixture.example"}, {**profile, "token": secrets.token_urlsafe(32)},
                        {**profile, "ca_file": "relative.pem"}):
            with patch.object(client, "read_private", return_value=json.dumps(changed).encode()), \
                    self.assertRaises(AccessError):
                client.deployment(AREA)
        with patch.object(client, "read_private", return_value=json.dumps(profile).encode()), \
                patch.object(client.ssl, "create_default_context", side_effect=OSError), self.assertRaises(AccessError):
            client.deployment(AREA)

    def test_public_commands_acquire_application_once_and_forward_options(self):
        for module, operation in ((install, "install"), (upgrade, "upgrade")):
            with self.subTest(operation=operation), patch.object(module.os, "geteuid", return_value=1000), \
                    patch.object(oh_application_update, operation, return_value=0) as apply, \
                    patch.object(module, operation) as host:
                self.assertEqual(module.main(["--dry-run", "--source", "/fictional/application", "--revision", "fixture"]), 0)
                apply.assert_called_once()
                self.assertEqual(apply.call_args.kwargs["source"], Path("/fictional/application"))
                self.assertEqual(apply.call_args.kwargs["revision"], "fixture")
                self.assertTrue(apply.call_args.kwargs["dry_run"])
                host.assert_not_called()

    def test_candidate_host_phase_checks_compatibility_without_recursive_update(self):
        for module, operation in ((install, "install"), (upgrade, "upgrade")):
            with self.subTest(operation=operation), patch.object(module.os, "geteuid", return_value=1000), \
                    patch.object(oh_application_update, operation) as apply, \
                    patch.object(oh_paths, "require_compatible") as compatible, \
                    patch.object(module, operation, return_value=0) as host:
                self.assertEqual(module.main(["--_host-phase", "--_run-approved"]), 0)
                compatible.assert_called_once()
                host.assert_called_once()
                self.assertTrue(host.call_args.kwargs["run_approved"])
                apply.assert_not_called()

    def test_failed_candidate_child_returns_its_status(self):
        for module, operation in ((install, "install"), (upgrade, "upgrade")):
            with self.subTest(operation=operation), patch.object(module.os, "geteuid", return_value=1000), \
                    patch.object(oh_application_update, operation, side_effect=subprocess.CalledProcessError(7, ["fixture"])):
                self.assertEqual(module.main([]), 7)

    def test_legacy_deploy_alias_cannot_partially_update_native_configuration(self):
        with patch.object(install, "main", return_value=0) as whole, patch.object(deploy, "apply") as partial:
            self.assertEqual(deploy.main(["--dry-run"]), 0)
            whole.assert_called_once_with(["--dry-run"])
            self.assertEqual(deploy.main(["--force"]), 2)
            partial.assert_not_called()

    def test_setup_and_migration_keep_service_descriptor_option(self):
        for action in ("setup", "migrate", "serve", "worker", "verify", "callbacks", "bootstrap"):
            with self.subTest(action=action), patch.dict(os.environ, {}, clear=False), patch.object(oh_paths, "SITE"):
                args = ["access", action, "--config", "/fictional/service/config.json"]
                self.assertEqual(oh_paths.select_args(args), args)

    def test_both_workflows_apply_ingress_before_native_and_gateway_before_completion(self):
        import oh_deploy
        context = steps.Context(AREA, {"openhab": {"version": "5.2.3"}},
                                {"root": "/srv/fictional", "uid": 9001, "gid": 9001},
                                "a" * 40, "b" * 64, "fixture", None,
                                prepared={"conf/services/addons.cfg": oh_deploy.Blob(b"binding = http\n")},
                                application={"version": "0.1.0.dev0", "digest": "c" * 64})
        plan = Mock()
        for sequence in (steps.install(context), steps.upgrade(context, plan, "fixture")):
            types = [type(step) for step in sequence]
            native = steps.Restart if steps.Restart in types else steps.Services
            self.assertLess(types.index(NativeIngress), types.index(native))
            self.assertLess(types.index(steps.Answers), types.index(Gateway))
            self.assertLess(types.index(steps.Token), types.index(Gateway))
            self.assertLess(types.index(Gateway), types.index(steps.NativeReady))
            self.assertLess(types.index(steps.Target), types.index(steps.NativeReady))
            self.assertEqual(types.count(steps.NativeReady), 1, "one authoritative wait owns the deadline and recovery budget")
            self.assertEqual(types.index(steps.NativeReady) + 1, types.index(steps.MetricsAnswer))
            self.assertEqual(types.index(steps.MetricsAnswer) + 1, types.index(steps.Verify))
            self.assertTrue(sequence[types.index(Gateway)].stop_on_failure)
        for sequence in (steps.install(context), steps.upgrade(context, plan, "fixture"), steps.clean_install(context)):
            types = [type(step) for step in sequence]
            probe = types.index(NativeIngressPrerequisites)
            self.assertLess(types.index(steps.Packages), probe)
            for disruptive in (steps.BootTest, steps.Backup, steps.Units, steps.NativePreparation,
                               steps.Stop, steps.RemoveUnits, steps.Purge, steps.OperatorCredentialsReset):
                if disruptive in types:
                    self.assertLess(probe, types.index(disruptive))
            self.assertTrue(sequence[probe].stop_on_failure)
        unit = unit_text()
        self.assertIn("Before=thermite-openhab.service", unit)
        self.assertNotIn("ExecStop=", unit, "stopping services must retain the native filter")


class GatewayReadinessLifecycle(unittest.TestCase):
    def workflow(self, operation, *, terminal=False, persistent=False, resumed=False):
        import oh_deploy
        import oh_sandbox
        context = steps.Context(AREA, {"openhab": {"version": "5.2.3"}},
                                {"root": "/srv/fictional", "uid": 9001, "gid": 9001, "http_port": 13601},
                                "a" * 40, "b" * 64, "fixture", None,
                                prepared={"conf/services/addons.cfg": oh_deploy.Blob(b"binding=sonos\n")},
                                application={"version": "0.1.0.dev0", "digest": "c" * 64})
        system = Mock(spec=System)
        now, events, active = [0.0], [], [False]
        system.clock.side_effect = lambda: now[0]
        system.sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        system.read_private.return_value = "fictional-runtime-token"
        system.interactive.return_value = True
        system.unit.return_value = Unit("active", "enabled", 0, 1)
        system.run.return_value = 0
        system.unit.side_effect = lambda *args, **kwargs: Unit("active", "enabled", 0, 2 if system.run.called else 1)

        def thing():
            if persistent or 5 <= now[0] < 10:
                state, detail = "UNINITIALIZED", "HANDLER_MISSING_ERROR"
            elif 10 <= now[0] < 15:
                state, detail = "UNINITIALIZED", "NONE"
            elif 15 <= now[0] < 20:
                state, detail = "INITIALIZING", "NONE"
            elif now[0] == 25:
                state, detail = "ONLINE", "NONE"
            else:
                state, detail = "OFFLINE", "COMMUNICATION_ERROR"
            if terminal and now[0] >= 5:
                state, detail = "OFFLINE", "CONFIGURATION_ERROR"
            return {"UID": "sonos:PLAY1:fixture", "statusInfo": {"status": state, "statusDetail": detail}}

        def probe(url, token, path, deadline, **kwargs):
            self.assertTrue(active[0], "readiness must follow gateway and callback activation")
            self.assertEqual(token, "fictional-runtime-token")
            self.assertEqual(deadline, 90)
            if path == steps.NativeReady.addons_path:
                return NativeProbe(200, [{"uid": "binding-sonos", "installed": True}])
            if path == steps.NativeReady.things_path:
                row = thing()
                events.append((now[0], row["statusInfo"]["status"], row["statusInfo"]["statusDetail"]))
                return NativeProbe(200, [row])
            self.assertEqual(path, steps.NativeReady.metrics_path)
            return NativeProbe(200)

        def gateway(_):
            events.append((now[0], "gateway-resumed" if resumed else "gateway-updated"))
            active[0] = True
            return 0

        def verify(_):
            self.assertNotEqual(oh_sandbox.installation_thing_verdict(thing()), "fail")
            events.append((now[0], "final-verification"))
            return 0

        system.native_probe.side_effect = probe
        sequence = steps.install(context) if operation == "install" else steps.upgrade(context, Mock(), "fixture")
        with ExitStack() as scope:
            scope.enter_context(patch.object(steps, "WAIT", 90))
            for step in sequence:
                if isinstance(step, steps.NativeReady):
                    continue
                scope.enter_context(patch.object(step, "check", return_value=Check(True, "fixture prerequisite")))
                action = gateway if isinstance(step, Gateway) else verify if isinstance(step, steps.Verify) else None
                scope.enter_context(patch.object(step, "actions", return_value=[Task("fixture phase", action)] if action else []))
            result = runner.run(sequence, system, AREA, root_confirmed=True)
        return result, now[0], events, system

    def test_gateway_reload_settles_before_install_upgrade_or_resume_can_complete(self):
        for operation in ("install", "upgrade"):
            for resumed in (False, True):
                with self.subTest(operation=operation, resumed=resumed):
                    result, elapsed, events, system = self.workflow(operation, resumed=resumed)
                    self.assertEqual(result, 0)
                    self.assertEqual(elapsed, 30, "the initial ready response must not hide a delayed handler reload")
                    self.assertEqual(events[-1], (30, "final-verification"))
                    self.assertIn((5, "UNINITIALIZED", "HANDLER_MISSING_ERROR"), events)
                    self.assertIn((10, "UNINITIALIZED", "NONE"), events)
                    self.assertIn((15, "INITIALIZING", "NONE"), events)
                    system.run.assert_not_called()

    def test_terminal_configuration_error_blocks_final_verification_without_restart(self):
        result, elapsed, events, system = self.workflow("upgrade", terminal=True)
        self.assertEqual(result, 1)
        self.assertEqual(elapsed, 5)
        self.assertFalse(any(event[-1] == "final-verification" for event in events))
        system.run.assert_not_called()

    def test_persistent_missing_handler_has_one_recovery_and_one_deadline(self):
        result, elapsed, events, system = self.workflow("upgrade", persistent=True)
        self.assertEqual(result, 1)
        self.assertEqual(elapsed, 90)
        self.assertFalse(any(event[-1] == "final-verification" for event in events))
        system.run.assert_called_once()
        self.assertEqual(system.run.call_args.args[0], ("sudo", "systemctl", "--no-block", "restart", "thermite-openhab.service"))
