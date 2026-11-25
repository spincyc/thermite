/* Read-only site paths from the same generated catalogue as the web map. */
"use strict";

const fs = require("fs");
const path = require("path");
const { wrap, fit, length } = require("./grid");
const SharedTopology = require("../../web/remote/topology");

const plain = (value) => String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f-\u009f]/g, " ");

function create(options = {}) {
  let manifest = options.manifest || null;
  let error = null;
  let generation = 0;
  let controller = null;
  const fetcher = options.fetch || globalThis.fetch;
  function valid(value) {
    if (!value || typeof value !== "object") throw new Error("Invalid topology catalogue");
    return SharedTopology.catalogue(Object.assign({ edges: [] }, value));
  }
  async function open() {
    const current = ++generation;
    error = null;
    if (manifest) { manifest = valid(manifest); return manifest; }
    controller = new AbortController();
    try {
      let value;
      if (options.mock) {
        value = JSON.parse(fs.readFileSync(path.join(__dirname, "../../web/remote/topology.json"), "utf8"));
      } else {
        const response = await fetcher((options.base || "").replace(/\/+$/, "") + "/static/remote/topology.json",
          { method: "GET", signal: controller.signal });
        if (!response.ok) throw new Error("Topology unavailable");
        value = JSON.parse(await response.text());
      }
      if (current === generation) manifest = valid(value);
    } catch (_) {
      if (current === generation) error = "The site topology is unavailable. Try reopening this view.";
    } finally {
      if (current === generation) controller = null;
    }
    return manifest;
  }
  function close() { generation += 1; if (controller) controller.abort(); controller = null; }
  return { open, close, stop: close, state: () => ({ manifest, error }) };
}

function url(base, room, source, mock, native = false) {
  const target = new URL(base || "http://127.0.0.1:13602");
  target.pathname = target.pathname.replace(/\/+$/, "") + "/site/";
  target.username = target.password = "";
  target.search = ""; target.hash = "";
  if (room) target.searchParams.set("room", room);
  if (source) target.searchParams.set("source", source);
  if (mock || native) target.searchParams.set("backend", mock ? "mock" : "native");
  return target.href;
}

function sourceGroups(manifest, roomId) {
  return SharedTopology.sourceGroups(manifest, roomId);
}

const FIELDS = ["source", "matrix", "switch", "receiver", "observer", "display", "status"];
const TITLES = { source: "Source", matrix: "Matrix", switch: "Switch", receiver: "Receiver", observer: "Observer", display: "Display", status: "Status", chain: "Chain" };
const STATUS_ORDER = ["Reported", "Route unconfirmed", "Switching", "Partially on", "Configured", "Room off", "Unknown", "Live unavailable"];
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

function status(manifest, roomId, source, reading = {}, connected) {
  return SharedTopology.routeStatus(manifest, roomId, source, reading, connected).label;
}

function ports(node) {
  const summary = SharedTopology.nativePorts(node);
  return summary ? summary.split(" · ") : [];
}

function shortLabel(node, manifest) {
  return plain(SharedTopology.compactLabel(manifest, node));
}

// A path that carries a picture or sound alone says so (routing's carries).
function carried(route) {
  const label = SharedTopology.carriesLabel(route);
  return label ? " (" + label.toLowerCase() + ")" : "";
}

function routeRows(manifest, roomId, reading, connected) {
  const routes = manifest.routes[roomId] || {};
  return Object.entries(routes).map(([id, route]) => {
    const stages = {};
    FIELDS.slice(1, -1).forEach(role => { stages[role] = route.nodes.filter(node => node.role === role); });
    const reported = status(manifest, roomId, id, reading, connected);
    return { id, room: roomId, label: plain(route.label || id) + carried(route), route, stages,
      status: route.variants?.synced && reported === "Reported" ? "Route unconfirmed" : reported };
  });
}

function orderedRows(rows, field = "source", descending = false) {
  if (!FIELDS.includes(field)) field = "source";
  const value = row => field === "source" ? row.label : field === "status" ? row.status
    : row.stages[field].map(node => plain(node.label) + " " + ports(node).join(" ")).join(" → ");
  return rows.slice().sort((a, b) => {
    const av = value(a), bv = value(b);
    if (!av || !bv) { if (Boolean(av) !== Boolean(bv)) return av ? -1 : 1; }
    const compared = field === "status" ? STATUS_ORDER.indexOf(av) - STATUS_ORDER.indexOf(bv)
      : av.localeCompare(bv, "en", { numeric: true, sensitivity: "base" });
    return (descending ? -compared : compared) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  });
}

function cycleSort(state, reverse = false) {
  if (reverse) state.sortDescending = !state.sortDescending;
  else state.sortField = FIELDS[(Math.max(0, FIELDS.indexOf(state.sortField || "source")) + 1) % FIELDS.length];
}

function sortBy(state, field) {
  if (!FIELDS.includes(field)) return;
  state.sortDescending = state.sortField === field ? !state.sortDescending : false;
  state.sortField = field;
}

function columns(width) {
  if (width >= 128) {
    const room = width - 2 - 3 * 6 - 16, each = Math.floor(room / 6), extra = room % 6;
    return FIELDS.map((key, index) => ({ key, width: key === "status" ? 16 : each + (index < extra ? 1 : 0) }));
  }
  const available = Math.max(3, width - 8), source = Math.max(1, Math.min(20, Math.floor(available * .3))), report = Math.max(1, Math.min(16, Math.floor(available * .3)));
  return [{ key: "source", width: source }, { key: "status", width: report }, { key: "chain", width: Math.max(1, available - source - report) }];
}

function stageText(nodes, manifest, width) {
  if (!nodes.length) return "—";
  return nodes.map(node => {
    const label = shortLabel(node, manifest), native = ports(node).join(" / ");
    if (!native) return label;
    if (length(label + " " + native) <= width) return label + " " + native;
    const compact = native.replace(/in /g, "i").replace(/out /g, "o").replace(/zone /g, "z").replace(/ \/ /g, "/");
    const left = width - length(compact) - 1;
    return left >= 6 ? fit(label, left).trimEnd() + " " + compact : label;
  }).join(" → ");
}

function content(g, width, ctx) {
  const state = ctx.topology, data = state.backend ? state.backend.state() : {};
  let y = 0;
  const line = (value, style = "dim") => wrap(plain(value), Math.max(4, width - 2)).forEach(part => g.put(1, y++, part, style));
  if (data.error) { line(data.error, "alert"); return y; }
  const manifest = data.manifest;
  if (!manifest) { line("Reading site topology…"); return y; }
  const room = manifest.rooms.find(entry => entry.id === state.room) || manifest.rooms[0];
  if (!room) { line("No rooms are configured."); return y; }
  const reading = ctx.session.state(room.id) || {}, routes = manifest.routes[room.id] || {};
  // Whether the house is heard is the session's to say.
  const connected = ctx.session.connected();
  const rows = orderedRows(routeRows(manifest, room.id, reading, connected), state.sortField, state.sortDescending);
  const preview = rows.find(row => row.id === state.source) || rows[0];
  const source = own(routes, reading.value) ? plain(routes[reading.value].label)
    : reading.value === "off" ? "Room off" : reading.value === "partial" ? "Partially on" : "Unknown";
  const report = connected ? source : "Live unavailable · last report: " + source;
  line(room.label + " · " + manifest.nodes.length + " devices", "strong");
  line(!rows.length ? "No room source selector" : "Room source: " + report + (own(routes, reading.pending) ? " · Switching to " + plain(routes[reading.pending].label) : ""));
  line("Source reports only; device power and link health are unknown.");
  y += 1;
  line("Configured video routes", "hotkey+bold");
  const cols = columns(width);
  let x = 1;
  cols.forEach((col, index) => {
    const active = col.key === (state.sortField || "source");
    const title = TITLES[col.key] + (active ? state.sortDescending ? " ↓" : " ↑" : "");
    g.put(x, y, fit(title, col.width), active ? "hotkey+bold" : "strong");
    if (FIELDS.includes(col.key)) g.hit(x, y, col.width, 1, { kind: "topology-sort", field: col.key, key: "topology-sort:" + col.key, label: "Sort by " + TITLES[col.key] }, false);
    x += col.width;
    if (index < cols.length - 1) { g.put(x, y, " │ ", "rule"); x += 3; }
  });
  y += 1; g.put(1, y++, "─".repeat(Math.max(0, width - 2)), "rule");
  rows.forEach(row => {
    const selected = preview && row.id === preview.id, style = selected ? "row.focus" : "row";
    g.fill(1, y, Math.max(0, width - 2), 1, style);
    let at = 1;
    cols.forEach((col, index) => {
      const value = col.key === "source" ? row.label : col.key === "status" ? row.status : col.key === "chain"
        ? row.route.nodes.map(node => shortLabel(node, manifest)).join(" → ") : stageText(row.stages[col.key], manifest, col.width);
      const tone = selected ? style : col.key === "status" && row.status === "Reported" ? "tone.ok"
        : col.key === "status" && row.status === "Switching" ? "tone.busy" : value === "—" ? "dim" : "base";
      g.put(at, y, fit(value, col.width), tone); at += col.width;
      if (index < cols.length - 1) { g.put(at, y, " │ ", selected ? style : "rule"); at += 3; }
    });
    g.hit(1, y++, Math.max(0, width - 2), 1, { kind: "topology-preview", source: row.id, key: "topology-preview:" + row.id, label: "Preview " + row.label + " configured route" }, false);
  });
  if (!rows.length) line("No AV route is configured for this room.");
  if (preview) {
    y += 1; line("Preview · " + preview.label + (preview.route.variants?.synced ? " · configured own route" : ""), "strong");
    line(preview.route.nodes.map(node => plain(node.label) + (ports(node).length ? " [" + ports(node).join(" · ") + "]" : "")).join(" → "), "base");
    if (preview.route.audioNodes) line("Audio · " + preview.route.audioNodes.map(node => plain(node.label) + (ports(node).length ? " [" + ports(node).join(" · ") + "]" : "")).join(" → "), "base");
    (preview.route.audioEndpoints || []).forEach(node => line("Audio endpoint · " + plain(node.label) + " · " + ports(node).join(" · "), "dim"));
    if (preview.route.variants?.synced) {
      line("Configured synced route · " + preview.route.variants.synced.nodes.map(node => plain(node.label)
        + (ports(node).length ? " [" + ports(node).join(" · ") + "]" : "")).join(" → "), "base");
      line("Use Signal & audio for the observed physical route.");
    }
  }
  const audio = rows.filter(row => row.route.audioNodes && row.route.audioNodes.length);
  if (audio.length) {
    y += 1; line("Configured audio routes", "hotkey+bold");
    audio.forEach(row => line(row.label + " · " + row.route.audioNodes.slice(1).map(node => plain(node.label) + " [" + ports(node).join(" · ") + "]").join(" → "), "base"));
  }
  y += 1; line("Configured USB", "hotkey+bold");
  const usb = rows.flatMap(row => (row.route.usb || []).map(entry => ({ row, entry })));
  if (!usb.length) line("No USB assignment configured for these sources.");
  else {
    const usable = Math.max(5, width - 14), source = Math.max(1, Math.floor(usable * .22));
    const host = 4, device = usable >= 50 ? Math.max(12, Math.floor(usable * .16)) : Math.max(1, Math.floor(usable * .2));
    const status = Math.min(10, Math.max(1, Math.floor(usable * .2)));
    const widths = [source, Math.max(1, usable - source - host - device - status), host, device, status];
    const put = (values, style) => { let at = 1; values.forEach((value, index) => { g.put(at, y, fit(value, widths[index]), style); at += widths[index]; if (index < values.length - 1) { g.put(at, y, " │ ", "rule"); at += 3; } }); y++; };
    put(["Source", "USB matrix", "HOST", "DEVICE ports", "Status"], "strong");
    usb.forEach(({ row, entry }) => {
      const node = manifest.nodes.find(node => node.id === entry.device);
      put([row.label, shortLabel({ device: entry.device, label: node ? node.label : entry.device }, manifest), plain(entry.host), entry.ports.map(plain).join(", "), "Configured"], "base");
    });
  }
  y += 1; line("Devices in this location", "strong");
  (room.devices || []).forEach(id => { const node = manifest.nodes.find(entry => entry.id === id); if (node) line("  " + node.label + " · " + node.kind, "base"); });
  if (state.link) { y += 1; line(state.link, "link"); }
  return y;
}

module.exports = { create, content, url, sourceGroups, routeRows, orderedRows, cycleSort, sortBy, columns };
