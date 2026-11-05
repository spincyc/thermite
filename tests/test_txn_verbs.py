"""The owner commands around an install transaction: what each one's dry
run shows, thermite rollback's choice of commit and its refusals, the
history record of a rolled-back transaction, and the gateway release a
bridged version runs.

A fictional site in a temporary directory and a Mock host; the
transaction itself is tests/test_txn_changeover.py's, so changeover.run
and changeover.resolve are patched here.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
from oh_access import AccessError  # noqa: E402
from oh_cli import history as history_cli  # noqa: E402
from oh_cli import install as install_cli  # noqa: E402
from oh_cli import revert as revert_cli  # noqa: E402
from oh_cli import rollback as rollback_cli  # noqa: E402
from oh_cli import uninstall as uninstall_cli  # noqa: E402
from oh_cli import upgrade as upgrade_cli  # noqa: E402
from oh_install import access, bridge, changeover, manifest, runner, steps  # noqa: E402
from oh_install.model import Check, Step, Task  # noqa: E402
from oh_install.system import System  # noqa: E402
from tests import txn_fakes  # noqa: E402

BEFORE, AFTER, EARLIER = "1" * 40, "2" * 40, "3" * 40
OLD_APP, NEW_APP = "a" * 64, "b" * 64
ROOT = "/srv/thermite-fixture"


class Fake(Step):
    """A preparation step: records what a run would have it do."""

    always = True
    stop_on_failure = True

    def __init__(self, log, problem=None):
        self.title = "Prepare the fixture"
        self.log, self.problem = log, problem

    def check(self, system):
        self.log.append("check")
        return Check(False, "fixture preparation", self.problem)

    def actions(self, system):
        return [Task("prepare the fixture", self.perform)]

    def perform(self, system):
        self.log.append("performed")
        return 0


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.site = self.temporary / "site"
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}}, {"uid": 9001, "gid": 9001, "root": ROOT}, AFTER,
            "c" * 64, "fixture-host", "192.0.2.10", application={"version": "1.0", "digest": NEW_APP})
        self.system = Mock(spec=System)
        self.system.login.return_value = "fixture-owner"
        self.system.process_groups.return_value = {9001}
        self.log = []
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        # What commit would record: the fixture's versions pin no images.
        self.patch(changeover, "release", return_value={"release": {"site": AFTER}})

    def patch(self, target, name, **kwargs):
        return self.scope.enter_context(patch.object(target, name, **kwargs))

    def repository(self):
        """The site as a Git repository with nothing uncommitted, as a
        revert needs it."""
        subprocess.run(["git", "init", "-q", str(self.site)], check=True)

    def said(self):
        return "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)


class Preview(Fixture):
    """changeover.preview: the preparation as a dry run, then one line
    saying what the transaction would do."""

    def preview(self, work):
        work.preparation = [Fake(self.log)]
        code = changeover.preview(self.context, self.system, work, first=4)
        self.system.run.assert_not_called()
        self.assertEqual(self.log, ["check"], "the preparation is only checked")
        self.assertIn("4. Prepare the fixture: fixture preparation", self.said())
        self.assertIn("   would: prepare the fixture", self.said())
        return code, self.system.say.call_args_list[-1].args[0]

    def test_an_install_pulls_and_switches_in_the_new_version(self):
        work = changeover.install_work(self.context, None)
        code, line = self.preview(work)
        self.assertEqual(code, 0)
        titles = ", ".join(step.title for step in work.switching)
        self.assertTrue(line.startswith("Then, as one install transaction: pull the pinned images, check the room"))
        self.assertIn(f"stop Thermite and set it aside; install and start the new version ({titles}); verify its "
                      "health; and commit.", line)
        self.assertNotIn("ask you to type", line)
        self.assertIn("rolls back in the same run", line)

    def test_an_uninstall_installs_nothing_new_and_pulls_nothing(self):
        code, line = self.preview(changeover.uninstall_work(self.context, None))
        self.assertEqual(code, 0)
        self.assertIn("set it aside; nothing new is installed; and commit", line)
        self.assertNotIn("pull", line)
        self.assertNotIn("ask you to type", line)

    def test_a_purge_and_a_clean_install_name_the_path_to_type(self):
        code, line = self.preview(changeover.uninstall_work(self.context, None, purge=True))
        self.assertIn(f"aside, ask you to type {ROOT}, begin", line)
        self.assertIn("nothing new is installed", line)
        self.log.clear()
        work = changeover.install_work(self.context, None, kind="clean")
        code, line = self.preview(work)
        self.assertIn(f"ask you to type {self.context.layout.root}, begin", line)
        self.assertIn("pull the pinned images", line)
        self.assertIn("install and start the new version (Clear the state the clean install replaces, ", line)

    def test_the_preparations_status_is_the_preview_s(self):
        work = changeover.uninstall_work(self.context, None)
        work.preparation = [Fake(self.log, problem="the fixture is broken")]
        self.assertEqual(changeover.preview(self.context, self.system, work, first=1), 1)
        self.assertIn("1. Prepare the fixture: the fixture is broken", self.said())
        self.assertIn("Then, as one install transaction", self.said(), "the transaction is still shown")
        self.system.run.assert_not_called()

    def test_a_revert_puts_the_kept_version_back(self):
        """Through thermite revert -n, whose Work is its own."""
        self.repository()
        directory = self.site / "var/transaction" / txn_fakes.ID
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(json.dumps({
            "version": 1, "id": txn_fakes.ID, "kind": "upgrade", "verb": "upgrade", "phase": "committed",
            "site": str(self.site), "root": ROOT, "installed": True, "bridged": False, "head_before": BEFORE,
            "head_after": AFTER, "application": {"current": NEW_APP, "previous": OLD_APP}, "asides": {}}))
        self.patch(revert_cli, "AREA", new=self.site)
        self.patch(steps, "committed_context", return_value=self.context)
        self.patch(manifest, "installed", return_value={"installed": True, "kept": txn_fakes.ID})
        self.patch(revert_cli, "environment", return_value=self.temporary / "environment-old")
        self.patch(steps, "Account", side_effect=lambda context: Fake(self.log))
        resolve = self.patch(changeover, "resolve")
        run = self.patch(changeover, "run")
        self.assertEqual(revert_cli.revert(self.system, True), 0)
        resolve.assert_not_called()
        run.assert_not_called()
        self.system.run.assert_not_called()
        self.assertEqual(self.log, ["check"])
        line = self.system.say.call_args_list[-1].args[0]
        self.assertIn(f"ask you to type {ROOT}, begin", line)
        self.assertIn("set it aside; put the kept version back, move the site back to it and start it; "
                      "verify its health with its own application; and commit", line)
        self.assertNotIn("pull", line)
        self.assertNotIn("install and start the new version", line)


class DryRuns(Fixture):
    """Each owner command's --dry-run reaches preview with its own Work, and
    neither resolves an earlier transaction nor runs one."""

    def setUp(self):
        super().setUp()
        self.installed = {"installed": True, "transaction": txn_fakes.OLD, "kept": txn_fakes.OLD,
                          "release": {"site": BEFORE}}
        for module in (install_cli, upgrade_cli, uninstall_cli, rollback_cli, revert_cli):
            self.patch(module, "AREA", new=self.site)
        self.patch(steps, "committed_context", return_value=self.context)
        self.patch(manifest, "installed", return_value=self.installed)
        self.patch(manifest, "thermite_present", return_value=[])
        self.patch(bridge, "needed", return_value=False)
        self.runs = self.patch(runner, "run", return_value=0)
        self.confirm = self.patch(runner, "confirm_run", return_value=True)
        self.resolve = self.patch(changeover, "resolve", return_value=changeover.COMMITTED)
        self.transaction = self.patch(changeover, "run", return_value=0)
        self.preview = self.patch(changeover, "preview", return_value=0)

    def previewed(self, code=0):
        self.assertEqual(code, 0)
        self.resolve.assert_not_called()
        self.transaction.assert_not_called()
        self.confirm.assert_not_called()
        self.preview.assert_called_once()
        (context, system, work), options = self.preview.call_args
        self.assertIs(system, self.system)
        self.preview.reset_mock()
        return context, work, options["first"]

    def test_install_and_a_clean_install(self):
        context, work, first = self.previewed(install_cli.install(self.system, True))
        self.assertEqual((work.kind, work.verb, work.confirm), ("install", "install", None))
        self.assertIsNotNone(work.release)
        self.assertEqual(first, len(steps.install_preflight(self.context)) + 1)
        self.assertIs(context, self.context)
        context, work, _ = self.previewed(install_cli.install(self.system, True, clean=True))
        self.assertEqual((work.kind, work.verb), ("clean", "install"))
        self.assertEqual(work.confirm.path, self.context.layout.root)

    def test_upgrade(self):
        _, work, first = self.previewed(upgrade_cli.upgrade(self.system, True))
        self.assertEqual((work.kind, work.verb, first), ("upgrade", "upgrade", 3))
        self.assertIsNotNone(work.release)

    def test_uninstall_and_purge(self):
        for purge, kind in ((False, "uninstall"), (True, "purge")):
            with self.subTest(purge=purge):
                _, work, first = self.previewed(uninstall_cli.uninstall(self.system, True, purge))
                self.assertEqual((work.kind, work.verb, work.release, first), (kind, "uninstall", None, 1))
                self.assertEqual(work.confirm is not None, purge)

    def test_rollback(self):
        earlier = steps.Context(**{**self.context.__dict__, "head": EARLIER})
        self.patch(steps, "committed_context",
                   side_effect=lambda area, system, revision=None: earlier if revision else self.context)
        self.patch(deploy, "deploys", return_value=[
            {"commit": EARLIER, "number": 1, "deployed_at": "2026-11-01T10:00:00+00:00", "current": False},
            {"commit": AFTER, "number": 2, "deployed_at": "2026-11-04T10:00:00+00:00", "current": True}])
        self.patch(deploy, "read_record", return_value={"commit": AFTER})
        context, work, first = self.previewed(rollback_cli.roll_back(self.system, None, True))
        self.assertIs(context, earlier)
        self.assertEqual((work.kind, work.verb, first), ("install", "rollback", 3))
        self.assertIn(f"What rolling back thermite on this host, fixture-host, to {EARLIER[:12]}, deploy #1",
                      self.said())

    def test_revert(self):
        self.repository()
        directory = self.site / "var/transaction" / txn_fakes.OLD
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(json.dumps({
            "version": 1, "id": txn_fakes.OLD, "kind": "upgrade", "verb": "upgrade", "phase": "committed",
            "site": str(self.site), "root": ROOT, "installed": True, "bridged": False, "head_before": BEFORE,
            "head_after": AFTER, "application": {"current": NEW_APP, "previous": OLD_APP}, "asides": {}}))
        self.patch(revert_cli, "environment", return_value=self.temporary / "environment-old")
        _, work, first = self.previewed(revert_cli.revert(self.system, True))
        self.assertEqual((work.kind, work.verb, work.release, work.reinstate, first), ("revert", "revert", None,
                                                                                       True, 1))

    def test_the_dry_run_status_is_the_preview_s_or_the_preflight_s(self):
        self.preview.return_value = 1
        self.assertEqual(install_cli.install(self.system, True), 1)
        self.preview.return_value = 0
        self.runs.return_value = 1
        self.assertEqual(install_cli.install(self.system, True), 1)
        self.assertEqual(upgrade_cli.upgrade(self.system, True), 1)
        self.transaction.assert_not_called()


class RollbackTarget(unittest.TestCase):
    """thermite rollback's commit: the deploy before the current one, or COMMIT."""

    def entries(self, *commits, current):
        return [{"commit": commit, "number": number, "deployed_at": f"2026-11-0{number}T10:00:00+00:00",
                 "current": number == current} for number, commit in enumerate(commits, 1)]

    def test_without_commit_the_deploy_before_the_current_one(self):
        entries = self.entries(EARLIER, BEFORE, AFTER, AFTER, BEFORE, current=4)
        self.assertEqual(rollback_cli.target(entries, {"commit": AFTER}, None), (BEFORE, entries[1]),
                         "entries of the current commit are skipped; later ones are not looked at")
        entries = self.entries(EARLIER, BEFORE, current=0)
        self.assertEqual(rollback_cli.target(entries, {"commit": AFTER}, None), (BEFORE, entries[1]),
                         "without a current entry, the whole history is before it")

    def test_without_commit_nothing_deployed_or_nothing_before_refuses(self):
        with self.assertRaisesRegex(deploy.DeployError, "nothing is deployed here yet"):
            rollback_cli.target(self.entries(EARLIER, current=1), {}, None)
        with self.assertRaisesRegex(deploy.DeployError, f"holds no deploy before {AFTER[:12]}'s: name the COMMIT"):
            rollback_cli.target(self.entries(AFTER, AFTER, current=2), {"commit": AFTER}, None)
        with self.assertRaisesRegex(deploy.DeployError, "name the COMMIT"):
            rollback_cli.target([], {"commit": AFTER}, None)

    def test_a_named_commit_is_resolved_with_its_latest_entry_if_any(self):
        entries = self.entries(EARLIER, BEFORE, EARLIER, AFTER, current=4)
        with patch.object(deploy, "resolve", return_value=EARLIER) as resolve:
            self.assertEqual(rollback_cli.target(entries, {"commit": AFTER}, "333333"), (EARLIER, entries[2]))
        resolve.assert_called_once_with(rollback_cli.AREA, "333333")
        with patch.object(deploy, "resolve", return_value="4" * 40):
            self.assertEqual(rollback_cli.target(entries, {}, "444444"), ("4" * 40, None),
                             "a commit never deployed here, even with nothing deployed")
        with patch.object(deploy, "resolve", side_effect=deploy.DeployError("unknown revision")), \
                self.assertRaisesRegex(deploy.DeployError, "unknown revision"):
            rollback_cli.target(entries, {"commit": AFTER}, "nonsense")


class Rollback(Fixture):
    """thermite rollback's refusals and the transaction it hands over."""

    def setUp(self):
        super().setUp()
        self.earlier = steps.Context(**{**self.context.__dict__, "head": EARLIER})
        self.installed = {"installed": True, "transaction": txn_fakes.OLD, "release": {"site": BEFORE}}
        self.patch(rollback_cli, "AREA", new=self.site)
        self.contexts = self.patch(steps, "committed_context", side_effect=lambda area, system, revision=None: (
            self.earlier if revision else self.context))
        self.patch(deploy, "deploys", return_value=[
            {"commit": EARLIER, "number": 1, "deployed_at": "2026-11-01T10:00:00+00:00", "current": False},
            {"commit": AFTER, "number": 2, "deployed_at": "2026-11-04T10:00:00+00:00", "current": True}])
        self.patch(deploy, "read_record", return_value={"commit": AFTER})
        self.resolve = self.patch(changeover, "resolve", return_value=None)
        self.manifest = self.patch(manifest, "installed", return_value=self.installed)
        self.present = self.patch(manifest, "thermite_present", return_value=[])
        self.bridge = self.patch(bridge, "needed", return_value=False)
        self.runs = self.patch(runner, "run", return_value=0)
        self.confirm = self.patch(runner, "confirm_run", return_value=True)
        self.transaction = self.patch(changeover, "run", return_value=changeover.COMMITTED)

    def roll_back(self, commit=None):
        return rollback_cli.roll_back(self.system, commit, False)

    def test_the_approved_rollback_is_one_unpinned_transaction_from_the_installed_commit(self):
        self.assertEqual(self.roll_back(), changeover.COMMITTED)
        self.resolve.assert_called_once_with(self.site, self.system, "rollback")
        self.assertEqual(self.confirm.call_args.args[1], "rollback")
        (context, system, build), options = self.transaction.call_args
        self.assertIs(context, self.earlier)
        self.assertEqual(options, {"installed": self.installed, "bridged": False, "before": BEFORE,
                                   "after": EARLIER, "first": 3, "pinned": False})
        work = build(txn_fakes.ID)
        self.assertEqual((work.kind, work.verb), ("install", "rollback"))
        self.assertIn(f"Rolling back thermite on this host, fixture-host, to {EARLIER[:12]}, deploy #1 of "
                      "2026-11-01T10:00:00+00:00, step by step:", self.said())

    def test_an_unknown_installed_commit_or_a_bridged_version_starts_from_head(self):
        self.installed["release"] = {}
        self.roll_back()
        self.assertEqual(self.transaction.call_args.kwargs["before"], AFTER)
        self.manifest.return_value = None
        self.bridge.return_value = True
        self.roll_back()
        self.assertEqual((self.transaction.call_args.kwargs["before"], self.transaction.call_args.kwargs["bridged"],
                          self.transaction.call_args.kwargs["installed"]), (AFTER, True, None))

    def test_a_commit_pinning_another_application_release_names_the_git_revert(self):
        self.earlier = steps.Context(**{**self.earlier.__dict__, "application": {"digest": OLD_APP}})
        with self.assertRaises(deploy.DeployError) as raised:
            self.roll_back()
        message = str(raised.exception)
        self.assertIn(f"{EARLIER[:12]} pins another application release than {AFTER[:12]} does", message)
        self.assertIn(f"`git -C {self.site} revert --no-edit {EARLIER[:12]}..HEAD`", message)
        self.assertIn("thermite revert", message)
        self.manifest.assert_not_called()
        self.transaction.assert_not_called()

    def test_nothing_installed_is_nothing_to_roll_back(self):
        for installed in (None, {**self.installed, "installed": False}):
            with self.subTest(installed=installed):
                self.manifest.return_value = installed
                self.assertEqual(self.roll_back(), 2)
                self.assertIn("Thermite is not installed here, so there is nothing to roll back", self.said())
        self.runs.assert_not_called()
        self.transaction.assert_not_called()

    def test_thermite_files_without_a_manifest_refuse_with_the_recovery(self):
        self.manifest.return_value = None
        self.present.return_value = [Path("/etc/containers/systemd/thermite-openhab.container")]
        self.assertEqual(self.roll_back(), 2)
        self.assertEqual(self.system.say.call_args.args[0], manifest.missing_recovery(self.earlier))
        self.transaction.assert_not_called()

    def test_a_damaged_manifest_refuses_with_the_recovery(self):
        self.manifest.side_effect = manifest.ManifestError("/etc/thermite/installed.json is damaged")
        self.assertEqual(self.roll_back(), 2)
        self.assertEqual(self.system.say.call_args.args[0], "/etc/thermite/installed.json is damaged. "
                                                             + manifest.missing_recovery(self.earlier))
        self.runs.assert_not_called()
        self.transaction.assert_not_called()

    def test_an_open_transaction_is_resolved_first_and_this_run_stops(self):
        for outcome, code in ((changeover.COMMITTED, changeover.REFUSED), (changeover.REFUSED, changeover.REFUSED),
                              (changeover.ROLLED_BACK, changeover.ROLLED_BACK),
                              (changeover.UNHEALTHY, changeover.UNHEALTHY), (changeover.STUCK, changeover.STUCK)):
            with self.subTest(outcome=outcome):
                self.resolve.return_value = outcome
                self.assertEqual(self.roll_back(), code)
        self.contexts.assert_not_called()
        self.transaction.assert_not_called()

    def test_a_failed_preflight_or_a_declined_approval_runs_nothing(self):
        self.runs.return_value = 1
        self.assertEqual(self.roll_back(), 1)
        self.confirm.assert_not_called()
        self.runs.return_value = 0
        self.confirm.return_value = False
        self.assertEqual(self.roll_back(), 1)
        self.transaction.assert_not_called()

    def main(self, side_effect=None, euid=1000):
        error = io.StringIO()
        with patch.object(rollback_cli.os, "geteuid", return_value=euid), \
                patch.object(rollback_cli, "System", return_value=self.system), \
                patch.object(rollback_cli, "roll_back", side_effect=side_effect, return_value=0) as roll_back, \
                redirect_stderr(error):
            code = rollback_cli.main(["3d22e36", "--dry-run"])
        return code, error.getvalue(), roll_back

    def test_main_refuses_root_and_maps_errors_and_interrupts(self):
        code, error, roll_back = self.main(euid=0)
        self.assertEqual(code, 2)
        self.assertIn("thermite rollback: run it as your user", error)
        roll_back.assert_not_called()
        code, error, roll_back = self.main()
        self.assertEqual(code, 0)
        roll_back.assert_called_once_with(self.system, "3d22e36", True)
        code, error, _ = self.main(deploy.DeployError("the history holds no deploy before it"))
        self.assertEqual((code, error), (2, "thermite rollback: the history holds no deploy before it\n"))
        code, error, _ = self.main(KeyboardInterrupt())
        self.assertEqual(code, changeover.INTERRUPTED)
        self.assertIn(f"Interrupted. Run thermite rollback --config {self.site} again.", self.said())


class RecordRollback(unittest.TestCase):
    """thermite history --_record-rollback: the history line of a rolled-back transaction."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.layout = deploy.State(Path(temporary.name))
        self.layout.history.parent.mkdir(parents=True)
        self.layout.history.write_text(json.dumps({"deployed_at": "2026-11-05T10:20:00+00:00", "commit": AFTER,
                                                   "by": "owner", "subject": "Pin", "how": "upgrade"}) + "\n")

    def record(self, event):
        error = io.StringIO()
        with redirect_stderr(error):
            code = history_cli.record_rollback(self.layout, event)
        return code, error.getvalue()

    def test_a_valid_event_is_recorded(self):
        event = {"transaction": txn_fakes.ID, "commit": AFTER, "restored": BEFORE}
        self.assertEqual(self.record(json.dumps(event)), (0, ""))
        [entry] = deploy.deploys(self.layout)
        self.assertEqual(entry["rolled_back"][0]["transaction"], txn_fakes.ID)

    def test_a_refused_event_says_why_and_records_nothing(self):
        before = self.layout.history.read_text()
        for event, words in (("not json", "could not record the rollback"), ("[]", "not a JSON object"),
                             ('"text"', "not a JSON object"),
                             (json.dumps({"transaction": "x", "commit": AFTER, "restored": None}), "")):
            with self.subTest(event=event):
                code, error = self.record(event)
                self.assertEqual(code, 2)
                self.assertTrue(error.startswith(f"thermite history: could not record the rollback in "
                                                 f"{self.layout.history}: "))
                self.assertIn(words, error)
        self.assertEqual(self.layout.history.read_text(), before)


class Gateway(unittest.TestCase):
    """The gateway release a bridged version runs, recorded at begin."""

    def gateway(self, **kwargs):
        with patch.object(access, "installed_release", **kwargs):
            return changeover._gateway(Mock(spec=System))

    def test_the_release_name_only_from_a_releases_directory(self):
        self.assertEqual(self.gateway(return_value=Path(ROOT) / "access/releases/release-0123"), "release-0123")
        self.assertIsNone(self.gateway(return_value=Path(ROOT) / "access/current/release-0123"))
        self.assertIsNone(self.gateway(return_value=None))

    def test_an_unreadable_gateway_is_unknown(self):
        for error in (OSError("unreadable unit"), ValueError("malformed unit"), AccessError("unexpected overrides")):
            with self.subTest(error=error):
                self.assertIsNone(self.gateway(side_effect=error))


if __name__ == "__main__":
    unittest.main()
