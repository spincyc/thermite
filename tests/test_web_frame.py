"""Every web page opens the same installed-app frame, and only the frame keeps the top safe-area inset."""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_routes  # noqa: E402
from oh_features import remote as remote_feature  # noqa: E402

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

    def test_shell_css_loads_before_page_css_and_uses_zero_specificity_focus(self):
        for _, _, asset in oh_routes.PAGES:
            head = (REMOTE / asset).read_text().split("</head>", 1)[0]
            sheets = re.findall(r'<link\s+rel="stylesheet"\s+href="([^"]+)"', head)
            with self.subTest(page=asset):
                self.assertGreaterEqual(len(sheets), 2)
                self.assertEqual(sheets[0], "theme.css")
                self.assertEqual(sheets[1], "shell.css")
        shell = (REMOTE / "shell.css").read_text()
        self.assertIn(":where(.thermite-content) :where(a,button,select,input):focus-visible", shell)
        self.assertIn(":where(.thermite-content) :where(summary,textarea):focus-visible", shell)

    def test_no_innerhtml_sinks_in_web_remote(self):
        for script in sorted(REMOTE.glob("*.js")):
            with self.subTest(script=script.name):
                self.assertNotIn("innerHTML", script.read_text())

    def test_remote_validate_preserves_camera_problems_when_theme_fails(self):
        cam = {"id": "front_cam", "controls": {"control": ["unifi_protect"]}}
        bad_camera = {"devices": [cam], "theme": ""}
        problems = remote_feature.validate(bad_camera)
        self.assertTrue(any("protect:" in p for p in problems), problems)
        self.assertTrue(any("theme:" in p for p in problems), problems)
        bad_theme_file = {"devices": [cam], "theme": "no-such-theme-file.json"}
        problems2 = remote_feature.validate(bad_theme_file)
        self.assertTrue(any("protect:" in p for p in problems2), problems2)
        self.assertTrue(any("theme:" in p for p in problems2), problems2)

    @unittest.skipUnless(shutil.which("node"), "node needed for web UI regressions")
    def test_web_ui_regressions(self):
        script = r"""
"use strict";
const assert = require("assert/strict");
const fs = require("fs"), path = require("path"), vm = require("vm");
const webDir = process.argv[1];
const testsDir = process.argv[2];
const read = (name) => fs.readFileSync(path.join(webDir, name), "utf8");
const DOM = require(path.join(testsDir, "remote_dom"));
if (!("firstElementChild" in DOM.Element.prototype)) {
  Object.defineProperty(DOM.Element.prototype, "firstElementChild", { get() { return this.children[0] || null; } });
}

// 1. access.js: already_initialized, new error codes, and preserving unknown error.code
const Access = require(path.join(webDir, "access"));
for (const code of ["already_initialized", "credential_expired", "too_many_streams", "invalid_lock",
  "invalid_active", "invalid_admin", "history_unavailable", "media_unavailable",
  "runtime_not_configured", "unknown_room", "body_too_large"]) {
  const err = Access.problem(code, 400);
  assert.equal(err.code, code);
  assert.notEqual(err.message, "The action could not be completed.", code);
}
const unknown = Access.problem("future_gateway_code", 418);
assert.equal(unknown.code, "future_gateway_code");
assert.equal(unknown.message, "The action could not be completed.");
const bogus = Access.problem("NOT VALID!!", 500);
assert.equal(bogus.code, "access_unavailable");

// 2. session.js: digit assignment & chooseDigit respect current.order
const quiet = { log() {}, info() {}, warn() {}, error() {} };
const sCtx = vm.createContext({ console: quiet, setTimeout, clearTimeout });
vm.runInContext(read("backend.js"), sCtx);
vm.runInContext(read("backend-mock.js"), sCtx);
vm.runInContext(read("session.js"), sCtx);
const orderHouse = {
  rooms: [{ id: "den", label: "Den", sources: ["src_a", "src_b"], order: ["src_b", "src_a"], display: "tv", devices: ["tv"] }],
  sources: {
    src_a: { label: "Alpha", kind: "streamer", remote: "apple_tv" },
    src_b: { label: "Beta", kind: "computer", remote: "pc" }
  },
  devices: { tv: { label: "TV", remote: "tv", control: {} } }
};
const chosen = [];
const mockBe = sCtx.RemoteBackend.create(orderHouse, { backend: "mock", latency: 0 });
const origChoose = mockBe.choose.bind(mockBe);
mockBe.choose = (room, source) => { chosen.push(source); return origChoose(room, source); };
const sess = sCtx.RemoteSession.create({ backend: mockBe, house: orderHouse, layouts: { buttons: {}, remotes: {} }, store: sCtx.RemoteSession.memoryStore() });
(async () => {
  await sess.start("den");
  const bubbles = Array.from(sess.bubbles(sess.controls())).filter((item) => item.choose);
  assert.deepEqual(bubbles.map((b) => [b.id, b.digit]), [["off", "0"], ["src_b", "1"], ["src_a", "2"]]);
  assert.equal(sess.chooseDigit("1"), "src_b");
  assert.equal(sess.chooseDigit("2"), "src_a");
  assert.deepEqual(chosen, ["src_b", "src_a"]);

  // 3. backend.js: stop() unsubscribes from access without calling access.stop()
  let accessStopped = 0, unsubscribed = 0;
  const fakeAccess = {
    native: false,
    subscribe() { return () => { unsubscribed += 1; }; },
    stop() { accessStopped += 1; },
    record() {}
  };
  class FakeES { constructor() { this.readyState = 0; } close() { this.readyState = 2; } }
  const oh = new sCtx.RemoteBackend.OpenHabBackend(orderHouse, { EventSource: FakeES, access: fakeAccess, active: false });
  oh.stop();
  assert.equal(unsubscribed, 1);
  assert.equal(accessStopped, 0);

  // 4. topology.js: empty-row colspan equals VIDEO_COLUMNS.length (9) and DATA_COLUMNS.length (5)
  const topoDoc = new DOM.Document(read("topology.html"));
  const topoCtx = vm.createContext({
    document: topoDoc,
    window: null,
    location: { search: "", origin: "https://site.example", protocol: "https:", pathname: "/site/" },
    URLSearchParams,
    console,
    setTimeout,
    clearTimeout,
    addEventListener() {},
    ThermiteNavigation: { asset: (n) => n },
    REMOTE_HOUSE: { rooms: [], sources: {}, devices: {} },
    RemoteBackend: { create: () => ({ mock: true, subscribe() {}, getState: async () => ({ value: "NULL" }), setActive() {} }) },
    fetch: async () => ({ ok: true, json: async () => ({ version: 1, nodes: [], rooms: [], routes: {}, edges: [] }) })
  });
  topoCtx.window = topoCtx;
  vm.runInContext(read("topology.js"), topoCtx);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  const videoEmpty = topoDoc.querySelector("[data-video-body] td.empty-panel");
  const dataEmpty = topoDoc.querySelector("[data-data-body] td.empty-panel");
  assert.equal(videoEmpty.getAttribute("colspan"), "9");
  assert.equal(dataEmpty.getAttribute("colspan"), "5");

  // 5. listen-session.js + listen.js: offline banner (audio down, music down, session down) & wide Library hides [data-page]
  const lDoc = new DOM.Document(read("index.html"));
  const lCtx = vm.createContext({
    document: lDoc,
    window: null,
    location: { search: "", origin: "https://site.example" },
    URLSearchParams,
    CSS: { escape: (s) => s },
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval() {},
    matchMedia: () => ({ matches: true, addEventListener() {} }),
    RemoteSession: { NOT_CONNECTED: "Not connected to thermite. Reconnecting…" }
  });
  lCtx.window = lCtx;
  vm.runInContext(read("listen-session.js"), lCtx);
  vm.runInContext(read("listen.js"), lCtx);
  const listenHouse = {
    rooms: [{ id: "den", label: "Den", speakers: ["spk1"] }],
    speakerRooms: [],
    devices: { spk1: { label: "Den Speaker", short: "Speaker", model: "Arc", audio: { place: "Den" } } }
  };
  let audioConnected = true, musicConnected = true, musicStatus = { state: "ready", text: "" };
  const fakeAudio = {
    subscribe() { return () => {}; },
    selected: () => "spk1",
    select() {},
    devices: () => [{ devices: [{ id: "spk1", playing: false, title: null, problem: null }] }],
    inputFeed: () => null,
    silent: () => false,
    view: () => ({
      device: { label: "Den Speaker", model: "Arc", place: "Den" },
      now: "Nothing playing", track: null, playing: false, transport: [], volume: null, muted: false,
      power: null, input: null, sections: [], facts: [], locked: null, connected: audioConnected, problem: null
    })
  };
  let musicAvailable = true;
  const fakeMusic = {
    subscribe() { return () => {}; },
    selected: () => "spk1",
    select() {},
    status: () => musicStatus,
    speakers: () => [{ speakers: [{ id: "spk1", available: musicAvailable, playing: false, paused: false, title: null }] }],
    view: () => (musicAvailable ? {
      connected: musicConnected, now: "Nothing playing", title: null, line: null, art: null, progress: null,
      playing: false, playLabel: "Play", canSkip: false,
      shuffle: { on: false, known: true, pending: false, label: "Shuffle off" },
      repeat: { value: "off", known: true, pending: false, label: "Repeat off" },
      group: null, queueOf: null, locked: null, groupLocked: null
    } : null),
    library: () => (musicAvailable ? { tab: "browse", blind: "", page: { title: "Album", items: [] }, search: null, queue: null } : null),
    tidal: () => null
  };
  const listenSess = lCtx.RemoteListen.create({ audio: fakeAudio, music: fakeMusic, house: listenHouse, store: { get: () => null, set() {} } });
  listenSess.setRoom("den");
  assert.equal(listenSess.view().connected, true);
  musicConnected = false;
  musicStatus = { state: "ready", text: "Not connected to thermite. Reconnecting…" };
  assert.equal(listenSess.view().connected, false, "music disconnect propagates to listen view.connected");
  let mainConnected = true;
  const fakePageSess = {
    mode: () => "listen",
    connected: () => mainConnected,
    current: () => ({ id: "den" }),
    listen: listenSess
  };
  const $ = (sel) => lDoc.querySelector(sel);
  const all = (sel) => lDoc.querySelectorAll(sel);
  const node = (tag, attrs, children) => {
    const el = lDoc.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v != null) el.setAttribute(k, String(v)); });
    [].concat(children === undefined ? [] : children).forEach((c) => { if (c != null) el.appendChild(typeof c === "string" ? lDoc.createTextNode(c) : c); });
    return el;
  };
  const icon = () => lDoc.createElementNS("http://www.w3.org/2000/svg", "svg");
  const page = lCtx.RemoteListenPage.create({ session: fakePageSess, sessions: { audio: fakeAudio, music: fakeMusic, listen: listenSess }, ui: { $, all, node, icon, openSheet() {} } });
  assert.equal(page.alert(), "Not connected to thermite. Reconnecting…");
  // Wide Library hides [data-page] when !library
  lDoc.querySelector("[data-page]").hidden = false;
  musicAvailable = false;
  musicConnected = true;
  musicStatus = { state: "unavailable", text: "Music Assistant is not answering yet." };
  page.update();
  assert.equal(lDoc.querySelector("[data-page]").hidden, true, "stale detail page hidden when !library");
  // Main session disconnect also surfaces alert even with music ready
  mainConnected = false;
  musicAvailable = true;
  musicStatus = { state: "ready", text: "" };
  assert.equal(page.alert(), "Not connected to thermite. Reconnecting…");

  // 6. home.js: Listen offline banner updates on heard.backend connection event
  const hDoc = new DOM.Document(read("home.html"));
  let listenConnCb = null;
  const fakeListenBackend = {
    connected: true,
    subscribe(fn) { listenConnCb = fn; return () => {}; },
    setActive() {}
  };
  const hCtx = vm.createContext({
    document: hDoc,
    window: null,
    location: { search: "", origin: "https://site.example", protocol: "https:", pathname: "/" },
    URLSearchParams,
    localStorage: { getItem: (k) => (k === "remote.mode" ? "listen" : null), setItem() {} },
    console,
    setTimeout,
    clearTimeout,
    addEventListener() {},
    ThermiteShell: { href: (id) => "/" + id + "/" },
    ThermiteNavigation: { pages: [], allowed: () => false },
    REMOTE_HOUSE: listenHouse,
    RemoteSession: {
      NOT_CONNECTED: "Not connected to thermite. Reconnecting…",
      create: () => ({ connected: () => true, view: () => ({ led: "off", short: "Off", connected: true }), roomLocked: () => ({ locked: false, text: "" }) })
    },
    RemoteBackend: {
      create: (h, opts) => (opts && opts.view === "home"
        ? { connected: true, subscribe() {}, getRooms: async () => [], getState: async () => ({ value: "off" }), setActive() {} }
        : fakeListenBackend)
    },
    RemoteListen: {
      sessions: () => ({
        music: { status: () => ({ state: "ready", text: "" }) },
        listen: {
          subscribe() {},
          status: () => ({ led: "ok", short: "Playing", spoken: "Playing", playing: "♪ Song" })
        },
        start: async () => {},
        setActive() {}
      })
    }
  });
  hCtx.window = hCtx;
  vm.runInContext(read("home.js"), hCtx);
  await new Promise((r) => setImmediate(r));
  assert.ok(listenConnCb, "home.js subscribes to heard.backend connection");
  fakeListenBackend.connected = false;
  listenConnCb({ type: "connection", connected: false });
  assert.equal(hDoc.querySelector("[data-home-status]").textContent, "Not connected to thermite. Reconnecting…");
  assert.equal(hDoc.querySelector("[data-home-rooms] .thermite-room-state").textContent, "Not connected");

  // 7. appearance.js: syncThemeColor updates <meta name="theme-color">
  const metaDoc = new DOM.Document('<!doctype html><html data-theme="light"><head><meta name="theme-color" content="#120e0c"></head><body></body></html>');
  const aCtx = vm.createContext({
    document: metaDoc,
    location: { search: "" },
    URLSearchParams,
    getComputedStyle: () => ({ getPropertyValue: (p) => (p === "--th-bg" ? " #f3f2ee " : "") })
  });
  vm.runInContext(read("appearance.js"), aCtx);
  aCtx.ThermiteAppearance.syncThemeColor();
  assert.equal(metaDoc.querySelector('meta[name="theme-color"]').getAttribute("content"), "#f3f2ee");

  // 8. access-ui.js: single Sign out button, empty rooms guard on Settings, and .policy-status refresh on save
  const accDoc = new DOM.Document(read("access.html"));
  const accCtx = vm.createContext({
    document: accDoc, location: { origin: "https://site.example", search: "", reload() {}, replace() {} }, URLSearchParams,
    ThermiteAccess: { client: { native: false, loadSecurity: async () => ({ enabled: true }), ready: async () => ({ id: 1, username: "Admin", is_guest: false, can_edit: true }), logout: async () => {} } },
    ThermiteNavigation: { href: () => "/security/" }
  });
  vm.runInContext(read("access-ui.js"), accCtx);
  await new Promise((r) => setImmediate(r));
  const loginBtns = accDoc.querySelectorAll("[data-login] button").map((b) => b.textContent);
  assert.deepEqual(loginBtns, ["Sign out"]);

  let emptyPolicyCalled = 0;
  const setDoc = new DOM.Document(read("settings.html"));
  const setCtx = vm.createContext({
    document: setDoc, location: { origin: "https://site.example", href: "https://site.example/room-settings/", search: "" }, URLSearchParams,
    REMOTE_HOUSE: { rooms: [], sources: {} },
    ThermiteAccess: { client: { native: false, accountsExist: () => true, loadSecurity: async () => ({ enabled: true }), ready: async () => ({ id: 1, admin: true, can_edit: true }), canEdit: () => true, loadPolicy: async () => { emptyPolicyCalled += 1; return {}; } } },
    ThermiteNavigation: { href: () => "/security/" }
  });
  vm.runInContext(read("access-ui.js"), setCtx);
  await new Promise((r) => setImmediate(r));
  assert.equal(emptyPolicyCalled, 0);
  assert.equal(setDoc.querySelector("[data-access-status]").textContent, "No rooms configured.");

  const setDoc2 = new DOM.Document(read("settings.html"));
  const setCtx2 = vm.createContext({
    document: setDoc2, location: { origin: "https://site.example", href: "https://site.example/room-settings/", search: "" }, URLSearchParams,
    REMOTE_HOUSE: { rooms: [{ id: "den", label: "Den", sources: [] }], sources: {} },
    ThermiteAccess: {
      client: {
        native: false,
        accountsExist: () => true,
        loadSecurity: async () => ({ enabled: true }),
        ready: async () => ({ id: 1, admin: true, can_edit: true }),
        canEdit: () => true,
        loadPolicy: async () => ({ room: "den", revision: 1, can_edit: true, config: { version: 1, enabled: false, daily_off: [], locks: [] }, effective: { locked: false } }),
        savePolicy: async () => ({ room: "den", revision: 2, can_edit: true, config: { version: 1, enabled: true, daily_off: [], locks: [] }, effective: { locked: false } })
      }
    },
    ThermiteNavigation: { href: () => "/security/" }
  });
  vm.runInContext(read("access-ui.js"), setCtx2);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal(setDoc2.querySelector(".policy-status strong").textContent, "Unlocked · no schedule");
  setDoc2.querySelector('[name="enabled"]').checked = true;
  setDoc2.querySelector(".policy-form").dispatch("submit");
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal(setDoc2.querySelector(".policy-status strong").textContent, "Unlocked · schedule on");

  process.stdout.write(JSON.stringify({ ok: true }));
})().catch((err) => { process.stderr.write(err.stack + "\n"); process.exit(1); });
"""
        node = shutil.which("node")
        assert node is not None
        run = subprocess.run([node, "-e", script, str(REMOTE), str(AREA / "tests")],
                             text=True, capture_output=True, timeout=60)
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout)
        self.assertEqual(json.loads(run.stdout), {"ok": True})


if __name__ == "__main__":
    unittest.main()
