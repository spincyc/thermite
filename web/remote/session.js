/*
 * The remote's behavior without a page: what each room shows, its bubbles,
 * which device the buttons control, the keys, the second press Power off and
 * All off take, the volume slider, the input list and the room's info.
 * remote.js draws a session in a browser: it only renders what the session
 * says and hands it the user's input, so another front end can do the same.
 * Load it after house.js, remotes.js and backend.js.
 *
 * RemoteSession.create({backend, house, layouts, store, volume}) returns a
 * session on a RemoteBackend (house and layouts default to REMOTE_HOUSE and
 * REMOTE_LAYOUTS; store is {get(key), set(key, value)}, in memory if absent;
 * volume is "slider" or "buttons", else what the store holds). start(room)
 * reads every room and device and resolves once the session can be drawn,
 * on that room, the stored one or the first. subscribe(callback) hears:
 *
 *   {type: "change"}                     rooms, states or the buttons changed: draw it all again
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
 * seconds while open. It follows room changes and confirmed source readback.
 * diagnostics(room?) returns its cached structured model, or null while a
 * different confirmed path is being read. closeDiagnostics() stops refresh;
 * callers close it when hiding the view or ending their session.
 */
(function (global) {
  "use strict";

  const KIND_NAME = {
    tv: "TV", projector: "Projector", avr: "Receiver", soundbar: "Soundbar", amp: "Amplifier",
    streaming_amp: "Streaming amplifier", streamer: "Streamer", disc_player: "Disc player",
    game_console: "Game console",
    curtain: "Curtain", lighting: "Lighting", hdmi_matrix: "HDMI matrix", hdmi_switch: "HDMI switch",
    usb_matrix: "USB matrix"
  };
  // A key presses the first of its buttons the remote on show has.
  const KEYS = {
    ArrowUp: ["up"], ArrowDown: ["down"], ArrowLeft: ["left"], ArrowRight: ["right"],
    Backspace: ["back"], Escape: ["back"],
    "+": ["volume_up"], "=": ["volume_up"], "-": ["volume_down"], "_": ["volume_down"],
    m: ["mute"], M: ["mute"], p: ["play_pause", "play"], P: ["play_pause", "play"]
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
    [["[", "]"], "Room"]
  ];
  const DEVICE_ROLE = { display: true, audio: true };
  // A held key presses again after REPEAT_DELAY, then every REPEAT_EVERY.
  const REPEAT_DELAY = 420;
  const REPEAT_EVERY = 150;
  // How often a slider being dragged sends, and how long it shows what it
  // sent before what the device reports (a receiver reports every few seconds).
  const SLIDE_EVERY = 250;
  const SLIDE_HOLD = 6000;
  // Power off and All off take a second press this soon after the first.
  const OFF_AGAIN_MS = 3000;

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
    const now = config.now || (() => Date.now());
    const later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
    const cancel = config.clearTimeout || ((handle) => global.clearTimeout(handle));
    // Only the mock can simulate the matrix; the views show its Item names and demo controls.
    const demo = Boolean(backend.simulateExternalSwitch);

    const listeners = [];
    const rooms = [];
    const roomsById = {};
    const states = {};
    // What each device openHAB drives last reported (backend getDevice).
    const devices = {};
    // A remote chosen over the buttons, per room; it holds while the room shows
    // what it showed then, and the buttons follow the next source after that.
    const picked = {};
    let current = null;
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

    function subscribe(callback) {
      listeners.push(callback);
      return () => {
        const at = listeners.indexOf(callback);
        if (at >= 0) listeners.splice(at, 1);
      };
    }

    function start(roomId) {
      backend.subscribe(onEvent);
      return backend.getRooms()
        .then((list) => {
          list.forEach((room) => {
            rooms.push(room);
            roomsById[room.id] = room;
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
          setRoom(roomId || store.get("remote.room"));
        });
    }

    // ---- state -------------------------------------------------------------

    function onEvent(event) {
      if (event.type === "activity") {
        emit(event);
        return;
      }
      if (event.type === "connection") {
        toast(event.connected ? "Connected to thermite again" : "Lost the connection to thermite",
          event.connected ? "Every room is read again" : "Trying again; rooms may be out of date", event.connected ? "" : "bad");
        return;
      }
      if (event.type === "matrix") {
        answering = event.answering;
        emit({ type: "matrix", answering: answering });
        toast(answering ? "HDMI matrix answering again" : "HDMI matrix stopped answering",
          "The next poll reads every room back", answering ? "" : "bad");
        return;
      }
      if (event.type === "problem") {
        emit({ type: "change" });
        return;
      }
      if (event.type === "device") {
        devices[event.device] = event.state;
        // The slider stops showing what it sent once the device reports it, and
        // sends that value again once the device has moved off it (its own knob).
        if (fader.device === event.device && event.state.level === fader.value && !fader.dragging) fader.at = 0;
        if (fader.sentTo === event.device && event.state.level !== fader.sent) fader.sent = null;
        emit({ type: "device", device: event.device });
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
      if (current && event.room === current.id && differs(before, event.state)) {
        announce(current.label + ": " + view(current, event.state).spoken);
      }
    }

    function differs(before, after) {
      return !before || before.value !== after.value || before.pending !== after.pending || before.status !== after.status;
    }

    function option(room, id) {
      return room.options.find((candidate) => candidate.id === id) || null;
    }

    // What a room shows, for the room list, announcements and the alert line:
    // the alert names the source's problem, then each of the room's devices
    // openHAB cannot drive (backend problemOf), after its label.
    function view(room, state) {
      const shown = sourceView(room, state);
      const problems = [shown.problem].concat(room.devices.map((device) => {
        const problem = backend.problemOf(device.id);
        return problem ? device.label + " " + problem : null;
      })).filter(Boolean);
      if (problems.length) shown.problem = problems.join(". ");
      return shown;
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
      const room = roomsById[id] || rooms[0];
      if (!room) return null;
      current = room;
      store.set("remote.room", room.id);
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
      if (!current) return;
      const index = rooms.indexOf(current);
      selectRoom(rooms[(index + delta + rooms.length) % rooms.length].id, true);
    }

    function choose(sourceId) {
      if (!current) return Promise.resolve(null);
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
      const choice = current && current.options[Number(digit)];
      if (!choice) return null;
      chooseSource(choice.id);
      return choice.id;
    }

    function press(button) {
      if (!current) return Promise.resolve(null);
      const target = activeTarget(controls());
      const control = target && !Remote.AUDIO[button] ? Remote.controlOf(house, target.id) : null;
      if (button === "input" && control && control.input) {
        openInputs(target);
        return Promise.resolve(null);
      }
      if (button === "power" && control && !offConfirmed(target, control)) return Promise.resolve(null);
      return backend.press(current.id, target ? target.id : null, button)
        .then((result) => {
          if (button === "mute") holdMute();
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
      const target = activeTarget(controls());
      const offered = target ? Remote.buttonsOf(target.remote) : [];
      const button = candidates.find((id) => Remote.AUDIO[id] || offered.indexOf(id) >= 0) || candidates[0];
      press(button);
      return button;
    }

    // Mute lights for a press at once, as the slider holds a volume just set,
    // and shows what the device reports once it does or the hold ends.
    function holdMute() {
      cancel(muteRelease);
      muteRelease = later(() => emit({ type: "audio" }), Remote.AIM_MS.mute + 50);
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
      fader.release = later(() => emit({ type: "audio" }), SLIDE_HOLD + 50);
      emit({ type: "audio" });
    }

    function sliderShown() {
      return volumeMode === "slider" && Boolean(current && current.audio);
    }

    // The slider moved to value; final when it was let go.
    function slide(value, final) {
      const audio = current && current.audio;
      if (!audio) return;
      if (final) fader.dragging = false;
      held(audio.id, Number(value));
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
      const recent = Boolean(audio) && fader.device === audio.id && (fader.dragging || now() - fader.at < SLIDE_HOLD);
      const value = !volume ? null : recent ? fader.value : state ? state.level : null;
      const isMuted = Boolean(control && control.mute && state) && muted(audio, state);
      let caption = audio ? audio.short : "No audio";
      if (audio && !volume) caption += " · not connected";
      else if (volume && value === null) caption += state && state.volume === "UNDEF" ? " · not answering" : " · reading…";
      else if (volume) caption += " · " + Remote.volumeText(volume, value);
      if (volume && isMuted) caption += " · muted";
      return {
        audio: audio,
        short: audio ? audio.short : "No audio",
        slider: volumeMode === "slider",
        // Mute lit, unlit, or null while there is no mute to show.
        muted: control && control.mute && state ? isMuted : null,
        value: value,
        max: volume ? volume.max : 100,
        disabled: value === null,
        fill: volume ? Math.min(100, ((value === null ? 0 : value) / volume.max) * 100) : 0,
        dragging: fader.dragging,
        caption: caption,
        label: "Volume" + (audio ? ", " + audio.short : ""),
        text: volume && value !== null ? Remote.volumeText(volume, value) : caption
      };
    }

    // ---- the buttons -------------------------------------------------------

    function controls() {
      return Remote.controlsFor(house, current.id, Remote.showing(states[current.id]));
    }

    function activeTarget(list) {
      const choice = picked[current.id];
      const kept = choice && choice.value === Remote.showing(states[current.id])
        && list.targets.find((target) => target.id === choice.device);
      return kept || list.targets.find((target) => target.id === list.preferred) || null;
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
      if (item.choose) {
        if (shown === "switching") return item.label + ", switching";
        if (item.id === Remote.OFF) return shown === "current" ? "Off, the room is off" : "Off: turn the room off";
        return item.label + (shown === "current" ? ", on the screen" : ", source");
      }
      return item.label + ", " + (KIND_NAME[item.kind] || item.kind);
    }

    // Off and the room's sources switch the room; any other bubble only brings
    // up its device's buttons.
    function tapBubble(item) {
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
      const others = list.targets.filter((candidate) => !target || candidate.id !== target.id);
      return {
        rows: [
          ["Buttons", target ? target.label + " · " + remote.label : current.label + " shows nothing: choose a source"],
          ["Volume, mute", audio ? audio.label : current.label + " has no audio device in the inventory"],
          ["Other remotes here", others.length ? others.map((candidate) => candidate.label).join(", ") : "None"]
        ],
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
      if (!signalOpen || !id || !states[id]) return Promise.resolve(null);
      const state = states[id];
      const acquiredAfter = signalSince[id] || 0;
      const generation = ++signalGeneration;
      const reading = backend.getDiagnostics ? backend.getDiagnostics(id, state.value, state.pending, acquiredAfter) : Promise.resolve(null);
      return Promise.resolve(reading).catch(() => null).then((model) => {
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
      if (!roomsById[id]) return Promise.resolve(null);
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
      if (!current || !demo) return;
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
      if (!current) return "";
      const state = states[current.id];
      return current.item + " = " + state.value + (state.pending ? " (sent " + state.pending + ")" : "");
    }

    return {
      demo: demo,
      start: start,
      subscribe: subscribe,
      rooms: () => rooms,
      room: (id) => roomsById[id] || null,
      current: () => current,
      state: (roomId) => states[roomId],
      device: (id) => devices[id] || null,
      isAnswering: () => answering,
      volumeMode: () => volumeMode,
      inputsOf: () => inputsOf,
      allOffArmed: () => Boolean(allOffArmed.at),
      view: view,
      selectRoom: selectRoom,
      stepRoom: stepRoom,
      choose: choose,
      chooseSource: chooseSource,
      chooseDigit: chooseDigit,
      press: press,
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
      audioView: audioView,
      controls: controls,
      activeTarget: activeTarget,
      target: () => activeTarget(controls()),
      pick: pick,
      remoteOf: remoteOf,
      spec: spec,
      drives: drives,
      bubbles: bubbles,
      bubbleState: bubbleState,
      bubbleLabel: bubbleLabel,
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
    DEVICE_ROLE: DEVICE_ROLE,
    REPEAT_DELAY: REPEAT_DELAY,
    REPEAT_EVERY: REPEAT_EVERY,
    SLIDE_EVERY: SLIDE_EVERY,
    SLIDE_HOLD: SLIDE_HOLD,
    OFF_AGAIN_MS: OFF_AGAIN_MS,
    keyHints: keyHints,
    memoryStore: memoryStore,
    create: create
  };
})(globalThis);
