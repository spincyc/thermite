"""Real file custody and locking around internal runtime provisioning."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_install.system import System

# Synthetic values used only inside each test's temporary directory.
FIXTURE_PASSWORD = "synthetic-private-password"
FIXTURE_TOKEN = "synthetic-private-token"
OLD_TOKEN = "old-token"
SAME_TOKEN = "same-token"


class RuntimeCredentialStorage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "credentials"
        self.credentials = self.directory / "runtime-credentials.json"
        self.token = self.directory / "api-token"
        self.system = System()

    def run_setup(self):
        return self.system.configure_runtime_access("http://127.0.0.1:13601", self.credentials,
                                                    self.token, self.system.clock() + 10)

    def test_record_is_durable_before_publication_and_secrets_are_never_printed(self):
        def provision(url, *, load, save, publish, **kwargs):
            self.assertIsNone(load())
            record = {"password": FIXTURE_PASSWORD, "token": FIXTURE_TOKEN}
            save(record)
            self.assertEqual(json.loads(self.credentials.read_text()), record)
            self.assertEqual(load(), record)
            publish(record["token"])
            return record["token"]
        output = io.StringIO()
        with patch("oh_install.native_auth.provision", side_effect=provision), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.run_setup(), FIXTURE_TOKEN)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.token.read_text(), FIXTURE_TOKEN + "\n")
        for path in (self.credentials, self.token, self.directory / ".runtime-access.lock"):
            info = path.stat()
            self.assertEqual(info.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),
                         [".runtime-access.lock", "api-token", "runtime-credentials.json"])

    def test_concurrent_setup_is_refused_before_protocol_and_lock_is_released_after_failure(self):
        def provision(*args, **kwargs):
            with self.assertRaises(BlockingIOError):
                self.run_setup()
            kwargs["save"]({"phase": "prepared"})
            raise OSError("synthetic interrupted publication")
        with patch("oh_install.native_auth.provision", side_effect=provision) as protocol:
            with self.assertRaises(OSError):
                self.run_setup()
            self.assertEqual(protocol.call_count, 1)
        def resume(*args, **kwargs):
            self.assertEqual(kwargs["load"](), {"phase": "prepared"})
            return "resumed"
        with patch("oh_install.native_auth.provision", side_effect=resume):
            self.assertEqual(self.run_setup(), "resumed")

    def test_token_changed_during_setup_is_preserved(self):
        self.system.write_private(self.token, "original\n")
        def provision(*args, **kwargs):
            self.system.write_private(self.token, "other-operator-update\n")
            kwargs["publish"]("must-not-overwrite")
        with patch("oh_install.native_auth.provision", side_effect=provision):
            with self.assertRaises(ValueError):
                self.run_setup()
        self.assertEqual(self.token.read_text(), "other-operator-update\n")

    def test_unsafe_existing_record_cannot_be_read_or_replaced(self):
        self.system.private_directory(self.directory)
        outside = self.root / "outside"
        outside.write_text("private fixture")
        outside.chmod(0o600)
        for kind in ("symlink", "hardlink", "mode", "oversize"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.credentials.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, self.credentials)
                else:
                    self.credentials.write_text("{}" if kind == "mode" else "x" * 65537)
                    self.credentials.chmod(0o644 if kind == "mode" else 0o600)
                def provision(*args, **kwargs):
                    kwargs["load"]()
                with patch("oh_install.native_auth.provision", side_effect=provision):
                    with self.assertRaises((ValueError, OSError)):
                        self.run_setup()
                self.credentials.unlink()
        self.assertEqual(outside.read_text(), "private fixture")
        self.assertFalse(self.token.exists())

    def test_symlinked_parent_and_lock_are_refused_before_protocol(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.directory.symlink_to(outside, target_is_directory=True)
        with patch("oh_install.native_auth.provision") as protocol:
            with self.assertRaises(OSError):
                self.run_setup()
            protocol.assert_not_called()
        self.directory.unlink()
        self.directory.mkdir(mode=0o700)
        target = outside / "lock"
        target.write_text("untouched")
        (self.directory / ".runtime-access.lock").symlink_to(target)
        with patch("oh_install.native_auth.provision") as protocol:
            with self.assertRaises(OSError):
                self.run_setup()
            protocol.assert_not_called()
        self.assertEqual(target.read_text(), "untouched")

    def test_explicit_import_read_is_private_and_does_not_create_missing_parents(self):
        missing = self.root / "absent" / "credential"
        with self.assertRaises(FileNotFoundError):
            self.system.read_private(missing)
        self.assertFalse(missing.parent.exists())
        candidate = self.root / "candidate"
        candidate.write_text("synthetic-token\n")
        candidate.chmod(0o600)
        self.assertEqual(self.system.read_private(candidate), "synthetic-token\n")
        candidate.chmod(0o644)
        with self.assertRaises(ValueError):
            self.system.read_private(candidate)
        self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o644)

    def test_explicit_import_retires_different_managed_access_before_publication(self):
        original = {"phase": "published", "password": FIXTURE_PASSWORD, "token": OLD_TOKEN}
        self.system.write_private(self.credentials, json.dumps(original))
        self.system.write_private(self.token, OLD_TOKEN + "\n")
        writer = self.system._write_private_at
        def write(parent, name, text):
            if name == "api-token":
                self.assertEqual(json.loads(self.credentials.read_text()), {**original, "phase": "retired"})
            writer(parent, name, text)
        with patch.object(self.system, "_write_private_at", side_effect=write):
            self.system.import_runtime_access(self.credentials, self.token, "imported-token")
        self.assertEqual(self.token.read_text(), "imported-token\n")
        self.assertEqual(json.loads(self.credentials.read_text())["password"], original["password"])

    def test_same_token_import_keeps_managed_recovery_and_failed_retirement_preserves_token(self):
        original = {"phase": "published", "token": SAME_TOKEN}
        self.system.write_private(self.credentials, json.dumps(original))
        self.system.write_private(self.token, SAME_TOKEN + "\n")
        self.system.import_runtime_access(self.credentials, self.token, SAME_TOKEN)
        self.assertEqual(json.loads(self.credentials.read_text()), original)
        with patch.object(self.system, "_write_private_at", side_effect=OSError("synthetic interrupted save")):
            with self.assertRaises(OSError):
                self.system.import_runtime_access(self.credentials, self.token, "replacement")
        self.assertEqual(self.token.read_text(), SAME_TOKEN + "\n")

    def test_duplicate_record_keys_cannot_downgrade_publication_state(self):
        original = '{"phase":"published","phase":"issuing","token":null}'
        self.system.write_private(self.credentials, original)
        with patch("oh_install.native_auth.provision", side_effect=lambda *args, **kwargs: kwargs["load"]()):
            with self.assertRaises(ValueError):
                self.run_setup()
        with self.assertRaises(ValueError):
            self.system.import_runtime_access(self.credentials, self.token, "replacement")
        self.assertEqual(self.credentials.read_text(), original)
        self.assertFalse(self.token.exists())

    def test_malformed_bearer_never_reaches_http_or_exposes_its_value(self):
        with patch("oh_install.system.oh_http.request") as request:
            for token in ("synthetic-private\nsecond-value", "synthetic\x00secret", "nonascii-\u00e9"):
                result = self.system.native_probe("http://127.0.0.1:13601", token, "/rest/", self.system.clock() + 1)
                self.assertEqual(result.status, 0)
                self.assertEqual(result.problem, "invalid runtime credential")
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
