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
import oh_units
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
        for argv, result in zip(cases, results):
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
                contained = line.startswith("Exec=")
                self.assertEqual(contained, name in oh_units.ACCESS_CONTAINED)
                words = shlex.split(line.removeprefix("Exec=") if contained else line.partition(" -- ")[2])
                # The interpreter and the program are the release's own, never
                # a path that moves with an upgrade: the unit and the parser
                # that reads its command line are always of one release. A
                # container's interpreter is the one its image built.
                self.assertEqual(words[:2], [str(release / ("container-env" if contained else "env") / "bin/python"),
                                             str(release / "program/bin/thermite")])
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


if __name__ == "__main__":
    unittest.main()
