"""Watched control/error snapshots cannot supersede newer native events."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is required for the actual shared backend")
class ItemReadOrdering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([NODE, str(AREA / "tests/backend_item_read_order_harness.js"), str(AREA)],
                             text=True, capture_output=True, timeout=10)
        cls.result = json.loads(run.stdout)
        cls.exit_code = run.returncode

    def test_native_power_and_sticky_error_events_win_over_older_get(self):
        self.assertTrue(self.result["cases"]["new_power_and_sticky_error_sse_win"]["pass"], self.result)

    def test_unchanged_native_events_also_invalidate_older_get(self):
        self.assertTrue(self.result["cases"]["same_value_sse_invalidates_older_get"]["pass"], self.result)

    def test_newer_get_supersedes_an_older_completion(self):
        self.assertTrue(self.result["cases"]["newer_get_supersedes_older_get"]["pass"], self.result)

    def test_page_and_stream_lifecycles_retire_held_gets(self):
        for lifecycle in ("hide_resume", "stop", "stream_restart"):
            with self.subTest(lifecycle=lifecycle):
                self.assertTrue(self.result["cases"][lifecycle + "_rejects_old_get"]["pass"], self.result)

    def test_each_independent_control_field_accepts_current_readback(self):
        self.assertTrue(self.result["cases"]["independent_items_keep_current_get"]["pass"], self.result)

    def test_actual_recovery_can_clear_an_old_problem(self):
        self.assertTrue(self.result["cases"]["fresh_get_updates_and_clears_recovered_error"]["pass"], self.result)

    def test_inactive_and_stopped_clients_do_not_issue_control_reads(self):
        self.assertTrue(self.result["cases"]["inactive_or_stopped_read_all_issues_no_requests"]["pass"], self.result)

    def test_entire_protocol_fixture_has_zero_real_actions(self):
        self.assertEqual((self.exit_code, self.result["actualNetworkRequests"], self.result["actualDeviceCommands"]), (0, 0, 0))
