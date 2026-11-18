// web/remote/alerts.js beside a fake gateway feed and a fake clock: what
// Home is given to draw for each answer, what "nothing is firing" may
// claim, the one doubtful reading held back, and when it reads. stdin is
// the house model (remote_model.build). Nothing leaves the process; every
// wait is a finite tick of the fake clock.
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const { clock } = require("./remote_dom");
const house = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(path.resolve(__dirname, "../web/remote/alerts.js"), "utf8");
// The sample feed, as a page asked for the mock loads it.
const sample = fs.readFileSync(path.resolve(__dirname, "../web/remote/alerts-mock.js"), "utf8");
const ROOM = house.rooms[0], DEVICE = ROOM.display;

// The model with a feed whose next answers are queued: an object is
// answered, an Error refused, "hang" never settles. reads counts the asks.
function feed() {
  const time = clock();
  const context = vm.createContext({ console, Promise, JSON, Math, Object, Array, Number, String, Boolean, Error, Set, Map,
    Date: class extends Date { static now() { return time.now(); } } });
  vm.runInContext(source, context, { filename: "alerts.js" });
  vm.runInContext(sample, context, { filename: "alerts-mock.js" });
  const made = { time, Alerts: context.RemoteAlerts, queue: [], reads: 0, last: null };
  made.model = context.RemoteAlerts.create({ house, now: time.now, setTimeout: time.setTimeout, clearTimeout: time.clearTimeout,
    read() {
      made.reads++;
      const next = made.queue.length ? made.queue.shift() : made.last;
      if (next === "hang") return new Promise(() => {});
      made.last = next;
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
    } });
  made.changes = 0; made.model.subscribe(() => { made.changes++; });
  // Answers one after another: each is read, and the view after it is returned.
  made.after = async (...answers) => {
    const views = [];
    for (const answer of answers) {
      made.queue.push(answer);
      if (!made.started) { made.started = true; made.model.setActive(true); await time.tick(1); } else await time.tick(made.Alerts.INTERVAL_MS);
      views.push(made.model.view());
    }
    return views.length === 1 ? views[0] : views;
  };
  return made;
}

const seconds = (made) => Math.floor(made.time.now() / 1000);
const answer = (monitoring, alerts, firing) => ({ version: 1, monitoring, evaluated: monitoring === "unreachable" ? null : 1,
  firing: monitoring === "unreachable" ? null : firing === undefined ? alerts.length : firing, alerts });
const alert = (severity, title, more) => Object.assign({ name: "Thermite" + title.replace(/\W/g, ""), severity, title, text: title + " text.", since: null }, more);

(async () => {
  const out = {};

  {
    // Before anything is read nothing is claimed; a healthy answer is the one state that says so.
    const made = feed();
    out.unread = made.model.view();
    out.clear = await made.after(answer("ok", []));
    assert.equal(made.reads, 1);
  }

  {
    // Every answer the gateway could give, and those it should not: only
    // monitoring "ok" with a count of 0 and no alert is "clear".
    const cases = [];
    const lists = [[], [alert("warning", "One")], [{ severity: "critical" }], "none"];
    for (const monitoring of ["ok", "stalled", "unreachable", "weird", undefined]) {
      for (const firing of [0, 1, 3, null, undefined, -1, "0"]) {
        for (const alerts of lists) {
          for (const version of [1, 2]) cases.push({ version, monitoring, evaluated: 1, firing, alerts: alerts === "none" ? undefined : alerts });
        }
      }
    }
    cases.push(null, "ok", [], new Error("refused"));
    let clear = 0;
    for (const given of cases) {
      // Twice: the first doubtful reading is held back.
      const made = feed(), shown = (await made.after(given, given))[1];
      const healthy = Boolean(given) && !(given instanceof Error) && given.version === 1 && given.monitoring === "ok" && given.firing === 0
        && Array.isArray(given.alerts) && given.alerts.length === 0;
      assert.equal(shown.state === "clear", healthy, JSON.stringify(given));
      assert.equal(shown.summary === made.Alerts.SUMMARY.clear, healthy, JSON.stringify(given));
      assert.notEqual(shown.summary, "", JSON.stringify(given));
      if (!healthy) assert.notEqual(shown.tone, "ok", JSON.stringify(given));
      clear += healthy;
    }
    out.matrix = { cases: cases.length, clear };
  }

  {
    // What is shown: the feed's order, the catalogue's words, and labels for the room, the device and the check.
    const made = feed(), at = seconds(made);
    const shown = await made.after(answer("ok", [
      alert("critical", "Control path", { since: at - 3 * 3600, room: ROOM.id, zone: ROOM.id, device: DEVICE, check: "thing", check_text: "Not responding to control" }),
      alert("critical", "Lock", { since: at - 600, zone: ROOM.id }),
      alert("warning", "Unknown ids", { since: at - 30, room: "no_such_room", zone: "no_such_zone", device: "no_such_device", check: "port", check_text: "Control port closed" }),
      alert("warning", "Disk", { since: at - 5 * 86400, service: "node" }),
      alert("loud", "A severity of later", {}),
    ], 5));
    out.lines = shown.lines.map((line) => [line.severity, line.title, line.where, line.text, line.since]);
    out.firing = [shown.state, shown.tone, shown.summary, shown.more, shown.moreText, shown.marks];
    for (const line of shown.lines) assert(!/no_such|_/.test(line.where), line.where);
    // A room's mark is the worst of its alerts.
    const worst = await made.after(answer("ok", [alert("warning", "First", { zone: ROOM.id }), alert("critical", "Second", { room: ROOM.id })]));
    const mild = await made.after(answer("ok", [alert("warning", "First", { zone: ROOM.id })]));
    out.marks = [worst.marks, mild.marks, mild.tone, mild.summary];
  }

  {
    // Alerts the viewer is not given are counted, with or without any that are.
    const made = feed();
    const some = await made.after(answer("ok", [alert("critical", "Control path", { zone: ROOM.id })], 3));
    const none = await made.after(answer("ok", [], 2));
    const one = await made.after(answer("ok", [], 1));
    // An alert this page cannot show (it has no title) is counted with them.
    const untitled = await made.after(answer("ok", [{ severity: "critical", text: "No title." }], 1));
    out.more = [some, none, one, untitled].map((shown) => [shown.state, shown.tone, shown.summary, shown.lines.length, shown.more, shown.moreText]);
  }

  {
    // Monitoring that cannot be read says so, and never that nothing fires.
    const made = feed();
    const [, left] = await made.after(answer("stalled", [alert("critical", "Control path", { zone: ROOM.id })], 2),
      answer("stalled", [alert("critical", "Control path", { zone: ROOM.id })], 2));
    const silent = await made.after(answer("stalled", []));
    const unreachable = await made.after(Object.assign(answer("unreachable", []), { alerts: [alert("critical", "Stale", {})] }));
    const refused = await made.after(new Error("offline"));
    out.unreadable = [left, silent, unreachable, refused].map((shown) => [shown.state, shown.tone, shown.summary, shown.lines.length, shown.more, Object.keys(shown.marks).length]);
  }

  {
    // One reading that is not "ok" may be a restart: it is held back and
    // read again sooner; the second is shown; an "ok" ends the doubt.
    const made = feed(), { INTERVAL_MS, CONFIRM_MS } = made.Alerts;
    const states = [(await made.after(answer("ok", []))).state];
    made.queue.push(answer("unreachable", []));
    await made.time.tick(INTERVAL_MS);
    states.push(made.model.view().state);
    const before = made.reads;
    made.queue.push(answer("unreachable", []));
    // The page opened one tick before its first read's answer was looked at.
    await made.time.tick(CONFIRM_MS - 2); assert.equal(made.reads, before, "the next read waits for the gateway to ask anew");
    await made.time.tick(1); assert.equal(made.reads, before + 1);
    states.push(made.model.view().state);
    // Already doubted: another kind of trouble is shown at once.
    states.push((await made.after(answer("stalled", []))).state);
    states.push((await made.after(answer("ok", []))).state);
    // A blip: one unanswered read between two healthy ones never shows.
    made.queue.push(new Error("restarting"), answer("ok", []));
    await made.time.tick(INTERVAL_MS); states.push(made.model.view().state);
    await made.time.tick(CONFIRM_MS); states.push(made.model.view().state);
    out.doubt = states;
    // The first reading of a page just opened is held back as well: nothing is claimed meanwhile.
    const opened = feed();
    const first = await opened.after(answer("stalled", [alert("critical", "Control path", { zone: ROOM.id })]));
    await opened.time.tick(CONFIRM_MS);
    out.opened = [first.state, first.lines.length, opened.model.view().state, opened.model.view().lines.length];
    // A preview's sample cannot blip: with confirmMs 0 it is read again at once.
    const preview = feed();
    preview.model = preview.Alerts.create({ house, now: preview.time.now, confirmMs: 0, setTimeout: preview.time.setTimeout, clearTimeout: preview.time.clearTimeout,
      read: () => Promise.resolve(answer("unreachable", [])) });
    preview.model.setActive(true); await preview.time.tick(1);
    out.preview = preview.model.view().state;
    preview.model.stop();
  }

  {
    // It reads at once, every INTERVAL_MS while shown, not at all while
    // hidden, and at once when shown again; what was read before the page
    // went away does not speak for now.
    const made = feed(), { INTERVAL_MS, CURRENT_MS } = made.Alerts;
    await made.after(answer("ok", []));
    await made.time.tick(INTERVAL_MS * 4);
    const polled = made.reads;
    made.model.setActive(false);
    await made.time.tick(INTERVAL_MS * 10);
    const hidden = made.reads - polled;
    // Still within the time a reading speaks for: it is kept. Then too old.
    const kept = feed(); await kept.after(answer("ok", []));
    kept.model.setActive(false); await kept.time.tick(CURRENT_MS - INTERVAL_MS); const recent = kept.model.view().state;
    made.queue.push("hang");
    const changes = made.changes;
    made.model.setActive(true); await made.time.tick(1);
    const away = [made.model.view().state, made.reads - polled, made.changes > changes];
    // One read at a time: while one hangs no other is made, and its late answer is not waited for after the page is hidden and shown.
    await made.time.tick(INTERVAL_MS * 3);
    const hung = made.reads - polled;
    made.model.setActive(false); made.queue.push(answer("ok", [])); made.model.setActive(true); await made.time.tick(1);
    out.polling = { polled, hidden, recent, away, hung, back: made.model.view().state };
    // Stopped, it reads no more and holds no timer.
    made.model.stop(); const stopped = made.reads;
    await made.time.tick(INTERVAL_MS * 4);
    out.polling.stopped = made.reads - stopped;
    // A clock stepped back does not make an old reading current.
    const stepped = feed();
    let back = 0;
    const model = stepped.Alerts.create({ house, now: () => stepped.time.now() - back, setTimeout: stepped.time.setTimeout,
      clearTimeout: stepped.time.clearTimeout, read: () => Promise.resolve(answer("ok", [])) });
    model.setActive(true); await stepped.time.tick(1);
    const before = model.view().state;
    back = 5000;
    out.polling.stepped = [before, model.view().state];
    model.stop();
  }

  {
    // The preview's samples, each through the model, and their words for the catalogue's test.
    const made = feed(), names = ["", "firing", "admin", "hidden", "house", "stalled", "silent", "unreachable", "unanswered"];
    out.samples = {}; out.catalogue = {};
    for (const name of names) {
      const model = made.Alerts.create({ house, now: made.time.now, confirmMs: 0, setTimeout: made.time.setTimeout, clearTimeout: made.time.clearTimeout,
        read: () => made.Alerts.sample(house, name) });
      model.setActive(true); await made.time.tick(2);
      const shown = model.view(); model.stop();
      out.samples[name] = [shown.state, shown.tone, shown.summary, shown.lines.map((line) => line.where), shown.more];
      const given = await made.Alerts.sample(house, name).catch(() => null);
      for (const one of (given || { alerts: [] }).alerts) out.catalogue[one.name] = [one.severity, one.title, one.text, one.check || null, one.check_text || null, one.service || null];
    }
    out.ago = [[0, 59], [0, 60], [0, 3599], [0, 3600], [0, 172799], [0, 172800], [null, 5], [10, 5]].map(([since, now]) => made.Alerts.ago(since, now));
  }

  process.stdout.write(JSON.stringify(out));
})().catch((error) => { process.stderr.write(String(error.stack).slice(0, 4000) + "\n"); process.exitCode = 1; });
