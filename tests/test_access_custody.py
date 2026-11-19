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

from oh_access import AccessError, custody, files, staging
from oh_access.files import private_directory


class ServiceAccountGuard(unittest.TestCase):
    """The account an access service runs as: looked up on a host, stated by
    the unit in a container, and in both the process's own."""

    def guard(self, stated=None, *, account=None):
        lookup = patch.object(files, "service_identity", side_effect=account or AssertionError("looked up"))
        with patch.dict(os.environ), lookup as looked:
            os.environ.pop(files.STATED_IDENTITY, None)
            if stated is not None:
                os.environ[files.STATED_IDENTITY] = stated
            try:
                return files.require_service_account()
            finally:
                self.looked = looked.called

    def test_stated_numbers_pass_only_for_a_process_that_runs_as_them_and_no_account_is_looked_up(self):
        uid, gid = os.geteuid(), os.getegid()
        if not uid or not gid:
            self.skipTest("the guard refuses root before it compares")
        self.assertEqual(self.guard(f"{uid}:{gid}"), (uid, gid))
        self.assertFalse(self.looked)
        for stated in (f"{uid + 1}:{gid}", f"{uid}:{gid + 1}", f"{gid + 1}:{uid + 1}"):
            with self.subTest(stated=stated), self.assertRaisesRegex(AccessError, "as user thermite and group thermite"):
                self.guard(stated)
            self.assertFalse(self.looked)

    def test_a_malformed_or_root_statement_is_refused_and_never_falls_back_to_the_lookup(self):
        for stated in ("", "0:0", "0:9001", "9001:0", "9001", "9001:", ":9001", "thermite:thermite", "9001:9001:1",
                       " 9001:9001", "9001:9001\n", "-1:9001", "09001:9001", "+9001:9001", "1" * 11 + ":9001"):
            with self.subTest(stated=stated), self.assertRaisesRegex(AccessError, "UID:GID"):
                self.guard(stated)
            self.assertFalse(self.looked)

    def test_without_a_statement_the_named_account_is_required_as_before(self):
        mine = (os.geteuid(), os.getegid())
        self.assertEqual(self.guard(account=lambda *, required: mine), mine)
        self.assertTrue(self.looked)
        with self.assertRaisesRegex(AccessError, "as user thermite and group thermite"):
            self.guard(account=lambda *, required: (mine[0] + 1, mine[1]))
        with self.assertRaisesRegex(AccessError, "Create the thermite service account"):
            self.guard(account=AccessError("Create the thermite service account and matching primary group"))

    def test_a_stage_plan_never_reads_the_statement(self):
        with patch.dict(os.environ, {files.STATED_IDENTITY: "1:1"}), \
                patch.object(files.pwd, "getpwnam", side_effect=KeyError), \
                patch.object(files.grp, "getgrnam", side_effect=KeyError):
            self.assertEqual(files.service_identity(), (files.UID, files.GID))


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
        # Any read, the fixture's or custody's, may advance the access time
        # (relatime, against a ctime that custody's chmod just set), so it
        # is not compared; what a replay must keep is.
        content = path.read_bytes() if path.is_file() else None
        info = path.stat()
        return (info.st_mode, info.st_ino, info.st_nlink, info.st_uid, info.st_gid, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns), content

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
        with self.assertRaisesRegex(AccessError, "plan directory is required"):
            staging.stage(target, state=state, house="unused", assets="unused", runtime_url="unused",
                          runtime_token_file="unused", tls_cert="unused", tls_key="unused", allowed_hosts=[],
                          listen=["127.0.0.1"], port=13601, timezone="UTC")
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
                   "verification_command": ["true"], "destination": str(self.release), "units": list(staging.units(self.release, state=self.state)),
                   "unit_install_commands": []}
        generated_units = staging.units(self.release, state=self.state)
        with staging._new_release(plan, purpose="Plan", private_parent=True) as descriptor:
            staging._export_plan(descriptor, receipt, self.plan, generated_units)
        # The receipt, the manifest, its helper and the unit copies: nothing
        # in a plan is a script for an owner to run.
        self.assertEqual({path.name for path in plan.iterdir()},
                         {"stage.json", "custody.json", "custody.py", "units"})
        self.assertEqual(json.loads((plan / "stage.json").read_text()), receipt)
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

    def test_service_writable_release_or_state_ancestor_is_refused_before_setup(self):
        for which in ("release", "state"):
            with self.subTest(which=which), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                root = Path(temporary)
                shared = root / "shared"
                protected = shared / "protected"
                protected.mkdir(parents=True, mode=0o750)
                shared.chmod(0o2770)
                target = (protected if which == "release" else root) / "new-release"
                state = (protected if which == "state" else root) / "new-state"
                with patch.object(staging, "service_identity", return_value=(os.geteuid() + 1, os.getegid())), \
                        patch.object(staging, "_disposable", return_value=False), patch.object(staging, "_state_location"), \
                        patch.object(staging, "_service_parents"), patch.object(staging, "_state_directory") as create, \
                        patch.object(staging.venv, "EnvBuilder") as builder, patch.object(staging.subprocess, "run") as run:
                    with self.assertRaisesRegex(AccessError, "Release and state ancestors.*protected from replacement"):
                        staging.stage(target, state=state, house="unused", assets="unused", runtime_url="unused",
                                      runtime_token_file="unused", tls_cert="unused", tls_key="unused", allowed_hosts=[],
                                      listen=["127.0.0.1"], port=13601, timezone="UTC", plan_directory=root / "plan",
                                      area=root / "source")
                create.assert_not_called()
                builder.assert_not_called()
                run.assert_not_called()
                self.assertFalse(target.exists())
                self.assertFalse(state.exists())
                self.assertFalse((root / "plan").exists())

    def test_operator_owned_2750_parents_and_root_sticky_ancestor_are_accepted(self):
        parent = self.root / "releases"
        parent.mkdir()
        parent.chmod(0o2750)
        staging._trusted_ancestors(parent, "Release and state")
        original = staging.os.fstat
        inode = parent.stat().st_ino
        def sticky_root(descriptor):
            info = original(descriptor)
            if info.st_ino == inode:
                values = list(info)
                values[0], values[4] = stat.S_IFDIR | 0o1777, 0
                return os.stat_result(values)
            return info
        with patch.object(staging.os, "fstat", side_effect=sticky_root):
            staging._trusted_ancestors(parent, "Release and state")

    def test_service_owned_read_only_ancestor_is_not_a_trusted_staging_parent(self):
        parent = self.root / "service-owned"
        parent.mkdir(mode=0o750)
        original = staging.os.fstat
        inode = parent.stat().st_ino
        def service_owned(descriptor):
            info = original(descriptor)
            if info.st_ino == inode:
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info
        with patch.object(staging.os, "fstat", side_effect=service_owned):
            with self.assertRaisesRegex(AccessError, "protected from replacement"):
                staging._trusted_ancestors(parent, "Release and state")


if __name__ == "__main__":
    unittest.main()
