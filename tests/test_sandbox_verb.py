"""thermite sandbox status --json, and sandbox logs' retirement.

No runtime: the sandbox's process, REST API and files are fakes.
"""
from pathlib import Path
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
from oh_cli import sandbox

WARNING = "thermite sandbox logs: thermite logs --sandbox replaces it; thermite sandbox logs goes in the next release\n"


class Sandbox(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR") or AREA / ".scratch")
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="sandbox-verb-", dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inst = types.SimpleNamespace(url="http://127.0.0.1:13616", http_port=13616,
                                          token_file=self.root / "api-token",
                                          openhab_log=self.root / "userdata/logs/openhab.log")
        self.enterContext(mock.patch.object(sandbox.lib, "sandbox_instance", return_value=self.inst))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = sandbox.main(list(argv))
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def running(self, answer=(200, {"runtimeInfo": {"version": "5.1.0"}}), mode=sandbox.lib.READ_ONLY):
        http = mock.Mock(try_get_json=mock.Mock(return_value=answer))
        for name, value in (("running_pgid", 4242), ("group_members", [4242, 4243, 4244]), ("read_mode", mode),
                            ("start_level", 100), ("read_token", "synthetic")):
            self.enterContext(mock.patch.object(sandbox.lib, name, return_value=value))
        self.enterContext(mock.patch.object(sandbox.lib, "Http", return_value=http))
        self.enterContext(mock.patch.object(sandbox.deploy, "lan_address", return_value="192.0.2.30"))

    def test_status_json_is_one_object_of_what_the_text_says(self):
        self.running()
        self.inst.token_file.write_text("synthetic")
        status, out, err = self.run_main("status", "--json")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out), {
            "running": True, "process_group": 4242, "processes": 3, "url": "http://127.0.0.1:13616",
            "rest": {"status": 200, "version": "5.1.0"}, "mode": sandbox.lib.READ_ONLY, "lan_url": None,
            "start_level": 100, "token_file": str(self.inst.token_file), "log": str(self.inst.openhab_log)})
        status, text, _ = self.run_main("status")
        self.assertEqual(status, 0)
        self.assertIn("sandbox: running (process group 4242, 3 processes)", text)
        self.assertIn("url:     http://127.0.0.1:13616  REST ok, runtime 5.1.0", text)

    def test_status_json_of_a_live_sandbox_whose_rest_does_not_answer(self):
        self.running(answer=(None, None), mode=sandbox.lib.LIVE)
        status, out, _ = self.run_main("status", "--json")
        self.assertEqual(status, 1)
        report = json.loads(out)
        self.assertEqual((report["rest"], report["lan_url"], report["token_file"]),
                         ({"status": None, "version": None}, "http://192.0.2.30:13616", None))

    def test_status_json_of_a_stopped_sandbox(self):
        with mock.patch.object(sandbox.lib, "running_pgid", return_value=None):
            status, out, _ = self.run_main("status", "--json")
            self.assertEqual(status, 3)
            self.assertEqual(json.loads(out), {"running": False, "url": "http://127.0.0.1:13616",
                                               "log": str(self.inst.openhab_log)})
            self.assertEqual(self.run_main("status")[1:], ("sandbox: stopped\n", ""))

    def test_logs_says_what_replaces_it_then_shows_the_log_as_before(self):
        self.inst.openhab_log.parent.mkdir(parents=True)
        self.inst.openhab_log.write_text("".join(f"line {n}\n" for n in range(1, 61)))
        status, out, err = self.run_main("logs", "-n", "2")
        self.assertEqual((status, out, err), (0, "line 59\nline 60\n", WARNING))
        sandbox.lib.sandbox_instance.assert_called_with(userdata=None)
        status, out, err = self.run_main("logs")
        self.assertEqual((status, out.splitlines()[0], err), (0, "line 11", WARNING))

    def test_logs_is_listed_as_retired(self):
        text = oh_cli.parser_of("sandbox").format_help()
        self.assertIn("(retired: thermite logs --sandbox)", text)
        self.assertNotIn("==SUPPRESS==", text)
        status, text, _ = self.run_main("logs", "--help")
        self.assertEqual(status, 0)
        self.assertIn("thermite logs --sandbox", text)
        self.assertNotIn("/srv/thermite/runtime/userdata", text)


if __name__ == "__main__":
    unittest.main()
