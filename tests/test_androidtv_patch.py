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
        self.assertEqual(set(pins["retire_when"]), {"connection_lifecycle"})
        self.assertEqual(patch.OVERRIDE, 'mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/'
                                         '5.2.3.thermite1;range="[5.2.3,5.2.4)"')

    def test_patch_targets_exactly_the_pinned_openhab_release(self):
        """Fails loudly when versions.json moves: retire the repair if upstream is fixed, else rebase it."""
        try:
            patch.require_pinned()
        except patch.PatchError as error:
            self.fail(str(error))
        with tempfile.TemporaryDirectory() as folder:
            moved = Path(folder) / "versions.json"
            moved.write_text(json.dumps({"openhab": {"version": "5.2.4"}}))
            with self.assertRaises(patch.PatchError) as caught:
                patch.require_pinned(moved)
            for text in ("openHAB 5.2.4", "exactly the 5.2.3", "retire the repair", "REPAIRS", "rebase it onto 5.2.4",
                         "zero fuzz", "--write-classes", "is not installed without it"):
                self.assertIn(text, str(caught.exception))
            with self.assertRaises(patch.PatchError):
                patch.build({}, java=Path("/fake/java"), runtime=Path(folder), work=Path(folder) / "work",
                            output=Path(folder) / "output", versions=moved)

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
            lock = {**patch.pins(), "assets": {
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


def scratch() -> tempfile.TemporaryDirectory:
    folder = AREA.parents[1] / ".scratch"
    folder.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=folder)


class Committed(unittest.TestCase):
    """classes/ is what an install puts into the bundle: the build holds it
    to the source, and inputs.json pins it."""

    def test_the_committed_classes_are_the_pinned_ones_and_the_reviewed_set(self):
        pins = patch.pins()
        self.assertEqual(set(pins["classes"]), patch.EXPECTED_CLASSES)
        self.assertEqual({name: hashlib.sha256(data).hexdigest() for name, data in patch.committed().items()},
                         pins["classes"])
        found = {str(path.relative_to(patch.ROOT / "classes")) for path in (patch.ROOT / "classes").rglob("*")
                 if path.is_file()}
        self.assertEqual(found, patch.EXPECTED_CLASSES)
        self.assertRegex(pins["content_sha256"], r"^[0-9a-f]{64}$")

    def test_a_build_that_is_not_the_pinned_one_is_refused_and_recorded_only_when_asked(self):
        with scratch() as folder:
            root = Path(folder)
            bundle = root / "upstream.jar"
            with zipfile.ZipFile(bundle, "w") as out:
                out.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\nBundle-Version: 5.2.3\nX: y\n\n")
            other = {name: b"another build" for name in patch.EXPECTED_CLASSES}
            patch.rebuild(bundle, other, root / "other.jar")
            with self.assertRaisesRegex(patch.PatchError, "--write-classes"):
                patch.reviewed(other, root / "other.jar")
            same = patch.committed()
            patch.rebuild(bundle, same, root / "same.jar")
            # The same classes in another bundle are another content.
            with self.assertRaisesRegex(patch.PatchError, "not the one inputs.json pins"):
                patch.reviewed(same, root / "same.jar")
            lock, classes = root / "inputs.json", root / "package"
            lock.write_text(patch.LOCK.read_text())
            with mocked.object(patch, "LOCK", lock), mocked.object(patch, "ROOT", classes):
                patch.reviewed(other, root / "other.jar", write=True)
                patch.reviewed(other, root / "other.jar")
                self.assertEqual(patch.committed(), other)
            self.assertNotEqual(json.loads(lock.read_text())["classes"], patch.pins()["classes"])

    def test_the_assembled_bundle_is_the_rebuilt_one(self):
        with scratch() as folder:
            root = Path(folder)
            bundle = root / "upstream.jar"
            with zipfile.ZipFile(bundle, "w") as out:
                out.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\nBundle-Version: 5.2.3\nX: y\n\n")
                out.writestr("LICENSE", b"upstream attribution")
            classes = patch.committed()
            patch.rebuild(bundle, classes, root / "rebuilt.jar")
            with open(bundle, "rb") as stream:
                self.assertEqual(patch.assemble(stream, classes), (root / "rebuilt.jar").read_bytes())


if __name__ == "__main__":
    unittest.main()
