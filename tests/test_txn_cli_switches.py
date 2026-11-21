"""The switches the owner commands around an install transaction share:
thermite rollback --restore-state (what the retired thermite revert does),
--yes on each verb that asks for approval, --json with a dry run, and
install's and upgrade's --token-file.

A fictional site in a temporary directory and a Mock host; the
transaction itself is tests/test_txn_changeover.py's, so it never runs
here.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
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

import oh_application_update  # noqa: E402
import oh_cli  # noqa: E402
import oh_deploy as deploy  # noqa: E402
import oh_paths  # noqa: E402
from oh_cli import install as install_cli  # noqa: E402
from oh_cli import revert as revert_cli  # noqa: E402
from oh_cli import rollback as rollback_cli  # noqa: E402
from oh_cli import uninstall as uninstall_cli  # noqa: E402
from oh_cli import upgrade as upgrade_cli  # noqa: E402
from oh_install import changeover, manifest, runner, steps  # noqa: E402
from oh_install.model import Check, Command, Step, Task  # noqa: E402
from oh_install.system import UNATTENDED, System  # noqa: E402
from tests import txn_fakes  # noqa: E402

BEFORE, AFTER, EARLIER = "1" * 40, "2" * 40, "3" * 40
OLD_APP, NEW_APP = "a" * 64, "b" * 64
ROOT = "/srv/thermite-fixture"


class Fake(Step):
    """A step whose check and actions are given."""

    def __init__(self, title, found, actions=(), always=False):
        self.title, self.found, self.listed, self.always = title, found, list(actions), always

    def check(self, system):
        return self.found

    def actions(self, system):
        return self.listed


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.site = self.temporary / "site"
        self.site.mkdir()
        self.context = steps.Context(
            self.site, {"openhab": {"version": "5.2.3"}}, {"uid": 9001, "gid": 9001, "root": ROOT}, AFTER,
            "c" * 64, "fixture-host", "192.0.2.10", application={"version": "1.0", "digest": NEW_APP})
        self.system = Mock(spec=System)
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        for module in (install_cli, uninstall_cli, rollback_cli, revert_cli):
            self.patch(module, "AREA", new=self.site)
        self.patch(steps, "committed_context", return_value=self.context)
        self.installed = {"installed": True, "transaction": txn_fakes.OLD, "kept": txn_fakes.OLD,
                          "release": {"site": BEFORE}}
        self.patch(manifest, "installed", return_value=self.installed)
        self.patch(manifest, "thermite_present", return_value=[])
        for module in (rollback_cli, uninstall_cli, revert_cli, install_cli, upgrade_cli):
            self.patch(module.os, "geteuid", return_value=1000)

    def patch(self, target, name, **kwargs):
        return self.scope.enter_context(patch.object(target, name, **kwargs))

    def kept(self):
        """The upgrade a restore goes back before, as its record says."""
        subprocess.run(["git", "init", "-q", str(self.site)], check=True)
        directory = self.site / "var/transaction" / txn_fakes.OLD
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(json.dumps({
            "version": 1, "id": txn_fakes.OLD, "kind": "upgrade", "verb": "upgrade", "phase": "committed",
            "site": str(self.site), "root": ROOT, "installed": True, "head_before": BEFORE,
            "head_after": AFTER, "application": {"current": NEW_APP, "previous": OLD_APP}, "asides": {}}))
        self.patch(revert_cli, "environment", return_value=self.temporary / "environment-old")
        self.patch(steps, "Account", side_effect=lambda context: Fake(
            "Account", Check(False, "fixture account"), [Task("make the fixture account", lambda system: 0)],
            always=True))

    def main(self, module, argv):
        """(exit status, standard output, standard error) of module.main(argv)."""
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = module.main(argv)
        return code, out.getvalue(), error.getvalue()


class RestoreState(Fixture):
    """thermite rollback --restore-state runs thermite revert's own
    functions, with rollback's name in what it says."""

    def test_its_dry_run_previews_the_revert_and_changes_nothing(self):
        self.kept()
        preview = self.patch(changeover, "preview", return_value=0)
        run = self.patch(changeover, "run")
        resolve = self.patch(changeover, "resolve")
        self.patch(rollback_cli, "System", return_value=self.system)
        code, out, error = self.main(rollback_cli, ["--restore-state", "-n"])
        self.assertEqual((code, error), (0, ""))
        resolve.assert_not_called()
        run.assert_not_called()
        (context, system, work), options = preview.call_args
        self.assertEqual((work.kind, work.verb, work.reinstate, work.confirm.path, options["first"]),
                         ("revert", "revert", True, Path(ROOT), 1))
        said = "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)
        self.assertIn(f"What reverting thermite on this host, fixture-host, to the version before transaction "
                      f"{txn_fakes.OLD} (site {BEFORE[:12]}), would do:", said)

    def test_a_run_is_thermite_revert_s_in_its_scope_with_rollback_s_name(self):
        seen = {}

        def scoped(site, system, verb, body, *, dry_run=False):
            seen.update(site=site, verb=verb, dry_run=dry_run)
            return body()
        self.patch(changeover, "scoped", side_effect=scoped)
        revert = self.patch(revert_cli, "revert", return_value=0)
        roll_back = self.patch(rollback_cli, "roll_back")
        self.patch(rollback_cli, "System", return_value=self.system)
        self.assertEqual(self.main(rollback_cli, ["--restore-state"]), (0, "", ""))
        revert.assert_called_once_with(self.system, False, plan=None)
        roll_back.assert_not_called()
        self.assertEqual(seen, {"site": self.site, "verb": "revert", "dry_run": False},
                         "the scope, as the transaction's record, keeps the verb earlier releases read")
        revert.side_effect = deploy.DeployError("nothing to revert to")
        code, _, error = self.main(rollback_cli, ["--restore-state"])
        self.assertEqual((code, error), (2, "thermite rollback: nothing to revert to\n"))
        revert.side_effect = KeyboardInterrupt()
        code, _, _ = self.main(rollback_cli, ["--restore-state"])
        self.assertEqual(code, changeover.INTERRUPTED)
        self.assertEqual(self.system.say.call_args.args[0],
                         f"\nInterrupted. Run thermite rollback --restore-state --config {self.site} again.")

    def test_a_commit_and_restore_state_are_one_or_the_other(self):
        run = self.patch(rollback_cli, "roll_back")
        with self.assertRaises(SystemExit) as raised:
            self.main(rollback_cli, ["3d22e36", "--restore-state"])
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_reruns_name_the_restore_however_it_began(self):
        self.assertEqual(changeover.command(self.site, "revert"),
                         f"thermite rollback --restore-state --config {self.site}")
        self.assertEqual(changeover.command(self.site, "rollback"), f"thermite rollback --config {self.site}")
        transaction = changeover.Transaction(self.site / "var", txn_fakes.ID,
                                             {"site": str(self.site), "verb": "revert", "kind": "revert"})
        self.assertEqual(transaction.rerun, f"thermite rollback --restore-state --config {self.site}")
        self.assertEqual(transaction.name, "Revert")


class RetiredRevert(Fixture):
    def test_thermite_revert_warns_then_runs_as_it_did(self):
        revert = self.patch(revert_cli, "revert", return_value=0)
        self.patch(revert_cli, "System", return_value=self.system)
        self.patch(oh_paths, "require_compatible")
        error = io.StringIO()
        with redirect_stderr(error):
            code = oh_cli.main(["revert", "-n"])
        self.assertEqual(code, 0)
        self.assertEqual(error.getvalue(), "thermite revert: thermite rollback --restore-state replaces it; "
                                           "thermite revert goes in the next release\n")
        revert.assert_called_once_with(self.system, True)
        self.assertNotIn("--yes", oh_cli.parser_of("revert").format_help(), "its switches are as they were")


class Yes(Fixture):
    """--yes gives the run's one approval up front (oh_install.system.UNATTENDED)
    and ends with it; a question --yes cannot answer refuses the run first."""

    def setUp(self):
        super().setUp()
        self.addCleanup(UNATTENDED.reset, UNATTENDED.set(False))
        self.system.interactive.return_value = False
        self.system.ask.side_effect = AssertionError("standard input was read")
        self.seen = {}

    def approve(self, operation):
        self.seen.update(unattended=UNATTENDED.get(), approved=runner.confirm_run(self.system, operation))
        return 0

    def test_uninstall_and_rollback(self):
        self.patch(changeover, "scoped", side_effect=lambda site, system, verb, body, dry_run=False: body())
        for module, name, argv in ((uninstall_cli, "uninstall", ["--yes"]), (rollback_cli, "roll_back", ["--yes"]),
                                   (rollback_cli, "roll_back", ["--yes", "3d22e36"])):
            with self.subTest(argv=argv, module=module.__name__):
                self.seen.clear()
                self.patch(module, "System", return_value=self.system)
                self.patch(module, name, side_effect=lambda *args, **kwargs: self.approve("fixture"))
                self.assertEqual(self.main(module, argv), (0, "", ""))
                self.assertEqual(self.seen, {"unattended": True, "approved": True})
                self.assertFalse(UNATTENDED.get(), "the approval ends with the run")

    def test_upgrade_approves_its_workflow_and_hands_yes_to_its_host_phase(self):
        def workflow(site, *, approve, host_args, **kwargs):
            self.seen.update(approved=approve(), host_args=host_args, unattended=UNATTENDED.get())
            return 0
        self.patch(changeover, "resolve", return_value=None)
        self.patch(oh_application_update, "upgrade", side_effect=workflow)
        with patch.object(System, "say"), patch.object(System, "ask", side_effect=AssertionError("asked")):
            self.assertEqual(self.main(upgrade_cli, ["--yes"])[0], 0)
        self.assertEqual((self.seen["approved"], self.seen["unattended"]), (True, True))
        self.assertIn("--yes", self.seen["host_args"], "the host phase runs unattended too")
        self.assertFalse(UNATTENDED.get())
        with patch.object(changeover, "pinned", side_effect=lambda *args: 0 if UNATTENDED.get() else 1) as phase:
            self.assertEqual(self.main(upgrade_cli, ["--_host-phase", "--_run-approved", "--yes"])[0], 0)
        phase.assert_called_once()

    def test_what_asks_to_type_a_path_is_refused_up_front(self):
        for module, name, argv, words in (
                (uninstall_cli, "uninstall", ["--yes", "--purge"],
                 "thermite uninstall: --yes cannot approve a purge, which asks you to type the installation root\n"),
                (uninstall_cli, "uninstall", ["--yes", "--purge", "-n"], None),
                (rollback_cli, "roll_back", ["--yes", "--restore-state"],
                 "thermite rollback: --yes cannot approve --restore-state, which asks you to type the installation "
                 "root\n")):
            with self.subTest(argv=argv):
                body = self.patch(module, name)
                restore = self.patch(revert_cli, "revert")
                code, out, error = self.main(module, argv)
                self.assertEqual((code, out), (2, ""))
                if words:
                    self.assertEqual(error, words)
                body.assert_not_called()
                restore.assert_not_called()


class Json(Fixture):
    """--json with a dry run: the plan the dry run shows, as one JSON object."""

    def setUp(self):
        super().setUp()
        self.patch(changeover, "release", return_value={"release": {"site": AFTER}})
        self.resolve = self.patch(changeover, "resolve")
        self.transaction = self.patch(changeover, "run")

    def report(self, module, argv):
        code, out, error = self.main(module, argv)
        self.resolve.assert_not_called()
        self.transaction.assert_not_called()
        self.assertEqual(error, "")
        self.assertEqual(len(out.splitlines()), 1, "one line: the report")
        report = json.loads(out)
        self.assertEqual(report["status"], code)
        return report

    def test_uninstall(self):
        report = self.report(uninstall_cli, ["-n", "--json"])
        self.assertEqual({key: report[key] for key in ("verb", "purge", "dry_run", "status", "steps")},
                         {"verb": "uninstall", "purge": False, "dry_run": True, "status": 0, "steps": []})
        self.assertEqual(report["said"], ["What uninstalling thermite on this host, fixture-host, would do:"])
        transaction = report["transaction"]
        self.assertEqual((transaction["kind"], transaction["pull"], transaction["confirm"], transaction["switching"]),
                         ("uninstall", False, None, []))
        self.assertEqual(transaction["summary"], changeover.outline(changeover.uninstall_work(self.context, None)))
        report = self.report(uninstall_cli, ["-n", "--json", "--purge"])
        self.assertEqual((report["purge"], report["transaction"]["kind"], report["transaction"]["confirm"]),
                         (True, "purge", ROOT))

    def test_rollback_numbers_its_preflight_and_preparation_as_its_dry_run_does(self):
        earlier = steps.Context(**{**self.context.__dict__, "head": EARLIER})
        self.patch(steps, "committed_context",
                   side_effect=lambda area, system, revision=None: earlier if revision else self.context)
        self.patch(deploy, "deploys", return_value=[
            {"commit": EARLIER, "number": 1, "deployed_at": "2026-11-01T10:00:00+00:00", "current": False},
            {"commit": AFTER, "number": 2, "deployed_at": "2026-11-04T10:00:00+00:00", "current": True}])
        self.patch(deploy, "read_record", return_value={"commit": AFTER})
        self.patch(steps, "Preconditions", side_effect=lambda context: Fake("Preconditions", Check(True, "fine")))
        self.patch(steps, "Committed", side_effect=lambda context, head: Fake(
            "Committed", Check(False, "", "the site has uncommitted changes")))
        work = changeover.Work("install", "rollback", [Fake(
            "Units", Check(False, "two units differ"), [Command(("systemctl", "daemon-reload"), root=True),
                                                        Task("wait for the fixture", lambda system: 0)])],
            [], [], None)
        self.patch(changeover, "install_work", return_value=work)
        report = self.report(rollback_cli, ["-n", "--json"])
        self.assertEqual((report["verb"], report["restore_state"], report["status"], report["commit"]),
                         ("rollback", False, 1, EARLIER))
        self.assertEqual(report["deploy"], {"number": 1, "deployed_at": "2026-11-01T10:00:00+00:00"})
        self.assertEqual(report["steps"], [
            {"number": 1, "title": "Preconditions", "done": True, "detail": "fine", "problem": None, "actions": []},
            {"number": 2, "title": "Committed", "done": False, "detail": "",
             "problem": "the site has uncommitted changes", "actions": []},
            {"number": 3, "title": "Units", "done": False, "detail": "two units differ", "problem": None,
             "actions": [{"argv": ["systemctl", "daemon-reload"], "root": True},
                         {"task": "wait for the fixture", "interactive": False}]}])
        self.assertEqual(report["transaction"]["summary"], changeover.outline(work))
        self.assertTrue(report["said"][0].startswith(f"What rolling back thermite on this host, fixture-host, to "
                                                     f"{EARLIER[:12]}, deploy #1"))

    def test_restore_state(self):
        self.kept()
        report = self.report(rollback_cli, ["--restore-state", "-n", "--json"])
        self.assertEqual((report["verb"], report["restore_state"], report["status"], report["reverts"],
                          report["commit"]), ("rollback", True, 0, txn_fakes.OLD, BEFORE))
        self.assertEqual(report["steps"], [{"number": 1, "title": "Account", "done": False,
                                            "detail": "fixture account", "problem": None,
                                            "actions": [{"task": "make the fixture account",
                                                         "interactive": False}]}])
        transaction = report["transaction"]
        self.assertEqual((transaction["kind"], transaction["reinstate"], transaction["confirm"],
                          transaction["switching"]),
                         ("revert", True, ROOT, ["Put the operator's files back", "Pin the previous version"]))
        self.assertIn("is lost", "\n".join(report["said"]))

    def test_a_refusal_is_reported_with_what_it_said(self):
        self.patch(manifest, "installed", return_value=None)
        report = self.report(rollback_cli, ["--restore-state", "-n", "--json"])
        self.assertEqual((report["status"], report["steps"], report["transaction"]), (2, [], None))
        self.assertEqual(report["said"], ["Nothing was changed: Thermite is not installed here, so there is nothing "
                                          "to revert."])

    def test_json_needs_a_dry_run(self):
        for module, name, argv in ((uninstall_cli, "uninstall", ["--json"]), (rollback_cli, "roll_back", ["--json"]),
                                   (rollback_cli, "roll_back", ["--json", "--restore-state"])):
            with self.subTest(argv=argv):
                body = self.patch(module, name)
                code, out, error = self.main(module, argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--json reports what a dry run would do: give it with -n/--dry-run", error)
                body.assert_not_called()

    def test_a_dry_run_without_json_still_prints_its_steps(self):
        found = Fake("Units", Check(False, "two units differ"), [Command(("systemctl", "daemon-reload"), root=True)])
        self.assertEqual(runner.plan([found], self.system, first=4)[0], 0)
        self.assertEqual(runner.run([found], self.system, self.site, dry_run=True, first=4), 0)
        self.assertEqual([call.args[0] for call in self.system.say.call_args_list],
                         ["4. Units: two units differ", "   would run: sudo systemctl daemon-reload"])

    def test_a_done_step_lists_nothing_unless_it_always_runs(self):
        done = Check(True, "in place")
        status, found = runner.plan([Fake("Done", done, [Task("redo", lambda system: 0)]),
                                     Fake("Always", done, [Task("verify", lambda system: 0)], always=True)],
                                    self.system)
        self.assertEqual((status, [entry["actions"] for entry in found]),
                         (0, [[], [{"task": "verify", "interactive": False}]]))


class TokenFile(Fixture):
    """--token-file FILE, once --runtime-token-file, on install and upgrade."""

    def test_the_new_spelling_and_the_old_one_with_a_warning(self):
        for module in (install_cli, upgrade_cli):
            with self.subTest(module=module.__name__):
                run = self.patch(install_cli, "run", return_value=0)
                self.assertEqual(self.main(module, ["--token-file", "/fictional/token"]), (0, "", ""))
                self.assertEqual(run.call_args.args[1].runtime_token_file, Path("/fictional/token"))
                code, _, error = self.main(module, ["--runtime-token-file", "/fictional/token"])
                self.assertEqual(code, 0)
                verb = module.__name__.rsplit(".", 1)[1]
                self.assertEqual(error, f"thermite {verb}: --runtime-token-file is now --token-file; "
                                        "--runtime-token-file goes in the next release\n")
                self.assertEqual(run.call_args.args[1].runtime_token_file, Path("/fictional/token"))
                help = oh_cli.parser_of(verb).format_help()
                self.assertIn("--token-file FILE", help)
                self.assertNotIn("--runtime-token-file", help)

    def test_the_host_phase_takes_the_spelling_the_workflow_hands_it_without_a_warning(self):
        for module in (install_cli, upgrade_cli):
            for argv in (["--runtime-token-file", "/fictional/token"], ["--runtime-token-file=/fictional/token"]):
                with self.subTest(module=module.__name__, argv=argv):
                    run = self.patch(install_cli, "run", return_value=0)
                    self.assertEqual(self.main(module, ["--_host-phase", "--_run-approved", *argv]), (0, "", ""))
                    self.assertEqual(run.call_args.args[1].runtime_token_file, Path("/fictional/token"))


if __name__ == "__main__":
    unittest.main()
