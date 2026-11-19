"""A page left open across an upgrade hears of it on its event stream and
replaces itself, once.

`thermite generate` gives the files of conf/html/remote one build, a hash of
them all, and writes it into every page (oh_features.remote.build_id). The
gateway's contract takes the build from the pages it serves, and every
stream's snapshot repeats it (oh_identity.hub; its test is
oh_identity/tests/test_event_hub.py). The one wrapper of the gateway's
streams compares the two (web/remote/access.js): no page asks for anything.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_inventory  # noqa: E402
import oh_render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import remote  # noqa: E402

NODE = shutil.which("node")
WEB = AREA / "web/remote"
OTHER = "0123456789abcdef"


def path(name: str) -> str:
    return f"{remote.TARGET}/{name}"


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = oh_inventory.load(AREA / "instance/devices.yaml")
        cls.conf = oh_render.build(House(cls.data))
        cls.files = cls.conf.render()
        cls.build = remote.build_id(cls.files)

    def test_every_generated_page_carries_the_one_build(self):
        pages = [name for name in remote.FILES if name.endswith(".html")]
        self.assertEqual(len(pages), 10)
        self.assertRegex(self.build, r"\A[0-9a-f]{16}\Z")
        for name in pages:
            page = self.files[path(name)].decode()
            self.assertEqual(remote.built(page), self.build, name)
            self.assertEqual(page.count("data-thermite-build="), 1, name)
            self.assertIn(f'<html data-thermite-hosted="true" data-thermite-build="{self.build}" ', page, name)
        # A source page, as a preview opens it, carries none.
        self.assertIsNone(remote.built((WEB / "index.html").read_text()))

    def test_the_build_names_every_file_generate_writes_for_the_pages(self):
        written = {name.removeprefix(remote.TARGET + "/") for name in self.files if name.startswith(remote.TARGET + "/")}
        self.assertEqual(written, set(remote.FILES))

    def test_changing_any_shipped_file_changes_the_build(self):
        seen = {self.build}
        for name in remote.FILES:
            value = self.files[path(name)]
            changed = {**self.files, path(name): value + (" " if isinstance(value, str) else b" ")}
            seen.add(remote.build_id(changed))
        self.assertEqual(len(seen), len(remote.FILES) + 1, "each file's change is a build of its own")
        # A file's name counts as its bytes do.
        swapped = {**self.files, path("remote.js"): self.files[path("pair.js")], path("pair.js"): self.files[path("remote.js")]}
        self.assertNotEqual(remote.build_id(swapped), self.build)

    def test_the_build_is_of_the_files_and_not_of_the_stamp(self):
        stamped = {name: value.replace(self.build.encode(), OTHER.encode()) if name.endswith(".html") else value
                   for name, value in self.files.items()}
        self.assertEqual(remote.built(stamped[path("home.html")]), OTHER)
        self.assertEqual(remote.build_id(stamped), self.build)
        self.assertEqual(remote.build_id(oh_render.render(House(self.data))), self.build, "the same house builds the same")

    def test_another_house_is_another_build(self):
        data = json.loads(json.dumps(self.data))
        room = next(iter(data["rooms"].values()))
        room["label"] = room.get("label", "") + " two"
        self.assertNotEqual(remote.build_id(oh_render.render(House(data))), self.build)

    def test_the_gateway_contract_names_the_build_of_the_pages_it_serves(self):
        house = House(self.data)
        self.assertEqual(oh_site_contract.compile(house, self.conf)["build"], self.build)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            # The pages the gateway serves say which build they are, whatever this checkout would generate.
            (root / "index.html").write_bytes(self.files[path("index.html")].replace(self.build.encode(), OTHER.encode()))
            self.assertEqual(oh_site_contract.compile(house, self.conf, asset_root=root)["build"], OTHER)
            # Pages from before builds carry none, and neither does their gateway's snapshot.
            (root / "index.html").write_text((WEB / "index.html").read_text())
            self.assertIsNone(oh_site_contract.compile(house, self.conf, asset_root=root)["build"])

    def test_no_page_compares_its_files_with_the_servers(self):
        """The check a page once made read each of its files from the cache
        and again from the server, every minute it was shown, and could not
        see a change: access.js reads everything without a cache."""
        asking = sorted(script.name for script in WEB.glob("*.js") if "force-cache" in script.read_text())
        self.assertEqual(asking, [])


@unittest.skipUnless(NODE, "needs node")
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A capped heap: a harness that ran away would end here, not in the host's memory.
        result = subprocess.run([NODE, "--max-old-space-size=256", str(AREA / "tests/build_identity_harness.js")],
                                capture_output=True, text=True, timeout=30)
        if result.returncode or not result.stdout:
            raise AssertionError(result.stderr or result.stdout or "the harness never finished")
        cls.proof = json.loads(result.stdout)

    def test_a_page_of_the_gateways_build_is_left_alone(self):
        self.assertEqual(self.proof["same"], [0, 0])
        self.assertEqual(self.proof["fresh"], [0, 0])

    def test_a_page_of_another_build_reloads_once_however_often_it_hears_so(self):
        self.assertEqual(self.proof["replaced"], [1, 0])
        self.assertEqual(self.proof["later"], 1)

    def test_a_page_the_reload_did_not_replace_says_so_once_and_never_reloads_again(self):
        reloads, notices = self.proof["stuck"]
        self.assertEqual(reloads, 0)
        self.assertEqual(notices, ["thermite was updated, but this page could not load the new version by itself. "
                                   "Close thermite and open it again."])
        self.assertEqual(self.proof["quiet"], [0, 0])

    def test_a_browser_without_storage_is_told_instead_of_reloaded(self):
        self.assertEqual(self.proof["unstored"], [0, 1])

    def test_a_preview_without_a_build_compares_nothing(self):
        self.assertEqual(self.proof["unbuilt"], [0, 0])


if __name__ == "__main__":
    unittest.main()
