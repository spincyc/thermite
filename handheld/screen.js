/*
 * What a handheld remote draws, from its RemoteSession (handheld/PROTOCOL.md,
 * Screen and Audio): the room, its bubbles and the remote on show, placed as
 * the terminal remote places them (tui/remote/view.js layout() and lower()).
 * screen() returns the model's fields without its id and the id is the first
 * 16 hex digits of the SHA-256 of their JSON, so a restarted bridge computes
 * the id a remote already shows. Nothing here does I/O.
 */
"use strict";

const crypto = require("crypto");

// Every remote has the volume keys and Mute; they drive the room's audio.
const ALWAYS = ["volume_up", "volume_down", "mute"];
const CHANNEL = ["channel_up", "channel_down"];
// The D-pad's own keys: a remote needs their specs (repeat) for its hard keys.
const PAD = ["up", "down", "left", "right", "ok"];
// Up to this many playback keys sit beside Mute when the remote has a D-pad.
const BESIDE = 3;

// A button's spec with only the fields it has: an absent icon or text is none
// and an absent repeat is false, which keeps a screen to fewer radio frames.
function spec(layouts, id) {
  const button = layouts.buttons[id] || {};
  const out = { label: button.label || id };
  if (button.icon) out.icon = button.icon;
  if (button.text) out.text = button.text;
  if (button.repeat === true) out.repeat = true;
  return out;
}

function row(label, keys, columns) {
  return { label: label || "", keys: keys.slice(), columns: columns || 0 };
}

// The rows in order: top; playback when it is long or there is no D-pad;
// then the key groups (primary ones first, as the terminal draws them) or
// one unlabelled row of keys.
function rowsOf(remote) {
  const rows = [];
  if (remote.top && remote.top.length) rows.push(row("", remote.top, 0));
  const playback = remote.playback || [];
  if (playback.length && (!remote.corners || playback.length > BESIDE)) rows.push(row("", playback, 0));
  if (remote.keyGroups) {
    const groups = remote.keyGroups.filter((group) => group.keys && group.keys.length);
    groups.filter((group) => group.primary).concat(groups.filter((group) => !group.primary))
      .forEach((group) => rows.push(row(group.label, group.keys, group.columns)));
  } else if (remote.keys && remote.keys.length) {
    rows.push(row("", remote.keys, 0));
  }
  return rows;
}

function besideOf(remote) {
  const playback = remote.playback || [];
  return remote.corners && playback.length <= BESIDE ? playback.slice() : [];
}

/* The screen's fields but its id, for the session's current room. */
function screen(session, layouts) {
  const current = session.current();
  const list = session.controls();
  const state = session.state(current.id);
  const shown = session.view(current, state);
  const target = session.activeTarget(list);
  const hidden = session.remoteHidden(list);
  const remote = hidden || !target ? null : session.remoteOf(target) || null;
  const keys = remote && !remote.note;
  const pad = keys && remote.corners ? { corners: [0, 1, 2, 3].map((at) => remote.corners[at] || null) } : null;
  const rows = keys ? rowsOf(remote) : [];
  const beside = keys ? besideOf(remote) : [];
  const channel = Boolean(keys && remote.channel);
  const ids = [];
  if (pad) ids.push(...pad.corners.filter(Boolean), ...PAD);
  rows.forEach((entry) => ids.push(...entry.keys));
  ids.push(...beside, ...ALWAYS);
  if (channel) ids.push(...CHANNEL);
  const buttons = {};
  ids.forEach((id) => { if (!buttons[id]) buttons[id] = spec(layouts, id); });
  return {
    room: current.id,
    label: current.label,
    rooms: session.rooms().map((room) => {
      const view = session.view(room, session.state(room.id));
      return { id: room.id, label: room.label, led: view.led, short: view.short };
    }),
    led: shown.led,
    short: shown.short,
    problem: shown.problem || "",
    notice: session.lockNotice() || "",
    locked: session.roomLocked(current.id).locked === true,
    bubbles: session.bubbles(list).map((item) => {
      const bubbleState = session.bubbleState(item, state);
      const lock = session.bubbleLock(item, bubbleState);
      return { id: item.id, label: item.label, state: bubbleState, lock: lock.locked === true, inert: lock.inert === true };
    }),
    selected: session.selectedBubble(list) || null,
    hidden: hidden,
    remote: remote ? { label: remote.label, note: remote.note ? remote.note.replace("{device}", target.label) : "" } : null,
    pad: pad,
    rows: rows,
    beside: beside,
    channel: channel,
    buttons: buttons,
    allOff: session.allOffArmed()
  };
}

/* The volume line, or null while the screen is hidden. */
function audio(session, hidden) {
  if (hidden) return null;
  const view = session.audioView();
  return {
    short: view.short, value: view.value, min: view.min, max: view.max, step: view.step,
    muted: view.muted, disabled: view.disabled, text: view.text, caption: view.caption
  };
}

/* The id of a screen whose other fields serialize to json. */
function digest(json) {
  return crypto.createHash("sha256").update(json).digest("hex").slice(0, 16);
}

module.exports = { screen, audio, digest, rowsOf, besideOf, ALWAYS, CHANNEL, PAD, BESIDE };
