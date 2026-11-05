"""The sandbox's and the boot check's read-only conf (oh_render.read_only)
commands nothing, for every integration in every phase it may be listed in;
the boot check's reaches no device, and the sandbox's holds no session
(oh_render.unaddressed)."""
import copy
import sys
import unittest
from pathlib import Path

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_integrations as integrations  # noqa: E402
import oh_render as render  # noqa: E402
from oh_conf import RULE_SENDS, RULE_SILENT, is_guarded, linked  # noqa: E402

#: Demo MAC addresses come from the example house; the package guard keeps
#: every other MAC out of published files.
MACS = [device["mac"] for device in yaml.safe_load((AREA / "instance/devices.yaml").read_text())["devices"]
        if device.get("mac")]
#: Devices other than the one under test that an integration needs.
MATRIX = {"id": "den_matrix", "room": "den", "kind": "hdmi_matrix", "make": "BZBGEAR", "model": "BK-808",
          "ip": "192.0.2.20", "mac": MACS[1], "controls": {"control": ["bk808_http"]}}
#: Per integration: the device fields it needs beyond id, room and address.
DEVICE = {
    "network_ping": {"kind": "computer", "model": "Computer"},
    "sony_rest": {"kind": "tv", "make": "Sony", "model": "XR-TEST"},
    "sony_ip_control": {"kind": "tv", "make": "Sony", "model": "XR-TEST"},
    "cast": {"kind": "streamer", "make": "Google", "model": "Chromecast"},
    "androidtv_googletv": {"kind": "tv", "make": "Sony", "model": "XR-TEST"},
    "pyatv_http": {"kind": "streamer", "make": "Apple", "model": "AppleTV14,1"},
    # A Sonos UDN is RINCON_, its MAC's hex digits and the port (oh_inventory checks it).
    "sonos": {"kind": "soundbar", "make": "Sonos", "model": "Arc", "sonos_udn": "RINCON_" + MACS[0].replace(":", "").upper() + "01400"},
    "denonmarantz_http": {"kind": "avr", "make": "Denon", "model": "AVR-X3800H"},
    "heos": {"kind": "avr", "make": "Denon", "model": "AVR-X3800H", "heos_pid": 12345},
    "wiim_http": {"kind": "streamer", "make": "WiiM", "model": "Pro"},
    "sony_adcp": {"kind": "projector", "make": "Sony", "model": "VPL-VW915ES"},
    "bk808_http": {"kind": "hdmi_matrix", "make": "BZBGEAR", "model": "BK-808"},
    "ukm404_http": {"kind": "usb_matrix", "model": "UKM-404"},
    "vrroom_http": {"kind": "hdmi_switch", "make": "HDFury", "model": "VRROOM"},
    "avpro_telnet": {"kind": "hdmi_matrix", "make": "AVPro Edge", "model": "AC-MX-44X"},
    "unifi_protect": {"kind": "camera", "make": "Ubiquiti", "model": "G4 Bullet",
                      "protect": {"device_id": "0123456789abcdef01234567"}},
    "drhdmi_http": {"kind": "hdmi_processor", "make": "HDFury", "model": "Dr.HDMI"},
    "ir": {"kind": "disc_player", "make": "Sony", "model": "UBP-X800M2"},
    "bk808_cec": {"kind": "disc_player", "make": "Sony", "model": "UBP-X800M2", "cec": {"matrix": "den_matrix", "input": 3}},
}


def phases(module) -> list[str]:
    """The phases devices.yaml may list the integration under."""
    return [module.PHASE] + (["control"] if getattr(module, "CONTROL", False) and module.PHASE != "control" else [])


HOUSES = {(module.NAME, phase) for module in integrations.REGISTRY for phase in phases(module)}


def house(name: str, phase: str) -> dict:
    """A fictional one-room house whose device lists `name` under `phase`
    (and under its own phase too, which `control` needs)."""
    module = next(m for m in integrations.REGISTRY if m.NAME == name)
    controls = {module.PHASE: [name]}
    if phase != module.PHASE:
        controls[phase] = [name]
    if name == "sony_ip_control":
        controls["observe"] = ["sony_rest"]
    if name == "heos":
        controls["control"] = ["denonmarantz_http", "heos"]
        controls["observe"] = ["denonmarantz_http"]
    device = {"id": "den_device", "room": "den", "ip": "192.0.2.10", "mac": MACS[0],
              **copy.deepcopy(DEVICE[name]), "controls": controls}
    data = {"version": 1, "rooms": {"den": {"label": "Den", "tag": "Room"}}, "devices": [device]}
    if name == "unifi_protect":
        data["protect"] = {"host": "192.0.2.1"}
    if name == "bk808_cec":
        data["devices"].append(copy.deepcopy(MATRIX))
        data["routing"] = {"hdmi_matrix": "den_matrix", "hdmi_off_input": 8, "usb_off_host": 4,
                           "sources": {"den_device": {"label": "Disc", "hdmi_input": 3}},
                           "rooms": {"den": {"hdmi_output": 1, "sources": ["den_device"]}}}
    return data


def commanding(files: dict) -> list[str]:
    """Every Item link in a conf that could carry a command to a device, and
    every rule that could still send."""
    documents = [yaml.safe_load(text) or {} for path, text in files.items()
                 if path.startswith("yaml/") and path.endswith(".yaml")]
    readonly = {f"{uid}:{channel}" for document in documents for uid, thing in (document.get("things") or {}).items()
                for channel, body in (thing.get("channels") or {}).items()
                if ((body or {}).get("config") or {}).get("mode") == "READONLY"}
    found = [f"{uid} is not READONLY" for document in documents for uid, thing in (document.get("things") or {}).items()
             if uid.startswith("http:") for channel in (thing.get("channels") or {})
             if f"{uid}:{channel}" not in readonly]
    for document in documents:
        for name, item in (document.get("items") or {}).items():
            for uid in linked(item):
                if uid not in readonly and not render.safe_link(uid, (item.get("channels") or {}).get(uid), readonly):
                    found.append(f"Item {name} commands {uid}")
    for path, text in files.items():
        if path.startswith("automation/js/") and isinstance(text, str):
            if RULE_SENDS in text or (RULE_SILENT not in text and path not in render.ITEM_ONLY_RULES):
                found.append(f"{path} may send")
    return found


class EveryIntegration(unittest.TestCase):
    def test_every_integration_in_every_phase_commands_nothing_read_only(self):
        for name, phase in sorted(HOUSES):
            with self.subTest(integration=name, phase=phase):
                data = house(name, phase)
                self.assertEqual(render.validate(data), [])
                files = render.render(data)
                read_only = render.read_only(files)
                self.assertEqual(commanding(read_only), [])
                self.assertEqual(set(read_only), set(files))
                for path, text in files.items():
                    if path.startswith("automation/js/") and isinstance(text, str) and RULE_SENDS in text:
                        self.assertEqual(read_only[path], text.replace(RULE_SENDS, RULE_SILENT))

    def test_every_registered_integration_is_covered(self):
        self.assertEqual({name for name, _ in HOUSES}, set(DEVICE))


class FailsClosed(unittest.TestCase):
    ADDONS = {"services/addons.cfg": "automation = jsscripting\n"}

    @staticmethod
    def items(files: dict, path: str = "yaml/x.yaml") -> dict:
        return yaml.safe_load(files[path])["items"]

    def test_an_item_of_any_binding_on_a_read_write_channel_is_guarded(self):
        text = yaml.safe_dump({"things": {"newbinding:thing:den_tv": {"channels": {"power": {"type": "switch"}}},
                                          "http:url:den_box": {"channels": {"power": {"type": "switch"}}}},
                               "items": {"den_tv_power": {"type": "Switch", "channel": "newbinding:thing:den_tv:power"},
                                         "den_box_power": {"type": "Switch", "channel": "http:url:den_box:power"},
                                         "den_tv_relay": {"type": "Switch", "channels": {"newbinding:thing:den_tv:power": {
                                             "profile": "transform:JS", "toItemScript": "|input",
                                             "commandFromItemScript": "|input"}}}}})
        read_only = render.read_only({"yaml/x.yaml": text, **self.ADDONS})
        items = self.items(read_only)
        self.assertTrue(is_guarded(items["den_tv_power"]))
        self.assertTrue(is_guarded(items["den_tv_relay"]))
        self.assertNotIn("commandFromItemScript", str(items["den_tv_relay"]))
        self.assertEqual(items["den_box_power"], {"type": "Switch", "channel": "http:url:den_box:power"})
        self.assertEqual(commanding(read_only), [])

    def test_guarding_needs_the_javascript_addon(self):
        text = yaml.safe_dump({"items": {"den_tv_power": {"type": "Switch", "channel": "sonos:Arc:den_tv:power"}}})
        with self.assertRaisesRegex(ValueError, "jsscripting"):
            render.read_only({"yaml/x.yaml": text})

    def test_a_send_declaration_is_silenced_however_it_is_spaced(self):
        for line in ("const SEND = true;", "  const SEND=true;", "const  SEND = true", "\tlet SEND = true ;  "):
            with self.subTest(line=line):
                out = render.read_only({"automation/js/x.js": f"// a rule\n{line}\nsend();\n"})
                self.assertNotRegex(out["automation/js/x.js"], r"SEND\s*=\s*true")
                self.assertIn(RULE_SILENT, out["automation/js/x.js"])

    def test_a_rule_without_a_send_declaration_is_refused(self):
        for text in ("send();\n", "const SEND = !READONLY;\n", "// const SEND = true; elsewhere\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "cannot be made read-only"):
                render.read_only({"automation/js/new_device.js": text})
        self.assertEqual(render.read_only({"automation/js/all_off.js": "items only\n"}),
                         {"automation/js/all_off.js": "items only\n"})

    def test_two_send_declarations_and_unreadable_formats_are_refused(self):
        with self.assertRaisesRegex(ValueError, "appears 2 times"):
            render.read_only({"automation/js/x.js": "const SEND = true;\nconst SEND=true;\n"})
        for path in ("things/x.things", "items/x.items", "automation/x.rules"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                render.read_only({path: "Switch x {channel=\"sonos:Arc:x:power\"}\n"})


def addresses(files: dict) -> set[str]:
    """Every IPv4 address and Sonos UDN in the files openHAB reads."""
    return {found for path, text in files.items() if isinstance(text, str) and not path.startswith("html/")
            for found in render.IPV4.findall(text) + render.SONOS_UDN.findall(text)}


class Unaddressed(unittest.TestCase):
    def test_every_integration_in_every_phase_reaches_nothing_in_the_boot_check(self):
        for name, phase in sorted(HOUSES):
            with self.subTest(integration=name, phase=phase):
                files = render.read_only(render.render(house(name, phase)))
                unaddressed = render.unaddressed(files)
                self.assertLessEqual(addresses(unaddressed), {render.NOWHERE, render.NO_SONOS})
                self.assertEqual(unaddressed[render.UPNP_CFG], render.NO_UPNP)
                self.assertEqual(commanding(unaddressed), [])

    def test_the_sandbox_leaves_only_sessions_to_the_runtime(self):
        self.assertEqual(render.SESSION_FILES,
                         {"yaml/cast.yaml", "yaml/googletv.yaml", "yaml/heos.yaml", "yaml/sonos.yaml"})
        data = house("androidtv_googletv", "control")
        data["devices"][0]["controls"]["observe"] = ["network_ping", "sony_rest", "cast"]
        self.assertEqual(render.validate(data), [])
        files = render.unaddressed(render.read_only(render.render(data)), render.SESSION_FILES)
        for path in ("yaml/cast.yaml", "yaml/googletv.yaml"):
            self.assertNotIn("192.0.2.10", files[path])
            self.assertIn(render.NOWHERE, files[path])
        for path in ("yaml/network.yaml", "yaml/sony.yaml"):
            self.assertIn("192.0.2.10", files[path])
        self.assertEqual(files[render.UPNP_CFG], render.NO_UPNP)

    def test_only_whole_addresses_change_and_the_remote_keeps_its_own(self):
        files = {"yaml/x.yaml": "url: http://192.0.2.10:8080/x v1.2.3.4.5 udn: RINCON_0123456789AB01400\n",
                 "html/remote/x.js": "const HOST = '127.0.0.1';\n", "icon.png": b"\x89PNG 192.0.2.10"}
        out = render.unaddressed(files)
        self.assertEqual(out["yaml/x.yaml"], f"url: http://{render.NOWHERE}:8080/x v1.2.3.4.5 udn: {render.NO_SONOS}\n")
        self.assertEqual(out["html/remote/x.js"], files["html/remote/x.js"])
        self.assertEqual(out["icon.png"], files["icon.png"])


if __name__ == "__main__":
    unittest.main()
