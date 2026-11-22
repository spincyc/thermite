"""Native signal collection on the control actors' existing sessions."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_integrations import _io, avpro_telnet, sony_adcp, vrroom_http, vrroom_telnet  # noqa: E402


@unittest.skipUnless(shutil.which("node"), "node is required for native collector fixtures")
class Collectors(unittest.TestCase):
    def test_serialized_queries_route_association_timeouts_and_metadata(self):
        scratch = AREA / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="signal-collectors-") as directory:
            fixture = Path(directory) / "sources.json"
            providers = (vrroom_telnet, sony_adcp, avpro_telnet)
            fixture.write_text(json.dumps({
                "sources": {provider.NAME: provider.SOURCE.read_text() for provider in providers},
                "shared": {provider.NAME: _io.device(provider.NAME) for provider in providers},
                "http": vrroom_http.signal_js()[1:],
            }), encoding="utf-8")
            result = subprocess.run([shutil.which("node"), str(AREA / "tests/signal_collectors_harness.js"), str(fixture)],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 18)

    def test_descriptors_separate_formats_capabilities_unparsed_reports_and_transport(self):
        device = {"id": "example_switch", "inputs": 4, "outputs": 4}
        native = vrroom_telnet.diagnostics(device)["readings"]
        self.assertTrue(all(reading.get("selector") for reading in native if reading["kind"] == "format"))
        self.assertEqual(next(reading for reading in native if reading["path"][-1] == "AUDOUT")["kind"], "metadata")
        self.assertTrue(all(reading["transports"] == ["hdmi"] for reading in native
                            if reading["path"][-1].startswith(("TX", "AUD", "SINK"))))
        http = vrroom_http.diagnostics(device)["readings"]
        self.assertTrue(any(reading["path"][-1] == "RX0" and reading["kind"] == "format" for reading in http))
        self.assertTrue(all(reading["kind"] == "metadata" for reading in http if reading["path"][-1] in {"AUDOUT", "EARCRX"}))
        self.assertTrue(all(reading["kind"] == "metadata" for reading in avpro_telnet.diagnostics(device)["readings"]))
        sony = sony_adcp.diagnostics(device)["readings"][0]
        self.assertEqual(sony["selector"], {"path": ["values", "input"], "node": "input"})
        self.assertEqual(sony["unknown"], ["Invalid"])
