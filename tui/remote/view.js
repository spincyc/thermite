/*
 * What the terminal remote shows, drawn from a RemoteSession (and on the
 * Pairing screen a PairingBackend) into a Grid: the header, the rooms, the
 * remote of the device the buttons control, the info panel, the log, the
 * status line, the shortcuts and any sheet over them. render() only reads;
 * it returns the frame with every place a click or the focus can reach, and
 * the scroll offsets it settled on, for controller.js to keep.
 */
"use strict";

const { Grid, fit, wrap, length } = require("./grid");
const { METER } = require("./theme");
const Cameras = require("./cameras");
const Topology = require("./topology");
const Landing = require("./landing");

// One column each: a key shows its icon as one of these, or its caption.
const GLYPHS = {
  up: "\u25b2", down: "\u25bc", left: "\u25c0", right: "\u25b6", plus: "+", minus: "\u2212",
  play: "\u25b6", pause: "\u2016", "play-pause": "\u25b6\u2016", stop: "\u25a0", rewind: "\u25c0\u25c0",
  forward: "\u25b6\u25b6", previous: "|\u25c0", next: "\u25b6|"
};
const LIT = "\u25cf";
const UNLIT = "\u25cb";
const ARROW = { left: "\u2039", right: "\u203a" };
// A block's left eighths, one to eight.
const EIGHTHS = ["\u258f", "\u258e", "\u258d", "\u258c", "\u258b", "\u258a", "\u2589", "\u2588"];

// The terminal's own keys, after the page's (RemoteSession.SHORTCUTS).
const TUI_SHORTCUTS = [
  [["Tab", "Shift-Tab"], "Next/previous pane"],
  [["r"], "Rooms"],
  [["i"], "Info"],
  [["d"], "Signal & audio"],
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
const MOUSE_HELP = "Click a room, a bubble, a key or a button; hold a key that repeats. The wheel scrolls a panel "
  + "and the bubbles, and moves the volume slider.";
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

// A label set into a frame's edge at x, as ┤ text ├; returns the column after it.
function inset(g, x, y, border, parts) {
  let at = g.put(x, y, "\u2524 ", border);
  parts.forEach((part) => {
    at = g.put(at, y, part[0], part[1], part[2]);
  });
  return g.put(at, y, " \u251c", border);
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
  const border = "border." + options.color + (active ? "+bold" : "");
  if (active) frame.activePane = options.key;
  if (options.solid) {
    g.fill(x, y, w, h, "base");
    g.hit(x, y, w, h, { kind: "none" }, false);
  }
  g.put(x, y, "\u256d" + "\u2500".repeat(w - 2) + "\u256e", border);
  for (let row = y + 1; row < y + h - 1; row += 1) {
    g.put(x, row, "\u2502", border);
    g.put(x + w - 1, row, "\u2502", border);
  }
  g.put(x, y + h - 1, "\u2570" + "\u2500".repeat(w - 2) + "\u256f", border);
  const room = w - 8;
  let at = x + 1;
  if (options.title && room > 2) {
    const title = (active ? "\u25b8 " : "") + options.title;
    at = inset(g, x + 1, y, border, [[fit(title, Math.min(room, length(title))), "title." + options.color + (active ? "+bold" : ""), options.titleHit]]);
  }
  if (options.right && x + w - 6 - length(options.right) > at + 1) {
    inset(g, x + w - 6 - length(options.right), y, border, [[options.right, "dim"]]);
  }
  if (options.footer && options.footer.length) {
    let foot = x + 1;
    const limit = x + w - 1;
    options.footer.forEach((segment) => {
      const size = length(segment[0]) + length(segment[1]) + 5;
      if (foot + size > limit) return;
      foot = inset(g, foot, y + h - 1, border, [[segment[0], "hotkey"], [" " + segment[1], "dim"]]) + 1;
    });
  }
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

function ledStyle(led) {
  return "led." + (led || "off");
}

function ledChar(led, blink) {
  if (led === "busy") return blink ? UNLIT : LIT;
  return led === "off" || led === "muted" || !led ? UNLIT : LIT;
}

// ---- the header, the status line and the shortcuts -----------------------

function header(frame, ctx, width) {
  const g = frame.grid;
  g.fill(0, 0, width, 1, "header");
  const remotes = ["remote", "pairing"].includes(ctx.ui.screen);
  let at = width >= 100 ? g.put(0, 0, " thermite ", "header.title") : 0;
  const tabs = [["home", "F1", "Home"], ["remote", "F2", "Remotes"], ["security", "F3", "Security"], ["cameras", "F4", "Cameras"], ["topology", "F5", "Site"]];
  if (width < 48) {
    const label = ctx.ui.screen === "pairing" ? "Remotes / Pair devices" : tabs.find(tab => tab[0] === ctx.ui.screen)[2];
    at = g.put(1, 0, fit("thermite · " + label, width - 2), "header.title");
  } else tabs.forEach((tab) => {
    const current = ctx.ui.screen === tab[0] || tab[0] === "remote" && remotes;
    const key = "tab:" + tab[0];
    const style = focused(ctx, key) ? "key.focus" : current ? "tab.current" : "tab";
    const label = width >= 76 ? tab[2] : { home: "Home", remote: "AV", security: "Sec", cameras: "Cam", topology: "Site" }[tab[0]];
    const size = length(" " + tab[1] + " " + label + " ");
    if (at + size + 1 > width) return;
    at = g.put(at, 0, " ", "header");
    at = g.line(at, 0, [[" " + tab[1] + " ", style + "+bold", null], [label + " ", style, null]]);
    const hit = tab[0] === "remote" && remotes
      ? { kind: "open", sheet: "remotes-menu", key, focus: true, label: "Remotes menu: rooms and Pair devices" }
      : { kind: "screen", screen: tab[0], key, focus: true, label: tab[2] + " page (" + tab[1] + ")" };
    g.hit(at - size, 0, size, 1, hit);
  });
  if (remotes && width - at >= 23) at = g.put(at + 1, 0, ctx.ui.screen === "pairing" ? " › Pair devices " : " F6 Pair devices ",
    focused(ctx, "remotes:pairing") ? "key.focus" : "tab", { kind: "screen", screen: "pairing", key: "remotes:pairing", focus: true, label: "Remotes / Pair devices (F6)" });
  if (ctx.ui.screen === "remote" && frame.layout.sheets) {
    frame.layout.sheets.forEach((entry) => {
      const key = "open:" + entry[0];
      at = g.put(at, 0, " ", "header");
      at = g.put(at, 0, " " + entry[1] + " ", focused(ctx, key) ? "key.focus" : "tab",
        { kind: "open", sheet: entry[0], key: key, focus: true, label: entry[2] });
    });
  }
  const current = ctx.session && ctx.ui.ready ? ctx.session.current() : null;
  const connected = ctx.ui.target.mock ? "mock" : ctx.ui.connected === true ? "ok" : ctx.ui.connected === false ? "bad" : "busy";
  const room = ctx.ui.screen === "remote" && current ? [current.label + "  ", "header.title"] : null;
  const led = remotes ? [ctx.ui.target.mock ? UNLIT : LIT, connected === "mock" ? "header.dim" : "header+" + ledStyle(connected)] : null;
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
  const help = paneHelp(ctx);
  if (help) {
    const label = help[0] + ": " + help.slice(1).map((entry) => entry[0].join(" ") + " " + entry[1]).join(" \u00b7 ");
    g.line(1, y, [["\u25b8 ", "hotkey"], [fit(label, width - 4), "base"]]);
    return;
  }
  const announce = ctx.ui.screen === "pairing" ? ctx.pairing.announce : ctx.ui.announce;
  if (announce && !logVisible) g.put(1, y, fit(announce, width - 2), "dim");
  else if (frame.regions.some((region) => region.key === "remote" && region.overflow > 0))
    g.put(1, y, fit("Mouse wheel scrolls controls \u00b7 Tab changes pane", width - 2), "dim");
}

function paneHelp(ctx) {
  if (ctx.ui.overlay && ctx.ui.overlay.kind === "topology") return ["Site topology", [["↑↓"], "room"], [["←→"], "preview source"], [["Enter"], "web map"], [["Esc"], "close"]];
  if (ctx.ui.overlay && ctx.ui.overlay.kind === "cameras") return ["Cameras", [["↑↓"], "camera"], [["Enter"], "open web"], [["Esc"], "close"]];
  switch (ctx.ui.pane) {
    case "home": return ["Home", [["↑↓←→"], "choose"], [["Enter"], "open"], [["PgUp/PgDn"], "scroll"]];
    case "security": return ["Security · Not connected", [["F1"], "Home"]];
    case "cameras": return ["Cameras", [["↑↓"], "camera"], [["Enter"], "open web"], [["F1"], "Home"]];
    case "topology": return ["Site", [["↑↓"], "room"], [["←→"], "preview source"], [["Enter"], "web map"]];
    case "rooms": return ["Rooms", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "apply source"]];
    case "sheet:rooms": return ["Rooms", [["\u2191\u2193"], "select room"], [["Enter"], "remote"]];
    case "sources": return ["Sources", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "activate"]];
    case "remote": return ["Remote", [["\u2190\u2191\u2192\u2193"], "D-pad"], [["Enter"], "OK"]];
    case "info": return ["Info", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "apply source"], [["PgUp/PgDn"], "scroll"]];
    case "signal": return ["Signal & audio", [["↑↓"], "room"], [["←→"], "source"], [["Enter"], "apply source"], [["PgUp/PgDn"], "scroll"]];
    case "sheet:info": return ["Info", [["\u2191\u2193"], "scroll"]];
    case "log": return ctx.ui.screen === "pairing" ? ["Log", [["\u2191\u2193"], "scroll"], [["PgUp/PgDn"], "scroll"]]
      : ["Log", [["\u2191\u2193"], "room"], [["\u2190\u2192"], "source"], [["Enter"], "apply source"], [["PgUp/PgDn"], "scroll"]];
    case "pair-list": return ["Pairing", [["\u2191\u2193"], "select device"], [["Enter"], "pair"], [["s/S"], "sort/reverse"]];
    case "pair-card": return ["Pairing", [["\u2191\u2193"], "field"], [["Enter"], "pair/send"]];
    case "sheet:help": return ["Help", [["\u2191\u2193"], "scroll"], [["Esc"], "close"]];
    case "sheet:signal": return ["Signal & audio", [["\u2191\u2193"], "scroll"], [["Esc"], "close"]];
    case "sheet:inputs": return ["Inputs", [["\u2191\u2193"], "choose"], [["Enter"], "select"]];
    default: return null;
  }
}

function shortcutBar(frame, ctx, width, y) {
  const g = frame.grid;
  const log = [["L"], ctx.ui.logVisible === false ? "Show log" : "Hide log"];
  const help = paneHelp(ctx);
  const shortcuts = !["remote", "pairing"].includes(ctx.ui.screen) ? TUI_SHORTCUTS.filter(entry => ["F1", "F2", "F3", "F4", "F5", "?", "q"].includes(entry[0][0]))
    : ctx.ui.screen === "pairing" ? PAIR_SHORTCUTS.filter((entry) => !["\u2191", "Enter", "Tab"].includes(entry[0][0]))
    : ctx.Session.SHORTCUTS.filter((entry) => !["ArrowUp", "\u2190", "Enter", "Backspace"].includes(entry[0][0])).concat(TUI_SHORTCUTS.filter((entry) => entry[0][0] !== "Tab"));
  const list = (["remote", "pairing"].includes(ctx.ui.screen) ? [log, [["Tab"], "Pane"]] : [[["Tab"], "Pane"]]).concat(help ? help.slice(1) : [], shortcuts.filter((entry) => entry[0][0] !== "L"));
  g.fill(0, y, width, 1, "footer");
  let at = 1;
  for (const entry of list) {
    const keys = entry[0].join(entry[2] || " ");
    const size = length(keys) + 1 + length(entry[1]) + 3;
    if (at + size > width) break;
    at = g.put(at, y, keys, "footer.key");
    at = g.put(at, y, " " + entry[1] + "   ", "footer");
  }
}

function pageScreen(frame, ctx, rect) {
  const pages = {
    home: ["thermite", Landing.content],
    security: ["Security", Landing.security],
    cameras: ["Cameras", Cameras.content],
    topology: ["Site topology", Topology.content]
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
        inner.put(2, 1, ctx.ui.error ? "The remote could not start" : "Reading every room\u2026", ctx.ui.error ? "alert" : "dim");
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
  const inline = signalInline(rect.w, rect.h + 3, ctx.ui.logVisible !== false);
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
  const sourceHeight = mainHeight >= 6 ? 3 : 0;
  if (sourceHeight) panel(frame, ctx, { x: rect.x + roomsWidth, y: rect.y, w: remoteWidth, h: sourceHeight }, {
    key: "sources", color: "remote", title: "sources", right: "\u2190\u2192 choose \u00b7 Enter",
    content: (inner, w) => bubbleRow(frame, inner, w, ctx, session.controls(), target, session.state(current.id))
  });
  panel(frame, ctx, { x: rect.x + roomsWidth, y: rect.y + sourceHeight, w: remoteWidth, h: mainHeight - sourceHeight }, {
    key: "remote", color: "remote", title: current.label,
    right: remote && !remote.note ? target.label + " \u00b7 " + remote.label : "",
    footer: [["0-9", "source"], ["Enter", "OK"], ["v", "volume"]],
    content: (inner, w) => remoteContent(frame, inner, w, ctx)
  });
  if (infoWidth) {
    const infoHeight = inline ? Math.min(10, Math.floor(mainHeight * 0.38)) : mainHeight;
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
  session.rooms().forEach((room) => {
    const shown = session.view(room, session.state(room.id));
    const key = "room:" + room.id;
    const style = focused(ctx, key) ? "row.focus" : room === current ? "row.current" : "row";
    g.fill(0, y, w, 1, style);
    if (style === "row.current") g.put(0, y, "\u258e", "row.mark");
    g.put(1, y, ledChar(shown.led, ctx.ui.blink), style === "row.focus" ? style : style + "+" + ledStyle(shown.led));
    const labelWidth = Math.max(4, w - 4 - Math.min(12, length(shown.short)) - 1);
    g.put(3, y, fit(room.label, labelWidth), style);
    const short = fit(shown.short, Math.max(0, w - 4 - labelWidth));
    g.put(w - 1 - length(short), y, short, style === "row" ? "dim" : style);
    g.hit(0, y, w, 1, { kind: "room", room: room.id, key: key, focus: true, label: room.label + ", " + shown.spoken });
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
  return {
    kind: "key", button: id, key: "key:" + id, focus: true, repeat: Boolean(spec.repeat),
    label: spec.label + " \u2192 " + ctx.session.drives(id, target) + (hints ? "  (" + hints.join(", ") + ")" : "")
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

// A key at (x, y), its caption centered in width; returns the column after it.
function key(g, ctx, x, y, id, width, options) {
  const label = keyLabel(ctx, id);
  return rounded(g, x, y, label, width || length(label) + 4, keyStyle(ctx, id, options), keyHit(ctx, id));
}

// Keys in rows centered in width, wrapped where they would not fit, a row
// apart; returns the row after them.
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
      x = key(g, ctx, x, y + index * 4, entry.id, entry.size) + gap;
    });
  });
  return y + rows.length * 4 - 1;
}

function remoteContent(frame, g, w, ctx) {
  const session = ctx.session;
  const current = session.current();
  const list = session.controls();
  const target = session.activeTarget(list);
  const state = session.state(current.id);
  let y = 0;
  const shown = session.view(current, state);
  if (shown.problem) y = paragraph(g, 2, y, w - 4, "\u26a0 " + shown.problem, "alert");
  const remote = session.remoteOf(target);
  const groups = remote && remote.keyGroups ? remote.keyGroups : null;
  const dense = w >= 104 && target && ["sony_tv", "sony_bluray"].includes(target.remote)
    && groups && groups.some((group) => !group.primary);
  const width = dense ? 100 : Math.min(54, w);
  const primaryWidth = dense ? 48 : width;
  const body = new Grid(width, null, "base"), primary = new Grid(primaryWidth, null, "base");
  let row = 0;
  if (!remote) row = note(primary, primaryWidth, row, "Nothing to control", "Choose a source above");
  else if (remote.note) row = note(primary, primaryWidth, row, target.label, remote.note.replace("{device}", target.label));
  else row = layout(primary, ctx, primaryWidth, row, remote, groups ? groups.filter((group) => group.primary) : null);
  lower(primary, ctx, primaryWidth, row + 1, remote, target);
  copyGrid(body, primary, 0, 0);
  if (dense) {
    const secondary = new Grid(48, null, "base");
    groupRows(secondary, ctx, 48, 0, groups.filter((group) => !group.primary));
    copyGrid(body, secondary, 52, 0);
  } else if (groups) {
    groupRows(body, ctx, width, primary.height + 1, groups.filter((group) => !group.primary));
  }
  const left = Math.floor((w - width) / 2);
  copyGrid(g, body, left, y);
  frame.remoteBody = { left, width, primaryWidth, columns: dense ? 2 : 1, height: body.height };
  return y + body.height;
}

function copyGrid(into, from, x, y) {
  into.blit(from, 0, x, y, from.width, from.height);
  into.order.push.apply(into.order, from.order);
}

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
      row += 4;
    }
  });
  return row;
}

// The bubbles: Off and the room's sources, then set apart its display and
// audio device; a row too wide scrolls, keeping what the room shows in sight.
function bubbleRow(frame, g, w, ctx, list, target, state) {
  const session = ctx.session;
  const items = session.bubbles(list);
  if (!items.length) return 0;
  const parts = [];
  items.forEach((item, index) => {
    if (index && ctx.Session.DEVICE_ROLE[item.role] && !ctx.Session.DEVICE_ROLE[items[index - 1].role]) {
      parts.push({ gap: true, width: 3 });
    }
    const shown = session.bubbleState(item, state);
    const led = shown === "current" ? "ok" : shown === "switching" ? "busy" : "off";
    const text = " " + (item.digit ? item.digit + " " : "") + (item.choose ? ledChar(led, ctx.ui.blink) + " " : "") + item.label + " ";
    parts.push({ item: item, shown: shown, led: led, text: text, width: length(text) });
  });
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
    const selected = target && item.id === target.id;
    let style = part.shown === "current" ? "bubble.current" : part.shown === "switching" ? "bubble.switching"
      : selected ? "bubble.selected" : "bubble";
    const cursor = !ctx.ui.overlay && ctx.ui.pane !== "remote" && ctx.ui.sourceCursor[session.current().id] === item.id;
    if (focused(ctx, key) || cursor) style = "bubble.focus";
    const remote = item.remote ? ctx.layouts.remotes[item.remote] : null;
    const hit = {
      kind: "bubble", item: item, key: key, focus: true,
      label: session.bubbleLabel(item, part.shown) + (remote ? " \u00b7 " + remote.label : "") + (item.digit ? "  (" + item.digit + ")" : "")
    };
    let at = x;
    if (item.digit) {
      at = row.put(at, 0, " " + item.digit, style === "bubble" ? "bubble.digit" : style, hit);
      row.put(at, 0, part.text.slice(1 + item.digit.length), style, hit);
    } else {
      row.put(at, 0, part.text, style, hit);
    }
    if (item.choose && style !== "bubble.focus") {
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
  if (w < 34) {
    let row = y;
    if (corners.slice(0, 2).some(Boolean)) row = keyRows(g, ctx, row, w, corners.slice(0, 2).filter(Boolean), 8) + 1;
    const gap = w >= 16 ? 1 : 0;
    const size = Math.max(4, Math.min(6, Math.floor((w - 2 - gap * 2) / 3)));
    const left = Math.floor((w - size * 3 - gap * 2) / 2);
    const center = left + size + gap;
    key(g, ctx, center, row, "up", size);
    key(g, ctx, left, row + 3, "left", size);
    key(g, ctx, center, row + 3, "ok", size, { ok: true });
    key(g, ctx, center + size + gap, row + 3, "right", size);
    key(g, ctx, center, row + 6, "down", size);
    row += 9;
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
  key(g, ctx, ok - 9, y + 3, "left", 7);
  key(g, ctx, ok, y + 3, "ok", 8, { ok: true });
  key(g, ctx, ok + 10, y + 3, "right", 7);
  key(g, ctx, ok, y + 6, "down", 8);
  corner(corners[2], y + 6, false);
  corner(corners[3], y + 6, true);
  return y + 9;
}

// Volume as the rocker or the slider, Mute, and the CH rocker, with their captions.
function lower(g, ctx, w, y, remote, target) {
  const session = ctx.session;
  const view = session.audioView();
  const idle = !session.current().audio;
  const width = Math.min(w, 44, Math.max(16, w - 4));
  const left = Math.floor((w - width) / 2);
  if (view.slider && width >= 16) {
    key(g, ctx, left, y, "volume_down", 5, { idle: idle });
    key(g, ctx, left + width - 5, y, "volume_up", 5, { idle: idle });
    const x = left + 6;
    const value = view.disabled ? "\u2013" : view.muted ? "muted" : view.text;
    const compact = w < 30;
    const room = compact ? 0 : Math.max(6, length(value));
    const track = compact ? width - 12 : Math.max(4, width - 13 - room);
    const focus = focused(ctx, "slider");
    meter(g, x, y + 1, track, view, focus);
    g.hit(x, y + 1, track, 1, { kind: "slider", key: "slider", focus: true, max: view.max,
      label: view.label + ": " + view.text + "  (\u2190 \u2192 Home End when focused)" });
    if (!compact) g.put(x + track + 1, y + 1, value, view.muted ? "tone.busy+bold" : focus ? "meter.value.focus" : "meter.value");
  } else {
    const span = Math.min(19, w);
    const x = Math.floor((w - span) / 2);
    key(g, ctx, x, y, "volume_down", 5, { idle: idle });
    if (span >= 15) g.put(Math.floor((w - 3) / 2), y + 1, "VOL", "meter.label");
    key(g, ctx, x + span - 5, y, "volume_up", 5, { idle: idle });
  }
  const caption = fit(view.slider && !view.disabled && w >= 30 ? view.short : view.caption, Math.min(w - 2, length(view.caption)));
  g.put(Math.max(1, Math.floor((w - length(caption)) / 2)), y + 3, caption, view.muted ? "tone.busy" : "dim");
  // Match the page's lower group: mute and short transport beneath volume,
  // with channel controls at its side. Keep labelled service keys above it.
  const playback = remote && remote.corners && (remote.playback || []).length <= 3 ? remote.playback || [] : [];
  const channel = Boolean(remote && !remote.note && remote.channel);
  const ids = ["mute"].concat(playback, channel ? ["channel_up", "channel_down"] : []);
  const end = keyRows(g, ctx, y + 5, w, ids, 5);
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

function infoContent(g, w, ctx, compact = false) {
  const session = ctx.session;
  const shown = session.info();
  if (compact) {
    let y = heading(g, 0, "Devices in the room");
    const roleWidth = 10, nameWidth = Math.max(8, w - roleWidth - 4);
    shown.devices.forEach((device) => {
      const names = wrap(device.label, nameWidth), roles = wrap(device.roles.join(", ") || "—", roleWidth);
      for (let row = 0; row < Math.max(names.length, roles.length); row++, y++) {
        g.put(1, y, names[row] || "", "base");
        g.put(w - roleWidth - 1, y, roles[row] || "", "dim");
      }
      if (device.meta) y = paragraph(g, 1, y, w - 2, device.meta, "faint");
    });
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
  y = heading(g, y + 1, "Devices in the room");
  if (!shown.devices.length) y = paragraph(g, 1, y, w - 2, "No devices in the inventory", "dim");
  shown.devices.forEach((device) => {
    let x = g.put(1, y, fit(device.label, w - 2), "base") + 1;
    device.roles.forEach((role) => {
      if (x + length(role) + 2 < w) x = g.put(x, y, " " + role + " ", "badge") + 1;
    });
    g.put(3, y + 1, fit(device.meta, w - 4), "dim");
    y += 2;
  });
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
  const w = Math.min(width - 2, ["signal", "topology"].includes(overlay.kind) ? 100 : ["info", "help", "cameras"].includes(overlay.kind) ? 66 : 50);
  const content = {
    rooms: (inner, iw) => roomsContent(inner, iw, ctx),
    info: (inner, iw) => infoContent(inner, iw, ctx),
    inputs: (inner, iw) => inputsContent(inner, iw, ctx),
    help: (inner, iw) => helpContent(inner, iw, ctx),
    signal: (inner, iw) => signalContent(inner, iw, ctx),
    "remotes-menu": (inner, iw) => {
      inner.put(1, 0, "Remotes", "strong");
      button(inner, ctx, 1, 2, "Rooms & remotes", "menu:remote", { kind: "screen", screen: "remote", key: "menu:remote", focus: true, label: "Rooms and remotes" });
      button(inner, ctx, 1, 6, "Pair devices · F6", "menu:pairing", { kind: "screen", screen: "pairing", key: "menu:pairing", focus: true, label: "Remotes / Pair devices (F6)" });
    },
    cameras: (inner, iw) => Cameras.content(inner, iw, ctx),
    topology: (inner, iw) => Topology.content(inner, iw, ctx)
  }[overlay.kind];
  const probe = new Grid(w - 2, null, "base");
  content(probe, w - 2);
  const h = Math.max(3, Math.min(height - 4, probe.height + 2));
  const x = Math.floor((width - w) / 2);
  const y = Math.max(1, Math.floor((height - h) / 2));
  panel(frame, ctx, { x: x, y: y, w: w, h: h }, {
    key: "sheet:" + overlay.kind, color: "sheet", solid: true, footer: [["Esc", "close"]],
    title: overlay.title || { rooms: "Rooms", info: "Info", help: "Keys", signal: "Signal & audio", cameras: "Cameras", topology: "Site topology", "remotes-menu": "Remotes" }[overlay.kind],
    content: content
  });
}

function signalContent(g, w, ctx) {
  const model = ctx.session.diagnostics ? ctx.session.diagnostics() : null;
  let y = paragraph(g, 1, 0, w - 2, "Current reports · bold/amber values are stale", "dim");
  if (!model) return paragraph(g, 1, y, w - 2, "Reading the confirmed signal path\u2026", "dim");
  y = paragraph(g, 1, y, w - 2, model.room.label + " \u00b7 " + (model.source ? model.source.label : "Source unknown"), "strong");
  if (model.route && model.route.length) y = paragraph(g, 1, y, w - 2, model.route.map((step) => step.label).join(" → "), "dim");
  if (model.pending) y = paragraph(g, 1, y, w - 2, "Switching to " + model.pending.label + "; readings still describe the confirmed source.", "tone.busy") + 1;
  const groups = [["video", "Current video"], ["audio", "Current audio"], ["path", "Configured path"],
    ["edid", "EDID capabilities"], ["processing", "Processing & settings"]];
  groups.forEach(([id, title]) => {
    y = heading(g, y, title);
    const rows = model.sections[id] || [];
    rows.forEach((entry) => {
      const value = entry.availability === "reported" ? entry.value : entry.availability === "unavailable" ? "Unavailable"
        : entry.availability === "not_reported" && entry.value === "Awaiting fresh report" ? "Awaiting fresh report" : "Not reported";
      const labelWidth = Math.max(10, Math.min(22, Math.floor((w - 3) * 0.38))), valueWidth = w - labelWidth - 4;
      const labels = wrap(entry.label, labelWidth), values = wrap(value, valueWidth);
      const reported = entry.availability === "reported";
      const stale = reported && entry.basis !== "configured"
        && (entry.stale || !Number.isFinite(entry.observedAt));
      const valueStyle = !reported ? "dim" : stale ? "tone.busy+bold" : "base";
      for (let row = 0; row < Math.max(labels.length, values.length); row++, y++) {
        g.put(1, y, labels[row] || "", "dim");
        g.put(labelWidth + 3, y, values[row] || "", valueStyle);
      }
      if (entry.detail) y = paragraph(g, labelWidth + 3, y, valueWidth, entry.detail, "faint");
    });
    if (!rows.length) y = paragraph(g, 1, y, w - 2, "Not reported", "dim");
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
  let y = heading(g, 0, "Remote");
  const rows = ctx.Session.SHORTCUTS.concat(TUI_SHORTCUTS);
  rows.forEach((entry) => {
    g.put(2, y, fit(entry[0].join(entry[2] || " "), 18), "hotkey");
    g.put(21, y, entry[1], "base");
    y += 1;
  });
  y = heading(g, y + 1, "Remotes / Pair devices (F6)");
  PAIR_SHORTCUTS.forEach((entry) => {
    g.put(2, y, fit(entry[0].join(" "), 18), "hotkey");
    g.put(21, y, entry[1], "base");
    y += 1;
  });
  y = heading(g, y + 1, "Mouse");
  return paragraph(g, 2, y, w - 4, MOUSE_HELP, "base");
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
    content: (inner, w) => pairCardContent(inner, w, ctx, rect.w < 72)
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
  if (!backend || !pairing.loaded) {
    g.put(1, y, "Reading thermite devices\u2026", "dim");
    return;
  }
  const devices = pairDevices(ctx);
  if (!devices.length) {
    g.put(1, y, "thermite has no TVs or streamers to pair yet.", "dim");
    return;
  }
  const device = backend.device(pairing.selected) || devices[0];
  const view = backend.view(device.id);
  if (narrow) {
    const index = devices.findIndex((candidate) => candidate.id === device.id);
    let x = g.put(1, y, ARROW.left, "hotkey", { kind: "pair-step", step: -1, key: "pair-step:-1" });
    x = g.put(x + 1, y, "device " + (index + 1) + " of " + devices.length + (pairing.mock ? "  (mock: 1234, A1B2C3)" : ""), "dim");
    g.put(x + 1, y, ARROW.right, "hotkey", { kind: "pair-step", step: 1, key: "pair-step:1" });
    y += 2;
  }
  let x = g.put(1, y, device.label, "strong");
  if (device.model) g.put(x + 2, y, device.model, "dim");
  y += 1;
  g.put(1, y, device.room, "dim");
  y += 2;
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
    g.put(1, y, "Code on the " + device.label + " screen", "base");
    y += 1;
    const code = pairing.codes[device.id] || "";
    const cells = [];
    for (let index = 0; index < device.length; index += 1) cells.push(code[index] || "_");
    const field = " " + cells.join(" ") + " ";
    x = g.put(1, y + 1, field, focused(ctx, "code") ? "field.focus" : "field",
      { kind: "code", device: device.id, key: "code", focus: true, label: "Type the code: " + ctx.Pairing.codeRule(device) });
    const ready = !busy && ctx.Pairing.codeOk(device, code);
    const send = view.busy === "code" ? "Sending\u2026" : "Send code";
    button(g, ctx, x + 2, y, send, "send-code", { kind: "send-code", device: device.id, key: "send-code", focus: true, label: send + (ready ? "" : " (type the whole code first)") },
      ready ? "switch.on" : "key.off");
    y += 3;
    g.put(1, y, "The code is " + ctx.Pairing.codeRule(device) + ".", "dim");
    y += 2;
    button(g, ctx, 1, y, view.busy === "request" ? "Asking\u2026" : "Ask for a new code", "new-code",
      { kind: "new-code", device: device.id, key: "new-code", focus: true, label: "Ask for a new code" }, busy ? "key.off" : "key");
    y += 4;
    if (view.cancel) {
      const stop = view.busy === "cancel" ? "Cancelling\u2026" : "Cancel: keep the old pairing";
      button(g, ctx, 1, y, stop, "pair-cancel",
        { kind: "pair-cancel", device: device.id, key: "pair-cancel", focus: true, label: stop }, busy ? "key.off" : "key");
      y += 4;
    }
    if (device.hint) y = paragraph(g, 1, y, w - 2, device.hint, "dim");
  }
}

module.exports = { render, GLYPHS, TUI_SHORTCUTS, PAIR_SHORTCUTS, WIDE, MEDIUM, pairDevices, pairColumns, PAIR_FIELDS, signalInline };
