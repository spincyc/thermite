"""Native Sonos Arc Items, command routing and room volume, with no device access.

Ported from the private site's tests when the application moved here; the
house is fictional. The Arc's own 0-100 scale is also covered with every
other device's in tests/test_volume_native.py.
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
from oh_features import remote_activity, remote_model  # noqa: E402
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
        # A volume off the Arc's own scale is dropped, never clamped.
        self.assertEqual(json.loads(run.stdout), [["50", "75", "100", None, None, None, None, None],
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


class AudioPage(unittest.TestCase):
    """Every controlled Arc or Amp: its remote, and its page's settings, each
    a native channel that takes only what that channel takes."""

    def amp(self, control=True):
        device = copy.deepcopy(house(control)["devices"][0])
        device.update(id="example_amp", kind="amp", model="Amp", ip="192.0.2.11", mac=mac("11"),
                      sonos_udn=sonos.udn(mac("11")))
        return device

    def test_a_standalone_amp_has_a_remote_and_a_page_without_an_arcs_extras(self):
        amp = self.amp()
        self.assertEqual(sonos.remote(amp), {"key": "example_amp_key", "mute": "example_amp_mute",
                                             "volume": {"item": "example_amp_volume", **sonos.VOLUME}})
        page = sonos.audio(amp)
        self.assertEqual(page["transport"], ["previous", "play_pause", "next"])
        self.assertEqual([entry["item"] for entry in page["settings"]],
                         [f"example_amp_{suffix}" for suffix in sonos.SETTINGS])
        self.assertNotIn("facts", page)
        self.assertIsNone(sonos.audio(self.amp(control=False)))

    def test_an_amps_remote_has_no_tv_key(self):
        self.assertEqual((sonos.remote_of(house()["devices"][0]), sonos.remote_of(self.amp())), ("sonos", "sonos_amp"))
        layouts = (AREA / "web/remote/remotes.js").read_text()
        amp = layouts[layouts.index("    sonos_amp: {"):]
        self.assertNotIn('"tv"', amp[:amp.index("\n    },")])
        self.assertNotIn("tv", sonos.keys(self.amp()))

    def test_the_feed_keeps_an_arcs_tv_audio_and_leaves_out_track_details(self):
        data = house()
        feed = remote_activity.manifest(oh_conf.House(data), oh_render.build(data))["items"]
        self.assertLessEqual({f"{ARC}_line_in", f"{ARC}_codec", f"{ARC}_title"}, set(feed))
        self.assertNotIn(f"{ARC}_album", feed)

    def test_an_arcs_page_adds_tv_audio_and_its_bonded_speakers_levels(self):
        page = sonos.audio(house()["devices"][0])
        self.assertEqual(page["transport"], ["previous", "play_pause", "next", "tv"])
        shown = {entry["item"].removeprefix(ARC + "_"): entry for entry in page["settings"]}
        self.assertEqual(list(shown), [*sonos.SETTINGS, *sonos.ARC_SETTINGS])
        self.assertEqual(shown["sub_gain"], {"item": f"{ARC}_sub_gain", "label": "Subwoofer Level", "section": "sound",
                                             "kind": "number", "min": -15, "max": 15, "step": 1})
        self.assertEqual(shown["repeat"]["options"], [["OFF", "Off"], ["ONE", "One"], ["ALL", "All"]])
        items = oh_render.build(house()).yaml[sonos.FILE].items
        self.assertEqual(oh_conf.linked(items[f"{ARC}_height"]), [f"sonos:Arc:{ARC}:heightlevel"])
        self.assertEqual(items[f"{ARC}_bass"]["tags"], ["Setpoint", "Level"])
        self.assertEqual(items[f"{ARC}_album"]["channel"], f"sonos:Arc:{ARC}:currentalbum")
        for suffix in [*sonos.SETTINGS, *sonos.ARC_SETTINGS]:
            self.assertFalse(oh_conf.is_guarded(items[f"{ARC}_{suffix}"]), suffix)
            self.assertFalse(items[f"{ARC}_{suffix}"]["autoupdate"], suffix)
        # Observed only, none of them is rendered.
        observed = oh_render.build(house(False)).yaml[sonos.FILE].items
        self.assertFalse({f"{ARC}_{suffix}" for suffix in [*sonos.SETTINGS, *sonos.ARC_SETTINGS]} & set(observed))

    @unittest.skipUnless(shutil.which("node"), "node needed for native link profiles")
    def test_a_level_or_a_choice_passes_its_link_only_when_its_channel_takes_it(self):
        items = oh_render.build(house()).yaml[sonos.FILE].items
        script = "const vm=require('vm'); const p=JSON.parse(process.argv[1]); " \
                 "process.stdout.write(JSON.stringify(p.map(([code,values])=>values.map(input=>vm.runInNewContext(code,{input})))));"

        def link(suffix):
            return next(iter(items[f"{ARC}_{suffix}"]["channels"].values()))["toHandlerScript"][1:]
        probes = [[link("bass"), ["-10", "10", "0", "11", "-11", "2.5", "UP"]],
                  [link("sub_gain"), ["-15", "15", "16"]],
                  [link("repeat"), ["OFF", "ONE", "ALL", "all", "SHUFFLE"]],
                  [link("music_mode"), ["0", "1", "2"]]]
        run = subprocess.run([shutil.which("node"), "-e", script, json.dumps(probes)], text=True,
                             capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(run.stdout), [["-10", "10", "0", None, None, None, None], ["-15", "15", None],
                                                ["OFF", "ONE", "ALL", None, None], ["0", "1", None]])


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

    def test_an_arcs_free_text_is_what_an_admin_may_keep_from_guests(self):
        """Title, artist and audio format (a text recipe) and playback, the
        player and TV audio (none) are all free text, which a Guest's
        :without_text view leaves out (oh_identity.topics), and only they:
        the volume stays."""
        _, authority = self.model(cottage())
        states = authority["states"]
        for suffix in ("title", "artist", "codec", "state", "player", "tv"):
            self.assertTrue(states[f"{COTTAGE_ARC}_{suffix}"].get("free_text"), suffix)
        self.assertTrue(all(spec.get("free_text") for spec in states.values() if spec["kind"] == "text"))
        self.assertFalse(states[f"{COTTAGE_ARC}_volume"].get("free_text"))

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
        # The settings poll on a slower Thing of their own; both reach only the stand-in.
        self.assertEqual(list(document["things"]), [f"http:url:{ARC}", f"http:url:settings:{ARC}"])
        for thing in document["things"].values():
            self.assertIn("127.0.0.1", thing["config"]["baseURL"])
        self.assertEqual(document["things"][f"http:url:settings:{ARC}"]["config"]["refresh"], sonos.SETTINGS_REFRESH)
        self.assertIn(sonos.simulation_udn(self.data["devices"][0]), text)
        self.assertIn("binding = http", files["services/addons.cfg"])
        self.assertNotIn("binding = sonos", files["services/addons.cfg"])

    def test_real_sonos_identity_fails_closed_even_without_real_ip(self):
        with mock.patch.object(sonos, "aim", side_effect=lambda files, device, address: files):
            with self.assertRaisesRegex(oh_sandbox.SandboxError, "real Sonos UDN"):
                self.check.conf()

    def test_http_switch_channels_have_required_native_wire_values(self):
        document = yaml.safe_load(self.check.conf()[sonos.FILE])
        channels = {name: channel for thing in document["things"].values() for name, channel in thing["channels"].items()}
        switches = {name: channel["config"] for name, channel in channels.items() if channel["type"] == "switch"}
        settings = sonos.settings(self.data["devices"][0])
        self.assertEqual(set(switches), {"mute", "line_in", *(name for name, (_, kind, *_) in settings.items()
                                                              if kind == "Switch")})
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
        channels = {uid + ":" + name: channel for uid, thing in document["things"].items()
                    for name, channel in thing["channels"].items()}
        # Item types declared by HTTP 5.2.3's native channel definitions.
        item_types = {"string": "String", "switch": "Switch", "dimmer": "Dimmer", "number": "Number"}
        self.assertEqual(channels[f"http:url:{ARC}:volume"]["type"], "dimmer")
        for name, item in document["items"].items():
            for uid in oh_conf.linked(item):
                channel = channels[uid]
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
