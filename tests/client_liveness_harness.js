// The shared access client's event-stream watchdog, against fake streams, a
// fake clock and fake timers. Nothing leaves the process.
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const area = path.resolve(__dirname, "..");
const read = name => fs.readFileSync(path.join(area, name), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const origin = "https://site.example:13601", csrf = "C".repeat(32);
const admin = { id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true,
  permissions: ["site.read", "room.control", "settings.manage"] };
const answer = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => value, text: async () => JSON.stringify(value),
  clone() { return answer(value, status); }, headers: { get: () => null, getSetCookie: () => [] } });

// A script in a context of its own, with a fake clock and timers.
function sandbox(files) {
  const clock = { now: 1_000_000 }, timers = new Map(); let next = 0;
  const context = vm.createContext({ URL, URLSearchParams, Headers, AbortController, Blob, console,
    setTimeout(fn, ms) { timers.set(++next, { fn, ms, at: clock.now + ms }); return next; },
    clearTimeout(id) { timers.delete(id); } });
  files.forEach(name => vm.runInContext(read(name), context));
  return {
    context, clock, timers,
    // The timers set for ms, as a list of their delays.
    delays: () => [...timers.values()].map(timer => timer.ms).sort((a, b) => a - b),
    // Moves the clock on and runs every timer that falls due, as time would.
    advance(ms) {
      const until = clock.now + ms;
      for (;;) {
        const due = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]); clock.now = Math.max(clock.now, due[1].at); due[1].fn();
      }
      clock.now = until;
    }
  };
}

// A gateway whose answers a test steers; a held route never answers until released.
function gateway() {
  const site = { calls: [], held: [], hold: new Set() };
  site.fetch = async (url, init = {}) => {
    const route = new URL(url).pathname, method = (init.method || "GET").toUpperCase();
    site.calls.push({ method, route, init });
    if (site.hold.has(route)) return new Promise((resolve, reject) => {
      const entry = { route, init, resolve, aborted: false };
      init.signal?.addEventListener("abort", () => { entry.aborted = true; reject(Object.assign(new Error("aborted"), { name: "AbortError" })); });
      site.held.push(entry);
    });
    if (route === "/api/v1/security") return answer({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, csrf });
    if (route === "/api/v1/session") return answer({ csrf });
    if (route === "/api/v1/me") return answer(admin);
    if (route === "/api/v1/locks") return answer({ rooms: {} });
    if (route === "/api/v1/telemetry") return answer(null, 204);
    if (route.endsWith("/policy")) return answer({ room: "den", revision: 0, can_lock: true, can_edit: true, effective: { allowed: true, locked: false } });
    return answer({ error: "not_found" }, 404);
  };
  return site;
}

function accessClient(extra = {}) {
  const box = sandbox(["web/remote/access.js"]), site = gateway();
  const client = box.context.ThermiteAccess.create({ base: origin, fetch: site.fetch, now: () => box.clock.now, ...extra });
  // What the client has counted, as its next report would carry it.
  const counted = async () => {
    await client.flushTelemetry(true);
    const report = site.calls.filter(call => call.route === "/api/v1/telemetry").pop();
    return report ? JSON.parse(report.init.body).events : {};
  };
  return { box, site, client, counted, Access: box.context.ThermiteAccess };
}

// A stand-in for the browser's (or the terminal's) EventSource.
function sources() {
  const opened = [];
  class Source {
    constructor(url) { this.url = url; this.readyState = 0; this.closed = false; this.protocol = "http/1.1"; opened.push(this); }
    open() { this.readyState = 1; this.onopen({}); }
    send(data) { this.onmessage({ data: typeof data === "string" ? data : JSON.stringify(data) }); }
    close() { this.closed = true; this.readyState = 2; }
  }
  return { Source, opened, live: () => opened.filter(source => !source.closed) };
}

(async () => {
  const proof = {};
  const HEARTBEAT = '{"type":"heartbeat"}';

  // ---- the stream watchdog ---------------------------------------------------

  {
    const { box, client, Access } = accessClient();
    assert.equal(Access.STREAM_HEARTBEAT_MS, 5000);
    const { Source, opened, live } = sources(), Events = client.eventSource(Source);
    const stream = new Events("/api/v1/events/remote"), messages = [], errors = [];
    stream.onmessage = event => messages.push(event.data); stream.onerror = event => errors.push({ ...event, readyState: stream.readyState });
    for (let turn = 0; turn < 10 && !opened.length; turn++) await tick();
    assert.equal(opened.length, 1);
    opened[0].open(); opened[0].send({ type: "snapshot", states: {} });
    // Heartbeats every 5 s keep a quiet stream for a minute, and reach no listener.
    for (let beat = 0; beat < 12; beat++) { box.advance(5000); live()[0].send(HEARTBEAT); }
    assert.equal(opened.length, 1, "a stream with heartbeats is never reopened");
    assert.deepEqual(messages, [JSON.stringify({ type: "snapshot", states: {} })], "heartbeats reach no listener");
    // Three heartbeats' silence: closed, counted, and opened again at once.
    box.advance(14999); assert.equal(opened.length, 1, "not before 15 s");
    box.advance(1);
    assert.equal(opened.length, 2); assert(opened[0].closed); assert.equal(opened[1].url, "/api/v1/events/remote");
    assert.deepEqual(errors, [{ type: "error", status: 0, stalled: true, readyState: 0 }], "the listener sees a reconnection");
    // The new stream is watched from its start, before it opens.
    box.advance(15000); assert.equal(opened.length, 3, "a stream that never opens is replaced too");
    opened[2].open(); opened[2].send({ type: "snapshot", states: {} });
    assert.equal(messages.length, 2);
    // A stream the browser closed for good (a non-200 answer) is its listener's to reopen.
    opened[2].readyState = 2; opened[2].onerror({});
    box.advance(60000); assert.equal(opened.length, 3, "no watchdog on a closed stream");
    stream.close(); assert.equal(box.timers.size, 0, "close clears the watchdog");
    proof.watchdog = { heartbeat_ms: 5000, silence_ms: 15000 };
  }

  {
    // A page shown again checks at once: a stream silent past the bound is
    // reopened then, whether or not the page saw itself hidden.
    const { box, client, counted } = accessClient();
    const { Source, opened, live } = sources(), Events = client.eventSource(Source);
    const stream = new Events("/api/v1/events/home"); stream.onerror = () => {};
    for (let turn = 0; turn < 10 && !opened.length; turn++) await tick();
    opened[0].open();
    client.setActive(false);
    box.advance(120000); assert.equal(opened.length, 1, "nothing is reopened while the page is hidden");
    assert.equal(box.timers.size, 0, "no watchdog runs while hidden");
    client.setActive(true); assert.equal(opened.length, 2, "reopened as the page is shown");
    // pageshow with no hide before it (a phone back from the background).
    live()[0].open(); box.clock.now += 4000;
    client.setActive(true); assert.equal(opened.length, 2, "a stream heard from within the bound is kept");
    box.clock.now += 20000;
    client.setActive(true); assert.equal(opened.length, 3, "a stream silent past the bound is reopened at once");
    assert.equal(live().length, 1);
    assert.deepEqual(await counted(), { stream_error: { count: 2, waits: [] } }, "each silent stream counts as a stream_error");
    stream.close();
    proof.resume = true;
  }

  process.stdout.write(JSON.stringify(proof));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
