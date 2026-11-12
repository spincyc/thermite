"""Real local subprocesses exercise bounded install output and cancellation."""

from pathlib import Path
import os
import json
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import unicodedata
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_install import output


class InstallOutput(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA.parents[1] / ".scratch/install_output_capture"))
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="capture-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reports = []

    def run_child(self, script, *, timeout=5, show_output=False):
        return output.run((sys.executable, "-B", "-c", script), self.root,
                          timeout=timeout, report=self.reports.append, show_output=show_output)

    def assert_descendant_stopped(self):
        found = re.search(r"descendant=(\d+)", "\n".join(self.reports))
        self.assertIsNotNone(found, self.reports)
        pid = int(found[1])
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            ready, _, _ = select.select([descriptor], [], [], 2)
            self.assertTrue(ready, f"descendant {pid} remained alive")
        finally:
            os.close(descriptor)

    def assert_no_reader_left(self):
        self.assertFalse(any(thread.name == "thermite-child-output" for thread in threading.enumerate()))

    def test_success_discards_output_uses_cwd_and_cannot_read_terminal_input(self):
        code = self.run_child("import os,sys; assert os.getcwd()==" + repr(str(self.root))
                              + "; assert sys.stdin.read()==''; print('routine output'); print('routine error stream',file=sys.stderr)")
        self.assertEqual(code, 0)
        self.assertEqual(self.reports, [])
        self.assert_no_reader_left()

    def test_opted_in_success_reports_sanitized_output_without_terminal_input_or_session(self):
        script = ("import os,sys; assert sys.stdin.read()==''; assert os.getsid(0)==os.getpid(); "
                  "os.write(1,b'\\x1b[31mWARNING\\x1b[0m device unavailable\\n'); "
                  "os.write(2,b'\\x1b]0;hidden-title\\x07second diagnostic\\x00\\n')")
        self.assertEqual(self.run_child(script, show_output=True), 0)
        self.assertEqual(self.reports, ["WARNING device unavailable\nsecond diagnostic"])
        self.assert_no_reader_left()

    def test_opted_in_success_remains_bounded_and_failure_is_reported_only_once(self):
        self.assertEqual(self.run_child("import os; os.write(1,b'x'*200000); print('\\nFINAL WARNING')",
                                        show_output=True), 0)
        self.assertEqual(len(self.reports), 1)
        self.assertLessEqual(len(self.reports[0]), output.TAIL_BYTES + 80)
        self.assertTrue(self.reports[0].startswith("[earlier child output omitted]"))
        self.assertTrue(self.reports[0].endswith("FINAL WARNING"))
        self.reports.clear()
        self.assertEqual(self.run_child("print('single failure diagnostic'); raise SystemExit(7)", show_output=True), 7)
        self.assertEqual(self.reports, ["single failure diagnostic"])

    def test_a_result_on_stdout_is_discarded_and_warnings_on_stderr_show(self):
        # The install engine: a JSON result nobody reads here, and warnings.
        script = ("import sys; print('{\"phase\": \"committed\"}'); "
                  "print('warning: systemctl start thermite.target failed', file=sys.stderr)")
        self.assertEqual(self.run_child(script, show_output="stderr"), 0)
        self.assertEqual(self.reports, ["warning: systemctl start thermite.target failed"])
        self.reports.clear()
        self.assertEqual(self.run_child("print('{}'); raise SystemExit(3)", show_output="stderr"), 3)
        self.assertEqual(self.reports, [], "a quiet engine says nothing, even with a nonzero outcome")
        self.assert_no_reader_left()

    def test_large_stdout_and_stderr_are_drained_and_only_bounded_failure_tail_is_reported(self):
        code = self.run_child("""import os,sys
os.write(1,b'early output must be discarded\\n')
for _ in range(256):
    os.write(1,b'x'*8192)
    os.write(2,b'y'*8192)
os.write(1,b'\\nLAST STDOUT\\n')
os.write(2,b'LAST STDERR\\n')
sys.exit(7)
""")
        self.assertEqual(code, 7)
        self.assertEqual(len(self.reports), 1)
        self.assertLessEqual(len(self.reports[0]), output.TAIL_BYTES + 80)
        self.assertNotIn("early output must be discarded", self.reports[0])
        self.assertIn("LAST STDOUT", self.reports[0])
        self.assertIn("LAST STDERR", self.reports[0])
        self.assert_no_reader_left()

    def test_failure_output_cannot_rewrite_terminal_or_apply_ansi_sequences(self):
        unsafe = "\x1b[31mERROR\x1b[0m\x1b]0;title injection\x07\x1bPprivate sequence\x1b\\\rnext\x08\x00\u202e line\n"
        self.assertEqual(self.run_child("import os; os.write(2," + repr(unsafe.encode()) + "); raise SystemExit(2)"), 2)
        text = "\n".join(self.reports)
        self.assertIn("ERROR", text)
        self.assertNotIn("title injection", text)
        self.assertNotIn("private sequence", text)
        self.assertTrue(all(char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Cs") for char in text))

    def test_missing_program_preserves_file_not_found_error(self):
        with self.assertRaises(FileNotFoundError):
            output.run((str(self.root / "missing-program"),), self.root, report=self.reports.append)
        self.assertEqual(self.reports, [])
        self.assert_no_reader_left()

    def test_terminal_preserving_capture_keeps_session_and_parent_with_its_own_group(self):
        script = ("import json,os; print(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),"
                  "'sid':os.getsid(0),'pgid':os.getpgrp()})); raise SystemExit(7)")
        code = output.run((sys.executable, "-B", "-c", script), self.root,
                          report=self.reports.append, keep_terminal=True)
        self.assertEqual(code, 7)
        child = json.loads(self.reports[0])
        self.assertEqual(child["ppid"], os.getpid())
        self.assertEqual(child["sid"], os.getsid(0))
        self.assertEqual(child["pgid"], child["pid"])
        self.assertNotEqual(child["pgid"], os.getpgrp())
        self.assert_no_reader_left()

    def test_reader_start_failure_stops_the_already_created_child_and_closes_its_pipe(self):
        children = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            children.append(process)
            return process
        with mock.patch.object(output.subprocess, "Popen", side_effect=spawn), \
                mock.patch.object(output.threading.Thread, "start", side_effect=RuntimeError("no reader available")):
            with self.assertRaisesRegex(RuntimeError, "no reader available"):
                self.run_child("import time; time.sleep(60)")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(children[0].stdout.closed)
        self.assert_no_reader_left()

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfds verify descendant retirement")
    def test_timeout_reports_diagnostics_and_kills_descendants_even_when_the_leader_exits(self):
        script = """import os,signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(60)'])
print('descendant='+str(child.pid),flush=True)
time.sleep(60)
"""
        with mock.patch.object(output, "STOP_GRACE", 0.2), self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_child(script, timeout=0.3)
        self.assertEqual(caught.exception.timeout, 0.3)
        self.assert_descendant_stopped()
        self.assert_no_reader_left()

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfds verify descendant retirement")
    def test_keyboard_interrupt_cleans_up_group_then_reraises_on_the_main_thread(self):
        script = """import os,signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(60)'])
print('descendant='+str(child.pid),flush=True)
os.kill(os.getppid(),signal.SIGINT)
time.sleep(60)
"""
        previous = signal.getsignal(signal.SIGINT)
        with mock.patch.object(output, "STOP_GRACE", 0.2), self.assertRaises(KeyboardInterrupt):
            self.run_child(script)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assert_descendant_stopped()
        self.assert_no_reader_left()

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfds verify descendant retirement")
    def test_parent_exit_cannot_leave_a_descendant_holding_the_output_pipe_forever(self):
        script = """import subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
print('descendant='+str(child.pid),flush=True)
"""
        with mock.patch.object(output, "STOP_GRACE", 0.2):
            self.assertEqual(self.run_child(script), 1)
        self.assertIn("descendants holding", "\n".join(self.reports))
        self.assert_descendant_stopped()
        self.assert_no_reader_left()

    def test_timeout_allows_boot_length_finally_cleanup_before_reraising(self):
        script = """import time
try:
    print('CHECK_STARTED',flush=True)
    time.sleep(60)
finally:
    time.sleep(3.2)
    print('FINALLY_CLEANUP_COMPLETED',flush=True)
"""
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_child(script, timeout=1.0)
        self.assertEqual(caught.exception.timeout, 1.0)
        self.assertIn("FINALLY_CLEANUP_COMPLETED", "\n".join(self.reports))
        self.assert_no_reader_left()


if __name__ == "__main__":
    unittest.main()
