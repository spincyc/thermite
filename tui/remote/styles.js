/*
 * The terminal's quieter use of a house theme. Its background stays as
 * chosen; surfaces sit close to it, and the palette's accents are softened
 * toward its own text. The web palette and the torch keep their colours.
 */
"use strict";

const { contrast } = require("./paint");

const PANELS = ["rooms", "remote", "info", "log", "pair"];
const METER = 32;

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
  const busy = readable(soft(c.busy, 0.58), c.bg);
  const bad = readable(soft(c.bad, 0.58), c.bg);
  const muted = readable(soft(c.muted, 0.15), c.bg);
  const faint = readable(soft(c.faint, 0.12), c.bg, highContrast ? 7 : 3);
  const selected = mix(surface, copper, 0.12);
  const focused = mix(surface, focus, 0.19);
  const pressed = mix(surface, copper, 0.22);
  const on = (bg, fg, bold) => ({ fg: readable(fg, bg), bg: bg, bold: Boolean(bold), underline: false });
  const focusStyle = () => on(focused, focus, true);
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
    "led.ok": { fg: sage },
    "led.busy": { fg: busy },
    "led.bad": { fg: bad },
    "led.off": { fg: faint },
    "led.muted": { fg: muted },
    row: {},
    "row.current": on(selected, c.text, true),
    "row.mark": on(selected, copper),
    "row.focus": focusStyle(),
    key: on(key, c.text),
    "key.idle": on(pad, faint),
    "key.lit": on(selected, busy, true),
    "key.focus": focusStyle(),
    "key.pressed": on(pressed, copper, true),
    "key.off": on(pad, faint),
    "key.ok": on(selected, c.text, true),
    pad: on(pad, c.text),
    bubble: on(key, muted),
    "bubble.selected": on(selected, c.text, true),
    "bubble.current": on(selected, c.text, true),
    "bubble.switching": on(selected, busy, true),
    "bubble.focus": focusStyle(),
    "bubble.digit": on(key, faint),
    alert: { fg: bad, bold: true },
    danger: on(surface, bad),
    "danger.armed": on(mix(surface, bad, 0.2), bad, true),
    link: { fg: copper, underline: true },
    badge: on(surface, slate),
    "switch.on": on(selected, sage, true),
    "switch.off": on(key, muted),
    field: Object.assign(on(key, c.text), { underline: true }),
    "field.focus": focusStyle(),
    "meter.label": { fg: muted },
    "meter.empty": { fg: highContrast ? c.line : mix(c.line, neutral, 0.25) },
    "meter.off": { fg: highContrast ? c.line : mix(c.line, neutral, 0.15) },
    "meter.muted": { fg: faint },
    "meter.cap": { fg: c.text },
    "meter.cap.focus": { fg: focus },
    "meter.cap.muted": { fg: muted },
    "meter.value": { fg: c.text, bold: true },
    "meter.value.focus": focusStyle(),
    meter: gradient([slate, sage, copper], METER),
    "toast.": on(surface, c.text, true),
    "toast.muted": on(key, c.text),
    "toast.bad": on(surface, bad, true),
    "toast.tag": on(selected, busy),
    "tone.ok": { fg: sage },
    "tone.busy": { fg: busy },
    "tone.bad": { fg: bad },
    "tone.muted": { fg: muted },
    "tone.": { fg: c.text }
  };
  const guides = [c.ok, c.accent, c.info, c.muted, c.info];
  PANELS.forEach((name, index) => {
    const title = readable(soft(mix(c.hues[index], guides[index], 0.6), 0.6), c.bg);
    out["border." + name] = { fg: highContrast ? title : mix(title, c.bg, 0.5) };
    out["title." + name] = { fg: title };
  });
  return out;
}

module.exports = { styles, mix, gradient, PANELS, METER };
