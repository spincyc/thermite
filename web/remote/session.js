/*
 * The remote's behavior without a page: what each room shows, its bubbles,
 * which device the buttons control, the keys, the second press Power off and
 * All off take, the volume slider, the input list and the room's info.
 * remote.js draws a session in a browser: it only renders what the session
 * says and hands it the user's input, so another front end can do the same.
 * Load it after house.js, remotes.js and backend.js.
 *
 * RemoteSession.create({backend, house, layouts, store, volume, listen})
 * returns a session on a RemoteBackend (house and layouts default to
 * REMOTE_HOUSE and REMOTE_LAYOUTS; store is {get(key), set(key, value)}, in
 * memory if absent; volume is "slider" or "buttons", else what the store
 * holds; listen is a RemoteListen, listen-session.js, for the rooms'
 * speakers). start(room) reads every room and device and resolves once the
 * session can be drawn, on that room, the stored one or the first. stop()
 * ends it: it lets the backend go and is not started again.
 *
 * rooms() are the routed rooms, then the rooms of speakers alone
 * (house.js speakerRooms, listenOnly). Each room has speakers, and modes()
 * (room?): "watch", its sources and remotes, and "listen", its speakers
 * through listen (both where it has a screen and speakers). mode() is the
 * one on show in every room, of kinds(), those any room has; setMode() or
 * toggleMode() chooses it, and it is kept ("remote.mode", as Home keeps
 * it). listed() are the rooms of that mode, those the room lists show,
 * where view() gives what each plays in Listen and its source in Watch,
 * with what has failed: the house not heard (connected(), and each view's
 * connected), a choice never read back (unconfirmed).
 * Choosing a room of the other mode alone brings its mode; choosing the
 * mode the room lacks goes to the room last on show in it. In Listen the
 * digits choose a speaker and the keys (pressKey) go to listen.
 * subscribe(callback) hears:
 *
 *   {type: "change"}                     rooms, states, the buttons or the connection changed: draw it all again
 *   {type: "audio"}                      only the volume or Mute changed (audioView())
 *   {type: "device", device}             a device's state changed (audio, and its input list)
 *   {type: "toast", title, body, tone, tag}   tone "", "muted" or "bad"
 *   {type: "announce", message}          for a screen reader
 *   {type: "inputs", device, title}      open the input list (inputOptions(), chooseInput())
 *   {type: "all-off", armed}             All off waits for its second press, or no longer does
 *   {type: "matrix", answering}          mock only: the HDMI matrix answers, or stopped
 *   {type: "diagnostics", room}         diagnostics(room) has refreshed signal rows
 *
 * openDiagnostics(room?) reads read-only signal snapshots now and every ten
 * seconds while open. It follows room changes and confirmed source readback,
 * and reads nothing while a room of speakers alone is on show.
 * diagnostics(room?) returns its cached structured model, or null while a
 * different confirmed path is being read. closeDiagnostics() stops refresh;
 * callers close it when hiding the view or ending their session.
 */
(function (global) {
  "use strict";

  const KIND_NAME = {
    tv: "TV", projector: "Projector", avr: "Receiver", soundbar: "Soundbar", amp: "Amplifier",
    streaming_amp: "Streaming amplifier", audio_streamer: "Audio streamer", streamer: "Streamer",
    disc_player: "Disc player",
    game_console: "Game console",
    curtain: "Curtain", lighting: "Lighting", hdmi_matrix: "HDMI matrix", hdmi_switch: "HDMI switch",
    usb_matrix: "USB matrix"
  };
  // A key presses the first of its buttons the remote on show has.
  const KEYS = {
    ArrowUp: ["up"], ArrowDown: ["down"], ArrowLeft: ["left"], ArrowRight: ["right"],
    Backspace: ["back"], Escape: ["back"],
    "+": ["volume_up"], "=": ["volume_up"], "-": ["volume_down"], "_": ["volume_down"],
    m: ["mute"], M: ["mute"], p: ["play_pause", "play"], P: ["play_pause", "play"],
    "<": ["previous"], ">": ["next"]
  };
  const SHORTCUTS = [
    [["←", "↑", "→", "↓"], "D-pad"],
    [["Enter"], "OK"],
    [["Backspace", "Esc"], "Back"],
    [["+", "−"], "Volume"],
    [["M"], "Mute"],
    [["P"], "Play/pause"],
    [["0"], "Off"],
    [["1", "9"], "Source", "–"],
    [["[", "]"], "Room"],
    [["Y"], "Watch or listen"]
  ];
  // Listen's, where a room plays its speakers: the front ends take Space,
  // S, R, G and / themselves (play/pause off a control, shuffle, repeat, the
  // group sheet, the search).
  const LISTEN_SHORTCUTS = [
    [["Space", "P"], "Play/pause"],
    [["<", ">"], "Track"],
    [["+", "−"], "Volume"],
    [["M"], "Mute"],
    [["S"], "Shuffle"],
    [["R"], "Repeat"],
    [["G"], "Group"],
    [["/"], "Search"],
    [["1", "9"], "Speaker", "–"],
    [["[", "]"], "Room"],
    [["Y"], "Watch or listen"]
  ];
  const DEVICE_ROLE = { display: true, audio: true };
  // A held key presses again after REPEAT_DELAY, then every REPEAT_EVERY.
  const REPEAT_DELAY = 420;
  const REPEAT_EVERY = 150;
  // How often a slider being dragged sends, and how long it shows what it
  // sent before what the device reports (longer for a control with a
  // readBack: Remote.heldMs).
  const SLIDE_EVERY = 250;
  const SLIDE_HOLD = 6000;
  // Power off and All off take a second press this soon after the first.
  const OFF_AGAIN_MS = 3000;
  // How long lockOn() waits for the room to report a source it chose.
  const LOCK_WAIT_MS = 45000;
  // What a room's alert says while the house is not heard, as Listen's does.
  const NOT_CONNECTED = "Not connected to thermite. Reconnecting\u2026";

  function memoryStore() {
    const memory = {};
    return { get: (key) => (key in memory ? memory[key] : null), set: (key, value) => { memory[key] = value; } };
  }

  // The keys that press each button, for aria-keyshortcuts and help.
  function keyHints() {
    const byButton = { ok: ["Enter"] };
    Object.keys(KEYS).forEach((key) => {
      if (key === "=" || key === "_" || (key.length === 1 && key !== key.toLowerCase())) return;
      KEYS[key].forEach((button) => {
        (byButton[button] = byButton[button] || []).push(key === "+" ? "Plus" : key === "-" ? "Minus" : key);
      });
    });
    return byButton;
  }

  function create(options) {
    const config = options || {};
    const Remote = global.RemoteBackend;
    const backend = config.backend;
    const house = config.house || global.REMOTE_HOUSE;
    const layouts = config.layouts || global.REMOTE_LAYOUTS;
    const store = config.store || memoryStore();
    const listen = config.listen || null;
    const now = config.now || (() => Date.now());
    const later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
    const cancel = config.clearTimeout || ((handle) => global.clearTimeout(handle));
    // Only the mock can simulate the matrix; the views show its Item names and demo controls.
    const demo = Boolean(backend.simulateExternalSwitch);

    const listeners = [];
    // Ends what start() began hearing from the backend.
    let unsubscribe = null;
    const rooms = [];
    const roomsById = {};
    const states = {};
    // What each device openHAB drives last reported (backend getDevice).
    const devices = {};
    // A remote chosen over the buttons, per room; it holds while the room shows
    // what it showed then, and the buttons follow the next source after that.
    const picked = {};
    let current = null;
    // The mode chosen (mode()); undefined until the kept one is read.
    let chosen;
    let answering = backend.isMatrixAnswering ? backend.isMatrixAnswering() : true;
    let volumeMode = { slider: "slider", buttons: "buttons" }[config.volume || store.get("remote.volume")] || "buttons";
    // The volume slider: what it last set, for which room and device, and what it sent.
    const fader = { room: null, device: null, value: null, at: 0, dragging: false, sent: null, sentTo: null, timer: 0, release: 0 };
    let offArmed = null;
    // When All off was pressed once, and the timer that forgets it.
    const allOffArmed = { at: 0, timer: 0 };
    let inputsOf = null;
    let muteRelease = 0;
    const signalViews = Object.create(null);
    const signalConfirmed = Object.create(null);
    const signalSince = Object.create(null);
    let signalOpen = false;
    let signalRoom = null;
    let signalGeneration = 0;
    let signalTimer = 0;
    const SIGNAL_REFRESH = 10000;

    function emit(event) {
      listeners.slice().forEach((listener) => {
        try {
          listener(event);
        } catch (error) {
          console.error(error);
        }
      });
    }

    function toast(title, body, tone, tag) {
      emit({ type: "toast", title: title, body: body || "", tone: tone || "", tag: tag || "" });
    }

    function announce(message) {
      emit({ type: "announce", message: message });
    }

    const lockRequests = new Set();
    let lockError = null;
    const SWITCHING = "Wait for the room to finish switching and report its source.";
    const UNKNOWN = "Room lock status unknown";
    const clock = (value) => {
      const at = new Date(value);
      if (!Number.isFinite(at.getTime())) return "";
      const sameDay = at.toDateString() === new Date(now()).toDateString();
      return at.toLocaleString([], sameDay ? { hour: "2-digit", minute: "2-digit" } : { weekday: "short", hour: "2-digit", minute: "2-digit" });
    };
    // The current room's lock as both clients show it. The server decides;
    // this only explains its last answer. With no answer yet ("checking") or
    // after reads fail ("unavailable") the lock is unknown and holds nothing
    // back; only failed reads say so (text), so opening a room does not flash
    // a notice. unavailable: the server answered that it cannot evaluate the
    // room's saved policy, so it refuses every command there.
    function roomLock() {
      const access = backend.access, room = current;
      const enabled = Boolean(room && !room.listenOnly && access && !access.native && access.accountsExist());
      const policy = enabled ? access.policy(room.id) : null, status = enabled ? access.policyStatus(room.id) : "off";
      const manual = Boolean(policy?.manual_lock), locked = policy?.effective?.locked === true;
      const state = room && states[room.id];
      const settled = state && !state.pending && room.options.some(option => option.id === state.value);
      const busy = Boolean(room && lockRequests.has(room.id));
      const admin = enabled && access.canEdit(), canManage = admin && policy?.can_lock === true;
      const unavailable = policy?.effective?.reason === "policy_unavailable";
      const until = locked && !manual && policy.effective.until ? clock(policy.effective.until) : "";
      const text = status === "unavailable" ? UNKNOWN : lockText(unavailable, locked, manual, until);
      // An administrator manages a lock once it is known.
      const reason = busy || manual ? "" : !canManage ? (admin && !policy ? "Room lock status is not known yet." : "")
        : unavailable ? "Lock status is unavailable." : !settled ? SWITCHING : "";
      const error = lockError && lockError.room === room?.id && lockError.revision === policy?.revision ? lockError.message : "";
      return { room: room?.id || null, enabled, status, manual, locked, unavailable, busy, canManage, text, until, reason, error,
        disabled: busy || !canManage || Boolean(reason), label: busy ? "Saving…" : manual ? "Unlock room" : "Lock room" };
    }

    // A lock in words, as the remote and the room lists say it.
    function lockText(unavailable, locked, manual, until) {
      if (unavailable) return "Room lock status unavailable · controls paused";
      if (locked && manual) return "Locked · until unlocked";
      return locked ? "Locked by schedule" + (until ? " until " + until : "") : "";
    }

    // A room's lock as the room lists show it: the current room's exactly as
    // its remote does (roomLock), any other from the gateway's summary of
    // every room, which the lists ask for with access.watchLocks.
    function roomLocked(roomId) {
      const access = backend.access;
      if (current && current.id === roomId && access?.policy(roomId)) {
        const lock = roomLock();
        return { locked: lock.locked, text: lock.locked ? lock.text : "" };
      }
      const lock = access && !access.native ? access.lockSummary(roomId) : null;
      if (!lock?.locked) return { locked: false, text: "" };
      return { locked: true, text: lockText(lock.reason === "policy_unavailable", true, lock.manual,
        !lock.manual && lock.until ? clock(lock.until) : "") };
    }

    async function toggleRoomLock(expectedRoom) {
      const lock = roomLock(), room = current;
      if (!room || expectedRoom && expectedRoom !== room.id || lock.disabled) return false;
      const policy = backend.access.policy(room.id), desired = !lock.manual;
      lockError = null;
      lockRequests.add(room.id); emit({ type: "change" });
      try {
        const saved = await backend.access.setRoomLock(room.id, policy.revision, desired);
        if (!saved) return false;
        toast(room.label, desired ? "Room locked until you unlock it."
          : saved.effective?.locked ? "Manual lock released. The room is still locked by its schedule." : "Room unlocked.");
        return true;
      } catch (error) {
        if (error.stale) return false;
        // Kept beside the control until the room's policy changes; a toast alone vanishes too soon.
        lockError = { room: room.id, revision: backend.access.policy(room.id)?.revision, message: error.message };
        toast(room.label, error.message, "bad");
        return false;
      } finally { lockRequests.delete(room.id); emit({ type: "change" }); }
    }

    // Locks the room on a source or Off. One the room already reports is
    // locked at once; any other is chosen first, as a tap would, and locked
    // once the room reports it. The server holds only what it still reads
    // (desired), so a change in between is refused, never captured.
    async function lockOn(sourceId, expectedRoom) {
      const lock = roomLock(), room = current, target = room && option(room, sourceId);
      if (!target || expectedRoom && expectedRoom !== room.id || lock.busy || !lock.canManage || lock.manual
          || lock.status !== "ready") return false;
      const state = states[room.id];
      if (state.pending) {
        toast(room.label, SWITCHING, "muted");
        return false;
      }
      const shown = state.value === sourceId;
      if (!shown && refused()) return false;
      lockError = null;
      lockRequests.add(room.id); emit({ type: "change" });
      try {
        if (!shown) await chooseReported(room, target);
        const saved = await backend.access.setRoomLock(room.id, backend.access.policy(room.id)?.revision, true, sourceId);
        if (!saved) return false;
        toast(room.label, "Room locked " + (sourceId === Remote.OFF ? "off" : "on " + target.label) + " until you unlock it.");
        return true;
      } catch (error) {
        if (error.stale) return false;
        lockError = { room: room.id, revision: backend.access.policy(room.id)?.revision, message: error.message };
        toast(room.label, error.message, "bad");
        return false;
      } finally { lockRequests.delete(room.id); emit({ type: "change" }); }
    }

    // Chooses target as a tap would and resolves once the room reports it
    // settled; rejects when the choice is refused, the room settles on
    // anything else, or LOCK_WAIT_MS pass first.
    function chooseReported(room, target) {
      return new Promise((resolve, reject) => {
        // seen: the room switched to target; sent: the choice was taken. A
        // refused one settles the room back first, which is not the answer.
        let seen = false, sent = false;
        const finish = (message) => {
          stop(); cancel(timer);
          if (message) reject(new Error(message)); else resolve();
        };
        const check = () => {
          const state = states[room.id];
          if (state.pending === target.id) seen = true;
          if (!seen || state.pending) return;
          if (state.value === target.id) return finish("");
          const shown = option(room, state.value);
          if (sent) finish(shown ? "The room shows " + shown.label + ", not " + target.label + ", so it was not locked."
            : "The room did not confirm " + target.label + ", so it was not locked.");
        };
        const stop = subscribe((event) => { if (event.type === "change") check(); });
        const timer = later(() => finish("The room did not report " + target.label + " within " + LOCK_WAIT_MS / 1000
          + " seconds, so it was not locked."), LOCK_WAIT_MS);
        delete picked[room.id];
        backend.choose(room.id, target.id).then(() => { seen = sent = true; check(); },
          (error) => finish("Could not switch to " + target.label + ", so the room was not locked. " + error.message));
      });
    }

    // What a bubble's menu (right-click, a long press, or a key) offers an
    // administrator: lock the room on that source or Off, unlock it, or keep
    // a schedule's lock on what the room shows. Empty: no menu.
    function lockMenu(item) {
      const lock = roomLock(), room = current;
      if (!room || !item.choose || !lock.canManage || lock.status !== "ready") return [];
      const entry = (label, run) => ({ label: label, run: run, disabled: lock.busy });
      if (lock.manual) return [entry("Unlock room", () => toggleRoomLock(room.id))];
      if (lock.locked) {
        return bubbleState(item, states[room.id]) === "idle" ? [] : [entry("Lock until unlocked", () => lockOn(item.id, room.id))];
      }
      return [entry(item.id === Remote.OFF ? "Lock off" : "Lock on " + item.label, () => lockOn(item.id, room.id))];
    }

    // Why ordinary controls cannot act on the current room, or null. Only a
    // known lock holds them back; while it is unknown they send, and the
    // server, which checks every command, refuses one for a locked room.
    // Both clients show this one answer.
    function controlBlock() {
      const access = backend.access;
      if (!current || current.listenOnly || !access || access.canControl(current.id)) return null;
      const lock = roomLock();
      if (lock.unavailable) return "Room lock status is unavailable, so controls are paused.";
      if (lock.locked) {
        return lock.manual ? "This room is locked until an administrator unlocks it."
          : "This room is locked by its schedule" + (lock.until ? " until " + lock.until : "") + ".";
      }
      return "Room control is unavailable.";
    }

    // True, after saying why, when ordinary controls must not send.
    function refused() {
      const reason = controlBlock();
      if (reason) toast("Read-only", reason, "muted");
      return Boolean(reason);
    }

    function subscribe(callback) {
      listeners.push(callback);
      return () => {
        const at = listeners.indexOf(callback);
        if (at >= 0) listeners.splice(at, 1);
      };
    }

    // A room of speakers alone as the routed rooms' getRooms() has them: no
    // sources, no controls, its speakers as its devices.
    function speakerRoom(entry) {
      const devices = (entry.speakers || []).filter((id) => house.devices[id]).map((id) => {
        const device = house.devices[id];
        return { id: id, label: device.label, short: device.short || device.label, kind: device.kind, model: device.model };
      });
      return { id: entry.id, label: entry.label, item: null, options: [], order: [], display: null, audio: null, devices: devices,
        controls: [], speakers: devices.map((device) => device.id), listenOnly: true };
    }

    function start(roomId) {
      unsubscribe = backend.subscribe(onEvent);
      return backend.getRooms()
        .then((list) => {
          list.forEach((room) => {
            room.speakers = ((house.rooms || []).find((entry) => entry.id === room.id) || {}).speakers || [];
            rooms.push(room);
            roomsById[room.id] = room;
          });
          // Rooms of speakers alone, for a front end that can play them.
          (listen ? house.speakerRooms || [] : []).forEach((entry) => {
            const room = speakerRoom(entry);
            rooms.push(room);
            roomsById[room.id] = room;
            // Nothing to switch: always Off, as the remote's watch half sees it.
            states[room.id] = { room: room.id, item: null, value: Remote.OFF, pending: null, status: "current", reason: "" };
          });
          const driven = Object.keys(house.devices).filter((id) => Remote.controlOf(house, id));
          const reading = driven.map((id) => backend.getDevice(id).then((state) => { devices[id] = state; }, console.warn));
          // A room that cannot be read yet shows "Reading…" until its next update.
          return Promise.all(reading).then(() => Promise.all(list.map((room) => backend.getState(room.id).catch((error) => {
            console.warn(error);
            return { room: room.id, item: room.item, value: "NULL", pending: null, status: "unknown", reason: "" };
          }))));
        })
        .then((list) => {
          // Another room's read can delay this list while newer state events
          // have already arrived. Snapshots only fill rooms not yet heard from.
          list.forEach((state) => {
            if (!Object.prototype.hasOwnProperty.call(states, state.room)) states[state.room] = state;
            rememberSignalSource(state.room, states[state.room]);
          });
          // A room asked for brings its mode, as does the room kept last while
          // no mode is kept; a kept mode stays, on its kept room while that
          // room still has it, else on the first of its rooms.
          const shown = mode(), modeKept = kinds().includes(chosen);
          const kept = [store.get("remote.room." + shown), store.get("remote.room")]
            .map((id) => roomsById[id]).find((room) => room && (!modeKept || modes(room).includes(shown)));
          setRoom(roomId || (kept && kept.id));
        });
    }

    // The session hears no more of the house and reads no more signal, so a
    // front end that forgets one need not keep its backend from it. The
    // slider's last volume is sent first, as Listen's sessions send theirs;
    // All off forgets its first press, which only its timer would have.
    function stop() {
      if (unsubscribe) unsubscribe();
      unsubscribe = null;
      closeDiagnostics();
      if (fader.timer) { cancel(fader.timer); sendVolume(); }
      [fader.timer, fader.release, muteRelease, allOffArmed.timer].forEach(cancel);
      fader.timer = fader.release = muteRelease = allOffArmed.timer = allOffArmed.at = 0;
    }

    // ---- state -------------------------------------------------------------

    function onEvent(event) {
      if (event.type === "access") {
        if (event.unauthorized) {
          closeDiagnostics();
          for (const cache of [signalViews, signalConfirmed, signalSince]) Object.keys(cache).forEach((id) => delete cache[id]);
          cancel(fader.timer); cancel(fader.release); cancel(muteRelease); cancel(allOffArmed.timer);
          Object.assign(fader, { room: null, device: null, value: null, at: 0, dragging: false, sent: null, sentTo: null, timer: 0, release: 0 });
          muteRelease = 0; inputsOf = null; offArmed = null; allOffArmed.at = 0; allOffArmed.timer = 0;
          Object.keys(devices).forEach((id) => delete devices[id]);
          Object.keys(states).filter((id) => !roomsById[id] || !roomsById[id].listenOnly)
            .forEach((id) => { states[id] = { room: id, value: "NULL", pending: null, status: "unknown", reason: "" }; });
        }
        emit(event); emit({ type: "change" }); return;
      }
      if (event.type === "activity") {
        emit(event);
        return;
      }
      if (event.type === "connection") {
        // Not a passing message: view() says it for as long as it lasts.
        emit({ type: "change" });
        announce(event.connected ? "Connected to thermite again" : "Not connected to thermite");
        return;
      }
      if (event.type === "matrix") {
        answering = event.answering;
        emit({ type: "matrix", answering: answering });
        toast(answering ? "HDMI matrix answering again" : "HDMI matrix stopped answering",
          "The next poll reads every room back", answering ? "" : "bad");
        return;
      }
      if (event.type === "problem" || event.type === "playing") {
        emit({ type: "change" });
        return;
      }
      if (event.type === "device") {
        const previous = devices[event.device];
        devices[event.device] = event.state;
        // The slider stops showing what it sent once the device reports it, and
        // sends that value again once the device has moved off it (its own knob).
        if (fader.device === event.device && event.state.level === fader.value && !fader.dragging) fader.at = 0;
        if (fader.sentTo === event.device && event.state.level !== fader.sent) fader.sent = null;
        emit({ type: "device", device: event.device });
        if ((Remote.controlOf(house, event.device) || {}).playback
            && ["playback", "power", "input"].some((field) => !previous || previous[field] !== event.state[field])) {
          emit({ type: "change" });
        }
        return;
      }
      if (event.type !== "state") return;
      const before = states[event.room];
      states[event.room] = event.state;
      const choice = picked[event.room];
      if (choice && choice.value !== Remote.showing(event.state)) delete picked[event.room];
      rememberSignalSource(event.room, event.state);
      if (signalOpen && signalRoom === event.room && differs(before, event.state)) refreshDiagnostics();
      emit({ type: "change" });
      if (current && event.room === current.id && mode() === "watch" && differs(before, event.state)) {
        announce(current.label + ": " + view(current, event.state).spoken);
      }
    }

    function differs(before, after) {
      return !before || before.value !== after.value || before.pending !== after.pending || before.status !== after.status
        || (before.unconfirmed || null) !== (after.unconfirmed || null);
    }

    // Whether the house is heard: false once the backend has lost its stream,
    // until it reads the house again. What the rooms show is from before then.
    function connected() {
      return backend.connected !== false;
    }

    function option(room, id) {
      return room.options.find((candidate) => candidate.id === id) || null;
    }

    // What a room shows, for the room list, announcements and the alert line.
    // In Listen (and in a room of speakers alone), what its speakers play
    // (listen status). Else its source: the alert names the source's problem,
    // then a choice that was never read back, then each of the room's
    // devices openHAB cannot drive (backend problemOf), after its label;
    // playing is what its source plays, else its audio device ("Disney+ ·
    // DuckTales"), or null. Failure is part of what a room shows, for as long
    // as it lasts: connected is false while the house is not heard, when led
    // and short are what was last read and the alert says only that;
    // unconfirmed is the label of a source chosen, sent and never read back
    // (backend getState), and unconfirmedId its id, by which a client finds
    // its bubble; both null otherwise, and in Listen, where nothing is chosen.
    function view(room, state) {
      if (room.listenOnly || (listen && room.speakers.length && mode() === "listen")) {
        return Object.assign({}, listen ? listen.status(room.id) : { led: "off", short: "\u2026", spoken: "reading", playing: null },
          { connected: connected(), unconfirmed: null, unconfirmedId: null });
      }
      const shown = sourceView(room, state);
      shown.connected = connected();
      const unread = state.unconfirmed ? option(room, state.unconfirmed) : null;
      shown.unconfirmed = state.unconfirmed ? (unread ? unread.label : state.unconfirmed) : null;
      shown.unconfirmedId = state.unconfirmed || null;
      shown.playing = state.status === "current" && option(room, state.value) ? playing(room, state.value) : null;
      // Nothing on the screen that says what plays: what its speakers play.
      if (!shown.playing && listen && room.speakers.length) shown.playing = listen.status(room.id).playing;
      const unconfirmed = shown.unconfirmed
        ? shown.unconfirmed + " was not confirmed: " + room.label + " did not report it. Check the room before choosing again" : null;
      if (unconfirmed) shown.spoken += ". " + shown.unconfirmed + " was not confirmed";
      const problems = [shown.problem, unconfirmed].concat(room.devices.map((device) => {
        const problem = backend.problemOf(device.id);
        return problem ? device.label + " " + problem : null;
      })).filter(Boolean);
      if (problems.length) shown.problem = problems.join(". ");
      if (!shown.connected) {
        shown.problem = NOT_CONNECTED;
        shown.spoken += ". Not connected to thermite";
      }
      return shown;
    }

    function playing(room, value) {
      const text = (id) => {
        const now = id ? backend.playingOf(id) : null;
        return now ? [now.app, now.title].filter(Boolean).join(" \u00b7 ") : null;
      };
      return text(value === Remote.OFF ? null : value) || text(room.audio && room.audio.id);
    }

    function sourceView(room, state) {
      if (state.status === "switching") {
        const target = option(room, state.pending);
        const label = target ? target.label : state.pending;
        return { led: "busy", short: "Switching to " + label, spoken: "switching to " + label };
      }
      if (state.status === "unavailable") {
        const reason = state.reason || "The matrix did not answer";
        return { led: "bad", short: "Unknown", spoken: "source unknown. " + reason, problem: "Source unknown: " + reason };
      }
      if (state.status === "unknown") return { led: "off", short: "…", spoken: "reading" };
      const source = option(room, state.value);
      if (!source) {
        const problem = "Source unknown: the matrix shows an input " + room.label + " does not offer";
        return { led: "bad", short: "Unknown", spoken: problem, problem: problem };
      }
      if (source.id === Remote.OFF) return { led: "off", short: "Off", spoken: "Off" };
      return { led: "ok", short: source.label, spoken: source.label };
    }

    function setRoom(id) {
      const room = roomsById[id] || listed()[0] || rooms[0];
      if (!room) return null;
      current = room;
      // A room of the other mode alone brings its mode with it.
      if (!modes(room).includes(mode()) && modes(room).length) keepMode(modes(room)[0]);
      store.set("remote.room." + mode(), room.id);
      // A room of speakers alone has no lock of its own: its speakers' rooms' are listen's.
      if (backend.access && !backend.access.native) backend.access.watchRoom(room.listenOnly ? null : room.id);
      store.set("remote.room", room.id);
      if (listen) listen.setRoom(room.id);
      if (signalOpen) { signalRoom = room.id; refreshDiagnostics(); }
      return room;
    }

    function selectRoom(id, speak) {
      const room = setRoom(id);
      if (!room) return;
      emit({ type: "change" });
      if (speak) announce(room.label + ": " + view(room, states[room.id]).spoken);
    }

    function stepRoom(delta) {
      const list = listed();
      if (!current || !list.length) return;
      const index = list.indexOf(current);
      selectRoom(list[(index + delta + list.length) % list.length].id, true);
    }

    function choose(sourceId) {
      if (!current || refused()) return Promise.resolve(null);
      return backend.choose(current.id, sourceId).catch((error) => {
        toast("Could not choose", error.message, "bad");
        return null;
      });
    }

    // Choosing what the room already shows sends nothing; either way the
    // buttons go back to the source.
    function chooseSource(sourceId) {
      delete picked[current.id];
      if (sourceId === Remote.showing(states[current.id])) {
        emit({ type: "change" });
        return Promise.resolve(states[current.id]);
      }
      return choose(sourceId);
    }

    // 0 is Off, then the room's sources in order: the id chosen, or null.
    function chooseDigit(digit) {
      if (current && mode() === "listen") {
        const speaker = listen.speakers().find((entry) => entry.digit === String(digit));
        if (!speaker) return null;
        listen.choose(speaker.id);
        return speaker.id;
      }
      const choice = current && current.options[Number(digit)];
      if (!choice || refused()) return null;
      chooseSource(choice.id);
      return choice.id;
    }

    function canPress(button, target) {
      if (!current) return false;
      const to = Remote.AUDIO[button] ? current.audio : target || activeTarget(controls());
      return Boolean(to && Remote.buttonAvailable(house, to.id, button));
    }

    function press(button) {
      if (!current || refused()) return Promise.resolve(null);
      if (remoteHidden()) {
        toast(spec(button) ? spec(button).label : button, "Nothing is on in the " + current.label + ": choose a source first", "muted");
        return Promise.resolve(null);
      }
      const target = activeTarget(controls());
      if (!canPress(button, target)) {
        toast(spec(button) ? spec(button).label : button,
          (target && target.availability && target.availability.note) || "This control is not available", "muted");
        return Promise.resolve(null);
      }
      const control = target && !Remote.AUDIO[button] ? Remote.controlOf(house, target.id) : null;
      if (button === "input" && control && control.input) {
        openInputs(target);
        return Promise.resolve(null);
      }
      if (button === "power" && control && !offConfirmed(target, control)) return Promise.resolve(null);
      return backend.press(current.id, target ? target.id : null, button)
        .then((result) => {
          if (button === "mute") holdMute(result.device && result.device.id);
          if (result.volume !== undefined) {
            held(result.device.id, result.volume);
            fader.sent = null;
            // The slider shows the new volume; a toast would cover the screen while a key repeats.
            if (sliderShown()) return result;
          }
          if (result.device) {
            toast(result.label, result.note || "→ " + result.device.label, "", result.sent ? "" : result.tag || "Not sent");
          } else {
            toast(result.label, result.note, "muted");
          }
          return result;
        }, (error) => {
          if (button === "mute") emit({ type: "audio" });
          toast(spec(button) ? spec(button).label : button, error.message, "muted");
          return null;
        });
    }

    // A key for a button the remote on show lacks still asks the backend,
    // which says why. Returns the button pressed.
    function pressKey(candidates) {
      if (current && mode() === "listen") return listen.key(candidates);
      if (refused()) return null;
      const target = activeTarget(controls());
      const offered = target ? Remote.buttonsOf(target.remote) : [];
      const button = candidates.find((id) => Remote.AUDIO[id] || offered.indexOf(id) >= 0) || candidates[0];
      press(button);
      return button;
    }

    // Mute lights for a press at once, as the slider holds a volume just set,
    // and shows what the device reports once it does or the hold ends.
    function holdMute(deviceId) {
      cancel(muteRelease);
      muteRelease = later(() => emit({ type: "audio" }), Remote.heldMs(Remote.controlOf(house, deviceId), Remote.AIM_MS.mute) + 50);
      emit({ type: "audio" });
    }

    function muted(audio, state) {
      const sent = backend.aimed(audio.id, "mute");
      return (sent !== null ? sent : state.mute) === "ON";
    }

    // Turning a device off takes a second press: a stray tap would end the film.
    function offConfirmed(target, control) {
      const state = devices[target.id];
      if (!control.power || !state || state.power !== "ON") return true;
      if (offArmed && offArmed.device === target.id && now() - offArmed.at < OFF_AGAIN_MS) {
        offArmed = null;
        return true;
      }
      offArmed = { device: target.id, at: now() };
      toast("Power", "Press again to turn off the " + target.short);
      return false;
    }

    // All off takes a second press too; true when this press sent it.
    function allOff() {
      if (!allOffArmed.at) {
        armAllOff(true);
        return false;
      }
      armAllOff(false);
      backend.allOff()
        .then((result) => toast(result.label, result.note, "", result.sent ? "" : result.tag || "Not sent"))
        .catch((error) => toast("Could not turn everything off", error.message, "bad"));
      return true;
    }

    function armAllOff(armed) {
      cancel(allOffArmed.timer);
      allOffArmed.at = armed ? now() : 0;
      if (armed) allOffArmed.timer = later(() => armAllOff(false), OFF_AGAIN_MS);
      emit({ type: "all-off", armed: armed });
      if (armed) announce("Press All off again to turn off every room and device");
    }

    function openInputs(target) {
      inputsOf = target.id;
      emit({ type: "inputs", device: target.id, title: target.short + " input" });
    }

    // The input list: [{value, label, inUse}].
    function inputOptions() {
      const control = Remote.controlOf(house, inputsOf);
      if (!control || !control.input) return [];
      const state = devices[inputsOf] || {};
      return control.input.options.map((entry) => ({ value: entry[0], label: entry[1], inUse: state.input === entry[0] }));
    }

    function chooseInput(value) {
      if (refused()) return Promise.resolve(null);
      return backend.setInput(current.id, inputsOf, value)
        .then((result) => {
          toast(result.device.short + " input", result.note, "", result.sent ? "" : result.tag || "Not sent");
          return result;
        }, (error) => {
          toast("Could not choose the input", error.message, "bad");
          return null;
        });
    }

    function operate(controlId, actionId) {
      if (refused()) return Promise.resolve(null);
      return backend.operate(current.id, controlId, actionId)
        .then((result) => {
          toast(result.label, result.note, "muted", result.sent ? "" : "Not connected");
          return result;
        }, (error) => {
          toast("Could not operate", error.message, "bad");
          return null;
        });
    }

    // ---- volume --------------------------------------------------------------

    // Hold the slider at a volume just set, until the device reports it.
    function held(deviceId, value) {
      Object.assign(fader, { room: current.id, device: deviceId, value: value, at: now() });
      // A device that ignored the volume (in standby) reports nothing, so the
      // slider goes back to what it last reported when the hold ends.
      cancel(fader.release);
      fader.release = later(() => emit({ type: "audio" }), Remote.heldMs(Remote.controlOf(house, deviceId), SLIDE_HOLD) + 50);
      emit({ type: "audio" });
    }

    function sliderShown() {
      return volumeMode === "slider" && Boolean(current && current.audio);
    }

    // The slider moved to value; final when it was let go.
    function slide(value, final) {
      const audio = current && current.audio;
      if (!audio || controlBlock() || remoteHidden()) return;
      if (final) fader.dragging = false;
      const control = Remote.controlOf(house, audio.id);
      if (!control || !control.volume) return;
      const volume = control.volume;
      const number = Number(value);
      if (!Number.isFinite(number)) return;
      held(audio.id, Remote.volumeValue(volume, number));
      if (!fader.timer) sendVolume();
    }

    function setDragging(dragging) {
      fader.dragging = Boolean(dragging);
    }

    // At most one volume every SLIDE_EVERY while the slider moves, and always the last.
    function sendVolume() {
      fader.timer = 0;
      if (fader.value === fader.sent && fader.device === fader.sentTo) return;
      fader.sent = fader.value;
      fader.sentTo = fader.device;
      backend.setVolume(fader.room, fader.value)
        .then((result) => {
          if (!result.sent && result.tag !== "Mock") toast(result.label, result.note, "muted", result.tag);
        })
        .catch((error) => toast("Volume", error.message, "bad"));
      fader.timer = later(sendVolume, SLIDE_EVERY);
    }

    function setVolumeMode(mode) {
      volumeMode = mode === "slider" ? "slider" : "buttons";
      store.set("remote.volume", volumeMode);
      emit({ type: "change" });
    }

    // The volume keys or the slider, and Mute lit while the room's audio is muted.
    function audioView() {
      const audio = current ? current.audio : null;
      const control = audio ? Remote.controlOf(house, audio.id) : null;
      const state = audio ? devices[audio.id] : null;
      const volume = control && control.volume;
      const recent = Boolean(audio) && fader.device === audio.id
        && (fader.dragging || now() - fader.at < Remote.heldMs(control, SLIDE_HOLD));
      const value = !volume ? null : recent ? fader.value : state ? state.level : null;
      const isMuted = Boolean(control && control.mute && state) && muted(audio, state);
      let caption = audio ? audio.short : "No audio";
      if (audio && !volume) caption += " · not connected";
      else if (volume && value === null) caption += state && state.volume === "UNDEF" ? " · not answering" : " · reading…";
      else if (volume) caption += " · " + Remote.volumeText(volume, value, state && state.volumeScale);
      if (volume && isMuted) caption += " · muted";
      return {
        audio: audio,
        short: audio ? audio.short : "No audio",
        slider: volumeMode === "slider",
        // Mute lit, unlit, or null while there is no mute to show.
        muted: control && control.mute && state ? isMuted : null,
        value: value,
        // The device's own scale: its min, max and step, nothing rescaled.
        max: volume ? volume.max : 100,
        min: volume ? volume.min : 0,
        step: volume ? Remote.volumeStep(volume) : 1,
        disabled: value === null,
        // How far along that scale the slider's track is drawn, 0-100.
        fill: volume && volume.max > volume.min
          ? Math.min(100, Math.max(0, (((value === null ? volume.min : value) - volume.min) / (volume.max - volume.min)) * 100)) : 0,
        dragging: fader.dragging,
        caption: caption,
        label: "Volume" + (audio ? ", " + audio.short : ""),
        text: volume && value !== null ? Remote.volumeText(volume, value, state && state.volumeScale) : caption
      };
    }

    // ---- the buttons -------------------------------------------------------

    function controls() {
      if (current.listenOnly) return { targets: [], preferred: null };
      return Remote.controlsFor(house, current.id, Remote.showing(states[current.id]));
    }

    // ---- watch and listen ---------------------------------------------------

    // What a room offers: "watch" where it has sources, "listen" where it
    // has speakers and a listen to drive them.
    function modes(room) {
      const of = room || current;
      if (!of) return [];
      return (of.options.length ? ["watch"] : []).concat(listen && of.speakers.length ? ["listen"] : []);
    }

    // The modes any room has, Watch first.
    function kinds() {
      return ["watch", "listen"].filter((kind) => rooms.some((room) => modes(room).includes(kind)));
    }

    // The mode on show in every room: the one chosen last, else the first
    // kind. The kept choice is read once: another page choosing changes the
    // next page opened, never this one under its room.
    function mode() {
      const offered = kinds();
      if (chosen === undefined) chosen = store.get("remote.mode");
      return offered.includes(chosen) ? chosen : offered[0] || "watch";
    }

    function keepMode(next) {
      chosen = next;
      store.set("remote.mode", next);
    }

    // The rooms of the mode on show, as the room lists have them.
    function listed() {
      const shown = mode();
      return rooms.filter((room) => modes(room).includes(shown));
    }

    // A room without the mode gives way to the room last on show in it.
    function setMode(next) {
      if (!current || !kinds().includes(next) || mode() === next) return false;
      keepMode(next);
      if (modes(current).includes(next)) store.set("remote.room." + next, current.id);
      else {
        const last = roomsById[store.get("remote.room." + next)];
        setRoom(last && modes(last).includes(next) ? last.id : listed()[0].id);
      }
      emit({ type: "change" });
      announce(current.label + ": " + (next === "listen" ? "Listen" : "Watch"));
      return true;
    }

    function toggleMode() {
      return kinds().length > 1 && setMode(mode() === "watch" ? "listen" : "watch");
    }

    // Listen to one of the room's speakers (a remote's volume caption names
    // the room's sound): true once on show.
    function listenTo(deviceId) {
      if (!current || !modes().includes("listen") || !current.speakers.includes(deviceId)) return false;
      listen.choose(deviceId);
      if (mode() !== "listen") setMode("listen");
      return true;
    }

    // A room that is off shows no remote until a device bubble is tapped.
    function activeTarget(list) {
      const choice = picked[current.id];
      const showing = Remote.showing(states[current.id]);
      const kept = choice && choice.value === showing && list.targets.find((target) => target.id === choice.device);
      return kept || (showing === Remote.OFF ? null : list.targets.find((target) => target.id === list.preferred) || null);
    }

    // True while the clients draw no remote at all: no keys, no volume.
    function remoteHidden(list) {
      return Remote.showing(states[current.id]) === Remote.OFF && !activeTarget(list || controls());
    }

    // The bubble with the selected look: the remote on show, or Off while there is none.
    function selectedBubble(list) {
      const target = activeTarget(list);
      return target ? target.id : remoteHidden(list) ? Remote.OFF : null;
    }

    function pick(deviceId) {
      picked[current.id] = { device: deviceId, value: Remote.showing(states[current.id]) };
      emit({ type: "change" });
    }

    function remoteOf(target) {
      return target ? layouts.remotes[target.remote] : null;
    }

    function spec(button) {
      return layouts.buttons[button];
    }

    // The device a button drives, as its label says: Volume and Mute the room's audio.
    function drives(button, target) {
      if (Remote.AUDIO[button]) return current.audio ? current.audio.label : current.label + " has no audio device in the inventory";
      return target ? target.label : "";
    }

    // The bubbles over the buttons: Off, then the room's sources, which switch
    // the room, in the room's order, then, set apart, its display and audio
    // device. Every bubble but Off brings up its buttons.
    function bubbles(list) {
      const items = current.options.map((choice, index) => ({
        id: choice.id, label: choice.label, kind: choice.kind, role: "source", choose: true,
        digit: index < 10 ? String(index) : "", remote: (house.sources[choice.id] || {}).remote || null
      }));
      list.targets.forEach((target) => {
        if (items.some((item) => item.id === target.id)) return;
        items.push({ id: target.id, label: target.short, kind: target.kind, role: target.role, choose: false, digit: "", remote: target.remote });
      });
      const rank = (item) => {
        if (item.id === Remote.OFF) return -1;
        if (DEVICE_ROLE[item.role]) return current.order.length + 1;
        const at = current.order.indexOf(item.id);
        return at < 0 ? current.order.length : at;
      };
      return items.map((item, index) => [rank(item), index, item])
        .sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((entry) => entry[2]);
    }

    // A bubble lights for what the room shows, or blinks for a choice still switching.
    function bubbleState(item, state) {
      if (!item.choose) return "idle";
      if (state.pending) return state.pending === item.id ? "switching" : "idle";
      return state.value === item.id ? "current" : "idle";
    }

    function bubbleLabel(item, shown) {
      if (!item.choose) return item.label + ", " + (KIND_NAME[item.kind] || item.kind);
      const label = shown === "switching" ? item.label + ", switching"
        : item.id === Remote.OFF ? (shown === "current" ? "Off, the room is off" : "Off: turn the room off")
          : item.label + (shown === "current" ? ", on the screen" : ", source");
      return bubbleLock(item, shown).locked ? label + ". " + roomLock().text : label;
    }

    // A locked room's padlock takes the LED of the bubble lit for what it
    // shows. While ordinary controls are blocked every other bubble is inert;
    // the lit one still answers a tap with why (tapBubble).
    function bubbleLock(item, shown) {
      const locked = shown !== "idle" && roomLock().locked;
      return { locked: locked, inert: !locked && Boolean(controlBlock()) };
    }

    // What the remote's face says about the lock in words: why a known lock
    // holds controls back, unless a padlock bubble shows it, or that a failed
    // read left it unknown (controlBlock() is null then). Nothing during the
    // first read.
    function lockNotice() {
      const lock = roomLock();
      if (lock.status === "unavailable") return lock.text;
      const reason = controlBlock();
      if (!reason) return "";
      const showing = Remote.showing(states[current.id]);
      return lock.locked && current.options.some((choice) => choice.id === showing) ? "" : reason;
    }

    // Off and the room's sources switch the room; any other bubble only brings
    // up its device's buttons. Neither acts while the room is read-only.
    function tapBubble(item) {
      if (refused()) return;
      if (item.choose) {
        chooseSource(item.id);
        return;
      }
      pick(item.id);
    }

    // What the info panel lists for the room: what the buttons control, its
    // room controls (not connected yet) and its devices.
    function info() {
      const list = controls();
      const target = activeTarget(list);
      const audio = current.audio;
      const remote = remoteOf(target);
      const playback = target && (Remote.controlOf(house, target.id) || {}).playback;
      const others = list.targets.filter((candidate) => !target || candidate.id !== target.id);
      return {
        rows: [
          ["Buttons", target ? target.label + " · " + remote.label : current.label + " shows nothing: choose a source"],
          ["Volume, mute", audio ? audio.label : current.label + " has no audio device in the inventory"],
          ["Other remotes here", others.length ? others.map((candidate) => candidate.label).join(", ") : "None"]
        ].concat(playback ? [[playback.label || "Playback", (() => {
          // The integration names its playback service and the input that carries it.
          const state = backend.deviceSnapshot(target.id);
          if (state.power !== "ON" || playback.input && state.input !== playback.input) return "Not active" + (playback.activate ? " · select " + playback.activate : "");
          return state.playback === "PLAY" ? "Playing" : state.playback === "PAUSE" ? "Paused or stopped" : "Unknown";
        })()]] : []),
        controls: current.controls.map((control) => ({
          id: control.id, label: control.label, device: control.device.label, connected: control.connected,
          actions: control.actions.map((action) => ({ id: action.id, label: action.label }))
        })),
        devices: current.devices.map((device) => {
          const roles = [];
          if (current.display && current.display.id === device.id) roles.push("Screen");
          if (current.audio && current.audio.id === device.id) roles.push("Volume");
          return { id: device.id, label: device.label, roles: roles,
            meta: [KIND_NAME[device.kind] || device.kind, device.model].filter(Boolean).join(" · ") };
        })
      };
    }

    // Signal reads are independent of controls and only run while a view is
    // open. Room/source changes invalidate late replies from the old path.
    function rememberSignalSource(roomId, state) {
      const room = house.rooms.find((candidate) => candidate.id === roomId);
      if (!room || !state || !(state.value === Remote.OFF || (room.sources || []).includes(state.value))) return;
      // Keep the last known choice through UNDEF: A → UNDEF → B is still a
      // confirmed source transition. The initial NULL → A has no prior path.
      if (signalConfirmed[roomId] && signalConfirmed[roomId] !== state.value) {
        signalSince[roomId] = now();
        delete signalViews[roomId];
      }
      signalConfirmed[roomId] = state.value;
    }

    function diagnostics(roomId) {
      const id = roomId || (current && current.id);
      const view = signalViews[id];
      const state = states[id];
      return view && state && view.confirmedValue === state.value && view.acquiredAfter === (signalSince[id] || 0) ? view.model : null;
    }

    function refreshDiagnostics() {
      cancel(signalTimer);
      signalTimer = 0;
      const id = signalRoom;
      // A room of speakers alone has no signal path: nothing is read while it shows.
      if (!signalOpen || !id || !states[id] || roomsById[id].listenOnly) return Promise.resolve(null);
      const state = states[id];
      const acquiredAfter = signalSince[id] || 0;
      const generation = ++signalGeneration;
      // A backend that throws reads as one that answered nothing.
      const reading = new Promise((resolve) => resolve(backend.getDiagnostics
        ? backend.getDiagnostics(id, state.value, state.pending, acquiredAfter) : null));
      return reading.catch(() => null).then((model) => {
        if (!signalOpen || generation !== signalGeneration || signalRoom !== id) return null;
        if (!model && global.RemoteDiagnostics) model = global.RemoteDiagnostics.unavailable(id, roomsById[id].label, now());
        signalViews[id] = { confirmedValue: state.value, acquiredAfter: acquiredAfter, model: model };
        emit({ type: "diagnostics", room: id });
        signalTimer = later(refreshDiagnostics, SIGNAL_REFRESH);
        return model;
      });
    }

    function openDiagnostics(roomId) {
      const id = roomId || (current && current.id);
      if (!roomsById[id] || roomsById[id].listenOnly) return Promise.resolve(null);
      signalOpen = true;
      signalRoom = id;
      return refreshDiagnostics();
    }

    function closeDiagnostics() {
      signalOpen = false;
      signalRoom = null;
      signalGeneration += 1;
      cancel(signalTimer);
      signalTimer = 0;
      if (backend.cancelDiagnostics) backend.cancelDiagnostics();
    }

    // ---- the mock's demo ---------------------------------------------------

    function simulateExternal() {
      if (!current || current.listenOnly || !demo) return;
      const state = states[current.id];
      const index = current.options.findIndex((candidate) => candidate.id === state.value);
      const next = current.options[(index + 1) % current.options.length];
      backend.simulateExternalSwitch(current.id, next.id);
      toast("Switched at the matrix", next.label + " to " + current.label + "; shown after the next poll");
    }

    function toggleMatrix() {
      if (demo) backend.setMatrixAnswering(!answering);
    }

    function itemStateText() {
      if (!current || current.listenOnly) return "";
      const state = states[current.id];
      return current.item + " = " + state.value + (state.pending ? " (sent " + state.pending + ")" : "");
    }

    return {
      demo: demo,
      start: start,
      stop: stop,
      subscribe: subscribe,
      rooms: () => rooms,
      listed: listed,
      listen: listen,
      modes: modes,
      kinds: kinds,
      mode: mode,
      setMode: setMode,
      toggleMode: toggleMode,
      listenTo: listenTo,
      room: (id) => roomsById[id] || null,
      current: () => current,
      state: (roomId) => states[roomId],
      device: (id) => devices[id] || null,
      isAnswering: () => answering,
      volumeMode: () => volumeMode,
      inputsOf: () => inputsOf,
      allOffArmed: () => Boolean(allOffArmed.at),
      view: view,
      connected: connected,
      selectRoom: selectRoom,
      stepRoom: stepRoom,
      choose: choose,
      chooseSource: chooseSource,
      chooseDigit: chooseDigit,
      press: press,
      canPress: canPress,
      pressKey: pressKey,
      allOff: allOff,
      inputOptions: inputOptions,
      chooseInput: chooseInput,
      operate: operate,
      slide: slide,
      setDragging: setDragging,
      sliderShown: sliderShown,
      setVolumeMode: setVolumeMode,
      toggleVolumeMode: () => setVolumeMode(volumeMode === "slider" ? "buttons" : "slider"),
      access: backend.access || null,
      controlAllowed: () => !controlBlock(),
      controlBlock,
      roomLock,
      roomLocked,
      toggleRoomLock,
      lockOn,
      lockMenu,
      lockNotice,
      audioView: audioView,
      controls: controls,
      activeTarget: activeTarget,
      remoteHidden: remoteHidden,
      selectedBubble: selectedBubble,
      target: () => activeTarget(controls()),
      pick: pick,
      remoteOf: remoteOf,
      spec: spec,
      drives: drives,
      bubbles: bubbles,
      bubbleState: bubbleState,
      bubbleLabel: bubbleLabel,
      bubbleLock: bubbleLock,
      tapBubble: tapBubble,
      info: info,
      diagnostics: diagnostics,
      openDiagnostics: openDiagnostics,
      closeDiagnostics: closeDiagnostics,
      simulateExternal: simulateExternal,
      toggleMatrix: toggleMatrix,
      itemStateText: itemStateText
    };
  }

  global.RemoteSession = {
    KIND_NAME: KIND_NAME,
    KEYS: KEYS,
    SHORTCUTS: SHORTCUTS,
    LISTEN_SHORTCUTS: LISTEN_SHORTCUTS,
    DEVICE_ROLE: DEVICE_ROLE,
    REPEAT_DELAY: REPEAT_DELAY,
    REPEAT_EVERY: REPEAT_EVERY,
    SLIDE_EVERY: SLIDE_EVERY,
    SLIDE_HOLD: SLIDE_HOLD,
    OFF_AGAIN_MS: OFF_AGAIN_MS,
    LOCK_WAIT_MS: LOCK_WAIT_MS,
    NOT_CONNECTED: NOT_CONNECTED,
    keyHints: keyHints,
    memoryStore: memoryStore,
    create: create
  };
})(globalThis);
