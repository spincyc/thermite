"""Source upgrades use real fixture Git repositories and a simulated host."""
import contextlib
from pathlib import Path
import io
import json
import os
import subprocess
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_init
import oh_release
import oh_source_upgrade as update
import oh_application_update as application
from oh_install import progress


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
        self.paused_commits = []
        self.real_run = update.run
        self.python = application.PYTHON
        self.packaged = self.new

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
            if args[3:4] == ("commit",):
                active = progress.current.get()
                self.paused_commits.append(active.paused if active else None)
            if self.fail == "commit" and args[3] == "commit":
                raise subprocess.CalledProcessError(1, args)
            return self.real_run(*argv, capture=capture)
        if args[:3] == (sys.executable, "-m", "venv"):
            binary = Path(args[3]) / "bin"
            binary.mkdir()
            (binary / "thermite").write_text("fixture CLI")
            (Path(args[3]) / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {application.PYTHON}.0\n")
            shutil.copytree(self.packaged, Path(args[3]) / "runtime")
            return ""
        if args[1:5] == ("-I", "-B", "-c", application.PROBE):
            if self.python is None:
                raise subprocess.CalledProcessError(1, args)
            return self.python + "\n"
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
            oh_init.initialize(self.site, program=self.packaged, upgrade=True)
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

    def simulated(self):
        """Every subprocess, the foreground host phase included, through simulate."""
        return patch.multiple(update, run=Mock(side_effect=self.simulate),
                              foreground=Mock(side_effect=self.simulate))

    def execute(self, **kwargs):
        self.previous_launcher = self.launcher.resolve()
        def approve():
            self.approvals.append(True)
            return True
        with self.simulated():
            return application.upgrade(self.site, source=self.source, home=self.home, approve=approve, **kwargs)


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

    def test_the_work_before_the_host_phase_runs_in_three_stages(self):
        seen = []

        @contextlib.contextmanager
        def stage(title):
            seen.append(title)
            yield
            seen.append("passed")

        self.execute(stage=stage)
        self.assertEqual(seen, ["Fetch the selected application", "passed", "Prepare its Python environment", "passed",
                                "Pin it in the site", "passed"])

    def test_the_site_commit_pauses_the_spinner_for_a_signing_prompt(self):
        said = []
        with contextlib.redirect_stdout(io.StringIO()):
            self.execute(stage=lambda title: progress.stage(SimpleNamespace(say=said.append), title))
        self.assertEqual(self.paused_commits, [1])
        self.assertEqual(said, ["[ OK ] Fetch the selected application", "[ OK ] Prepare its Python environment",
                                "[ OK ] Pin it in the site"])

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

    def test_a_failed_host_phase_keeps_the_candidate_and_the_same_command_reuses_it(self):
        # The host phase's install transaction undoes its own pin commit when
        # it rolls back; this fixture's host phase only fails.
        self.fail = "upgrade"
        self.assertEqual(self.execute(), 1)
        host = next(args for args in self.calls if args[1:2] == ("upgrade",))
        self.assertEqual(host[host.index("--_site-before") + 1], self.before)
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


class ChildOutput(unittest.TestCase):
    def setUp(self):
        options = patch.object(oh_cli, "switches", oh_cli.Switches())
        options.start()
        self.addCleanup(options.stop)

    def test_a_child_speaks_only_when_it_fails(self):
        script = "import sys; print('remote: counting objects'); print('fatal: \\x1b[31mgone\\x1b[0m', file=sys.stderr)"
        for code in (0, 3):
            with self.subTest(code=code), contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as error, \
                    patch.object(update.subprocess, "Popen", wraps=subprocess.Popen) as spawned:
                argv = (sys.executable, "-I", "-u", "-c", f"{script}; raise SystemExit({code})")
                if code:
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        update.run(*argv)
                    self.assertEqual(raised.exception.returncode, code)
                else:
                    self.assertEqual(update.run(*argv), "")
                options = spawned.call_args.kwargs
                self.assertEqual((options["stdin"], options["stdout"], options["stderr"]),
                                 (subprocess.DEVNULL, subprocess.PIPE, subprocess.STDOUT))
                self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0", "git fails rather than ask unseen")
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(error.getvalue(), "remote: counting objects\nfatal: gone\n" if code else "")

    def test_verbose_leaves_a_child_its_streams(self):
        oh_cli.switches.verbose = True
        child = Mock(returncode=0, stdout=None)
        child.communicate.return_value = (None, None)
        with patch.object(update.subprocess, "Popen", return_value=child) as spawned:
            self.assertEqual(update.run("git", "fetch"), "")
        options = spawned.call_args.kwargs
        self.assertEqual((options["stdin"], options["stdout"], options["stderr"]), (None, None, None))
        self.assertNotIn("GIT_TERMINAL_PROMPT", options["env"])

    def test_ctrl_c_lets_the_child_clean_up_before_it_is_killed(self):
        child = Mock(stdout=None)
        child.communicate.side_effect = KeyboardInterrupt
        with patch.object(update.subprocess, "Popen", return_value=child), self.assertRaises(KeyboardInterrupt):
            update.run("git", "commit")
        self.assertEqual(child.mock_calls, [call.communicate(), call.wait(timeout=update.INTERRUPT_GRACE),
                                            call.kill(), call.wait()])
