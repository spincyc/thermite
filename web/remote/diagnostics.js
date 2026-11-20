/* Read-only observations of configured signal paths. A capability, setting,
 * track's metadata or successful Item read is never a measured format. */
(function (global) {
  "use strict";
  const CATEGORIES = ["path", "video", "audio", "edid", "processing"];
  const OWN = (object, key) => Boolean(object) && Object.prototype.hasOwnProperty.call(object, key);
  const ITEM = /^[A-Za-z][A-Za-z0-9_]{0,160}$/;
  const SECRET = /(?:^|_)(?:pairing|password|passwd|passphrase|secret|token|psk|authorization|credential|auth)(?:_|$)/i; // lint: allow (an Item-name pattern, no credential)
  const MAX_ITEMS = 24;
  const GROUPS = [["video", "Current video"], ["audio", "Current audio"], ["path", "Configured path"],
    ["edid", "EDID capabilities", "Advertised support, not the current format."], ["processing", "Processing & settings"]];
  const REASONS = { invalid_response: "Invalid device response", no_response: "No response", read_failed: "Snapshot could not be read",
    route_changed: "Route changed during measurement", not_active: "Device is not active" };
  const STATUS = { not_reported: "Not reported", unsupported: "No format telemetry", unavailable: "Unavailable",
    no_signal: "No signal", silence: "Silence", awaiting_route: "Awaiting matching route", not_applicable: "Not on this media path" };
  function text(value) {
    return String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "").trim().slice(0, 180);
  }
  const safeItem = name => ITEM.test(name) && !SECRET.test(name);
  const scalar = value => typeof value === "string" || typeof value === "boolean" || typeof value === "number" && Number.isFinite(value);
  const valueAt = (data, path) => path.reduce((value, key) => value && typeof value === "object" && OWN(value, key) ? value[key] : null, data);
  const kindOf = reading => reading.kind || ({ video: "format", audio: "format", edid: "capability", processing: "setting" }[reading.category]);
  const observed = status => ["reported", "no_signal", "silence"].includes(status);
  function row(label, value, status, extra) {
    return Object.assign({ label: text(label), value: text(value), status, availability: observed(status) ? "reported"
      : status === "unavailable" ? "unavailable" : "not_reported", basis: "reported" }, extra || {});
  }
  function unavailable(roomId, label, at, reason) {
    const sections = {};
    CATEGORIES.forEach(category => { sections[category] = [row("Signal information", "Unavailable", "unavailable")]; });
    return { room: { id: roomId, label: text(label || roomId) }, source: null, pending: null,
      availability: "unavailable", reason: reason || "Unavailable · signal catalogue could not be read", updatedAt: at, route: [], sections };
  }
  function ports(node) {
    return [node.input != null ? (node.inputKind === "selector" ? "input selector " : "input ") + text(node.input) : "", node.output != null ? "output " + text(node.output) : "",
      node.zone != null && node.zone !== "main" ? "zone " + text(node.zone) : ""].filter(Boolean).join(" · ");
  }
  function copyNode(node) {
    const copy = { device: node.device, label: text(node.label), role: node.role };
    ["input", "output", "zone", "inputKind", "inputTransport", "outputTransport", "wiring"].forEach(key => { if (node[key] != null) copy[key] = text(node[key]); });
    return copy;
  }
  function create(manifest) {
    if (!manifest || manifest.version !== 1 || !manifest.rooms || !manifest.devices) throw new Error("Invalid diagnostics manifest");
    function routeStateItem(roomId) {
      const item = OWN(manifest.rooms, roomId) && manifest.rooms[roomId].routeStateItem;
      return typeof item === "string" && safeItem(item) ? item : null;
    }
    // Lifecycle read before and after collection proves which same-source
    // variant was observed; a source's name alone cannot identify that cable.
    function context(roomId, sourceId, snapshot) {
      if (!routeStateItem(roomId)) return { ready: true, key: "", variant: "" };
      let data;
      try { data = JSON.parse(snapshot && snapshot.value); } catch (_) { return { ready: false, key: "", variant: "" }; }
      const valid = data && data.version === 1 && data.choice === sourceId && typeof data.synced === "boolean"
        && Number.isSafeInteger(data.generation) && data.generation >= 0;
      return { ready: Boolean(valid && data.pathReady === true && !data.fault),
        key: valid ? JSON.stringify([sourceId, data.synced, data.generation]) : "", variant: valid && data.synced ? "synced" : "" };
    }
    function route(roomId, sourceId, routeContext) {
      const room = OWN(manifest.rooms, roomId) ? manifest.rooms[roomId] : null;
      const base = room && OWN(room.sources, sourceId) ? room.sources[sourceId] : null;
      const variant = routeContext && routeContext.variant;
      const source = base && variant && OWN(base.variants, variant) ? Object.assign({}, base, base.variants[variant]) : base;
      const ready = !routeStateItem(roomId) || Boolean(routeContext && routeContext.ready
        && (!variant || base && OWN(base.variants, variant)));
      return { room, source, ready, nodes: source && Array.isArray(source.nodes) ? source.nodes : [],
        audioNodes: source && Array.isArray(source.audioNodes) ? source.audioNodes : [],
        audioEndpoints: source && Array.isArray(source.audioEndpoints) ? source.audioEndpoints : [],
        usb: Array.isArray(source?.usb) ? source.usb : [] };
    }
    function hops(path) {
      const carries = path.source && path.source.carries;
      return path.nodes.map(node => ({ ...node, branch: "main", video: carries !== "audio", audio: carries !== "display" && !path.audioNodes.length }))
        .concat(path.audioNodes.map(node => ({ ...node, branch: "audio", video: false, audio: true })),
          path.audioEndpoints.map(node => ({ ...node, branch: "endpoint", video: false, audio: carries !== "display" })));
    }
    function hopKey(node) { return JSON.stringify([node.branch, node.device, node.input, node.output, node.zone]); }
    function readings(roomId, sourceId, routeContext) {
      const path = route(roomId, sourceId, routeContext);
      return hops(path).flatMap(node => {
        const provider = OWN(manifest.devices, node.device) ? manifest.devices[node.device] : null;
        if (!provider || !Array.isArray(provider.items) || !Array.isArray(provider.readings)) return [];
        return provider.readings.filter(reading => {
          if (!reading || !CATEGORIES.includes(reading.category) || reading.category === "path") return false;
          const kind = kindOf(reading);
          // Link/EDID/processing observations matter on an HDMI audio branch
          // too. Only a format belonging to the other media path is excluded.
          if (kind === "format" && (reading.category === "video" && !node.video || reading.category === "audio" && !node.audio)) return false;
          if (!node.audio && reading.media === "audio" || !node.video && reading.media === "video" && kind === "format") return false;
          const transport = reading.port?.direction === "input" ? node.inputTransport
            : reading.port?.direction === "output" ? node.outputTransport : node.outputTransport || node.inputTransport;
          if (Array.isArray(reading.transports) && transport && !reading.transports.includes(transport)) return false;
          if (!safeItem(reading.item) || !provider.items.includes(reading.item)) return false;
          if (!reading.scalar && (!Array.isArray(reading.path) || reading.path.length > 8)) return false;
          if (reading.zone != null && String(reading.zone) !== String(node.zone)) return false;
          if (reading.port && String(node[reading.port.direction]) !== String(reading.port.number)) return false;
          return true;
        }).map(reading => ({ reading, node, maxAge: reading.maxAgeSeconds || provider.maxAgeSeconds || 15 }));
      });
    }
    function items(roomId, sourceId, routeContext) {
      const names = [...new Set(readings(roomId, sourceId, routeContext).map(entry => entry.reading.item))];
      if (names.length > MAX_ITEMS) throw new Error("Too many diagnostics snapshots");
      return names;
    }
    function project(roomId, sourceId, snapshots, at, pendingId, acquiredAfter, routeContext) {
      const chosen = route(roomId, sourceId, routeContext);
      const sections = { path: [], video: [], audio: [], edid: [], processing: [] };
      const nodes = chosen.nodes.map(copyNode), audioNodes = chosen.audioNodes.map(copyNode), audioEndpoints = chosen.audioEndpoints.map(copyNode);
      const pending = chosen.room && OWN(chosen.room.sources, pendingId) ? { id: pendingId, label: text(chosen.room.sources[pendingId].label) }
        : pendingId === "off" ? { id: "off", label: "Off" } : null;
      function pathRows(chain, prefix) {
        chain.forEach(node => sections.path.push(row(prefix + node.role, node.label + (ports(node) ? " · " + ports(node) : ""), "reported",
          { basis: "configured", device: node.device })));
      }
      pathRows(nodes, ""); pathRows(audioNodes, "Audio "); pathRows(audioEndpoints, "Audio endpoint · wiring unknown · ");
      const carries = chosen.source && chosen.source.carries;
      if (nodes.length && ["display", "audio"].includes(carries)) sections.path.push(row("Carries", carries === "display" ? "Display only" : "Audio only", "reported", { basis: "configured" }));
      if (!nodes.length) sections.path.push(row("Path", sourceId === "off" ? "Room is off" : "Source not reported", "not_reported", { basis: "configured" }));
      for (const usb of chosen.usb) {
        if (!usb || usb.host == null || !Array.isArray(usb.ports) || !usb.ports.length) continue;
        sections.path.push(row("USB · " + (usb.label || usb.device), "HOST " + usb.host + " → DEVICE " + usb.ports.join(", "),
          "reported", { basis: "configured", device: usb.device }));
      }
      const activeReadings = readings(roomId, sourceId, routeContext);
      activeReadings.forEach(entry => {
        const { reading, node, maxAge } = entry, kind = kindOf(reading);
        const snapshot = OWN(snapshots, reading.item) ? snapshots[reading.item] : null;
        let data = null;
        if (snapshot && typeof snapshot.value === "string" && snapshot.value.length <= 65536 && !reading.scalar) {
          try { data = JSON.parse(snapshot.value); } catch (_) { /* invalid response below */ }
        }
        const envelope = data && data.version === 1 && data.values && typeof data.values === "object" && !Array.isArray(data.values);
        const value = reading.scalar ? snapshot && snapshot.value : envelope ? valueAt(data, reading.path) : null;
        const valid = scalar(value) && text(value) && !["NULL", "UNDEF"].includes(text(value));
        const observedAt = data && Number.isFinite(data.observedAt) && data.observedAt > 0 && data.observedAt <= at + 5000 ? data.observedAt : null;
        const age = observedAt == null ? null : Math.max(0, (at - observedAt) / 1000);
        const beforeChange = Number.isFinite(acquiredAfter) && acquiredAfter > 0 && (observedAt == null || observedAt <= acquiredAfter);
        const extra = { device: node.device, detail: text(node.label), hop: hopKey(node), ports: ports(node), kind,
          provider: text(reading.provider || ""), observedAt, maxAgeSeconds: maxAge, ageSeconds: age,
          stale: age == null || age > maxAge, basis: reading.scalar ? "item" : "reported" };
        let status = valid ? "reported" : envelope ? "not_reported" : "unavailable", shown = valid ? text(value) : "";
        const matches = values => Array.isArray(values) && values.some(candidate => text(candidate).toLowerCase() === text(value).toLowerCase());
        if (valid && matches(reading.unknown)) { status = "not_reported"; shown = ""; }
        if (valid && matches(reading.noSignal)) { status = "no_signal"; shown = "No signal"; }
        if (valid && matches(reading.silence)) { status = "silence"; shown = "Silence"; }
        if (status === "reported" && reading.options) {
          if (OWN(reading.options, String(value))) shown = text(reading.options[String(value)]);
          else { status = "not_reported"; shown = ""; }
        }
        if (data && data.status === "unavailable" || snapshot && snapshot.reason) { status = "unavailable"; shown = ""; }
        if (status === "unavailable") extra.reason = REASONS[data && data.reason] || REASONS[snapshot && snapshot.reason]
          || (data && data.status === "unavailable" || snapshot && snapshot.reason ? "Device snapshot unavailable"
            : snapshot && snapshot.value && !["NULL", "UNDEF"].includes(snapshot.value) ? REASONS.invalid_response : "No device snapshot");
        const selector = reading.selector;
        const selected = selector && envelope && Array.isArray(selector.path) ? valueAt(data, selector.path) : null;
        const matchesRoute = !selector || scalar(selected) && node[selector.node] != null && String(selected) === String(node[selector.node]);
        if ((observed(status) || !snapshot && kind === "format") && (kind === "format" && (pending || !chosen.ready || beforeChange || node.wiring === "unknown" || reading.scalar && !reading.selector) || !matchesRoute)) {
          extra.unassociatedValue = shown;
          status = "awaiting_route"; shown = "";
          extra.reason = pending ? "Source is switching" : !chosen.ready ? "Route is not confirmed"
            : reading.scalar && !reading.selector ? "Device report has no route association or measurement time"
            : !matchesRoute ? "Reported input does not confirm this path" : node.wiring === "unknown" ? "Audio wiring is not configured" : "Sample predates the route change";
        }
        sections[reading.category].push(row(reading.label, shown || STATUS[status], status, extra));
      });
      // Every applicable hop has a current-format entry, including devices
      // whose integrations expose only settings or no diagnostics at all.
      hops(chosen).forEach(node => {
        ["video", "audio"].forEach(category => {
          if (!node[category] || activeReadings.some(entry => hopKey(entry.node) === hopKey(node) && entry.reading.category === category && kindOf(entry.reading) === "format")) return;
          const label = node.role === "source" ? "Output " + category + " format" : node.role === "display" || node.role === "audio" ? "Received " + category + " format" : "Input / output " + category + " format";
          sections[category].push(row(label, STATUS.unsupported, "unsupported", { device: node.device, hop: hopKey(node), ports: ports(node), kind: "format" }));
        });
      });
      ["video", "audio"].forEach(category => {
        if (!sections[category].length) sections[category].push(row("Current " + category + " signal", sourceId === "off" ? "Room is off"
          : carries === (category === "video" ? "audio" : "display") ? STATUS.not_applicable : STATUS.not_reported,
        sourceId === "off" || carries === (category === "video" ? "audio" : "display") ? "not_applicable" : "not_reported"));
      });
      if (!activeReadings.some(entry => entry.reading.category === "edid" && entry.reading.port && entry.reading.port.direction === "input")) sections.edid.push(row("Source-facing EDID", "Not reported", "not_reported"));
      if (!activeReadings.some(entry => entry.reading.category === "edid" && entry.reading.port && entry.reading.port.direction === "output")) sections.edid.push(row("Destination EDID capabilities", "Not reported", "not_reported"));
      sections.processing.push(row("Source OS video settings (HDR / match range)", "Not reported", "not_reported"));
      sections.processing.push(row("Source OS audio settings", "Not reported", "not_reported"));
      const order = hops(chosen).map(hopKey);
      Object.values(sections).forEach(entries => entries.sort((a, b) => (a.hop ? order.indexOf(a.hop) : order.length) - (b.hop ? order.indexOf(b.hop) : order.length)));
      activeReadings.forEach(entry => {
        const same = sections[entry.reading.category].filter(value => value.hop === hopKey(entry.node) && value.label === text(entry.reading.label));
        if (same.length > 1) same.forEach(value => { if (value.provider) value.label += " (" + value.provider + ")"; });
      });
      const measured = ["video", "audio"].flatMap(category => sections[category]).filter(entry => entry.kind === "format");
      return { room: { id: roomId, label: text(chosen.room ? chosen.room.label : roomId) },
        source: chosen.source ? { id: sourceId, label: text(chosen.source.label) } : null, pending,
        availability: measured.some(entry => entry.availability === "reported" && !entry.stale) ? "reported"
          : measured.some(entry => entry.availability === "unavailable") ? "unavailable" : "not_reported",
        routeReady: chosen.ready, routeKey: routeContext && routeContext.key || "", updatedAt: at, route: nodes,
        ...(audioNodes.length ? { audioRoute: audioNodes } : {}), ...(audioEndpoints.length ? { audioEndpoints } : {}), sections };
    }
    return { items, project, routeStateItem, context };
  }
  function ago(seconds) {
    return seconds < 120 ? Math.floor(seconds) + "s ago" : seconds < 7200 ? Math.floor(seconds / 60) + " min ago" : Math.floor(seconds / 3600) + " h ago";
  }
  // Pending and connection context changes immediately, independently of a
  // potentially slow snapshot read. Keep the cached observation immutable.
  function contextualize(model, pending, connected, at = Date.now()) {
    if (!model) return model;
    const formats = ["video", "audio"].flatMap(category => model.sections[category] || []).filter(entry => entry.kind === "format");
    const fresh = entry => observed(entry.status || entry.availability) && !entry.stale && Number.isFinite(entry.observedAt)
      && at - entry.observedAt <= (entry.maxAgeSeconds || 15) * 1000;
    const availability = connected === false ? "unavailable" : pending ? "not_reported" : formats.some(fresh) ? "reported"
      : formats.some(entry => entry.availability === "unavailable") ? "unavailable" : !formats.length && model.availability === "unavailable" ? "unavailable" : "not_reported";
    const copy = { ...model, pending, connected, availability };
    if (!pending && connected !== false) return copy;
    copy.sections = Object.fromEntries(Object.entries(model.sections).map(([category, entries]) => [category, entries.map(entry => {
      if (entry.kind !== "format" || !observed(entry.status || entry.availability)) return entry;
      return { ...entry, status: "awaiting_route", availability: "not_reported", value: STATUS.awaiting_route, unassociatedValue: entry.value,
        reason: pending ? "Source is switching" : "Room updates are unavailable" };
    })]));
    return copy;
  }
  function outline(model, now) {
    const sections = model && model.sections || {}, names = {};
    (model && model.route || []).concat(model && model.audioRoute || [], model && model.audioEndpoints || []).forEach(node => { names[node.device] = text(node.label); });
    const every = CATEGORIES.flatMap(category => sections[category] || []);
    if (!every.length || every.every(entry => entry.availability === "unavailable" && !entry.device)) return { blank: true, groups: [] };
    const groups = GROUPS.map(([id, title, note]) => {
      const blocks = [], counts = { reported: 0, formats: 0, noSignal: 0, silence: 0, unavailable: 0, unsupported: 0, awaiting: 0, absent: 0 };
      (sections[id] || []).forEach(entry => {
        const configured = entry.basis === "configured", device = configured ? "" : entry.device || "", key = configured ? "" : entry.hop || device;
        let block = blocks.find(candidate => candidate.id === key);
        if (!block) { block = { id: key, device: device ? (names[device] || text(device)) + (entry.ports ? " · " + entry.ports : "") : "",
          rows: [], quiet: [], times: [], unknown: false, stale: false }; blocks.push(block); }
        const label = text(id === "path" ? String(entry.label).charAt(0).toUpperCase() + String(entry.label).slice(1) : entry.label);
        const status = entry.status || entry.availability;
        if (!observed(status)) {
          if (entry.unassociatedValue) {
            if (Number.isFinite(entry.observedAt)) block.times.push(entry.observedAt);
            else block.unknown = true;
            block.stale ||= Boolean(entry.stale) || !Number.isFinite(entry.observedAt)
              || Number.isFinite(entry.maxAgeSeconds) && now - entry.observedAt > entry.maxAgeSeconds * 1000;
          }
          if (status === "unavailable") counts.unavailable++;
          else if (status === "unsupported") counts.unsupported++;
          else if (status === "awaiting_route") counts.awaiting++;
          else counts.absent++;
          const value = entry.value || STATUS[status] || STATUS.not_reported;
          block.quiet.push(label + ": " + value + (entry.reason ? " · " + text(entry.reason) : "")
            + (entry.unassociatedValue ? " · device reports " + text(entry.unassociatedValue) : ""));
          return;
        }
        const known = Number.isFinite(entry.observedAt);
        const stale = !configured && (Boolean(entry.stale) || !known || Number.isFinite(entry.maxAgeSeconds) && now - entry.observedAt > entry.maxAgeSeconds * 1000);
        if (!configured && known) block.times.push(entry.observedAt);
        if (!configured && !known) block.unknown = true;
        block.stale ||= stale;
        counts.reported++;
        if (status === "no_signal") counts.noSignal++;
        if (status === "silence") counts.silence++;
        if (entry.kind === "format" && status === "reported" && !stale) counts.formats++;
        block.rows.push({ label, value: text(entry.value), stale, status: stale ? "stale" : status, kind: entry.kind });
      });
      if (!blocks.length) blocks.push({ id: "", device: "", rows: [], quiet: ["Not reported"], times: [], unknown: false, stale: false });
      const stale = blocks.some(block => block.stale);
      const summary = [id === "video" || id === "audio" ? counts.formats + " formats" : counts.reported + (id === "path" ? " configured" : " reported"),
        counts.noSignal ? counts.noSignal + " no signal" : "", counts.silence ? counts.silence + " silent" : "",
        counts.unavailable ? counts.unavailable + " unavailable" : "", counts.awaiting ? counts.awaiting + " awaiting" : "",
        counts.unsupported ? counts.unsupported + " unobserved" : "", counts.absent && !counts.unavailable && !counts.awaiting ? counts.absent + " not reported" : "",
        stale ? "stale" : ""].filter(Boolean).join(" · ");
      return { id, title, note: note || "", count: counts.reported, summary, stale, blocks: blocks.map(block => {
        const age = block.unknown ? "age unknown" : block.times.length ? ago(Math.max(0, (now - Math.min(...block.times)) / 1000)) : "";
        return { device: block.device, age: age && block.stale ? "stale · " + age : age, stale: block.stale, rows: block.rows, quiet: block.quiet };
      }) };
    });
    return { blank: false, groups };
  }
  global.RemoteDiagnostics = { create, unavailable, outline, contextualize, MAX_ITEMS };
})(globalThis);
