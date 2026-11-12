/* Rooms through the day in the terminal, with fake gateway answers and timers. */
"use strict";
const assert = require("assert/strict");
const Day = require("../tui/remote/day"), Main = require("../tui/remote/main");
console.log = console.info = () => {};
const tick = () => new Promise(resolve => setImmediate(resolve));
const END = 1793615400, START = END - 24 * 3600;
const answer = () => ({ version: 1, start: START, end: END, step: 60, labels: { off: "Off", example_player: "Player" },
  rooms: [{ room: "example_west", label: "Example West", segments: [[START, START + 3600, "off"],
    [START + 3600, START + 7200, null], [START + 10800, END, "example_player"]] },
  { room: "example_east", label: "Example\u202e East", segments: [] }] });

function timers() {
  const pending = new Map(); let next = 1;
  return { pending, setTimeout: (fn, ms) => { pending.set(next, { fn, ms }); return next++; },
    clearTimeout: id => { pending.delete(id); },
    fire(ms) { for (const [id, entry] of [...pending]) if (entry.ms === ms) { pending.delete(id); entry.fn(); } } };
}

(async () => {
  // The answer is checked, and its text made plain.
  const data = Day.valid(answer());
  assert.equal(data.rooms[1].label, "Example  East");
  for (const broken of [{ ...answer(), version: 2 }, { ...answer(), step: 0 }, { ...answer(), labels: { "bad value": "Bad" } },
    { ...answer(), rooms: Array(65).fill({ room: "r", label: "R", segments: [] }) },
    { ...answer(), rooms: [{ room: "r", label: "R", segments: [[START, START + 60, "attic"]] }] },
    { ...answer(), rooms: [{ room: "r", label: "R", segments: [[START + 120, START + 180, "off"], [START, START + 60, "off"]] }] },
    { ...answer(), rooms: [{ room: "r", label: "R", segments: [[START, END + 60, "off"]] }] }]) {
    assert.throws(() => Day.valid(broken), /Invalid room history/);
  }
  // A column shows the value holding most of its time; no data wins only when it holds most.
  assert.deepEqual(Day.columns(data.rooms[0], data, 24).slice(0, 4), ["off", null, undefined, "example_player"]);
  assert.deepEqual(Day.columns(data.rooms[1], data, 4), [undefined, undefined, undefined, undefined]);

  // Read on open, again every REFRESH_MS while open, never once closed.
  const clock = timers(), asked = [];
  let reply = () => Promise.resolve(answer()), changes = 0;
  const backend = Day.create({ request: (path, init) => { asked.push([path, Boolean(init.signal)]); return reply(); },
    changed: () => { changes++; }, now: () => END * 1000, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  await backend.open(); await tick();
  assert.deepEqual(asked, [["/api/v1/history/rooms", true]]);
  assert(backend.state().loaded && !backend.state().error && changes > 0);
  clock.fire(Day.REFRESH_MS); await tick(); await tick();
  assert.equal(asked.length, 2, "a refresh while open");
  backend.close();
  assert.equal([...clock.pending.values()].filter(entry => entry.ms === Day.REFRESH_MS).length, 0, "closing ends the refresh");
  // A read that never answers gives up, aborted, and says so; a late answer after closing is ignored.
  let aborted = false;
  reply = () => new Promise(() => {});
  const hung = Day.create({ request: (path, init) => { init.signal.addEventListener("abort", () => { aborted = true; }); return reply(); },
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  const waiting = hung.open(); await tick(); clock.fire(Day.TIMEOUT_MS); await waiting;
  assert(aborted); assert.match(hung.state().error, /unavailable/); assert(!hung.state().loading);
  hung.close();
  let release;
  const late = Day.create({ request: () => new Promise(resolve => { release = resolve; }),
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  const pending = late.open(); await tick(); late.close(); release(answer()); await pending;
  assert(!late.state().loaded);
  const refused = Day.create({ request: () => Promise.reject(Object.assign(new Error("x"), { status: 503 })),
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  await refused.open(); assert.match(refused.state().error, /unavailable/); refused.close();
  const native = Day.create({ request: null });
  await native.open(); assert.match(native.state().error, /gateway/); native.close();

  // The terminal: h from Home and from Remotes opens it; it sends nothing to a device.
  const p = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), { setInterval: () => 0, clearInterval() {} });
  let commands = 0;
  for (const method of ["choose", "press", "pressKey", "slide", "operate", "chooseInput", "allOff", "tapBubble"])
    p.session[method] = () => { commands++; return Promise.resolve(); };
  await p.controller.start(); await tick();
  let frame = p.controller.frame(140, 40);
  assert(frame.grid.text().includes("Rooms through the day ›"));
  p.controller.key("h", { name: "h" }); await tick();
  assert.equal(p.controller.ui.overlay.kind, "day");
  frame = p.controller.frame(140, 40);
  const text = frame.grid.text();
  for (const words of ["Rooms through the day", "not the requested source", "Preview · a fictional day", "Example West",
    "Example East", "Player", "Unknown: no readback", "No data", "Now"]) assert(text.includes(words), words);
  assert.equal(p.controller.ui.focus, "day:example_west");
  p.controller.key(null, { name: "down" }); assert.equal(p.controller.ui.focus, "day:example_east");
  frame = p.controller.frame(140, 40);
  assert(frame.grid.text().includes("Example East · latest"));
  // Narrow terminals keep every room's row.
  frame = p.controller.frame(60, 30);
  assert(frame.grid.text().includes("Example West") && frame.grid.text().includes("Example East"));
  p.controller.key(null, { name: "escape" }); assert.equal(p.controller.ui.overlay, null);
  // Wide, so Remotes shows its remote rather than its rooms sheet.
  p.controller.frame(140, 40);
  p.controller.key(null, { name: "f2" }); await tick();
  assert.equal(p.controller.ui.screen, "remote");
  p.controller.key("h", { name: "h" }); await tick();
  assert.equal(p.controller.ui.overlay.kind, "day");
  // While it is open, remote keys stay with the sheet: nothing reaches a device.
  for (const ch of ["+", "-", "p", "m", "1", "0"]) p.controller.key(ch, { name: ch });
  assert.equal(p.controller.ui.overlay.kind, "day");
  p.controller.key(null, { name: "escape" }); assert.equal(p.controller.ui.overlay, null);
  p.controller.key("?", { name: "?" });
  assert(p.controller.frame(140, 60).grid.text().includes("Rooms through the day"), "help lists h");
  p.controller.stop();
  assert.equal(commands, 0);
  process.stdout.write(JSON.stringify({ pass: true, validated: true, columns: true, refresh: true, bounded: true,
    keyboard: true, commands }) + "\n");
})().then(() => process.exit(0), error => { process.stderr.write(error.stack + "\n"); process.exit(1); });
