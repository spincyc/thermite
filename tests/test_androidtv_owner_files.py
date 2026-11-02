from pathlib import Path
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_androidtv_patch import ARTIFACT, pins
from oh_androidtv_patch import owner_files as owner


class OwnerFiles(unittest.TestCase):
    def setUp(self):
        scratch = AREA.parents[1] / ".scratch"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.old = {
            "userdata/etc/org.ops4j.pax.url.mvn.cfg": b"# preserved\norg.ops4j.pax.url.mvn.localRepository=/original/cache\norg.ops4j.pax.url.mvn.defaultRepositories=file:/system/@id=system.repository\n",
            "userdata/etc/overrides.properties": b"# unrelated\nmvn:example/another/1\n",
            "conf/yaml/googletv.yaml": b"version: 1\nthings:\n  androidtv:googletv:fixture:\n    config: {ipAddress: 192.0.2.1}\n",
            "conf/automation/js/androidtv_googletv.js": b"const SEND = true; // synthetic source\n",
            "userdata/androidtv/keep.keystore": b"synthetic sentinel, must never be opened by maintenance",
            "userdata/cache/retain.bin": b"cached runtime must remain untouched",
        }
        for relative, data in self.old.items():
            path = self.state / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.artifact = self.root / ARTIFACT
        self.artifact.write_bytes(b"approved synthetic derivative")
        self.receipt = self.root / "build.json"
        self.receipt.write_text(json.dumps({"upstream": "5.2.3", "patched": "5.2.3.thermite1",
            "artifact": {"sha256": owner.digest(self.artifact.read_bytes())},
            "inputs": {"bundle": pins()["assets"]["bundle"]["sha256"]}}))
        self.receipt_sha = owner.digest(self.receipt.read_bytes())
        self.package = self.root / "package"
        self.stopped = False

    def assert_stopped(self):
        if not self.stopped:
            raise owner.OwnerFileError("service_not_stopped")

    def stage(self):
        return owner.stage(self.state, self.artifact, self.receipt, self.receipt_sha, self.package)

    def test_stage_is_read_only_and_private_no_credential_or_cache_read(self):
        real = owner.snapshot
        seen = []
        def snapshot(path):
            seen.append(str(path))
            if "keep.keystore" in str(path) or "retain.bin" in str(path):
                raise AssertionError("unrelated userdata opened")
            return real(path)
        with patch.object(owner, "snapshot", side_effect=snapshot):
            result = self.stage()
        self.assertEqual(result["activation"], "not_started")
        self.assertEqual(result["google_things"], 1)
        self.assertEqual(self.package.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.package.iterdir()))
        for relative, before in self.old.items():
            self.assertEqual((self.state / relative).read_bytes(), before)
        self.assertFalse((self.state / owner.CORE["artifact"]).exists())
        self.assertFalse(any("androidtv/keep" in name or "cache/retain" in name for name in seen))

    def test_install_quarantine_restore_and_exact_rollback_require_service_gate(self):
        result = self.stage()
        manifest = owner.load(self.package, result["manifest_sha256"])
        for action in (owner.install_core, owner.quarantine_google, owner.rollback_files):
            with self.assertRaisesRegex(owner.OwnerFileError, "service_not_stopped"):
                action(self.package, manifest, assert_stopped=self.assert_stopped)
        self.stopped = True
        owner.quarantine_google(self.package, manifest, assert_stopped=self.assert_stopped)
        self.assertTrue(all(not (self.state / relative).exists() for relative in owner.GOOGLE.values()))
        owner.install_core(self.package, manifest, assert_stopped=self.assert_stopped)
        self.assertEqual((self.state / owner.CORE["artifact"]).read_bytes(), self.artifact.read_bytes())
        self.assertIn(b"mvn:org.openhab.addons.bundles", (self.state / owner.CORE["overrides"]).read_bytes())
        owner.restore_google(self.package, manifest, assert_safe=self.assert_stopped)
        owner.rollback_files(self.package, manifest, assert_stopped=self.assert_stopped)
        self.assertFalse((self.state / owner.CORE["artifact"]).exists())
        for relative, before in self.old.items():
            self.assertEqual((self.state / relative).read_bytes(), before)

    def test_changed_preimage_refuses_before_any_install(self):
        result = self.stage()
        manifest = owner.load(self.package, result["manifest_sha256"])
        self.stopped = True
        target = self.state / owner.CORE["overrides"]
        target.write_bytes(b"concurrent owner edit\n")
        with self.assertRaisesRegex(owner.OwnerFileError, "preimage_mismatch"):
            owner.install_core(self.package, manifest, assert_stopped=self.assert_stopped)
        self.assertFalse((self.state / owner.CORE["artifact"]).exists())
        self.assertEqual(target.read_bytes(), b"concurrent owner edit\n")

    def test_rollback_refuses_content_or_permission_drift(self):
        result = self.stage()
        manifest = owner.load(self.package, result["manifest_sha256"])
        self.stopped = True
        owner.install_core(self.package, manifest, assert_stopped=self.assert_stopped)
        target = self.state / owner.CORE["maven"]
        target.chmod(0o600 if target.stat().st_mode & 0o777 != 0o600 else 0o640)
        with self.assertRaisesRegex(owner.OwnerFileError, "rollback_concurrent_edit"):
            owner.rollback_files(self.package, manifest, assert_stopped=self.assert_stopped)
        self.assertTrue((self.state / owner.CORE["artifact"]).exists())

    def test_external_manifest_and_blob_pins_are_required(self):
        result = self.stage()
        with self.assertRaisesRegex(owner.OwnerFileError, "manifest_external_sha_mismatch"):
            owner.load(self.package, "0" * 64)
        (self.package / "artifact.desired").write_bytes(b"tampered")
        with self.assertRaisesRegex(owner.OwnerFileError, "package_blob_changed"):
            owner.load(self.package, result["manifest_sha256"])

    def test_parent_symlink_and_hardlinked_file_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.state / "userdata/thermite").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.stage()
        self.assertEqual(list(outside.iterdir()), [])
        (self.state / "userdata/thermite").unlink()
        link = self.root / "hardlink"
        os.link(self.receipt, link)
        with self.assertRaisesRegex(owner.OwnerFileError, "single_link"):
            self.stage()

    def test_fifo_leaf_is_rejected_without_waiting_for_a_writer(self):
        path = self.root / "not-a-regular-receipt"
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(owner.OwnerFileError, "regular_single_link_bounded"):
            owner.snapshot(path)

    def test_mixed_provider_file_and_competing_override_are_rejected(self):
        path = self.state / owner.GOOGLE["google_yaml"]
        original = path.read_bytes()
        path.write_bytes(original + b"  another:kind:device: {}\n")
        with self.assertRaisesRegex(owner.OwnerFileError, "only_managed_google"):
            self.stage()
        path.write_bytes(original)
        (self.state / owner.CORE["overrides"]).write_bytes(b"mvn:unrelated/androidtv-patch/7\n")
        with self.assertRaisesRegex(owner.OwnerFileError, "configuration_merge_refused"):
            self.stage()
        self.assertFalse(self.package.exists())
