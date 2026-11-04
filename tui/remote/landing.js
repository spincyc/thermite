/* Home is navigation and reported room state, never a device control. */
"use strict";
const { fit, wrap } = require("./grid");
const Torch = require("./torch");
const Navigation = require("../../web/remote/navigation");

const MODULES = Navigation.pages.filter(page => page.module).map(page => ({ ...page, id: page.screen }));

function content(g, width, ctx, height) {
  let y = 1;
  const line = (value, style = "dim") => wrap(value, Math.max(4, width - 4)).forEach(part => g.put(2, y++, part, style));
  line("Your home, in one place.");
  y += 1;
  const columns = width >= 72 ? 2 : 1, gap = 2;
  const cardWidth = Math.max(10, Math.floor((width - 4 - gap * (columns - 1)) / columns));
  const cardHeight = 2;
  const modules = MODULES;
  modules.forEach((module, index) => {
    const x = 2 + (index % columns) * (cardWidth + gap), top = y + Math.floor(index / columns) * (cardHeight + 1);
    const key = "home:" + module.id;
    const active = ctx.ui.focusVisible && ctx.ui.focus === key;
    const style = active ? "row.focus" : "row";
    const hit = { kind: "screen", screen: module.id, key, focus: true, label: module.label + " (" + module.key + ")" };
    g.fill(x, top, cardWidth, cardHeight, style);
    g.put(x, top, fit(module.key + "  " + module.label, cardWidth), active ? style + "+bold" : "link");
    g.put(x, top + 1, fit(module.compact, cardWidth), active ? style : "dim");
    g.hit(x, top, cardWidth, cardHeight, hit);
  });
  y += Math.ceil(modules.length / columns) * (cardHeight + 1);
  const accessLinks = Navigation.pages.filter(page => (page.account || page.system) && Navigation.allowed(page, ctx.access))
    .map(page => [page.screen, page.key + " " + page.label]);
  let accessX = 2;
  accessLinks.forEach(([screen, label]) => {
    if (accessX + label.length > width - 2) { y++; accessX = 2; }
    const key = "home:" + screen;
    g.put(accessX, y, label, ctx.ui.focus === key ? "row.focus" : "link", { kind: "screen", screen, key, focus: true, label }); accessX += label.length + 3;
  }); if (accessLinks.length) y += 2;
  line("Rooms · reported sources", "section");
  if (!ctx.ui.ready) { line("Reading rooms…"); return y; }
  if (!ctx.session.rooms().length) { line("No rooms configured"); return y; }
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
  line("Not connected", "section"); y += 1;
  line("Security status and zones are unavailable until a system is connected.");
  return y;
}

function system(g, width, ctx) {
  let y = 1;
  const state = ctx.system.backend?.state;
  const line = (value, style = "dim") => wrap(value, Math.max(4, width - 4)).forEach(part => g.put(2, y++, part, style));
  const action = (label, id, tool) => {
    const key = "system:" + id + (tool ? ":" + tool.id : "");
    g.put(2, y++, fit(label, width - 4), ctx.ui.focus === key ? "row.focus" : "link",
      { kind: "system-action", action: id, tool: tool?.id, key, focus: true, label });
  };
  line("Runtime " + (state?.manifest?.runtime.version || "unavailable") + " · configured", "strong");
  line("Live health is not checked here.");
  if (state?.loading) line("Reading system information…");
  if (state?.error) line(state.error, "alert");
  action("Refresh information", "refresh");
  if (!state?.loaded) return y;
  y++;
  action((ctx.system.expanded ? "▾ " : "▸ ") + "Advanced tools", "advanced");
  if (ctx.system.expanded) {
    for (const tool of state.manifest.tools) {
      y++; line(tool.label, "strong"); line(tool.description);
      if (tool.localOnly) {
        line("Host only · open locally or forward over SSH:");
        line(tool.url); if (tool.forwarding) line(tool.forwarding);
      } else action("Open " + tool.label + " in browser ›", "open", tool);
    }
  }
  if (ctx.system.link) { y++; line(ctx.system.link, "link"); }
  return y;
}

module.exports = { MODULES, content, security, system };
