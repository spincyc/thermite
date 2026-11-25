"""instance/host.yaml has one reader, lib/oh_host.py: no part of the deploy,
the program or a site, and a file it refuses is reported by the verbs that
read it. What it accepts and refuses, setting by setting, is held where
each setting is tested (test_deploy, test_ports, test_shares, test_guest_http,
test_house_yaml, test_unit_invariants)."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_host  # noqa: E402
from oh_cli import health, status  # noqa: E402

#: What the reader stands below: it imports none of them, anywhere in it.
ABOVE = {"oh_deploy", "oh_install", "oh_cli", "oh_site", "oh_sandbox", "oh_paths"}


def imported(path: Path) -> set[str]:
    """The top-level name of every module a source imports, at its top or
    inside a function."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found


class Leaf(unittest.TestCase):
    def test_the_reader_imports_no_deploy_program_or_site(self):
        self.assertFalse(imported(AREA / "lib/oh_host.py") & ABOVE)

    def test_the_deploy_keeps_no_name_of_it(self):
        for name in ("load_host", "validate_host", "HostError", "HOST_KEYS", "HOST_REQUIRED", "METRICS_KEYS", "DEFAULT_ROOT"):
            self.assertFalse(hasattr(deploy, name), name)

    def test_a_refused_file_is_a_value_error_of_its_own(self):
        with self.assertRaisesRegex(oh_host.HostError, r"^instance/host\.yaml: missing timezone") as caught:
            oh_host.load(b"{}")
        self.assertIsInstance(caught.exception, ValueError)
        self.assertNotIsInstance(caught.exception, deploy.DeployError)

    def test_the_installation_root_defaults_to_the_readers(self):
        host = {"timezone": "Etc/UTC", "http_port": 13602, "https_port": 13603, "uid": 9001, "gid": 9001}
        self.assertEqual(deploy.installation(oh_host.validate(host)).root, Path(oh_host.DEFAULT_ROOT))
        self.assertEqual(deploy.installation({**host, "root": "/mnt/example"}).root, Path("/mnt/example"))


class Reported(unittest.TestCase):
    """A descriptor-less checkout whose host file names an unknown setting."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name)
        (self.area / "instance").mkdir()
        (self.area / "versions.json").write_text((AREA / "versions.json").read_text(encoding="utf-8"), encoding="utf-8")
        host = oh_host.load(AREA / "instance/host.yaml")
        (self.area / "instance/host.yaml").write_text(json.dumps({**host, "state_root": "/mnt/example"}), encoding="utf-8")
        self.problem = f"{self.area / 'instance/host.yaml'}: unknown setting state_root"

    def test_status_names_the_problem_and_fails(self):
        err = io.StringIO()
        with mock.patch.object(status, "AREA", self.area), contextlib.redirect_stderr(err):
            self.assertEqual(status.main([]), 2)
        self.assertTrue(err.getvalue().startswith(f"thermite status: {self.problem}"), err.getvalue())

    def test_health_of_production_names_the_problem_and_fails(self):
        err = io.StringIO()
        with mock.patch.object(health, "AREA", self.area), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            health.main(["--production"])
        self.assertEqual(caught.exception.code, 2)
        self.assertTrue(err.getvalue().startswith(f"error: {self.problem}"), err.getvalue())


class ShapeAndValidation(unittest.TestCase):
    """oh_host.SHAPE, schema(), HostFile TypedDict parity, optional defaults,
    version: 1, and multi-error collection."""

    def test_shape_covers_every_declared_top_level_and_nested_key(self):
        import oh_music
        import oh_ports
        import oh_shape
        import oh_shares
        from oh_access import guest

        top = oh_host.SHAPE.keys
        self.assertIsNotNone(top)
        self.assertEqual(frozenset(top), oh_host.KEYS)
        self.assertEqual(set(top), {"version", "timezone", "root", "http_port", "https_port", "uid", "gid",
                                    "gateway", "metrics", "guest_http", "music", "shares"})
        self.assertEqual(oh_host.REQUIRED, ("timezone",))
        self.assertEqual(set(top["gateway"].keys), set(oh_ports.GATEWAY_FIELDS))
        self.assertEqual(frozenset(top["metrics"].keys), oh_host.METRICS_KEYS)
        self.assertEqual(set(top["guest_http"].keys), set(guest.FIELDS))
        self.assertEqual(frozenset(top["music"].keys), oh_music.KEYS)
        self.assertEqual(frozenset(top["shares"].each.keys), oh_shares.KEYS)
        # oh_shape.rows() walks every top-level and nested key cleanly.
        paths = {path for path, _ in oh_shape.rows(top)}
        self.assertLessEqual({"version", "timezone", "gateway.port", "metrics.retention",
                              "guest_http.clients", "music.libraries",
                              "shares.<share name>.where"}, paths)

    def test_host_file_typed_dict_matches_shape_exactly(self):
        import oh_types
        from tests.test_house_types import keys_differ

        self.assertIs(oh_host.HostFile, oh_types.HostFile)
        self.assertEqual(keys_differ(oh_types.HostFile, oh_host.SHAPE.keys), [])

    def test_schema_generates_draft_2020_12_json_schema(self):
        doc = oh_host.schema(schema_id="https://example.test/host.schema.json")
        self.assertEqual(doc["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(doc["$id"], "https://example.test/host.schema.json")
        self.assertEqual(doc["title"], "thermite host file")
        self.assertEqual(doc["type"], "object")
        self.assertEqual(doc["required"], ["timezone"])
        self.assertFalse(doc["additionalProperties"])
        self.assertEqual(set(doc["properties"]), set(oh_host.KEYS))
        self.assertEqual(doc["properties"]["version"]["enum"], [1])
        self.assertEqual(doc["properties"]["shares"]["additionalProperties"]["required"],
                         ["type", "server", "where"])
        self.assertNotIn("$id", oh_host.schema())

    def test_version_and_optional_defaults_are_validated(self):
        minimal = {"timezone": "Etc/UTC"}
        self.assertEqual(oh_host.problems(minimal), [])
        resolved = oh_host.validate(minimal)
        self.assertEqual(resolved, {"timezone": "Etc/UTC", **oh_host.DEFAULTS})
        self.assertEqual(minimal, {"timezone": "Etc/UTC"})

        versioned = {"version": 1, "timezone": "Etc/UTC"}
        self.assertEqual(oh_host.validate(versioned)["version"], 1)
        self.assertEqual(oh_host.load(b"version: 1\ntimezone: Etc/UTC\n"), {**oh_host.DEFAULTS, **versioned})

        for bad_version in (0, 2, "1", True, 1.0, None):
            with self.subTest(version=bad_version):
                found = oh_host.problems({"version": bad_version, "timezone": "Etc/UTC"})
                self.assertEqual(found, ["version must be 1"])
                with self.assertRaises(oh_host.HostError) as caught:
                    oh_host.validate({"version": bad_version, "timezone": "Etc/UTC"})
                self.assertEqual(caught.exception.problems, found)

        # Collisions against defaulted ports and node_exporter_uid are still caught.
        self.assertTrue(any("collides" in p for p in oh_host.problems(
            {"timezone": "Etc/UTC", "metrics": {"grafana_port": oh_host.DEFAULTS["http_port"]}})))
        self.assertTrue(any("node_exporter_uid" in p for p in oh_host.problems(
            {"timezone": "Etc/UTC", "gid": oh_host.DEFAULTS["uid"] - 1})))

    def test_multi_error_collection_reports_every_problem_in_one_pass(self):
        broken = {
            "version": 2,
            "timezone": "Mars/Olympus_Mons",
            "root": "relative/path",
            "uid": 0,
            "gid": 9001,
            "http_port": 80,
            "unknown_key": True,
            "gateway": {"port": 99999, "listen": []},
            "metrics": {"retention": "forever", "units": ["bad\nunit"], "extra": 1},
            "guest_http": {"listen": [], "clients": []},
            "music": {"libraries": {"BadName": "relative"}},
            "shares": {"music": {"type": "smbfs", "server": "nas.example", "where": "/mnt/music"}},
        }
        found = oh_host.problems(broken)
        self.assertGreaterEqual(len(found), 10, found)
        for expected in (
            "unknown setting unknown_key",
            "version must be 1",
            "not a known IANA time zone",
            "root must be",
            "uid and gid",
            "unknown metrics setting extra",
            "metrics.units",
            "metrics.retention",
            "guest_http.listen",
            "music.libraries",
            "shares.music.type",
            "gateway.port: listener port",
            "gateway.listen must be a non-empty list",
            "http: listener port",
        ):
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in item for item in found), (expected, found))

        with self.assertRaises(oh_host.HostError) as caught:
            oh_host.validate(broken)
        self.assertEqual(caught.exception.problems, found)
        for item in found:
            self.assertIn(item, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
