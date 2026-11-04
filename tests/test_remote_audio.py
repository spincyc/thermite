"""Which device a room's Volume and Mute drive, in the model and the clients.

A room's `audio` (routing.rooms.<id>.audio) names it: a streaming amplifier
beside a passive desk monitor, say. Without one it is the receiver, unless
that is on a zone of its own, else the display. Whatever the model picks,
the site contract lets that room command it. Only the public example and
synthetic devices are used; no device is contacted.
"""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
import oh_render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_features import remote_model, routing  # noqa: E402

AMP = {"kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro",
       "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}
TV = {"kind": "tv", "make": "Sony", "model": "Example TV",
      "controls": {"observe": ["sony_rest"], "control": ["sony_ip_control"]}}


def add(data, ident, room, spec, number):
    data["devices"].append({"id": ident, "room": room, **copy.deepcopy(spec), "ip": f"192.0.2.{number}",
                            "mac": "02:00:00:00:02:" + f"{number:02x}"})


def desk(audio="fixture_amp"):
    """The example house with example_east's screen a passive monitor and a
    streaming amplifier beside it, named its `audio` (none for None)."""
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    add(data, "fixture_monitor", "example_east", {"kind": "monitor", "controls": {}}, 40)
    add(data, "fixture_amp", "example_east", AMP, 41)
    east = data["routing"]["rooms"]["example_east"]
    east["display"] = {"device": "fixture_monitor", "passive": True}
    if audio:
        east["audio"] = audio
    return data


def zones():
    """A Cinema on a receiver's main zone and a Lounge on its zone 2."""
    def device(ident, room, kind, number, **extra):
        return {"id": ident, "room": room, "kind": kind, "ip": f"192.0.2.{number}", **extra}
    return {
        "version": 1,
        "rooms": {"example_cinema": {"label": "Example Cinema", "tag": "Room"},
                  "example_lounge": {"label": "Example Lounge", "tag": "Room"}},
        "devices": [
            device("example_matrix", "example_cinema", "hdmi_matrix", 20, model="HDMI matrix",
                   controls={"control": ["bk808_http"]}),
            device("example_avr", "example_cinema", "avr", 22, make="Marantz", model="CINEMA 30", zones=2,
                   controls={"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}),
            device("example_screen", "example_cinema", **{**copy.deepcopy(TV), "number": 23}),
            device("example_tv", "example_lounge", **{**copy.deepcopy(TV), "number": 24}),
            device("example_computer", "example_cinema", "computer", 26, model="Computer"),
        ],
        "routing": {
            "hdmi_matrix": "example_matrix", "hdmi_off_input": 8, "usb_off_host": 4,
            "sources": {"example_computer": {"label": "Computer", "hdmi_input": 1}},
            "rooms": {
                "example_cinema": {"hdmi_output": 1, "sources": ["example_computer"],
                                   "receiver": {"device": "example_avr", "input": "BD"},
                                   "display": {"device": "example_screen", "input": "hdmi1"}},
                "example_lounge": {"hdmi_output": 2, "sources": ["example_computer"],
                                   "receiver": {"device": "example_avr", "zone": 2, "input": "AUX2"},
                                   "display": {"device": "example_tv", "input": "hdmi2"}},
            },
        },
    }


def compiled(data):
    problems = oh_render.validate(data)
    if problems:
        raise AssertionError(problems)
    house = House(data)
    return remote_model.build(house), oh_site_contract.compile(house, oh_render.build(data))


def audio(model, room):
    return next(row for row in model["rooms"] if row["id"] == room)["audio"]


class RoomAudio(unittest.TestCase):
    def test_named_audio_drives_the_amplifier_beside_a_passive_screen(self):
        model, authority = compiled(desk())
        self.assertEqual(audio(model, "example_east"), "fixture_amp")
        for point in ("volume", "mute"):
            self.assertIn("fixture_amp." + point, authority["rooms"]["example_east"]["actions"])

    def test_without_audio_the_receiver_or_display_is_kept_and_nothing_is_guessed(self):
        model, _ = compiled(desk(audio=None))
        self.assertIsNone(audio(model, "example_east"))
        self.assertEqual(audio(model, "example_west"), "example_tv")
        model, _ = compiled(zones())
        self.assertEqual(audio(model, "example_cinema"), "example_avr")

    def test_a_zone_receiver_is_not_its_rooms_volume_its_display_is(self):
        # The receiver's remote volume is its main zone's: the Cinema's.
        model, authority = compiled(zones())
        self.assertEqual(audio(model, "example_lounge"), "example_tv")
        self.assertNotIn("example_avr.volume", authority["rooms"]["example_lounge"]["actions"])

    def test_every_room_audio_is_one_its_gateway_commands(self):
        for data in (desk(), desk(audio=None), zones(), json.loads((AREA / "instance/devices.yaml").read_text())):
            model, authority = compiled(data)
            for row in model["rooms"]:
                with self.subTest(room=row["id"], audio=row["audio"]):
                    if row["audio"]:
                        self.assertIn(row["audio"] + ".volume", authority["rooms"][row["id"]]["actions"])

    def test_audio_must_be_a_volume_device_of_the_room_and_not_a_zone_receiver(self):
        def problems(data, room, value):
            data["routing"]["rooms"][room]["audio"] = value
            return [p for p in oh_render.validate(data) if ".audio" in p]
        self.assertEqual(problems(desk(), "example_east", "fixture_ampp"),
                         ["routing.rooms.example_east.audio: 'fixture_ampp' is not a device id; did you mean fixture_amp?"])
        self.assertIn("offers no volume", problems(desk(), "example_east", "example_computer")[0])
        self.assertIn("is in example_west, not in a room example_east covers",
                      problems(desk(), "example_east", "example_tv")[0])
        self.assertIn("is this room's receiver on zone 2", problems(zones(), "example_lounge", "example_avr")[0])
        self.assertEqual(problems(desk(), "example_west", "example_tv"), [])

    def test_a_building_covers_its_nested_rooms_and_lists_their_devices(self):
        data = desk(audio=None)
        data["rooms"]["example_east"]["tag"] = "Building"
        data["rooms"]["example_east_den"] = {"label": "Example East Den", "tag": "Room", "in": "example_east"}
        next(d for d in data["devices"] if d["id"] == "fixture_amp")["room"] = "example_east_den"
        self.assertEqual(routing.covered_rooms(data, "example_east"), {"example_east", "example_east_den"})
        self.assertEqual(routing.covered_rooms(data, "example_west"), {"example_west"})
        data["routing"]["rooms"]["example_east"]["audio"] = "fixture_amp"
        model, authority = compiled(data)
        east = next(row for row in model["rooms"] if row["id"] == "example_east")
        self.assertIn("fixture_amp", east["devices"])
        self.assertEqual(east["audio"], "fixture_amp")
        self.assertIn("fixture_amp.volume", authority["rooms"]["example_east"]["actions"])

    @unittest.skipUnless(shutil.which("node"), "needs node for the shared web/TUI session")
    def test_web_and_tui_session_send_volume_and_mute_to_the_named_audio(self):
        model, _ = compiled(desk())
        script = r'''
const assert = require("node:assert/strict"), fs = require("node:fs");
const Shared = require("./tui/remote/shared");
console.info = () => {};
(async () => {
  const shared = Shared.load(), house = JSON.parse(fs.readFileSync(0, "utf8"));
  const backend = shared.Remote.create(house, { backend: "mock", latency: 0 });
  const session = shared.Session.create({ backend, house, layouts: shared.layouts, volume: "slider" });
  await session.start("example_east");
  await session.chooseSource("example_computer");
  const view = session.audioView();
  assert.equal(view.audio && view.audio.id, "fixture_amp");
  assert.notEqual(view.caption, "No audio");
  for (const button of ["volume_up", "mute"]) {
    const result = await backend.press("example_east", null, button);
    assert.equal(result.device && result.device.id, "fixture_amp", button);
  }
  assert.equal((await backend.setVolume("example_east", 40)).device.id, "fixture_amp");
})().then(() => process.exit(0), (error) => { console.error(error); process.exit(1); });
'''
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(model), cwd=AREA, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
