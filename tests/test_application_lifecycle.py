"""Application lifecycle regressions with real child processes and fixture Git
repositories. Nothing here runs a host phase, sudo or the network."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_source_upgrade as fixtures

AREA = fixtures.AREA
import oh_application_update as application  # noqa: E402
import oh_cli  # noqa: E402
import oh_package  # noqa: E402
import oh_release  # noqa: E402
import oh_source_upgrade as transaction  # noqa: E402
from oh_cli import install as install_cli, upgrade as upgrade_cli  # noqa: E402
from oh_install import ownership, steps  # noqa: E402

#: The release the owner runs now; its launcher drives the next upgrade.
INSTALLED = "feac59d"


def has_commit(commit: str) -> bool:
    return subprocess.run(["git", "-C", str(AREA), "cat-file", "-e", commit + "^{commit}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def write_tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if name.startswith("bin/"):
            path.chmod(0o755)
    return root


def working_runtime(root: Path, extra: bytes = b"") -> tuple[Path, dict]:
    """This checkout's exported files as an installed release, optionally changed."""
    paths = json.loads((AREA / ".thermite-export.json").read_text())["paths"]
    files = {name: (AREA / name).read_bytes() for name in paths}
    files["README.md"] += extra
    write_tree(root, oh_package.runtime_files(files))
    return root, oh_release.manifest(root)


def wait_for(condition, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the fixture child")
        time.sleep(0.02)


CHILD = r"""
import pathlib, sys, time
ready, release, marker = map(pathlib.Path, sys.argv[1:4])
try:
    ready.write_text("waiting\n")
    time.sleep(60)
except KeyboardInterrupt:
    ready.write_text("cleaning\n")
    deadline = time.monotonic() + 30
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    marker.write_text("child cleanup completed\n")
    sys.exit(130)
"""

DRIVER = r"""
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import oh_source_upgrade as transaction
store, child, ready, release, marker, report = sys.argv[2:]
with transaction.locked(pathlib.Path(store)):
    try:
        transaction.foreground(sys.executable, "-B", child, ready, release, marker)
    except KeyboardInterrupt:
        pathlib.Path(report).write_text(json.dumps({"interrupted": True, "cleanup": pathlib.Path(marker).is_file()}))
        raise SystemExit(130)
pathlib.Path(report).write_text(json.dumps({"interrupted": False}))
"""


class Interrupts(fixtures.ApplicationFixture):
    def test_ctrl_c_waits_for_host_phase_cleanup_while_holding_the_lock(self):
        child, driver = self.root / "child.py", self.root / "driver.py"
        child.write_text(CHILD)
        driver.write_text(DRIVER)
        ready, release, marker, report = (self.root / name for name in ("ready", "release", "marker", "report"))
        store = self.root / "store"
        process = subprocess.Popen([sys.executable, "-B", str(driver), str(AREA / "lib"), str(store), str(child),
                                    str(ready), str(release), str(marker), str(report)], start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        wait_for(lambda: ready.is_file() and ready.read_text() == "waiting\n")
        os.killpg(process.pid, signal.SIGINT)  # what a terminal's Ctrl-C sends the foreground group
        wait_for(lambda: ready.read_text() == "cleaning\n")
        time.sleep(0.6)  # subprocess.run would have killed the child 0.25 s into its cleanup
        self.assertIsNone(process.poll())
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(ValueError, "another Thermite"), transaction.locked(store):
            pass
        release.write_text("go\n")
        self.assertEqual(process.wait(timeout=30), 130)
        self.assertEqual(marker.read_text(), "child cleanup completed\n")
        self.assertEqual(json.loads(report.read_text()), {"interrupted": True, "cleanup": True})
        with transaction.locked(store):
            pass

    def test_foreground_status_and_interrupt_handler_are_restored(self):
        before = signal.getsignal(signal.SIGINT)
        self.assertEqual(transaction.foreground(sys.executable, "-c", "raise SystemExit(7)"), 7)
        self.assertIs(signal.getsignal(signal.SIGINT), before)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            application.host(sys.executable, "-c", "raise SystemExit(5)")
        self.assertEqual(caught.exception.returncode, 5)
        self.assertIs(signal.getsignal(signal.SIGINT), before)


class PinnedRelease(fixtures.ApplicationFixture):
    """After a failed host phase the site pins the candidate; the old owner
    command must run that exact cached release instead of refusing."""

    def setUp(self):
        super().setUp()
        self.installed, self.installed_release = working_runtime(self.root / "installed")
        self.pinned = self.root / "pinned"
        script = "import json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'marker': os.environ.get('THERMITE_PINNED_RELEASE')}))\n"
        files = {"bin/thermite": script.encode()}
        write_tree(self.pinned, files)
        release = oh_release.create(files)
        (self.pinned / oh_release.FILE).write_bytes(oh_release.encoded(release))
        self.digest = release["digest"]
        self.cached = oh_release.cache(self.pinned, self.site / "var/application-releases")
        (self.site / "thermite-site.json").write_text(json.dumps(
            {"version": 2, "application": release["application"], "release": self.digest}))

    def launch(self, launcher: Path, *args: str, **environment: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(self.home), **environment}
        env.pop("THERMITE_SITE", None)
        return subprocess.run([sys.executable, "-B", str(launcher), "validate", "--config", str(self.site), *args],
                              capture_output=True, text=True, timeout=60, env=env)

    def test_old_owner_command_runs_the_verified_pinned_release(self):
        result = self.launch(self.installed / "bin/thermite")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"argv": ["validate", "--config", str(self.site)],
                                                     "marker": self.digest})
        self.assertIn(f"running its verified copy {self.cached}", result.stderr)

    def test_damaged_or_looping_pin_names_the_candidate_and_resume_command(self):
        store = self.home / ".local/share/thermite/application"
        (store / "environment-fixture/bin").mkdir(parents=True)
        (store / "environment-fixture/bin/thermite").write_text("prepared candidate\n")
        (store / "git-fixture.json").write_text(json.dumps(
            {"environment": "environment-fixture", "release": {"digest": self.digest}}))
        looping = self.launch(self.installed / "bin/thermite", THERMITE_PINNED_RELEASE=self.digest)
        self.assertEqual(looping.returncode, 2)
        self.assertIn(f"Its verified copy is {self.cached}", looping.stderr)
        self.assertIn(f"Its prepared launcher is {store / 'environment-fixture/bin/thermite'}", looping.stderr)
        (self.cached / "bin/thermite").chmod(0o644)
        (self.cached / "bin/thermite").write_text("tampered\n")
        damaged = self.launch(self.installed / "bin/thermite")
        self.assertEqual(damaged.returncode, 2)
        self.assertNotIn("tampered", damaged.stdout)
        for text in (f"release {self.digest[:12]}", f"runs release {self.installed_release['digest'][:12]}",
                     f"Its verified copy {self.cached} is unusable", "thermite upgrade --config"):
            self.assertIn(text, damaged.stderr)

    def test_source_checkout_names_the_pinned_copy_without_running_it(self):
        result = self.launch(AREA / "bin/thermite")
        self.assertEqual(result.returncode, 2)
        self.assertIn("runs a source checkout", result.stderr)
        self.assertIn(f"Its verified copy is {self.cached}", result.stderr)


class Lifecycle(fixtures.ApplicationFixture):
    def apply(self, operation="upgrade", **kwargs):
        self.previous_launcher = self.launcher.resolve()
        self.said = []
        options = {"home": self.home, "approve": lambda: True, "say": self.said.append, **kwargs}
        with self.simulated():
            return application.apply(operation, self.site, **options)

    def environments(self):
        return sorted((self.home / ".local/share/thermite/application").glob("environment-*"))

    def record(self):
        return next((self.home / ".local/share/thermite/application").glob("git-*.json"))

    def recorded(self):
        record = self.record()
        return record.parent / json.loads(record.read_text())["environment"]

    def test_successful_run_warns_when_path_selects_another_thermite(self):
        for found, warned in ((lambda: str(self.launcher), False), (lambda: "/usr/bin/thermite", True),
                              (lambda: None, True)):
            with self.subTest(warned=warned), patch.object(application.shutil, "which", side_effect=lambda _: found()):
                self.assertEqual(self.apply(source=self.source), 0)
                warning = [line for line in self.said if line.startswith("Warning: `thermite` on PATH")]
                self.assertEqual(bool(warning), warned)
                if warned:
                    self.assertIn(str(self.launcher.parent), warning[0])

    def test_resume_names_the_fetched_commit_and_forwarded_options(self):
        transaction.git(self.source, "branch", "-m", "main")
        commit = transaction.clean(self.source)
        self.fail = "upgrade"
        with patch.object(application, "OFFICIAL_SOURCE", str(self.source)), \
                self.assertRaises(subprocess.CalledProcessError):
            self.apply(host_args=("--native-timeout", "1200"))
        child = next(args for args in self.calls if args[1:2] == ("upgrade",))
        self.assertEqual(child[-2:], ("--native-timeout", "1200"))
        resume = next(line for line in self.said if line.startswith("Resume: "))
        self.assertEqual(resume.split()[-4:], ["--revision", commit, "--native-timeout", "1200"])

    def test_old_launcher_cannot_silently_repin_an_older_release(self):
        old = oh_release.manifest(self.old)["digest"]
        self.assertEqual(self.apply(source=self.source), 0)
        upgraded = transaction.clean(self.repo)
        approve = Mock(side_effect=AssertionError("refusal must come before approval"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(application.Downgrade, old[:12]) as refused:
                self.apply("install", program=self.old, approve=approve, dry_run=dry_run)
            self.assertIn("--allow-downgrade", str(refused.exception))
        approve.assert_not_called()
        self.assertEqual(transaction.clean(self.repo), upgraded)
        self.packaged = self.old
        self.assertEqual(self.apply("install", program=self.old, allow_downgrade=True), 0)
        self.assertEqual(json.loads((self.site / "thermite-site.json").read_text())["release"], old)

    def test_release_order_uses_source_ancestry_then_site_pin_history(self):
        store = self.home / ".local/share/thermite/application"
        store.mkdir(parents=True)
        first = transaction.clean(self.source)
        (self.source / "README").write_text("later reviewed application\n")
        transaction.git(self.source, "commit", "-am", "Later fixture")
        later = transaction.clean(self.source)
        pinned = json.loads((self.site / "thermite-site.json").read_text())["release"]
        (store / f"git-{later}.json").write_text(json.dumps({"environment": "environment-x",
                                                            "release": {"digest": pinned}}))
        older = application.older(store, self.repo, self.site, "e" * 64, first, [self.source])
        self.assertIn(f"{first[:12]} precedes", older)
        self.assertIsNone(application.older(store, self.repo, self.site, pinned, first, [self.source]))
        (store / f"git-{later}.json").unlink()
        (store / f"git-{first}.json").write_text(json.dumps({"environment": "environment-x",
                                                            "release": {"digest": pinned}}))
        self.assertIsNone(application.older(store, self.repo, self.site, "e" * 64, later, [self.source]))
        # Without source ancestry, a release pinned again after a deliberate
        # revert is newer than the one it replaced, and an unknown one is allowed.
        descriptor = self.site / "thermite-site.json"
        original = json.loads(descriptor.read_text())
        for digest in ("f" * 64, original["release"]):
            descriptor.write_text(json.dumps({**original, "release": digest}))
            transaction.git(self.repo, "commit", "-qam", f"Pin {digest[:12]}")
        self.assertEqual(application.pin_order(self.repo, self.site), [original["release"], "f" * 64])
        self.assertIsNone(application.older(store, self.repo, self.site, "f" * 64, None))
        self.assertIsNone(application.older(store, self.repo, self.site, "d" * 64, None))
        descriptor.write_text(json.dumps({**original, "release": "f" * 64}))
        transaction.git(self.repo, "commit", "-qam", "Pin again")
        self.assertIn("pinned it before", application.older(store, self.repo, self.site, original["release"], None))

    def test_retained_environment_is_rebuilt_for_another_python_and_kept(self):
        self.assertEqual(self.apply(source=self.source), 0)
        [first] = self.environments()
        record = self.record()
        saved = json.loads(record.read_text())
        self.assertEqual((saved["python"], saved["pyvenv"]), (application.PYTHON, application.PYTHON + ".0"))
        cases = (({**saved, "python": "3.0"}, None, "built for Python 3.0"),
                 ({key: value for key, value in saved.items() if key not in ("python", "pyvenv")}, None,
                  "can no longer start"),
                 ({key: value for key, value in saved.items() if key not in ("python", "pyvenv")}, "3.0.1",
                  "built for Python 3.0 and its interpreter is now"))
        for changed, pyvenv, reason in cases:
            with self.subTest(reason=reason):
                current = self.recorded()
                record.write_text(json.dumps({**changed, "environment": current.name}))
                if pyvenv:
                    (current / "pyvenv.cfg").write_text(f"version = {pyvenv}\n")
                self.python = None if reason == "can no longer start" else application.PYTHON
                self.calls.clear()
                self.assertEqual(self.apply(source=self.source), 0)
                self.assertTrue(any(reason in line and str(current) in line for line in self.said), self.said)
                self.assertTrue(any("--no-deps" in args for args in self.calls))
                self.assertTrue(current.is_dir(), "the previous environment is retained")
                self.assertNotEqual(self.recorded(), current)
                self.assertEqual(self.launcher.resolve(), self.recorded().resolve() / "bin/thermite")
        self.assertIn(first, self.environments())
        legacy = {key: value for key, value in json.loads(record.read_text()).items() if key not in ("python", "pyvenv")}
        record.write_text(json.dumps(legacy))
        self.python = application.PYTHON
        self.calls.clear()
        self.assertEqual(self.apply(source=self.source), 0)
        self.assertFalse(any("pip" in args for args in self.calls), "a healthy older record is reused")

    def test_application_dependency_lock_comes_from_the_selected_source_first(self):
        lock = self.source / application.REQUIREMENTS
        lock.parent.mkdir()
        lock.write_text("# fixture lock\n")
        transaction.git(self.source, "add", application.REQUIREMENTS)
        transaction.git(self.source, "commit", "-m", "Lock fixture")
        commit = transaction.clean(self.source)
        self.assertEqual(self.apply(source=self.source), 0)
        locked = next(args for args in self.calls if "--require-hashes" in args)
        self.assertEqual(Path(locked[-1]), self.home / ".local/share/thermite/application/sources" / commit
                         / application.REQUIREMENTS)
        with patch.object(application, "REQUIREMENTS", "lib/absent-lock.txt"), \
                self.assertRaisesRegex(ValueError, "hashed dependency lock"):
            application.requirements(self.source)

    def test_dry_run_shows_the_pinned_release_host_plan_only(self):
        with patch.object(transaction, "run", side_effect=self.simulate), \
                patch.object(transaction, "foreground", return_value=3) as child:
            self.said = []
            self.assertEqual(application.apply("install", self.site, program=self.old, dry_run=True, home=self.home,
                                               approve=Mock(side_effect=AssertionError), say=self.said.append,
                                               host_args=("--native-timeout", "600")), 3)
            child.assert_called_once_with(sys.executable, self.old / "bin/thermite", "install", "--config", self.site,
                                          "--_host-phase", "--dry-run", "--native-timeout", "600")
            self.assertTrue(any(line.startswith("Provisional host plan from the current release") for line in self.said))
            child.reset_mock()
            self.said.clear()
            self.assertEqual(application.apply("upgrade", self.site, program=self.new, dry_run=True, home=self.home,
                                               approve=Mock(side_effect=AssertionError), say=self.said.append), 0)
            child.assert_not_called()
            self.assertTrue(any(line.startswith("No host plan is shown") for line in self.said))
        self.assertFalse((self.home / ".local/share").exists())

    def test_failed_upgrade_recovery_returns_the_pin_and_runs_the_previous_cached_release(self):
        previous = json.loads((self.site / "thermite-site.json").read_text())["release"]
        current = oh_release.manifest(self.new)
        (self.site / "thermite-site.json").write_text(json.dumps(
            {"version": 2, "application": current["application"], "release": current["digest"]}))
        transaction.git(self.repo, "commit", "-qam", "Upgrade Thermite application")
        pin = transaction.clean(self.repo)
        said = []
        system = Mock()
        system.say.side_effect = said.append
        backup = Mock()
        backup.completed.return_value = False
        context = SimpleNamespace(area=self.site, application={"digest": current["digest"]}, var=self.site / "var")
        upgrade_cli.going_back(system, context, backup, self.before, "fixture")
        text = "\n".join(said)
        site = shlex.quote(str(self.site))
        self.assertIn(f"git -C {site} revert --no-edit {pin[:12]}", text)
        self.assertIn(f"python3 {shlex.quote(str(self.site / 'var/application-releases' / previous / 'bin/thermite'))} "
                      f"install --config {site}", text)
        self.assertNotIn("git switch", text)
        self.assertIn("docs/installation.md#update-or-remove", text)
        said.clear()
        upgrade_cli.going_back(system, context, backup, pin, "fixture")
        self.assertIn(f"revert --no-edit {pin[:12]}..HEAD", "\n".join(said))


class Commands(unittest.TestCase):
    def test_native_timeout_and_downgrade_reach_the_outer_and_host_phases(self):
        for module, operation in ((install_cli, "install"), (upgrade_cli, "upgrade")):
            with self.subTest(operation=operation), patch.object(module.os, "geteuid", return_value=1000), \
                    patch.object(application, operation, return_value=0) as outer:
                self.assertEqual(module.main(["--native-timeout", "1200", "--allow-downgrade", "--dry-run"]), 0)
                self.assertTrue(outer.call_args.kwargs["allow_downgrade"])
                host_args = list(outer.call_args.kwargs["host_args"])
                index = host_args.index("--native-timeout")
                self.assertEqual(host_args[index:index + 2], ["--native-timeout", "1200"])
                self.assertNotIn("--allow-downgrade", host_args)
            with self.subTest(operation=operation, phase="host"), patch.object(module.os, "geteuid", return_value=1000), \
                    patch("oh_paths.require_compatible"), patch.object(module, operation, return_value=0) as host:
                self.assertEqual(module.main(["--_host-phase", "--_run-approved", "--native-timeout", "900"]), 0)
                self.assertEqual(host.call_args.kwargs["native_timeout"], 900)
            with self.subTest(operation=operation, value="outside"), patch("sys.stderr", new=io.StringIO()), \
                    self.assertRaises(SystemExit):
                module.main(["--native-timeout", "10", "--dry-run"])

    def test_host_phase_threads_native_timeout_into_its_context(self):
        context = steps.Context(AREA, {"openhab": {"version": "5.2.3"}},
                                {"root": "/srv/fictional", "uid": 9001, "gid": 9001},
                                "a" * 40, "b" * 64, "fixture", None)
        seen = []

        class Seen(Exception):
            pass

        def capture(found, *args):
            seen.append(found.native_timeout)
            raise Seen
        system = Mock()
        system.login.return_value = "fixture"
        with patch.object(steps, "committed_context", return_value=context), \
                patch.object(steps, "install_preflight", side_effect=capture), \
                patch.object(ownership, "inspect", side_effect=capture):
            for call in (lambda: install_cli.install(system, True, native_timeout=1200),
                         lambda: upgrade_cli.upgrade(system, True, "fixture", native_timeout=1500),
                         lambda: install_cli.install(system, True)):
                with self.assertRaises(Seen):
                    call()
        self.assertEqual(seen, [1200, 1500, None])


@unittest.skipUnless(has_commit(INSTALLED), f"needs the installed release commit {INSTALLED}")
class InstalledLauncherUpgrade(fixtures.ApplicationFixture):
    """The owner's next upgrade is driven by the installed release's own
    launcher code: it must accept this release without any new step or flag."""

    DRIVER = r"""
import json, os, shutil, subprocess, sys
from pathlib import Path
old_lib, source, site, home, runtime, bootstrap, packages_path, report = sys.argv[1:9]
sys.path.insert(0, old_lib)
import oh_application_update as application
import oh_source_upgrade as transaction
real, host = transaction.run, []
def run(*argv, capture=False):
    args = [str(arg) for arg in argv]
    if args[1:3] == ["-m", "venv"]:
        environment = Path(args[3])
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True)
        packages = next((environment / "lib").glob("python*/site-packages"))
        (packages / "fixture-dependencies.pth").write_text(packages_path + "\n")
        shutil.copytree(runtime, packages / "thermite_app/runtime")
        shutil.copyfile(bootstrap, packages / "thermite_app/__init__.py")
        script = environment / "bin/thermite"
        script.write_bytes(b"#!" + os.fsencode(environment / "bin/python") + b"\n" + application.CONSOLE_SCRIPTS[1])
        script.chmod(0o755)
        return ""
    if "pip" in args:
        return ""  # the environment above is what pip installs from the fetched source
    if "--_host-phase" in args:
        host.append(args)
        return ""
    return real(*argv, capture=capture)
transaction.run = run
application.OFFICIAL_SOURCE = source
code = application.apply("upgrade", Path(site), home=Path(home), approve=lambda: True)
Path(report).write_text(json.dumps({"code": code, "host": host}))
"""

    def test_installed_release_launcher_upgrades_to_this_release(self):
        import yaml
        root = self.root / "installed-upgrade"
        manifest = json.loads(subprocess.run(["git", "-C", str(AREA), "show", f"{INSTALLED}:.thermite-export.json"],
                                             capture_output=True, check=True).stdout)["paths"]
        archive = subprocess.run(["git", "-C", str(AREA), "archive", "--format=tar", INSTALLED, "--", *manifest],
                                 capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            files = {member.name: tar.extractfile(member).read() for member in tar if member.isfile()}
        installed = write_tree(root / "installed", oh_package.runtime_files(files))
        installed_release = oh_release.manifest(installed)
        runtime, release = working_runtime(root / "runtime")
        bootstrap = root / "bootstrap.py"
        bootstrap.write_text(oh_package.BOOTSTRAP)
        site, home = root / "site", root / "home"
        environment = {**os.environ, "HOME": str(home), "TMPDIR": str(self.root)}
        environment.pop("THERMITE_SITE", None)
        subprocess.run([sys.executable, "-B", str(installed / "bin/thermite"), "init", str(site)],
                       check=True, capture_output=True, env=environment, timeout=120)
        # The owner's edits before this upgrade: the installed release wrote
        # state_root, which this release rejects; the default root replaces it.
        # Its native ports were 13601 and 13602; the HTTPS application has
        # 13601 now, and every other listener moved up one.
        host_file = site / "instance/host.yaml"
        host = yaml.safe_load(host_file.read_text())
        self.assertEqual(host.pop("state_root"), "/srv/thermite")
        self.assertEqual((host["http_port"], host["https_port"]), (13601, 13602))
        host.update(http_port=13602, https_port=13603)
        host_file.write_text(json.dumps(host, indent=2) + "\n")
        self.initialize_git(site)
        official = root / "official"
        official.mkdir()
        (official / "README").write_text("fetched application\n")
        self.initialize_git(official)
        transaction.git(official, "branch", "-m", "main")
        driver, report = root / "driver.py", root / "report.json"
        driver.write_text(self.DRIVER)
        result = subprocess.run([sys.executable, "-B", str(driver), str(installed / "lib"), str(official), str(site),
                                 str(home), str(runtime), str(bootstrap), str(Path(yaml.__file__).parent.parent),
                                 str(report)], capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outcome = json.loads(report.read_text())
        self.assertEqual(outcome["code"], 0)
        [host] = outcome["host"]
        command = Path(host[0])
        self.assertEqual(host[1:], ["upgrade", "--config", str(site), "--_host-phase", "--_run-approved"])
        arguments = oh_cli.parser_of("upgrade").parse_args([*host[2:], "--runtime-token-file", "/fictional/token"])
        self.assertTrue(arguments._host_phase and arguments._run_approved)
        self.assertEqual((arguments.native_timeout, arguments.allow_downgrade), (None, False))
        self.assertEqual(json.loads((site / "thermite-site.json").read_text()),
                         {"version": 2, "application": release["application"], "release": release["digest"]})
        self.assertEqual((home / ".local/bin/thermite").resolve(), command.resolve())
        self.assertEqual(transaction.git(site, "log", "-1", "--format=%s"), "Upgrade Thermite application")
        # This release's own next upgrade adopts the installed launcher's
        # records and pin history: no rebuild and no false downgrade.
        store = home / ".local/share/thermite/application"
        [record] = store.glob("git-*.json")
        key = record.stem
        said = []
        reused, verified = application.candidate(store, store / "sources" / key.removeprefix("git-"), key, said.append)
        self.assertEqual((reused, verified["digest"]), (command.parents[1], release["digest"]))
        self.assertTrue(said[0].startswith("Using verified Thermite"), said)
        self.assertIsNone(application.older(store, site, site, "c" * 64, None))
        self.assertIn("pinned it before",
                      application.older(store, site, site, installed_release["digest"], None))


if __name__ == "__main__":
    unittest.main()
