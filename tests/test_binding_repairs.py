"""A house whose conf lists a repaired binding runs the repaired bundle:
installed with its add-on files, recorded, and checked after the start."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_addons  # noqa: E402
import oh_heos_patch  # noqa: E402
import oh_public  # noqa: E402
import oh_sandbox  # noqa: E402
from oh_install import changeover, engine, steps  # noqa: E402
from oh_install.model import Command  # noqa: E402

VERSIONS = json.loads((AREA / "versions.json").read_text(encoding="utf-8"))
STORE = AREA / "build/sandbox/downloads/addons"
MAVEN = "org.ops4j.pax.url.mvn.defaultRepositories=file:/system@id=system.repository\n"
MANIFEST = (b"Manifest-Version: 1.0\r\nBundle-SymbolicName: org.openhab.binding.heos\r\nBundle-Version: 5.2.3\r\n"
            b"Import-Package: unchanged\r\n\r\n")
HEOS, ANDROIDTV = (next(repair for repair in oh_addons.known_repairs() if repair.addon == addon)
                   for addon in ("binding-heos", "binding-androidtv"))


def jar(entries: dict[str, bytes], compression=zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as out:
        for name, data in entries.items():
            out.writestr(name, data)
    return buffer.getvalue()


class Declared(unittest.TestCase):
    def test_each_repair_names_its_addon_bundle_and_pinned_upstream_file(self):
        asked = set()
        for path in sorted((AREA / "lib").rglob("*.py")):
            asked |= {f"{kind}-{name}" for kind, name in
                      re.findall(r'\bAddon\(\s*"([a-z]+)",\s*"([a-z0-9]+)"', path.read_text(encoding="utf-8"))}
        self.assertEqual([repair.package for repair in oh_addons.known_repairs()], list(oh_addons.REPAIRS))
        for repair in oh_addons.known_repairs():
            with self.subTest(repair=repair.package):
                self.assertIn(repair.addon, asked, "no integration asks for the add-on this repairs")
                self.assertEqual(repair.upstream, VERSIONS["openhab"]["version"])
                # The upstream bundle is one of the add-on's own pinned files, and the same file.
                self.assertIn(repair.source, VERSIONS["addons"]["requires"][repair.addon])
                self.assertEqual(VERSIONS["addons"]["files"][repair.source]["sha256"], repair.source_sha256)
                self.assertEqual(repair.path, f"org/openhab/addons/bundles/{repair.bundle}/{repair.patched}/"
                                              f"{repair.bundle}-{repair.patched}.jar")
                self.assertEqual(repair.override, f'mvn:org.openhab.addons.bundles/{repair.bundle}/{repair.patched};'
                                                  'range="[5.2.3,5.2.4)"')
                self.assertTrue(repair.comment.startswith("thermite pinned "))
                self.assertRegex(repair.pin, engine.PIN)
                self.assertRegex(repair.content, r"^[0-9a-f]{64}$")
                self.assertTrue((AREA / "lib" / repair.package / "README.md").is_file())
                self.assertIn(f"lib/{repair.package}/README.md", oh_addons.UNREPAIRED)

    def test_a_public_package_carries_the_committed_classes_and_no_other_binary_of_theirs(self):
        approved = oh_addons.approved_artifacts()
        self.assertEqual(set(approved), {str(path.relative_to(AREA)) for package in oh_addons.REPAIRS
                                         for path in (AREA / "lib" / package / "classes").rglob("*") if path.is_file()})
        for name, sha256 in approved.items():
            self.assertEqual(hashlib.sha256((AREA / name).read_bytes()).hexdigest(), sha256, name)
        name = sorted(approved)[0]
        files = {name: (AREA / name).read_bytes()}
        self.assertEqual(oh_public.guard(files, AREA, facts=set()), [])
        self.assertEqual(oh_public.guard({name: files[name] + b"\xff"}, AREA, facts=set()),
                         [f"{name}: unapproved binary asset"])

    def test_only_the_addons_a_house_lists_are_repaired_and_only_for_the_pinned_openhab(self):
        self.assertEqual(oh_addons.repairs(VERSIONS, ["binding-http", "automation-jsscripting"]), [])
        self.assertEqual(oh_addons.repairs(VERSIONS, ["binding-heos", "binding-http"]), [HEOS])
        self.assertEqual(oh_addons.repairs(VERSIONS, ["binding-heos", "binding-androidtv"]), [ANDROIDTV, HEOS])
        moved = {"openhab": {"version": "5.2.4"}}
        self.assertEqual(oh_addons.repairs(moved, ["binding-http"]), [])
        for addon, words in (("binding-heos", "exactly the 5.2.3 HEOS binding"),
                             ("binding-androidtv", "exactly the 5.2.3 Android TV binding")):
            with self.subTest(addon=addon), self.assertRaises(oh_addons.AddonError) as refused:
                oh_addons.repairs(moved, [addon])
            for text in ("versions.json pins openHAB 5.2.4", words, "retire the repair", "rebase it onto 5.2.4"):
                self.assertIn(text, str(refused.exception))

    def test_the_bundle_an_install_assembles_is_the_reviewed_one(self):
        # Only where the pinned add-on files are fetched: the real bundles are megabytes.
        for repair in (HEOS, ANDROIDTV):
            with self.subTest(repair=repair.package):
                source = STORE / repair.source
                if not source.is_file():
                    self.skipTest("the pinned add-on files are not fetched here")
                data = repair.assemble(source.read_bytes())
                self.assertEqual(oh_addons.identity(io.BytesIO(data)),
                                 (repair.bundle, repair.patched, repair.marker, repair.content))


class Fixture(unittest.TestCase):
    """The HEOS repair's own names over a small stand-in bundle and classes."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        classes = {name: b"repaired " + name.encode() for name in oh_heos_patch.EXPECTED_CLASSES}
        for name, data in classes.items():
            (self.root / "package/classes" / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / "package/classes" / name).write_bytes(data)
        self.upstream = jar({"META-INF/MANIFEST.MF": MANIFEST, "OH-INF/thing/player.xml": b"thing schema",
                             **{name: b"upstream class" for name in oh_heos_patch.EXPECTED_CLASSES}})
        package = types.ModuleType("oh_fixture_patch")
        package.ROOT, package.assemble = self.root / "package", oh_heos_patch.assemble
        self.enterContext(mock.patch.dict(sys.modules, {"oh_fixture_patch": package}))
        repaired = oh_heos_patch.assemble(io.BytesIO(self.upstream), classes)
        self.repair = dataclasses.replace(
            HEOS, package="oh_fixture_patch", source_sha256=hashlib.sha256(self.upstream).hexdigest(),
            classes={name: hashlib.sha256(data).hexdigest() for name, data in classes.items()},
            content=oh_addons.identity(io.BytesIO(repaired))[3])
        self.repaired = repaired
        self.enterContext(mock.patch.object(oh_addons, "known_repairs", return_value=[ANDROIDTV, self.repair]))
        self.files = {self.repair.source: {"sha256": self.repair.source_sha256, "size": len(self.upstream)}}
        self.store = self.root / "store"
        (self.store / self.repair.source).parent.mkdir(parents=True)
        (self.store / self.repair.source).write_bytes(self.upstream)
        self.userdata = self.root / "userdata"
        (self.userdata / "etc").mkdir(parents=True)
        (self.userdata / "etc" / oh_addons.MAVEN_CFG).write_text(MAVEN)
        (self.userdata / "etc" / oh_addons.OVERRIDES).write_text("")
        (self.userdata / "jsondb").mkdir()
        (self.userdata / "jsondb/things.json").write_text("{}")
        self.overrides = self.userdata / "etc" / oh_addons.OVERRIDES

    def provision(self, wanted=None, **options):
        return oh_addons.provision(self.userdata, self.files, self.store,
                                   wanted=[self.repair] if wanted is None else wanted, **options)

    def cache(self, data: bytes, bundle: int = 212, generation: int = 0) -> Path:
        path = self.userdata / "cache/org.eclipse.osgi" / str(bundle) / str(generation) / "bundleFile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def as_pax_caches_it(self) -> bytes:
        """The repaired bundle as the framework keeps it: Pax adds a header
        to the manifest and packs the entries its own way."""
        with zipfile.ZipFile(io.BytesIO(self.repaired)) as source:
            entries = {name: source.read(name) for name in reversed(source.namelist())}
        manifest = entries["META-INF/MANIFEST.MF"].rstrip(b"\r\n")
        entries["META-INF/MANIFEST.MF"] = (manifest + b"\r\nBundle-UpdateLocation: mvn:org.openhab.addons.bundles/"
                                           b"org.openhab.binding.heos/5.2.3.thermite1\r\n\r\n")
        return jar(entries, zipfile.ZIP_DEFLATED)


    def assertEmptied(self):
        """Nothing of the old cache, and the directory whose absence makes
        the distribution's start script run its own priming server."""
        cache = self.userdata / "cache"
        self.assertEqual([path.name for path in cache.iterdir()], ["org.eclipse.osgi"])
        holder = cache / "org.eclipse.osgi"
        self.assertTrue(holder.is_dir() and not holder.is_symlink())
        self.assertEqual(list(holder.iterdir()), [])
        for directory in (cache, holder):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def helper(self, *arguments: str) -> tuple[int, str, str]:
        versions = self.root / "versions.json"
        versions.write_text(json.dumps({"openhab": VERSIONS["openhab"], "addons": {
            "repository": "https://repository.example/release", "requires": {"binding-heos": [self.repair.source]},
            "files": self.files}}))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = oh_addons.main([str(self.userdata), "--versions", str(versions), "--store", str(self.store),
                                       "--uid", str(os.getuid()), "--gid", str(os.getgid()), *arguments])
            except SystemExit as stopped:
                code = stopped.code
        return code, out.getvalue(), err.getvalue()

    def tree(self, top: Path) -> dict:
        """Every entry below `top`: its mode, and a file's bytes."""
        return {str(path.relative_to(top)): (stat.S_IMODE(path.lstat().st_mode),
                                             path.read_bytes() if path.is_file() else None)
                for path in sorted(top.rglob("*"))}


class Installed(Fixture):
    def test_the_repaired_bundle_and_its_override_are_installed_with_the_addon_files(self):
        changed = self.provision()
        self.assertEqual(changed, [self.repair.path, self.repair.source, "etc/" + oh_addons.MAVEN_CFG,
                                   "etc/" + oh_addons.OVERRIDES])
        repository = self.userdata / oh_addons.REPOSITORY
        self.assertEqual((repository / self.repair.source).read_bytes(), self.upstream)
        self.assertEqual((repository / self.repair.path).read_bytes(), self.repaired)
        with zipfile.ZipFile(repository / self.repair.path) as bundle:
            self.assertIn(b"Bundle-Version: 5.2.3.thermite1\r\n", bundle.read("META-INF/MANIFEST.MF"))
            self.assertIn(b"Thermite-Patch: heos-reconnect-v1\r\n", bundle.read("META-INF/MANIFEST.MF"))
            self.assertEqual(bundle.read("OH-INF/thing/player.xml"), b"thing schema")
            self.assertTrue(all(bundle.read(name).startswith(b"repaired ") for name in oh_heos_patch.EXPECTED_CLASSES))
        self.assertEqual(self.overrides.read_text(), "# thermite pinned HEOS reconnect repair\n" + HEOS.override + "\n")
        self.assertEqual(self.provision(), [], "a settled runtime is left as it is")

    def test_a_house_without_the_binding_loses_the_repair(self):
        self.provision()
        self.assertEqual(self.provision(wanted=[]), ["etc/" + oh_addons.OVERRIDES])
        self.assertEqual(self.overrides.read_text(), "")
        self.assertFalse((self.userdata / oh_addons.REPOSITORY / self.repair.path).exists())
        self.assertTrue((self.userdata / oh_addons.REPOSITORY / self.repair.source).is_file())

    def test_the_upstream_bundle_and_the_classes_must_be_the_pinned_ones(self):
        other = {self.repair.source: {"sha256": "0" * 64, "size": len(self.upstream)}}
        with self.assertRaisesRegex(oh_addons.AddonError, "does not pin .* as the bundle the HEOS reconnect repair"):
            oh_addons.provision(self.userdata, other, self.store, wanted=[self.repair])
        with self.assertRaisesRegex(oh_addons.AddonError, "does not pin"):
            oh_addons.provision(self.userdata, {}, self.store, wanted=[self.repair])
        with self.assertRaisesRegex(oh_addons.AddonError, "is not the bundle the HEOS reconnect repair was built from"):
            self.repair.assemble(self.upstream + b"\n")
        name = sorted(oh_heos_patch.EXPECTED_CLASSES)[0]
        (self.root / "package/classes" / name).write_bytes(b"another class")
        with self.assertRaisesRegex(oh_addons.AddonError, "is not the class its inputs.json pins"):
            self.provision()
        self.assertEqual(self.overrides.read_text(), "", "no override without its bundle")

    def test_a_rebuilt_bundle_that_is_not_the_reviewed_one_is_never_installed(self):
        altered = dataclasses.replace(self.repair, content="0" * 64)
        with self.assertRaisesRegex(oh_addons.AddonError, "is not the reviewed HEOS reconnect repair"):
            self.provision(wanted=[altered])
        self.assertFalse((self.userdata / oh_addons.REPOSITORY / self.repair.path).exists())


class Overrides(unittest.TestCase):
    KNOWN = [ANDROIDTV, HEOS]
    BY_HAND = ("# thermite pinned HEOS reconnect repair\n" + HEOS.override + "\n"
               "# thermite pinned Android TV lifecycle repair\n" + ANDROIDTV.override + "\n")

    def test_thermites_lines_are_exactly_the_wanted_repairs(self):
        both = oh_addons.overrides("", self.KNOWN, self.KNOWN)
        self.assertEqual(both, "# thermite pinned Android TV lifecycle repair\n" + ANDROIDTV.override + "\n"
                               "# thermite pinned HEOS reconnect repair\n" + HEOS.override + "\n")
        self.assertIs(oh_addons.overrides(both, self.KNOWN, self.KNOWN), both)
        self.assertEqual(oh_addons.overrides(both, [HEOS], self.KNOWN),
                         "# thermite pinned HEOS reconnect repair\n" + HEOS.override + "\n")
        self.assertEqual(oh_addons.overrides(both, [], self.KNOWN), "")
        self.assertEqual(oh_addons.overrides("", [], self.KNOWN), "")

    def test_the_lines_installed_by_hand_become_the_same_lines(self):
        self.assertEqual(oh_addons.overrides(self.BY_HAND, self.KNOWN, self.KNOWN),
                         oh_addons.overrides("", self.KNOWN, self.KNOWN))

    def test_other_lines_stay_and_a_retired_repairs_lines_go(self):
        text = ('# the owner\'s own\nmvn:org.example/other/1.0.1;range="[1.0,1.1)"\n'
                "# thermite pinned Example repair\n"
                'mvn:org.openhab.addons.bundles/org.openhab.binding.example/5.2.3.thermite2;range="[5.2.3,5.2.4)"\n')
        self.assertEqual(oh_addons.overrides(text, [HEOS], self.KNOWN),
                         '# the owner\'s own\nmvn:org.example/other/1.0.1;range="[1.0,1.1)"\n'
                         "# thermite pinned HEOS reconnect repair\n" + HEOS.override + "\n")

    def test_another_override_of_a_repaired_binding_is_refused(self):
        for line in ("mvn:org.openhab.addons.bundles/org.openhab.binding.heos/5.2.3.custom\n",
                     "file:/openhab/addons/org.openhab.binding.androidtv-custom.jar\n"):
            with self.subTest(line=line), self.assertRaisesRegex(oh_addons.AddonError, "that is not Thermite's"):
                oh_addons.overrides(line, [HEOS], self.KNOWN)


class OsgiCache(Fixture):
    """Karaf applies an override only when it provisions a feature."""

    def test_a_cache_that_holds_the_upstream_binding_is_cleared(self):
        self.cache(self.upstream)
        (self.userdata / "cache/org.eclipse.osgi/framework.info.7").write_bytes(b"the framework's bundle database")
        (self.userdata / "cache/other").mkdir()
        (self.userdata / "tmp").mkdir()
        changed = self.provision()
        self.assertIn("cache (cleared, so that the next start installs every bundle anew: it holds "
                      "org.openhab.binding.heos 5.2.3, not the HEOS reconnect repair (5.2.3.thermite1))", changed)
        self.assertEmptied()
        self.assertTrue((self.userdata / "jsondb/things.json").is_file())
        self.assertTrue((self.userdata / "tmp").is_dir())

    def test_a_cache_that_holds_the_repaired_binding_stays(self):
        cached = self.cache(self.as_pax_caches_it())
        self.cache(jar({"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\nBundle-SymbolicName: org.example.other;"
                        b"singleton:=true\r\nBundle-Version: 1.0\r\n\r\n"}), bundle=7)
        self.assertNotIn("cache", " ".join(self.provision()))
        self.assertTrue(cached.is_file())

    def test_each_bundle_that_cannot_stay_is_named(self):
        self.cache(self.upstream)
        self.cache(jar({"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\nBundle-SymbolicName: org.openhab.binding."
                        b"androidtv\r\nBundle-Version: 5.2.3.thermite1\r\nThermite-Patch: googletv-lifecycle-v1\r\n"
                        b"X: y\r\n\r\n"}), bundle=213)
        self.assertIn("cache (cleared, so that the next start installs every bundle anew: it holds a repaired "
                      "org.openhab.binding.androidtv 5.2.3.thermite1 that this version does not install; it holds "
                      "org.openhab.binding.heos 5.2.3, not the HEOS reconnect repair (5.2.3.thermite1))",
                      self.provision())
        self.assertEmptied()

    def test_the_newest_generation_of_a_bundle_is_the_one_that_counts(self):
        self.cache(self.upstream, generation=0)
        self.cache(self.as_pax_caches_it(), generation=1)
        self.provision()
        self.assertTrue((self.userdata / "cache").is_dir())
        self.cache(self.upstream, generation=10)
        self.provision()
        self.assertEmptied()

    def test_a_repaired_binding_of_other_content_or_one_no_longer_wanted_is_cleared(self):
        with zipfile.ZipFile(io.BytesIO(self.repaired)) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        entries["OH-INF/thing/player.xml"] = b"another build"
        self.cache(jar(entries))
        self.assertIn("cleared", " ".join(self.provision()))
        self.cache(self.as_pax_caches_it())
        changed = self.provision(wanted=[])
        self.assertIn("it holds a repaired org.openhab.binding.heos 5.2.3.thermite1 that this version does not "
                      "install", " ".join(changed))
        self.assertEmptied()

    def test_nothing_planted_in_the_cache_is_followed_or_read_as_a_bundle(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "kept").write_bytes(self.upstream)
        holder = self.userdata / "cache/org.eclipse.osgi"
        (holder / "3/0").mkdir(parents=True)
        (holder / "3/0/bundleFile").symlink_to(outside / "kept")
        (holder / "4").symlink_to(outside, target_is_directory=True)
        (outside / "0").mkdir()
        (outside / "0/bundleFile").write_bytes(self.upstream)
        (holder / "6").mkdir()
        (holder / "6/0").symlink_to(outside / "0", target_is_directory=True)
        (holder / "5/0").mkdir(parents=True)
        (holder / "5/0/bundleFile").write_bytes(b"not a bundle")
        self.assertNotIn("cache", " ".join(self.provision()))
        self.cache(self.upstream)
        self.provision()
        self.assertEmptied()
        self.assertEqual((outside / "kept").read_bytes(), self.upstream)


class Unprimed(Fixture):
    """An installation never starts the runtime on userdata that has no
    OSGi cache: openHAB's start script would first build one with a server
    of its own, which can leave add-ons installed and never started. Its
    step runs the helper while openHAB is stopped, after openHAB's update,
    which removes the cache."""

    def install(self) -> str:
        with mock.patch.object(oh_addons, "MOUNTED", self.userdata):
            code, out, err = self.helper("binding-heos")
        self.assertEqual((code, err), (0, ""))
        return out

    def test_userdata_with_state_and_no_cache_gets_an_empty_one(self):
        self.assertIn("add-on repository: cache (made empty: userdata had none, and openHAB's start script must "
                      "not build it with a server of its own)\n", self.install())
        self.assertEmptied()
        self.assertEqual((self.userdata / "jsondb/things.json").read_text(), "{}")
        self.assertNotIn("cache", self.install())
        self.assertEmptied()

    def test_a_cache_directory_without_its_bundles_is_completed(self):
        (self.userdata / "cache").mkdir()
        (self.userdata / "cache/kept").write_text("another tenant of the directory")
        self.assertIn("cache (made empty", self.install())
        self.assertEqual(sorted(path.name for path in (self.userdata / "cache").iterdir()), ["kept", "org.eclipse.osgi"])
        self.assertEqual(list((self.userdata / "cache/org.eclipse.osgi").iterdir()), [])

    def test_a_fresh_install_that_preparation_primed_keeps_its_cache(self):
        # Primed without add-ons: the framework's own bundles and database, no binding.
        self.cache(jar({"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\r\nBundle-SymbolicName: org.apache.karaf."
                        b"features.core\r\nBundle-Version: 4.4.8\r\n\r\n"}), bundle=19)
        (self.userdata / "cache/org.eclipse.osgi/framework.info.3").write_bytes(b"the framework's bundle database")
        before = self.tree(self.userdata / "cache")
        self.assertNotIn("cache", self.install())
        self.assertEqual(self.tree(self.userdata / "cache"), before)

    def test_an_ordinary_upgrade_keeps_its_cache(self):
        self.install()
        (self.userdata / "cache/org.eclipse.osgi").rmdir()
        self.cache(self.as_pax_caches_it())
        (self.userdata / "cache/org.eclipse.osgi/framework.info.7").write_bytes(b"the framework's bundle database")
        before = self.tree(self.userdata / "cache")
        self.assertEqual(self.install(), "add-on repository: as pinned\n")
        self.assertEqual(self.tree(self.userdata / "cache"), before)

    def test_a_test_runtime_builds_its_own_cache_and_gets_none(self):
        self.assertNotIn("cache", " ".join(self.provision()))
        self.assertFalse((self.userdata / "cache").exists())

    def test_a_cache_that_is_not_userdatas_own_directory_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        for planted in ("cache", "cache/org.eclipse.osgi"):
            with self.subTest(planted=planted):
                link = self.userdata / planted
                link.parent.mkdir(exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                try:
                    with mock.patch.object(oh_addons, "MOUNTED", self.userdata):
                        code, _, err = self.helper("binding-heos")
                finally:
                    link.unlink()
                self.assertEqual(code, 2)
                self.assertIn("could not install the pinned add-on files: ", err)
                self.assertIn(repr(planted.rsplit("/", 1)[-1]), err)
                self.assertEqual(list(outside.iterdir()), [])


class Verified(Fixture):
    def test_a_runtime_that_installed_the_repaired_binding_passes(self):
        self.cache(self.as_pax_caches_it())
        self.assertEqual(oh_addons.verify(self.userdata, [self.repair]), [])
        self.assertEqual(oh_addons.verify(self.userdata, []), [])

    def test_an_unrepaired_or_missing_binding_is_named(self):
        self.assertEqual(oh_addons.verify(self.userdata, [self.repair]),
                         ["openHAB has not installed org.openhab.binding.heos, which binding-heos needs"])
        self.cache(self.upstream)
        self.assertEqual(oh_addons.verify(self.userdata, [self.repair]),
                         ["openHAB runs org.openhab.binding.heos 5.2.3, not the HEOS reconnect repair "
                          "(5.2.3.thermite1)"])
        with zipfile.ZipFile(io.BytesIO(self.repaired)) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        entries[sorted(oh_heos_patch.EXPECTED_CLASSES)[0]] = b"another class"
        self.cache(jar(entries))
        self.assertEqual(oh_addons.verify(self.userdata, [self.repair]),
                         ["openHAB runs a org.openhab.binding.heos 5.2.3.thermite1 whose content is not the reviewed "
                          "HEOS reconnect repair"])

    def test_the_helper_installs_then_fails_verification_with_what_it_means(self):
        with mock.patch.object(oh_addons, "MOUNTED", self.userdata):
            code, out, _ = self.helper("binding-heos")
        self.assertEqual(code, 0)
        self.assertIn("add-on repository: " + self.repair.path, out)
        self.cache(self.upstream)
        before = sorted(str(path) for path in self.userdata.rglob("*"))
        code, out, err = self.helper("--verify", "binding-heos")
        self.assertEqual(code, 1)
        self.assertIn("openHAB runs org.openhab.binding.heos 5.2.3, not the HEOS reconnect repair", err)
        self.assertIn("what ran before is put back", err)
        self.assertIn("Nothing in the site is wrong", err)
        self.assertEqual(sorted(str(path) for path in self.userdata.rglob("*")), before, "verification only reads")
        self.cache(self.as_pax_caches_it())
        code, out, err = self.helper("--verify", "binding-heos")
        self.assertEqual((code, out, err), (0, "binding repairs: org.openhab.binding.heos 5.2.3.thermite1\n", ""))
        self.assertEqual(self.helper("--verify")[:2], (0, "binding repairs: none needed\n"))


class Transaction(unittest.TestCase):
    """The install steps of a house with both repaired bindings."""

    def setUp(self):
        self.site = Path(self.enterContext(tempfile.TemporaryDirectory())) / "site"
        (self.site / "conf/services").mkdir(parents=True)
        self.addons = self.site / "conf/services/addons.cfg"
        self.addons.write_text("remote = false\nbinding = http,heos,androidtv\n")
        self.versions = json.loads((AREA / "versions.json").read_text())
        self.context = steps.Context(self.site, self.versions, oh_public.demo_host(), "c" * 40, "d" * 64, "example",
                                     "192.0.2.5")
        self.system = mock.Mock()
        self.system.stat.return_value = None

    def test_the_manifest_records_each_repair_by_name(self):
        with mock.patch.object(changeover.manifest, "pins", return_value={}), \
                mock.patch.object(changeover.manifest, "images", return_value=[]):
            recorded = changeover.release(self.context)["pins"]
            self.assertEqual({name: value for name, value in recorded.items() if name != "addons"},
                             {"repair_binding_androidtv": "sha256:" + ANDROIDTV.content,
                              "repair_binding_heos": "sha256:" + HEOS.content})
            engine._pins(recorded)
            self.addons.write_text("remote = false\nbinding = http\n")
            plain = changeover.release(self.context)["pins"]
            self.assertEqual(set(plain), {"addons"})
            self.addons.write_text("remote = false\nbinding = http,heos\n")
            self.assertEqual(set(changeover.release(self.context)["pins"]), {"addons", "repair_binding_heos"})
        # The files' own digest follows the repairs too: another build is another version.
        files = oh_addons.pinned(self.versions, ["binding-heos"])
        self.assertNotEqual(oh_addons.digest(files, [HEOS]), oh_addons.digest(files))
        self.assertNotEqual(oh_addons.digest(files, [HEOS]),
                            oh_addons.digest(files, [dataclasses.replace(HEOS, content="0" * 64)]))

    def test_the_repairs_are_verified_after_the_start_and_before_the_final_health(self):
        with mock.patch.object(steps.ownership, "inspect", return_value=steps.ownership.Owned()):
            verified = changeover.verification(self.context)
            kinds = [type(step) for step in verified]
            self.assertEqual(kinds.index(steps.BindingRepairs), kinds.index(steps.Health) - 1)
            self.assertLess(kinds.index(changeover.Authorize), kinds.index(steps.BindingRepairs))
            step = verified[kinds.index(steps.BindingRepairs)]
            self.assertTrue(step.always)
            self.assertEqual(step.check(self.system).detail,
                             "openHAB installed the Android TV connection repair (org.openhab.binding.androidtv "
                             "5.2.3.thermite1) and the HEOS reconnect repair (org.openhab.binding.heos 5.2.3.thermite1)")
            (command,) = step.actions(self.system)
            install = steps.AddonRepository(self.context).actions(self.system)[0]
            self.assertIsInstance(command, Command)
            self.assertTrue(command.root and command.show_output)
            self.assertEqual([word for word in command.argv if word != "--verify"], list(install.argv))
            self.assertIn("--verify", command.argv)
            self.assertNotIn("--verify", install.argv)
            self.assertIn("and the HEOS reconnect repair", steps.AddonRepository(self.context).check(self.system).detail)
            self.addons.write_text("remote = false\nbinding = http\n")
            self.assertNotIn(steps.BindingRepairs, [type(step) for step in changeover.verification(self.context)])

    def test_an_openhab_the_repair_was_not_written_for_is_refused_before_anything_stops(self):
        self.versions["openhab"] = {**self.versions["openhab"], "version": "5.2.4"}
        check = steps.AddonFiles(self.context).check(self.system)
        self.assertFalse(check.done)
        self.assertIn("repairs exactly the 5.2.3", check.problem)
        self.assertIn("retire the repair", check.problem)
        self.assertEqual(steps.addon_pins(self.context), {"addons": "unpinned"})
        self.assertNotIn("repair", steps.AddonRepository(self.context).check(self.system).detail)
        self.assertNotIn(steps.BindingRepairs, [type(step) for step in changeover.verification(self.context)])
        with mock.patch.object(steps.ownership, "inspect", return_value=steps.ownership.Owned()):
            prepared = [type(step) for step in changeover.preparation(self.context)]
        self.assertIn(steps.AddonFiles, prepared)

    def test_a_test_runtime_is_warmed_anew_when_a_repair_changes(self):
        rt = mock.Mock(openhab_version="5.2.3", addons=self.versions["addons"])
        rt.java_home.name = "jre"
        plain = oh_sandbox.template_key(rt, "binding = http\n")
        repaired = oh_sandbox.template_key(rt, "binding = http,heos\n")
        with mock.patch.object(oh_addons, "known_repairs",
                               return_value=[dataclasses.replace(HEOS, content="0" * 64)]):
            self.assertNotEqual(oh_sandbox.template_key(rt, "binding = http,heos\n"), repaired)
            self.assertEqual(oh_sandbox.template_key(rt, "binding = http\n"), plain)


if __name__ == "__main__":
    unittest.main()
