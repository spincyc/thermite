"""Committed-input deployment with two offline synthetic application releases."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_deploy as deploy
import oh_init
import oh_release
import oh_site_snapshot
from oh_cli import rollback
from oh_install import steps


def git(site, *args):
    return subprocess.run(["git", "-C", str(site), *args], check=True, capture_output=True, text=True).stdout.strip()


class CommittedSnapshots(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/split_migration_audit"))
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root / "site"
        self.site.mkdir()
        git(self.site, "init", "-q")
        git(self.site, "config", "user.name", "Snapshot fixture")
        git(self.site, "config", "user.email", "snapshot@example.invalid")
        (self.site / ".gitignore").write_text("/var/\n/conf/\n/metrics/\n/deploy/\n")
        (self.site / "instance").mkdir()
        (self.site / "instance/host.yaml").write_text(
            "root: /srv/thermite-fixture\ntimezone: Etc/UTC\nhttp_port: 13602\nhttps_port: 13603\nuid: 9001\ngid: 9001\n")
        (self.site / "versions.json").write_text('{"native":"fixture-one"}\n')
        self.var = self.site / "var"
        self.var.mkdir()

    def release(self, marker):
        script = '''import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['generate']
site = Path(os.environ['THERMITE_SITE'])
assert 'PYTHONPATH' not in os.environ and 'PYTHONHOME' not in os.environ
assert 'OPENHAB_SECRETS' not in os.environ and not (site / 'lib').exists()
data = json.loads((site / 'instance/devices.yaml').read_text())
outputs = {'conf/fixture.js': 'APP=' + MARKER + ';HOUSE=' + data['fixture'],
           'metrics/fixture.json': json.dumps({'application': MARKER, 'house': data['fixture']}),
           'deploy/thermite-openhab.container': MARKER + '\\n' + (site / 'instance/host.yaml').read_text()}
for name, value in outputs.items():
    path = site / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
'''.replace("MARKER", repr(marker))
        files = {name: ("fixture helper " + marker).encode() for name in
                 dict.fromkeys((*deploy.ATV_APP, *deploy.metrics.EXPORTER_APP))}
        files["bin/thermite"] = script.encode()
        identity = oh_release.create(files)
        path = self.var / "application-releases" / identity["digest"]
        path.mkdir(parents=True)
        for name, value in files.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        (path / oh_release.FILE).write_bytes(oh_release.encoded(identity))
        return identity, path

    def revision(self, release, house, schema=2):
        (self.site / "thermite-site.json").write_text(json.dumps(
            {"version": schema, "application": release["application"], "release": release["digest"]}))
        (self.site / "instance/devices.yaml").write_text(json.dumps({"fixture": house}))
        git(self.site, "add", ".")
        git(self.site, "commit", "-qm", house)
        return git(self.site, "rev-parse", "HEAD")

    def test_old_revision_uses_exact_old_application_and_committed_inputs_ignoring_working_cache(self):
        first, _ = self.release("one")
        old = self.revision(first, "old")
        second, _ = self.release("two")
        current = self.revision(second, "current")
        (self.site / "instance/devices.yaml").write_text('{"fixture":"uncommitted"}')
        for prefix in ("conf", "metrics", "deploy"):
            (self.site / prefix).mkdir()
            (self.site / prefix / "untrusted.txt").write_text("working output is not authority")
        injected = self.site / "lib"
        injected.mkdir()
        (injected / "sitecustomize.py").write_text("raise RuntimeError('working code must never execute')")
        before = sorted(str(path.relative_to(self.var)) for path in self.var.rglob("*"))
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(injected), "PYTHONHOME": str(injected),
                                          "OPENHAB_SECRETS": "must-not-be-inherited"}):
            previous, application = deploy.prepared_files(self.site, old, self.var)
            latest, _ = deploy.prepared_files(self.site, current, self.var)
        self.assertEqual(previous["conf/fixture.js"].data, b"APP=one;HOUSE=old")
        self.assertEqual(latest["conf/fixture.js"].data, b"APP=two;HOUSE=current")
        self.assertEqual(application, {"version": first["application"], "digest": first["digest"]})
        self.assertFalse(any("untrusted" in name for name in previous))
        self.assertEqual(before, sorted(str(path.relative_to(self.var)) for path in self.var.rglob("*")))
        self.assertFalse(list(self.var.glob(".site-render-*")))
        self.assertEqual((self.site / "instance/devices.yaml").read_text(), '{"fixture":"uncommitted"}')

    def test_units_and_host_facts_are_derived_from_the_selected_commit(self):
        release, _ = self.release("units")
        commit = self.revision(release, "config")
        prepared, _ = deploy.prepared_files(self.site, commit, self.var)
        target = self.root / "installed.container"
        target.write_bytes(prepared["deploy/thermite-openhab.container"].data)
        mapping = {"deploy/thermite-openhab.container": target}
        self.assertEqual(deploy.unit_differences(self.site, commit, mapping), [])
        target.write_text("foreign unit")
        self.assertEqual(deploy.unit_differences(self.site, commit, mapping), list(mapping))
        (self.site / "instance/host.yaml").write_text("invalid working host")
        versions, host, requirement = deploy.committed_inputs(self.site, commit)
        self.assertEqual(host["root"], "/srv/thermite-fixture")
        self.assertEqual(versions, {"native": "fixture-one"})
        self.assertEqual(requirement["version"], 2)

    def test_tampered_or_missing_exact_release_fails_before_rendering(self):
        release, cache = self.release("tamper")
        commit = self.revision(release, "config")
        (cache / "bin/thermite").write_text("tampered application")
        with mock.patch.object(oh_site_snapshot, "render") as render:
            with self.assertRaisesRegex(deploy.DeployError, "exact application release"):
                deploy.prepared_files(self.site, commit, self.var)
            render.assert_not_called()

    def test_schema2_refuses_committed_application_code_and_instance_executables(self):
        release, _ = self.release("inputs")
        self.revision(release, "config")
        (self.site / "lib").mkdir()
        code = self.site / "lib/injected.py"
        code.write_text("raise RuntimeError('must never execute')")
        git(self.site, "add", "lib")
        git(self.site, "commit", "-qm", "invalid application source")
        with self.assertRaisesRegex(deploy.DeployError, "must not contain application source"):
            deploy.prepared_files(self.site, git(self.site, "rev-parse", "HEAD"), self.var)
        git(self.site, "rm", "lib/injected.py")
        (self.site / "instance/injected.py").write_text("raise RuntimeError('must never execute')")
        git(self.site, "add", "instance")
        git(self.site, "commit", "-qm", "invalid data input")
        with self.assertRaisesRegex(deploy.DeployError, "could not derive committed"):
            deploy.prepared_files(self.site, git(self.site, "rev-parse", "HEAD"), self.var)
        self.assertFalse(list(self.var.glob(".site-render-*")))

    def checkout_commit(self):
        """A commit that carries no descriptor, with generated conf/ committed."""
        (self.site / "conf").mkdir()
        (self.site / "conf/committed.js").write_text("committed generated configuration")
        git(self.site, "add", "-f", "conf")
        git(self.site, "add", ".")
        git(self.site, "commit", "-qm", "no descriptor")
        return git(self.site, "rev-parse", "HEAD")

    def test_a_checkouts_commit_is_its_committed_trees(self):
        files, application = deploy.deployment_files(self.site, self.checkout_commit(), self.var)
        self.assertEqual(files["conf/committed.js"].data, b"committed generated configuration")
        self.assertIsNone(application)

    def test_a_commit_without_the_version_2_descriptor_is_refused_by_name_before_anything_renders(self):
        release, _ = self.release("retired")
        bare = self.checkout_commit()
        first = self.revision(release, "schema one", schema=1)
        self.revision(release, "current")
        said = (f"^{first[:12]} is a schema-1 site commit \\(thermite-site.json says version 1: the retired layout, "
                "whose generated conf/ was committed\\), and this thermite installs only a site commit whose "
                "thermite-site.json says version 2. To run that commit's house again, commit its instance/ and "
                "versions.json on top of HEAD, then thermite check and thermite install$")
        system = mock.Mock()
        with mock.patch.object(oh_site_snapshot, "render") as render:
            for read in (lambda: deploy.prepared_files(self.site, first, self.var),
                         lambda: deploy.committed_inputs(self.site, first),
                         lambda: deploy.selection(self.site, first),
                         lambda: deploy.unit_differences(self.site, first),
                         lambda: deploy.installed_units(self.site, rev=first),
                         # thermite rollback COMMIT plans the commit as an install plans HEAD.
                         lambda: steps.committed_context(self.site, system, revision=first)):
                with self.assertRaisesRegex(deploy.DeployError, said):
                    read()
            with self.assertRaisesRegex(deploy.DeployError, (
                    f"^{bare[:12]} holds no thermite-site.json for this site \\(it is from before the site had "
                    "one\\), and this thermite installs only a site commit whose thermite-site.json says version 2. ")):
                steps.committed_context(self.site, system, revision=bare)
            render.assert_not_called()
        self.assertEqual(system.mock_calls, [])
        # The verb plans HEAD, which renders, then refuses the commit it was given.
        with mock.patch.object(rollback, "AREA", self.site), self.assertRaisesRegex(deploy.DeployError, said):
            rollback.roll_back(mock.Mock(), first, True)
        self.assertFalse(list(self.var.glob(".site-render-*")))

    def test_failed_render_redacts_child_output_and_cleans_temporary_files(self):
        release, cache = self.release("failure")
        commit = self.revision(release, "config")
        inputs = {name: blob.data for name, blob in deploy.export(
            self.site, commit, ("instance", "versions.json", "thermite-site.json")).items()}
        result = subprocess.CompletedProcess([], 2, stdout=b"private fixture input", stderr=b"private fixture input")
        with mock.patch.object(oh_site_snapshot.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(oh_site_snapshot.SnapshotError, "refused the committed") as error:
                oh_site_snapshot.render(inputs, cache, self.var, release["digest"])
        self.assertNotIn("private fixture input", str(error.exception))
        self.assertFalse(list(self.var.glob(".site-render-*")))
        [log] = self.var.glob("site-render-error-*.log")
        self.assertIn(str(log), str(error.exception))
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"private fixture input", log.read_bytes())

    def test_upgrade_refuses_schema_1_and_preserves_private_state_and_ignore_rules(self):
        _, cached = self.release("launcher")
        program = self.root / "program"
        shutil.copytree(cached, program)
        for name in ("instance/devices.yaml", "instance/host.yaml", "versions.json"):
            target = program / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.site / name).read_bytes() if name != "instance/devices.yaml" else b'{"fixture":"new"}')
        files = {path.relative_to(program).as_posix(): path.read_bytes() for path in program.rglob("*")
                 if path.is_file() and path.name != oh_release.FILE}
        (program / oh_release.FILE).write_bytes(oh_release.encoded(oh_release.create(files)))
        target = self.root / "initialized-site"
        oh_init.initialize(target, program=program)
        pinned = json.loads((target / "thermite-site.json").read_text())
        (target / ".gitignore").write_text("# owner rule\n/private-notes/\n")
        (target / "var/operator-state").write_bytes(b"retain private state")
        # A site at schema 1 is refused by name, and left as it was.
        (target / "thermite-site.json").write_text(json.dumps({**pinned, "version": 1}))
        before = {path.name: path.read_bytes() for path in target.iterdir() if path.is_file()}
        with self.assertRaisesRegex(ValueError, f"^{target} is a schema-1 site .* then upgrade again$"):
            oh_init.initialize(target, program=program, upgrade=True)
        self.assertEqual({path.name: path.read_bytes() for path in target.iterdir() if path.is_file()}, before)
        (target / "thermite-site.json").write_text(json.dumps(pinned))
        report = oh_init.initialize(target, program=program, upgrade=True)
        self.assertEqual(report["previous"], pinned)
        self.assertEqual(json.loads((target / "thermite-site.json").read_text()), pinned)
        self.assertEqual((target / "var/operator-state").read_bytes(), b"retain private state")
        self.assertTrue((target / ".gitignore").read_text().startswith("# owner rule\n/private-notes/\n"))
        self.assertTrue({"/conf/", "/metrics/", "/deploy/"} <= set((target / ".gitignore").read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
