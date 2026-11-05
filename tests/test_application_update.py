"""Unified acquisition runs against fixture Git repositories, never a host."""
from pathlib import Path
import importlib.util
import marshal
import subprocess
import struct
import sys
from unittest.mock import Mock, patch

from tests import test_source_upgrade as fixtures

import oh_application_update as application
import oh_release
import oh_source_upgrade as transaction


class ApplicationUpdate(fixtures.ApplicationFixture):
    def apply(self, operation="upgrade", **kwargs):
        self.previous_launcher = self.launcher.resolve()
        options = {"home": self.home, "approve": lambda: True, **kwargs}
        with self.simulated():
            return application.apply(operation, self.site, **options)

    def test_remote_default_fetches_main_only_after_approval(self):
        transaction.git(self.source, "branch", "-m", "main")
        approved = []
        def approve():
            self.assertFalse((self.home / ".local/share").exists())
            self.assertEqual(transaction.clean(self.repo), self.before)
            approved.append(True)
            return True
        with patch.object(application, "OFFICIAL_SOURCE", str(self.source)):
            self.assertEqual(self.apply(approve=approve), 0)
        self.assertEqual(approved, [True])
        fetch = next(args for args in self.calls if "fetch" in args)
        self.assertEqual(fetch[-1], "main")
        self.assertNotEqual(self.launcher.resolve(), self.old / "bin/thermite")

    def test_dry_run_is_read_only_and_does_not_fetch_or_ask(self):
        approve = Mock(side_effect=AssertionError("dry run asked for approval"))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(self.apply(dry_run=True, approve=approve), 0)
        self.assertFalse((self.home / ".local/share").exists())
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.repo.rglob("*") if p.is_file()})
        self.assertFalse(any("fetch" in args or "clone" in args or "pip" in args for args in self.calls))
        approve.assert_not_called()
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")

    def test_decline_changes_neither_application_nor_site(self):
        self.assertEqual(self.apply(source=self.source, approve=lambda: False), 1)
        self.assertFalse((self.home / ".local/share").exists())
        self.assertEqual(transaction.clean(self.repo), self.before)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")

    def test_install_defaults_to_current_source_and_forwards_host_options(self):
        token = self.root / "private token"
        self.assertEqual(self.apply("install", program=self.source,
                                    runtime_token_file=token, host_args=("--clean", "--restart-runtime")), 0)
        child = next(args for args in self.calls if args[1:2] == ("install",))
        self.assertEqual(child[1:6], ("install", "--config", str(self.site), "--_host-phase", "--_run-approved"))
        self.assertEqual(child[6], "--_site-before", "the host phase undoes the pin if nothing commits")
        self.assertRegex(child[7], "^[0-9a-f]{40}$")
        self.assertEqual(child[8:], ("--clean", "--restart-runtime", "--runtime-token-file", str(token)))
        self.assertFalse(any("fetch" in args for args in self.calls))

    def test_install_current_verified_release_does_not_fetch_or_need_source_git(self):
        self.assertEqual(self.apply("install", program=self.new), 0)
        self.assertFalse(any("clone" in args or "fetch" in args for args in self.calls))
        locked, pip = [args for args in self.calls if "pip" in args]
        self.assertEqual(locked[-3:], ("--require-hashes", "-r", str(fixtures.AREA / application.REQUIREMENTS)))
        self.assertEqual(pip[-3:], ("--no-deps", "--no-build-isolation", str(self.new)))
        for call in (locked, pip):
            self.assertIn(str(self.home), call[0])
            self.assertIn("--isolated", call)

    def test_revision_override_packages_exact_commit_without_switching_source(self):
        selected = transaction.clean(self.source)
        (self.source / "README").write_text("later reviewed application\n")
        transaction.git(self.source, "add", "README")
        transaction.git(self.source, "commit", "-m", "Later fixture")
        later = transaction.clean(self.source)
        self.assertEqual(self.apply(source=self.source, revision=selected), 0)
        self.assertEqual(transaction.clean(self.source), later)
        pip = next(args for args in self.calls if "--no-deps" in args)
        self.assertEqual(transaction.clean(Path(pip[-1])), selected)

    def test_repeated_current_application_reuses_environment_and_keeps_launcher_and_commit(self):
        self.assertEqual(self.apply(source=self.source), 0)
        pin = transaction.clean(self.repo)
        launcher = self.launcher.lstat()
        self.calls.clear()
        self.assertEqual(self.apply(source=self.source), 0)
        self.assertEqual(transaction.clean(self.repo), pin)
        self.assertEqual(self.launcher.lstat(), launcher)
        self.assertFalse(any("pip" in args or "clone" in args for args in self.calls))
        self.assertTrue(any(args[1:2] == ("upgrade",) for args in self.calls), "readiness must still run")

    def test_concurrent_source_edits_stop_before_site_commit(self):
        self.change_source_during_build = True
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.apply(source=self.source)
        self.assertEqual(transaction.clean(self.repo), self.before)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")
        self.assertEqual((self.source / "README").read_text(), "concurrent source edit\n")

    def test_concurrent_site_edits_during_host_verification_keep_previous_launcher(self):
        self.edit_during_host = True
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.apply(source=self.source)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")
        self.assertEqual((self.site / "instance/host.yaml").read_text(), "concurrent host edit\n")

    def test_changed_launcher_during_host_verification_is_preserved(self):
        simulate = self.simulate
        other = self.root / "another owner command"
        other.write_text("owner change\n")
        def edit(*args, **kwargs):
            result = simulate(*args, **kwargs)
            if str(args[1]) == "upgrade":
                self.launcher.unlink()
                self.launcher.symlink_to(other)
            return result
        with patch.object(self, "simulate", side_effect=edit), self.assertRaisesRegex(ValueError, "owner command changed"):
            self.apply(source=self.source)
        self.assertEqual(self.launcher.resolve(), other)

    def test_damaged_retained_candidate_is_never_reused_or_selected(self):
        self.fail = "upgrade"
        self.assertEqual(self.apply(source=self.source), 1, "the host phase's outcome is the exit status")
        environment = next((self.home / ".local/share/thermite/application").glob("environment-*"))
        (environment / "runtime/bin/thermite").write_text("changed after staging\n")
        self.fail = None
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "differs from its release"):
            self.apply(source=self.source)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")
        self.assertFalse(any(args[1:2] == ("upgrade",) for args in self.calls))

    def test_symlink_application_store_is_refused_before_approval(self):
        store = self.home / ".local/share/thermite/application"
        store.parent.mkdir(parents=True)
        store.symlink_to(self.source, target_is_directory=True)
        approve = Mock(side_effect=AssertionError("unsafe path asked for approval"))
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.apply(source=self.source, approve=approve)
        approve.assert_not_called()

    def test_site_change_while_waiting_for_approval_stops_before_acquisition(self):
        def approve():
            (self.repo / "owner edit").write_text("preserve\n")
            return True
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.apply(source=self.source, approve=approve)
        self.assertFalse(any("clone" in args or "pip" in args for args in self.calls))
        self.assertEqual((self.repo / "owner edit").read_text(), "preserve\n")

    def test_candidate_verification_runs_inside_its_real_isolated_interpreter(self):
        environment = self.root / "verifier environment"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True)
        package = (environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
                   / "site-packages/thermite_app")
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        runtime = package / "runtime"
        (runtime / "lib").mkdir(parents=True)
        files = {"lib/oh_release.py": Path(oh_release.__file__).read_bytes(),
                 "lib/oh_application_update.py": b"HOST_PHASE_PROTOCOL = 1\n"}
        for name, data in files.items():
            (runtime / name).write_bytes(data)
        (runtime / oh_release.FILE).write_bytes(oh_release.encoded(oh_release.create(files)))
        (environment / "bin/thermite").write_text("fixture launcher\n")
        source = runtime / "lib/oh_release.py"
        cache = Path(importlib.util.cache_from_source(str(source)))
        cache.parent.mkdir()
        info = source.stat()
        poisoned = compile("raise RuntimeError('UNHASHED_BYTECODE_EXECUTED')\n", str(source), "exec")
        cache.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(info.st_mtime) & 0xffffffff,
                                                                   info.st_size & 0xffffffff) + marshal.dumps(poisoned))
        release = application.verify(environment)
        self.assertEqual(release["digest"], oh_release.manifest(runtime)["digest"])
        (runtime / "lib/oh_application_update.py").write_text("HOST_PHASE_PROTOCOL = 2\n")
        with self.assertRaises(subprocess.CalledProcessError):
            application.verify(environment)

    def test_standard_installed_console_script_is_backed_up_and_adopted_after_success(self):
        self.launcher.unlink()
        content = b"#!/home/fixture/.local/share/thermite/venv/bin/python\n" + application.CONSOLE_SCRIPTS[0]
        self.launcher.write_bytes(content)
        self.launcher.chmod(0o755)
        self.assertEqual(self.apply(source=self.source), 0)
        self.assertTrue(self.launcher.is_symlink())
        backups = list((self.home / ".local/share/thermite/application").glob("previous-launcher-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), content)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o755)

    def test_failed_host_preserves_standard_console_script_in_place(self):
        self.launcher.unlink()
        content = b"#!/usr/bin/python3\n" + application.CONSOLE_SCRIPTS[1]
        self.launcher.write_bytes(content)
        self.launcher.chmod(0o755)
        self.fail = "upgrade"
        self.assertEqual(self.apply(source=self.source), 1, "the host phase's outcome is the exit status")
        self.assertFalse(self.launcher.is_symlink())
        self.assertEqual(self.launcher.read_bytes(), content)

    def test_standard_console_script_with_extra_user_code_is_preserved(self):
        self.launcher.unlink()
        content = b"#!/usr/bin/python3\n" + application.CONSOLE_SCRIPTS[1] + b"# customized\n"
        self.launcher.write_bytes(content)
        self.launcher.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "non-symlink launcher"):
            self.apply(source=self.source)
        self.assertEqual(self.launcher.read_bytes(), content)
