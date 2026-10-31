/*
 * A small bowl of flaming thermite in ordinary terminal braille glyphs:
 * an asymmetric flame contour, a short hot inner stroke and a fine cup.
 * The mark has open negative space rather than a filled dot
 * lattice. Supersampled curves soften its ink against the theme ground.
 * Only the flame drifts; brief tiny sparks cross the surrounding space.
 *
 * frame(spec, rows, time, colors) returns [glyph, fg hex, bg hex, mono].
 * time null composes a still mark with no sparks. Paths, bounds and stroke
 * widths come from the same canonical brand spec used by SVG and Python.
 */
"use strict";
const { contrast } = require("./paint");
const FRAME_MS = 100;
const LOOP_MS = 6000;
const MIN_ROWS = 4;
const MAX_ROWS = 6;
const SAMPLES = 4;
const TAU = 2 * Math.PI;
const DOTS = [0x01, 0x08, 0x02, 0x10, 0x04, 0x20, 0x40, 0x80];
// Start ms, x, horizontal/vertical speed, lifetime: brief thermite pinpricks.
const SPARKS = [[600, 29, -13, -49, 520], [690, 35, 18, -58, 450], [3370, 34, 11, -53, 540]];

function polygon(path, steps) {
  const points = [];
  const tokens = path.match(/[MLCZ]|-?\d+(?:\.\d+)?/g);
  let op = null;
  let args = [];
  const flush = () => {
    if (op === "M" || op === "L") points.push([args[0], args[1]]);
    else if (op === "C") {
      const [x0, y0] = points[points.length - 1];
      const [x1, y1, x2, y2, x3, y3] = args;
      for (let i = 1; i <= steps; i += 1) {
        const t = i / steps;
        const u = 1 - t;
        points.push([u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
          u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3]);
      }
    }
  };
  tokens.forEach((token) => {
    if ("MLCZ".indexOf(token) >= 0) {
      flush();
      op = token;
      args = [];
    } else args.push(parseFloat(token));
  });
  flush();
  return points;
}



function size(spec, rows) {
  const unit = (spec.height - spec.top) / (rows * 4);
  const columns = Math.ceil(spec.width / unit / 2);
  return { columns, unit, left: spec.width / 2 - columns * unit, top: spec.top };
}
const profiles = new WeakMap();
function profile(spec) {
  if (!profiles.has(spec)) {
    const geometry = spec.layers.map(([role, path]) => [role, polygon(path, spec.steps)]);
    const rim = geometry.find(([role]) => role === "metal");
    const base = rim ? Math.min(...rim[1].map(point => point[1])) : spec.height;
    profiles.set(spec, { geometry, base });
  }
  return profiles.get(spec);
}
function rgb(color) { return [1, 3, 5].map((at) => parseInt(color.slice(at, at + 2), 16)); }
function hex(color) { return "#" + color.map((v) => Math.round(v).toString(16).padStart(2, "0")).join(""); }
function mix(a, b, t) { return a.map((v, k) => v + (b[k] - v) * t); }
function hot(colors) {
  const color = hex(mix(rgb(colors.glow), rgb(colors.core), 0.7));
  // Incandescent cream is visible against a dark ground. On paper, use
  // the palette's flame ink so the small core and sparks do not disappear.
  return rgb(contrast(color, colors.bg) >= 3 ? color : colors.flame);
}
function sway(points, phase, core, spec, base) {
  return points.map(([x, y]) => {
    const h = Math.max(0, (base - y) / (base - spec.top));
    const scale = spec.width / 64;
    return [x + scale * h * h * (1.5 * Math.sin(phase * 2 - h * 2 + (core ? 0.4 : 0))
      + 0.45 * Math.sin(phase * 3 + h * 5)), y - scale * 0.8 * h * h * Math.sin(phase * 2 + x * 0.08)];
  });
}
function distance(x, y, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const t = Math.max(0, Math.min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy || 1)));
  return Math.hypot(x - a[0] - t * dx, y - a[1] - t * dy);
}
// Four by four sub-samples per braille dot. The supplied stroke widths
// remain in canonical drawing units, as in the SVG.
function raster(spec, rows, time) {
  const layout = size(spec, rows), { columns, unit, left } = layout;
  const { geometry, base } = profile(spec);
  const width = columns * 2 * SAMPLES, height = rows * 4 * SAMPLES;
  const field = new Uint8Array(width * height);
  geometry.forEach(([role, outline], layer) => {
    if (rows < 5 && role === "core") return;
    const stroke = spec.strokes[role] / 2;
    const points = time == null || role === "metal" ? outline : sway(outline, TAU * time / LOOP_MS, role === "core", spec, base);
    for (let k = 1; k < points.length; k += 1) {
      const a = points[k - 1], b = points[k];
      const x0 = Math.max(0, Math.floor((Math.min(a[0], b[0]) - stroke - left) / unit * SAMPLES));
      const x1 = Math.min(width, Math.ceil((Math.max(a[0], b[0]) + stroke - left) / unit * SAMPLES));
      const y0 = Math.max(0, Math.floor((Math.min(a[1], b[1]) - stroke - spec.top) / unit * SAMPLES));
      const y1 = Math.min(height, Math.ceil((Math.max(a[1], b[1]) + stroke - spec.top) / unit * SAMPLES));
      for (let y = y0; y < y1; y += 1) for (let x = x0; x < x1; x += 1) {
        if (distance(left + (x + 0.5) * unit / SAMPLES, spec.top + (y + 0.5) * unit / SAMPLES, a, b) <= stroke)
          field[y * width + x] = layer + 1;
      }
    }
  });
  const dots = [];
  for (let y = 0; y < rows * 4; y += 1) {
    const line = [];
    for (let x = 0; x < columns * 2; x += 1) {
      const votes = new Array(geometry.length + 1).fill(0);
      for (let j = 0; j < SAMPLES; j += 1) for (let i = 0; i < SAMPLES; i += 1)
        votes[field[(y * SAMPLES + j) * width + x * SAMPLES + i]] += 1;
      const coverage = 1 - votes[0] / (SAMPLES * SAMPLES);
      let material = 1;
      for (let k = 2; k < votes.length; k += 1) if (votes[k] > votes[material]) material = k;
      line.push({ coverage, role: geometry[material - 1][0] });
    }
    dots.push(line);
  }
  return { dots, layout };
}
function sparks(cells, spec, rows, time, colors, layout) {
  if (time == null) return;
  const active = new Map();
  SPARKS.forEach(([start, from, vx, vy, lifetime]) => {
    const age = (time - start + LOOP_MS) % LOOP_MS;
    if (age >= lifetime) return;
    const seconds = age / 1000;
    const scale = spec.width / 64;
    const x = (from + vx * seconds) * scale;
    const y = profile(spec).base * .43 + (vy * seconds + 14 * seconds * seconds) * scale;
    const dotColumn = Math.floor((x - layout.left) / layout.unit);
    const dotRow = Math.floor((y - layout.top) / layout.unit);
    const column = Math.floor(dotColumn / 2), row = Math.floor(dotRow / 4);
    if (column < 0 || column >= layout.columns || row < 0 || row >= rows || cells[row][column]) return;
    const key = row + ":" + column;
    const point = active.get(key) || { row, column, bits: 0, heat: 0 };
    point.bits |= DOTS[dotRow % 4 * 2 + dotColumn % 2];
    // At small sizes a pinprick can emerge from the flame late. Keep it
    // legible on paper before its rapid final fade; lifetime stays brief.
    point.heat = Math.max(point.heat, Math.pow(1 - age / lifetime, 0.3));
    active.set(key, point);
  });
  active.forEach(({ row, column, bits, heat }) => {
    cells[row][column] = [String.fromCharCode(0x2800 + bits), hex(mix(rgb(colors.bg), hot(colors), heat)), colors.bg, "\u00b7"];
  });
}
const cache = new WeakMap();
function frame(spec, rows, time, colors) {
  let themes = cache.get(spec);
  if (!themes) { themes = new WeakMap(); cache.set(spec, themes); }
  let frames = themes.get(colors);
  if (!frames) { frames = new Map(); themes.set(colors, frames); }
  const index = time == null ? -1 : Math.floor(time / FRAME_MS) % (LOOP_MS / FRAME_MS), key = rows + ":" + index;
  if (frames.has(key)) return frames.get(key);
  const at = index < 0 ? null : index * FRAME_MS;
  const { dots, layout } = raster(spec, rows, at);
  const ink = { flame: rgb(colors.flame), core: hot(colors), metal: rgb(colors.metal) };
  const bg = rgb(colors.bg);
  const cells = Array.from({ length: rows }, (_, y) => Array.from({ length: layout.columns }, (_, x) => {
    let bits = 0, weight = 0, color = [0, 0, 0], coverage = 0;
    for (let j = 0; j < 4; j += 1) for (let i = 0; i < 2; i += 1) {
      const dot = dots[y * 4 + j][x * 2 + i];
      if (dot.coverage < 0.2) continue;
      bits |= DOTS[j * 2 + i];
      coverage = Math.max(coverage, dot.coverage);
      weight += dot.coverage;
      color = color.map((v, k) => v + ink[dot.role][k] * dot.coverage);
    }
    if (!bits) return null;
    // Keep AA edge ink quiet, but the main contour luminous and legible.
    color = mix(bg, color.map((v) => v / weight), Math.min(1, coverage * 1.35));
    const glyph = String.fromCharCode(0x2800 + bits);
    return [glyph, hex(color), colors.bg, glyph];
  }));
  sparks(cells, spec, rows, at, colors, layout);
  if (frames.size >= 96) frames.clear();
  frames.set(key, cells);
  return cells;
}
function rowsFor(height) { return height < MIN_ROWS ? 0 : Math.min(MAX_ROWS, height); }
module.exports = { FRAME_MS, LOOP_MS, MIN_ROWS, MAX_ROWS, frame, rowsFor, size, polygon };
