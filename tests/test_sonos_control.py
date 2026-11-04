"""Native Sonos Arc Items, command routing and room volume, with no device access.

Ported from the private site's tests when the application moved here; the
house is fictional. Volume limits beyond the Arc's own 0-100 range are not
covered here (tests/test_volume_limits.py owns them).
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import urllib.request

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_conf  # noqa: E402
import oh_render  # noqa: E402
import oh_sandbox  # noqa: E402
import oh_site_contract  # noqa: E402
import oh_sonos_sim  # noqa: E402
from oh_features import remote_model  # noqa: E402
from oh_integrations import sonos  # noqa: E402
from oh_testing import controls  # noqa: E402

ARC, UDN = "example_arc", "RINCON_02000000001001400"
COTTAGE, NOOK, COTTAGE_ARC, COTTAGE_TV = "example_cottage", "example_cottage_nook", "example_cottage_arc", "example_cottage_tv"


def mac(last):
    return "02:00:00:00:00:" + last


def house(control=True):
    return {"version": 1, "rooms": {"example_lounge": {"label": "Example Lounge", "tag": "Room"}}, "devices": [
        {"id": ARC, "room": "example_lounge", "kind": "soundbar", "make": "Sonos", "model": "Arc",
         "ip": "192.0.2.10", "mac": mac("10"), "sonos_udn": UDN,
         "controls": {"observe": ["sonos"], "control": ["sonos"] if control else []}}]}


def cottage(audio=True):
    """The example house with a Building whose TV is in the Building itself
    and whose Arc is in a room nested in it, named the room's `audio`."""
    data = json.loads((AREA / "instance/devices.yaml").read_text())
    data["rooms"][COTTAGE] = {"label": "Example Cottage", "tag": "Building"}
    data["rooms"][NOOK] = {"label": "Example Cottage Nook", "tag": "LivingRoom", "in": COTTAGE}
    data["devices"] += [
        {"id": COTTAGE_TV, "room": COTTAGE, "kind": "tv", "make": "Sony", "model": "Example TV",
         "ip": "192.0.2.30", "mac": mac("30"), "controls": {"observe": ["sony_rest"], "control": ["sony_ip_control"]}},
        {"id": "example_cottage_computer", "room": COTTAGE, "kind": "computer", "model": "Computer",
         "ip": "192.0.2.31", "controls": {}},
        {"id": COTTAGE_ARC, "room": NOOK, "kind": "soundbar", "make": "Sonos", "model": "Arc",
         "ip": "192.0.2.32", "mac": mac("32"), "sonos_udn": sonos.udn(mac("32")),
         "controls": {"observe": ["sonos"], "control": ["sonos"]}}]
    data["routing"]["sources"]["example_cottage_computer"] = {"label": "Cottage computer"}
    room = {"sources": ["example_cottage_computer"],
            "display": {"device": COTTAGE_TV, "sources": {"example_cottage_computer": "hdmi1"}}}
    if audio:
        room["audio"] = COTTAGE_ARC
    data["routing"]["rooms"][COTTAGE] = room
    return data


class NativeItems(unittest.TestCase):
    def test_arc_exposes_native_channels_and_its_full_volume_range(self):
        data = house()
        conf = oh_render.build(data)
        items = conf.yaml[sonos.FILE].items
        self.assertEqual(items[f"{ARC}_player"]["channel"], f"sonos:Arc:{ARC}:control")
        self.assertEqual(oh_conf.linked(items[f"{ARC}_tv"]), [f"sonos:Arc:{ARC}:playlinein"])
        self.assertEqual(items[f"{ARC}_codec"]["channel"], f"sonos:Arc:{ARC}:codec")
        self.assertEqual(items[f"{ARC}_line_in"]["channel"], f"sonos:Arc:{ARC}:linein")
        self.assertFalse(oh_conf.is_guarded(items[f"{ARC}_volume"]))
        self.assertFalse(items[f"{ARC}_volume"]["autoupdate"])
        control = sonos.remote(data["devices"][0])
        self.assertEqual(control["volume"]["max"], 100)
        self.assertEqual(items[f"{ARC}_volume"]["metadata"]["stateDescription"]["config"]["max"], 100)
        self.assertNotIn("power", control)
        self.assertIn("tv", sonos.keys(data["devices"][0]))

    def test_observer_stays_display_only_and_read_only_guards_native_control(self):
        observed = oh_render.build(house(False)).yaml[sonos.FILE].items
        self.assertTrue(oh_conf.is_guarded(observed[f"{ARC}_volume"]))
        self.assertNotIn(f"{ARC}_key", observed)
        self.assertIsNone(sonos.remote(house(False)["devices"][0]))
        files = oh_render.render(house())
        quiet = oh_render.read_only(files)
        self.assertIn("const SEND = false;", quiet[sonos.RULE])
        items = yaml.safe_load(quiet[sonos.FILE])["items"]
        self.assertTrue(all(oh_conf.is_guarded(i) for i in items.values() if oh_conf.linked(i)))

    def test_bonded_amp_without_sonos_is_not_a_command_target(self):
        device = copy.deepcopy(house()["devices"][0])
        device.update(kind="amp", model="Amp", controls={"observe": ["network_ping"]})
        self.assertIsNone(sonos.remote(device))
        self.assertNotIn("tv", sonos.keys(device))

    @unittest.skipUnless(shutil.which("node"), "node needed for native link profiles")
    def test_native_volume_and_tv_profiles_reject_unrelated_commands(self):
        conf = oh_render.build(house())
        items = conf.yaml[sonos.FILE].items
        tv = next(iter(items[f"{ARC}_tv"]["channels"].values()))["toHandlerScript"][1:]
        script = "const vm=require('vm'); const p=JSON.parse(process.argv[1]); " \
                 "process.stdout.write(JSON.stringify(p.map(([code,values])=>values.map(input=>vm.runInNewContext(code,{input})))));"
        volume = next(iter(items[f"{ARC}_volume"]["channels"].values()))["toHandlerScript"][1:]
        probes = [[volume, ["50", "75", "100", "101", "-1", "INCREASE", "DECREASE", "UNDEF"]],
                  [tv, [UDN, "RINCON_ANOTHER", "http://example.invalid/", "ON"]]]
        run = subprocess.run([shutil.which("node"), "-e", script, json.dumps(probes)], text=True,
                             capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(run.stdout), [["50", "75", "100", "100", None, None, None, None],
                                                [UDN, None, None, None]])

    @unittest.skipUnless(shutil.which("node"), "node needed for native key-rule stand-in")
    def test_key_rule_routes_only_native_allowlisted_commands(self):
        rule = sonos.files(house()["devices"], oh_conf.House(house()))[sonos.RULE]
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/sonos_control_harness.js")],
                             input=rule, text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(run.stdout)
        self.assertEqual(result["sent"], [
            [f"{ARC}_player", "PREVIOUS"], [f"{ARC}_player", "NEXT"],
            [f"{ARC}_player", "PAUSE"], [f"{ARC}_player", "PLAY"],
            [f"{ARC}_night", "ON"], [f"{ARC}_speech", "OFF"],
            [f"{ARC}_tv", UDN]])
        self.assertEqual(result["unknownSent"], [])
        self.assertEqual(result["readOnlySent"], [])


class RoomVolume(unittest.TestCase):
    """A room whose sound is an Arc beside its TV drives the Arc, never the
    TV, even when the Arc is in a room nested in the room's Building."""

    def model(self, data):
        problems = oh_render.validate(data)
        self.assertEqual(problems, [])
        house_ = oh_conf.House(data)
        return remote_model.build(house_), oh_site_contract.compile(house_, oh_render.build(data))

    def test_building_room_lists_its_nested_arc_and_the_gateway_grants_it(self):
        model, authority = self.model(cottage())
        room = next(row for row in model["rooms"] if row["id"] == COTTAGE)
        self.assertEqual(room["audio"], COTTAGE_ARC)
        self.assertIn(COTTAGE_ARC, room["devices"])
        self.assertIn(COTTAGE_ARC + ".volume", authority["rooms"][COTTAGE]["actions"])
        # Without `audio` the display is the room's volume, as before.
        model, _ = self.model(cottage(audio=False))
        self.assertEqual(next(row for row in model["rooms"] if row["id"] == COTTAGE)["audio"], COTTAGE_TV)

    @unittest.skipUnless(shutil.which("node"), "node needed for the shared web/TUI backend")
    def test_building_volume_uses_arc_native_range_and_never_the_tv(self):
        model, _ = self.model(cottage())
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        run = subprocess.run([shutil.which("node"), str(AREA / "tests/sonos_remote_harness.js"),
                              COTTAGE, COTTAGE_TV, COTTAGE_ARC], input=json.dumps(model), text=True, env=env,
                             capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        volumes = [body for item, body in result["posted"] if item.endswith("_volume")]
        self.assertEqual(volumes, ["50", "75", "100", "100"])
        self.assertTrue(result["posted"])
        self.assertTrue(all(item.startswith(COTTAGE_ARC + "_") for item, _ in result["posted"]), result["posted"])
        self.assertEqual(result["volumeTarget"], COTTAGE_ARC)


class ContractStandIn(unittest.TestCase):
    def setUp(self):
        data = house()
        observer = copy.deepcopy(data["devices"][0])
        observer.update(id="example_bonded_amp", model="Amp", kind="amp", ip="192.0.2.11", mac=mac("11"),
                        sonos_udn="RINCON_02000000001101400", controls={"observe": ["sonos"]})
        data["devices"].append(observer)
        self.data = data
        self.check = controls.Check(data, log=lambda _: None)
        for stand_in in self.check.stand_ins.values():
            self.addCleanup(stand_in.server.stop)

    def test_conf_has_only_selected_loopback_contract_and_fake_identities(self):
        files = self.check.conf()
        text = "\n".join(v for v in files.values() if isinstance(v, str))
        self.assertNotIn("example_bonded_amp", files[sonos.FILE] + files[sonos.RULE])
        for device in self.data["devices"]:
            self.assertNotIn(device["ip"], text)
            self.assertNotIn(device["sonos_udn"], text)
        document = yaml.safe_load(files[sonos.FILE])
        self.assertEqual(list(document["things"]), [f"http:url:{ARC}"])
        self.assertIn("127.0.0.1", document["things"][f"http:url:{ARC}"]["config"]["baseURL"])
        self.assertIn(sonos.simulation_udn(self.data["devices"][0]), text)
        self.assertIn("binding = http", files["services/addons.cfg"])
        self.assertNotIn("binding = sonos", files["services/addons.cfg"])

    def test_real_sonos_identity_fails_closed_even_without_real_ip(self):
        with mock.patch.object(sonos, "aim", side_effect=lambda files, device, address: files):
            with self.assertRaisesRegex(oh_sandbox.SandboxError, "real Sonos UDN"):
                self.check.conf()

    def test_http_switch_channels_have_required_native_wire_values(self):
        document = yaml.safe_load(self.check.conf()[sonos.FILE])
        channels = document["things"][f"http:url:{ARC}"]["channels"]
        switches = {name: channel["config"] for name, channel in channels.items() if channel["type"] == "switch"}
        self.assertEqual(set(switches), {"mute", "night", "speech", "line_in"})
        stand_in = self.check.stand_ins[ARC]
        stand_in.server.start()

        def get(extension):
            with urllib.request.urlopen("http://" + stand_in.server.address + extension, timeout=2) as response:
                return response.read().decode()

        # Pinned HTTP 5.2.3 requires both mappings, including READONLY switches.
        # Check their actual wire values against the loopback contract, not
        # a numeric/bool substitute that the binding would fail to recognize.
        for name, config in switches.items():
            self.assertEqual((config["onValue"], config["offValue"]), ("ON", "OFF"), name)
            if name == "line_in":
                self.assertEqual(config["mode"], "READONLY")
                self.assertNotIn("commandExtension", config)
                self.assertEqual(get(config["stateExtension"]), config["onValue"])
                continue
            self.assertEqual(config["mode"], "READWRITE")
            for value in (config["onValue"], config["offValue"]):
                extension = config["commandExtension"].replace("%2$s", value)
                self.assertEqual(get(extension), "OK")
                self.assertEqual(get(config["stateExtension"]), value)

    def test_http_channel_item_types_match_the_native_schema(self):
        document = yaml.safe_load(self.check.conf()[sonos.FILE])
        channels = document["things"][f"http:url:{ARC}"]["channels"]
        # Item types declared by HTTP 5.2.3's native channel definitions.
        item_types = {"string": "String", "switch": "Switch", "dimmer": "Dimmer", "number": "Number"}
        self.assertEqual(channels["volume"]["type"], "dimmer")
        for name, item in document["items"].items():
            for uid in oh_conf.linked(item):
                channel = channels[uid.rsplit(":", 1)[-1]]
                self.assertEqual(item["type"], item_types[channel["type"]], name)

    def test_loopback_contract_accepts_only_expected_native_values(self):
        player = oh_sonos_sim.Player("RINCON_FAKE")
        self.assertTrue(player.command("volume", "75"))
        self.assertEqual(player.volume, 75)
        self.assertFalse(player.command("volume", "101"))
        self.assertFalse(player.command("tv", "RINCON_REAL_OTHER"))
        self.assertTrue(player.command("tv", "RINCON_FAKE"))
        self.assertTrue(player.command("player", "PAUSE"))
        self.assertEqual(player.read("state"), "PAUSED_PLAYBACK")
        self.assertFalse(player.command("player", "ARBITRARY"))


if __name__ == "__main__":
    unittest.main()
