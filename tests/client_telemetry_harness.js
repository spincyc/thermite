/* The shared access client's own give-ups, counted and reported to /api/v1/telemetry, against a fake gateway. */
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), http = require("node:http"), path = require("node:path"), vm = require("node:vm");
const Access = require("../web/remote/access"), { EventSource } = require("../tui/remote/events");
const SOURCE = fs.readFileSync(path.join(__dirname, "../web/remote/access.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const origin = "https://site.example:13601", csrf = "C".repeat(32), TELEMETRY = "/api/v1/telemetry";
const guest = { id: 0, username: "Guest", admin: false, can_edit: false, is_guest: true, auth_enabled: true, permissions: ["site.read", "room.control"] };
const answer = (value, status = 200, headers = {}) => ({ ok: status >= 200 && status < 300, status, json: async () => value,
  clone() { return answer(value, status, headers); }, headers: { get: name => headers[name] ?? null, getSetCookie: () => [] } });
const never = () => new Promise(() => {});

// A gateway whose answers each test steers: hang holds a route, telemetry answers the report.
function gateway() {
  const site = { calls: [], reports: [], hang: new Set(), telemetry: () => answer(null, 204), clock: 1_000_000 };
  site.fetch = async (url, init = {}) => {
    const route = new URL(url).pathname, method = (init.method || "GET").toUpperCase();
    site.calls.push(method + " " + route);
    if (route === TELEMETRY) {
      site.reports.push({ body: JSON.parse(init.body), type: init.headers.get("Content-Type"), csrf: init.headers.get("X-CSRFToken"),
        bearer: init.headers.get("Authorization"), keepalive: init.keepalive });
      return site.telemetry();
    }
    if (site.hang.has(route)) { site.clock += 8250; return never(); }
    if (route === "/api/v1/security") return answer({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, csrf });
    if (route === "/api/v1/session") return answer({ csrf });
    if (route === "/api/v1/me") return answer(guest);
    if (route === "/api/v1/locks") return answer({ rooms: {} });
    if (route.endsWith("/policy")) return answer({ room: "den", revision: 0, can_lock: false, effective: { allowed: true, locked: false } });
    if (route.endsWith("/commands")) return answer({ accepted: true }, 202);
    return answer({ error: "not_found" }, 404);
  };
  return site;
}
const create = (site, extra = {}) => Access.create({ base: origin, fetch: site.fetch, now: () => site.clock, policyTimeout: 15, commandTimeout: 15, ...extra });
const sent = site => site.calls.filter(call => call === "POST " + TELEMETRY).length;

// access.js as a page loads it: its own globals, visibilitychange and all.
function page(options = {}) {
  const handlers = {}, beacons = [], fetches = [], reports = options.reports || (() => answer(null, 204));
  const context = { URL, URLSearchParams, Headers, Blob, AbortController, setTimeout, clearTimeout, console,
    location: { href: origin + "/remotes/?" + (options.query || ""), origin, search: "?" + (options.query || ""), replace() {} },
    document: { visibilityState: "visible", body: { dataset: { thermitePage: "remotes" } }, querySelectorAll: () => [],
      addEventListener: (name, fn) => { handlers[name] = fn; } },
    addEventListener() {},
    fetch: async (url, init = {}) => {
      fetches.push({ route: new URL(url).pathname, init });
      if (new URL(url).pathname === "/api/v1/security") return answer({ enabled: false, revision: 1, bootstrap_required: false, setup_available: false, csrf });
      if (new URL(url).pathname === TELEMETRY) return reports();
      return answer(null, 204);
    } };
  if ("performance" in options) context.performance = options.performance;
  if (options.sendBeacon) context.navigator = { sendBeacon(url, blob) { beacons.push({ url, blob }); return options.sendBeacon(url, blob); } };
  vm.createContext(context); vm.runInContext(SOURCE, context);
  return { context, client: context.ThermiteAccess.client, beacons, fetches,
    posted: () => fetches.filter(call => call.route === TELEMETRY).map(call => JSON.parse(call.init.body)),
    hide() { context.document.visibilityState = "hidden"; handlers.visibilitychange(); },
    show() { context.document.visibilityState = "visible"; handlers.visibilitychange(); } };
}

(async () => {
  const proof = {};

  // A hung policy read gives up as client_timeout with no server status;
  // the room shows unavailable, and its commands are still sent.
  let site = gateway(), client = create(site);
  await client.ready();
  site.hang.add("/api/v1/rooms/den/policy");
  await assert.rejects(client.loadPolicy("den"), error => error.code === "client_timeout" && error.status === 0
    && error.message === Access.MESSAGES.client_timeout);
  assert.equal(client.policyStatus("den"), "unavailable");
  site.hang.add("/api/v1/locks");
  await assert.rejects(client.loadLocks(), error => error.code === "client_timeout" && error.status === 0);
  proof.clientTimeout = true;

  // The next answered request (a command: an unread lock no longer holds
  // one back) reports both, once, as the terminal client.
  site.hang.clear(); assert.deepEqual(await client.command("den", "source", "off"), { accepted: true }); await tick();
  assert.equal(sent(site), 1);
  assert.deepEqual(site.reports[0].body, { client: "tui", protocol: "unknown",
    events: { policy_timeout: { count: 1, waits: [8.25] }, locks_timeout: { count: 1, waits: [8.25] } } });
  assert.equal(site.reports[0].type, "application/json"); assert.equal(site.reports[0].csrf, csrf); assert.equal(site.reports[0].bearer, null);
  proof.flushAfterSuccess = true;

  // 204 cleared it: nothing more to send, however many answers follow.
  site.clock += 60000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 1);
  proof.acceptedClears = true;

  // At most every 15 s, and one report in flight at a time.
  client.record("stream_error", 1.5, "/api/v1/events/remote"); await client.loadLocks(); await tick();
  assert.equal(sent(site), 2);
  client.record("stream_error", 2.5, "/api/v1/events/remote"); site.clock += 5000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 2, "within 15 s of the last report");
  let release; site.telemetry = () => new Promise(resolve => { release = () => resolve(answer(null, 204)); });
  site.clock += 15000; await client.loadLocks(); await tick();
  site.clock += 15000; client.record("stream_error", 3.5); await client.loadLocks(); await client.loadLocks(); await tick();
  assert.equal(sent(site), 3, "one report in flight at a time");
  release(); await tick(); await tick();
  assert.deepEqual(site.reports[2].body.events, { stream_error: { count: 1, waits: [2.5] } });
  proof.spacedAndSingle = true;

  // 400 drops the report for good.
  site = gateway(); client = create(site); await client.ready();
  site.telemetry = () => answer({ error: "invalid_telemetry" }, 400);
  client.record("snapshot_timeout", 8); await client.loadLocks(); await tick();
  site.telemetry = () => answer(null, 204);
  site.clock += 16000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 1, "a refused report is never sent again");
  site.telemetry = () => answer({ error: "json_required" }, 415);
  client.record("snapshot_timeout", 8); site.clock += 16000; await client.loadLocks(); await tick();
  site.telemetry = () => answer(null, 204); site.clock += 16000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 2);
  proof.refusedDrops = true;

  // 429 keeps the counts and waits for Retry-After; so do 5xx and network
  // failures, which are never themselves recorded.
  site = gateway(); client = create(site); await client.ready();
  site.telemetry = () => answer({ error: "telemetry_throttled" }, 429, { "Retry-After": "40" });
  client.record("command_timeout", 8); await client.loadLocks(); await tick();
  site.telemetry = () => answer({ error: "unavailable" }, 503);
  client.record("command_timeout", 9);
  site.clock += 16000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 1, "Retry-After holds the next report");
  site.clock += 25000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 2);
  site.telemetry = () => { throw new TypeError("network down"); };
  site.clock += 16000; await client.loadLocks(); await tick();
  site.telemetry = () => answer(null, 204);
  site.clock += 16000; await client.loadLocks(); await tick();
  assert.equal(sent(site), 4);
  assert.deepEqual(site.reports.map(report => report.body.events), [{ command_timeout: { count: 1, waits: [8] } },
    ...Array(3).fill({ command_timeout: { count: 2, waits: [8, 9] } })],
    "kept counts return unchanged; failed reports add nothing");
  proof.throttledKeeps = proof.noRecursion = true;

  // Counts stop at 1000 a kind with the latest 16 waits; the fullest report fits 2048 bytes.
  site = gateway(); client = create(site, { token: String("terminal-credential") }); await client.ready();
  for (let index = 0; index < 1005; index++) client.record("policy_timeout", index / 10);
  for (const kind of ["locks_timeout", "command_timeout", "stream_error", "stream_refused", "snapshot_timeout"])
    for (let index = 0; index < 20; index++) client.record(kind, 599.9999 - index / 1000);
  client.record("policy_timeout", 700); client.record("policy_timeout", -3); client.record("made_up", 1);
  await client.loadLocks(); await tick();
  const full = site.reports[0];
  assert.equal(full.body.events.policy_timeout.count, 1000);
  assert.deepEqual(full.body.events.policy_timeout.waits, [99.1, 99.2, 99.3, 99.4, 99.5, 99.6, 99.7, 99.8, 99.9, 100, 100.1, 100.2, 100.3, 100.4, 600, 0]);
  assert(Object.values(full.body.events).every(entry => entry.waits.length === 16 && entry.waits.every(value => value >= 0 && value <= 600)));
  assert(!("made_up" in full.body.events));
  const bytes = Buffer.byteLength(JSON.stringify({ ...full.body, client: "web", protocol: "http/1.1" }));
  assert(bytes <= 2048, bytes + " bytes");
  assert.equal(full.bearer, "Bearer terminal-credential"); assert.equal(full.csrf, null);  // lint: allow — fixture credential
  proof.caps = proof.sizeCap = true;

  // A command that outlives commandTimeout is counted once and still answers.
  site = gateway();
  let late; const original = site.fetch;
  site.fetch = async (url, init) => new URL(url).pathname.endsWith("/commands")
    ? new Promise(resolve => { site.clock += 8000; late = () => resolve(answer({ accepted: true }, 202)); }) : original(url, init);
  client = create(site); await client.ready(); await client.loadPolicy("den");
  const command = client.command("den", "source", "off"); await wait(40); site.clock += 30000; late();
  assert.deepEqual(await command, { accepted: true });
  await client.loadLocks(); await tick();
  assert.deepEqual(site.reports[0].body.events, { command_timeout: { count: 1, waits: [8] } });
  proof.commandTimeout = true;

  // Streams: a terminal stream refused with 429 is stream_refused; any other
  // failure is stream_error, with the wait only while it never opened.
  site = gateway(); client = create(site); await client.ready();
  const streams = [];
  class Stream { constructor(url) { this.url = url; this.protocol = "http/1.1"; this.readyState = 0; streams.push(this); } close() { this.readyState = 2; } }
  const Events = client.eventSource(Stream), stream = new Events("/api/v1/events/remote"); await tick(); await tick();
  site.clock += 2000; streams[0].onerror({ status: 429 });
  site.clock += 3000; streams[0].onerror({ status: null });
  streams[0].onopen({}); site.clock += 60000; streams[0].onerror({});
  stream.close(); streams[0].onerror({});
  await client.loadLocks(); await tick();
  assert.deepEqual(site.reports[0].body, { client: "tui", protocol: "http/1.1",
    events: { stream_refused: { count: 1, waits: [] }, stream_error: { count: 2, waits: [3] } } });
  proof.streams = true;

  // The terminal's EventSource names its protocol and forgets an earlier attempt's status.
  let answered = 0;
  const server = http.createServer((request, response) => { answered++; response.writeHead(429); response.end(); server.close(); request.socket.destroy(); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const statuses = [], terminal = new EventSource("http://127.0.0.1:" + server.address().port + "/api/v1/events/remote", { retry: 5 });
  terminal.onerror = event => statuses.push(event.status);
  for (let turn = 0; turn < 100 && statuses.length < 2; turn++) await wait(10);
  terminal.close();
  assert.equal(terminal.protocol, "http/1.1"); assert.equal(answered, 1); assert.deepEqual(statuses.slice(0, 2), [429, null]);
  proof.terminalStream = true;

  // The page: its protocol from PerformanceResourceTiming, normalised.
  const entries = (named, all) => ({ getEntriesByName: () => named, getEntriesByType: () => all });
  const resource = (name, nextHopProtocol) => ({ name: origin + name, nextHopProtocol });
  for (const [timing, expected] of [
    [entries([resource("/x", "h2")], []), "h2"], [entries([resource("/x", "H2C")], []), "h2"],
    [entries([resource("/x", "http/1.1")], []), "http/1.1"], [entries([resource("/x", "http/1.0")], []), "http/1.1"],
    [entries([resource("/x", "h3-29")], []), "h3"], [entries([resource("/x", "spdy/3")], []), "unknown"],
    [entries([], [resource("/a", "h2"), { name: "https://other.example/b", nextHopProtocol: "http/1.1" }]), "h2"],
    [entries([resource("/x", "")], [resource("/a", "http/1.1"), resource("/b", "")]), "http/1.1"],
    [{ getEntriesByName() { throw new Error("no timing"); } }, "unknown"], [undefined, "unknown"]]) {
    const view = page({ performance: timing });
    view.client.record("stream_error", 1, "/api/v1/events/remote"); view.hide(); await tick();
    assert.equal(view.posted()[0].protocol, expected, expected);
  }
  proof.protocol = true;

  // Hiding the page posts the counts as a keepalive JSON request, never a
  // beacon: a beacon is only queued, and may never arrive. The counts are
  // held until the answer: 204 clears them, a failure or no answer returns
  // them for the next report.
  let view = page({ sendBeacon: () => true });
  view.client.record("snapshot_timeout", 8, "/api/v1/events/remote"); view.hide(); await tick(); await tick();
  assert.equal(view.beacons.length, 0, "no beacon");
  let posted = view.fetches.filter(call => call.route === TELEMETRY);
  assert.equal(posted.length, 1); assert.equal(posted[0].init.method, "POST"); assert.equal(posted[0].init.keepalive, true);
  assert.equal(posted[0].init.headers.get("Content-Type"), "application/json");
  assert.deepEqual(JSON.parse(posted[0].init.body), { client: "web", protocol: "unknown", events: { snapshot_timeout: { count: 1, waits: [8] } } });
  view.hide(); await tick(); assert.equal(view.posted().length, 1, "204 cleared them: nothing left to send");
  for (const failing of [() => answer({ error: "unavailable" }, 503), () => { throw new TypeError("network down"); }]) {
    let fail = true;
    view = page({ reports: () => fail ? failing() : answer(null, 204) });
    view.client.record("stream_error", 2); view.hide(); await tick(); await tick();
    fail = false; view.show(); view.hide(); await tick(); await tick();
    assert.deepEqual(view.posted().map(report => report.events), [{ stream_error: { count: 1, waits: [2] } }, { stream_error: { count: 1, waits: [2] } }],
      "an undelivered report is sent again");
    view.hide(); await tick(); assert.equal(view.posted().length, 2);
  }
  // A report the page hid before its answer is held, not lost, while it is in flight.
  let deliver; view = page({ reports: () => new Promise(resolve => { deliver = status => resolve(answer(null, status)); }) });
  view.client.record("stream_error", 3); view.hide(); await tick();
  deliver(503); await tick(); await tick();
  deliver = null; view.show(); view.hide(); await tick();
  assert.equal(view.posted().length, 2); assert.deepEqual(view.posted()[1].events, { stream_error: { count: 1, waits: [3] } });
  deliver(204); await tick(); await tick();
  view.show(); view.hide(); await tick(); assert.equal(view.posted().length, 2);
  proof.keepaliveReport = proof.heldUntilAnswered = true;

  // Nothing is counted while the page is hidden, nor any wait that spanned a
  // hidden (perhaps suspended) page: it measured the page, not the gateway.
  view = page();
  view.hide(); view.client.record("stream_error", 1); view.client.record("snapshot_timeout", 8); view.client.record("stream_refused");
  view.show(); view.hide(); await tick();
  assert.equal(view.posted().length, 0, "nothing recorded while hidden");
  site = gateway();
  const held = [];
  site.fetch = (original => async (url, init) => new URL(url).pathname.endsWith("/commands")
    ? new Promise(resolve => held.push(() => resolve(answer({ accepted: true }, 202)))) : original(url, init))(site.fetch);
  client = create(site); await client.ready(); await client.loadPolicy("den");
  // Overdue while hidden: not counted.
  let pending = client.command("den", "source", "off"); await tick(); await tick();
  client.setActive(false); site.clock += 9000; await wait(40); held.shift()(); await pending;
  // Sent before the page hid, overdue after it was shown again: not counted.
  client.setActive(true); pending = client.command("den", "source", "off"); await tick(); await tick();
  client.setActive(false); site.clock += 2000; client.setActive(true); site.clock += 7000; await wait(40); held.shift()(); await pending;
  // Sent and overdue while shown: counted.
  site.clock += 1000; pending = client.command("den", "source", "off"); await tick(); await tick();
  site.clock += 8000; await wait(40); held.shift()(); await pending;
  site.clock += 60000; await client.loadLocks(); await tick();
  assert.deepEqual(site.reports.map(report => report.body.events), [{ command_timeout: { count: 1, waits: [8] } }]);
  proof.hiddenNotCounted = true;

  // Mock and native pages, and mock or native clients, record and send nothing.
  for (const query of ["backend=mock", "backend=native"]) {
    view = page({ query });
    view.client.record("stream_error", 1); view.hide(); await tick();
    assert.equal(await view.client.flushTelemetry(), false);
    assert(!view.fetches.some(call => call.route === TELEMETRY));
  }
  for (const options of [{ mock: true }, { native: true }]) {
    site = gateway(); client = create(site, options);
    client.record("policy_timeout", 1); assert.equal(await client.flushTelemetry(true), false);
    assert.equal(sent(site), 0);
  }
  proof.quietModes = true;

  // A labelled client (thermite tui, the handheld bridge) reports as its kind.
  site = gateway(); client = create(site, { client: "tui/handheld.example" }); await client.ready();
  client.record("command_timeout", 3); await client.loadLocks(); await tick();
  assert.equal(site.reports[0].body.client, "tui");
  proof.labelledKind = true;

  process.stdout.write(JSON.stringify(proof));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
