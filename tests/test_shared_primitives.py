"""Unit tests for Stage 2c shared integration primitives: _items, shared BK-808
CEC Thing folding, and declarative recovery() hook."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_render as render  # noqa: E402
from oh_conf import GUARD_PROFILE, House  # noqa: E402
from oh_integrations import _instr, _items, bk808_cec, bk808_http, vrroom_telnet  # noqa: E402
from tests import test_read_only  # noqa: E402


class ItemsPrimitive(unittest.TestCase):
    def test_control_point_uses_command_from_item_script_and_rejects_deprecated_key(self):
        with self.assertRaisesRegex(ValueError, "commandFromItemScript"):
            _items.control_point("dev", "String", "Input", ["Control"], "http:url:dev:input",
                                 link={"toHandlerScript": "|input"})
        item = _items.control_point(
            "dev", "String", "Input", ["Control", "Channel"], "http:url:dev:input",
            icon="video",
            link={"toItemScript": "|input", "commandFromItemScript": _items.enum_guard_js(["hdmi1", "hdmi2"])},
            options=[("hdmi1", "HDMI 1"), ("hdmi2", "HDMI 2")])
        self.assertIs(item["autoupdate"], False)
        self.assertEqual(item["channels"]["http:url:dev:input"]["profile"], GUARD_PROFILE)
        self.assertIn("commandFromItemScript", item["channels"]["http:url:dev:input"])
        self.assertNotIn("toHandlerScript", item["channels"]["http:url:dev:input"])
        self.assertEqual(item["metadata"]["stateDescription"]["config"]["options"], "hdmi1=HDMI 1,hdmi2=HDMI 2")
        self.assertEqual(item["metadata"]["commandDescription"]["config"]["options"], "hdmi1=HDMI 1,hdmi2=HDMI 2")

    def test_control_point_supports_state_only_options_and_separate_command_options(self):
        state_only = _items.control_point(
            "dev", "Number", "EDID", ["Control"], "http:url:dev:edid",
            options="1=1080p", command_options="")
        self.assertIn("stateDescription", state_only["metadata"])
        self.assertNotIn("commandDescription", state_only["metadata"])
        split = _items.control_point(
            "dev", "String", "Input", ["Control"],
            options=[("tv", "TV"), ("hdmi1", "HDMI 1")],
            command_options=[("hdmi1", "HDMI 1")])
        self.assertEqual(split["metadata"]["stateDescription"]["config"]["options"], "tv=TV,hdmi1=HDMI 1")
        self.assertEqual(split["metadata"]["commandDescription"]["config"]["options"], "hdmi1=HDMI 1")

    @unittest.skipUnless(shutil.which("node"), "node is required to evaluate enum_guard_js")
    def test_enum_guard_js_allows_mapped_and_listed_values_only(self):
        unwrapped = _items.enum_guard_js(["ON", "OFF"])
        self.assertTrue(unwrapped.startswith("|"))
        wrapped = _items.enum_guard_js({"GAME": "GAME1"}, otherwise="input", wrap=True)
        self.assertTrue(wrapped.startswith("JS(|") and wrapped.endswith(")"))
        program = ("const [code, vals] = JSON.parse(process.argv[1]);"
                   "console.log(JSON.stringify(vals.map(input => eval(code))));")
        run1 = subprocess.run([shutil.which("node"), "-e", program, json.dumps([unwrapped[1:], ["ON", "OFF", "TOGGLE", ""]])],
                              capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(run1.stdout), ["ON", "OFF", None, None])
        run2 = subprocess.run([shutil.which("node"), "-e", program, json.dumps([wrapped[len("JS(|"):-1], ["GAME", "BD"]])],
                              capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(run2.stdout), ["GAME1", "BD"])


class SharedBK808CecThing(unittest.TestCase):
    def test_cec_folds_onto_shared_bk808_matrix_thing_without_duplicate_http_thing(self):
        self.assertIn("cec command", _instr.COMHEADS)
        data = test_read_only.house("bk808_cec", "control")
        self.assertEqual(render.validate(data), [])
        conf = render.build(House(copy.deepcopy(data)))
        # No separate CEC HTTP Thing is created; the matrix Thing carries a WRITEONLY cec channel.
        self.assertEqual(conf.yaml[bk808_cec.FILE].things, {})
        self.assertNotIn("den_device_cec_transport", conf.yaml[bk808_cec.FILE].items)
        self.assertNotIn("den_device_cec_native", conf.yaml[bk808_cec.FILE].items)
        self.assertIn("den_device_key", conf.yaml[bk808_cec.FILE].items)
        matrix_thing = conf.yaml[bk808_http.FILE].things["http:url:den_matrix"]
        self.assertEqual(matrix_thing["channels"]["cec"]["config"]["mode"], "WRITEONLY")
        self.assertEqual(matrix_thing["channels"]["cec"]["config"]["stateTransformation"], ["JS(|null)"])
        self.assertEqual(bk808_cec.thing_uid(data["devices"][0]), "http:url:den_matrix")
        self.assertEqual(bk808_cec.transport(data["devices"][0]), "http:url:den_matrix")
        # Rule targets the shared matrix Thing and aims bk808_http.FILE cleanly.
        files = render.render(House(copy.deepcopy(data)))
        self.assertIn('"thing":"http:url:den_matrix"', files[bk808_cec.RULE])
        aimed = bk808_cec.aim(files, data["devices"][0], "127.0.0.1:18088")
        aimed_matrix = yaml.safe_load(aimed[bk808_http.FILE])["things"]["http:url:den_matrix"]
        self.assertEqual(aimed_matrix["config"]["baseURL"], _instr.url("127.0.0.1:18088"))
        # Read-only turns the cec channel READONLY without adding a new poll URL/body.
        ro = render.read_only(files)
        ro_matrix = yaml.safe_load(ro[bk808_http.FILE])["things"]["http:url:den_matrix"]
        self.assertEqual(ro_matrix["channels"]["cec"]["config"]["mode"], "READONLY")
        self.assertEqual(ro_matrix["channels"]["cec"]["config"]["stateContent"],
                         _instr.body("get video status", language=0))


class RecoveryHook(unittest.TestCase):
    def test_vrroom_declares_recovery_separately_from_reboot(self):
        switch = {"id": "desk_switch", "room": "desk", "kind": "hdmi_switch",
                  "controls": {"observe": ["vrroom_http"], "control": ["vrroom_telnet"]}}
        self.assertNotIn("hung", vrroom_telnet.reboot(switch))
        self.assertEqual(vrroom_telnet.recovery(switch), {
            "things": ["http:url:desk_switch"], "seconds": 300,
            "item": "desk_switch_reboot", "action": "reboot"})


if __name__ == "__main__":
    unittest.main()
