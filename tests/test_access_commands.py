"""Every access command thermite spells is one its own access verb accepts.

Three places spell `thermite access ACTION`: the launcher, which takes the
site selector out of a command line before any parser runs; the access
verb's parser; and the code that writes such commands down, in the staged
units and in the installer. These tests run the real ones against each
other, for every action the parser has.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli
import oh_paths
from oh_access import staging
from oh_install.access import Gateway
from tests import test_access_deployment as deployment


def service_actions(parser):
    """The access actions that run on a release's service descriptor, read
    from the parser so that a new one is tested without being listed here."""
    [actions] = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    return sorted(name for name, action in actions.choices.items() if "--descriptor" in action._option_string_actions)


class Accepted:
    """For a test case: hand a command line to the real launcher and parser."""

    def accepted(self, argv):
        """What the access verb makes of `argv` ("access", then the rest),
        handed over as bin/thermite hands it over."""
        argv = [str(word) for word in argv]
        with patch.dict(os.environ), patch.object(oh_paths, "SITE", oh_paths.SITE):
            os.environ.pop("THERMITE_SITE", None)
            passed = oh_paths.select_args(argv)
            site = os.environ.get("THERMITE_SITE")
        self.assertIsNone(site, f"the launcher reads the site out of thermite {shlex.join(argv)}, so the access "
                                "verb never sees that switch")
        self.assertEqual(passed[:1], ["access"])
        refusal = io.StringIO()
        try:
            with contextlib.redirect_stderr(refusal):
                return oh_cli.parser_of("access").parse_args(passed[1:])
        except SystemExit:
            self.fail(f"thermite does not accept its own {shlex.join(argv)}:\n{refusal.getvalue()}")


class Launcher(unittest.TestCase):
    def test_bin_thermite_runs_every_service_action_as_far_as_the_action_itself(self):
        actions = service_actions(oh_cli.parser_of("access"))
        # The ones a unit starts, and the three the launcher once could not run.
        self.assertLessEqual({"serve", "worker", "callbacks", "audit", "prune", "music-tidal"}, set(actions))
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            site.mkdir()
            descriptor = Path(temporary) / "release/config.json"  # Absent: no action gets further than refusing.
            environment = {name: value for name, value in os.environ.items()
                           if name not in ("THERMITE_SITE", oh_paths.REEXEC)}

            def run(argv):
                return subprocess.run([sys.executable, str(AREA / "bin/thermite"), *argv], cwd=temporary,
                                      env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=120)

            cases = []
            for action in actions:
                more = ["--user", "admin"] if action in ("bootstrap", "reset-admin") else []
                cases += [["access", action, "--descriptor", str(descriptor), *more],
                          ["-qv", "access", action, "--descriptor", str(descriptor), *more],
                          ["access", action, f"--descriptor={descriptor}", *more, "--config", str(site)]]
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(run, cases))
        for argv, result in zip(cases, results, strict=True):
            with self.subTest(command=shlex.join(argv)):
                # A command line the launcher or the parser turned down ends in
                # argparse's usage or in "thermite: ..."; only the action's own
                # code, past both, says "thermite access: ...".
                said = result.stderr.strip().splitlines()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("usage:", result.stderr)
                self.assertTrue(said and said[-1].startswith("thermite access: "), result.stderr)


class StagedUnits(Accepted, unittest.TestCase):
    def test_each_unit_starts_its_own_release_with_a_command_that_release_accepts(self):
        release = Path("/srv/fictional/access/releases/20261118T000000Z-0a1b2c")
        units = staging.units(release, state="/srv/fictional/access/state", callbacks=True,
                              image=deployment.IMAGE, identity=deployment.IDENTITY)
        self.assertEqual(set(units), set(staging.ACCESS_UNITS))
        started = {}
        for name, text in units.items():
            with self.subTest(unit=name):
                [line] = [line for line in text.splitlines() if line.startswith(("ExecStart=", "Exec="))]
                words = shlex.split(line.removeprefix("Exec="))
                # The interpreter and the program are the release's own, never
                # a path that moves with an upgrade: the unit and the parser
                # that reads its command line are always of one release. The
                # interpreter is the one the image built into the release.
                self.assertEqual(words[:2], [str(release / "env/bin/python"), str(release / "program/bin/thermite")])
                parsed = self.accepted(words[2:])
                started[parsed.command] = parsed.descriptor
        self.assertEqual(started, {"serve": release / "config.json", "worker": release / "config.json",
                                   "callbacks": release / "callbacks.json"})


class Installer(Accepted, deployment.GatewayFixture):
    def test_a_gateway_deployment_runs_only_access_commands_its_release_accepts(self):
        gateway, system = self.gateway(), deployment.Host()
        system.installed, system.active = Path("/srv/old-release"), True
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0, system.messages)
        program = str(gateway.release / "program/bin/thermite")
        ran = {}
        for command in system.commands:
            if program in command:
                parsed = self.accepted(command[command.index(program) + 1:])
                ran[parsed.command] = parsed.descriptor
        self.assertEqual(ran, {"verify": gateway.release / "config.json", "migrate": gateway.release / "config.json"})


class CliBehavior(unittest.TestCase):
    def test_login_prints_every_tui_problem_line(self):
        from oh_cli import access as access_cli, tui
        err = io.StringIO()
        with patch.object(tui, "resolve", return_value="https://thermite.example:13601"), \
                patch.object(tui, "trust", side_effect=tui.Problem("first diagnostic", "second hint")), \
                contextlib.redirect_stderr(err):
            code = access_cli.main(["login", "--url", "https://thermite.example:13601", "--user", "operator"])
        self.assertEqual(code, 2)
        self.assertIn("thermite access: first diagnostic", err.getvalue())
        self.assertIn("thermite access: second hint", err.getvalue())

    def test_terminal_subcommands_honor_json_flag_and_emit_human_output_without_it(self):
        import json
        from oh_access import client
        from oh_cli import access as access_cli, tui
        sample = {"origin": "https://thermite.example:13601",
                  "user": {"username": "operator", "admin": False, "can_edit": True}, "policies": {}}
        with patch.object(tui, "resolve", return_value="https://thermite.example:13601"), \
                patch.object(client, "status", return_value=sample):
            out_json = io.StringIO()
            with contextlib.redirect_stdout(out_json):
                self.assertEqual(access_cli.main(["status", "--url", "https://thermite.example:13601", "--json"]), 0)
            lines = out_json.getvalue().splitlines()
            self.assertEqual(len(lines), 1, "--json emits one canonical line via oh_cli.emit()")
            self.assertEqual(json.loads(lines[0]), sample)
            out_human = io.StringIO()
            with contextlib.redirect_stdout(out_human):
                self.assertEqual(access_cli.main(["status", "--url", "https://thermite.example:13601"]), 0)
            self.assertIn("operator (editor)", out_human.getvalue())

    def test_logout_removes_locally_expired_credential(self):
        from oh_access import client
        from oh_access.files import json_private
        with tempfile.TemporaryDirectory() as home:
            url = "https://thermite.example:13601"
            cred = client.credential_path(url, home)
            ca = client.ca_path(cred)
            fake_token = "thm_" + "a" * 40  # lint: allow (fictional)
            json_private(cred, {"version": 1, "origin": url, "token": fake_token,
                                "id": "tok-1", "expires_at": "2020-01-01T00:00:00Z"})
            json_private(ca, {"version": 1, "ca_file": "/nonexistent/ca.pem"})
            called = []
            result = client.logout(url, config_home=home, http=lambda *a, **kw: called.append(a))
            self.assertEqual(result, {"origin": url, "logged_out": True})
            self.assertEqual(called, [])
            self.assertFalse(cred.exists())
            self.assertFalse(ca.exists())

    def test_init_json_emits_single_canonical_line(self):
        import json
        from oh_cli import init as init_cli
        out = io.StringIO()
        with patch.object(init_cli, "initialize", return_value={"application": "0.1.0", "site": "/tmp/x"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(init_cli.main(["--json", "/tmp/x"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {"application": "0.1.0", "site": "/tmp/x"})


if __name__ == "__main__":
    unittest.main()
