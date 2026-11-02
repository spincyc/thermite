"""A replayed root custody plan cannot follow service-controlled replacements."""

import json
import io
import os
from pathlib import Path
import resource
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_access import AccessError, custody, staging
from oh_access.files import private_directory


class Custody(unittest.TestCase):
    def setUp(self):
        if os.environ.get("TMPDIR"):
            scratch = Path(os.environ["TMPDIR"])
        else:
            repository = next((parent for parent in (AREA, *AREA.parents) if (parent / ".git").exists()), AREA)
            scratch = repository / ".scratch/test-access-custody"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        (self.release / "program/bin").mkdir(parents=True)
        (self.release / "program/bin/tool").write_text("staged executable")
        (self.release / "program/bin/tool").chmod(0o700)
        (self.release / "config.json").write_text("private configuration")
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.plan = custody.manifest([staging._inventory(self.release, release=True),
                                      staging._inventory(self.state)], os.geteuid(), os.getegid())
        self.external = self.root / "external-prefix"
        (self.external / "bin").mkdir(parents=True)
        (self.external / "bin/tool").write_text("external executable")

    def snapshot(self, path):
        return path.stat(), path.read_bytes() if path.is_file() else None

    def test_replay_is_idempotent_and_preserves_new_private_state(self):
        custody.apply(self.plan)
        secret = self.state / "identity.json"
        secret.write_text("identity created after handoff")
        secret.chmod(0o600)
        before = {path: self.snapshot(path) for path in (self.release, self.release / "config.json", secret)}
        with patch.object(custody.os, "fchmod") as chmod, patch.object(custody.os, "fchown") as chown:
            custody.apply(self.plan)
        chmod.assert_not_called()
        chown.assert_not_called()
        self.assertEqual({path: self.snapshot(path) for path in before}, before)

    def test_intermediate_symlink_replay_rejects_before_any_mutation(self):
        custody.apply(self.plan)
        shutil.rmtree(self.release / "program")
        (self.release / "program").symlink_to(self.external, target_is_directory=True)
        self.release.chmod(0o750)
        before = {path: self.snapshot(path) for path in (self.release, self.external / "bin", self.external / "bin/tool")}
        with patch.object(custody.os, "fchmod") as chmod, patch.object(custody.os, "fchown") as chown:
            with self.assertRaises((OSError, custody.CustodyError)):
                custody.apply(self.plan)
        chmod.assert_not_called()
        chown.assert_not_called()
        self.assertEqual({path: self.snapshot(path) for path in before}, before)

    def test_replaced_regular_inode_is_rejected_before_any_mutation(self):
        original = self.release / "config.json"
        original.rename(self.release / "old-config")
        original.write_text("replaced")
        with patch.object(custody.os, "fchmod") as chmod, self.assertRaisesRegex(custody.CustodyError, "inode changed"):
            custody.apply(self.plan)
        chmod.assert_not_called()

    def test_added_hardlink_is_rejected_before_any_mutation(self):
        os.link(self.release / "config.json", self.external / "outside-link")
        with patch.object(custody.os, "fchmod") as chmod, self.assertRaisesRegex(custody.CustodyError, "hard links"):
            custody.apply(self.plan)
        chmod.assert_not_called()

    def test_same_device_mount_is_rejected_before_any_mutation(self):
        inode = (self.release / "config.json").stat().st_ino
        with patch.object(custody, "_mount_id", side_effect=lambda descriptor: 2 if os.fstat(descriptor).st_ino == inode else 1), \
                patch.object(custody.os, "fchmod") as chmod, self.assertRaisesRegex(custody.CustodyError, "mount changed"):
            custody.apply(self.plan)
        chmod.assert_not_called()

    def test_all_roots_are_validated_before_release_modes_change(self):
        self.state.rename(self.root / "original-state")
        self.state.mkdir(mode=0o700)
        before = self.release.stat()
        with patch.object(custody.os, "fchmod") as chmod, self.assertRaisesRegex(custody.CustodyError, "inode changed"):
            custody.apply(self.plan)
        chmod.assert_not_called()
        self.assertEqual(self.release.stat(), before)

    def test_partial_failure_can_resume_without_replacing_files(self):
        original = os.fchmod
        config = self.release / "config.json"
        inode = config.stat().st_ino
        before = config.read_bytes(), inode
        def fail_at_config(descriptor, mode):
            if os.fstat(descriptor).st_ino == inode:
                raise OSError("injected interrupted handoff")
            original(descriptor, mode)
        with patch.object(custody.os, "fchmod", side_effect=fail_at_config):
            with self.assertRaisesRegex(OSError, "interrupted handoff"):
                custody.apply(self.plan)
        custody.apply(self.plan)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual((config.read_bytes(), config.stat().st_ino), before)

    def test_standalone_entry_checks_saved_identity_then_applies_manifest(self):
        path = self.root / "custody.json"
        path.write_text(json.dumps(self.plan))
        path.chmod(0o600)
        account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid())
        group = SimpleNamespace(gr_gid=os.getegid())
        with patch.object(custody.pwd, "getpwnam", return_value=account), \
                patch.object(custody.grp, "getgrnam", return_value=group), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(custody.main([str(path)]), 0)
        account.pw_uid += 1
        with patch.object(custody.pwd, "getpwnam", return_value=account), \
                patch.object(custody.grp, "getgrnam", return_value=group), \
                patch.object(custody.os, "fchmod") as chmod, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(custody.main([str(path)]), 2)
        chmod.assert_not_called()

    def test_repair_walk_rechecks_service_replacements(self):
        original = os.fchmod
        replaced = False
        def replace_after_preflight(descriptor, mode):
            nonlocal replaced
            original(descriptor, mode)
            if not replaced:
                replaced = True
                shutil.rmtree(self.release / "program")
                (self.release / "program").symlink_to(self.external, target_is_directory=True)
        before = self.snapshot(self.external / "bin"), self.snapshot(self.external / "bin/tool")
        with patch.object(custody.os, "fchmod", side_effect=replace_after_preflight):
            with self.assertRaises((OSError, custody.CustodyError)):
                custody.apply(self.plan)
        self.assertTrue(replaced)
        self.assertEqual((self.snapshot(self.external / "bin"), self.snapshot(self.external / "bin/tool")), before)

    def test_internal_environment_link_is_not_dereferenced(self):
        (self.release / "env/lib").mkdir(parents=True)
        (self.release / "env/lib64").symlink_to("lib", target_is_directory=True)
        plan = custody.manifest([staging._inventory(self.release, release=True)], os.geteuid(), os.getegid())
        custody.apply(plan)
        self.assertTrue((self.release / "env/lib64").is_symlink())
        self.assertEqual((self.release / "env/lib64").readlink(), Path("lib"))

    def test_manifest_and_walk_keep_descriptor_use_bounded(self):
        for number in range(140):
            (self.release / f"program/file-{number}").write_text("fixture")
        plan = custody.manifest([staging._inventory(self.release, release=True)], os.geteuid(), os.getegid())
        old = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, old[1]))
            custody.apply(plan)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, old)

    def test_required_operator_plan_fails_before_creating_state_or_release(self):
        target, state = self.root / "new-release", self.root / "new-state"
        with self.assertRaisesRegex(AccessError, "--plan-directory"):
            staging.stage(target, state=state, house="unused", assets="unused", runtime_url="unused",
                          runtime_token_file="unused", tls_cert="unused", tls_key="unused", allowed_hosts=[],
                          bind_host="127.0.0.1", port=13600, timezone="UTC")
        self.assertFalse(target.exists())
        self.assertFalse(state.exists())

    def test_operator_plan_cannot_be_replaced_through_writable_ancestors(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o770)
        shared.chmod(0o770)
        parent = shared / "private-plan-parent"
        parent.mkdir(mode=0o700)
        with self.assertRaisesRegex(AccessError, "protected from replacement"):
            staging._plan_location(parent / "plan", self.root / "new-release", self.state)

    def test_export_uses_operator_helper_and_finite_inode_manifest(self):
        plan = self.root / "operator-plan"
        parent = self.root / "plans"
        parent.mkdir(mode=0o700)
        plan = parent / "first"
        receipt = {"ownership_commands": [["sudo", "/usr/bin/python3", "-I", "-S", str(plan / "custody.py"), str(plan / "custody.json")]],
                   "verification_command": ["true"]}
        generated_units = staging.units(self.release, state=self.state)
        with staging._new_release(plan, purpose="Plan", private_parent=True) as descriptor:
            staging._export_plan(descriptor, receipt, self.plan, generated_units)
        script = (plan / "handoff.sh").read_text()
        self.assertIn("sudo /usr/bin/python3 -I -S ", script)
        self.assertNotIn("sudo chmod", script)
        self.assertNotIn("sudo chown", script)
        self.assertEqual(json.loads((plan / "custody.json").read_text()), self.plan)
        self.assertEqual((plan / "custody.py").read_bytes(), Path(custody.__file__).read_bytes())
        for path in plan.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        # A service can replace its release units after handoff; the operator's
        # reviewed root-install source must retain the staged bytes.
        (self.release / "units").mkdir()
        for name, source in generated_units.items():
            (self.release / "units" / name).write_text("[Service]\nExecStart=/malicious-root-command\n")
            self.assertEqual((plan / "units" / name).read_text(), source)
            self.assertEqual(stat.S_IMODE((plan / "units" / name).stat().st_mode), 0o600)

    def test_private_directory_needs_only_search_access_to_ancestors(self):
        parent = self.root / "search-only-parent"
        parent.mkdir()
        child = parent / "private"
        child.mkdir(mode=0o700)
        parent.chmod(0o311 | stat.S_ISGID)
        try:
            self.assertEqual(private_directory(child), child)
            created = private_directory(parent / "new-private")
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700)
        finally:
            parent.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
