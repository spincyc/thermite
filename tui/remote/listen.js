/*
 * Listen on the terminal's Remotes screen: a room's speakers as
 * web/remote/listen-session.js gives them, drawn as the page's Listen
 * without covers. The speakers' bubbles; what the chosen one plays, where
 * it is in the track, its transport, the Group, Input and Library chips
 * and its volume as Watch's; in sheets the library (Search, Browse, Queue,
 * the pages opened on them, and an Admin's TIDAL sign-in), an item's ways
 * to play and where it leads, the group, the
 * inputs and a setting's choices; under Info the speaker's power, settings
 * and facts. view.js hands it its drawing kit (headings, rounded keys, the
 * meter) so a key here looks as Watch's; controller.js turns keys and
 * clicks into the sessions' calls.
 */
"use strict";

const { Grid, LOCK, ending, fit, length } = require("./grid");

// The terminal's own keys in Listen, after the page's (RemoteSession.LISTEN_SHORTCUTS).
const SHORTCUTS = [
  [["↑", "↓"], "Choose a control"],
  [["←", "→"], "Step a volume, or move 10 s on the position"],
  [["Enter"], "Press it, open an item, or offer how to play it"],
  [["i"], "Info: the speaker's power, settings and facts"],
  [["Backspace"], "Back a page (Library)"],
  [["Esc"], "Clear the search, or close the sheet"]
];
// What moves a speaker's group: a lock in the group holds these.
const GROUP_KEYS = ["play_pause", "previous", "next", "shuffle", "repeat"];
// A choice of this many options or fewer, each this short, shows them all
// in a row, as the page's segments; a longer one opens a list.
const SEGMENTS = 4;
const SEGMENT_LABEL = 12;

// A transport key's caption: its glyph, Play and Pause with their word.
function caption(kit, entry) {
  if (entry.id === "shuffle") return "⤮";
  if (entry.id === "repeat") return entry.mode === "one" ? "↻1" : "↻";
  if (entry.icon === "play") return kit.GLYPHS.play + " Play";
  if (entry.icon === "pause") return kit.GLYPHS.pause + " Pause";
  return kit.GLYPHS[entry.icon] || entry.label;
}

// Keys [{text, style, hit}] in rows centered in w, wrapped where they
// would not fit, a row apart; returns the row after them.
function keyRows(g, kit, y, w, keys) {
  if (!keys.length) return y;
  const sizes = keys.map((entry) => Math.min(w - 2, length(entry.text) + 4));
  const gap = sizes.reduce((sum, size) => sum + size, 0) + (keys.length - 1) * 2 <= w - 2 ? 2 : 1;
  const rows = [[]];
  let used = 0;
  keys.forEach((entry, index) => {
    if (rows[rows.length - 1].length && used + gap + sizes[index] > w - 2) { rows.push([]); used = 0; }
    used += (rows[rows.length - 1].length ? gap : 0) + sizes[index];
    rows[rows.length - 1].push(index);
  });
  rows.forEach((row, at) => {
    let x = Math.max(0, Math.floor((w - row.reduce((sum, index) => sum + sizes[index], 0) - (row.length - 1) * gap) / 2));
    row.forEach((index) => { x = kit.rounded(g, x, y + at * 4, keys[index].text, sizes[index], keys[index].style, keys[index].hit) + gap; });
  });
  return y + rows.length * 4 - 1;
}

function keyStyle(ctx, kit, key, options) {
  if (ctx.ui.pressed[key]) return "key.pressed";
  if (kit.focused(ctx, key)) return "key.focus";
  if (options.off) return "key.off";
  if (options.pending) return "bubble.switching";
  if (options.lit) return "key.lit";
  return options.ok ? "key.ok" : "key";
}

// A level on one row: label, − track +, its value; returns the row after it.
function level(g, ctx, kit, x, y, w, label, kind, value, held, muted) {
  const focus = kit.focused(ctx, kind), text = value.disabled ? "–" : value.text;
  const step = (at, glyph, delta) => g.put(at, y, glyph, value.disabled || held ? "faint" : "hotkey",
    { kind: kind + "-step", step: delta, key: kind + ":" + delta, label: label + (delta < 0 ? " down" : " up") });
  let at = g.put(x, y, label, "base") + 1;
  at = step(at, kit.GLYPHS.minus, -1) + 1;
  const track = Math.max(4, Math.min(32, w - at - 4 - length(text)));
  kit.meter(g, at, y, track, { disabled: value.disabled, fill: value.fill, muted: Boolean(muted) }, focus);
  g.hit(at, y, track, 1, { kind, key: kind, focus: true, min: value.min, max: value.max, step: value.step,
    label: label + ": " + value.text + "  (← → when focused)" });
  at = step(at + track + 1, "+", 1);
  g.put(at + 1, y, text, value.pending ? "tone.busy+bold" : focus ? "meter.value.focus" : "meter.value");
  return y + 1;
}

// ---- the speakers' bubbles ---------------------------------------------------

// The room's speakers in a row, the chosen one marked, each lit while it
// plays; the chosen one's padlock while its room is locked. A row too wide
// starts at a bubble that keeps the focused one, else the chosen one, in
// sight, with Watch's ‹ › where more are hidden.
function speakers(g, w, ctx, kit) {
  const listen = ctx.listen, view = listen.view(), list = listen.speakers();
  const width = (speaker) => (speaker.digit ? 1 + length(speaker.digit) : 0) + 4 + length(speaker.name);
  const total = list.reduce((sum, speaker) => sum + width(speaker) + 1, 0) - 1;
  const row = new Grid(Math.max(1, total), 1, "base"), starts = [], ends = [];
  let x = 0, reveal = null;
  list.forEach((speaker) => {
    starts.push(x);
    ends.push(x + width(speaker));
    if (kit.focused(ctx, "speaker:" + speaker.id) || !reveal && speaker.current) reveal = { from: x, to: x + width(speaker) };
    x = bubble(row, x, ctx, kit, view, speaker) + 1;
  });
  const window = w - 2, over = total > window;
  let offset = 0;
  if (over && reveal) {
    // The first bubble's start that keeps it in sight, the end of the row left blank rather than cut.
    offset = starts.find((at) => at >= reveal.to - window && at <= reveal.from);
    if (offset === undefined) offset = reveal.from;
  }
  // Only whole bubbles: one the edge would cut is left to the arrow (one alone wider than the row is cut).
  const whole = Math.max(offset, ...ends.filter((end, index) => starts[index] >= offset && end <= offset + window)) - offset;
  const shown = over && whole > 0 ? whole : window;
  g.order.push.apply(g.order, row.order);
  g.blit(kit.slice(row, offset, shown), 0, 1, 0, shown, 1);
  if (over) {
    g.put(0, 0, kit.ARROW.left, offset > 0 ? "hotkey" : "faint");
    g.put(w - 1, 0, kit.ARROW.right, offset + shown < total ? "hotkey" : "faint");
  }
  return 1;
}

// One speaker's bubble at x; returns the column after it.
function bubble(g, x, ctx, kit, view, speaker) {
  const key = "speaker:" + speaker.id, lock = speaker.current && view && view.locked;
  const led = kit.lightOf(ctx, speaker.playing ? "ok" : "off");
  const rest = " " + (lock ? LOCK : kit.ledChar(led)) + " " + speaker.name + " ";
  const style = kit.focused(ctx, key) ? "bubble.focus" : ctx.ui.pressed[key] ? "bubble.switching" : speaker.current ? "bubble.current" : "bubble";
  const hit = { kind: "listen-speaker", speaker: speaker.id, key, focus: true,
    label: speaker.label + (speaker.playing ? ", playing" : "") + (speaker.current ? ", chosen" : "") + (lock ? ". " + view.locked.text : "")
      + (speaker.digit ? "  (" + speaker.digit + ")" : "") };
  const at = speaker.digit ? g.put(x, 0, " " + speaker.digit, style === "bubble" ? "bubble.digit" : style, hit) : x;
  g.put(at, 0, rest, style, hit);
  if (!lock && style !== "bubble.focus") g.put(at + 1, 0, kit.ledChar(led), style + "+" + kit.ledStyle(led));
  return at + length(rest);
}

// ---- the face -----------------------------------------------------------------

// The chosen speaker in the remote pane; returns the row after it.
function face(g, w, ctx, kit) {
  const listen = ctx.listen, view = listen.view();
  let y = 0;
  const say = (text, style) => { y = kit.paragraph(g, 2, y, w - 4, text, style); };
  if (!view) {
    say("Reading…", "dim");
    return y;
  }
  // Its own room's lock holds every control; a member's, what moves the group.
  const locked = Boolean(view.locked), moves = locked || Boolean(view.groupLocked);
  // As Watch's: the lock in words, then what is wrong.
  if (view.locked) say("Read-only · " + view.locked.text, "dim");
  else if (view.groupLocked) say(view.groupLocked.text, "dim");
  if (!kit.heard(ctx) || view.connected === false) say("⚠ " + ctx.Session.NOT_CONNECTED, "alert");
  if (view.problem) say("⚠ " + view.speaker.label + " " + view.problem, "alert");
  if (view.music) say("⚠ " + view.music, "alert");
  // Its name where no bubble says it.
  if (view.speakers.length < 2) {
    const model = view.speaker.model && !view.speaker.label.includes(view.speaker.model) ? " · " + view.speaker.model : "";
    say((locked ? LOCK + " " : "") + view.speaker.label + model, "dim");
  }
  say(view.now, view.title ? "strong" : "dim");
  if (view.line) say(view.line, "dim");
  if (view.badges.length) {
    let x = 2;
    view.badges.forEach((text) => {
      if (x > 2 && x + length(text) + 2 > w - 2) { y += 1; x = 2; }
      x = g.put(x, y, " " + fit(text, Math.min(length(text), w - 6)) + " ", "badge") + 1;
    });
    y += 1;
  }
  if (view.queueOf) say("Plays " + view.queueOf + "'s queue", "dim");
  const progress = view.progress;
  if (progress) {
    const key = "listen-progress", focus = kit.focused(ctx, key);
    let x = g.put(2, y, progress.at, progress.pending ? "tone.busy" : "dim") + 1;
    const track = Math.max(4, Math.min(48, w - x - length(progress.left) - 3));
    kit.meter(g, x, y, track, { disabled: progress.duration === null || moves, fill: progress.fill, muted: false }, focus);
    g.hit(x, y, track, 1, { kind: key, key, focus: true, min: 0, max: progress.duration || 0,
      label: "Position: " + progress.text + "  (← → 10 s)" });
    if (progress.left) g.put(x + track + 1, y, progress.left, "dim");
    y += 1;
  }
  // Shuffle, previous, play, next, repeat, and a key of the device's own.
  const keys = view.transport.map((entry) => {
    const key = "listen-key:" + entry.id;
    const spoken = { shuffle: " (S)", repeat: " (R)", previous: " (<)", next: " (>)", play_pause: " (P)" }[entry.id] || "";
    return { text: caption(kit, entry), hit: { kind: "listen-key", button: entry.id, key, focus: true, label: entry.label + spoken },
      style: keyStyle(ctx, kit, key, { off: entry.disabled || locked || moves && GROUP_KEYS.includes(entry.id),
        pending: entry.pending, lit: entry.toggle && entry.on, ok: entry.play }) };
  });
  if (keys.length) y = keyRows(g, kit, y + 1, w, keys) + 1;
  // The group, the input and the library, each a sheet.
  const chips = [];
  if (view.canGroup) chips.push([view.group ? view.group.text : "Group", { kind: "listen-group", key: "listen-group", focus: true,
    label: (view.group ? view.group.text + ". " : "") + "Group speakers (G)" }, moves ? "key.off" : "key"]);
  if (view.input) chips.push([view.input.label + " ▾", { kind: "listen-inputs", key: "listen-inputs", focus: true,
    label: "Input: " + view.input.label + " (Enter: choose)" }, locked ? "key.off" : view.input.pending ? "bubble.switching" : "key"]);
  if (view.library) chips.push(["Library", { kind: "listen-library", key: "listen-library", focus: true, label: "Library: search, browse, queue (/)" }, "key"]);
  if (chips.length) {
    const room = Math.max(6, Math.floor((w - 4) / chips.length) - 3);
    const texts = chips.map(([text]) => fit(text, Math.min(length(text), room)).trimEnd());
    let x = Math.max(2, Math.floor((w - texts.reduce((sum, text) => sum + length(text) + 3, -1)) / 2));
    chips.forEach(([, hit, style], index) => { x = kit.chip(g, ctx, x, y, texts[index], hit, style) + 1; });
    y += 1;
  }
  const away = listen.music && listen.music.status();
  if (away && away.state === "absent") say(away.text, "dim");
  y = volume(g, w, ctx, kit, y + 1, view);
  if (view.group) y = level(g, ctx, kit, 2, y + 1, w - 2, "Group", "listen-group-volume", view.group.volume, moves) + 1;
  return y;
}

// Volume as Watch's: the rocker or the slider, its caption, and Mute.
function volume(g, w, ctx, kit, y, view) {
  const value = view.volume, locked = Boolean(view.locked);
  const name = view.speaker.name;
  const step = (x, id, glyph) => {
    const key = "listen-key:" + id;
    return kit.rounded(g, x, y, glyph, 5, keyStyle(ctx, kit, key, { off: locked || !value || value.disabled }),
      { kind: "listen-key", button: id, key, focus: true, repeat: true, label: (id === "volume_up" ? "Volume up (+)" : "Volume down (−)") + " → " + name });
  };
  if (value && ctx.session.volumeMode() === "slider") {
    const width = Math.min(w, 44, Math.max(16, w - 4)), left = Math.floor((w - width) / 2);
    step(left, "volume_down", kit.GLYPHS.minus);
    step(left + width - 5, "volume_up", "+");
    const x = left + 6, text = value.disabled ? "–" : view.muted ? "muted" : value.text;
    const compact = w < 30, room = compact ? 0 : Math.max(6, length(text));
    const track = compact ? width - 12 : Math.max(4, width - 13 - room), focus = kit.focused(ctx, "listen-volume");
    kit.meter(g, x, y + 1, track, { disabled: value.disabled, fill: value.fill, muted: view.muted }, focus);
    g.hit(x, y + 1, track, 1, { kind: "listen-volume", key: "listen-volume", focus: true, min: value.min, max: value.max, step: value.step,
      label: "Volume, " + name + ": " + value.text + "  (← → when focused, + −)" });
    if (!compact) g.put(x + track + 1, y + 1, text, view.muted ? "tone.busy+bold" : value.pending ? "tone.busy+bold" : focus ? "meter.value.focus" : "meter.value");
  } else if (value) {
    const span = Math.min(19, w), x = Math.floor((w - span) / 2);
    step(x, "volume_down", kit.GLYPHS.minus);
    if (span >= 15) g.put(Math.floor((w - 3) / 2), y + 1, "VOL", "meter.label");
    step(x + span - 5, "volume_up", "+");
  }
  const said = name + (value ? " · " + value.text : " · no volume") + (view.muted ? " · muted" : "");
  const text = fit(said, Math.min(w - 2, length(said)));
  const at = value ? y + 3 : y;
  g.put(Math.max(1, Math.floor((w - length(text)) / 2)), at, text, view.muted ? "tone.busy" : "dim");
  if (view.muted === null) return at + 1;
  const key = "listen-key:mute";
  const size = 8, x = Math.floor((w - size) / 2);
  kit.rounded(g, x, at + 2, "Mute", size, keyStyle(ctx, kit, key, { off: locked, lit: view.muted }),
    { kind: "listen-key", button: "mute", key, focus: true, label: "Mute: " + (view.muted ? "on" : "off") + " (M)" });
  return at + 5;
}

// ---- Info: the speaker's power, settings and facts ----------------------------

// In audio.js's order; a room's lock greys them, and the session refuses them with a toast.
function info(g, w, ctx, kit) {
  const view = ctx.listen.view();
  if (!view) return kit.paragraph(g, 1, 0, w - 2, "Reading the speaker…", "dim");
  const locked = Boolean(view.locked);
  let y = kit.heading(g, 0, view.speaker.label);
  if (view.speaker.model && !view.speaker.label.includes(view.speaker.model)) y = kit.paragraph(g, 1, y, w - 2, view.speaker.model, "dim");
  if (locked) y = kit.paragraph(g, 1, y, w - 2, LOCK + " " + view.locked.text, "dim");
  // Each control after its label, or under it where the panel is narrow.
  const labels = ["Power"].concat(...view.sections.map((section) => section.settings.map((setting) => setting.label)),
    view.facts.map((fact) => fact.label));
  const column = Math.min(24, Math.max(...labels.map(length))) + 3;
  const narrow = w - column < 24;
  const field = (label, style) => {
    g.put(1, y, fit(label, narrow ? w - 2 : column - 2), style || "base");
    if (!narrow) return column;
    y += 1;
    return 3;
  };
  // Options in a row, the current one lit: a short choice.
  const choices = (item, options, pending, from) => {
    let at = from;
    options.forEach((option) => {
      const key = "audio-option:" + item + ":" + option.value, text = " " + (option.current ? kit.LIT : kit.UNLIT) + " " + option.label + " ";
      if (at > from && at + length(text) > w - 1) { at = from; y += 1; }
      const style = kit.focused(ctx, key) ? "bubble.focus" : locked ? "bubble.inert" : option.current ? pending ? "bubble.switching" : "bubble.current" : "bubble";
      at = g.put(at, y, text, style, { kind: "audio-option", item, value: option.value, key, focus: true, label: option.label + (option.current ? ", chosen" : "") }) + 1;
    });
  };
  if (view.power !== null) {
    y += 1;
    kit.chip(g, ctx, field("Power"), y, view.power ? "on" : "off", { kind: "audio-power", key: "audio-power", focus: true,
      label: "Power: " + (view.power ? "on" : "off") }, locked ? "key.off" : view.power ? "switch.on" : "switch.off");
    y += 1;
  }
  view.sections.forEach((section) => {
    y = kit.heading(g, y + 1, section.title);
    section.settings.forEach((setting) => {
      const key = "audio-set:" + setting.item, item = setting.item;
      const x = field(setting.label);
      if (setting.kind === "switch") {
        kit.chip(g, ctx, x, y, setting.on ? "on" : "off", { kind: "audio-switch", item, key, focus: true, label: setting.label + ": " + setting.text },
          locked ? "key.off" : setting.pending ? "bubble.switching" : setting.on ? "switch.on" : "switch.off");
      } else if (setting.kind === "number") {
        // − value +: ← and → step it while it has the focus; a click on a sign steps it too.
        const text = " " + setting.text + " ", style = kit.focused(ctx, key) ? "key.focus" : locked ? "key.off" : setting.pending ? "bubble.switching" : "key";
        const sign = (glyph, off) => [" " + glyph + " ", kit.focused(ctx, key) ? style : off || locked ? "key.off" : "key"];
        const end = g.line(x, y, [sign(kit.GLYPHS.minus, setting.atMin), [text, style], sign("+", setting.atMax)]);
        g.hit(x, y, end - x, 1, { kind: "audio-level", item, key, focus: true, label: setting.label + ": " + setting.text + "  (← →)" });
        g.hit(x, y, 3, 1, { kind: "audio-step", item, step: -1, key: key + ":-1", label: setting.label + " down" });
        g.hit(end - 3, y, 3, 1, { kind: "audio-step", item, step: 1, key: key + ":1", label: setting.label + " up" });
      } else if (setting.options.length <= SEGMENTS && setting.options.every((option) => length(option.label) <= SEGMENT_LABEL)) {
        choices(item, setting.options, setting.pending, x);
      } else {
        kit.chip(g, ctx, x, y, setting.text + " ▾", { kind: "audio-choose", item, value: setting.value, title: setting.label, key, focus: true,
          label: setting.label + ": " + setting.text + " (Enter: choose)" }, locked ? "key.off" : setting.pending ? "bubble.switching" : "key");
      }
      y += 1;
    });
  });
  if (view.facts.length) {
    y = kit.heading(g, y + 1, "About this device");
    view.facts.forEach((fact) => {
      const x = field(fact.label, "dim");
      y = kit.paragraph(g, x, y, w - x - 1, fact.text, "base");
    });
  }
  return y;
}

// A list of options, the current one lit: the inputs, or a long setting's
// choices (a value the device only reports, offered by none, above them).
function optionRows(g, w, ctx, kit, y, kind, item, options) {
  options.forEach((option) => {
    const key = kind + ":" + (item ? item + ":" : "") + option.value;
    const style = kit.focused(ctx, key) ? "row.focus" : option.current ? "row.current" : "row";
    g.fill(0, y, w, 1, style);
    g.put(1, y, option.current ? kit.LIT : kit.UNLIT, style === "row.focus" ? style : style + "+" + (option.current ? "led.ok" : "led.off"));
    g.put(3, y, fit(option.label, w - 4), style);
    g.hit(0, y, w, 1, { kind, item, value: option.value, key, focus: true, label: option.label + (option.current ? ", chosen" : "") });
    y += 1;
  });
  return y;
}

function choiceSheet(g, w, ctx, kit) {
  const view = ctx.listen.view(), item = ctx.ui.overlay.item;
  const setting = view ? [].concat(...view.sections.map((section) => section.settings)).find((entry) => entry.item === item) : null;
  if (!setting || !setting.options) return g.put(1, 0, "Nothing to choose", "dim");
  const y = setting.options.some((option) => option.current) ? 0 : kit.paragraph(g, 1, 0, w - 2, "Now " + setting.text, "dim");
  return optionRows(g, w, ctx, kit, y, "audio-option", item, setting.options);
}

function inputSheet(g, w, ctx, kit) {
  const view = ctx.listen.view();
  if (!view || !view.input) return g.put(1, 0, "No inputs to choose", "dim");
  return optionRows(g, w, ctx, kit, 0, "audio-input", null, view.input.options);
}

// ---- the library --------------------------------------------------------------

// An Admin's TIDAL sign-in: the code to approve at link.tidal.com, followed until it is done.
function tidalBlock(g, w, ctx, kit, y, tidal) {
  y = kit.heading(g, y, "TIDAL");
  if (tidal.text) y = kit.paragraph(g, 1, y, w - 2, tidal.text, tidal.state === "failed" ? "alert" : "base");
  const waiting = tidal.state === "waiting";
  if (waiting && tidal.code) {
    g.line(1, y, [["Code  ", "dim"], [tidal.code, "strong"]]);
    y += 1;
    if (tidal.url) y = kit.paragraph(g, 1, y, w - 2, tidal.url, "link");
  }
  let x = 1;
  if (!waiting) {
    const label = tidal.state === "failed" ? "Try again" : tidal.signedIn ? "Sign in again" : "Sign in to TIDAL";
    x = kit.chip(g, ctx, x, y, label, { kind: "music-tidal-start", key: "music-tidal-start", focus: true, label: label }, tidal.pending ? "key.off" : "key") + 1;
  }
  if (waiting || tidal.state === "failed") {
    const label = waiting ? "Cancel" : "Close";
    kit.chip(g, ctx, x, y, label, { kind: "music-tidal-cancel", key: "music-tidal-cancel", focus: true, label: label + " the TIDAL sign-in" }, "key");
  }
  return y + 2;
}

// Options in a row, the current one lit (the tabs, a search's kind and source).
function choices(g, w, ctx, kit, y, from, options, kind) {
  let at = from;
  options.forEach((option) => {
    const key = kind + ":" + option.value, text = " " + (option.current ? kit.LIT : kit.UNLIT) + " " + option.label + " ";
    if (at > from && at + length(text) > w - 1) { at = from; y += 1; }
    const style = kit.focused(ctx, key) ? "bubble.focus" : option.current ? "bubble.current" : "bubble";
    at = g.put(at, y, text, style, { kind, value: option.value, key, focus: true, label: option.label + (option.current ? ", chosen" : "") }) + 1;
  });
  return y + 1;
}

// The library sheet: TIDAL's sign-in for an Admin, then Search, Browse or Queue.
function librarySheet(g, w, ctx, kit) {
  const music = ctx.listen.music;
  let y = 0;
  const library = music && music.library();
  if (!library) {
    const status = music ? music.status() : null;
    return kit.paragraph(g, 1, y, w - 2, status && status.text || "Music Assistant does not play this speaker now.", "dim");
  }
  const tidal = music.tidal();
  if (tidal) y = tidalBlock(g, w, ctx, kit, y, tidal) + 1;
  y = choices(g, w, ctx, kit, y, 1, library.tabs.map((tab) => ({ value: tab.id, label: tab.label, current: tab.current })), "music-tab");
  if (library.blind) return kit.paragraph(g, 1, y + 1, w - 2, library.blind, "dim");
  y += 1;
  const say = (text, style) => { if (text) y = kit.paragraph(g, 1, y, w - 2, text, style || "dim"); };
  if (library.search) {
    const search = library.search, key = "music-search", focus = kit.focused(ctx, key);
    const width = Math.max(8, w - 10);
    const shown = search.query + (focus ? "▏" : "");
    g.put(1, y, "Search", "base");
    g.put(8, y, fit(length(shown) > width ? "…" + ending(shown, width - 1) : shown || (focus ? "" : "type / to search"), width),
      focus ? "field.focus" : search.query ? "field" : "field+dim",
      { kind: "music-search", key, focus: true, label: "Search: " + (search.query || "empty") + "  (type; Enter searches)" });
    y += 1;
    y = choices(g, w, ctx, kit, y, 1, search.kinds, "music-kind");
    y = choices(g, w, ctx, kit, y, 1, search.sources, "music-source");
    say(search.text);
    search.sections.forEach((section) => {
      y = kit.heading(g, y + 1, section.title);
      section.items.forEach((item, index) => { y = itemRow(g, w, ctx, y, item, "search:" + section.kind + ":" + index); });
      if (section.more) {
        kit.chip(g, ctx, 1, y, "All " + section.total + " " + section.title.toLowerCase(),
          { kind: "music-all", value: section.kind, key: "music-all:" + section.kind, focus: true, label: "All " + section.total + " " + section.title.toLowerCase() }, "key");
        y += 1;
      }
    });
  } else if (library.page) {
    y = pageBlock(g, w, ctx, kit, y, library.page);
  } else if (library.queue) {
    const queue = library.queue;
    if (queue.owner) say("The queue of " + queue.owner + "'s group");
    say(queue.text);
    queue.items.forEach((item, index) => { y = itemRow(g, w, ctx, y, item, "queue:" + index); });
    if (queue.more) { kit.chip(g, ctx, 1, y, "More", { kind: "music-more", key: "music-more", focus: true, label: "More of the queue" }, "key"); y += 1; }
  }
  return y;
}

// A page: Back to what it opened from, and the title of one without a
// header; an album's, artist's or playlist's header (its name, artists to
// open, what it is, then Play, Shuffle and More); an artist's sections, a
// few of each and All the rest; and its items, an album's by number.
function pageBlock(g, w, ctx, kit, y, page) {
  const music = ctx.listen.music;
  const say = (text, style) => { if (text) y = kit.paragraph(g, 1, y, w - 2, text, style || "dim"); };
  if (page.back) {
    const back = "‹ " + fit(page.back, Math.max(4, Math.floor(w / 2) - 4));
    const x = kit.chip(g, ctx, 1, y, back, { kind: "music-back", key: "music-back", focus: true, label: "Back to " + page.back + " (Backspace)" }, "key") + 1;
    if (!page.header) g.put(x + 1, y, fit(page.name, Math.max(4, w - x - 2)), "strong");
    y += 2;
  }
  const head = page.header;
  if (head) {
    g.put(1, y, fit(head.name, w - 2), "strong");
    y += 1;
    if (head.artists.length) {
      let x = 1;
      head.artists.forEach((artist, index) => {
        if (index) x = g.put(x, y, ",", "dim");
        x = artist.ref ? kit.chip(g, ctx, x, y, artist.name, { kind: "music-artist", item: artist, key: "music-artist:" + index, focus: true,
          label: "Go to " + artist.name }, "link") : g.put(x + 1, y, artist.name, "base") + 1;
      });
      y += 1;
    }
    const line = [head.line, head.source].filter(Boolean).join(" · ");
    if (line) g.put(1, y, fit(line, w - 2), "dim");
    y += 2;
    const plays = music.actions(head.item).some((action) => action.id === "next" && !action.disabled);
    let x = 1;
    [["replace", kit.GLYPHS.play + " Play", "Play "], ["shuffle", "⤮ Shuffle", "Shuffle "], ["more", "⋯ More", "More for "]].forEach(([action, text, said]) => {
      const off = action !== "more" && !plays;
      x = kit.chip(g, ctx, x, y, text, { kind: "music-head", action, item: head.item, key: "music-head:" + action, focus: true, disabled: off,
        label: said + head.name }, off ? "key.off" : "key") + 1;
    });
    y += 2;
  }
  say(page.text);
  page.sections.forEach((section) => {
    y = kit.heading(g, y, section.title);
    if (section.text) say(section.text, "alert");
    section.items.forEach((item, index) => { y = itemRow(g, w, ctx, y, item, "section:" + section.id + ":" + index); });
    if (section.more) {
      const label = "All " + section.total + " " + section.title.toLowerCase();
      kit.chip(g, ctx, 1, y, label, { kind: "music-section", value: section.id, key: "music-section:" + section.id, focus: true, label: label }, "key");
      y += 1;
    }
    y += 1;
  });
  page.items.forEach((item, index) => { y = itemRow(g, w, ctx, y, item, "page:" + index, page.numbered); });
  if (page.more) { kit.chip(g, ctx, 1, y, "More", { kind: "music-more", key: "music-more", focus: true, label: "More of " + page.name }, "key"); y += 1; }
  return y;
}

// An entry: its name and line, its source and length; what opens, an
// arrow; an album's track, its number first.
function itemRow(g, w, ctx, y, item, id, numbered) {
  const key = "music-item:" + id;
  const focus = ctx.ui.focusVisible && ctx.ui.focus === key;
  const style = focus ? "row.focus" : item.current ? "row.current" : "row";
  g.fill(0, y, w, 1, style);
  if (item.current) g.put(0, y, "▎", "row.mark");
  const right = [item.source, item.duration].filter(Boolean).join("  ") + (item.opens ? " ›" : "");
  const room = Math.max(4, w - 4 - length(right));
  const from = numbered ? g.put(2, y, String(item.number || "").padStart(2) + "  ", style === "row" ? "dim" : style) : 2;
  let at = g.put(from, y, fit(item.name, Math.max(2, Math.min(room - (from - 2), length(item.name)))), style + (item.current ? "+bold" : ""));
  if (item.line && room - (at - 2) > 6) g.put(at, y, fit(" · " + item.line, room - (at - 2)), style === "row" ? "dim" : style);
  if (right) g.put(w - 1 - length(right), y, right, style === "row" ? "dim" : style);
  g.hit(0, y, w, 1, { kind: "music-item", item, key, focus: true,
    label: [numbered && item.number ? "Track " + item.number : null, item.name, item.current ? "playing now" : null, item.line, item.source,
      item.duration].filter(Boolean).join(", ") + (item.opens ? "  (Enter: open)" : item.plays ? "  (Enter: play)" : "") });
  return y + 1;
}

// The sheet an item opens: Open, Play from here, Play now, Play next, Add
// to queue, and where it leads (its album, its artists).
function itemSheet(g, w, ctx, kit) {
  const music = ctx.listen.music;
  const item = ctx.ui.overlay.item;
  if (!music || !item) return g.put(1, 0, "Nothing to play", "dim");
  let y = kit.paragraph(g, 1, 0, w - 2, [item.line, item.source, item.duration].filter(Boolean).join(" · ") || item.name, "dim") + 1;
  music.actions(item).forEach((action) => {
    const key = "music-action:" + action.id;
    kit.button(g, ctx, 1, y, action.label, key, { kind: "music-action", action: action.id, key, focus: true, disabled: action.disabled,
      label: action.id.startsWith("go:") ? action.label : action.label + " " + item.name },
    action.disabled ? "key.off" : "key");
    y += 3;
  });
  const view = music.view();
  if (view) y = kit.paragraph(g, 1, y, w - 2, "On " + view.speaker.name + (view.queueOf ? ", in " + view.queueOf + "'s group" : "")
    + ". Play now replaces the queue.", "dim");
  return y;
}

// The group sheet: who is in it (each removed, or the group ended) and its
// volume, who may join, and what Music Assistant answered.
function groupSheet(g, w, ctx, kit) {
  const music = ctx.listen.music;
  const grouping = music && music.grouping();
  if (!grouping) return g.put(1, 0, "Nothing to group", "dim");
  const picked = ctx.music.picked;
  let y = kit.paragraph(g, 1, 0, w - 2, "Speakers you add play what the group plays.", "dim");
  if (grouping.grouped) {
    y = kit.heading(g, y + 1, "In the group");
    grouping.members.forEach((member) => {
      const action = member.leader ? "End" : "Remove";
      g.put(2, y, fit(member.name + (member.leader ? " · leads" : ""), Math.max(4, w - 14)), "base");
      kit.chip(g, ctx, w - 3 - length(action), y, action, { kind: "music-leave", speaker: member.id, key: "music-leave:" + member.id, focus: true,
        label: (member.leader ? "End the group led by " : "Remove ") + member.name }, "key");
      y += 1;
    });
    const view = music.view();
    if (grouping.volume) y = level(g, ctx, kit, 2, y, w - 2, "Volume", "listen-group-volume", grouping.volume, view && view.groupLocked);
  }
  y = kit.heading(g, y + 1, "Add");
  grouping.candidates.forEach((candidate) => {
    const key = "music-pick:" + candidate.id;
    const on = picked.has(candidate.id);
    const style = kit.focused(ctx, key) ? "row.focus" : "row";
    g.fill(0, y, w, 1, style);
    g.put(2, y, on ? "[x]" : "[ ]", candidate.available ? style : "faint");
    g.put(6, y, fit(candidate.name, Math.max(4, w - 8 - length(candidate.note))), candidate.available ? style : "faint");
    if (candidate.note) g.put(w - 1 - length(candidate.note), y, candidate.note, style === "row" ? "dim" : style);
    g.hit(0, y, w, 1, { kind: "music-pick", speaker: candidate.id, key, focus: true, disabled: !candidate.available,
      label: candidate.name + (candidate.note ? ", " + candidate.note : "") + (on ? ", chosen" : "") });
    y += 1;
  });
  y += 1;
  const join = grouping.pending ? "Joining…" : picked.size ? "Join " + picked.size : "Join";
  kit.button(g, ctx, 1, y, join, "music-join", { kind: "music-join", key: "music-join", focus: true, label: join }, grouping.pending || !picked.size ? "key.off" : "key");
  y += 3;
  if (grouping.result) {
    if (grouping.result.text) y = kit.paragraph(g, 1, y, w - 2, grouping.result.text, "base");
    if (grouping.result.waiting) y = kit.paragraph(g, 1, y, w - 2, grouping.result.waiting, "base");
    grouping.result.refused.forEach((entry) => { y = kit.paragraph(g, 1, y, w - 2, entry.reason, "alert"); });
  }
  if (grouping.grouped) {
    kit.button(g, ctx, 1, y + 1, "Ungroup all", "music-end", { kind: "music-end", key: "music-end", focus: true, label: "Ungroup all" }, "key");
    y += 4;
  }
  return y;
}

module.exports = { SHORTCUTS, speakers, face, info, choiceSheet, inputSheet, librarySheet, itemSheet, groupSheet };
