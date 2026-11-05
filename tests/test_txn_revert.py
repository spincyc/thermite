"""thermite revert: the version the last install or upgrade set aside, with
its state, as its own install transaction (Q4).

A fictional site and application store in temporary directories; the
transaction itself is tests/test_txn_changeover.py's and the engine's
reinstate is tests/test_txn_engine.py's.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_application_update  # noqa: E402
import oh_source_upgrade  # noqa: E402
from oh_cli import revert as revert_cli  # noqa: E402
from oh_install import changeover, manifest, runner, steps  # noqa: E402
from oh_install.system import System  # noqa: E402
from tests import txn_fakes  # noqa: E402

BEFORE, AFTER = "1" * 40, "2" * 40
OLD_APP, NEW_APP = "a" * 64, "b" * 64


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.site = self.temporary / "site"
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}},
            {"uid": 9001, "gid": 9001, "root": "/srv/thermite-fixture"}, AFTER, "c" * 64, "fixture-host",
            "192.0.2.10", application={"version": "1.0", "digest": NEW_APP})
        self.record = {"version": 1, "id": txn_fakes.ID, "kind": "upgrade", "verb": "upgrade", "phase": "committed",
                       "site": str(self.site), "root": "/srv/thermite-fixture", "installed": True, "bridged": False,
                       "head_before": BEFORE, "head_after": AFTER,
                       "application": {"current": NEW_APP, "previous": OLD_APP}, "asides": {}}
        directory = self.site / "var/transaction" / txn_fakes.ID
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(json.dumps(self.record))
        self.installed = {"installed": True, "kept": txn_fakes.ID, "transaction": txn_fakes.ID}
        self.environment = self.temporary / "home/.local/share/thermite/application/environment-old"
        self.system = Mock(spec=System)
        scope = ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(patch.object(revert_cli, "AREA", self.site))
        scope.enter_context(patch.object(steps, "committed_context", return_value=self.context))
        self.manifest = scope.enter_context(patch.object(manifest, "installed", return_value=self.installed))
        self.found = scope.enter_context(patch.object(revert_cli, "environment", return_value=self.environment))

    def save(self, **changes):
        self.record.update(changes)
        (self.site / "var/transaction" / txn_fakes.ID / "record.json").write_text(json.dumps(self.record))

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)


class Refusals(Fixture):
    def problem(self):
        return revert_cli.refusal(self.context, self.system)[0]

    def test_the_kept_upgrade_of_a_transaction_installed_version_can_be_reverted(self):
        self.assertEqual(revert_cli.refusal(self.context, self.system), (None, self.record))

    def test_nothing_to_revert_to(self):
        for changes, words in (({"kind": "uninstall"}, "is an uninstall (committed)"),
                               ({"kind": "clean"}, "is a clean"),
                               ({"kind": "revert"}, "is a revert"),
                               ({"phase": "rolled-back"}, "(rolled-back)"),
                               ({"bridged": True}, "was not installed by a transaction"),
                               ({"installed": False}, "was not installed by a transaction")):
            with self.subTest(changes=changes):
                original = dict(self.record)
                self.save(**changes)
                self.assertIn(words, self.problem())
                self.save(**original)
        self.manifest.return_value = None
        self.assertIn("not installed here", self.problem())
        self.manifest.return_value = {**self.installed, "kept": txn_fakes.OLD}
        self.assertIn("record of the transaction to revert, is missing", self.problem())

    def test_the_site_must_be_at_the_upgrade_commit_and_the_old_environment_kept(self):
        self.context = steps.Context(**{**self.context.__dict__, "head": "3" * 40})
        self.assertIn(f"the site is at {'3' * 12}, not {AFTER[:12]}", self.problem())
        self.context = steps.Context(**{**self.context.__dict__, "head": AFTER})
        self.found.return_value = None
        self.assertIn(f"the previous application, {OLD_APP[:12]}, has no prepared environment", self.problem())

    def test_a_refusal_changes_nothing(self):
        self.found.return_value = None
        with patch.object(changeover, "resolve", return_value=None), patch.object(changeover, "run") as run:
            self.assertEqual(revert_cli.revert(self.system, False), 2)
        run.assert_not_called()
        self.assertIn("Nothing was changed", self.said())


class Revert(Fixture):
    def test_one_revert_transaction_after_a_plain_warning_and_typing_the_root(self):
        with patch.object(changeover, "resolve", return_value=None), \
                patch.object(runner, "confirm_run", return_value=True), \
                patch.object(changeover, "run", return_value=0) as run:
            self.assertEqual(revert_cli.revert(self.system, False), 0)
        self.assertIn("is lost", self.said())
        (context, _, build), options = run.call_args
        self.assertEqual(options, {"installed": self.installed, "bridged": False, "before": AFTER, "after": BEFORE,
                                   "first": 1, "pinned": False})
        work = build("20261106T090000Z-00e5f6")
        self.assertEqual((work.kind, work.verb, work.release, work.pull, work.reinstate), ("revert", "revert", None,
                                                                                            False, True))
        self.assertEqual(work.items(None), [])
        self.assertEqual(work.confirm.path, Path("/srv/thermite-fixture"))
        self.assertIsInstance(work.switching[0], revert_cli.PutBack)
        health = work.verification[-1].actions(None)[0]
        self.assertEqual(health.argv[0], str(self.environment / "bin/thermite"), "the previous version's own health")
        self.assertEqual(health.argv[-2:], ("--revision", BEFORE))

    def test_an_open_transaction_is_resolved_first(self):
        with patch.object(changeover, "resolve", return_value=changeover.COMMITTED), \
                patch.object(changeover, "run") as run:
            self.assertEqual(revert_cli.revert(self.system, False), changeover.REFUSED)
        run.assert_not_called()

    def test_after_the_commit_the_pin_and_the_owner_command_go_back(self):
        command = self.environment / "bin/thermite"
        with patch.object(oh_source_upgrade, "reset_pin", return_value=True) as reset, \
                patch.object(oh_application_update, "select", return_value=Path("/home/owner/.local/bin/thermite")) \
                as select:
            revert_cli.reverted(self.system, None, command, BEFORE, AFTER)
        reset.assert_called_once_with(self.site, expected=AFTER, previous=BEFORE)
        self.assertEqual(select.call_args.args[1], command)
        self.assertIn(f"Site: back at {BEFORE[:12]}", self.said())
        with patch.object(oh_source_upgrade, "reset_pin", side_effect=oh_source_upgrade.PinMoved("moved")), \
                patch.object(oh_application_update, "select", side_effect=ValueError("custom launcher")):
            revert_cli.reverted(self.system, None, command, BEFORE, AFTER)
        self.assertIn(f"revert --no-edit {AFTER[:12]}", self.said())
        self.assertIn("still selects the reverted application (custom launcher)", self.said())


class Environments(unittest.TestCase):
    def test_the_prepared_environment_of_a_release_is_found_by_its_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = home / ".local/share/thermite/application"
            for name, digest in (("environment-old", OLD_APP), ("environment-new", NEW_APP)):
                (store / name / "bin").mkdir(parents=True)
                (store / name / "bin/thermite").write_text("#!/bin/sh\n")
                (store / f"release-{digest}.json").write_text(json.dumps(
                    {"environment": name, "release": {"digest": digest}}))
            self.assertEqual(revert_cli.environment(OLD_APP, home), store / "environment-old")
            self.assertIsNone(revert_cli.environment("f" * 64, home))
            (store / "environment-old/bin/thermite").unlink()
            self.assertIsNone(revert_cli.environment(OLD_APP, home), "an environment without its command is gone")

    def test_selecting_a_command_replaces_the_launcher_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            command = home / ".local/share/thermite/application/environment-old/bin/thermite"
            command.parent.mkdir(parents=True)
            command.write_text("#!/bin/sh\n")
            launcher = oh_application_update.select(home, command)
            self.assertEqual(os.readlink(launcher), str(command))
            self.assertEqual(oh_application_update.select(home, command), launcher)
            self.assertEqual([path.name for path in launcher.parent.iterdir()], ["thermite"])


if __name__ == "__main__":
    unittest.main()
