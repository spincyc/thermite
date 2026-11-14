/*
 * The Audio page's behavior without a page: each device with an audio page
 * (house.js devices[id].audio, beside its remote's control), what it plays,
 * its volume, mute and input, its settings and facts, and every command the
 * page sends (RemoteBackend command()). audio.js draws a session in a
 * browser and the terminal remote draws the same one; neither decides
 * anything this does not. Load it after house.js and backend.js.
 *
 * RemoteAudio.create({backend, house, access, store}) returns a session on
 * a RemoteBackend (house defaults to REMOTE_HOUSE; access is the access.js
 * client, for the locks; store is {get(key), set(key, value)}, in memory if
 * absent). The session has access poll the locks while it runs, unless
 * watchLocks is false: a host that polls them itself (the terminal).
 * start(device) chooses that device, the stored one or the first.
 * subscribe(callback) hears:
 *
 *   {type: "change"}                     draw it again (devices(), view())
 *   {type: "toast", title, body, tone}   tone "", "muted" or "bad"
 *   {type: "announce", message}          for a screen reader
 *
 * devices() lists each place's devices; view() is the chosen device's page,
 * or null without one; select(id) chooses another. press(key),
 * setVolume(value), stepVolume(±1), toggleMute(), togglePower(),
 * chooseInput(value), set(item, value), toggle(item) and step(item, ±1)
 * send its commands. Each shows what it sent until the device reads it
 * back, or HOLD_MS pass (longer for a device that reports late:
 * RemoteBackend.heldMs). A setting or fact shows only while its Item has a
 * value and its `when` holds. A room that owns the device (its rooms, "own")
 * and is locked locks the page; the gateway checks every command anyway.
 */
(function (global) {
  "use strict";

  // What reads as no value, as the playing rule (playing.js) has it: a
  // WiiM names a field it does not know Unknown.
  const NONE = ["NULL", "UNDEF", "", "Unknown"];
  // How long a value sent shows before what the device reports, and how
  // often a volume being dragged sends (as the remote's slider).
  const HOLD_MS = 6000;
  const SLIDE_EVERY = 250;
  const SECTIONS = [["playback", "Playback"], ["sound", "Sound"], ["device", "Device"]];
  const TRANSPORT = {
    previous: { label: "Previous", icon: "previous" },
    next: { label: "Next", icon: "next" },
    tv: { label: "TV audio", icon: "tv" }
  };

  function memoryStore() {
    const memory = {};
    return { get: (key) => (key in memory ? memory[key] : null), set: (key, value) => { memory[key] = value; } };
  }

  function present(value) {
    return value === undefined || value === null || NONE.includes(String(value).trim()) ? null : String(value);
  }

  function number(value) {
    const parsed = value === null ? NaN : parseFloat(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  // −4 for -4: the sign a reader sees.
  function signed(value) {
    return String(value).replace(/^-/, "−");
  }

  function duration(value) {
    const seconds = Math.round(number(value) || 0);
    if (seconds <= 0) return null;
    const pad = (n) => String(n).padStart(2, "0");
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return (hours ? hours + ":" + pad(minutes) : minutes) + ":" + pad(seconds % 60);
  }

  // 44.1 kHz · 24-bit
  function format(rate, depth) {
    const hz = number(rate), bits = number(depth);
    return [hz > 0 ? String(Math.round(hz / 100) / 10) + " kHz" : null, bits > 0 ? Math.round(bits) + "-bit" : null]
      .filter(Boolean).join(" · ") || null;
  }

  // A reading with its unit: 38 °C, 80%, Yes for ON.
  function reading(value, unit) {
    if (value === "ON") return "Yes";
    if (value === "OFF") return "No";
    const parsed = number(value);
    if (parsed === null || !unit) return value;
    const text = signed(Number.isInteger(parsed) ? parsed : parsed.toFixed(1));
    return text + (["%", "°"].includes(unit) ? "" : " ") + unit;
  }

  function create(options) {
    const config = options || {};
    const Remote = global.RemoteBackend;
    const backend = config.backend;
    const house = config.house || global.REMOTE_HOUSE;
    const access = config.access && !config.access.native && !config.access.mock ? config.access : null;
    const store = config.store || memoryStore();
    const polls = Boolean(access) && config.watchLocks !== false;
    const now = config.now || (() => Date.now());
    const later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
    const cancel = config.clearTimeout || ((handle) => global.clearTimeout(handle));

    const ids = Object.keys(house.devices).filter((id) => house.devices[id].audio && Remote.controlOf(house, id));
    const listeners = [];
    const unsubscribe = [];
    // What each device last reported (getDevice) and its page's Items (audioOf).
    const devices = {};
    const values = {};
    // Device id + " " + action -> {value, at}: what a command sent, shown
    // until the device reads it back.
    const held = {};
    const fader = { device: null, value: null, sent: null, sentTo: null, timer: 0 };
    // The draws due when a hold ends, cancelled when the session stops.
    const ends = new Set();
    let current = null;
    let connected = null;
    let pending = 0;
    // Counts each start and stop, so a start's late work knows it is stale.
    let life = 0;

    function emit(event) {
      listeners.slice().forEach((listener) => {
        try { listener(event); } catch (error) { console.error(error); }
      });
    }

    // Many Items arrive at once (a stream's snapshot): one draw for them all.
    function changed() {
      if (pending) return;
      pending = later(() => { pending = 0; emit({ type: "change" }); }, 0);
    }

    function toast(title, body, tone) {
      emit({ type: "toast", title: title, body: body || "", tone: tone || "" });
    }

    function page(id) {
      return house.devices[id].audio;
    }

    function control(id) {
      return Remote.controlOf(house, id);
    }

    // An Item of the device's page: its control's as getDevice reports it,
    // else as audioOf does; null for no value.
    function valueOf(id, item) {
      const own = control(id), state = devices[id] || {};
      const fields = {};
      if (own.power) fields[own.power] = "power";
      if (own.mute) fields[own.mute] = "mute";
      if (own.volume) fields[own.volume.item] = "volume";
      if (own.input) fields[own.input.item] = "input";
      return present(fields[item] ? state[fields[item]] : (values[id] || {})[item]);
    }

    function holds(id, when) {
      return !when || when.is.includes(valueOf(id, when.item));
    }

    function holdMs(id) {
      return Remote.heldMs(control(id), HOLD_MS);
    }

    // What the page shows for an action: the value sent while it is not
    // read back and HOLD_MS have not passed, else what was read.
    function shown(id, action, read, same) {
      const key = id + " " + action, sent = held[key];
      if (!sent) return { value: read, pending: false };
      if (read !== null && (same ? same(read, sent.value) : read === sent.value)) {
        delete held[key];
        return { value: read, pending: false };
      }
      if (now() - sent.at >= holdMs(id)) {
        delete held[key];
        return { value: read, pending: false };
      }
      return { value: sent.value, pending: true };
    }

    const sameNumber = (a, b) => number(a) === number(b);

    // A device's report ends each hold it answers, then and there: a value read
    // back and then moved on (its own knob) before a draw is not missed.
    function heard(id) {
      const state = devices[id] || {}, own = control(id);
      ["mute", "power", "input"].forEach((field) => { if (own[field]) shown(id, field, present(state[field])); });
      if (own.volume) shown(id, "volume", present(state.volume), sameNumber);
      (page(id).settings || []).forEach((setting) => {
        shown(id, setting.item, valueOf(id, setting.item), setting.kind === "number" ? sameNumber : null);
      });
      playingOf(id);
    }

    function hold(id, action, value) {
      held[id + " " + action] = { value: String(value), at: now() };
      // A device that ignored it reports nothing: draw what it read once the hold ends.
      const end = later(() => { ends.delete(end); changed(); }, holdMs(id) + 50);
      ends.add(end);
    }

    // The rooms that own the device and are locked, by label.
    function lockedBy(id) {
      if (!access) return [];
      const rooms = page(id).rooms || {};
      return Object.keys(rooms).filter((room) => rooms[room] === "own" && (access.lockSummary(room) || {}).locked)
        .map((room) => (house.rooms.find((entry) => entry.id === room) || { label: room }).label);
    }

    function label(id) {
      const target = house.devices[id];
      const place = page(id).place;
      const shared = ids.filter((other) => page(other).place === place).length > 1;
      return shared ? place + " " + target.label : place;
    }

    // A command, said to a screen reader once sent (a volume being dragged
    // says itself); a failure drops what the page held for it (heldAs, the
    // action unless that differs) and lets the same volume be sent again.
    function send(id, action, value, title, text, heldAs) {
      const sent = backend.command(id, action, value);
      return Promise.resolve(sent).then((result) => {
        if (result && !result.sent && result.tag !== "Mock") toast(title, text, "muted");
        else if (action !== "volume") emit({ type: "announce", message: label(id) + ": " + title + (text ? " " + text : "") });
      }).catch((error) => {
        delete held[id + " " + (heldAs || action)];
        if (action === "volume" && fader.sentTo === id) fader.sent = null;
        changed();
        toast(title, error && error.message ? error.message : String(error), "bad");
      });
    }

    function blocked() {
      if (!current) return true;
      const locked = lockedBy(current);
      if (!locked.length) return false;
      toast(label(current), "Locked by " + locked.join(", "), "bad");
      return true;
    }

    // ---- reading --------------------------------------------------------------

    // Whether it plays: true, false, or null while its state is not known;
    // a play/pause just pressed shows what it asked for.
    function playingOf(id) {
      const state = page(id).state;
      const status = state ? valueOf(id, state.item) : null;
      const value = shown(id, "playing", status === null ? null : String(state.playing.includes(status))).value;
      return value === null ? null : value === "true";
    }

    function devicesView() {
      const places = [];
      ids.forEach((id) => {
        const place = page(id).place;
        let group = places.find((entry) => entry.place === place);
        if (!group) places.push(group = { place: place, devices: [] });
        const track = page(id).track || {};
        const playing = playingOf(id);
        group.devices.push({ id: id, label: house.devices[id].label, name: label(id), current: id === current, playing: playing,
          title: playing && track.title ? valueOf(id, track.title) : null, locked: lockedBy(id).length > 0 });
      });
      return places;
    }

    function trackView(id) {
      const track = page(id).track;
      if (!track) return null;
      const read = (field) => (track[field] ? valueOf(id, track[field]) : null);
      const title = read("title");
      if (!title) return null;
      return { title: title, line: [read("artist"), read("album")].filter(Boolean).join(" · ") || null,
        service: read("service"), duration: duration(read("duration")), format: format(read("sample_rate"), read("bit_depth")) };
    }

    function volumeView(id) {
      const volume = control(id).volume;
      if (!volume) return null;
      const state = shown(id, "volume", present((devices[id] || {}).volume), sameNumber);
      const value = number(state.value);
      return { value: value, min: volume.min, max: volume.max, step: Remote.volumeStep(volume), disabled: value === null,
        pending: state.pending,
        text: value === null ? ((devices[id] || {}).volume === "UNDEF" ? "Not answering" : "Reading…") : Remote.volumeText(volume, value),
        fill: volume.max > volume.min && value !== null ? Math.min(100, Math.max(0, (value - volume.min) / (volume.max - volume.min) * 100)) : 0 };
    }

    function settingView(id, setting) {
      const state = shown(id, setting.item, valueOf(id, setting.item), setting.kind === "number" ? sameNumber : null);
      if (state.value === null || !holds(id, setting.when)) return null;
      const out = { item: setting.item, label: setting.label, kind: setting.kind, value: state.value, pending: state.pending };
      if (setting.kind === "switch") {
        out.on = state.value === "ON";
        out.text = out.on ? "On" : "Off";
      } else if (setting.kind === "enum") {
        const known = setting.options.concat(setting.reads || []).find((option) => option[0] === state.value);
        out.text = known ? known[1] : state.value;
        out.options = setting.options.map((option) => ({ value: option[0], label: option[1], current: option[0] === state.value }));
      } else {
        const level = number(state.value);
        Object.assign(out, { min: setting.min, max: setting.max, step: setting.step, unit: setting.unit || "",
          level: level, atMin: level !== null && level <= setting.min, atMax: level !== null && level >= setting.max,
          text: level === null ? state.value : signed(level) + (setting.unit ? " " + setting.unit : "") });
      }
      return out;
    }

    // The chosen device's page.
    function view() {
      if (!current) return null;
      const id = current, own = control(id), state = devices[id] || {}, audio = page(id);
      const isPlaying = playingOf(id);
      const mute = own.mute ? shown(id, "mute", present(state.mute)) : null;
      const power = own.power ? shown(id, "power", present(state.power)) : null;
      const input = own.input ? shown(id, "input", present(state.input)) : null;
      const sections = SECTIONS.map(([section, title]) => ({ id: section, title: title,
        settings: (audio.settings || []).filter((setting) => setting.section === section)
          .map((setting) => settingView(id, setting)).filter(Boolean) })).filter((section) => section.settings.length);
      const facts = (audio.facts || []).map((fact) => ({ label: fact.label, value: valueOf(id, fact.item), unit: fact.unit }))
        .filter((fact) => fact.value !== null).map((fact) => ({ label: fact.label, text: reading(fact.value, fact.unit) }));
      const locked = lockedBy(id);
      const track = trackView(id);
      const from = input && own.input.options.find((option) => option[0] === input.value);
      return {
        device: { id: id, label: house.devices[id].label, name: label(id), model: house.devices[id].model, place: audio.place },
        connected: connected,
        problem: backend.problemOf ? backend.problemOf(id) : null,
        locked: locked.length ? { rooms: locked, text: "Locked by " + locked.join(", ") } : null,
        playing: isPlaying,
        track: track,
        // What heads the page: the title, else whether it plays (a TV's
        // sound, an input or a title kept from guests has none).
        now: track ? track.title : isPlaying === null ? "Reading…"
          : isPlaying ? "Playing" + (from ? " from " + from[1] : "") : "Nothing playing",
        transport: (audio.transport || []).map((key) => key === "play_pause"
          ? { id: key, label: isPlaying ? "Pause" : "Play", icon: isPlaying ? "pause" : "play" }
          : { id: key, label: (TRANSPORT[key] || { label: key }).label, icon: (TRANSPORT[key] || { icon: key }).icon }),
        volume: volumeView(id),
        muted: mute && mute.value !== null ? mute.value === "ON" : null,
        power: power && power.value !== null ? power.value === "ON" : null,
        input: input ? { value: input.value, pending: input.pending,
          label: ((own.input.options.find((option) => option[0] === input.value) || [null, input.value])[1]),
          options: own.input.options.map((option) => ({ value: option[0], label: option[1], current: option[0] === input.value })) } : null,
        sections: sections,
        facts: facts
      };
    }

    // ---- commands -------------------------------------------------------------

    function press(key) {
      if (blocked()) return Promise.resolve();
      const id = current, audio = page(id);
      if (!(audio.transport || []).includes(key)) return Promise.resolve();
      const entry = view().transport.find((button) => button.id === key);
      // Play/pause shows what it asked for until the device says: the
      // opposite of what shows, so a second press undoes the first.
      const plays = key === "play_pause" && audio.state ? playingOf(id) : null;
      if (plays !== null) hold(id, "playing", String(!plays));
      changed();
      return send(id, "key", key, entry.label, "", plays !== null ? "playing" : null);
    }

    // The volume moved to value. At most one is sent every SLIDE_EVERY
    // while it moves, and always the last.
    // Nothing while the volume is not known: a slider with no value has no
    // place to move from.
    function setVolume(value) {
      if (blocked()) return;
      const volume = control(current).volume;
      const parsed = Number(value);
      if (!volume || !Number.isFinite(parsed) || volumeView(current).value === null) return;
      fader.device = current;
      fader.value = Remote.volumeValue(volume, parsed);
      hold(current, "volume", fader.value);
      if (!fader.timer) sendVolume();
      changed();
    }

    function sendVolume() {
      fader.timer = 0;
      if (fader.value === fader.sent && fader.device === fader.sentTo) { changed(); return; }
      fader.sent = fader.value;
      fader.sentTo = fader.device;
      send(fader.device, "volume", fader.value, "Volume", Remote.volumeText(control(fader.device).volume, fader.value));
      fader.timer = later(sendVolume, SLIDE_EVERY);
    }

    // The last of a slide still under way, sent now and no later.
    function flush() {
      if (!fader.timer) return;
      cancel(fader.timer);
      fader.timer = 0;
      if (fader.value === fader.sent && fader.device === fader.sentTo) return;
      fader.sent = fader.value;
      fader.sentTo = fader.device;
      send(fader.device, "volume", fader.value, "Volume", Remote.volumeText(control(fader.device).volume, fader.value));
    }

    function stepVolume(direction) {
      const shownVolume = current && volumeView(current);
      if (!shownVolume || shownVolume.value === null) return;
      const next = Math.min(shownVolume.max, Math.max(shownVolume.min, shownVolume.value + direction * shownVolume.step));
      if (next === shownVolume.value || blocked()) return;
      setVolume(next);
      // A step is not a drag a screen reader follows: say where it went.
      emit({ type: "announce", message: label(current) + ": Volume " + Remote.volumeText(control(current).volume, fader.value) });
    }

    function toggleMute() {
      if (blocked()) return Promise.resolve();
      const muted = view().muted;
      if (muted === null) return Promise.resolve();
      const value = muted ? "OFF" : "ON";
      hold(current, "mute", value);
      changed();
      return send(current, "mute", value, "Mute", muted ? "off" : "on");
    }

    function togglePower() {
      if (blocked()) return Promise.resolve();
      const on = view().power;
      if (on === null) return Promise.resolve();
      const value = on ? "OFF" : "ON";
      hold(current, "power", value);
      changed();
      return send(current, "power", value, "Power", on ? "off" : "on");
    }

    function chooseInput(value) {
      if (blocked()) return Promise.resolve();
      const input = control(current).input;
      const option = input && input.options.find((entry) => entry[0] === value);
      if (!option) return Promise.resolve();
      hold(current, "input", value);
      changed();
      return send(current, "input", value, "Input", option[1]);
    }

    function setting(item) {
      return current ? (page(current).settings || []).find((entry) => entry.item === item) || null : null;
    }

    // A setting sent: a switch "ON" or "OFF", a choice's value, a level's number.
    function set(item, value) {
      if (blocked()) return Promise.resolve();
      const spec = setting(item);
      if (!spec) return Promise.resolve();
      let command = value, text;
      if (spec.kind === "switch") {
        if (value !== "ON" && value !== "OFF") return Promise.resolve();
        text = value === "ON" ? "on" : "off";
      } else if (spec.kind === "enum") {
        const option = spec.options.find((entry) => entry[0] === value);
        if (!option) return Promise.resolve();
        text = option[1];
      } else {
        command = Number(value);
        if (!Number.isFinite(command) || command < spec.min || command > spec.max) return Promise.resolve();
        text = signed(command) + (spec.unit ? " " + spec.unit : "");
      }
      hold(current, item, command);
      changed();
      return send(current, item, command, spec.label, text);
    }

    function toggle(item) {
      const shownSetting = current && setting(item) && settingView(current, setting(item));
      return shownSetting && shownSetting.kind === "switch" ? set(item, shownSetting.on ? "OFF" : "ON") : Promise.resolve();
    }

    function step(item, direction) {
      const shownSetting = current && setting(item) && settingView(current, setting(item));
      if (!shownSetting || shownSetting.kind !== "number" || shownSetting.level === null) return Promise.resolve();
      const next = Math.min(shownSetting.max, Math.max(shownSetting.min, shownSetting.level + direction * shownSetting.step));
      return next === shownSetting.level ? Promise.resolve() : set(item, next);
    }

    // ---- life -----------------------------------------------------------------

    function select(id) {
      if (!ids.includes(id) || id === current) return;
      // The last of a slide still under way reaches the device it was for.
      flush();
      current = id;
      store.set("audio.device", id);
      emit({ type: "change" });
    }

    function readDevice(id) {
      return backend.getDevice(id).then((state) => { devices[id] = state; }, () => {});
    }

    function start(id) {
      if (unsubscribe.length) stop();
      connected = typeof backend.connected === "boolean" ? backend.connected : null;
      unsubscribe.push(backend.subscribe((event) => {
        if (event.type === "device" && ids.includes(event.device)) {
          const before = devices[event.device] || {};
          devices[event.device] = event.state;
          // Moved off what was sent between slides (its own knob), that volume
          // may be sent again; a slide's late echoes and other reports are not that.
          const moved = number(event.state.volume) !== number(before.volume);
          if (fader.sentTo === event.device && !fader.timer && moved && number(event.state.volume) !== number(fader.sent)) {
            fader.sent = null;
          }
          heard(event.device);
          changed();
        }
        else if (event.type === "audio" && ids.includes(event.device)) {
          values[event.device] = backend.audioOf(event.device);
          heard(event.device);
          changed();
        }
        else if (event.type === "problem" && ids.includes(event.device)) changed();
        else if (event.type === "connection") { connected = event.connected; changed(); }
        else if (event.type === "access") changed();
      }));
      const started = ++life;
      if (access) {
        unsubscribe.push(access.subscribe((event) => { if (event.type === "locks" || event.type === "policy") changed(); }));
        // Locks are read only once access knows accounts are on (as home.js).
        if (polls) Promise.resolve(access.ready()).then(() => { if (life === started) access.watchLocks(true); }).catch(() => {});
      }
      ids.forEach((device) => { values[device] = backend.audioOf(device); });
      const stored = store.get("audio.device");
      current = [id, stored].find((candidate) => ids.includes(candidate)) || ids[0] || null;
      return Promise.all(ids.map(readDevice)).then(() => { emit({ type: "change" }); });
    }

    function stop() {
      life += 1;
      flush();
      unsubscribe.splice(0).forEach((off) => off());
      if (polls) access.watchLocks(false);
      cancel(pending);
      ends.forEach(cancel);
      ends.clear();
      // A session started again hears nothing of the time between: it draws,
      // holds and sends afresh.
      pending = 0;
      fader.sent = fader.sentTo = null;
      Object.keys(held).forEach((key) => { delete held[key]; });
    }

    return {
      start: start,
      stop: stop,
      subscribe: function (callback) {
        listeners.push(callback);
        return () => { const index = listeners.indexOf(callback); if (index >= 0) listeners.splice(index, 1); };
      },
      devices: devicesView,
      view: view,
      select: select,
      selected: () => current,
      press: press,
      setVolume: setVolume,
      stepVolume: stepVolume,
      toggleMute: toggleMute,
      togglePower: togglePower,
      chooseInput: chooseInput,
      set: set,
      toggle: toggle,
      step: step
    };
  }

  global.RemoteAudio = { create: create, HOLD_MS: HOLD_MS, SLIDE_EVERY: SLIDE_EVERY, duration: duration, format: format, reading: reading };
})(globalThis);
