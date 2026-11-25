/*
 * Shared button vocabulary and layout planner for thermite remotes.
 *
 * Integration-owned remote layouts are emitted into house.js
 * (REMOTE_HOUSE.layouts.remotes) by lib/oh_features/remote_model.py from each
 * integration's remote_layouts() hook. This file keeps the shared button icon
 * and caption vocabulary plus minimal built-in fallback layouts (pc, ps5) and
 * exports RemoteLayouts.layoutPlan() / forHouse() for Web, TUI, and Handheld.
 */
(function (global) {
  "use strict";

  const PAD = ["up", "down", "left", "right", "ok"];
  const CHANNEL = ["channel_up", "channel_down"];

  const BUTTONS = {
    power_on: { label: "Power on", text: "On" },
    power_off: { label: "Standby", text: "Standby" },
    power: { label: "Power", icon: "power" },
    input: { label: "Input", icon: "input" },
    settings: { label: "Settings", icon: "settings" },
    menu: { label: "Menu", icon: "menu" },
    home: { label: "Home", icon: "home" },
    back: { label: "Back", icon: "back" },
    tv: { label: "TV", icon: "tv" },
    eject: { label: "Open/close", icon: "eject" },

    up: { label: "Up", icon: "up", repeat: true },
    down: { label: "Down", icon: "down", repeat: true },
    left: { label: "Left", icon: "left", repeat: true },
    right: { label: "Right", icon: "right", repeat: true },
    ok: { label: "OK", text: "OK" },

    play_pause: { label: "Play/pause", icon: "play-pause" },
    play: { label: "Play", icon: "play" },
    pause: { label: "Pause", icon: "pause" },
    stop: { label: "Stop", icon: "stop" },
    rewind: { label: "Rewind", icon: "rewind" },
    fast_forward: { label: "Fast-forward", icon: "forward" },
    previous: { label: "Previous", icon: "previous" },
    next: { label: "Next", icon: "next" },

    channel_up: { label: "Channel up", icon: "up", repeat: true },
    channel_down: { label: "Channel down", icon: "down", repeat: true },
    volume_up: { label: "Volume up", icon: "plus", repeat: true },
    volume_down: { label: "Volume down", icon: "minus", repeat: true },
    mute: { label: "Mute", icon: "mute" },

    netflix: { label: "Netflix", text: "NETFLIX" },
    youtube: { label: "YouTube", text: "YouTube" },
    favorite: { label: "Favorite", text: "Favorite" },
    bluetooth: { label: "Bluetooth audio", text: "Bluetooth" },
    top_menu: { label: "Top menu", text: "Top menu" },
    popup_menu: { label: "Pop-up menu", text: "Pop-up" },
    options: { label: "Options", text: "Options" },
    display: { label: "Display", text: "Display" },
    audio: { label: "Audio track", text: "Audio" },
    subtitle: { label: "Subtitles", text: "Subtitle" },
    info: { label: "Info", text: "Info" },
    guide: { label: "Programme guide", text: "Guide" },
    action_menu: { label: "Action menu", text: "Action menu" },
    exit: { label: "Exit", text: "Exit" },
    digit_1: { label: "1", text: "1" },
    digit_2: { label: "2", text: "2" },
    digit_3: { label: "3", text: "3" },
    digit_4: { label: "4", text: "4" },
    digit_5: { label: "5", text: "5" },
    digit_6: { label: "6", text: "6" },
    digit_7: { label: "7", text: "7" },
    digit_8: { label: "8", text: "8" },
    digit_9: { label: "9", text: "9" },
    digit_0: { label: "0", text: "0" },
    digit_dot: { label: "Channel decimal point", text: "." },
    digit_enter: { label: "Channel enter", text: "Enter" },
    red: { label: "Red", text: "Red" },
    green: { label: "Green", text: "Green" },
    yellow: { label: "Yellow", text: "Yellow" },
    blue: { label: "Blue", text: "Blue" },
    help: { label: "Help", text: "Help" },
    sound_movie: { label: "Movie sound", text: "Movie" },
    sound_music: { label: "Music sound", text: "Music" },
    sound_game: { label: "Game sound", text: "Game" },
    sound_pure: { label: "Pure sound", text: "Pure" },
    picture: { label: "Picture mode", text: "Picture" },
    aspect: { label: "Aspect", text: "Aspect" },
    lens: { label: "Picture position", text: "Position" },
    night: { label: "Night sound", text: "Night" },
    speech: { label: "Speech enhancement", text: "Speech" }
  };

  // Built-in fallbacks for devices without an integration remote layout.
  const BUILTIN_REMOTES = {
    pc: {
      label: "Keyboard and mouse",
      note: "{device} is a PC: use its own keyboard and mouse"
    },
    ps5: {
      label: "DualSense controller",
      note: "thermite cannot drive {device}: use its own controller"
    }
  };

  function houseRemotes(source) {
    if (!source || typeof source !== "object") return null;
    if (source.layouts && source.layouts.remotes && typeof source.layouts.remotes === "object") return source.layouts.remotes;
    if (source.remotes && typeof source.remotes === "object") return source.remotes;
    return source;
  }

  function normalizeRemote(raw) {
    if (!raw || typeof raw !== "object") return null;
    const groups = Array.isArray(raw.keyGroups)
      ? raw.keyGroups.filter((group) => group && Array.isArray(group.keys) && group.keys.length).map((group) => ({
          label: String(group.label || ""),
          keys: group.keys.slice(),
          columns: Number(group.columns) || group.keys.length,
          primary: Boolean(group.primary)
        }))
      : null;
    const keys = groups ? groups.flatMap((group) => group.keys) : (Array.isArray(raw.keys) ? raw.keys.slice() : null);
    const out = Object.assign({}, raw);
    if (groups) out.keyGroups = groups;
    if (keys) out.keys = keys;
    return out;
  }

  function mergeRemotes(houseOrLayouts) {
    const out = {};
    const add = (map) => {
      if (!map || typeof map !== "object") return;
      Object.keys(map).forEach((id) => {
        const spec = normalizeRemote(map[id]);
        if (spec) out[id] = spec;
      });
    };
    add(BUILTIN_REMOTES);
    if (global.REMOTE_LAYOUTS && global.REMOTE_LAYOUTS.remotes && global.REMOTE_LAYOUTS.remotes !== BUILTIN_REMOTES) {
      add(global.REMOTE_LAYOUTS.remotes);
    }
    if (global.REMOTE_HOUSE && global.REMOTE_HOUSE.layouts && global.REMOTE_HOUSE.layouts.remotes) {
      add(global.REMOTE_HOUSE.layouts.remotes);
    }
    add(houseRemotes(houseOrLayouts));
    return out;
  }

  function forHouse(house) {
    return {
      version: 1,
      buttons: (global.REMOTE_LAYOUTS && global.REMOTE_LAYOUTS.buttons) || BUTTONS,
      remotes: mergeRemotes(house)
    };
  }

  function lookupRemote(remoteOrId, layouts) {
    if (!remoteOrId) return { id: null, spec: null };
    if (typeof remoteOrId === "object") return { id: remoteOrId.id || null, spec: remoteOrId };
    const id = String(remoteOrId);
    const candidates = [
      houseRemotes(layouts),
      global.REMOTE_HOUSE && global.REMOTE_HOUSE.layouts && global.REMOTE_HOUSE.layouts.remotes,
      global.REMOTE_LAYOUTS && global.REMOTE_LAYOUTS.remotes,
      BUILTIN_REMOTES
    ];
    for (let i = 0; i < candidates.length; i += 1) {
      const map = candidates[i];
      if (map && Object.prototype.hasOwnProperty.call(map, id) && map[id]) {
        return { id, spec: map[id] };
      }
    }
    return { id, spec: null };
  }

  // Normalized layout plan shared by Web (remote.js), TUI (view.js), and
  // Handheld (screen.js). Derives flat `keys` from `keyGroups` when omitted
  // and computes the complete ordered list of unique button IDs (`buttons`).
  function layoutPlan(remoteOrId, layouts) {
    const found = lookupRemote(remoteOrId, layouts);
    if (!found.spec) return null;
    const raw = found.spec;
    const corners = Array.isArray(raw.corners) ? [0, 1, 2, 3].map((at) => raw.corners[at] || null) : null;
    const top = Array.isArray(raw.top) ? raw.top.slice() : [];
    const playback = Array.isArray(raw.playback) ? raw.playback.slice() : [];
    const keyGroups = Array.isArray(raw.keyGroups)
      ? raw.keyGroups.filter((group) => group && Array.isArray(group.keys) && group.keys.length).map((group) => ({
          label: String(group.label || ""),
          keys: group.keys.slice(),
          columns: Number(group.columns) || group.keys.length,
          primary: Boolean(group.primary)
        }))
      : [];
    const keys = keyGroups.length
      ? keyGroups.flatMap((group) => group.keys)
      : (Array.isArray(raw.keys) ? raw.keys.slice() : []);
    const channel = Boolean(raw.channel);
    const wide = Boolean(raw.wide);
    const note = raw.note ? String(raw.note) : "";
    const buttons = [];
    const seen = new Set();
    const push = (id) => {
      if (id && !seen.has(id)) {
        seen.add(id);
        buttons.push(id);
      }
    };
    if (!note) {
      if (corners) {
        corners.forEach(push);
        PAD.forEach(push);
      }
      top.forEach(push);
      playback.forEach(push);
      keys.forEach(push);
      if (channel) CHANNEL.forEach(push);
    }
    return {
      id: found.id,
      label: String(raw.label || found.id || ""),
      corners,
      top,
      playback,
      channel,
      keyGroups,
      keys,
      wide,
      note,
      buttons
    };
  }

  function buttonsOf(remoteOrId, layouts) {
    const plan = layoutPlan(remoteOrId, layouts);
    return plan ? plan.buttons.slice() : [];
  }

  const initialRemotes = {};
  Object.keys(BUILTIN_REMOTES).forEach((id) => { initialRemotes[id] = normalizeRemote(BUILTIN_REMOTES[id]); });
  if (global.REMOTE_HOUSE && global.REMOTE_HOUSE.layouts && global.REMOTE_HOUSE.layouts.remotes) {
    Object.keys(global.REMOTE_HOUSE.layouts.remotes).forEach((id) => {
      const spec = normalizeRemote(global.REMOTE_HOUSE.layouts.remotes[id]);
      if (spec) initialRemotes[id] = spec;
    });
  }

  global.REMOTE_LAYOUTS = {
    version: 1,
    buttons: BUTTONS,
    remotes: initialRemotes
  };

  const api = {
    version: 1,
    buttons: BUTTONS,
    remotes: global.REMOTE_LAYOUTS.remotes,
    PAD,
    CHANNEL,
    forHouse,
    mergeRemotes,
    layoutPlan,
    buttonsOf
  };
  global.RemoteLayouts = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
