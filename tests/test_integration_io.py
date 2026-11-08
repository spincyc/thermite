"""Per-device contexts plus native fault tests for the production I/O helper."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_conf import House  # noqa: E402
from oh_integrations import _io, avpro_telnet, pyatv_http, sony_adcp, sony_ip_control  # noqa: E402
from oh_testing import controls  # noqa: E402


def inventory(module):
    kind, make, model = {
        avpro_telnet: ("hdmi_matrix", "AVPro Edge", "AC-MX-44X"),
        pyatv_http: ("streamer", "Apple", "AppleTV14,1"),
        sony_adcp: ("projector", "Sony", "VPL-VW915ES"),
        sony_ip_control: ("tv", "Sony", "XR-TEST"),
    }[module]
    observe = ["sony_rest"] if module is sony_ip_control else [module.NAME] if module is sony_adcp else []
    return {"version": 1, "rooms": {"den": {"label": "Den", "tag": "Room"},
                                     "attic": {"label": "Attic", "tag": "Room"}},
            "devices": [{"id": "den_device", "room": "den", "kind": kind, "make": make, "model": model,
                         "ip": "192.0.2.10",
                         "controls": {"observe": observe, "control": [module.NAME]}}]}


def projectors():
    house = inventory(sony_adcp)
    house["devices"].append({**house["devices"][0], "id": "attic_device", "room": "attic",
                             "ip": "192.0.2.11",
                             "controls": {"observe": [sony_adcp.NAME]}})
    return house


class Embedding(unittest.TestCase):
    def test_the_shared_device_parts_are_optional_but_once_and_after_the_io(self):
        self.assertEqual(_io.embed(f"a\n{_io.MARK}\nb\n"), f"a\n{_io.SOURCE.read_text()}\nb\n")
        for bad in (f"{_io.MARK}\n{_io.DEVICE_MARK}\n{_io.DEVICE_MARK}\n", f"{_io.DEVICE_MARK}\n{_io.MARK}\n",
                    f"{_io.MARK}\nx = 1; {_io.DEVICE_MARK}\n", f"{_io.DEVICE_MARK}\n"):
            with self.subTest(source=bad), self.assertRaises(ValueError):
                _io.embed(bad)


class DeviceContexts(unittest.TestCase):
    def test_control_fixture_excludes_observer_device_scripts(self):
        check = controls.Check(projectors(), log=lambda _: None)
        self.addCleanup(lambda: [stand_in.server.stop() for stand_in in check.stand_ins.values()])
        files = check.conf()
        self.assertIn(sony_adcp.rule_path({"id": "den_device"}), files)
        self.assertNotIn(sony_adcp.rule_path({"id": "attic_device"}), files)

    def test_one_context_per_device_with_independent_poll_ids_and_retargeting(self):
        for module in (avpro_telnet, pyatv_http, sony_adcp, sony_ip_control):
            with self.subTest(integration=module.NAME):
                house = inventory(module)
                original = house["devices"][0]
                second = {**original, "id": "other_device", "ip": "192.0.2.240"}
                devices = [original, second]
                files = module.files(devices, House(house))
                self.assertEqual(set(files), {module.rule_path(d) for d in devices})
                for device in devices:
                    text = files[module.rule_path(device)]
                    line = next(line for line in text.splitlines() if line.startswith("const DEVICES = "))
                    self.assertEqual([d["id"] for d in json.loads(line[len("const DEVICES = "):-1])], [device["id"]])
                    self.assertIn("${DEVICES[0].id}_poll", text)
                aimed = module.aim(files, original, "127.0.0.1:40123")
                self.assertEqual(aimed[module.rule_path(second)], files[module.rule_path(second)])
                self.assertNotEqual(aimed[module.rule_path(original)], files[module.rule_path(original)])


@unittest.skipUnless(all(os.environ.get(name) for name in ("THERMITE_JAVA", "THERMITE_ECJ", "THERMITE_RUNTIME", "THERMITE_GRAAL")),
                     "native fault test needs cached pinned Java/runtime/ECJ/Graal paths")
class NativeIo(unittest.TestCase):
    def test_real_jvm_deadlines_frame_bounds_cookies_and_worker_ownership(self):
        jars = sorted(Path(os.environ["THERMITE_RUNTIME"]).rglob("*.jar"))
        jars += sorted(Path(os.environ["THERMITE_GRAAL"]).rglob("*.jar"))
        classpath = os.pathsep.join(str(p) for p in jars)
        java = os.environ["THERMITE_JAVA"]
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([java, "-jar", os.environ["THERMITE_ECJ"], "-21", "-proc:none", "-cp", classpath,
                                     "-d", tmp, str(AREA / "tests/integration_io/NativeTest.java")],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([java, "-cp", tmp + os.pathsep + classpath, "NativeTest", str(AREA / "lib/oh_integrations/_io.js")],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("deadlines, cookie ownership and disposal passed", result.stdout)
