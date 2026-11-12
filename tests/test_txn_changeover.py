"""thermite upgrade as one install transaction: prepare, begin, switch,
verify and commit, or roll back in the same run; the site pin; reruns.

A fictional site in a temporary directory, an in-memory host, and the
engine's commands answered by a script; nothing runs as root or touches
this host's services.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_source_upgrade  # noqa: E402
import oh_units  # noqa: E402
from oh_install import bridge, changeover, engine, manifest, runner, steps  # noqa: E402
from oh_install.model import Check, Command, Step, Task  # noqa: E402
from oh_install.system import Stat, System, Unit, UnitOrigin  # noqa: E402
from tests import txn_fakes  # noqa: E402

BEFORE, AFTER = "1" * 40, "2" * 40
IMAGE = "registry.example.test/openhab@sha256:" + "e" * 64
#: The installed manifest, as far as the transaction reads it.
INSTALLED = {"installed": True, "transaction": "20261101T000000Z-aaaaaa", "application": {"current": "a" * 64}}
SPACE = {"required": 300 * 1024 * 1024, "available": 4 * 1024 ** 3, "margin": engine.MARGIN, "extra": 0,
         "items": [["runtime/userdata", 40 * 1024 * 1024]]}


class Fake(Step):
    """A step that records running and answers with `outcome`, or with
    what `outcome(system)` returns."""

    always = True
    stop_on_failure = True

    def __init__(self, name, log, outcome=0):
        self.title = name
        self.name, self.log, self.outcome = name, log, outcome

    def check(self, system):
        return Check(False, "fixture")

    def actions(self, system):
        return [Task(self.name, self.perform)]

    def perform(self, system):
        self.log.append(self.name)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome(system) if callable(self.outcome) else self.outcome


class Host:
    """The operator's view: commands, sudo, and the engine as a script."""

    def __init__(self, test, *, plan=0, begin=0, rollback=3, commit=0, phase="switching", health=0, sudo=()):
        self.test = test
        self.log = test.log
        #: Each sudo -v's exit status, in turn; then 0.
        self.sudo = iter(sudo)
        self.answers = {"begin": begin, "guard": 0, "collect": 0, "commit": commit, "rollback": rollback}
        self.plan, self.phase, self.health = plan, phase, health
        self.system = Mock(spec=System)
        self.system.interactive.return_value = True
        self.system.process_groups.return_value = {9001}
        self.system.unit_file.return_value = None
        self.system.unit_origins.side_effect = lambda names: {name: UnitOrigin() for name in names}
        self.system.unit.side_effect = lambda name: Unit("inactive", "disabled", None)
        self.system.run.side_effect = self.run
        self.system.capture.side_effect = self.capture

    def verb(self, argv):
        """The engine verb of a root command, checked to run the transaction's copy."""
        if argv[:3] != (changeover.PYTHON, "-I", "-S") or not argv[3].endswith("/engine.py"):
            return None
        self.test.assertEqual(argv[3], str(self.test.directory / "engine.py"))
        self.test.assertEqual(argv[5:7], ("--root", str(self.test.context.installation.root)))
        if argv[4] not in ("plan", "status"):
            self.test.assertEqual(argv[7:9], ("--id", txn_fakes.ID))
        return argv[4]

    def run(self, argv, cwd):
        argv = tuple(argv)
        if argv == ("sudo", "-v"):
            self.log.append("sudo -v")
            return next(self.sudo, 0)
        if argv[0] == "sudo" and (verb := self.verb(argv[1:])):
            self.log.append(verb)
            return self.answers[verb]
        if "--_record-rollback" in argv:
            self.log.append("history " + json.loads(argv[-1])["transaction"])
            return 0
        self.log.append(" ".join(argv))
        return 0

    def capture(self, argv, **kwargs):
        argv = tuple(argv)
        if argv[:2] == ("sudo", "-n") and self.verb(argv[2:]) == "plan":
            self.log.append("plan")
            short = dict(SPACE, available=100 * 1024 * 1024)
            return self.plan, json.dumps(short if self.plan == engine.SHORT else SPACE)
        if argv[:2] == ("sudo", "-n") and self.verb(argv[2:]) == "status":
            self.log.append("status")
            found = [{"id": txn_fakes.ID, "phase": self.phase, "open": True}] if self.phase else []
            return 0, json.dumps(found)
        if argv[:5] == ("sudo", "-n", "podman", "image", "exists"):
            return 0, ""
        raise AssertionError(f"unexpected capture {argv}")


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name) / "site"
        (self.site / "var").mkdir(parents=True, mode=0o700)
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}},
            {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"}, AFTER, "c" * 64, "fixture-host",
            "192.0.2.10", application={"version": "1.0", "digest": "d" * 64})
        self.log = []
        self.unpinned = []
        scope = ExitStack()
        self.addCleanup(scope.close)
        for name, value in (("images", [IMAGE]), ("pins", {"openhab": "sha256:" + "e" * 64}),
                            ("tools", txn_fakes.TOOLS), ("transaction_id", txn_fakes.ID), ("installed", None)):
            scope.enter_context(patch.object(manifest, name, return_value=value))
        scope.enter_context(patch.object(changeover.Transaction, "unpin",
                                         lambda transaction, system: self.unpinned.append(transaction.id)))
        scope.enter_context(patch.object(changeover, "health", self.health))

    def health(self, system, transaction):
        """The reinstated version's health, as its owner command answers."""
        self.log.append("old health")
        return self.host.health == 0

    def work(self, prepared, switched, verified, kind="upgrade", verb="upgrade"):
        return changeover.Work(kind, verb, prepared, switched, verified, changeover.release(self.context),
                               items=lambda system: [])

    def transact(self, work, installed=INSTALLED, before=BEFORE, **options):
        return changeover.run(self.context, self.host.system, lambda id: work, installed=installed, bridged=False,
                              before=before, first=3, **options)

    def run_upgrade(self, *, prepare=(), switch=(), verify=(), **answers):
        self.host = Host(self, **answers)
        prepared = [Fake(name, self.log, outcome) for name, outcome in (prepare or [("prepare", 0)])]
        switched = [Fake(name, self.log, outcome) for name, outcome in (switch or [("switch", 0)])]
        verified = [changeover.Authorize(), *(Fake(name, self.log, outcome)
                                               for name, outcome in (verify or [("health", 0)]))]
        return self.transact(self.work(prepared, switched, verified))

    @property
    def directory(self):
        return self.site / "var/transaction" / txn_fakes.ID

    def record(self):
        return json.loads((self.directory / "record.json").read_text())

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.host.system.say.call_args_list)

    def is_open(self):
        return changeover.open_transaction(self.site / "var")


class Committed(Fixture):
    def test_everything_before_begin_runs_while_thermite_runs_then_one_transaction_commits(self):
        self.assertEqual(self.run_upgrade(), changeover.COMMITTED)
        self.assertEqual(self.log, ["prepare", "sudo -v", "sudo -v", "plan", "begin", "guard", "collect", "switch",
                                    "sudo -v", "health", "commit"])
        self.assertIsNone(self.is_open())
        self.assertEqual(self.record()["phase"], "committed")
        desired = json.loads((self.directory / "desired.json").read_text())
        self.assertEqual((desired["kind"], desired["pulled"], desired["tools"]), ("upgrade", [], txn_fakes.TOOLS))
        self.assertFalse((self.directory / "current.json").exists(), "the installed manifest is the current one")
        operator = engine.validate_operator(json.loads((self.directory / "operator.json").read_text()))
        self.assertEqual((operator["head_before"], operator["head_after"]), (BEFORE, AFTER))
        release = engine.validate_release(json.loads((self.directory / "release.json").read_text()))
        self.assertEqual((release["application"], release["images"]), ("d" * 64, [IMAGE]))
        self.assertEqual((self.directory / "engine.py").read_bytes(), Path(engine.__file__).read_bytes(),
                         "root runs the copy the transaction began with")
        self.assertIn("Room to set aside the current version", self.said(), "the copy-size estimate (9-Q15)")
        self.assertEqual(self.unpinned, [])

    def test_a_commit_removes_earlier_finished_records(self):
        older = self.site / "var/transaction" / txn_fakes.OLD
        older.mkdir(parents=True)
        (older / "record.json").write_text(json.dumps({"id": txn_fakes.OLD, "phase": "committed"}))
        self.assertEqual(self.run_upgrade(), changeover.COMMITTED)
        self.assertFalse(older.exists())
        self.assertTrue(self.directory.exists(), "the gateway profile names this one's record")


class Refused(Fixture):
    def test_too_little_room_refuses_before_anything_stops(self):
        self.assertEqual(self.run_upgrade(plan=engine.SHORT), changeover.REFUSED)
        self.assertEqual(self.log[-1], "plan")
        self.assertIsNone(self.is_open())
        self.assertEqual(self.record()["phase"], "refused")
        self.assertEqual(self.unpinned, [txn_fakes.ID], "the site pins the version that runs again")
        self.assertIn("Not enough space", self.said())
        self.assertIn("nothing of Thermite was stopped or changed", self.said())

    def test_a_refused_begin_closes_the_transaction(self):
        self.assertEqual(self.run_upgrade(begin=1), changeover.REFUSED)
        self.assertEqual(self.log[-1], "begin")
        self.assertIsNone(self.is_open())
        self.assertEqual(self.record()["phase"], "refused")

    def test_a_failed_preparation_never_begins(self):
        self.assertEqual(self.run_upgrade(prepare=[("packages", 1)]), changeover.REFUSED)
        self.assertEqual(self.log, ["packages"])

    def exported(self, system):
        """What GatewayInputs leaves: the installed gateway's adoption export."""
        export = self.directory / "gateway/adopted-0a1b2c"
        export.mkdir(parents=True, mode=0o700)
        (export / "tls-key.pem").write_text("fixture key\n")
        return 0

    def test_a_refusal_or_an_interrupt_before_begin_drops_the_gateway_copies(self):
        for outcome, code in ((1, changeover.REFUSED), (KeyboardInterrupt(), changeover.INTERRUPTED)):
            with self.subTest(outcome=outcome):
                shutil.rmtree(self.site / "var/transaction", ignore_errors=True)
                self.assertEqual(self.run_upgrade(prepare=[("gateway inputs", self.exported),
                                                           ("packages", outcome)]), code)
                self.assertFalse((self.directory / "gateway").exists())
                self.assertEqual(self.record()["phase"], "refused", "its record stays")


class RolledBack(Fixture):
    def setUp(self):
        super().setUp()
        self.token = self.site / self.context.token_file
        self.token.parent.mkdir(parents=True)
        self.token.write_text("old token\n")
        self.profile = self.site / "var/gateway/current.json"

    def rewrite(self, system):
        """A switch that rewrites the operator's files, then fails."""
        self.log.append("switch")
        self.token.write_text("new token\n")
        self.profile.parent.mkdir(parents=True, exist_ok=True)
        self.profile.write_text("{}")
        return 1

    def test_a_failed_switch_rolls_back_in_the_same_run(self):
        switch = Fake("switch", self.log)
        switch.perform = self.rewrite
        self.host = Host(self)
        code = self.transact(self.work([], [switch, Fake("never", self.log)], []))
        self.assertEqual(code, changeover.ROLLED_BACK)
        self.assertEqual(self.log[-5:], ["switch", "sudo -v", "rollback", f"history {txn_fakes.ID}", "old health"])
        self.assertEqual(self.token.read_text(), "old token\n", "the operator's files as they were (F3)")
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.unpinned, [txn_fakes.ID])
        self.assertIsNone(self.is_open())
        self.assertEqual(self.record()["phase"], "rolled-back")
        self.assertIn("runs again, healthy", self.said())

    def test_failed_health_rolls_back_and_an_unhealthy_old_version_keeps_running(self):
        self.assertEqual(self.run_upgrade(verify=[("health", 1)], health=1), changeover.UNHEALTHY)
        self.assertNotIn("commit", self.log)
        self.assertEqual(self.log[-3:], ["rollback", f"history {txn_fakes.ID}", "old health"])
        self.assertIn("It is left as it is", self.said())

    def test_a_refused_commit_rolls_back(self):
        self.assertEqual(self.run_upgrade(commit=1), changeover.ROLLED_BACK)
        self.assertEqual(self.log[-5:], ["commit", "sudo -v", "rollback", f"history {txn_fakes.ID}", "old health"])

    def test_an_unexpected_error_in_a_step_rolls_back(self):
        self.assertEqual(self.run_upgrade(switch=[("switch", ValueError("fixture"))]), changeover.ROLLED_BACK)
        self.assertIn("ValueError: fixture", self.said())

    def test_a_stuck_rollback_leaves_the_transaction_open_with_the_manual_procedure(self):
        self.assertEqual(self.run_upgrade(switch=[("switch", 1)], rollback=engine.STUCK), changeover.STUCK)
        self.assertEqual(self.is_open(), txn_fakes.ID)
        self.assertIn(f"sudo /usr/bin/python3 -I -S /srv/thermite-fixture/.transaction/{txn_fakes.ID}/engine.py "
                      f"rollback --root /srv/thermite-fixture --id {txn_fakes.ID} --print", self.said())
        self.assertEqual(self.unpinned, [])
        self.assertNotIn("old health", self.log)
        self.assertIn("Thermite stays stopped", self.said())

    def test_a_rollback_the_engine_did_not_finish_says_thermite_may_be_stopped(self):
        self.assertEqual(self.run_upgrade(switch=[("switch", 1)], rollback=engine.FAILED), changeover.STUCK)
        self.assertEqual(self.is_open(), txn_fakes.ID)
        self.assertIn("Thermite may be stopped", self.said())
        self.assertNotIn("stays stopped", self.said())

    def test_a_rollback_goes_on_in_numbered_stages_that_keep_its_output(self):
        self.assertEqual(self.run_upgrade(switch=[("switch", 1)]), changeover.ROLLED_BACK)
        # prepare 3, images 4, room 5, begin 6, guard 7, stop 8, collect 9, switch 10.
        for label in ("11. Authorize the rollback", f"12. Roll back transaction {txn_fakes.ID}",
                      "13. Record the rollback in thermite history"):
            self.assertIn(f"[ OK ] {label}", self.said())
        self.assertNotIn("next, under sudo", self.said())
        self.assertFalse(changeover.Transaction.load(self.site / "var", txn_fakes.ID).command("commit").terminal,
                         "only sudo's prompt owns the terminal; the engine's output stays in its stage")

    def test_an_unanswered_sudo_prompt_leaves_the_rollback_to_the_next_run(self):
        # The third sudo -v is the rollback's own, long after the run began.
        self.assertEqual(self.run_upgrade(switch=[("switch", 1)], sudo=(0, 0, 1)), changeover.INTERRUPTED)
        self.assertEqual(self.log[-2:], ["switch", "sudo -v"])
        self.assertNotIn("rollback", self.log)
        self.assertEqual(self.is_open(), txn_fakes.ID)
        self.assertIn("sudo did not authenticate, so nothing was rolled back", self.said())
        self.assertNotIn("stays stopped", self.said())
        self.assertEqual(self.unpinned, [])

    def test_an_interrupt_leaves_the_rollback_to_the_next_run(self):
        self.assertEqual(self.run_upgrade(switch=[("switch", KeyboardInterrupt())]), changeover.INTERRUPTED)
        self.assertNotIn("rollback", self.log)
        self.assertEqual(self.is_open(), txn_fakes.ID)
        self.assertIn("to roll it back", self.said())

    def test_an_interrupt_before_begin_changes_nothing(self):
        self.assertEqual(self.run_upgrade(prepare=[("packages", KeyboardInterrupt())]), changeover.INTERRUPTED)
        self.assertIsNone(self.is_open())
        self.assertEqual(self.unpinned, [txn_fakes.ID])


class Rerun(Fixture):
    """9-F8b: the next thermite upgrade resolves an open transaction, then stops."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_upgrade(switch=[("switch", KeyboardInterrupt())]), changeover.INTERRUPTED)
        self.log.clear()

    def resolve(self, **answers):
        self.host = Host(self, **answers)
        return changeover.resolve(self.site, self.host.system)

    def test_an_open_transaction_is_rolled_back_and_the_run_stops(self):
        self.assertEqual(self.resolve(), changeover.ROLLED_BACK)
        self.assertEqual(self.log, ["sudo -v", "status", "sudo -v", "rollback", f"history {txn_fakes.ID}",
                                    "old health"])
        self.assertIsNone(self.is_open())
        self.assertIn("Stopped there, without upgrading", self.said())

    def test_a_committed_transaction_is_finished(self):
        self.assertEqual(self.resolve(phase="committed", rollback=0), changeover.COMMITTED)
        self.assertEqual(self.log, ["sudo -v", "status", "rollback"])
        self.assertEqual(self.record()["phase"], "committed")
        self.assertIsNone(self.is_open())

    def test_a_finished_rollback_is_completed_on_the_operator_side(self):
        self.assertEqual(self.resolve(phase="rolled-back"), changeover.ROLLED_BACK)
        self.assertEqual(self.log, ["sudo -v", "status", f"history {txn_fakes.ID}", "old health"])

    def test_one_that_never_began_is_closed_its_pin_undone_and_the_run_stops(self):
        self.assertEqual(self.resolve(phase=None), changeover.REFUSED)
        self.assertEqual(self.log, ["sudo -v", "status"])
        self.assertEqual(self.unpinned, [txn_fakes.ID])
        self.assertIsNone(self.is_open())
        self.assertEqual(self.record()["phase"], "refused")
        self.assertIn("Stopped there, without upgrading", self.said())
        self.assertIsNone(self.resolve(), "nothing is left to resolve")

    def test_status_names_it(self):
        from oh_cli import status
        report, problems, said = {}, [], []
        with patch.object(status, "AREA", self.site):
            status.show_transaction(self.context.host, report, said.append, problems,
                                    manifest=self.site / "no-installed-manifest")
        self.assertEqual(report["open_transaction"], txn_fakes.ID)
        self.assertEqual(problems, [f"install transaction {txn_fakes.ID} has not finished"])
        self.assertIn("commits or rolls it back, then stops", said[-1])


class Health(unittest.TestCase):
    """9-Q10: the reinstated version's health, through the command it kept."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.site = Path(temporary.name) / "site"
        self.transaction = changeover.Transaction(self.site / "var", txn_fakes.ID,
                                                  {"site": str(self.site), "head_before": BEFORE})
        self.system = Mock(spec=System)
        self.ran = []
        self.system.run.side_effect = lambda argv, cwd: self.ran.append(tuple(argv)) or 0

    def test_the_owner_command_checks_the_revision_that_runs_again(self):
        launcher = self.home / ".local/bin/thermite"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        with patch.object(Path, "home", return_value=self.home):
            self.assertTrue(_health(self.system, self.transaction))
        self.assertEqual(self.ran, [(str(launcher), "--site", str(self.site), "health", "--production",
                                     "--installation", "--settle", "120", "--revision", BEFORE)])

    def test_no_owner_command_is_not_healthy(self):
        with patch.object(Path, "home", return_value=self.home):
            self.assertFalse(_health(self.system, self.transaction))
        self.assertEqual(self.ran, [])


_health = changeover.health


class GitSite(unittest.TestCase):
    """A site whose last commit pins a new release."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture Owner")
        self.git("config", "user.email", "owner@example.test")
        (self.root / "thermite-site.json").write_text('{"release": "old"}\n')
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Site")
        self.before = self.git("rev-parse", "HEAD")
        (self.root / "thermite-site.json").write_text('{"release": "new"}\n')
        (self.root / ".gitignore").write_text("/var/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Upgrade Thermite application")
        self.after = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def locked(self):
        """Another git process holds the index, as an editor's may."""
        lock = self.root / ".git/index.lock"
        lock.write_text("")
        return lock


class SitePin(GitSite):
    """F7: a rolled-back upgrade's pin commit is undone, compare and swap."""

    def test_the_pin_commit_is_undone_with_its_files(self):
        self.assertTrue(oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "old"}\n')
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before),
                         "already undone")

    def test_a_site_that_moved_on_is_left_alone(self):
        (self.root / "notes.txt").write_text("owner's own commit\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Owner")
        moved = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(oh_source_upgrade.PinMoved, "not the pin commit"):
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        self.assertEqual(self.git("rev-parse", "HEAD"), moved)
        self.git("reset", "-q", "--hard", self.after)
        (self.root / "thermite-site.json").write_text("edited\n")
        with self.assertRaisesRegex(oh_source_upgrade.PinMoved, "uncommitted"):
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        self.assertEqual((self.root / "thermite-site.json").read_text(), "edited\n")

    def test_a_reset_whose_files_could_not_follow_is_finished_by_a_rerun(self):
        lock = self.locked()
        with self.assertRaises(oh_source_upgrade.PinUnfinished) as raised:
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "new"}\n')
        self.assertIn(f"restore --source {self.before} --staged --worktree -- .gitignore thermite-site.json",
                      raised.exception.command)
        lock.unlink()
        self.assertTrue(oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before))
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "old"}\n')
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before))

    def test_the_printed_command_finishes_it_too(self):
        lock = self.locked()
        with self.assertRaises(oh_source_upgrade.PinUnfinished) as raised:
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        lock.unlink()
        subprocess.run(raised.exception.command, shell=True, check=True, capture_output=True)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "old"}\n')

    def test_files_already_back_with_head_still_at_the_pin_commit_only_move_head(self):
        self.git("restore", "--source", self.before, "--staged", "--worktree", "--", ".gitignore",
                 "thermite-site.json")
        self.assertTrue(oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_site_already_back_needs_nothing_whatever_else_is_uncommitted(self):
        self.git("reset", "-q", "--hard", self.before)
        (self.root / "notes.txt").write_text("owner's file\n")
        self.git("add", "notes.txt")
        self.assertFalse(oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.git("status", "--porcelain"), "A  notes.txt")

    def test_other_changes_beside_a_half_done_reset_are_left_alone(self):
        self.git("update-ref", "HEAD", self.before, self.after)
        (self.root / "notes.txt").write_text("owner's file\n")
        self.git("add", "notes.txt")
        with self.assertRaisesRegex(oh_source_upgrade.PinMoved, "uncommitted"):
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "new"}\n')


class Unpin(GitSite):
    """Transaction.unpin over a real site: it never raises, and says how to
    finish what it could not."""

    def setUp(self):
        super().setUp()
        self.system = Mock(spec=System)
        self.transaction = changeover.Transaction(
            self.root / "var", txn_fakes.ID, {"site": str(self.root), "head_before": self.before,
                                              "head_after": self.after, "pinned": True, "kind": "upgrade"})

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)

    def test_a_rolled_back_transaction_puts_the_site_back(self):
        self.transaction.unpin(self.system)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn(f"Site: back at {self.before[:12]}", self.said())

    def test_a_failed_restore_is_said_with_its_command_and_a_rerun_finishes_it(self):
        lock = self.locked()
        self.transaction.unpin(self.system)
        self.assertIn(f"The site's branch is back at {self.before[:12]}, but its files could not be put back",
                      self.said())
        self.assertIn(f"git -C {self.root} restore --source {self.before}", self.said())
        lock.unlink()
        self.transaction.unpin(self.system)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "old"}\n')

    def test_the_next_run_finishes_a_reset_a_crash_cut_short(self):
        # The run that rolled back was killed between moving HEAD and
        # putting the files back, so its transaction is still open.
        self.git("update-ref", "HEAD", self.before, self.after)
        self.transaction.record.update(id=txn_fakes.ID, verb="upgrade", history=False, installed=True)
        self.transaction.save(phase="open")
        self.transaction.open()
        with patch.object(changeover.Transaction, "journal", return_value={"id": txn_fakes.ID, "phase": "rolled-back"}), \
                patch.object(changeover, "health", return_value=True):
            self.assertEqual(changeover.resolve(self.root, self.system), changeover.ROLLED_BACK)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "old"}\n')
        self.assertIsNone(changeover.open_transaction(self.root / "var"))

    def test_a_site_that_moved_on_gets_the_revert_to_run(self):
        (self.root / "thermite-site.json").write_text("edited\n")
        self.transaction.unpin(self.system)
        self.assertIn(f"To pin it: git -C {self.root} revert --no-edit {self.after[:12]}", self.said())
        self.assertEqual((self.root / "thermite-site.json").read_text(), "edited\n")

    def test_a_site_that_is_no_repository_is_said_not_raised(self):
        self.transaction.record["site"] = str(self.root / "var")
        (self.root / "var").mkdir()
        with patch.dict("os.environ", {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            self.transaction.unpin(self.system)
        self.assertIn("The site does not pin the version that runs", self.said())

    def test_a_revert_that_rolls_back_goes_forward_again(self):
        # A revert's transaction reset the site from the pin commit to the
        # one before; its rollback puts the pin commit back.
        self.git("reset", "-q", "--hard", self.before)
        revert = changeover.Transaction(self.root / "var", txn_fakes.ID, {
            "site": str(self.root), "head_before": self.after, "head_after": self.before, "pinned": True,
            "kind": "revert"})
        revert.unpin(self.system)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)
        self.assertEqual((self.root / "thermite-site.json").read_text(), '{"release": "new"}\n')
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.git("reset", "-q", "--hard", self.before)
        (self.root / "notes.txt").write_text("owner's file\n")
        self.git("add", "notes.txt")
        revert.unpin(self.system)
        self.assertIn(f"To pin it: git -C {self.root} merge --ff-only {self.after[:12]}", self.said())


class Escaped(unittest.TestCase):
    def test_a_failed_command_never_says_nothing_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            failure = subprocess.CalledProcessError(128, ["git", "-C", str(site), "rev-parse", "HEAD"])
            said = changeover.escaped(site, "upgrade", failure)
            self.assertEqual(said, f"a step failed (git exited 128); no install transaction is open: run "
                                   f"thermite upgrade --config {site} again to go on")
            (site / "var/transaction").mkdir(parents=True)
            (site / "var/transaction/open").write_text(txn_fakes.ID + "\n")
            said = changeover.escaped(site, "install", failure)
            self.assertIn(f"install transaction {txn_fakes.ID} has not finished: run thermite upgrade "
                          f"--config {site} to finish or roll it back", said)
            self.assertNotIn("nothing", said)


class History(unittest.TestCase):
    def test_a_rollback_is_shown_with_the_deploy_it_undid(self):
        from oh_cli import history
        with tempfile.TemporaryDirectory() as temporary:
            layout = deploy.State(Path(temporary))
            layout.history.parent.mkdir(parents=True)
            layout.history.write_text(json.dumps({"deployed_at": "2026-11-05T10:20:00+00:00", "commit": AFTER,
                                                  "by": "owner", "subject": "Pin", "how": "upgrade"}) + "\n")
            line = deploy.record_rollback(layout, {"transaction": txn_fakes.ID, "commit": AFTER, "restored": BEFORE})
            self.assertEqual(line["event"], "rolled-back")
            [entry] = deploy.deploys(layout)
            self.assertEqual(entry["rolled_back"][0]["transaction"], txn_fakes.ID)
            self.assertTrue(any(text.startswith("rolled back") and txn_fakes.ID in text and BEFORE[:12] in text
                                for text in history.details(entry)))
            for bad in ({"transaction": "x", "commit": AFTER, "restored": None},
                        {"transaction": txn_fakes.ID, "commit": "short", "restored": None},
                        {"transaction": txn_fakes.ID, "commit": AFTER}):
                with self.subTest(event=bad), self.assertRaises(deploy.DeployError):
                    deploy.record_rollback(layout, bad)


class Kinds(Fixture):
    """Install, a clean install, uninstall and purge: the same transaction."""

    def test_a_fresh_install_that_rolls_back_leaves_nothing_and_checks_no_health(self):
        self.host = Host(self)
        work = self.work([], [Fake("switch", self.log, 1)], [], kind="install", verb="install")
        self.assertEqual(self.transact(work, installed=None), changeover.ROLLED_BACK)
        self.assertNotIn("old health", self.log)
        self.assertIn("Nothing of Thermite was installed before it, and nothing is now", self.said())
        self.assertEqual((self.record()["kind"], self.record()["verb"]), ("install", "install"))

    def bridged(self, **answers):
        """The first transaction on a host installed before the engine,
        rolled back by its switch."""
        self.host = Host(self, **answers)
        work = self.work([], [Fake("switch", self.log, 1)], [])
        with patch.object(bridge, "describe", return_value={}), patch.object(changeover, "_gateway", return_value=None):
            return changeover.run(self.context, self.host.system, lambda id: work, installed=None, bridged=True,
                                  before=None, first=3)

    def test_a_bridged_run_given_no_pin_commit_still_records_the_head_it_installs(self):
        # changeover.pinned recognizes the version 1 wrapper's pin commit;
        # run itself never guesses, so `after` stays the head.
        self.assertEqual(self.bridged(), changeover.ROLLED_BACK)
        self.assertEqual((self.record()["head_before"], self.record()["head_after"], self.record()["bridged"]),
                         (None, AFTER, True))

    def test_a_bridged_rollback_says_how_version_1_installs_again(self):
        # Version 1's own install refuses a root holding .transaction.
        for health, code in ((0, changeover.ROLLED_BACK), (1, changeover.UNHEALTHY)):
            with self.subTest(health=health):
                shutil.rmtree(self.site / "var/transaction", ignore_errors=True)
                self.assertEqual(self.bridged(health=health), code)
                said = self.said()
                self.assertIn("may say next that a completed site release commit is retained", said)
                self.assertIn(f"refuses while /srv/thermite-fixture/.transaction exists, which keeps this transaction "
                              f"until one commits: run {changeover.command(self.site, 'upgrade')} again, or, as "
                              "root, remove /srv/thermite-fixture/.transaction first.", said)
        shutil.rmtree(self.site / "var/transaction")
        self.run_upgrade(switch=[("switch", 1)])
        self.assertNotIn("installed before the install engine", self.said())

    def test_an_uninstall_commits_what_is_installed_and_records_no_deploy(self):
        self.host = Host(self)
        work = changeover.uninstall_work(self.context, txn_fakes.ID)
        self.assertEqual((work.kind, work.release, work.pull, work.switching), ("uninstall", None, False, []))
        work.preparation = [Fake("account", self.log)]
        with patch.object(steps.Halt, "check", return_value=Check(True, "fixture")):
            self.assertEqual(self.transact(work, before=None, pinned=False), changeover.COMMITTED)
        self.assertFalse((self.directory / "release.json").exists(), "the installed release carries over")
        self.assertEqual(json.loads((self.directory / "desired.json").read_text())["items"], [])
        self.assertEqual((self.record()["head_before"], self.record()["head_after"]), (AFTER, AFTER))
        self.assertIn("its state stays under /srv/thermite-fixture", self.said())

    def test_a_rolled_back_uninstall_starts_thermite_again_without_a_history_line(self):
        self.host = Host(self)
        work = self.work([], [], [Fake("fails", self.log, 1)], kind="uninstall", verb="uninstall")
        work.release = None
        self.assertEqual(self.transact(work, before=None), changeover.ROLLED_BACK)
        self.assertFalse(any(entry.startswith("history") for entry in self.log))
        self.assertEqual(self.log[-1], "old health")

    def test_a_purge_types_the_root_first_and_removes_the_operator_records_at_the_commit(self):
        self.host = Host(self)
        work = changeover.uninstall_work(self.context, txn_fakes.ID, purge=True)
        self.assertEqual([item.to_json() for item in work.items(self.host.system)],
                         [{"path": "/srv/thermite-fixture/access", "class": "tree-move"},
                          {"path": "/srv/thermite-fixture/runtime", "class": "tree-move"}])
        work.preparation = []
        self.host.system.ask.return_value = "/srv/thermite-fixture/runtime"
        with patch.object(steps.Halt, "check", return_value=Check(True, "fixture")):
            self.assertEqual(self.transact(work, before=None), changeover.REFUSED)
        self.assertNotIn("begin", self.log)
        self.assertIn("That is not /srv/thermite-fixture; nothing was changed", self.said())
        self.log.clear()
        (self.site / "var/transaction" / txn_fakes.ID).rename(self.site / "var/transaction/earlier")
        self.host.system.ask.return_value = "/srv/thermite-fixture"
        with patch.object(steps.Halt, "check", return_value=Check(True, "fixture")):
            self.assertEqual(self.transact(work, before=None), changeover.COMMITTED)
        self.assertEqual(self.log[-2:], ["sudo -v", "commit"])
        self.assertFalse((self.site / "var/transaction").exists())
        self.assertIn("Purge committed", self.said())

    def purge(self, **answers):
        self.host = Host(self, **answers)
        work = changeover.uninstall_work(self.context, txn_fakes.ID, purge=True)
        work.preparation = []
        self.host.system.ask.return_value = "/srv/thermite-fixture"
        with patch.object(steps.Halt, "check", return_value=Check(True, "fixture")):
            return self.transact(work, before=None)

    def test_a_purge_its_rollback_finishes_still_removes_the_operator_records(self):
        self.assertEqual(self.purge(commit=1, rollback=0, phase="kept"), changeover.COMMITTED)
        self.assertEqual(self.log[-3:], ["rollback", "sudo -v", "status"])
        self.assertFalse((self.site / "var/transaction").exists(), "the token and credentials set aside go too")
        self.assertIn("Purge committed", self.said())

    def test_a_purge_a_later_run_resolves_still_removes_the_operator_records(self):
        with patch.object(changeover, "Authorize", side_effect=lambda: Fake("authorize", self.log,
                                                                             KeyboardInterrupt())):
            self.assertEqual(self.purge(), changeover.INTERRUPTED)
        self.assertTrue((self.directory / "operator").is_dir() or (self.directory / "record.json").exists())
        self.host = Host(self, phase="committed", rollback=0)
        self.assertEqual(changeover.resolve(self.site, self.host.system, "uninstall"), changeover.COMMITTED)
        self.assertFalse((self.site / "var/transaction").exists())

    def test_retention_runs_once_on_every_path_to_a_commit(self):
        with patch.object(changeover, "retain") as retain:
            self.assertEqual(self.run_upgrade(), changeover.COMMITTED)
            self.assertEqual(retain.call_count, 1, "straight through")
            retain.reset_mock()
            shutil.rmtree(self.directory)
            self.assertEqual(self.run_upgrade(commit=1, rollback=0, phase="kept"), changeover.COMMITTED)
            self.assertEqual(retain.call_count, 1, "finished by its rollback")
            retain.reset_mock()
            shutil.rmtree(self.directory)
            self.assertEqual(self.run_upgrade(switch=[("switch", KeyboardInterrupt())]), changeover.INTERRUPTED)
            retain.assert_not_called()
            self.host = Host(self, phase="committed", rollback=0)
            self.assertEqual(changeover.resolve(self.site, self.host.system), changeover.COMMITTED)
            self.assertEqual(retain.call_count, 1, "finished by a later run")
            transaction = retain.call_args.args[1]
            self.assertEqual((transaction.id, transaction.root), (txn_fakes.ID, Path("/srv/thermite-fixture")))

    def test_a_clean_install_moves_the_state_trees_aside_and_clears_the_records_first(self):
        self.context = steps.Context(**{**self.context.__dict__, "host": {**self.context.host, "http_port": 8080,
                                                                          "https_port": 8443}})
        with patch.object(steps, "NativeDeadline"):
            work = changeover.install_work(self.context, txn_fakes.ID, kind="clean")
        self.assertEqual((work.kind, work.verb), ("clean", "install"))
        self.assertIsInstance(work.switching[0], changeover.ClearState)
        self.assertEqual(work.confirm.path, self.context.layout.root)
        layout = self.context.layout
        declared = [manifest.Item(layout.userdata, "tree"), manifest.Item(layout.prometheus_data, "shared-pinned",
                                                                          image="prometheus", value="sha256:" + "f" * 64),
                    manifest.Item(layout.logs, "shared"), manifest.Item(layout.record, "file"),
                    manifest.Item(self.context.installation.access / "state", "tree")]
        with patch.object(manifest, "desired", return_value=declared):
            items = {str(item.path): item.cls for item in work.items(None)}
        self.assertEqual(items, {str(layout.userdata): "tree-move", str(layout.prometheus_data): "tree-move",
                                 str(layout.logs): "shared", str(layout.record): "file",
                                 str(self.context.installation.access / "state"): "tree"},
                         "the gateway's identity stays; logs and history stay")
        clear = work.switching[0].actions(None)
        self.assertTrue(clear[0].root)
        self.assertIn(str(layout.record), clear[0].argv)
        self.assertIn(str(self.site / self.context.token_file), clear[1].argv)

    def test_callbacks_found_by_a_commit_get_one_more_install_transaction(self):
        self.host = Host(self)
        gateway = Fake("gateway", self.log)
        gateway.deferred = True
        work = self.work([], [gateway], [])
        work.gateway = gateway
        with patch.object(changeover, "relay", return_value=changeover.COMMITTED) as relay:
            self.assertEqual(self.transact(work), changeover.COMMITTED)
        relay.assert_called_once()
        with patch.object(changeover, "run", return_value=changeover.ROLLED_BACK) as again, \
                patch.object(manifest, "installed", return_value=INSTALLED):
            transaction = changeover.Transaction.load(self.site / "var", txn_fakes.ID)
            self.assertEqual(changeover.relay(self.context, self.host.system, transaction, first=9),
                             changeover.COMMITTED, "the commit stands")
        self.assertEqual(again.call_args.kwargs["before"], None)
        self.assertEqual(again.call_args.kwargs["after"], AFTER, "the site does not move")
        self.assertIn("stays committed", self.said())

    def test_the_open_transaction_is_resolved_by_the_command_that_began_it(self):
        self.host = Host(self)
        work = self.work([], [Fake("switch", self.log, KeyboardInterrupt())], [], kind="uninstall", verb="uninstall")
        self.assertEqual(self.transact(work), changeover.INTERRUPTED)
        self.assertEqual(changeover.pending_command(self.site), f"thermite uninstall --config {self.site}")
        self.assertIn(f"Run thermite uninstall --config {self.site} to roll it back", self.said())

    def test_a_commit_keeps_the_current_and_previous_gateway_releases_and_removes_older_ones(self):
        self.host = Host(self)
        releases = self.context.installation.access / "releases"
        installed = {**INSTALLED, "items": [{"path": str(releases), "class": "versioned",
                                             "current": txn_fakes.ID, "previous": txn_fakes.OLD}]}
        self.host.system.directory_entries.return_value = [txn_fakes.OLD, txn_fakes.ID, "20250101-staged",
                                                            "fixture-v1"]
        transaction = changeover.Transaction(self.site / "var", txn_fakes.ID, {
            "gateway_before": "fixture-v1", "root": "/srv/thermite-fixture", "site": str(self.site)})
        with patch.object(manifest, "installed", return_value=installed):
            changeover.retain(self.host.system, transaction)
        self.assertEqual(self.log, [f"sudo rm -rf --one-file-system -- {releases}/20250101-staged"],
                         "the release the bridged version ran stays until the next commit")
        self.log.clear()
        transaction.record["gateway_before"] = None
        with patch.object(manifest, "installed", return_value=installed):
            changeover.retain(self.host.system, transaction)
        self.assertEqual(self.log, [f"sudo rm -rf --one-file-system -- {releases}/20250101-staged {releases}/fixture-v1"])
        self.log.clear()
        transaction.record["bridged"] = True
        with patch.object(manifest, "installed", return_value=installed):
            changeover.retain(self.host.system, transaction)
        self.assertEqual(self.log, [], "a bridged version's own release unknown: nothing is removed")

    def test_a_transaction_that_rolled_back_before_its_deploy_is_shown_under_the_one_that_ran(self):
        from oh_cli import history
        with tempfile.TemporaryDirectory() as temporary:
            layout = deploy.State(Path(temporary))
            layout.history.parent.mkdir(parents=True)
            layout.history.write_text(json.dumps({"deployed_at": "2026-11-05T10:20:00+00:00", "commit": BEFORE,
                                                  "by": "owner", "subject": "Pin", "how": "install"}) + "\n")
            deploy.record_rollback(layout, {"transaction": txn_fakes.ID, "commit": AFTER, "restored": BEFORE})
            [entry] = deploy.deploys(layout)
            [line] = [text for text in history.details(entry) if text.startswith("rolled back")]
            self.assertIn(f"of {AFTER[:12]}, which never deployed here", line)


def item(data: dict) -> manifest.Item:
    """A desired item, from the engine's JSON."""
    return manifest.Item(Path(data["path"]), data["class"], unit=data.get("unit"), apply_unit=data.get("apply_unit"),
                         target=Path(data["target"]) if "target" in data else None, image=data.get("pin"),
                         value=data.get("value"))


class FreshInstall(unittest.TestCase):
    """thermite install on a host Thermite never ran on, from the empty host
    to the commit: changeover's own steps, the real install engine over the
    in-memory host, and the installation root, which nothing else makes
    before the engine plans inside it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name) / "site"
        (self.site / "var").mkdir(parents=True, mode=0o700)
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}}, {"uid": 9001, "gid": 9001, "root": txn_fakes.ROOT},
            AFTER, "c" * 64, "fixture-host", "192.0.2.10", application={"version": "1.0", "digest": "d" * 64})
        self.disk = txn_fakes.MemoryDisk()
        for directory in (txn_fakes.UNITS, txn_fakes.WANTS, "/etc/containers/systemd", "/srv"):
            self.disk.put(directory, "dir", durable=True)
        self.disk.put(txn_fakes.APPARMOR, data=b"Y\n", durable=True)
        self.disk.settle()
        self.host = txn_fakes.FakeHost(self.disk)
        self.engine = engine.Engine(self.disk, self.host, txn_fakes.ROOT, source=txn_fakes.SOURCE,
                                    clock=lambda: txn_fakes.CLOCK)
        self.new = txn_fakes.version("new")
        self.log = []
        self.system = Mock(spec=System)
        self.system.interactive.return_value = True
        self.system.process_groups.return_value = {9001}
        self.system.unit_origins.side_effect = lambda names: {name: UnitOrigin() for name in names}
        self.system.unit_file.return_value = None
        self.system.fstype.return_value = "ext4"
        self.system.stat.side_effect = self.stat
        self.system.directory_entries.side_effect = lambda path: self.disk.listdir(path)
        self.system.run.side_effect = self.command
        self.system.capture.side_effect = self.captured
        scope = ExitStack()
        self.addCleanup(scope.close)
        for name, value in (("tools", txn_fakes.TOOLS), ("transaction_id", txn_fakes.ID)):
            scope.enter_context(patch.object(manifest, name, return_value=value))
        scope.enter_context(patch.object(manifest, "installed", lambda system, root: self.installed()))

    def stat(self, path):
        node = self.disk.nodes.get(str(path))
        return None if node is None else Stat(node.uid, node.gid, node.mode, node.kind == "dir", 0.0, len(node.data))

    def installed(self):
        node = self.disk.nodes.get("/etc/thermite/installed.json")
        return None if node is None else json.loads(node.data)

    def verb(self, argv):
        """An engine verb, run as the engine's own main would run it."""
        options = engine.parser().parse_args(argv)
        self.assertEqual(options.root, txn_fakes.ROOT)
        self.log.append(options.verb)

        def read(path):
            return json.loads(Path(path).read_text())

        self.engine.check_root()
        if options.verb == "plan":
            return json.dumps(self.engine.plan(read(options.desired)))
        if options.verb == "begin":
            self.engine.begin(options.id, read(options.desired), read(options.operator),
                              release=read(options.release) if options.release else None)
        else:
            getattr(self.engine, options.verb)(options.id)
        return ""

    def command(self, argv, cwd):
        argv = tuple(map(str, argv))
        if argv == ("sudo", "-v"):
            return 0
        if argv[1:4] == (changeover.PYTHON, "-I", "-S"):
            try:
                self.verb(argv[5:])
            except engine.EngineError as exc:
                self.log.append(f"refused: {exc}")
                return engine.FAILED
            return 0
        self.log.append(" ".join(argv))
        if argv[:2] == ("sudo", "mkdir"):
            self.disk.put(argv[-1], "dir", mode=0o755)
        elif argv[:3] == ("sudo", "install", "-d"):
            self.disk.put(argv[-1], "dir", mode=int(argv[-2], 8))
        else:
            raise AssertionError(f"unexpected command {argv}")
        return 0

    def captured(self, argv, **kwargs):
        argv = tuple(map(str, argv))
        self.assertEqual(argv[2:5], (changeover.PYTHON, "-I", "-S"))
        try:
            return 0, self.verb(argv[6:])
        except engine.EngineError as exc:
            self.log.append(f"refused: {exc}")
            return engine.FAILED, ""

    def switch(self, system):
        txn_fakes.switch(self.disk, self.host, self.new)
        return 0

    def healthy(self, system):
        self.assertIn(engine.TARGET, self.host.active)
        return 0

    def test_an_empty_host_gets_its_root_then_one_transaction_commits(self):
        made = [step for step in changeover.preparation(self.context) if isinstance(step, steps.InstallationRoot)]
        work = changeover.Work(
            "install", "install", made, [Fake("switch", self.log, self.switch)],
            [changeover.Authorize(), Fake("health", self.log, self.healthy)], txn_fakes.release(self.new),
            items=lambda system: [item(each) for each in txn_fakes.desired_items(self.new)],
            pull=False)
        self.assertNotIn(txn_fakes.ROOT, self.disk.nodes)
        code = changeover.run(self.context, self.system, lambda id: work, installed=None, bridged=False, before=None,
                              first=1)
        self.assertEqual(code, changeover.COMMITTED, self.log)
        self.assertEqual(self.log, [f"sudo mkdir {txn_fakes.ROOT}",
                                    f"sudo install -d -o root -g root -m 00755 {txn_fakes.ROOT}",
                                    "plan", "begin", "guard", "collect", "switch", "health", "commit"])
        root = self.disk.nodes[txn_fakes.ROOT]
        self.assertEqual((root.kind, root.uid, root.gid, root.mode), ("dir", 0, 0, 0o755))
        installed = self.installed()
        self.assertEqual((installed["installed"], installed["transaction"], installed["release"]),
                         (True, txn_fakes.ID, {"label": "new"}))
        self.assertTrue(self.host.enabled())
        self.assertEqual(self.engine.read(txn_fakes.ID).phase, "kept")
        record = json.loads((self.site / "var/transaction" / txn_fakes.ID / "record.json").read_text())
        self.assertEqual((record["phase"], record["installed"]), ("committed", False))
        self.assertIsNone(changeover.open_transaction(self.site / "var"))


class Unchanged(Fixture):
    """A rerun with nothing to change only checks health: the installed
    version is this one as recorded (release, pins, application, items)
    and as found (units, filter, confinement, deploy, target, services).
    Any one difference installs it again."""

    ITEMS = [manifest.Item(Path("/etc/systemd/system/thermite.target"), "unit", unit="thermite.target"),
             manifest.Item(Path("/srv/thermite-fixture/runtime/userdata"), "tree")]

    def setUp(self):
        super().setUp()
        self.system = Mock(spec=System)
        self.system.unit.return_value = Unit("active", "enabled")
        wanted = changeover.release(self.context)
        self.installed = {"installed": True, "release": wanted["release"], "pins": wanted["pins"],
                          "application": {"current": wanted["application"], "previous": None},
                          "items": [{"path": str(item.path), "class": item.cls} for item in self.ITEMS]}
        self.found = {}
        for target, name, value in (
                (manifest, "desired", self.ITEMS), (steps.Units, "stale", False),
                (changeover.NativeIngress, "current", True), (steps.NodeConfinement, "check", Check(True, "fixture")),
                (steps.Deploy, "check", Check(True, "fixture")), (steps.Services, "plan", ([], [], False)),
                (steps.MetricsServices, "plan", ([], [], False))):
            self.found[name if name not in ("check", "plan") else f"{target.__name__}.{name}"] = self.enterContext(
                patch.object(target, name, return_value=value))

    def unchanged(self, installed=None):
        return changeover.unchanged(self.context, self.system, self.installed if installed is None else installed)

    def test_the_version_recorded_and_found_is_unchanged(self):
        self.assertTrue(self.unchanged())

    def test_nothing_or_nothing_installed_is_never_unchanged(self):
        self.assertFalse(changeover.unchanged(self.context, self.system, None))
        self.assertFalse(self.unchanged({**self.installed, "installed": False}))

    def test_a_different_record_installs(self):
        for key, value in (("release", {**self.installed["release"], "site": BEFORE}),
                           ("pins", {"openhab": "sha256:" + "f" * 64}),
                           ("application", {"current": "a" * 64, "previous": None}),
                           ("items", self.installed["items"][:1]),
                           ("items", [{**self.installed["items"][0], "class": "file"}, self.installed["items"][1]])):
            with self.subTest(key=key, value=value):
                self.assertFalse(self.unchanged({**self.installed, key: value}))

    def test_a_difference_found_on_the_host_installs(self):
        runtime, grafana = "thermite-openhab.service", "thermite-grafana.service"
        for name, value in (("stale", True), ("current", False), ("NodeConfinement.check", Check(False, "fixture")),
                            ("Deploy.check", Check(False, "fixture")), ("Services.plan", ([runtime], [], False)),
                            ("Services.plan", ([], [runtime], False)), ("Services.plan", ([], [], True)),
                            ("MetricsServices.plan", ([grafana], [], False))):
            with self.subTest(found=name, value=value):
                kept = self.found[name].return_value
                self.found[name].return_value = value
                self.assertFalse(self.unchanged())
                self.found[name].return_value = kept
        for state in (Unit("inactive", "enabled"), Unit("active", "disabled")):
            with self.subTest(target=state):
                self.system.unit.return_value = state
                self.assertFalse(self.unchanged())


class Runner(unittest.TestCase):
    def test_a_failed_run_starts_nothing_again(self):
        # The transaction's rollback is the only undo.
        system = Mock(spec=System)
        system.interactive.return_value = True
        system.run.side_effect = lambda argv, cwd: 0

        class Stops(Step):
            title = "stop"
            always = True

            def check(self, system):
                return Check(False, "fixture")

            def actions(self, system):
                return [Command(("systemctl", "stop", oh_units.TARGET), root=True)]

        self.assertEqual(runner.run([Stops(), Fake("fails", [], 1)], system, Path("/home/owner/fixture-site"),
                                    root_confirmed=True), 1)
        self.assertEqual([call.args[0] for call in system.run.call_args_list],
                         [("sudo", "systemctl", "stop", oh_units.TARGET)])


if __name__ == "__main__":
    unittest.main()
