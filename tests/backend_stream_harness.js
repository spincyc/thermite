// The actual shared backend and terminal against fake streams, reads and
// timers: one event stream per page, its snapshot instead of a burst of
// GETs, the views, native openHAB, reconnection and the bounded reads left.
"use strict";
const assert = require("assert/strict");
const fs = require("fs"), path = require("path"), vm = require("vm");
const area = process.argv[2];
const read = name => fs.readFileSync(path.join(area, name), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const ROOMS = 9;
const house = { rooms: [], sources: { source: { label: "Source", kind: "pc" }, other: { label: "Other", kind: "pc" } }, devices: {} };
for (let index = 0; index < ROOMS; index++) {
  const id = "room_" + index, display = "display_" + index;
  house.rooms.push({ id, label: "Room " + index, sources: ["source", "other"], display, devices: [display],
    routeProblem: "loc_" + id + "_source_problem" });
  house.devices[display] = { label: "Display " + index, remote: "tv", problem: display + "_problem",
    control: { power: display + "_power" } };
}
const diagnosticItems = ["display_0_video_signal", "display_0_audio_signal", "display_0_edid_signal"];
const diagnosticManifest = { version: 1,
  rooms: { room_0: { label: "Room 0", sources: { source: { label: "Source",
    nodes: [{ device: "display_0", label: "Display 0", role: "display" }] } } } },
  devices: { display_0: { items: diagnosticItems,
    readings: diagnosticItems.map((item, index) => ({ item, category: ["video", "audio", "edid"][index], label: "Signal " + index,
      path: ["values", "format"] })) } } };
const activityItems = { loc_room_0_source: { label: "Source", room: "Room 0", kind: "enum", options: { off: "Off", source: "Source", other: "Other" },
  states: true, commands: true }, display_0_power: { label: "power", device: "Display 0", kind: "switch", states: true, commands: true } };

// Every room on "source", every display on, no problems: a view's snapshot.
function snapshotStates(view, values = {}) {
  const states = {};
  house.rooms.forEach(room => {
    states["loc_" + room.id + "_source"] = "source";
    if (view === "remote") { states[room.routeProblem] = "NULL"; states[room.display + "_power"] = "ON"; states[room.display + "_problem"] = "NULL"; }
  });
  return Object.assign(states, values);
}

function client(options = {}) {
  const timers = new Map(); let timerId = 0;
  const context = vm.createContext({ console: { info() {}, warn() {}, error() {} }, AbortController,
    setTimeout(fn, ms) { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout(id) { timers.delete(id); } });
  vm.runInContext(read("web/remote/backend.js"), context);
  vm.runInContext(read("web/remote/diagnostics.js"), context);
  if (options.activity) vm.runInContext(read("web/remote/activity.js"), context);
  context.REMOTE_LAYOUTS = { buttons: { power: { label: "Power" } }, remotes: { tv: { label: "TV", keys: ["power"] } } };
  const reads = [], writes = [], streams = [], authorized = [], events = [], recorded = [];
  let active = 0, peak = 0;
  class Events {
    constructor(url) { this.url = url; this.closed = false; this.readyState = 0; streams.push(this); }
    close() { this.closed = true; this.readyState = 2; }
    open() { this.readyState = 1; this.onopen({}); }
    send(value) { this.onmessage({ data: JSON.stringify(value) }); }
  }
  const access = options.native ? undefined : {
    native: false, subscribe() {}, stop() {}, record(...report) { recorded.push(report); },
    command(room, action, value) { authorized.push({ room, action, value }); return Promise.resolve({ accepted: true }); },
  };
  const backend = new context.RemoteBackend.OpenHabBackend(house, {
    EventSource: Events, access, active: options.active !== false, view: options.view,
    ...(options.activity ? { activityManifest: { version: 1, items: activityItems } } : {}),
    ...(options.diagnostics ? { diagnosticsManifest: diagnosticManifest } : {}),
    fetch(url, init) {
      if (init.method !== "GET") {
        const write = { url, value: init.body, aborted: false };
        writes.push(write);
        if (!options.hangWrites) return Promise.resolve({ ok: true, text: async () => "" });
        return new Promise((_, reject) => init.signal.addEventListener("abort", () => { write.aborted = true; reject(new Error("aborted")); }));
      }
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      const request = { url, init, settled: false };
      active++; peak = Math.max(peak, active);
      request.finish = value => {
        if (request.settled) return;
        request.settled = true; active--;
        resolve({ ok: true, status: 200, text: async () => value });
      };
      if (init.signal) init.signal.addEventListener("abort", () => {
        if (request.settled) return;
        request.settled = true; active--;
        const error = new Error("Synthetic aborted request"); error.name = "AbortError";
        reject(error);
      }, { once: true });
      reads.push(request);
      return promise;
    },
  });
  backend.subscribe(event => events.push(event));
  const live = () => streams.filter(stream => !stream.closed);
  const pending = () => reads.filter(request => !request.settled);
  function event(item, value, verb = "stateupdated", old) {
    const type = { stateupdated: "ItemStateUpdatedEvent", statechanged: "ItemStateChangedEvent", command: "ItemCommandEvent" }[verb];
    live()[0].send({ topic: "openhab/items/" + item + "/" + verb, type,
      payload: JSON.stringify(Object.assign({ type: "String", value }, old === undefined ? {} : { oldValue: old })) });
  }
  // Item reads: anything under /rest/items, the bulk snapshot included.
  const itemReads = () => reads.filter(request => request.url.includes("/rest/items"));
  // Fires the timer set for ms, as the clock would; false if there is none.
  function fire(ms) {
    const entry = [...timers].find(([, timer]) => timer.ms === ms);
    if (!entry) return false;
    timers.delete(entry[0]); entry[1].fn();
    return true;
  }
  return { backend, reads, writes, authorized, streams, events, recorded, timers, live, pending, event, itemReads, fire,
    active: () => active, peak: () => peak,
    async stop() { backend.stop(); pending().forEach(request => request.finish("UNDEF")); await tick(); assert.equal(timers.size, 0, "stop clears every timer"); } };
}

const cases = {};
async function probe(name, run, options) {
  const c = client(options);
  try { cases[name] = await run(c) || {}; }
  finally { await c.stop(); }
}

(async () => {
  await probe("remote_page_one_stream_snapshot_no_get_burst", async c => {
    assert.equal(c.streams.length, 1, "one EventSource for the page");
    assert.equal(c.streams[0].url, "/api/v1/events/remote");
    let resolved = false;
    const states = Promise.all(house.rooms.map(room => c.backend.getState(room.id)));
    states.then(() => { resolved = true; });
    c.live()[0].open(); await tick();
    assert.equal(resolved, false, "getState waits for the snapshot, not a GET");
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote", { display_1_power: "OFF", display_2_problem: "No answer",
      loc_room_3_source: "UNDEF", loc_room_3_source_problem: "{}" }) });
    const answered = await states;
    assert(answered.every(state => state.value === (state.room === "room_3" ? "UNDEF" : "source")));
    assert.equal(answered[3].status, "unavailable");
    assert.equal(answered[3].reason, "No current source readback; waiting for device diagnostics");
    assert.equal((await c.backend.getDevice("display_1")).power, "OFF");
    assert.equal(c.backend.problemOf("display_2"), "No answer");
    assert.equal(c.reads.length, 0, "no GET at all on open");
    assert.equal(c.streams.length, 1);
    return { streams: c.streams.length, gets: c.reads.length, rooms: answered.length };
  });

  await probe("remote_view_events_after_the_snapshot", async c => {
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote") });
    c.event("display_0_power", "OFF", "statechanged", "ON");
    assert.equal(c.backend.devices.display_0.power, "OFF");
    c.event("display_0_problem", "Unreachable", "statechanged", "NULL");
    assert.equal(c.backend.problemOf("display_0"), "Unreachable");
    c.event("loc_room_0_source", "other");
    assert.equal(c.backend.snapshot("room_0").value, "other");
    // The remote follows a Source as stateupdated; a statechanged copy (openHAB's wildcards) is not a second report.
    c.event("loc_room_0_source", "off", "statechanged", "other");
    assert.equal(c.backend.snapshot("room_0").value, "other");
    c.event("unrelated_item", "ON", "statechanged", "OFF");
    assert.equal(c.reads.length, 0);
    return { applied: 3 };
  });

  await probe("home_view_sources_only", async c => {
    assert.deepEqual(c.streams.map(stream => stream.url), ["/api/v1/events/home"]);
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "home", states: snapshotStates("home") });
    assert.equal((await c.backend.getState("room_4")).value, "source");
    c.event("loc_room_4_source", "other", "statechanged", "source");
    assert.equal(c.backend.snapshot("room_4").value, "other");
    c.event("loc_room_4_source", "off", "stateupdated");
    assert.equal(c.backend.snapshot("room_4").value, "other", "Home follows Sources as statechanged only");
    assert.equal(c.backend.watched().length, 0, "no control, problem or route problem Item");
    assert.equal(c.reads.length, 0, "no activity catalogue or Item read");
    return { streams: c.streams.length, gets: c.reads.length };
  }, { view: "home", activity: true });

  await probe("unavailable_then_resync", async c => {
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote") });
    c.live()[0].send({ type: "unavailable" });
    assert.deepEqual(c.events.filter(event => event.type === "connection").map(event => event.connected), [false]);
    const waiting = c.backend.getState("room_5");
    let resolved = false; waiting.then(() => { resolved = true; });
    await tick();
    assert.equal(resolved, false, "a resync is awaited again");
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote", { loc_room_5_source: "other" }) });
    assert.equal((await waiting).value, "other");
    assert.deepEqual(c.events.filter(event => event.type === "connection").map(event => event.connected), [false, true]);
    assert.equal(c.streams.length, 1, "the stream stayed open");
    return { reconnects: 1, gets: c.reads.length };
  });

  await probe("dropped_stream_reopens_with_a_fresh_snapshot", async c => {
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote") });
    for (let repeat = 0; repeat < 20; repeat++) {
      c.live()[0].onerror({});
      c.live()[0].open();
      c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote", { loc_room_6_source: repeat % 2 ? "source" : "other" }) });
    }
    assert.equal(c.backend.snapshot("room_6").value, "source");
    assert.equal(c.reads.length, 0, "reconnects read nothing");
    // A browser gives up on a non-200 answer (readyState 2): reopened after a backoff.
    c.live()[0].readyState = 2; c.live()[0].onerror({});
    assert(c.fire(1000), "a reopen is scheduled");
    assert.equal(c.live().length, 1);
    assert.equal(c.streams.length, 2);
    return { reconnects: 20, gets: c.reads.length };
  });

  // A reconnect's snapshot replaces what the page knew: an Item it lacks
  // reads as not known, never as its value before the gap.
  await probe("reconnect_snapshot_replaces_state", async c => {
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote", { display_2_problem: "No answer",
      loc_room_3_source: "UNDEF", loc_room_3_source_problem: "{}" }) });
    assert.equal(c.backend.devices.display_1.power, "ON"); assert.equal(c.backend.problemOf("display_2"), "No answer");
    c.live()[0].onerror({});
    c.live()[0].open();
    const states = snapshotStates("remote");
    ["loc_room_2_source", "display_1_power", "display_2_problem", "loc_room_3_source_problem"].forEach(item => delete states[item]);
    c.live()[0].send({ type: "snapshot", view: "remote", states: Object.assign(states, { loc_room_3_source: "UNDEF" }) });
    assert.equal(c.backend.snapshot("room_2").value, "NULL"); assert.equal(c.backend.snapshot("room_2").status, "unknown");
    assert.equal(c.backend.devices.display_1.power, "NULL");
    assert.equal(c.backend.problemOf("display_2"), null);
    assert.equal(c.backend.snapshot("room_3").reason, "No current source readback; waiting for device diagnostics");
    assert(!("display_1_power" in c.backend.values));
    assert.equal(c.backend.snapshot("room_4").value, "source");
    // So does one after the gateway lost openHAB, on a stream that stayed open.
    c.live()[0].send({ type: "unavailable" });
    c.live()[0].send({ type: "snapshot", view: "remote", states: {} });
    assert(c.backend.house.rooms.every(room => c.backend.snapshot(room.id).status === "unknown"));
    assert.equal(c.reads.length, 0);
    return { unknown: 9 };
  });

  // A command openHAB itself never answers is aborted at 15 s; the room stops switching.
  await probe("native_command_deadline", async c => {
    let failure = null;
    const choosing = c.backend.choose("room_0", "other").catch(error => { failure = error; });
    await tick();
    assert.equal(c.backend.snapshot("room_0").status, "switching");
    assert(c.fire(15000), "a command deadline is set");
    await choosing;
    assert(c.writes[0].aborted);
    assert.match(failure.message, /did not answer in time/);
    assert.equal(c.backend.snapshot("room_0").pending, null);
    return { deadline_ms: 15000 };
  }, { native: true, hangWrites: true });

  await probe("hidden_page_holds_no_stream", async c => {
    c.backend.setActive(false);
    assert.equal(c.live().length, 0);
    assert.equal((await c.backend.getState("room_0")).value, "NULL", "a hidden page answers at once");
    c.backend.setActive(true);
    assert.equal(c.live().length, 1);
    assert.equal(c.streams.length, 2);
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote") });
    assert.equal((await c.backend.getState("room_0")).value, "source");
    assert.equal(c.reads.length, 0);
    return { streams_opened: c.streams.length };
  });

  await probe("snapshot_deadline", async c => {
    const waiting = c.backend.getState("room_0");
    assert(c.fire(8000), "getState waits at most SNAPSHOT_TIMEOUT");
    assert.equal((await waiting).status, "unknown");
    assert.deepEqual(c.recorded, [["snapshot_timeout", 8, "/api/v1/events/remote"]], "client telemetry counts the give-up");
    return { waited_ms: 8000 };
  });

  for (const native of [false, true]) await probe(native ? "native_command_before_snapshot" : "gateway_command_before_snapshot", async c => {
    const choosing = c.backend.choose("room_0", "other");
    await tick();
    const sent = native ? c.writes : c.authorized;
    assert.equal(sent.length, 1, "a command never waits for the snapshot");
    if (native) assert.deepEqual(sent[0], { url: "/rest/items/loc_room_0_source", value: "other", aborted: false });
    else assert.deepEqual(sent[0], { room: "room_0", action: "source", value: "other" });
    c.live()[0].open();
    c.event("loc_room_0_source", "other");
    assert.equal((await choosing).pending, null);
    // A choice not read back settles at confirmMs() without reading the Item.
    const second = c.backend.choose("room_1", "other");
    await tick();
    assert(c.fire(16000));
    assert.equal((await second).pending, null);
    assert(!c.reads.some(request => /\/rest\/items\/[^?]+\/state$/.test(request.url)), "no single Item read");
    return { commands: sent.length };
  }, { native });

  await probe("native_wildcards_and_one_bulk_read", async c => {
    assert.equal(c.streams.length, 1);
    assert.equal(c.streams[0].url, "/rest/events?topics=openhab/items/loc_*_source/stateupdated,openhab/items/*/statechanged,openhab/items/*/command");
    c.live()[0].open(); await tick();
    assert.equal(c.reads.length, 1);
    assert.equal(c.reads[0].url, "/rest/items?fields=name,state");
    assert.equal(c.reads[0].init.headers.Accept, "application/json");
    // An event during the read is newer than the read.
    c.event("display_7_power", "OFF", "statechanged", "ON");
    c.reads[0].finish(JSON.stringify(Object.entries(snapshotStates("remote")).map(([name, state]) => ({ name, state }))
      .concat([{ name: "unrelated_item", state: "x" }])));
    assert.equal((await c.backend.getState("room_8")).value, "source");
    assert.equal(c.backend.devices.display_7.power, "OFF");
    assert.equal(c.backend.devices.display_6.power, "ON");
    c.event("loc_room_8_source", "off", "statechanged", "source");
    assert.equal(c.backend.snapshot("room_8").value, "source", "the remote keeps a Source's stateupdated only");
    assert.equal(c.reads.length, 1);
    return { streams: 1, gets: c.reads.length };
  }, { native: true });

  await probe("native_home_topics", async c => {
    assert.equal(c.streams[0].url, "/rest/events?topics=openhab/items/loc_*_source/statechanged");
    return {};
  }, { native: true, view: "home" });

  await probe("activity_reads_the_view_stream", async c => {
    assert.equal(c.streams.length, 1, "the feed opens no stream of its own");
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote") });
    const lines = () => c.events.filter(event => event.type === "activity" && event.kind !== "connection").map(event => event.body);
    c.event("loc_room_0_source", "source");
    assert.deepEqual(lines(), [], "a repeated Source update is no activity");
    c.event("loc_room_0_source", "other");
    c.event("display_0_power", "OFF", "statechanged", "ON");
    c.event("display_0_power", "OFF", "command");
    assert.deepEqual(lines(), ["Reported: Other", "Reported: Off", "Requested: Off"]);
    // After a gap the snapshot is the new baseline: no line for the gap itself.
    c.live()[0].send({ type: "unavailable" });
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote", { loc_room_0_source: "off" }) });
    c.event("loc_room_0_source", "off");
    assert.equal(lines().length, 3);
    return { lines: lines().length };
  }, { activity: true });

  await probe("diagnostics_reads_stay_bounded", async c => {
    c.live()[0].open();
    c.live()[0].send({ type: "snapshot", view: "remote", states: snapshotStates("remote") });
    const diagnostics = c.backend.getDiagnostics("room_0", "source");
    await tick(); await tick();
    assert.equal(c.active(), 2, "diagnostics read at most two Items at once");
    const choosing = c.backend.choose("room_0", "other");
    await tick();
    assert.equal(c.authorized.length, 1, "a command does not wait for diagnostics");
    for (let turn = 0; turn < 10 && c.pending().length; turn++) {
      c.pending().forEach(request => request.finish(JSON.stringify({ version: 1, observedAt: Date.now(), values: { format: "Reported" } })));
      await tick(); await tick();
    }
    const view = await diagnostics;
    assert.equal(view.source.id, "source");
    assert.equal(c.reads.length, 3);
    assert.equal(c.peak(), 2);
    c.event("loc_room_0_source", "other");
    await choosing;
    c.backend.getDiagnostics("room_0", "source"); await tick(); await tick();
    c.backend.setActive(false); await tick();
    assert.equal(c.active(), 0, "a hidden page cancels its reads");
    return { diagnostic_reads: 3, peak: c.peak() };
  }, { diagnostics: true });

  // The terminal remote: one remote-view stream through the gateway, its
  // Pairing screen the pair view; no Item GETs on start.
  {
    const Main = require(path.resolve(area, "tui/remote/main")), Shared = require(path.resolve(area, "tui/remote/shared"));
    const origin = "https://site.example", calls = [], opened = [];
    const json = (value, status = 200) => ({ ok: status < 300, status, json: async () => value, text: async () => JSON.stringify(value),
      clone() { return json(value, status); }, headers: { get: () => "application/json", getSetCookie: () => [] } });
    const principal = { id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true,
      permissions: ["site.read", "room.control", "pairing.manage"] };
    const fetch = async (url, init = {}) => {
      const route = new URL(url).pathname; calls.push((init.method || "GET") + " " + route);
      if (route === "/api/v1/security") return json({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, csrf: "C".repeat(32) });
      if (route === "/api/v1/session") return json({ csrf: "C".repeat(32) });
      if (route === "/api/v1/me") return json(principal);
      if (route === "/api/v1/locks") return json({ rooms: {} });
      if (/^\/api\/v1\/rooms\/[a-z0-9_]+\/policy$/.test(route)) return json({ error: "not_found" }, 404);
      if (route === "/rest/items") return json([]);
      return json({ error: "not_found" }, 404);
    };
    const shared = Shared.load();
    class ViewEvents {
      constructor(url) {
        this.url = url; this.readyState = 0; opened.push(this);
        setImmediate(() => {
          if (this.readyState === 2) return;
          this.readyState = 1; this.onopen({});
          const states = Object.fromEntries(shared.house.rooms.map(room => ["loc_" + room.id + "_source", "off"]));
          this.onmessage({ data: JSON.stringify({ type: "snapshot", view: url.split("/").pop(), states }) });
        });
      }
      close() { this.readyState = 2; }
    }
    const access = shared.Access.create({ base: origin, fetch, policyInterval: 3600000 });
    const parts = Main.assemble(Main.parse(["--url", origin, "--still"]), { shared, access, fetch, EventSource: ViewEvents, environ: {},
      setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {} });
    parts.controller.start();
    for (let turn = 0; turn < 40 && opened.length < 1; turn++) await tick();
    const room = shared.house.rooms[0].id;
    const state = await parts.backend.getState(room);
    assert.equal(state.value, "off");
    assert.deepEqual(opened.map(source => source.url), [origin + "/api/v1/events/remote"]);
    assert(!calls.some(call => call.startsWith("GET /rest/items/")), "no Item read on start: " + calls.join(", "));
    const pair = shared.Pairing.create({ backend: "openhab", base: origin, fetch: access.fetch, EventSource: access.eventSource(ViewEvents),
      location: null, native: false });
    await pair.start().catch(() => null);
    for (let turn = 0; turn < 40 && opened.length < 2; turn++) await tick();
    assert.equal(opened[1].url, origin + "/api/v1/events/pair");
    pair.stop(); parts.controller.stop(); parts.backend.stop();
    cases.terminal_remote_view_and_pair_view = { streams: opened.length, item_gets: 0 };
  }

  console.log(JSON.stringify({ cases, actualNetworkRequests: 0, actualDeviceCommands: 0 }));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
