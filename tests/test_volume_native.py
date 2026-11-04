"""Each device's own volume scale, end to end: no cap, no rescaling.

Every volume integration declares the scale its device reports and takes
(VOLUME: min, max, step, unit). The rendered Items, the openHAB command
profiles and rules, the gateway's validation and readback, the activity
feed and the web/TUI remote all use exactly that scale. The house file and
room policies have no way to cap it.

Only the public example and synthetic devices are used: documentation
addresses and locally administered MACs. JavaScript runs the real shared
web/TUI code with an in-memory backend; no device is contacted.
"""

import functools
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
import oh_inventory  # noqa: E402
import oh_integrations  # noqa: E402
import oh_render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_features import remote_activity, remote_model  # noqa: E402
from oh_integrations import androidtv_googletv, denonmarantz_http, sonos, sony_ip_control, wiim_http  # noqa: E402

#: Each fixture device's own volume scale, written out from its sources
#: (each integration's VOLUME cites them), so that any change is deliberate.
NATIVE = {
    "example_tv": (sony_ip_control, "example_tv_volume", {"min": 0, "max": 100, "step": 1}),
    "fixture_android": (androidtv_googletv, "fixture_android_android_volume",
                        {"min": 0, "max": 100, "step": 1, "unit": "%"}),
    "fixture_avr": (denonmarantz_http, "fixture_avr_main_volume", {"min": 0, "max": 98, "step": 0.5}),
    "fixture_arc": (sonos, "fixture_arc_volume", {"min": 0, "max": 100, "step": 1}),
    "fixture_wiim": (wiim_http, "fixture_wiim_volume", {"min": 0, "max": 100, "step": 1}),
}
#: The routed room each device's volume is commanded in.
ROOMS = {"example_tv": "example_west", "fixture_android": "example_west", "fixture_avr": "example_west",
         "fixture_arc": "example_east", "fixture_wiim": "example_office"}
#: (value, receiver display mode, text) as each device shows its own number.
TEXTS = {
    "example_tv": [[100, None, "100"], [0, None, "0"]],
    "fixture_android": [[40, None, "40 %"]],
    "fixture_avr": [[69.5, "absolute", "69.5"], [98, "absolute", "98"], [69.5, "relative", "−10.5 dB"],
                    [98, "relative", "18.0 dB"], [0, "relative", "---.- dB"], [50, None, "50 (native)"]],
    "fixture_arc": [[100, None, "100"]],
    "fixture_wiim": [[38, None, "38"], [100, None, "100"]],
}


def fixture():
    """The public example plus an AVR, an Android TV, a Sonos Arc and an
    office desk shaped like the site's: a passive monitor and a WiiM."""
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["rooms"]["example_office"] = {"label": "Example Office"}

    def add(ident, kind, model, room, controls, index, **extra):
        data["devices"].append({"id": ident, "kind": kind, "model": model, "room": room, "ip": f"192.0.2.{index}",
                                "mac": f"02:00:00:00:01:{index:02x}", "controls": controls, **extra})
    add("fixture_avr", "avr", "CINEMA 60", "example_west",
        {"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}, 200)
    add("fixture_android", "tv", "Example Android TV", "example_west", {"control": ["androidtv_googletv"]}, 201)
    add("fixture_arc", "soundbar", "Arc", "example_east", {"observe": ["sonos"], "control": ["sonos"]}, 202,
        sonos_udn=sonos.udn(f"02:00:00:00:01:{0xca:02x}"))
    add("fixture_wiim", "streaming_amp", "Amp Pro", "example_office",
        {"observe": ["wiim_http"], "control": ["wiim_http"]}, 203)
    add("fixture_monitor", "monitor", "Example Monitor", "example_office", {}, 204)
    data["routing"]["rooms"]["example_west"]["receiver"] = {"device": "fixture_avr", "input": "TV"}
    data["routing"]["rooms"]["example_office"] = {
        "label": "Example Office Desk", "hdmi_output": 3, "sources": ["example_computer"],
        "display": {"device": "fixture_monitor", "passive": True}, "audio": "fixture_wiim"}
    return data


@functools.lru_cache(maxsize=None)
def compiled():
    data = fixture()
    problems = oh_render.validate(data)
    if problems:
        raise AssertionError(problems)
    house = House(data)
    conf = oh_render.build(data)
    return data, conf, remote_model.build(house), oh_site_contract.compile(house, conf), \
        remote_activity.manifest(house, conf)


def item(conf, module, name):
    return conf.yaml[module.FILE].items[name]


def profiles(conf):
    """device -> the inline JS openHAB runs on a volume command to it."""
    avr = item(conf, denonmarantz_http, "fixture_avr_main_volume")
    arc = item(conf, sonos, "fixture_arc_volume")
    wiim = conf.yaml[wiim_http.FILE].things["http:url:fixture_wiim"]["channels"]["volume"]["config"]
    rule = (AREA / "lib/oh_integrations/sony_ip_control/sony_ip_control.js").read_text()
    number = re.search(r"^const NUMBER = .*;$", rule, re.M).group()
    on_scale = re.search(r"^function onScale\(scale, command\) \{\n.*?^\}$", rule, re.M | re.S).group()
    return {
        "fixture_avr": next(iter(avr["channels"].values()))["commandFromItemScript"][1:],
        "fixture_arc": next(iter(arc["channels"].values()))["toHandlerScript"][1:],
        "fixture_wiim": re.fullmatch(r"JS\(\|(.*)\)", wiim["commandTransformation"][0], re.S).group(1),
        # The Sony rule sends setAudioVolume only for a command onScale() passes.
        "example_tv": ("(function () { " + number + "\n" + on_scale + "\nreturn onScale("
                       + json.dumps(NATIVE["example_tv"][2]) + ", input) ? input : null; })()"),
    }


def grid(scale):
    """Every value on a scale, min to max."""
    return [scale["min"] + k * scale["step"] for k in range(round((scale["max"] - scale["min"]) / scale["step"]) + 1)]


def text(value):
    return f"{value:g}"


CLIENTS = r'''
const assert = require("node:assert/strict"), fs = require("node:fs");
const Main = require("./tui/remote/main"), Shared = require("./tui/remote/shared");
console.log = console.info = () => {};
const tick = () => new Promise((resolve) => setTimeout(resolve, 2));
(async () => {
  const input = JSON.parse(fs.readFileSync(0, "utf8")), out = {};
  for (const test of input.cases) {
    const shared = Shared.load(); shared.house = structuredClone(input.model);
    const room = shared.house.rooms.find((entry) => entry.id === test.room);
    // Which device is a room's audio is routing's choice (remote_model); each takes the role in turn here.
    room.audio = test.device;
    shared.house.initial = { [test.room]: room.sources[0] };  // an Off room draws no remote
    const parts = Main.assemble(Main.parse(["--mock", "--room", test.room, "--still", "--volume", "slider", "--latency", "0"]), {
      shared, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {}
    });
    // Each command reaches the mock device and is sent to the gateway exactly as an OpenHabBackend sends it.
    const commands = [], gateway = { aim() {}, aims: {}, access: { native: false,
      command: (room, action, value) => { commands.push([room, action, value]); return Promise.resolve(); } } };
    const mock = parts.backend.send.bind(parts.backend);
    parts.backend.send = (id, step, result) => shared.Remote.OpenHabBackend.prototype.send
      .call(gateway, id, step, Object.assign({}, result)).then(() => mock(id, step, result));
    await parts.controller.start(test.room);
    const scale = test.scale, spec = shared.house.devices[test.device].control.volume, id = test.device;
    const view = () => parts.session.audioView();
    assert.deepEqual([spec.min, spec.max, spec.step, spec.unit ?? null], [scale.min, scale.max, scale.step, scale.unit], id);
    assert.deepEqual([view().min, view().max, view().step], [scale.min, scale.max, scale.step], id);
    assert(!/limit/.test(view().caption), id + " caption " + view().caption);
    const frame = parts.controller.frame(149, 30), slider = frame.focusables.find((hit) => hit.kind === "slider");
    assert(slider, id);
    assert.deepEqual([slider.min, slider.max, slider.step], [scale.min, scale.max, scale.step], id);
    parts.controller.ui.pane = "remote"; parts.controller.ui.focus = slider.key;
    const sent = () => commands.at(-1)[2];
    parts.controller.key(null, { name: "end" }); await tick();
    assert.equal(sent(), scale.max, id + " End"); assert.equal(view().value, scale.max); assert.equal(view().fill, 100);
    assert.equal(view().text, test.maxText, id + " text at max");
    parts.controller.key(null, { name: "home" }); await tick();
    assert.equal(sent(), scale.min, id + " Home"); assert.equal(view().fill, 0);
    parts.controller.key(null, { name: "right" }); await tick();
    assert.equal(sent(), scale.min + scale.step, id + " one native step");
    await parts.session.press("volume_up"); await tick();
    assert.equal(sent(), scale.min + 2 * scale.step, id + " volume key moves one native step");
    // The slider and the keys stay on the device's own scale, its max the only limit.
    parts.session.slide(scale.max * 10, true); await tick(); assert.equal(sent(), scale.max, id + " past max");
    parts.session.slide(scale.min - 5, true); await tick(); assert.equal(sent(), scale.min, id + " below min");
    parts.session.slide(scale.min + scale.step * 1.4, true); await tick();
    assert.equal(sent(), scale.min + scale.step, id + " between steps");
    for (const [value, mode, expected] of test.texts) assert.equal(shared.Remote.volumeText(spec, value, mode), expected, id);
    parts.controller.stop();
    out[id] = commands;
  }
  // The activity feed reads each volume on its device's own scale, commands and reports alike.
  const Activity = Shared.load() && globalThis.RemoteActivity, events = [];
  const feed = Activity.create({ items: input.activity.items, emit: (event) => events.push(event.body) });
  for (const [name, value, expected] of input.feed) {
    events.length = 0;
    feed.accept("openhab/items/" + name + "/command", JSON.stringify({ value }));
    assert.deepEqual(events, expected === null ? [] : ["Requested: " + expected], name + " " + value);
    events.length = 0;
    feed.accept("openhab/items/" + name + "/statechanged", JSON.stringify({ value, oldValue: "1" }));
    assert.deepEqual(events, [expected === null ? "Reported value changed (value hidden)" : "Reported: " + expected], name);
  }
  process.stdout.write(JSON.stringify(out));
})().then(() => process.exit(0), (error) => { console.error(error); process.exit(1); });
'''

GATEWAY = r'''
import json, math, sys
from oh_identity import composition
composition.setup(sys.argv[1])
from oh_identity.runtime import RuntimeContract, project
from oh_identity.services import Problem
from oh_room_policy import model as policy

data = json.load(sys.stdin)
contract = RuntimeContract(**data["authority"])


def refused(room, action, value):
    try:
        contract.command(room, action, value)
    except Problem as error:
        return error.code == "invalid_command"
    return False


checked = 0
for room, entry in data["authority"]["rooms"].items():
    for action, spec in entry["actions"].items():
        if not action.endswith(".volume"):
            continue
        low, high, step = spec["min"], spec["max"], spec["step"]
        assert [low, high, step] == data["native"][action.split(".")[0]], (action, spec)
        state = contract.states[spec["item"]]
        for k in range(round((high - low) / step) + 1):
            value = low + k * step
            item, command, _ = contract.command(room, action, value)
            assert item == spec["item"] and float(command) == value, (action, value, command)
            assert project(item, command, state) == command, (action, command)
            checked += 1
        for value in (high + step, low - step, low + step / 2, True, math.nan, math.inf, str(high), None):
            assert refused(room, action, value), (action, value)
        assert project(spec["item"], str(high + step), state) == "UNDEF"
for room, action, value in data["sent"]:
    item, command, _ = contract.command(room, action, value)
    assert float(command) == value
# A room policy has no volume setting: nothing there can cap a volume.
try:
    policy.validate({"version": 1, "room": "example_west", "timezone": "UTC", "revision": 0, "volume_max": 30},
                    "example_west", contract.policy_contract())
except policy.PolicyError as error:
    assert error.code == "unknown_field", error.code
else:
    raise AssertionError("a room policy accepted a volume cap")
print(checked)
'''


@functools.lru_cache(maxsize=None)
def clients():
    """device -> the gateway commands its web/TUI volume controls sent."""
    _, _, model, _, activity = compiled()
    cases = [{"room": ROOMS[ident], "device": ident, "scale": {**scale, "unit": scale.get("unit")},
              "texts": TEXTS[ident], "maxText": text(scale["max"]) + (" " + scale["unit"] if scale.get("unit") else "")}
             for ident, (_, _, scale) in NATIVE.items()]
    feed = [["fixture_avr_main_volume", "98", "98"], ["fixture_avr_main_volume", "69.5", "69.5"],
            ["fixture_avr_main_volume", "98.5", None], ["fixture_android_android_volume", "40", "40 %"],
            ["fixture_wiim_volume", "100", "100"], ["fixture_arc_volume", "100", "100"],
            ["example_tv_volume", "100", "100"], ["example_tv_volume", "101", None]]
    result = subprocess.run([shutil.which("node"), "-e", CLIENTS], cwd=AREA, capture_output=True, text=True, timeout=60,
                            input=json.dumps({"cases": cases, "model": model, "activity": activity, "feed": feed}))
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def gateway(sent):
    """Check every native value, the refusals and `sent` with the real gateway validator."""
    _, _, _, authority, _ = compiled()
    scratch = AREA / ".scratch/volume_native"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gateway-", dir=scratch) as temporary:
        os.chmod(temporary, 0o700)
        env = {key: value for key, value in os.environ.items()
               if key not in {"DJANGO_SETTINGS_MODULE", "THERMITE_IDENTITY_STATE", "THERMITE_ALLOWED_HOSTS"}}
        env["PYTHONPATH"] = str(AREA / "lib")
        native = {ident: [scale["min"], scale["max"], scale["step"]] for ident, (_, _, scale) in NATIVE.items()}
        native["example_display"] = native["example_tv"]
        result = subprocess.run([sys.executable, "-B", "-c", GATEWAY, temporary], cwd=AREA, env=env,
                                input=json.dumps({"authority": authority, "sent": sent, "native": native}),
                                capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return int(result.stdout)


def transform(expression, values):
    """openHAB's inline JS transformation `expression` applied to each value."""
    script = ("const out = []; for (const input of JSON.parse(process.argv[1])) out.push((function (input) { return ("
              + expression + "); })(input)); process.stdout.write(JSON.stringify(out));")
    result = subprocess.run([shutil.which("node"), "-e", script, json.dumps(values)], capture_output=True, text=True,
                            timeout=15)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


NEEDS_NODE = unittest.skipUnless(shutil.which("node"), "needs node for the shared web/TUI code and JS profiles")
NEEDS_GATEWAY = unittest.skipUnless(importlib.util.find_spec("django"), "optional gateway framework is not installed")


class NativeVolume(unittest.TestCase):
    def test_each_integration_declares_and_renders_its_device_own_scale(self):
        _, conf, model, authority, activity = compiled()
        for ident, (module, name, scale) in NATIVE.items():
            with self.subTest(provider=module.NAME):
                self.assertEqual(module.VOLUME, scale)
                volume = dict(model["devices"][ident]["control"]["volume"])
                volume.pop("displayMode", None)
                self.assertEqual(volume, {"item": name, **scale})
                self.assertEqual(item(conf, module, name)["metadata"]["stateDescription"]["config"],
                                 {key: scale[key] for key in ("min", "max", "step")})
                action = authority["rooms"][ROOMS[ident]]["actions"][ident + ".volume"]
                self.assertEqual({key: action[key] for key in ("kind", "item", "min", "max", "step")},
                                 {"kind": "number", "item": name, "min": scale["min"], "max": scale["max"],
                                  "step": scale["step"]})
                self.assertEqual(authority["states"][name], {"kind": "number", "min": scale["min"],
                                                            "max": scale["max"], "commands": True})
                feed = activity["items"][name]
                self.assertEqual({key: feed.get(key) for key in ("min", "max", "step", "unit")},
                                 {key: scale.get(key) for key in ("min", "max", "step", "unit")})
                self.assertNotIn("commandMax", feed)
        # The receiver's number follows its own display: absolute, or dB when set to relative.
        modes = model["devices"]["fixture_avr"]["control"]["volume"]["displayMode"]
        self.assertEqual(modes["modes"]["relative"]["offset"], -80)
        # The rules that send the TVs' volume carry the same scale.
        devices = {device["id"]: device for device in compiled()[0]["devices"]}
        self.assertEqual(sony_ip_control.table([devices["example_tv"]])[0]["volume"], NATIVE["example_tv"][2])
        self.assertEqual(androidtv_googletv.table([devices["fixture_android"]])[0]["volume"],
                         NATIVE["fixture_android"][2])
        # The WiiM's channel writes the value as it is: setPlayerCmd:vol:<value>.
        wiim = conf.yaml[wiim_http.FILE].things["http:url:fixture_wiim"]["channels"]["volume"]["config"]
        self.assertEqual(wiim["commandExtension"], "command=setPlayerCmd:vol:%2$s")

    def test_neither_the_house_file_nor_an_integration_can_cap_a_volume(self):
        self.assertFalse([key for key in oh_inventory.keys()[1] if "volume" in key])
        for module in oh_integrations.REGISTRY:
            self.assertFalse(hasattr(module, "volume_max") or hasattr(module, "DEFAULT_VOLUME_MAX"), module.NAME)
        for ident in NATIVE:
            with self.subTest(device=ident):
                data = fixture()
                next(device for device in data["devices"] if device["id"] == ident)["volume_max"] = 50
                self.assertIn(f"{ident}: unknown key 'volume_max'", oh_render.validate(data))

    @NEEDS_NODE
    def test_openhab_profiles_send_the_whole_scale_and_drop_the_rest(self):
        _, conf, _, _, _ = compiled()
        for ident, expression in profiles(conf).items():
            scale = NATIVE[ident][2]
            with self.subTest(device=ident):
                values = [text(value) for value in grid(scale)]
                self.assertEqual(transform(expression, values), values)
                outside = [text(scale["max"] + scale["step"]), text(scale["min"] - scale["step"]),
                           text(scale["min"] + scale["step"] / 2), "INCREASE", "DECREASE", "ON", "", "1e2"]
                self.assertEqual(transform(expression, outside), [None] * len(outside))

    @NEEDS_NODE
    def test_web_and_tui_show_and_step_each_device_own_number(self):
        sent = clients()
        for ident, (_, _, scale) in NATIVE.items():
            with self.subTest(device=ident):
                values = [value for _, _, value in sent[ident]]
                self.assertEqual(values, [scale["max"], scale["min"], scale["min"] + scale["step"],
                                          scale["min"] + 2 * scale["step"], scale["max"], scale["min"],
                                          scale["min"] + scale["step"]])
                self.assertEqual({(room, action) for room, action, _ in sent[ident]}, {(ROOMS[ident], ident + ".volume")})

    @NEEDS_GATEWAY
    def test_gateway_accepts_every_native_value_and_refuses_the_rest_without_policy_caps(self):
        sent = [command for commands in clients().values() for command in commands] if shutil.which("node") else []
        expected = sum(len(grid(scale)) for _, _, scale in NATIVE.values()) + len(grid(NATIVE["example_tv"][2]))
        self.assertEqual(gateway(sent), expected)

    @NEEDS_NODE
    def test_office_desk_wiim_volume_works_end_to_end(self):
        """A desk with a passive monitor and a WiiM: the remote's slider and
        keys send the WiiM's own 0-100 in steps of 1, the gateway takes them
        for the desk, and openHAB writes each to the player unchanged."""
        _, conf, model, authority, _ = compiled()
        desk = next(room for room in model["rooms"] if room["id"] == "example_office")
        self.assertIn("fixture_wiim", desk["devices"])
        self.assertEqual(desk["audio"], "fixture_wiim")
        action = authority["rooms"]["example_office"]["actions"]["fixture_wiim.volume"]
        self.assertEqual((action["min"], action["max"], action["step"]), (0, 100, 1))
        sent = clients()["fixture_wiim"]
        self.assertEqual([value for _, _, value in sent], [100, 0, 1, 2, 100, 0, 1])
        written = transform(profiles(conf)["fixture_wiim"], [text(value) for _, _, value in sent])
        self.assertEqual(["setPlayerCmd:vol:" + value for value in written],
                         ["setPlayerCmd:vol:" + text(value) for _, _, value in sent])
        if importlib.util.find_spec("django"):
            gateway(sent)


if __name__ == "__main__":
    unittest.main()
