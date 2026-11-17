"use strict";
// The terminal remote's Remotes screen and its other screens
// (tui/remote/controller.js, view.js), driven as a user drives them: keys
// and the mouse through controller.key() and mouse(), each frame drawn
// first. The house (THERMITE_REMOTE_HOUSE_FILE) has two routed rooms (WEST;
// EAST, which watches and listens) and two rooms of speakers alone (DEN,
// LOUNGE). On the mock, and through stand-ins on openHAB itself (--native)
// and on the gateway, whose backends read as the installed remote's do
// (the signal, a room's schedule). Prints what the screen drew and
// sent; nothing leaves the process. It waits for what it expects to draw,
// never for a fixed time.
const assert = require("node:assert/strict"), { EventEmitter } = require("node:events");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared"), App = require("../tui/remote/app");
const Cameras = require("../tui/remote/cameras");
console.log = console.info = () => {};
const [WEST, EAST, DEN, LOUNGE] = process.argv.slice(2);
const ORIGIN = "http://openhab.example:8080", GATEWAY = "https://thermite.example:13601";

// Two cameras for the mock's catalogue, which has none.
const camera = (id, label) => ({ id, label, thing: "unifi:camera:example:" + id, media: { snapshot: "/unifi/media/image/unifi:camera:example:" + id },
  items: { online: id + "_online", motion: id + "_motion", recording: id + "_recording" } });
const createCameras = Cameras.create;
Cameras.create = (options) => createCameras({ ...options, manifest: { version: 1, statusGroup: "house_camera_status", pollSeconds: 5,
  cameras: [camera("front_door", "Front Door"), camera("yard", "Yard")] } });
const reply = (status, body, type) => ({ ok: status >= 200 && status < 300, status, headers: { get: () => type || "text/plain", getSetCookie: () => [] },
  text: async () => body, json: async () => JSON.parse(body), clone() { return reply(status, body, type); } });
const json = (value, status = 200) => reply(status, JSON.stringify(value), "application/json");
// An event stream that opens, then says what first (a frame, or nothing).
function stream(first) {
  return class {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      setImmediate(() => {
        if (this.readyState === 2) return;
        this.readyState = 1;
        if (this.onopen) this.onopen({});
        const frame = first(url);
        if (frame && this.onmessage) this.onmessage({ data: JSON.stringify(frame) });
      });
    }
    close() { this.readyState = 2; }
  };
}

// openHAB's REST API and event stream as the native backend reads them:
// each routed room's Source Item, a diagnostics manifest with no readings,
// every request recorded.
function openhab(house) {
  const site = { calls: [] };
  site.fetch = async (url, init = {}) => {
    const target = new URL(url), method = (init.method || "GET").toUpperCase();
    site.calls.push(method + " " + target.pathname);
    if (method === "GET" && target.pathname === "/rest/items") return json(house.rooms.map((room) => ({ name: "loc_" + room.id + "_source", state: "off" })));
    if (target.pathname === "/static/remote/diagnostics.json") return json({ version: 1, rooms: {}, devices: {} });
    return reply(method === "POST" ? 200 : 404, "");
  };
  // Its streams kept, for an event sent while the remote runs.
  site.sources = [];
  const Source = stream(() => null);
  site.EventSource = class extends Source { constructor(url) { super(url); site.sources.push(this); } };
  site.send = (item, value) => site.sources.forEach((source) => source.onmessage && source.onmessage({ data: JSON.stringify({
    topic: "openhab/items/" + item + "/stateupdated", type: "ItemStateUpdatedEvent", payload: JSON.stringify({ type: "String", value }) }) }));
  return site;
}

// The gateway as an administrator's terminal reads it: authentication on,
// every routed room unlocked with a policy, no other room known, the remote
// view's stream a snapshot of each room's Source; every request recorded.
function gateway(house) {
  const site = { calls: [] };
  const routed = (id) => house.rooms.some((room) => room.id === id);
  const policy = (room) => ({ room, revision: 0, config: null, manual_lock: null, can_edit: true, can_lock: true, enforcement: { state: "unknown" },
    effective: { allowed: true, locked: false, reason: "unlocked", desired: null, until: null, evaluated_at: String(Date.now()) } });
  site.fetch = async (url, init = {}) => {
    const target = new URL(url), route = target.pathname, method = (init.method || "GET").toUpperCase();
    site.calls.push(method + " " + route);
    // A sign-in ended (site.expired): every request refused.
    if (site.expired) return json({ error: "unauthorized" }, 401);
    if (route === "/api/v1/security") return json({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, guest_text: true, csrf: "C".repeat(32) });
    if (route === "/api/v1/session") return json({ csrf: "C".repeat(32) });
    if (route === "/api/v1/me") return json({ id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true,
      permissions: ["site.read", "room.control", "camera.view", "settings.manage", "users.manage", "pairing.manage"] });
    if (route === "/api/v1/users") return json({ users: [{ id: 1, username: "Admin", admin: true, active: true }, { id: 2, username: "example", admin: false, active: true }] });
    if (route === "/api/v1/locks") return json({ rooms: Object.fromEntries(house.rooms.map((room) => [room.id, { locked: false, manual: false, until: null, reason: "unlocked" }])) });
    const match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/(policy|commands)$/.exec(route);
    if (match && routed(match[1])) return match[2] === "policy" ? json(policy(match[1])) : json({ accepted: true }, 202);
    return json({ error: "not_found" }, 404);
  };
  site.EventSource = stream((url) => (url.endsWith("/api/v1/events/remote") ? { type: "snapshot", view: "remote",
    states: Object.fromEntries(house.rooms.map((room) => ["loc_" + room.id + "_source", "off"])) } : null));
  return site;
}

// The remote on the mock, on openHAB itself (native) or on the gateway, the
// rooms each signal read asked for recorded (diagnosed) and the read still made.
function assemble(options = {}) {
  const shared = Shared.load();
  const site = options.native ? openhab(shared.house) : options.gateway ? gateway(shared.house) : null;
  const args = !site ? ["--mock", "--still", "--latency", "0"] : ["--url", options.native ? ORIGIN : GATEWAY, "--still"].concat(options.native ? ["--native"] : []);
  const opened = [];
  const parts = Main.assemble(Main.parse(args), { shared, setInterval: () => 0, clearInterval() {}, environ: {}, ...(options.store ? { store: options.store } : {}),
    screen: options.screen || "remote", onQuit: () => { parts.quit = true; }, openURL: (url) => { opened.push(url); return false; },
    ...(site ? { fetch: site.fetch, EventSource: site.EventSource } : {}) });
  parts.opened = opened;
  parts.site = site;
  // What the remote's buttons, choices and volume sent, as [room or device, what, value].
  parts.sent = [];
  const record = (name, entry) => {
    const send = parts.backend[name].bind(parts.backend);
    parts.backend[name] = (...args) => { parts.sent.push(entry(...args)); return send(...args); };
  };
  record("press", (room, device, button) => [device, button]);
  record("choose", (room, source) => [room, "source", source]);
  record("setVolume", (room, value) => [room, "volume", value]);
  record("setInput", (room, device, value) => [device, "input", value]);
  record("operate", (room, control, action) => [room, control, action]);
  record("allOff", () => ["all", "off"]);
  parts.diagnosed = [];
  if (parts.backend.getDiagnostics) {
    const read = parts.backend.getDiagnostics.bind(parts.backend);
    parts.backend.getDiagnostics = (room, ...rest) => { parts.diagnosed.push(room); return read(room, ...rest); };
  }
  return parts;
}

let size = [110, 50];
const draw = (parts) => parts.controller.frame(size[0], size[1]);
const text = (parts) => draw(parts).grid.text();
// Polls until the predicate holds (drawing first, as the terminal would); fails after a while.
async function until(parts, predicate, what) {
  for (let i = 0; i < 400; i++) {
    draw(parts);
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("Never: " + what + "\n" + text(parts));
}
function key(parts, ch, event) {
  draw(parts);
  parts.controller.key(ch, event || { name: ch });
  draw(parts);
  return parts.controller.ui.screen;
}
// A click on the hit of this key.
function click(parts, hitKey, button = "left") {
  const entry = draw(parts).grid.hits.find((hit) => hit.hit.key === hitKey);
  assert.ok(entry, "no " + hitKey);
  parts.controller.mouse({ action: "mousedown", button, x: entry.x, y: entry.y });
  parts.controller.mouse({ action: "mouseup", button, x: entry.x, y: entry.y });
  draw(parts);
  return entry;
}
// The arrows reach the control: up to the first, then down to it.
function focusOn(parts, target) {
  for (let i = 0; i < 60; i++) key(parts, null, { name: "up" });
  for (let i = 0; i < 60 && parts.controller.ui.focus !== target; i++) key(parts, null, { name: "down" });
  assert.equal(parts.controller.ui.focus, target);
}
async function open(options, room) {
  const parts = assemble(options);
  await parts.controller.start(room);
  await until(parts, () => parts.controller.ui.ready, "ready");
  return parts;
}
const room = (parts) => [parts.session.current().id, parts.session.mode()];

const toast = (parts) => parts.controller.ui.toast && [parts.controller.ui.toast.title, parts.controller.ui.toast.body];
const footer = (parts) => text(parts).split("\n").slice(-1)[0];
const sent = (parts, from) => parts.sent.slice(from);

// ---- Watch: panes, rooms, sources, the remote's keys ----------------------

async function watch(proof) {
  size = [150, 50];
  const parts = await open({}, WEST);
  const { controller, session } = parts;
  const ui = controller.ui;
  proof.watch = { start: [ui.pane, ui.focus], footer: footer(parts) };
  // Tab and Shift-Tab go round the panes.
  const panes = [];
  for (let i = 0; i < 6; i++) { key(parts, null, { name: "tab" }); panes.push(ui.pane); }
  key(parts, null, { name: "tab", shift: true });
  proof.watch.panes = [panes, ui.pane];
  // The rooms: ↓ and ↑ choose the room; ← → a source, Enter switches to it.
  for (let i = 0; i < 6 && ui.pane !== "rooms"; i++) key(parts, null, { name: "tab" });
  key(parts, null, { name: "down" });
  const down = session.current().id;
  key(parts, null, { name: "up" });
  key(parts, null, { name: "right" });
  key(parts, null, { name: "right" });
  key(parts, null, { name: "left" });
  const cursor = ui.paneFocus.sources;
  key(parts, null, { name: "enter" });
  await until(parts, () => session.state(WEST).value === "example_player", "the player");
  proof.watch.rooms = { down, up: session.current().id, cursor, sent: sent(parts, 0) };
  // Digits: 0 Off, 1-9 a source.
  let from = parts.sent.length;
  key(parts, "2");
  await until(parts, () => session.state(WEST).value === "example_computer", "the computer");
  key(parts, "0");
  await until(parts, () => session.state(WEST).value === "off", "off");
  key(parts, "7");
  proof.watch.digits = sent(parts, from);
  key(parts, "1");
  await until(parts, () => session.state(WEST).value === "example_player", "the player again");
  // The remote: the arrows its D-pad, Enter OK, Backspace and Esc back, the page's keys.
  for (let i = 0; i < 6 && ui.pane !== "remote"; i++) key(parts, null, { name: "tab" });
  from = parts.sent.length;
  for (const name of ["up", "down", "left", "right", "enter", "space", "backspace", "escape"]) key(parts, null, { name });
  for (const ch of ["p", "+", "-", "m", "<", ">"]) key(parts, ch);
  proof.watch.remote = sent(parts, from).map((entry) => entry[1]);
  // Esc and Backspace off the remote press nothing.
  for (let i = 0; i < 6 && ui.pane !== "rooms"; i++) key(parts, null, { name: "tab" });
  from = parts.sent.length;
  key(parts, null, { name: "escape" });
  key(parts, null, { name: "backspace" });
  proof.watch.quiet = sent(parts, from);
  parts.controller.stop();
  size = [110, 50];
}

// ---- Watch: its sheets and the terminal's own keys --------------------------

async function sheets(proof) {
  size = [110, 50];
  const parts = await open({}, WEST);
  const { controller, session, store } = parts;
  const ui = controller.ui;
  const sheet = () => ui.overlay && ui.overlay.kind;
  proof.sheets = {};
  // r, i, d, h and ? each open their sheet; Esc closes it, back on the pane it was opened from.
  for (const ch of ["r", "i", "d", "h", "?"]) {
    key(parts, ch);
    const opened = [sheet(), ui.pane, ui.focus];
    key(parts, null, { name: "escape" });
    proof.sheets[ch] = opened.concat([sheet(), ui.pane]);
  }
  // Info's controls, each reached with the arrows and pressed with Enter.
  key(parts, "i");
  const mode = session.volumeMode();
  focusOn(parts, "volume-mode");
  key(parts, null, { name: "enter" });
  focusOn(parts, "control:screen:down");
  key(parts, null, { name: "enter" });
  focusOn(parts, "simulate-matrix");
  key(parts, null, { name: "space" });
  await until(parts, () => parts.sent.length, "the screen lowered");
  proof.sheets.info = { mode: [mode, session.volumeMode()], sent: parts.sent.slice(), answering: session.isAnswering(), sheet: sheet() };
  key(parts, null, { name: "enter" });
  key(parts, null, { name: "escape" });
  // The rooms sheet: ↓ chooses the next room at once, Enter goes to its remote.
  key(parts, "r");
  key(parts, null, { name: "down" });
  const chosen = session.current().id;
  key(parts, null, { name: "enter" });
  proof.sheets.rooms = [chosen, sheet(), ui.pane];
  key(parts, "[");
  // The help: Watch's keys, the pages', the terminal's.
  key(parts, "?");
  const help = text(parts);
  proof.sheets.help = ["Remote controls", "Source", "Watch or listen", "Signal & audio", "Rooms through the day", "Volume slider",
    "Navigation", "Pair devices", "Terminal", "Motion", "Log pane", "Mouse"].filter((words) => !help.includes(words));
  key(parts, null, { name: "escape" });
  // v, t, a and L: the volume slider, the theme, motion and the log, each kept.
  const before = [session.volumeMode(), ui.theme, ui.still, ui.logVisible];
  for (const ch of ["v", "t", "a", "L"]) key(parts, ch);
  proof.sheets.toggles = [before, [session.volumeMode(), ui.theme, ui.still, ui.logVisible],
    [store.get("remote.volume"), store.get("remote.theme"), store.get("remote.still"), store.get("remote.logVisible")]];
  for (const ch of ["v", "t", "a", "L"]) key(parts, ch);
  // q quits.
  key(parts, "q");
  proof.sheets.quit = Boolean(parts.quit);
  size = [110, 50];
}

// ---- Watch with the mouse ---------------------------------------------------

async function mouse(proof) {
  size = [110, 50];
  const parts = await open({}, WEST);
  const { controller, session } = parts;
  const ui = controller.ui;
  const hits = () => draw(parts).grid.hits;
  const at = (hitKey, dx = 0, action = "mousedown", button = "left") => {
    const entry = hits().find((hit) => hit.hit.key === hitKey);
    assert.ok(entry, "no " + hitKey);
    controller.mouse({ action, button, x: entry.x + dx, y: entry.y });
    draw(parts);
    return entry;
  };
  proof.mouse = {};
  // A source's bubble switches the room; a device's brings up its buttons.
  click(parts, "bubble:example_player");
  await until(parts, () => session.state(WEST).value === "example_player", "the player");
  click(parts, "bubble:example_tv");
  proof.mouse.bubbles = [parts.sent.slice(), session.target().id];
  // The TV's Input key lists its inputs; ↓ and Enter choose one.
  click(parts, "key:input");
  proof.mouse.inputs = [ui.overlay && ui.overlay.kind, ui.focus];
  key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  proof.mouse.inputs.push(ui.overlay, parts.sent.filter((entry) => entry[1] === "input"));
  // A key held repeats until let go.
  let from = parts.sent.length;
  at("key:volume_up");
  await until(parts, () => sent(parts, from).length >= 3, "the held key repeating");
  at("key:volume_up", 0, "mouseup");
  const held = sent(parts, from).length;
  await new Promise((resolve) => setTimeout(resolve, 600));
  proof.mouse.held = [sent(parts, from).every((entry) => entry[1] === "volume_up"), sent(parts, from).length === held];
  // The slider: dragged to its end and let go; the wheel steps it.
  key(parts, "v");
  from = parts.sent.length;
  const slider = at("slider");
  controller.mouse({ action: "mousemove", button: "left", x: slider.x + slider.w - 1, y: slider.y });
  controller.mouse({ action: "mouseup", button: "left", x: slider.x + slider.w - 1, y: slider.y });
  await until(parts, () => sent(parts, from).some((entry) => entry[2] === 100), "the volume at its end");
  controller.mouse({ action: "wheeldown", x: slider.x + 2, y: slider.y });
  await until(parts, () => sent(parts, from).some((entry) => entry[2] === 99), "the wheel's step");
  proof.mouse.slider = [ui.focus, sent(parts, from).slice(-1)[0]];
  key(parts, "v");
  // All off takes a second click.
  from = parts.sent.length;
  click(parts, "all-off");
  const armed = [session.allOffArmed(), text(parts).includes("Press again to confirm"), sent(parts, from).length];
  click(parts, "all-off");
  await until(parts, () => sent(parts, from).length, "all off");
  proof.mouse.allOff = [armed, sent(parts, from).filter((entry) => entry[0] === "all")];
  // The header: the theme (where it has room for it), a page's tab, the Remotes menu.
  const theme = ui.theme;
  size = [150, 50];
  click(parts, "theme");
  size = [110, 50];
  proof.mouse.header = [theme !== ui.theme];
  click(parts, "tab:home");
  proof.mouse.header.push(ui.screen);
  // Home lists Watch's rooms; its switch lists Listen's, as the remote's does.
  const listed = () => draw(parts).grid.hits.filter((hit) => hit.hit.kind === "home-room").map((hit) => hit.hit.room);
  proof.mouse.home = [session.mode(), listed()];
  click(parts, "home:mode:listen");
  proof.mouse.home.push(session.mode(), listed());
  click(parts, "home-room:" + DEN);
  proof.mouse.header.push(ui.screen, ...room(parts));
  click(parts, "tab:remote");
  proof.mouse.header.push(ui.overlay && ui.overlay.kind);
  click(parts, "menu:pairing");
  proof.mouse.header.push(ui.screen);
  key(parts, null, { name: "escape" });
  proof.mouse.header.push(ui.screen);
  controller.stop();
}

// Narrow: the rooms and Info are sheets the header opens, and the sources
// a row the arrows at its ends, and the wheel, scroll.
async function narrow(proof) {
  size = [70, 40];
  const parts = await open({}, WEST);
  const { controller, session } = parts;
  const ui = controller.ui;
  proof.narrow = { start: ui.overlay && ui.overlay.kind, header: {} };
  // The header's chips for the rooms and Info, each whole, before Pair devices.
  for (const width of [60, 70, 90]) {
    const frame = parts.controller.frame(width, 40);
    proof.narrow.header[width] = [frame.grid.text().split("\n")[0].trimEnd().split("   ").slice(-2),
      frame.grid.hits.filter((hit) => hit.y === 0 && /^(open|remotes):/.test(hit.hit.key)).map((hit) => hit.hit.key)];
  }
  key(parts, null, { name: "escape" });
  click(parts, "open:info");
  proof.narrow.info = ui.overlay && ui.overlay.kind;
  key(parts, null, { name: "escape" });
  click(parts, "open:rooms");
  proof.narrow.rooms = ui.overlay && ui.overlay.kind;
  click(parts, "room:" + EAST);
  proof.narrow.chosen = [session.current().id, ui.overlay];
  key(parts, "[");
  size = [40, 40];
  const offset = () => ui.bubbles.offset || 0;
  const before = offset();
  click(parts, "scroll-bubbles:1");
  const clicked = offset();
  const row = draw(parts).grid.hits.find((hit) => hit.hit.kind === "bubbles-row");
  controller.mouse({ action: "wheelup", x: row.x + 3, y: row.y });
  proof.narrow.bubbles = [before, clicked > before, offset() < clicked];
  controller.stop();
  size = [110, 50];
}

// ---- the other screens --------------------------------------------------------

// Home, Cameras, Site and Pair devices from the keys, from a room of
// speakers alone; Esc goes Home from a page, back to Remotes from Pairing.
async function pages(proof) {
  size = [110, 50];
  const parts = await open({ screen: "home" }, LOUNGE);
  const { controller, session } = parts;
  const ui = controller.ui;
  const sheet = () => ui.overlay && ui.overlay.kind;
  proof.pages = { home: [ui.screen, ui.focus] };
  // Home: the arrows choose, Enter opens; h is the day.
  key(parts, null, { name: "enter" });
  proof.pages.home.push(ui.screen, ...room(parts));
  key(parts, null, { name: "f1" });
  // From a room of speakers alone Home lists Listen's rooms; y lists Watch's.
  key(parts, "y");
  proof.pages.home.push(session.mode());
  focusOn(parts, "home-room:" + WEST);
  key(parts, null, { name: "enter" });
  proof.pages.home.push(ui.screen, ...room(parts));
  key(parts, null, { name: "f1" });
  key(parts, "h");
  proof.pages.home.push(sheet());
  key(parts, null, { name: "escape" });
  key(parts, null, { name: "escape" });
  proof.pages.home.push(ui.screen);
  session.selectRoom(LOUNGE, true);
  // Cameras: the arrows choose one, Enter opens it in the browser.
  key(parts, null, { name: "f4" });
  await until(parts, () => String(ui.focus).startsWith("camera:"), "the cameras");
  const first = ui.focus;
  key(parts, null, { name: "down" });
  const second = ui.focus;
  key(parts, null, { name: "enter" });
  await until(parts, () => toast(parts), "the camera opened");
  proof.pages.cameras = [first !== second, parts.opened.slice(-1)[0].includes("/cameras/"), toast(parts) && toast(parts)[0]];
  key(parts, null, { name: "escape" });
  proof.pages.cameras.push(ui.screen);
  // Site from a room of speakers alone: the topology's first room; the arrows step its rooms and sources, s sorts, Enter opens the map.
  key(parts, null, { name: "f5" });
  await until(parts, () => controller.context().topology.source, "the topology");
  const topology = controller.context().topology;
  const start = [topology.room, topology.source];
  key(parts, null, { name: "down" });
  const next = topology.room;
  key(parts, null, { name: "right" });
  const source = topology.source;
  const sort = [topology.sortField, topology.sortDescending];
  key(parts, "s");
  key(parts, "S");
  key(parts, null, { name: "enter" });
  await until(parts, () => parts.opened.some((url) => url.includes("/site/") || url.includes("topology")), "the map opened");
  proof.pages.topology = { start, moved: next !== start[0], source: source !== start[1],
    sort: [sort, [topology.sortField, topology.sortDescending]], opened: parts.opened.slice(-1)[0] };
  // F6 opens Pair devices only from Remotes; Ctrl-P too. Esc goes back to Remotes.
  key(parts, null, { name: "f6" });
  proof.pages.pairing = [ui.screen];
  key(parts, null, { name: "f2" });
  key(parts, null, { name: "p", ctrl: true });
  proof.pages.pairing.push(ui.screen);
  await until(parts, () => controller.pairing.loaded && controller.pairing.selected, "the devices");
  // ↑ ↓ choose a device; Enter asks it for a code, typed (what it does not take ignored, Backspace a slip) and sent with Enter.
  const pairing = controller.pairing, PLAYER = "example_player";
  for (let i = 0; i < 4 && pairing.selected !== PLAYER; i++) key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  await until(parts, () => pairing.backend.view(PLAYER).form, "the code's field");
  for (const ch of ["1", "2", "3", "x", "5"]) key(parts, ch);
  key(parts, null, { name: "backspace" });
  key(parts, "4");
  const code = pairing.codes[PLAYER];
  key(parts, null, { name: "enter" });
  await until(parts, () => pairing.backend.device(PLAYER).status === "paired", "paired");
  // s sorts by the next column, S reverses it.
  key(parts, "s");
  key(parts, "S");
  proof.pages.paired = [code, pairing.backend.device(PLAYER).status, pairing.sortField, pairing.sortDescending];
  key(parts, null, { name: "escape" });
  proof.pages.pairing.push(ui.screen, ...room(parts));
  controller.stop();
}

// ---- start-up on a room of speakers alone -------------------------------------

// The room kept from the last run is a room of speakers alone. Wide enough for
// the signal beside the remote, it starts on Listen, whose rooms the list
// holds, and reads no signal; d has none to show. y goes on to Watch's
// first room, reads its signal, and the list shows a routed room's Source
// that changed meanwhile. On the gateway, Room settings started on it
// opens on a routed room.
async function startup(proof) {
  size = [150, 50];
  const kept = (room) => { const data = { "remote.room": room }; return { get: (key) => data[key] ?? null, set: (key, value) => { data[key] = value; } }; };
  let parts = await open({ native: true, store: kept(DEN) });
  const { controller, session } = parts;
  proof.startup = { start: [...room(parts), parts.diagnosed.slice()] };
  await until(parts, () => parts.site.sources.length, "the stream");
  parts.site.send("loc_" + EAST + "_source", "example_computer");
  await until(parts, () => session.state(EAST).value === "example_computer", "the east room's Source");
  proof.startup.stream = [...room(parts), session.listed().map((entry) => entry.id)];
  key(parts, "d");
  proof.startup.signal = [controller.ui.overlay && controller.ui.overlay.kind, controller.ui.pane, toast(parts)];
  key(parts, null, { name: "escape" });
  size = [110, 50];
  draw(parts);
  key(parts, "d");
  proof.startup.signal.push(controller.ui.overlay && controller.ui.overlay.kind, controller.ui.pane, parts.diagnosed.slice());
  // openHAB has not said which input the speaker is on.
  proof.startup.input = [text(parts).split("\n").map((line) => (/\S+ \u25be/.exec(line) || [""])[0]).find(Boolean),
    draw(parts).grid.hits.find((hit) => hit.hit.kind === "listen-inputs").hit.label];
  key(parts, null, { name: "escape" });
  size = [150, 50];
  key(parts, "y");
  await until(parts, () => parts.diagnosed.length, "the west room's signal");
  proof.startup.walk = [...room(parts), parts.diagnosed.slice(), text(parts).split("\n").some((line) => /Example East +Computer/.test(line))];
  controller.stop();
  parts = await open({ gateway: true, screen: "settings", store: kept(LOUNGE) });
  await until(parts, () => text(parts).includes("Edit schedule"), "the schedule");
  proof.startup.settings = [parts.controller.ui.screen, text(parts).includes("Example West"),
    parts.site.calls.filter((call) => call.endsWith("/policy")).filter((call, index, all) => all.indexOf(call) === index)];
  parts.controller.stop();
}

// ---- the gateway's pages -------------------------------------------------------

// An administrator on the gateway, from a room of speakers alone: F3
// Security, F8 Users (a user's form from the arrows and Enter, Esc out of
// it), F9 Account. A sign-in that ends puts the remote on Account, saying
// so; Ctrl-C quits.
async function admin(proof) {
  size = [110, 50];
  const parts = await open({ gateway: true }, LOUNGE);
  const { controller } = parts;
  const ui = controller.ui;
  proof.admin = {};
  key(parts, null, { name: "f3" });
  await until(parts, () => text(parts).includes("Security"), "Security");
  proof.admin.security = ui.screen;
  key(parts, null, { name: "f8" });
  await until(parts, () => text(parts).includes("+ Add user"), "the users");
  focusOn(parts, "access:edit-user:2");
  key(parts, null, { name: "enter" });
  proof.admin.users = [ui.screen, ui.overlay && ui.overlay.kind, text(parts).includes("Username: example")];
  key(parts, null, { name: "escape" });
  proof.admin.users.push(ui.overlay);
  key(parts, null, { name: "f9" });
  proof.admin.account = ui.screen;
  key(parts, null, { name: "f2" });
  parts.site.expired = true;
  parts.session.selectRoom(WEST, true);
  key(parts, "1");
  await until(parts, () => ui.screen === "account", "the sign-in ended");
  proof.admin.ended = [ui.screen, ui.error, text(parts).includes("thermite access login")];
  key(parts, null, { name: "c", ctrl: true });
  proof.admin.quit = Boolean(parts.quit);
  controller.stop();
}

// ---- the signal, and rooms of speakers alone -------------------------------

// Wide, Watch's signal panel shows and reads; its ↓ walks the rooms with a
// screen. Listen's rooms, a room of speakers alone among them, read none,
// however they are reached: y, ↓ in the rooms, ], the rooms sheet, a click.
// y again: Watch, on the room last in it, which reads again.
async function signal(proof, native) {
  size = [150, 50];
  const parts = await open({ native }, EAST);
  await until(parts, () => parts.diagnosed.includes(EAST) && text(parts).includes("signal & audio"), "the signal read");
  const walk = [];
  key(parts, null, { name: "down" });
  walk.push(room(parts));
  key(parts, "y");
  walk.push(room(parts));
  for (let i = 0; i < 3; i++) {
    key(parts, null, { name: "down" });
    walk.push(room(parts));
  }
  key(parts, "]");
  walk.push(room(parts));
  key(parts, "r");
  key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  walk.push(room(parts));
  key(parts, null, { name: "tab" });
  click(parts, "room:" + EAST);
  click(parts, "room:" + DEN);
  walk.push(room(parts));
  const read = parts.diagnosed.length;
  key(parts, "y");
  await until(parts, () => parts.diagnosed.length > read && parts.diagnosed.slice(-1)[0] === WEST, "the signal read again");
  walk.push(room(parts));
  proof.signal = { walk, diagnosed: [...new Set(parts.diagnosed)], shown: text(parts).includes("signal & audio") };
  // The backend's read of a room it does not know rejects; it never throws.
  const asked = Object.getPrototypeOf(parts.backend).getDiagnostics.call(parts.backend, DEN);
  proof.signal.unknown = [asked instanceof Promise, await asked.then(() => "resolved", (error) => error.message)];
  // A backend that throws all the same: the panel says the signal is unavailable.
  const unavailable = text(parts).includes("Unavailable");
  parts.backend.getDiagnostics = () => { throw new Error("thrown"); };
  key(parts, null, { name: "tab" });
  click(parts, "room:" + EAST);
  await until(parts, () => text(parts).includes("Unavailable"), "the signal unavailable");
  proof.signal.thrown = [unavailable, ...room(parts)];
  parts.controller.stop();
  size = [110, 50];
}

// ---- room settings from a room of speakers alone ---------------------------

// F7 from a room of speakers alone: a routed room's schedule (a speaker's
// room has none), which an administrator edits.
async function settings(proof) {
  const parts = await open({ gateway: true }, DEN);
  key(parts, null, { name: "f7" });
  await until(parts, () => text(parts).includes("Edit schedule"), "the schedule");
  const shown = text(parts);
  focusOn(parts, "access:edit-policy:");
  key(parts, null, { name: "enter" });
  proof.settings = { room: [shown.includes("Example West"), parts.session.current().id], sheet: parts.controller.ui.overlay && parts.controller.ui.overlay.kind,
    form: text(parts).includes("Schedule enabled"), read: parts.site.calls.filter((call) => call.endsWith("/policy")).filter((call, index, all) => all.indexOf(call) === index) };
  key(parts, null, { name: "escape" });
  proof.settings.closed = parts.controller.ui.overlay;
  parts.controller.stop();
}

// ---- the terminal ----------------------------------------------------------

// app.js on a stand-in for blessed's program: a key or a click the remote
// fails on is said and kept, a frame that fails is kept, and the terminal
// goes on.
async function terminal(proof) {
  const parts = await open({}, WEST);
  const program = Object.assign(new EventEmitter(), { cols: 110, rows: 50, destroy() {} });
  const kept = [];
  const app = App.run({ blessed: { program: () => program }, controller: parts.controller, env: {},
    output: { columns: 110, rows: 50, write: () => true }, onError: (error) => kept.push(error.message) });
  const press = (ch, name) => program.emit("keypress", ch, { name: name || ch });
  const stepRoom = parts.session.stepRoom, selectRoom = parts.session.selectRoom;
  parts.session.stepRoom = () => { throw new Error("no step"); };
  press("]");
  const toast = () => parts.controller.ui.toast && [parts.controller.ui.toast.title, parts.controller.ui.toast.body];
  proof.terminal = { key: toast() };
  parts.session.stepRoom = stepRoom;
  press("]");
  proof.terminal.after = parts.session.current().id;
  parts.session.selectRoom = () => { throw new Error("no room"); };
  parts.controller.ui.toast = null;
  const row = parts.controller.lastFrame().grid.hits.find((hit) => hit.hit.key === "room:" + WEST);
  program.emit("mouse", { action: "mousedown", button: "left", x: row.x, y: row.y });
  proof.terminal.click = toast();
  parts.session.selectRoom = selectRoom;
  const frame = parts.controller.frame;
  parts.controller.frame = () => { throw new Error("no frame"); };
  const draws = app.draws();
  app.draw();
  parts.controller.frame = frame;
  app.draw();
  proof.terminal.kept = kept;
  proof.terminal.draws = app.draws() - draws;
  app.destroy();
  parts.controller.stop();
}

(async () => {
  const proof = {};
  await watch(proof);
  await sheets(proof);
  await mouse(proof);
  await narrow(proof);
  await pages(proof);
  await startup(proof);
  await admin(proof);
  await terminal(proof);
  await signal(proof, true);
  await settings(proof);
  process.stdout.write(JSON.stringify(proof) + "\n");
})().then(() => process.exit(0), (error) => { process.stderr.write(error.stack + "\n"); process.exit(1); });
