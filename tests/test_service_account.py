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
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_service as service
import oh_logging


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

    def test_openhab_logs_alone_become_group_readable(self):
        # Rotations made before filePermissions, the updater's own file, and
        # private userdata files a group traversal would have exposed.
        logs = [self.file("logs/openhab.log", 0o600), self.file("logs/events.log.3.gz", 0o600),
                self.file("logs/update.log", 0o664), self.file("logs/odd", 0o4755)]
        private = [self.file("userdata/jsondb/users.json", 0o644), self.file("userdata/sony_ip_control/cookie", 0o644),
                   self.file("userdata/logs/openhab.log", 0o640)]
        (self.root / "logs").chmod(0o700)
        before = {path: path.read_bytes() for path in [*logs, *private]}
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE((self.root / "logs").stat().st_mode), 0o2750)
        self.assertEqual(stat.S_IMODE((self.root / "userdata").stat().st_mode), 0o700)
        for path in logs:
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        for path in private:
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid, runtime=True), [])

    def test_backup_completion_witnesses_keep_group_read_access_without_widening_private_files(self):
        receipt = self.file("backups/finished.tar.gz.complete.json", 0o640)
        private = [self.file("backups/finished.tar.gz", 0o640),
                   self.file("backups/private.tar.gz.complete.json", 0o600),
                   self.file("backups/nested/other.tar.gz.complete.json", 0o640),
                   self.file("backups/unrelated.json", 0o640)]
        before = {path: path.read_bytes() for path in [receipt, *private]}
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o640)
        for path in private:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])

    def test_only_known_graal_cache_helper_retains_owner_execution(self):
        prefix = "userdata/.cache/org.graalvm.polyglot/engine/libtruffleattach/"
        helper = self.file(prefix + "a" * 64 + "/bin/libtruffleattach.so", 0o775)
        other = [self.file("userdata/credential", 0o775),
                 self.file(prefix + "not-a-hash/bin/libtruffleattach.so", 0o775),
                 self.file(prefix + "b" * 64 + "/bin/other.so", 0o775)]
        before = {path: (path.read_bytes(), path.stat().st_ino) for path in [helper, *other]}
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o700)
        for parent in helper.relative_to(self.root).parents:
            if parent != Path("."):
                self.assertEqual(stat.S_IMODE((self.root / parent).stat().st_mode), 0o700)
        for path in other:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_ino) for path in [helper, *other]}, before)
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])
        self.assertEqual(service.normalize_state(self.root, self.uid, self.gid), [])

    def graal_helper(self, mode=0o775):
        helper = self.file("userdata/.cache/org.graalvm.polyglot/engine/libtruffleattach/"
                           + "a" * 64 + "/bin/libtruffleattach.so", mode)
        self.root.chmod(0o750)
        for parent in helper.parents:
            if parent == self.root:
                break
            parent.chmod(0o700)
        return helper

    def test_fresh_graal_producer_mode_is_private_and_tightened_mode_is_never_widened(self):
        helper = self.graal_helper()
        for mode in (0o775, 0o700):
            with self.subTest(mode=oct(mode)):
                helper.chmod(mode)
                before = helper.stat()
                self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])
                self.assertEqual(service.normalize_state(self.root, self.uid, self.gid), [])
                self.assertEqual(helper.stat(), before)

    def test_legacy_graal_0600_helper_regains_owner_execution_without_replacement(self):
        helper = self.graal_helper(0o600)
        before = helper.read_bytes(), helper.stat().st_ino
        issues = service.normalize_state(self.root, self.uid, self.gid)
        self.assertTrue(any(issue.path == helper and issue.wanted_mode == 0o700 for issue in issues))
        self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o700)
        self.assertEqual((helper.read_bytes(), helper.stat().st_ino), before)
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])

    def test_graal_exception_leaves_neighbors_and_credentials_strict(self):
        helper = self.graal_helper()
        neighbor = self.file(str(helper.relative_to(self.root).parent / "neighbor.so"), 0o775)
        credential = self.file("userdata/credential", 0o775)
        pairing = self.file("secrets/pairing", 0o775)
        issues = service.audit_state(self.root, self.uid, self.gid)
        self.assertFalse(any(issue.path == helper for issue in issues))
        for path in (neighbor, credential, pairing):
            self.assertTrue(any(issue.path == path and issue.wanted_mode == 0o600 for issue in issues))
        service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o775)
        for path in (neighbor, credential, pairing):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_each_graal_ancestor_must_have_private_permissions(self):
        helper = self.graal_helper()
        for parent in helper.parents:
            if parent == self.root:
                break
            with self.subTest(parent=parent.relative_to(self.root)):
                parent.chmod(0o750)
                issues = service.audit_state(self.root, self.uid, self.gid)
                self.assertTrue(any(issue.path == parent and issue.wanted_mode == 0o700 for issue in issues))
                self.assertTrue(any(issue.path == helper and issue.wanted_mode == 0o700 for issue in issues))
                parent.chmod(0o700)

    def test_every_graal_ancestor_and_helper_must_have_the_expected_uid_and_gid(self):
        helper = self.graal_helper()
        checked = [helper]
        for parent in helper.parents:
            if parent == self.root:
                break
            checked.append(parent)
        original = os.fstat
        for path in checked:
            inode = path.stat().st_ino
            for field in (4, 5):
                def wrong_identity(descriptor):
                    info = original(descriptor)
                    if info.st_ino == inode:
                        values = list(info)
                        values[field] += 1
                        return os.stat_result(values)
                    return info
                with self.subTest(path=path.relative_to(self.root), identity=field), \
                        mock.patch.object(service.os, "fstat", side_effect=wrong_identity):
                    issues = service.audit_state(self.root, self.uid, self.gid)
                self.assertTrue(any(issue.path == path for issue in issues))
                self.assertTrue(any(issue.path == helper and issue.wanted_mode == 0o700 for issue in issues))

    def test_graal_other_modes_and_special_bits_are_repaired_behind_private_parents(self):
        helper = self.graal_helper()
        for mode in (0o755, 0o6775, 0o1775):
            with self.subTest(mode=oct(mode)):
                helper.chmod(mode)
                issues = service.audit_state(self.root, self.uid, self.gid)
                self.assertTrue(any(issue.path == helper and issue.wanted_mode == 0o700 for issue in issues))
                service.normalize_state(self.root, self.uid, self.gid)
                self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o700)
                self.assertEqual(service.audit_state(self.root, self.uid, self.gid), [])

    def test_graal_cache_helper_must_be_regular_and_never_follows_a_link(self):
        helper = self.graal_helper()
        external = Path(self.tmp.name, "external-library")
        external.write_text("external")
        external.chmod(0o775)
        before = external.stat()
        helper.unlink()
        helper.symlink_to(external)
        with self.assertRaisesRegex(service.ServiceError, "not a regular file"):
            service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(external.stat(), before)

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

    def test_logging_check_distinguishes_missing_protection_from_failure_to_read(self):
        args = [str(self.root), "--check-pairing-logging"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing:
            service.main(args)
        self.assertEqual(missing.exception.code, service.PAIRING_LOGGING_UNPROTECTED)
        with mock.patch.object(service, "_open_root", side_effect=PermissionError("cannot read")), \
                contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as unreadable:
            service.main(args)
        self.assertEqual(unreadable.exception.code, 2)
        self.assertNotIn("protection is missing", output.getvalue())

    def test_logging_check_checks_both_files_without_mutating_them(self):
        native = self.file("userdata/" + str(oh_logging.RELATIVE), 0o600)
        selector = self.file("userdata/" + str(oh_logging.SELECTOR), 0o600)
        native.write_bytes(oh_logging.guarded(b"<Configuration><Loggers><Root/></Loggers></Configuration>"))
        selector.write_bytes(oh_logging.DEFAULT_SELECTOR)
        def snapshot(path):
            info = path.stat()
            return path.read_bytes(), info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_mtime_ns
        before = {path: snapshot(path) for path in (native, selector)}
        self.assertEqual(service.main([str(self.root), "--check-pairing-logging"]), 0)
        self.assertEqual({path: snapshot(path) for path in before}, before)
        selector.unlink()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing:
            service.main([str(self.root), "--check-pairing-logging"])
        self.assertEqual(missing.exception.code, service.PAIRING_LOGGING_UNPROTECTED)

    def test_logging_check_does_not_treat_an_unreadable_link_as_a_completed_check(self):
        native = self.file("userdata/" + str(oh_logging.RELATIVE), 0o600)
        outside = self.file("untouched", 0o600)
        native.unlink()
        native.symlink_to(outside)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as unreadable:
            service.main([str(self.root), "--check-pairing-logging"])
        self.assertEqual(unreadable.exception.code, 2)
        self.assertTrue(native.is_symlink())

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

    def test_partial_repairs_cannot_issue_full_repair_receipt(self):
        for scope in ({"deployment": True}, {"runtime": True}):
            with self.subTest(scope=scope), self.assertRaisesRegex(service.ServiceError, "partial repair"):
                service.normalize_state(self.root, self.uid, self.gid, receipt=True, **scope)

    def test_runtime_scope_repairs_openhab_trees_and_leaves_other_stores_running(self):
        native = [self.file("conf/yaml/site.yaml"), self.file("userdata/etc/logging.xml"),
                  self.file("logs/openhab.log", 0o600), self.file("addons/fixture.jar"), self.file("secrets/fixture")]
        # The live stores of running services: an entry a whole-tree walk
        # refuses, and modes it would otherwise narrow.
        sample = self.file("monitoring/prometheus/chunks/sample", 0o644)
        os.mkfifo(self.root / "monitoring/prometheus/active-socket-like-entry")
        pairing = self.file("atv/secrets/pairing", 0o644)
        others = (sample.stat(), pairing.stat())
        issues = service.normalize_state(self.root, self.uid, self.gid, runtime=True)
        repaired = {issue.path.relative_to(self.root).parts[0] for issue in issues}
        self.assertEqual(repaired, set(service.RUNTIME_STORES))
        self.assertLessEqual(set(native), {issue.path for issue in issues})
        self.assertEqual(service.audit_state(self.root, self.uid, self.gid, runtime=True), [])
        self.assertEqual((sample.stat(), pairing.stat()), others)

    def test_runtime_scope_keeps_the_full_receipt_unless_it_fails(self):
        self.file("deploy/history")
        service.normalize_state(self.root, self.uid, self.gid, receipt=True)
        receipt = self.root / service.RECEIPT
        before = receipt.stat()
        self.file("userdata/etc/logging.xml", 0o644)
        service.normalize_state(self.root, self.uid, self.gid, runtime=True)
        self.assertEqual(receipt.stat(), before)
        outside = Path(self.tmp.name, "external")
        outside.write_text("keep")
        os.link(outside, self.root / "userdata/external")
        with self.assertRaisesRegex(service.ServiceError, "hard-linked"):
            service.normalize_state(self.root, self.uid, self.gid, runtime=True)
        self.assertFalse(receipt.exists(), "a failed openHAB repair sends the next deploy through a full one")


def kept(path):
    """A path's stat facts, less its access time: the walk under test reads
    directories, which may move their atime to the next second."""
    info = path.stat()
    return (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid, info.st_gid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


class OwnedRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "venv")
        self.root.mkdir()
        self.uid, self.gid = os.geteuid(), os.getegid()

    def tearDown(self):
        self.tmp.cleanup()

    file = Permissions.file

    def test_retains_read_and_executable_bits_and_only_narrows_access(self):
        paths = [(self.file(name, before), after) for name, before, after in (
            ("bin/activate", 0o644, 0o640), ("bin/console", 0o755, 0o750),
            ("lib/module.so", 0o711, 0o710), ("private", 0o600, 0o600),
            ("read-only", 0o440, 0o440))]
        self.root.chmod(0o700)
        before = {path: (path.read_bytes(), path.stat().st_ino) for path, _ in paths}
        self.assertGreater(service.normalize_owned_runtime(self.root), 0)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for path, wanted in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), wanted)
            self.assertEqual((path.read_bytes(), path.stat().st_ino), before[path])
        self.assertEqual(service.normalize_owned_runtime(self.root), 0)

    def test_interpreter_and_directory_symlink_targets_are_untouched(self):
        outside = Path(self.tmp.name, "global-python")
        outside.write_text("global interpreter")
        outside.chmod(0o755)
        external = Path(self.tmp.name, "external-lib")
        external.mkdir(mode=0o777)
        (self.root / "python").symlink_to(outside)
        (self.root / "lib64").symlink_to(external, target_is_directory=True)
        before = kept(outside), kept(external)
        service.normalize_owned_runtime(self.root)
        self.assertEqual((kept(outside), kept(external)), before)

    def test_unsafe_hardlink_rejects_the_whole_runtime_before_chmod(self):
        earlier = self.file("a-console", 0o755)
        external = Path(self.tmp.name, "external")
        external.write_text("global package")
        external.chmod(0o644)
        os.link(external, self.root / "z-linked")
        before = kept(earlier), kept(external), kept(self.root)
        with self.assertRaisesRegex(service.ServiceError, "hard-linked"):
            service.normalize_owned_runtime(self.root)
        self.assertEqual((kept(earlier), kept(external), kept(self.root)), before)

    def test_wrong_owner_rejects_before_chmod(self):
        target = self.file("console", 0o755)
        before = kept(target), kept(self.root)
        with mock.patch.object(service.os, "geteuid", return_value=self.uid + 1):
            with self.assertRaisesRegex(service.ServiceError, "not owned"):
                service.normalize_owned_runtime(self.root)
        self.assertEqual((kept(target), kept(self.root)), before)

    def test_same_device_mount_crossing_rejects_before_chmod(self):
        earlier = self.file("a-console", 0o755)
        mounted = self.file("z-mounted", 0o644)
        inode = mounted.stat().st_ino
        before = kept(earlier), kept(mounted), kept(self.root)
        def mount_id(descriptor):
            return 2 if os.fstat(descriptor).st_ino == inode else 1
        with mock.patch.object(service, "_mount_id", side_effect=mount_id):
            with self.assertRaisesRegex(service.ServiceError, "mounted runtime"):
                service.normalize_owned_runtime(self.root)
        self.assertEqual((kept(earlier), kept(mounted), kept(self.root)), before)

    def test_symlink_root_or_ancestor_is_rejected(self):
        link = Path(self.tmp.name, "link")
        link.symlink_to(self.root, target_is_directory=True)
        for root in (link, link / "child"):
            with self.subTest(root=root), self.assertRaises(OSError):
                service.normalize_owned_runtime(root)


if __name__ == "__main__":
    unittest.main()
