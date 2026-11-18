/*
 * The terminal's quieter use of a house theme. Its background stays as
 * chosen; surfaces sit close to it, and the palette's accents are softened
 * toward its own text. The web palette and the torch keep their colours,
 * and so does what says a state (LIGHTS, an alert): softened, on, switching
 * and a problem came out as one colour.
 */
"use strict";

const { contrast } = require("./paint");

const PANELS = ["rooms", "remote", "info", "log", "pair"];
// The House board's state colours (lib/oh_metrics/dashboards.py).
const DAY = { source: "#D7B18A", off: "#394354", unknown: "#808A96" };
const METER = 32;
// What a light can say. Each state has a glyph no other has, so it reads
// where colour cannot say it: with NO_COLOR, on a console of 16 colours,
// in a row drawn in the focus's own style. role is the colour styles()
// gives its style, "led.<state>", and bold its attribute; busy goes
// between its two glyphs where the terminal has motion on. The installer
// draws on, off, switching and a problem on the Linux console, in the font
// it sets there (iso/installer.py): those four are glyphs it drew before
// this table, or ASCII.
const LIGHTS = {
  ok: { glyph: "\u25cf", role: "ok" },
  busy: { glyph: "\u25d0", blink: "\u25d1", role: "busy" },
  bad: { glyph: "!", role: "bad", bold: true },
  // A source chosen, sent and never read back.
  unconfirmed: { glyph: "?", role: "bad", bold: true },
  off: { glyph: "\u25cb", role: "off" },
  // Read before the house was lost: neither on nor off, and no colour, as a light that is off has none.
  stale: { glyph: "\u25cc", role: "off" }
};

// A light's glyph and style: [glyph, "led.<state>"]; a state the table lacks is off.
function light(state, blink) {
  const name = Object.prototype.hasOwnProperty.call(LIGHTS, state) ? state : "off";
  return [blink && LIGHTS[name].blink || LIGHTS[name].glyph, "led." + name];
}

function rgb(color) {
  return [1, 3, 5].map((at) => parseInt(color.slice(at, at + 2), 16));
}

function mix(a, b, share) {
  const from = rgb(a);
  const to = rgb(b);
  return "#" + from.map((value, index) => Math.round(value + (to[index] - value) * share)
    .toString(16).padStart(2, "0")).join("");
}

function gradient(stops, count) {
  return Array.from({ length: count }, (_, index) => {
    const at = index / (count - 1) * (stops.length - 1);
    const from = Math.min(stops.length - 2, Math.floor(at));
    return mix(stops[from], stops[from + 1], at - from);
  });
}

function styles(c) {
  const highContrast = [c.text, c.muted, c.faint].every((color) => contrast(color, c.bg) >= 7);
  const minimum = highContrast ? 7 : 4.5;
  const neutral = mix(c.text, c.bg, 0.4);
  const soft = (color, share) => highContrast ? color : mix(color, neutral, share);
  const readable = (color, bg, least) => {
    const required = least || minimum;
    for (let step = 0; step <= 20; step += 1) {
      const candidate = mix(color, c.text, step / 20);
      if (contrast(candidate, bg) >= required) return candidate;
    }
    return c.text;
  };
  const surface = mix(c.surface, mix(c.bg, c.text, 0.045), 0.8);
  const key = mix(c.raised, mix(c.bg, c.text, 0.075), 0.85);
  const pad = mix(c.bg, key, 0.45);
  const copper = readable(soft(c.accent, 0.58), c.bg);
  const sage = readable(soft(c.ok, 0.55), c.bg);
  const slate = readable(soft(c.info, 0.5), c.bg);
  const focus = readable(soft(mix(c.focus, c.info, 0.65), 0.5), c.bg);
  // What says a state keeps the palette's own colour for it: softened as
  // the accents are, switching, a problem and the hotkeys' copper were one
  // colour to the eye, and one cell of xterm's 256.
  const ok = readable(c.ok, c.bg);
  const busy = readable(c.busy, c.bg);
  const bad = readable(c.bad, c.bg);
  const muted = readable(soft(c.muted, 0.15), c.bg);
  const faint = readable(soft(c.faint, 0.12), c.bg, highContrast ? 7 : 3);
  const selected = mix(surface, copper, 0.12);
  const focused = mix(surface, focus, 0.19);
  const pressed = mix(surface, copper, 0.22);
  const on = (bg, fg, bold) => ({ fg: readable(fg, bg), bg: bg, bold: Boolean(bold), underline: false });
  // A state's colour as a foreground: hue keeps it a colour in xterm's 256 (paint.js).
  const state = (fg, bold) => ({ fg: fg, bold: Boolean(bold), hue: true });
  const stateOn = (bg, fg, bold) => Object.assign(on(bg, fg, bold), { hue: true });
  // The focus: its ground is too near the base's to show without colour, so it asks for reverse video there.
  const focusStyle = () => Object.assign(on(focused, focus, true), { reverse: true });
  // A block of a fixed colour, its words in whichever of the theme's text
  // and ground reads better on it.
  const block = (bg) => ({ fg: contrast(c.text, bg) >= contrast(c.bg, bg) ? c.text : c.bg, bg: bg, bold: false, underline: false });
  const out = {
    base: { fg: c.text, bg: c.bg },
    dim: { fg: muted },
    faint: { fg: faint },
    strong: { fg: c.text, bold: true },
    hotkey: { fg: copper },
    section: { fg: c.text, bold: true },
    rule: { fg: highContrast ? c.line : mix(c.line, neutral, 0.25) },
    header: on(surface, c.text),
    "header.title": on(surface, c.text, true),
    "header.brand": on(surface, copper),
    "header.dim": on(surface, muted),
    "header.hotkey": on(surface, copper),
    tab: on(surface, muted),
    "tab.current": on(selected, c.text, true),
    footer: on(surface, muted),
    "footer.key": on(surface, copper),
    "border.sheet": { fg: highContrast ? focus : mix(focus, c.bg, 0.38) },
    "title.sheet": { fg: c.text, bold: true },
    thumb: { fg: muted },
    row: {},
    "row.current": on(selected, c.text, true),
    "row.mark": on(selected, copper),
    "row.focus": focusStyle(),
    key: on(key, c.text),
    "key.idle": on(pad, faint),
    "key.lit": stateOn(selected, busy, true),
    "key.focus": focusStyle(),
    "key.pressed": on(pressed, copper, true),
    "key.off": on(pad, faint),
    "key.ok": on(selected, c.text, true),
    pad: on(pad, c.text),
    bubble: on(key, muted),
    "bubble.selected": on(selected, c.text, true),
    "bubble.current": on(selected, c.text, true),
    "bubble.switching": stateOn(selected, busy, true),
    "bubble.focus": focusStyle(),
    "bubble.digit": on(key, faint),
    // Greyed while the room is read-only: below the contrast the others keep.
    "bubble.inert": { fg: faint, bg: key, bold: false, underline: false },
    alert: state(bad, true),
    danger: stateOn(surface, bad),
    "danger.armed": stateOn(mix(surface, bad, 0.2), bad, true),
    link: { fg: copper, underline: true },
    badge: on(surface, slate),
    "switch.on": stateOn(selected, ok, true),
    "switch.off": on(key, muted),
    field: Object.assign(on(key, c.text), { underline: true }),
    "field.focus": focusStyle(),
    "meter.label": { fg: muted },
    "meter.empty": { fg: highContrast ? c.line : mix(c.line, neutral, 0.25) },
    "meter.off": { fg: highContrast ? c.line : mix(c.line, neutral, 0.15) },
    "meter.muted": { fg: faint },
    "meter.cap": { fg: c.text },
    "meter.cap.focus": { fg: focus, reverse: true },
    "meter.cap.muted": { fg: muted },
    "meter.value": { fg: c.text, bold: true },
    "meter.value.focus": focusStyle(),
    meter: gradient([slate, sage, copper], METER),
    "toast.": on(surface, c.text, true),
    "toast.muted": on(key, c.text),
    "toast.bad": stateOn(surface, bad, true),
    "toast.tag": stateOn(selected, busy),
    // Rooms through the day (day.js), in the House board's own colours
    // (lib/oh_metrics/dashboards.py IN_USE, OFF and UNKNOWN): tan, slate, grey.
    "day.source": block(DAY.source),
    "day.off": block(DAY.off),
    "day.unknown": block(DAY.unknown),
    "day.none": { fg: faint },
    "tone.ok": state(ok),
    "tone.busy": state(busy),
    "tone.bad": state(bad),
    "tone.muted": { fg: muted },
    "tone.": { fg: c.text }
  };
  const roles = { ok: state(ok), busy: state(busy), bad: state(bad), off: { fg: faint } };
  Object.keys(LIGHTS).forEach((name) => {
    out["led." + name] = Object.assign({}, roles[LIGHTS[name].role], LIGHTS[name].bold ? { bold: true } : {});
  });
  const guides = [c.ok, c.accent, c.info, c.muted, c.info];
  PANELS.forEach((name, index) => {
    const title = readable(soft(mix(c.hues[index], guides[index], 0.6), 0.6), c.bg);
    out["border." + name] = { fg: highContrast ? title : mix(title, c.bg, 0.5) };
    out["title." + name] = { fg: title };
  });
  return out;
}

module.exports = { styles, light, mix, gradient, LIGHTS, PANELS, METER, DAY };
