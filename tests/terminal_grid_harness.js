"use strict";
// The terminal remote's grid (tui/remote/grid.js) and painter (paint.js)
// against a small model of a terminal. Random frames are drawn into grids
// and painted over one another as the remote paints; the bytes play on a
// terminal that writes each character where its cursor stands and moves
// the cursor the columns the character takes. Every write must land in the
// cell the grid has it in, and every frame must show what the grid holds.
// The PRNG is seeded, so a run is the same each time. Prints what it found;
// nothing leaves the process.
const Grid = require("../tui/remote/grid"), { Painter } = require("../tui/remote/paint");

// The model's widths, written apart from grid.js's (for the characters the
// frames use): none for a mark or a format character, drawn in the cell
// before; two for CJK and an emoji by default; one for any other.
const ZERO = /[\p{Mn}\p{Me}\p{Cf}\u1160-\u11ff]/u;
const WIDE = /[\u3041-\u33ff\u4e00-\u9fff\uac00-\ud7a3\p{Emoji_Presentation}]/u;

// A terminal without autowrap: a write past the right edge is a fault, as
// is one that lands where the grid has something else; overwriting either
// half of a wide character blanks both.
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
    const row = this.cells[this.y];
    if (ZERO.test(point)) {
      const at = row[this.x - 1] === "" ? this.x - 2 : this.x - 1;
      if (at < 0) this.faults.push("a mark at the row's start");
      else row[at] += point;
      return;
    }
    const wide = WIDE.test(point);
    if (this.x + (wide ? 2 : 1) > this.width) {
      this.faults.push(JSON.stringify(point) + " past the edge at " + this.x + "," + this.y);
      return;
    }
    if (!expect(this.x, this.y).startsWith(point)) {
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

const ALPHABET = ["a", "b", " ", "\u00e9", "e\u0301", "\u6771", "\u4eac", "\u{1f3b5}", "\u0939", "\u093f", "\u0902", "\u0e01",
  "\u0e33", "\u0e31", "\u200d", "\ufe0f", "\u2764", "\u{1f1e6}", "\u2502"];
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
grid = new Grid.Grid(8, 1);
grid.put(0, 0, "\u{1f468}\u200d\u{1f469}\u200d\u{1f467}");
proof.zwj = cells(grid)[0];
grid = new Grid.Grid(5, 1);
grid.put(0, 0, "\u0e01\u0e33\u0e25\u0e31\u0e07");
proof.thai = cells(grid)[0];
// The painter alone, on a row no grid draws now (a right half with no
// character before it): what follows still lands in its own cell.
const terminal = new Terminal(6, 1), row = [{ ch: "", style: "base" }, { ch: "a", style: "base" },
  { ch: "\u6771", style: "base", wide: true }, { ch: "", style: "base" }, { ch: "b", style: "base" }, { ch: " ", style: "base" }];
new Painter((out) => terminal.play(out, (x) => row[x].ch), 1).paint([row], 6, 1, () => "\x1b[0m");
proof.orphan = [terminal.text(), terminal.faults];
// Random frames, in color and without.
proof.fuzz = [[1, 1], [2, 1], [3, 0], [4, 1], [5, 0]].map(([seed, depth]) => fuzz(seed, 200, depth));
process.stdout.write(JSON.stringify(proof) + "\n");
