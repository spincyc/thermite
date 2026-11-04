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
            "root: /srv/thermite-fixture\ntimezone: Etc/UTC\nhttp_port: 13601\nhttps_port: 13602\nuid: 9001\ngid: 9001\n")
        (self.site / "versions.json").write_text('{"native":"fixture-one"}\n')
        self.var = self.site / "var"
        self.var.mkdir()

    def release(self, marker):
        script = '''import json, os, sys
from pathlib import Path
assert sys.argv[1] == '--site' and sys.argv[3:] == ['generate']
site = Path(sys.argv[2])
assert os.environ['THERMITE_SITE'] == str(site)
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

    def test_history_boot_waiver_must_match_the_actual_final_render_before_installation(self):
        release, _ = self.release("fenced")
        commit = self.revision(release, "config")
        historical = {"conf/fixture.js": deploy.Blob(b"previously observed output")}
        digest = deploy.tree_digest({"fixture.js": historical["conf/fixture.js"]})
        with mock.patch.object(deploy, "install") as install:
            with self.assertRaisesRegex(deploy.DeployError, "final configuration differs"):
                deploy.deploy(self.site, deploy.State(self.root / "state"), self.var, commit,
                              force=True, expected_conf_digest=digest)
            install.assert_not_called()

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

    def test_schema1_keeps_committed_generated_outputs_and_full_checkout_keeps_legacy_export(self):
        release, _ = self.release("legacy")
        (self.site / "conf").mkdir()
        (self.site / "conf/legacy.js").write_text("committed legacy configuration")
        git(self.site, "add", "-f", "conf")
        commit = self.revision(release, "legacy", schema=1)
        files, application = deploy.deployment_files(self.site, commit, self.var)
        self.assertEqual(files["conf/legacy.js"].data, b"committed legacy configuration")
        self.assertEqual(application["digest"], release["digest"])
        git(self.site, "rm", "thermite-site.json")
        git(self.site, "commit", "-qm", "complete checkout")
        files, application = deploy.deployment_files(self.site, git(self.site, "rev-parse", "HEAD"), self.var)
        self.assertEqual(files["conf/legacy.js"].data, b"committed legacy configuration")
        self.assertIsNone(application)

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

    def test_upgrade_preserves_private_state_and_ignore_rules(self):
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
        descriptor = json.loads((target / "thermite-site.json").read_text())
        descriptor["version"] = 1
        (target / "thermite-site.json").write_text(json.dumps(descriptor))
        (target / ".gitignore").write_text("# owner rule\n/private-notes/\n")
        (target / "var/operator-state").write_bytes(b"retain private state")
        report = oh_init.initialize(target, program=program, upgrade=True)
        self.assertEqual(report["previous"]["version"], 1)
        self.assertEqual(json.loads((target / "thermite-site.json").read_text())["version"], 2)
        self.assertEqual((target / "var/operator-state").read_bytes(), b"retain private state")
        self.assertTrue((target / ".gitignore").read_text().startswith("# owner rule\n/private-notes/\n"))
        self.assertTrue({"/conf/", "/metrics/", "/deploy/"} <= set((target / ".gitignore").read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
