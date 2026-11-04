"""Vendor facts live in their integration modules; generic code reads them
through the registries (the contract in lib/oh_integrations/__init__.py)."""

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_conf  # noqa: E402
import oh_control4  # noqa: E402
import oh_discover  # noqa: E402
import oh_integrations as integrations  # noqa: E402
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_features import remote_diagnostics, threadpools  # noqa: E402
from oh_integrations import androidtv_googletv, unifi_protect  # noqa: E402

EXAMPLE = json.loads((AREA / "instance/devices.yaml").read_text())
#: Generic modules: none may name an integration. network_ping, the
#: vendor-neutral reachability check, is the inventory cross-check's one
#: exception (a device observing it needs its network Thing).
GENERIC = ("lib/oh_conf.py", "lib/oh_discover.py", "lib/oh_inventory.py", "lib/oh_render.py",
           "lib/oh_site_contract.py", "lib/oh_metrics/__init__.py", *sorted(
               str(path.relative_to(AREA)) for path in (AREA / "lib/oh_features").glob("*.py")))


class Generic(unittest.TestCase):
    def test_no_generic_module_names_an_integration(self):
        names = {module.NAME for module in integrations.REGISTRY} - {"network_ping"}
        for path in GENERIC:
            text = (AREA / path).read_text(encoding="utf-8")
            for name in sorted(names):
                with self.subTest(path=path, name=name):
                    self.assertIsNone(re.search(rf"[\"'/]{name}[\"'.]", text))

    def test_declared_collects_each_value_once_in_registry_order(self):
        first = types.SimpleNamespace(DISCOVER_PORTS=(2, 1))
        second = types.SimpleNamespace(DISCOVER_PORTS=(1, 3))
        self.assertEqual(integrations.declared("DISCOVER_PORTS", (first, types.SimpleNamespace(), second)), (2, 1, 3))


class Discovery(unittest.TestCase):
    def test_every_declared_port_and_service_is_probed_and_marks_av_gear(self):
        for module in integrations.REGISTRY:
            for port in getattr(module, "DISCOVER_PORTS", ()):
                self.assertIn(port, oh_discover.PORTS)
                self.assertTrue(oh_discover.is_av({"ports": [port]}), (module.NAME, port))
            for service in getattr(module, "DISCOVER_MDNS", ()):
                self.assertTrue(oh_discover.is_av({"mdns": {"services": [service]}}), (module.NAME, service))
        self.assertEqual(list(oh_discover.PORTS), sorted(set(oh_discover.PORTS)))
        self.assertFalse(oh_discover.is_av({"ports": [22, 80, 443]}))


class Keys(unittest.TestCase):
    def problems(self, change):
        data = copy.deepcopy(EXAMPLE)
        change(data)
        return oh_inventory.validate(data)

    def test_device_and_house_keys_come_from_their_owners(self):
        top, device = oh_inventory.keys()
        for module in (*integrations.REGISTRY, oh_control4):
            self.assertLessEqual(set(getattr(module, "DEVICE_KEYS", ())), device)
            self.assertLessEqual(set(getattr(module, "HOUSE_KEYS", ())), top)
        self.assertLessEqual({"notes", "control4"}, device)
        # A key nobody declares is refused, with a hint from the declared ones.
        self.assertIn("example_tv: unknown key 'sonos_ud'; did you mean sonos_udn?",
                      self.problems(lambda d: d["devices"][0].update(sonos_ud="x")))
        with mock.patch.object(integrations.sonos, "DEVICE_KEYS", ()):
            self.assertIn("example_tv: unknown key 'sonos_udn'",
                          " ".join(self.problems(lambda d: d["devices"][0].update(sonos_udn="x"))))
        with mock.patch.object(oh_control4, "HOUSE_KEYS", ()):
            self.assertTrue(any("unknown top-level key 'control4_rooms'" in problem
                                for problem in self.problems(lambda d: d.update(control4_rooms={}))))

    def test_control4_keys_are_validated_as_strictly_by_their_owner(self):
        def change(data):
            data["devices"][0]["c4_item"] = [12, 0, True]
            data["devices"][1]["c4_item"] = 12
            data.update(control4_stale=[13, 12, "x"], control4_rooms={"Den": "nowhere", "Hall": "example_west"},
                        control4_director="example_tv")
        found = self.problems(change)
        for expected in ("example_tv: c4_item 0 is not a Control4 item id",
                         "example_tv: c4_item True is not a Control4 item id",
                         "example_player: c4_item 12 already used by example_tv",
                         "control4_stale: c4_item 12 already used by example_tv",
                         "control4_stale: 'x' is not a Control4 item id",
                         "control4_rooms: 'Den' maps to 'nowhere', which is not in rooms"):
            self.assertIn(expected, found)
        self.assertEqual(len(found), 6, found)
        self.assertEqual(self.problems(lambda d: d.update(control4_stale=5)),
                         ["control4_stale must be a list of Control4 item ids"])
        self.assertEqual(self.problems(lambda d: d.update(control4_rooms=["x"])), ["control4_rooms must be a mapping"])

    def test_a_module_needing_a_mac_says_so(self):
        needing = {module.NAME for module in integrations.REGISTRY if getattr(module, "NEEDS_MAC", False)}
        self.assertEqual(needing, {"sonos"})


def android_house():
    data = copy.deepcopy(EXAMPLE)
    data["devices"].append({"id": "fixture_android", "room": "example_west", "kind": "tv", "model": "Example",
                            "ip": "192.0.2.210", "controls": {"control": [androidtv_googletv.NAME]}})
    return data


class HandlerPool(unittest.TestCase):
    def test_the_pool_follows_the_integrations_that_declare_it(self):
        self.assertNotIn(threadpools.FILE, oh_render.render(copy.deepcopy(EXAMPLE)))
        files = oh_render.render(android_house())
        self.assertEqual(files[threadpools.FILE].splitlines()[1:], [
            "# Scheduling headroom while Google TV connections block thingHandler.",
            "# Capacity mitigation only: does not fix or bound connection-task leaks.",
            "org.openhab.threadpool:thingHandler=32"])
        self.assertIn(threadpools.RULE, files)
        # Any integration may declare the need (reasons in REGISTRY order); none is named by the feature.
        with mock.patch.object(integrations.sony_ip_control, "HANDLER_POOL", 40, create=True), \
                mock.patch.object(integrations.sony_ip_control, "HANDLER_POOL_WHY", "a TV blocks it", create=True):
            text = oh_render.render(android_house())[threadpools.FILE]
        self.assertIn("while a TV blocks it and Google TV connections block thingHandler.", text)
        self.assertIn("thingHandler=40", text)

    @unittest.skipUnless(shutil.which("node"), "node is required for the production telemetry rule")
    def test_a_steady_pool_changes_one_item_per_sample_and_never_mixes_samples(self):
        # Each change is an events.log line; an UNDEF bracket on every
        # 15-second sample made two (about 11.5k a day).
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/threadpools_harness.js"), str(threadpools.SOURCE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["passed"], 5)

    def test_declared_house_items_and_item_only_rules_are_accepted(self):
        self.assertLessEqual({*threadpools.HOUSE_ITEMS, *unifi_protect.HOUSE_ITEMS},
                             set(integrations.declared("HOUSE_ITEMS", (*integrations.REGISTRY, threadpools))))
        self.assertLessEqual({threadpools.RULE, unifi_protect.RULE}, oh_render.ITEM_ONLY_RULES)


class Readback(unittest.TestCase):
    def test_route_diagnosis_reports_only_inputs_its_devices_read_back(self):
        data = copy.deepcopy(EXAMPLE)
        house = oh_conf.House(data)
        contract = oh_site_contract.compile(house, oh_render.build(copy.deepcopy(data)))
        reported = {name: spec["fields"]["reported"]["values"] for name, spec in contract["states"].items()
                    if spec.get("kind") == "json" and "reported" in spec.get("fields", {})}
        matrix, tv = integrations.bk808_http, integrations.sony_ip_control
        self.assertTrue(reported)
        for values in reported.values():
            self.assertEqual(values, [None, *matrix.READBACK_INPUTS, *tv.READBACK_INPUTS])
        self.assertEqual(tv.READBACK_INPUTS, ("tv", "hdmi1", "hdmi2", "hdmi3", "hdmi4"))


class Manifests(unittest.TestCase):
    def test_diagnostics_and_cameras_are_built_once_per_render(self):
        data = copy.deepcopy(EXAMPLE)
        data["protect"] = {"host": "192.0.2.1"}
        data["devices"].append({"id": "fixture_camera", "room": "example_west", "kind": "camera", "ip": "192.0.2.211",
                                "controls": {"control": [unifi_protect.NAME]},
                                "protect": {"device_id": "0123456789abcdef01234567"}})
        self.assertEqual(oh_render.validate(copy.deepcopy(data)), [])
        with mock.patch.object(remote_diagnostics, "_manifest", wraps=remote_diagnostics._manifest) as diagnostics, \
                mock.patch.object(unifi_protect, "descriptor", wraps=unifi_protect.descriptor) as cameras:
            conf = oh_render.build(copy.deepcopy(data))
            conf.render()
            oh_metrics.render(copy.deepcopy(data), {"http_port": 13601}, conf=conf)
            oh_site_contract.compile(oh_conf.House(copy.deepcopy(data)), conf)
        self.assertEqual(diagnostics.call_count, 1)
        self.assertEqual(cameras.call_count, 1)


if __name__ == "__main__":
    unittest.main()
