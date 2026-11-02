"""Bounded boot-report readiness with mocked HTTP, time and JVM lifecycle."""

import argparse
import collections
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_sandbox as lib
from oh_metrics import dashboards
from oh_testing import boot

ROOT = "/rest/"
SYSTEM = "/rest/systeminfo"
ADDONS = "/rest/addons?serviceId=all"
THINGS = boot.THINGS_PATH
ITEMS = "/rest/items?recursive=false&fields=name,type"
ROUTES = (ROOT, SYSTEM, ADDONS, THINGS, ITEMS, boot.METRICS_PATH)


def thing(status="ONLINE", detail="NONE"):
    return {"UID": "http:url:example", "statusInfo": {"status": status, "statusDetail": detail}}


class BootReadiness(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.conf = root / "conf"
        (self.conf / "services").mkdir(parents=True)
        (self.conf / "services/addons.cfg").write_text("binding=http\nmisc=metrics\n")
        self.inst = lib.Instance("boot-check", root / "state", self.conf, root / "state/userdata",
                                 13618, 13619, 13620, shutdown_port=13625)
        self.inst.userdata.mkdir(parents=True)
        self.rt = lib.Runtime("5.2.3", root / "distribution", root / "jre")
        self.args = argparse.Namespace(conf=self.conf, rebuild=False, timeout=20, settle=2, stall=5,
                                       require_online=True, keep=False, json=False)
        self.reset()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(boot.time, "monotonic", side_effect=lambda: self.now))
        stack.enter_context(mock.patch.object(boot.time, "sleep", side_effect=self.sleep))
        stack.enter_context(mock.patch.object(lib, "running_pgid", return_value=None))
        self.start = stack.enter_context(mock.patch.object(lib, "boot", side_effect=self.booted))
        self.stop = stack.enter_context(mock.patch.object(lib, "stop_instance", return_value="stopped"))
        stack.enter_context(mock.patch.object(lib, "start_level", return_value=100))
        stack.enter_context(mock.patch.object(lib.subprocess, "Popen", side_effect=AssertionError("no JVM permitted")))
        def request(client, method, path, body=None, headers=None):
            self.asked.append((path, self.now, client.timeout))
            self.calls[path] += 1
            if self.handler:
                return self.handler(path)
            return self.response(path)
        stack.enter_context(mock.patch.object(lib.Http, "request", request))

    def reset(self):
        self.now, self.asked, self.calls, self.handler = 0.0, [], collections.Counter(), None
        self.payloads = {
            ROOT: {"runtimeInfo": {"version": "5.2.3"}},
            SYSTEM: {"systemInfo": {"javaVendorVersion": "Temurin-21.0.13+9"}},
            ADDONS: [{"uid": "binding-http", "installed": True}, {"uid": "misc-metrics", "installed": True}],
            THINGS: [thing()], ITEMS: [{"name": "example_power", "type": "Switch"}],
        }
        self.exposition = "".join(name + " 1\n" for name in sorted(set(dashboards.OPENHAB_METRICS.values()))).encode()

    def sleep(self, seconds):
        self.now += seconds

    def booted(self, *args, **kwargs):
        return lib.Boot(123, "synthetic test bearer", set(), False, {"add-ons": self.now})

    def response(self, path):
        return 200, self.exposition if path == boot.METRICS_PATH else json.dumps(self.payloads[path]).encode()

    def run_check(self):
        return boot.run_check(self.args, self.rt, self.inst, log=lambda message: None)

    def test_every_unavailable_collection_route_discards_snapshot_and_requires_fresh_settling(self):
        for route in ROUTES:
            for status in (404, 503, 0):
                with self.subTest(route=route, status=status):
                    self.reset()
                    state = {"collecting": False, "failed": False}
                    def unavailable(path):
                        if path == ROOT:
                            state["collecting"] = True
                        if state["collecting"] and not state["failed"] and path == route:
                            state.update(collecting=False, failed=True)
                            if status == 0:
                                raise urllib.error.URLError("private upstream sentinel")
                            raise urllib.error.HTTPError("http://private.invalid/secret", status, "private sentinel", {}, None)
                        code, data = self.response(path)
                        if not state["failed"]:
                            stale = {ROOT: {"runtimeInfo": {"version": "stale-version"}},
                                     SYSTEM: {"systemInfo": {"javaVendorVersion": "stale-java"}},
                                     ADDONS: [], ITEMS: []}
                            if state["collecting"]:
                                stale[THINGS] = [{**thing(), "UID": "http:url:stale"}]
                            if path in stale:
                                data = json.dumps(stale[path]).encode()
                        return code, data
                    self.handler = unavailable
                    report = self.run_check()
                    self.assertTrue(state["failed"])
                    self.assertGreaterEqual(self.now, 5)  # Re-start the two-second quiet window after the outage.
                    self.assertEqual(report["openhab_version"], "5.2.3")
                    self.assertEqual(report["java"], "Temurin-21.0.13+9")
                    self.assertEqual(report["addons"]["missing"], [])
                    self.assertEqual((report["things"]["count"], report["items"]["count"]), (1, 1))
                    self.assertEqual(report["things"]["things"][0]["uid"], "http:url:example")
                    self.assertEqual(report["metrics"]["http"], 200)
                    self.assertEqual(report["failures"], [])
                    self.assertEqual(report["things_outcome"], "settled")
                    self.assertEqual(self.calls[ROOT], 2)

    def test_repeated_outages_end_at_original_deadline_and_cli_returns_two_without_traceback(self):
        self.args.timeout = 3
        def unavailable(path):
            raise urllib.error.HTTPError(path, 404, "private body sentinel", {}, None)
        self.handler = unavailable
        with mock.patch.object(lib, "load_versions", return_value={}), \
                mock.patch.object(lib, "boot_check_instance", return_value=self.inst), \
                mock.patch.object(lib, "mirror_conf"), mock.patch.object(lib, "fetch", return_value=self.rt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as stopped:
                boot.run(self.args)
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.now, 3)
        self.assertIn("deadline", errors.getvalue())
        self.assertIn(THINGS, errors.getvalue())
        self.assertIn("404", errors.getvalue())
        self.assertNotIn("sentinel", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.stop.assert_called_once_with(self.inst, timeout=3, log=mock.ANY)

    def test_terminal_status_and_malformed_json_or_shapes_are_clean_and_not_retried(self):
        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                self.reset()
                def refused(path):
                    raise urllib.error.HTTPError("http://private.invalid/secret", status, "private body sentinel", {}, None)
                self.handler = refused
                with self.assertRaises(boot.ReportError) as failed:
                    self.run_check()
                self.assertNotIsInstance(failed.exception, boot.ReportUnavailable)
                self.assertIn(f"HTTP {status}", str(failed.exception))
                self.assertNotIn("sentinel", str(failed.exception))
                self.assertNotIn("private.invalid", str(failed.exception))
                self.assertEqual(len(self.asked), 1)
        malformed = {ROOT: [], SYSTEM: {"systemInfo": {}}, ADDONS: [{"installed": "yes"}],
                     THINGS: [{"UID": "bad", "statusInfo": {"status": 7}}], ITEMS: [{"name": "bad"}]}
        for route, invalid in malformed.items():
            with self.subTest(route=route):
                self.reset()
                self.payloads[route] = invalid
                client = boot.ReportHttp(self.inst.url, "synthetic test bearer", 20)
                with self.assertRaisesRegex(boot.ReportError, "invalid JSON shape"):
                    client.get_json(route)
                self.assertEqual(len(self.asked), 1)
        self.handler = lambda path: (200, b"private malformed body sentinel")
        with self.assertRaisesRegex(boot.ReportError, "malformed JSON") as failed:
            boot.ReportHttp(self.inst.url, None, 20).get_json(ROOT)
        self.assertNotIn("sentinel", str(failed.exception))

    def test_query_failure_does_not_append_private_upstream_log_lines(self):
        self.inst.openhab_log.parent.mkdir()
        self.inst.openhab_log.write_text("private log body sentinel\n")
        def refused(path):
            raise urllib.error.HTTPError(path, 401, "private response sentinel", {}, None)
        self.handler = refused
        with self.assertRaisesRegex(boot.ReportError, "HTTP 401") as failed:
            self.run_check()
        self.assertNotIn("sentinel", str(failed.exception))
        self.assertNotIn("last lines", str(failed.exception))

    def test_complete_failed_snapshots_are_returned_without_retry(self):
        for failure in ("thing", "addons", "metrics", "model"):
            with self.subTest(failure=failure):
                self.reset()
                if failure == "thing":
                    self.payloads[THINGS] = [thing("OFFLINE", "COMMUNICATION_ERROR")]
                elif failure == "addons":
                    self.payloads[ADDONS] = []
                elif failure == "metrics":
                    self.exposition = b"unrelated_metric 1\n"
                record = types.SimpleNamespace(lineno=1, ts="synthetic", level="ERROR", logger="model", message="model failed")
                problems = {"model": [record] if failure == "model" else [], "errors": []}
                with mock.patch.object(lib, "log_problems", return_value=problems):
                    report = self.run_check()
                self.assertTrue(report["failures"])
                self.assertEqual(self.calls[ROOT], 1)
                self.assertEqual(self.calls[boot.METRICS_PATH], 1)
                self.assertEqual(self.now, 2)

    def test_default_pending_device_warning_and_strict_online_failure_are_preserved(self):
        for required in (False, True):
            self.reset()
            self.args.require_online = required
            self.args.stall = 2
            self.payloads[THINGS] = [thing("INITIALIZING")]
            report = self.run_check()
            self.assertEqual(bool(report["things"]["failed"]), required)
            self.assertEqual(bool(report["failures"]), required)

    def test_stalled_outcome_never_resumes_settling_after_snapshot_outage(self):
        self.args.stall, self.args.settle = 2, 100
        self.payloads[THINGS] = [thing("UNINITIALIZED", "HANDLER_MISSING_ERROR")]
        def recovering(path):
            if path == ROOT and self.calls[ROOT] == 1:
                self.payloads[THINGS] = [thing()]
                raise urllib.error.HTTPError(path, 404, "private sentinel", {}, None)
            return self.response(path)
        self.handler = recovering
        report = self.run_check()
        self.assertEqual(report["things_outcome"], "stalled")
        self.assertIn("Things stalled for 2s", report["failures"])
        self.assertEqual(report["timings"]["things stalled"], 2)
        self.assertEqual(self.now, 3)
        self.assertEqual(self.calls[THINGS], 4)  # Three polls, then only the successful report read.

    def test_request_timeout_is_remaining_budget_and_late_response_cannot_pass(self):
        self.now = 9
        def late(path):
            self.now = 11
            return self.response(path)
        self.handler = late
        client = boot.ReportHttp(self.inst.url, None, 10)
        with self.assertRaisesRegex(boot.ReportError, "deadline"):
            client.get_json(ROOT)
        self.assertEqual(self.asked, [(ROOT, 9, 1)])
        self.asked.clear()
        with self.assertRaisesRegex(boot.ReportError, "deadline"):
            client.get_json(ROOT)
        self.assertEqual(self.asked, [])

    def test_deadline_expiring_during_final_metrics_request_cannot_return_report(self):
        def late_metrics(path):
            if path == boot.METRICS_PATH:
                self.now = self.args.timeout
            return self.response(path)
        self.handler = late_metrics
        with self.assertRaisesRegex(boot.ReportError, "deadline.*metrics"):
            self.run_check()
        self.assertEqual(self.calls[ROOT], 1)
        self.assertEqual(self.now, self.args.timeout)
        self.stop.assert_called_once_with(self.inst, timeout=3, log=mock.ANY)


if __name__ == "__main__":
    unittest.main()
