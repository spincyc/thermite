/*
 * A screen as a grid of cells, each one character and a style, with the
 * places a click or the focus can reach (hits). The view draws a whole
 * frame into one; app.js turns it into blessed tags, and the tests read it
 * as text. Every character drawn must take one column. order lists the
 * focusable hits as drawn, those clipped out of sight included, for Tab.
 */
"use strict";

class Grid {
  // height null: the grid grows as rows are drawn (a panel's content, scrolled later).
  constructor(width, height, style) {
    this.width = Math.max(0, width);
    this.height = height == null ? 0 : Math.max(0, height);
    this.grows = height == null;
    this.style = style || "base";
    this.rows = [];
    this.hits = [];
    this.order = [];
    for (let y = 0; y < this.height; y += 1) this.rows.push(this.blank());
  }

  blank() {
    const row = [];
    for (let x = 0; x < this.width; x += 1) row.push({ ch: " ", style: this.style });
    return row;
  }

  ensure(y) {
    if (!this.grows) return y >= 0 && y < this.height;
    while (this.rows.length <= y) this.rows.push(this.blank());
    this.height = this.rows.length;
    return y >= 0;
  }

  // Writes text at (x, y), clipped to the grid; returns the column after it.
  // mono is an optional character to use when color cannot distinguish a
  // glyph's foreground and background (the torch's two-tone metal cells).
  put(x, y, text, style, hit, mono) {
    const chars = characters(text);
    if (hit && chars.length && hit.focus) this.order.push(hit);
    if (!this.ensure(y)) return x + chars.length;
    chars.forEach((ch, index) => {
      const at = x + index;
      if (at >= 0 && at < this.width) {
        this.rows[y][at] = { ch: ch, style: style || this.style };
        if (mono) this.rows[y][at].mono = mono;
      }
    });
    if (hit && chars.length) this.hit(x, y, chars.length, 1, hit, false);
    return x + chars.length;
  }

  // Segments [text, style, hit] side by side from x; returns the column after them.
  line(x, y, segments) {
    let at = x;
    segments.forEach((segment) => {
      if (segment) at = this.put(at, y, segment[0], segment[1], segment[2]);
    });
    return at;
  }

  fill(x, y, w, h, style, ch) {
    for (let row = y; row < y + h; row += 1) this.put(x, row, (ch || " ").repeat(Math.max(0, w)), style);
  }

  // A place a click reaches; mark false keeps it out of the focus order.
  hit(x, y, w, h, hit, mark) {
    if (mark !== false && hit.focus) this.order.push(hit);
    const left = Math.max(0, x);
    const right = Math.min(this.width, x + w);
    if (right <= left) return;
    this.hits.push({ x: left, y: y, w: right - left, h: h, hit: hit });
  }

  // The topmost hit at (x, y), or null.
  hitAt(x, y) {
    for (let index = this.hits.length - 1; index >= 0; index -= 1) {
      const entry = this.hits[index];
      if (x >= entry.x && x < entry.x + entry.w && y >= entry.y && y < entry.y + entry.h) return entry;
    }
    return null;
  }

  // Copies rows from..from+h of src to (x, y), with their hits.
  blit(src, from, x, y, w, h) {
    for (let row = 0; row < h; row += 1) {
      const line = src.rows[from + row];
      if (!line || !this.ensure(y + row)) continue;
      for (let col = 0; col < Math.min(w, src.width); col += 1) {
        const at = x + col;
        if (at >= 0 && at < this.width) this.rows[y + row][at] = line[col];
      }
    }
    src.hits.forEach((entry) => {
      if (entry.y < from || entry.y >= from + h) return;
      const left = Math.max(0, entry.x);
      const right = Math.min(w, entry.x + entry.w);
      if (right > left) this.hit(x + left, y + entry.y - from, right - left, 1, entry.hit, false);
    });
  }

  text() {
    return this.rows.map((row) => row.map((cell) => cell.ch).join("").replace(/\s+$/, "")).join("\n");
  }
}

// Text cut to width with an ellipsis, or padded to it.
function fit(text, width, align) {
  const chars = characters(text);
  if (width <= 0) return "";
  if (chars.length > width) return chars.slice(0, Math.max(0, width - 1)).join("") + "\u2026";
  const pad = " ".repeat(width - chars.length);
  return align === "right" ? pad + chars.join("") : chars.join("") + pad;
}

function length(text) {
  return Array.from(String(text)).length;
}

// A cell is printable text. A newline, tab or terminal control from a label
// must never move the terminal's cursor outside the cell the painter owns.
function characters(text) {
  return Array.from(String(text).replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " "));
}

// Words wrapped to width; a word longer than width is cut.
function wrap(text, width) {
  const lines = [];
  String(text).split("\n").forEach((paragraph) => {
    let line = "";
    paragraph.split(/\s+/).filter(Boolean).forEach((word) => {
      while (length(word) > width) {
        if (line) {
          lines.push(line);
          line = "";
        }
        lines.push(Array.from(word).slice(0, width).join(""));
        word = Array.from(word).slice(width).join("");
      }
      if (!line) line = word;
      else if (length(line) + 1 + length(word) <= width) line += " " + word;
      else {
        lines.push(line);
        line = word;
      }
    });
    lines.push(line);
  });
  return lines;
}

module.exports = { Grid, fit, wrap, length };
