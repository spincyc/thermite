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
from oh_install import changeover, engine, manifest, runner, steps  # noqa: E402
from oh_install.model import Check, Command, Step, Task  # noqa: E402
from oh_install.system import System, Unit, UnitOrigin  # noqa: E402
from tests import txn_fakes  # noqa: E402

BEFORE, AFTER = "1" * 40, "2" * 40
IMAGE = "registry.example.test/openhab@sha256:" + "e" * 64
SPACE = {"required": 300 * 1024 * 1024, "available": 4 * 1024 ** 3, "margin": engine.MARGIN, "extra": 0,
         "items": [["runtime/userdata", 40 * 1024 * 1024]]}


class Fake(Step):
    """A step that records running and answers with `outcome`."""

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
        return self.outcome


class Host:
    """The operator's view: commands, sudo, and the engine as a script."""

    def __init__(self, test, *, plan=0, begin=0, rollback=3, commit=0, phase="switching", health=0):
        self.test = test
        self.log = test.log
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
            return 0
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
                            ("tools", txn_fakes.TOOLS), ("transaction_id", txn_fakes.ID)):
            scope.enter_context(patch.object(manifest, name, return_value=value))
        scope.enter_context(patch.object(changeover.Transaction, "unpin",
                                         lambda transaction, system: self.unpinned.append(transaction.id)))
        scope.enter_context(patch.object(changeover, "health", self.health))

    def health(self, system, transaction):
        """The reinstated version's health, as its owner command answers."""
        self.log.append("old health")
        return self.host.health == 0

    def run_upgrade(self, *, prepare=(), switch=(), verify=(), **answers):
        self.host = Host(self, **answers)
        prepared = [Fake(name, self.log, outcome) for name, outcome in (prepare or [("prepare", 0)])]
        switched = [Fake(name, self.log, outcome) for name, outcome in (switch or [("switch", 0)])]
        verified = [changeover.Authorize(), *(Fake(name, self.log, outcome)
                                               for name, outcome in (verify or [("health", 0)]))]
        with patch.object(changeover, "preparation", return_value=prepared), \
                patch.object(changeover, "switching", return_value=switched), \
                patch.object(changeover, "verification", return_value=verified):
            return changeover.upgrade(self.context, self.host.system, installed=None, bridged=False, before=BEFORE,
                                      first=3)

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
        with patch.object(changeover, "preparation", return_value=[]), \
                patch.object(changeover, "switching", return_value=[switch, Fake("never", self.log)]), \
                patch.object(changeover, "verification", return_value=[]):
            code = changeover.upgrade(self.context, self.host.system, installed=None, bridged=False, before=BEFORE,
                                      first=3)
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
        self.assertEqual(problems, [f"upgrade transaction {txn_fakes.ID} has not finished"])
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


class SitePin(unittest.TestCase):
    """F7: a rolled-back upgrade's pin commit is undone, compare and swap."""

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
        with self.assertRaisesRegex(oh_source_upgrade.PinMoved, "not the upgrade's pin commit"):
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        self.assertEqual(self.git("rev-parse", "HEAD"), moved)
        self.git("reset", "-q", "--hard", self.after)
        (self.root / "thermite-site.json").write_text("edited\n")
        with self.assertRaisesRegex(oh_source_upgrade.PinMoved, "uncommitted"):
            oh_source_upgrade.reset_pin(self.root, expected=self.after, previous=self.before)
        self.assertEqual((self.root / "thermite-site.json").read_text(), "edited\n")


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


class Runner(unittest.TestCase):
    def test_inside_a_transaction_nothing_is_started_again(self):
        system = Mock(spec=System)
        system.interactive.return_value = True
        system.run.side_effect = lambda argv, cwd: 0

        class Stops(Step):
            title = "stop"
            always = True

            def check(self, system):
                return Check(False, "fixture")

            def actions(self, system):
                return [Command(("systemctl", "stop", oh_units.TARGET), root=True, stops=(oh_units.TARGET,))]

        failing = Fake("fails", [], 1)
        for transactional, started in ((False, True), (True, False)):
            with self.subTest(transactional=transactional):
                system.run.reset_mock()
                runner.run([Stops(), failing], system, Path("/home/owner/fixture-site"), root_confirmed=True,
                           transactional=transactional)
                commands = [call.args[0] for call in system.run.call_args_list]
                self.assertEqual(("sudo", "systemctl", "start", oh_units.TARGET) in commands, started)


if __name__ == "__main__":
    unittest.main()
