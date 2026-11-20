"""Signal facts retain provenance and distinguish malformed replies from silence."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_integrations import _diagnostics as diag  # noqa: E402
from oh_integrations import bk808_http, denonmarantz_http, drhdmi_http, sonos, wiim_http  # noqa: E402


@unittest.skipUnless(shutil.which("node"), "node is required for response transformations")
class ResponseEnvelope(unittest.TestCase):
    def transform(self, script, value):
        result = subprocess.run([shutil.which("node"), "-e",
                                 "const fs=require('fs'),vm=require('vm');"
                                 "const d=JSON.parse(fs.readFileSync(0,'utf8'));"
                                 "process.stdout.write(vm.runInNewContext(d.script,{input:d.input}));"],
                                input=json.dumps({"script": script[1:], "input": value}),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ssi_directive_is_an_invalid_response_not_no_signal(self):
        result = self.transform(diag.json_snapshot(["RX0"], required="RX0"), "<!--#infoPage-->\r\n")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "invalid_response")
        self.assertEqual(result["values"], {})
        self.assertGreater(result["observedAt"], 0)

    def test_json_keeps_only_requested_bounded_fields(self):
        result = self.transform(diag.json_snapshot(["RX0"], required="RX0"),
                                json.dumps({"RX0": "\x1b1080p60", "secret": "private", "unexpected": [1]}))
        self.assertEqual(result["values"], {"RX0": "1080p60"})
        self.assertNotIn("status", result)

    def test_invalid_xml_never_retains_raw_error_or_old_measurement(self):
        script = diag.xml_snapshot("GetAudioInfo", ["signal"])
        for value in ("<rx><cmd><name>Other</name></cmd></rx>", "<!DOCTYPE rx><rx></rx>",
                      '<rx><cmd><name>GetAudioInfo</name><list><param name="signal" control="1">PCM</param>'
                      '<param name="signal" control="1">Other</param></list></cmd></rx>'):
            with self.subTest(value=value):
                result = self.transform(script, value)
                self.assertEqual(result["reason"], "invalid_response")
                self.assertEqual(result["values"], {})

    def test_unknown_receiver_value_remains_a_provider_sentinel(self):
        result = self.transform(diag.xml_snapshot("GetAudioInfo", ["signal"]),
                                '<rx><cmd><name>GetAudioInfo</name><list>'
                                '<param name="signal" control="1">Unknown</param></list></cmd></rx>')
        self.assertEqual(result["values"], {"signal": "Unknown"})


class ProviderMeaning(unittest.TestCase):
    def test_edid_and_hpd_are_not_current_formats(self):
        for provider in (bk808_http, drhdmi_http):
            readings = provider.diagnostics({"id": "fixture"})["readings"]
            self.assertTrue(readings)
            self.assertNotIn("format", {reading["kind"] for reading in readings})
            self.assertTrue(all(reading["kind"] == "capability" for reading in readings
                                if reading["category"] == "edid"))

    def test_receiver_formats_require_source_association_and_known_values(self):
        readings = denonmarantz_http.diagnostics({"id": "fixture", "model": "CINEMA 60"})["readings"]
        for reading in readings:
            self.assertEqual(reading["zone"], "main")
            self.assertEqual(reading["unknown"], ["---", "Unknown"])
            if reading["kind"] == "format":
                self.assertEqual(reading["selector"]["node"], "input")

    def test_wiim_track_metadata_does_not_claim_optical_measurement(self):
        device = {"id": "fixture", "model": "Amp Pro", "controls": {"control": ["wiim_http"]}}
        readings = wiim_http.diagnostics(device)["readings"]
        self.assertEqual([reading["kind"] for reading in readings], ["setting", "metadata", "metadata"])
        self.assertTrue(all(reading["scalar"] for reading in readings))
        self.assertEqual(wiim_http.diagnostics({**device, "controls": {"observe": ["wiim_http"]}})["readings"], readings)

    def test_sonos_silence_and_no_signal_are_distinct_and_age_is_unknown(self):
        device = {"id": "fixture", "model": "Arc", "controls": {"control": ["sonos"]}}
        codec = sonos.diagnostics(device)["readings"][0]
        self.assertEqual(codec["noSignal"], ["noSignal"])
        self.assertEqual(codec["silence"], ["silence"])
        self.assertTrue(codec["scalar"])
        self.assertEqual(sonos.diagnostics({**device, "controls": {"observe": ["sonos"]}})["readings"][0], codec)
        self.assertIsNone(sonos.diagnostics({**device, "model": "Amp"}))


if __name__ == "__main__":
    unittest.main()
