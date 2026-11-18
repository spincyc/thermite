/*
 * The house's firing alerts, as Home shows them: one model of what the
 * gateway's feed last said (GET /api/v1/alerts, oh_identity.alerts), read
 * by home.js alerts(). No page decides anything about an alert itself.
 *
 *   RemoteAlerts.create({ read, house, now, confirmMs, setTimeout, clearTimeout })
 *     read() resolves to the feed's answer and rejects when the gateway
 *     does not give one; it must settle (access.js alerts() gives up
 *     after its deadline). The model reads at once and every INTERVAL_MS
 *     while it is active (setActive: a hidden page reads nothing and
 *     reads at once when shown again), one read at a time.
 *   .view()        what to draw, below
 *   .subscribe(fn) fn() whenever view() may have changed
 *   .setActive(on), .stop()
 *
 * view() is
 *
 *   { state, tone, summary, lines, more, marks }
 *
 * state   "reading"      nothing read yet, or what was read is too old to
 *                        speak for now (the page was away)
 *         "clear"        monitoring is evaluating its rules and nothing
 *                        is firing: the only state that says so
 *         "firing"       monitoring is evaluating, and something fires
 *         "stalled"      Prometheus answers but its rules are not being
 *                        evaluated; lines are what it last left
 *         "unreachable"  the gateway cannot read Prometheus
 *         "unanswered"   the gateway gave no answer this page can read
 * tone    "" | "ok" | "warning" | "critical": the worst of what is shown;
 *         alerts that cannot be read are a warning themselves
 * summary one sentence for the state, never empty but while reading
 * lines   the alerts this viewer is given, in the feed's order (critical
 *         first, then longest active):
 *           { severity, title, where, text, since, zone, room }
 *         where names the room, the device and the check by their labels
 *         ("Example West · Example Tv · Not responding to control"), a
 *         house-wide alert its service; an id this page's house does not
 *         have is left out, never shown
 * more    how many alerts are firing that this viewer is not given (those
 *         about the house as a whole for anyone but an Admin, any beyond
 *         the feed's fifty, any its catalogue does not name), with their
 *         sentence in moreText; 0 and "" when there are none or the count
 *         is not known
 * marks   { <room id>: "critical" | "warning" } for the rooms a line is
 *         about, so a room's row can carry the worst of its alerts
 *
 * An empty list is not good news by itself. Prometheus answers with an
 * empty list while its rules are not loaded, and the gateway with one
 * when it cannot ask: only state "clear" may say that nothing is firing,
 * and it needs monitoring "ok", firing 0 and no alert in the same answer,
 * read within CURRENT_MS.
 *
 * One reading that is not "ok" may be a restart: the first is held back
 * and read again after CONFIRM_MS (past the gateway's shared read of 15 s);
 * the second is shown. What was shown before stays for that long, and no
 * longer than CURRENT_MS. A preview, whose sample cannot blip, passes
 * confirmMs 0.
 */
(function (global) {
  "use strict";
  // The gateway asks for a read every 30 s while Home is shown.
  const INTERVAL_MS = 30000;
  // A doubtful reading is read again once the gateway would ask Prometheus anew.
  const CONFIRM_MS = 16000;
  // How long a reading speaks for the present: two reads and the wait for the second's answer.
  const CURRENT_MS = 75000;
  const MONITORING = ["ok", "stalled", "unreachable"];
  const SUMMARY = {
    clear: "Nothing is firing.",
    stalled: "Monitoring is not evaluating its rules, so alerts cannot be read.",
    left: "Monitoring is not evaluating its rules. It last left:",
    unreachable: "Monitoring does not answer, so alerts cannot be read.",
    unanswered: "thermite does not answer, so alerts cannot be read."
  };
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const text = (value) => (typeof value === "string" && value.trim() ? value : null);
  const count = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
  const plural = (number, word) => number + " " + word + (number === 1 ? "" : "s");

  // The feed's answer as the model keeps it, or null for one it cannot
  // read: a version it does not know, a monitoring state it has no words
  // for, an "ok" without its count. An alert without a title is not shown,
  // and so is counted with those the viewer is not given.
  function parse(answer) {
    if (!answer || typeof answer !== "object" || answer.version !== 1 || !MONITORING.includes(answer.monitoring)
      || !Array.isArray(answer.alerts)) return null;
    const firing = count(answer.firing);
    if (answer.monitoring === "ok" && firing === null) return null;
    const alerts = [];
    answer.alerts.forEach((alert) => {
      if (!alert || typeof alert !== "object" || !text(alert.title)) return;
      const kept = { severity: alert.severity === "critical" ? "critical" : "warning", title: alert.title,
        text: text(alert.text) || "", since: count(alert.since) };
      ["room", "zone", "device", "service", "check_text"].forEach((field) => { if (text(alert[field])) kept[field] = alert[field]; });
      alerts.push(kept);
    });
    // unshown: the alerts of the answer this page cannot show, at least as many as are counted as not given.
    return { kind: answer.monitoring, firing: firing, evaluated: count(answer.evaluated),
      alerts: answer.monitoring === "unreachable" ? [] : alerts,
      unshown: answer.monitoring === "unreachable" ? 0 : answer.alerts.length - alerts.length };
  }

  // How long ago, in the largest unit that fits: "3 min", "2 h", "5 d".
  function ago(since, now) {
    if (since === null || !(now >= since)) return "";
    const minutes = Math.floor((now - since) / 60);
    if (minutes < 1) return "under a minute";
    if (minutes < 60) return minutes + " min";
    return minutes < 2880 ? Math.floor(minutes / 60) + " h" : Math.floor(minutes / 1440) + " d";
  }

  function create(options) {
    const house = options.house || { rooms: [], devices: {} };
    const now = options.now || (() => Date.now());
    const later = options.setTimeout || ((fn, ms) => global.setTimeout(fn, ms)), cancel = options.clearTimeout || ((id) => global.clearTimeout(id));
    const confirm = options.confirmMs === undefined ? CONFIRM_MS : options.confirmMs;
    const places = Object.create(null);
    (house.rooms || []).concat(house.speakerRooms || []).forEach((room) => { if (!places[room.id]) places[room.id] = room.label; });
    const listeners = [];
    // shown: the reading view() draws, {kind, firing, evaluated, alerts, at}, or null.
    // doubted: a reading that was not "ok" has been held back, and the next one counts.
    let shown = null, doubted = false, active = false, stopped = false, timer = null, reading = false, turn = 0;

    function emit() { listeners.slice().forEach((listener) => { try { listener(); } catch (error) { console.error(error); } }); }

    function take(next) {
      next.at = now();
      const held = next.kind !== "ok" && !doubted && (!shown || shown.kind === "ok");
      doubted = next.kind !== "ok";
      if (!held) shown = next;
      emit();
      return held;
    }

    function schedule(ms) {
      cancel(timer); timer = null;
      if (!active || stopped) return;
      timer = later(() => { timer = null; read(); }, ms);
      if (timer && timer.unref) timer.unref();
    }

    function read() {
      if (reading || !active || stopped) return;
      reading = true;
      const mine = ++turn;
      const settle = (next) => {
        reading = false;
        // An answer to a page since hidden or stopped says nothing of now.
        if (mine !== turn || !active || stopped) return;
        schedule(take(next) ? confirm : INTERVAL_MS);
      };
      let asked;
      try { asked = Promise.resolve(options.read()); } catch (error) { asked = Promise.reject(error); }
      asked.then((answer) => settle(parse(answer) || { kind: "unanswered", firing: null, evaluated: null, alerts: [] }),
        () => settle({ kind: "unanswered", firing: null, evaluated: null, alerts: [] }));
    }

    function where(alert) {
      if (alert.service) return alert.service;
      const place = places[alert.zone] || places[alert.room] || null;
      const device = alert.device && own(house.devices || {}, alert.device) ? house.devices[alert.device].label : null;
      return [place, device, alert.check_text || null].filter(Boolean).join(" · ");
    }

    function view() {
      const at = now();
      const current = shown && at - shown.at <= CURRENT_MS && at >= shown.at;
      if (!shown || (!current && shown.kind === "ok")) {
        return { state: "reading", tone: "", summary: "", lines: [], more: 0, moreText: "", marks: {} };
      }
      const lines = shown.alerts.map((alert) => ({ severity: alert.severity, title: alert.title, where: where(alert), text: alert.text,
        since: ago(alert.since, Math.floor(at / 1000)), zone: alert.zone || null, room: alert.room || null }));
      const more = Math.max(shown.firing === null ? 0 : shown.firing - lines.length, shown.unshown || 0, 0);
      const critical = lines.filter((line) => line.severity === "critical").length, warnings = lines.length - critical;
      const marks = {};
      lines.forEach((line) => {
        const id = own(places, line.zone || "") ? line.zone : own(places, line.room || "") ? line.room : null;
        if (id && marks[id] !== "critical") marks[id] = line.severity;
      });
      let state = shown.kind, summary;
      if (state === "ok") {
        state = lines.length || more ? "firing" : "clear";
        summary = state === "clear" ? SUMMARY.clear
          : [critical ? plural(critical, "critical alert") : "", warnings ? plural(warnings, "warning") : ""].filter(Boolean).join(" and ")
            || plural(more, "alert") + " firing";
      } else {
        summary = state === "stalled" && lines.length ? SUMMARY.left : SUMMARY[state];
      }
      const shownMore = state === "firing" || state === "stalled" ? more : 0;
      return { state: state, tone: state === "clear" ? "ok" : critical ? "critical" : "warning", summary: summary, lines: lines,
        more: shownMore,
        moreText: !shownMore ? "" : (lines.length ? "and " + shownMore + " more that " + (shownMore === 1 ? "is" : "are") : (shownMore === 1 ? "It is" : "They are"))
          + " not shown here. An Admin sees those about the house as a whole.",
        marks: marks };
    }

    return {
      view: view,
      subscribe(listener) { listeners.push(listener); return () => { const index = listeners.indexOf(listener); if (index >= 0) listeners.splice(index, 1); }; },
      setActive(on) {
        const next = Boolean(on) && !stopped;
        if (next === active) return;
        active = next;
        // What a hidden page was waiting for is not waited for: shown again, it asks anew.
        turn++; reading = false; cancel(timer); timer = null;
        if (active) { emit(); read(); }
      },
      stop() { stopped = true; active = false; turn++; cancel(timer); timer = null; }
    };
  }

  // The feed's answers a preview shows (?backend=mock&alerts=<name>), in
  // the feed's own shape and the catalogue's words (tests/test_home_alerts.py
  // holds them to oh_metrics.alerts), about the sample house's first room
  // and its display. Its alerts began before the page was opened, and age.
  const OPENED = Math.floor(Date.now() / 1000);
  function sample(house, name) {
    const room = (house.rooms || [])[0] || {}, at = OPENED;
    const failing = { name: "ThermiteCheckFailing", severity: "critical", title: "A room's control path needs attention",
      text: "A device a room is using (or was using when the room last reported its source), a speaker in use (it played in the last half hour, or was playing when it stopped answering), or routing infrastructure, has failed a control check for five minutes. House health lists the room, device and check under Needs attention now.",
      since: at - 3 * 3600, room: room.id, zone: room.id, device: room.display, check: "thing", check_text: "Not responding to control" };
    const unknown = { name: "ThermiteControlUnknown", severity: "warning", title: "A room in use cannot be read",
      text: "For ten minutes a control check that a room in use, a speaker in use or routing infrastructure needs has had no reading: most often the room's source, when one of its devices stopped reporting. A room counts as in use for three days after it last reported a source; one that reports Off does not. House health lists the room, device and check under Needs attention now.",
      since: at - 14 * 60, room: room.id, zone: room.id, check: "source", check_text: "Source not reported" };
    const disk = { name: "ThermiteHostDiskLow", severity: "warning", title: "Host storage is nearly full",
      text: "A writable host filesystem has less than ten percent free. Inspect disk use before removing any data.", since: at - 2 * 86400, service: "node" };
    const answer = (monitoring, alerts, firing) => ({ version: 1, monitoring: monitoring, evaluated: monitoring === "unreachable" ? null : Math.floor(Date.now() / 1000) - 12,
      firing: monitoring === "unreachable" ? null : firing === undefined ? alerts.length : firing, alerts: alerts });
    const samples = {
      firing: () => answer("ok", [failing, unknown]),
      admin: () => answer("ok", [failing, unknown, disk]),
      hidden: () => answer("ok", [failing], 3),
      house: () => answer("ok", [], 2),
      stalled: () => answer("stalled", [failing]),
      silent: () => answer("stalled", []),
      unreachable: () => answer("unreachable", [])
    };
    if (name === "unanswered") return Promise.reject(new Error("offline"));
    return Promise.resolve((own(samples, name || "") ? samples[name] : () => answer("ok", []))());
  }

  global.RemoteAlerts = { create: create, parse: parse, ago: ago, sample: sample,
    INTERVAL_MS: INTERVAL_MS, CONFIRM_MS: CONFIRM_MS, CURRENT_MS: CURRENT_MS, SUMMARY: SUMMARY };
})(globalThis);
