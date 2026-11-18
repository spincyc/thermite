"""Every web page opens the same installed-app frame, and only the frame keeps the top safe-area inset."""
import re
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_routes  # noqa: E402

REMOTE = AREA / "web/remote"

# An installed iPhone app draws every page under a translucent status bar;
# shell.css pads its bar or sidebar by the inset, so no page needs to.
INSTALLED_APP_HEAD = (
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">',
    '<meta name="theme-color" content="#120e0c">',
    '<meta name="mobile-web-app-capable" content="yes">',
    '<meta name="apple-mobile-web-app-capable" content="yes">',
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
    '<meta name="apple-mobile-web-app-title" content="thermite">',
    '<link rel="manifest" href="manifest.webmanifest">',
    '<link rel="apple-touch-icon" href="apple-touch-icon.png">',
)


class WebFrame(unittest.TestCase):
    def test_every_page_declares_the_installed_app_frame(self):
        for _, _, asset in oh_routes.PAGES:
            head = re.sub(r",\s+", ",", (REMOTE / asset).read_text().split("</head>", 1)[0])
            for tag in INSTALLED_APP_HEAD:
                with self.subTest(page=asset, tag=tag):
                    self.assertIn(tag, head)

    def test_only_the_frame_keeps_the_top_inset(self):
        for sheet in sorted(REMOTE.glob("*.css")):
            if sheet.name == "shell.css":
                continue
            with self.subTest(sheet=sheet.name):
                self.assertNotIn("safe-area-inset-top", re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S),
                                 "a page under the frame must not add the inset again")

    def test_a_toast_opens_below_the_bar_and_the_inset(self):
        # Below 1180 px the bar is drawn over the top of the page, and a toast placed from the top of the
        # window was hidden behind it. The frame's header height is the bar's 44 px and the inset.
        shell = (REMOTE / "shell.css").read_text()
        self.assertIn("--thermite-inset-top: env(safe-area-inset-top, 0px)", shell)
        self.assertRegex(shell, r"@media\(max-width:1179px\) \{\s*body\.thermite-app \{ --thermite-header-height: "
                                r"calc\(44px \+ var\(--thermite-inset-top\)\); \}")
        remote = (REMOTE / "remote.css").read_text()
        # From 820 px the toast is at the foot of the window, clear of the bar.
        self.assertEqual(re.findall(r"\.toast \{[^}]*?\btop: ([^;]+);", remote), ["calc(var(--thermite-header-height) + 6px)", "auto"])

    def test_remotes_lends_the_bar_its_room_its_switch_info_and_log(self):
        page = (REMOTE / "index.html").read_text()
        lent = dict(re.findall(r'id="([a-z-]+)" data-thermite-bar="(title|action)"', page))
        self.assertEqual(lent, {"room-switch": "title", "info-open": "action", "log-toggle": "action"})
        # The Watch | Listen switch follows the room into the title.
        self.assertLess(page.index('id="room-switch"'), page.index('data-modes data-thermite-bar="title"'))
        self.assertLess(page.index('data-modes data-thermite-bar="title"'), page.index('id="info-open"'))
        self.assertIn('class="remote__top" data-thermite-bar-host', page)


if __name__ == "__main__":
    unittest.main()
