"""Exercise production HEOS, IR, CEC, Android TV, AVPro, Sony ADCP, Sony IP control and Apple TV scripts against
stub openHAB and Java types."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_integrations import _commands, _io  # noqa: E402

#: Each script the harness runs, by its integration's NAME, in argv order.
SCRIPTS = {"heos": "heos.js", "ir": "globalcache_ir.js", "bk808_cec": "bk808_cec.js",
           "androidtv_googletv": "androidtv_googletv/androidtv_googletv.js",
           "avpro_telnet": "avpro_telnet/avpro_telnet.js", "sony_adcp": "sony_adcp/sony_adcp.js",
           "sony_ip_control": "sony_ip_control/sony_ip_control.js", "pyatv_http": "pyatv_http/pyatv_http.js"}


def shared(directory, names) -> Path:
    """A file of what the scripts share, as each integration's script embeds
    it (_io.device()), with the command record's vocabulary, for a harness."""
    path = Path(directory) / "shared.json"
    vocabulary = {"problems": list(_commands.CODES), "drops": list(_commands.DROPS),
                  "outcomes": list(_commands.OUTCOMES), "clear": _commands.CLEAR}
    path.write_text(json.dumps({**{name: _io.device(name) for name in names}, "vocabulary": vocabulary}),
                    encoding="utf-8")
    return path


@unittest.skipUnless(shutil.which("node"), "node is required for the production integration scripts")
class IntegrationScripts(unittest.TestCase):
    def test_receiver_wire_format_key_drops_pairing_recovery_and_native_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([shutil.which("node"), str(AREA / "tests/integration_scripts_harness.js"),
                                     *(str(AREA / "lib/oh_integrations" / name) for name in SCRIPTS.values()),
                                     str(shared(directory, [*SCRIPTS, "test"]))],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 48)
