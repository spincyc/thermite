"""Configured System endpoints remain finite, portable and free of site secrets."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_ports
from oh_features import remote_system


class SystemMetadata(unittest.TestCase):
    def test_default_tools_have_exact_order_paths_and_loopback_policy(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = remote_system.manifest({}, "5.2.3")
        self.assertEqual(set(result), {"version", "runtime", "tools"})
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["runtime"], {"version": "5.2.3"})
        self.assertEqual([tool["id"] for tool in result["tools"]],
                         ["openhab", "grafana", "prometheus"])
        self.assertEqual([tool["port"] for tool in result["tools"]], [13602, 13611, 13607])
        self.assertEqual([tool["path"] for tool in result["tools"]], ["/settings/", "/", "/query"])
        self.assertEqual([tool["localOnly"] for tool in result["tools"]], [True, False, True])
        for tool in result["tools"]:
            self.assertEqual(set(tool), {"id", "label", "description", "scheme", "port", "path", "localOnly"})
            self.assertEqual(tool["scheme"], "http")
            self.assertIs(type(tool["localOnly"]), bool)
            self.assertTrue(tool["label"])
            self.assertTrue(tool["description"])

    def test_actual_host_overrides_replace_defaults_without_serializing_private_facts(self):
        sentinel = "-".join(("private", "credential", "sentinel"))
        host = {"http_port": 14001, "metrics": {"grafana_port": 14002, "prometheus_port": 14003},
                "hostname": "private-host.invalid",
                "root": "/private/site/path", "password": sentinel}
        result = remote_system.manifest(host, "5.3.0-SNAPSHOT")
        self.assertEqual([tool["port"] for tool in result["tools"]], list(range(14001, 14004)))
        encoded = json.dumps(result)
        for private in (host["hostname"], host["root"], host["password"]):
            self.assertNotIn(private, encoded)
        self.assertNotIn("url", result["tools"][0])
        self.assertFalse(any("healthy" in tool or "status" in tool or "installed" in tool for tool in result["tools"]))

    def test_collision_and_malformed_configuration_fail_before_metadata_is_emitted(self):
        for host in ({"http_port": oh_ports.DEFAULTS["grafana"]}, {"metrics": {"grafana_port": True}},
                     {"metrics": ["invalid"]}, [], None):
            with self.subTest(host=host), self.assertRaises(ValueError):
                remote_system.manifest(host, "5.2.3")
        for version in (None, True, "", "5.2.3\nprivate", "https://foreign.invalid/"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                remote_system.manifest({}, version)

    def test_described_is_of_the_host_and_versions_it_is_given_and_reads_no_file(self):
        host = {"http_port": 14001, "metrics": {"grafana_port": 14002}}
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("a file was read")), \
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("a file was read")):
            result = remote_system.described(host, {"openhab": {"version": "0.0.0"}})
        self.assertEqual(result, remote_system.manifest(host, "0.0.0"))
        self.assertEqual([tool["port"] for tool in result["tools"][:2]], [14001, 14002])
        for versions in ({}, {"openhab": None}, []):
            with self.subTest(versions=versions), self.assertRaisesRegex(ValueError, "pinned runtime version"):
                remote_system.described(host, versions)

    def test_without_them_the_ports_are_the_products_and_the_runtime_the_applications_own_pin(self):
        self.assertEqual(remote_system.PINS, AREA / "versions.json")
        pinned = json.loads(remote_system.PINS.read_text(encoding="utf-8"))["openhab"]["version"]
        self.assertEqual(remote_system.described(), remote_system.manifest({}, pinned))
        # Not the site's: one whose files say otherwise changes nothing.
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            (site / "instance").mkdir()
            (site / "instance/host.yaml").write_text("http_port: 14001\n")
            (site / "versions.json").write_text(json.dumps({"openhab": {"version": "0.0.0"}}))
            with mock.patch.dict(os.environ, {"THERMITE_SITE": str(site)}):
                self.assertEqual(remote_system.described(), remote_system.manifest({}, pinned))


if __name__ == "__main__":
    unittest.main()
