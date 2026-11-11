"""One event stream per page: its snapshot replaces the startup reads."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is required for the actual shared backend")
class BackendStream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([NODE, str(AREA / "tests/backend_stream_harness.js"), str(AREA)],
                             text=True, capture_output=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout)
        cls.proof = json.loads(run.stdout)
        cls.cases = cls.proof["cases"]

    def test_a_page_opens_one_stream_and_reads_no_item_on_open(self):
        opened = self.cases["remote_page_one_stream_snapshot_no_get_burst"]
        self.assertEqual((opened["streams"], opened["gets"], opened["rooms"]), (1, 0, 9))

    def test_each_view_follows_its_own_items(self):
        self.assertEqual(self.cases["remote_view_events_after_the_snapshot"]["applied"], 3)
        self.assertEqual(self.cases["home_view_sources_only"], {"streams": 1, "gets": 0})
        self.assertIn("native_home_topics", self.cases)

    def test_gaps_resync_from_a_snapshot_without_reads(self):
        self.assertEqual(self.cases["unavailable_then_resync"]["gets"], 0)
        self.assertEqual(self.cases["dropped_stream_reopens_with_a_fresh_snapshot"], {"reconnects": 20, "gets": 0})
        self.assertEqual(self.cases["hidden_page_holds_no_stream"]["streams_opened"], 2)
        self.assertEqual(self.cases["snapshot_deadline"]["waited_ms"], 8000)

    def test_a_reconnects_snapshot_replaces_state_and_what_it_lacks_is_unknown(self):
        self.assertEqual(self.cases["reconnect_snapshot_replaces_state"], {"unknown": 9})

    def test_a_native_command_is_aborted_when_unanswered(self):
        self.assertEqual(self.cases["native_command_deadline"], {"deadline_ms": 15000})

    def test_commands_never_wait_for_the_snapshot(self):
        for name in ("gateway_command_before_snapshot", "native_command_before_snapshot"):
            self.assertEqual(self.cases[name]["commands"], 2, name)

    def test_native_openhab_is_read_once_per_stream(self):
        self.assertEqual(self.cases["native_wildcards_and_one_bulk_read"], {"streams": 1, "gets": 1})

    def test_activity_and_diagnostics_open_nothing_of_their_own(self):
        self.assertEqual(self.cases["activity_reads_the_view_stream"]["lines"], 6)
        self.assertEqual(self.cases["diagnostics_reads_stay_bounded"], {"diagnostic_reads": 3, "peak": 2})

    def test_terminal_remote_and_pairing_use_their_views(self):
        self.assertEqual(self.cases["terminal_remote_view_and_pair_view"], {"streams": 2, "item_gets": 0})

    def test_entire_fixture_has_zero_real_actions(self):
        self.assertEqual((self.proof["actualNetworkRequests"], self.proof["actualDeviceCommands"]), (0, 0))
