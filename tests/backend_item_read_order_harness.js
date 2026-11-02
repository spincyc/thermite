// Actual shared backend with synthetic HTTP completions and native-shaped SSE.
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const area = process.argv[2];
const context = vm.createContext({ setTimeout, clearTimeout, AbortController,
  console: { info() {}, warn() {}, error() {} } });
vm.runInContext(fs.readFileSync(path.join(area, "web/remote/backend.js"), "utf8"), context);
context.REMOTE_LAYOUTS = { buttons: { power: { label: "Power" } },
  remotes: { television: { label: "Television", keys: ["power"] } } };
const house = { rooms: [{ id: "room", label: "Room", sources: ["source"], display: "tv", audio: "avr",
  devices: ["tv", "avr"], routeProblem: "loc_room_source_problem" }],
  sources: { source: { label: "Source", device: "tv" } }, devices: {
    tv: { label: "Television", short: "TV", remote: "television", problem: "tv_problem", control: { power: "tv_power" } },
    avr: { label: "Receiver", short: "AVR", control: { power: "avr_power", mute: "avr_mute",
      volume: { item: "avr_volume", min: 0, max: 40, step: 1 },
      input: { item: "avr_input", options: [["NET", "HEOS Music"]] }, playback: { item: "avr_playback" } } }
  } };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
class FakeEvents { close() {} }
function client() {
  const reads = [], writes = [];
  const backend = new context.RemoteBackend.OpenHabBackend(house, { EventSource: FakeEvents,
    fetch(url, init) {
      if (init.method !== "GET") {
        writes.push({ url, command: init.body });
        return Promise.resolve({ ok: true, text: async () => "" });
      }
      const request = deferred(); reads.push({ url, ...request }); return request.promise;
    } });
  function event(item, value) {
    backend.onMessage(JSON.stringify({ type: "ItemStateUpdatedEvent", topic: "openhab/items/" + item + "/stateupdated",
      payload: JSON.stringify({ type: "String", value }) }));
  }
  function reading() {
    const first = reads.length, done = backend.readAll(), pending = reads.slice(first);
    return { done, release(values) {
      for (const read of pending) {
        const item = read.url.split("/").at(-2);
        const value = Object.hasOwn(values, item) ? values[item] : item === "loc_room_source" ? "source" : "NULL";
        read.resolve({ ok: true, status: 200, text: async () => value });
      }
    } };
  }
  return { backend, reads, writes, event, reading,
    power: async () => (await backend.getDevice("tv")).power,
    problem: () => backend.problemOf("tv") };
}
const cases = {};
async function probe(name, run) {
  const c = client();
  try { cases[name] = await run(c); }
  catch (error) { cases[name] = { pass: false, error: error.message }; }
  finally { c.backend.stop(); }
}
(async () => {
  await probe("new_power_and_sticky_error_sse_win", async c => {
    const old = c.reading(); c.event("loc_room_source", "source");
    c.event("tv_power", "ON"); c.event("tv_problem", "Synthetic native failure");
    old.release({ tv_power: "OFF", tv_problem: "UNDEF" }); await old.done;
    await c.backend.press("room", "tv", "power");
    return { pass: await c.power() === "ON" && c.problem() === "Synthetic native failure"
      && c.writes.length === 1 && c.writes[0].command === "OFF", power: await c.power(), problem: c.problem(), mockWrites: c.writes };
  });
  await probe("same_value_sse_invalidates_older_get", async c => {
    c.event("tv_power", "OFF"); c.event("tv_problem", "Synthetic native failure");
    const old = c.reading(); c.event("tv_power", "OFF"); c.event("tv_problem", "Synthetic native failure");
    old.release({ tv_power: "ON", tv_problem: "UNDEF" }); await old.done;
    return { pass: await c.power() === "OFF" && c.problem() === "Synthetic native failure" };
  });
  await probe("newer_get_supersedes_older_get", async c => {
    const old = c.reading(), latest = c.reading();
    latest.release({ tv_power: "OFF", tv_problem: "Synthetic native failure" }); await latest.done;
    old.release({ tv_power: "ON", tv_problem: "UNDEF" }); await old.done;
    return { pass: await c.power() === "OFF" && c.problem() === "Synthetic native failure" };
  });
  for (const lifecycle of ["hide_resume", "stop", "stream_restart"]) await probe(lifecycle + "_rejects_old_get", async c => {
    c.event("tv_power", "ON"); c.event("tv_problem", "Synthetic native failure"); const old = c.reading();
    if (lifecycle === "hide_resume") { c.backend.setActive(false); c.backend.setActive(true); }
    else if (lifecycle === "stop") c.backend.stop();
    else c.backend.listen();
    old.release({ tv_power: "OFF", tv_problem: "UNDEF" }); await old.done;
    return { pass: await c.power() === "ON" && c.problem() === "Synthetic native failure" };
  });
  await probe("independent_items_keep_current_get", async c => {
    const old = c.reading(); c.event("tv_power", "ON");
    old.release({ tv_power: "OFF", avr_power: "ON", avr_volume: "22", avr_mute: "OFF", avr_input: "NET", avr_playback: "PLAY" }); await old.done;
    const avr = await c.backend.getDevice("avr");
    return { pass: await c.power() === "ON" && avr.power === "ON" && avr.volume === "22"
      && avr.mute === "OFF" && avr.input === "NET" && avr.playback === "PLAY" };
  });
  await probe("fresh_get_updates_and_clears_recovered_error", async c => {
    c.event("tv_problem", "Synthetic old failure"); const fresh = c.reading();
    fresh.release({ tv_power: "ON", tv_problem: "UNDEF" }); await fresh.done;
    return { pass: await c.power() === "ON" && c.problem() === null };
  });
  await probe("inactive_or_stopped_read_all_issues_no_requests", async c => {
    c.backend.setActive(false); const before = c.reads.length, hidden = c.reading(); hidden.release({}); await hidden.done;
    c.backend.stop(); const stopped = c.reading(); stopped.release({}); await stopped.done;
    return { pass: c.reads.length === before, requestsAfterInactive: c.reads.length - before };
  });
  const result = { cases, actualNetworkRequests: 0, actualDeviceCommands: 0 };
  process.stdout.write(JSON.stringify(result) + "\n");
  if (Object.values(cases).some(proof => !proof.pass)) process.exitCode = 1;
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
