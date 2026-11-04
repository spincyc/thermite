"""Source upgrades use real fixture Git repositories and a simulated host."""
from pathlib import Path
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_init
import oh_release
import oh_source_upgrade as update
import oh_application_update as application


class ApplicationFixture(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/unified-application"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "operator home"
        self.source = self.root / "application source"
        self.repo = self.root / "private repository"
        self.site = self.repo / "automation/site"
        self.old = self.program("old")
        self.new = self.program("new")
        self.source.mkdir()
        (self.source / "README").write_text("reviewed application\n")
        oh_init.initialize(self.site, program=self.old)
        self.initialize_git(self.source)
        self.initialize_git(self.repo)
        self.before = update.git(self.repo, "rev-parse", "HEAD")
        self.launcher = self.home / ".local/bin/thermite"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(self.old / "bin/thermite")
        self.calls = []
        self.fail = None
        self.edit_during_check = False
        self.edit_during_host = False
        self.change_source_during_build = False
        self.approvals = []
        self.real_run = update.run

    def program(self, name):
        root = self.root / name
        files = {"bin/thermite": name.encode(), "instance/devices.yaml": b"fictional: true\n",
                 "instance/host.yaml": b"fixture: true\n", "versions.json": b"{}\n"}
        for filename, data in files.items():
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (root / oh_release.FILE).write_text(json.dumps(oh_release.create(files)))
        return root

    def initialize_git(self, path):
        update.git(path, "init", "--initial-branch=fixture")
        update.git(path, "config", "user.name", "Fixture Operator")
        update.git(path, "config", "user.email", "fixture@example.invalid")
        update.git(path, "add", ".")
        update.git(path, "commit", "-m", "Fixture baseline")

    def simulate(self, *argv, capture=False):
        args = tuple(map(str, argv))
        self.calls.append(args)
        if args[0] == "git":
            if self.fail == "commit" and args[3] == "commit":
                raise subprocess.CalledProcessError(1, args)
            return self.real_run(*argv, capture=capture)
        if args[:3] == (sys.executable, "-m", "venv"):
            binary = Path(args[3]) / "bin"
            binary.mkdir()
            (binary / "thermite").write_text("fixture CLI")
            shutil.copytree(self.new, Path(args[3]) / "runtime")
            return ""
        if args[1:6] == ("-I", "-m", "pip", "--isolated", "install"):
            if self.fail == "pip":
                raise subprocess.CalledProcessError(1, args)
            if self.change_source_during_build:
                (self.source / "README").write_text("concurrent source edit\n")
            return ""
        if args[1:5] == ("-I", "-B", "-c", application.VERIFY):
            root = Path(args[0]).parents[1] / "runtime"
            release = oh_release.manifest(root)
            return json.dumps({"root": str(root), "digest": release["digest"], "application": release["application"]})
        if args[1] == "init":
            oh_init.initialize(self.site, program=self.new, upgrade=True)
        elif args[1] in ("upgrade", "install"):
            self.assertEqual(update.clean(self.repo), update.git(self.repo, "rev-parse", "HEAD"))
            self.assertEqual(self.launcher.resolve(), self.previous_launcher)
            self.assertIn("--_host-phase", args)
            self.assertIn("--_run-approved", args)
            if self.edit_during_host:
                (self.site / "instance/host.yaml").write_text("concurrent host edit\n")
        elif args[1] == "check" and self.edit_during_check:
            with (self.site / ".gitignore").open("a") as out:
                out.write("/concurrent-user-edit/\n")
        elif args[1] != "check":
            self.failTest(f"unexpected command: {args}")
        if args[1] == self.fail:
            raise subprocess.CalledProcessError(1, args)
        return ""

    def execute(self, **kwargs):
        self.previous_launcher = self.launcher.resolve()
        def approve():
            self.approvals.append(True)
            return True
        with patch.object(update, "run", side_effect=self.simulate):
            return update.upgrade(self.site, source=self.source, home=self.home, approve=approve, **kwargs)


class SourceUpgrade(ApplicationFixture):

    def test_complete_upgrade_commits_only_pin_then_selects_successful_environment(self):
        self.execute()
        after = update.clean(self.repo)
        self.assertNotEqual(after, self.before)
        changed = update.git(self.repo, "diff", "--name-only", self.before, after).splitlines()
        self.assertEqual(changed, ["automation/site/thermite-site.json"])
        self.assertEqual(json.loads((self.site / "thermite-site.json").read_text())["release"],
                         oh_release.manifest(self.new)["digest"])
        self.assertNotEqual(self.launcher.resolve(), self.old / "bin/thermite")
        self.assertTrue(self.launcher.resolve().is_file())
        self.assertTrue((self.old / "bin/thermite").is_file())
        self.assertEqual([args[1] for args in self.calls if args[0].endswith("/bin/thermite")],
                         ["init", "check", "upgrade"])
        self.assertEqual(self.approvals, [True])

    def test_packaging_validation_and_commit_failures_preserve_old_pin_and_launcher(self):
        for failure in ("pip", "init", "check", "commit"):
            with self.subTest(failure=failure):
                self.fail = failure
                self.calls.clear()
                with self.assertRaises(subprocess.CalledProcessError):
                    self.execute()
                self.assertEqual(update.clean(self.repo), self.before)
                self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")
                self.assertFalse(any(args[1:2] == ("upgrade",) for args in self.calls))

    def test_runtime_failure_retains_candidate_and_same_command_can_resume(self):
        self.fail = "upgrade"
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        committed = update.clean(self.repo)
        self.assertNotEqual(committed, self.before)
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")
        candidates = list((self.home / ".local/share/thermite/application").glob("*/bin/thermite"))
        self.assertEqual(len(candidates), 1)
        self.fail = None
        self.calls.clear()
        self.execute()
        self.assertEqual(update.clean(self.repo), committed, "retry must not make a duplicate pin commit")
        self.assertTrue(candidates[0].is_file())
        self.assertEqual(self.launcher.resolve(), candidates[0])
        self.assertFalse(any("pip" in args for args in self.calls))

    def test_dirty_application_or_any_part_of_site_repository_stops_before_packaging(self):
        for directory in (self.source, self.repo):
            dirty = directory / "unrelated work"
            dirty.write_text("keep me\n")
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, "local changes"):
                self.execute()
            self.assertEqual(dirty.read_text(), "keep me\n")
            dirty.unlink()
        self.assertFalse((self.home / ".local/share").exists())

    def test_non_symlink_launcher_is_preserved(self):
        self.launcher.unlink()
        self.launcher.write_text("custom command\n")
        with self.assertRaisesRegex(ValueError, "non-symlink launcher"):
            self.execute()
        self.assertEqual(self.launcher.read_text(), "custom command\n")
        self.assertFalse((self.home / ".local/share").exists())

    def test_concurrent_edit_is_neither_committed_nor_overwritten_by_cleanup(self):
        self.edit_during_check = True
        with self.assertRaisesRegex(ValueError, "changed during validation"):
            self.execute()
        self.assertEqual(update.git(self.repo, "rev-parse", "HEAD"), self.before)
        self.assertIn("/concurrent-user-edit/", (self.site / ".gitignore").read_text())
        self.assertEqual(json.loads((self.site / "thermite-site.json").read_text())["release"],
                         oh_release.manifest(self.old)["digest"])
        self.assertEqual(self.launcher.resolve(), self.old / "bin/thermite")

    def test_concurrent_upgrade_is_refused_before_creating_environment(self):
        directory = self.home / ".local/share/thermite/application"
        with update.locked(directory), self.assertRaisesRegex(ValueError, "another Thermite"):
            self.execute()
        self.assertEqual(list(directory.iterdir()), [directory / ".upgrade.lock"])

    def test_source_alias_forwards_to_the_one_public_upgrade_command(self):
        with patch.object(update.subprocess, "call", return_value=7) as call:
            self.assertEqual(update.main(["--config", str(self.site), "--dry-run"]), 7)
        call.assert_called_once_with([sys.executable, str(update.SOURCE / "bin/thermite"),
                                      "upgrade", "--config", str(self.site), "--dry-run"])
