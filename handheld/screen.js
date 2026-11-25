/*
 * What a handheld remote draws, from its RemoteSession (handheld/PROTOCOL.md,
 * Screen and Audio): the room, its bubbles and the remote on show, placed as
 * the terminal remote places them (tui/remote/view.js layout() and lower()).
 * screen() returns the model's fields without its id and the id is the first
 * 16 hex digits of the SHA-256 of their JSON, so a restarted bridge computes
 * the id a remote already shows. What has failed is the session's to say:
 * its problem line, and, while the house is not heard (connected()), every
 * light and bubble "stale" in place of what was read before. Nothing here
 * does I/O.
 */
"use strict";

const crypto = require("crypto");
const RemoteLayouts = require("../web/remote/remotes");

// Every remote has the volume keys and Mute; they drive the room's audio.
const ALWAYS = ["volume_up", "volume_down", "mute"];
const CHANNEL = ["channel_up", "channel_down"];
// The D-pad's own keys: a remote needs their specs (repeat) for its hard keys.
const PAD = ["up", "down", "left", "right", "ok"];
// Up to this many playback keys sit beside Mute when the remote has a D-pad.
const BESIDE = 3;
// Compact Listen transport bar keys.
const LISTEN_TRANSPORT = ["previous", "play_pause", "next"];
// Bound free text so Listen screens stay within 1-2 ESP-NOW frames.
const TEXT_LIMIT = 48;
const NOTICE_LIMIT = 96;

function clip(value, max) {
  const text = String(value || "").trim();
  if (!text) return "";
  const limit = max || TEXT_LIMIT;
  return text.length > limit ? text.slice(0, limit - 1) + "\u2026" : text;
}

// A button's spec with only the fields it has: an absent icon or text is none
// and an absent repeat is false, which keeps a screen to fewer radio frames.
function spec(layouts, id) {
  const button = ((layouts && layouts.buttons) || RemoteLayouts.buttons)[id] || {};
  const out = { label: button.label || id };
  if (button.icon) out.icon = button.icon;
  if (button.text) out.text = button.text;
  if (button.repeat === true) out.repeat = true;
  return out;
}

function row(label, keys, columns) {
  return { label: label || "", keys: keys.slice(), columns: columns || 0 };
}

function planOf(remote, layouts) {
  return remote ? RemoteLayouts.layoutPlan(remote, layouts) : null;
}

// The rows in order: top; playback when it is long or there is no D-pad;
// then the key groups (primary ones first, as the terminal draws them) or
// one unlabelled row of keys.
function rowsOf(remote, layouts) {
  const plan = planOf(remote, layouts);
  if (!plan) return [];
  const rows = [];
  if (plan.top.length) rows.push(row("", plan.top, 0));
  if (plan.playback.length && (!plan.corners || plan.playback.length > BESIDE)) rows.push(row("", plan.playback, 0));
  if (plan.keyGroups.length) {
    plan.keyGroups.filter((group) => group.primary).concat(plan.keyGroups.filter((group) => !group.primary))
      .forEach((group) => rows.push(row(group.label, group.keys, group.columns)));
  } else if (plan.keys.length) {
    rows.push(row("", plan.keys, 0));
  }
  return rows;
}

function besideOf(remote, layouts) {
  const plan = planOf(remote, layouts);
  if (!plan) return [];
  return plan.corners && plan.playback.length <= BESIDE ? plan.playback.slice() : [];
}

/* The screen's fields but its id, for the session's current room. */
function screen(session, layouts) {
  // Not heard: on, off, switching and a problem are claims about now that nothing backs.
  const heard = session.connected();
  const current = session.current();
  if (!current) {
    const buttons = {};
    ALWAYS.forEach((id) => { buttons[id] = spec(layouts, id); });
    return {
      room: "",
      label: "",
      rooms: [],
      led: heard ? "off" : "stale",
      short: "No rooms",
      problem: heard ? "" : "Not connected to thermite. Reconnecting\u2026",
      notice: "",
      locked: false,
      bubbles: [],
      selected: null,
      hidden: true,
      remote: null,
      pad: null,
      rows: [],
      beside: [],
      channel: false,
      buttons: buttons,
      allOff: session.allOffArmed()
    };
  }
  const kinds = typeof session.kinds === "function" ? session.kinds() : ["watch"];
  const activeMode = typeof session.mode === "function" ? session.mode() : "watch";
  const multiMode = kinds.length > 1;
  const roomList = session.rooms().map((room) => {
    const view = session.view(room, session.state(room.id));
    const roomModes = typeof session.modes === "function" ? session.modes(room) : ["watch"];
    const roomMode = roomModes.includes(activeMode) ? activeMode : (roomModes[0] || "watch");
    const entry = {
      id: room.id,
      label: room.label,
      led: heard ? view.led : "stale",
      short: view.short,
      mode: roomMode
    };
    if (session.roomLocked(room.id).locked === true) entry.locked = true;
    return entry;
  });
  const state = session.state(current.id);
  const shown = session.view(current, state);
  if (activeMode === "listen") {
    const listen = session.listen || null;
    const lv = listen ? listen.view() : null;
    const listenHeard = Boolean(heard && (!lv || lv.connected !== false));
    const speakers = listen ? listen.speakers(current.id) : [];
    const locked = Boolean(lv && lv.locked);
    const offered = lv && Array.isArray(lv.transport)
      ? LISTEN_TRANSPORT.filter((id) => lv.transport.some((entry) => entry.id === id))
      : [];
    const transportKeys = offered.length ? offered : (lv ? LISTEN_TRANSPORT.slice() : []);
    const rows = transportKeys.length ? [row("", transportKeys, transportKeys.length)] : [];
    const buttons = {};
    transportKeys.concat(ALWAYS).forEach((id) => {
      if (buttons[id]) return;
      if (id === "play_pause" && lv) {
        buttons[id] = lv.playing === true ? { label: "Pause", icon: "pause" } : { label: "Play", icon: "play" };
      } else {
        buttons[id] = spec(layouts, id);
      }
    });
    const playState = !listenHeard ? "stale"
      : lv && lv.playing === true ? "playing"
      : shown.short === "Paused" ? "paused"
      : lv && lv.playing === null ? "unknown"
      : "idle";
    const result = {
      room: current.id,
      label: current.label,
      rooms: roomList,
      led: listenHeard ? shown.led : "stale",
      short: shown.short,
      problem: clip((lv && (lv.problem || lv.music)) || (!listenHeard ? "Not connected to thermite. Reconnecting\u2026" : ""), NOTICE_LIMIT),
      notice: locked && lv.locked.text ? clip(lv.locked.text, NOTICE_LIMIT) : "",
      locked: locked,
      bubbles: speakers.map((speaker) => ({
        id: speaker.id,
        label: speaker.name,
        state: !listenHeard ? "stale" : speaker.playing ? "current" : "idle",
        lock: Boolean(locked && speaker.current),
        inert: false
      })),
      selected: listen ? (listen.selected() || null) : null,
      hidden: !lv,
      remote: lv ? { label: lv.speaker.name, note: "" } : null,
      pad: null,
      rows: rows,
      beside: [],
      channel: false,
      buttons: buttons,
      allOff: session.allOffArmed(),
      mode: "listen",
      playing: {
        title: clip(lv ? (lv.title || lv.now || "") : "", TEXT_LIMIT),
        artist: clip(lv && lv.line ? lv.line : "", TEXT_LIMIT),
        state: playState
      }
    };
    if (multiMode) result.modes = kinds.slice();
    return result;
  }
  const list = session.controls();
  const target = session.activeTarget(list);
  const hidden = session.remoteHidden(list);
  const remote = hidden || !target ? null : planOf(session.remoteOf(target) || target.remote, layouts);
  const keys = remote && !remote.note;
  const pad = keys && remote.corners ? { corners: remote.corners.slice() } : null;
  const rows = keys ? rowsOf(remote, layouts) : [];
  const beside = keys ? besideOf(remote, layouts) : [];
  const channel = Boolean(keys && remote.channel);
  const ids = [];
  if (pad) ids.push(...pad.corners.filter(Boolean), ...PAD);
  rows.forEach((entry) => ids.push(...entry.keys));
  ids.push(...beside, ...ALWAYS);
  if (channel) ids.push(...CHANNEL);
  const buttons = {};
  ids.forEach((id) => { if (!buttons[id]) buttons[id] = spec(layouts, id); });
  const result = {
    room: current.id,
    label: current.label,
    rooms: roomList,
    led: heard ? shown.led : "stale",
    short: shown.short,
    problem: shown.problem || "",
    notice: session.lockNotice() || "",
    locked: session.roomLocked(current.id).locked === true,
    bubbles: session.bubbles(list).map((item) => {
      const bubbleState = session.bubbleState(item, state);
      const lock = session.bubbleLock(item, bubbleState);
      // The choice that was sent and never read back is marked on its own
      // bubble: view().unconfirmedId names it, and unconfirmed is its label.
      const shownState = !item.choose ? bubbleState : !heard ? "stale" : shown.unconfirmedId === item.id ? "unconfirmed" : bubbleState;
      return { id: item.id, label: item.label, state: shownState, lock: lock.locked === true, inert: lock.inert === true };
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
  if (multiMode) {
    result.mode = activeMode;
    result.modes = kinds.slice();
  }
  return result;
}

/* The volume line, or null while the screen is hidden. */
function audio(session, hidden) {
  if (hidden) return null;
  if (typeof session.mode === "function" && session.mode() === "listen" && session.listen) {
    const lv = session.listen.view();
    if (!lv || !lv.volume) return null;
    const vol = lv.volume;
    const short = lv.speaker.name || lv.speaker.label || "Speaker";
    const text = vol.text || (vol.value === null ? "Reading\u2026" : String(vol.value));
    const caption = short + " \u00b7 " + text + (lv.muted === true ? " \u00b7 muted" : "");
    return {
      short: short, value: vol.value, min: vol.min, max: vol.max, step: vol.step,
      muted: lv.muted, disabled: vol.disabled === true, text: text, caption: caption
    };
  }
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

module.exports = { screen, audio, digest, rowsOf, besideOf, clip, ALWAYS, CHANNEL, PAD, BESIDE, LISTEN_TRANSPORT, TEXT_LIMIT };
