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
                         ["openhab", "grafana", "prometheus", "alertmanager", "ntfy"])
        self.assertEqual([tool["port"] for tool in result["tools"]], [13601, 13610, 13606, 13611, 13612])
        self.assertEqual([tool["path"] for tool in result["tools"]], ["/settings/", "/", "/query", "/", "/"])
        self.assertEqual([tool["localOnly"] for tool in result["tools"]], [True, False, True, True, True])
        for tool in result["tools"]:
            self.assertEqual(set(tool), {"id", "label", "description", "scheme", "port", "path", "localOnly"})
            self.assertEqual(tool["scheme"], "http")
            self.assertIs(type(tool["localOnly"]), bool)
            self.assertTrue(tool["label"])
            self.assertTrue(tool["description"])

    def test_native_administration_is_direct_only_for_explicit_native_access(self):
        native = remote_system.manifest({}, "5.2.3", mode="native")
        self.assertFalse(native["tools"][0]["localOnly"])
        authenticated = remote_system.manifest({}, "5.2.3", mode="authenticated")
        self.assertTrue(authenticated["tools"][0]["localOnly"])
        self.assertEqual(native["tools"][1:], authenticated["tools"][1:])
        with self.assertRaises(ValueError):
            remote_system.manifest({}, "5.2.3", mode="automatic")

    def test_actual_host_overrides_replace_defaults_without_serializing_private_facts(self):
        sentinel = "-".join(("private", "credential", "sentinel"))
        host = {"http_port": 14001, "metrics": {"grafana_port": 14002, "prometheus_port": 14003,
                "alertmanager_port": 14004, "ntfy_port": 14005}, "hostname": "private-host.invalid",
                "root": "/private/site/path", "password": sentinel}
        result = remote_system.manifest(host, "5.3.0-SNAPSHOT")
        self.assertEqual([tool["port"] for tool in result["tools"]], list(range(14001, 14006)))
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

    def test_selected_reads_host_from_site_and_version_from_program_only(self):
        scratch = Path(os.environ.get("TMPDIR", AREA.parents[1] / ".scratch/thermite_system_metadata"))
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary)
            site, program = root / "site", root / "program"
            (site / "instance").mkdir(parents=True)
            (program / "instance").mkdir(parents=True)
            host = ("root: /srv/thermite\ntimezone: Etc/UTC\nhttps_port: 13602\nuid: 9001\ngid: 9001\n")
            (site / "instance/host.yaml").write_text(host + "http_port: 14001\nmetrics:\n  grafana_port: 14002\n")
            (program / "instance/host.yaml").write_text(host + "http_port: 15001\n")
            (site / "versions.json").write_text(json.dumps({"openhab": {"version": "0.0.0"}}))
            (program / "versions.json").write_text(json.dumps({"openhab": {"version": "5.2.3"}}))
            result = remote_system.selected(site=site, program=program)
            self.assertEqual(result["runtime"]["version"], "5.2.3")
            self.assertEqual([tool["port"] for tool in result["tools"][:2]], [14001, 14002])
            (program / "versions.json").write_text("{}")
            with self.assertRaises(ValueError):
                remote_system.selected(site=site, program=program)
            (program / "versions.json").write_text(json.dumps({"openhab": {"version": "5.2.3"}}))
            # The site's host file is validated by the one host loader.
            (site / "instance/host.yaml").write_text("http_port: 14001\n")
            with self.assertRaisesRegex(ValueError, "valid host file.*missing timezone"):
                remote_system.selected(site=site, program=program)


if __name__ == "__main__":
    unittest.main()
