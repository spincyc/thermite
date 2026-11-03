"""Finite node-exporter confinement writes, using only the test user's identity."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_apparmor as confinement


class KernelState(unittest.TestCase):
    def test_explicit_enabled_and_disabled_kernel_values(self):
        for value, expected in ((b"Y", True), (b"Y\n", True), (b" N\n", False)):
            with self.subTest(value=value):
                system = Mock()
                system.read_bytes.return_value = value
                self.assertIs(confinement.enabled(system), expected)
                system.read_bytes.assert_called_once_with(confinement.MODULE_ENABLED)
                system.stat.assert_not_called()

    def test_absent_module_is_distinct_from_unreadable_existing_status(self):
        system = Mock()
        system.read_bytes.return_value = None
        system.stat.return_value = None
        self.assertIs(confinement.enabled(system), False)
        system.stat.return_value = SimpleNamespace(mode=0o444)
        with self.assertRaisesRegex(ValueError, "cannot read"):
            confinement.enabled(system)

    def test_ambiguous_kernel_status_is_refused(self):
        for value in (b"", b"yes", b"n", b"1", b"Y N", b"Y\x00"):
            with self.subTest(value=value):
                system = Mock()
                system.read_bytes.return_value = value
                with self.assertRaisesRegex(ValueError, "unrecognized"):
                    confinement.enabled(system)

    def test_inactive_configuration_has_no_profile_override(self):
        artifacts = confinement.expected(False)
        self.assertEqual(set(artifacts), {confinement.CONFIG})
        self.assertIn(b"[containers]\n", artifacts[confinement.CONFIG])
        self.assertNotIn(b"apparmor_profile", artifacts[confinement.CONFIG])
        self.assertNotIn(b"unconfined", artifacts[confinement.CONFIG])

    def test_active_configuration_includes_exact_paired_policy_and_license(self):
        artifacts = confinement.expected(True)
        self.assertEqual(set(artifacts), {confinement.CONFIG, confinement.POLICY, confinement.LICENSE})
        self.assertIn(b'apparmor_profile="thermite-node-exporter"\n', artifacts[confinement.CONFIG])
        self.assertEqual(artifacts[confinement.POLICY], confinement.SOURCE.read_bytes())
        self.assertEqual(artifacts[confinement.LICENSE], Path(str(confinement.SOURCE) + ".LICENSE").read_bytes())
        self.assertIn(confinement.PROFILE.encode(), artifacts[confinement.POLICY])
        self.assertTrue(artifacts[confinement.LICENSE])

    def test_expected_requires_boolean_state(self):
        for value in (None, 0, 1, "Y", b"N"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                confinement.expected(value)


class ArtifactWrites(unittest.TestCase):
    def setUp(self):
        repository = next((parent for parent in (AREA, *AREA.parents) if (parent / ".git").exists()), AREA)
        scratch = Path(os.environ.get("TMPDIR", repository / ".scratch/review-node-helper-tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.root / "administrative"
        self.parent.mkdir(mode=0o755)
        self.target = self.parent / "node.conf"
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.outside = self.root / "outside"
        self.outside.mkdir(mode=0o755)
        (self.outside / "node.conf").write_bytes(b"unrelated outside bytes")

    def write(self, content=b"[containers]\n", **changes):
        return confinement._write(changes.pop("path", self.target), content, uid=changes.pop("uid", self.uid),
                                  gid=changes.pop("gid", self.gid), **changes)

    def snapshot(self, path):
        content = path.read_bytes()
        return path.stat(), content

    def assert_no_temporary(self, parent=None):
        self.assertFalse(any(path.name.startswith(".") for path in (parent or self.parent).iterdir()))

    def test_creates_normalized_parent_and_regular_file_under_restrictive_umask(self):
        target = self.root / "nested/administrative/node.conf"
        previous = os.umask(0o077)
        try:
            self.assertTrue(self.write(path=target))
        finally:
            os.umask(previous)
        for path, mode in ((target, 0o644), (target.parent, 0o755), (target.parent.parent, 0o755)):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, mode))
        self.assertEqual(target.stat().st_nlink, 1)
        self.assert_no_temporary(target.parent)

    def test_same_bytes_preserve_inode_and_changed_bytes_replace_atomically(self):
        self.assertTrue(self.write(b"first policy\n"))
        original = self.target.stat().st_ino
        self.assertFalse(self.write(b"first policy\n"))
        self.assertEqual(self.target.stat().st_ino, original)
        self.assertTrue(self.write(b"second policy\n"))
        self.assertNotEqual(self.target.stat().st_ino, original)
        self.assertEqual(self.target.read_bytes(), b"second policy\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)
        self.assert_no_temporary()

    def test_target_symlink_does_not_modify_its_outside_target(self):
        external = self.outside / "node.conf"
        before = self.snapshot(external)
        self.target.symlink_to(external)
        with self.assertRaises((OSError, ValueError)):
            self.write()
        self.assertEqual(self.snapshot(external), before)
        self.assertTrue(self.target.is_symlink())
        self.assert_no_temporary()

    def test_ancestor_symlink_does_not_modify_outside_directory(self):
        link = self.root / "administrative-link"
        link.symlink_to(self.outside, target_is_directory=True)
        before = self.snapshot(self.outside / "node.conf")
        with self.assertRaises((OSError, ValueError)):
            self.write(path=link / "node.conf")
        self.assertEqual(self.snapshot(self.outside / "node.conf"), before)
        self.assertEqual(sorted(path.name for path in self.outside.iterdir()), ["node.conf"])

    def test_hardlink_refusal_preserves_outside_inode_and_bytes(self):
        external = self.outside / "node.conf"
        os.link(external, self.target)
        before = self.snapshot(external)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.write()
        self.assertEqual(self.snapshot(external), before)
        self.assertEqual(self.target.stat().st_nlink, 2)
        self.assert_no_temporary()

    def test_fifo_is_refused_without_opening_a_writer_or_creating_temporary_files(self):
        os.mkfifo(self.target, 0o600)
        before = self.target.lstat()
        with self.assertRaisesRegex(ValueError, "regular"):
            self.write()
        self.assertEqual(self.target.lstat(), before)
        self.assert_no_temporary()

    def test_foreign_parent_is_refused_without_mutation(self):
        before = self.parent.stat()
        with self.assertRaisesRegex(ValueError, "parent"):
            self.write(uid=self.uid + 1)
        self.assertEqual(self.parent.stat(), before)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_foreign_target_is_refused_before_mutation(self):
        self.target.write_bytes(b"retained policy")
        before = self.snapshot(self.target)
        original = os.fstat
        def foreign(descriptor):
            found = original(descriptor)
            if found.st_ino == before[0].st_ino:
                values = list(found)
                values[4] = self.uid + 1
                return os.stat_result(values)
            return found
        with patch.object(confinement.os, "fstat", side_effect=foreign), self.assertRaisesRegex(ValueError, "administrator"):
            self.write()
        self.assertEqual(self.snapshot(self.target), before)
        self.assert_no_temporary()

    def test_group_or_world_writable_parent_or_target_is_refused(self):
        for parent_mode, target_mode in ((0o775, 0o644), (0o777, 0o644), (0o755, 0o664), (0o755, 0o646)):
            with self.subTest(parent_mode=oct(parent_mode), target_mode=oct(target_mode)):
                self.parent.chmod(0o755)
                self.target.write_bytes(b"retained policy")
                self.parent.chmod(parent_mode)
                self.target.chmod(target_mode)
                before = self.snapshot(self.target)
                with self.assertRaises(ValueError):
                    self.write()
                self.assertEqual(self.snapshot(self.target), before)
                self.assert_no_temporary()

    def test_publication_failure_preserves_original_and_cleans_temporary_file(self):
        self.target.write_bytes(b"retained policy")
        before = self.snapshot(self.target)
        with patch.object(confinement.os, "replace", side_effect=OSError("injected publication failure")):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(self.snapshot(self.target), before)
        self.assert_no_temporary()

    def test_sync_failure_has_complete_bytes_and_can_be_replayed(self):
        original_sync = os.fsync
        for failed_call in (1, 2):
            with self.subTest(failed_call=failed_call):
                self.write(b"old configuration")
                inode = self.target.stat().st_ino
                count = 0
                def fail_once(descriptor):
                    nonlocal count
                    count += 1
                    if count == failed_call:
                        raise OSError("injected sync failure")
                    return original_sync(descriptor)
                with patch.object(confinement.os, "fsync", side_effect=fail_once), self.assertRaises(OSError):
                    self.write(b"new configuration")
                self.assertEqual(self.target.read_bytes(), b"old configuration" if failed_call == 1 else b"new configuration")
                self.assertEqual(self.target.stat().st_ino == inode, failed_call == 1)
                self.assertEqual(self.target.stat().st_nlink, 1)
                self.assert_no_temporary()
                self.write(b"new configuration")
                current = self.target.stat().st_ino
                self.assertFalse(self.write(b"new configuration"))
                self.assertEqual(self.target.stat().st_ino, current)

    def test_initial_publication_does_not_clobber_a_concurrent_file(self):
        original = os.link
        def collide(*args, **kwargs):
            self.target.write_bytes(b"concurrent configuration")
            return original(*args, **kwargs)
        with patch.object(confinement.os, "link", side_effect=collide), self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(self.target.read_bytes(), b"concurrent configuration")
        self.assert_no_temporary()

    def test_changed_target_is_refused_before_replacement(self):
        self.target.write_bytes(b"initial configuration")
        original = os.stat
        replaced = False
        def replace_before_compare(path, *args, **kwargs):
            nonlocal replaced
            if path == self.target.name and not replaced:
                replaced = True
                self.target.rename(self.parent / "prior.conf")
                self.target.write_bytes(b"concurrent configuration")
            return original(path, *args, **kwargs)
        with patch.object(confinement.os, "stat", side_effect=replace_before_compare), \
                self.assertRaisesRegex(ValueError, "changed during"):
            self.write()
        self.assertEqual(self.target.read_bytes(), b"concurrent configuration")
        self.assertEqual((self.parent / "prior.conf").read_bytes(), b"initial configuration")
        self.assert_no_temporary()

    def test_invalid_paths_and_oversized_content_create_nothing(self):
        for path, content in ((Path("relative/node.conf"), b"policy"),
                              (self.parent / ".." / "escaped.conf", b"policy"),
                              (Path("/"), b"policy"), (self.target, b"x" * (confinement.MAXIMUM + 1))):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.write(path=path, content=content)
        self.assertEqual(list(self.parent.iterdir()), [])


class HelperCLI(unittest.TestCase):
    def test_non_root_or_extra_arguments_are_refused_before_reads_or_writes(self):
        for uid, arguments in ((1000, []), (0, ["unexpected"])):
            with self.subTest(uid=uid, arguments=arguments), patch.object(confinement.os, "geteuid", return_value=uid), \
                    patch.object(confinement, "expected") as expected, patch.object(confinement, "_write") as write, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(confinement.main(arguments), 2)
            expected.assert_not_called()
            write.assert_not_called()

    def test_cli_uses_actual_kernel_state_and_only_finite_expected_paths(self):
        for value, active in ((b"Y\n", True), (b"N\n", False)):
            artifacts = confinement.expected(active)
            with self.subTest(value=value), patch.object(confinement.os, "geteuid", return_value=0), \
                    patch.object(confinement.MODULE_ENABLED.__class__, "read_bytes", return_value=value), \
                    patch.object(confinement, "expected", return_value=artifacts) as expected, \
                    patch.object(confinement, "_write", return_value=True) as write, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(confinement.main([]), 0)
            expected.assert_called_once_with(active)
            self.assertEqual(write.call_args_list, [call(path, content) for path, content in artifacts.items()])
            report = json.loads(output.getvalue())
            self.assertIs(report["apparmor"], active)
            self.assertEqual(report["changed"], [str(path) for path in artifacts])

    def test_missing_kernel_module_installs_only_neutral_configuration(self):
        artifacts = confinement.expected(False)
        with patch.object(confinement.os, "geteuid", return_value=0), \
                patch.object(Path, "read_bytes", side_effect=FileNotFoundError), \
                patch.object(confinement, "_write", return_value=False) as write, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(confinement.main([]), 0)
        self.assertEqual(write.call_args_list, [call(path, content) for path, content in artifacts.items()])
        report = json.loads(output.getvalue())
        self.assertIs(report["apparmor"], False)
        self.assertEqual(report["changed"], [])

    def test_missing_paired_license_is_refused_before_publishing_any_artifact(self):
        original = Path.read_bytes
        def read(path):
            if path == confinement.MODULE_ENABLED:
                return b"Y"
            if path == Path(str(confinement.SOURCE) + ".LICENSE"):
                raise FileNotFoundError("missing paired license")
            return original(path)
        with patch.object(confinement.os, "geteuid", return_value=0), patch.object(Path, "read_bytes", read), \
                patch.object(confinement, "_write") as write, redirect_stderr(io.StringIO()):
            self.assertEqual(confinement.main([]), 2)
        write.assert_not_called()

    def test_cli_ambiguous_or_unreadable_kernel_state_never_writes(self):
        for value in (b"", b"unknown", PermissionError("kernel status unreadable")):
            with self.subTest(value=value), patch.object(confinement.os, "geteuid", return_value=0), \
                    patch.object(Path, "read_bytes", **({"side_effect": value} if isinstance(value, Exception) else {"return_value": value})), \
                    patch.object(confinement, "_write") as write, redirect_stderr(io.StringIO()):
                self.assertEqual(confinement.main([]), 2)
            write.assert_not_called()

    def test_cli_reports_write_failure_without_success_receipt(self):
        with patch.object(confinement.os, "geteuid", return_value=0), patch.object(Path, "read_bytes", return_value=b"N"), \
                patch.object(confinement, "_write", side_effect=ValueError("unsafe artifact")), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(confinement.main([]), 2)
        self.assertEqual(output.getvalue(), "")

    def test_verify_cli_requires_root_and_an_allowlisted_absolute_parser(self):
        for uid, arguments in ((1000, ["--verify", confinement.PARSERS[0]]),
                               (0, ["--verify", "/tmp/arbitrary-parser"]),
                               (0, ["--verify", "apparmor_parser"]), (0, ["--verify"]),
                               (0, ["--verify", confinement.PARSERS[0], "extra"])):
            with self.subTest(uid=uid, arguments=arguments), patch.object(confinement.os, "geteuid", return_value=uid), \
                    patch.object(confinement, "_verify") as verify, patch.object(confinement, "_write") as write, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(confinement.main(arguments), 2)
            verify.assert_not_called()
            write.assert_not_called()

    def test_verify_cli_refuses_disabled_kernel_without_loading_policy(self):
        with patch.object(confinement.os, "geteuid", return_value=0), patch.object(Path, "read_bytes", return_value=b"N"), \
                patch.object(confinement, "_verify") as verify, patch.object(confinement, "_write") as write, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(confinement.main(["--verify", confinement.PARSERS[0]]), 2)
        verify.assert_not_called()
        write.assert_not_called()

    def test_verify_cli_uses_only_verification_and_reports_its_result(self):
        report = {"verified": True, "profile": confinement.PROFILE, "enforced": True, "restart_marked": False}
        with patch.object(confinement.os, "geteuid", return_value=0), patch.object(Path, "read_bytes", return_value=b"Y"), \
                patch.object(confinement, "_verify", return_value=report) as verify, \
                patch.object(confinement, "_write") as write, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(confinement.main(["--verify", confinement.PARSERS[0]]), 0)
        verify.assert_called_once_with(confinement.PARSERS[0])
        write.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_verify_cli_catches_parser_failure_and_timeout(self):
        for issue in (subprocess.CalledProcessError(1, [confinement.PARSERS[0]]),
                      subprocess.TimeoutExpired([confinement.PARSERS[0]], 60)):
            with self.subTest(issue=issue), patch.object(confinement.os, "geteuid", return_value=0), \
                    patch.object(Path, "read_bytes", return_value=b"Y"), \
                    patch.object(confinement, "_verify", side_effect=issue), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                self.assertEqual(confinement.main(["--verify", confinement.PARSERS[0]]), 2)
            self.assertEqual(output.getvalue(), "")


class EnforcementVerification(unittest.TestCase):
    def setUp(self):
        repository = next((parent for parent in (AREA, *AREA.parents) if (parent / ".git").exists()), AREA)
        scratch = Path(os.environ.get("TMPDIR", repository / ".scratch/review-node-helper-tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source/policy"
        self.source.parent.mkdir(mode=0o755)
        self.source.write_bytes(b"profile thermite-node-exporter { # fixture policy\n}\n")
        Path(str(self.source) + ".LICENSE").write_bytes(b"fixture paired license\n")
        self.config = self.root / "config/node.conf"
        self.policy = self.root / "policies/node"
        self.license = self.root / "licenses/node.LICENSE"
        self.profiles = self.root / "kernel-profiles"
        paths = patch.multiple(confinement, CONFIG=self.config, POLICY=self.policy, LICENSE=self.license,
                               SOURCE=self.source, PROFILES=self.profiles)
        paths.start()
        self.addCleanup(paths.stop)
        self.uid, self.gid = os.geteuid(), os.getegid()
        for path, content in confinement.expected(True).items():
            confinement._write(path, content, uid=self.uid, gid=self.gid)
        # Controlled old times avoid clock-resolution assumptions when checking
        # whether successful verification published a new restart marker.
        os.utime(self.policy, ns=(1_000_000_000, 1_000_000_000))
        os.utime(self.config, ns=(2_000_000_000, 2_000_000_000))
        self.profiles.write_text(confinement.PROFILE + " (enforce)\n")
        self.parser = confinement.PARSERS[0]
        self.run = Mock(side_effect=self.enforce)
        parser = patch.object(confinement.subprocess, "run", self.run)
        parser.start()
        self.addCleanup(parser.stop)

    def enforce(self, *args, **kwargs):
        self.profiles.write_text(confinement.PROFILE + " (enforce)\n")
        return subprocess.CompletedProcess(args[0], 0)

    def verify(self, parser=None):
        with redirect_stdout(io.StringIO()):
            return confinement._verify(parser or self.parser, uid=self.uid, gid=self.gid)

    def pending(self):
        os.utime(self.policy, ns=(3_000_000_000, 3_000_000_000))

    def test_current_enforced_profile_preserves_marker_and_never_invokes_parser(self):
        before = self.config.stat()
        self.assertEqual(self.verify(), {"verified": True, "profile": confinement.PROFILE,
                                         "enforced": True, "restart_marked": False})
        self.run.assert_not_called()
        after = self.config.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_missing_or_complain_profile_loads_then_marks_one_restart(self):
        for loaded in ("", confinement.PROFILE + " (complain)\n"):
            with self.subTest(loaded=loaded):
                self.profiles.write_text(loaded)
                before = self.config.stat()
                self.assertTrue(self.verify()["restart_marked"])
                self.run.assert_called_once_with([self.parser, "-r", str(self.policy)], check=True, timeout=60)
                after = self.config.stat()
                self.assertEqual(after.st_ino, before.st_ino)
                self.assertGreater(after.st_mtime_ns, before.st_mtime_ns)
                self.run.reset_mock()
                self.assertFalse(self.verify()["restart_marked"])
                self.run.assert_not_called()
                self.assertEqual(self.config.stat().st_mtime_ns, after.st_mtime_ns)

    def test_new_policy_reloads_even_if_old_profile_is_enforced(self):
        self.pending()
        before = self.config.stat()
        self.assertTrue(self.verify()["restart_marked"])
        self.run.assert_called_once_with([self.parser, "-r", str(self.policy)], check=True, timeout=60)
        self.assertGreater(self.config.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertGreaterEqual(self.config.stat().st_mtime_ns, self.policy.stat().st_mtime_ns)

    def test_parser_failure_or_timeout_keeps_policy_pending(self):
        for issue in (subprocess.CalledProcessError(1, [self.parser]), subprocess.TimeoutExpired([self.parser], 60)):
            with self.subTest(issue=issue):
                self.pending()
                before = self.config.stat().st_mtime_ns
                self.run.side_effect = issue
                with self.assertRaises(type(issue)):
                    self.verify()
                self.assertEqual(self.config.stat().st_mtime_ns, before)
                self.assertGreater(self.policy.stat().st_mtime_ns, self.config.stat().st_mtime_ns)

    def test_successful_parser_without_enforce_state_keeps_policy_pending(self):
        self.pending()
        self.profiles.write_text(confinement.PROFILE + " (complain)\n")
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([self.parser], 0)
        before = self.config.stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "enforce mode"):
            self.verify()
        self.assertEqual(self.config.stat().st_mtime_ns, before)
        self.assertGreater(self.policy.stat().st_mtime_ns, self.config.stat().st_mtime_ns)

    def test_missing_profile_never_counts_a_different_profile_as_enforced(self):
        self.profiles.write_text("other-node-exporter (enforce)\n")
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([self.parser], 0)
        before = self.config.stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "enforce mode"):
            self.verify()
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_missing_artifact_parent_is_not_created_by_verification(self):
        missing = self.root / "missing-parent/node.conf"
        with patch.object(confinement, "CONFIG", missing), self.assertRaises(FileNotFoundError):
            self.verify()
        self.assertFalse(missing.parent.exists())
        self.run.assert_not_called()

    def test_unreviewed_bytes_and_unsafe_modes_are_refused_before_parser(self):
        for path in (self.config, self.policy, self.license):
            content = path.read_bytes()
            for mode in (0o600, 0o664):
                with self.subTest(path=path, mode=oct(mode)):
                    path.chmod(mode)
                    try:
                        with self.assertRaisesRegex(ValueError, "unsafe"):
                            self.verify()
                    finally:
                        path.chmod(0o644)
            path.write_bytes(content + b"unreviewed change")
            try:
                with self.assertRaisesRegex(ValueError, "differs from"):
                    self.verify()
            finally:
                path.write_bytes(content)
        self.run.assert_not_called()

    def test_foreign_uid_or_gid_is_refused_before_parser(self):
        inode = self.policy.stat().st_ino
        original = os.fstat
        for field in (4, 5):
            def foreign(descriptor):
                info = original(descriptor)
                if info.st_ino == inode:
                    values = list(info)
                    values[field] += 1
                    return os.stat_result(values)
                return info
            with self.subTest(field=field), patch.object(confinement.os, "fstat", side_effect=foreign), \
                    self.assertRaisesRegex(ValueError, "unsafe"):
                self.verify()
        self.run.assert_not_called()

    def test_symlink_hardlink_and_fifo_artifacts_are_refused_before_parser(self):
        content = self.policy.read_bytes()
        external = self.root / "outside-policy"
        external.write_bytes(content)
        for kind in ("symlink", "hardlink", "fifo"):
            self.policy.unlink()
            if kind == "symlink":
                self.policy.symlink_to(external)
            elif kind == "hardlink":
                os.link(external, self.policy)
            else:
                os.mkfifo(self.policy, 0o644)
            before = external.stat()
            try:
                with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)):
                    self.verify()
                self.assertEqual(external.stat(), before)
            finally:
                self.policy.unlink()
                self.policy.write_bytes(content)
                self.policy.chmod(0o644)
        self.run.assert_not_called()

    def test_writable_artifact_parent_is_refused_before_parser(self):
        self.policy.parent.chmod(0o775)
        with self.assertRaisesRegex(ValueError, "parent"):
            self.verify()
        self.run.assert_not_called()

    def test_changed_configuration_inode_does_not_mark_a_replacement(self):
        self.pending()
        def replace_during_parse(*args, **kwargs):
            self.config.rename(self.root / "prior-config")
            self.config.write_bytes(confinement.expected(True)[self.config])
            self.config.chmod(0o644)
            os.utime(self.config, ns=(4_000_000_000, 4_000_000_000))
            return self.enforce(*args, **kwargs)
        self.run.side_effect = replace_during_parse
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.verify()
        self.assertEqual(self.config.stat().st_mtime_ns, 4_000_000_000)

    def test_direct_verification_rejects_arbitrary_parser_before_file_reads(self):
        with self.assertRaisesRegex(ValueError, "system AppArmor parser"):
            self.verify("/tmp/not-a-system-parser")
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
