/*
 * Rooms through the day: each room's confirmed source readback over the last
 * 24 hours, the House board's timeline in Grafana, read from the gateway
 * (GET /api/v1/history/rooms, lib/oh_identity/history.py), which reads the
 * house's Prometheus with the board's own queries. Not the requested source.
 * Tan: a source. Slate: Off. Grey: Unknown, the room's devices gave no
 * readback. A dot: no data. Read-only: nothing here operates a device.
 *
 * The sheet reads it when it opens and again every REFRESH_MS while open; a
 * read gives up after TIMEOUT_MS. --mock draws a fictional day of the
 * house's rooms instead, and --native has no gateway to ask.
 */
"use strict";

const { fit, length } = require("./grid");

const PATH = "/api/v1/history/rooms";
const REFRESH_MS = 60000;
const TIMEOUT_MS = 10000;
const DAY_S = 24 * 3600;
const MAX_ROOMS = 64;
const MAX_SEGMENTS = 4000;
// How many of the focused room's latest runs the sheet lists.
const RECENT = 8;
// The hours an axis label may fall on, the fewest that leave room for its text.
const TICK_HOURS = [1, 2, 3, 4, 6, 12];
const UNKNOWN = "Unknown";
const NO_DATA = "No data";

const plain = (value) => String(value == null ? "" : value)
  .replace(/[\u0000-\u001f\u007f-\u009f؜‎‏‪-‮⁦-⁩]/g, " ").trim();
const integer = (value) => Number.isSafeInteger(value);
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

// The gateway's answer, checked and cut to what the sheet draws, or an error.
function valid(raw) {
  const fail = () => { throw new Error("Invalid room history"); };
  if (!raw || raw.version !== 1 || !integer(raw.start) || !integer(raw.end) || !integer(raw.step)
      || raw.step <= 0 || raw.end <= raw.start || !raw.labels || typeof raw.labels !== "object"
      || !Array.isArray(raw.rooms) || raw.rooms.length > MAX_ROOMS) fail();
  const labels = {};
  Object.entries(raw.labels).forEach(([value, label]) => {
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,99}$/.test(value) || typeof label !== "string" || !plain(label)) fail();
    labels[value] = plain(label).slice(0, 80);
  });
  const rooms = raw.rooms.map((room) => {
    if (!room || typeof room.room !== "string" || typeof room.label !== "string" || !Array.isArray(room.segments)
        || room.segments.length > MAX_SEGMENTS) fail();
    let last = raw.start;
    const segments = room.segments.map((segment) => {
      if (!Array.isArray(segment) || segment.length !== 3) fail();
      const [from, to, value] = segment;
      if (!integer(from) || !integer(to) || from < last || to <= from || to > raw.end
          || !(value === null || typeof value === "string" && own(labels, value))) fail();
      last = to;
      return [from, to, value];
    });
    return { room: room.room, label: plain(room.label).slice(0, 80) || room.room, segments };
  });
  return { start: raw.start, end: raw.end, step: raw.step, labels, rooms };
}

// A fictional day for --mock: the house's rooms Off, one on its first
// source each evening, one Unknown for two hours, and one minute's gap.
function fictional(house, nowMs) {
  const end = Math.floor(nowMs / 60000) * 60, start = end - DAY_S;
  const labels = { off: "Off" };
  const rooms = ((house && house.rooms) || []).slice(0, MAX_ROOMS).map((room, index) => {
    const source = (room.sources || [])[0];
    if (source) labels[source] = plain((house.sources && house.sources[source] || {}).label || source);
    const at = (hours) => start + Math.round(hours * 3600 / 60) * 60, evening = 6 + index % 6;
    const segments = index % 2 === 0 && source
      ? [[start, at(evening), "off"], [at(evening), at(evening + 3), source], [at(evening + 3), end, "off"]]
      : [[start, at(4), "off"], [at(4), at(4) + 60 * 120, null], [at(4) + 60 * 120, at(12), "off"],
        [at(12) + 60, end, "off"]];
    return { room: room.id, label: room.label || room.id, segments };
  });
  return { start, end, step: 60, labels, rooms };
}

/*
 * The sheet's reads. options: request(path, init) -> the answer's JSON (the
 * gateway through the access client), or null where there is none (--native);
 * mock and house for --mock; changed(); now and the timers.
 */
function create(options = {}) {
  const now = options.now || (() => Date.now());
  const later = options.setTimeout || ((callback, ms) => setTimeout(callback, ms));
  const cancel = options.clearTimeout || ((handle) => clearTimeout(handle));
  const changed = options.changed || (() => {});
  const state = { loading: false, loaded: false, error: "", data: null, at: null, mock: Boolean(options.mock) };
  let generation = 0, opened = false, refresh = null, controller = null;

  function schedule() {
    if (refresh) cancel(refresh);
    refresh = opened ? later(() => { refresh = null; load(); }, REFRESH_MS) : null;
  }

  async function load() {
    if (!opened) return;
    const current = ++generation;
    if (state.mock) {
      state.data = fictional(options.house, now()); state.at = now(); state.loaded = true; state.error = "";
      schedule(); changed(); return;
    }
    if (!options.request) {
      state.error = "Room history comes from the Thermite gateway; this terminal talks to openHAB directly.";
      changed(); return;
    }
    controller = typeof AbortController === "function" ? new AbortController() : null;
    state.loading = true; changed();
    let timer;
    try {
      const answer = await Promise.race([
        options.request(PATH, controller ? { signal: controller.signal } : {}),
        new Promise((_, reject) => { timer = later(() => { controller?.abort(); reject(new Error("timeout")); }, TIMEOUT_MS); })
      ]);
      if (current !== generation) return;
      state.data = valid(answer); state.at = now(); state.loaded = true; state.error = "";
    } catch (error) {
      if (current !== generation) return;
      state.error = error && error.status === 401 ? "Sign in again to read room history."
        : "Room history is unavailable: the gateway could not read the house's metrics.";
    } finally {
      cancel(timer);
      if (current === generation) { controller = null; state.loading = false; schedule(); changed(); }
    }
  }

  function open() {
    if (opened) return Promise.resolve();
    opened = true;
    return load();
  }

  function close() {
    opened = false;
    generation += 1;
    if (refresh) cancel(refresh);
    refresh = null;
    if (controller) controller.abort();
    controller = null;
    state.loading = false;
  }

  return { open, close, stop: close, state: () => state, load };
}

// ---- drawing ----------------------------------------------------------------

function styleOf(value) {
  return value === undefined ? "day.none" : value === null ? "day.unknown" : value === "off" ? "day.off" : "day.source";
}

function wordOf(data, value) {
  return value === undefined ? NO_DATA : value === null ? UNKNOWN : data.labels[value] || value;
}

// The value each of width columns shows: the one holding most of its time
// (undefined: no data), as Grafana merges a pixel's samples.
function columns(room, data, width) {
  const span = (data.end - data.start) / width;
  const result = [];
  let index = 0;
  for (let column = 0; column < width; column += 1) {
    const from = data.start + column * span, to = from + span;
    const held = new Map();
    let covered = 0;
    while (index < room.segments.length && room.segments[index][1] <= from) index += 1;
    for (let at = index; at < room.segments.length && room.segments[at][0] < to; at += 1) {
      const [start, end, value] = room.segments[at];
      const time = Math.min(end, to) - Math.max(start, from);
      if (time <= 0) continue;
      held.set(value, (held.get(value) || 0) + time);
      covered += time;
    }
    let best, most = span - covered;
    held.forEach((time, value) => { if (time >= most) { best = value; most = time; } });
    result.push(best);
  }
  return result;
}

function clock(seconds, withDay, reference) {
  const date = new Date(seconds * 1000);
  const two = (number) => String(number).padStart(2, "0");
  const day = withDay && reference && new Date(reference * 1000).toDateString() !== date.toDateString()
    ? date.toLocaleDateString("en-US", { weekday: "short" }) + " " : "";
  return day + two(date.getHours()) + ":" + two(date.getMinutes());
}

function duration(seconds) {
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return minutes + " min";
  return Math.floor(minutes / 60) + " h" + (minutes % 60 ? " " + (minutes % 60) + " min" : "");
}

// Local-time hour marks across the bar: [column, text], as many as fit.
function ticks(data, x, width) {
  const span = (data.end - data.start) / width;
  for (const hours of TICK_HOURS) {
    const marks = [];
    const first = new Date(data.start * 1000);
    first.setMinutes(0, 0, 0);
    for (let at = first.getTime() / 1000; at < data.end; at += 3600) {
      const hour = new Date(at * 1000).getHours();
      if (at < data.start || hour % hours) continue;
      marks.push([x + Math.floor((at - data.start) / span), clock(at)]);
    }
    if (marks.every((mark, index) => index === 0 || mark[0] - marks[index - 1][0] >= 7)) return marks;
  }
  return [];
}

function content(g, width, ctx) {
  const day = ctx.day || {};
  const state = day.backend ? day.backend.state() : { loading: true };
  let y = 0;
  const line = (text, style = "dim") => { g.put(1, y++, fit(text, Math.max(4, width - 2)), style); };
  line("Each room's confirmed source over the last 24 hours, not the requested source.");
  if (state.mock) line("Preview · a fictional day", "tone.busy");
  if (state.error) line(state.error, "alert");
  y += 1;
  const data = state.data;
  if (!data) { line(state.loading ? "Reading room history…" : state.error ? "" : "Room history is unavailable."); return y; }
  if (!data.rooms.length) { line("No rooms configured"); return y; }
  const nameWidth = Math.min(18, Math.max(6, ...data.rooms.map((room) => length(room.label))));
  const nowWidth = width >= 72 ? Math.min(16, Math.max(7, ...data.rooms.map((room) => {
    const last = room.segments[room.segments.length - 1];
    return length(wordOf(data, last && last[1] === data.end ? last[2] : undefined));
  }))) : 0;
  const x = nameWidth + 3;
  const barWidth = Math.max(8, width - x - (nowWidth ? nowWidth + 2 : 0) - 1);
  const axis = ticks(data, x, barWidth);
  axis.forEach(([at, text]) => {
    if (at + 1 + length(text) > x + barWidth) return;
    g.put(at, y, "╵", "faint");
    g.put(at + 1, y, text, "dim");
  });
  if (nowWidth) g.put(x + barWidth + 2, y, "Now", "dim");
  y += 1;
  const focusKey = ctx.ui.focus && String(ctx.ui.focus).startsWith("day:") ? ctx.ui.focus : null;
  const chosen = data.rooms.find((room) => "day:" + room.room === focusKey) || data.rooms[0];
  data.rooms.forEach((room) => {
    const key = "day:" + room.room;
    const active = ctx.ui.focusVisible && ctx.ui.focus === key;
    const last = room.segments[room.segments.length - 1];
    const now = last && last[1] === data.end ? last[2] : undefined;
    g.put(1, y, fit(room.label, nameWidth + 1), active ? "row.focus" : room === chosen ? "strong" : "base");
    const cells = columns(room, data, barWidth);
    cells.forEach((value, column) => g.put(x + column, y, value === undefined ? "·" : " ", styleOf(value)));
    // Each run wide enough shows its words, as the board shows values.
    for (let from = 0; from < cells.length;) {
      let to = from + 1;
      while (to < cells.length && cells[to] === cells[from]) to += 1;
      const word = cells[from] === undefined ? "" : wordOf(data, cells[from]);
      if (word && to - from >= length(word) + 2) g.put(x + from + 1, y, word, styleOf(cells[from]));
      from = to;
    }
    if (nowWidth) g.put(x + barWidth + 2, y, fit(wordOf(data, now), nowWidth), now === undefined ? "faint" : "base");
    g.hit(0, y, width, 1, { kind: "day-room", room: room.room, key, focus: true,
      label: room.label + ": " + wordOf(data, now) + " now" });
    y += 1;
  });
  y += 1;
  let at = 1;
  [["Source", "day.source"], ["Off", "day.off"], [UNKNOWN + ": no readback", "day.unknown"]].forEach(([text, style]) => {
    at = g.put(at, y, "  ", style);
    at = g.put(at + 1, y, text, "dim") + 3;
  });
  at = g.put(at, y, "·", "day.none");
  g.put(at + 1, y, NO_DATA, "dim");
  y += 1;
  if (state.at != null) line("Read " + clock(Math.floor(state.at / 1000)) + (state.mock ? "" : " · every minute while open"), "faint");
  y += 1;
  g.put(1, y++, fit(chosen.label + " · latest", Math.max(4, width - 2)), "section");
  const recent = chosen.segments.slice(-RECENT).reverse();
  if (!recent.length) line(NO_DATA);
  recent.forEach(([from, to, value]) => {
    const when = clock(from, true, data.end) + " – " + (to === data.end ? "now" : clock(to, true, data.end));
    const parts = [[fit(when, 22) + " ", "dim"], [fit(wordOf(data, value), 24) + " ", value === null ? "tone.busy" : "base"],
      [duration(to - from), "dim"]];
    g.line(2, y++, parts);
  });
  return y;
}

module.exports = { PATH, REFRESH_MS, TIMEOUT_MS, create, content, valid, columns, fictional };
