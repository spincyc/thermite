"""An install transaction fetches the pinned add-on files before anything
stops, installs them before anything starts, and records them."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_addons  # noqa: E402
import oh_deploy as deploy  # noqa: E402
import oh_public  # noqa: E402
from oh_install import changeover, steps  # noqa: E402
from oh_install.model import Command, Task  # noqa: E402

HTTP = "org/example/bundles/binding.http/1.0/binding.http-1.0.jar"
JS = "org/example/osgiify/js/2.0/js-2.0.jar"
FILES = {HTTP: b"the http binding", JS: b"the script engine"}
ADDONS_CFG = "remote = false\nautomation = jsscripting\nbinding = http\n"


def pins(files=FILES) -> dict:
    return {"repository": "https://repository.example/release",
            "requires": {"binding-http": [HTTP], "automation-jsscripting": [JS]},
            "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                      for path, data in files.items()}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.site = Path(self.enterContext(tempfile.TemporaryDirectory())) / "site"
        (self.site / "conf/services").mkdir(parents=True)
        (self.site / "conf/services/addons.cfg").write_text(ADDONS_CFG)
        self.versions = {**json.loads((AREA / "versions.json").read_text()), "addons": pins()}
        self.context = steps.Context(self.site, self.versions, oh_public.demo_host(), "c" * 40, "d" * 64, "example",
                                     "192.0.2.5")
        self.store = self.site / "build/sandbox/downloads/addons"
        self.system = mock.Mock()
        self.fetched = []

    def download(self, url, sha256, directory, log=print):
        self.fetched.append(url)
        data = FILES[url.removeprefix("https://repository.example/release/")]
        if hashlib.sha256(data).hexdigest() != sha256:
            raise steps.oh_addons.AddonError(f"{url} does not match its pin")
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / url.rsplit("/", 1)[1]).write_bytes(data)

    def said(self) -> str:
        return "\n".join(str(call.args[0]) for call in self.system.say.call_args_list)


class Fetched(Fixture):
    def test_the_conf_names_the_addons_and_the_boot_checks_cache_holds_their_files(self):
        self.assertEqual(steps.addon_uids(self.context), ["automation-jsscripting", "binding-http"])
        self.assertEqual(steps.addon_store(self.context), self.store)
        prepared = {"conf/services/addons.cfg": deploy.Blob(b"binding = http\n")}
        import dataclasses
        snapshot = dataclasses.replace(self.context, prepared=prepared, application={"digest": "a" * 64})
        self.assertEqual(steps.addon_uids(snapshot), ["binding-http"])

    def test_missing_files_are_fetched_and_verified_while_thermite_runs(self):
        step = steps.AddonFiles(self.context, download=self.download)
        check = step.check(self.system)
        self.assertFalse(check.done)
        self.assertIn("2 of the 2 pinned add-on files", check.detail)
        (task,) = step.actions(self.system)
        self.assertIsInstance(task, Task)
        self.assertEqual(task.perform(self.system), 0)
        self.assertEqual(sorted(self.fetched), sorted("https://repository.example/release/" + path for path in FILES))
        self.assertTrue(step.check(self.system).done)
        self.assertEqual((self.store / HTTP).read_bytes(), FILES[HTTP])
        # A cached file of the right size and other content is fetched anew.
        (self.store / HTTP).write_bytes(FILES[HTTP][::-1])
        self.fetched.clear()
        check = step.check(self.system)
        self.assertFalse(check.done)
        self.assertIn("1 of the 2 pinned add-on files", check.detail)
        self.assertEqual(step.actions(self.system)[0].perform(self.system), 0)
        self.assertEqual(self.fetched, ["https://repository.example/release/" + HTTP])
        self.assertTrue(step.check(self.system).done)

    def test_a_file_that_cannot_be_fetched_refuses_before_anything_stops(self):
        def offline(url, sha256, directory, log=print):
            raise OSError("Network is unreachable")
        step = steps.AddonFiles(self.context, download=offline)
        self.assertEqual(step.actions(self.system)[0].perform(self.system), 1)
        self.assertIn("could not be fetched and verified: Network is unreachable. Nothing was changed.", self.said())
        self.assertIn("copy build/sandbox/downloads/addons from a site that has them", self.said())
        self.assertFalse(step.check(self.system).done)

    def test_a_served_file_that_is_not_the_pinned_one_refuses(self):
        self.versions["addons"] = pins({**FILES, JS: b"the script engine, as pinned"})
        step = steps.AddonFiles(self.context, download=self.download)
        self.assertEqual(step.actions(self.system)[0].perform(self.system), 1)
        self.assertIn("does not match its pin", self.said())

    def test_an_addon_versions_json_does_not_pin_is_a_problem_no_action_fixes(self):
        (self.site / "conf/services/addons.cfg").write_text(ADDONS_CFG + "binding = http,sonos\n")
        check = steps.AddonFiles(self.context).check(self.system)
        self.assertFalse(check.done)
        self.assertIn("thermite sandbox fetch --pin-addons binding-sonos", check.problem)
        # Such a version is never installed, so it has no pin to record.
        self.assertEqual(steps.addon_pins(self.context), {"addons": "unpinned"})

    def test_a_site_whose_versions_json_has_no_addons_entry_is_told_to_copy_it(self):
        del self.versions["addons"]
        self.assertIn("copy the entry from Thermite's versions.json", steps.AddonFiles(self.context).check(self.system).problem)


class Installed(Fixture):
    def test_the_repository_is_installed_after_the_offline_preparation_and_before_any_start(self):
        with mock.patch.object(steps.ownership, "inspect", return_value=steps.ownership.Owned()):
            prepared = [type(step) for step in changeover.preparation(self.context)]
            switched = [type(step) for step in changeover.switching(self.context, None)]
        self.assertLess(prepared.index(steps.AddonFiles), prepared.index(steps.BootTest))
        self.assertEqual(switched.index(steps.AddonRepository), switched.index(steps.NativePreparation) + 1)
        for later in (steps.Deploy, steps.Services, steps.StartTarget, steps.NativeReady):
            self.assertLess(switched.index(steps.AddonRepository), switched.index(later), later.__name__)

    def test_root_installs_from_the_cache_what_this_versions_json_pins(self):
        step = steps.AddonRepository(self.context)
        self.assertTrue(step.always)
        (command,) = step.actions(self.system)
        self.assertIsInstance(command, Command)
        self.assertTrue(command.root)
        from oh_paths import PROGRAM
        self.assertEqual(command.argv, (
            "/usr/bin/python3", "-I", "-B", str(PROGRAM / "lib/oh_addons.py"), str(self.context.layout.userdata),
            "--versions", str(self.site / "versions.json"), "--store", str(self.store),
            "--uid", str(self.context.host["uid"]), "--gid", str(self.context.host["gid"]),
            "automation-jsscripting", "binding-http"))
        # Inside userdata, which the transaction sets aside and puts back whole.
        self.assertEqual(step.desired(self.system), [])
        self.assertIn(self.context.layout.userdata, [item.path for item in
                                                     steps.NativePreparation(self.context).desired(self.system)])

    def test_the_command_root_runs_parses_and_installs_what_was_fetched(self):
        steps.AddonFiles(self.context, download=self.download).actions(self.system)[0].perform(self.system)
        (self.site / "versions.json").write_text(json.dumps(self.versions))
        userdata = self.site / "userdata"
        (userdata / "etc").mkdir(parents=True)
        (userdata / "etc" / oh_addons.MAVEN_CFG).write_text(
            "org.ops4j.pax.url.mvn.defaultRepositories=file:/system@id=system.repository\n")
        argv = list(steps.AddonRepository(self.context).actions(self.system)[0].argv[4:])
        argv[0] = str(userdata)
        import os
        argv[argv.index("--uid") + 1], argv[argv.index("--gid") + 1] = str(os.getuid()), str(os.getgid())
        with mock.patch("builtins.print"):
            self.assertEqual(oh_addons.main(argv), 0)
        self.assertEqual((userdata / oh_addons.REPOSITORY / JS).read_bytes(), FILES[JS])
        self.assertIn("file:///openhab/userdata/thermite/maven/@id=thermite",
                      (userdata / "etc" / oh_addons.MAVEN_CFG).read_text())

    def test_the_installed_manifest_records_the_addon_files_among_the_pins(self):
        with mock.patch.object(changeover.manifest, "pins", return_value={"openhab": "sha256:" + "e" * 64}), \
                mock.patch.object(changeover.manifest, "images", return_value=[]):
            recorded = changeover.release(self.context)["pins"]
            self.assertEqual(recorded, {"openhab": "sha256:" + "e" * 64,
                                        "addons": oh_addons.digest(pins()["files"])})
            # Another file behind the same add-ons is another version to install.
            self.versions["addons"] = pins({**FILES, JS: b"another script engine"})
            self.assertNotEqual(changeover.release(self.context)["pins"], recorded)
        from oh_install import engine
        engine._pins(recorded)


if __name__ == "__main__":
    unittest.main()
