"""The install transaction's owner-side writes (lib/oh_install/writes.py):
what its Deploy step and its history records run, in place of a verb."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_paths  # noqa: E402
from oh_install import writes  # noqa: E402

COMMIT = "d" * 40
TRANSACTION = "20261105T101500Z-3f9a2c"


class DeployCommit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name) / "site"
        self.host = {"root": str(Path(temporary.name) / "installation"), "uid": 9001, "gid": 9001}
        self.layout = deploy.state(self.host)
        self.result = deploy.Deployed(
            COMMIT, "Pin the fixture release",
            deploy.Changes(added=["items/new.items"], changed=["rules/a.rules"], removed=["things/old.things"],
                           unchanged=4),
            deploy.Changes(changed=["oh_atv.py"], unchanged=2),
            {"boot_checked": True}, False, with_metrics=True,
            metrics=deploy.Changes(changed=["prometheus/prometheus.yml"], unchanged=3),
            exporter_app=deploy.Changes(unchanged=5),
            reloads=["Prometheus is not running yet; it reads the new configuration when it starts"])

    def run_main(self, *argv, compatible=None, applied=None):
        """(exit status, what it printed, what it complained of, the deploy and the selection it called)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(writes, "AREA", self.site), \
                mock.patch.object(oh_paths, "require_compatible", side_effect=compatible), \
                mock.patch.object(deploy, "selection", return_value=(COMMIT, self.host, None)) as selection, \
                mock.patch.object(deploy, "deploy", return_value=self.result, side_effect=applied) as run, \
                mock.patch.object(deploy, "record_gateway") as gateway, \
                mock.patch.object(deploy, "record_rollback") as rollback, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = writes.main(list(argv))
        self.assertFalse(gateway.called or rollback.called)
        return code, out.getvalue().splitlines(), err.getvalue(), run, selection

    def test_the_selected_commit_goes_into_its_own_hosts_state_and_the_changes_are_listed(self):
        code, lines, complaint, run, selection = self.run_main("deploy", COMMIT, "upgrade")
        self.assertEqual((code, complaint), (0, ""))
        selection.assert_called_once_with(self.site, COMMIT)
        (area, layout, var, commit), options = run.call_args
        self.assertEqual((area, layout.root, var, commit), (self.site, self.layout.root, self.site / "var", COMMIT))
        # Nothing forces, previews or approves: the transaction around it did.
        self.assertEqual(options, {"how": "upgrade", "host": self.host})
        self.assertEqual(lines, [
            f"deployed {COMMIT[:12]} Pin the fixture release",
            "conf    1 added, 1 changed, 1 removed, 4 unchanged",
            "          added   items/new.items",
            "          changed rules/a.rules",
            "          removed things/old.things",
            "helper  0 added, 1 changed, 0 removed, 2 unchanged",
            "          changed oh_atv.py",
            "metrics 0 added, 1 changed, 0 removed, 3 unchanged",
            "          changed prometheus/prometheus.yml",
            "exporter0 added, 0 changed, 0 removed, 5 unchanged",
            "metrics Prometheus is not running yet; it reads the new configuration when it starts"])

    def test_kept_metrics_say_so_instead_of_their_changes(self):
        self.result.metrics_kept = True
        _, lines, _, _, _ = self.run_main("deploy", COMMIT, "rollback")
        self.assertEqual(lines[-1], f"metrics kept as deployed: {COMMIT[:12]} predates the installed metrics, "
                                    f"so {self.layout.metrics} and {self.layout.exporter_app} keep what the last "
                                    "deploy installed")
        self.assertFalse([line for line in lines if line.startswith("exporter")])

    def test_a_refusal_is_said_once_and_fails_the_step(self):
        for refusal in (deploy.DeployError("fixture stop"), PermissionError("fixture stop")):
            with self.subTest(refusal=type(refusal).__name__):
                code, lines, complaint, _, _ = self.run_main("deploy", COMMIT, "install", applied=refusal)
                self.assertEqual((code, lines, complaint), (2, [], "thermite: fixture stop\n"))
        code, _, complaint, run, _ = self.run_main("deploy", COMMIT, "install",
                                                   compatible=oh_paths.SiteError("another release is pinned"))
        self.assertEqual((code, complaint), (2, "thermite: another release is pinned\n"))
        run.assert_not_called()

    def test_it_takes_no_switch_and_no_other_word(self):
        for argv in ([], ["deploy"], ["deploy", COMMIT], ["deploy", COMMIT, "deploy"], ["install"],
                     ["deploy", "HEAD", "install"], ["deploy", COMMIT[:12], "install"],
                     ["deploy", COMMIT, "install", "--force"], ["deploy", "--dry-run", "install"],
                     ["--state", "/tmp", "deploy", COMMIT, "install"], ["gateway", "/tmp"], ["rolled-back"]):
            with self.subTest(argv=argv):
                code, lines, complaint, run, selection = self.run_main(*argv)
                self.assertEqual((code, lines), (2, []))
                self.assertIn("the install transaction runs this file", complaint)
                self.assertFalse(run.called or selection.called)


class RunByPath(unittest.TestCase):
    """As the transaction runs it: the file by its path, from the site."""

    def test_it_reads_the_application_beside_it_and_leaves_nothing_there(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = deploy.State(Path(temporary) / "runtime")
            layout.history.parent.mkdir(parents=True)
            layout.history.write_text(json.dumps({"deployed_at": "2026-11-05T10:20:00+00:00", "commit": COMMIT,
                                                  "by": "owner", "subject": "Pin", "how": "upgrade"}) + "\n")
            event = {"transaction": TRANSACTION, "commit": COMMIT, "restored": "c" * 40}
            # -v's import trail, bounded by the application's own size, says
            # which file each module came from and whether bytecode was written.
            done = subprocess.run(
                [sys.executable, str(AREA / "lib/oh_install/writes.py"), "rolled-back", str(layout.root),
                 json.dumps(event)], cwd=temporary, env={"PATH": os.environ["PATH"], "PYTHONVERBOSE": "1"},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            [entry] = deploy.deploys(layout)
            self.assertEqual(entry["rolled_back"][0]["transaction"], TRANSACTION)
        trail = done.stderr
        self.assertIn(str(AREA / "lib/oh_deploy.py"), trail)
        self.assertNotIn(str(AREA / "lib/oh_install/secrets.py"), trail,
                         "the standard library's secrets, not the file beside this one")
        self.assertNotIn("# wrote ", trail)
        self.assertNotIn(str(AREA / "lib/__pycache__"), trail)

    def test_a_site_is_selected_as_the_launcher_selects_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            done = subprocess.run(
                [sys.executable, str(AREA / "lib/oh_install/writes.py"), "--config", "--config", temporary],
                cwd=temporary, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "thermite: --config needs a directory\n"))


if __name__ == "__main__":
    unittest.main()
