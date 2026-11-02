import contextlib
import io
import json
import os
from pathlib import Path
import resource
import stat
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_service as service


class Permissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "state")
        self.root.mkdir()
        self.uid, self.gid = os.geteuid(), os.getegid()

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name, mode=0o666):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("retained native identity / pairing material\n")
        path.chmod(mode)
        return path

    def test_finite_policy_preserves_credentials_and_restricts_public_modes(self):
        files = [self.file("conf/yaml/site.yaml"), self.file("conf/bin/program", 0o777),
                 self.file("userdata/jsondb/org.openhab.core.auth.AuthenticationStore.json"),
                 self.file("userdata/etc/keystore"), self.file("monitoring/prometheus/chunks/sample"),
                 self.file("monitoring/secrets/openhab-token"), self.file("atv/secrets/pairing"),
                 self.file("backups/native.tar"), self.file("secrets/key"), self.file("deploy/history")]
        before = {path: (path.read_bytes(), path.stat().st_ino) for path in files}
        self.assertTrue(service.normalize_state(self.root, self.uid, self.gid))
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])
        for name, mode in (("", 0o750), ("conf", 0o2770), ("conf/yaml/site.yaml", 0o660),
                           ("conf/bin/program", 0o770), ("userdata", 0o700), ("userdata/etc/keystore", 0o600),
                           ("monitoring/prometheus", 0o750), ("monitoring/prometheus/chunks/sample", 0o640),
                           ("monitoring/secrets", 0o700), ("monitoring/secrets/openhab-token", 0o600),
                           ("atv/secrets/pairing", 0o600), ("backups", 0o750), ("backups/native.tar", 0o600)):
            with self.subTest(name=name):
                info = (self.root / name).stat()
                self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, mode))
        self.assertEqual({path: (path.read_bytes(), path.stat().st_ino) for path in files}, before)
        self.assertEqual(service.normalize_state(self.root, self.uid, self.gid), [])

    def test_never_widens_existing_private_config(self):
        self.file("conf/legacy-secret", 0o600)
        private = self.root / "conf/private"
        private.mkdir(mode=0o700)
        self.file("conf/private/credential", 0o600)
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE((self.root / "conf/legacy-secret").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o2700)

    def test_links_never_touch_external_targets(self):
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        target = outside / "secret"
        target.write_text("outside")
        target.chmod(0o644)
        (self.root / "atv").mkdir()
        (self.root / "atv/venv-python").symlink_to(target)
        (self.root / "atv/directory-link").symlink_to(outside, target_is_directory=True)
        before = (target.stat(), target.read_bytes())
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual((target.stat(), target.read_bytes()), before)
        self.assertTrue((self.root / "atv/directory-link").is_symlink())

    def test_rejects_symlink_state_root_and_ancestor(self):
        link = Path(self.tmp.name, "link")
        link.symlink_to(self.root, target_is_directory=True)
        for root in (link, link / "child"):
            with self.subTest(root=root), self.assertRaises(OSError):
                service.normalize_state(root, self.uid, self.gid)

    def test_rejects_external_hardlink_before_repair(self):
        target = Path(self.tmp.name, "external")
        target.write_text("outside")
        (self.root / "conf").mkdir()
        os.link(target, self.root / "conf/linked")
        mode = stat.S_IMODE(self.root.stat().st_mode)
        with self.assertRaisesRegex(service.ServiceError, "hard-linked"):
            service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), mode)

    def test_refuses_special_files_before_repair(self):
        (self.root / "conf").mkdir()
        os.mkfifo(self.root / "conf/fifo")
        mode = stat.S_IMODE(self.root.stat().st_mode)
        with self.assertRaisesRegex(service.ServiceError, "special state entry"):
            service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), mode)

    def test_bounded_descriptors_for_large_stores(self):
        for number in range(140):
            self.file(f"monitoring/prometheus/{number}")
        old = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, old[1]))
            service.normalize_state(self.root, self.uid, self.gid)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, old)

    def test_check_cli_is_read_only_and_requires_real_service_ids(self):
        path = self.file("conf/site.yaml")
        before = path.stat()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(service.main([str(self.root), "--uid", str(self.uid), "--gid", str(self.gid), "--check"]), 1)
        self.assertIn("expected", output.getvalue())
        self.assertEqual(path.stat(), before)
        for uid, gid in ((0, self.gid), (self.uid, 0), (-1, self.gid)):
            with self.assertRaises(service.ServiceError):
                service.audit_state(self.root, uid, gid)

    def test_deployment_scope_repairs_outputs_without_walking_active_private_stores(self):
        self.file("conf/site.yaml")
        secret = self.file("userdata/credential", 0o600)
        private_before = secret.stat()
        os.mkfifo(self.root / "userdata/active-socket-like-entry")
        service.normalize_state(self.root, self.uid, self.gid, deployment=True)
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid, deployment=True), [])
        self.assertEqual(secret.stat(), private_before)

    def test_full_normalization_preserves_unrelated_private_trees(self):
        self.file("conf/site.yaml")
        unrelated = self.file("operator-private/credential", 0o600)
        unrelated.parent.chmod(0o700)
        before = (unrelated.stat(), unrelated.parent.stat())
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual((unrelated.stat(), unrelated.parent.stat()), before)

    def test_access_custody_refused_before_full_normalization_writes(self):
        config = self.file("conf/site.yaml")
        self.file("access/config.json", 0o600)
        before = config.stat()
        with self.assertRaisesRegex(service.ServiceError, "access custody"):
            service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(config.stat(), before)

    def test_full_repair_receipt_has_fixed_schema_identity_and_permissions(self):
        self.file("deploy/history")
        service.normalize_state(self.root, self.uid, self.gid, receipt=True)
        receipt = self.root / service.RECEIPT
        self.assertEqual(json.loads(receipt.read_text()), {"version": service.RECEIPT_VERSION,
                         "uid": self.uid, "gid": self.gid, "policy": service.POLICY})
        info = receipt.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, 0o660))
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])
        service.normalize_state(self.root, self.uid, self.gid, deployment=True)
        self.assertEqual(receipt.stat(), info)

    def test_failed_full_repair_invalidates_stale_receipt(self):
        self.file("deploy/history")
        service.normalize_state(self.root, self.uid, self.gid, receipt=True)
        outside = Path(self.tmp.name, "external")
        outside.write_text("keep")
        (self.root / "conf").mkdir()
        os.link(outside, self.root / "conf/external")
        before = outside.stat()
        with self.assertRaisesRegex(service.ServiceError, "hard-linked"):
            service.normalize_state(self.root, self.uid, self.gid, receipt=True)
        self.assertFalse((self.root / service.RECEIPT).exists())
        self.assertEqual(outside.stat(), before)

    def test_deployment_cannot_issue_full_repair_receipt(self):
        with self.assertRaisesRegex(service.ServiceError, "deployment-only"):
            service.normalize_state(self.root, self.uid, self.gid, deployment=True, receipt=True)


if __name__ == "__main__":
    unittest.main()
