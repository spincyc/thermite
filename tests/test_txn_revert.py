"""thermite revert: the version the last install or upgrade set aside, with
its state, as its own install transaction (Q4).

A fictional site and application store in temporary directories; the
transaction itself is tests/test_txn_changeover.py's and the engine's
reinstate is tests/test_txn_engine.py's.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_application_update  # noqa: E402
import oh_paths  # noqa: E402
import oh_release  # noqa: E402
from oh_cli import revert as revert_cli  # noqa: E402
from oh_install import changeover, engine, manifest, runner, steps  # noqa: E402
from oh_install.model import Check  # noqa: E402
from oh_install.system import System, Unit  # noqa: E402
from tests import txn_fakes  # noqa: E402
from tests.test_txn_changeover import Host  # noqa: E402

BEFORE, AFTER = "1" * 40, "2" * 40
OLD_APP, NEW_APP = "a" * 64, "b" * 64


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.site = self.temporary / "site"
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}},
            {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"}, AFTER, "c" * 64, "fixture-host",
            "192.0.2.10", application={"version": "1.0", "digest": NEW_APP})
        self.record = {"version": 1, "id": txn_fakes.ID, "kind": "upgrade", "verb": "upgrade", "phase": "committed",
                       "site": str(self.site), "root": "/srv/thermite-fixture", "installed": True, "bridged": False,
                       "head_before": BEFORE, "head_after": AFTER,
                       "application": {"current": NEW_APP, "previous": OLD_APP}, "asides": {}}
        directory = self.site / "var/transaction" / txn_fakes.ID
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(json.dumps(self.record))
        subprocess.run(["git", "init", "-q", str(self.site)], check=True)
        self.installed = {"installed": True, "kept": txn_fakes.ID, "transaction": txn_fakes.ID}
        self.environment = self.temporary / "home/.local/share/thermite/application/environment-old"
        self.system = Mock(spec=System)
        scope = ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(patch.object(revert_cli, "AREA", self.site))
        scope.enter_context(patch.object(steps, "committed_context", return_value=self.context))
        self.manifest = scope.enter_context(patch.object(manifest, "installed", return_value=self.installed))
        self.found = scope.enter_context(patch.object(revert_cli, "environment", return_value=self.environment))

    def save(self, **changes):
        self.record.update(changes)
        (self.site / "var/transaction" / txn_fakes.ID / "record.json").write_text(json.dumps(self.record))

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)


class Refusals(Fixture):
    def problem(self):
        return revert_cli.refusal(self.context, self.system)[0]

    def test_the_kept_upgrade_of_a_transaction_installed_version_can_be_reverted(self):
        self.assertEqual(revert_cli.refusal(self.context, self.system), (None, self.record))

    def test_nothing_to_revert_to(self):
        for changes, words in (({"kind": "uninstall"}, "is an uninstall (committed)"),
                               ({"kind": "clean"}, "is a clean"),
                               ({"kind": "revert"}, "is a revert"),
                               ({"phase": "rolled-back"}, "(rolled-back)"),
                               ({"bridged": True}, "was not installed by a transaction"),
                               ({"installed": False}, "was not installed by a transaction")):
            with self.subTest(changes=changes):
                original = dict(self.record)
                self.save(**changes)
                self.assertIn(words, self.problem())
                self.save(**original)
        self.manifest.return_value = None
        self.assertIn("not installed here", self.problem())
        self.manifest.return_value = {**self.installed, "kept": txn_fakes.OLD}
        self.assertIn("record of the transaction to revert, is missing", self.problem())

    def test_the_site_must_be_at_the_upgrade_commit_and_the_old_environment_kept(self):
        self.context = steps.Context(**{**self.context.__dict__, "head": "3" * 40})
        self.assertIn(f"the site is at {'3' * 12}, not {AFTER[:12]}", self.problem())
        self.context = steps.Context(**{**self.context.__dict__, "head": AFTER})
        self.found.return_value = None
        self.assertIn(f"the previous application, {OLD_APP[:12]}, has no prepared environment", self.problem())

    def test_a_refusal_changes_nothing(self):
        self.found.return_value = None
        with patch.object(changeover, "resolve", return_value=None), patch.object(changeover, "run") as run:
            self.assertEqual(revert_cli.revert(self.system, False), 2)
        run.assert_not_called()
        self.assertIn("Nothing was changed", self.said())


class Revert(Fixture):
    def test_one_revert_transaction_after_a_plain_warning_and_typing_the_root(self):
        with patch.object(changeover, "resolve", return_value=None), \
                patch.object(runner, "confirm_run", return_value=True), \
                patch.object(changeover, "run", return_value=0) as run:
            self.assertEqual(revert_cli.revert(self.system, False), 0)
        self.assertIn("is lost", self.said())
        (context, _, build), options = run.call_args
        self.assertEqual(options, {"installed": self.installed, "bridged": False, "before": AFTER, "after": BEFORE,
                                   "first": 1})
        work = build("20261106T090000Z-00e5f6")
        self.assertEqual((work.kind, work.verb, work.release, work.pull, work.reinstate), ("revert", "revert", None,
                                                                                            False, True))
        self.assertEqual(work.items(None), [])
        self.assertEqual(work.confirm.path, Path("/srv/thermite-fixture"))
        self.assertEqual([type(step) for step in work.switching], [revert_cli.PutBack, revert_cli.SitePin])
        self.assertEqual((work.switching[1].before, work.switching[1].after), (BEFORE, AFTER))
        health = work.verification[-1].actions(None)[0]
        self.assertEqual(health.argv[0], str(self.environment / "bin/thermite"), "the previous version's own health")
        self.assertEqual(health.argv[-2:], ("--revision", BEFORE))
        self.assertTrue(health.show_output)
        self.assertEqual(work.then, {"select": str(self.environment / "bin/thermite")},
                         "its commit selects the previous owner command, whichever run finishes it")

    def test_an_open_transaction_is_resolved_first(self):
        with patch.object(changeover, "resolve", return_value=changeover.COMMITTED), \
                patch.object(changeover, "run") as run:
            self.assertEqual(revert_cli.revert(self.system, False), changeover.REFUSED)
        run.assert_not_called()

    def test_the_operator_files_go_back_as_the_reverted_upgrade_set_them_aside(self):
        token = self.site / "var/token"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("token the revert drops\n")
        aside = self.site / "var/transaction" / txn_fakes.ID / "operator/0"
        aside.parent.mkdir(parents=True)
        aside.write_text("token as it was\n")
        self.save(asides={str(token): "0", str(self.site / "var/gateway/current.json"): None})
        (self.site / "var/gateway").mkdir()
        (self.site / "var/gateway/current.json").write_text("{}")
        step = revert_cli.PutBack(changeover.Transaction.load(self.site / "var", txn_fakes.ID))
        self.assertEqual(runner.run([step], self.system, self.site, root_confirmed=True), 0)
        self.assertEqual(token.read_text(), "token as it was\n")
        self.assertFalse((self.site / "var/gateway/current.json").exists(), "it did not exist then")


class Reverting(Host):
    """The engine's commands as the script answers them; the previous
    version's health command is run as that version's launcher would run
    it, on the site as it is then."""

    def __init__(self, test, *, healthy=0, **answers):
        super().__init__(test, **answers)
        self.answers["reinstate"] = 0
        self.healthy = healthy
        self.started = False
        self.system.unit.side_effect = lambda name: Unit("active" if self.started else "inactive", "disabled", None)

    def run(self, argv, cwd):
        if tuple(argv) == ("sudo", "systemctl", "start", "thermite.target"):
            self.started = True
        if argv[0] == str(self.test.command):
            self.log.append("previous health")
            self.test.reexecuted.extend(self.test.launch(argv[1:]))
            if isinstance(self.healthy, BaseException):
                raise self.healthy
            return self.healthy
        return super().run(argv, cwd)


class Transacted(unittest.TestCase):
    """A revert as one transaction, on a site in Git whose last commit pins
    the new release: the site goes back inside it, so that the previous
    version's health is its own; a rollback puts the pin commit back; the
    commit's owner command switch happens on every path to the commit."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.site = self.temporary / "site"
        self.site.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture Owner")
        self.git("config", "user.email", "owner@example.test")
        (self.site / ".gitignore").write_text("/var/\n")
        self.pin(OLD_APP, "Site")
        self.before = self.git("rev-parse", "HEAD")
        self.pin(NEW_APP, "Upgrade Thermite application")
        self.after = self.git("rev-parse", "HEAD")
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}},
            {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"}, self.after, "c" * 64, "fixture-host",
            "192.0.2.10", application={"version": "1.0", "digest": NEW_APP})
        kept = self.site / "var/transaction" / txn_fakes.OLD
        kept.mkdir(parents=True)
        (kept / "record.json").write_text(json.dumps({
            "version": 1, "id": txn_fakes.OLD, "kind": "upgrade", "verb": "upgrade", "phase": "committed",
            "site": str(self.site), "root": "/srv/thermite-fixture", "installed": True, "bridged": False,
            "head_before": self.before, "head_after": self.after,
            "application": {"current": NEW_APP, "previous": OLD_APP}, "asides": {}}))
        self.environment = self.temporary / "home/.local/share/thermite/application/environment-old"
        self.command = self.environment / "bin/thermite"
        (self.environment / "runtime").mkdir(parents=True)
        (self.environment / "runtime" / oh_paths.RELEASE_FILE).write_text("{}")
        # The new release, as the site's cache keeps it for a re-execution.
        self.cached = self.site / oh_paths.RELEASES / NEW_APP
        self.cached.mkdir(parents=True)
        self.log, self.reexecuted, self.selected = [], [], []
        installed = {"installed": True, "kept": txn_fakes.OLD, "transaction": txn_fakes.OLD,
                     "application": {"current": NEW_APP, "previous": OLD_APP}}
        scope = ExitStack()
        self.addCleanup(scope.close)
        for target, name, value in (
                (revert_cli, "AREA", self.site), (steps, "committed_context", Mock(return_value=self.context)),
                (manifest, "installed", Mock(return_value=installed)),
                (manifest, "transaction_id", Mock(return_value=txn_fakes.ID)),
                (manifest, "tools", Mock(return_value=txn_fakes.TOOLS)),
                (revert_cli, "environment", Mock(return_value=self.environment)),
                (runner, "confirm_run", Mock(return_value=True)),
                (changeover, "health", Mock(return_value=True)),
                (oh_application_update, "select", Mock(side_effect=self.select)),
                (steps.Account, "check", Mock(return_value=Check(True, "fixture"))),
                (steps.Halt, "check", Mock(return_value=Check(True, "fixture")))):
            scope.enter_context(patch.object(target, name, value))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.site), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def pin(self, release, subject):
        (self.site / oh_paths.SITE_FILE).write_text(json.dumps(
            {"version": 2, "application": oh_release.VERSION, "release": release}) + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)

    def select(self, home, command):
        self.selected.append(command)
        return home / ".local/bin/thermite"

    def launch(self, argv):
        """What the previous environment's bin/thermite does first with the
        site as it is now: the programs it re-executes into, if any."""
        execs = []

        def execv(program, args):
            execs.append(args[1])
            raise InterruptedError("re-executed")
        with patch.object(oh_paths, "PROGRAM", self.environment / "runtime"), \
                patch.object(oh_paths, "SITE", oh_paths.SITE), patch.object(oh_paths, "LAUNCHED", list(argv)), \
                patch.dict(os.environ, {}), \
                patch.object(oh_release, "manifest", return_value={"digest": OLD_APP}), \
                patch.object(oh_release, "resolve", return_value=self.cached), \
                patch.object(os, "execv", side_effect=execv), redirect_stderr(io.StringIO()):
            os.environ.pop(oh_paths.REEXEC, None)
            oh_paths.select_args(list(argv))
            try:
                oh_paths.require_compatible()
            except InterruptedError:
                pass
            except oh_paths.SiteError as exc:
                execs.append(f"refused: {exc}")
        return execs

    @property
    def directory(self):
        return self.site / "var/transaction" / txn_fakes.ID

    def revert(self, typed="/srv/thermite-fixture", **answers):
        self.host = Reverting(self, **answers)
        self.host.system.ask.side_effect = typed if callable(typed) else lambda prompt: typed
        with patch.object(Path, "home", return_value=self.temporary / "home"):
            return revert_cli.revert(self.host.system, False)

    def record(self):
        return json.loads((self.directory / "record.json").read_text())

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.host.system.say.call_args_list)

    def edit(self):
        """The owner's own uncommitted edit, beside the pin's files."""
        (self.site / ".gitignore").write_text("/var/\n/notes/\n")

    def test_a_site_with_uncommitted_changes_is_refused_before_anything_stops(self):
        self.edit()
        self.assertEqual(self.revert(), 2)
        self.assertEqual(self.log, [], "nothing ran, not even the engine's plan")
        self.assertIn("Nothing was changed: the site's Git repository has uncommitted changes", self.said())
        self.assertNotIn("does not pin", self.said())
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)
        self.assertEqual((self.site / ".gitignore").read_text(), "/var/\n/notes/\n")

    def test_changes_made_while_it_asks_are_never_said_to_unpin_the_site(self):
        # They came after the refusal checked: a wrong root typed refuses
        # before begin, with the site where it was.
        def typed(answer):
            def ask(prompt):
                self.edit()
                return answer
            return ask
        self.assertEqual(self.revert(typed("/srv/elsewhere")), changeover.REFUSED)
        self.assertNotIn("does not pin", self.said())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)
        # The right root: the site cannot go back with them, so the revert
        # rolls back, and the site already pins the version that runs.
        self.git("checkout", "--", ".gitignore")
        shutil.rmtree(self.site / "var/transaction" / txn_fakes.ID)
        self.assertEqual(self.revert(typed("/srv/thermite-fixture")), changeover.ROLLED_BACK)
        self.assertIn("The site cannot go back", self.said())
        self.assertNotIn("does not pin", self.said())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)
        self.assertEqual((self.site / ".gitignore").read_text(), "/var/\n/notes/\n")
        self.assertEqual(self.record()["phase"], "rolled-back")

    def test_the_previous_version_checks_its_health_with_its_own_application(self):
        self.assertEqual(self.revert(), changeover.COMMITTED)
        self.assertIn("previous health", self.log)
        self.assertEqual(self.reexecuted, [], "nothing re-executes the site's cached new release")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.selected, [self.command], "the previous owner command, once")
        self.assertEqual(self.record()["phase"], "committed")
        self.assertLess(self.log.index("reinstate"), self.log.index("previous health"))

    def test_the_site_pinning_the_new_release_would_have_re_executed_it(self):
        # What the health command found before the site went back inside
        # the transaction.
        self.assertEqual(self.launch(("--site", str(self.site), "health")), [str(self.cached / "bin/thermite")])

    def test_a_revert_that_rolls_back_puts_the_pin_commit_back(self):
        self.assertEqual(self.revert(healthy=1), changeover.ROLLED_BACK)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)
        self.assertEqual(json.loads((self.site / oh_paths.SITE_FILE).read_text())["release"], NEW_APP)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.selected, [])
        self.assertEqual(self.record()["phase"], "rolled-back")

    def test_a_commit_the_rollback_finishes_still_selects_the_previous_command(self):
        # The engine's commit reached its commit point and then failed (a
        # systemctl enable, say): its rollback verb finishes the commit.
        self.assertEqual(self.revert(commit=1, rollback=0, phase="kept"), changeover.COMMITTED)
        self.assertEqual(self.log[-3:], ["rollback", "sudo -v", "status"])
        self.assertEqual(self.selected, [self.command])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)

    def test_a_commit_a_later_run_resolves_still_selects_the_previous_command(self):
        self.assertEqual(self.revert(healthy=KeyboardInterrupt()), changeover.INTERRUPTED)
        self.assertEqual(changeover.open_transaction(self.site / "var"), txn_fakes.ID)
        self.assertEqual(self.selected, [])
        # The engine committed it before the run was gone; the next revert
        # finishes it, then stops.
        self.assertEqual(self.revert(phase="committed", rollback=0), changeover.REFUSED)
        self.assertEqual(self.selected, [self.command])
        self.assertEqual(self.record()["phase"], "committed")
        self.assertIsNone(changeover.open_transaction(self.site / "var"))


class Environments(unittest.TestCase):
    def test_the_prepared_environment_of_a_release_is_found_by_its_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = home / ".local/share/thermite/application"
            for name, digest in (("environment-old", OLD_APP), ("environment-new", NEW_APP)):
                (store / name / "bin").mkdir(parents=True)
                (store / name / "bin/thermite").write_text("#!/bin/sh\n")
                (store / f"release-{digest}.json").write_text(json.dumps(
                    {"environment": name, "release": {"digest": digest}}))
            self.assertEqual(revert_cli.environment(OLD_APP, home), store / "environment-old")
            self.assertIsNone(revert_cli.environment("f" * 64, home))
            (store / "environment-old/bin/thermite").unlink()
            self.assertIsNone(revert_cli.environment(OLD_APP, home), "an environment without its command is gone")

    def test_selecting_a_command_replaces_the_launcher_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            command = home / ".local/share/thermite/application/environment-old/bin/thermite"
            command.parent.mkdir(parents=True)
            command.write_text("#!/bin/sh\n")
            launcher = oh_application_update.select(home, command)
            self.assertEqual(os.readlink(launcher), str(command))
            self.assertEqual(oh_application_update.select(home, command), launcher)
            self.assertEqual([path.name for path in launcher.parent.iterdir()], ["thermite"])


if __name__ == "__main__":
    unittest.main()
