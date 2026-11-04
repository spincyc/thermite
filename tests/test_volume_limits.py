"""Full provider ranges and explicit owner caps agree across control boundaries.

Only the public example and synthetic devices are used. JavaScript runs with
the real shared web/TUI code and an in-memory backend; no device is contacted.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
import oh_render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_features import remote_model  # noqa: E402
from oh_integrations import androidtv_googletv, denonmarantz_http, sony_ip_control, wiim_http  # noqa: E402

PROVIDERS = {"example_tv": sony_ip_control, "fixture_android": androidtv_googletv,
             "fixture_avr": denonmarantz_http, "fixture_wiim": wiim_http}
NATIVE_MAX = {"example_tv": 100, "fixture_android": 100, "fixture_avr": 98, "fixture_wiim": 100}
OWNER_CAPS = {"example_tv": 31, "fixture_android": 29, "fixture_avr": 70.5, "fixture_wiim": 53}


def fixture(caps=None):
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    additions = (("fixture_avr", "avr", "CINEMA 60", denonmarantz_http),
                 ("fixture_wiim", "streaming_amp", "Amp Pro", wiim_http),
                 ("fixture_android", "tv", "Example Android TV", androidtv_googletv))
    for index, (ident, kind, model, provider) in enumerate(additions, 200):
        controls = {"control": [provider.NAME]}
        if provider.PHASE == "observe":
            controls["observe"] = [provider.NAME]
        data["devices"].append({"id": ident, "kind": kind, "model": model, "room": "example_west",
                                "ip": f"192.0.2.{index}", "mac": "02:00:00:00:01:" + f"{index:02x}",
                                "controls": controls})
    data["routing"]["rooms"]["example_west"]["receiver"] = {"device": "fixture_avr", "input": "TV"}
    for device in data["devices"]:
        if device["id"] in (caps or {}):
            device["volume_max"] = caps[device["id"]]
    return data


def compiled(caps=None):
    data = fixture(caps)
    problems = oh_render.validate(data)
    if problems:
        raise AssertionError(problems)
    house = House(data)
    conf = oh_render.build(data)
    model = remote_model.build(house)
    authority = oh_site_contract.compile(house, conf)
    return data, conf, model, authority


class VolumeLimits(unittest.TestCase):
    def assert_boundaries(self, caps):
        data, conf, model, authority = compiled(caps)
        devices = {device["id"]: device for device in data["devices"]}
        for ident, provider in PROVIDERS.items():
            with self.subTest(provider=provider.NAME, caps=caps):
                expected = (caps or {}).get(ident, NATIVE_MAX[ident])
                volume = model["devices"][ident]["control"]["volume"]
                item = conf.yaml[provider.FILE].items[volume["item"]]
                action = authority["rooms"]["example_west"]["actions"][ident + ".volume"]
                self.assertEqual(provider.volume_max(devices[ident]), expected)
                self.assertEqual(volume["max"], expected)
                self.assertEqual(item["metadata"]["stateDescription"]["config"]["max"], expected)
                self.assertEqual(action["max"], expected)
                self.assertEqual(action["step"], .5 if ident == "fixture_avr" else 1)
                # Policy limits commands; readback retains the device's entire scale.
                self.assertEqual(authority["states"][volume["item"]]["max"], NATIVE_MAX[ident])
                if provider in (sony_ip_control, androidtv_googletv):
                    self.assertEqual(provider.table([devices[ident]])[0]["max"], expected)
        return conf, model, authority

    def test_default_rendered_controls_and_gateway_offer_the_full_provider_range(self):
        self.assert_boundaries(None)

    def test_explicit_owner_caps_are_retained_without_rescaling_readback(self):
        self.assert_boundaries(OWNER_CAPS)

    def test_zero_cap_and_native_upper_bound_remain_valid_explicit_policies(self):
        self.assert_boundaries({ident: 0 for ident in PROVIDERS})
        for ident, provider in PROVIDERS.items():
            device = next(row for row in fixture()["devices"] if row["id"] == ident)
            for cap in (0, NATIVE_MAX[ident]):
                with self.subTest(provider=provider.NAME, cap=cap):
                    configured = {**device, "volume_max": cap}
                    self.assertEqual(provider.validate(configured), [])
                    self.assertEqual(provider.volume_max(configured), cap)
            for cap in (-1, NATIVE_MAX[ident] + 1, True, "100", .25):
                with self.subTest(provider=provider.NAME, invalid=cap):
                    self.assertTrue(provider.validate({**device, "volume_max": cap}))

    def test_unavailable_speaker_controls_and_observe_only_devices_stay_uncommandable(self):
        for ident, provider in PROVIDERS.items():
            device = next(row for row in fixture()["devices"] if row["id"] == ident)
            if provider.PHASE == "observe":
                device["controls"].pop("control")
                self.assertIsNone(provider.remote(device))
        device = next(row for row in fixture()["devices"] if row["id"] == "example_tv")
        self.assertNotIn("volume", sony_ip_control.remote({**device, "sony_speaker_audio": False}))

    @unittest.skipUnless(shutil.which("node"), "needs node for the shared web/TUI controls")
    def test_real_web_tui_sliders_and_command_transforms_use_the_same_limits(self):
        cases = []
        for caps in (None, OWNER_CAPS):
            conf, model, _ = self.assert_boundaries(caps)
            profiles = {}
            avr_item = conf.yaml[denonmarantz_http.FILE].items["fixture_avr_main_volume"]
            profiles["fixture_avr"] = next(iter(avr_item["channels"].values()))["commandFromItemScript"][1:]
            wiim = conf.yaml[wiim_http.FILE].things["http:url:fixture_wiim"]
            profiles["fixture_wiim"] = wiim["channels"]["volume"]["config"]["commandTransformation"][0][4:-1]
            cases.append({"model": model, "profiles": profiles})
        script = r'''
const assert = require("node:assert/strict"), fs = require("node:fs");
const Main = require("./tui/remote/main"), Shared = require("./tui/remote/shared");
console.log = console.info = () => {};
(async () => {
  for (const test of JSON.parse(fs.readFileSync(0, "utf8"))) {
    for (const id of ["example_tv", "fixture_android", "fixture_avr", "fixture_wiim"]) {
      const shared = Shared.load(); shared.house = structuredClone(test.model);
      shared.house.rooms.find(room => room.id === "example_west").audio = id;
      const parts = Main.assemble(Main.parse(["--mock", "--room", "example_west", "--still", "--volume", "slider"]), {
        shared, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {}
      });
      const sent = []; parts.backend.setVolume = (room, value) => { sent.push(value); return Promise.resolve({ sent: true }); };
      await parts.controller.start("example_west");
      const spec = shared.house.devices[id].control.volume, view = parts.session.audioView();
      assert.equal(view.max, spec.max); assert.equal(view.step, id === "fixture_avr" ? .5 : 1);
      assert.equal(shared.Remote.volumeValue(spec, 1000), spec.max);
      assert.equal(shared.Remote.volumeValue(spec, -1), 0);
      const frame = parts.controller.frame(149, 30), slider = frame.focusables.find(hit => hit.kind === "slider");
      assert(slider, id); assert.equal(slider.max, spec.max); assert.equal(slider.step, view.step);
      parts.controller.ui.pane = "remote"; parts.controller.ui.focus = slider.key;
      parts.controller.key(null, { name: "end" });
      assert.equal(sent.at(-1), spec.max, id + " keyboard endpoint");
      if (id === "fixture_avr") {
        assert.equal(shared.Remote.volumeText(spec, 80, "relative"), "0.0 dB");
        assert.equal(shared.Remote.volumeText(spec, 98, "relative"), "18.0 dB");
        assert.equal(shared.Remote.volumeValue(spec, 69.6), 69.5);
      }
      if (test.profiles[id]) {
        const transform = new Function("input", "return (" + test.profiles[id] + ");");
        assert.equal(transform(String(spec.max)), String(spec.max));
        assert.equal(transform("1000"), String(spec.max));
        assert.equal(transform("INCREASE"), null); assert.equal(transform("DECREASE"), null);
        assert.equal(transform("-1"), null);
      }
      parts.controller.stop();
    }
  }
  process.stdout.write("web/TUI endpoints and native command profiles agree\n");
})().then(() => process.exit(0), error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(cases), cwd=AREA,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(importlib.util.find_spec("django"), "optional gateway framework is not installed")
    def test_real_gateway_validator_accepts_full_range_and_preserves_explicit_caps(self):
        cases = [compiled(caps)[3] for caps in (None, OWNER_CAPS, {ident: 0 for ident in PROVIDERS})]
        scratch = AREA / ".scratch/volume_range_audit"
        scratch.mkdir(parents=True, exist_ok=True)
        script = r'''
import json, sys
from oh_identity import composition
composition.setup(sys.argv[1])
from oh_identity.runtime import RuntimeContract
from oh_identity.services import Problem
for data in json.load(sys.stdin):
    contract = RuntimeContract(**data)
    for action, spec in data["rooms"]["example_west"]["actions"].items():
        if not action.endswith(".volume"):
            continue
        for value in (spec["min"], spec["max"]):
            assert contract.command("example_west", action, value)[1] == str(value)
        for value in (spec["max"] + spec["step"], True, float("nan"), spec["min"] + spec["step"] / 2):
            try:
                contract.command("example_west", action, value)
            except Problem as error:
                assert error.code == "invalid_command"
            else:
                raise AssertionError("gateway accepted an invalid volume")
'''
        with tempfile.TemporaryDirectory(prefix="gateway-", dir=scratch) as temporary:
            env = {key: value for key, value in os.environ.items()
                   if key not in {"DJANGO_SETTINGS_MODULE", "THERMITE_IDENTITY_STATE", "THERMITE_ALLOWED_HOSTS"}}
            env["PYTHONPATH"] = str(AREA / "lib")
            result = subprocess.run([sys.executable, "-B", "-c", script, temporary], input=json.dumps(cases),
                                    cwd=AREA, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
