"""openHAB's add-on files are pinned, verified and installed from userdata's own repository."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_addons  # noqa: E402

VERSIONS = json.loads((AREA / "versions.json").read_text(encoding="utf-8"))
PIN = oh_addons.pin
HTTP = "org/example/bundles/binding.http/1.0/binding.http-1.0.jar"
JS = "org/example/osgiify/js/2.0/js-2.0.jar"
# The distribution's own settings, in its layout (userdata/etc/org.ops4j.pax.url.mvn.cfg).
MAVEN = """\
# comment
org.ops4j.pax.url.mvn.localRepository=${openhab.userdata}/tmp/mvn
org.ops4j.pax.url.mvn.useFallbackRepositories=false
org.ops4j.pax.url.mvn.defaultRepositories=\\
    ${karaf.home.uri}${karaf.default.repository}@id=system.repository@snapshots, \\
    ${karaf.data.uri}tmp/kar@id=kar.repository@multi@snapshots, \\
    ${karaf.base.uri}${karaf.default.repository}@id=child.system.repository@snapshots
org.ops4j.pax.url.mvn.repositories= \\
\thttps://openhab.jfrog.io/openhab/libs-release@id=openhab
org.ops4j.pax.url.mvn.timeout = 5000
"""
ENTRY = "file:///openhab/userdata/thermite/maven/@id=thermite"
FEATURES = """<?xml version="1.0" encoding="UTF-8"?>
<features xmlns="http://karaf.apache.org/xmlns/features/v1.6.0" name="{name}">
{body}
</features>
"""


def pin(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def versions(files: dict[str, bytes], requires: dict[str, list[str]]) -> dict:
    return {"addons": {"repository": "https://repository.example/release", "requires": requires,
                       "files": {path: pin(data) for path, data in files.items()}}}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, path: Path, data: bytes | str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path


class Needs(Scratch):
    def setUp(self):
        super().setUp()
        system = self.root / "runtime/system"
        self.write(system / "org/example/addons/1.0/addons-1.0-features.xml", FEATURES.format(name="addons", body="""
    <feature name="openhab-binding-http" version="1.0">
        <feature>openhab-runtime-base</feature>
        <feature>openhab-transformation-map</feature>
        <bundle start-level="80">mvn:org.example.bundles/binding.http/1.0</bundle>
    </feature>
    <feature name="openhab-transformation-map" version="1.0">
        <feature>openhab-runtime-base</feature>
        <bundle dependency="true">mvn:org.example.osgiify/js/2.0</bundle>
        <bundle>mvn:org.example.bundles/carried/1.0</bundle>
    </feature>"""))
        self.write(system / "org/example/distro/1.0/distro-1.0-features.xml", FEATURES.format(name="distro", body="""
    <feature name="openhab-runtime-base" version="1.0">
        <feature>openhab-runtime-base</feature>
        <feature>a-feature-of-another-repository</feature>
        <bundle>mvn:org.example.core/base/1.0</bundle>
    </feature>"""))
        for carried in ("org/example/core/base/1.0/base-1.0.jar", "org/example/bundles/carried/1.0/carried-1.0.jar"):
            self.write(system / carried, b"carried")

    def test_an_addon_needs_its_features_bundles_that_the_distribution_does_not_carry(self):
        runtime = self.root / "runtime"
        self.assertEqual(oh_addons.needs(runtime, "binding-http"), sorted([HTTP, JS]))
        self.assertEqual(oh_addons.needs(runtime, "transformation-map"), [JS])

    def test_an_addon_the_distribution_does_not_define_is_refused(self):
        with self.assertRaisesRegex(oh_addons.AddonError, "defines no add-on binding-nosuch"):
            oh_addons.needs(self.root / "runtime", "binding-nosuch")

    def test_a_bundle_location_names_its_place_in_a_maven_repository(self):
        for uri, path in (("mvn:org.example.bundles/binding.http/1.0", HTTP),
                          ("mvn:a.b/c/1.0/xml/features", "a/b/c/1.0/c-1.0-features.xml"),
                          ("wrap:mvn:a.b/c/1.0$Bundle-SymbolicName=c", "a/b/c/1.0/c-1.0.jar")):
            self.assertEqual(oh_addons.artifact_path(uri), path)
        with self.assertRaises(oh_addons.AddonError):
            oh_addons.artifact_path("file:/tmp/some.jar")

    def test_the_pinned_lists_are_what_the_pinned_distribution_resolves(self):
        # Only where thermite sandbox fetch has extracted the distribution.
        runtime = AREA / "build/sandbox" / f"openhab-{VERSIONS['openhab']['version']}" / "runtime"
        if not (runtime / "system").is_dir():
            self.skipTest("the pinned distribution is not fetched here")
        known = oh_addons.features(runtime / "system")
        for uid, paths in oh_addons.entry(VERSIONS)["requires"].items():
            with self.subTest(addon=uid):
                self.assertEqual(oh_addons.needs(runtime, uid, known), paths)


class Pins(Scratch):
    def test_the_shipped_pins_are_well_formed_and_cover_what_they_require(self):
        addons = oh_addons.entry(VERSIONS)
        self.assertEqual({path for paths in addons["requires"].values() for path in paths}, set(addons["files"]))
        # A runtime that asked for the example house's four add-ons fetched exactly these.
        example = oh_addons.pinned(VERSIONS, ["automation-jsscripting", "binding-http", "misc-metrics",
                                              "transformation-jsonpath"])
        self.assertEqual(len(example), 20)
        self.assertEqual(sum("org/openhab/osgiify/org.graalvm." in path for path in example), 16)

    def test_every_addon_a_house_can_ask_for_is_pinned(self):
        asked = set()
        for path in sorted((AREA / "lib").rglob("*.py")):
            asked |= {f"{kind}-{name}" for kind, name in
                      re.findall(r'\bAddon\(\s*"([a-z]+)",\s*"([a-z0-9]+)"', path.read_text(encoding="utf-8"))}
        self.assertIn("binding-http", asked)
        missing = sorted(asked - set(oh_addons.entry(VERSIONS)["requires"]))
        self.assertEqual(missing, [], "an integration asks for an add-on whose files versions.json does not pin: run "
                         f"thermite sandbox fetch --pin-addons {' '.join(missing)}")

    def test_an_unpinned_addon_is_refused_with_the_command_that_pins_it(self):
        pins = versions({HTTP: b"http"}, {"binding-http": [HTTP]})
        self.assertEqual(oh_addons.pinned(pins, ["binding-http", "binding-http"]), {HTTP: pin(b"http")})
        self.assertEqual(oh_addons.pinned({}, []), {})
        with self.assertRaisesRegex(oh_addons.AddonError, "thermite sandbox fetch --pin-addons binding-sonos"):
            oh_addons.pinned(pins, ["binding-http", "binding-sonos"])
        with self.assertRaisesRegex(oh_addons.AddonError, "copy the entry from Thermite's versions.json"):
            oh_addons.pinned({"openhab": {}}, ["binding-http"])

    def test_a_malformed_entry_is_refused(self):
        good = versions({HTTP: b"http"}, {"binding-http": [HTTP]})["addons"]
        for change in ({"repository": "http://repository.example"}, {"requires": {"binding-http": [JS]}},
                       {"files": {HTTP: {"sha256": "A" * 64, "size": 4}}}, {"files": {HTTP: {"sha256": "0" * 64}}},
                       {"files": {"../escape.jar": pin(b"x")}, "requires": {}},
                       {"files": {"/absolute/a.jar": pin(b"x")}, "requires": {}}):
            with self.subTest(change=change), self.assertRaises(oh_addons.AddonError):
                oh_addons.entry({"addons": {**good, **change}})

    def test_the_recorded_digest_follows_the_files_and_their_pins(self):
        one = {HTTP: pin(b"http")}
        self.assertEqual(oh_addons.digest(one), oh_addons.digest(dict(one)))
        self.assertNotEqual(oh_addons.digest(one), oh_addons.digest({HTTP: pin(b"other")}))
        self.assertNotEqual(oh_addons.digest(one), oh_addons.digest({**one, JS: pin(b"js")}))


class Cache(Scratch):
    def setUp(self):
        super().setUp()
        self.served = {HTTP: b"the http binding", JS: b"the script engine"}
        self.pins = versions(self.served, {"binding-http": [HTTP], "automation-js": [JS]})
        self.asked = []

    def download(self, url, sha256, directory, log=print):
        self.asked.append(url)
        path = url.removeprefix("https://repository.example/release/")
        data = self.served[path]
        if hashlib.sha256(data).hexdigest() != sha256:
            raise RuntimeError("does not match its pin")
        return self.write(Path(directory) / Path(path).name, data)

    def test_only_a_file_the_cache_lacks_or_holds_wrong_is_fetched(self):
        store = self.root / "store"
        self.write(store / HTTP, self.served[HTTP])
        self.write(store / JS, b"the script engine, damaged")
        files = oh_addons.ensure(self.pins, ["binding-http", "automation-js"], store, self.download)
        self.assertEqual(self.asked, ["https://repository.example/release/" + JS])
        self.assertEqual(set(files), {HTTP, JS})
        self.assertTrue(all(oh_addons.stored(store, path, files[path]) for path in files))
        self.asked.clear()
        oh_addons.ensure(self.pins, ["binding-http", "automation-js"], store, self.download)
        self.assertEqual(self.asked, [])

    def test_a_served_file_that_is_not_the_pinned_one_is_refused(self):
        self.served[HTTP] = b"another http binding"
        with self.assertRaisesRegex(RuntimeError, "does not match its pin"):
            oh_addons.ensure(self.pins, ["binding-http"], self.root / "store", self.download)


class Pinning(Scratch):
    """thermite sandbox fetch --pin-addons: what makes and refreshes the pins."""

    def setUp(self):
        super().setUp()
        system = self.root / "runtime/system"
        self.write(system / "org/example/addons/1.0/addons-1.0-features.xml", FEATURES.format(name="addons", body="""
    <feature name="openhab-binding-http" version="1.0">
        <bundle>mvn:org.example.bundles/binding.http/1.0</bundle>
        <bundle>mvn:org.example.osgiify/js/2.0</bundle>
    </feature>"""))
        self.served = {HTTP: b"the http binding", JS: b"the script engine"}
        self.published = {path: hashlib.sha256(data).hexdigest() for path, data in self.served.items()}
        self.asked = []

    def fetch(self, url, target):
        self.asked.append(url)
        path = url.removeprefix("https://repository.example/release/")
        if path.endswith(".sha256"):
            self.write(Path(target), self.published[path.removesuffix(".sha256")] + "\n")
        else:
            self.write(Path(target), self.served[path])

    def test_a_new_file_is_pinned_to_what_the_repository_serves_and_publishes(self):
        before = versions({HTTP: self.served[HTTP]}, {"binding-old": [HTTP]})
        self.write(self.root / "store" / HTTP, self.served[HTTP])
        made = oh_addons.pin(before, self.root / "runtime", ["binding-http"], self.root / "store", self.fetch,
                             log=lambda line: None)
        self.assertEqual(made["requires"], {"binding-http": sorted([HTTP, JS]), "binding-old": [HTTP]})
        self.assertEqual(made["files"], {HTTP: pin(self.served[HTTP]), JS: pin(self.served[JS])})
        # The file pinned already was neither fetched nor pinned anew.
        self.assertEqual(self.asked, [f"https://repository.example/release/{JS}",
                                      f"https://repository.example/release/{JS}.sha256"])
        oh_addons.entry({"addons": made})

    def test_a_file_that_is_not_what_the_repository_publishes_is_not_pinned(self):
        self.published[JS] = "0" * 64
        with self.assertRaisesRegex(oh_addons.AddonError, "is not the file its published .sha256 names"):
            oh_addons.pin({"addons": {"repository": "https://repository.example/release"}}, self.root / "runtime",
                          ["binding-http"], self.root / "store", self.fetch, log=lambda line: None)

    def test_a_repository_without_sha256_files_is_held_to_its_sha1_and_one_without_either_is_refused(self):
        def fetch(url, target):
            if url.endswith(".sha256"):
                raise OSError("HTTP Error 404: Not Found")
            if url.endswith(".sha1"):
                data = self.served[url.removeprefix("https://repository.example/release/").removesuffix(".sha1")]
                return self.write(Path(target), hashlib.sha1(data).hexdigest())
            self.fetch(url, target)
        start = {"addons": {"repository": "https://repository.example/release"}}
        made = oh_addons.pin(start, self.root / "runtime", ["binding-http"], self.root / "store", fetch,
                             log=lambda line: None)
        self.assertEqual(made["files"], {HTTP: pin(self.served[HTTP]), JS: pin(self.served[JS])})

        def bare(url, target):
            if url.endswith((".sha256", ".sha1")):
                raise OSError("HTTP Error 404: Not Found")
            self.fetch(url, target)
        with self.assertRaisesRegex(oh_addons.AddonError, "publishes no checksum"):
            oh_addons.pin(start, self.root / "runtime", ["binding-http"], self.root / "other", bare,
                          log=lambda line: None)

    def test_a_pinned_file_is_never_pinned_anew_to_what_is_served_now(self):
        before = versions({HTTP: b"the http binding, as pinned", JS: self.served[JS]}, {"binding-http": [HTTP, JS]})
        with self.assertRaisesRegex(oh_addons.AddonError, "is not the file versions.json pins"):
            oh_addons.pin(before, self.root / "runtime", ["binding-http"], self.root / "store", self.fetch,
                          log=lambda line: None)


class PinCommand(Pinning):
    """thermite sandbox fetch --pin-addons writes the entry beside the other pins."""

    def setUp(self):
        super().setUp()
        import oh_sandbox
        self.sandbox = oh_sandbox
        self.file = self.root / "versions.json"
        self.file.write_text(json.dumps({"openhab": {"version": "1.0"}, "java": {}, "_sources": {"openhab": "x"}},
                                        indent=2) + "\n")
        self.enterContext(mock.patch.object(oh_sandbox, "ADDONS", self.root / "store"))
        self.enterContext(mock.patch.object(oh_sandbox, "load_versions", side_effect=lambda path: {}))
        self.enterContext(mock.patch.object(oh_sandbox, "fetch", return_value=mock.Mock(dist=self.root)))
        self.enterContext(mock.patch.object(oh_sandbox.urllib.request, "urlopen", side_effect=self.open))
        self.enterContext(mock.patch.object(oh_addons, "pin", side_effect=self.pin))
        self.lines = []

    def pin(self, versions, runtime, uids, store, fetch, log):
        # The command's own fetch, over the repository this fixture serves.
        current = {**(versions.get("addons") or {}), "repository": "https://repository.example/release"}
        return PIN(dict(versions, addons=current), runtime, uids, store, fetch, log)

    def open(self, request, timeout):
        self.assertEqual(request.get_header("User-agent"), "homelab-openhab-sandbox")
        self.asked.append(request.full_url)
        path = request.full_url.removeprefix("https://repository.example/release/")
        data = (self.published[path.removesuffix(".sha256")].encode() if path.endswith(".sha256")
                else self.served[path])
        return contextlib.closing(io.BytesIO(data))

    def test_the_named_addon_is_pinned_before_the_notes_and_a_second_run_changes_nothing(self):
        made = self.sandbox.pin_addons(self.file, ["binding-http"], log=self.lines.append)
        written = json.loads(self.file.read_text())
        self.assertEqual(list(written), ["openhab", "java", "addons", "_sources"])
        self.assertEqual(written["addons"], made)
        self.assertEqual(made["files"], {HTTP: pin(self.served[HTTP]), JS: pin(self.served[JS])})
        self.assertEqual(len(self.asked), 4)
        self.assertIn(f"pins    wrote {self.file}: the 2 files of 1 add-ons", self.lines)
        before = self.file.read_bytes()
        self.asked.clear()
        # None named: every add-on pinned already, from the cache.
        self.sandbox.pin_addons(self.file, [], log=self.lines.append)
        self.assertEqual((self.asked, self.file.read_bytes()), ([], before))
        self.assertIn("pins    the 2 files of 1 add-ons are pinned as they are", self.lines)

    def test_a_refusal_writes_nothing(self):
        self.published[JS] = "0" * 64
        before = self.file.read_bytes()
        with self.assertRaisesRegex(self.sandbox.SandboxError, "is not the file its published .sha256 names"):
            self.sandbox.pin_addons(self.file, ["binding-http"], log=self.lines.append)
        self.assertEqual(self.file.read_bytes(), before)


class MavenSettings(unittest.TestCase):
    def test_the_repository_comes_first_and_no_remote_one_is_left(self):
        text = oh_addons.maven_config(MAVEN, ENTRY)
        properties = {key: value for _, _, key, value in oh_addons._properties(text)}
        self.assertEqual([part.strip() for part in properties[oh_addons.DEFAULT_REPOSITORIES].split(",")], [
            ENTRY, "${karaf.home.uri}${karaf.default.repository}@id=system.repository@snapshots",
            "${karaf.data.uri}tmp/kar@id=kar.repository@multi@snapshots",
            "${karaf.base.uri}${karaf.default.repository}@id=child.system.repository@snapshots"])
        self.assertEqual(properties[oh_addons.REMOTE_REPOSITORIES], "")
        self.assertNotIn("jfrog", text)
        for kept in ("# comment", "org.ops4j.pax.url.mvn.localRepository=${openhab.userdata}/tmp/mvn",
                     "org.ops4j.pax.url.mvn.timeout = 5000"):
            self.assertIn(kept, text.splitlines())
        self.assertEqual(oh_addons.maven_config(text, ENTRY), text, "settled settings are left byte for byte")

    def test_the_entries_the_binding_repairs_were_installed_with_by_hand_are_replaced(self):
        by_hand = MAVEN.replace("defaultRepositories=\\\n", "defaultRepositories=file:///openhab/userdata/thermite/maven/"
                                "@id=thermite.heos,file:///openhab/userdata/thermite/maven/@id=thermite.androidtv, \\\n")
        self.assertIn("@id=thermite.heos", by_hand)
        self.assertEqual(oh_addons.maven_config(by_hand, ENTRY), oh_addons.maven_config(MAVEN, ENTRY))

    def test_settings_openhab_wrote_back_without_a_remote_list_gain_an_empty_one(self):
        rewritten = "org.ops4j.pax.url.mvn.defaultRepositories = " + ENTRY + ", file:/system@id=system.repository\n"
        text = oh_addons.maven_config(rewritten, ENTRY)
        self.assertEqual(text.splitlines(), [rewritten.strip(), "org.ops4j.pax.url.mvn.repositories="])

    def test_settings_that_are_not_the_distributions_are_refused(self):
        for text in ("", MAVEN + "org.ops4j.pax.url.mvn.defaultRepositories=file:/elsewhere@id=other\n"):
            with self.subTest(text=text[-60:]), self.assertRaisesRegex(oh_addons.AddonError, "exactly once"):
                oh_addons.maven_config(text, ENTRY)
        with self.assertRaises(oh_addons.AddonError):
            oh_addons.repository_entry(Path("relative/maven"))


class Repository(Scratch):
    def setUp(self):
        super().setUp()
        self.userdata = self.root / "userdata"
        self.write(self.userdata / "etc" / oh_addons.MAVEN_CFG, MAVEN)
        self.files = {HTTP: b"the http binding", JS: b"the script engine"}
        self.store = self.root / "store"
        for path, data in self.files.items():
            self.write(self.store / path, data)
        self.pins = {path: pin(data) for path, data in self.files.items()}
        self.repository = self.userdata / oh_addons.REPOSITORY

    def tree(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.repository)): path.read_bytes()
                for path in sorted(self.repository.rglob("*")) if path.is_file()}

    def test_userdata_gains_exactly_the_pinned_files_and_the_resolver_reads_them_first(self):
        changed = oh_addons.provision(self.userdata, self.pins, self.store, mounted=oh_addons.MOUNTED)
        self.assertEqual(changed, [HTTP, JS, "etc/" + oh_addons.MAVEN_CFG])
        self.assertEqual(self.tree(), self.files)
        self.assertEqual((self.userdata / "etc" / oh_addons.MAVEN_CFG).read_text(), oh_addons.maven_config(MAVEN, ENTRY))
        for path in (self.repository, (self.repository / HTTP).parent, self.repository / HTTP):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual(oh_addons.provision(self.userdata, self.pins, self.store, mounted=oh_addons.MOUNTED), [])

    def test_a_sandbox_names_the_repository_where_its_own_userdata_is(self):
        oh_addons.provision(self.userdata, self.pins, self.store)
        self.assertIn((self.repository.as_uri() + "/@id=thermite"), (self.userdata / "etc" / oh_addons.MAVEN_CFG).read_text())

    def test_a_cached_file_that_is_not_the_pinned_one_is_never_installed(self):
        (self.store / JS).write_bytes(b"the script engine, tampered")
        with self.assertRaisesRegex(oh_addons.AddonError, "is not the size versions.json pins"):
            oh_addons.provision(self.userdata, self.pins, self.store)
        (self.store / JS).write_bytes(b"THE SCRIPT ENGINE")
        with self.assertRaisesRegex(oh_addons.AddonError, "sha256 differs"):
            oh_addons.provision(self.userdata, self.pins, self.store)
        self.assertNotIn(JS, self.tree())
        self.assertEqual((self.userdata / "etc" / oh_addons.MAVEN_CFG).read_text(), MAVEN,
                         "the resolver is pointed at the repository only once it is whole")

    def test_what_is_no_longer_pinned_leaves_the_repository(self):
        stale = "org/example/bundles/binding.http/0.9/binding.http-0.9.jar"
        self.write(self.repository / stale, b"an earlier release")
        self.write(self.repository / HTTP, b"changed in place")
        oh_addons.provision(self.userdata, self.pins, self.store)
        self.assertEqual(self.tree(), self.files)
        self.assertFalse((self.repository / stale).parent.exists())

    def test_a_link_planted_in_the_repository_is_removed_not_followed(self):
        outside = self.write(self.root / "outside/secret.jar", b"not thermite's")
        (self.repository / "org/example").mkdir(parents=True)
        (self.repository / "org/example/bundles").symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            oh_addons.provision(self.userdata, self.pins, self.store)
        self.assertEqual(outside.read_bytes(), b"not thermite's")
        self.assertEqual(sorted(path.name for path in outside.parent.iterdir()), ["secret.jar"])
        (self.repository / "org/example/bundles").unlink()
        (self.repository / "org/linked").symlink_to(outside.parent, target_is_directory=True)
        (self.repository / "org/file-link.jar").symlink_to(outside)
        oh_addons.provision(self.userdata, self.pins, self.store)
        self.assertEqual(self.tree(), self.files)
        self.assertEqual(outside.read_bytes(), b"not thermite's")
        self.assertFalse((self.repository / "org/linked").is_symlink())

    def test_the_helper_refuses_with_words_and_changes_nothing(self):
        versions_file = self.write(self.root / "versions.json", json.dumps(
            versions(self.files, {"binding-http": [HTTP]})))
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            oh_addons.main([str(self.userdata), "--versions", str(versions_file), "--store", str(self.store),
                            "--uid", str(os.getuid()), "--gid", str(os.getgid()), "binding-sonos"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("could not install the pinned add-on files", errors.getvalue())
        self.assertIn("pins no files for binding-sonos", errors.getvalue())
        self.assertFalse(self.repository.exists())


if __name__ == "__main__":
    unittest.main()
