"use strict";
// The terminal remote's grid (tui/remote/grid.js) and painter (paint.js)
// against a small model of a terminal. Random frames are drawn into grids
// and painted over one another as the remote paints; the bytes play on a
// terminal that writes each character where its cursor stands and moves
// the cursor the columns glibc's wcwidth gives the character. Every write must land in the
// cell the grid has it in, and every frame must show what the grid holds.
// The PRNG is seeded, so a run is the same each time. And the lights
// (styles.js LIGHTS) and the focus where colour is short: without any, and
// in xterm's 256, for every palette thermite ships. Prints what it found;
// nothing leaves the process.
const fs = require("node:fs"), path = require("node:path");
const Grid = require("../tui/remote/grid"), Paint = require("../tui/remote/paint"), Theme = require("../tui/remote/theme");
const { Painter } = Paint;

// The model's widths, apart from grid.js's: glibc 2.44's wcwidth for each
// code point the frames use, written out. None is drawn in the cell
// before; a code point not here is a fault.
const WIDTHS = new Map([
  ["a", 1], ["b", 1], [" ", 1], ["#", 1], ["e", 1], ["\u00e9", 1], ["\u0301", 0], ["\u00ad", 1], ["\u6771", 2], ["\u4eac", 2],
  ["\u{1f3b5}", 2], ["\u0939", 1], ["\u093f", 1], ["\u0902", 0], ["\u0e01", 1], ["\u0e33", 1], ["\u0e31", 0], ["\u200d", 0],
  ["\ufe0f", 0], ["\u2764", 1], ["\u{1f1e6}", 1], ["\u{1f1e7}", 1], ["\u{1f1ec}", 1], ["\u{1f1ef}", 1], ["\u{1f1f5}", 1],
  ["\u2502", 1]]);

// A terminal without autowrap: a write past the right edge is a fault, as
// is one that lands where the grid has something else (but a wide
// character's right half, by the rest of what the grid has before it: a
// flag's second indicator); overwriting either half of a wide character
// blanks both.
class Terminal {
  constructor(width, height) {
    this.width = width;
    this.height = height;
    this.x = this.y = 0;
    this.faults = [];
    this.clear();
  }

  clear() {
    this.cells = Array.from({ length: this.height }, () => new Array(this.width).fill(" "));
  }

  erase(x) {
    const row = this.cells[this.y];
    if (row[x] === "") row[x - 1] = " ";
    else if (row[x + 1] === "") row[x + 1] = " ";
    row[x] = " ";
  }

  // Plays bytes, expect giving the character the grid has at (x, y).
  play(bytes, expect) {
    const control = /^\x1b(?:\[[?\d;]*[A-Za-z]|\][^\x07]*\x07)/;
    let rest = bytes;
    while (rest) {
      const match = control.exec(rest);
      if (match) {
        const move = /^\x1b\[(\d+);(\d+)H$/.exec(match[0]);
        if (move) [this.y, this.x] = [Number(move[1]) - 1, Number(move[2]) - 1];
        else if (match[0] === "\x1b[2J") this.clear();
        rest = rest.slice(match[0].length);
        continue;
      }
      const point = String.fromCodePoint(rest.codePointAt(0));
      rest = rest.slice(point.length);
      this.print(point, expect);
    }
  }

  print(point, expect) {
    const row = this.cells[this.y], width = WIDTHS.get(point);
    if (width === undefined) {
      this.faults.push("no width for " + JSON.stringify(point));
      return;
    }
    if (!width) {
      const at = row[this.x - 1] === "" ? this.x - 2 : this.x - 1;
      if (at < 0) this.faults.push("a mark at the row's start");
      else row[at] += point;
      return;
    }
    const wide = width === 2;
    if (this.x + (wide ? 2 : 1) > this.width) {
      this.faults.push(JSON.stringify(point) + " past the edge at " + this.x + "," + this.y);
      return;
    }
    const rest = expect(this.x, this.y) === "" && this.x > 0 && expect(this.x - 1, this.y).startsWith(row[this.x - 1] + point);
    if (!rest && !expect(this.x, this.y).startsWith(point)) {
      this.faults.push(JSON.stringify(point) + " at " + this.x + "," + this.y + " where the grid has " + JSON.stringify(expect(this.x, this.y)));
    }
    this.erase(this.x);
    if (wide) this.erase(this.x + 1);
    row[this.x] = point;
    if (wide) row[this.x + 1] = "";
    this.x += wide ? 2 : 1;
  }

  text() {
    return this.cells.map((row) => row.join("").replace(/\s+$/, "")).join("\n");
  }
}

// A terminal that draws an emoji asked for with VS16 two columns wide, as
// kitty does, where wcwidth gives the heart one.
class Wider extends Terminal {
  print(point, expect) {
    const row = this.cells[this.y];
    if (point !== "\ufe0f" || row[this.x - 1] !== "\u2764") return super.print(point, expect);
    row[this.x - 1] += point;
    this.erase(this.x);
    row[this.x] = "";
    this.x += 1;
  }
}

// mulberry32: a small seeded PRNG.
function random(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// A soft hyphen the grid drops (glibc's wcwidth would give it a column) and a flag among them.
const ALPHABET = ["a", "b", " ", "\u00e9", "e\u0301", "\u00ad", "\u6771", "\u4eac", "\u{1f3b5}", "\u0939", "\u093f", "\u0902",
  "\u0e01", "\u0e33", "\u0e31", "\u200d", "\ufe0f", "\u2764", "\u{1f1e6}", "\u{1f1ef}\u{1f1f5}", "\u2502"];
const STYLES = ["base", "dim", "alert"];

// Random frames painted one over another; how many diverged from the grid, and the first faults.
function fuzz(seed, frames, depth) {
  const next = random(seed), pick = (list) => list[Math.floor(next() * list.length)];
  const between = (low, high) => low + Math.floor(next() * (high - low + 1));
  const text = () => Array.from({ length: between(1, 7) }, () => pick(ALPHABET)).join("");
  const width = between(4, 14), height = 3;
  const painter = new Painter(() => {}, depth), terminal = new Terminal(width, height);
  let diverged = 0, faults = [];
  for (let frame = 0; frame < frames; frame += 1) {
    const grid = new Grid.Grid(width, height);
    for (let step = between(1, 8); step > 0; step -= 1) {
      const kind = next();
      if (kind < 0.6) grid.put(between(-3, width + 1), between(0, height - 1), text(), pick(STYLES), null, next() < 0.2 ? "#" : undefined);
      else if (kind < 0.8) grid.fill(between(-2, width), between(0, height - 1), between(1, 5), 1, pick(STYLES));
      else {
        const source = new Grid.Grid(between(2, 8), null);
        source.put(between(-1, 3), 0, text(), pick(STYLES));
        grid.blit(source, 0, between(-3, width), between(0, height - 1), between(1, 8), 1);
      }
    }
    const shown = (cell) => (!depth && cell.mono && cell.ch ? cell.mono : cell.ch);
    let bytes = "";
    painter.write = (out) => { bytes += out; };
    painter.paint(grid.rows, width, height, (style) => "\x1b[" + (STYLES.indexOf(style) + 1) + "m");
    const before = terminal.faults.length;
    terminal.play(bytes, (x, y) => shown(grid.rows[y][x]));
    const want = grid.rows.map((row) => row.map(shown).join("").replace(/\s+$/, "")).join("\n");
    if (terminal.text() !== want || terminal.faults.length > before) {
      diverged += 1;
      if (faults.length < 3) faults.push({ want: want, got: terminal.text(), faults: terminal.faults.slice(before, before + 3) });
    }
  }
  return { diverged: diverged, faults: faults };
}

const cells = (grid) => grid.rows.map((row) => row.map((cell) => cell.ch));
const proof = {};
// The edges: a wide character never left in the last column, nor a right half at the first.
let grid = new Grid.Grid(5, 1);
grid.put(3, 0, "a\u6771b");
proof.lastColumn = cells(grid)[0];
grid = new Grid.Grid(5, 1);
grid.put(-1, 0, "\u6771\u4eaca");
proof.firstColumn = cells(grid)[0];
// A mono character stands in for a narrow cell only, never a wide one or its half.
grid = new Grid.Grid(4, 1);
grid.put(0, 0, "\u6771a", "base", null, "#");
proof.mono = grid.rows[0].map((cell) => cell.mono || null);
// Columns, a code point at a time.
proof.columns = ["\u0939\u093f\u0902\u0926\u0940 \u0917\u093e\u0928\u093e", "\u0e01\u0e33\u0e25\u0e31\u0e07",
  "\u{1f468}\u200d\u{1f469}\u200d\u{1f467}", "\u2764\ufe0f", "\u{1f1e6}", "Cafe\u0301", "\u0301a"].map((text) => Grid.length(text));
// Where glibc's wcwidth (2.44, Unicode 17) differs from the blocks: hexagrams,
// trigrams and digrams, Khitan, Kana Extended-B and Tai Xuan Jing take two;
// a Hangul vowel of Jamo Extended-B and the Hangul fillers none.
proof.blocks = ["\u4dc0", "\u2630", "\u268a", "\u{18b00}", "\u{1aff0}", "\u{1d300}", "\u1100\u1161\ud7b0", "a\u3164", "a\uffa0"]
  .map((text) => Grid.length(text));
grid = new Grid.Grid(8, 1);
grid.put(0, 0, "\u{1f468}\u200d\u{1f469}\u200d\u{1f467}");
proof.zwj = cells(grid)[0];
grid = new Grid.Grid(5, 1);
grid.put(0, 0, "\u0e01\u0e33\u0e25\u0e31\u0e07");
proof.thai = cells(grid)[0];
// A flag is one wide character, its indicators a column each in a terminal
// as wcwidth; a third indicator is a cell of its own.
grid = new Grid.Grid(8, 1);
grid.put(0, 0, "\u{1f1ef}\u{1f1f5}\u{1f1ec}\u{1f1e7}\u{1f1e6}a");
let flags = new Terminal(8, 1);
new Painter((out) => flags.play(out, (x) => grid.rows[0][x].ch), 1).paint(grid.rows, 8, 1, () => "\x1b[0m");
proof.flags = [cells(grid)[0], flags.text(), flags.faults];
// Format characters go (a soft hyphen, a bidi isolate, a zero width space); ZWJ and a flag's tags stay.
proof.format = ["Ex\u00adample", "\u2067ab\u2069 c\u200bd", "\u{1f3f4}\u{e0067}\u{e0062}\u{e0073}\u{e0063}\u{e0074}\u{e007f}"].map((text) => {
  grid = new Grid.Grid(8, 1);
  grid.put(0, 0, text);
  return [Grid.length(text), cells(grid)[0].join("").trimEnd()];
});
// Wrapped narrower than a wide character: one whole character to a line, and no empty line after.
proof.wrap = [Grid.wrap("\u6771\u4eac", 1), Grid.wrap("\u6771\u4eac", 2), Grid.wrap("ab \u6771\u4eac\u6771", 3)];
// The painter alone, on a row no grid draws now (a right half with no
// character before it): what follows still lands in its own cell.
const terminal = new Terminal(6, 1), row = [{ ch: "", style: "base" }, { ch: "a", style: "base" },
  { ch: "\u6771", style: "base", wide: true }, { ch: "", style: "base" }, { ch: "b", style: "base" }, { ch: " ", style: "base" }];
new Painter((out) => terminal.play(out, (x) => row[x].ch), 1).paint([row], 6, 1, () => "\x1b[0m");
proof.orphan = [terminal.text(), terminal.faults];
// On a terminal that draws a cell wider than the grid has it, the cell
// after is placed in its own column: what it overwrites is that cell's,
// and the rest of the row stays where the grid has it.
grid = new Grid.Grid(5, 1);
grid.put(0, 0, "\u2764\ufe0f ab");
const wider = new Wider(5, 1);
new Painter((out) => wider.play(out, (x) => grid.rows[0][x].ch), 1).paint(grid.rows, 5, 1, () => "\x1b[0m");
proof.wider = [wider.text(), wider.faults];
// The lights: each state's glyphs and the columns they take; and for each
// shipped palette and mode, each state's style without colour, its colour
// in xterm's 256 beside the hotkeys' accent, which of them are grays there,
// and the focus styles that are not reverse video without colour.
const palettes = path.join(__dirname, "..", "lib", "oh_brand", "palettes");
const index = (code) => Number((/38;5;(\d+)/.exec(code) || [])[1]);
const gray = (at) => at >= 232 || (at >= 16 && Math.floor((at - 16) / 36) === Math.floor((at - 16) / 6) % 6 && Math.floor((at - 16) / 6) % 6 === (at - 16) % 6);
proof.lights = {
  glyphs: Object.fromEntries(Object.entries(Theme.LIGHTS).map(([name, entry]) => [name, [entry.glyph].concat(entry.blink || [])])),
  columns: Object.values(Theme.LIGHTS).flatMap((entry) => [entry.glyph].concat(entry.blink || [])).map((glyph) => Grid.length(glyph)),
  drawn: Object.keys(Theme.LIGHTS).concat(["no such state"]).map((name) => Theme.light(name).concat(Theme.light(name, true)[0])),
  palettes: {}
};
fs.readdirSync(palettes).filter((name) => name.endsWith(".json")).sort().forEach((file) => {
  const look = Theme.create(JSON.parse(fs.readFileSync(path.join(palettes, file), "utf8")));
  ["dark", "light"].forEach((mode) => {
    const base = Theme.resolve(look, mode, "base"), code = (name, depth) => Paint.sgr(Theme.resolve(look, mode, name), depth, base);
    const states = Object.keys(Theme.LIGHTS), colors = Object.fromEntries(states.map((name) => [name, index(code("led." + name, 256))]));
    proof.lights.palettes[file.replace(/\.json$/, "") + " " + mode] = {
      mono: Object.fromEntries(states.map((name) => [name, code("led." + name, 0).replace("\x1b", "")])),
      colors: Object.assign(colors, { hotkey: index(code("hotkey", 256)), alert: index(code("alert", 256)) }),
      grays: Object.keys(colors).filter((name) => gray(colors[name])),
      unseen: Object.keys(look.modes[mode]).filter((name) => /\.focus$/.test(name) && !/;7m$/.test(code(name, 0)))
    };
  });
});
// Random frames, in color and without.
proof.fuzz = [[1, 1], [2, 1], [3, 0], [4, 1], [5, 0]].map(([seed, depth]) => fuzz(seed, 200, depth));
process.stdout.write(JSON.stringify(proof) + "\n");
