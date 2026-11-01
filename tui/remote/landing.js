/* Home is navigation and reported room state, never a device control. */
"use strict";
const { fit, wrap } = require("./grid");
const Torch = require("./torch");

const MODULES = [
  { id: "remote", key: "F2", label: "Remotes", detail: "Rooms, sources and device controls", note: "Pair devices from the Remotes menu" },
  { id: "security", key: "F3", label: "Security", detail: "Security system", note: "Not connected" },
  { id: "cameras", key: "F4", label: "Cameras", detail: "Camera status and browser viewing", note: "Open a camera when you need it" },
  { id: "topology", key: "F5", label: "Site", detail: "Configured video and USB connections", note: "Explore without switching a source" }
];

function content(g, width, ctx, height) {
  let y = 1;
  const line = (value, style = "dim") => wrap(value, Math.max(4, width - 4)).forEach(part => g.put(2, y++, part, style));
  line("thermite", "strong");
  line("Your home, in one place.");
  y += 1;
  const columns = width >= 88 ? 2 : 1, gap = 2;
  const cardWidth = Math.max(10, Math.floor((width - 4 - gap * (columns - 1)) / columns));
  const cardHeight = columns === 1 ? 3 : 6;
  const modules = MODULES;
  modules.forEach((module, index) => {
    const x = 2 + (index % columns) * (cardWidth + gap), top = y + Math.floor(index / columns) * (cardHeight + 1);
    const key = "home:" + module.id;
    const active = ctx.ui.focusVisible && ctx.ui.focus === key;
    const style = active ? "key.focus" : "key";
    const hit = { kind: "screen", screen: module.id, key, focus: true, label: module.label + " (" + module.key + ")" };
    g.put(x, top, "╭" + "─".repeat(cardWidth - 2) + "╮", style);
    for (let row = 1; row < cardHeight - 1; row++) {
      g.put(x, top + row, "│" + " ".repeat(cardWidth - 2) + "│", style);
    }
    g.put(x, top + cardHeight - 1, "╰" + "─".repeat(cardWidth - 2) + "╯", style);
    const compact = { remote: "Rooms, devices & pairing", security: "Not connected", cameras: "Status & viewing", topology: "Configured connections", settings: "Schedules", users: "Accounts & grants", account: "Sign-in & access" };
    g.put(x + 2, top + 1, fit(module.key + "  " + module.label + (columns === 1 ? " · " + compact[module.id] : ""), cardWidth - 4), style + "+bold");
    if (columns > 1) {
      wrap(module.detail, Math.max(4, cardWidth - 4)).slice(0, 2).forEach((part, row) => g.put(x + 2, top + 2 + row, part, style));
      g.put(x + 2, top + 4, fit(module.note, cardWidth - 4), active ? style : "bubble");
    }
    g.hit(x, top, cardWidth, cardHeight, hit);
  });
  y += Math.ceil(modules.length / columns) * (cardHeight + 1) + 1;
  const accessLinks = ctx.access?.native ? [] : [["settings", "F7 Settings"], ...(ctx.access?.canEdit() ? [["users", "F8 Users"]] : []), ["account", "F9 Account"]];
  let accessX = 2;
  accessLinks.forEach(([screen, label]) => {
    if (accessX + label.length > width - 2) { y++; accessX = 2; }
    const key = "home:" + screen;
    g.put(accessX, y, label, ctx.ui.focus === key ? "row.focus" : "link", { kind: "screen", screen, key, focus: true, label }); accessX += label.length + 3;
  }); if (accessLinks.length) y += 2;
  line("Rooms · reported sources", "section");
  if (!ctx.ui.ready) { line("Reading rooms…"); return y; }
  const nameWidth = Math.min(25, Math.max(10, Math.floor((width - 5) * .4)));
  ctx.session.rooms().forEach(room => {
    const state = ctx.session.state(room.id), shown = ctx.session.view(room, state);
    const key = "home-room:" + room.id, active = ctx.ui.focusVisible && ctx.ui.focus === key;
    const style = active ? "row.focus" : "row";
    g.fill(1, y, width - 2, 1, style);
    g.put(2, y, fit(room.label, nameWidth), active ? style : "base");
    g.put(nameWidth + 4, y, fit(shown.short, Math.max(1, width - nameWidth - 6)), active ? style : "dim");
    g.hit(1, y++, width - 2, 1, { kind: "home-room", room: room.id, key, focus: true,
      label: room.label + ", " + shown.spoken + ". Open Remotes" });
  });
  const used = y;
  // The existing compact mark belongs to Home's quiet hero space. Never
  // displace cards or the complete room summary to make room for it.
  const spec = ctx.look && ctx.look.torch;
  if (spec && width >= 88 && height >= used && !ctx.ui.overlay) {
    const still = ctx.still || ctx.now == null;
    const colors = ctx.look.colors[ctx.ui.theme] || ctx.look.colors.dark;
    const cells = Torch.frame(spec, 4, still ? null : ctx.now, colors);
    const left = width - cells[0].length - 5;
    cells.forEach((line, row) => line.forEach((cell, column) => {
      if (cell) g.put(left + column, row, cell[0], "torch." + cell[1] + "." + (cell[2] || ""), null, cell[3]);
    }));
    if (!still) ctx.animate = Math.min(ctx.animate || Infinity, Torch.FRAME_MS);
  }
  return used;
}

function security(g, width) {
  let y = 2;
  const line = (value, style = "dim") => wrap(value, Math.max(4, width - 4)).forEach(part => g.put(2, y++, part, style));
  line("Security", "strong"); y += 1;
  line("Not connected", "section"); y += 1;
  line("The security system has not been connected to thermite.");
  line("No live system status or zones are available here.");
  return y;
}

module.exports = { MODULES, content, security };
