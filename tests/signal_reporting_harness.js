"use strict";
// Actual projector, acquisition/session and terminal renderer, with synthetic
// device reports and clocks. No network, device command or installed runtime.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const { createRequire } = require("node:module");
const area = path.resolve(__dirname, ".."), read = name => fs.readFileSync(path.join(area, name), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const copy = value => JSON.parse(JSON.stringify(value));
function scripts() {
  const timers = new Map(); let next = 0;
  const context = vm.createContext({ console, AbortController,
    setTimeout(fn, ms) { const id = ++next; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } });
  for (const name of ["diagnostics", "backend", "session", "topology"]) vm.runInContext(read("web/remote/" + name + ".js"), context);
  return { context, timers };
}
const { context } = scripts(), D = context.RemoteDiagnostics;
const node = (device, role, extra = {}) => ({ device, label: device.charAt(0).toUpperCase() + device.slice(1), role, ...extra });
const spec = (key, category = "video", extra = {}) => ({ item: "switch_signal", category, label: key, path: ["values", key], kind: "format", ...extra });
const source = input => ({ label: input ? "Other source" : "Computer", carries: "both", nodes: [node("computer", "source"), node("switch", "switch", { input, output: 0 }), node("screen", "display")] });
const fixture = () => ({ version: 1, rooms: { desk: { label: "Desk", sources: {
  a: { ...source(0), usb: [{ device: "usb_a", label: "Desk matrix", host: 3, ports: [1, 2] },
    { device: "usb_b", label: "Shelf matrix", host: 1, ports: [3] }] }, b: source(1) } } }, devices: {
  switch: { items: ["switch_signal"], maxAgeSeconds: 15, readings: [
    spec("Input video", "video", { selector: { path: ["values", "input"], node: "input" }, unknown: ["---", "Unknown"], noSignal: ["no signal"] }),
    spec("Output video", "video", { port: { direction: "output", number: 0 }, unknown: ["---"] }),
    spec("Output audio", "audio", { silence: ["silence"], noSignal: ["no signal"], unknown: ["Unknown"] }),
    spec("HPD", "video", { kind: "link" }), spec("EDID", "edid", { kind: "capability" }), spec("Processing", "processing", { kind: "setting" })] } } });
const snapshot = (values, observedAt = 99000, extra = {}) => ({ value: JSON.stringify({ version: 1, observedAt, values, ...extra }) });
const view = (values, options = {}) => D.create(options.manifest || fixture()).project("desk", options.source || "a",
  { switch_signal: snapshot(values, options.at || 99000, options.envelope || {}) }, options.now || 100000, options.pending, options.since, options.routeContext);
const field = (model, label) => Object.values(model.sections).flat().find(row => row.label === label);
const outline = (model, category, now = 100000) => D.outline(model, now).groups.find(group => group.id === category);
const cases = [];
function test(name, run) { run(); cases.push(name); }

test("formats_require_matching_source_and_post_transition_sample", () => {
  const values = { input: "1", "Input video": "2160p60", "Output video": "1080p60" };
  assert.equal(field(view(values), "Input video").status, "awaiting_route");
  assert.equal(field(view(values), "Input video").unassociatedValue, "2160p60");
  assert.equal(field(view({ ...values, input: "0" }), "Input video").status, "reported");
  assert.equal(field(view(values, { source: "b", pending: "a" }), "Input video").status, "awaiting_route");
  assert.equal(field(view(values, { source: "b", since: 99500 }), "Input video").status, "awaiting_route");
  assert.equal(field(view(values, { source: "b", since: 99500, at: 99900 }), "Input video").status, "reported");
});
test("no_signal_silence_unknown_and_failed_acquisition_are_distinct", () => {
  const unknown = view({ input: "0", "Input video": "---", "Output video": "---", "Output audio": "Unknown", HPD: "1" });
  assert.equal(field(unknown, "Input video").status, "not_reported");
  assert.equal(field(unknown, "Output audio").status, "not_reported");
  assert.equal(unknown.availability, "not_reported", "link flags do not prove a format");
  assert(outline(unknown, "video").summary.startsWith("0 formats"));
  assert.equal(field(view({ input: "0", "Input video": "no signal" }), "Input video").status, "no_signal");
  assert.equal(field(view({ "Output audio": "silence" }), "Output audio").status, "silence");
  const failure = view({}, { envelope: { status: "unavailable", reason: "invalid_response" } });
  assert.equal(field(failure, "Input video").status, "unavailable");
  assert.equal(field(failure, "Input video").reason, "Invalid device response");
  assert.equal(field(view({}, { envelope: { status: "unavailable", reason: "route_changed" } }), "Input video").reason, "Route changed during measurement");
  assert.equal(field(view({}, { envelope: { status: "unavailable", reason: "future_reason" } }), "Input video").reason, "Device snapshot unavailable");
  assert(outline(failure, "video").blocks.some(block => block.quiet.some(line => line.includes("Input video: Unavailable · Invalid device response"))));
  assert(outline(failure, "video").summary.includes("unavailable"));
});
test("staleness_is_recomputed_when_a_cached_model_is_drawn", () => {
  const model = view({ input: "0", "Input video": "2160p60" });
  assert.equal(outline(model, "video").stale, false);
  const late = outline(model, "video", 120000);
  assert.equal(late.stale, true);
  assert(late.blocks.some(block => block.age === "stale · 21s ago"));
  assert(late.summary.startsWith("0 formats"));
});
test("uninstrumented_hops_are_visible_in_path_order", () => {
  const rows = view({ input: "0", "Input video": "2160p60" }).sections.video;
  assert.equal(rows[0].device, "computer"); assert.equal(rows[0].status, "unsupported");
  assert.equal(rows.at(-1).device, "screen"); assert.equal(rows.at(-1).status, "unsupported");
});
test("audio_branch_retains_transport_settings_without_claiming_hdmi_as_optical", () => {
  const manifest = fixture(), route = manifest.rooms.desk.sources.a;
  route.audioNodes = [node("computer", "source"), node("switch", "switch", { input: 0, output: "optical", outputTransport: "optical" }),
    node("amp", "audio", { input: "optical", inputKind: "selector", inputTransport: "optical" })];
  manifest.devices.switch.readings.push(spec("HDMI audio out", "audio", { transports: ["hdmi"] }),
    spec("Optical processing", "processing", { kind: "setting", port: { direction: "output", number: "optical" } }));
  manifest.devices.amp = { items: ["amp_signal"], readings: [{ ...spec("Amp HDMI input", "audio", { transports: ["hdmi"], port: { direction: "input", number: "optical" } }), item: "amp_signal" }] };
  const model = view({ "HDMI audio out": "PCM", "Optical processing": "Bypass" }, { manifest });
  assert.equal(model.audioRoute.at(-1).inputTransport, "optical");
  assert.equal(field(model, "HDMI audio out"), undefined);
  assert.equal(field(model, "Amp HDMI input"), undefined);
  assert.equal(field(model, "Optical processing").value, "Bypass");
  assert(model.sections.audio.some(row => row.device === "amp" && row.status === "unsupported"));
  assert(outline(model, "audio").blocks.some(block => block.device.includes("input selector optical")));
});
test("display_only_route_does_not_report_room_audio", () => {
  const manifest = fixture(); manifest.rooms.desk.sources.a.carries = "display";
  const model = view({ "Output audio": "PCM" }, { manifest });
  assert.equal(model.sections.audio.length, 1); assert.equal(model.sections.audio[0].status, "not_applicable");
});
test("scalar_facts_have_unknown_age_and_track_metadata_is_not_a_format", () => {
  const manifest = fixture(); manifest.devices.switch.items.push("switch_codec", "switch_rate");
  manifest.devices.switch.readings.push({ item: "switch_codec", scalar: true, category: "audio", kind: "format", label: "TV codec", noSignal: ["none"] },
    { item: "switch_rate", scalar: true, category: "processing", kind: "metadata", label: "Track sample rate" });
  const model = D.create(manifest).project("desk", "a", { switch_codec: { value: "none", receivedAt: 100000 }, switch_rate: { value: "48000", receivedAt: 100000 } }, 100000);
  assert.equal(field(model, "Track sample rate").observedAt, null);
  assert.equal(field(model, "Track sample rate").kind, "metadata");
  assert.equal(field(model, "TV codec").status, "awaiting_route");
  assert.equal(field(model, "TV codec").unassociatedValue, "No signal");
  assert(outline(model, "processing").blocks.some(block => block.age === "stale · age unknown"));
});
test("unknown_audio_wiring_is_a_separate_endpoint_not_an_invented_cable", () => {
  const manifest = fixture(); manifest.rooms.desk.sources.a.audioEndpoints = [node("amp", "audio", { input: "optical", wiring: "unknown" })];
  const model = view({}, { manifest });
  assert.equal(model.audioRoute, undefined); assert.equal(model.audioEndpoints[0].device, "amp");
  assert(model.sections.path.some(row => row.label.includes("wiring unknown")));
  assert(model.sections.audio.some(row => row.device === "amp" && row.status === "unsupported"));
});
test("configured_path_lists_each_usb_matrix_without_claiming_signal_or_readback", () => {
  const manifest = fixture();
  const model = view({}, { manifest }), path = outline(model, "path");
  assert.equal(path.summary, "5 configured");
  assert.deepEqual(copy(path.blocks[0].rows.filter(row => row.label.startsWith("USB")).map(row => [row.label, row.value])),
    [["USB · Desk matrix", "HOST 3 → DEVICE 1, 2"], ["USB · Shelf matrix", "HOST 1 → DEVICE 3"]]);
  assert.equal(model.availability, "not_reported");
  assert.deepEqual(copy(D.create(manifest).items("desk", "a")), ["switch_signal"]);
  assert(![...model.sections.video, ...model.sections.audio].some(row => row.device?.startsWith("usb_")));
  for (const source of ["off", "b"]) {
    assert(!view({}, { manifest, source }).sections.path.some(row => row.label.startsWith("USB")));
  }
});
test("untrusted_item_names_and_excess_reads_never_escape_the_allowlist", () => {
  const manifest = fixture(); manifest.devices.switch.items.push("../secret", "my_password");
  manifest.devices.switch.readings.push({ ...spec("bad"), item: "../secret" }, { ...spec("secret"), item: "my_password" });
  assert.deepEqual(Array.from(D.create(manifest).items("desk", "a")), ["switch_signal"]);
  for (let i = 0; i <= D.MAX_ITEMS; i++) { const item = "extra_" + i; manifest.devices.switch.items.push(item); manifest.devices.switch.readings.push({ ...spec("extra" + i), item }); }
  assert.throws(() => D.create(manifest).items("desk", "a"), /Too many diagnostics/);
});

const house = { rooms: [{ id: "desk", label: "Desk", sources: ["a", "b"], devices: [] }], sources: { a: { label: "Computer" }, b: { label: "Other" } }, devices: {} };
const room = { id: "desk", label: "Desk", item: "loc_desk_source", options: [{ id: "a", label: "Computer" }, { id: "b", label: "Other" }, { id: "off", label: "Off" }], order: [], devices: [], controls: [] };
async function sessionCases() {
  let clock = 100000, emit, resolveRead; const calls = [];
  const { context: c } = scripts();
  const backend = { connected: true, playingOf() { return null; }, problemOf() { return null; }, subscribe(fn) { emit = fn; return () => {}; }, getRooms: async () => [room],
    getState: async () => ({ room: "desk", value: "a", pending: null, status: "current" }), cancelDiagnostics() {},
    getDiagnostics(id, source, pending, since) { calls.push({ source, pending, since }); return new Promise(resolve => { resolveRead = resolve; }); } };
  const session = c.RemoteSession.create({ backend, house, layouts: { buttons: {}, remotes: {} }, now: () => clock });
  await session.start("desk"); const opened = session.openDiagnostics();
  resolveRead(view({ input: "0", "Input video": "2160p60" })); await opened;
  clock++; emit({ type: "state", room: "desk", state: { room: "desk", value: "a", pending: "b", status: "pending" } });
  assert.equal(session.diagnostics().pending.id, "b");
  assert.equal(field(session.diagnostics(), "Input video").status, "awaiting_route");
  assert.equal(session.diagnostics().availability, "not_reported");
  assert(outline(session.diagnostics(), "video").blocks.some(block => block.age === "1s ago"));
  const oldResolve = resolveRead;
  clock++; emit({ type: "state", room: "desk", state: { room: "desk", value: "a", pending: null, status: "current" } });
  assert.equal(session.diagnostics(), null, "cancelled switching requires a new sample too");
  assert.equal(calls.at(-1).since, clock);
  oldResolve(view({ input: "1", "Input video": "old request" })); await tick();
  assert.equal(session.diagnostics(), null, "late old-context replies are ignored");
  resolveRead(view({ input: "0", "Input video": "new request" }, { at: clock + 1, now: clock + 1 })); await tick();
  assert.equal(field(session.diagnostics(), "Input video").value, "new request");
  backend.connected = false; emit({ type: "connection", connected: false });
  assert.equal(field(session.diagnostics(), "Input video").status, "awaiting_route");
  assert.equal(session.diagnostics().availability, "unavailable");
  session.stop(); cases.push("pending_cancellation_disconnect_and_late_replies_keep_context_truthful");
}

async function backendCases() {
  const manifest = fixture(), base = manifest.rooms.desk.sources.a;
  manifest.rooms.desk.routeStateItem = "loc_desk_source_lifecycle";
  base.variants = { synced: { ...copy(base), nodes: [node("computer", "source"), node("switch", "switch", { input: 1, output: 0 }), node("screen", "display")] } };
  let clock = 100000, generation = 1, synced = false, timestamp = 99000, drift = false, active = 0, peak = 0;
  const requests = [], lifecycle = () => ({ version: 1, choice: "a", synced, generation, pathReady: true });
  const { context: c } = scripts();
  class Events { constructor() { this.readyState = 0; } close() { this.readyState = 2; } }
  const backend = new c.RemoteBackend.OpenHabBackend(house, { diagnosticsManifest: manifest, EventSource: Events, now: () => clock,
    fetch: async (url, options) => {
      assert.equal(options.method, "GET"); requests.push(url); active++; peak = Math.max(peak, active); await tick(); active--;
      let value;
      if (url.includes("_lifecycle")) value = JSON.stringify(lifecycle());
      else {
        value = snapshot({ input: synced ? "1" : "0", "Input video": "2160p60" }, timestamp).value;
        if (drift) { generation++; synced = !synced; drift = false; }
      }
      return { ok: true, status: 200, text: async () => value };
    } });
  let model = await backend.getDiagnostics("desk", "a");
  assert.equal(model.route[1].input, "0"); assert.equal(field(model, "Input video").status, "reported");
  drift = true; model = await backend.getDiagnostics("desk", "a");
  assert.equal(model.routeReady, false); assert.equal(field(model, "Input video").status, "awaiting_route");
  clock = 100500; model = await backend.getDiagnostics("desk", "a");
  assert.equal(model.route[1].input, "1"); assert.equal(field(model, "Input video").status, "awaiting_route", "old sample cannot cross same-source route generation");
  timestamp = 100600; clock = 100700; model = await backend.getDiagnostics("desk", "a");
  assert.equal(field(model, "Input video").status, "reported");
  assert(peak <= 2); assert.equal(requests.filter(url => url.includes("_lifecycle")).length, 8);
  backend.stop(); cases.push("backend_brackets_samples_with_stable_same_source_route_generation");
}

function renderingCases() {
  // Export the existing renderer in this test context; its body is unchanged.
  const file = path.join(area, "tui/remote/view.js"), loaded = { exports: {} };
  const c = vm.createContext({ module: loaded, exports: loaded.exports, require: createRequire(file), RemoteDiagnostics: D });
  vm.runInContext(read("tui/remote/view.js") + "\nmodule.exports.signalContent = signalContent;", c);
  const { Grid } = require("../tui/remote/grid");
  const model = view({}, { envelope: { status: "unavailable", reason: "invalid_response" } });
  for (const width of [36, 100]) {
    const grid = new Grid(width, null, "base");
    loaded.exports.signalContent(grid, width, { session: { diagnostics: () => model }, ui: { signalGroups: ["video", "audio", "path"] }, now: 100000 });
    const text = grid.rows.map(row => row.map(cell => cell.ch).join("")).join("\n");
    assert(text.includes("Input video: Unavailable")); assert(text.replace(/\s+/g, " ").includes("No format telemetry"));
    assert(text.includes("USB · Desk matrix") && text.includes("HOST 3 → DEVICE 1, 2"));
    assert(text.includes("USB · Shelf matrix") && text.includes("HOST 1 → DEVICE 3"));
    assert(text.includes("unavailable"), "collapsed settings retain failures");
    assert(grid.rows.every(row => row.length === width));
  }
  cases.push("terminal_keeps_field_labels_and_failure_summaries_at_narrow_width");
}
function topologyCases() {
  const base = { label: "Computer", nodes: [node("computer", "source"), node("switch", "switch", { input: 0, output: 0 })],
    audioEndpoints: [node("amp", "audio", { input: "optical", inputKind: "selector", wiring: "unknown" })] };
  base.variants = { synced: { label: "Computer", nodes: [node("computer", "source"), node("switch", "switch", { input: 1, output: 0 })] } };
  const manifest = { version: 1, nodes: ["computer", "switch", "amp"].map(id => ({ id, label: id })), rooms: [{ id: "desk", label: "Desk", devices: [] }],
    routes: { desk: { a: base } }, edges: [] };
  const T = context.ThermiteTopology, parsed = T.catalogue(manifest), projected = T.project(parsed, "desk", "a", { desk: { value: "a" } });
  assert.equal(projected.paths.length, 2); assert(projected.paths.every(p => !p.confirmed));
  assert(projected.nodes.some(n => n.id === "amp"));
  assert.equal(parsed.routes.desk.a.audioEndpoints[0].inputKind, "selector");
  assert(T.videoRows(parsed, projected, { desk: { value: "a" } }, true).every(row => row.status.label === "Route unconfirmed"));
  const cable = { kind: "video", from: "computer", to: "switch", fromPort: "", toPort: "0", toZone: "2" };
  assert.equal(T.carries(cable, parsed.routes.desk.a), false);
  parsed.routes.desk.a.nodes[1].zone = "2";
  assert.equal(T.carries(cable, parsed.routes.desk.a), true);
  cable.transport = "hdmi"; parsed.routes.desk.a.nodes[1].inputTransport = "optical";
  assert.equal(T.carries(cable, parsed.routes.desk.a), false);
  const terminal = require("../tui/remote/topology");
  assert.equal(terminal.routeRows(manifest, "desk", { value: "a" }, true)[0].status, "Route unconfirmed");
  cases.push("topology_preserves_variants_and_unconnected_endpoints_without_false_confirmation");
}

function stage3bOutcomeCases() {
  const m = fixture();
  m.rooms.desk.routeStateItem = "loc_desk_source_lifecycle";
  const diag = D.create(m);
  const failedCtx = diag.context("desk", "a", { value: JSON.stringify({
    version: 1, choice: "a", synced: false, generation: 2, pathReady: false,
    status: "failed", reasons: ["matrix timeout", "display offline"]
  }) });
  assert.equal(failedCtx.ready, false);
  assert.equal(failedCtx.status, "failed");
  assert.equal(failedCtx.detail, "Route failed: matrix timeout; display offline");
  const failedModel = diag.project("desk", "a", { switch_signal: snapshot({ input: "0", "Input video": "2160p60" }) }, 100000, null, 0, failedCtx);
  assert.equal(failedModel.routeReady, false);
  assert.equal(failedModel.routeStatus, "failed");
  assert.equal(failedModel.routeDetail, "Route failed: matrix timeout; display offline");
  assert.equal(field(failedModel, "Input video").status, "awaiting_route");
  assert.equal(field(failedModel, "Input video").reason, "Route failed: matrix timeout; display offline");

  const runningCtx = diag.context("desk", "a", { value: JSON.stringify({
    version: 1, choice: "a", synced: false, generation: 3, pathReady: false,
    status: "running", waiting: ["tv_power", "matrix_input"]
  }) });
  assert.equal(runningCtx.ready, false);
  assert.equal(runningCtx.status, "running");
  assert.equal(runningCtx.detail, "Waiting on tv_power, matrix_input");
  const runningModel = diag.project("desk", "a", { switch_signal: snapshot({ input: "0", "Input video": "2160p60" }) }, 100000, null, 0, runningCtx);
  assert.equal(runningModel.routeDetail, "Waiting on tv_power, matrix_input");
  assert.equal(field(runningModel, "Input video").reason, "Waiting on tv_power, matrix_input");

  const yieldedCtx = diag.context("desk", "a", { value: JSON.stringify({
    version: 1, choice: "a", synced: false, generation: 4, pathReady: false,
    status: "yielded"
  }) });
  assert.equal(yieldedCtx.ready, false);
  assert.equal(yieldedCtx.status, "yielded");
  assert.equal(yieldedCtx.detail, "Route yielded to another request");

  // Terminal signalContent surfaces the routeDetail instead of generic waiting text.
  const file = path.join(area, "tui/remote/view.js"), loaded = { exports: {} };
  const c = vm.createContext({ module: loaded, exports: loaded.exports, require: createRequire(file), RemoteDiagnostics: D });
  vm.runInContext(read("tui/remote/view.js") + "\nmodule.exports.signalContent = signalContent;", c);
  const { Grid } = require("../tui/remote/grid");
  const grid = new Grid(100, null, "base");
  loaded.exports.signalContent(grid, 100, { session: { diagnostics: () => failedModel }, ui: { signalGroups: ["video"] }, now: 100000 });
  const text = grid.rows.map(row => row.map(cell => cell.ch).join("")).join("\n");
  assert(text.includes("Route failed: matrix timeout; display offline"), text);
  cases.push("stage3b_route_outcomes_surface_reasons_and_waiting");
}
(async () => { await sessionCases(); await backendCases(); renderingCases(); topologyCases(); stage3bOutcomeCases(); process.stdout.write(JSON.stringify({ passed: cases.length, cases })); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
