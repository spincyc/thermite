"use strict";
// The terminal remote's Remotes screen and its other screens
// (tui/remote/controller.js, view.js), driven as a user drives them: keys
// and the mouse through controller.key() and mouse(), each frame drawn
// first. The house (THERMITE_REMOTE_HOUSE_FILE) has two routed rooms (WEST;
// EAST, which watches and listens) and two rooms of speakers alone (DEN,
// LOUNGE). On the mock, and on openHAB itself (--native) through a
// stand-in for its REST API and event stream, whose backend reads the
// signal as the installed remote's does. Prints what the screen drew and
// sent; nothing leaves the process. It waits for what it expects to draw,
// never for a fixed time.
const assert = require("node:assert/strict");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared");
console.log = console.info = () => {};
const [WEST, EAST, DEN, LOUNGE] = process.argv.slice(2);
const ORIGIN = "http://openhab.example:8080";

// openHAB's REST API and event stream as the native backend reads them:
// each routed room's Source Item, a diagnostics manifest with no readings,
// every request recorded.
function openhab(house) {
  const site = { calls: [] };
  const reply = (status, body, type) => ({ ok: status < 300, status, headers: { get: () => type || "text/plain", getSetCookie: () => [] },
    text: async () => body, json: async () => JSON.parse(body) });
  site.fetch = async (url, init = {}) => {
    const target = new URL(url), method = (init.method || "GET").toUpperCase();
    site.calls.push(method + " " + target.pathname);
    if (method === "GET" && target.pathname === "/rest/items") {
      return reply(200, JSON.stringify(house.rooms.map((room) => ({ name: "loc_" + room.id + "_source", state: "off" }))), "application/json");
    }
    if (target.pathname === "/static/remote/diagnostics.json") return reply(200, JSON.stringify({ version: 1, rooms: {}, devices: {} }), "application/json");
    return reply(method === "POST" ? 200 : 404, "");
  };
  site.EventSource = class {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      setImmediate(() => {
        if (this.readyState === 2) return;
        this.readyState = 1;
        if (this.onopen) this.onopen({});
      });
    }
    close() { this.readyState = 2; }
  };
  return site;
}

// The remote on the mock or on openHAB itself (native), the rooms each
// signal read asked for recorded (diagnosed) and the read still made.
function assemble(options = {}) {
  const shared = Shared.load();
  const site = options.native ? openhab(shared.house) : null;
  const args = site ? ["--url", ORIGIN, "--native", "--still"] : ["--mock", "--still", "--latency", "0"];
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

(async () => {
  const proof = {};
  await signal(proof, true);
  process.stdout.write(JSON.stringify(proof) + "\n");
})().then(() => process.exit(0), (error) => { process.stderr.write(error.stack + "\n"); process.exit(1); });
