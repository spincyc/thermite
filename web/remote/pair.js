/*
 * The Pairing page: every TV and streamer openHAB can pair with, by room,
 * and the steps to pair one (ask for a code, type back what its screen
 * shows). The logic is PairingBackend, usable without a page; the view at
 * the end runs only in a browser.
 *
 * openHAB (OpenHabBackend's conventions, web/remote/backend.js):
 *   list     GET /rest/items?metadata=pairing&fields=name,state,metadata; the
 *            Items with `pairing` metadata are the devices, and the same list
 *            carries the state of each one's status Item and optional result
 *            Item (a fresh command acknowledgement with safe failure detail).
 *   command  POST /rest/items/<id>_pairing, text/plain: REQUEST, or the code;
 *            for a device whose pairing again is confirmed first (its
 *            confirm text), AGAIN once confirmed, and CANCEL to go back to
 *            the pairing it had while it pairs again (status repairing).
 *   follow   EventSource on the status and result Items' stateupdated events; the list
 *            is read again whenever the stream (re)opens, and every 5 s
 *            instead when there is no stream.
 */
(function (global) {
  "use strict";

  const LIST_URL = "/rest/items?metadata=pairing&fields=name,state,metadata";
  const EVENTS_URL = "/rest/events?topics=openhab/items/*_pairing_status/stateupdated,openhab/items/*_pairing_result/stateupdated";
  const REQUEST = "REQUEST";
  const AGAIN = "AGAIN";
  const CANCEL = "CANCEL";
  // The code field stays open this long after asking, even before the status says code_shown.
  const CODE_OPEN_MS = 5 * 60 * 1000;
  const ANSWER_MS = 30 * 1000;
  const POLL_MS = 5000;
  // Errors in a row, with no open between, before the stream is given up for polling.
  const STREAM_FAILURES = 3;
  const LAST = Number.MAX_SAFE_INTEGER;

  const STATES = {
    paired: "Paired",
    unpaired: "Not paired",
    code_shown: "Showing a code",
    repairing: "Pairing again",
    checking: "Checking",
    failed: "Code not accepted",
    closed: "Cannot ask for a code",
    unreachable: "Not answering (plug it in and turn it on)",
    unavailable: "Cannot pair right now",
    unknown: "Checking"
  };
  const TONES = {
    paired: "ok",
    unpaired: "",
    code_shown: "busy",
    repairing: "busy",
    checking: "busy",
    failed: "bad",
    closed: "bad",
    unreachable: "bad",
    unavailable: "muted",
    unknown: "busy"
  };
  const CHARS = { digits: "0-9", hex: "0-9A-Fa-f" };
  const CHARS_TEXT = { digits: "digits", hex: "letters and numbers" };

  const MESSAGES = {
    checking: "Checking the code\u2026",
    paired: "Paired. You can use the remote now.",
    failed: "The code was not accepted. Ask for a new code and try again.",
    failedAgain: "The code was not accepted, so thermite goes back to the pairing it had.",
    restored: "Back on the pairing it had: the remote works as before. Pair again to try once more.",
    repairing: "Type the new code its screen shows. Until then thermite cannot control it; Cancel goes back to the pairing it had.",
    offline: "Could not reach thermite. Check that this phone is on the home Wi-Fi."
  };

  // ---- reading the list ----------------------------------------------------

  function text(value) {
    return value == null ? "" : String(value).trim();
  }

  function number(value, fallback) {
    let parsed = NaN;
    if (typeof value === "number") parsed = value;
    else if (typeof value === "string" && value.trim() !== "") parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function flag(value) {
    if (typeof value === "string") return /^(true|yes|on|1)$/i.test(value.trim());
    return value === true || value === 1;
  }

  /** A status Item's state as one of STATES; NULL, UNDEF and anything unknown are "unknown". */
  function statusOf(value) {
    const state = text(value);
    return state !== "unknown" && Object.prototype.hasOwnProperty.call(STATES, state) ? state : "unknown";
  }

  // A credential-free acknowledgement. Error text is built here from known
  // fields, never copied from the TV's response or an arbitrary error string.
  function resultOf(value) {
    try {
      const result = JSON.parse(value);
      return result && typeof result.id === "string" && result.id && statusOf(result.state) !== "unknown"
        ? result : null;
    } catch (error) {
      return null;
    }
  }

  function registrationError(result) {
    const error = result && result.error;
    if (!error || typeof error !== "object") return null;
    const why = error.code === 1 ? "Sony error 1 (Internal Server Error)"
      : error.api === true ? "a Sony API error" : "no authentication cookie";
    const http = Number.isInteger(error.http) && error.http >= 100 && error.http <= 599 ? ", HTTP " + error.http : "";
    return "TV registration failed: " + why + http + ".";
  }

  /**
   * The REST list of Items -> rooms of devices: [{room, order, devices}],
   * rooms by their order then name, devices by label then id. Items without
   * pairing metadata, or whose code cannot be checked, are left out.
   */
  function describe(items) {
    const list = Array.isArray(items) ? items : [];
    const states = {};
    list.forEach((item) => {
      if (item && typeof item.name === "string") states[item.name] = item.state;
    });
    const seen = {};
    const rooms = {};
    list.forEach((item) => {
      const meta = item && item.metadata && item.metadata.pairing;
      if (!meta || typeof item.name !== "string") return;
      const config = meta.config || {};
      const chars = text(config.chars).toLowerCase();
      const length = number(config.length, NaN);
      if (!CHARS[chars] || !(length >= 1) || Math.floor(length) !== length) return;
      const id = text(config.device) || item.name.replace(/_pairing$/, "");
      if (seen[id]) return;
      seen[id] = true;
      const statusItem = text(config.status) || item.name + "_status";
      const room = text(config.room) || "Other";
      const order = number(config.order, LAST);
      const device = {
        id: id,
        item: item.name,
        statusItem: statusItem,
        label: text(config.label) || id,
        model: text(config.model),
        room: room,
        order: order,
        length: length,
        chars: chars,
        again: flag(config.again),
        hint: text(config.hint),
        confirm: text(config.confirm),
        integration: text(meta.value) || text(config.integration),
        status: statusOf(states[statusItem])
      };
      if (text(config.result)) {
        device.resultItem = text(config.result);
        device.result = resultOf(states[device.resultItem]);
      }
      if (!rooms[room]) rooms[room] = { room: room, order: order, devices: [] };
      rooms[room].order = Math.min(rooms[room].order, order);
      rooms[room].devices.push(device);
    });
    const compare = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
    return Object.keys(rooms).map((name) => rooms[name])
      .sort((a, b) => a.order - b.order || compare(a.room, b.room))
      .map((room) => {
        room.devices.sort((a, b) => compare(a.label, b.label) || compare(a.id, b.id));
        return room;
      });
  }

  // ---- codes ---------------------------------------------------------------

  /** The code as it is sent (spaces gone, hex upper-cased), or null if it is not one. */
  function cleanCode(device, value) {
    if (!device || !CHARS[device.chars]) return null;
    const code = String(value == null ? "" : value).replace(/\s+/g, "");
    if (!new RegExp("^[" + CHARS[device.chars] + "]{" + device.length + "}$").test(code)) return null;
    return device.chars === "hex" ? code.toUpperCase() : code;
  }

  function codeOk(device, value) {
    return cleanCode(device, value) !== null;
  }

  /** What a code for the device looks like, in words: "4 digits". */
  function codeRule(device) {
    return device.length + " " + (CHARS_TEXT[device.chars] || "characters");
  }

  function servedByOpenHab(location) {
    return Boolean(location) && /^https?:$/.test(location.protocol) && location.pathname.indexOf("/static/") === 0;
  }

  // ---- the backend ---------------------------------------------------------

  function Backend(options) {
    const config = options || {};
    this.base = config.base || "";
    this.setTimeout = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
    this.clearTimeout = config.clearTimeout || ((handle) => global.clearTimeout(handle));
    this.now = config.now || (() => Date.now());
    this.pollMs = config.pollMs != null ? config.pollMs : POLL_MS;
    this.codeOpenMs = config.codeOpenMs != null ? config.codeOpenMs : CODE_OPEN_MS;
    this.answerMs = config.answerMs != null ? config.answerMs : ANSWER_MS;
    this.mock = Boolean(config.mock);
    if (this.mock) {
      this.server = new MockServer(this, config.mockDelay != null ? config.mockDelay : 300);
      this.fetch = this.server.fetch;
      this.EventSource = this.server.EventSource;
    } else {
      this.fetch = config.fetch || ((url, init) => global.fetch(url, init));
      this.EventSource = "EventSource" in config ? config.EventSource : global.EventSource;
    }
    this.rooms = [];
    this.devices = {};
    this.byStatus = {};
    this.byResult = {};
    this.local = {};
    this.listeners = [];
    this.source = null;
    this.connected = null;
    this.failures = 0;
    this.polling = false;
    this.pollGeneration = 0;
    this.pollTimer = null;
    this.stopped = false;
    this.started = false;
    this.active = config.active !== false;
  }

  Backend.prototype.subscribe = function (callback) {
    this.listeners.push(callback);
    return () => {
      this.listeners = this.listeners.filter((listener) => listener !== callback);
    };
  };

  Backend.prototype.emit = function (event) {
    this.listeners.slice().forEach((listener) => {
      try {
        listener(event);
      } catch (error) {
        console.error(error);
      }
    });
  };

  Backend.prototype.changed = function (id) {
    this.emit({ type: "device", device: id });
  };

  Backend.prototype.device = function (id) {
    return this.devices[id] || null;
  };

  Backend.prototype.localOf = function (id) {
    if (!this.local[id]) {
      this.local[id] = { asked: null, pending: null, busy: null, awaitingResult: false, requesting: false, note: null, error: null, tone: "", confirming: false, again: false, timers: {} };
    }
    return this.local[id];
  };

  Backend.prototype.later = function (id, name, ms, callback) {
    const local = this.localOf(id);
    if (local.timers[name] != null) this.clearTimeout(local.timers[name]);
    local.timers[name] = this.setTimeout(() => {
      delete local.timers[name];
      callback();
    }, ms);
  };

  Backend.prototype.cancel = function (id, name) {
    const local = this.localOf(id);
    if (local.timers[name] != null) this.clearTimeout(local.timers[name]);
    delete local.timers[name];
  };

  Backend.prototype.start = function () {
    this.stopped = false;
    this.started = true;
    if (this.active) {
      if (this.EventSource) this.listen();
      else this.poll();
    }
    return this.load();
  };

  Backend.prototype.stop = function () {
    this.stopped = true;
    this.pollGeneration += 1;
    this.started = false;
    if (this.source) this.source.close();
    this.source = null;
    if (this.pollTimer != null) this.clearTimeout(this.pollTimer);
    this.pollTimer = null;
    Object.keys(this.local).forEach((id) => {
      Object.keys(this.local[id].timers).forEach((name) => this.cancel(id, name));
    });
  };

  Backend.prototype.setActive = function (active) {
    const next = Boolean(active);
    if (this.active === next || this.stopped) return Promise.resolve();
    this.active = next;
    this.pollGeneration += 1;
    if (this.source) this.source.close();
    this.source = null;
    if (this.pollTimer != null) this.clearTimeout(this.pollTimer);
    this.pollTimer = null;
    this.polling = false;
    if (!next || !this.started) return Promise.resolve();
    if (this.EventSource) this.listen();
    else this.poll();
    // Keep in-flight pairing state and timers intact; this only reads the
    // latest status/result, and never requests or submits a code.
    return this.load();
  };

  /** Reads the list again; the page calls this when it comes back into view. */
  Backend.prototype.refresh = function () {
    return this.load();
  };

  Backend.prototype.load = function () {
    const url = this.base + LIST_URL;
    return Promise.resolve()
      .then(() => this.fetch(url, { method: "GET", headers: { Accept: "application/json" } }))
      .then((response) => {
        if (!response.ok) throw new Error("Could not read the devices from thermite (HTTP " + response.status + ")");
        return response.text();
      }, () => {
        throw new Error(MESSAGES.offline);
      })
      .then((body) => {
        let items;
        try {
          items = JSON.parse(body);
        } catch (error) {
          throw new Error("thermite sent a list this page cannot read");
        }
        this.merge(describe(items));
        return this.rooms;
      })
      .catch((error) => {
        this.emit({ type: "error", message: error.message });
        throw error;
      });
  };

  Backend.prototype.merge = function (rooms) {
    const previous = this.devices;
    const statuses = {};
    const results = {};
    this.devices = {};
    this.byStatus = {};
    this.byResult = {};
    rooms.forEach((room) => room.devices.forEach((device) => {
      statuses[device.id] = device.status;
      if (previous[device.id]) device.status = previous[device.id].status;
      this.devices[device.id] = device;
      this.byStatus[device.statusItem] = device.id;
      if (device.resultItem) {
        results[device.id] = device.result;
        device.result = previous[device.id] ? previous[device.id].result : null;
        this.byResult[device.resultItem] = device.id;
      }
    }));
    Object.keys(this.local).forEach((id) => {
      if (this.devices[id]) return;
      Object.keys(this.local[id].timers).forEach((name) => this.cancel(id, name));
      delete this.local[id];
    });
    this.rooms = rooms;
    this.emit({ type: "list", rooms: rooms });
    Object.keys(statuses).forEach((id) => {
      if (previous[id]) this.applyStatus(id, statuses[id]);
      else this.changed(id);
    });
    Object.keys(results).forEach((id) => this.applyResult(id, results[id], false));
  };

  Backend.prototype.applyStatus = function (id, value) {
    const device = this.devices[id];
    if (!device) return;
    const local = this.localOf(id);
    const previous = device.status;
    const next = statusOf(value);
    device.status = next;
    if (next === "paired" || next === "code_shown" || next === "checking") local.error = null;
    const sent = local.pending !== null;
    // An update that repeats the status (openHAB sends one on every refresh) changes nothing.
    if (next !== previous || sent) {
      if (next !== previous) local.note = null;
      if (local.awaitingResult) {
        // A repeated status can still belong to the previous command. Sony's
        // fresh result settles the command even if no status transition occurs.
      } else if (next === "paired" || next === "failed") {
        if (sent) {
          local.note = next === "failed" && local.again ? MESSAGES.failedAgain : MESSAGES[next];
          local.tone = next === "paired" ? "ok" : "bad";
        } else if (next === "paired" && local.again) {
          // Paired again without a code that paired it: cancelled, refused or too late.
          local.note = MESSAGES.restored;
          local.tone = "";
        }
        if (next === "paired") local.again = false;
        this.settle(id);
      } else if ((next === "closed" || next === "unreachable" || next === "unavailable") && next !== previous) {
        local.again = false;
        this.settle(id);
      }
    }
    if (next !== previous) this.emit({ type: "status", device: id, status: next, previous: previous });
    this.changed(id);
  };

  Backend.prototype.applyResult = function (id, result, follow) {
    const device = this.devices[id];
    if (!device || !result || (device.result && result.id === device.result.id)) return;
    device.result = result;
    const local = this.localOf(id);
    local.awaitingResult = false;
    // Status changes notify observers immediately. Make the registration
    // failure available first, so a request for a PIN cannot briefly appear
    // to have submitted a code that the TV refused.
    if (follow !== false) local.error = result.state === "failed" ? registrationError(result) : null;
    // A result may precede its status event. A full list already carries the
    // latest status, which can have advanced since this saved acknowledgement.
    if (follow !== false) this.applyStatus(id, result.state);
    local.error = result.state === "failed" && (device.status === "failed" || device.status === "unpaired")
      ? registrationError(result) : null;
    this.settle(id);
    this.changed(id);
  };

  Backend.prototype.settle = function (id) {
    const local = this.localOf(id);
    local.awaitingResult = false;
    local.requesting = false;
    local.pending = null;
    local.asked = null;
    this.cancel(id, "answer");
    this.cancel(id, "asked");
  };

  Backend.prototype.post = function (item, body) {
    const url = this.base + "/rest/items/" + encodeURIComponent(item);
    return Promise.resolve()
      .then(() => this.fetch(url, { method: "POST", headers: { "Content-Type": "text/plain", Accept: "application/json" }, body: body }))
      .then((response) => {
        if (!response.ok) throw new Error("thermite did not take the request (HTTP " + response.status + ")");
      }, () => {
        throw new Error(MESSAGES.offline);
      });
  };

  /** Asks the device to show a code (Pair, Pair again, Ask for a new code). */
  Backend.prototype.request = function (id) {
    return this.ask(id, REQUEST);
  };

  /**
   * Pair again: asks for a code, or, for a device whose new pairing
   * replaces the one it has, first asks the question in view().confirm.
   */
  Backend.prototype.again = function (id) {
    const device = this.devices[id];
    if (!device) return Promise.reject(new Error("No such device: " + id));
    if (!device.confirm) return this.request(id);
    const local = this.localOf(id);
    local.confirming = true;
    local.note = null;
    this.changed(id);
    return Promise.resolve();
  };

  /** The answer to view().confirm: pair it again (AGAIN), or keep the pairing it has. */
  Backend.prototype.confirmAgain = function (id) {
    const local = this.localOf(id);
    local.confirming = false;
    local.again = true;
    return this.ask(id, AGAIN).catch((error) => {
      local.again = false;
      throw error;
    });
  };

  Backend.prototype.keepPairing = function (id) {
    if (!this.devices[id]) return;
    this.localOf(id).confirming = false;
    this.changed(id);
  };

  /** Stops pairing again; the device goes back to the pairing it had. */
  Backend.prototype.cancelAgain = function (id) {
    const device = this.devices[id];
    if (!device) return Promise.reject(new Error("No such device: " + id));
    const local = this.localOf(id);
    local.busy = "cancel";
    local.note = null;
    this.changed(id);
    return this.post(device.item, CANCEL).then(() => {
      local.busy = null;
      local.asked = null;
      local.pending = null;
      this.cancel(id, "asked");
      this.cancel(id, "answer");
      this.changed(id);
    }, (error) => {
      local.busy = null;
      local.note = error.message;
      local.tone = "bad";
      this.changed(id);
      throw error;
    });
  };

  Backend.prototype.ask = function (id, body) {
    const device = this.devices[id];
    if (!device) return Promise.reject(new Error("No such device: " + id));
    const local = this.localOf(id);
    const before = device.result && device.result.id;
    local.busy = "request";
    local.awaitingResult = Boolean(device.resultItem);
    local.requesting = Boolean(device.resultItem);
    local.note = null;
    local.error = null;
    local.pending = null;
    this.cancel(id, "answer");
    this.changed(id);
    return this.post(device.item, body).then(() => {
      local.busy = null;
      const current = this.devices[id];
      if (current && (!current.result || current.result.id === before)) {
        if (current.resultItem) {
          local.pending = this.now();
          local.asked = null;
          this.later(id, "answer", this.answerMs, () => this.noAnswer(id));
        } else {
          local.asked = this.now();
          this.later(id, "asked", this.codeOpenMs, () => this.changed(id));
        }
      }
      this.changed(id);
    }, (error) => {
      local.busy = null;
      const current = this.devices[id];
      if (current && current.result && current.result.id !== before) {
        // openHAB processed the command even if its POST response was lost.
        this.changed(id);
        return;
      }
      local.awaitingResult = false;
      local.requesting = false;
      local.note = error.message;
      local.tone = "bad";
      this.changed(id);
      throw error;
    });
  };

  /** Sends the code typed in; it is not kept anywhere once sent. */
  Backend.prototype.sendCode = function (id, value) {
    const device = this.devices[id];
    if (!device) return Promise.reject(new Error("No such device: " + id));
    const code = cleanCode(device, value);
    if (code === null) return Promise.reject(new Error("The code is " + codeRule(device)));
    const local = this.localOf(id);
    const before = device.result && device.result.id;
    local.busy = "code";
    local.awaitingResult = Boolean(device.resultItem);
    local.requesting = false;
    local.note = null;
    local.error = null;
    this.changed(id);
    return this.post(device.item, code).then(() => {
      local.busy = null;
      const current = this.devices[id];
      if (current && (!current.result || current.result.id === before)) {
        local.pending = this.now();
        local.asked = this.now();
        this.later(id, "answer", this.answerMs, () => this.noAnswer(id));
      }
      this.changed(id);
    }, (error) => {
      local.busy = null;
      const current = this.devices[id];
      if (current && current.result && current.result.id !== before) {
        this.changed(id);
        return;
      }
      local.awaitingResult = false;
      local.note = error.message;
      local.tone = "bad";
      this.changed(id);
      throw error;
    });
  };

  Backend.prototype.noAnswer = function (id) {
    const after = () => {
      const device = this.devices[id];
      const local = this.local[id];
      if (!device || !local || local.pending === null) return;
      local.awaitingResult = false;
      local.pending = null;
      local.asked = local.requesting ? null : this.now();
      local.requesting = false;
      if (local.asked !== null) this.later(id, "asked", this.codeOpenMs, () => this.changed(id));
      local.note = "There is no answer from the " + device.label + " yet. If its screen shows a new code, type that one, or ask for a new code.";
      local.tone = "busy";
      this.changed(id);
    };
    return this.load().then(after, after);
  };

  /** What the page shows for a device now. */
  Backend.prototype.view = function (id) {
    const device = this.devices[id];
    if (!device) return null;
    const local = this.localOf(id);
    const status = device.status;
    const asked = local.asked !== null && this.now() - local.asked < this.codeOpenMs;
    const checking = local.pending !== null;
    const confirming = local.confirming && status === "paired" && !checking;
    const repairing = status === "repairing";
    // Closed, the request asked for nothing: a request still closed changes no status, so asked alone opens no form.
    const form = !checking && !confirming && status !== "unavailable"
      && (status === "code_shown" || repairing || (asked && status !== "closed"));
    let action = null;
    if (!checking && !form && !confirming && status !== "unavailable") {
      if (status === "paired") action = device.again ? "again" : null;
      else action = "pair";
    }
    let note = local.error || local.note;
    let tone = local.error ? "bad" : local.tone;
    if (checking) {
      note = local.requesting ? "Asking the TV to show a code\u2026" : MESSAGES.checking;
      tone = "busy";
    } else if (!note && repairing) {
      note = MESSAGES.repairing;
      tone = "busy";
    } else if (!note && status === "unavailable") {
      note = "thermite cannot pair with it right now: its connection to the " + device.label + " is not running.";
      tone = "";
    } else if (!note && status === "closed") {
      note = "The " + device.label + " does not let thermite ask it for a code: its pairing service does not answer. "
        + "Restart it (unplug it for a minute), then ask for a code again.";
      tone = "bad";
    }
    return {
      status: status,
      label: local.error && status === "failed" ? "Pairing failed" : STATES[status],
      tone: TONES[status],
      action: action,
      form: form,
      // The question to answer before pairing again, and whether pairing again can be cancelled.
      confirm: confirming ? device.confirm : null,
      cancel: form && (repairing || local.again),
      checking: checking,
      busy: local.busy,
      note: note,
      noteTone: note ? tone : ""
    };
  };

  Backend.prototype.listen = function () {
    if (this.stopped || !this.active) return;
    const source = new this.EventSource(this.base + EVENTS_URL);
    this.source = source;
    this.failures = 0;
    source.onmessage = (message) => {
      if (this.source === source && !this.stopped && this.active) this.onMessage(message && message.data);
    };
    source.onopen = () => {
      if (this.source !== source || this.stopped || !this.active) return;
      this.failures = 0;
      if (this.connected === false) this.emit({ type: "connection", connected: true });
      this.connected = true;
      this.load().catch(() => null);
    };
    source.onerror = () => {
      if (this.source !== source || this.stopped || !this.active) return;
      this.failures += 1;
      if (this.connected !== false) this.emit({ type: "connection", connected: false });
      this.connected = false;
      // EventSource reconnects by itself, but gives up (CLOSED) on some answers.
      if (this.failures >= STREAM_FAILURES || source.readyState === 2) {
        source.close();
        if (this.source === source) this.source = null;
        this.poll();
      }
    };
  };

  Backend.prototype.poll = function () {
    if (this.polling || this.stopped || !this.active) return;
    this.polling = true;
    const generation = this.pollGeneration;
    const tick = () => {
      this.pollTimer = null;
      if (this.stopped || !this.active || generation !== this.pollGeneration) return;
      const next = () => {
        if (!this.stopped && this.active && generation === this.pollGeneration) this.pollTimer = this.setTimeout(tick, this.pollMs);
      };
      this.load().then(next, next);
    };
    this.pollTimer = this.setTimeout(tick, this.pollMs);
  };

  Backend.prototype.onMessage = function (data) {
    let event;
    let payload;
    try {
      event = JSON.parse(data);
      payload = typeof event.payload === "string" ? JSON.parse(event.payload) : event.payload;
    } catch (error) {
      return;
    }
    const match = /^openhab\/items\/([^/]+)\/stateupdated$/.exec((event && event.topic) || "");
    if (!match || !payload || typeof payload !== "object") return;
    const id = this.byStatus[match[1]];
    if (id) this.applyStatus(id, payload.value);
    const resultId = this.byResult[match[1]];
    if (resultId) this.applyResult(resultId, resultOf(payload.value));
  };

  // ---- the mock ------------------------------------------------------------

  const MOCK_CODES = { digits: "1234", hex: "A1B2C3" };

  function mockItems() {
    const devices = [["example_player", "pyatv_http", "Example Player", "Apple TV", "Example West", 1, 4, "digits", true, "unpaired", "Ask for a code while standing where you can see the screen."], ["example_tv", "sony_ip_control", "Example TV", "Sony TV", "Example West", 1, 4, "digits", true, "paired", "Check Normal IP authentication on the TV before asking for a code."], ["example_streamer", "androidtv_googletv", "Example Streamer", "Google TV", "Example East", 3, 6, "hex", true, "paired", "Wake the screen with its own remote, then ask for a code."]];
    const confirm = (label) => "Pair the " + label + " again? It shows a new code, and thermite cannot control it "
      + "until that code is typed here. Cancel goes back to the pairing it has now.";
    const items = [{ name: "loc_example_west_source", state: "off" }];
    devices.forEach((d) => {
      items.push({
        name: d[0] + "_pairing",
        state: "NULL",
        metadata: { pairing: { value: d[1], config: {
          device: d[0], integration: d[1], label: d[2], model: d[3], room: d[4], order: d[5],
          status: d[0] + "_pairing_status", length: d[6], chars: d[7], again: d[8], hint: d[10],
          confirm: d[1] === "androidtv_googletv" ? confirm(d[2]) : "" } } }
      });
      items.push({ name: d[0] + "_pairing_status", state: d[9] });
    });
    return items;
  }

  /** A stand-in for openHAB's REST API and event stream, for the page opened off openHAB. */
  function MockServer(backend, delay) {
    const server = this;
    this.items = mockItems();
    this.sources = [];
    this.fetch = (url, init) => server.answer(url, init || {});
    function MockEventSource(url) {
      this.url = url;
      this.readyState = 0;
      server.sources.push(this);
      backend.setTimeout(() => {
        if (this.readyState === 2) return;
        this.readyState = 1;
        if (this.onopen) this.onopen({});
      }, 0);
    }
    MockEventSource.prototype.close = function () {
      this.readyState = 2;
      server.sources = server.sources.filter((source) => source !== this);
    };
    this.EventSource = MockEventSource;
    this.later = (callback) => backend.setTimeout(callback, delay);
  }

  MockServer.prototype.reply = function (status, body) {
    return Promise.resolve({ ok: status < 300, status: status, text: () => Promise.resolve(body || "") });
  };

  MockServer.prototype.set = function (name, value) {
    const item = this.items.find((entry) => entry.name === name);
    if (item) item.state = value;
    const data = JSON.stringify({
      topic: "openhab/items/" + name + "/stateupdated",
      payload: JSON.stringify({ type: "String", value: value }),
      type: "ItemStateUpdatedEvent"
    });
    this.sources.slice().forEach((source) => {
      if (source.onmessage) source.onmessage({ data: data });
    });
  };

  MockServer.prototype.answer = function (url, init) {
    const method = init.method || "GET";
    if (method === "GET" && url.endsWith(LIST_URL)) return this.reply(200, JSON.stringify(this.items));
    const match = /\/rest\/items\/([^/?]+)$/.exec(url);
    const item = match && this.items.find((entry) => entry.name === decodeURIComponent(match[1]));
    if (!item || method !== "POST" || !item.metadata) return this.reply(404);
    const config = item.metadata.pairing.config;
    const body = String(init.body);
    const status = this.items.find((entry) => entry.name === config.status);
    const repairing = status && status.state === "repairing";
    // As the androidtv_googletv rule does: pairing again is left by a code that pairs, or goes back.
    const goBack = (state) => this.later(() => {
      this.set(config.status, state);
      this.later(() => this.set(config.status, "paired"));
    });
    if (body === AGAIN) {
      this.later(() => this.set(config.status, "repairing"));
    } else if (body === CANCEL) {
      if (repairing) goBack("checking");
    } else if (body === REQUEST) {
      if (!repairing) this.later(() => this.set(config.status, "code_shown"));
    } else if (body === MOCK_CODES[config.chars]) {
      this.later(() => this.set(config.status, "paired"));
    } else if (repairing) {
      goBack("failed");
    } else {
      this.later(() => this.set(config.status, "failed"));
    }
    return this.reply(200);
  };

  function create(options) {
    const config = Object.assign({}, options || {});
    const location = "location" in config ? config.location : global.location;
    const kind = config.backend || (servedByOpenHab(location) ? "openhab" : "mock");
    config.mock = kind === "mock";
    return new Backend(config);
  }

  global.PairingBackend = {
    LIST_URL: LIST_URL,
    EVENTS_URL: EVENTS_URL,
    REQUEST: REQUEST,
    AGAIN: AGAIN,
    CANCEL: CANCEL,
    STATES: STATES,
    MESSAGES: MESSAGES,
    MOCK_CODES: MOCK_CODES,
    ICONS: ["back", "check", "warn", "info", "dot"],
    Backend: Backend,
    MockServer: MockServer,
    create: create,
    servedByOpenHab: servedByOpenHab,
    describe: describe,
    statusOf: statusOf,
    codeOk: codeOk,
    cleanCode: cleanCode,
    codeRule: codeRule
  };

  // ---- the page ------------------------------------------------------------

  if (typeof document === "undefined") return;

  const STATUS_ICONS = { ok: "check", bad: "warn", muted: "info" };

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = content;
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function setIcon(svg, name) {
    svg.firstChild.setAttribute("href", "#i-" + name);
  }

  function startPage() {
    const params = new URLSearchParams(location.search);
    const chosen = params.get("backend");
    const backend = create({ backend: chosen === "mock" || chosen === "openhab" ? chosen : undefined,
      active: document.visibilityState !== "hidden" });
    const roomsNode = document.getElementById("rooms");
    const alertNode = document.getElementById("alert");
    const alertText = document.getElementById("alert-text");
    const announcer = document.getElementById("announcer");
    document.getElementById("mock-note").hidden = !backend.mock;
    const cards = Object.create(null);
    const sorting = { key: "room", descending: false };
    const headers = Object.create(null);
    const table = element("table", "pair-table");
    const caption = element("caption", "pair-table__caption", "Looking for TVs and streamers\u2026");
    const tableHead = element("thead");
    const headerRow = element("tr");
    const tableBody = element("tbody");
    const empty = element("p", "pair__empty", "thermite has no TVs or streamers to pair yet.");
    empty.hidden = true;
    [["room", "Room"], ["device", "Device"], ["model", "Model"], ["status", "Status"], ["actions", "Actions"]].forEach(([key, label]) => {
      const th = element("th", "pair-table__" + key);
      th.scope = "col";
      const button = element("button", "pair-table__sort", label);
      button.type = "button";
      button.addEventListener("click", () => {
        sorting.descending = sorting.key === key ? !sorting.descending : false;
        sorting.key = key;
        arrange();
      });
      th.appendChild(button); headerRow.appendChild(th);
      headers[key] = { th, button, label };
    });
    tableHead.appendChild(headerRow);
    table.append(caption, tableHead, tableBody);
    roomsNode.replaceChildren(table, empty);
    let focusCode = null;
    let focusConfirm = null;

    let queued = [];
    function announce(message) {
      if (queued.indexOf(message) < 0) queued.push(message);
      if (queued.length > 1) return;
      announcer.textContent = "";
      // Cleared first and filled a moment later, so the same message twice is read twice.
      setTimeout(() => {
        announcer.textContent = queued.join(" ");
        queued = [];
      }, 50);
    }

    function showAlert(message) {
      alertText.textContent = message || "";
      alertNode.hidden = !message;
    }

    function act(promise) {
      promise.catch(() => null);
    }

    // Reorder the existing row and detail nodes, never an input's contents.
    // Moving a focused DOM subtree can blur it, so restore that same element
    // and its caret without scrolling the page or horizontal table region.
    function keepFocus(work) {
      const active = document.activeElement;
      const caret = active && typeof active.selectionStart === "number"
        ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
      const left = roomsNode.scrollLeft, top = roomsNode.scrollTop;
      const x = window.scrollX, y = window.scrollY;
      work();
      if (active && table.contains(active)) {
        if (document.activeElement !== active) active.focus({ preventScroll: true });
        if (caret && active.setSelectionRange) active.setSelectionRange(...caret);
      }
      roomsNode.scrollLeft = left; roomsNode.scrollTop = top;
      if (window.scrollTo && (window.scrollX !== x || window.scrollY !== y)) window.scrollTo(x, y);
    }

    function arrange() {
      const compare = (a, b) => a.localeCompare(b, "en", { numeric: true, sensitivity: "base" });
      const entries = Object.values(cards).sort((a, b) => compare(a.sort[sorting.key], b.sort[sorting.key])
        * (sorting.descending ? -1 : 1) || compare(a.id, b.id));
      const nodes = entries.flatMap(entry => [entry.root, entry.detail]);
      if (nodes.some((node, index) => tableBody.children[index] !== node)) keepFocus(() => nodes.forEach(node => tableBody.appendChild(node)));
      Object.entries(headers).forEach(([key, entry]) => {
        const selected = key === sorting.key;
        entry.th.setAttribute("aria-sort", selected ? sorting.descending ? "descending" : "ascending" : "none");
        entry.button.textContent = entry.label + (selected ? sorting.descending ? " \u2193" : " \u2191" : "");
        entry.button.setAttribute("aria-label", "Sort by " + entry.label);
      });
    }

    function card(device) {
      const id = device.id;
      const root = element("tr", "pair-device");
      root.dataset.device = id;
      const room = element("td", "pair-device__room", device.room);
      const name = element("th", "pair-device__label", device.label);
      name.scope = "row";
      const model = element("td", "pair-device__model", device.model || "\u2014");
      root.append(room, name, model);

      const status = element("p", "pair-device__status");
      const statusIcon = icon("dot");
      const statusText = element("span");
      status.append(statusIcon, statusText);
      const statusCell = element("td");
      statusCell.appendChild(status);
      root.appendChild(statusCell);

      const detail = element("tr", "pair-device__detail");
      detail.dataset.deviceDetail = id;
      detail.hidden = true;
      const detailCell = element("td");
      detailCell.colSpan = 5;
      const detailBody = element("div", "pair-device__body");
      detailCell.appendChild(detailBody); detail.appendChild(detailCell);

      const note = element("p", "pair-device__note");
      note.hidden = true;
      detailBody.appendChild(note);

      const actions = element("div", "pair-device__actions");
      const pair = element("button", "button button--primary", "Pair");
      pair.type = "button";
      const again = element("button", "button button--quiet", "Pair again");
      again.type = "button";
      const idle = element("span", "pair-device__idle", "\u2014");
      actions.append(pair, again, idle);
      const actionCell = element("td");
      actionCell.appendChild(actions); root.appendChild(actionCell);

      const confirm = element("div", "pair-confirm");
      confirm.hidden = true;
      const question = element("p", "pair-confirm__text");
      question.id = "confirm-" + id;
      confirm.setAttribute("role", "group");
      confirm.setAttribute("aria-labelledby", question.id);
      const yes = element("button", "button button--primary", "Pair again");
      yes.type = "button";
      const keep = element("button", "button button--quiet", "Keep this pairing");
      keep.type = "button";
      confirm.append(question, yes, keep);
      detailBody.appendChild(confirm);

      const form = element("form", "pair-code");
      form.noValidate = true;
      form.hidden = true;
      const inputId = "code-" + id;
      const label = element("label", "pair-code__label", "Code on the " + device.label + " screen");
      label.htmlFor = inputId;
      const row = element("div", "pair-code__row");
      const input = element("input", "pair-code__input");
      input.id = inputId;
      input.type = "text";
      input.name = "code";
      input.maxLength = device.length;
      input.size = device.length + 1;
      input.setAttribute("autocomplete", "one-time-code");
      input.setAttribute("autocorrect", "off");
      input.setAttribute("spellcheck", "false");
      if (device.chars === "digits") {
        input.setAttribute("inputmode", "numeric");
        input.setAttribute("pattern", "[0-9]*");
      } else {
        input.setAttribute("autocapitalize", "characters");
        input.classList.add("pair-code__input--hex");
      }
      const rule = element("p", "pair-code__rule", "The code is " + codeRule(device) + ".");
      rule.id = inputId + "-rule";
      input.setAttribute("aria-describedby", rule.id);
      const send = element("button", "button button--primary pair-code__send", "Send code");
      send.type = "submit";
      send.disabled = true;
      row.append(input, send);
      const fresh = element("button", "button button--quiet pair-code__again", "Ask for a new code");
      fresh.type = "button";
      const stop = element("button", "button button--quiet pair-code__cancel", "Cancel: keep the old pairing");
      stop.type = "button";
      stop.hidden = true;
      form.append(label, row, rule, fresh, stop);
      const hint = element("p", "pair-code__hint", device.hint);
      hint.hidden = !device.hint;
      form.appendChild(hint);
      detailBody.appendChild(form);

      const ask = () => {
        focusCode = id;
        act(backend.request(id));
      };
      pair.addEventListener("click", ask);
      again.addEventListener("click", () => {
        const current = backend.device(id);
        if (!current) return;
        focusConfirm = current.confirm ? id : null;
        focusCode = id;
        act(backend.again(id));
      });
      yes.addEventListener("click", () => {
        focusCode = id;
        act(backend.confirmAgain(id));
      });
      keep.addEventListener("click", () => {
        backend.keepPairing(id);
        status.focus();
      });
      stop.addEventListener("click", () => {
        input.value = "";
        status.focus();
        act(backend.cancelAgain(id));
      });
      fresh.addEventListener("click", () => {
        input.value = "";
        ask();
      });
      input.addEventListener("input", () => {
        // Spaces are dropped as they are typed, or maxlength would leave no room for the code.
        if (/\s/.test(input.value)) input.value = input.value.replace(/\s+/g, "");
        update(id);
      });
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const current = backend.device(id);
        if (!current || !codeOk(current, input.value) || backend.view(id).busy) return;
        const value = input.value;
        input.value = "";
        status.focus();
        act(backend.sendCode(id, value));
      });
      status.tabIndex = -1;

      return { id, root, room, name, model, detail, status, statusIcon, statusText, note, pair, again, idle,
        confirm, question, yes, keep, form, input, label, rule, hint, send, fresh, stop,
        sort: { room: device.room, device: device.label, model: device.model || "", status: "", actions: "" }, lastNote: null };
    }

    function update(id, reorder = true) {
      const entry = cards[id];
      const device = backend.device(id);
      const view = backend.view(id);
      if (!entry || !device || !view) return;
      entry.status.dataset.tone = view.tone;
      setIcon(entry.statusIcon, STATUS_ICONS[view.tone] || "dot");
      entry.statusText.textContent = view.label;
      entry.note.hidden = !view.note;
      entry.note.textContent = view.note || "";
      entry.note.dataset.tone = view.noteTone;
      if (view.note && view.note !== entry.lastNote) {
        announce(device.label + ", " + device.room + ": " + view.note);
      }
      entry.lastNote = view.note;
      const busy = Boolean(view.busy);
      entry.pair.hidden = view.action !== "pair";
      entry.again.hidden = view.action !== "again";
      entry.pair.disabled = entry.again.disabled = busy;
      entry.pair.textContent = view.busy === "request" && view.action === "pair" ? "Asking\u2026" : "Pair";
      entry.again.textContent = view.busy === "request" && view.action === "again" ? "Asking\u2026" : "Pair again";
      const asking = entry.confirm.hidden && Boolean(view.confirm);
      entry.confirm.hidden = !view.confirm;
      entry.question.textContent = view.confirm || "";
      entry.yes.disabled = entry.keep.disabled = busy;
      if (asking) {
        announce(device.label + ", " + device.room + ": " + view.confirm);
        if (focusConfirm === id) {
          focusConfirm = null;
          entry.keep.focus();
        }
      }
      entry.stop.hidden = !view.cancel;
      entry.stop.disabled = busy;
      entry.stop.textContent = view.busy === "cancel" ? "Cancelling\u2026" : "Cancel: keep the old pairing";
      const opening = entry.form.hidden && view.form;
      entry.form.hidden = !view.form;
      if (!view.form) entry.input.value = "";
      entry.send.disabled = busy || !codeOk(device, entry.input.value);
      entry.send.textContent = view.busy === "code" ? "Sending\u2026" : "Send code";
      entry.fresh.disabled = busy;
      entry.fresh.textContent = view.busy === "request" ? "Asking\u2026" : "Ask for a new code";
      if (opening && focusCode === id) {
        focusCode = null;
        entry.input.focus();
      }
      entry.idle.hidden = !entry.pair.hidden || !entry.again.hidden;
      const action = view.confirm ? "Confirm pairing" : view.form ? "Enter code" : view.action === "pair" ? "Pair" : view.action === "again" ? "Pair again" : "\u2014";
      entry.idle.textContent = action;
      entry.detail.hidden = !view.note && !view.confirm && !view.form;
      entry.sort.status = view.label;
      entry.sort.actions = action;
      if (reorder && (sorting.key === "status" || sorting.key === "actions")) arrange();
    }

    function build(rooms) {
      keepFocus(() => {
        const known = new Set();
        rooms.forEach(room => room.devices.forEach(device => {
          known.add(device.id);
          const entry = cards[device.id] || (cards[device.id] = card(device));
          entry.room.textContent = device.room;
          entry.name.textContent = device.label;
          entry.model.textContent = device.model || "\u2014";
          entry.room.title = device.room; entry.name.title = device.label; entry.model.title = device.model || "";
          entry.label.textContent = "Code on the " + device.label + " screen";
          entry.rule.textContent = "The code is " + codeRule(device) + ".";
          entry.hint.textContent = device.hint || ""; entry.hint.hidden = !device.hint;
          entry.input.maxLength = device.length; entry.input.size = device.length + 1;
          entry.input.classList.toggle("pair-code__input--hex", device.chars === "hex");
          if (device.chars === "digits") {
            entry.input.setAttribute("inputmode", "numeric"); entry.input.setAttribute("pattern", "[0-9]*");
            entry.input.removeAttribute("autocapitalize");
          } else {
            entry.input.setAttribute("autocapitalize", "characters");
            entry.input.removeAttribute("inputmode"); entry.input.removeAttribute("pattern");
          }
          entry.sort.room = device.room; entry.sort.device = device.label; entry.sort.model = device.model || "";
          update(device.id, false);
        }));
        Object.keys(cards).forEach(id => {
          if (known.has(id)) return;
          cards[id].input.value = "";
          cards[id].root.remove(); cards[id].detail.remove(); delete cards[id];
        });
        roomsNode.removeAttribute("aria-busy");
        caption.textContent = known.size + (known.size === 1 ? " device" : " devices") + " \u00b7 pair one screen at a time";
        table.hidden = !known.size; empty.hidden = Boolean(known.size);
        arrange();
      });
    }

    backend.subscribe((event) => {
      if (event.type === "list") {
        showAlert(null);
        build(event.rooms);
      } else if (event.type === "device") {
        update(event.device);
      } else if (event.type === "status") {
        const device = backend.device(event.device);
        // A status that comes with a note (Paired, not accepted) is announced by the note.
        if (device && !backend.view(event.device).note) announce(device.label + ", " + device.room + ": " + STATES[event.status] + ".");
      } else if (event.type === "error") {
        showAlert(event.message);
      }
    });

    document.addEventListener("visibilitychange", () => {
      act(backend.setActive(document.visibilityState !== "hidden"));
      if (document.visibilityState === "visible") {
        checkForUpdate();
      }
    });
    window.addEventListener("pagehide", () => { act(backend.setActive(false)); });
    window.addEventListener("pageshow", () => { act(backend.setActive(document.visibilityState !== "hidden")); });
    act(backend.start());
    checkForUpdate();
  }

  // The same check as the remote's (README "Updates"): openHAB serves
  // /static/ with a year's max-age, so compare each file with the server's
  // copy and reload once when one differs.
  const UPDATE_EVERY = 60000;
  let checkedAt = 0;

  function checkForUpdate() {
    if (!/^https?:$/.test(location.protocol) || typeof fetch !== "function") return;
    if (Date.now() - checkedAt < UPDATE_EVERY) return;
    checkedAt = Date.now();
    const urls = [location.href.split("#")[0]].concat(Array.from(
      document.querySelectorAll("script[src], link[rel=stylesheet][href]"), (node) => node.src || node.href));
    Promise.all(urls.map(changedOnServer))
      .then((changed) => {
        if (!changed.some(Boolean)) return;
        // A reload that did not bring the new files must not become a loop.
        if (Date.now() - Number(stored("pair.updated") || 0) < UPDATE_EVERY) return;
        stored("pair.updated", String(Date.now()));
        location.reload();
      })
      .catch(() => { /* offline or thermite restarting: the next check tries again */ });
  }

  function changedOnServer(url) {
    return fetch(url, { cache: "force-cache" })
      .then((cached) => cached.text())
      .then((running) => fetch(url, { cache: "no-cache" })
        .then((latest) => (latest.ok ? latest.text() : running))
        .then((text) => text !== running));
  }

  function stored(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (error) {
      return null;
    }
    return value;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startPage);
  else startPage();
})(globalThis);
