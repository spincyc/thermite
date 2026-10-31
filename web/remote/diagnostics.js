/* Read-only signal views shared by the browser and terminal. Paths are
 * configured wiring selected by confirmed room readback. A capability is
 * never promoted to a measured signal or a source's OS setting. */
(function (global) {
  "use strict";
  const CATEGORIES = ["path", "video", "audio", "edid", "processing"];
  const OWN = (object, key) => Boolean(object) && Object.prototype.hasOwnProperty.call(object, key);
  const ITEM = /^[A-Za-z][A-Za-z0-9_]{0,160}$/;
  const SECRET = /(?:^|_)(?:pairing|password|passwd|passphrase|secret|token|psk|authorization|credential|auth)(?:_|$)/i; // lint: allow (an Item-name pattern, no credential)
  const MAX_ITEMS = 12;

  function text(value) {
    return String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "").trim().slice(0, 180);
  }
  function row(label, value, availability, extra) {
    return Object.assign({ label: text(label), value: text(value), availability: availability, basis: "reported" }, extra || {});
  }
  function unavailable(roomId, label, at) {
    const sections = {};
    CATEGORIES.forEach((category) => { sections[category] = [row("Signal information", "Unavailable", "unavailable")]; });
    return { room: { id: roomId, label: text(label || roomId) }, source: null, pending: null,
      availability: "unavailable", updatedAt: at, route: [], sections: sections };
  }
  function create(manifest) {
    if (!manifest || manifest.version !== 1 || !manifest.rooms || !manifest.devices) throw new Error("Invalid diagnostics manifest");
    function route(roomId, sourceId) {
      const room = OWN(manifest.rooms, roomId) ? manifest.rooms[roomId] : null;
      const source = room && OWN(room.sources, sourceId) ? room.sources[sourceId] : null;
      return { room: room, source: source, nodes: source && Array.isArray(source.nodes) ? source.nodes : [] };
    }
    function readings(roomId, sourceId) {
      return route(roomId, sourceId).nodes.flatMap((node) => {
        const provider = OWN(manifest.devices, node.device) ? manifest.devices[node.device] : null;
        if (!provider || !Array.isArray(provider.items) || !Array.isArray(provider.readings)) return [];
        return provider.readings.filter((reading) => {
          if (!reading || !CATEGORIES.includes(reading.category) || reading.category === "path") return false;
          if (!ITEM.test(reading.item) || SECRET.test(reading.item) || !provider.items.includes(reading.item)) return false;
          if (!Array.isArray(reading.path) || reading.path.length > 8) return false;
          if (reading.zone && reading.zone !== node.zone) return false;
          if (reading.port && node[reading.port.direction] !== reading.port.number) return false;
          return true;
        }).map((reading) => ({ reading: reading, node: node, maxAge: provider.maxAgeSeconds || 15 }));
      });
    }
    function items(roomId, sourceId) {
      const names = [...new Set(readings(roomId, sourceId).map((entry) => entry.reading.item))];
      if (names.length > MAX_ITEMS) throw new Error("Too many diagnostics snapshots");
      return names;
    }
    function project(roomId, sourceId, snapshots, at, pendingId, acquiredAfter) {
      const chosen = route(roomId, sourceId);
      const sections = { path: [], video: [], audio: [], edid: [], processing: [] };
      const nodes = chosen.nodes.map((node) => {
        const copy = { device: node.device, label: text(node.label), role: node.role };
        ["input", "output", "zone"].forEach((key) => { if (node[key] != null) copy[key] = text(node[key]); });
        return copy;
      });
      nodes.forEach((node) => {
        const ports = [node.input != null ? "input " + node.input : "", node.output != null ? "output " + node.output : "",
          node.zone && node.zone !== "main" ? "zone " + node.zone : ""].filter(Boolean).join(" · ");
        sections.path.push(row(node.role, node.label + (ports ? " · " + ports : ""), "reported", { basis: "configured", device: node.device }));
      });
      if (!nodes.length) sections.path.push(row("Path", sourceId === "off" ? "Room is off" : "Source not reported", "not_reported", { basis: "configured" }));
      const activeReadings = readings(roomId, sourceId);
      activeReadings.forEach((entry) => {
        const reading = entry.reading;
        const snapshot = OWN(snapshots, reading.item) ? snapshots[reading.item] : null;
        let data = null;
        if (snapshot && typeof snapshot.value === "string" && snapshot.value.length <= 65536) {
          try { data = JSON.parse(snapshot.value); } catch (_) { /* canonical unavailable below */ }
        }
        const envelope = data && data.version === 1 && data.values && typeof data.values === "object" && !Array.isArray(data.values);
        let value = envelope ? data : null;
        reading.path.forEach((key) => { value = value && typeof value === "object" && OWN(value, key) ? value[key] : null; });
        const scalar = typeof value === "string" || (typeof value === "number" && Number.isFinite(value)) || typeof value === "boolean";
        const valid = scalar && text(value) && !["NULL", "UNDEF"].includes(text(value));
        const observedAt = data && Number.isFinite(data.observedAt) && data.observedAt > 0 && data.observedAt <= at + 5000 ? data.observedAt : null;
        const age = observedAt == null ? null : Math.max(0, (at - observedAt) / 1000);
        const priorSource = Boolean(envelope) && Number.isFinite(acquiredAfter) && acquiredAfter > 0
          && (observedAt == null || observedAt <= acquiredAfter);
        const extra = { device: entry.node.device, detail: text(entry.node.label), observedAt: observedAt,
          ageSeconds: age, stale: priorSource || age == null || age > entry.maxAge };
        let availability = valid ? "reported" : envelope ? "not_reported" : "unavailable";
        let shown = valid ? value : envelope ? "Not reported" : "Unavailable";
        if (valid && reading.options) {
          if (OWN(reading.options, String(value))) shown = reading.options[String(value)];
          else { shown = "Not reported"; availability = "not_reported"; }
        }
        if (priorSource) {
          shown = "Awaiting fresh report";
          availability = "not_reported";
          extra.detail = text(entry.node.label + " · acquired before the source changed");
        }
        sections[reading.category].push(row(reading.label, shown, availability, extra));
      });
      if (!sections.video.length) sections.video.push(row("Current video signal", "Not reported", "not_reported"));
      if (!sections.audio.length) sections.audio.push(row("Current audio signal", "Not reported", "not_reported"));
      if (!activeReadings.some((entry) => entry.reading.category === "edid" && entry.reading.port && entry.reading.port.direction === "input")) {
        sections.edid.push(row("Source advertised EDID", "Not reported", "not_reported"));
      }
      if (!activeReadings.some((entry) => entry.reading.category === "edid" && entry.reading.port && entry.reading.port.direction === "output")) {
        sections.edid.push(row("Destination EDID capabilities", "Not reported", "not_reported"));
      }
      // Device-reported HDMI formats and EDIDs do not reveal these OS settings.
      sections.processing.push(row("Source OS video settings (HDR / match range)", "Not reported", "not_reported"));
      sections.processing.push(row("Source OS audio settings", "Not reported", "not_reported"));
      if (nodes.some((node) => node.role === "display")) sections.processing.push(row("Display HDMI input settings", "Not reported", "not_reported"));
      const pending = chosen.room && OWN(chosen.room.sources, pendingId) ? { id: pendingId, label: text(chosen.room.sources[pendingId].label) } : null;
      const measured = ["video", "audio", "edid", "processing"].flatMap((category) => sections[category]).filter((row) => row.device);
      return { room: { id: roomId, label: text(chosen.room ? chosen.room.label : roomId) },
        source: chosen.source ? { id: sourceId, label: text(chosen.source.label) } : null, pending: pending,
        availability: measured.some((row) => row.availability === "reported") ? "reported"
          : measured.some((row) => row.availability === "unavailable") ? "unavailable" : "not_reported",
        updatedAt: at, route: nodes, sections: sections };
    }
    return { items: items, project: project };
  }
  global.RemoteDiagnostics = { create: create, unavailable: unavailable, MAX_ITEMS: MAX_ITEMS };
})(globalThis);
