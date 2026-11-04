/* Real shared System client and terminal, with finite in-memory transports. */
"use strict";
const assert = require("assert/strict"), fs = require("fs"), path = require("path"), vm = require("vm");
const System = require("../web/remote/system"), Navigation = require("../web/remote/navigation");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared");
console.log = console.info = () => {};
const tick = () => new Promise(resolve => setImmediate(resolve));
const manifest = { version: 1, runtime: { version: "5.2.3" }, tools: [
  { id: "openhab", label: "openHAB", description: "Native administration", scheme: "http", port: 15601, path: "/settings/", localOnly: false },
  { id: "grafana", label: "Grafana", description: "Dashboards", url: "http://site.example:15610/", localOnly: false },
  { id: "prometheus", label: "Prometheus", description: "Metrics", scheme: "http", port: 15606, path: "/", localOnly: true }
] };
const access = native => ({ native, ready: async () => {}, canEdit: () => false });
function response(value = manifest) {
  const bytes = new TextEncoder().encode(JSON.stringify(value)); let sent = false;
  return { ok: true, body: { cancel: async () => {}, getReader: () => ({
    read: async () => sent ? { done: true } : (sent = true, { done: false, value: bytes }),
    cancel: async () => {}, releaseLock() {}
  }) } };
}
(async () => {
  const model = System.catalogue(manifest, "https://site.example:15600");
  assert.equal(model.tools[0].url, "http://site.example:15601/settings/");
  assert.equal(model.tools[2].url, "http://127.0.0.1:15606/");
  assert.equal(model.tools[2].forwarding, "ssh -N -L 15606:127.0.0.1:15606 site.example");
  assert.equal(System.catalogue({ ...manifest, tools: [manifest.tools[0]] }, "http://127.0.0.1:15601").tools[0].url,
    "http://127.0.0.1:15601/settings/");
  assert.equal(System.catalogue({ ...manifest, tools: [{ ...manifest.tools[0], url: "/settings/" }] }, "http://site.example:15601").tools[0].url,
    "http://site.example:15601/settings/");
  for (const url of ["javascript:private-fixture", "data:text/html,private-fixture", "//outside.example/", "file:///private-fixture",
    "https://user:private-fixture@site.example/", "https://site.example/?token=private-fixture", "https://site.example/#private-fixture",
    "http://127.0.0.1:15606/", "http://[::1]/", "http://localhost/"]) {
    assert.throws(() => System.catalogue({ ...manifest, tools: [{ ...manifest.tools[0], url }] }, "https://site.example:15600"),
      error => error.message === "System information is unavailable.");
  }
  for (const tool of [{ ...manifest.tools[0], id: "unknown" }, { ...manifest.tools[0], port: 0 },
    { ...manifest.tools[0], path: "//outside.example/" }, { ...manifest.tools[0], label: {} },
    { ...manifest.tools[0], description: ["private-fixture"] }, { ...manifest.tools[0], label: "private\u202efixture" }]) {
    assert.throws(() => System.catalogue({ ...manifest, tools: [tool] }, "https://site.example:15600"));
  }
  let calls = 0, expire;
  const native = access(true); native.subscribe = fn => { expire = fn; };
  const client = System.create({ access: native, base: "https://site.example:15600", fetch: async (url, init) => {
    calls++; assert.equal(url, "https://site.example:15600/static/remote/system.json"); assert.equal(init.method, "GET");
    return response();
  } });
  assert.equal(calls, 0); await client.open(); assert.equal(calls, 1); assert(client.state.loaded);
  await tick(); assert.equal(calls, 1, "no polling or tool request");
  expire({ type: "unauthorized" }); assert.equal(client.state.manifest, null);
  const viewer = System.create({ access: access(false), base: "https://site.example:15600", fetch: () => { throw Error("viewer fetched private metadata"); } });
  await viewer.open(); assert(!viewer.state.loaded); assert(viewer.state.error.includes("Only administrators"));
  assert(!Navigation.allowed(Navigation.pages.find(page => page.id === "system"), access(false)));
  assert(Navigation.allowed(Navigation.pages.find(page => page.id === "system"), native));
  let release;
  const late = System.create({ access: native, base: "https://site.example:15600", fetch: () => new Promise(resolve => { release = resolve; }) });
  const pending = late.open(); await tick(); late.close(); release(response()); await pending;
  assert.equal(late.state.manifest, null); assert(!late.state.loading);
  let deadline, cancelled = 0;
  const slow = System.create({ access: native, base: "https://site.example:15600", fetch: () => new Promise(() => {}),
    setTimeout: (fn, ms) => { assert.equal(ms, System.TIMEOUT_MS); deadline = fn; return 1; }, clearTimeout: () => { cancelled++; } });
  const waiting = slow.open(); await tick(); deadline(); await waiting;
  assert(!slow.state.loading); assert.equal(slow.state.error, "System information is unavailable."); assert.equal(cancelled, 1);
  const unready = System.create({ access: { ...native, ready: () => new Promise(() => {}) }, base: "https://site.example:15600",
    fetch: () => { throw Error("authorization was not ready"); }, setTimeout: fn => { deadline = fn; return 2; }, clearTimeout() {} });
  const authorizing = unready.open(); await tick(); deadline(); await authorizing;
  assert(!unready.state.loading); assert.equal(unready.state.error, "System information is unavailable.");
  let bodyCancelled = false;
  const huge = System.create({ access: native, base: "https://site.example:15600", fetch: async () => ({ ok: true,
    body: { getReader: () => ({ read: async () => ({ value: new Uint8Array(System.MAX_BYTES + 1), done: false }),
      cancel: async () => { bodyCancelled = true; }, releaseLock() {} }) } }) });
  await huge.open(); assert(bodyCancelled); assert(!huge.state.loaded); assert.equal(huge.state.error, "System information is unavailable.");
  for (const kind of ["header", "status", "no-reader"]) {
    let rejectedBodyCancelled = false, readerOpened = false, textCalled = false;
    const rejected = { ok: kind !== "status", headers: { get: () => kind === "header" ? String(System.MAX_BYTES + 1) : null },
      body: { cancel: async () => { rejectedBodyCancelled = true; } },
      text: () => { textCalled = true; throw Error("unbounded text read"); } };
    if (kind !== "no-reader") rejected.body.getReader = () => { readerOpened = true; throw Error("rejected body read"); };
    const bounded = System.create({ access: native, base: "https://site.example:15600", fetch: async () => rejected });
    await bounded.open(); assert(rejectedBodyCancelled, kind); assert(!readerOpened, kind); assert(!textCalled, kind);
    assert(!bounded.state.loaded); assert.equal(bounded.state.error, "System information is unavailable.");
  }
  // Execute the browser's actual entry point, including hide/resume and the
  // collapsed disclosure, without a browser or any service request.
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.hidden = false; this.open = false; this.textContent = ""; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
  }
  const elements = Object.fromEntries(["status", "version", "tools", "advanced", "refresh"].map(id => [id, new Element()]));
  const browserEvents = {}, documentEvents = {}, browserCalls = [], browser = {
    URL, URLSearchParams, Uint8Array, TextEncoder, TextDecoder, AbortController, setTimeout, clearTimeout,
    location: { origin: "https://site.example:15600" }, ThermiteAccess: { client: native },
    fetch: async (url, init) => { browserCalls.push({ url, init }); return response(); },
    document: { body: { dataset: { thermitePage: "system" } }, visibilityState: "visible",
      createElement: () => new Element(), querySelector: selector => elements[selector.replace("[data-system-", "").replace("]", "")],
      addEventListener: (name, callback) => { documentEvents[name] = callback; } },
    addEventListener: (name, callback) => { browserEvents[name] = callback; }
  };
  vm.createContext(browser); vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/remote/system.js"), "utf8"), browser);
  await tick(); assert.equal(browserCalls.length, 1); assert.equal(elements.version.textContent, "5.2.3");
  assert.equal(elements.advanced.open, false); assert.equal(elements.advanced.hidden, false);
  const links = elements.tools.children.flatMap(row => row.children).filter(node => node.href);
  assert.deepEqual(links.map(node => node.href), ["http://site.example:15601/settings/", "http://site.example:15610/"]);
  elements.advanced.open = true; assert.equal(browserCalls.length, 1);
  browser.document.visibilityState = "hidden"; documentEvents.visibilitychange();
  assert.equal(elements.advanced.open, false); assert.equal(elements.tools.children.length, 0); assert(elements.status.textContent.includes("paused"));
  browser.document.visibilityState = "visible"; documentEvents.visibilitychange(); await tick(); browserEvents.pageshow(); await tick();
  assert.equal(browserCalls.length, 2); assert(browserCalls.every(call => call.url.endsWith("/system.json") && call.init.method === "GET"));
  browserEvents.pagehide();
  const opened = [], p = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), {
    systemManifest: manifest, openURL: url => { opened.push(url); return false; }, setInterval: () => 0, clearInterval() {}
  });
  let commands = 0;
  for (const method of ["choose", "press", "pressKey", "slide", "operate", "chooseInput", "allOff", "tapBubble"])
    p.session[method] = () => { commands++; return Promise.resolve(); };
  await p.controller.start();
  assert(!p.controller.context().system.backend.state.loaded);
  p.controller.key(null, { name: "f10" }); await tick(); let frame = p.controller.frame(149, 45);
  assert.equal(p.controller.ui.screen, "system"); assert(frame.grid.text().includes("5.2.3"));
  assert(frame.grid.text().includes("does not establish live system health"));
  assert(!frame.focusables.some(hit => hit.kind === "system-action" && hit.action === "open"));
  assert.equal(opened.length, 0);
  p.controller.key(null, { name: "down" }); p.controller.key(null, { name: "down" });
  assert.equal(p.controller.ui.focus, "system:advanced");
  p.controller.key(null, { name: "enter" }); assert(p.controller.context().system.expanded);
  p.controller.key(null, { name: "enter" }); assert(!p.controller.context().system.expanded);
  function click(key) {
    frame = p.controller.frame(149, 60);
    const hit = frame.grid.hits.find(entry => entry.hit.key === key); assert(hit, key);
    p.controller.mouse({ action: "mousedown", x: hit.x, y: hit.y });
  }
  click("system:advanced"); frame = p.controller.frame(149, 60);
  assert(frame.grid.text().includes("On the Thermite host"));
  assert(!frame.focusables.some(hit => hit.tool === "prometheus"));
  assert.equal(opened.length, 0);
  click("system:open:openhab"); await tick(); assert.deepEqual(opened, ["http://localhost:15601/settings/"]);
  for (const ch of ["+", "-", "p", "m", "1", "0"]) p.controller.key(ch, { name: ch });
  assert.equal(commands, 0);
  p.controller.key(null, { name: "f1" }); assert(!p.controller.context().system.expanded);
  assert.equal(p.controller.context().system.backend.state.manifest, null);
  p.access.canEdit = () => false; p.controller.key(null, { name: "f10" }); assert.equal(p.controller.ui.screen, "home");
  p.controller.stop();
  const nativeShared = Shared.load(), nativeAccess = nativeShared.Access.create({ native: true, base: "http://site.example:15601" });
  nativeShared.Access = { ...nativeShared.Access, create: () => nativeAccess };
  const nativeTui = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), { shared: nativeShared, setInterval: () => 0, clearInterval() {} });
  await nativeTui.controller.start(); nativeTui.controller.key(null, { name: "f10" }); await tick();
  assert.equal(nativeTui.controller.ui.screen, "system"); nativeTui.controller.stop();
  const shared = Shared.load(); shared.house = { ...shared.house, rooms: [], devices: {}, sources: {} };
  const empty = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), { shared, setInterval: () => 0, clearInterval() {} });
  await empty.controller.start(); assert(empty.controller.frame(100, 30).grid.text().includes("No rooms configured"));
  empty.controller.key(null, { name: "f10" }); await tick(); assert.equal(empty.controller.ui.screen, "system"); empty.controller.stop();
  // Browser Home's empty-room lifecycle also stays honest and issues no commands.
  const notice = { textContent: "" }, listeners = {};
  const context = { URLSearchParams, REMOTE_HOUSE: { rooms: [] }, location: { search: "" },
    RemoteBackend: { create: () => ({ subscribe() {}, setActive() {} }) },
    document: { visibilityState: "visible", querySelector: name => name === "[data-home-status]" ? notice : null,
      addEventListener: (name, fn) => { listeners[name] = fn; } }, addEventListener() {} };
  vm.createContext(context); vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/remote/home.js"), "utf8"), context);
  assert.equal(notice.textContent, "No rooms configured"); context.document.visibilityState = "hidden"; listeners.visibilitychange();
  assert.equal(notice.textContent, "No rooms configured");
  process.stdout.write(JSON.stringify({ pass: true, safeEndpoints: true, gatedMetadata: true, noAutomaticLaunch: true,
    cancelledReads: true, browserLifecycle: true, systemKeyboard: true, localToolsStayLocal: true, emptyRooms: true, commands }) + "\n");
})().then(() => process.exit(0), error => { process.stderr.write(error.stack + "\n"); process.exit(1); });
