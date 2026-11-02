"""Finite administrative credential custody, using only the test user's uid/gid."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_install import secrets


class SecretFiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o750)
        self.source = self.root / "operator-copy"
        self.source.write_bytes(b"synthetic credential value\n")
        self.source.chmod(0o600)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.environment = patch.dict(os.environ, {"SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def install(self, name="openhab-token", **changes):
        values = {"name": name, "source": self.source, "uid": self.uid, "gid": self.gid}
        values.update(changes)
        return secrets.install(self.state, **values)

    def target(self, name="openhab-token"):
        return self.state / "monitoring/secrets" / name

    def test_sync_creates_private_owned_file_and_updates_only_changed_bytes(self):
        result = self.install()
        self.assertTrue(result["changed"])
        target = self.target()
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        for path, mode in ((target, 0o600), (target.parent, 0o700), (target.parent.parent, 0o750)):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, mode))
        inode = target.stat().st_ino
        target.chmod(0o640)
        self.assertFalse(self.install()["changed"])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.source.write_bytes(b"a different synthetic credential\n")
        self.assertTrue(self.install()["changed"])
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertFalse(any(path.name.startswith(".") for path in target.parent.iterdir()))

    def test_existing_grafana_password_preserves_bytes_inode_and_ignores_source(self):
        name = "grafana-admin-password"
        self.install(name)
        target = self.target(name)
        before = (target.read_bytes(), target.stat().st_ino)
        target.chmod(0o644)
        target.parent.chmod(0o755)
        unavailable = self.root / "missing-owner-copy"
        result = self.install(name, source=unavailable, preserve_existing=True)
        self.assertTrue(result["preserved"])
        self.assertFalse(result["changed"])
        self.assertEqual((target.read_bytes(), target.stat().st_ino), before)
        self.assertFalse(unavailable.exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertTrue(self.install(name, source=None, preserve_existing=True)["preserved"])

    def test_missing_grafana_password_uses_source_once(self):
        result = self.install("grafana-admin-password", preserve_existing=True)
        self.assertTrue(result["changed"])
        self.assertFalse(result["preserved"])

    def test_grafana_generation_happens_only_when_service_target_and_owner_copy_are_missing(self):
        self.source.unlink()
        result = self.install("grafana-admin-password", preserve_existing=True, generate_if_missing=True)
        target = self.target("grafana-admin-password")
        self.assertTrue(result["changed"])
        self.assertFalse(result["preserved"])
        content = target.read_bytes()
        self.assertGreaterEqual(len(content), 32)
        self.assertEqual(self.source.read_bytes(), content)
        info = self.source.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink), (self.uid, self.gid, 0o600, 1))
        self.source.unlink()
        result = self.install("grafana-admin-password", preserve_existing=True, generate_if_missing=True)
        self.assertTrue(result["preserved"])
        self.assertEqual(target.read_bytes(), content)
        self.assertFalse(self.source.exists(), "an unavailable copy must not be silently replaced with a different password")

    def test_generation_refuses_a_writable_operator_copy_parent(self):
        self.source.unlink()
        self.root.chmod(0o777)
        with self.assertRaises(secrets.SecretError):
            self.install("grafana-admin-password", preserve_existing=True, generate_if_missing=True)
        self.assertFalse(self.source.exists())
        self.assertEqual(list(self.state.iterdir()), [])

    def test_bad_source_is_rejected_before_creating_private_parents(self):
        self.source.chmod(0o644)
        with self.assertRaises(secrets.SecretError):
            self.install()
        self.assertEqual(list(self.state.iterdir()), [])
        self.source.chmod(0o600)
        with patch.dict(os.environ, {"SUDO_UID": str(self.uid + 1)}), self.assertRaises(secrets.SecretError):
            self.install()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_empty_and_oversized_sources_are_refused_without_writes(self):
        for content in (b"", b"x" * (secrets.MAXIMUM + 1)):
            with self.subTest(size=len(content)):
                self.source.write_bytes(content)
                with self.assertRaises(secrets.SecretError):
                    self.install()
                self.assertEqual(list(self.state.iterdir()), [])

    def test_source_links_and_parent_links_are_refused(self):
        link = self.root / "source-link"
        link.symlink_to(self.source)
        with self.assertRaises(OSError):
            self.install(source=link)
        link.unlink()
        os.link(self.source, link)
        with self.assertRaises(secrets.SecretError):
            self.install()
        link.unlink()
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source=linked_parent / self.source.name)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_state_and_monitoring_symlinks_are_refused_before_writes(self):
        alias = self.root / "state-link"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(OSError):
            secrets.install(alias, name="openhab-token", source=self.source, uid=self.uid, gid=self.gid)
        outside = self.root / "outside"
        outside.mkdir()
        (self.state / "monitoring").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_target_symlink_hardlink_and_special_file_are_refused_without_repairs(self):
        private = self.state / "monitoring/secrets"
        private.mkdir(parents=True, mode=0o755)
        private.chmod(0o755)
        target = self.target("grafana-admin-password")
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink": target.symlink_to(self.source)
                elif kind == "hardlink": os.link(self.source, target)
                elif kind == "fifo": os.mkfifo(target)
                else: target.mkdir()
                with self.assertRaises((OSError, secrets.SecretError)):
                    self.install("grafana-admin-password", preserve_existing=True, source=None)
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o755)
                if target.is_dir(): target.rmdir()
                else: target.unlink()

    def test_name_escape_and_parent_traversal_are_refused(self):
        for name in ("../escape", "/etc/passwd", "unknown"):
            with self.subTest(name=name), self.assertRaises(secrets.SecretError):
                self.install(name)
        with self.assertRaises(secrets.SecretError):
            secrets.install(self.state / ".." / "state", name="openhab-token", source=self.source, uid=self.uid, gid=self.gid)
        with self.assertRaises(secrets.SecretError):
            self.install(preserve_existing=True)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_cli_reports_disposition_without_credential_content(self):
        output, errors = io.StringIO(), io.StringIO()
        argv = [str(self.state), "--name", "grafana-admin-password", "--source", str(self.source),
                "--uid", str(self.uid), "--gid", str(self.gid), "--preserve-existing"]
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(secrets.main(argv), 0)
        self.assertTrue(json.loads(output.getvalue())["installed"])
        self.assertNotIn(self.source.read_text().strip(), output.getvalue() + errors.getvalue())
        self.source.unlink()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(secrets.main(argv), 0)
        self.assertTrue(json.loads(output.getvalue())["preserved"])

    def test_standalone_script_installs_without_package_imports(self):
        result = subprocess.run([sys.executable, str(AREA / "lib/oh_install/secrets.py"), str(self.state),
                                 "--name", "openhab-token", "--source", str(self.source),
                                 "--uid", str(self.uid), "--gid", str(self.gid)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["changed"])
        self.assertNotIn(self.source.read_text().strip(), result.stdout + result.stderr)
        self.assertEqual(self.target().read_bytes(), self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
