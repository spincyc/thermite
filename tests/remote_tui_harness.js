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
console.log = console.info = () => {};
const [WEST, EAST, DEN, LOUNGE] = process.argv.slice(2);
const ORIGIN = "http://openhab.example:8080", GATEWAY = "https://thermite.example:13601";

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
  site.EventSource = stream(() => null);
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
    if (route === "/api/v1/security") return json({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, guest_text: true, csrf: "C".repeat(32) });
    if (route === "/api/v1/session") return json({ csrf: "C".repeat(32) });
    if (route === "/api/v1/me") return json({ id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true,
      permissions: ["site.read", "room.control", "camera.view", "settings.manage", "users.manage", "pairing.manage"] });
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
  const parts = Main.assemble(Main.parse(args), { shared, setInterval: () => 0, clearInterval() {}, environ: {},
    screen: options.screen || "remote", onQuit: () => { parts.quit = true; }, ...(site ? { fetch: site.fetch, EventSource: site.EventSource } : {}) });
  parts.site = site;
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

// ---- the signal, and rooms of speakers alone -------------------------------

// Wide, Watch's signal panel shows and reads; a room of speakers alone has
// none, however it is reached: ↓ in the rooms, ], the rooms sheet, a click.
async function signal(proof, native) {
  size = [150, 50];
  const parts = await open({ native }, EAST);
  await until(parts, () => parts.diagnosed.includes(EAST) && text(parts).includes("signal & audio"), "the signal read");
  const walk = [];
  for (let i = 0; i < 4; i++) {
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
  click(parts, "room:" + WEST);
  await until(parts, () => parts.diagnosed.slice(-1)[0] === WEST, "the signal read again");
  proof.signal = { walk, diagnosed: [...new Set(parts.diagnosed)], shown: text(parts).includes("signal & audio") };
  // The backend's read of a room it does not know rejects; it never throws.
  const asked = Object.getPrototypeOf(parts.backend).getDiagnostics.call(parts.backend, DEN);
  proof.signal.unknown = [asked instanceof Promise, await asked.then(() => "resolved", (error) => error.message)];
  // A backend that throws all the same: the panel says the signal is unavailable.
  const read = text(parts).includes("Unavailable");
  parts.backend.getDiagnostics = () => { throw new Error("thrown"); };
  key(parts, null, { name: "tab" });
  click(parts, "room:" + EAST);
  await until(parts, () => text(parts).includes("Unavailable"), "the signal unavailable");
  proof.signal.thrown = [read, ...room(parts)];
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
  const row = parts.controller.lastFrame().grid.hits.find((hit) => hit.hit.key === "room:" + DEN);
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
  await terminal(proof);
  await signal(proof, true);
  await settings(proof);
  process.stdout.write(JSON.stringify(proof) + "\n");
})().then(() => process.exit(0), (error) => { process.stderr.write(error.stack + "\n"); process.exit(1); });
