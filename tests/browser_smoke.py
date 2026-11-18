"""Every page of the example house in a real browser, as a phone and as a desktop.

`make check-browser` runs this; `make check` does not, because it starts a
browser and takes about half a minute. tests/browser_smoke.cjs drives the
system's Chromium (no package is installed) through the ten generated pages
at 390x844 and 1440x900, served on loopback behind the gateway's
Content-Security-Policy header with the sample backend, and says what each
page did; the tests here say what it must not do. It opens each page once
more as a phone does, without ?backend=mock, to see that none of the
sample's files (*-mock.js) is downloaded then.

Without a Chromium on PATH (or named by THERMITE_CHROMIUM), or with a Node
older than 22, every test is skipped and says why. Where a skip would hide
that nothing was checked, as in a scheduled run, THERMITE_BROWSER_REQUIRED=1
makes it a failure.
"""
import json
import os
import re
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
import oh_routes  # noqa: E402
from oh_features import remote  # noqa: E402
from oh_identity.hardening import CONTENT_SECURITY_POLICY  # noqa: E402

#: The driver's own bound is 120 s; this one is for a driver that hangs.
TIMEOUT = 150
#: The driver's exit status for "no browser to drive".
NO_BROWSER = 77
PAGES = sorted(asset for _, _, asset in oh_routes.PAGES)
SIZES = ("phone", "desktop")


class BrowserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            cls.unavailable("node is not on PATH")
        files = oh_render.build(oh_inventory.load(AREA / "instance/devices.yaml")).render()
        with tempfile.TemporaryDirectory() as work:
            site = Path(work) / "remote"
            site.mkdir()
            for name, content in files.items():
                if name.startswith(remote.TARGET + "/"):
                    (site / name.removeprefix(remote.TARGET + "/")).write_bytes(
                        content.encode() if isinstance(content, str) else content)
            run = subprocess.run([node, str(AREA / "tests/browser_smoke.cjs"), str(site), CONTENT_SECURITY_POLICY],
                                 text=True, capture_output=True, timeout=TIMEOUT, env={**os.environ, "TMPDIR": work})
        if run.returncode == NO_BROWSER:
            cls.unavailable(run.stderr.strip())
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout or "the browser driver said nothing")
        cls.pages = json.loads(run.stdout)["pages"]

    @classmethod
    def unavailable(cls, reason):
        if os.environ.get("THERMITE_BROWSER_REQUIRED") == "1":
            raise AssertionError("the browser check could not run: " + reason)
        raise unittest.SkipTest(reason)

    def each(self, key, empty):
        """Every page at every size must have left this part of its report empty."""
        for page in PAGES:
            for size in SIZES:
                with self.subTest(page=page, size=size):
                    self.assertEqual(self.pages[page][size][key], empty)

    def test_every_page_drew_its_frame_and_its_words_at_both_sizes(self):
        self.assertEqual(sorted(self.pages), PAGES)
        for page in PAGES:
            for size in SIZES:
                with self.subTest(page=page, size=size):
                    self.assertTrue(self.pages[page][size]["frame"])

    def test_no_page_breaks_the_gateways_content_security_policy(self):
        self.each("csp", [])

    def test_no_page_throws_or_logs_an_error(self):
        self.each("errors", [])

    def test_no_page_scrolls_sideways(self):
        self.each("sideways", 0)

    def test_what_scrolls_sideways_inside_a_page_says_where_more_is(self):
        self.each("wide", [])

    def test_on_a_phone_every_control_is_at_least_44_px_wide_and_tall(self):
        for page in PAGES:
            with self.subTest(page=page):
                self.assertEqual(self.pages[page]["phone"]["small"], [])

    def test_what_a_page_opens_on_a_phone_is_held_to_the_same(self):
        # Remotes' room information, a sheet over the page: its switch, its buttons.
        self.assertEqual(sorted(self.pages["index.html"]["phone"]["states"]), ["info"])
        for page in PAGES:
            for name, seen in self.pages[page]["phone"]["states"].items():
                with self.subTest(page=page, state=name):
                    self.assertTrue(seen["opened"])
                    self.assertEqual((seen["small"], seen["wide"], seen["sideways"]), ([], [], 0))

    def test_nothing_in_sight_is_painted_under_the_bar(self):
        # Remotes answered the driver's key with a toast: once it opened under the bar.
        for size in SIZES:
            self.assertTrue(self.pages["index.html"][size]["toast"], size)
        self.each("under", [])

    def test_the_sample_house_is_downloaded_only_by_a_page_asked_for_it(self):
        for page in PAGES:
            named = sorted(re.findall(r'<link rel="thermite-mock" href="([^"]+)">', (AREA / "web/remote" / page).read_text()))
            with self.subTest(page=page):
                self.assertTrue(named, "every page has at least the demo account")
                for size in SIZES:
                    self.assertEqual(self.pages[page][size]["mocks"], named, size)
                # As a phone opens it, without ?backend=mock: none of them.
                self.assertEqual(self.pages[page]["plain"]["mocks"], [])


if __name__ == "__main__":
    unittest.main()
