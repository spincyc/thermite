"""The scope of an install transaction (lib/oh_install/scope.py): one lock
from before an earlier transaction is looked for until this one has
committed or rolled back; SIGHUP and SIGTERM as an interrupt that rolls
back in the same run, after the child that still ran; sudo kept fresh; and
no way to run a transaction outside it.

The lock is a real flock, tested against a second process. The signals are
real ones, sent to this process. A child the run may not signal is a real
process; only the kernel's refusal (as for a root command under sudo) is
simulated. sudo and the install engine are scripts, as in
tests/test_txn_changeover.py: nothing here runs as root.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_cli import install as install_cli, revert as revert_cli, rollback as rollback_cli  # noqa: E402
from oh_cli import uninstall as uninstall_cli, upgrade as upgrade_cli  # noqa: E402
from oh_install import changeover, engine, output, runner, scope  # noqa: E402
from oh_install.system import System  # noqa: E402
from tests import test_txn_changeover as base, txn_fakes  # noqa: E402

#: No wait in this module is longer than this many seconds.
LIMIT = 20.0


def wait_for(found, what: str):
    """What `found()` returns once it is true; fails after LIMIT seconds."""
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        if value := found():
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def python(script: str, *args, **options) -> subprocess.Popen:
    """Another process of this application's Python, running `script`."""
    return subprocess.Popen([sys.executable, "-c", script, str(AREA / "lib"), *map(str, args)], **options)


class Site(unittest.TestCase):
    def setUp(self):
        self.site = Path(self.enterContext(tempfile.TemporaryDirectory())) / "site"
        self.site.mkdir()
        self.var = self.site / "var"
        self.system = Mock(spec=System)

    def scope(self, verb="upgrade"):
        return scope.Scope(self.var, self.system, verb)


#: A second run of an owner command on the site argv[2]: it would resolve
#: the open transaction, and prints what it did instead.
SECOND_RUN = """
import json, sys
sys.path.insert(0, sys.argv[1])
from unittest.mock import Mock
from oh_install import changeover
from oh_install.system import System
system = Mock(spec=System)
code = changeover.scoped(sys.argv[2], system, "rollback", lambda: changeover.resolve(sys.argv[2], system, "rollback"))
print(json.dumps({"code": code, "said": [str(call.args[0]) for call in system.say.call_args_list],
                  "commands": system.run.call_count + system.capture.call_count}))
"""


class Lock(Site):
    """L2: a second run reports the first as in progress and changes nothing."""

    def second_run(self) -> dict:
        done = subprocess.run([sys.executable, "-c", SECOND_RUN, str(AREA / "lib"), str(self.site)],
                              capture_output=True, text=True, timeout=LIMIT)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def open_transaction(self):
        transaction = changeover.Transaction(self.var, txn_fakes.ID, {
            "id": txn_fakes.ID, "verb": "upgrade", "kind": "upgrade", "site": str(self.site),
            "root": "/srv/thermite-fixture", "phase": "open"})
        transaction.directory.mkdir(parents=True)
        transaction.save()
        transaction.open()
        return transaction

    def test_a_second_run_says_who_is_running_and_leaves_the_open_transaction_alone(self):
        transaction = self.open_transaction()
        with self.scope("upgrade"):
            found = self.second_run()
        self.assertEqual(found["code"], changeover.REFUSED)
        self.assertEqual(found["commands"], 0, "it ran nothing: no sudo, no engine, no rollback")
        [said] = found["said"]
        self.assertRegex(said, rf"^Nothing was changed: thermite upgrade \(process {os.getpid()}, since "
                               r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\) is in progress for this site\. Run thermite "
                               r"rollback --config \S+ again once it has ended")
        self.assertEqual(changeover.open_transaction(self.var), txn_fakes.ID)
        self.assertEqual(changeover.Transaction.load(self.var, txn_fakes.ID).record, transaction.record)

    def test_once_the_run_has_ended_the_next_one_resolves_the_transaction_it_left(self):
        self.open_transaction()
        with self.scope():
            pass
        self.assertIsNone(scope.running(self.var))
        self.assertEqual((self.var / scope.LOCK).read_bytes(), b"", "nobody is named as running any more")
        found = self.second_run()
        self.assertIn(f"Transaction {txn_fakes.ID} of an earlier run did not finish; resolving it first.",
                      found["said"])

    def test_the_holder_is_named_to_whoever_asks_without_their_taking_the_lock(self):
        self.assertIsNone(scope.running(self.var), "no lock file yet, and asking makes none")
        self.assertFalse(self.var.exists())
        with self.scope("revert"):
            holder = scope.running(self.var)
            self.assertEqual((holder.verb, holder.pid), ("revert", os.getpid()))
            self.assertEqual(str(holder),
                             f"thermite rollback --restore-state (process {os.getpid()}, since {holder.since})")
            self.assertEqual(changeover.in_progress(self.site), holder)
        self.assertIsNone(scope.running(self.var))

    def test_a_run_that_starts_while_status_tests_the_lock_still_gets_it(self):
        self.var.mkdir()
        (self.var / scope.LOCK).touch()
        reader = os.open(self.var / scope.LOCK, os.O_RDONLY)
        self.addCleanup(os.close, reader)
        fcntl.flock(reader, fcntl.LOCK_SH)
        release = threading.Timer(scope.PATIENCE / 3, fcntl.flock, (reader, fcntl.LOCK_UN))
        release.start()
        self.addCleanup(release.join, LIMIT)
        with self.scope():
            self.assertEqual(scope.running(self.var).pid, os.getpid())

    def test_a_holder_that_names_nobody_is_in_progress_all_the_same(self):
        self.var.mkdir()
        other = os.open(self.var / scope.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX)
        with self.assertRaises(scope.InProgress) as refused:
            self.scope().__enter__()
        self.assertEqual(str(refused.exception), "another thermite command is in progress")
        self.assertEqual(scope._held, {})

    def test_a_directory_that_is_no_site_gets_no_lock_and_no_var(self):
        missing = self.site / "nowhere"
        with self.assertRaisesRegex(ValueError, "nowhere is not a directory: --config must name an existing "
                                                "Thermite site"):
            scope.Scope(missing / "var", self.system, "install").__enter__()
        self.assertFalse(missing.exists())

    def test_a_dry_run_takes_no_scope_and_makes_nothing(self):
        self.assertEqual(changeover.scoped(self.site, self.system, "upgrade", lambda: 7, dry_run=True), 7)
        self.assertFalse(self.var.exists())


class OnlyInside(Site):
    """By design: nothing begins, resolves or rolls back a transaction, and
    no engine command that changes the host exists, outside the scope."""

    def transaction(self):
        return changeover.Transaction(self.var, txn_fakes.ID, {"id": txn_fakes.ID, "site": str(self.site),
                                                               "root": "/srv/thermite-fixture"})

    def test_every_engine_verb_but_the_reading_ones_needs_the_scope(self):
        [verbs] = [action.choices for action in engine.parser()._actions if getattr(action, "choices", None)]
        self.assertEqual(changeover.READING, ("plan", "status"), "the verbs that change nothing, and no other")
        self.assertGreater(len(verbs), len(changeover.READING))
        transaction = self.transaction()
        for verb in verbs:
            with self.subTest(verb=verb):
                if verb in changeover.READING:
                    self.assertEqual(transaction.argv(verb)[4], verb)
                    continue
                with self.assertRaisesRegex(RuntimeError, "must run inside its scope"):
                    transaction.argv(verb)
                with self.assertRaisesRegex(RuntimeError, "must run inside its scope"):
                    transaction.command(verb)
                with self.scope():
                    self.assertEqual(transaction.command(verb).argv[4], verb)

    def test_the_driver_refuses_to_work_outside_it_before_it_touches_anything(self):
        context = Mock()
        context.var = self.var
        calls = {
            "resolve": lambda: changeover.resolve(self.site, self.system, "upgrade"),
            "run": lambda: changeover.run(context, self.system, lambda id: None, installed=None,
                                          before=None, first=1),
            "roll_back": lambda: changeover.roll_back(self.system, self.transaction(), "a step failed"),
            "pinned": lambda: changeover.pinned(self.system, self.site, "1" * 40, False, lambda before: 0),
        }
        for name, call in calls.items():
            with self.subTest(call=name):
                with self.assertRaisesRegex(RuntimeError, "must run inside its scope"):
                    call()
        self.assertEqual(self.system.mock_calls, [])
        self.assertFalse(self.var.exists())

    def test_every_owner_command_that_runs_a_transaction_holds_the_scope_around_all_of_it(self):
        seen = []

        def inside(*args, **kwargs):
            seen.append(scope.held(self.var).verb)
            return 0

        def enter(module, argv=(), sequence=None):
            with patch.object(sequence or module, "AREA", self.site), \
                    patch.object(module.os, "geteuid", return_value=1000):
                self.assertEqual(module.main(list(argv)), 0)

        for module, name in ((rollback_cli, "roll_back"), (revert_cli, "revert"), (uninstall_cli, "uninstall")):
            with patch.object(module, name, inside):
                enter(module)
        # install and upgrade, one sequence in install's module: the check
        # for an earlier transaction, then (as the application workflow's
        # child) the host phase.
        for module, verb in ((install_cli, "install"), (upgrade_cli, "upgrade")):
            with patch.object(changeover, "resolve", side_effect=lambda *args: inside() and None or None), \
                    patch(f"oh_application_update.{verb}", return_value=0):
                enter(module, sequence=install_cli)
            with patch.object(changeover, "pinned", inside):
                enter(module, ["--_host-phase", "--_run-approved"], sequence=install_cli)
        self.assertEqual(seen, ["rollback", "revert", "uninstall", "install", "install", "upgrade", "upgrade"])
        self.assertEqual(scope._held, {}, "and none holds it afterwards")


class Unasked(base.Host):
    """The script of tests/test_txn_changeover.py, which also takes the
    root commands a run issues once nobody is there to ask: under sudo -n."""

    def __init__(self, test, **answers):
        super().__init__(test, **answers)
        #: Whether each root command was issued under sudo -n.
        self.unasked = []
        #: What happens while the engine runs a verb.
        self.during = {}
        #: Whether sudo -n still knows the owner.
        self.forgotten = False

    def run(self, argv, cwd):
        argv = tuple(argv)
        if argv[0] == "sudo":
            self.unasked.append(argv[1] == "-n")
            if argv[1] == "-n":
                if self.forgotten:
                    self.log.append("sudo -n refused")
                    return 1
                argv = ("sudo", *argv[2:])
            if (verb := self.verb(argv[1:])) in self.during:
                self.during[verb]()
        return super().run(argv, cwd)


class Signalled(base.Fixture):
    """L4: SIGHUP and SIGTERM end the run as an interrupt that rolls back
    in the same run."""

    def setUp(self):
        super().setUp()
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signals are handled in the main thread only")
        self.assertNotIn(signal.getsignal(signal.SIGTERM), (signal.SIG_DFL, signal.SIG_IGN),
                         "the scope handles it: sending it here must not end the tests")

    def send(self, number):
        def outcome(system):
            os.kill(os.getpid(), number)
            raise AssertionError("the signal did not interrupt the step")
        return outcome

    def upgrade(self, *, prepare=(), switch=(), **answers):
        self.host = Unasked(self, **answers)
        prepared = [base.Fake(name, self.log, outcome) for name, outcome in (prepare or [("prepare", 0)])]
        switched = [base.Fake(name, self.log, outcome) for name, outcome in (switch or [("switch", 0)])]
        verified = [changeover.Authorize(), base.Fake("health", self.log, 0)]
        return self.transact(self.work(prepared, switched, verified))

    def rolls_back(self, number):
        self.assertEqual(self.upgrade(switch=[("switch", self.send(number))]), changeover.ROLLED_BACK)
        self.assertEqual(self.log[-5:], ["switch", "sudo -v", "rollback", f"history {txn_fakes.ID}", "old health"])
        self.assertIn(f"Rolling back transaction {txn_fakes.ID} (the run was sent {number.name})", self.said())
        self.assertEqual(self.record()["phase"], "rolled-back")
        self.assertIsNone(self.is_open())
        self.assertEqual(self.unpinned, [txn_fakes.ID])

    def test_a_hangup_during_the_switch_rolls_back_and_starts_the_version_before(self):
        self.rolls_back(signal.SIGHUP)

    def test_a_termination_during_the_switch_rolls_back_and_starts_the_version_before(self):
        self.rolls_back(signal.SIGTERM)

    def test_after_the_signal_every_root_command_fails_rather_than_asks(self):
        self.assertEqual(self.upgrade(switch=[("switch", self.send(signal.SIGHUP))]), changeover.ROLLED_BACK)
        asked = self.host.unasked
        self.assertEqual(asked[-2:], [True, True], "the rollback's sudo -v and engine command run under sudo -n")
        self.assertNotIn(True, asked[:-2], "what ran before the signal asked as usual")
        self.assertGreater(len(asked), 2)
        self.host.system.ask.assert_not_called()

    def test_when_sudo_has_forgotten_the_owner_the_transaction_stays_open_and_nobody_is_asked(self):
        def switch(system):
            # A terminal that hung up took sudo's credentials with it.
            self.host.forgotten = True
            return self.send(signal.SIGHUP)(system)
        self.assertEqual(self.upgrade(switch=[("switch", switch)]), changeover.INTERRUPTED)
        self.assertEqual(self.log[-2:], ["switch", "sudo -n refused"])
        self.assertIn("sudo did not authenticate, so nothing was rolled back", self.said())
        self.assertEqual(self.is_open(), txn_fakes.ID)
        self.assertEqual(self.unpinned, [])
        self.host.system.ask.assert_not_called()

    def test_a_signal_before_begin_changes_nothing_and_undoes_the_pin(self):
        self.assertEqual(self.upgrade(prepare=[("prepare", self.send(signal.SIGTERM))]), changeover.INTERRUPTED)
        self.assertEqual(self.log, ["prepare"])
        self.assertEqual(self.record()["phase"], "refused")
        self.assertEqual(self.unpinned, [txn_fakes.ID])
        self.assertIsNone(self.is_open())

    def test_a_second_signal_does_not_interrupt_the_rollback(self):
        first = self.send(signal.SIGHUP)

        def switch(system):
            # A hangup arrives twice: from the shell, and from the kernel.
            self.host.during["rollback"] = lambda: os.kill(os.getpid(), signal.SIGHUP)
            return first(system)
        self.assertEqual(self.upgrade(switch=[("switch", switch)]), changeover.ROLLED_BACK)
        self.assertEqual(self.record()["phase"], "rolled-back")

    def test_a_signal_during_a_rollback_already_under_way_runs_it_again_to_its_end(self):
        def switch(system):
            self.host.during["rollback"] = self.once(lambda: os.kill(os.getpid(), signal.SIGTERM))
            return 1
        self.assertEqual(self.upgrade(switch=[("switch", switch)]), changeover.ROLLED_BACK)
        self.assertEqual(self.log.count("rollback"), 1, "the first was interrupted before the engine answered")
        self.assertIn("(a step failed)", self.said())
        self.assertIn("(the run was sent SIGTERM)", self.said())
        self.assertEqual(self.record()["phase"], "rolled-back")

    def once(self, action):
        done = []
        return lambda: done or (done.append(True), action())

    def test_ctrl_c_still_leaves_the_rollback_to_the_owner(self):
        self.assertEqual(self.upgrade(switch=[("switch", KeyboardInterrupt())]), changeover.INTERRUPTED)
        self.assertEqual(self.is_open(), txn_fakes.ID)
        self.assertNotIn("rollback", self.log)

    def test_the_signals_are_the_callers_again_when_the_scope_ends(self):
        self.scope.close()
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)
        self.assertFalse(scope.ended())

    def test_a_run_told_to_ignore_hangups_still_does(self):
        self.scope.close()
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # As under nohup.
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        with scope.Scope(self.site / "var", Mock(spec=System), "upgrade") as held:
            os.kill(os.getpid(), signal.SIGHUP)
            self.assertIsNone(held.signal)
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


#: A command that still changes the host when its run is told to end. It
#: says so in argv[1], works for argv[3] seconds, then records in argv[2]
#: whether the run still held the lock argv[4] when it finished. SIGINT,
#: where it can be sent, ends it at once, and it records that instead.
MUTATING = """
import fcntl, os, signal, sys, time
started, finished, seconds, lock = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]

def result(text):
    with open(finished + ".partial", "w") as stream:
        stream.write(text)
    os.replace(finished + ".partial", finished)

def stopped(number, frame):
    result("stopped")
    raise SystemExit(0)

signal.signal(signal.SIGINT, stopped)
open(started, "w").close()
time.sleep(seconds)
descriptor = os.open(lock, os.O_RDONLY)
try:
    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    result("finished, lock free")
except BlockingIOError:
    result("finished, lock held")
"""


class ChildStillRunning(Site):
    """L4: the driver's children run in their own session or process group.
    One that still runs when the signal arrives is stopped, or waited for
    when this user may not signal it; either way before the run goes on,
    and the lock is released only after that."""

    def setUp(self):
        super().setUp()
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signals are handled in the main thread only")
        self.started, self.finished = self.site / "started", self.site / "finished"
        self.reports = []

    def run_child(self, seconds, *, privileged):
        """output.run on the child, as System.run starts a root command
        (its own process group) or any other (its own session); the run is
        sent SIGTERM once the child has started."""
        argv = [sys.executable, "-c", MUTATING, str(self.started), str(self.finished), str(seconds),
                str(self.var / scope.LOCK)]

        def signal_once_started():
            wait_for(self.started.exists, "the child to start")
            os.kill(os.getpid(), signal.SIGTERM)
        sender = threading.Thread(target=signal_once_started, daemon=True)
        with self.scope():
            sender.start()
            try:
                with self.assertRaises(scope.Terminated):
                    output.run(argv, self.site, timeout=LIMIT, report=self.reports.append, keep_terminal=privileged)
            finally:
                sender.join(LIMIT)
            # The run goes on from here (to its rollback), still holding the lock.
            return self.finished.read_text() if self.finished.exists() else None

    def test_a_child_the_run_may_not_signal_is_waited_for_and_the_lock_outlasts_it(self):
        real = os.killpg

        def refused(pid, number):
            # What the kernel answers this user for a root command under sudo.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return real(pid, number)
            raise PermissionError(errno.EPERM, "Operation not permitted")
        # Shorter than the child's work: giving up on it would show.
        with patch.object(output.os, "killpg", refused), patch.object(output, "STOP_GRACE", 0.2), \
                patch.object(output, "KILL_GRACE", 0.2):
            self.assertEqual(self.run_child(1.5, privileged=True), "finished, lock held",
                             "it ran to its own end before the run went on, under the lock")
        self.assertIn("Waiting for the command to end: it runs as root, and this run may not stop it.", self.reports)
        self.assertIsNone(scope.running(self.var))

    def test_a_child_the_run_may_signal_is_stopped_before_the_run_goes_on(self):
        self.assertEqual(self.run_child(LIMIT, privileged=False), "stopped")
        self.assertNotIn("Waiting for the command to end: it runs as root, and this run may not stop it.",
                         self.reports)

    def test_a_signal_that_arrives_while_a_child_is_being_stopped_waits_for_that(self):
        with self.scope() as held:
            with self.assertRaises(scope.Terminated):
                with output._undisturbed():
                    os.kill(os.getpid(), signal.SIGHUP)
                    stopping_went_on = True
            self.assertTrue(stopping_went_on)
            self.assertIs(held.signal, signal.SIGHUP)


#: A run inside the scope of the site argv[2] whose terminal hangs up: it
#: reports to argv[3] whether it could still say something afterwards.
HANGS_UP = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
from unittest.mock import Mock
from oh_install import scope
from oh_install.system import System
site, report = sys.argv[2], sys.argv[3]
with scope.Scope(os.path.join(site, "var"), Mock(spec=System), "upgrade"):
    open(os.path.join(site, "ready"), "w").close()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.05)
        outcome = "no signal"
    except scope.Terminated as stop:
        try:
            print("Rolling back", flush=True)
            print("on standard error", file=sys.stderr, flush=True)
            outcome = f"{stop}: said"
        except OSError as error:
            outcome = f"{stop}: could not say it ({error})"
with open(report, "w") as stream:
    stream.write(outcome)
"""


class Hangup(Site):
    def test_what_a_run_says_after_its_terminal_hung_up_goes_nowhere_and_stops_nothing(self):
        master, slave = pty.openpty()
        kept = self.site / "kept.log"
        report = self.site / "report"
        with open(kept, "w") as log:
            child = python(HANGS_UP, self.site, report, stdin=subprocess.DEVNULL, stdout=slave, stderr=log,
                           start_new_session=True)
        os.close(slave)
        try:
            wait_for((self.site / "ready").exists, "the run to hold its scope")
            os.close(master)  # The terminal is gone: every write to it fails now.
            os.kill(child.pid, signal.SIGHUP)
            self.assertEqual(child.wait(timeout=LIMIT), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=LIMIT)
        self.assertEqual(report.read_text(), "SIGHUP: said")
        self.assertEqual(kept.read_text(), "on standard error\n", "a stream that still works is left alone")


class Fresh(Site):
    """sudo's credentials, validated again while the transaction lasts."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(scope, "REFRESH", 0.02))
        self.answers = []
        self.system.capture.side_effect = lambda argv, **options: (self.answers.append(tuple(argv)) or self.code, "")
        self.code = 0

    def test_they_are_validated_again_without_asking_until_the_scope_ends(self):
        with self.scope() as held:
            time.sleep(0.1)
            self.assertEqual(self.answers, [], "not before the transaction needs them")
            held.keep_fresh()
            held.keep_fresh()
            wait_for(lambda: len(self.answers) >= 3, "three validations")
        ended = len(self.answers)
        self.assertEqual(set(self.answers), {("sudo", "-n", "-v")})
        time.sleep(0.1)
        self.assertEqual(len(self.answers), ended, "none after the scope ended")
        self.assertEqual([thread.name for thread in threading.enumerate() if thread.name == "thermite-sudo-fresh"], [])

    def test_once_sudo_refuses_it_is_not_asked_again(self):
        self.code = 1
        with self.scope() as held:
            held.keep_fresh()
            wait_for(lambda: self.answers, "the first validation")
            time.sleep(0.1)
            self.assertEqual(len(self.answers), 1)


class FreshFromBegin(base.Fixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(scope.Scope, "keep_fresh", lambda held: self.log.append("keep sudo fresh")))

    def test_a_transaction_keeps_them_fresh_from_begin_to_its_end(self):
        self.assertEqual(self.run_upgrade(), changeover.COMMITTED)
        self.assertEqual(self.log[self.log.index("begin"):][:3], ["begin", "keep sudo fresh", "guard"])
        self.assertEqual(self.log.count("keep sudo fresh"), 1)

    def test_a_transaction_that_never_begins_has_nothing_to_keep_fresh(self):
        self.assertEqual(self.run_upgrade(begin=1), changeover.REFUSED)
        self.assertNotIn("keep sudo fresh", self.log)

    def test_resolving_an_earlier_one_keeps_them_fresh_before_its_rollback(self):
        self.assertEqual(self.run_upgrade(switch=[("switch", KeyboardInterrupt())]), changeover.INTERRUPTED)
        self.log.clear()
        self.host = base.Host(self)
        self.assertEqual(changeover.resolve(self.site, self.host.system), changeover.ROLLED_BACK)
        self.assertEqual(self.log[:4], ["sudo -v", "status", "keep sudo fresh", "sudo -v"])


class RootCommands(unittest.TestCase):
    def test_outside_an_ended_run_a_root_command_still_needs_the_owner(self):
        system = Mock(spec=System)
        system.interactive.return_value = False
        self.assertFalse(scope.ended())
        self.assertIsNone(runner.perform(base.Command(("true",), root=True), system, AREA, root_confirmed=True))
        system.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
