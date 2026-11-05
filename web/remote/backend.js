/*
 * The seam between the remote's UI and the house. remote.js talks only to
 * the object RemoteBackend.create() returns, which has eleven methods, each
 * returning a Promise except subscribe and problemOf:
 *
 *   getRooms()            [{id, label, item, options: [{id, label, kind, model}],
 *                           display, audio, devices: [{id, label, short, kind, model}],
 *                           order: [id, ...],
 *                           controls: [{id, label, device, actions: [{id, label}], connected}]}]
 *                         options start with Off (id "off"). order is every option but
 *                         Off, as the remote lists them (house.js order, else the options).
 *   operate(room, control, action)
 *                         resolves with {room, control, action, label, device, sent, note}
 *   getState(room)        {room, item, value, pending, status, reason}
 *                         value   what the house last read back: "off", a source id,
 *                                 "UNDEF" (the room's matrix or devices did not say) or
 *                                 "NULL" (not read yet)
 *                         pending the source chosen and not yet confirmed, or null
 *                         status  "current", "switching", "unavailable" or "unknown"
 *   choose(room, source)  sends the choice; resolves with the state once it is read back
 *   press(room, device, button)
 *                         resolves with {room, button, label, device, note, sent, tag}: what the
 *                         button drives. Volume and Mute drive the room's audio device
 *                         whatever device is named; any other button needs a device
 *                         controlsFor() offers for what the room is showing(), whose remote
 *                         (remotes.js) has it. Only a device with a control (house.js) is
 *                         driven: Power toggles what was last read, Mute and Volume what
 *                         was last sent while it is not read back yet (Volume steps, never
 *                         past control.volume.max), Input asks for a choice
 *                         (setInput) and every other button goes to control.key
 *   getDevice(device)     {device, power, volume, mute, input, level} for a device with a control:
 *                         each as openHAB last read it ("NULL" until read, "UNDEF" while the
 *                         device does not answer); level is the volume as a number, or null
 *   setVolume(room, value) sets the room's audio device to value, on the device's own scale
 *                         (control.volume min..max, a multiple of its step);
 *                         resolves like press()
 *   setInput(room, device, value)
 *                         chooses one of the device's control.input options; resolves like press()
 *   allOff()              turns every room Off and every device with a control.power off;
 *                         resolves with {label, note, sent, tag} once that is sent, while each
 *                         room shows "switching" to Off until it is read back, as after choose()
 *   problemOf(device)     why openHAB cannot drive the device, as its house.js problem Item
 *                         last said (a phrase to follow its label), or null
 *   subscribe(callback)   callback({type: "state", room, state}), callback({type: "device", device,
 *                         state}) (state as getDevice), callback({type: "problem", device, problem})
 *                         (as problemOf), callback({type: "matrix", answering}) (mock only) and
 *                         callback({type: "connection", connected}) (openHAB only);
 *                         returns an unsubscribe function
 *
 * A choice is never assumed: the room shows "switching" until the matrix is
 * read back, as openHAB's routing.js does.
 *
 * aimed(device, field) is the volume or mute last sent to a device while it
 * is not read back yet (for at most AIM_MS[field], or heldMs()), else null.
 *
 * RemoteBackend.controlsFor(house, room, value) lists what the room's buttons
 * can control while it shows `value`; remotes.js must be loaded first.
 * RemoteBackend.showing(state) is the value the buttons follow: a choice
 * still switching, else what was read back. RemoteBackend.controlOf(house,
 * device) is the device's control, or null; volumeText(spec, value) shows a
 * volume as the device does (52, or -28.0 dB on a receiver set to show dB).
 * A control's volume is the device's own scale: {item, min, max, step,
 * unit?, displayMode?}; nothing is rescaled, and the device's max is the
 * only limit. A control's readBack is the seconds its device takes to report
 * a command, when it reports one only at its next poll (a Marantz receiver);
 * heldMs(control, ms) is how long a value sent to it is held: ms, or that
 * and a second when longer.
 *
 * create() picks OpenHabBackend when openHAB serves the page (conf/html/ is
 * /static/), and MockBackend otherwise (the page opened from disk or another
 * server); settings.backend ("openhab" or "mock") overrides that.
 * OpenHabBackend follows one view of the house over one event stream
 * (settings.view): "remote", the remote page and the terminal (each room's
 * Source as stateupdated; each control's power, volume, mute and input Item,
 * each device's problem Item and each room's route problem as statechanged;
 * and the activity feed's Items), or "home", Home and Topology (the rooms'
 * Sources as statechanged, no activity feed). Through the gateway the stream
 * is GET /api/v1/events/<view>, whose first frame is a snapshot of the view's
 * Items, {"type":"snapshot","states":{item: value}}, sent again after any gap,
 * and {"type":"unavailable"} while openHAB is not answering it. Served by
 * openHAB itself (native), it is /rest/events with wildcard topics, and the
 * snapshot one GET /rest/items?fields=name,state when the stream opens.
 *   getState   the room as the snapshot and the events since say; it waits
 *              for the first snapshot (up to SNAPSHOT_TIMEOUT)
 *   choose     POST /rest/items/loc_<room>_source, text/plain body "off" or a source id;
 *              pending stays set until the Item's next update arrives, or confirmMs() pass
 *   press, setVolume, setInput
 *              POST /rest/items/<Item>, text/plain body: ON or OFF, a volume, an input value, or
 *              the button id for control.key; the device's integration turns it into the
 *              device's own command, and drops a volume off the device's own scale
 *   allOff     POST /rest/items/house_all_off, text/plain body ON; openHAB's all_off.js then
 *              sends every room's Source Off and every controlled power Item OFF
 */
(function (global) {
  "use strict";

  const OFF = "off";
  const UNDEF = "UNDEF";
  const NULL_STATE = "NULL";

  // On every remote, and always the room's audio device.
  const AUDIO = { volume_up: true, volume_down: true, mute: true };

  // How long a choice waits to be read back: routing.js's RECENT_MS (the
  // model's holdMs, 15 s unless a receiver reports later), and a second.
  function confirmMs(house) {
    return (house.holdMs || 15000) + 1000;
  }
  // How long a value sent is stepped or toggled from instead of the one read
  // back: a held volume key repeats faster than a device reports, and a
  // second Mute can come before the first is read back (a second after the
  // command on a WiiM, or at its next poll if that read is missed). A
  // control's readBack lengthens both (heldMs).
  const AIM_MS = { volume: 3000, mute: 12000 };

  function heldMs(control, ms) {
    const readBack = control ? Number(control.readBack) : 0;
    return readBack > 0 ? Math.max(ms, readBack * 1000 + 1000) : ms;
  }
  const FIELDS = ["power", "volume", "mute", "input"];
  // The Item openHAB turns everything off on (lib/oh_features/all_off).
  const ALL_OFF = "house_all_off";
  const TOPIC = /^openhab\/items\/([^/]+)\/(stateupdated|statechanged|command)$/;
  const ACTIVITY_PATH = "/static/remote/activity.json";
  // Each view's topics when openHAB serves the page itself. openHAB tests
  // every filter against every event, so a few wildcards cost it less than
  // an exact list; this page drops the other Items unread. A room's Source
  // arrives as both state verbs there, and the view keeps one.
  const NATIVE_TOPICS = {
    remote: "openhab/items/loc_*_source/stateupdated,openhab/items/*/statechanged,openhab/items/*/command",
    home: "openhab/items/loc_*_source/statechanged"
  };
  const STATES_PATH = "/rest/items?fields=name,state";
  // Delays before reopening an event stream a browser closed for good.
  const REOPEN_MS = [1000, 2000, 5000, 15000, 30000];
  const EVENT_TYPES = { command: "ItemCommandEvent", stateupdated: "ItemStateUpdatedEvent", statechanged: "ItemStateChangedEvent" };
  // Background reads: the diagnostics panel's Items, the two static
  // catalogues and, served by openHAB itself, the snapshot. Over HTTP/1.1 a
  // browser allows six connections per origin across its tabs, and each page
  // holds one for its stream. Commands never enter this queue.
  const BACKGROUND_READS = 2;
  const SNAPSHOT_TIMEOUT = 8000;

  // A choice is never shown as made, but its buttons are offered at once.
  function showing(state) {
    return state.pending || state.value;
  }

  function sourceItem(roomId) {
    return "loc_" + roomId + "_source";
  }

  function findRoom(house, roomId) {
    const room = house.rooms.find((candidate) => candidate.id === roomId);
    if (!room) throw new Error("unknown room: " + roomId);
    return room;
  }

  function device(house, id) {
    if (!id) return null;
    const known = house.devices[id] || house.sources[id];
    if (!known) return { id: id, label: id, short: id, kind: "", model: "", remote: null };
    return {
      id: id, label: known.label, short: known.short || known.label, kind: known.kind, model: known.model || "",
      remote: known.remote || null, availability: known.availability || null
    };
  }

  function layouts() {
    if (!global.REMOTE_LAYOUTS) throw new Error("remotes.js is not loaded");
    return global.REMOTE_LAYOUTS;
  }

  // A commissioned IR profile offers only its individually verified keys.
  // Absence of this descriptor preserves the established native remotes.
  function buttonAvailable(house, deviceId, button) {
    const target = device(house, deviceId);
    if (!target) return false;
    const availability = target.availability;
    return !availability || (Array.isArray(availability.keyButtons) && availability.keyButtons.indexOf(button) >= 0);
  }

  // Every button a remote has, Volume and Mute aside.
  function buttonsOf(remoteId) {
    const remote = layouts().remotes[remoteId];
    if (!remote) throw new Error("unknown remote: " + remoteId);
    return [].concat(
      remote.corners || [],
      remote.corners ? ["up", "down", "left", "right", "ok"] : [],
      remote.top || [],
      remote.playback || [],
      remote.channel ? ["channel_up", "channel_down"] : [],
      remote.keys || []
    ).filter((id, index, all) => id && all.indexOf(id) === index);
  }

  function options(house, room) {
    return [{ id: OFF, label: "Off", kind: "off", model: "" }].concat(room.sources.map((id) => {
      const source = house.sources[id];
      return { id: id, label: source.label, kind: source.kind, model: source.model || "" };
    }));
  }

  function describeRoom(house, room) {
    return {
      id: room.id,
      label: room.label,
      item: sourceItem(room.id),
      options: options(house, room),
      display: device(house, room.display),
      audio: device(house, room.audio),
      devices: room.devices.map((id) => device(house, id)),
      order: room.order || room.sources,
      controls: (room.controls || []).map((control) => ({
        id: control.id,
        label: control.label,
        device: device(house, control.device),
        actions: control.actions.map((action) => ({ id: action[0], label: action[1] })),
        connected: control.connected === true
      }))
    };
  }

  // What a room's buttons can control while it shows `value`, in the order
  // the page lists them: the source on the screen, its display and its audio
  // device. Off offers the first source in the visible source order without
  // routing or powering it; unknown readback still falls back to the display.
  function controlsFor(house, roomId, value) {
    const room = findRoom(house, roomId);
    const sourceId = value === OFF ? (room.order || room.sources)[0] : value;
    const source = house.sources[sourceId] ? sourceId : null;
    const roles = [[source, "source"], [room.display, "display"], [room.audio, "audio"]];
    const targets = [];
    roles.forEach((entry) => {
      if (!entry[0] || targets.some((target) => target.id === entry[0])) return;
      const target = device(house, entry[0]);
      if (!target.remote) return;
      target.role = entry[1];
      targets.push(target);
    });
    const preferred = targets.find((target) => target.role === "source")
      || targets.find((target) => target.role === "display") || targets[0] || null;
    return { room: roomId, targets: targets, preferred: preferred ? preferred.id : null };
  }

  // What a press would drive while the room shows `value`; a button the
  // device's remote does not have is refused.
  function pressFor(house, roomId, deviceId, button, value) {
    const spec = layouts().buttons[button];
    if (!spec) throw new Error("unknown button: " + button);
    const room = findRoom(house, roomId);
    const result = { room: roomId, button: button, label: spec.label, device: null, note: "" };
    if (AUDIO[button]) {
      if (room.audio) result.device = device(house, room.audio);
      else result.note = room.label + " has no audio device in the inventory";
      return result;
    }
    const target = controlsFor(house, roomId, value).targets.find((candidate) => candidate.id === deviceId);
    if (!target) {
      throw new Error(deviceId ? device(house, deviceId).label + " is not on the " + room.label + " screen now"
        : "Nothing is on in the " + room.label + ": choose a source first");
    }
    const remote = layouts().remotes[target.remote];
    if (remote.note) throw new Error(remote.note.replace("{device}", target.label));
    if (buttonsOf(target.remote).indexOf(button) < 0) throw new Error("The " + remote.label + " has no " + spec.label);
    if (!buttonAvailable(house, target.id, button)) throw new Error((target.availability || {}).note || "This key is not configured");
    result.device = target;
    return result;
  }

  // What openHAB drives on a device: house.js devices.<id>.control, as the
  // device's integration renders it (oh_integrations remote()); null for none.
  function controlOf(house, id) {
    const known = id ? house.devices[id] : null;
    return (known && known.control) || null;
  }

  // Item name -> [device id, field] for every Item a control is read from.
  function controlItems(house) {
    const items = Object.create(null);
    Object.keys(house.devices).forEach((id) => {
      const control = controlOf(house, id);
      if (!control) return;
      if (control.power) items[control.power] = [id, "power"];
      if (control.volume) items[control.volume.item] = [id, "volume"];
      if (control.volume && control.volume.displayMode && control.volume.displayMode.item) {
        items[control.volume.displayMode.item] = [id, "volumeScale"];
      }
      if (control.mute) items[control.mute] = [id, "mute"];
      if (control.input) items[control.input.item] = [id, "input"];
      if (control.playback) items[control.playback.item] = [id, "playback"];
    });
    return items;
  }

  // Item name -> device id for every device's problem Item.
  function problemItems(house) {
    const items = Object.create(null);
    Object.keys(house.devices).forEach((id) => {
      if (house.devices[id].problem) items[house.devices[id].problem] = id;
    });
    return items;
  }

  function level(raw) {
    const value = parseFloat(raw);
    return Number.isFinite(value) ? value : null;
  }

  // The device's own step: what one volume key press moves, and the slider's grain.
  function volumeStep(spec) {
    const step = Number(spec && spec.step);
    return Number.isFinite(step) && step > 0 ? step : 1;
  }

  // The nearest value on the device's own scale: within min..max, on a step.
  function held(spec, value) {
    const min = Number.isFinite(spec.min) ? spec.min : 0;
    const max = Number.isFinite(spec.max) ? spec.max : min;
    const step = volumeStep(spec);
    const steps = Math.round((Math.min(max, Math.max(min, value)) - min) / step);
    return Math.min(max, Math.max(min, Number((min + steps * step).toFixed(decimals(step)))));
  }

  // How many decimals a step has: 0.5 has 1, 1 has 0.
  function decimals(step) {
    const text = String(step);
    return text.indexOf(".") < 0 ? 0 : text.length - text.indexOf(".") - 1;
  }

  // A volume as the device shows it: 52 on a WiiM or a receiver's absolute
  // scale, 69.5 between whole steps, -28.0 dB on a receiver set to show dB.
  function volumeText(spec, value, scale) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "";
    const display = spec.displayMode;
    const mode = display && display.modes && Object.prototype.hasOwnProperty.call(display.modes, scale)
      ? display.modes[scale] : null;
    const format = mode || spec;
    if (mode && Number(value) === spec.min && mode.minimum) return mode.minimum;
    const shown = Number((Number(value) + (format.offset || 0)).toFixed(decimals(volumeStep(spec))));
    const text = Number.isInteger(format.precision) ? Math.abs(shown).toFixed(format.precision) : String(Math.abs(shown));
    return (shown < 0 ? "\u2212" : "") + text + (format.unit ? " " + format.unit : "")
      + (display && !mode ? " (native)" : "");
  }

  // What a press sends to a device with a control: {field, item, command,
  // note}, or only a note when it sends nothing. aimed(field) is the volume or
  // mute last sent, while it is newer than what was read back, else null.
  function plan(target, control, button, state, aimed) {
    const missing = (what) => ({ note: "thermite does not control the " + what + " of the " + target.label + " yet" });
    if (button === "volume_up" || button === "volume_down") {
      const spec = control.volume;
      if (!spec) return missing("volume");
      const from = aimed("volume") !== null ? aimed("volume") : level(state.volume);
      if (from === null) throw new Error("The volume of the " + target.label + " is not known yet");
      const to = held(spec, from + (button === "volume_up" ? volumeStep(spec) : -volumeStep(spec)));
      if (to === from) return { note: (to === spec.max ? "At its maximum, " : "At its minimum, ") + volumeText(spec, to, state.volumeScale) };
      return { field: "volume", item: spec.item, command: String(to), volume: to, note: volumeText(spec, to, state.volumeScale) };
    }
    if (button === "mute") {
      if (!control.mute) return missing("mute");
      const mute = (aimed("mute") !== null ? aimed("mute") : state.mute) !== "ON";
      return { field: "mute", item: control.mute, command: mute ? "ON" : "OFF", note: mute ? "Muted" : "Sound on" };
    }
    if (button === "power") {
      if (!control.power && control.key && target.availability && Array.isArray(target.availability.keyButtons)
          && target.availability.keyButtons.indexOf("power") >= 0) {
        return { field: "key", item: control.key, command: "power", note: "IR power key; device state is not reported" };
      }
      if (!control.power) return missing("power");
      if (state.power !== "ON" && state.power !== "OFF") throw new Error("The " + target.label + " has not said whether it is on");
      const on = state.power === "OFF";
      return { field: "power", item: control.power, command: on ? "ON" : "OFF", note: on ? "Turning on" : "Turning off" };
    }
    if (button === "input" && control.input) return { note: "Choose the input from the list" };
    if (!control.key) return { note: "thermite does not control the " + target.label + " yet" };
    return { field: "key", item: control.key, command: button, note: "" };
  }
  // No room control is connected yet: Control4 still drives each of them.
  function operation(house, roomId, controlId, actionId) {
    const room = findRoom(house, roomId);
    const control = (room.controls || []).find((candidate) => candidate.id === controlId);
    if (!control) throw new Error(room.label + " has no control " + controlId);
    const action = control.actions.find((candidate) => candidate[0] === actionId);
    if (!action) throw new Error(control.label + " has no action " + actionId);
    const target = device(house, control.device);
    return {
      room: roomId, control: controlId, action: actionId, label: control.label + ": " + action[1],
      device: target, sent: false, note: target.label + " is not connected to thermite yet"
    };
  }

  // What All off turns off: every room on the matrix and each device openHAB
  // powers, named while they are few enough to read.
  const ALL_OFF_NAMED = 4;
  function allOffResult(house) {
    const names = Object.keys(house.devices).filter((id) => (controlOf(house, id) || {}).power)
      .map((id) => device(house, id).short);
    let listed = names.length < 2 ? names.join("") : names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
    if (listed) listed = names.length > ALL_OFF_NAMED ? " and " + names.length + " devices" : " and the " + listed;
    return { label: "All off", note: "Turning off every room" + listed, sent: false, tag: "" };
  }

  function statusOf(item) {
    if (item.pending) return "switching";
    if (item.value === UNDEF) return "unavailable";
    if (item.value === NULL_STATE) return "unknown";
    return "current";
  }

  function sourceProblem(house, raw) {
    const fallback = "No current source readback; waiting for device diagnostics";
    let issue;
    try { issue = JSON.parse(raw); } catch (_) { return fallback; }
    if (!issue || issue.version !== 1 || !house.devices[issue.device]
        || !["read selected input", "read matrix output input", "read power"].includes(issue.action)
        || !["unavailable", "unsupported_input"].includes(issue.reason)) return fallback;
    const device = house.devices[issue.device];
    const name = device.label + (device.model && !device.label.includes(device.model) ? " (" + device.model + ")" : "");
    const reported = typeof issue.reported === "string" && /^(?:\d+|hdmi\d+|tv)$/.test(issue.reported)
      ? "input " + issue.reported : "an unsupported input";
    return name + ": " + issue.action + " — " + (issue.reason === "unavailable"
      ? "no current report (the device may be offline)" : reported + " is not offered in this room");
  }

  function settled(promise) {
    try {
      return Promise.resolve(promise());
    } catch (error) {
      return Promise.reject(error);
    }
  }

  // What both backends share: state snapshots and the event listeners.
  const Common = {
    snapshot: function (roomId) {
      findRoom(this.house, roomId);
      const item = this.items[roomId];
      const status = statusOf(item);
      return {
        room: roomId,
        item: sourceItem(roomId),
        value: item.value,
        pending: item.pending,
        status: status,
        reason: status === "unavailable" ? sourceProblem(this.house, item.diagnosis) : ""
      };
    },
    getRooms: function () {
      return Promise.resolve(this.house.rooms.map((room) => describeRoom(this.house, room)));
    },
    operate: function (roomId, controlId, actionId) {
      return settled(() => {
        const result = operation(this.house, roomId, controlId, actionId);
        console.info("[remote] " + roomId + " " + controlId + " " + actionId + " -> not connected");
        return result;
      });
    },
    subscribe: function (callback) {
      this.listeners.push(callback);
      return () => {
        this.listeners = this.listeners.filter((listener) => listener !== callback);
      };
    },
    emit: function (event) {
      this.listeners.slice().forEach((listener) => {
        try {
          listener(event);
        } catch (error) {
          console.error(error);
        }
      });
    },
    emitState: function (roomId) {
      this.emit({ type: "state", room: roomId, state: this.snapshot(roomId) });
    },
    // The rooms All off switches: those not showing Off.
    notOff: function () {
      return this.house.rooms.map((room) => room.id)
        .filter((roomId) => showing(this.snapshot(roomId)) !== OFF);
    },
    // A check before a choice is sent: the room must offer the source.
    offered: function (roomId, sourceId) {
      const room = findRoom(this.house, roomId);
      if (sourceId !== OFF && room.sources.indexOf(sourceId) < 0) {
        throw new Error(room.label + " does not offer " + sourceId);
      }
      return room;
    },
    press: function (roomId, deviceId, button) {
      return settled(() => {
        const result = pressFor(this.house, roomId, deviceId, button, showing(this.snapshot(roomId)));
        result.sent = false;
        if (!result.device) return result;
        const control = controlOf(this.house, result.device.id);
        if (!control) return this.unsent(result);
        const id = result.device.id;
        const step = plan(result.device, control, button, this.devices[id], (field) => this.aimed(id, field));
        result.note = step.note;
        if (step.field === "volume") result.volume = step.volume;
        if (!step.item) {
          result.tag = "Not sent";
          return result;
        }
        return this.send(id, step, result);
      });
    },
    setVolume: function (roomId, value) {
      return settled(() => {
        const room = findRoom(this.house, roomId);
        const result = { room: roomId, button: "volume", label: "Volume", device: null, note: "", sent: false };
        if (!room.audio) {
          result.note = room.label + " has no audio device in the inventory";
          return result;
        }
        result.device = device(this.house, room.audio);
        const control = controlOf(this.house, room.audio);
        if (!control || !control.volume) return this.unsent(result);
        const number = Number(value);
        if (value === null || value === "" || !Number.isFinite(number)) throw new Error("Not a volume: " + value);
        const to = held(control.volume, number);
        result.note = volumeText(control.volume, to, this.devices[room.audio].volumeScale);
        result.volume = to;
        return this.send(room.audio, { field: "volume", item: control.volume.item, command: String(to), volume: to }, result);
      });
    },
    setInput: function (roomId, deviceId, value) {
      return settled(() => {
        const room = findRoom(this.house, roomId);
        if (room.devices.indexOf(deviceId) < 0) throw new Error(device(this.house, deviceId).label + " is not in the " + room.label);
        const target = device(this.house, deviceId);
        const control = controlOf(this.house, deviceId);
        if (!control || !control.input) throw new Error("thermite does not choose the input of the " + target.label);
        const option = control.input.options.find((candidate) => candidate[0] === value);
        if (!option) throw new Error("The " + target.label + " has no input " + value);
        const result = { room: roomId, button: "input", label: "Input", device: target, note: option[1], sent: false };
        return this.send(deviceId, { field: "input", item: control.input.item, command: value }, result);
      });
    },
    getDevice: function (deviceId) {
      return settled(() => {
        if (!this.devices[deviceId]) throw new Error("thermite does not control the " + device(this.house, deviceId).label);
        return this.deviceSnapshot(deviceId);
      });
    },
    deviceSnapshot: function (deviceId) {
      const state = this.devices[deviceId];
      const snapshot = {
        device: deviceId, power: state.power, volume: state.volume, mute: state.mute, input: state.input,
        level: level(state.volume)
      };
      const control = controlOf(this.house, deviceId);
      if (control.volume && control.volume.displayMode) snapshot.volumeScale = state.volumeScale || NULL_STATE;
      if (control.playback) snapshot.playback = ["PLAY", "PAUSE"].includes(state.playback) ? state.playback : UNDEF;
      return snapshot;
    },
    // The volume or mute last sent, until the device reports it or AIM_MS pass.
    aimed: function (deviceId, field) {
      const aim = this.aims[deviceId + " " + field];
      return aim && Date.now() - aim.at < heldMs(controlOf(this.house, deviceId), AIM_MS[field]) ? aim.value : null;
    },
    aim: function (deviceId, step) {
      if (!AIM_MS[step.field]) return;
      this.aims[deviceId + " " + step.field] = { value: step.field === "volume" ? step.volume : step.command, at: Date.now() };
    },
    problemOf: function (deviceId) {
      return this.problems[deviceId] || null;
    },
    // NULL, UNDEF or an empty state is no problem.
    updateProblem: function (deviceId, value) {
      const problem = value && value !== NULL_STATE && value !== UNDEF ? value : null;
      if (this.problemOf(deviceId) === problem) return;
      const before = this.problemOf(deviceId);
      this.problems[deviceId] = problem;
      if (this.mock && this.activity && this.house.devices[deviceId].problem) this.activity.accept(
        "openhab/items/" + this.house.devices[deviceId].problem + "/statechanged",
        JSON.stringify({ value: problem || UNDEF, oldValue: before || UNDEF }));
      this.emit({ type: "problem", device: deviceId, problem: problem });
    },
    updateDevice: function (deviceId, field, value) {
      const state = this.devices[deviceId];
      const key = deviceId + " " + field;
      const aim = this.aims[key];
      if (aim && (field === "volume" ? level(value) : value) === aim.value) delete this.aims[key];
      if (!state || state[field] === value) return;
      const before = state[field];
      state[field] = value;
      if (this.mock && this.activity) {
        const control = controlOf(this.house, deviceId);
        const entry = control[field];
        const item = typeof entry === "string" ? entry : entry && entry.item;
        if (item) this.activity.accept("openhab/items/" + item + "/statechanged", JSON.stringify({ value, oldValue: before }));
      }
      this.emit({ type: "device", device: deviceId, state: this.deviceSnapshot(deviceId) });
    }
  };

  // The state of every device with a control, each field "NULL" until read.
  function unread(house) {
    const devices = {};
    Object.keys(house.devices).filter((id) => controlOf(house, id)).forEach((id) => {
      devices[id] = {};
      FIELDS.forEach((field) => { devices[id][field] = NULL_STATE; });
    });
    return devices;
  }

  // ---- mock ----------------------------------------------------------------

  function MockBackend(house, settings) {
    const config = settings || {};
    this.house = house;
    this.mock = true;
    this.latency = config.latency != null ? config.latency : 600;
    this.timeout = config.timeout != null ? config.timeout : 2000;
    this.poll = config.poll != null ? config.poll : 2000;
    this.answering = config.answering !== false;
    this.listeners = [];
    this.matrix = {};
    this.items = {};
    this.timers = {};
    this.waiting = {};
    const initial = config.initial || house.initial || {};
    house.rooms.forEach((room) => {
      const route = initial[room.id] || OFF;
      this.matrix[room.id] = route;
      this.items[room.id] = { value: this.answering ? route : UNDEF, pending: null };
    });
    // Every device with a control starts on, unmuted, 60% of the way up its own scale, on its first input.
    this.aims = {};
    this.problems = Object.assign({}, config.problems);
    this.devices = unread(house);
    Object.keys(this.devices).forEach((id) => {
      const control = controlOf(house, id);
      const state = this.devices[id];
      if (control.power) state.power = "ON";
      if (control.volume) state.volume = String(held(control.volume, control.volume.min + (control.volume.max - control.volume.min) * 0.6));
      if (control.volume && control.volume.displayMode) state.volumeScale = "absolute";
      if (control.mute) state.mute = "OFF";
      if (control.input) state.input = control.input.options[0][0];
    });
    if (global.RemoteActivity) this.activity = global.RemoteActivity.create({
      items: global.RemoteActivity.preview(house).items, layouts: global.REMOTE_LAYOUTS,
      now: config.now, emit: (event) => this.emit(event)
    });
  }

  Object.assign(MockBackend.prototype, Common);

  MockBackend.prototype.getState = function (roomId) {
    return settled(() => this.snapshot(roomId));
  };

  MockBackend.prototype.choose = function (roomId, sourceId) {
    try {
      this.offered(roomId, sourceId);
    } catch (error) {
      return Promise.reject(error);
    }
    clearTimeout(this.timers[roomId]);
    const item = this.items[roomId];
    const before = item.value;
    if (this.activity) this.activity.accept("openhab/items/" + sourceItem(roomId) + "/command", JSON.stringify({ value: sourceId }));
    item.pending = sourceId;
    this.settle(roomId);
    this.emitState(roomId);
    console.info("[remote mock] command " + sourceItem(roomId) + " " + sourceId);
    // A matrix that does not answer is only noticed when the command times out.
    const delay = this.answering ? this.latency : this.timeout;
    return new Promise((resolve) => {
      this.waiting[roomId] = resolve;
      this.timers[roomId] = setTimeout(() => {
        if (this.answering) this.matrix[roomId] = sourceId;
        item.value = this.answering ? this.matrix[roomId] : UNDEF;
        item.pending = null;
        if (this.activity) this.activity.accept("openhab/items/" + sourceItem(roomId) + "/statechanged",
          JSON.stringify({ value: item.value, oldValue: before }));
        this.emitState(roomId);
        this.settle(roomId);
      }, delay);
    });
  };

  MockBackend.prototype.settle = function (roomId) {
    const resolve = this.waiting[roomId];
    delete this.waiting[roomId];
    if (resolve) resolve(this.snapshot(roomId));
  };

  MockBackend.prototype.unsent = function (result) {
    result.tag = "Mock: not sent";
    console.info("[remote mock] " + result.room + " " + result.button + " -> " + result.device.id);
    return result;
  };

  // A device with a control takes the command a moment later; nothing is sent.
  MockBackend.prototype.send = function (deviceId, step, result) {
    result.tag = "Mock";
    console.info("[remote mock] command " + step.item + " " + step.command);
    this.aim(deviceId, step);
    if (this.activity) this.activity.accept("openhab/items/" + step.item + "/command", JSON.stringify({ value: step.command }));
    if (step.field !== "key") {
      setTimeout(() => this.updateDevice(deviceId, step.field, step.command), this.latency / 4);
    }
    return Promise.resolve(result);
  };

  // Every room Off as choose() does, and every powered device off a moment later.
  MockBackend.prototype.allOff = function () {
    console.info("[remote mock] command " + ALL_OFF + " ON");
    if (this.activity) this.activity.accept("openhab/items/" + ALL_OFF + "/command", JSON.stringify({ value: "ON" }));
    this.notOff().forEach((roomId) => this.choose(roomId, OFF));
    Object.keys(this.devices).forEach((id) => {
      if (controlOf(this.house, id).power) setTimeout(() => this.updateDevice(id, "power", "OFF"), this.latency / 4);
    });
    const result = allOffResult(this.house);
    result.tag = "Mock";
    return Promise.resolve(result);
  };

  // Simulation controls; an OpenHabBackend has none of these.
  MockBackend.prototype.isMatrixAnswering = function () {
    return this.answering;
  };

  MockBackend.prototype.setMatrixAnswering = function (answering) {
    this.answering = Boolean(answering);
    this.emit({ type: "matrix", answering: this.answering });
    clearTimeout(this.timers.poll);
    this.timers.poll = setTimeout(() => this.readBack(), this.poll);
  };

  // A device's problem Item changing, as openHAB would state it.
  MockBackend.prototype.simulateProblem = function (deviceId, problem) {
    this.updateProblem(deviceId, problem);
  };

  // A switch made at the matrix itself (Control4, its front panel or web page).
  MockBackend.prototype.simulateExternalSwitch = function (roomId, sourceId) {
    this.offered(roomId, OFF);
    this.matrix[roomId] = sourceId;
    clearTimeout(this.timers.poll);
    this.timers.poll = setTimeout(() => this.readBack(), this.poll);
  };

  MockBackend.prototype.readBack = function () {
    this.house.rooms.forEach((room) => {
      const item = this.items[room.id];
      if (item.pending) return;
      const value = this.answering ? this.matrix[room.id] : UNDEF;
      if (value !== item.value) {
        const before = item.value;
        item.value = value;
        if (this.activity) this.activity.accept("openhab/items/" + sourceItem(room.id) + "/statechanged",
          JSON.stringify({ value, oldValue: before }));
        this.emitState(room.id);
      }
    });
  };

  // ---- openHAB -------------------------------------------------------------

  function OpenHabBackend(house, settings) {
    const config = settings || {};
    this.house = house;
    this.access = config.access || (global.ThermiteAccess && global.ThermiteAccess.client) || null;
    if (typeof document !== "undefined" && !this.access) throw new Error("The access client could not load. Reload thermite.");
    this.base = config.base || "";
    this.fetch = config.fetch || ((url, init) => global.fetch(url, init));
    this.EventSource = config.EventSource || global.EventSource;
    this.timeout = config.timeout != null ? config.timeout : confirmMs(house);
    this.view = config.view === "home" ? "home" : "remote";
    // The state verb a room's Source arrives as in this view.
    this.sourceVerb = this.view === "home" ? "statechanged" : "stateupdated";
    this.listeners = [];
    this.items = {};
    this.byItem = Object.create(null);
    this.routeProblemItems = Object.create(null);
    // The latest value of every Item the view carries, to seed the activity feed.
    this.values = Object.create(null);
    this.readQueue = [];
    this.runningReads = new Set();
    this.snapshotTimeout = config.snapshotTimeout || SNAPSHOT_TIMEOUT;
    this.synced = false;
    this.syncWaiters = [];
    this.touched = null;
    this.timers = {};
    this.waiting = {};
    this.connected = null;
    this.now = config.now || (() => Date.now());
    this.source = null;
    this.streamGeneration = 0;
    this.stopped = false;
    this.active = config.active !== false;
    this.diagnosticsSpec = config.diagnosticsManifest || null;
    this.diagnosticsLoading = null;
    this.diagnosticsModel = null;
    this.diagnosticsTimeout = config.diagnosticsTimeout || 8000;
    this.diagnosticsGeneration = 0;
    this.activityRetryDelay = 1000;
    this.activityNoticed = false;
    const remote = this.view === "remote";
    house.rooms.forEach((room) => {
      this.items[room.id] = { value: NULL_STATE, pending: null };
      this.byItem[sourceItem(room.id)] = room.id;
      if (remote && room.routeProblem) this.routeProblemItems[room.routeProblem] = room.id;
    });
    this.aims = {};
    this.problems = {};
    this.devices = unread(house);
    this.controlItems = remote ? controlItems(house) : Object.create(null);
    this.problemItems = remote ? problemItems(house) : Object.create(null);
    if (this.access && !this.access.native) {
      this.access.subscribe((event) => {
        if (event.type === "unauthorized") {
          this.stop(); this.connected = false; this.devices = unread(house); this.problems = {}; this.aims = {}; this.activity = null;
          Object.values(this.items).forEach((item) => { item.value = NULL_STATE; item.pending = null; });
        }
        this.emit({ type: "access", unauthorized: event.type === "unauthorized", guest: event.guest === true });
      });
    }
    if (remote && global.RemoteActivity) {
      if (config.activityManifest) this.activateActivity(config.activityManifest);
      else this.loadActivity();
    }
    this.listen();
  }

  Object.assign(OpenHabBackend.prototype, Common);

  // Served by openHAB itself: no gateway, so no view stream or command API.
  OpenHabBackend.prototype.native = function () {
    return !this.access || this.access.native === true;
  };

  OpenHabBackend.prototype.request = function (method, path, body, signal, accept) {
    const init = { method: method, headers: { Accept: accept || "text/plain" } };
    if (signal) init.signal = signal;
    if (method === "GET") init.cache = "no-cache";
    if (body !== undefined) {
      init.headers["Content-Type"] = "text/plain";
      init.body = body;
    }
    return this.fetch(this.base + path, init).then((response) => {
      if (!response.ok) throw new Error("thermite answered " + response.status + " to " + method + " " + path);
      return response.text().then((text) => {
        if (this.stopped && this.access && !this.access.native) throw global.ThermiteAccess.problem("session_expired", 401);
        return text;
      });
    });
  };

  // Background GETs share BACKGROUND_READS slots, each with a deadline that
  // includes queueing. A cancelled request holds its slot until fetch
  // settles, so a transport without abort support cannot grow concurrency.
  // A hidden page starts none but the activity catalogue.
  OpenHabBackend.prototype.backgroundRequest = function (path, kind, accept) {
    if (this.stopped || !this.active && kind !== "activity") return Promise.resolve(null);
    const job = { kind, path, accept, controller: global.AbortController ? new global.AbortController() : null, completed: false };
    job.promise = new Promise((resolve, reject) => {
      job.finish = (text, error) => {
        if (job.completed) return;
        job.completed = true;
        clearTimeout(job.timer);
        if (!this.runningReads.has(job)) this.readQueue = this.readQueue.filter((entry) => entry !== job);
        if (error) reject(error); else resolve(text);
      };
    });
    job.cancel = (error) => {
      job.finish(null, error);
      if (job.controller) job.controller.abort();
    };
    const unavailable = kind === "diagnostics" ? "Diagnostics unavailable" : kind === "activity" ? "Activity feed unavailable" : "Snapshot unavailable";
    job.timer = setTimeout(() => job.cancel(new Error(unavailable)), kind === "diagnostics" ? this.diagnosticsTimeout : this.snapshotTimeout);
    this.readQueue.push(job);
    this.drainReads();
    return job.promise;
  };

  OpenHabBackend.prototype.drainReads = function () {
    while (!this.stopped && this.runningReads.size < BACKGROUND_READS && this.readQueue.length) {
      const index = this.active ? 0 : this.readQueue.findIndex((job) => job.kind === "activity");
      if (index < 0) break;
      const job = this.readQueue.splice(index, 1)[0];
      if (job.completed) continue;
      this.runningReads.add(job);
      const finished = (text, error) => {
        job.finish(text, error);
        this.runningReads.delete(job);
        this.drainReads();
      };
      let request;
      try { request = this.request("GET", job.path, undefined, job.controller && job.controller.signal, job.accept); }
      catch (error) { request = Promise.reject(error); }
      Promise.resolve(request).then((text) => finished(text), (error) => finished(null, error));
    }
  };

  OpenHabBackend.prototype.cancelReads = function (kind) {
    for (const job of this.readQueue.concat([...this.runningReads])) {
      if (!kind || job.kind === kind) job.cancel();
    }
  };

  // Diagnostics never widen the event stream or run on a button's path.
  // Exact, integration-described Items only.
  OpenHabBackend.prototype.diagnosticsRequest = function (path) {
    return this.backgroundRequest(path, "diagnostics").then((text) => {
      if (text === null) throw new Error("Diagnostics unavailable");
      return text;
    }).catch(() => { throw new Error("Diagnostics unavailable"); });
  };

  OpenHabBackend.prototype.cancelDiagnostics = function () {
    this.diagnosticsGeneration += 1;
    this.cancelReads("diagnostics");
    this.diagnosticsLoading = null;
  };

  OpenHabBackend.prototype.getDiagnostics = function (roomId, sourceId, pendingId, acquiredAfter) {
    findRoom(this.house, roomId);
    if (!global.RemoteDiagnostics) return Promise.resolve(null);
    if (!this.diagnosticsModel && !this.diagnosticsLoading) {
      const loading = (this.diagnosticsSpec ? Promise.resolve(this.diagnosticsSpec)
        : this.diagnosticsRequest("/static/remote/diagnostics.json").then(JSON.parse))
        .then((manifest) => global.RemoteDiagnostics.create(manifest))
        .then((model) => { if (!this.stopped) this.diagnosticsModel = model; return model; })
        .catch(() => null)
        .finally(() => { if (this.diagnosticsLoading === loading) this.diagnosticsLoading = null; });
      this.diagnosticsLoading = loading;
    }
    const generation = ++this.diagnosticsGeneration;
    const loading = this.diagnosticsModel ? Promise.resolve(this.diagnosticsModel) : this.diagnosticsLoading;
    return loading.then((model) => {
      if (generation !== this.diagnosticsGeneration) return null;
      if (!model || this.stopped) return global.RemoteDiagnostics.unavailable(roomId, findRoom(this.house, roomId).label, this.now());
      this.cancelReads("diagnostics");
      const names = model.items(roomId, sourceId);
      const snapshots = Object.create(null);
      let index = 0;
      const worker = async () => {
        while (index < names.length && !this.stopped && generation === this.diagnosticsGeneration) {
          const item = names[index++];
          try { snapshots[item] = { value: await this.diagnosticsRequest("/rest/items/" + item + "/state"), receivedAt: this.now() }; }
          catch (_) { /* canonical unavailable in projection, never raw errors */ }
        }
      };
      return Promise.all(Array.from({ length: Math.min(BACKGROUND_READS, names.length) }, worker))
        .then(() => generation === this.diagnosticsGeneration && !this.stopped
          ? model.project(roomId, sourceId, snapshots, this.now(), pendingId, acquiredAfter) : null);
    });
  };

  OpenHabBackend.prototype.getState = function (roomId) {
    try {
      findRoom(this.house, roomId);
    } catch (error) {
      return Promise.reject(error);
    }
    return this.whenSynced().then(() => this.snapshot(roomId));
  };

  // Resolves once the view's snapshot is applied, after snapshotTimeout
  // without one, or at once while the page is hidden or stopped.
  OpenHabBackend.prototype.whenSynced = function () {
    if (this.synced || this.stopped || !this.active || !this.EventSource) return Promise.resolve();
    return new Promise((resolve) => {
      const waiter = () => {
        clearTimeout(timer);
        this.syncWaiters = this.syncWaiters.filter((entry) => entry !== waiter);
        resolve();
      };
      // A give-up the gateway never sees: client telemetry counts it.
      const timer = setTimeout(() => {
        if (!this.native()) this.access?.record?.("snapshot_timeout", this.snapshotTimeout / 1000, this.eventsUrl());
        waiter();
      }, this.snapshotTimeout);
      this.syncWaiters.push(waiter);
    });
  };

  OpenHabBackend.prototype.release = function () {
    this.syncWaiters.slice().forEach((waiter) => waiter());
  };

  // Every Item a control is read from, then every problem Item.
  OpenHabBackend.prototype.watched = function () {
    return Object.keys(this.controlItems).concat(Object.keys(this.problemItems), Object.keys(this.routeProblemItems));
  };

  OpenHabBackend.prototype.updateRouteProblem = function (item, value) {
    const room = this.routeProblemItems[item];
    if (!room || this.items[room].diagnosis === value) return;
    this.items[room].diagnosis = value;
    this.emitState(room);
  };

  OpenHabBackend.prototype.updateItem = function (item, value) {
    if (this.routeProblemItems[item]) { this.updateRouteProblem(item, value); return; }
    const entry = this.controlItems[item];
    if (entry) this.updateDevice(entry[0], entry[1], value);
    else if (this.problemItems[item]) this.updateProblem(this.problemItems[item], value);
  };

  // The view's Items as the gateway (or openHAB) holds them now: after a
  // (re)connection, all the page shows. An Item an event has reported since
  // a native read began keeps that newer value.
  OpenHabBackend.prototype.applySnapshot = function (states, touched) {
    const fresh = (item) => Object.prototype.hasOwnProperty.call(states, item) && typeof states[item] === "string"
      && !(touched && touched.has(item));
    Object.keys(states).forEach((item) => {
      if (!fresh(item)) return;
      this.values[item] = states[item];
      // What the feed reports next is a change from this, not from before the gap.
      if (this.activity) this.activity.seed(item, states[item], true);
    });
    this.watched().forEach((item) => { if (fresh(item)) this.updateItem(item, states[item]); });
    Object.keys(this.byItem).forEach((item) => { if (fresh(item)) this.update(this.byItem[item], states[item]); });
    this.synced = true;
    this.reconnected();
    this.release();
  };

  OpenHabBackend.prototype.reconnected = function () {
    if (this.connected === false) {
      this.emit({ type: "connection", connected: true });
      if (this.activity) this.activityNotice("Connected to thermite again", "Events during the gap may be missing");
    }
    this.connected = true;
  };

  OpenHabBackend.prototype.disconnected = function () {
    this.synced = false;
    this.touched = null;
    this.cancelReads("snapshot");
    if (this.connected !== false) {
      this.emit({ type: "connection", connected: false });
      if (this.activity) this.activityNotice("Lost the connection to thermite", "Events during the gap may be missing", "bad");
    }
    this.connected = false;
  };

  OpenHabBackend.prototype.eventsUrl = function () {
    return this.base + (this.native() ? "/rest/events?topics=" + NATIVE_TOPICS[this.view] : "/api/v1/events/" + this.view);
  };

  // openHAB itself has no snapshot frame: the view's states are read once
  // the stream is open, so no event between the read and the stream is lost.
  OpenHabBackend.prototype.readStates = function (generation) {
    const touched = this.touched = new Set();
    this.backgroundRequest(STATES_PATH, "snapshot", "application/json").then((text) => {
      if (text === null || generation !== this.streamGeneration || this.touched !== touched) return;
      const states = Object.create(null);
      const list = JSON.parse(text);
      (Array.isArray(list) ? list : []).forEach((entry) => {
        if (entry && typeof entry.name === "string" && typeof entry.state === "string") states[entry.name] = entry.state;
      });
      this.touched = null;
      this.applySnapshot(states, touched);
    }).catch((error) => {
      if (generation === this.streamGeneration && this.touched === touched) { this.touched = null; console.warn(error); }
    });
  };

  OpenHabBackend.prototype.activityNotice = function (title, body, tone) {
    this.emit({ type: "activity", at: this.now(), kind: "connection", room: null, device: null, title, body, tone: tone || "" });
  };

  OpenHabBackend.prototype.loadActivity = function () {
    if (this.stopped) return Promise.resolve();
    if (this.activityLoading) return this.activityLoading;
    this.activityLoading = this.backgroundRequest(ACTIVITY_PATH, "activity").then((text) => {
      if (this.stopped) return;
      this.activateActivity(JSON.parse(text));
      this.activityRetryDelay = 1000;
    }).catch(() => {
      if (this.stopped) return;
      if (!this.activityNoticed) {
        this.activityNoticed = true;
        this.activityNotice("Activity feed unavailable", "Remote controls still work; retrying", "bad");
      }
      this.timers.activityManifest = setTimeout(() => {
        delete this.timers.activityManifest;
        this.loadActivity();
      }, this.activityRetryDelay);
      // A retry alone need not keep a terminal client alive after its input
      // and event streams have closed. Browser timer handles are numbers.
      if (this.timers.activityManifest && this.timers.activityManifest.unref) this.timers.activityManifest.unref();
      this.activityRetryDelay = Math.min(30000, this.activityRetryDelay * 2);
    }).finally(() => { this.activityLoading = null; });
    return this.activityLoading;
  };

  // The feed reads the events the view's stream already carries.
  OpenHabBackend.prototype.activateActivity = function (manifest) {
    if (!manifest || manifest.version !== 1 || !manifest.items || typeof manifest.items !== "object") throw new Error("Invalid activity manifest");
    const activity = global.RemoteActivity.create({ items: manifest.items, layouts: global.REMOTE_LAYOUTS,
      now: this.now, emit: (event) => this.emit(event) });
    Object.keys(this.values).forEach((item) => activity.seed(item, this.values[item]));
    this.activity = activity;
  };

  // The view's one event stream, open while the page is shown.
  OpenHabBackend.prototype.listen = function () {
    if (this.source) this.source.close();
    this.source = null;
    const generation = ++this.streamGeneration;
    this.synced = false;
    this.touched = null;
    this.cancelReads("snapshot");
    if (!this.EventSource || !this.active || this.stopped) {
      this.release();
      return;
    }
    const source = this.source = new this.EventSource(this.eventsUrl());
    const current = () => generation === this.streamGeneration && !this.stopped;
    source.onmessage = (message) => { if (current()) this.onMessage(message.data); };
    source.onopen = () => {
      if (!current()) return;
      this.reopenAttempt = 0;
      // The gateway's first frame is the snapshot; openHAB is read instead.
      if (this.native()) { this.reconnected(); this.readStates(generation); }
    };
    source.onerror = () => {
      if (!current()) return;
      this.disconnected();
      // A browser closes a stream for good on any non-200 answer, such as a
      // 502 while the gateway restarts; it never retries that by itself.
      if (source.readyState === 2) this.reopenStream(generation);
    };
  };

  OpenHabBackend.prototype.reopenStream = function (generation) {
    if (this.timers.reopen) return;
    const attempt = this.reopenAttempt || 0;
    this.reopenAttempt = attempt + 1;
    this.timers.reopen = setTimeout(() => {
      delete this.timers.reopen;
      if (generation === this.streamGeneration && this.active && !this.stopped) this.listen();
    }, REOPEN_MS[Math.min(attempt, REOPEN_MS.length - 1)]);
    if (this.timers.reopen.unref) this.timers.reopen.unref();
  };

  // A hidden page releases its stream, which holds an HTTP/1.1 origin's few
  // connections and the gateway's per-peer stream allowance, and reopens it
  // (with a fresh snapshot) when shown. Terminal callers stay active.
  OpenHabBackend.prototype.setActive = function (active) {
    const next = Boolean(active);
    if (next === this.active || this.stopped) return;
    this.active = next;
    if (!next) { this.connected = false; this.cancelDiagnostics(); }
    this.listen();
  };

  OpenHabBackend.prototype.stop = function () {
    if (this.access) this.access.stop();
    this.stopped = true;
    this.cancelDiagnostics();
    this.cancelReads();
    this.streamGeneration += 1;
    if (this.source) this.source.close();
    this.source = null;
    this.release();
    Object.values(this.timers).forEach((timer) => clearTimeout(timer));
  };

  OpenHabBackend.prototype.onMessage = function (data) {
    let event;
    let payload;
    try {
      event = JSON.parse(data);
    } catch (error) {
      return;
    }
    if (!event || typeof event !== "object") return;
    if (event.type === "snapshot" && typeof event.topic !== "string") {
      if (!this.native() && event.states && typeof event.states === "object") this.applySnapshot(event.states);
      return;
    }
    if (event.type === "unavailable") { this.disconnected(); return; }
    const match = TOPIC.exec(typeof event.topic === "string" ? event.topic : "");
    if (!match || (event.type !== undefined && event.type !== EVENT_TYPES[match[2]])) return;
    const item = match[1], verb = match[2], roomId = this.byItem[item];
    // A room's Source is followed as one state verb.
    if (roomId && verb !== "command" && verb !== this.sourceVerb) return;
    if (this.activity) this.activity.accept(event.topic, event.payload);
    // Commands and Items the view does not show are ignored before inner parsing.
    if (verb === "command" || !(roomId || this.controlItems[item] || this.problemItems[item] || this.routeProblemItems[item])) return;
    try { payload = JSON.parse(event.payload); } catch (error) { return; }
    if (!payload || payload.value === undefined) return;
    const value = String(payload.value);
    if (this.touched) this.touched.add(item);
    this.values[item] = value;
    if (roomId) this.update(roomId, value);
    else this.updateItem(item, value);
  };

  // A read-back: it confirms a pending choice when it shows that source, or
  // ends it when the matrix does not answer.
  OpenHabBackend.prototype.update = function (roomId, value) {
    const item = this.items[roomId];
    const before = item.value + "\n" + item.pending;
    item.value = value;
    if (item.pending !== null && (value === item.pending || value === UNDEF)) this.settle(roomId);
    if (before !== item.value + "\n" + item.pending) this.emitState(roomId);
  };

  OpenHabBackend.prototype.settle = function (roomId) {
    clearTimeout(this.timers[roomId]);
    delete this.timers[roomId];
    this.items[roomId].pending = null;
    const resolve = this.waiting[roomId];
    delete this.waiting[roomId];
    if (resolve) resolve(this.snapshot(roomId));
  };

  OpenHabBackend.prototype.choose = function (roomId, sourceId) {
    try {
      this.offered(roomId, sourceId);
    } catch (error) {
      return Promise.reject(error);
    }
    const confirmed = this.expectChoice(roomId, sourceId);
    return (this.access && !this.access.native ? this.access.command(roomId, "source", sourceId)
      : this.request("POST", "/rest/items/" + sourceItem(roomId), sourceId)).then(() => {
      this.choiceSent(roomId, sourceId);
      return confirmed;
    }, (error) => {
      this.choiceFailed(roomId, sourceId);
      throw error;
    });
  };

  // The room switching to the source until it is read back; resolves with its state then.
  OpenHabBackend.prototype.expectChoice = function (roomId, sourceId) {
    this.settle(roomId);
    this.items[roomId].pending = sourceId;
    this.emitState(roomId);
    return new Promise((resolve) => { this.waiting[roomId] = resolve; });
  };

  // openHAB took the command: wait for the read-back, or confirmMs(). The
  // stream (and its snapshot after any gap) holds what the room reads.
  OpenHabBackend.prototype.choiceSent = function (roomId, sourceId) {
    const item = this.items[roomId];
    if (item.pending !== sourceId) return;
    if (item.value === sourceId) {
      // The room already shows it: nothing switches, so nothing is read back.
      this.settle(roomId);
      this.emitState(roomId);
      return;
    }
    this.timers[roomId] = setTimeout(() => {
      delete this.timers[roomId];
      if (item.pending !== sourceId) return;
      this.settle(roomId);
      this.emitState(roomId);
    }, this.timeout);
  };

  OpenHabBackend.prototype.choiceFailed = function (roomId, sourceId) {
    if (this.items[roomId].pending !== sourceId) return;
    this.settle(roomId);
    this.emitState(roomId);
  };

  // One command: openHAB's all_off.js sends each room Off and each powered
  // device off. The rooms switch to Off as after choose(); the devices show
  // it as they are read back.
  OpenHabBackend.prototype.allOff = function () {
    if (this.access && !this.access.native) return this.access.allOff().then((answer) => {
      const accepted = Array.isArray(answer.accepted) ? answer.accepted : [];
      const skipped = Array.isArray(answer.skipped) ? answer.skipped : [];
      accepted.filter((room) => this.items[room]).forEach((room) => { this.expectChoice(room, OFF); this.choiceSent(room, OFF); });
      return { label: "All off", note: accepted.length + " rooms accepted" + (skipped.length ? "; " + skipped.length + " skipped" : ""),
        accepted, skipped, sent: accepted.length > 0, tag: "" };
    });
    const rooms = this.notOff();
    rooms.forEach((roomId) => this.expectChoice(roomId, OFF));
    return this.request("POST", "/rest/items/" + ALL_OFF, "ON").then(() => {
      rooms.forEach((roomId) => this.choiceSent(roomId, OFF));
      const result = allOffResult(this.house);
      result.sent = true;
      return result;
    }, (error) => {
      rooms.forEach((roomId) => this.choiceFailed(roomId, OFF));
      throw error;
    });
  };

  OpenHabBackend.prototype.operate = function (roomId, controlId, actionId) {
    if (!this.access || this.access.native) return Common.operate.call(this, roomId, controlId, actionId);
    return settled(() => {
      const result = operation(this.house, roomId, controlId, actionId);
      const control = findRoom(this.house, roomId).controls.find((entry) => entry.id === controlId);
      if (control.connected !== true) return result;
      return this.access.command(roomId, "control." + control.device, actionId).then(() => {
        result.sent = true; result.note = "Accepted"; return result;
      });
    });
  };

  // Control4 drives some room devices and nothing drives others yet: say only what holds for all.
  OpenHabBackend.prototype.unsent = function (result) {
    result.note = "thermite does not control the " + result.device.label + " yet";
    result.tag = "Not sent";
    return result;
  };

  OpenHabBackend.prototype.send = function (deviceId, step, result) {
    this.aim(deviceId, step);
    return (this.access && !this.access.native
      ? this.access.command(result.room, deviceId + "." + step.field, step.field === "volume" ? Number(step.command) : step.command)
      : this.request("POST", "/rest/items/" + step.item, step.command)).then(() => {
      result.sent = true;
      return result;
    }, (error) => {
      delete this.aims[deviceId + " " + step.field];
      throw error;
    });
  };

  function servedByOpenHab(location) {
    return global.ThermiteNavigation ? global.ThermiteNavigation.hosted(location)
      : Boolean(location) && /^https?:$/.test(location.protocol) && location.pathname.indexOf("/static/") === 0;
  }

  global.RemoteBackend = {
    OFF: OFF,
    UNDEF: UNDEF,
    AUDIO: AUDIO,
    AIM_MS: AIM_MS,
    heldMs: heldMs,
    ALL_OFF: ALL_OFF,
    MockBackend: MockBackend,
    OpenHabBackend: OpenHabBackend,
    controlsFor: controlsFor,
    controlOf: controlOf,
    volumeText: volumeText,
    volumeValue: held,
    volumeStep: volumeStep,
    showing: showing,
    buttonsOf: buttonsOf,
    buttonAvailable: buttonAvailable,
    sourceItem: sourceItem,
    servedByOpenHab: servedByOpenHab,
    create: function (house, settings) {
      const config = settings || {};
      const kind = config.backend || (servedByOpenHab(global.location) ? "openhab" : "mock");
      return kind === "openhab" ? new OpenHabBackend(house, config) : new MockBackend(house, config);
    }
  };
})(globalThis);
