"""Clean browser URLs retain real backends, local assets and safe sign-in returns."""
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs

import oh_routes
from oh_features import remote

AREA = Path(__file__).resolve().parents[1]


class Addresses(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"}:
                self.links.append((tag, value))


class CleanRoutes(unittest.TestCase):
    def test_served_pages_keep_assets_absolute_and_svg_fragments_local(self):
        for _, path, asset in oh_routes.PAGES:
            content = remote.served_page((remote.SOURCE / asset).read_text())
            self.assertIn('data-thermite-hosted="true"', content)
            self.assertNotIn('<base', content)
            addresses = Addresses()
            addresses.feed(content)
            for tag, url in addresses.links:
                with self.subTest(page=path, tag=tag, url=url):
                    if tag == "use":
                        self.assertTrue(url.startswith("#"))
                    elif tag == "a":
                        self.assertIn(url, oh_routes.ROUTES)
                    else:
                        self.assertTrue(url.startswith(remote.ASSET_URL))
                        self.assertIn(url.removeprefix(remote.ASSET_URL), remote.FILES)
            self.assertLess(content.index('src="/static/remote/navigation.js"'), content.index('src="/static/remote/access.js"'))

    def test_installed_app_starts_at_home_with_assets_in_the_manifest_directory(self):
        manifest = json.loads(remote.served_manifest((remote.SOURCE / "manifest.webmanifest").read_text()))
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertTrue(all(icon["src"] in remote.FILES for icon in manifest["icons"]))

    def test_generated_page_versions_match_the_served_assets(self):
        import oh_render
        files = oh_render.render({"version": 1, "rooms": {}, "devices": []},
                                 system_metadata={"version": 1, "runtime": {"version": "test"}, "tools": []})
        for _, _, asset in oh_routes.PAGES:
            addresses = Addresses()
            addresses.feed(files[f"html/remote/{asset}"].decode())
            for tag, url in addresses.links:
                if tag in {"a", "use"}:
                    continue
                target = urlsplit(url)
                value = files["html/remote/" + target.path.removeprefix(remote.ASSET_URL)]
                digest = hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()[:16]
                self.assertEqual(parse_qs(target.query), {"v": [digest]})
        self.assertIn('href="/static/remote/manifest.webmanifest?v=', files["html/remote/home.html"].decode())

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_browser_navigation_and_backend_detection_share_the_route_contract(self):
        script = r'''
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const pages = JSON.parse(process.argv[1]);
const context = vm.createContext({ URL, URLSearchParams, setTimeout, clearTimeout });
for (const name of ["navigation", "house", "remotes", "activity", "backend", "pair", "access"])
  vm.runInContext(fs.readFileSync("web/remote/" + name + ".js", "utf8"), context);
context.document = { documentElement: { dataset: { thermiteHosted: "true" } } };
for (const [id, path, asset] of pages) {
  context.location = { protocol: "https:", origin: "https://site.test", pathname: path };
  assert.equal(context.ThermiteNavigation.href(id), path);
  assert.equal(context.ThermiteNavigation.asset("theme.css"), "/static/remote/theme.css");
  assert.equal(context.RemoteBackend.servedByOpenHab(context.location), true);
  assert.equal(context.PairingBackend.servedByOpenHab(context.location), true);
  assert.equal(context.ThermiteAccess.returnTo(path + "?room=den&choose=off", "https://site.test"),
    id === "access" ? "/" : path + "?room=den");
  assert.equal(context.ThermiteNavigation.canonical("/static/remote/" + asset), path);
}
assert.equal(context.ThermiteAccess.returnTo("/settings/", "https://site.test"), "/");
assert.equal(context.ThermiteAccess.returnTo("https://other.test/system/", "https://site.test"), "/");
context.document.documentElement.dataset = {};
context.location = { protocol: "http:", pathname: "/web/remote/index.html" };
assert.equal(context.ThermiteNavigation.href("remotes", "?backend=mock"), "./?backend=mock");
assert.equal(context.ThermiteNavigation.asset("theme.css"), "theme.css");
assert.equal(context.RemoteBackend.servedByOpenHab(context.location), false);
context.location = { protocol: "https:", pathname: "/static/remote/index.html" };
assert.equal(context.ThermiteNavigation.href("remotes"), "/remotes/");
context.location.protocol = "file:";
assert.equal(context.RemoteBackend.servedByOpenHab(context.location), false);
const Cameras = require("./tui/remote/cameras"), Topology = require("./tui/remote/topology");
assert.equal(Cameras.url("http://site.test:13601", "front", false, true), "http://site.test:13601/cameras/?backend=native#front");
assert.equal(Topology.url("http://site.test:13601", "den", "player_a", false, true), "http://site.test:13601/site/?room=den&source=player_a&backend=native");
assert.equal(new URL(Cameras.url("http://site.test:13601", "front", true, true)).searchParams.get("backend"), "mock");
'''
        result = subprocess.run([shutil.which("node"), "-e", script, json.dumps(oh_routes.PAGES)],
                                cwd=AREA, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
