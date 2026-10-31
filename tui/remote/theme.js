/*
 * The terminal's styles, as the view names them: {fg, bg, bold, underline},
 * drawn from a thermite theme (lib/oh_brand): a color for each role in a
 * dark and a light mode, and five hues, one for each panel, as btop colors
 * its boxes. The grounds between the theme's own (a key, a selection, the
 * D-pad's pad) are mixed from its roles, so any theme gets them.
 * bin/thermite remote passes the theme it chose and the torch, as paths
 * (lib/oh_brand spec(), drawn by torch.js), in THERMITE_REMOTE_LOOK; without it this reads the
 * default theme's file and draws no torch.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const ENV = "THERMITE_REMOTE_LOOK";
const DEFAULT_FILE = path.join(__dirname, "..", "..", "lib", "oh_brand", "palettes", "thermite.json");
const { styles, mix, PANELS, METER } = require("./styles");

// A theme as lib/oh_brand writes it ({name, dark, light}), and the torch's
// cells if it has them, as both modes' styles.
function create(look) {
  return {
    name: look.name,
    colors: { dark: look.dark, light: look.light },
    modes: { dark: styles(look.dark), light: styles(look.light) },
    torch: look.torch || null
  };
}

function load(env) {
  const given = (env || process.env)[ENV];
  return create(JSON.parse(given || fs.readFileSync(DEFAULT_FILE, "utf8")));
}

let fallback = null;

// A cell's style as {fg, bg, bold, underline}, from a name or names joined
// by "+" ("bubble.current+underline"); "meter.N" is the meter's Nth color
// and "torch.FG.BG" a cell of the torch: FG a role, BG a role, "ROLE%N"
// (N percent of the role over the background) or empty. Rasterized torch
// cells give their antialiased foreground/background as literal #rrggbb.
function resolve(look, mode, style) {
  if (!look) look = fallback || (fallback = load({}));
  const palette = look.modes[mode] || look.modes.dark;
  const colors = look.colors[mode] || look.colors.dark;
  const out = { fg: palette.base.fg, bg: palette.base.bg, bold: false, underline: false };
  String(style || "base").split("+").forEach((name) => {
    let own = palette[name] || {};
    if (name === "underline") own = { underline: true };
    else if (name === "bold") own = { bold: true };
    else if (/^meter\.\d+$/.test(name)) own = { fg: palette.meter[Number(name.slice(6))] || palette.meter[0] };
    else if (name.indexOf("torch.") === 0) {
      const roles = name.slice(6).split(".");
      const [role, share] = (roles[1] || "").split("%");
      const color = (value) => /^#[0-9a-fA-F]{6}$/.test(value || "") ? value : colors[value];
      own = { fg: color(roles[0]), bg: share ? mix(colors.bg, colors[role], Number(share) / 100) : color(role) || null };
    }
    if (own.fg) out.fg = own.fg;
    if (own.bg) out.bg = own.bg;
    if (own.bold) out.bold = true;
    if (typeof own.underline === "boolean") out.underline = own.underline;
  });
  return out;
}

module.exports = { ENV, PANELS, METER, create, load, resolve, styles, mix };
