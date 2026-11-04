"""The public commands always enter the whole application/host workflow."""
from __future__ import annotations

import os
import json
import argparse
import secrets
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
from oh_install import steps
from oh_install.access import Gateway
from oh_install.ingress import NativeIngress, NativeIngressPrerequisites, unit_text


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
                                {"state_root": "/srv/fictional", "uid": 9001, "gid": 9001},
                                "a" * 40, "b" * 64, "fixture", None,
                                prepared={"conf/services/addons.cfg": oh_deploy.Blob(b"binding = http\n")},
                                application={"version": "0.1.0.dev0", "digest": "c" * 64})
        plan = Mock()
        for sequence in (steps.install(context), steps.upgrade(context, plan, "fixture")):
            types = [type(step) for step in sequence]
            native = steps.Restart if steps.Restart in types else steps.Services
            self.assertLess(types.index(NativeIngress), types.index(native))
            self.assertLess(types.index(steps.NativeReady), types.index(Gateway))
            self.assertLess(types.index(Gateway), types.index(steps.Verify))
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
