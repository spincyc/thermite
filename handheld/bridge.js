/*
 * The bridge's heart (handheld/PROTOCOL.md): one RemoteSession per enrolled
 * remote, all on one shared RemoteBackend, each behind its own BridgeEnd.
 * It opens the frames the base relays, applies each operation once, answers
 * every one with ack, and sends each awake remote its screen and audio when
 * they change, at most once per event-loop turn. It does no I/O itself: the
 * caller hands it frames (receive), what the base said of each frame it sent
 * (send's done callback) and a send(mac, frame, done) to reach the base.
 *
 *   Bridge.create({shared: {Session, house, layouts}, backend, send, log, timers})
 *   bridge.add({name, mac, key, store, status})  a remote (key: 32-byte Buffer)
 *   bridge.remove(mac)
 *   bridge.receive(mac, frame)                   a frame the base received
 *   bridge.remote(mac)                           the remote's state, for tests and logs
 */
"use strict";

const Link = require("./link");
const Screen = require("./screen");

// How many operation numbers each remote remembers as applied.
const REMEMBERED = 32;
const STATUS = {
  have: (value) => typeof value === "string" && value.length <= 32,
  battery: (value) => value === null || (Number.isInteger(value) && value >= 0 && value <= 100),
  charging: (value) => typeof value === "boolean",
  board: (value) => typeof value === "string" && value.length <= 64,
  fw: (value) => typeof value === "string" && value.length <= 32,
  w: (value) => Number.isInteger(value) && value > 0 && value <= 4096,
  h: (value) => Number.isInteger(value) && value > 0 && value <= 4096
};
const KEYS_MAX = 8;

/* The last REMEMBERED operation numbers: constant work to check and to add. */
class Applied {
  constructor() {
    this.ring = new Array(REMEMBERED).fill(null);
    this.at = 0;
    this.set = new Set();
  }

  has(op) { return this.set.has(op); }

  add(op) {
    const old = this.ring[this.at];
    if (old !== null) this.set.delete(old);
    this.ring[this.at] = op;
    this.at = (this.at + 1) % REMEMBERED;
    this.set.add(op);
  }
}

// The backend as one session sees it: its own subscribe, so forgetting a
// remote stops its session hearing the house.
function facade(backend, subscriptions) {
  const bound = new Map();
  return new Proxy(backend, {
    get(target, name) {
      if (name === "subscribe") {
        return (callback) => {
          const stop = target.subscribe(callback);
          subscriptions.push(stop);
          return stop;
        };
      }
      const value = Reflect.get(target, name, target);
      if (typeof value !== "function") return value;
      if (!bound.has(name)) bound.set(name, value.bind(target));
      return bound.get(name);
    }
  });
}

function create(options) {
  const shared = options.shared;
  const backend = options.backend;
  const send = options.send;
  const log = options.log || (() => {});
  const timers = options.timers || {};
  const schedule = options.schedule || ((callback) => setImmediate(callback));
  const onStatus = options.onStatus || (() => {});
  const onError = options.onError || ((error) => log("bridge: " + ((error && error.stack) || error)));
  const remotes = new Map();
  const dirty = new Set();
  let scheduled = false;
  let dropped = 0;

  function add(entry) {
    if (remotes.has(entry.mac)) remove(entry.mac);
    const subscriptions = [];
    const session = shared.Session.create(Object.assign({
      backend: facade(backend, subscriptions), house: shared.house, layouts: shared.layouts, store: entry.store
    }, timers));
    const remote = {
      name: entry.name, mac: entry.mac, session, subscriptions,
      end: new Link.BridgeEnd({ key: entry.key, random: options.random }),
      ready: false, awake: false, applied: new Applied(), status: Object.assign({}, entry.status),
      // What the remote shows: the screen id it has (as it said, or as sent), and the audio sent.
      has: null, sentAudio: null,
      // The screen and audio as last computed: the screen's fields' JSON, its id, the whole screen.
      screenJSON: null, screenId: null, screen: null, hidden: true, audioJSON: null, audio: null,
      stale: true, ack: null, toast: null, inputs: null, sleeping: false, removed: false
    };
    remotes.set(entry.mac, remote);
    subscriptions.push(session.subscribe((event) => heard(remote, event)));
    session.start().then(() => {
      if (remote.removed) return;
      remote.ready = true;
      log(remote.name + ": in " + session.current().label);
      mark(remote);
    }, (error) => onError(error));
    return remote;
  }

  function remove(mac) {
    const remote = remotes.get(mac);
    if (!remote) return false;
    remote.removed = true;
    remote.subscriptions.forEach((stop) => stop());
    remote.session.closeDiagnostics();
    remotes.delete(mac);
    dirty.delete(remote);
    return true;
  }

  function heard(remote, event) {
    if (event.type === "change" || event.type === "all-off" || event.type === "access"
        || event.type === "audio" || event.type === "device") {
      remote.stale = true;
      mark(remote);
    } else if (event.type === "toast") {
      remote.toast = { title: String(event.title || ""), body: String(event.body || ""), tone: event.tone || "" };
      mark(remote);
    } else if (event.type === "inputs") {
      remote.inputs = { title: String(event.title || ""), options: remote.session.inputOptions() };
      mark(remote);
    }
  }

  function mark(remote) {
    dirty.add(remote);
    if (scheduled) return;
    scheduled = true;
    schedule(flush);
  }

  // One frame from the base. A frame from a MAC not enrolled is dropped.
  function receive(mac, frame) {
    const remote = remotes.get(mac);
    if (!remote) {
      dropped += 1;
      return false;
    }
    const result = remote.end.receive(frame);
    if (!result) return false;
    if (result.hello) {
      result.frames.forEach((sync) => send(remote.mac, sync, (ok) => delivered(remote, ok)));
      // The new session's DATA starts over; what the remote shows does not.
      status(remote, result.hello);
      remote.awake = true;
      remote.sleeping = false;
      remote.ack = null;
      mark(remote);
      return true;
    }
    message(remote, result.message);
    return true;
  }

  function status(remote, fields) {
    let changed = false;
    Object.keys(STATUS).forEach((name) => {
      if (!Object.prototype.hasOwnProperty.call(fields, name) || !STATUS[name](fields[name])) return;
      if (remote.status[name] !== fields[name]) changed = true;
      remote.status[name] = fields[name];
    });
    if (typeof fields.have === "string" && STATUS.have(fields.have)) {
      // A remote that shows anything else gets the whole screen and audio.
      if (fields.have !== remote.screenId) {
        remote.has = fields.have;
        remote.sentAudio = null;
      } else {
        remote.has = remote.screenId;
      }
    }
    if (changed) onStatus(remote.mac, remote.status);
  }

  function message(remote, fields) {
    status(remote, fields);
    remote.awake = true;
    remote.sleeping = false;
    const op = fields.op;
    if (!Number.isInteger(op) || op < 0 || op > 0xffffffff) {
      mark(remote);
      return;
    }
    // Until the session can be drawn it cannot act: the remote retries, then says Not connected.
    if (!remote.ready) return;
    if (remote.ack !== null) flushRemote(remote);
    remote.ack = op;
    if (!remote.applied.has(op)) {
      remote.applied.add(op);
      try {
        apply(remote, fields);
      } catch (error) {
        onError(error);
      }
    }
    if (fields.sleep === true) remote.sleeping = true;
    mark(remote);
  }

  // The operations, in PROTOCOL.md's order; a value of the wrong shape does nothing.
  function apply(remote, fields) {
    const session = remote.session;
    const key = fields.key;
    if (Array.isArray(key) && key.length && key.length <= KEYS_MAX
        && key.every((id) => typeof id === "string" && Object.prototype.hasOwnProperty.call(shared.layouts.buttons, id))) {
      session.pressKey(key);
    }
    if (typeof fields.bubble === "string") {
      const item = session.bubbles(session.controls()).find((candidate) => candidate.id === fields.bubble);
      if (item) session.tapBubble(item);
    }
    if (typeof fields.room === "string" && session.room(fields.room)) session.selectRoom(fields.room);
    if (fields.allOff === true) session.allOff();
    if (fields.input !== undefined && session.inputsOf()
        && session.inputOptions().some((option) => option.value === fields.input)) {
      session.chooseInput(fields.input);
    }
  }

  function delivered(remote, ok) {
    if (ok || remote.removed) return;
    // Not delivered: asleep or away until it speaks again, showing what it showed.
    if (remote.awake) log(remote.name + ": not answering; waiting for it to speak");
    remote.awake = false;
    remote.has = null;
    remote.sentAudio = null;
  }

  // Recompute what changed; a screen's JSON once per change.
  function compute(remote) {
    if (!remote.ready || !remote.stale) return;
    remote.stale = false;
    const fields = Screen.screen(remote.session, shared.layouts);
    const json = JSON.stringify(fields);
    if (json !== remote.screenJSON) {
      remote.screenJSON = json;
      remote.screenId = Screen.digest(json);
      remote.screen = Object.assign({ id: remote.screenId }, fields);
      remote.hidden = fields.hidden;
    }
    const audio = Screen.audio(remote.session, remote.hidden);
    const audioJSON = JSON.stringify(audio);
    if (audioJSON !== remote.audioJSON) {
      remote.audioJSON = audioJSON;
      remote.audio = audio;
    }
  }

  function flush() {
    scheduled = false;
    const list = Array.from(dirty);
    dirty.clear();
    list.forEach((remote) => {
      try {
        flushRemote(remote);
      } catch (error) {
        onError(error);
      }
    });
  }

  function flushRemote(remote) {
    if (remote.removed) return;
    compute(remote);
    if (!remote.awake) {
      remote.ack = null;
      remote.toast = null;
      remote.inputs = null;
      return;
    }
    const out = {};
    if (remote.ack !== null) out.ack = remote.ack;
    if (remote.sleeping) {
      // The last word to a remote going to sleep is the ack it waits for.
      remote.ack = null;
      remote.toast = null;
      remote.inputs = null;
      remote.awake = false;
      remote.sleeping = false;
      if (out.ack !== undefined) transmit(remote, out);
      return;
    }
    if (remote.toast) out.toast = remote.toast;
    if (remote.inputs) out.inputs = remote.inputs;
    remote.ack = null;
    remote.toast = null;
    remote.inputs = null;
    let screen = null;
    if (remote.ready && remote.screenId !== remote.has) {
      screen = remote.screen;
      remote.has = remote.screenId;
      remote.sentAudio = null;
    }
    if (remote.ready && remote.audioJSON !== remote.sentAudio) {
      out.audio = remote.audio;
      remote.sentAudio = remote.audioJSON;
    }
    if (screen === null) {
      if (Object.keys(out).length) transmit(remote, out);
      return;
    }
    out.screen = screen;
    if (transmit(remote, out, true)) return;
    // Too long as one message: the rest first, then the screen alone.
    delete out.screen;
    if (Object.keys(out).length) transmit(remote, out);
    if (!transmit(remote, { screen: screen }, true)) {
      log(remote.name + ": the screen is over " + Link.MESSAGE_MAX + " bytes; not sent");
      remote.has = null;
    }
  }

  // Frames of one message to the base; false when it is too long (quiet: for the caller to split).
  function transmit(remote, object, quiet) {
    let frames;
    try {
      frames = remote.end.send(object);
    } catch (error) {
      if (error instanceof RangeError) {
        if (!quiet) log(remote.name + ": a message over " + Link.MESSAGE_MAX + " bytes was not sent");
        return false;
      }
      throw error;
    }
    frames.forEach((frame) => send(remote.mac, frame, (ok) => delivered(remote, ok)));
    return true;
  }

  return {
    add, remove, receive,
    remote: (mac) => remotes.get(mac) || null,
    remotes: () => Array.from(remotes.values()),
    dropped: () => dropped,
    flush
  };
}

module.exports = { create, Applied, REMEMBERED };
