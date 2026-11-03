"""Real private tar backups and interrupted publication, without sudo or services."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_install import backup
import oh_service


class Publication(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/install-backup"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir()
        self.backups = self.root / "backups"
        self.backups.mkdir(mode=0o750)
        for name, content in (("conf/site.yaml", "synthetic configuration"),
                              ("userdata/jsondb/auth.json", "synthetic retained identity"),
                              ("userdata/cache/discard", "cache"), ("userdata/tmp/discard", "temporary"),
                              ("monitoring/grafana/grafana.db", "synthetic database")):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.name = "20261103T120000Z-openhab-5.2.3-grafana.tar.gz"
        self.archive = self.backups / self.name
        self.receipt = backup.receipt_path(self.archive)
        self.uid, self.gid = os.geteuid(), os.getegid()

    def create(self, name=None):
        return backup.create(self.root, name=name or self.name, uid=self.uid, gid=self.gid, grafana=True)

    def test_success_is_a_restorable_private_archive_with_a_durable_completion_witness(self):
        self.assertEqual(self.create(), self.archive)
        with gzip.open(self.archive, "rb") as stream:
            self.assertTrue(stream.read())  # Also consumes the gzip trailer/CRC.
        with tarfile.open(self.archive) as archive:
            names = archive.getnames()
            self.assertEqual(archive.extractfile("userdata/jsondb/auth.json").read(), b"synthetic retained identity")
            self.assertEqual(archive.extractfile("monitoring/grafana/grafana.db").read(), b"synthetic database")
        self.assertIn("conf/site.yaml", names)
        self.assertFalse(any(name.startswith(("userdata/cache", "userdata/tmp")) for name in names))
        for path, mode in ((self.archive, 0o600), (self.receipt, 0o640)):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink),
                             (self.uid, self.gid, mode, 1))
        record = json.loads(self.receipt.read_bytes())
        self.assertEqual(record["size"], self.archive.stat().st_size)
        self.assertTrue(backup.completed(self.receipt.read_bytes(), self.name, self.archive.stat().st_mtime))
        before = self.archive.read_bytes(), self.receipt.read_bytes()
        oh_service.normalize_state(self.root, self.uid, self.gid)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o640)
        self.assertEqual((self.archive.read_bytes(), self.receipt.read_bytes()), before)
        self.assertTrue(backup.completed(self.receipt.read_bytes(), self.name, self.archive.stat().st_mtime))
        self.assertEqual(sorted(path.name for path in self.backups.iterdir()), [self.name, self.receipt.name])

    def test_tar_failure_or_interrupt_never_publishes_partial_output(self):
        for failure in (subprocess.CalledProcessError(2, "tar"), KeyboardInterrupt()):
            def fail(argv, *, stdout, **options):
                os.write(stdout, b"partial gzip header, no complete archive")
                raise failure
            with self.subTest(failure=type(failure)), mock.patch.object(backup.subprocess, "run", side_effect=fail):
                with self.assertRaises(type(failure)):
                    self.create()
            self.assertEqual(list(self.backups.iterdir()), [])

    def test_missing_source_fails_actual_tar_without_a_completion_record(self):
        (self.root / "conf/site.yaml").unlink()
        (self.root / "conf").rmdir()
        original = subprocess.run
        def quiet(*args, **kwargs):
            return original(*args, **kwargs, stderr=subprocess.PIPE)
        with mock.patch.object(backup.subprocess, "run", side_effect=quiet):
            with self.assertRaises(subprocess.CalledProcessError):
                self.create()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_failure_after_archive_publication_has_no_receipt_and_never_clobbers_on_retry(self):
        link = os.link
        def fail_receipt(source, target, **options):
            if target == self.receipt.name:
                raise OSError("injected receipt publication failure")
            return link(source, target, **options)
        with mock.patch.object(backup.os, "link", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "injected"):
                self.create()
        self.assertTrue(self.archive.is_file())
        self.assertFalse(self.receipt.exists())
        before = self.archive.read_bytes(), self.archive.stat()
        with mock.patch.object(backup.subprocess, "run") as run:
            with self.assertRaisesRegex(backup.BackupError, "occupied"):
                self.create()
        run.assert_not_called()
        self.assertEqual((self.archive.read_bytes(), self.archive.stat()), before)
        next_name = self.name.replace("-openhab-", "-1-openhab-")
        retry = self.create(next_name)
        self.assertTrue(backup.completed(backup.receipt_path(retry).read_bytes(), next_name, retry.stat().st_mtime))

    def test_a_destination_created_during_tar_is_not_replaced(self):
        run = subprocess.run
        def collide(*args, **options):
            result = run(*args, **options)
            self.archive.write_bytes(b"older archive preserved")
            return result
        with mock.patch.object(backup.subprocess, "run", side_effect=collide):
            with self.assertRaises(FileExistsError):
                self.create()
        self.assertEqual(self.archive.read_bytes(), b"older archive preserved")
        self.assertFalse(self.receipt.exists())
        self.assertEqual(list(self.backups.iterdir()), [self.archive])

    def test_existing_receipt_or_legacy_partial_file_stops_before_tar(self):
        for path in (self.archive, self.receipt):
            with self.subTest(path=path.name):
                path.write_bytes(b"legacy or incomplete bytes retained")
                with mock.patch.object(backup.subprocess, "run") as run:
                    with self.assertRaisesRegex(backup.BackupError, "occupied"):
                        self.create()
                run.assert_not_called()
                self.assertEqual(path.read_bytes(), b"legacy or incomplete bytes retained")
                path.unlink()

    def test_symlinked_backup_directory_is_refused_without_writing_through_it(self):
        self.backups.rmdir()
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.backups.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(backup.subprocess, "run") as run:
            with self.assertRaises(OSError):
                self.create()
        run.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_completion_requires_a_valid_receipt_bound_to_the_archive(self):
        self.create()
        record = json.loads(self.receipt.read_bytes())
        for changed in (None, {}, {**record, "archive": "another.tar.gz"}, {**record, "version": 2},
                        {**record, "size": 0}, {**record, "mtime": record["mtime"] + 1}):
            with self.subTest(record=changed):
                self.assertFalse(backup.completed(json.dumps(changed).encode(), self.name, self.archive.stat().st_mtime))
        self.assertFalse(backup.completed(b"truncated JSON", self.name, self.archive.stat().st_mtime))

    def test_truncation_with_the_original_timestamp_cannot_reuse_completion(self):
        from oh_install.system import System
        self.create()
        original = self.archive.stat()
        self.archive.write_bytes(b"truncated")
        os.utime(self.archive, ns=(original.st_atime_ns, original.st_mtime_ns))
        changed = System().stat(self.archive)
        self.assertEqual(changed.mtime, original.st_mtime)
        self.assertEqual(changed.size, len(b"truncated"))
        self.assertFalse(backup.completed(self.receipt.read_bytes(), self.name, changed.mtime, size=changed.size))


if __name__ == "__main__":
    unittest.main()
