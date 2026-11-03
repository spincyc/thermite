// Actual shared backend; HTTP/SSE and deadlines are local controlled fakes.
"use strict";
const assert = require("assert/strict");
const fs = require("fs"), path = require("path"), vm = require("vm");
const area = process.argv[2];
const source = fs.readFileSync(path.join(area, "web/remote/backend.js"), "utf8");
const diagnosticsSource = fs.readFileSync(path.join(area, "web/remote/diagnostics.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const house = { rooms: [], sources: { source: { label: "Source", kind: "pc" } }, devices: {} };
for (let index = 0; index < 12; index++) {
  const id = "room_" + index, display = "display_" + index;
  house.rooms.push({ id, label: "Room " + index, sources: ["source"], display, devices: [display],
    routeProblem: "loc_" + id + "_source_problem" });
  house.devices[display] = { label: "Display " + index, remote: "tv", problem: display + "_problem",
    control: { power: display + "_power" } };
}
const diagnosticItems = ["display_0_video_signal", "display_0_audio_signal", "display_0_edid_signal"];
const diagnosticManifest = { version: 1,
  rooms: { room_0: { label: "Room 0", sources: { source: { label: "Source",
    nodes: [{ device: "display_0", label: "Display 0", role: "display" }] } } } },
  devices: { display_0: { items: [...diagnosticItems, "display_0_token"],
    readings: [...diagnosticItems, "display_0_token"].map((item, index) => ({ item,
      category: ["video", "audio", "edid", "video"][index], label: "Signal " + index, path: ["values", "format"] })) } } };

function client(options = {}) {
  const timers = new Map(); let timerId = 0;
  const context = vm.createContext({ console: { info() {}, warn() {}, error() {} },
    ...(options.noAbort ? {} : { AbortController }),
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); } });
  vm.runInContext(source, context);
  vm.runInContext(diagnosticsSource, context);
  if (options.activity) vm.runInContext(fs.readFileSync(path.join(area, "web/remote/activity.js"), "utf8"), context);
  context.REMOTE_LAYOUTS = { buttons: { power: { label: "Power" } }, remotes: { tv: { label: "TV", keys: ["power"] } } };
  const reads = [], writes = [], streams = [], authorized = [];
  let active = 0, peak = 0;
  class Events {
    constructor(url) { this.url = url; this.closed = false; streams.push(this); }
    close() { this.closed = true; }
  }
  const access = options.authorized ? {
    native: false, subscribe() {}, stop() {},
    command(room, action, value) { authorized.push({ room, action, value }); return Promise.resolve({ accepted: true }); },
  } : undefined;
  const backend = new context.RemoteBackend.OpenHabBackend(house, {
    EventSource: Events, access, active: options.active !== false,
    ...(options.activity ? { activityManifest: { version: 1, items: {} } } : {}),
    ...(options.preloadedDiagnostics ? { diagnosticsManifest: diagnosticManifest } : {}),
    fetch(url, init) {
      if (init.method !== "GET") {
        writes.push({ url, value: init.body });
        return Promise.resolve({ ok: true, text: async () => "" });
      }
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      const read = { url, init, settled: false, body: null };
      active++; peak = Math.max(peak, active);
      read.finish = value => {
        if (read.settled) return;
        read.settled = true; active--;
        if (read.body) read.body.resolve(value);
        else resolve({ ok: true, status: 200, text: async () => value });
      };
      read.headers = () => {
        let yes, no;
        const body = new Promise((resolveBody, rejectBody) => { yes = resolveBody; no = rejectBody; });
        read.body = { resolve: yes, reject: no };
        resolve({ ok: true, status: 200, text: () => body });
      };
      if (init.signal && !options.ignoreAbort) init.signal.addEventListener("abort", () => {
        if (read.settled) return;
        read.settled = true; active--;
        const error = new Error("Synthetic aborted request"); error.name = "AbortError";
        if (read.body) read.body.reject(error); else reject(error);
      }, { once: true });
      reads.push(read);
      return promise;
    },
  });
  function event(item, value) {
    backend.onMessage(JSON.stringify({ type: "ItemStateUpdatedEvent", topic: "openhab/items/" + item + "/stateupdated",
      payload: JSON.stringify({ type: "String", value }) }));
  }
  const live = () => streams.filter(stream => !stream.closed);
  const pending = () => reads.filter(read => !read.settled);
  function valueOf(read) {
    if (read.url.endsWith("/diagnostics.json")) return JSON.stringify(diagnosticManifest);
    if (read.url.endsWith("/activity.json")) return JSON.stringify({ version: 1, items: {} });
    if (read.url.endsWith("_signal/state")) return JSON.stringify({ version: 1, observedAt: Date.now(),
      values: { format: "Reported format" }, privateField: "private-unforwarded" });
    return read.url.endsWith("_source/state") ? "source" : read.url.endsWith("_power/state") ? "ON" : "UNDEF";
  }
  async function complete(promise) {
    let done = false, failure;
    promise.then(() => { done = true; }, error => { done = true; failure = error; });
    for (let turn = 0; turn < 100 && !done; turn++) {
      pending().forEach(read => read.finish(valueOf(read)));
      await tick();
    }
    assert(done, "bounded fake snapshot workload must complete");
    if (failure) throw failure;
    return promise;
  }
  return { backend, reads, writes, authorized, event, timers, live, pending, complete, valueOf,
    active: () => active, peak: () => peak,
    async stop() { backend.stop(); pending().forEach(read => read.finish("UNDEF")); await tick(); assert.equal(timers.size, 0); } };
}

const cases = {};
async function probe(name, run, options) {
  const c = client(options);
  try { cases[name] = await run(c) || {}; }
  finally { await c.stop(); }
}

(async () => {
  await probe("bounded_coalesced_sweep", async c => {
    const sweep = c.backend.readAll();
    for (let repeat = 0; repeat < 100; repeat++) assert.equal(c.backend.readAll(), sweep);
    assert.equal(c.active(), 2, "whole-house recovery reserves connections for controls");
    await c.complete(sweep);
    assert.equal(c.reads.length, 48, "one exact read per source/control/problem");
    assert.equal(c.peak(), 2);
    for (const room of house.rooms) assert.equal(c.backend.snapshot(room.id).value, "source");
    return { requests: c.reads.length, peak: c.peak(), coalesced_calls: 100 };
  });

  await probe("reconnect_and_visibility_retire_work", async c => {
    c.live().forEach(stream => stream.onopen());
    for (let repeat = 0; repeat < 20; repeat++) {
      c.live()[0].onerror();
      assert.equal(c.active(), 0, "disconnect aborts active reads");
      c.live().forEach(stream => stream.onopen());
      await tick();
      assert.equal(c.active(), 2);
    }
    assert.equal(c.reads.length, 42, "retired queued reads never reach HTTP");
    const retired = c.reads.slice();
    c.backend.setActive(false); await tick();
    assert.equal(c.active(), 0);
    assert(retired.every(read => read.init.signal.aborted));
    assert.equal(c.timers.size, 0);
    await c.backend.readAll();
    assert.equal(c.reads.length, 42, "hidden reads do not restart work");
    c.backend.setActive(true); c.live().forEach(stream => stream.onopen());
    await c.complete(c.backend.readAll());
    for (const room of house.rooms) assert.equal(c.backend.snapshot(room.id).value, "source");
    return { reconnects: 20, retired_requests: retired.length, peak: c.peak() };
  });

  for (const authorized of [false, true]) await probe(authorized ? "authorized_command_bypasses_reads" : "native_command_bypasses_reads", async c => {
    const sweep = c.backend.readAll();
    const choosing = c.backend.choose("room_0", "source");
    await tick();
    const sent = authorized ? c.authorized : c.writes;
    assert.equal(sent.length, 1, "command dispatch does not wait for any blocked snapshot");
    assert.equal(c.active(), 2);
    if (authorized) assert.deepEqual(sent[0], { room: "room_0", action: "source", value: "source" });
    else assert.deepEqual(sent[0], { url: "/rest/items/loc_room_0_source", value: "source" });
    c.event("loc_room_0_source", "source");
    assert.equal((await choosing).pending, null);
    await c.complete(sweep);
    return { commands_before_snapshot_completion: sent.length, peak: c.peak() };
  }, { authorized });

  await probe("diagnostics_recovery_and_catalogues_share_budget", async c => {
    c.backend.listen([0, 1, 2].map(index => "/rest/events?topics=openhab/items/loc_room_" + index + "_source/stateupdated"));
    c.live().forEach(stream => stream.onopen());
    await c.complete(c.backend.readAll());
    assert.equal(c.live().length, 3);
    const diagnostics = c.backend.getDiagnostics("room_0", "source");
    await tick();
    assert.equal(c.active(), 1, "the diagnostics catalogue also acquires admission");
    const catalogue = c.pending()[0];
    assert(catalogue.url.endsWith("/diagnostics.json"));
    catalogue.finish(c.valueOf(catalogue)); await tick();
    assert.equal(c.active(), 2);
    assert(c.pending().every(read => read.url.endsWith("_signal/state")));
    const recovery = c.backend.readAll(), activity = c.backend.loadActivity();
    const choosing = c.backend.choose("room_0", "source");
    await tick();
    assert.equal(c.writes.length, 1, "a control sends while diagnostics occupy the shared read budget");
    assert.equal(c.active(), 2, "neither recovery nor metadata gets an extra connection");
    await choosing;
    const [, view] = await c.complete(Promise.all([recovery, diagnostics, activity]));
    assert.equal(view.source.id, "source");
    assert.equal(view.sections.video[0].availability, "reported");
    assert(!JSON.stringify(view).includes("private-unforwarded"));
    assert(!c.reads.some(read => read.url.includes("display_0_token")), "diagnostics still use their filtered allowlist");
    assert(c.reads.some(read => read.url.endsWith("/activity.json")));
    assert.equal(c.peak(), 2);
    return { open_streams: 3, combined_read_peak: c.peak(), commands_before_diagnostics_complete: c.writes.length };
  }, { activity: true });

  await probe("cancel_diagnostics_preserves_recovery", async c => {
    const diagnostics = c.backend.getDiagnostics("room_0", "source"); await tick();
    assert.equal(c.active(), 2);
    const recovery = c.backend.readAll();
    c.backend.cancelDiagnostics(); await tick();
    assert.equal(await diagnostics, null);
    assert.equal(c.active(), 2);
    assert(c.pending().every(read => !read.url.endsWith("_signal/state")), "canceling the panel does not cancel queued source reads");
    await c.complete(recovery);
    assert.equal(c.reads.filter(read => read.url.endsWith("_signal/state")).length, 2, "the third diagnostic Item never starts after cancellation");
    assert.equal(c.backend.snapshot("room_11").value, "source");
    return { diagnostic_requests: 2, recovery_completed: true, combined_read_peak: c.peak() };
  }, { preloadedDiagnostics: true });

  await probe("hide_cancels_shared_dynamic_reads", async c => {
    const diagnostics = c.backend.getDiagnostics("room_0", "source"); await tick();
    const recovery = c.backend.readAll();
    c.backend.setActive(false); await tick();
    await Promise.all([diagnostics, recovery]);
    assert.equal(c.active(), 0);
    assert.equal(c.timers.size, 0);
    assert.equal(c.reads.length, 2, "neither queued recovery nor the third signal Item starts while hidden");
    assert(c.reads.every(read => read.init.signal.aborted));
    c.backend.setActive(true);
    const resumed = await c.complete(c.backend.getDiagnostics("room_0", "source"));
    assert.equal(resumed.source.id, "source");
    return { retired_requests: 2, resumed_diagnostics: true, combined_read_peak: c.peak() };
  }, { preloadedDiagnostics: true });

  await probe("hidden_activity_catalogue_has_admission_and_deadline", async c => {
    const activity = c.backend.loadActivity();
    assert.equal(c.backend.loadActivity(), activity, "catalogue discovery remains coalesced");
    await c.backend.readAll();
    await c.backend.getDiagnostics("room_0", "source");
    assert.equal(c.active(), 1, "hidden startup only permits the finite static Activity catalogue");
    assert.equal(c.reads.length, 1);
    [...c.timers.values()].forEach(fire => fire());
    await activity; await tick();
    assert.equal(c.active(), 0);
    assert.equal(c.reads.length, 1);
    assert.equal(c.timers.size, 1, "failure uses the existing single backoff timer");
    return { hidden_catalogue_requests: 1, dynamic_reads: 0, timed_out_requests: 1 };
  }, { activity: true, active: false });

  await probe("body_read_is_canceled_on_hide", async c => {
    const reading = c.backend.getState("room_0");
    c.reads[0].headers(); await tick();
    assert.equal(c.active(), 1);
    c.backend.setActive(false); await tick();
    assert.equal(c.active(), 0);
    assert.equal((await reading).value, "NULL");
    assert.equal(c.timers.size, 0);
    return { canceled_body_reads: 1 };
  });

  await probe("duplicate_queued_items_share_read", async c => {
    const readings = [c.backend.getState("room_0"), c.backend.getState("room_1")];
    for (let repeat = 0; repeat < 100; repeat++) readings.push(c.backend.getState("room_2"));
    await c.complete(Promise.all(readings));
    assert.equal(c.reads.length, 3);
    assert.equal(c.backend.snapshot("room_2").value, "source");
    return { callers: readings.length, requests: c.reads.length, peak: c.peak() };
  });

  await probe("new_native_event_avoids_obsolete_get", async c => {
    const sweep = c.backend.readAll();
    c.event("loc_room_11_source", "source");
    await c.complete(sweep);
    assert(!c.reads.some(read => read.url === "/rest/items/loc_room_11_source/state"));
    assert.equal(c.backend.snapshot("room_11").value, "source");
    return { requests: c.reads.length, saved_queued_reads: 1 };
  });

  for (const noAbort of [false, true]) await probe(noAbort ? "missing_abort_support_stays_bounded" : "ignored_abort_stays_bounded", async c => {
    c.live().forEach(stream => stream.onopen());
    for (let repeat = 0; repeat < 20; repeat++) {
      c.live()[0].onerror(); c.live().forEach(stream => stream.onopen()); await tick();
    }
    assert.equal(c.reads.length, 2, "retired requests retain slots until the transport actually settles");
    assert.equal(c.active(), 2);
    c.backend.setActive(false);
    assert.equal(c.timers.size, 0);
    c.pending().forEach(read => read.finish("source")); await tick();
    assert.equal(c.active(), 0);
    assert.equal(c.backend.snapshot("room_0").value, "NULL", "late retired replies cannot become readback");
    return { reconnects: 20, requests: c.reads.length, peak: c.peak() };
  }, { ignoreAbort: true, noAbort });

  await probe("snapshot_deadline_includes_queue", async c => {
    const sweep = c.backend.readAll();
    [...c.timers.values()].forEach(fire => fire());
    await sweep; await tick();
    assert.equal(c.reads.length, 2, "queued work expires without reaching HTTP");
    assert.equal(c.active(), 0);
    assert.equal(c.timers.size, 0);
    const explicit = c.backend.getState("room_0").then(() => null, error => error.message);
    [...c.timers.values()].forEach(fire => fire());
    assert.equal(await explicit, "Snapshot unavailable", "a current failed read still reports failure");
    return { initial_requests: 2, queued_expired_before_http: 46 };
  });

  console.log(JSON.stringify({ cases, actualNetworkRequests: 0, actualDeviceCommands: 0 }));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
