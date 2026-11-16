/*
 * The terminal's Music screen (y from Remotes): the Music page's session
 * (web/remote/music-session.js) drawn in music.js's order, without covers:
 * the speakers, a group's together; what the chosen one plays, its
 * transport, volume and group; TIDAL's sign-in for an Admin; and the
 * library's Search, Browse and Queue. view.js hands it its drawing kit
 * (headings, rounded keys, the meter) so a key here looks as Audio's;
 * controller.js turns keys and clicks into the session's calls.
 */
"use strict";

const { LOCK, fit, length } = require("./grid");

const SHORTCUTS = [
  [["↑", "↓"], "Choose a control"],
  [["Enter", "Space"], "Press it, open an item, or offer how to play it"],
  [["←", "→"], "Step the volume, or move 10 s on the position"],
  [["P", "Space"], "Play/pause (Space off a button)"],
  [["+", "−"], "Volume"],
  [["<", ">"], "Previous/next track"],
  [["S"], "Shuffle"],
  [["R"], "Repeat: off, all, one"],
  [["G"], "Group speakers"],
  [["[", "]"], "Previous/next speaker"],
  [["/"], "Search: type, Enter searches, Esc clears"],
  [["Backspace"], "Back a folder (Browse)"],
  [["Esc"], "Remotes"]
];

// The Music screen; returns the row after it.
function content(g, w, ctx, kit) {
  const session = ctx.music && ctx.music.session;
  let y = 0;
  const say = (text, style) => { y = kit.paragraph(g, 1, y, w - 2, text, style); };
  if (!session) {
    say("Reading the speakers…", "dim");
    return y;
  }
  if (ctx.ui.target.mock) { say("Demo · invented speakers and music. Nothing plays.", "tone.busy"); y += 1; }
  const status = session.status(), view = session.view();
  const locked = status.state === "ready" && view && view.groupLocked;
  const alert = locked ? LOCK + " " + view.groupLocked.text : status.text;
  if (alert) { say(alert, status.state === "reading" ? "dim" : locked ? "strong" : "alert"); y += 1; }
  if (!["ready", "unavailable"].includes(status.state)) return y;
  y = speakers(g, w, ctx, kit, y, session);
  if (!view) return y;
  y = now(g, w, ctx, kit, y, view);
  const tidal = session.tidal();
  if (tidal) y = tidalBlock(g, w, ctx, kit, y, tidal);
  const library = session.library();
  if (library) y = libraryBlock(g, w, ctx, kit, y, library);
  return y;
}

// One row a speaker, a group's under a line of what it plays.
function speakers(g, w, ctx, kit, y, session) {
  y = kit.heading(g, y, "Speakers");
  session.speakers().forEach((block) => {
    if (block.group) {
      g.put(1, y, fit("╭ Group · " + block.line, w - 2), "dim");
      y += 1;
    }
    block.speakers.forEach((speaker) => {
      const key = "music-speaker:" + speaker.id;
      const style = kit.focused(ctx, key) ? "row.focus" : speaker.current ? "row.current" : "row";
      const led = !speaker.available ? "muted" : speaker.playing ? "ok" : "off";
      const indent = block.group ? 3 : 2;
      g.fill(0, y, w, 1, style);
      if (speaker.current) g.put(0, y, "▎", "row.mark");
      if (block.group) g.put(1, y, "│", style === "row" ? "dim" : style);
      g.put(indent, y, speaker.locked ? LOCK : kit.ledChar(led), style === "row.focus" ? style : style + "+" + kit.ledStyle(led));
      const note = block.group ? (speaker.leader ? "leads" : "") : speaker.line;
      const right = fit(note, Math.max(0, Math.floor((w - indent - 4) / 2))).trimEnd();
      g.put(indent + 2, y, fit(speaker.name, Math.max(4, w - indent - 4 - length(right))), style);
      if (right) g.put(w - 1 - length(right), y, right, style === "row" ? "dim" : style);
      g.hit(0, y, w, 1, { kind: "music-speaker", speaker: speaker.id, key, focus: true,
        label: speaker.name + ", " + speaker.line + (speaker.locked ? ", locked" : "") + "  ([ ])" });
      y += 1;
    });
  });
  return y + 1;
}

// What the chosen speaker plays, where it is in it, its keys, volume and group.
function now(g, w, ctx, kit, y, view) {
  // Its own room's lock holds every control; a member's, those that move the group.
  const locked = Boolean(view.locked), shared = Boolean(view.groupLocked);
  y = kit.heading(g, y, "Now playing");
  const model = view.speaker.name.includes(view.speaker.label) ? view.speaker.provider : view.speaker.label;
  y = kit.paragraph(g, 1, y, w - 2, view.speaker.name + (model ? " · " + model : ""), "dim");
  y = kit.paragraph(g, 1, y, w - 2, view.now, view.title ? "strong" : "dim");
  if (view.line) y = kit.paragraph(g, 1, y, w - 2, view.line, "dim");
  if (view.queueOf) y = kit.paragraph(g, 1, y, w - 2, "Plays " + view.queueOf + "'s queue", "dim");
  const progress = view.progress;
  if (progress) {
    const key = "music-progress", focus = kit.focused(ctx, key);
    let x = g.put(1, y, progress.at, progress.pending ? "tone.busy" : "dim") + 1;
    const track = Math.max(6, Math.min(48, w - x - length(progress.left) - 3));
    kit.meter(g, x, y, track, { disabled: progress.duration === null || shared, fill: progress.fill, muted: false }, focus);
    g.hit(x, y, track, 1, { kind: "music-progress", key, focus: true, min: 0, max: progress.duration || 0,
      label: "Position: " + progress.text + "  (← → 10 s)" });
    x += track + 1;
    if (progress.left) g.put(x, y, progress.left, "dim");
    y += 1;
  }
  // Shuffle, previous, play, next, repeat, wrapped where the panel is narrow.
  const keys = [
    ["music-shuffle", view.shuffle.on ? "Shuffle on" : "Shuffle", view.shuffle.label + " (S)", view.shuffle.on, !view.shuffle.known, view.shuffle.pending],
    ["music-previous", kit.GLYPHS.previous, "Previous track (<)", false, !view.canSkip, false],
    ["music-play", view.playing ? kit.GLYPHS.pause + " Pause" : kit.GLYPHS.play + " Play", view.playLabel + " (P)", false, false, false],
    ["music-next", kit.GLYPHS.next, "Next track (>)", false, !view.canSkip, false],
    ["music-repeat", view.repeat.label, view.repeat.label + " (R)", view.repeat.value !== "off", !view.repeat.known, view.repeat.pending]
  ];
  let x = 1;
  y += 1;
  keys.forEach(([key, label, spoken, lit, off, pending]) => {
    const size = length(label) + 4;
    if (x > 1 && x + size > w - 1) { x = 1; y += 3; }
    const style = ctx.ui.pressed[key] ? "key.pressed" : kit.focused(ctx, key) ? "key.focus" : shared || off ? "key.off"
      : pending ? "bubble.switching" : lit ? "key.lit" : key === "music-play" ? "key.ok" : "key";
    x = kit.rounded(g, x, y, label, size, style, { kind: key, key, focus: true, label: spoken }) + 2;
  });
  y += 4;
  const column = 15;
  const narrow = w - column < 24;
  const field = (label) => {
    g.put(1, y, label, "base");
    if (!narrow) return column;
    y += 1;
    return 3;
  };
  const level = (label, kind, value, muted, held) => {
    const focus = kit.focused(ctx, kind), text = value.disabled ? "–" : value.text;
    const step = (at, glyph, delta) => g.put(at, y, glyph, value.disabled || held ? "faint" : "hotkey",
      { kind: kind + "-step", step: delta, key: kind + ":" + delta, label: label + (delta < 0 ? " down" : " up") });
    let at = step(field(label), kit.GLYPHS.minus, -1) + 1;
    const track = Math.max(6, Math.min(32, w - at - 10 - length(text)));
    kit.meter(g, at, y, track, { disabled: value.disabled, fill: value.fill, muted: muted }, focus);
    g.hit(at, y, track, 1, { kind, key: kind, focus: true, min: value.min, max: value.max,
      label: label + ": " + value.text + "  (← → when focused" + (kind === "music-volume" ? ", + −)" : ")") });
    at = step(at + track + 1, "+", 1);
    at = g.put(at + 1, y, text, value.pending ? "tone.busy+bold" : focus ? "meter.value.focus" : "meter.value");
    if (muted) g.put(at + 1, y, "muted", "tone.busy");
    y += 1;
  };
  level("Volume", "music-volume", view.volume, view.muted, locked);
  x = field("Group");
  x = g.put(x, y, fit(view.group ? view.group.text : "Not grouped", Math.max(4, w - x - 12)).trimEnd(), "base") + 2;
  kit.chip(g, ctx, x, y, "Group…", { kind: "music-group", key: "music-group", focus: true, label: "Group speakers (G)" }, shared ? "key.off" : "key");
  y += 1;
  if (view.group) level("Group volume", "music-group-volume", view.group.volume, false, shared);
  return y + 1;
}

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

function libraryBlock(g, w, ctx, kit, y, library) {
  y = kit.heading(g, y, "Library");
  y = choices(g, w, ctx, kit, y, 1, library.tabs.map((tab) => ({ value: tab.id, label: tab.label, current: tab.current })), "music-tab");
  if (library.blind) return kit.paragraph(g, 1, y + 1, w - 2, library.blind, "dim");
  y += 1;
  const say = (text, style) => { if (text) y = kit.paragraph(g, 1, y, w - 2, text, style || "dim"); };
  if (library.search) {
    const search = library.search, key = "music-search", focus = kit.focused(ctx, key);
    const width = Math.max(8, w - 10);
    const shown = search.query + (focus ? "▏" : "");
    g.put(1, y, "Search", "base");
    g.put(8, y, fit(length(shown) > width ? "…" + Array.from(shown).slice(-(width - 1)).join("") : shown || (focus ? "" : "type / to search"), width),
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
  } else if (library.browse) {
    const browse = library.browse;
    const x = kit.chip(g, ctx, 1, y, "‹ Back", { kind: "music-back", key: "music-back", focus: true, label: "Back a folder (Backspace)" },
      browse.depth ? "key" : "key.off") + 1;
    g.put(x, y, fit(browse.path.join(" › "), Math.max(4, w - x - 1)), "strong");
    y += 1;
    say(browse.text);
    browse.items.forEach((item, index) => { y = itemRow(g, w, ctx, y, item, "browse:" + index); });
    if (browse.more) { kit.chip(g, ctx, 1, y, "More", { kind: "music-more", key: "music-more", focus: true, label: "More of this folder" }, "key"); y += 1; }
  } else if (library.queue) {
    const queue = library.queue;
    if (queue.owner) say("The queue of " + queue.owner + "'s group");
    say(queue.text);
    queue.items.forEach((item, index) => { y = itemRow(g, w, ctx, y, item, "queue:" + index); });
    if (queue.more) { kit.chip(g, ctx, 1, y, "More", { kind: "music-more", key: "music-more", focus: true, label: "More of the queue" }, "key"); y += 1; }
  }
  return y;
}

// An entry: its name and line, its source and length; a folder's arrow.
function itemRow(g, w, ctx, y, item, id) {
  const key = "music-item:" + id;
  const focus = ctx.ui.focusVisible && ctx.ui.focus === key;
  const style = focus ? "row.focus" : item.current ? "row.current" : "row";
  g.fill(0, y, w, 1, style);
  if (item.current) g.put(0, y, "▎", "row.mark");
  const right = [item.source, item.duration].filter(Boolean).join("  ") + (item.opens && !item.plays ? " ›" : "");
  const room = Math.max(4, w - 4 - length(right));
  let at = g.put(2, y, fit(item.name, Math.min(room, length(item.name))), style + (item.current ? "+bold" : ""));
  if (item.line && room - (at - 2) > 6) g.put(at, y, fit(" · " + item.line, room - (at - 2)), style === "row" ? "dim" : style);
  if (right) g.put(w - 1 - length(right), y, right, style === "row" ? "dim" : style);
  g.hit(0, y, w, 1, { kind: "music-item", item, key, focus: true,
    label: [item.name, item.current ? "playing now" : null, item.line, item.source, item.duration].filter(Boolean).join(", ")
      + (item.plays ? "  (Enter: play)" : item.opens ? "  (Enter: open)" : "") });
  return y + 1;
}

// The sheet an item opens: Open, Play now, Play next, Add to queue.
function itemSheet(g, w, ctx, kit) {
  const session = ctx.music && ctx.music.session;
  const item = ctx.ui.overlay.item;
  if (!session || !item) return g.put(1, 0, "Nothing to play", "dim");
  let y = kit.paragraph(g, 1, 0, w - 2, [item.line, item.source, item.duration].filter(Boolean).join(" · ") || item.name, "dim") + 1;
  session.actions(item).forEach((action) => {
    const key = "music-action:" + action.id;
    kit.button(g, ctx, 1, y, action.label, key, { kind: "music-action", action: action.id, key, focus: true, disabled: action.disabled,
      label: action.label + " " + item.name },
      action.disabled ? "key.off" : "key");
    y += 3;
  });
  const view = session.view();
  if (view) y = kit.paragraph(g, 1, y, w - 2, "On " + view.speaker.name + (view.queueOf ? ", in " + view.queueOf + "'s group" : "")
    + ". Play now replaces the queue.", "dim");
  return y;
}

// The group sheet: who is in it (each removed, or the group ended), who
// may join, and what Music Assistant answered.
function groupSheet(g, w, ctx, kit) {
  const session = ctx.music && ctx.music.session;
  const grouping = session && session.grouping();
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

module.exports = { SHORTCUTS, content, itemSheet, groupSheet };
