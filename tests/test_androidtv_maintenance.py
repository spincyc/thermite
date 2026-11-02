from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

from tests import test_androidtv_owner_files as fixture

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_androidtv_patch import maintenance
from oh_androidtv_patch import owner_files as files


class Transaction(unittest.TestCase):
    """Real file transactions; the only fake is the service/OSGi boundary."""
    def setUp(self):
        fixture.OwnerFiles.setUp(self)
        self.staged = fixture.OwnerFiles.stage(self)
        self.manifest = files.load(self.package, self.staged["manifest_sha256"])
        self.calls = []
        outer = self
        class Adapter:
            running = True
            patched = False
            inject = None
            def preflight(self, manifest, *, allow_stopped=False):
                outer.calls.append("preflight")
                assert manifest["state"] == str(outer.state)
            def report(self, absent=False):
                return {"status": "ok", "things": [] if absent else [{"uid": "androidtv:googletv:fixture",
                        "editable": False, "disabled": True}],
                        "bundles": [{"id": 7, "version": "5.2.3.thermite1" if self.patched else "5.2.3", "state": 32}]}
            def probe(self, action):
                outer.calls.append(action)
                if not self.running:
                    raise AssertionError("probe without running native")
                if action == "refresh":
                    assert all(not (outer.state / value).exists() for value in files.GOOGLE.values())
                    if self.inject == "refresh":
                        raise files.OwnerFileError("native_probe_timeout")
                    value = self.report(True)
                    value["refreshed"] = True
                    return value
                value = self.report()
                if self.inject == "editable":
                    value["things"][0]["editable"] = True
                return value
            def stop(self):
                outer.calls.append("stop")
                # Sources MUST still exist at the moment the original service is stopped.
                if not outer.calls.count("start"):
                    assert all((outer.state / value).is_file() for value in files.GOOGLE.values())
                self.running = False
            def assert_stopped(self):
                if self.running:
                    raise AssertionError("file operation with old JVM alive")
            def is_running(self):
                return self.running
            def start(self):
                outer.calls.append("start")
                self.assert_stopped()
                assert all(not (outer.state / value).exists() for value in files.GOOGLE.values())
                self.patched = (outer.state / files.CORE["artifact"]).exists()
                self.running = True
            def assert_no_probe(self):
                outer.calls.append("probe_removed")
            def await_things(self, expected):
                outer.calls.append("await_things")
                assert all((outer.state / value).is_file() for value in files.GOOGLE.values())
                value = self.report()
                if self.inject == "flags":
                    value["things"][0]["disabled"] = False
                return value
        self.adapter = Adapter()
        self.verify = patch.object(maintenance, "verify_loaded", side_effect=lambda *a, **k: {"verified": True})
        self.verify.start()
        self.addCleanup(self.verify.stop)

    def operate(self, rollback=False):
        return maintenance.operate(self.package, self.staged["manifest_sha256"], self.adapter, rollback=rollback)

    def test_stop_first_apply_and_rollback_hold_sources_until_binding_verified(self):
        result = self.operate()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.calls, ["preflight", "inspect", "stop", "start", "refresh", "probe_removed", "await_things"])
        self.assertTrue(result["disabled"]["androidtv:googletv:fixture"])
        self.calls.clear()
        result = self.operate(rollback=True)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.calls, ["preflight", "inspect", "stop", "start", "refresh", "probe_removed", "await_things"])
        for relative, content in self.old.items():
            self.assertEqual((self.state / relative).read_bytes(), content)
        self.assertFalse((self.state / files.CORE["artifact"]).exists())

    def test_unknown_editable_thing_refuses_before_service_stop(self):
        self.adapter.inject = "editable"
        with self.assertRaisesRegex(files.OwnerFileError, "editable_or_invalid"):
            self.operate()
        self.assertEqual(self.calls, ["preflight", "inspect"])
        self.assertTrue(self.adapter.running)

    def test_failed_refresh_stops_and_leaves_google_quarantined_for_explicit_rollback(self):
        self.adapter.inject = "refresh"
        with self.assertRaisesRegex(files.OwnerFileError, "native_probe_timeout"):
            self.operate()
        self.assertFalse(self.adapter.running)
        self.assertTrue(all(not (self.state / value).exists() for value in files.GOOGLE.values()))
        self.assertEqual(json.loads((self.package / "transaction.json").read_text())["status"], "failed_stopped")
        self.adapter.inject = None
        self.assertEqual(self.operate(rollback=True)["status"], "rolled_back")

    def test_enabled_flag_drift_stops_before_any_unverified_running_state_is_accepted(self):
        self.adapter.inject = "flags"
        with self.assertRaisesRegex(files.OwnerFileError, "enabled_flags_changed"):
            self.operate()
        self.assertFalse(self.adapter.running)
        self.assertTrue(all(not (self.state / value).exists() for value in files.GOOGLE.values()))

    def test_repeated_apply_and_unrelated_rollback_edit_fail_closed(self):
        self.operate()
        with self.assertRaisesRegex(files.OwnerFileError, "transaction_already_started"):
            self.operate()
        path = self.state / files.CORE["overrides"]
        path.write_bytes(path.read_bytes() + b"# concurrent owner edit\n")
        with self.assertRaisesRegex(files.OwnerFileError, "rollback_concurrent_edit"):
            self.operate(rollback=True)
        self.assertIn(b"concurrent owner edit", path.read_bytes())

    def test_probe_source_has_no_command_trigger_credentials_or_generic_refresh_input(self):
        source = (AREA / "lib/oh_androidtv_patch/maintenance_probe.js").read_text()
        self.assertIn("current.length !== 0", source)
        self.assertIn("features.refreshFeatures(options)", source)
        for forbidden in ("ItemCommandTrigger", "sendCommand", "Authorization", "password", "karaf_command"):
            self.assertNotIn(forbidden, source.replace("passwords", "secrets"))

    def test_native_proof_requires_external_sha_complete_cleanup_and_exact_program(self):
        path = self.root / "proof.json"
        value = {"status": "pass", "checks": {"verified": True},
                 "cleanup": {str(i): True for i in range(10)}, "artifact_sha256": "a" * 64,
                 "program": maintenance.program_hashes(), "apply": {"status": "applied"},
                 "rollback": {"status": "rolled_back"}}
        path.write_text(json.dumps(value))
        expected = files.digest(path.read_bytes())
        self.assertEqual(maintenance.proof(path, expected, "a" * 64, maintenance=True)["sha256"], expected)
        with self.assertRaisesRegex(files.OwnerFileError, "external_sha_mismatch"):
            maintenance.proof(path, "b" * 64, "a" * 64, maintenance=True)
        value["cleanup"]["0"] = False
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(files.OwnerFileError, "native_proof_not_complete"):
            maintenance.proof(path, files.digest(path.read_bytes()), "a" * 64, maintenance=True)
        value["cleanup"]["0"] = True
        value["program"]["maintenance.py"] = "c" * 64
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(files.OwnerFileError, "maintenance_proof_program_mismatch"):
            maintenance.proof(path, files.digest(path.read_bytes()), "a" * 64, maintenance=True)
