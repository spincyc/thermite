"""Lifecycle presentation must not hide failures or interfere with prompts."""

import contextlib
import io
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli
from oh_install import progress, runner
from oh_install.model import Check, Command, Step, Task
from oh_install.system import System


class Terminal(io.StringIO):
    encoding = "utf-8"

    def __init__(self):
        super().__init__()
        self.rotated = threading.Event()

    def isatty(self):
        return True

    def write(self, value):
        if "[ / ]" in value:
            self.rotated.set()
        return super().write(value)


class Stage(Step):
    title = "Service ready"

    def __init__(self, checks, actions=(), *, always=False):
        self.checks = iter(checks)
        self.work = actions
        self.always = always

    def check(self, system):
        return next(self.checks)

    def actions(self, system):
        return self.work


class Presentation(unittest.TestCase):
    def setUp(self):
        self.options = mock.patch.object(oh_cli, "switches", oh_cli.Switches())
        self.options.start()
        self.addCleanup(self.options.stop)
        self.env = mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_stage(self, step, stream=None):
        stream = stream if stream is not None else io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = runner.run([step], System(), Path.cwd(), root_confirmed=True)
        self.assertIsNone(progress.current.get())
        self.assertFalse(progress.capture_command.get())
        return result, stream.getvalue()

    def test_already_done_is_one_compact_line_without_color_when_redirected(self):
        result, out = self.run_stage(Stage([Check(True, "long implementation detail")]))
        self.assertEqual(result, 0)
        self.assertEqual(out, "[ OK ] 1. Service ready\n")

    def test_spins_during_check_then_green_pass_only_after_it_finishes(self):
        stream = Terminal()

        def wait_for_paint(system):
            self.assertTrue(stream.rotated.wait(2), stream.getvalue())
            self.assertNotIn("✓", stream.getvalue())
            return Check(True, "ready")

        step = Stage([])
        step.check = wait_for_paint
        result, out = self.run_stage(step, stream)
        self.assertEqual(result, 0)
        self.assertIn("[ | ]", out)
        self.assertIn("[ / ]", out)
        self.assertIn("\x1b[1;32m✓\x1b[0m", out)
        self.assertFalse(any(t.name == "thermite-progress" for t in threading.enumerate()))

    def test_successful_command_does_not_pass_a_failed_postcheck(self):
        stage = Stage([Check(False, "needs work"), Check(False, "service did not start")],
                      [Task("start", lambda system: 0)])
        result, out = self.run_stage(stage)
        self.assertEqual(result, 1)
        self.assertIn("[ FAIL ]", out)
        self.assertIn("service did not start", out)
        self.assertNotIn("[ OK ]", out)

    def test_always_step_failure_stays_failed_after_later_success(self):
        ran = []
        stage = Stage([Check(False, "verify")], [Task("health", lambda system: 7),
                      Task("status", lambda system: ran.append("status") or 0)], always=True)
        result, out = self.run_stage(stage)
        self.assertEqual(result, 7)
        self.assertEqual(ran, ["status"])
        self.assertIn("[ FAIL ]", out)
        self.assertNotIn("[ OK ]", out)

    def test_verbose_shows_details_and_disables_animation(self):
        oh_cli.switches.verbose = True
        result, out = self.run_stage(Stage([Check(True, "detailed receipt")]), Terminal())
        self.assertEqual(result, 0)
        self.assertIn("detailed receipt", out)
        self.assertNotIn("\r", out)

    def test_no_color_and_dumb_terminal(self):
        oh_cli.switches.no_color = True
        _, out = self.run_stage(Stage([Check(True, "ready")]), Terminal())
        self.assertNotIn(";32m", out)
        oh_cli.switches.no_color = False
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            _, out = self.run_stage(Stage([Check(True, "ready")]), Terminal())
        self.assertNotIn(";32m", out)
        with mock.patch.dict(os.environ, {"TERM": "dumb"}):
            _, out = self.run_stage(Stage([Check(True, "ready")]), Terminal())
        self.assertNotIn("\x1b", out)
        self.assertIn("[ OK ]", out)

    def test_quiet_failure_keeps_task_diagnostics_on_stderr(self):
        oh_cli.switches.quiet = True

        def work(system):
            system.say("The service refused its configuration")
            return 1

        with contextlib.redirect_stderr(io.StringIO()) as error:
            result, out = self.run_stage(Stage([Check(False, "start")], [Task("start", work)]))
        self.assertEqual(result, 1)
        self.assertIn("[ FAIL ]", error.getvalue())
        self.assertIn("The service refused its configuration", error.getvalue())
        self.assertNotIn("[ OK ]", out)

    def test_secret_prompt_pauses_painting_and_preserves_quiet_setup_instructions(self):
        oh_cli.switches.quiet = True
        stream = Terminal()
        system = System()

        def paste(prompt):
            self.assertGreater(progress.current.get().paused, 0)
            return "synthetic-private-value"

        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()) as error:
            with progress.Progress(system, "Administrator") as display:
                system.say("Open the profile page")
                system.say("Then create an API token")
                with mock.patch("getpass.getpass", side_effect=paste):
                    self.assertEqual(system.secret("Token: "), "synthetic-private-value")
                display.passed("saved")
        self.assertIn("Open the profile page", error.getvalue())
        self.assertIn("Then create an API token", error.getvalue())
        self.assertNotIn("synthetic-private-value", error.getvalue() + stream.getvalue())

    def test_terminal_command_inherits_streams_and_pauses_painting(self):
        stream = Terminal()
        system = System()

        def child(argv, **options):
            self.assertGreater(progress.current.get().paused, 0)
            self.assertFalse(progress.capture_command.get())
            self.assertNotIn("stdin", options)
            self.assertNotIn("stdout", options)
            self.assertNotIn("stderr", options)
            return mock.Mock(returncode=0)

        stage = Stage([Check(False, "work"), Check(True, "done")],
                      [Command(("child",), terminal=True, approved_argv=("child", "--yes"))])
        with mock.patch("oh_install.system.subprocess.run", side_effect=child) as run:
            result, _ = self.run_stage(stage, stream)
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.args[0], ["child", "--yes"])

    def test_cancel_clears_spinner_without_success_and_resets_context(self):
        def cancel(system):
            raise KeyboardInterrupt

        stream = Terminal()
        with contextlib.redirect_stdout(stream), self.assertRaises(KeyboardInterrupt):
            runner.run([Stage([Check(False, "wait")], [Task("wait", cancel)])], System(), Path.cwd())
        self.assertIn("STOP", stream.getvalue())
        self.assertNotIn("✓", stream.getvalue())
        self.assertIsNone(progress.current.get())
        self.assertFalse(any(t.name == "thermite-progress" for t in threading.enumerate()))

    def test_quiet_terminal_child_gets_a_visible_output_stream(self):
        oh_cli.switches.quiet = True
        terminal = io.StringIO()
        with mock.patch("builtins.open", return_value=terminal) as opened, \
                mock.patch("oh_install.system.subprocess.run", return_value=mock.Mock(returncode=0)) as child:
            with progress.command(terminal=True):
                self.assertEqual(System().run(["interactive-child"], AREA), 0)
            self.assertEqual(child.call_args.kwargs["stdout"], terminal)
            self.assertNotIn("stdin", child.call_args.kwargs)
            opened.assert_called_once_with("/dev/tty", "w")
        self.assertTrue(terminal.closed)
        self.assertFalse(progress.terminal_command.get())

    def test_quiet_terminal_child_without_controlling_tty_uses_visible_stderr(self):
        oh_cli.switches.quiet = True
        with mock.patch("builtins.open", side_effect=OSError), \
                mock.patch("oh_install.system.subprocess.run", return_value=mock.Mock(returncode=0)) as child:
            with progress.command(terminal=True):
                self.assertEqual(System().run(["interactive-child"], AREA), 0)
            self.assertIs(child.call_args.kwargs["stdout"], sys.stderr)

    def test_noninteractive_success_is_hidden_and_failure_details_remain(self):
        for code in (0, 5):
            with self.subTest(code=code):
                command = Command((sys.executable, "-B", "-c",
                                   f"print('child diagnostic'); raise SystemExit({code})"))
                result, out = self.run_stage(Stage([Check(False, "run"), Check(True, "ready")], [command]))
                self.assertEqual(result, code)
                self.assertEqual("child diagnostic" in out, bool(code))
                self.assertEqual("[ OK ]" in out, not code)


if __name__ == "__main__":
    unittest.main()
