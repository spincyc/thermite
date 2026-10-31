/*
 * The terminal's bytes: a frame's grid as escape sequences, in 24-bit
 * color where the terminal shows it and in xterm's 256 otherwise, and only
 * the cells that changed since the last frame. blessed cannot do this (it
 * packs a color in 9 bits, so every theme color lands on its nearest
 * xterm-256 one, and some on the 16 the terminal's own theme sets);
 * app.js keeps blessed for the keys, the mouse and resizes alone.
 */
"use strict";

const childProcess = require("child_process");

// Overrides what colorDepth() finds: truecolor (or 24bit), 256, or none.
const FORCE = "THERMITE_REMOTE_COLOR";
// Terminals that show 24-bit color without saying so in COLORTERM.
const DIRECT_TERMS = /-direct$|^xterm-(kitty|ghostty)$|^(alacritty|foot|wezterm|contour|rio|ghostty)/;
const DIRECT_PROGRAMS = ["iTerm.app", "WezTerm", "vscode", "Hyper", "ghostty", "rio", "WarpTerminal"];
const LEVELS = [0, 95, 135, 175, 215, 255];

function rgb(color) {
  return [1, 3, 5].map((at) => parseInt(color.slice(at, at + 2), 16));
}

function hex(color) {
  return typeof color === "string" && /^#[0-9a-fA-F]{6}$/.test(color);
}

// 24, 256 or 0: NO_COLOR, then THERMITE_REMOTE_COLOR, the multiplexer's
// capabilities, COLORTERM and terminals known to show 24 bits.
// run(file, args) answers for tmux (tests pass their own).
function colorDepth(env, run) {
  const e = env || process.env;
  if (e.NO_COLOR) return 0;
  const forced = String(e[FORCE] || "").toLowerCase();
  if (forced === "truecolor" || forced === "24bit") return 24;
  if (forced === "256") return 256;
  if (forced === "none") return 0;
  const term = String(e.TERM || "").toLowerCase();
  if (term === "dumb") return 0;
  if (e.TMUX) {
    // The outer terminal's variables reach the panes, but tmux decides.
    try {
      const features = (run || tmux)("tmux", ["display-message", "-p", "#{client_termfeatures}"]);
      return /(^|,)RGB(,|$)/.test(String(features).trim()) ? 24 : 256;
    } catch (error) {
      return 256;
    }
  }
  if (e.STY || term.indexOf("screen") === 0) return 256;
  if (/^(truecolor|24bit)$/i.test(e.COLORTERM || "")) return 24;
  if (DIRECT_TERMS.test(term)) return 24;
  if (DIRECT_PROGRAMS.includes(e.TERM_PROGRAM) || e.WT_SESSION || e.KONSOLE_VERSION || e.KITTY_WINDOW_ID
    || e.ALACRITTY_WINDOW_ID || e.WEZTERM_PANE || Number(e.VTE_VERSION) >= 3600) return 24;
  return 256;
}

function tmux(file, args) {
  return childProcess.execFileSync(file, args, { encoding: "utf8", timeout: 500, stdio: ["ignore", "pipe", "ignore"] });
}

// The nearest of xterm's 6x6x6 cube and gray ramp (16-255), never one of
// the 16 the terminal's theme sets; as oh_brand.xterm256.
function xterm256(color) {
  const [r, g, b] = rgb(color);
  const near = (value, count, level) => {
    let best = 0;
    for (let index = 1; index < count; index += 1) {
      if (Math.abs(level(index) - value) < Math.abs(level(best) - value)) best = index;
    }
    return best;
  };
  const cube = [r, g, b].map((value) => near(value, 6, (index) => LEVELS[index]));
  const gray = near((r + g + b) / 3, 24, (index) => 8 + 10 * index);
  const distance = (other) => other.reduce((sum, value, index) => sum + (value - [r, g, b][index]) ** 2, 0);
  if (distance([8 + 10 * gray, 8 + 10 * gray, 8 + 10 * gray]) < distance(cube.map((index) => LEVELS[index]))) {
    return 232 + gray;
  }
  return 16 + 36 * cube[0] + 6 * cube[1] + cube[2];
}

function luminance(color) {
  const [r, g, b] = rgb(color).map((value) => {
    const c = value / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function color(value, depth, background) {
  if (!hex(value)) return null;
  const lead = background ? "48" : "38";
  return depth === 24 ? lead + ";2;" + rgb(value).join(";") : lead + ";5;" + xterm256(value);
}

// A style ({fg, bg, bold, underline}) as one SGR sequence, from a reset,
// so that each one stands alone. Without color a ground that stands out
// from the base's (the focus, a lit key) is reverse video.
function sgr(style, depth, base) {
  const codes = ["0"];
  if (style.bold) codes.push("1");
  if (style.underline) codes.push("4");
  if (depth) {
    [color(style.fg, depth, false), color(style.bg, depth, true)].forEach((code) => {
      if (code) codes.push(code);
    });
  } else if (hex(style.bg) && base && hex(base.bg) && contrast(style.bg, base.bg) >= 3) {
    codes.push("7");
  }
  return "\x1b[" + codes.join(";") + "m";
}

const MOUSE = ["?1000", "?1002", "?1006"];

class Painter {
  // write(text) sends to the terminal; depth is colorDepth()'s.
  constructor(write, depth) {
    this.write = write;
    this.depth = depth;
    this.front = null;
    this.width = 0;
    this.height = 0;
  }

  // The alternate screen, no cursor, no wrap at the right edge (a write
  // to the last cell would scroll), the mouse (buttons, drags, SGR).
  enter(title) {
    this.write("\x1b[?1049h\x1b[?25l\x1b[?7l" + MOUSE.map((mode) => "\x1b[" + mode + "h").join("")
      + (title ? "\x1b]0;" + title + "\x07" : ""));
  }

  leave() {
    this.write("\x1b[0m" + MOUSE.slice().reverse().map((mode) => "\x1b[" + mode + "l").join("")
      + "\x1b[?7h\x1b[?25h\x1b[?1049l");
    this.front = null;
  }

  // Forgets what the terminal shows: the next paint draws every cell.
  reset() {
    this.front = null;
  }

  // Draws rows (a grid's: {ch, style} cells) of width by height, codeOf
  // giving a style's SGR; returns the bytes written.
  paint(rows, width, height, codeOf) {
    let out = "";
    if (!this.front || this.width !== width || this.height !== height) {
      this.front = [];
      for (let y = 0; y < height; y += 1) this.front.push(new Array(width).fill(null));
      this.width = width;
      this.height = height;
      out += "\x1b[0m\x1b[2J";
    }
    let pen = null;
    let at = null;
    for (let y = 0; y < height; y += 1) {
      const row = rows[y] || [];
      const shown = this.front[y];
      for (let x = 0; x < width; x += 1) {
        const cell = row[x] || { ch: " ", style: "base" };
        const ch = !this.depth && cell.mono ? cell.mono : cell.ch;
        const code = codeOf(cell.style);
        const old = shown[x];
        if (old && old.ch === ch && old.code === code) continue;
        if (!at || at.y !== y || at.x !== x) out += "\x1b[" + (y + 1) + ";" + (x + 1) + "H";
        if (pen !== code) out += code;
        pen = code;
        out += ch;
        at = { x: x + 1, y: y };
        shown[x] = { ch: ch, code: code };
      }
    }
    // A terminal that knows synchronized output shows the frame at once.
    if (out) this.write("\x1b[?2026h" + out + "\x1b[?2026l");
    return out.length;
  }

  // What the terminal shows on row y, as text (for the tests).
  line(y) {
    return this.front && this.front[y] ? this.front[y].map((cell) => (cell ? cell.ch : " ")).join("") : "";
  }
}

module.exports = { FORCE, Painter, colorDepth, xterm256, sgr, contrast };
