"""Routing telemetry: the JSON routing.js posts to the routing metrics Item."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_features import routing  # noqa: E402
from oh_features.routing import lifecycle_io  # noqa: E402


class Item(unittest.TestCase):
    def test_routing_js_posts_to_the_rendered_item(self):
        self.assertEqual(routing.METRICS_ITEM, "house_routing_metrics")
        self.assertIn(routing.METRICS_ITEM, routing.HOUSE_ITEMS)
        source = routing.SOURCE.read_text(encoding="utf-8")
        self.assertEqual(source.count(f"const METRICS_ITEM = '{routing.METRICS_ITEM}';"), 1)


@unittest.skipUnless(shutil.which("node"), "node is required for the production routing scripts")
class Telemetry(unittest.TestCase):
    def test_route_timing_resends_and_executor_outcomes(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/test_routing_telemetry_harness.js"),
                                 str(routing.SOURCE), str(lifecycle_io.SOURCE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 12)
