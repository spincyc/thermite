/*
 * The bridge's heart (handheld/PROTOCOL.md): one RemoteSession (with optional
 * RemoteListen over RemoteAudio and RemoteMusic) per enrolled remote, all on
 * one shared RemoteBackend (and optional shared MusicBackend), each behind its
 * own BridgeEnd and per-remote handheld/<name> access label. It opens the
 * frames the base relays, applies each operation once, answers every one with
 * ack, retains each awake remote's room lock policy on the shared access client,
 * and sends each awake remote its screen and audio when they change, at most
 * once per event-loop turn. It does no I/O itself: the caller hands it frames
 * (receive), what the base said of each frame it sent (send's done callback)
 * and a send(mac, frame, done) to reach the base.
 *
 *   Bridge.create({shared: {Session, house, layouts, Audio?, Music?, Listen?},
 *                  backend, musicBackend?, access?, send, log, timers, onStatus, onActivity})
 *   bridge.add({name, mac, key, store, status})  a remote (key: 32-byte Buffer)
 *   bridge.remove(mac)
 *   bridge.receive(mac, frame, rssi?)            a frame the base received
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

/* The gateway's command trail names each remote handheld/<name>. */
function remoteClientLabel(name) {
  const clean = String(name || "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 63);
  return "handheld/" + (clean || "remote");
}

function hasSpeakers(house) {
  return Boolean(house && ((house.speakerRooms || []).length > 0
    || (house.rooms || []).some((room) => (room.speakers || []).length > 0)));
}

/* Lightweight per-remote backend facade that keeps shared SSE streams/state on
 * baseBackend while routing each remote's commands through its own access label. */
function scopedBackend(baseBackend, remoteAccess) {
  if (!baseBackend || !remoteAccess || !baseBackend.access || baseBackend.access.native
      || baseBackend.access.mock || baseBackend.access === remoteAccess) {
    return baseBackend;
  }
  const view = Object.create(baseBackend);
  view.access = remoteAccess;
  view.fetch = remoteAccess.fetch;
  [
    "getRooms", "getState", "getDevice", "whenSynced", "subscribe", "snapshot",
    "deviceSnapshot", "audioOf", "problemOf", "playingOf", "aimed",
    "getDiagnostics", "cancelDiagnostics"
  ].forEach((method) => {
    if (typeof baseBackend[method] === "function") view[method] = (...args) => baseBackend[method](...args);
  });
  view.stop = () => {};
  return view;
}

/* Lightweight per-remote MusicBackend facade: ref-counts shared SSE open/close
 * so forgetting one remote never closes another remote's live music stream. */
function scopedMusicBackend(baseMusic, remoteAccess, ref) {
  if (!baseMusic) return null;
  const view = Object.create(baseMusic);
  if (remoteAccess && !remoteAccess.native && !remoteAccess.mock) view.access = remoteAccess;
  let opened = false;
  view.open = () => {
    if (opened) return;
    opened = true;
    ref.open();
  };
  view.close = () => {
    if (!opened) return;
    opened = false;
    ref.close();
  };
  view.stop = view.close;
  view.setActive = () => {};
  if (typeof baseMusic.subscribe === "function") view.subscribe = (fn) => baseMusic.subscribe(fn);
  return view;
}

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

function create(options) {
  const shared = options.shared;
  const backend = options.backend;
  const musicBackend = options.musicBackend || null;
  const access = options.access || (backend && backend.access) || null;
  const send = options.send;
  const log = options.log || (() => {});
  const timers = options.timers || {};
  const now = timers.now || (() => Date.now());
  const later = timers.setTimeout ? { setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout } : {};
  const clock = timers.now ? { now: timers.now } : {};
  const schedule = options.schedule || ((callback) => setImmediate(callback));
  const onStatus = options.onStatus || (() => {});
  const onActivity = options.onActivity || (() => {});
  const onError = options.onError || ((error) => log("bridge: " + ((error && error.stack) || error)));
  const remotes = new Map();
  const dirty = new Set();
  let scheduled = false;
  let dropped = 0;
  let locksActive = false;
  let musicOpens = 0;

  const musicRef = musicBackend ? {
    open() {
      if (musicOpens === 0 && typeof musicBackend.open === "function") musicBackend.open();
      musicOpens += 1;
    },
    close() {
      if (musicOpens <= 0) return;
      musicOpens -= 1;
      if (musicOpens === 0 && typeof musicBackend.close === "function") musicBackend.close();
    }
  } : null;

  function syncWatch(remote) {
    if (!access || access.native || access.mock || typeof access.retainRoom !== "function") return;
    const room = !remote.removed && remote.ready && remote.awake ? remote.session.current() : null;
    const want = room && !room.listenOnly ? room.id : null;
    if (want === remote.watchedRoom) return;
    if (remote.releaseRoom) {
      remote.releaseRoom();
      remote.releaseRoom = null;
    }
    remote.watchedRoom = want;
    if (want) remote.releaseRoom = access.retainRoom(want);
  }

  function syncLocksWatch() {
    if (!access || access.native || access.mock) return;
    const anyAwake = Array.from(remotes.values()).some((remote) => !remote.removed && remote.ready && remote.awake);
    if (anyAwake === locksActive) return;
    locksActive = anyAwake;
    if (typeof access.watchLocks === "function") access.watchLocks(anyAwake);
    if (!anyAwake && typeof access.watchRoom === "function") access.watchRoom(null);
  }

  function add(entry) {
    if (remotes.has(entry.mac)) remove(entry.mac);
    const label = remoteClientLabel(entry.name);
    const remoteAccess = access && !access.native && !access.mock && typeof access.forClient === "function"
      ? access.forClient(label)
      : access;
    const remoteBackend = scopedBackend(backend, remoteAccess);
    const canListen = hasSpeakers(shared.house) && shared.Audio && shared.Listen;
    const remoteAudio = canListen
      ? shared.Audio.create(Object.assign({
        backend: remoteBackend, house: shared.house, access: remoteAccess, store: entry.store, watchLocks: false
      }, later, clock))
      : null;
    const remoteMusic = canListen && musicBackend && shared.Music
      ? shared.Music.create(Object.assign({
        backend: scopedMusicBackend(musicBackend, remoteAccess, musicRef),
        access: remoteAccess, house: shared.house, store: entry.store, watchLocks: false
      }, later, clock))
      : null;
    const remoteListen = canListen
      ? shared.Listen.create(Object.assign({
        audio: remoteAudio, music: remoteMusic, house: shared.house, store: entry.store
      }, later))
      : null;
    const session = shared.Session.create(Object.assign({
      backend: remoteBackend, house: shared.house, layouts: shared.layouts, store: entry.store, listen: remoteListen
    }, timers));
    const remote = {
      name: entry.name, mac: entry.mac, clientLabel: label,
      session, audioSession: remoteAudio, musicSession: remoteMusic, listen: remoteListen,
      unsubscribe: null, unsubscribeListen: null,
      watchedRoom: null, releaseRoom: null,
      end: new Link.BridgeEnd({ key: entry.key, random: options.random }),
      ready: false, awake: false, applied: new Applied(), status: Object.assign({}, entry.status),
      lastSeen: null, rssi: null,
      // What the remote shows: the screen id it has (as it said, or as sent), and the audio sent.
      has: null, sentAudio: null,
      // The screen and audio as last computed: the screen's fields' JSON, its id, the whole screen.
      screenJSON: null, screenId: null, screen: null, hidden: true, audioJSON: null, audio: null,
      stale: true, ack: null, toast: null, inputs: null, sleeping: false, removed: false
    };
    remotes.set(entry.mac, remote);
    remote.unsubscribe = session.subscribe((event) => heard(remote, event));
    if (remoteListen) remote.unsubscribeListen = remoteListen.subscribe((event) => heardListen(remote, event));
    const prepare = backend.access && !backend.access.native && backend.access.ready
      ? Promise.resolve().then(() => backend.access.ready())
      : Promise.resolve();
    prepare.then(() => {
      if (remote.removed) return;
      return session.start();
    }).then(() => {
      if (remote.removed || !remoteListen) return;
      return Promise.all([
        remoteAudio.start(remoteListen.selected()),
        remoteMusic ? remoteMusic.start(remoteListen.selected()) : null
      ]);
    }).then(() => {
      if (remote.removed) return;
      remote.ready = true;
      syncWatch(remote);
      syncLocksWatch();
      const room = session.current();
      log(remote.name + ": " + (room ? "in " + room.label : "no routed rooms"));
      mark(remote);
    }, (error) => onError(error));
    return remote;
  }

  function remove(mac) {
    const remote = remotes.get(mac);
    if (!remote) return false;
    remote.removed = true;
    if (remote.releaseRoom) {
      remote.releaseRoom();
      remote.releaseRoom = null;
    }
    remote.watchedRoom = null;
    // The bridge leaves the session, and the session the shared backend
    // (stop() sends a volume it still held, which nothing here need hear).
    if (remote.unsubscribe) remote.unsubscribe();
    if (remote.unsubscribeListen) remote.unsubscribeListen();
    if (remote.listen) remote.listen.stop();
    if (remote.audioSession) remote.audioSession.stop();
    if (remote.musicSession) remote.musicSession.stop();
    remote.session.stop();
    remotes.delete(mac);
    dirty.delete(remote);
    syncLocksWatch();
    return true;
  }

  function heard(remote, event) {
    if (event.type === "change" || event.type === "all-off" || event.type === "access"
        || event.type === "audio" || event.type === "device") {
      if (event.type === "change") syncWatch(remote);
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

  function heardListen(remote, event) {
    if (event.type === "change") {
      remote.stale = true;
      mark(remote);
    } else if (event.type === "toast") {
      remote.toast = { title: String(event.title || ""), body: String(event.body || ""), tone: event.tone || "" };
      mark(remote);
    }
  }

  function mark(remote) {
    dirty.add(remote);
    if (scheduled) return;
    scheduled = true;
    schedule(flush);
  }

  function recordRx(remote, rssi) {
    remote.lastSeen = now();
    if (Number.isInteger(rssi) && rssi >= -127 && rssi <= 20) remote.rssi = rssi;
    onActivity(remote);
  }

  // One frame from the base. A frame from a MAC not enrolled is dropped.
  function receive(mac, frame, rssi) {
    const remote = remotes.get(mac);
    if (!remote) {
      dropped += 1;
      return false;
    }
    const result = remote.end.receive(frame);
    if (!result) return false;
    if (result.hello) {
      // A pending HELLO keeps the active session alive; a failed pending SYNC must not disturb it.
      const done = result.pending ? () => {} : (ok) => delivered(remote, ok);
      result.frames.forEach((sync) => send(remote.mac, sync, done));
      if (!result.pending) {
        // Cold start: the new session's DATA starts over; what the remote shows does not.
        recordRx(remote, rssi);
        status(remote, result.hello);
        remote.awake = true;
        remote.sleeping = false;
        remote.ack = null;
        syncWatch(remote);
        syncLocksWatch();
        mark(remote);
      }
      return true;
    }
    if (result.promoted) {
      recordRx(remote, rssi);
      status(remote, result.promoted);
      remote.awake = true;
      remote.sleeping = false;
      remote.ack = null;
      syncWatch(remote);
      syncLocksWatch();
      mark(remote);
    }
    if (result.message) {
      if (!result.promoted) recordRx(remote, rssi);
      message(remote, result.message);
    }
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
    syncWatch(remote);
    syncLocksWatch();
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
    const current = session.current();
    const inListen = typeof session.mode === "function" && session.mode() === "listen";
    if (current) {
      const key = fields.key;
      if (Array.isArray(key) && key.length && key.length <= KEYS_MAX
          && key.every((id) => typeof id === "string" && Object.prototype.hasOwnProperty.call(shared.layouts.buttons, id))) {
        session.pressKey(key);
      }
      if (typeof fields.bubble === "string") {
        if (inListen && session.listen) {
          const speaker = session.listen.speakers(current.id).find((candidate) => candidate.id === fields.bubble);
          if (speaker) session.listen.choose(speaker.id);
        } else {
          const item = session.bubbles(session.controls()).find((candidate) => candidate.id === fields.bubble);
          if (item) session.tapBubble(item);
        }
      }
    }
    if (typeof fields.room === "string" && session.room(fields.room)) session.selectRoom(fields.room);
    if (typeof fields.mode === "string") {
      if (fields.mode === "toggle" && typeof session.toggleMode === "function") session.toggleMode();
      else if ((fields.mode === "watch" || fields.mode === "listen") && typeof session.setMode === "function") {
        session.setMode(fields.mode);
      }
    }
    if (typeof fields.volume === "number" && Number.isFinite(fields.volume)) {
      const nowListen = typeof session.mode === "function" && session.mode() === "listen";
      if (nowListen && session.listen) {
        session.listen.setVolume(fields.volume);
      } else if (session.current()) {
        session.slide(fields.volume, true);
      }
    }
    if (fields.allOff === true) session.allOff();
    if (current && (!session.mode || session.mode() !== "listen") && fields.input !== undefined && session.inputsOf()
        && session.inputOptions().some((option) => option.value === fields.input)) {
      session.chooseInput(fields.input);
    }
    syncWatch(remote);
  }

  function delivered(remote, ok) {
    if (ok || remote.removed) return;
    // Not delivered: asleep or away until it speaks again, showing what it showed.
    if (remote.awake) log(remote.name + ": not answering; waiting for it to speak");
    remote.awake = false;
    remote.has = null;
    remote.sentAudio = null;
    syncWatch(remote);
    syncLocksWatch();
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
      syncWatch(remote);
      syncLocksWatch();
      if (out.ack !== undefined) transmit(remote, out);
      return;
    }
    compute(remote);
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

module.exports = { create, Applied, REMEMBERED, remoteClientLabel, hasSpeakers };
