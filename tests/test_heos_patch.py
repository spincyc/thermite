"""HEOS patch pins, version guard, packaging and owner activation inputs.

The Java reconnect gate and its negative control run in the explicit build
(python3 -m oh_heos_patch build --test-sources tests/heos_patch), not here.
"""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch as mocked
import subprocess
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_heos_patch as patch

MAVEN = ('# keep comments\norg.ops4j.pax.url.mvn.localRepository=${openhab.userdata}/tmp/mvn\n'
         'org.ops4j.pax.url.mvn.defaultRepositories=\\\n'
         '    file:///openhab/userdata/thermite/maven/@id=thermite.androidtv, \\\n'
         '    ${karaf.home.uri}system@id=system.repository\n'
         '# unrelated\norg.ops4j.pax.url.mvn.repositories=https://example.invalid/maven\n')
ANDROIDTV = ('# thermite pinned Android TV lifecycle repair\n'
             'mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1;range="[5.2.3,5.2.4)"\n')


def scratch() -> tempfile.TemporaryDirectory:
    folder = AREA.parents[1] / ".scratch"
    folder.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=folder)


def versions(root: Path, version: str) -> Path:
    path = root / "versions.json"
    path.write_text(json.dumps({"openhab": {"version": version}}))
    return path


class VersionGuard(unittest.TestCase):
    def test_patch_targets_exactly_the_pinned_openhab_release(self):
        """Fails loudly when versions.json moves: retire the patch if upstream is fixed, else rebase it."""
        try:
            patch.require_pinned()
        except patch.PatchError as error:
            self.fail(str(error))

    def test_a_moved_pin_refuses_with_retire_and_rebase_instructions(self):
        with scratch() as folder:
            moved = versions(Path(folder), "5.2.4")
            with self.assertRaises(patch.PatchError) as caught:
                patch.require_pinned(moved)
            message = str(caught.exception)
            for text in ("openHAB 5.2.4", "exactly the 5.2.3", "retire the patch", "roll back", "#21781",
                         "rebase it onto 5.2.4", "inputs.json", "zero fuzz", "still fails on the unmodified"):
                self.assertIn(text, message)
            with self.assertRaises(patch.PatchError):
                patch.build({}, java=Path("/fake/java"), runtime=Path(folder), work=Path(folder) / "work",
                            output=Path(folder) / "output", versions=moved)
            with self.assertRaises(patch.PatchError):
                patch.prepare(Path(folder), maven=Path(folder) / "m", output=Path(folder) / "o", versions=moved)

    def test_exact_pins_and_override_scope(self):
        pins = patch.pins()
        self.assertEqual(pins["upstream"], "5.2.3")
        self.assertEqual(pins["patched"], "5.2.3.thermite1")
        self.assertEqual(set(pins["assets"]), {"bundle", "compiler", "annotations", "system", "events", "command"})
        self.assertEqual(set(pins["retire_when"]), {"connection_leak", "quiet_event_stream"})
        for key, entry in pins["assets"].items():
            self.assertTrue(entry["url"].startswith("https://"))
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            if key in patch.PATCHES:
                self.assertIn("/openhab-addons/5.2.3/bundles/org.openhab.binding.heos/", entry["url"])
        self.assertIn("/org.openhab.binding.heos/5.2.3/", pins["assets"]["bundle"]["url"])
        self.assertIn("5.2.3.thermite1", patch.ARTIFACT)
        self.assertEqual(patch.OVERRIDE,
                         'mvn:org.openhab.addons.bundles/org.openhab.binding.heos/5.2.3.thermite1;range="[5.2.3,5.2.4)"')


class Merges(unittest.TestCase):
    def test_override_coexists_with_other_thermite_overrides(self):
        merged = patch.override_text(ANDROIDTV)
        self.assertTrue(merged.startswith(ANDROIDTV))
        self.assertIn(patch.OVERRIDE, merged)
        self.assertEqual(patch.override_text(merged), merged)
        self.assertEqual(patch.override_text(""), "# thermite pinned HEOS reconnect repair\n" + patch.OVERRIDE + "\n")
        with self.assertRaises(patch.PatchError):
            patch.override_text("mvn:org.openhab.addons.bundles/org.openhab.binding.heos/5.2.3.custom\n")

    def test_private_repository_is_heos_own_and_preserves_everything_else(self):
        result = patch.maven_config_text(MAVEN)
        self.assertIn("defaultRepositories=file:///openhab/userdata/thermite/maven/@id=thermite.heos,", result)
        self.assertIn("file:///openhab/userdata/thermite/maven/@id=thermite.androidtv", result)
        self.assertIn("${karaf.home.uri}system@id=system.repository", result)
        self.assertIn("localRepository=${openhab.userdata}/tmp/mvn\n", result)
        self.assertTrue(result.endswith("# unrelated\norg.ops4j.pax.url.mvn.repositories=https://example.invalid/maven\n"))
        self.assertEqual(patch.maven_config_text(result), result)
        for bad in ("# no effective property\n", MAVEN + "\n" + MAVEN,
                    result.replace("file:///openhab/userdata/thermite/maven/@id=thermite.heos",
                                   "file:///other/maven/@id=thermite.heos")):
            with self.subTest(bad=bad), self.assertRaises(patch.PatchError):
                patch.maven_config_text(bad)
        with self.assertRaises(patch.PatchError):
            patch.repository_entry(Path("relative"))


class Packaging(unittest.TestCase):
    def test_rebuild_is_deterministic_and_preserves_unrelated_resources(self):
        with scratch() as folder:
            root = Path(folder)
            original = root / "vendor.jar"
            manifest = b"Manifest-Version: 1.0\r\nBundle-Version: 5.2.3\r\nImport-Package: unchanged\r\n\r\n"
            with zipfile.ZipFile(original, "w") as out:
                out.writestr("META-INF/MANIFEST.MF", manifest)
                out.writestr("OH-INF/thing/HeosSystem.xml", b"unchanged thing schema")
                out.writestr("LICENSE", b"upstream attribution")
            classes = {name: b"synthetic class" for name in patch.EXPECTED_CLASSES}
            first, second = root / "first.jar", root / "second.jar"
            one = patch.rebuild(original, classes, first)
            two = patch.rebuild(original, dict(reversed(list(classes.items()))), second)
            self.assertEqual(one["sha256"], two["sha256"])
            with zipfile.ZipFile(first) as out:
                self.assertEqual(out.read("OH-INF/thing/HeosSystem.xml"), b"unchanged thing schema")
                self.assertEqual(out.read("LICENSE"), b"upstream attribution")
                text = out.read("META-INF/MANIFEST.MF")
                self.assertIn(b"Bundle-Version: 5.2.3.thermite1\r\n", text)
                self.assertIn(b"Thermite-Patch: heos-reconnect-v1\r\n", text)
                self.assertIn(b"Import-Package: unchanged\r\n", text)
            with self.assertRaises(patch.PatchError):
                patch.rebuild(original, {}, root / "bad.jar")
            link = root / "link.jar"
            link.symlink_to(first)
            with self.assertRaises(patch.PatchError):
                patch.verified(link, one["sha256"])

    def test_patches_apply_cleanly_and_stay_narrow(self):
        """Each patch applies with zero fuzz to the pinned upstream text it was written against."""
        system = (patch.ROOT / "HeosSystem.patch").read_text()
        events = (patch.ROOT / "HeosEventController.patch").read_text()
        command = (patch.ROOT / "HeosSendCommand.patch").read_text()
        self.assertIn("+        eventController.resetLastEventTime();", system)
        self.assertIn("+        if (executor != null && !executor.isShutdown()) {", system)
        self.assertIn("+            closeConnection();\n+            throw e;", system)
        self.assertIn("+    void resetLastEventTime() {", events)
        self.assertIn("+        if (!client.isConnected()) {", command)
        self.assertNotIn("LAST_EVENT_THRESHOLD =", system)
        for text in (system, events, command):
            self.assertLess(len(text.splitlines()), 80)
            self.assertNotIn("+import", text)
        with scratch() as folder:
            root = Path(folder)
            for name in patch.PATCHES.values():
                target = root / name.replace(".patch", ".java")
                lines = (patch.ROOT / name).read_text().splitlines(keepends=True)
                old = [line[1:] for line in lines[2:] if line[:1] in (" ", "-")]
                new = [line[1:] for line in lines[2:] if line[:1] in (" ", "+")]
                target.write_text("".join(old))
                subprocess.run(["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "--quiet", str(target),
                                str(patch.ROOT / name)], check=True, capture_output=True)
                self.assertEqual(target.read_text(), "".join(new))

    def test_failed_java_gate_or_passing_control_publishes_no_artifact(self):
        with scratch() as folder:
            root = Path(folder)
            runtime, fixtures, output = root / "runtime", root / "fixtures", root / "output"
            (runtime / "system").mkdir(parents=True)
            (runtime / "system" / "dependency.jar").write_bytes(b"fake compile dependency")
            fixtures.mkdir()
            (fixtures / "ReconnectTest.java").write_text("test fixture")
            bundle = root / "original.jar"
            with zipfile.ZipFile(bundle, "w") as out:
                out.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\nBundle-Version: 5.2.3\nX: y\n\n")
                for name in patch.EXPECTED_CLASSES:
                    out.writestr(name, b"upstream class")
            paths = {"bundle": bundle}
            for key in ("compiler", "annotations", *patch.PATCHES):
                paths[key] = root / key
                paths[key].write_text("fake verified input")
            lock = {**patch.pins(), "assets": {key: {"sha256": patch.sha(path), "name": path.name}
                                               for key, path in paths.items()}}
            dependencies = {"system/dependency.jar": patch.sha(runtime / "system/dependency.jar")}
            lock["runtime_system_sha256"] = hashlib.sha256(
                json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            lock["runtime_system_jars"] = 1

            def compile_or_test(args, **kwargs):
                if "-d" in args:
                    dest = Path(args[args.index("-d") + 1])
                    if dest.name == "classes":
                        for name in patch.EXPECTED_CLASSES:
                            path = dest / name
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(b"fake compiled class")
                return ""

            for gate, control in ((subprocess.CompletedProcess([], 1, "FAIL synthetic gate", ""), None),
                                  (subprocess.CompletedProcess([], 0, "PASS", ""),
                                   subprocess.CompletedProcess([], 0, "PASS", ""))):
                results = iter([gate, control])

                def execute(args, **kwargs):
                    if patch.TEST_MAIN in args:
                        return next(results)
                    compile_or_test(args)
                    return subprocess.CompletedProcess(args, 0, "", "")

                with self.subTest(gate=gate.stdout), mocked.object(patch, "pins", return_value=lock), \
                        mocked.object(patch, "_execute", side_effect=execute):
                    with self.assertRaises(patch.PatchError):
                        patch.build(paths, java=Path("/fake/java"), runtime=runtime, work=root / "work",
                                    output=output, test_sources=fixtures)
                    self.assertEqual(list(output.iterdir()), [])
            (runtime / "system" / "dependency.jar").write_bytes(b"changed dependency")
            with mocked.object(patch, "pins", return_value=lock):
                with self.assertRaisesRegex(patch.PatchError, "runtime dependency tree differs"):
                    patch.build(paths, java=Path("/fake/java"), runtime=runtime, work=root / "work",
                                output=output, test_sources=fixtures)


class Activation(unittest.TestCase):
    def staged(self, root: Path, *, gated: bool = True) -> Path:
        staged = root / "staged"
        staged.mkdir()
        bundle = root / "upstream.jar"
        with zipfile.ZipFile(bundle, "w") as out:
            out.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\nBundle-Version: 5.2.3\n\n")
            for name in patch.EXPECTED_CLASSES:
                out.writestr(name, b"upstream class")
        artifact = staged / patch.ARTIFACT
        built = patch.rebuild(bundle, {name: b"patched class" for name in patch.EXPECTED_CLASSES}, artifact)
        lock = patch.pins()
        receipt = {"upstream": "5.2.3", "patched": "5.2.3.thermite1",
                   "inputs": {"bundle": lock["assets"]["bundle"]["sha256"]}, "artifact": built,
                   "classes": patch.class_hashes(artifact), "upstream_classes": patch.class_hashes(bundle)}
        if gated:
            receipt.update(java_tests="PASS 16 reconnect assertions", negative_control={"exit": 1, "failures": ["FAIL x"]})
        (staged / "build.json").write_text(json.dumps(receipt))
        return staged

    def test_prepare_writes_reviewable_data_only_from_copies(self):
        with scratch() as folder:
            root = Path(folder)
            staged = self.staged(root)
            (root / "maven.cfg").write_text(MAVEN)
            (root / "overrides").write_text(ANDROIDTV)
            result = patch.prepare(staged, maven=root / "maven.cfg", overrides=root / "overrides", output=root / "out")
            out = root / "out"
            self.assertTrue(result["overrides_changed"] and result["maven_changed"])
            self.assertEqual(result["maven_path"], str(patch.MAVEN_PATH))
            self.assertEqual((out / "overrides.properties.before").read_text(), ANDROIDTV)
            self.assertEqual((out / "overrides.properties").read_text(), patch.override_text(ANDROIDTV))
            self.assertEqual((out / "org.ops4j.pax.url.mvn.cfg").read_text(), patch.maven_config_text(MAVEN))
            self.assertEqual(patch.sha(out / patch.ARTIFACT), patch.sha(staged / patch.ARTIFACT))
            apply_probe = (out / ("apply-" + patch.PROBE)).read_text()
            rollback_probe = (out / ("rollback-" + patch.PROBE)).read_text()
            self.assertNotIn("__THERMITE_CONFIG__", apply_probe)
            self.assertIn('"version": "5.2.3.thermite1"', apply_probe)
            self.assertIn('"version": "5.2.3"', rollback_probe)
            self.assertIn("refreshFeatures", apply_probe)
            for word in ("ItemRegistry", "sendCommand", "postUpdate", "password", "http"):
                self.assertNotIn(word, apply_probe)
            sums = dict(reversed(line.split("  ", 1)) for line in (out / "SHA256SUMS").read_text().splitlines())
            self.assertEqual(set(sums), {path.name for path in out.iterdir()} - {"SHA256SUMS"})
            for name, digest in sums.items():
                self.assertEqual(patch.sha(out / name), digest)
            with self.assertRaises(patch.PatchError):
                patch.prepare(staged, maven=root / "maven.cfg", output=out)

    def test_prepare_refuses_an_ungated_or_altered_build(self):
        with scratch() as folder:
            root = Path(folder)
            (root / "maven.cfg").write_text(MAVEN)
            staged = self.staged(root, gated=False)
            with self.assertRaisesRegex(patch.PatchError, "gated"):
                patch.prepare(staged, maven=root / "maven.cfg", output=root / "out")
            (staged / "build.json").unlink()
            (staged / patch.ARTIFACT).unlink()
            staged.rmdir()
            staged = self.staged(root)
            with zipfile.ZipFile(staged / patch.ARTIFACT, "a") as out:
                out.writestr("extra", b"tampered")
            with self.assertRaises(patch.PatchError):
                patch.prepare(staged, maven=root / "maven.cfg", output=root / "out")
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
