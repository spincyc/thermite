"""Pure packaging/ownership tests; actual Java lifecycle tests are a separate explicit build gate."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch as mocked
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_androidtv_patch as patch


class Packaging(unittest.TestCase):
    def test_exact_pins_and_pid_override_scope(self):
        pins = patch.pins()
        self.assertEqual(pins["upstream"], "5.2.3")
        self.assertEqual(pins["patched"], "5.2.3.thermite1")
        self.assertEqual(set(pins["assets"]), {"manager", "handler", "bundle", "compiler", "annotations"})
        for entry in pins["assets"].values():
            self.assertTrue(entry["url"].startswith("https://"))
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
        original = '# retained\nfile:/openhab/addons/other.jar;range="[1,2)"\n'
        merged = patch.override_text(original)
        self.assertTrue(merged.startswith(original))
        self.assertIn("mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1", merged)
        self.assertIn('range="[5.2.3,5.2.4)"', merged)
        self.assertEqual(patch.override_text(merged), merged)
        with self.assertRaises(patch.PatchError):
            patch.override_text("file:/openhab/addons/androidtv-custom.jar\n")
        sandbox = Path("/owned fixture/userdata/thermite/maven")
        self.assertIn(sandbox.as_uri(), patch.repository_entry(sandbox))
        with self.assertRaises(patch.PatchError):
            patch.repository_entry(Path("relative"))

    def test_private_maven_default_preserves_existing_caches_repositories_and_other_settings(self):
        original = ('# keep comments\norg.ops4j.pax.url.mvn.localRepository=${openhab.userdata}/tmp/mvn\n'
                    'org.ops4j.pax.url.mvn.defaultRepositories=\\\n'
                    '    ${karaf.home.uri}system@id=system.repository, \\\n'
                    '    ${karaf.data.uri}tmp/kar@id=kar.repository\n'
                    '# unrelated\norg.ops4j.pax.url.mvn.repositories=https://example.invalid/maven\n')
        result = patch.maven_config_text(original, repository=Path('/owned/maven'))
        self.assertIn('defaultRepositories=file:///owned/maven/@id=thermite.androidtv,', result)
        self.assertIn('${karaf.home.uri}system@id=system.repository', result)
        self.assertIn('${karaf.data.uri}tmp/kar@id=kar.repository', result)
        self.assertIn('localRepository=${openhab.userdata}/tmp/mvn\n', result)
        self.assertTrue(result.endswith('# unrelated\norg.ops4j.pax.url.mvn.repositories=https://example.invalid/maven\n'))
        self.assertEqual(patch.maven_config_text(result, repository=Path('/owned/maven')), result)
        for bad in ('# no effective property\n', original + '\n' + original,
                    result.replace('file:///owned/maven/', 'file:///other/maven/')):
            with self.subTest(bad=bad), self.assertRaises(patch.PatchError):
                patch.maven_config_text(bad, repository=Path('/owned/maven'))

    def test_rebuild_is_deterministic_and_preserves_unrelated_protocol_resources(self):
        scratch = AREA.parents[1] / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            original = root / "vendor.jar"
            manifest = (b'Manifest-Version: 1.0\r\nBundle-Version: 5.2.3\r\n'
                        b'Import-Package: unchanged\r\n\r\nName: class\r\nSHA-256-Digest: obsolete\r\n\r\n')
            with zipfile.ZipFile(original, "w") as out:
                out.writestr("META-INF/MANIFEST.MF", manifest)
                out.writestr("META-INF/VENDOR.SF", b"invalid after patch")
                out.writestr("META-INF/VENDOR.RSA", b"invalid after patch")
                out.writestr("OH-INF/thing/googletv.xml", b"unchanged pairing schema")
                out.writestr("LICENSE", b"upstream attribution")
            classes = {name: b"synthetic class" for name in patch.EXPECTED_CLASSES}
            first, second = root / "first.jar", root / "second.jar"
            one = patch.rebuild(original, classes, first)
            two = patch.rebuild(original, dict(reversed(list(classes.items()))), second)
            self.assertEqual(one["sha256"], two["sha256"])
            with zipfile.ZipFile(first) as out:
                self.assertFalse(any(name.endswith((".SF", ".RSA")) for name in out.namelist()))
                self.assertEqual(out.read("OH-INF/thing/googletv.xml"), b"unchanged pairing schema")
                self.assertEqual(out.read("LICENSE"), b"upstream attribution")
                text = out.read("META-INF/MANIFEST.MF")
                self.assertIn(b"Bundle-Version: 5.2.3.thermite1\r\n", text)
                self.assertIn(b"Import-Package: unchanged\r\n", text)
                self.assertNotIn(b"obsolete", text)
            with self.assertRaises(patch.PatchError):
                patch.rebuild(original, {}, root / "bad.jar")
            with self.assertRaises(patch.PatchError):
                patch.verified(first, "0" * 64)
            link = root / "link.jar"
            link.symlink_to(first)
            with self.assertRaises(patch.PatchError):
                patch.verified(link, one["sha256"])

    def test_patch_remains_small_and_changes_no_remote_protocol_or_keystore_contract(self):
        manager = (patch.ROOT / "googletv-lifecycle.patch").read_text()
        handler = (patch.ROOT / "handler-ownership.patch").read_text()
        self.assertIn("sockets.open(attempt", manager)
        self.assertIn("sockets.stop()", manager)
        self.assertIn("generation != retryGeneration", manager)
        self.assertIn("previous.dispose()", manager)
        self.assertIn("this.googletvConnectionManager = new", handler)
        additions = "\n".join(line for line in manager.splitlines() if line.startswith("+"))
        self.assertNotIn("saveKeyStore", additions)
        self.assertNotIn("generateNewKeyPair", additions)
        self.assertNotIn("setCaCert", additions)
        self.assertLess(len(manager.splitlines()), 550)

    def test_failed_java_gate_publishes_no_artifact(self):
        scratch = AREA.parents[1] / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            runtime, fixtures, output = root / "runtime", root / "fixtures", root / "output"
            (runtime / "system").mkdir(parents=True)
            (runtime / "system" / "dependency.jar").write_bytes(b"fake compile dependency")
            fixtures.mkdir()
            (fixtures / "Test.java").write_text("test fixture")
            bundle = root / "original.jar"
            with zipfile.ZipFile(bundle, "w") as out:
                out.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\nBundle-Version: 5.2.3\nX: y\n\n")
            paths = {"bundle": bundle}
            for key in ("manager", "handler", "compiler", "annotations"):
                paths[key] = root / key
                paths[key].write_text("fake verified input")
            lock = {"upstream": "5.2.3", "patched": "5.2.3.thermite1", "assets": {
                key: {"sha256": patch.sha(path), "name": path.name} for key, path in paths.items()}}
            dependencies = {"system/dependency.jar": patch.sha(runtime / "system/dependency.jar")}
            lock["runtime_system_sha256"] = hashlib.sha256(
                json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            lock["runtime_system_jars"] = 1

            def run(args, **kwargs):
                if "LifecycleTest" in " ".join(args):
                    raise patch.PatchError("synthetic lifecycle gate failure")
                if "-d" in args:
                    dest = Path(args[args.index("-d") + 1])
                    if dest.name == "classes":
                        for name in patch.EXPECTED_CLASSES:
                            path = dest / name
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(b"fake compiled class")
                return ""

            with mocked.object(patch, "pins", return_value=lock), mocked.object(patch, "_run", side_effect=run):
                with self.assertRaisesRegex(patch.PatchError, "lifecycle gate failure"):
                    patch.build(paths, java=Path("/fake/java"), runtime=runtime, work=root / "work",
                                output=output, test_sources=fixtures)
            self.assertEqual(list(output.iterdir()), [])
            (runtime / "system" / "dependency.jar").write_bytes(b"changed dependency")
            with mocked.object(patch, "pins", return_value=lock):
                with self.assertRaisesRegex(patch.PatchError, "runtime dependency tree differs"):
                    patch.build(paths, java=Path("/fake/java"), runtime=runtime, work=root / "work",
                                output=output, test_sources=fixtures)


if __name__ == "__main__":
    unittest.main()
