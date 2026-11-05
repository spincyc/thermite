"""The web and terminal clients report their own give-ups to the gateway."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node")
class ClientTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(AREA / "tests/client_telemetry_harness.js")],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        cls.proof = json.loads(result.stdout)

    def test_a_lock_read_that_gives_up_is_a_client_timeout_and_the_room_shows_unavailable(self):
        self.assertTrue(self.proof["clientTimeout"])

    def test_counts_are_reported_after_the_next_answered_request_spaced_and_one_at_a_time(self):
        for key in ("flushAfterSuccess", "acceptedClears", "spacedAndSingle"):
            self.assertTrue(self.proof[key], key)

    def test_refused_reports_are_dropped_and_throttled_or_failed_ones_kept_without_recursion(self):
        for key in ("refusedDrops", "throttledKeeps", "noRecursion"):
            self.assertTrue(self.proof[key], key)

    def test_counts_and_waits_are_capped_and_the_fullest_report_fits_2048_bytes(self):
        self.assertTrue(self.proof["caps"] and self.proof["sizeCap"])

    def test_commands_and_event_streams_are_counted_by_kind(self):
        for key in ("commandTimeout", "streams", "terminalStream"):
            self.assertTrue(self.proof[key], key)

    def test_the_page_reports_its_normalised_protocol_and_beacons_as_it_hides(self):
        for key in ("protocol", "beacon", "beaconFallback"):
            self.assertTrue(self.proof[key], key)

    def test_mock_and_native_modes_send_nothing(self):
        self.assertTrue(self.proof["quietModes"])
