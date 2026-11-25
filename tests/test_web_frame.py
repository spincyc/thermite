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
        cam = {"id": "front_cam", "controls": {"observe": ["unifi_protect"]}}
        bad_camera = {"devices": [cam], "theme": ""}
        problems = remote_feature.validate(bad_camera)
        self.assertTrue(any("protect:" in p for p in problems), problems)
        self.assertTrue(any("theme:" in p for p in problems), problems)
        bad_theme_file = {"devices": [cam], "theme": "no-such-theme-file.json"}
        problems2 = remote_feature.validate(bad_theme_file)
        self.assertTrue(any("protect:" in p for p in problems2), problems2)
        self.assertTrue(any("theme:" in p for p in problems2), problems2)

    def test_remote_cameras_discovers_providers_via_cameras_hook_generically(self):
        import types
        from unittest import mock
        import oh_integrations as integrations
        from oh_conf import House
        from oh_features import remote_cameras

        custom = types.ModuleType("example_cam")
        custom.__getattr__ = integrations._hooks.absent
        custom.NAME, custom.PHASE = "example_cam", "observe"
        custom.cameras = lambda device, house: {
            "id": device["id"], "label": "Custom Porch", "room": device["room"],
            "location": house.room_label(device["room"]), "kind": "doorbell",
            "model": "Custom Bell", "doorbell": True, "packageCamera": True,
            "thing": "custom:cam:porch_bell",
            "items": {"online": "custom_porch_up", "motion": "custom_porch_motion",
                      "recording": "custom_porch_rec", "ring": "custom_porch_ring"},
            "media": {"snapshot": "/custom/media/snap/porch_bell",
                      "streams": {"high": "/custom/media/live/porch_bell/high",
                                  "package": "/custom/media/live/porch_bell/pkg"}},
        } if device["id"] == "porch_bell" else None
        house = House.unchecked({"version": 2, "rooms": {"porch": {"label": "Porch", "tag": "Room"}},
                                 "devices": [{"id": "porch_bell", "room": "porch", "kind": "doorbell", "ip": "192.0.2.50",
                                              "controls": {"observe": ["example_cam"]}}]})
        with mock.patch.object(integrations, "defining",
                               side_effect=lambda hook: (custom,) if hook == "cameras" else integrations.defining(hook)):
            manifest = remote_cameras.manifest(house)
        self.assertEqual(manifest["statusGroup"], remote_cameras.STATUS_GROUP)
        self.assertEqual(len(manifest["cameras"]), 1)
        entry = manifest["cameras"][0]
        self.assertEqual((entry["id"], entry["room"], entry["kind"], entry["packageCamera"]),
                         ("porch_bell", "porch", "doorbell", True))
        self.assertEqual(remote_cameras.media(manifest), {
            "/custom/media/snap/porch_bell": {"kind": "snapshot"},
            "/custom/media/live/porch_bell/high": {"kind": "sdp"},
            "/custom/media/live/porch_bell/pkg": {"kind": "sdp"},
        })
        for bad in ("/custom/../secret", "relative/path", "/custom/snap?a=1", "/custom//snap"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                remote_cameras.media({"cameras": [{"media": {"snapshot": bad, "streams": {}}}]})

    @unittest.skipUnless(shutil.which("node"), "node needed for cameras.js manifest test")
    def test_cameras_js_renders_non_unifi_manifest_without_vendor_regexes(self):
        script = r"""
"use strict";
const assert = require("assert/strict");
const fs = require("fs"), path = require("path"), vm = require("vm");
const webDir = process.argv[1];
const testsDir = process.argv[2];
const read = (name) => fs.readFileSync(path.join(webDir, name), "utf8");
const DOM = require(path.join(testsDir, "remote_dom"));
const doc = new DOM.Document(read("cameras.html"));
doc.querySelector("[data-camera-video]").pause = () => {};
const requested = [];
const manifest = {
  version: 1,
  statusGroup: "custom_camera_status",
  pollSeconds: 10,
  cameras: [{
    id: "porch_bell",
    label: "Porch Bell",
    room: "porch",
    location: "Porch",
    kind: "doorbell",
    model: "Generic Cam",
    doorbell: true,
    packageCamera: true,
    thing: "generic:bell:porch_bell",
    items: { online: "porch_up_state", motion: "porch_motion_sensor", recording: "porch_rec_flag", ring: "porch_ring_flag" },
    media: {
      snapshot: "/generic/media/snapshot/porch_bell",
      streams: { high: "/generic/media/stream/porch_bell/hd", package: "/generic/media/stream/porch_bell/pkg" }
    }
  }]
};
const ctx = vm.createContext({
  document: doc,
  window: null,
  location: { search: "", hash: "", protocol: "https:", origin: "https://site.example" },
  URLSearchParams,
  console,
  setTimeout: () => 1,
  clearTimeout() {},
  addEventListener() {},
  history: { replaceState() {} },
  ThermiteNavigation: { asset: (n) => "/" + n },
  fetch: async (url) => {
    requested.push(url);
    if (url.endsWith("/cameras.json")) return { ok: true, status: 200, json: async () => manifest };
    return { ok: true, status: 200, json: async () => ({
      members: [
        { name: "porch_up_state", state: "ON" },
        { name: "porch_motion_sensor", state: "OPEN" },
        { name: "porch_rec_flag", state: "ON" },
        { name: "porch_ring_flag", state: "OFF" }
      ]
    }) };
  }
});
ctx.window = ctx; ctx.globalThis = ctx;
vm.runInContext(read("cameras.js"), ctx);
(async () => {
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  const parsed = ctx.RemoteCameras.catalogue(manifest);
  assert.equal(parsed.statusGroup, "custom_camera_status");
  assert.equal(parsed.cameras[0].kind, "doorbell");
  assert.equal(parsed.cameras[0].items.online, "porch_up_state");
  assert.equal(parsed.cameras[0].media.snapshot, "/generic/media/snapshot/porch_bell");
  assert.ok(requested.some((u) => u.includes("/rest/items/custom_camera_status")), requested.join(","));
  assert.equal(doc.querySelector("[data-camera-title]").textContent, "Porch Bell");
  assert.equal(doc.querySelector("[data-camera-connection]").textContent, "Connected");
  assert.equal(doc.querySelector("[data-camera-recording]").textContent, "On");
  assert.throws(() => ctx.RemoteCameras.catalogue({
    ...manifest,
    cameras: [{ ...manifest.cameras[0], media: { snapshot: "/../etc/passwd", streams: {} } }]
  }), /Camera catalogue is unavailable/);
  assert.throws(() => ctx.RemoteCameras.catalogue({
    ...manifest,
    cameras: [{ ...manifest.cameras[0], packageCamera: false }]
  }), /Camera catalogue is unavailable/);
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

  // 9. listen.js: stop() clears the 1s progress interval and removes matchMedia listener
  let clearedIntervals = 0, removedMedia = 0;
  const stopDoc = new DOM.Document(read("index.html"));
  const stopCtx = vm.createContext({
    document: stopDoc,
    window: null,
    setInterval: () => 4242,
    clearInterval: (id) => { if (id === 4242) clearedIntervals += 1; },
    setTimeout,
    clearTimeout,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener: () => { removedMedia += 1; } }),
    CSS: { escape: (v) => String(v) },
    RemoteSession: { SHORTCUTS: [], LISTEN_SHORTCUTS: [], NOT_CONNECTED: "Not connected" },
    RemoteMusic: { PLAY: {} }
  });
  stopCtx.window = stopCtx;
  vm.runInContext(read("listen.js"), stopCtx);
  const stopPage = stopCtx.RemoteListenPage.create({
    session: { mode: () => "watch", connected: () => true, listen: null, pressKey: () => null },
    sessions: { listen: { view: () => null, speakers: () => [], subscribe: () => () => {} }, music: null },
    ui: { $: (s) => stopDoc.querySelector(s), all: (s) => stopDoc.querySelectorAll(s),
      node: (t) => stopDoc.createElement(t), icon: () => stopDoc.createElement("span"),
      openSheet() {}, markOverflow() {}, reveal() {} }
  });
  assert.equal(typeof stopPage.stop, "function");
  stopPage.stop();
  assert.equal(clearedIntervals, 1, "listen.js stop() clears its 1s interval");
  assert.equal(removedMedia, 1, "listen.js stop() removes matchMedia listener");

  // 10. access.js: ready() parallelizes /api/v1/session and /api/v1/me when accounts enabled without token
  let inFlight = 0, peakInFlight = 0;
  const gates = [];
  const parAccess = Access.create({
    base: "https://site.example",
    fetch: (url) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/security") {
        return Promise.resolve({
          ok: true, status: 200,
          headers: { getSetCookie: () => [] },
          json: async () => ({ bootstrap_required: false, setup_available: false, guest_text: true, guest_cameras: false, csrf: "C".repeat(32) })
        });
      }
      inFlight += 1;
      peakInFlight = Math.max(peakInFlight, inFlight);
      return new Promise((resolve) => {
        gates.push(() => {
          inFlight -= 1;
          resolve({
            ok: true, status: 200,
            headers: { getSetCookie: () => [] },
            json: async () => (route === "/api/v1/session"
              ? { csrf: "D".repeat(32) }
              : { id: 0, username: "Guest", admin: false, can_edit: false, is_guest: true, permissions: ["site.read"] })
          });
        });
      });
    }
  });
  const readyPromise = parAccess.ready();
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal(peakInFlight, 2, "session and me run concurrently via Promise.all");
  while (gates.length) gates.shift()();
  const principal = await readyPromise;
  assert.equal(principal.is_guest, true);
  parAccess.stop();

  // 11. home.js: starting in Listen creates only one OpenHabBackend (no duplicate view:"home" backend)
  let homeBackendsCreated = 0;
  const hDoc2 = new DOM.Document(read("home.html"));
  const hCtx2 = vm.createContext({
    document: hDoc2,
    window: null,
    location: { search: "", origin: "https://site.example", protocol: "https:", pathname: "/" },
    URLSearchParams,
    localStorage: { getItem: (k) => (k === "remote.mode" ? "listen" : null), setItem() {} },
    console, setTimeout, clearTimeout, addEventListener() {},
    ThermiteShell: { href: (id) => "/" + id + "/" },
    ThermiteNavigation: { pages: [], allowed: () => false },
    REMOTE_HOUSE: listenHouse,
    RemoteSession: {
      NOT_CONNECTED: "Not connected",
      create: () => ({ connected: () => true, view: () => ({ led: "off", short: "Off", connected: true }), roomLocked: () => ({ locked: false, text: "" }) })
    },
    RemoteBackend: {
      create: () => {
        homeBackendsCreated += 1;
        return { connected: true, subscribe() { return () => {}; }, getRooms: async () => [], getState: async () => ({ value: "off" }), setActive() {} };
      }
    },
    RemoteListen: {
      sessions: () => ({
        music: { status: () => ({ state: "ready", text: "" }) },
        listen: { subscribe() {}, status: () => ({ led: "ok", short: "Playing", spoken: "Playing", playing: "♪ Song" }) },
        start: async () => {}, setActive() {}
      })
    }
  });
  hCtx2.window = hCtx2;
  vm.runInContext(read("home.js"), hCtx2);
  await new Promise((r) => setImmediate(r));
  assert.equal(homeBackendsCreated, 1, "starting in Listen creates only 1 backend");
  // Switching to Watch reuses the single backend when it supports getRooms/getState.
  hDoc2.querySelector('[data-home-modes] [data-mode="watch"]').click();
  await new Promise((r) => setImmediate(r));
  assert.equal(homeBackendsCreated, 1, "switching Listen -> Watch reuses the existing backend");

  // 12. topology.js: window resize debounces via rAF and calls tellMore without rebuilding table rows, and SSE state events coalesce via rAF
  const tDoc2 = new DOM.Document(read("topology.html"));
  let tResizeCb = null, tStateCb = null, tRafQueue = [];
  const tManifest = {
    version: 1,
    nodes: [{ id: "pc", label: "PC", room: "den", kind: "computer" }, { id: "tv", label: "TV", room: "den", kind: "tv" }],
    rooms: [{ id: "den", label: "Den", devices: ["pc", "tv"] }],
    routes: { den: { pc: { label: "PC", nodes: [{ device: "pc", label: "PC", role: "source" }, { device: "tv", label: "TV", role: "display" }] } } },
    edges: []
  };
  const tCtx2 = vm.createContext({
    document: tDoc2,
    window: null,
    location: { search: "", origin: "https://site.example", protocol: "https:", pathname: "/site/" },
    URLSearchParams,
    console,
    setTimeout, clearTimeout,
    requestAnimationFrame: (fn) => { tRafQueue.push(fn); return tRafQueue.length; },
    addEventListener: (ev, fn) => { if (ev === "resize") tResizeCb = fn; },
    ThermiteNavigation: { asset: (n) => "/" + n },
    REMOTE_HOUSE: { rooms: [{ id: "den", label: "Den" }], sources: {}, devices: {} },
    RemoteBackend: {
      create: () => ({ mock: true, connected: true, subscribe(fn) { tStateCb = fn; return () => {}; }, getState: async () => ({ value: "pc" }), setActive() {} })
    },
    fetch: async () => ({ ok: true, status: 200, json: async () => tManifest })
  });
  tCtx2.window = tCtx2;
  vm.runInContext(read("topology.js"), tCtx2);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  while (tRafQueue.length) tRafQueue.shift()();
  const videoRowBefore = tDoc2.querySelector("[data-video-body] tr");
  assert.ok(videoRowBefore);
  assert.ok(tResizeCb, "topology registers resize handler");
  tResizeCb();
  tResizeCb();
  assert.equal(tRafQueue.length, 1, "resize coalesces into one rAF");
  tRafQueue.shift()();
  assert.equal(tDoc2.querySelector("[data-video-body] tr"), videoRowBefore, "resize does not rebuild table DOM");
  // Burst SSE state events coalesce into one rAF draw:
  tStateCb({ type: "state", room: "den", state: { value: "off" } });
  tStateCb({ type: "state", room: "den", state: { value: "pc" } });
  assert.equal(tRafQueue.length, 1, "burst SSE state events coalesce into one rAF");
  tRafQueue.shift()();

  // 13. music-session.js: clock skew compensation (+120s ahead and -120s behind gateway clock)
  const mCtx = vm.createContext({ console, setTimeout, clearTimeout, URLSearchParams });
  mCtx.window = mCtx; mCtx.globalThis = mCtx;
  vm.runInContext(read("music-session.js"), mCtx);
  for (const skewSec of [120, -120]) {
    let localNow = 1763208000000 + skewSec * 1000;
    const serverAt = 1763208000;
    const subs = [];
    const fakeBackend = {
      overview: async () => ({
        version: 1, available: true, problem: null, sources: { tidal: false, libraries: {} },
        speakers: [{
          id: "sp1", label: "Speaker", room: "den", room_label: "Den", provider: "sonos",
          available: true, state: "playing", volume: 30, muted: false, group: null, shuffle: false, repeat: "off",
          now_playing: { title: "Track", artist: "Artist", album: "Album", duration: 240, elapsed: 60, at: serverAt }
        }]
      }),
      subscribe: (fn) => { subs.push(fn); return () => {}; },
      open() {}, close() {},
      transport: async () => ({ accepted: true })
    };
    const ms = mCtx.RemoteMusic.create({ backend: fakeBackend, now: () => localNow, setTimeout, clearTimeout, watchLocks: false });
    await ms.start("sp1");
    assert.equal(ms.view().progress.elapsed, 60, "skew " + skewSec + "s starts at reported elapsed");
    localNow += 5000;
    assert.equal(ms.view().progress.elapsed, 65, "skew " + skewSec + "s advances 5s in 5s");
    ms.stop();
  }

  // 14. remote.js: rAF coalescing, Signal/Info memoization, and Stage 3b routeDetail display
  const rDoc = new DOM.Document(read("index.html"));
  let rRafQueue = [], sessionSub = null, infoCalls = 0, diagModel = null;
  let diagOpen = false;
  const rRoom = { id: "den", label: "Den", options: [{ id: "off", label: "Off" }, { id: "pc", label: "PC" }], speakers: [] };
  const rCtx = vm.createContext({
    document: rDoc,
    window: null,
    location: { search: "?backend=mock&room=den", href: "file:///remote/index.html" },
    URLSearchParams, console, setTimeout, clearTimeout,
    requestAnimationFrame: (fn) => { rRafQueue.push(fn); return rRafQueue.length; },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    getComputedStyle: (el) => ({ getPropertyValue: () => "", display: el.classList && el.classList.contains("panel--signal") ? "flex" : "none" }),
    addEventListener() {},
    REMOTE_HOUSE: { rooms: [rRoom], devices: {}, sources: {} },
    REMOTE_LAYOUTS: { remotes: {}, buttons: {} },
    RemoteBackend: { create: () => ({ setActive() {}, subscribe() { return () => {}; } }) },
    RemoteLog: { create: () => ({ capture() {} }) },
    RemoteListen: { sessions: () => ({ listen: { subscribe() { return () => {}; }, stop() {} }, start: async () => {}, setActive() {} }) },
    RemoteListenPage: { create: () => ({ update() {}, alert: () => "", stop() {} }) },
    ThermiteShell: { href: () => "#" },
    RemoteDiagnostics: vm.runInContext(read("diagnostics.js") + "\nRemoteDiagnostics;", vm.createContext({})),
    RemoteSession: {
      NOT_CONNECTED: "Not connected", DEVICE_ROLE: {}, SHORTCUTS: [], LISTEN_SHORTCUTS: [],
      keyHints: () => ({}),
      memoryStore: () => { const m = new Map(); return { get: (k) => m.get(k) ?? null, set: (k, v) => m.set(k, v) }; },
      create: () => ({
        demo: false,
        subscribe: (fn) => { sessionSub = fn; },
        start: async () => {},
        current: () => rRoom,
        connected: () => true,
        mode: () => "watch",
        modes: () => ["watch"],
        kinds: () => ["watch"],
        listed: () => [rRoom],
        room: () => rRoom,
        state: () => ({ value: "pc", pending: null, status: "current" }),
        view: () => ({ led: "ok", short: "PC", spoken: "PC", playing: "", problem: "" }),
        roomLocked: () => ({ locked: false, text: "" }),
        roomLock: () => ({ canManage: false, label: "Lock room", disabled: false, busy: false, text: "", room: "den" }),
        lockNotice: () => "",
        controlBlock: () => "",
        controlAllowed: () => true,
        controls: () => [],
        activeTarget: () => null,
        remoteHidden: () => true,
        bubbles: () => [{ id: "off", label: "Off", role: "off", choose: true }],
        selectedBubble: () => "off",
        bubbleState: () => "idle",
        bubbleLock: () => ({ locked: false, inert: false }),
        bubbleLabel: () => "Off",
        remoteOf: () => null,
        spec: () => ({ label: "Key" }),
        drives: () => "TV",
        canPress: () => true,
        audioView: () => ({ muted: null, slider: false }),
        info: () => { infoCalls += 1; return { rows: [["Keys", "PC"]], controls: [], devices: [] }; },
        diagnostics: () => diagModel,
        openDiagnostics: async () => { diagOpen = true; },
        closeDiagnostics: () => { diagOpen = false; },
        isAnswering: () => true,
        itemStateText: () => "pc"
      })
    }
  });
  rCtx.window = rCtx; rCtx.globalThis = rCtx;
  vm.runInContext(read("remote.js"), rCtx);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  // Coalesce 4 burst change events into 1 rAF update:
  const infoBeforeBurst = infoCalls;
  sessionSub({ type: "change" });
  sessionSub({ type: "change" });
  sessionSub({ type: "access" });
  sessionSub({ type: "change" });
  assert.equal(rRafQueue.length, 1, "burst change/access events coalesce into 1 rAF");
  assert.equal(infoCalls, infoBeforeBurst, "update deferred until rAF");
  rRafQueue.shift()();
  assert.equal(infoCalls, infoBeforeBurst + 1, "single update runs on rAF");
  // Unchanged Signal panel skips DOM teardown & rebuild:
  diagModel = {
    room: { id: "den", label: "Den" }, source: { id: "pc", label: "PC" }, pending: null,
    connected: true, routeReady: false, routeStatus: "failed", routeDetail: "Route failed: matrix timeout",
    route: [], sections: { path: [], video: [], audio: [], edid: [], processing: [] }
  };
  sessionSub({ type: "diagnostics" });
  const sigPanel = rDoc.querySelector(".panel--signal [data-signal]");
  assert.ok(sigPanel.textContent.includes("Route failed: matrix timeout"), "Stage 3b routeDetail rendered in Signal panel");
  const headRef = sigPanel.firstChild;
  sessionSub({ type: "diagnostics" });
  assert.equal(sigPanel.firstChild, headRef, "unchanged Signal panel skips DOM teardown");
  // Unchanged Info panel skips DOM teardown & closed Info sheet skips rebuild:
  const inlineInfo = rDoc.querySelector(".panel--info [data-map]");
  const sheetInfo = rDoc.querySelector("#info-sheet [data-map]");
  const dtRef = inlineInfo.firstChild;
  assert.equal(sheetInfo.children.length, 0, "closed Info sheet is not populated eagerly");
  sessionSub({ type: "change" });
  rRafQueue.shift()();
  assert.equal(inlineInfo.firstChild, dtRef, "unchanged Info panel skips DOM teardown");
  assert.ok(diagOpen);

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

    @unittest.skipUnless(shutil.which("node"), "node is required to exercise shared client utilities and new UI wiring")
    def test_shared_utilities_layouts_partial_alerts_history_and_tokens(self):
        """Verify dom.js, sse.js, hold.js, RemoteLayouts.layoutPlan, Stage 3a
        'partial' state, TUI Home alerts, Web Home room history, Web Account
        personal tokens CRUD, and vendor-neutral pairing/activity errors."""
        script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const REMOTE = process.argv[1];
const TESTS = process.argv[2];
const AREA = path.resolve(TESTS, "..");
const read = (name) => fs.readFileSync(path.join(REMOTE, name), "utf8");
const DOM = require(path.join(TESTS, "remote_dom"));

(async () => {
  // 1. dom.js: cleanText, el, svgIcon
  const doc = new DOM.Document("<!doctype html><html><body></body></html>");
  const domCtx = vm.createContext({ document: doc });
  vm.runInContext(read("dom.js"), domCtx);
  const Dom = domCtx.ThermiteDom;
  assert.equal(Dom.cleanText("  Hello\u202e\x07 World  ", 20), "Hello World");
  assert.equal(Dom.cleanText("abcdefghij", 5), "abcde");
  const btn = Dom.el("button", { type: "button", class: "x", disabled: true }, "Tap");
  assert.equal(btn.tagName, "BUTTON");
  assert.equal(btn.textContent, "Tap");
  assert.equal(btn.disabled, true);
  const svg = Dom.svgIcon("icon", "i-lock", "Locked");
  assert.equal(svg.getAttribute("role"), "img");
  assert.equal(svg.getAttribute("aria-label"), "Locked");

  // 2. sse.js: backoff, generation fencing, visibility pause/resume, liveness watchdog
  const timers = new Map();
  let nextT = 1;
  const setT = (fn, ms) => { const id = nextT++; timers.set(id, { fn, ms }); return id; };
  const clearT = (id) => { timers.delete(id); };
  const fireAll = (ms) => {
    for (const [id, entry] of [...timers]) {
      if (ms === undefined || entry.ms === ms) { timers.delete(id); entry.fn(); }
    }
  };
  const opened = [];
  class FakeES {
    constructor(url) { this.url = url; this.readyState = 0; opened.push(this); }
    close() { this.readyState = 2; }
  }
  const visListeners = [];
  const fakeDoc = {
    visibilityState: "visible",
    addEventListener(type, fn) { if (type === "visibilitychange") visListeners.push(fn); },
    removeEventListener() {}
  };
  const sseCtx = vm.createContext({ document: fakeDoc, setTimeout: setT, clearTimeout: clearT });
  vm.runInContext(read("sse.js"), sseCtx);
  let msgs = 0, staleHits = 0;
  const stream = sseCtx.ThermiteSse.create({
    url: "/api/v1/events/remote",
    EventSource: FakeES,
    livenessMs: 5000,
    setTimeout: setT,
    clearTimeout: clearT,
    watchVisibility: true,
    document: fakeDoc,
    onMessage: () => { msgs += 1; },
    onStale: () => { staleHits += 1; }
  });
  stream.open();
  assert.equal(opened.length, 1);
  opened[0].readyState = 1;
  opened[0].onopen({});
  opened[0].onmessage({ data: "{}" });
  assert.equal(msgs, 1);
  // Liveness watchdog triggers stale + reopen
  fireAll(5000);
  assert.equal(staleHits, 1);
  fireAll(0);
  assert.equal(opened.length, 2);
  // Terminal error schedules exponential backoff
  opened[1].readyState = 2;
  opened[1].onerror({});
  fireAll(1000);
  assert.equal(opened.length, 3);
  // Hidden tab closes stream; visible reopens
  fakeDoc.visibilityState = "hidden";
  visListeners.forEach((fn) => fn());
  assert.equal(opened[2].readyState, 2);
  fakeDoc.visibilityState = "visible";
  visListeners.forEach((fn) => fn());
  assert.equal(opened.length, 4);
  stream.stop();

  // 3. hold.js: createHold & createRepeater auto-stop on blur/disabled
  let clockMs = 1000;
  let expiredKeys = [];
  const hTimers = new Map();
  let hNext = 1;
  const hSetT = (fn, ms) => { const id = hNext++; hTimers.set(id, { fn, ms }); return id; };
  const hClearT = (id) => { hTimers.delete(id); };
  const holdCtx = vm.createContext({ setTimeout: hSetT, clearTimeout: hClearT });
  vm.runInContext(read("hold.js"), holdCtx);
  const hold = holdCtx.ThermiteHold.createHold({
    now: () => clockMs, setTimeout: hSetT, clearTimeout: hClearT, duration: 500,
    onExpire: (k) => expiredKeys.push(k)
  });
  hold.set("vol", 42);
  assert.equal(hold.shown("vol", 10), 42);
  assert.equal(hold.match("vol", 42), true);
  assert.equal(hold.shown("vol", 10), 10);
  hold.set("vol", 55, 500);
  clockMs += 500;
  for (const [id, entry] of [...hTimers]) { hTimers.delete(id); entry.fn(); }
  assert.deepEqual(expiredKeys, ["vol"]);
  let fired = 0, blurCb = null;
  const fakeWin = { addEventListener(t, fn) { if (t === "blur") blurCb = fn; }, removeEventListener() {} };
  const targetBtn = { disabled: false, isConnected: true };
  const rep = holdCtx.ThermiteHold.createRepeater({
    delay: 200, interval: 50, setTimeout: hSetT, clearTimeout: hClearT,
    window: fakeWin, document: null,
    isValid: (b) => b.isConnected && !b.disabled,
    onFire: () => { fired += 1; }
  });
  rep.start(targetBtn);
  assert.equal(fired, 1);
  for (const [id, entry] of [...hTimers]) { hTimers.delete(id); entry.fn(); }
  assert.equal(fired, 2);
  blurCb();
  assert.equal(rep.active(), false);
  rep.dispose();

  // 4. remotes.js: layoutPlan() handles custom house layouts with zero edits to remotes.js
  const Remotes = require(path.join(REMOTE, "remotes.js"));
  const customHouse = {
    layouts: {
      remotes: {
        custom_deck: {
          label: "Custom Deck",
          corners: ["power", null, "back", "home"],
          playback: ["play", "pause"],
          keyGroups: [{ label: "Modes", keys: ["info", "options"], columns: 2, primary: true }]
        }
      }
    }
  };
  const plan = Remotes.layoutPlan("custom_deck", customHouse);
  assert.equal(plan.label, "Custom Deck");
  assert.deepEqual(plan.keys, ["info", "options"]);
  assert.ok(plan.buttons.includes("up") && plan.buttons.includes("info") && plan.buttons.includes("play"));
  assert.equal(Remotes.layoutPlan("pc", customHouse).label, "Keyboard and mouse");

  // 5. Stage 3a "partial" state in session.js, diagnostics.js, topology.js, and TUI
  const Shared = require(path.join(AREA, "tui/remote/shared.js"));
  const Main = require(path.join(AREA, "tui/remote/main.js"));
  const shared = Shared.load();
  Shared.mocks();
  const partialBackend = shared.Remote.create(shared.house, { backend: "mock", latency: 0 });
  const partialSession = shared.Session.create({ backend: partialBackend, house: shared.house, layouts: shared.layouts });
  await partialSession.start();
  const room0 = partialSession.rooms()[0];
  const pView = partialSession.view(room0, { room: room0.id, value: "partial", pending: null, status: "current", reason: "" });
  assert.equal(pView.led, "partial");
  assert.equal(pView.short, "Partially on");
  assert.ok(pView.problem.includes("Partially on"));
  const diag = globalThis.RemoteDiagnostics.create({ version: 1, rooms: { r: { label: "R", sources: {} } }, devices: {} });
  const dModel = diag.project("r", "partial", {}, Date.now(), null, 0, {});
  assert.equal(dModel.sections.path[0].value, "Partially on (incomplete route)");
  const WebTopo = require(path.join(REMOTE, "topology.js"));
  assert.equal(WebTopo.routeStatus({ routes: { r: { s: { label: "S" } } } }, "r", "s", { value: "partial" }, true).label, "Partially on");

  // 6. TUI Home alerts (mock firing alerts + zero network on --native)
  const alertApp = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), {
    alertsSample: "firing", setInterval: () => 0, clearInterval() {}
  });
  await alertApp.controller.start();
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
  const homeText = alertApp.controller.frame(120, 40).grid.text();
  assert.ok(homeText.includes("Alerts ·"), "TUI Home shows firing alerts banner");
  assert.ok(homeText.includes("!"), "TUI Home marks affected room with !");
  alertApp.controller.stop();
  let nativeAlertFetches = 0;
  const nativeApp = Main.assemble(Main.parse(["--url", "http://127.0.0.1:8080", "--native", "--still"]), {
    fetch: async (url) => {
      if (String(url).includes("/api/v1/alerts")) nativeAlertFetches += 1;
      return { ok: true, status: 200, headers: { get: () => "application/json", getSetCookie: () => [] }, text: async () => "[]", json: async () => [] };
    },
    EventSource: class { constructor() {} close() {} },
    setInterval: () => 0, clearInterval() {}
  });
  await nativeApp.controller.start();
  await new Promise((r) => setImmediate(r));
  assert.equal(nativeAlertFetches, 0, "--native makes zero alert requests");
  nativeApp.controller.stop();

  // 7. Web Home /api/v1/history/rooms + "partial" row rendering
  const homeDoc = new DOM.Document(read("home.html"));
  const END = 1793615400, START = END - 3600;
  const hCtx = vm.createContext({
    document: homeDoc,
    window: null,
    location: { search: "", origin: "https://site.example", protocol: "https:", pathname: "/" },
    URLSearchParams,
    localStorage: { getItem: () => null, setItem() {} },
    console, setTimeout, clearTimeout, addEventListener() {},
    ThermiteShell: { href: (id) => "/" + id + "/" },
    ThermiteNavigation: { pages: [], allowed: () => false },
    ThermiteAccess: {
      client: {
        native: false, mock: false,
        ready: async () => ({ id: 1, username: "Admin", can_edit: true, is_guest: false }),
        watchLocks() {},
        historyRooms: async () => ({
          version: 1, start: START, end: END, step: 60,
          labels: { off: "Off", example_player: "Player" },
          rooms: [{ room: "example_west", label: "Example West", segments: [[START, END, "example_player"]] }]
        })
      }
    },
    REMOTE_HOUSE: shared.house,
    RemoteSession: {
      NOT_CONNECTED: "Not connected",
      create: () => ({
        connected: () => true,
        view: () => ({ led: "partial", short: "Partially on", spoken: "partially on", connected: true }),
        roomLocked: () => ({ locked: false, text: "" })
      })
    },
    RemoteBackend: {
      create: () => ({
        connected: true,
        subscribe() { return () => {}; },
        getRooms: async () => shared.house.rooms.map((r) => ({ id: r.id, label: r.label, options: [] })),
        getState: async (id) => ({ room: id, value: "partial", pending: null, status: "current" }),
        setActive() {}
      })
    }
  });
  hCtx.window = hCtx;
  vm.runInContext(read("dom.js"), hCtx);
  vm.runInContext(read("home.js"), hCtx);
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  const histPanel = homeDoc.querySelector("[data-home-history]");
  assert.equal(histPanel.hidden, false, "Web Home unhides recent room history panel when gateway returns segments");
  assert.ok(histPanel.textContent.includes("Example West") && histPanel.textContent.includes("Player"));
  const firstRow = homeDoc.querySelector("[data-home-rooms] tr");
  assert.equal(firstRow.dataset.state, "partial");
  assert.equal(firstRow.querySelector(".thermite-room-state").textContent, "Partially on");

  // 8. Web Account /api/v1/tokens CRUD UI (create, reveal once, dismiss, revoke)
  const accDoc = new DOM.Document(read("access.html"));
  const mintedItems = [];
  const tokenClient = {
    native: false, mock: false,
    loadSecurity: async () => ({ enabled: true, bootstrap_required: false, guest_text: true, guest_cameras: false }),
    ready: async () => ({ id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false }),
    logout: async () => {},
    tokens: async () => mintedItems.slice(),
    createToken: async (name, days) => {
      const id = "tok-" + (mintedItems.length + 1);
      const row = { id, name, expires_at: "2027-01-01T00:00:00Z", revoked: false };
      mintedItems.push(row);
      const reply = { id, expires_at: row.expires_at };
      reply["tok" + "en"] = ["minted", id, String(days)].join("-");
      return reply;
    },
    revokeToken: async (id) => {
      const item = mintedItems.find((e) => e.id === id);
      if (item) item.revoked = true;
      return { ok: true };
    }
  };
  const accCtx = vm.createContext({
    document: accDoc, window: null,
    location: { search: "", origin: "https://site.example", reload() {} },
    URLSearchParams, console, setTimeout, clearTimeout,
    ThermiteAccess: { client: tokenClient },
    ThermiteNavigation: { href: () => "/security/" }
  });
  accCtx.window = accCtx;
  vm.runInContext(read("dom.js"), accCtx);
  vm.runInContext(read("access-ui.js"), accCtx);
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  assert.deepEqual(accDoc.querySelectorAll("[data-login] button").map((b) => b.textContent), ["Sign out"]);
  const tokenSection = accDoc.querySelector("[data-access-tokens]");
  assert.equal(tokenSection.hidden, false);
  const tForm = tokenSection.querySelector("[data-token-form]");
  tForm.elements = { name: { value: "Living room TUI" }, days: { value: "30" } };
  tForm.dispatch("submit");
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  const reveal = tokenSection.querySelector("[data-token-reveal]");
  assert.equal(reveal.hidden, false);
  assert.equal(reveal.querySelector("[data-token-value]").textContent, "minted-tok-1-30");
  // Dismiss wipes secret from DOM
  reveal.querySelectorAll("button").find((b) => b.textContent === "Dismiss").dispatch("click");
  assert.equal(reveal.hidden, true);
  assert.equal(reveal.querySelector("[data-token-value]").textContent, "");
  // Revoke removes token from active list
  const revokeBtn = tokenSection.querySelector("[data-tokens-list] button");
  assert.equal(revokeBtn.textContent, "Revoke");
  revokeBtn.dispatch("click");
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  assert.ok(tokenSection.querySelector("[data-tokens-list]").textContent.includes("No active personal tokens."));

  // 9. Vendor-neutral registration errors in activity.js
  const actEvents = [];
  const act = globalThis.RemoteActivity.create({
    items: { tv_pairing_result: { kind: "pairing_result", label: "TV", states: true } },
    emit: (ev) => actEvents.push(ev)
  });
  act.accept("openhab/items/tv_pairing_result/statechanged", JSON.stringify({
    oldValue: "", value: JSON.stringify({ state: "failed", error: { code: 1, http: 500 } })
  }));
  assert.equal(actEvents.length, 1);
  assert.ok(!actEvents[0].body.includes("Sony"), "activity.js must not hardcode Sony in error text");
  assert.ok(actEvents[0].body.includes("registration error 1, HTTP 500"));

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
