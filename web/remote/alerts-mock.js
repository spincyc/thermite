/*
 * The sample alerts: the feed's answers a preview shows
 * (?backend=mock&alerts=<name>), in the feed's own shape and the
 * catalogue's words (tests/test_home_alerts.py holds them to
 * oh_metrics.alerts), about the sample house's first room and its display.
 * Its alerts began before the page was opened, and age. A phone never loads
 * this file: a page loads it only when asked for the sample (navigation.js).
 * RemoteAlerts.sample(house, name) in alerts.js answers with it.
 */
(function (global) {
  "use strict";

  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
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

  (global.ThermiteMocks = global.ThermiteMocks || {}).alerts = sample;
})(globalThis);
