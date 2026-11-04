"""Lifecycle presentation must not hide failures or interfere with prompts."""

import contextlib
import io
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli
from oh_install import progress, runner
from oh_install import output
from oh_install import steps
from oh_install.model import Check, Command, Step, Task
from oh_install.system import NativeProbe, System
from oh_deploy import MetricsHealth


class CaptureDiagnostics(unittest.TestCase):
    def setUp(self):
        options = mock.patch.object(oh_cli, "switches", oh_cli.Switches())
        options.start()
        self.addCleanup(options.stop)
        self.system = System()

    def test_opted_in_failure_reports_status_and_stderr_without_stdout_or_arguments(self):
        argv = ("/private/programs/nft", "--json", "private-argument")
        child = subprocess.CompletedProcess(argv, 3, "private captured firewall", "Kernel rules unavailable\n")
        oh_cli.switches.verbose = True
        with mock.patch("oh_install.system.subprocess.run", return_value=child) as run, \
                contextlib.redirect_stdout(io.StringIO()) as visible, \
                contextlib.redirect_stderr(io.StringIO()) as trace:
            self.assertEqual(self.system.capture(argv, timeout=4, diagnostics=True), (3, child.stdout))
        run.assert_called_once_with(list(argv), capture_output=True, text=True, check=False, timeout=4)
        self.assertIn("nft: exit status 3", visible.getvalue())
        self.assertIn("Kernel rules unavailable", visible.getvalue())
        shown = visible.getvalue() + trace.getvalue()
        for private in (child.stdout, "private-argument", "/private/programs"):
            self.assertNotIn(private, shown)

    def test_failure_stderr_is_bounded_and_terminal_controls_are_removed(self):
        stderr = "earlier stderr\n" + "x" * 200 + "\n\x1b[31mKernel\x1b[0m\x00\u202e unavailable\r\n"
        child = subprocess.CompletedProcess(("nft",), 1, "private captured firewall", stderr)
        with mock.patch("oh_install.system.subprocess.run", return_value=child), \
                mock.patch.object(output, "TAIL_BYTES", 64), mock.patch.object(self.system, "say") as say:
            self.assertEqual(self.system.capture(("nft",), diagnostics=True), (1, child.stdout))
        say.assert_called_once()
        shown = say.call_args.args[0]
        self.assertIn("[earlier stderr omitted]", shown)
        self.assertIn("Kernel unavailable", shown)
        self.assertLessEqual(len(shown), len("   nft: exit status 1\n[earlier stderr omitted]\n") + 64)
        self.assertNotIn("earlier stderr\n", shown)
        for private in (child.stdout, "\x1b", "\x00", "\u202e", "\r"):
            self.assertNotIn(private, shown)

    def test_timeout_reports_fixed_reason_and_safe_byte_stderr_only(self):
        argv = ("nft", "private-argument")
        expired = subprocess.TimeoutExpired(argv, 5, output=b"private captured firewall",
                                            stderr=b"\x1b[31mKernel still unavailable\x1b[0m\n")
        with mock.patch("oh_install.system.subprocess.run", side_effect=expired), \
                mock.patch.object(self.system, "say") as say:
            self.assertEqual(self.system.capture(argv, timeout=5, diagnostics=True), (124, ""))
        say.assert_called_once()
        shown = say.call_args.args[0]
        self.assertIn("exit status 124 (timed out)", shown)
        self.assertIn("Kernel still unavailable", shown)
        for private in ("private-argument", "private captured firewall", "\x1b"):
            self.assertNotIn(private, shown)

    def test_missing_executable_reports_fixed_reason_without_exception_details(self):
        error = FileNotFoundError(2, "private exception detail", "/private/programs/nft")
        with mock.patch("oh_install.system.subprocess.run", side_effect=error), \
                mock.patch.object(self.system, "say") as say:
            self.assertEqual(self.system.capture(("/private/programs/nft", "private-argument"), diagnostics=True), (127, ""))
        say.assert_called_once_with("   nft: exit status 127 (program not found)")

    def test_executable_label_is_bounded_and_has_no_control_characters(self):
        executable = "/private/\x1b[31mnft\n" + "x" * 200
        child = subprocess.CompletedProcess((executable,), 1, "private output", "")
        with mock.patch("oh_install.system.subprocess.run", return_value=child), \
                mock.patch.object(self.system, "say") as say:
            self.system.capture((executable,), diagnostics=True)
        shown = say.call_args.args[0]
        self.assertLessEqual(len(shown), len("   : exit status 1") + 64)
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\n", shown)
        self.assertNotIn("/private", shown)

    def test_success_keeps_stderr_silent_and_returns_stdout_unchanged(self):
        child = subprocess.CompletedProcess(("nft",), 0, "private captured firewall\n", "successful warning\n")
        with mock.patch("oh_install.system.subprocess.run", return_value=child), \
                mock.patch.object(self.system, "say") as say:
            self.assertEqual(self.system.capture(("nft",), diagnostics=True), (0, child.stdout))
        say.assert_not_called()

    def test_default_nonzero_stdout_remains_available_without_diagnostics(self):
        argv = ("pacman", "-T", "fixture-package")
        child = subprocess.CompletedProcess(argv, 127, "fixture-package\n", "expected package absence\n")
        with mock.patch("oh_install.system.subprocess.run", return_value=child) as run, \
                mock.patch.object(self.system, "say") as say:
            self.assertEqual(self.system.capture(argv), (127, child.stdout))
        run.assert_called_once_with(list(argv), capture_output=True, text=True, check=False, timeout=None)
        say.assert_not_called()

    def test_default_missing_and_timeout_remain_silent(self):
        errors = ((FileNotFoundError(), 127),
                  (subprocess.TimeoutExpired(("child",), 5, output="private output", stderr="private error"), 124))
        for error, code in errors:
            with self.subTest(code=code), mock.patch("oh_install.system.subprocess.run", side_effect=error), \
                    mock.patch.object(self.system, "say") as say:
                self.assertEqual(self.system.capture(("child",)), (code, ""))
            say.assert_not_called()


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
        self.assertFalse(progress.show_command_output.get())
        return result, stream.getvalue()

    def test_already_done_is_one_compact_line_without_color_when_redirected(self):
        result, out = self.run_stage(Stage([Check(True, "long implementation detail")]))
        self.assertEqual(result, 0)
        self.assertEqual(out, "[ OK ] 1. Service ready\n")

    def test_pending_stage_explains_work_before_starting_and_reports_elapsed_time(self):
        stream = io.StringIO()
        detail = "First run downloads the runtime and prepares its cache"

        with mock.patch.object(progress, "monotonic", return_value=100) as clock:
            def work(system):
                self.assertIn(detail, stream.getvalue())
                clock.return_value = 223
                return 0

            stage = Stage([Check(False, detail), Check(True, "verified")], [Task("prepare", work)])
            result, out = self.run_stage(stage, stream)
        self.assertEqual(result, 0)
        self.assertEqual(out.count(detail), 1)
        self.assertIn("[ .. ] 1. Service ready: " + detail, out)
        self.assertIn("[ OK ] 1. Service ready (2m 03s)", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\x1b", out)

    def test_spinner_keeps_elapsed_time_visible_when_title_is_truncated(self):
        stream = Terminal()
        with contextlib.redirect_stdout(stream), \
                mock.patch.object(progress, "monotonic", return_value=100) as clock, \
                mock.patch.object(progress.threading, "Thread"), \
                mock.patch.object(progress.shutil, "get_terminal_size", return_value=os.terminal_size((36, 24))):
            with progress.Progress(System(), "6. Configuration boot check") as display:
                clock.return_value = 223
                display._paint()
                frame = stream.getvalue().split("\r\x1b[2K")[-1]
                self.assertTrue(frame.startswith("[ / ] 6. Configuration"), frame)
                self.assertTrue(frame.endswith(" (2m 03s)"), frame)
                self.assertLessEqual(len(frame), 35)
                display.passed("verified")
        self.assertIn("Configuration boot check (2m 03s)", stream.getvalue())

    def test_tiny_terminal_does_not_wrap_progress_frames(self):
        stream = Terminal()
        with contextlib.redirect_stdout(stream), \
                mock.patch.object(progress.threading, "Thread"), \
                mock.patch.object(progress.shutil, "get_terminal_size", return_value=os.terminal_size((5, 24))):
            with progress.Progress(System(), "Configuration boot check") as display:
                frame = stream.getvalue().split("\r\x1b[2K")[-1]
                self.assertLessEqual(len(frame), 4)
                display.passed("verified")

    def test_quiet_success_does_not_print_work_details_or_duration(self):
        oh_cli.switches.quiet = True
        with mock.patch.object(progress, "monotonic", return_value=100) as clock:
            def work(system):
                clock.return_value = 223
                return 0

            result, out = self.run_stage(Stage([Check(False, "prepare runtime"), Check(True, "verified")],
                                              [Task("prepare", work)]))
        self.assertEqual(result, 0)
        self.assertEqual(out, "")

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

    def test_success_warnings_show_without_pausing_the_noninteractive_command(self):
        stream = Terminal()
        spawn = output.subprocess.Popen

        def launch(*args, **options):
            active = progress.current.get()
            self.assertEqual(active.paused, 0)
            self.assertTrue(progress.capture_command.get())
            self.assertTrue(progress.show_command_output.get())
            self.assertFalse(progress.terminal_command.get())
            self.assertEqual(options["stdin"], output.subprocess.DEVNULL)
            self.assertTrue(options["start_new_session"])
            child = spawn(*args, **options)
            self.assertTrue(stream.rotated.wait(2), stream.getvalue())
            return child

        command = Command(("unused-command",), approved_argv=(sys.executable, "-B", "-c",
                          "print('\\x1b[31mWARNING\\x1b[0m device unavailable')"), show_output=True)
        with mock.patch.object(output.subprocess, "Popen", side_effect=launch):
            result, text = self.run_stage(Stage([Check(False, "check"), Check(True, "ready")], [command]), stream)
        self.assertEqual(result, 0)
        self.assertEqual(text.count("WARNING device unavailable"), 1)
        self.assertIn("✓", text)
        self.assertFalse(any(thread.name == "thermite-progress" for thread in threading.enumerate()))

    def test_quiet_opted_in_success_is_hidden_but_failure_output_remains_once(self):
        oh_cli.switches.quiet = True
        for code in (0, 7):
            with self.subTest(code=code), contextlib.redirect_stderr(io.StringIO()) as error:
                command = Command((sys.executable, "-B", "-c",
                                   f"print('\\x1b[31mWARNING\\x1b[0m device unavailable'); raise SystemExit({code})"),
                                  show_output=True)
                result, text = self.run_stage(Stage([Check(False, "check"), Check(True, "ready")], [command]))
                self.assertEqual(result, code)
                self.assertNotIn("WARNING device unavailable", text)
                self.assertEqual(error.getvalue().count("WARNING device unavailable"), int(bool(code)))

    def test_output_opt_in_resets_after_nested_context_and_exception(self):
        self.assertFalse(progress.show_command_output.get())
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            with progress.command(terminal=False, show_output=True):
                self.assertTrue(progress.show_command_output.get())
                with progress.command(terminal=True):
                    self.assertFalse(progress.show_command_output.get())
                self.assertTrue(progress.show_command_output.get())
                raise RuntimeError("cancel")
        self.assertFalse(progress.show_command_output.get())
        self.assertFalse(progress.capture_command.get())
        self.assertFalse(progress.terminal_command.get())

    def test_verbose_opted_in_command_keeps_inherited_streams(self):
        oh_cli.switches.verbose = True
        command = Command(("visible-child",), show_output=True)

        def child(argv, **options):
            self.assertFalse(progress.capture_command.get())
            self.assertTrue(progress.show_command_output.get())
            self.assertNotIn("stdin", options)
            self.assertNotIn("stdout", options)
            self.assertNotIn("stderr", options)
            return mock.Mock(returncode=0)

        with mock.patch("oh_install.system.subprocess.run", side_effect=child), \
                mock.patch.object(output, "run") as capture:
            result, _ = self.run_stage(Stage([Check(False, "check"), Check(True, "ready")], [command]))
        self.assertEqual(result, 0)
        capture.assert_not_called()

    def test_root_command_resumes_animation_after_password_validation(self):
        stream = Terminal()

        def authenticate(argv, **options):
            self.assertEqual(argv, ["sudo", "-v"])
            self.assertGreater(progress.current.get().paused, 0)
            self.assertNotIn("stdin", options)
            return mock.Mock(returncode=0)

        def execute(argv, cwd, **options):
            self.assertEqual(argv, ("sudo", "-n", "systemctl", "stop", "example.service"))
            self.assertEqual(progress.current.get().paused, 0)
            self.assertTrue(options["keep_terminal"])
            self.assertTrue(stream.rotated.wait(2), stream.getvalue())
            return 0

        stage = Stage([Check(False, "stopping"), Check(True, "stopped")],
                      [Command(("systemctl", "stop", "example.service"), root=True)])
        with mock.patch.object(System, "interactive", return_value=True), \
                mock.patch("oh_install.system.subprocess.run", side_effect=authenticate) as validation, \
                mock.patch("oh_install.output.run", side_effect=execute) as command:
            result, text = self.run_stage(stage, stream)
        self.assertEqual(result, 0)
        self.assertIn("✓", text)
        self.assertEqual((validation.call_count, command.call_count), (1, 1))

    def test_failed_authentication_never_runs_the_root_payload(self):
        stage = Stage([Check(False, "stopping")], [Command(("systemctl", "stop", "example.service"), root=True)])
        with mock.patch.object(System, "interactive", return_value=True), \
                mock.patch("oh_install.system.subprocess.run", return_value=mock.Mock(returncode=1)), \
                mock.patch("oh_install.output.run") as command:
            result, text = self.run_stage(stage)
        self.assertEqual(result, 1)
        command.assert_not_called()
        self.assertIn("[ FAIL ]", text)

    def test_root_payload_failure_is_not_retried(self):
        stage = Stage([Check(False, "stopping")], [Command(("systemctl", "stop", "example.service"), root=True)])
        with mock.patch.object(System, "interactive", return_value=True), \
                mock.patch("oh_install.system.subprocess.run", return_value=mock.Mock(returncode=0)) as validation, \
                mock.patch("oh_install.output.run", return_value=1) as command:
            result, _ = self.run_stage(stage)
        self.assertEqual(result, 1)
        self.assertEqual((validation.call_count, command.call_count), (1, 1))

    def test_explicit_interactive_root_command_keeps_the_terminal(self):
        stage = Stage([Check(False, "packages"), Check(True, "installed")],
                      [Command(("package-manager",), root=True, terminal=True)])
        with mock.patch.object(System, "interactive", return_value=True), \
                mock.patch("oh_install.system.subprocess.run", return_value=mock.Mock(returncode=0)) as command, \
                mock.patch("oh_install.output.run") as capture:
            result, _ = self.run_stage(stage)
        self.assertEqual(result, 0)
        self.assertEqual(command.call_args.args[0], ["sudo", "package-manager"])
        capture.assert_not_called()

    def test_root_authentication_and_command_share_one_startup_budget(self):
        from oh_install.steps import _RestartBeforeDeadline

        for elapsed, expected in ((3, 7), (10, None)):
            with self.subTest(elapsed=elapsed):
                system = System()
                with contextlib.redirect_stdout(io.StringIO()) as text, \
                        mock.patch.object(system, "clock", side_effect=[100, 100, 100 + elapsed]), \
                        mock.patch("oh_install.system.subprocess.run", return_value=mock.Mock(returncode=0)) as validation, \
                        mock.patch("oh_install.output.run", return_value=0) as command:
                    with progress.Progress(system, "Native readiness") as display, progress.command(terminal=False):
                        code = _RestartBeforeDeadline(system, 110).run(("sudo", "systemctl", "restart", "example.service"), AREA)
                        display.finish("OK" if code == 0 else "FAIL")
                self.assertEqual(validation.call_args.kwargs["timeout"], 10)
                if expected is None:
                    self.assertEqual(code, 124)
                    command.assert_not_called()
                    self.assertIn("no command was run", text.getvalue())
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(command.call_args.kwargs["timeout"], expected)


class ReadinessCompletion(unittest.TestCase):
    kinds = (steps.Answers, steps.MetricsAnswer, steps.NativeReady)

    def setUp(self):
        for patch in (mock.patch.object(oh_cli, "switches", oh_cli.Switches(verbose=True)),
                      mock.patch.object(steps, "WAIT", 10), mock.patch.object(steps, "METRICS_WAIT", 10),
                      mock.patch.object(steps, "POLL", 5)):
            patch.start()
            self.addCleanup(patch.stop)

    def fixture(self, kind, states):
        context = SimpleNamespace(url="http://127.0.0.1:13601", area=AREA,
                                  token_file=Path("fictional-api-token"), host={}, address="192.0.2.10",
                                  hostname="fixture", grafana_url="http://127.0.0.1:13610/",
                                  prepared={"conf/services/addons.cfg": SimpleNamespace(data=b"binding=http\nmisc=metrics\n")})
        system = mock.Mock(spec=System)
        now = [0.0]
        system.clock.side_effect = lambda: now[0]
        system.sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
        system.interactive.return_value = False
        system.read_private.return_value = "fictional-fixture-token"
        if kind is steps.Answers:
            probe, pending, ready = system.openhab_version, None, ["5.2.3"]
        elif kind is steps.MetricsAnswer:
            pending = MetricsHealth("http://127.0.0.1:13606", None, None, None, (), context.grafana_url, None)
            ready = [MetricsHealth("http://127.0.0.1:13606", 200, 2, 2, (), context.grafana_url, 200, True)]
            probe = system.metrics_health
        else:
            probe, pending = system.native_probe, NativeProbe(0)
            ready = [NativeProbe(200, [{"uid": "binding-http", "installed": True},
                                       {"uid": "misc-metrics", "installed": True}]),
                     NativeProbe(200, []), NativeProbe(200)]
        answers = iter([answer for state in states for answer in (ready if state == "ready" else [pending])])
        probe.side_effect = lambda *args, **kwargs: next(answers, pending)
        return kind(context), system, probe, now

    @staticmethod
    def sentinel():
        step = mock.Mock(spec=Step)
        step.title, step.always = "Following stage", False
        step.check.return_value = Check(True, "following stage reached")
        return step

    def run_steps(self, sequence, system, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(sequence, system, AREA, **kwargs)
        self.assertIsNone(progress.current.get())
        return result

    def test_polling_success_reaches_next_step_without_a_duplicate_probe(self):
        for kind in self.kinds:
            with self.subTest(step=kind.__name__):
                step, system, probe, now = self.fixture(kind, ("pending", "ready", "pending"))
                following = self.sentinel()
                self.assertEqual(self.run_steps([step, following], system), 0)
                following.check.assert_called_once_with(system)
                expected = 4 if kind is steps.NativeReady else 2
                self.assertEqual(probe.call_count, expected)
                self.assertEqual(now[0], 0)
                messages = "\n".join(call.args[0] for call in system.say.call_args_list)
                detail = {steps.Answers: "openHAB 5.2.3", steps.MetricsAnswer: "targets 2/2 up",
                          steps.NativeReady: "authenticated native metrics answer"}[kind]
                self.assertIn(detail, messages)
                if kind is steps.NativeReady:
                    self.assertEqual([call.args[2] for call in probe.call_args_list],
                                     [step.addons_path, step.addons_path, step.things_path, step.metrics_path])
                    self.assertEqual({call.args[1] for call in probe.call_args_list}, {"fictional-fixture-token"})
                    self.assertEqual({call.args[3] for call in probe.call_args_list}, {10})
                self.assertFalse(step.check(system).done, "ordinary checks must read the host again")
                self.assertEqual(probe.call_count, expected + 1)
                self.assertFalse(step.after_actions(system).done, "the successful observation must be consumed only once")
                self.assertEqual(probe.call_count, expected + 2)

    def test_completed_wait_does_not_hide_failure_from_a_dry_run_or_later_run(self):
        for kind in self.kinds:
            with self.subTest(step=kind.__name__):
                step, system, probe, now = self.fixture(kind, ("pending", "ready"))
                self.assertEqual(self.run_steps([step], system), 0)
                calls = probe.call_count
                system.say.reset_mock()
                self.assertEqual(self.run_steps([step], system, dry_run=True), 0)
                self.assertEqual(probe.call_count, calls + 1)
                self.assertEqual(now[0], 0, "a dry run must not wait")
                self.assertTrue(any("would" in call.args[0] for call in system.say.call_args_list))
                following = self.sentinel()
                self.assertEqual(self.run_steps([step, following], system), 1)
                following.check.assert_not_called()
                self.assertEqual(now[0], 10, "later failure must use the startup allowance, not cached success")

    def test_persistent_readiness_failure_uses_the_full_allowance_and_stops_the_runner(self):
        for kind in self.kinds:
            with self.subTest(step=kind.__name__):
                step, system, probe, now = self.fixture(kind, ("pending",))
                following = self.sentinel()
                self.assertEqual(self.run_steps([step, following], system), 1)
                following.check.assert_not_called()
                self.assertEqual(now[0], 10)
                self.assertEqual([call.args[0] for call in system.sleep.call_args_list], [5, 5])
                system.run.assert_not_called()

    def test_a_failed_new_wait_clears_an_unconsumed_success(self):
        for kind in self.kinds:
            with self.subTest(step=kind.__name__):
                step, system, probe, now = self.fixture(kind, ("ready",))
                self.assertEqual(step.wait(system), 0)
                self.assertEqual(step.wait(system), 1)
                self.assertEqual(now[0], 10)
                self.assertFalse(step.after_actions(system).done)

    def test_native_authentication_and_configuration_failures_cannot_complete_readiness(self):
        for fault in ("authentication", "configuration"):
            with self.subTest(fault=fault):
                step, system, probe, now = self.fixture(steps.NativeReady, ())
                replies = [NativeProbe(0)]
                if fault == "authentication":
                    replies.append(NativeProbe(401))
                else:
                    replies.extend((NativeProbe(200, [{"uid": "binding-http", "installed": True},
                                                      {"uid": "misc-metrics", "installed": True}]),
                                    NativeProbe(200, [{"UID": "http:fixture:device", "statusInfo": {
                                        "status": "OFFLINE", "statusDetail": "CONFIGURATION_ERROR"}}]),
                                    NativeProbe(200)))
                probe.side_effect = replies
                following = self.sentinel()
                self.assertEqual(self.run_steps([step, following], system), 1)
                following.check.assert_not_called()
                system.run.assert_not_called()
                self.assertEqual(now[0], 0, "terminal errors must not be polled until the deadline")
                messages = "\n".join(call.args[0] for call in system.say.call_args_list)
                self.assertIn("HTTP 401" if fault == "authentication" else "configuration error", messages)


if __name__ == "__main__":
    unittest.main()
