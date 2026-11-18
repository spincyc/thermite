"""The sample house is files of its own, which a phone never downloads.

Everything a preview fakes (the house, its music, Home's alerts, pairing and
the demo account) is in web/remote/*-mock.js. A page names the ones it can
use in its head and navigation.js loads them only when the page is asked
for the sample (?backend=mock) or is opened from its sources; the script
each stands in for takes it when asked for a mock. tests/browser_smoke.py
sees the same in a real browser (`make check-browser`).
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_features import remote  # noqa: E402

WEB = AREA / "web/remote"
#: The script that makes a mock when asked for one -> the file that holds it.
MOCKS = {"backend.js": "backend-mock.js", "music-backend.js": "music-backend-mock.js", "alerts.js": "alerts-mock.js",
         "pair.js": "pair-mock.js", "access.js": "access-mock.js"}
PAGES = sorted(name for name in remote.FILES if name.endswith(".html"))

SCRIPT = r'''
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");
const read = (name) => fs.readFileSync(path.join("web/remote", name), "utf8");

// navigation.js in a page being read: what it writes into it.
function written(address, hosted, links, state) {
  const out = [];
  const document = { readyState: state || "loading", documentElement: { dataset: hosted ? { thermiteHosted: "true" } : {} },
    querySelectorAll: (selector) => (selector === 'link[rel="thermite-mock"]' ? links.map((href) => ({ getAttribute: () => href })) : []),
    write: (text) => out.push(text) };
  vm.runInContext(read("navigation.js"), vm.createContext({ document, location: new URL(address), URLSearchParams }));
  return out;
}

// A script alone, asked for its mock: what it says, and what it does once the mock's file has run.
async function asked(names, mock, ask) {
  const context = vm.createContext({ console, URL, URLSearchParams, Promise, setTimeout, clearTimeout, fetch: () => Promise.reject(new Error("offline")) });
  context.globalThis = context;
  names.forEach((name) => vm.runInContext(read(name), context, { filename: name }));
  const said = async () => { try { await vm.runInContext(ask, context); return "made"; } catch (error) { return error.message; } };
  const before = await said();
  vm.runInContext(read(mock), context, { filename: mock });
  return [before, await said()];
}

(async () => {
  const served = ["/static/remote/access-mock.js?v=0123456789abcdef", "/static/remote/backend-mock.js?v=fedcba9876543210"];
  const house = JSON.stringify({ rooms: [], devices: {}, sources: {} });
  process.stdout.write(JSON.stringify({
    phone: written("https://house.example/remotes/?room=example_west", true, served),
    asked: written("https://house.example/remotes/?backend=mock", true, served),
    preview: written("file:///checkout/web/remote/index.html", false, ["access-mock.js", "backend-mock.js"]),
    late: written("https://house.example/remotes/?backend=mock", true, served, "complete"),
    foreign: written("https://house.example/remotes/?backend=mock", true, ["https://elsewhere.example/backend-mock.js", "javascript:alert(1)", '"><script>'].concat(served.slice(0, 1))),
    missing: {
      "backend.js": await asked(["navigation.js", "backend.js"], "backend-mock.js", "RemoteBackend.create(" + house + ", { backend: 'mock' })"),
      "music-backend.js": await asked(["navigation.js", "music-backend.js"], "music-backend-mock.js", "MusicBackend.create({ backend: 'mock' })"),
      "alerts.js": await asked(["alerts.js"], "alerts-mock.js", "RemoteAlerts.sample(" + house + ", 'firing')"),
      "pair.js": await asked(["navigation.js", "pair.js"], "pair-mock.js", "PairingBackend.create({ backend: 'mock', location: null })"),
      "access.js": await asked(["navigation.js", "access.js"], "access-mock.js", "ThermiteAccess.create({ mock: true, base: 'https://house.example' }).ready()")
    }
  }));
})().catch((error) => { process.stderr.write(String(error.stack || error) + "\n"); process.exitCode = 1; });
'''


def tag(address: str) -> str:
    return f'<script defer src="{address}"></script>'


class Named(unittest.TestCase):
    def test_every_page_names_the_mock_of_each_script_it_loads_and_loads_none_itself(self):
        for page in PAGES:
            text = (WEB / page).read_text()
            scripts = re.findall(r'<script[^>]*\ssrc="([^"]+)"', text)
            named = re.findall(r'<link rel="thermite-mock" href="([^"]+)">', text)
            with self.subTest(page=page):
                self.assertEqual(sorted(named), sorted(MOCKS[name] for name in scripts if name in MOCKS))
                self.assertFalse([name for name in scripts if name.endswith("-mock.js")], "a phone would download it")
                # navigation.js reads the names as it runs: they come before it.
                self.assertLess(text.rindex('rel="thermite-mock"'), text.index('<script src="navigation.js">'))

    def test_each_mock_is_a_file_of_the_build_and_nothing_else_is_named_so(self):
        self.assertEqual({name for name in remote.FILES if name.endswith("-mock.js")}, set(MOCKS.values()))
        self.assertEqual({path.name for path in WEB.glob("*-mock.js")}, set(MOCKS.values()))

    def test_a_generated_page_names_each_mock_by_its_bytes(self):
        versions = {"access-mock.js": "0123456789abcdef"}
        page = remote.served_page((WEB / "security.html").read_text(), versions)
        self.assertIn('<link rel="thermite-mock" href="/static/remote/access-mock.js?v=0123456789abcdef">', page)


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's scripts")
class Loaded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([shutil.which("node"), "-e", SCRIPT], cwd=AREA, text=True, capture_output=True, timeout=60)
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout)
        cls.out = json.loads(run.stdout)

    def test_a_page_not_asked_for_the_sample_loads_none_of_it(self):
        self.assertEqual(self.out["phone"], [])

    def test_a_page_asked_for_the_sample_loads_what_it_names_in_order_before_its_own_scripts(self):
        # Written while the page is read, deferred: they run before the page's deferred scripts.
        self.assertEqual(self.out["asked"], [tag("/static/remote/access-mock.js?v=0123456789abcdef"),
                                             tag("/static/remote/backend-mock.js?v=fedcba9876543210")])
        # Once the page is read there is no writing into it.
        self.assertEqual(self.out["late"], [])

    def test_a_page_opened_from_its_sources_has_no_house_and_loads_the_sample(self):
        self.assertEqual(self.out["preview"], [tag("access-mock.js"), tag("backend-mock.js")])

    def test_only_this_sites_own_mock_files_are_written_into_a_page(self):
        self.assertEqual(self.out["foreign"], [tag("/static/remote/access-mock.js?v=0123456789abcdef")])

    def test_a_script_asked_for_a_mock_it_was_not_given_names_the_file_to_load(self):
        self.assertEqual(sorted(self.out["missing"]), sorted(MOCKS))
        for name, (before, after) in self.out["missing"].items():
            with self.subTest(script=name):
                self.assertIn("?backend=mock, which loads " + MOCKS[name], before)
                self.assertEqual(after, "made")


if __name__ == "__main__":
    unittest.main()
