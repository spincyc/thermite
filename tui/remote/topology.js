/* Read-only site paths from the same generated catalogue as the web map. */
"use strict";

const fs = require("fs");
const path = require("path");
const { wrap, fit, length } = require("./grid");

const plain = (value) => String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f-\u009f]/g, " ");

function create(options = {}) {
  let manifest = options.manifest || null;
  let error = null;
  let generation = 0;
  let controller = null;
  const fetcher = options.fetch || globalThis.fetch;
  function valid(value) {
    if (!value || value.version !== 1 || !Array.isArray(value.nodes) || !Array.isArray(value.rooms)
        || !value.routes || typeof value.routes !== "object") throw new Error("Invalid topology catalogue");
    return value;
  }
  async function open() {
    const current = ++generation;
    error = null;
    if (manifest) { manifest = valid(manifest); return manifest; }
    controller = new AbortController();
    try {
      let value;
      if (options.mock) {
        value = JSON.parse(fs.readFileSync(path.join(__dirname, "../../conf/html/remote/topology.json"), "utf8"));
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

function url(base, room, source, mock) {
  const target = new URL(base || "http://127.0.0.1:8080");
  target.pathname = target.pathname.replace(/\/+$/, "") + "/static/remote/topology.html";
  target.username = target.password = "";
  target.search = ""; target.hash = "";
  if (room) target.searchParams.set("room", room);
  if (source) target.searchParams.set("source", source);
  if (mock) target.searchParams.set("backend", "mock");
  return target.href;
}

function sourceGroups(manifest, roomId) {
  const uses = new Map();
  Object.entries(manifest.routes).forEach(([room, routes]) => Object.keys(routes).forEach((id) => {
    if (!uses.has(id)) uses.set(id, new Set());
    uses.get(id).add(room);
  }));
  const groups = { shared: [], local: [] };
  Object.entries(manifest.routes[roomId] || {}).forEach(([id, route]) => {
    groups[uses.get(id).size > 1 ? "shared" : "local"].push({ id, label: route.label });
  });
  return groups;
}

const FIELDS = ["source", "matrix", "switch", "receiver", "observer", "display", "status"];
const TITLES = { source: "Source", matrix: "Matrix", switch: "Switch", receiver: "Receiver", observer: "Observer", display: "Display", status: "Status", chain: "Chain" };
const STATUS_ORDER = ["Reported", "Switching", "Configured", "Room off", "Unknown", "Live unavailable"];
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

function status(routes, source, reading = {}, connected) {
  if (connected === false) return "Live unavailable";
  if (reading.pending === source && own(routes, reading.pending)) return "Switching";
  if (reading.value === source && own(routes, reading.value)) return "Reported";
  if (reading.value === "off") return "Room off";
  return own(routes, reading.value) ? "Configured" : "Unknown";
}

function ports(node) {
  const out = [];
  if (node.input != null) out.push("in " + plain(node.input));
  if (node.output != null) out.push("out " + plain(node.output));
  if (node.zone != null) out.push("zone " + plain(node.zone));
  return out;
}

function shortLabel(node, manifest) {
  let label = plain(node.label || node.device);
  const device = manifest.nodes.find(entry => entry.id === node.device);
  if (device && device.model) {
    const model = plain(device.model), make = plain(device.make);
    const suffix = " (" + (!make || model.toLowerCase().startsWith(make.toLowerCase()) ? model : make + " " + model) + ")";
    if (label.endsWith(suffix)) label = label.slice(0, -suffix.length);
  }
  return label;
}

function routeRows(manifest, roomId, reading, connected) {
  const routes = manifest.routes[roomId] || {};
  return Object.entries(routes).map(([id, route]) => {
    const stages = {};
    FIELDS.slice(1, -1).forEach(role => { stages[role] = route.nodes.filter(node => node.role === role); });
    return { id, room: roomId, label: plain(route.label || id), route, stages, status: status(routes, id, reading, connected) };
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
  line("↑↓ room · ←→ preview source · Enter web map · s sort · S reverse");
  if (data.error) { line(data.error, "alert"); return y; }
  const manifest = data.manifest;
  if (!manifest) { line("Reading site topology…"); return y; }
  const room = manifest.rooms.find(entry => entry.id === state.room) || manifest.rooms[0];
  if (!room) { line("No rooms are configured."); return y; }
  const reading = ctx.session.state(room.id) || {}, routes = manifest.routes[room.id] || {};
  const rows = orderedRows(routeRows(manifest, room.id, reading, ctx.ui && ctx.ui.connected), state.sortField, state.sortDescending);
  const preview = rows.find(row => row.id === state.source) || rows[0];
  const source = own(routes, reading.value) ? plain(routes[reading.value].label) : reading.value === "off" ? "Room off" : "Unknown";
  const report = ctx.ui && ctx.ui.connected === false ? "Live unavailable · last report: " + source : source;
  line(room.label + " · " + manifest.nodes.length + " devices", "strong");
  line(!rows.length ? "No room source selector" : "Room source: " + report + (own(routes, reading.pending) ? " · Switching to " + plain(routes[reading.pending].label) : ""));
  line("Sort: " + TITLES[state.sortField || "source"] + (state.sortDescending ? " ↓" : " ↑") + " · Preview: " + (preview ? preview.label : "None"));
  line("Status describes the room source, not device power or link health.");
  y += 1;
  line("Video chains · all configured routes", "hotkey+bold");
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
    y += 1; line("Selected path · " + preview.label + " · configured preview", "strong");
    line(preview.route.nodes.map(node => plain(node.label) + (ports(node).length ? " [" + ports(node).join(" · ") + "]" : "")).join(" → "), "base");
  }
  y += 1; line("Data rows · USB · Configured only", "tone.ok+bold");
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
