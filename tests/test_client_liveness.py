"""Clients notice a dead event stream."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


class Heartbeat(unittest.TestCase):
    def test_the_clients_watch_for_the_heartbeat_the_gateway_sends(self):
        streams = (AREA / "lib/oh_identity/streams.py").read_text()
        access = (AREA / "web/remote/access.js").read_text()
        seconds = int(re.search(r"^HEARTBEAT = (\d+)$", streams, re.M)[1])
        self.assertEqual(int(re.search(r"const STREAM_HEARTBEAT_MS = (\d+);", access)[1]), seconds * 1000)
        frame = re.search(r"^HEARTBEAT_FRAME = b'data: (.*)\\n\\n'$", streams, re.M)[1]
        self.assertEqual(re.search(r"const HEARTBEAT_FRAME = '(.*)';", access)[1], frame)
        self.assertEqual(json.loads(frame), {"type": "heartbeat"})


@unittest.skipUnless(NODE, "needs node")
class ClientLiveness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([NODE, str(AREA / "tests/client_liveness_harness.js")],
                                capture_output=True, text=True, timeout=30)
        if result.returncode or not result.stdout:
            raise AssertionError(result.stderr or result.stdout or "the harness never finished")
        cls.proof = json.loads(result.stdout)

    def test_a_stream_silent_for_three_heartbeats_is_reopened_and_heartbeats_reach_no_listener(self):
        self.assertEqual(self.proof["watchdog"], {"heartbeat_ms": 5000, "silence_ms": 15000})

    def test_a_page_shown_again_reopens_a_silent_stream_at_once_and_none_while_hidden(self):
        self.assertTrue(self.proof["resume"])
