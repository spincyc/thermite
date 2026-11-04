"""A live terminal uses its committed site's model, never fictional controls."""
from pathlib import Path
import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_brand
import oh_deploy
from oh_cli import remote

MODEL = {"version": 1, "rooms": [{"id": "synthetic_room", "label": "Synthetic room", "sources": []}],
         "sources": {}, "devices": {}, "initial": {}}
CONTENT = ("globalThis.REMOTE_HOUSE = " + json.dumps(MODEL) + ";\n").encode()


class SiteModel(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/split_input_review"))
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="remote-model-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root / "site"
        self.site.mkdir()
        self.arguments = argparse.Namespace(mock=False, native=True, url=None, sandbox=False)
        self.enterContext(mock.patch.object(remote, "AREA", self.site))

    def schema_two(self):
        (self.site / "thermite-site.json").write_text(json.dumps(
            {"version": 2, "application": "0.1.0.dev0", "release": "a" * 64}))
        self.resolve = self.enterContext(mock.patch.object(oh_deploy, "resolve", return_value="b" * 40))
        self.prepare = self.enterContext(mock.patch.object(oh_deploy, "prepared_files", return_value=(
            {remote.MODEL: oh_deploy.Blob(CONTENT)}, {"digest": "a" * 64})))

    def test_committed_model_overrides_working_output_and_is_private_and_content_addressed(self):
        self.schema_two()
        working = self.site / remote.MODEL
        working.parent.mkdir(parents=True)
        working.write_text("uncommitted output must not become controls")
        selected = remote.model_file(self.arguments)
        self.resolve.assert_called_once_with(self.site, "HEAD")
        self.prepare.assert_called_once_with(self.site, "b" * 40, self.site / "var")
        self.assertEqual(selected, self.site / "var/tui-remote/models" / (hashlib.sha256(CONTENT).hexdigest() + ".js"))
        self.assertEqual(selected.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(selected.parent.stat().st_mode), 0o700)
        self.assertEqual(working.read_text(), "uncommitted output must not become controls")
        inode = selected.stat().st_ino
        self.assertEqual(remote.model_file(self.arguments), selected)
        self.assertEqual(selected.stat().st_ino, inode, "a valid model needs no rewrite")
        selected.write_bytes(b"incomplete previous write")
        remote.model_file(self.arguments)
        self.assertEqual(selected.read_bytes(), CONTENT)

    def test_model_and_parent_symlinks_are_refused_without_touching_their_targets(self):
        self.schema_two()
        selected = remote.model_file(self.arguments)
        outside = self.root / "unrelated.js"
        outside.write_bytes(b"unrelated owner file")
        outside.chmod(0o600)
        selected.unlink()
        selected.symlink_to(outside)
        with self.assertRaises(remote.RemoteModelError):
            remote.model_file(self.arguments)
        self.assertEqual(outside.read_bytes(), b"unrelated owner file")
        shutil.rmtree(self.site / "var")
        directory = self.root / "unrelated-directory"
        directory.mkdir()
        (self.site / "var").symlink_to(directory, target_is_directory=True)
        with self.assertRaises(remote.RemoteModelError):
            remote.model_file(self.arguments)
        self.assertEqual(list(directory.iterdir()), [])

    def test_executable_or_incomplete_snapshot_model_is_refused_before_cache_writes(self):
        self.schema_two()
        for content in (b"globalThis.REMOTE_HOUSE={};process.exit(1);", b"globalThis.REMOTE_HOUSE={};",
                        b"x" * (remote.MODEL_LIMIT + 1)):
            self.prepare.return_value = ({remote.MODEL: oh_deploy.Blob(content)}, {})
            with self.subTest(size=len(content)), self.assertRaises(remote.RemoteModelError):
                remote.model_file(self.arguments)
            self.assertFalse((self.site / "var").exists())

    def test_source_and_schema_one_need_generation_without_demo_fallback(self):
        for descriptor in (None, {"version": 1, "application": "0.1.0.dev0", "release": "a" * 64}):
            if descriptor:
                (self.site / "thermite-site.json").write_text(json.dumps(descriptor))
            with self.subTest(descriptor=descriptor), self.assertRaisesRegex(remote.RemoteModelError, "thermite generate"):
                remote.model_file(self.arguments)
        path = self.site / remote.MODEL
        path.parent.mkdir(parents=True)
        path.write_bytes(CONTENT)
        self.assertEqual(remote.model_file(self.arguments), path)

    def test_native_and_authenticated_launches_both_use_the_committed_model(self):
        self.schema_two()
        credential = self.root / "personal-access.json"
        credential.write_text("credential validation belongs to the shared client")
        for native in (True, False):
            with self.subTest(native=native), mock.patch.object(remote, "native_mode", return_value=native), \
                    mock.patch.object(remote, "target", return_value=["--url", "https://site.example.invalid"]), \
                    mock.patch("oh_access.client.credential_path", return_value=credential), \
                    mock.patch("oh_access.client.saved_ca", return_value=None):
                environment = remote.launch_environment(self.arguments, oh_brand.load())
            self.assertEqual(Path(environment["THERMITE_REMOTE_HOUSE_FILE"]).read_bytes(), CONTENT)
            self.assertEqual("THERMITE_REMOTE_TOKEN_FILE" in environment, not native)

    def test_mock_without_generated_model_omits_even_an_inherited_explicit_path(self):
        self.arguments.mock = True
        with mock.patch.dict(os.environ, {"THERMITE_REMOTE_HOUSE_FILE": str(self.root / "missing.js")}), \
                mock.patch.object(oh_deploy, "prepared_files") as prepare:
            environment = remote.launch_environment(self.arguments, oh_brand.load())
        self.assertNotIn("THERMITE_REMOTE_HOUSE_FILE", environment)
        prepare.assert_not_called()

    def test_missing_model_reports_generation_instead_of_a_login_failure(self):
        options = argparse.Namespace(mock=False, native=True, url=None, sandbox=False, latency=None,
                                     matrix_down=False, setup=False, theme=None)
        parser = mock.Mock()
        parser.parse_args.return_value = options
        error = io.StringIO()
        with mock.patch.object(remote, "parser", return_value=parser), \
                mock.patch.object(remote.oh_cli.switches, "quiet", False), \
                mock.patch.object(remote, "palette", return_value=oh_brand.load()), \
                mock.patch.object(remote.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(remote, "ready", return_value=True), \
                mock.patch.object(remote, "node_argv", return_value=["/usr/bin/node"]), \
                mock.patch.object(remote.os, "execve") as execute, contextlib.redirect_stderr(error):
            self.assertEqual(remote.main([]), 2)
        self.assertIn("thermite generate", error.getvalue())
        self.assertNotIn("log in", error.getvalue())
        execute.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is required for the shared model loader")
    def test_shared_loader_uses_selected_model_and_refuses_a_missing_explicit_path(self):
        self.schema_two()
        selected = remote.model_file(self.arguments)
        script = """const shared = require(process.argv[1]);
globalThis.fetch = () => { throw new Error('network forbidden'); };
try { process.stdout.write(JSON.stringify(shared.load().house.rooms.map(room => room.id))); }
catch (_) { process.stderr.write('selected model unavailable'); process.exitCode = 2; }
"""
        environment = {**os.environ, "THERMITE_REMOTE_HOUSE_FILE": str(selected)}
        command = [shutil.which("node"), "-e", script, str(AREA / "tui/remote/shared.js")]
        good = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout), ["synthetic_room"])
        selected.unlink()
        missing = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

    @unittest.skipUnless(shutil.which("node"), "Node is required for the topology preview")
    def test_mock_topology_uses_bundled_catalogue_without_generated_site_files(self):
        runtime = self.root / "runtime"
        for name in ("tui/remote/topology.js", "tui/remote/grid.js", "web/remote/topology.json"):
            destination = runtime / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(AREA / name, destination)
        script = """const topology = require(process.argv[1]);
const backend = topology.create({ mock: true, fetch: () => { throw Error('network forbidden'); } });
backend.open().then(manifest => {
  if (backend.state().error || !manifest) throw Error('bundled preview unavailable');
  process.stdout.write(JSON.stringify(manifest.rooms.map(room => room.id)));
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(runtime / "tui/remote/topology.js")],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = json.loads((runtime / "web/remote/topology.json").read_text())
        self.assertEqual(json.loads(result.stdout), [room["id"] for room in expected["rooms"]])
        self.assertFalse((runtime / "conf").exists())


if __name__ == "__main__":
    unittest.main()
