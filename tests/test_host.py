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


if __name__ == "__main__":
    unittest.main()
