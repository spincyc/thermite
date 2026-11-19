"""What thermite does outside itself (lib/oh_trace.py): libraries say it to
a leaf, and the CLI shows it under -v."""
from __future__ import annotations

import ast
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli  # noqa: E402
import oh_deploy  # noqa: E402
import oh_trace  # noqa: E402


def imported(path: Path) -> set[str]:
    """The top-level name of every module a source imports, anywhere in it."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found


class Lines(unittest.TestCase):
    def said(self, verbose: bool) -> str:
        err = io.StringIO()
        with mock.patch.object(oh_cli.switches, "verbose", verbose), contextlib.redirect_stderr(err):
            oh_trace.run(["git", "-C", "a site", "status"], "/srv")
            oh_trace.http("GET", "http://127.0.0.1:13602/rest/")
            oh_trace.trace("trust the system certificates")
        return err.getvalue()

    def test_the_cli_shows_them_on_stderr_only_under_v(self):
        self.assertEqual(self.said(False), "")
        self.assertEqual(self.said(True), "thermite: run git -C 'a site' status   (in /srv)\n"
                                          "thermite: GET http://127.0.0.1:13602/rest/\n"
                                          "thermite: trust the system certificates\n")

    def test_the_clis_own_commands_are_lines_of_the_same_shape(self):
        err = io.StringIO()
        with mock.patch.object(oh_cli.switches, "verbose", True), contextlib.redirect_stderr(err), \
                mock.patch.object(oh_cli.subprocess, "run", side_effect=OSError("no git here")):
            self.assertIsNone(oh_cli.commit(Path("a site")))
        self.assertEqual(err.getvalue(), "thermite: run git -C 'a site' rev-parse --verify --quiet HEAD\n")

    def test_a_library_without_the_cli_prints_nothing_and_loads_no_cli(self):
        code = ("import sys, oh_trace\n"
                "oh_trace.run(['true'])\n"
                "oh_trace.http('GET', 'http://192.0.2.1/')\n"
                "print(sorted(name for name in sys.modules if name.startswith('oh_')))\n")
        done = subprocess.run([sys.executable, "-B", "-c", code], cwd=AREA / "lib", capture_output=True, text=True,
                              timeout=60, env={"PATH": os.environ["PATH"]})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "['oh_trace']\n", ""))


class Leaf(unittest.TestCase):
    def test_the_cli_names_the_leafs_logger(self):
        self.assertEqual(oh_cli.TRACE, oh_trace.NAME)

    def test_the_leaf_imports_nothing_of_the_application(self):
        self.assertFalse({name for name in imported(AREA / "lib/oh_trace.py") if name.startswith("oh_")})

    def test_no_library_imports_the_cli_only_to_trace(self):
        # These two used nothing else of it.
        for name in ("lib/oh_sandbox.py", "lib/oh_deploy.py"):
            self.assertNotIn("oh_cli", imported(AREA / name), name)

    def test_the_apple_tv_helpers_files_do_not_need_the_leaf(self):
        # Its container gets exactly these files, and a deploy of an older
        # release looks every one of them up in that release
        # (oh_release.blobs): the list cannot grow by a module.
        for name in oh_deploy.ATV_APP:
            if name.endswith(".py") or name == "bin/thermite":
                self.assertNotIn("oh_trace", imported(AREA / name), name)


if __name__ == "__main__":
    unittest.main()
