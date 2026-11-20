/*
 * What the terminal remote shows, drawn from a RemoteSession (on the
 * Pairing screen a PairingBackend; a room's speakers, in Listen, from its
 * RemoteListen, drawn by listen.js) into a Grid: the header, the rooms,
 * the remote of the device the buttons control or the speaker listened
 * to, the info panel, the log, the status line, the shortcuts and any
 * sheet over them. What has failed is the session's to say (view() and
 * connected()): while the house is not heard every light is the stale one,
 * which claims nothing, and a source chosen and never read back is marked
 * on its bubble.
 * render() only reads; it returns the frame with every place a click or the
 * focus can reach, and the scroll offsets it settled on, for controller.js
 * to keep.
 */
"use strict";

const { Grid, LOCK, box, inset, fit, wrap, length } = require("./grid");
const { METER, LIGHTS, light } = require("./theme");
const Cameras = require("./cameras");
const Day = require("./day");
const Topology = require("./topology");
const Landing = require("./landing");
const Listen = require("./listen");
const Navigation = require("../../web/remote/navigation");

// One column each: a key shows its icon as one of these, or its caption.
const GLYPHS = {
  up: "\u25b2", down: "\u25bc", left: "\u25c0", right: "\u25b6", plus: "+", minus: "\u2212",
  play: "\u25b6", pause: "\u2016", "play-pause": "\u25b6\u2016", stop: "\u25a0", rewind: "\u25c0\u25c0",
  forward: "\u25b6\u25b6", previous: "|\u25c0", next: "\u25b6|"
};
// A list's marks, the chosen row's and the others': the lights' on and off (styles.js LIGHTS).
const LIT = LIGHTS.ok.glyph;
const UNLIT = LIGHTS.off.glyph;
const ARROW = { left: "\u2039", right: "\u203a" };
// A block's left eighths, one to eight.
const EIGHTHS = ["\u258f", "\u258e", "\u258d", "\u258c", "\u258b", "\u258a", "\u2589", "\u2588"];

// The terminal's own keys, after the page's (RemoteSession.SHORTCUTS).
const TUI_SHORTCUTS = [
  [["Tab", "Shift-Tab"], "Next/previous pane"],
  [["n", "N"], "Next/previous button of the remote"],
  [["Space"], "Press that button"],
  [["PgUp", "PgDn"], "Scroll the pane"],
  [["r"], "Rooms"],
  [["i"], "Info"],
  [["d"], "Signal & audio; again, all of it or less"],
  [["h"], "Rooms through the day"],
  [["v"], "Volume slider"],
  [["t"], "Theme"],
  [["a"], "Motion"],
  [["L"], "Log pane"],
  [["F1"], "Home"],
  [["F2"], "Remotes"],
  [["F3"], "Security"],
  [["F4"], "Cameras"],
  [["F5"], "Site"],
  [["F6", "Ctrl-P"], "Pair devices (Remotes)"],
  [["F7"], "Room settings"],
  [["F8"], "Users"],
  [["F9"], "Account"],
  [["F10"], "System"],
  [["?"], "Help"],
  [["q"], "Quit"]
];
const PAIR_SHORTCUTS = [
  [["\u2191", "\u2193"], "Device"],
  [["Enter"], "Pair, send"],
  [["0-9", "A-F"], "Code"],
  [["Backspace"], "Delete"],
  [["Tab"], "Pane"],
  [["L"], "Log pane"],
  [["Esc", "F2"], "Remotes"],
  [["F1"], "Home"],
  [["?"], "Help"],
  [["q"], "Quit"]
];
const MOUSE_HELP = "Click a room, a bubble, a key or a button, or Watch or Listen above the remote; hold a key that repeats. "
  + "The wheel scrolls a panel and the bubbles, and moves a volume slider.";
// Panels side by side from these widths.
const WIDE = 112;
const MEDIUM = 80;
const ROOMS_WIDTH = 30;
const LOG_FROM = 30;

function signalInline(width, height, logVisible = true) {
  const h = Math.max(0, height - 3);
  const log = logVisible && h >= LOG_FROM ? Math.min(10, h - 24) : 0;
  return width >= 140 && h - log >= 24;
}

function render(ctx, width, height) {
  const frame = {
    grid: new Grid(width, height, "base"),
    regions: [],
    scroll: Object.assign({}, ctx.ui.scroll),
    bubbles: Object.assign({}, ctx.ui.bubbles),
    // The focus order: the panels' controls, the header's, or a sheet's alone.
    order: [],
    sheetOrder: [],
    focusables: [],
    layout: {}
  };
  const g = frame.grid;
  const main = { x: 0, y: 1, w: width, h: Math.max(0, height - 3) };
  if (ctx.ui.screen === "pairing") pairingScreen(frame, ctx, main);
  else if (ctx.ui.screen === "remote") remoteScreen(frame, ctx, main);
  else pageScreen(frame, ctx, main);
  header(frame, ctx, width);
  if (ctx.ui.overlay) sheet(frame, ctx, width, height);
  const seen = {};
  (ctx.ui.overlay ? frame.sheetOrder : frame.order.concat(g.order)).forEach((hit) => {
    if (seen[hit.key]) return;
    seen[hit.key] = true;
    frame.focusables.push(hit);
  });
  statusLine(frame, ctx, width, height - 2);
  shortcutBar(frame, ctx, width, height - 1);
  return frame;
}

// ---- frames ---------------------------------------------------------------

function focused(ctx, key) {
  return Boolean(ctx.ui.focusVisible && key && ctx.ui.focus === key);
}

// A btop box: a rounded frame, its title in the top edge, hints in the
// bottom edge, and its content scrolled to keep the focus in sight.
function panel(frame, ctx, rect, options) {
  const g = frame.grid;
  const { x, y, w, h } = rect;
  if (w < 6 || h < 3) return;
  const inner = new Grid(w - 2, null, "base");
  options.content(inner, w - 2, h - 2);
  const active = ctx.ui.focusVisible && (!ctx.ui.overlay || options.solid) && ctx.ui.pane === options.key;
  // Rows out of sight: how to reach them (options.hidden) goes first in the bottom edge.
  const footer = options.hidden && inner.height > h - 2 ? options.hidden.concat(options.footer || []) : options.footer;
  const border = "border." + options.color + (active ? "+bold" : "");
  if (active) frame.activePane = options.key;
  if (options.solid) {
    g.fill(x, y, w, h, "base");
    g.hit(x, y, w, h, { kind: "none" }, false);
  }
  box(g, rect, {
    border: border, title: options.title && (active ? "\u25b8 " : "") + options.title,
    titleStyle: "title." + options.color + (active ? "+bold" : ""), titleHit: options.titleHit,
    right: options.right, footer: footer
  });
  // Keep the position of every focus stop, including rows below the viewport,
  // so keyboard navigation can reveal the next control in this pane.
  const bounds = new Map();
  inner.hits.forEach((entry) => {
    const key = entry.hit.key;
    if (!key) return;
    const before = bounds.get(key);
    bounds.set(key, before ? {
      left: Math.min(before.left, entry.x), right: Math.max(before.right, entry.x + entry.w),
      top: Math.min(before.top, entry.y), bottom: Math.max(before.bottom, entry.y + entry.h)
    } : { left: entry.x, right: entry.x + entry.w, top: entry.y, bottom: entry.y + entry.h });
  });
  const order = options.solid ? frame.sheetOrder : frame.order;
  inner.order.forEach((hit) => order.push(Object.assign({}, hit, { pane: options.key, bounds: bounds.get(hit.key) })));
  const visible = h - 2;
  const key = options.key;
  // A bottom panel (the log) keeps its distance from the end: 0 follows what is added.
  const kept = frame.scroll[key] || 0;
  let offset = options.bottom ? inner.height - visible - kept : kept;
  const roomSelection = options.key === "rooms" && ctx.ui.revealRoom && !ctx.ui.overlay;
  const focus = roomSelection ? inner.hits.filter((entry) => entry.hit.room === ctx.session.current().id)
    : ctx.ui.revealFocus === false ? [] : inner.hits.filter((entry) => focused(ctx, entry.hit.key));
  if (focus.length) {
    const top = Math.min(...focus.map((entry) => entry.y));
    const bottom = Math.max(...focus.map((entry) => entry.y + entry.h));
    if (top < offset) offset = top;
    else if (bottom > offset + visible) offset = bottom - visible;
  }
  offset = Math.max(0, Math.min(offset, inner.height - visible));
  frame.scroll[key] = options.bottom ? Math.max(0, inner.height - visible - offset) : offset;
  g.blit(inner, offset, x + 1, y + 1, w - 2, visible);
  const overflow = inner.height - visible;
  if (overflow > 0) {
    const size = Math.max(1, Math.round((visible * visible) / inner.height));
    const top = Math.round((offset / overflow) * (visible - size));
    for (let row = 0; row < size; row += 1) g.put(x + w - 1, y + 1 + top + row, "\u2503", "thumb");
  }
  frame.regions.push({ key: key, x: x + 1, y: y + 1, w: w - 2, h: visible, overflow: Math.max(0, overflow), fromEnd: Boolean(options.bottom) });
}

// A section's name and a rule to the panel's edge.
function heading(g, y, text, style) {
  const at = g.put(1, y, text, style || "section");
  if (g.width - at > 3) g.put(at + 1, y, "\u2500".repeat(g.width - at - 2), "rule");
  return y + 1;
}

// Words wrapped to width from (x, y); returns the row after them.
function paragraph(g, x, y, width, text, style) {
  wrap(text, Math.max(4, width)).forEach((line, index) => g.put(x, y + index, line, style));
  return y + wrap(text, Math.max(4, width)).length;
}

// A light's style and its glyph, each state its own (styles.js LIGHTS).
function ledStyle(led) {
  return light(led)[1];
}

function ledChar(led, blink) {
  return light(led, blink)[0];
}

// Whether the house is heard (session.js connected()). While it is not,
// what the session shows was read before.
function heard(ctx) {
  return !ctx.session || ctx.session.connected();
}

// What a light may say: the state it was given while the house is heard.
// While it is not, on, off, switching and a problem are all claims about
// now that nothing backs, so every light is the stale one: one glyph, no
// colour, no blink.
function lightOf(ctx, led) {
  return heard(ctx) ? led : "stale";
}

// ---- the header, the status line and the shortcuts -----------------------

function header(frame, ctx, width) {
  const g = frame.grid;
  g.fill(0, 0, width, 1, "header");
  const remotes = ["remote", "pairing"].includes(ctx.ui.screen);
  let at = width >= 100 ? g.put(0, 0, " thermite ", "header.title") : 0;
  const tabs = Navigation.pages.filter(page => page.top && Navigation.allowed(page, ctx.access)
    && !(page.system && remotes && width < 100)).map(page => [page.screen, page.key, page.label]);
  if (width < 48) {
    const label = ctx.ui.screen === "pairing" ? "Remotes / Pair devices"
      : tabs.find(tab => tab[0] === ctx.ui.screen)?.[2] || ({ settings: "Room settings", users: "Users", account: "Account" })[ctx.ui.screen] || "Home";
    at = g.put(1, 0, fit("thermite · " + label, width - 2), "header.title");
  } else tabs.forEach((tab) => {
    const current = ctx.ui.screen === tab[0] || tab[0] === "remote" && remotes;
    const key = "tab:" + tab[0];
    const style = focused(ctx, key) ? "key.focus" : current ? "tab.current" : "tab";
    const label = width >= 76 ? tab[2] : { home: "Home", remote: "AV", security: "Sec", cameras: "Cam", topology: "Site", system: "Sys", settings: "Set", users: "Users", account: "Me" }[tab[0]];
    const size = length(" " + tab[1] + " " + label + " ");
    if (at + size + 1 > width) return;
    at = g.put(at, 0, " ", "header");
    at = g.line(at, 0, [[" " + tab[1] + " ", style + "+bold", null], [label + " ", style, null]]);
    const hit = tab[0] === "remote" && remotes
      ? { kind: "open", sheet: "remotes-menu", key, focus: true, label: "Remotes menu: rooms and Pair devices" }
      : { kind: "screen", screen: tab[0], key, focus: true, label: tab[2] + " page (" + tab[1] + ")" };
    g.hit(at - size, 0, size, 1, hit);
  });
  if (["settings", "users", "account"].includes(ctx.ui.screen)) at = g.put(at + 1, 0, fit("› " + ({ settings: "Room settings", users: "Users", account: "Account" })[ctx.ui.screen], Math.max(0, width - at - 2)), "tab.current");
  // A narrow remote's sheets (its rooms, Info) before Pair devices, which F6 and the Remotes menu open; each whole or not at all.
  const sheets = ctx.ui.screen === "remote" && frame.layout.sheets ? frame.layout.sheets : [];
  const chips = sheets.reduce((sum, entry) => sum + length(entry[1]) + 3, 0);
  if (remotes && width - at - chips >= 23 && Navigation.allowed(Navigation.pages.find(page => page.id === "pairing"), ctx.access)) at = g.put(at + 1, 0, ctx.ui.screen === "pairing" ? " › Pair devices " : " F6 Pair devices ",
    focused(ctx, "remotes:pairing") ? "key.focus" : "tab", { kind: "screen", screen: "pairing", key: "remotes:pairing", focus: true, label: "Remotes / Pair devices (F6)" });
  if (sheets.length) {
    sheets.forEach((entry) => {
      const key = "open:" + entry[0];
      if (at + length(entry[1]) + 3 > width) return;
      at = g.put(at, 0, " ", "header");
      at = g.put(at, 0, " " + entry[1] + " ", focused(ctx, key) ? "key.focus" : "tab",
        { kind: "open", sheet: entry[0], key: key, focus: true, label: entry[2] });
    });
  }
  const current = ctx.session && ctx.ui.ready ? ctx.session.current() : null;
  // The connection's own light: reading until the session can be drawn, then whether the house is heard.
  const connected = ctx.ui.target.mock ? "mock" : !ctx.ui.ready ? "busy" : heard(ctx) ? "ok" : "bad";
  const room = ctx.ui.screen === "remote" && current ? [current.label + "  ", "header.title"] : null;
  const led = remotes ? (connected === "mock" ? [UNLIT, "header.dim"] : [ledChar(connected, ctx.ui.blink), "header+" + ledStyle(connected)]) : null;
  const target = remotes ? [" " + ctx.ui.target.label + " ", "header.dim"] : null;
  const theme = [" \u25d0 ", focused(ctx, "theme") ? "key.focus" : "header.hotkey",
    { kind: "theme", key: "theme", focus: true, label: "Switch to the " + (ctx.ui.theme === "dark" ? "light" : "dark") + " theme (t)" }];
  const clock = [" " + ctx.ui.clock + " ", "header.dim"];
  // The most of these that fits: the room goes first (the panel's title has it), then the clock.
  const choices = [[room, led, target, theme, clock], [led, target, theme, clock], [led, target, theme], [led, theme]];
  for (const choice of choices) {
    const parts = choice.filter(Boolean);
    const x = width - parts.reduce((sum, part) => sum + length(part[0]), 0);
    if (x > at + 1) {
      g.line(x, 0, parts);
      return;
    }
  }
}

function statusLine(frame, ctx, width, y) {
  const g = frame.grid;
  const logVisible = frame.regions.some((region) => region.key === "log");
  const toast = ctx.ui.toast;
  if (toast) {
    const style = "toast." + (toast.tone || "");
    g.fill(0, y, width, 1, style);
    let at = g.put(1, y, toast.title, style + "+bold");
    if (toast.tag) at = g.put(at + 2, y, " " + toast.tag + " ", "toast.tag");
    if (toast.body) g.put(at + 2, y, fit(toast.body, Math.max(0, width - at - 3)), style);
    return;
  }
  if (ctx.ui.error) {
    g.put(1, y, fit(ctx.ui.error, width - 2), "alert");
    return;
  }
  const help = paneHelp(ctx, frame);
  if (help) {
    const label = help[0] + ": " + help.slice(1).map((entry) => entry[0].join(" ") + " " + entry[1]).join(" \u00b7 ");
    g.line(1, y, [["\u25b8 ", "hotkey"], [fit(label, width - 4), "base"]]);
    return;
  }
  const announce = ctx.ui.screen === "pairing" ? ctx.pairing.announce : ctx.ui.announce;
  if (announce && !logVisible) g.put(1, y, fit(announce, width - 2), "dim");
}

// The keys of the pane with the focus, for the status line and the help
// sheet; frame, where given, says whether the remote hides rows.
function paneHelp(ctx, frame) {
  if (listening(ctx)) {
    const help = listenHelp(ctx);
    if (help) return help;
  }
  if (ctx.ui.overlay && ctx.ui.overlay.kind === "topology") return ["Site topology", [["↑↓"], "room"], [["←→"], "preview source"], [["Enter"], "web map"], [["Esc"], "close"]];
  if (ctx.ui.overlay && ctx.ui.overlay.kind === "cameras") return ["Cameras", [["↑↓"], "camera"], [["Enter"], "open web"], [["Esc"], "close"]];
  switch (ctx.ui.pane) {
    case "home": return ["Home", [["↑↓←→"], "choose"], [["Enter"], "open"], [["h"], "rooms through the day"], [["PgUp/PgDn"], "scroll"]];
    case "security": return ["Security · Not connected", [["F1"], "Home"]];
    case "system": return ["System", [["↑↓"], "choose"], [["Enter"], "open"], [["PgUp/PgDn"], "scroll"]];
    case "cameras": return ["Cameras", [["↑↓"], "camera"], [["Enter"], "open web"], [["F1"], "Home"]];
    case "topology": return ["Site", [["↑↓"], "room"], [["←→"], "preview"], [["Enter"], "web map"], [["s/S"], "sort/reverse"]];
    case "settings": case "users": case "account": return ["Account & settings", [["↑↓"], "choose"], [["Enter"], "open"], [["PgUp/PgDn"], "scroll"]];
    case "rooms": return ["Rooms", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "apply source"]];
    case "sheet:rooms": return ["Rooms", [["\u2191\u2193"], "select room"], [["Enter"], "remote"]];
    case "sources": return ["Sources", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "activate"]]
      .concat(ctx.session?.roomLock().canManage ? [[["o"], "lock menu"]] : []);
    case "remote": return remoteHelp(ctx, frame);
    case "info": return ["Info", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "apply source"], [["PgUp/PgDn"], "scroll"]];
    case "signal": return ["Signal & audio", [["↑↓"], "room"], [["←→"], "source"], [["Enter"], "apply source"], [["PgUp/PgDn"], "scroll"], [["d"], signalMore(ctx)]];
    case "sheet:info": return ["Info", [["\u2191\u2193"], "choose"], [["Enter"], "press"], [["PgUp/PgDn"], "scroll"], [["Esc"], "close"]];
    case "log": return ctx.ui.screen === "pairing" ? ["Log", [["\u2191\u2193"], "scroll"], [["PgUp/PgDn"], "scroll"]]
      : ["Log", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "apply source"], [["PgUp/PgDn"], "scroll"]];
    case "pair-list": return ["Pairing", [["\u2191\u2193"], "select device"], [["Enter"], "pair"], [["s/S"], "sort/reverse"]];
    case "pair-card": return ["Pairing", [["\u2191\u2193"], "field"], [["Enter"], "pair/send"]];
    case "sheet:help": return ["Help", [["\u2191\u2193"], "scroll"], [["Esc"], "close"]];
    case "sheet:day": return ["Rooms through the day", [["\u2191\u2193"], "room"], [["PgUp/PgDn"], "scroll"], [["Esc"], "close"]];
    case "sheet:signal": return ["Signal & audio", [["\u2191\u2193"], "scroll"], [["d"], signalMore(ctx)], [["Esc"], "close"]];
    case "sheet:inputs": return ["Inputs", [["\u2191\u2193"], "choose"], [["Enter"], "select"]];
    case "sheet:lock-menu": return ["Room lock", [["\u2191\u2193"], "choose"], [["Enter"], "select"], [["Esc"], "close"]];
    default: return null;
  }
}

// Watch's remote: the arrows are the D-pad's (a focused slider's while it
// has the focus), n and N walk its buttons and Space presses the one
// reached; PgUp and PgDn where it hides rows.
function remoteHelp(ctx, frame) {
  const slider = focused(ctx, "slider");
  const hidden = Boolean(frame) && frame.regions.some((region) => region.key === "remote" && region.overflow > 0);
  return (slider ? ["Volume", [["\u2190\u2192"], "step"], [["Home/End"], "least/most"], [["Enter"], "OK"], [["n/N"], "button"]]
    : ["Remote", [["\u2190\u2191\u2192\u2193"], "D-pad"], [["Enter"], "OK"], [["n/N"], "button"], [["Space"], "press"]]
      .concat(hidden ? [[["PgUp/PgDn"], "scroll"]] : []))
    .concat(ctx.session?.roomLock().canManage ? [[["k"], "lock/unlock room"]] : []);
}

// Listen's panes and sheets.
function listenHelp(ctx) {
  const title = ctx.ui.overlay ? ctx.ui.overlay.title : "";
  switch (ctx.ui.pane) {
    case "rooms": return ["Rooms", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "speaker"], [["Enter"], "listen"]];
    case "speakers": return ["Speakers", [["\u2190\u2192"], "speaker"], [["\u2191\u2193"], "room"], [["1-9"], "speaker"]];
    case "remote": return ["Listen", [["\u2191\u2193"], "choose"], [["\u2190\u2192"], "volume/position"], [["Enter"], "press"],
      [["P"], "play/pause"], [["+\u2212"], "volume"], [["R"], "repeat"], [["r"], "rooms"]].concat(musicKeys(ctx, "search", "group"));
    case "info": return ["Info", [["i"], "settings"], [["\u2191\u2193"], "room"], [["\u2190\u2192"], "speaker"]];
    case "sheet:info": return ["Info", [["\u2191\u2193"], "choose"], [["\u2190\u2192"], "level"], [["Enter"], "press"], [["Esc"], "close"]];
    case "sheet:audio-choice": case "sheet:audio-inputs": return [title || "Choose", [["\u2191\u2193"], "choose"], [["Enter"], "select"], [["Esc"], "close"]];
    case "sheet:music-library": return ["Library", [["\u2191\u2193"], "choose"], [["Enter"], "press/play"], [["/"], "search"],
      [["Backspace"], "back"], [["Esc"], "clear/close"]];
    case "sheet:music-item": return [title || "Play", [["\u2191\u2193"], "choose"], [["Enter"], "play"], [["Esc"], "close"]];
    case "sheet:music-group": return ["Group", [["\u2191\u2193"], "choose"], [["Space"], "tick"], [["\u2190\u2192"], "volume"], [["Esc"], "close"]];
    default: return null;
  }
}

// / and G where the speaker has the library and a group (Music Assistant's).
function musicKeys(ctx, search, group) {
  const view = ctx.listen.view();
  return (view && view.library ? [[["/"], search]] : []).concat(view && view.canGroup ? [[["G"], group]] : []);
}

// Whether Remotes shows its room's speakers.
function listening(ctx) {
  return Boolean(ctx.ui.screen === "remote" && ctx.ui.ready && ctx.listen && ctx.session.current() && ctx.session.mode() === "listen");
}

function shortcutBar(frame, ctx, width, y) {
  const g = frame.grid;
  const log = [["L"], ctx.ui.logVisible === false ? "Show log" : "Hide log"];
  // Pane controls already appear on the status line. Keep the footer quiet
  // and reserve Help/Quit before adding optional shortcuts.
  // Y first where the house has both: the other mode is one key away.
  const both = ["remote", "home"].includes(ctx.ui.screen) && ctx.ui.ready && ctx.session.current() && ctx.session.kinds().length > 1;
  const other = both ? [[["y"], ctx.session.mode() === "listen" ? "Watch" : "Listen"]] : [];
  const list = ctx.ui.overlay ? [[["Esc"], "Close"]]
    : [[["Tab"], "Pane"]].concat(listening(ctx) ? other.concat([[["r"], "Rooms"]], musicKeys(ctx, "Search", "Group").map(([keys, label]) => [keys.map((k) => k.toLowerCase()), label]),
      [[["i"], "Info"], log, [["t"], "Theme"]])
      : ctx.ui.screen === "remote" ? other.concat([[["r"], "Rooms"], [["d"], "Signal"], [["h"], "Day"], log, [["v"], "Volume"], [["t"], "Theme"]])
      : ctx.ui.screen === "pairing" ? [[["Esc"], "Remotes"], log]
        : (ctx.ui.screen === "home" ? other.concat([[["h"], "Day"]]) : [[["F1"], "Home"]]).concat([[["t"], "Theme"]]));
  g.fill(0, y, width, 1, "footer");
  const essentials = ctx.ui.overlay
    ? [[[ctx.ui.overlay.kind === "access-form" ? "Ctrl-C" : "q"], "Quit"]]
    : [[["?"], "Help"], [["q"], "Quit"]];
  const essentialWidth = essentials.reduce((sum, entry) => sum + length(entry[0][0]) + length(entry[1]) + 4, 0);
  const right = Math.max(1, width - essentialWidth);
  let at = 1;
  function draw(entry) {
    const keys = entry[0].join(entry[2] || " ");
    at = g.put(at, y, keys, "footer.key");
    at = g.put(at, y, " " + entry[1] + "   ", "footer");
  }
  for (const entry of list) {
    const size = length(entry[0].join(entry[2] || " ")) + length(entry[1]) + 4;
    if (at + size > right) break;
    draw(entry);
  }
  at = right;
  essentials.forEach(draw);
}

function pageScreen(frame, ctx, rect) {
  const pages = {
    home: ["thermite", Landing.content],
    security: ["Security", Landing.security],
    system: ["System", Landing.system],
    cameras: ["Cameras", Cameras.content],
    topology: ["Site topology", Topology.content],
    settings: ["Room settings", (g, w, ctx) => ctx.access.content(g, w, ctx)],
    users: ["Users", (g, w, ctx) => ctx.access.content(g, w, ctx)],
    account: ["Account", (g, w, ctx) => ctx.access.content(g, w, ctx)]
  };
  const [title, content] = pages[ctx.ui.screen] || pages.home;
  frame.layout = { page: ctx.ui.screen };
  panel(frame, ctx, rect, { key: ctx.ui.screen, color: "info", title, footer: [["F1", "Home"]],
    content: (inner, width, height) => content(inner, width, ctx, height) });
}

// ---- the remote screen ----------------------------------------------------

function remoteScreen(frame, ctx, rect) {
  const session = ctx.session;
  if (!ctx.ui.ready || !session.current()) {
    panel(frame, ctx, rect, {
      key: "remote", color: "remote", title: "remote",
      content: (inner, w) => {
        inner.put(2, 1, ctx.ui.error ? "The remote could not start" : ctx.ui.ready ? "No rooms configured" : "Reading every room\u2026", ctx.ui.error ? "alert" : "dim");
        if (ctx.ui.error) paragraph(inner, 2, 2, w - 4, ctx.ui.error, "base");
      }
    });
    return;
  }
  const logHeight = logRows(rect, ctx);
  const mainHeight = rect.h - logHeight;
  const roomsWidth = rect.w >= MEDIUM ? ROOMS_WIDTH : 0;
  const infoWidth = rect.w >= WIDE ? Math.min(46, Math.floor(rect.w * 0.3)) : 0;
  const remoteWidth = rect.w - roomsWidth - infoWidth;
  const listen = listening(ctx);
  // Listen has no signal path to show.
  const inline = !listen && signalInline(rect.w, rect.h + 3, ctx.ui.logVisible !== false);
  frame.layout = { rooms: Boolean(roomsWidth), info: Boolean(infoWidth), signal: inline, log: Boolean(logHeight), sheets: [] };
  if (!roomsWidth) frame.layout.sheets.push(["rooms", "\u2261 Rooms", "Rooms (r)"]);
  if (!infoWidth) frame.layout.sheets.push(["info", "i Info", "What the buttons control, and the room (i)"]);
  if (roomsWidth) {
    panel(frame, ctx, { x: rect.x, y: rect.y, w: roomsWidth, h: mainHeight }, {
      key: "rooms", color: "rooms", title: "rooms", footer: [["\u2191\u2193", "room"], ["Tab", "pane"]],
      content: (inner, w) => roomsContent(inner, w, ctx)
    });
  }
  const current = session.current();
  const target = session.target();
  const remote = session.remoteOf(target);
  // In Listen the bubbles are the room's speakers, none for one alone.
  const sourceHeight = mainHeight >= 6 && (!listen || ctx.listen.speakers().length > 1) ? 3 : 0;
  if (sourceHeight) panel(frame, ctx, { x: rect.x + roomsWidth, y: rect.y, w: remoteWidth, h: sourceHeight }, listen ? {
    key: "speakers", color: "remote", title: "speakers", right: "1-9 \u00b7 \u2190\u2192",
    content: (inner, w) => Listen.speakers(inner, w, ctx, KIT)
  } : {
    key: "sources", color: "remote", title: "sources", right: "\u2190\u2192 choose \u00b7 Enter",
    content: (inner, w) => bubbleRow(frame, inner, w, ctx, session.controls(), session.state(current.id))
  });
  const area = { x: rect.x + roomsWidth, y: rect.y + sourceHeight, w: remoteWidth, h: mainHeight - sourceHeight };
  const modes = modeSwitch(ctx, area, current.label);
  const right = listen || !remote || remote.note ? "" : target.label + " \u00b7 " + remote.label;
  panel(frame, ctx, area, {
    key: "remote", color: "remote", title: modes ? modes.title : current.label,
    right: !modes || area.x + area.w - 6 - length(right) > modes.end + 1 ? right : "",
    footer: listen ? [["1-9", "speaker"]].concat(musicKeys(ctx, "search", "group").map(([keys, label]) => [keys[0].toLowerCase(), label]))
      : [["0-9", "source"], ["Enter", "OK"], ["n/N", "button"], ["Space", "press"], ["v", "volume"]],
    hidden: [["PgUp/Dn", "scroll"]],
    content: (inner, w, h) => listen ? Listen.face(inner, w, ctx, KIT) : remoteContent(frame, inner, w, h, ctx)
  });
  if (modes) modes.draw(frame.grid);
  if (infoWidth) {
    // Above the signal the info is as tall as what it says, up to two fifths of the column.
    let infoHeight = mainHeight;
    if (inline) {
      const probe = new Grid(infoWidth - 2, null, "base");
      infoContent(probe, infoWidth - 2, ctx, true);
      infoHeight = Math.max(3, Math.min(probe.height + 2, Math.floor(mainHeight * 0.4)));
    }
    panel(frame, ctx, { x: rect.x + roomsWidth + remoteWidth, y: rect.y, w: infoWidth, h: infoHeight }, {
      key: "info", color: "info", title: "info",
      content: (inner, w) => infoContent(inner, w, ctx, inline)
    });
    if (inline) panel(frame, ctx, { x: rect.x + roomsWidth + remoteWidth, y: rect.y + infoHeight, w: infoWidth, h: mainHeight - infoHeight }, {
      key: "signal", color: "info", title: "signal & audio", right: "d", footer: [["PgUp/Dn", "scroll"]],
      content: (inner, w) => signalContent(inner, w, ctx)
    });
  }
  logPanel(frame, ctx, rect, logHeight);
}

// Watch | Listen in the remote's top edge, after its title, where the house
// has both: a click chooses one, Y the other. Null where it has one, or
// the edge has no room for it beside four columns of the title.
function modeSwitch(ctx, rect, label) {
  if (ctx.session.kinds().length < 2) return null;
  const mode = ctx.session.mode();
  const parts = [["watch", " Watch "], ["listen", " \u266a Listen "]];
  const width = parts.reduce((sum, part) => sum + length(part[1]), 0) + 5;
  // An active pane's title starts "▸ ": two columns kept for it, active or not.
  const room = rect.w - 8 - width - 1 - 2;
  if (room < 4) return null;
  const active = ctx.ui.focusVisible && !ctx.ui.overlay && ctx.ui.pane === "remote";
  const title = fit(label, Math.min(room, length(label))).trimEnd();
  const at = rect.x + 1 + length(title) + 4 + 1 + (active ? 2 : 0);
  const border = "border.remote" + (active ? "+bold" : "");
  return {
    title, end: at + width,
    draw: (g) => inset(g, at, rect.y, border, parts.flatMap(([id, text], index) => (index ? [["\u2502", border]] : []).concat([[text,
      id === mode ? "tab.current" : "dim", { kind: "mode", mode: id, key: "mode:" + id,
        label: (id === "watch" ? "Watch: the rooms' sources and remotes" : "Listen: the rooms' speakers") + " (Y)" }]])))
  };
}

// The log under the panels, in a window tall enough for both.
function logRows(rect, ctx) {
  if (ctx.ui.logVisible === false) return 0;
  return rect.h >= LOG_FROM ? Math.min(10, rect.h - 24) : 0;
}

function logPanel(frame, ctx, rect, rows) {
  if (!rows) return;
  panel(frame, ctx, { x: rect.x, y: rect.y + rect.h - rows, w: rect.w, h: rows }, {
    key: "log", color: "log", title: "log", bottom: true,
    content: (inner, w) => logContent(inner, w, ctx)
  });
}

function roomsContent(g, w, ctx) {
  const session = ctx.session;
  const current = session.current();
  let y = 0;
  const listed = session.listed().map((room) => [room, session.view(room, session.state(room.id))]);
  // The room, its padlock, its source and its light in columns: the source
  // as wide as the widest, up to 12 and two fifths of the row, against the
  // light at the right edge, so the padlocks, sources and lights line up and
  // names keep the most of it.
  const sourceWidth = Math.min(12, Math.max(4, Math.floor((w - 7) * 2 / 5)), Math.max(1, ...listed.map(([, shown]) => length(shown.short))));
  const ledAt = w - 2, lockAt = ledAt - 1 - sourceWidth - 2, labelWidth = Math.max(4, lockAt - 2);
  listed.forEach(([room, shown]) => {
    const lock = session.roomLocked(room.id);
    const key = "room:" + room.id;
    const style = focused(ctx, key) ? "row.focus" : room === current ? "row.current" : "row";
    g.fill(0, y, w, 1, style);
    if (style === "row.current") g.put(0, y, "\u258e", "row.mark");
    g.put(1, y, fit(room.label, labelWidth), style);
    if (lock.locked) g.put(lockAt, y, LOCK, style);
    const short = fit(shown.short, sourceWidth).trimEnd();
    g.put(ledAt - 1 - length(short), y, short, style === "row" ? "dim" : style);
    const led = lightOf(ctx, shown.led);
    g.put(ledAt, y, ledChar(led, ctx.ui.blink), style === "row.focus" ? style : style + "+" + ledStyle(led));
    g.hit(0, y, w, 1, { kind: "room", room: room.id, key: key, focus: true,
      label: room.label + ", " + shown.spoken + (lock.locked ? ". " + lock.text : "") });
    y += 1;
  });
  y += 1;
  const armed = session.allOffArmed();
  const text = armed ? "Press again to confirm" : "All off";
  const style = focused(ctx, "all-off") ? "key.focus" : armed ? "danger.armed" : "danger";
  rounded(g, 1, y, text, Math.min(w - 2, length(text) + 4), style,
    { kind: "all-off", key: "all-off", focus: true, label: armed ? "Press again: turn off every room and device" : "All off: every room and every device thermite powers" });
  return y + 3;
}

// ---- the remote -----------------------------------------------------------

function keyLabel(ctx, id) {
  if (id === "channel_up") return "CH+";
  if (id === "channel_down") return "CH\u2212";
  const spec = ctx.session.spec(id);
  return (spec.icon && GLYPHS[spec.icon]) || spec.text || spec.label;
}

function keyStyle(ctx, id, options) {
  // A key that is refused is dim; the focus still shows where n and N stand.
  if (ctx.session.controlBlock()) return focused(ctx, "key:" + id) ? "key.focus+dim" : "dim";
  if (ctx.session.canPress && !ctx.session.canPress(id)) return focused(ctx, "key:" + id) ? "key.focus+dim" : "dim";
  const key = "key:" + id;
  if (ctx.ui.pressed[key]) return "key.pressed";
  if (focused(ctx, key)) return "key.focus";
  if ((options && options.lit) || (id === "mute" && ctx.session.audioView().muted === true)) return "key.lit";
  if (options && options.idle) return "key.idle";
  if (options && options.ok) return "key.ok";
  return "key";
}

function keyHit(ctx, id) {
  const spec = ctx.session.spec(id);
  const target = ctx.session.target();
  const hints = ctx.hints[id];
  const available = !ctx.session.canPress || ctx.session.canPress(id, target);
  return {
    kind: "key", button: id, key: "key:" + id, focus: true, disabled: !available, repeat: available && Boolean(spec.repeat),
    label: spec.label + " \u2192 " + ctx.session.drives(id, target)
      + (!available && target && target.availability ? " · " + target.availability.note : "")
      + (hints ? "  (" + hints.join(", ") + ")" : "")
  };
}

function keyWidth(ctx, id, least) {
  return Math.max(least || 0, length(keyLabel(ctx, id)) + 4);
}

// A complete three-row rounded outline, one Tab stop and a clickable
// rectangle on every row. The middle-row hit stays first for focus/drag.
function rounded(g, x, y, text, width, style, hit) {
  const size = Math.max(4, width);
  const label = fit(text, Math.min(length(text), size - 2));
  const pad = Math.max(0, Math.floor((size - 2 - length(label)) / 2));
  const edge = style.indexOf("focus") >= 0 || style.indexOf("pressed") >= 0 ? "hotkey" : "rule";
  g.put(x, y, "\u256d" + "\u2500".repeat(size - 2) + "\u256e", edge);
  g.put(x, y + 1, "\u2502" + " ".repeat(pad) + label + " ".repeat(size - 2 - pad - length(label)) + "\u2502", style, hit);
  g.put(x, y + 1, "\u2502", edge);
  g.put(x + size - 1, y + 1, "\u2502", edge);
  g.put(x, y + 2, "\u2570" + "\u2500".repeat(size - 2) + "\u256f", edge);
  g.hit(x, y, size, 1, hit, false);
  g.hit(x, y + 2, size, 1, hit, false);
  return x + size;
}

// A key on one row, for a remote too tall for its panel: the caption
// centered in width, its style the only edge it has.
function flat(g, x, y, text, width, style, hit) {
  const size = Math.max(3, width);
  const label = fit(text, Math.min(length(text), size));
  const pad = Math.floor((size - length(label)) / 2);
  g.put(x, y, " ".repeat(pad) + label + " ".repeat(size - pad - length(label)), style, hit);
  return x + size;
}

// The rows a remote's key takes, and from one row of keys to the next: a
// rounded outline three rows high and a row apart, or, where the panel is
// too short for the remote (ctx.flat, remoteContent), one row and none.
function keyHeight(ctx) {
  return ctx.flat ? 1 : 3;
}

function keyPitch(ctx) {
  return ctx.flat ? 1 : 4;
}

// A key at (x, y), its caption centered in width; returns the column after it.
function key(g, ctx, x, y, id, width, options) {
  const label = keyLabel(ctx, id);
  return (ctx.flat ? flat : rounded)(g, x, y, label, width || length(label) + 4, keyStyle(ctx, id, options), keyHit(ctx, id));
}

// Keys in rows centered in width, wrapped where they would not fit;
// returns the row after them.
function keyRows(g, ctx, y, width, ids, least) {
  if (!ids.length) return y;
  const rows = [[]];
  const available = Math.max(4, width - 2);
  let gap = 2;
  let sizes = ids.map((id) => Math.min(available, keyWidth(ctx, id, least)));
  const total = () => sizes.reduce((sum, size) => sum + size, 0) + (ids.length - 1) * gap;
  if (total() > available) gap = 1;
  if (total() > available) {
    const compact = ids.map((id) => Math.min(available, Math.max(4, length(keyLabel(ctx, id)) + 2)));
    if (compact.reduce((sum, size) => sum + size, 0) + ids.length - 1 <= available) sizes = compact;
    else gap = 2;
  }
  let used = 0;
  ids.forEach((id, index) => {
    const size = sizes[index];
    if (rows[rows.length - 1].length && used + gap + size > available) {
      rows.push([]);
      used = 0;
    }
    used += (rows[rows.length - 1].length ? gap : 0) + size;
    rows[rows.length - 1].push({ id, size });
  });
  rows.forEach((row, index) => {
    const total = row.reduce((sum, entry) => sum + entry.size, 0) + (row.length - 1) * gap;
    let x = Math.max(0, Math.floor((width - total) / 2));
    row.forEach((entry) => {
      x = key(g, ctx, x, y + index * keyPitch(ctx), entry.id, entry.size) + gap;
    });
  });
  return y + (rows.length - 1) * keyPitch(ctx) + keyHeight(ctx);
}

function remoteContent(frame, g, w, height, ctx) {
  const session = ctx.session;
  const current = session.current();
  const list = session.controls();
  const target = session.activeTarget(list);
  const state = session.state(current.id);
  let y = 0;
  // The padlock bubble shows a lock; words only where no bubble can, or
  // while the lock is unknown, which leaves the controls working.
  const notice = session.lockNotice();
  if (notice) y = paragraph(g, 2, y, w - 4, (session.controlBlock() ? "Read-only · " : "") + notice, "dim");
  const shown = session.view(current, state);
  if (shown.problem) y = paragraph(g, 2, y, w - 4, "\u26a0 " + shown.problem, "alert");
  // What the room's source plays, one quiet line above its buttons.
  if (shown.playing && w > 8) {
    g.put(2, y, "\u25b6", "hotkey");
    g.put(4, y, fit(shown.playing, w - 6).trimEnd(), "dim");
    y += 1;
  }
  // A room that is off shows no remote until a device bubble is tapped.
  if (session.remoteHidden(list)) return paragraph(g, 2, y + 1, w - 4, "Off \u00b7 choose a source, or a device for its buttons", "dim");
  const remote = session.remoteOf(target);
  const groups = remote && remote.keyGroups ? remote.keyGroups : null;
  const dense = w >= 104 && remote && remote.wide && groups && groups.some((group) => !group.primary);
  const width = dense ? 100 : Math.min(54, w);
  const primaryWidth = dense ? 48 : width;
  // The remote's face, its keys three rows high or (flat) one row each.
  const face = (drawn) => {
    const body = new Grid(width, null, "base"), primary = new Grid(primaryWidth, null, "base");
    let row = 0;
    if (!remote) row = note(primary, primaryWidth, row, "Nothing to control", "Choose a source above");
    else if (remote.note) row = note(primary, primaryWidth, row, target.label, remote.note.replace("{device}", target.label));
    else row = layout(primary, drawn, primaryWidth, row, remote, groups ? groups.filter((group) => group.primary) : null);
    lower(primary, drawn, primaryWidth, row + 1, remote, target);
    copyGrid(body, primary, 0, 0);
    if (dense) {
      const secondary = new Grid(48, null, "base");
      groupRows(secondary, drawn, 48, 0, groups.filter((group) => !group.primary));
      copyGrid(body, secondary, 52, 0);
    } else if (groups) {
      groupRows(body, drawn, width, primary.height + 1, groups.filter((group) => !group.primary));
    }
    return body;
  };
  // A remote taller than its panel has its keys on one row each; what the
  // panel still hides, PgUp and PgDn, the wheel and the focus (n, N) scroll to.
  let body = face(ctx);
  const short = y + body.height > height;
  if (short) body = face(Object.assign({}, ctx, { flat: true }));
  const left = Math.floor((w - width) / 2);
  copyGrid(g, body, left, y);
  frame.remoteBody = { left, width, primaryWidth, columns: dense ? 2 : 1, height: body.height, flat: short };
  return y + body.height;
}

function copyGrid(into, from, x, y) {
  into.blit(from, 0, x, y, from.width, from.height);
  into.order.push.apply(into.order, from.order);
}

// Each group's heading and its keys in columns; returns the row after them
// and a blank one.
function groupRows(g, ctx, w, y, groups) {
  let row = y;
  (groups || []).forEach((group) => {
    if (!group.keys.length) return;
    if (group.label && group.label !== "Apps") row = heading(g, row, group.label);
    const cell = Math.min(Math.max(4, w - 2), Math.max(...group.keys.map((id) => keyWidth(ctx, id, 7))));
    const gap = 2;
    const columns = Math.min(group.columns || group.keys.length, Math.max(1, Math.floor((w - 2 + gap) / (cell + gap))));
    for (let index = 0; index < group.keys.length; index += columns) {
      const ids = group.keys.slice(index, index + columns);
      let x = Math.max(0, Math.floor((w - (ids.length * cell + (ids.length - 1) * gap)) / 2));
      ids.forEach((id) => { x = key(g, ctx, x, row, id, cell) + gap; });
      row += keyPitch(ctx);
    }
  });
  // One-row keys have no row between them: the blank one comes after the last.
  return ctx.flat && row > y ? row + 1 : row;
}

// The bubbles: Off and the room's sources, then set apart its display and
// audio device; a row too wide scrolls, keeping what the room shows in sight.
function bubbleRow(frame, g, w, ctx, list, state) {
  const session = ctx.session;
  const items = session.bubbles(list);
  if (!items.length) return 0;
  const parts = [];
  // The choice that was never read back, marked on its own bubble (the alert has its label).
  const unread = session.view(session.current(), state).unconfirmedId;
  items.forEach((item, index) => {
    if (index && ctx.Session.DEVICE_ROLE[item.role] && !ctx.Session.DEVICE_ROLE[items[index - 1].role]) {
      parts.push({ gap: true, width: 3 });
    }
    const shown = session.bubbleState(item, state);
    const lock = session.bubbleLock(item, shown);
    const unconfirmed = item.choose && unread === item.id;
    const led = lightOf(ctx, unconfirmed ? "unconfirmed" : shown === "current" ? "ok" : shown === "switching" ? "busy" : "off");
    const mark = lock.locked ? LOCK : item.choose ? ledChar(led, ctx.ui.blink) : "";
    const text = " " + (item.digit ? item.digit + " " : "") + (mark ? mark + " " : "") + item.label + " ";
    parts.push({ item: item, shown: shown, lock: lock, led: led, unconfirmed: unconfirmed, text: text, width: length(text) });
  });
  const selectedId = session.selectedBubble(list);
  const total = parts.reduce((sum, part) => sum + part.width, 0) + parts.length - 1;
  const row = new Grid(Math.max(total, 1), 1, "base");
  let x = 0;
  let lit = null;
  let focus = null;
  const starts = [];
  parts.forEach((part) => {
    starts.push(x - 1, x);
    if (part.gap) {
      row.put(x + 1, 0, "\u2502", "faint");
      x += part.width + 1;
      return;
    }
    const item = part.item;
    const key = "bubble:" + item.id;
    // Not heard, a bubble that was switching keeps its place without the colour that says it is switching now.
    let style = part.lock.inert ? "bubble.inert" : part.shown === "current" ? "bubble.current"
      : part.shown === "switching" ? (heard(ctx) ? "bubble.switching" : "bubble.current") : item.id === selectedId ? "bubble.selected" : "bubble";
    const cursor = !ctx.ui.overlay && ctx.ui.pane !== "remote" && ctx.ui.sourceCursor[session.current().id] === item.id;
    if (focused(ctx, key) || cursor) style = "bubble.focus";
    const remote = item.remote ? ctx.layouts.remotes[item.remote] : null;
    const hit = {
      kind: "bubble", item: item, key: key, focus: true, disabled: part.lock.inert,
      label: session.bubbleLabel(item, part.shown) + (part.unconfirmed ? ", not confirmed" : "") + (remote ? " \u00b7 " + remote.label : "")
        + (item.digit ? "  (" + item.digit + ")" : "")
    };
    let at = x;
    if (item.digit) {
      at = row.put(at, 0, " " + item.digit, style === "bubble" ? "bubble.digit" : style, hit);
      row.put(at, 0, part.text.slice(1 + item.digit.length), style, hit);
    } else {
      row.put(at, 0, part.text, style, hit);
    }
    if (item.choose && style !== "bubble.focus" && !part.lock.locked) {
      row.put(at + 1, 0, ledChar(part.led, ctx.ui.blink), style.split("+")[0] + "+" + ledStyle(part.led));
    }
    if ((focused(ctx, key) || cursor) && ctx.ui.revealFocus !== false) focus = { from: x, to: x + part.width, id: item.id };
    else if (part.shown !== "idle") lit = { from: x, to: x + part.width, id: item.id };
    x += part.width + 1;
  });
  g.order.push.apply(g.order, row.order);
  const window = w - 2;
  const before = frame.bubbles;
  const fresh = before.room !== session.current().id;
  let offset = fresh ? 0 : before.offset || 0;
  const litId = lit ? lit.id : null;
  const reveal = focus || lit;
  const revealing = total > window && reveal && (fresh || before.lit !== litId || before.window !== window || focus);
  if (revealing) {
    if (reveal.from - 1 < offset) offset = reveal.from - 1;
    else if (reveal.to + 2 > offset + window) offset = reveal.to + 2 - window;
  }
  const max = Math.max(0, total - window);
  offset = Math.max(0, Math.min(offset, max));
  // A bubble cut at the left edge reads as another one: the row starts at a
  // bubble or the space before it, the way it was going, except at its end.
  if (offset > 0 && offset < max && starts.indexOf(offset) < 0) {
    const back = starts.filter((start) => start < offset).pop() || 0;
    const ahead = starts.find((start) => start > offset);
    const forward = offset > (fresh ? 0 : before.offset || 0);
    offset = forward && ahead !== undefined && (!revealing || ahead <= reveal.from) ? Math.min(ahead, max) : back;
  }
  frame.bubbles = { room: session.current().id, lit: litId, window: window, offset: offset, overflow: total > window, step: Math.max(8, window - 8) };
  g.hit(0, 0, w, 1, { kind: "bubbles-row" });
  g.blit(slice(row, offset, window), 0, 1, 0, window, 1);
  if (total > window) {
    const more = { left: offset > 0, right: offset < total - window };
    g.put(0, 0, ARROW.left, more.left ? "hotkey" : "faint", more.left ? { kind: "scroll-bubbles", step: -1, key: "scroll-bubbles:-1" } : null);
    g.put(w - 1, 0, ARROW.right, more.right ? "hotkey" : "faint", more.right ? { kind: "scroll-bubbles", step: 1, key: "scroll-bubbles:1" } : null);
  }
  return 1;
}

// Columns from..from+width of a one-row grid, with their hits.
function slice(row, from, width) {
  const out = new Grid(width, 1, "base");
  for (let col = 0; col < width; col += 1) {
    const cell = row.rows[0][from + col];
    if (cell) out.rows[0][col] = cell;
  }
  row.hits.forEach((entry) => {
    const left = Math.max(entry.x - from, 0);
    const right = Math.min(entry.x + entry.w - from, width);
    if (right > left) out.hit(left, 0, right - left, 1, entry.hit);
  });
  return out;
}

function note(g, w, y, title, body) {
  g.put(Math.max(1, Math.floor((w - length(title) - 2) / 2)), y + 1, UNLIT + " " + title, "strong");
  const lines = wrap(body, Math.max(10, w - 8));
  lines.forEach((line, index) => g.put(Math.max(1, Math.floor((w - length(line)) / 2)), y + 2 + index, line, "dim"));
  return y + 3 + lines.length;
}

// The remote of the device the buttons control: its row of keys, the D-pad
// with its corners, playback and its labelled keys.
function layout(g, ctx, w, y, remote, groups) {
  let row = y;
  if (remote.corners) row = cluster(g, ctx, w, row, remote.corners) + 1;
  if (remote.top) row = keyRows(g, ctx, row, w, remote.top) + 1;
  const playback = remote.playback || [];
  // The page puts up to three playback keys in its lower group when a
  // D-pad exists. Longer transport rows stay above the labelled keys.
  if (playback.length && (!remote.corners || playback.length > 3)) row = keyRows(g, ctx, row, w, playback, 5) + 1;
  if (groups) row = groupRows(g, ctx, w, row, groups);
  else if (remote.keys && remote.keys.length) row = keyRows(g, ctx, row, w, remote.keys, 8) + 1;
  return row - 1;
}

// The D-pad on a pad of its own, OK at its heart, and the corner keys.
function cluster(g, ctx, w, y, corners) {
  const step = keyHeight(ctx);
  if (w < 34) {
    let row = y;
    if (corners.slice(0, 2).some(Boolean)) row = keyRows(g, ctx, row, w, corners.slice(0, 2).filter(Boolean), 8) + 1;
    const gap = w >= 16 ? 1 : 0;
    const size = Math.max(4, Math.min(6, Math.floor((w - 2 - gap * 2) / 3)));
    const left = Math.floor((w - size * 3 - gap * 2) / 2);
    const center = left + size + gap;
    key(g, ctx, center, row, "up", size);
    key(g, ctx, left, row + step, "left", size);
    key(g, ctx, center, row + step, "ok", size, { ok: true });
    key(g, ctx, center + size + gap, row + step, "right", size);
    key(g, ctx, center, row + 2 * step, "down", size);
    row += 3 * step;
    if (corners.slice(2).some(Boolean)) row = keyRows(g, ctx, row + 1, w, corners.slice(2).filter(Boolean), 8);
    return row;
  }
  const width = Math.min(w - 2, 40);
  const left = Math.floor((w - width) / 2);
  const middle = left + Math.floor(width / 2);
  const ok = middle - 4;
  const corner = (id, row, right) => {
    if (!id) return;
    const size = keyWidth(ctx, id, 10);
    key(g, ctx, right ? left + width - size : left, row, id, size);
  };
  corner(corners[0], y, false);
  corner(corners[1], y, true);
  key(g, ctx, ok, y, "up", 8);
  key(g, ctx, ok - 9, y + step, "left", 7);
  key(g, ctx, ok, y + step, "ok", 8, { ok: true });
  key(g, ctx, ok + 10, y + step, "right", 7);
  key(g, ctx, ok, y + 2 * step, "down", 8);
  corner(corners[2], y + 2 * step, false);
  corner(corners[3], y + 2 * step, true);
  return y + 3 * step;
}

// Volume as the rocker or the slider, Mute, and the CH rocker, with their captions.
function lower(g, ctx, w, y, remote, target) {
  const session = ctx.session;
  const view = session.audioView();
  const idle = !session.current().audio;
  const width = Math.min(w, 44, Math.max(16, w - 4));
  const left = Math.floor((w - width) / 2);
  // The track and VOL sit in the keys' middle row (a one-row key's only one);
  // Mute's row comes a row after the caption, and the keys' own gap.
  const tall = keyHeight(ctx), mid = (tall - 1) / 2, gap = keyPitch(ctx) - tall;
  if (view.slider && width >= 16) {
    key(g, ctx, left, y, "volume_down", 5, { idle: idle });
    key(g, ctx, left + width - 5, y, "volume_up", 5, { idle: idle });
    const x = left + 6;
    const value = view.disabled ? "\u2013" : view.muted ? "muted" : view.text;
    const compact = w < 30;
    const room = compact ? 0 : Math.max(6, length(value));
    const track = compact ? width - 12 : Math.max(4, width - 13 - room);
    const focus = focused(ctx, "slider");
    meter(g, x, y + mid, track, view, focus);
    g.hit(x, y + mid, track, 1, { kind: "slider", key: "slider", focus: true, max: view.max, min: view.min, step: view.step,
      label: view.label + ": " + view.text + "  (\u2190 \u2192 Home End when focused)" });
    if (!compact) g.put(x + track + 1, y + mid, value, view.muted ? "tone.busy+bold" : focus ? "meter.value.focus" : "meter.value");
  } else {
    const span = Math.min(19, w);
    const x = Math.floor((w - span) / 2);
    key(g, ctx, x, y, "volume_down", 5, { idle: idle });
    if (span >= 15) g.put(Math.floor((w - 3) / 2), y + mid, "VOL", "meter.label");
    key(g, ctx, x + span - 5, y, "volume_up", 5, { idle: idle });
  }
  const caption = fit(view.slider && !view.disabled && w >= 30 ? view.short : view.caption, Math.min(w - 2, length(view.caption)));
  // Where it names the room's speaker, a click listens to it.
  const speaker = view.audio && session.modes().includes("listen") && session.current().speakers.includes(view.audio.id);
  g.put(Math.max(1, Math.floor((w - length(caption)) / 2)), y + tall, caption, view.muted ? "tone.busy" : "dim",
    speaker ? { kind: "mode", mode: "listen", device: view.audio.id, key: "listen-to", label: "Listen to " + view.audio.short + " (Y)" } : null);
  // Match the page's lower group: mute and short transport beneath volume,
  // with channel controls at its side. Keep labelled service keys above it.
  const playback = remote && remote.corners && (remote.playback || []).length <= 3 ? remote.playback || [] : [];
  const channel = Boolean(remote && !remote.note && remote.channel);
  const ids = ["mute"].concat(playback, channel ? ["channel_up", "channel_down"] : []);
  const end = keyRows(g, ctx, y + tall + 1 + gap, w, ids, 5);
  if (channel) {
    const text = (target ? target.short : "") + " channels";
    g.put(Math.max(1, Math.floor((w - length(text)) / 2)), end, fit(text, w - 2), "dim");
  }
  return end + (channel ? 1 : 0);
}

// The volume as a slim track: filled in the meter's gradient up to a cap
// whose right edge stands at the level to an eighth of a cell, the rest a
// thin line; dimmed while muted, dashed while there is no level.
function meter(g, x, y, track, view, focus) {
  const exact = view.disabled ? 0 : Math.max(0, Math.min(1, view.fill / 100)) * track;
  let full = Math.floor(exact);
  let eighths = Math.round((exact - full) * 8);
  if (eighths === 8) {
    full += 1;
    eighths = 0;
  }
  const cap = Math.min(track - 1, full);
  for (let cell = 0; cell < track; cell += 1) {
    let ch = "\u2500";
    let style = "meter.empty";
    if (view.disabled) {
      ch = "\u254c";
      style = "meter.off";
    } else if (cell === cap) {
      ch = full >= track ? EIGHTHS[7] : EIGHTHS[Math.max(1, eighths) - 1];
      style = view.muted ? "meter.cap.muted" : focus ? "meter.cap.focus" : "meter.cap";
    } else if (cell < full) {
      ch = "\u2501";
      style = view.muted ? "meter.muted" : "meter." + Math.round((cell / Math.max(1, track - 1)) * (METER - 1));
    }
    g.put(x + cell, y, ch, style);
  }
}

// ---- the info panel ---------------------------------------------------------

function switchRow(g, ctx, y, on, key, label, hint, hit) {
  const style = focused(ctx, key) ? "key.focus" : on ? "switch.on" : "switch.off";
  rounded(g, 1, y, on ? "on" : "off", 8, style, hit);
  g.put(10, y + 1, label, "base", hit);
  if (hint) g.put(10, y + 2, hint, "dim");
  return y + 3;
}

function button(g, ctx, x, y, text, key, hit, style) {
  return rounded(g, x, y, text, length(text) + 4, focused(ctx, key) ? "key.focus" : style || "key", hit);
}

// A device a row: its name, what it is beside it where that fits and on the
// row below where it does not, and what of the room it is at the right edge.
function deviceRows(g, y, w, devices) {
  if (!devices.length) return paragraph(g, 1, y, w - 2, "No devices in the inventory", "dim");
  devices.forEach((device) => {
    const roles = device.roles.map((role) => " " + role + " ");
    const badges = roles.reduce((sum, role) => sum + length(role) + 1, 0);
    const room = Math.max(8, w - 2 - badges);
    const names = wrap(device.label, room);
    names.forEach((line, index) => g.put(1, y + index, line, "base"));
    let x = w - badges;
    roles.forEach((role) => { x = g.put(x, y, role, "badge") + 1; });
    const end = 1 + length(names[names.length - 1]);
    const beside = Boolean(device.meta) && end + 2 + length(device.meta) <= 1 + (names.length > 1 ? w - 2 : room);
    if (beside) g.put(end + 2, y + names.length - 1, device.meta, "dim");
    y += names.length;
    if (device.meta && !beside) y = paragraph(g, 3, y, w - 4, device.meta, "dim");
  });
  return y;
}

function infoContent(g, w, ctx, compact = false) {
  if (listening(ctx)) return Listen.info(g, w, ctx, KIT);
  const session = ctx.session;
  const shown = session.info();
  if (compact) {
    let y = deviceRows(g, heading(g, 0, "Devices in the room"), w, shown.devices);
    y = heading(g, y + 1, "Controls");
    shown.rows.forEach(([label, value]) => { y = paragraph(g, 1, y, w - 2, label + ": " + value, "dim"); });
    return paragraph(g, 1, y + 1, w - 2, "v · Volume " + (session.volumeMode() === "slider" ? "slider" : "buttons"), "dim");
  }
  g.put(1, 0, "Signal & audio  d", "link", { kind: "open", sheet: "signal", key: "signal-open", focus: true, label: "Signal & audio (d)" });
  let y = heading(g, 2, "What the buttons control");
  const labelWidth = Math.max(...shown.rows.map((entry) => length(entry[0]))) + 2;
  shown.rows.forEach((entry) => {
    g.put(1, y, fit(entry[0], labelWidth), "dim");
    y = paragraph(g, labelWidth + 2, y, w - labelWidth - 3, entry[1], "base");
  });
  const lock = session.roomLock();
  if (lock.canManage) {
    y = heading(g, y + 1, "Room lock");
    y = paragraph(g, 1, y, w - 2, lock.text || "Unlocked. Locking holds the current source or Off until you unlock it.", lock.locked ? "strong" : "dim");
    if (lock.error || lock.reason) y = paragraph(g, 1, y, w - 2, lock.error || lock.reason, lock.error ? "alert" : "dim");
    const key = "room-lock:" + session.current().id;
    const hit = lock.disabled ? null : { kind: "room-lock", key, room: session.current().id, focus: true, label: lock.label + " (K)" };
    button(g, ctx, 1, y, lock.label + " (K)", key, hit, lock.disabled ? "dim" : "key"); y += 4;
  }
  y = heading(g, y + 1, "Volume");
  const slider = session.volumeMode() === "slider";
  y = switchRow(g, ctx, y, slider, "volume-mode", "Volume slider", "Off: the + and \u2212 keys",
    { kind: "volume-mode", key: "volume-mode", focus: true, label: "Volume slider: " + (slider ? "on" : "off") + " (v)" });
  if (shown.controls.length) {
    y = heading(g, y + 1, "Room controls");
    shown.controls.forEach((control) => {
      g.line(1, y, [[control.label, "base"], ["  " + control.device, "dim"]]);
      y += 1;
      let x = 1;
      control.actions.forEach((action) => {
        const key = "control:" + control.id + ":" + action.id;
        if (x + length(action.label) + 4 > w - 1) {
          x = 1;
          y += 4;
        }
        x = button(g, ctx, x, y, action.label, key, {
          kind: "control", control: control.id, action: action.id, key: key, focus: true,
          label: control.label + " " + action.label + (control.connected ? "" : ", not connected")
        }, control.connected ? "key" : "key.off") + 1;
      });
      y += 4;
    });
    y = paragraph(g, 1, y, w - 2, "Not connected yet. Later, turning the room on and off will run them; these buttons stay as a backup.", "dim");
  }
  y = deviceRows(g, heading(g, y + 1, "Devices in the room"), w, shown.devices);
  if (session.demo) {
    y = heading(g, y + 1, "Demo: mock backend");
    const answering = session.isAnswering();
    y = switchRow(g, ctx, y, answering, "simulate-matrix", "HDMI matrix answers", "Off: rooms read back UNDEF",
      { kind: "simulate-matrix", key: "simulate-matrix", focus: true, label: "HDMI matrix answers: " + (answering ? "on" : "off") });
    button(g, ctx, 1, y, "Switch at the matrix", "simulate-external",
      { kind: "simulate-external", key: "simulate-external", focus: true, label: "Switch at the matrix: as Control4 or its front panel would" });
    y += 4;
    y = paragraph(g, 1, y, w - 2, "Source Item " + session.itemStateText(), "dim");
  }
  return y;
}

function logContent(g, w, ctx) {
  const entries = ctx.ui.log.length ? ctx.ui.log : [{ time: ctx.ui.clock, title: "", body: "Live commands, state changes and device problems from every room appear here", tone: "muted" }];
  let y = 0;
  entries.forEach((entry) => {
    let x = 1;
    const segment = (text, style) => {
      String(text || "").split(/\s+/).filter(Boolean).forEach((word) => {
        wrap(word, w - 2).forEach((part) => {
          if (x > 1 && x + length(part) > w - 1) { y += 1; x = 1; }
          x = g.put(x, y, part, style) + 1;
        });
      });
    };
    segment(entry.time, "dim");
    segment(entry.title, "tone." + (entry.tone === "bad" ? "bad" : entry.tone === "muted" ? "muted" : "") + "+bold");
    segment(entry.tag, "toast.tag");
    segment(entry.body, "base");
    y += 1;
  });
}

// ---- sheets -------------------------------------------------------------------

function sheet(frame, ctx, width, height) {
  const overlay = ctx.ui.overlay;
  frame.grid.hit(0, 0, width, height, { kind: "dismiss" });
  const w = Math.min(width - 2, overlay.kind === "day" ? 160 : ["topology", "music-library"].includes(overlay.kind) ? 100 : overlay.kind === "signal" ? 76
    : ["info", "help", "cameras"].includes(overlay.kind) ? 66 : ["lock-menu", "audio-choice", "audio-inputs"].includes(overlay.kind) ? 40
      : overlay.kind === "music-group" ? 60 : 50);
  const content = {
    "access-form": (inner, iw) => ctx.access.formContent(inner, iw, ctx),
    rooms: (inner, iw) => roomsContent(inner, iw, ctx),
    info: (inner, iw) => infoContent(inner, iw, ctx),
    inputs: (inner, iw) => inputsContent(inner, iw, ctx),
    help: (inner, iw) => helpContent(inner, iw, ctx),
    signal: (inner, iw) => signalContent(inner, iw, ctx),
    "remotes-menu": (inner, iw) => {
      inner.put(1, 0, "Remotes", "strong");
      button(inner, ctx, 1, 2, "Rooms & remotes", "menu:remote", { kind: "screen", screen: "remote", key: "menu:remote", focus: true, label: "Rooms and remotes" });
      if (Navigation.allowed(Navigation.pages.find(page => page.id === "pairing"), ctx.access)) button(inner, ctx, 1, 6, "Pair devices · F6", "menu:pairing", { kind: "screen", screen: "pairing", key: "menu:pairing", focus: true, label: "Remotes / Pair devices (F6)" });
      else paragraph(inner, 1, 6, iw - 2, ctx.access.accountsExist() ? "Sign in as an administrator to pair devices." : "Create the first administrator in Security (F3) to pair devices.", "dim");
    },
    "audio-choice": (inner, iw) => Listen.choiceSheet(inner, iw, ctx, KIT),
    "audio-inputs": (inner, iw) => Listen.inputSheet(inner, iw, ctx, KIT),
    "music-library": (inner, iw) => Listen.librarySheet(inner, iw, ctx, KIT),
    "music-item": (inner, iw) => Listen.itemSheet(inner, iw, ctx, KIT),
    "music-group": (inner, iw) => Listen.groupSheet(inner, iw, ctx, KIT),
    // A bubble's lock menu (right-click, or o): what the session offers.
    "lock-menu": (inner, iw) => ctx.session.lockMenu(overlay.item).forEach((entry, index) => {
      const key = "lock-menu:" + index;
      button(inner, ctx, 1, index * 3, entry.label, key, { kind: "lock-menu", entry, key, focus: true, label: entry.label }, entry.disabled ? "key.off" : "key");
    }),
    cameras: (inner, iw) => Cameras.content(inner, iw, ctx),
    topology: (inner, iw) => Topology.content(inner, iw, ctx),
    day: (inner, iw) => Day.content(inner, iw, ctx)
  }[overlay.kind];
  const probe = new Grid(w - 2, null, "base");
  content(probe, w - 2);
  const h = Math.max(3, Math.min(height - 4, probe.height + 2));
  const x = Math.floor((width - w) / 2);
  const y = Math.max(1, Math.floor((height - h) / 2));
  panel(frame, ctx, { x: x, y: y, w: w, h: h }, {
    key: "sheet:" + overlay.kind, color: "sheet", solid: true, footer: [["Esc", "close"]],
    title: overlay.title || { rooms: "Rooms", info: "Info", help: "Keys", signal: "Signal & audio", cameras: "Cameras", topology: "Site topology", "remotes-menu": "Remotes", day: "Rooms through the day" }[overlay.kind],
    content: content
  });
}

// The signal view's groups that are open: the two current ones, until others are chosen.
const SIGNAL_GROUPS = ["video", "audio", "path", "edid", "processing"];
const SIGNAL_OPEN = ["video", "audio"];

function signalGroups(ctx) {
  return Array.isArray(ctx.ui.signalGroups) ? ctx.ui.signalGroups : SIGNAL_OPEN;
}

// What d does in the signal view: opens every group, or all but the two current ones closed.
function signalMore(ctx) {
  return SIGNAL_GROUPS.every((id) => signalGroups(ctx).includes(id)) ? "show less" : "show all";
}

// The room's signal as diagnostics.js outline() arranges it: a group a
// heading, which a click opens and closes (d: all of them, or the two
// current ones), and closed says how many readings it holds; under it each
// device once, with its readings' age, and a reading a row, its value at
// the right edge or on the row below where the two do not fit.
function signalContent(g, w, ctx) {
  const model = ctx.session.diagnostics ? ctx.session.diagnostics() : null;
  if (!model) return paragraph(g, 1, 0, w - 2, "Reading the confirmed signal path\u2026", "dim");
  let y = paragraph(g, 1, 0, w - 2, model.room.label + " \u00b7 " + (model.source ? model.source.label : "Source unknown"), "strong");
  if (model.route && model.route.length) y = paragraph(g, 1, y, w - 2, model.route.map((step) => step.label).join(" → "), "dim");
  if (model.audioRoute && model.audioRoute.length) y = paragraph(g, 1, y, w - 2, "Audio · " + model.audioRoute.map((step) => step.label).join(" → "), "dim");
  if (model.audioEndpoints && model.audioEndpoints.length) y = paragraph(g, 1, y, w - 2,
    "Audio endpoint · " + model.audioEndpoints.map(step => step.label + (step.input ? " · input " + step.input : "")).join(", ") + " · wiring unknown", "dim");
  if (model.routeReady === false) y = paragraph(g, 1, y, w - 2, "Awaiting confirmation of the physical route.", "tone.busy");
  if (model.pending) y = paragraph(g, 1, y, w - 2, "Switching to " + model.pending.label + "; current formats await the confirmed route.", "tone.busy");
  if (model.connected === false) y = paragraph(g, 1, y, w - 2, "Room updates unavailable; format attribution is unconfirmed.", "tone.busy");
  const view = globalThis.RemoteDiagnostics.outline(model, ctx.now);
  if (view.blank) return paragraph(g, 1, y, w - 2, model.reason || "Signal information unavailable", "dim");
  const open = signalGroups(ctx);
  // A row's label, then its value at the right edge after a leader, or on the row below.
  const pair = (x, label, value, style) => {
    const room = w - 1 - x;
    if (length(label) + 2 + length(value) <= room) {
      const gap = room - length(label) - length(value);
      g.line(x, y, [[label, "dim"], [gap > 3 ? " " + "\u00b7".repeat(gap - 2) + " " : " ".repeat(gap), "rule"], [value, style]]);
      y += 1;
      return;
    }
    y = paragraph(g, x, y, room, label, "dim");
    wrap(value, Math.max(4, room - 2)).forEach((line) => { g.put(w - 1 - length(line), y, line, style); y += 1; });
  };
  view.groups.forEach((group) => {
    const shown = open.includes(group.id);
    const summary = shown ? "" : group.summary || (group.count ? String(group.count) : "\u2014");
    const tally = length(summary) + length(group.title) + 6 <= w ? summary : "";
    const at = g.put(1, y, (shown ? "\u25be " : "\u25b8 ") + group.title, "section");
    const end = w - 1 - (tally ? length(tally) + 1 : 0);
    if (end - at > 2) g.put(at + 1, y, "\u2500".repeat(end - at - 1), "rule");
    if (tally) g.put(w - 1 - length(tally), y, tally, group.stale ? "tone.busy" : "dim");
    g.hit(0, y, w, 1, { kind: "signal-group", group: group.id, label: group.title + (shown ? ": close" : ": open") }, false);
    y += 1;
    if (!shown) { if (!tally) y = paragraph(g, 2, y, w - 4, summary, group.stale ? "tone.busy" : "dim"); return; }
    if (group.note) y = paragraph(g, 1, y, w - 2, group.note, "faint");
    group.blocks.forEach((block) => {
      const x = block.device ? 2 : 1;
      if (block.device) {
        const age = block.age ? "  " + block.age : "";
        g.put(1, y, fit(block.device, Math.min(length(block.device), Math.max(4, w - 2 - length(age)))), "strong");
        if (age) g.put(w - 1 - length(block.age), y, block.age, block.stale ? "tone.busy" : "dim");
        y += 1;
      }
      block.rows.forEach((row) => pair(x, row.label, row.value, row.stale ? "tone.busy+bold" : "base"));
      block.quiet.forEach((line) => { y = paragraph(g, x, y, w - 1 - x, line, "faint"); });
    });
  });
  return y;
}

function inputsContent(g, w, ctx) {
  const options = ctx.session.inputOptions();
  options.forEach((entry, y) => {
    const key = "input:" + entry.value;
    const style = focused(ctx, key) ? "row.focus" : entry.inUse ? "row.current" : "row";
    g.fill(0, y, w, 1, style);
    g.put(1, y, entry.inUse ? LIT : UNLIT, style === "row.focus" ? style : style + "+" + (entry.inUse ? "led.ok" : "led.off"));
    g.put(3, y, fit(entry.label, w - 12), style);
    if (entry.inUse) g.put(w - 7, y, "In use", style === "row" ? "dim" : style);
    g.hit(0, y, w, 1, { kind: "input", value: entry.value, key: key, focus: true, label: entry.label + (entry.inUse ? ", in use" : "") });
  });
  if (!options.length) g.put(1, 0, "No inputs to choose", "dim");
}

function helpContent(g, w, ctx) {
  let y = 0;
  const rows = (title, entries) => {
    y = heading(g, y, title);
    const keyWidth = Math.min(18, Math.max(8, Math.floor((w - 5) / 3)));
    entries.forEach((entry) => {
      const keys = wrap(entry[0].join(entry[2] || " "), keyWidth);
      keys.forEach((part, index) => g.put(2, y + index, part, "hotkey"));
      y = Math.max(y + keys.length, paragraph(g, keyWidth + 3, y, w - keyWidth - 5, entry[1], "base"));
    });
    y++;
  };
  const manage = ctx.ui.screen === "remote" && ctx.session?.roomLock().canManage;
  if (listening(ctx)) rows("Listen", ctx.Session.LISTEN_SHORTCUTS.concat(Listen.SHORTCUTS, TUI_SHORTCUTS.filter(entry => ["r", "h", "F6"].includes(entry[0][0])))
    .concat(manage ? [[["K"], "Lock or unlock this room (administrators)"]] : []));
  else if (ctx.ui.screen === "remote") rows("Remote controls", ctx.Session.SHORTCUTS.concat(TUI_SHORTCUTS.filter(entry => ["n", "Space", "r", "i", "d", "h", "v", "F6"].includes(entry[0][0])))
    .concat(manage ? [[["K"], "Lock or unlock this room (administrators)"],
      [["o"], "Lock menu of the selected source or Off: lock on it, or unlock (administrators)"]] : []));
  else if (ctx.ui.screen === "pairing") rows("Pair devices", PAIR_SHORTCUTS.filter(entry => !["Tab", "L", "F1", "?", "q"].includes(entry[0][0])).concat([[["s/S"], "Sort/reverse"]]));
  else {
    const help = paneHelp({ ...ctx, ui: { ...ctx.ui, overlay: null, pane: ctx.ui.screen } });
    if (help) rows(help[0], help.slice(1));
  }
  rows("Navigation", TUI_SHORTCUTS.filter(entry => /^F\d+$/.test(entry[0][0]) && entry[0][0] !== "F6"));
  rows("Terminal", TUI_SHORTCUTS.filter(entry => ["Tab", "PgUp", "t", "a", "L", "?", "q"].includes(entry[0][0])));
  y = heading(g, y + 1, "Mouse");
  return paragraph(g, 2, y, w - 4, MOUSE_HELP + (manage ? " Right-click a source or Off for its lock menu." : ""), "base");
}

// ---- the kit ----------------------------------------------------------------

// A one-line button; returns the column after it.
function chip(g, ctx, x, y, text, hit, style) {
  return g.put(x, y, " " + text + " ", focused(ctx, hit.key) ? "key.focus" : style, hit);
}

// ---- the Pairing screen -----------------------------------------------------

function pairingScreen(frame, ctx, rect) {
  const pairing = ctx.pairing;
  const logHeight = logRows(rect, ctx);
  const height = rect.h - logHeight;
  const count = pairDevices(ctx).length;
  const listHeight = height >= 8 ? Math.max(3, Math.min(count + 5, Math.max(6, Math.floor(height * .42)), height - 5)) : 0;
  frame.layout = { pairList: Boolean(listHeight), pairTable: Boolean(listHeight), log: Boolean(logHeight) };
  if (listHeight) {
    panel(frame, ctx, { x: rect.x, y: rect.y, w: rect.w, h: listHeight }, {
      key: "pair-list", color: "rooms", title: "devices", footer: [["\u2191\u2193", "device"], ["s/S", "sort/reverse"]],
      content: (inner, w) => pairListContent(inner, w, ctx)
    });
  }
  const device = pairing.backend && pairing.selected ? pairing.backend.device(pairing.selected) : null;
  panel(frame, ctx, { x: rect.x, y: rect.y + listHeight, w: rect.w, h: height - listHeight }, {
    key: "pair-card", color: "pair", title: device ? "pair \u00b7 " + device.label : "pair",
    right: device ? device.room : "", footer: [["Esc", "remote"]],
    content: (inner, w) => pairCardContent(inner, w, ctx, !listHeight)
  });
  logPanel(frame, ctx, rect, logHeight);
}

const PAIR_FIELDS = ["room", "device", "model", "status"];
const PAIR_TITLES = { room: "Room", device: "Device", model: "Model", status: "Status" };

function pairDevices(ctx) {
  const backend = ctx.pairing.backend;
  if (!backend) return [];
  const field = PAIR_FIELDS.includes(ctx.pairing.sortField) ? ctx.pairing.sortField : "room";
  const value = device => String(field === "status" ? backend.view(device.id).label : field === "device" ? device.label : device[field] || "");
  return [].concat.apply([], backend.rooms.map(room => room.devices)).sort((a, b) => {
    const compared = value(a).localeCompare(value(b), "en", { numeric: true, sensitivity: "base" });
    return (ctx.pairing.sortDescending ? -compared : compared) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  });
}

function pairColumns(width) {
  if (width < 54) {
    const available = Math.max(2, width - 5), status = Math.max(1, Math.min(16, Math.floor(available * .4)));
    return [{ key: "device", width: available - status }, { key: "status", width: status }];
  }
  if (width < 88) {
    const available = width - 8, room = Math.floor(available * .28), status = Math.min(18, Math.floor(available * .3));
    return [{ key: "room", width: room }, { key: "device", width: available - room - status }, { key: "status", width: status }];
  }
  const available = Math.max(4, width - 11);
  const room = Math.max(1, Math.floor(available * .22)), device = Math.max(1, Math.floor(available * .28));
  const status = Math.max(1, Math.min(18, Math.floor(available * .25)));
  return [{ key: "room", width: room }, { key: "device", width: device },
    { key: "model", width: Math.max(1, available - room - device - status) }, { key: "status", width: status }];
}

function pairListContent(g, w, ctx) {
  const backend = ctx.pairing.backend;
  let y = 0;
  if (ctx.pairing.mock) {
    y = paragraph(g, 1, y, w - 2, "Mock · sample devices; codes 1234 / A1B2C3", "dim");
  }
  if (!backend && ctx.pairing.alert) { g.put(1, y, "Pairing unavailable for this account", "dim"); return; }
  if (!backend || !ctx.pairing.loaded) {
    g.put(1, y, "Reading the devices\u2026", "dim");
    return;
  }
  const columns = pairColumns(w), field = ctx.pairing.sortField || "room";
  let at = 1;
  columns.forEach((column, index) => {
    const active = column.key === field;
    g.put(at, y, fit(PAIR_TITLES[column.key] + (active ? ctx.pairing.sortDescending ? " ↓" : " ↑" : ""), column.width), active ? "hotkey+bold" : "strong");
    g.hit(at, y, column.width, 1, { kind: "pair-sort", field: column.key, key: "pair-sort:" + column.key, label: "Sort pairing devices by " + PAIR_TITLES[column.key] }, false);
    at += column.width;
    if (index < columns.length - 1) { g.put(at, y, " │ ", "rule"); at += 3; }
  });
  y++; g.put(1, y++, "─".repeat(Math.max(0, w - 2)), "rule");
  pairDevices(ctx).forEach((device) => {
      const view = backend.view(device.id);
      const key = "pair-device:" + device.id;
      const style = focused(ctx, key) ? "row.focus" : device.id === ctx.pairing.selected ? "row.current" : "row";
      g.fill(0, y, w, 1, style);
      const values = { room: device.room, device: device.label, model: device.model || "—", status: view.label };
      let at = 1;
      columns.forEach((column, index) => {
        g.put(at, y, fit(values[column.key], column.width), style);
        at += column.width;
        if (index < columns.length - 1) { g.put(at, y, " │ ", style === "row" ? "rule" : style); at += 3; }
      });
      g.hit(0, y, w, 1, { kind: "pair-device", device: device.id, key: key, focus: true, label: device.label + ", " + device.room + ": " + view.label });
      y += 1;
  });
}

function pairCardContent(g, w, ctx, narrow) {
  const pairing = ctx.pairing;
  const backend = pairing.backend;
  let y = 0;
  if (pairing.alert) y = paragraph(g, 1, y, w - 2, "\u26a0 " + pairing.alert, "alert") + 1;
  if (!backend && pairing.alert) return y;
  if (!backend || !pairing.loaded) {
    g.put(1, y, "Reading thermite devices\u2026", "dim");
    return;
  }
  const devices = pairDevices(ctx);
  if (!devices.length) {
    paragraph(g, 1, y, w - 2, "No TVs or streamers to pair.", "dim");
    return;
  }
  const device = backend.device(pairing.selected) || devices[0];
  const view = backend.view(device.id);
  if (narrow) {
    const index = devices.findIndex((candidate) => candidate.id === device.id);
    let x = g.put(1, y, ARROW.left, "hotkey", { kind: "pair-step", step: -1, key: "pair-step:-1" });
    x = g.put(x + 1, y, "Device " + (index + 1) + " of " + devices.length, "dim");
    g.put(x + 1, y, ARROW.right, "hotkey", { kind: "pair-step", step: 1, key: "pair-step:1" });
    y += 2;
    if (pairing.mock) y = paragraph(g, 1, y, w - 2, "Mock codes: 1234 / A1B2C3", "dim") + 1;
  }
  y = paragraph(g, 1, y, w - 2, device.label, "strong");
  y = paragraph(g, 1, y, w - 2, [device.room, device.model].filter(Boolean).join(" · "), "dim") + 1;
  let x;
  g.line(1, y, [[ledChar(view.tone || "off", ctx.ui.blink) + " ", ledStyle(view.tone || "off")], [view.label, "tone." + view.tone + "+bold"]]);
  y += 2;
  if (view.note) y = paragraph(g, 1, y, w - 2, view.note, "tone." + (view.noteTone || "")) + 1;
  const busy = Boolean(view.busy);
  if (view.action) {
    const text = view.busy === "request" ? "Asking\u2026" : view.action === "again" ? "Pair again" : "Pair";
    button(g, ctx, 1, y, text, "pair-request", {
      kind: "pair-request", device: device.id, key: "pair-request", focus: true, label: text + ": the " + device.label + " shows a code"
    }, busy ? "key.off" : view.action === "again" ? "key" : "switch.on");
    y += 4;
  }
  if (view.confirm) {
    y = paragraph(g, 1, y, w - 2, view.confirm, "base") + 1;
    x = button(g, ctx, 1, y, "Pair again", "pair-confirm", {
      kind: "pair-confirm", device: device.id, key: "pair-confirm", focus: true, label: "Pair again: " + view.confirm
    }, busy ? "key.off" : "switch.on");
    if (x + 2 + length("Keep this pairing") + 4 > w - 1) { x = -1; y += 4; }
    button(g, ctx, x + 2, y, "Keep this pairing", "pair-keep", {
      kind: "pair-keep", device: device.id, key: "pair-keep", focus: true, label: "Keep this pairing"
    }, "key");
    y += 4;
  }
  if (view.form) {
    y = paragraph(g, 1, y, w - 2, "Code shown on screen", "base");
    const code = pairing.codes[device.id] || "";
    const cells = [];
    for (let index = 0; index < device.length; index += 1) cells.push(code[index] || "_");
    const field = " " + cells.join(" ") + " ";
    x = g.put(1, y + 1, field, focused(ctx, "code") ? "field.focus" : "field",
      { kind: "code", device: device.id, key: "code", focus: true, label: "Type the code: " + ctx.Pairing.codeRule(device) });
    const ready = !busy && ctx.Pairing.codeOk(device, code);
    const send = view.busy === "code" ? "Sending\u2026" : "Send code";
    if (x + 2 + length(send) + 4 > w - 1) { x = -1; y += 3; }
    button(g, ctx, x + 2, y, send, "send-code", { kind: "send-code", device: device.id, key: "send-code", focus: true, label: send + (ready ? "" : " (type the whole code first)") },
      ready ? "switch.on" : "key.off");
    y += 3;
    y = paragraph(g, 1, y, w - 2, ctx.Pairing.codeRule(device), "dim") + 1;
    button(g, ctx, 1, y, view.busy === "request" ? "Asking\u2026" : "New code", "new-code",
      { kind: "new-code", device: device.id, key: "new-code", focus: true, label: "Ask for a new code" }, busy ? "key.off" : "key");
    y += 4;
    if (view.cancel) {
      const stop = view.busy === "cancel" ? "Cancelling\u2026" : "Keep old pairing";
      button(g, ctx, 1, y, stop, "pair-cancel",
        { kind: "pair-cancel", device: device.id, key: "pair-cancel", focus: true, label: stop }, busy ? "key.off" : "key");
      y += 4;
    }
    if (device.hint) y = paragraph(g, 1, y, w - 2, device.hint, "dim");
  }
}

// What listen.js draws with, so its keys, meters and rows look as the rest.
const KIT = { focused, heading, paragraph, chip, button, rounded, meter, ledChar, ledStyle, lightOf, heard, slice, GLYPHS, LIT, UNLIT, ARROW };

module.exports = { render, GLYPHS, TUI_SHORTCUTS, PAIR_SHORTCUTS, WIDE, MEDIUM, pairDevices, pairColumns, PAIR_FIELDS, signalInline, SIGNAL_GROUPS, SIGNAL_OPEN };
