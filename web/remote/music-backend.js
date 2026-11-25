/*
 * Music's two backends, as backend.js has the remote's: the gateway's music
 * API (oh_identity.music_api) and its event stream, or a mock house of
 * invented speakers and a library that plays nothing, which is
 * music-backend-mock.js's, a file loaded only where the sample is asked
 * for. music-session.js drives either; neither decides what a page shows.
 *
 * MusicBackend.create(settings) is the gateway's when the page is served by
 * it (ThermiteNavigation.hosted) and access is not the mock's, else the
 * mock; settings.backend ("gateway" or "mock") chooses. Both answer, each
 * a promise of the API's answer or a rejection with access.js's problem
 * ({code, status, message}):
 *
 *   overview()                          GET /api/v1/music
 *   search(q, kind, source)             GET …/search
 *   browse(ref, offset, limit)          GET …/browse (ref null: the roots)
 *   details(ref)                        GET …/item: an album's, artist's or playlist's page
 *   queue(speaker, offset, limit)       GET …/<speaker>/queue
 *   play(speaker, ref, option, extra)   POST …/<speaker>/play (extra: {start} or {shuffle})
 *   transport(speaker, action, value)   POST …/<speaker>/transport
 *   volume(speaker, level, group)       POST …/<speaker>/volume
 *   group(speaker, members), ungroup(speaker)
 *   tidal(), tidalStart(), tidalFinish()   an Admin's TIDAL sign-in
 *
 * art(id, size) is a cover's address (null without one); subscribe(callback)
 * hears the stream as {type: "snapshot", music}, {type: "speaker",
 * speaker}, {type: "unavailable"} and {type: "connection", connected}.
 * open() opens the stream, close() lets it go; setActive(false) releases it
 * while the page is hidden and setActive(true) opens it again.
 */
(function (global) {
  "use strict";

  // How long a stream closed for good waits before it opens again, as backend.js's.
  const REOPEN_MS = [1000, 2000, 5000, 15000, 30000];
  // A search or a folder Music Assistant may take 20 s over (oh_music.client.SLOW):
  // the page waits for it a little longer.
  const SLOW_MS = 25000;
  const ART = /^[0-9a-f]{64}$/;

  function problem(code, status) {
    if (global.ThermiteAccess) return global.ThermiteAccess.problem(code, status);
    return Object.assign(new Error(code), { code: code, status: status || 0 });
  }

  function query(values) {
    const params = new URLSearchParams();
    Object.keys(values).forEach((key) => {
      if (values[key] !== null && values[key] !== undefined && values[key] !== "") params.set(key, String(values[key]));
    });
    const text = params.toString();
    return text ? "?" + text : "";
  }

  function listeners(target) {
    target.listeners = [];
    target.subscribe = function (callback) {
      target.listeners.push(callback);
      return () => { const index = target.listeners.indexOf(callback); if (index >= 0) target.listeners.splice(index, 1); };
    };
    target.emit = function (event) {
      target.listeners.slice().forEach((listener) => {
        try { listener(event); } catch (error) { console.error(error); }
      });
    };
  }

  // ---- the gateway ----------------------------------------------------------

  function GatewayMusic(settings) {
    const config = settings || {};
    listeners(this);
    this.access = config.access || (global.ThermiteAccess && global.ThermiteAccess.client) || null;
    this.base = config.base || "";
    this.EventSource = config.EventSource || global.EventSource;
    this.later = config.setTimeout || ((callback, ms) => global.setTimeout(callback, ms));
    this.cancel = config.clearTimeout || ((handle) => global.clearTimeout(handle));
    this.active = config.active !== false;
    this.wanted = false;
    this.source = null;
    this.sse = null;
    this.generation = 0;
    this.attempt = 0;
    this.timer = 0;
  }

  // A gateway's reads and commands; openHAB itself (--native) has no music.
  GatewayMusic.prototype.read = function (path, ms) {
    if (!this.access || this.access.native) return Promise.reject(problem("access_not_configured", 404));
    return this.access.musicRead(path, ms);
  };

  GatewayMusic.prototype.command = function (path, value) {
    if (!this.access || this.access.native) return Promise.reject(problem("access_not_configured", 404));
    return this.access.musicCommand(path, value);
  };

  GatewayMusic.prototype.overview = function () { return this.read(""); };
  GatewayMusic.prototype.search = function (q, kind, source) { return this.read("/search" + query({ q: q, kind: kind, source: source }), SLOW_MS); };
  GatewayMusic.prototype.browse = function (ref, offset, limit) { return this.read("/browse" + query({ ref: ref, offset: offset, limit: limit }), SLOW_MS); };
  GatewayMusic.prototype.queue = function (speaker, offset, limit) {
    return this.read("/" + encodeURIComponent(speaker) + "/queue" + query({ offset: offset, limit: limit }));
  };
  GatewayMusic.prototype.details = function (ref) { return this.read("/item" + query({ ref: ref }), SLOW_MS); };
  GatewayMusic.prototype.play = function (speaker, ref, option, extra) {
    return this.command(encodeURIComponent(speaker) + "/play", Object.assign({ ref: ref, option: option }, extra));
  };
  GatewayMusic.prototype.transport = function (speaker, action, value) {
    return this.command(encodeURIComponent(speaker) + "/transport", value === undefined ? { action: action } : { action: action, value: value });
  };
  GatewayMusic.prototype.volume = function (speaker, level, group) {
    return this.command(encodeURIComponent(speaker) + "/volume", group ? { group_level: level } : { level: level });
  };
  GatewayMusic.prototype.group = function (speaker, members) { return this.command(encodeURIComponent(speaker) + "/group", { members: members }); };
  GatewayMusic.prototype.ungroup = function (speaker) { return this.command(encodeURIComponent(speaker) + "/ungroup", {}); };
  GatewayMusic.prototype.tidal = function () { return this.read("/tidal"); };
  GatewayMusic.prototype.tidalStart = function () { return this.command("tidal/start", {}); };
  GatewayMusic.prototype.tidalFinish = function () { return this.command("tidal/finish", {}); };

  // Covers come only through the gateway's art route (the pages' CSP has no other image source).
  GatewayMusic.prototype.art = function (id, size) {
    return typeof id === "string" && ART.test(id) ? this.base + "/api/v1/music/art/" + id + "?size=" + (size || 256) : null;
  };

  GatewayMusic.prototype.open = function () {
    this.wanted = true;
    this.listen();
  };

  GatewayMusic.prototype.close = function () {
    this.wanted = false;
    this.listen();
  };

  // The music stream, open while wanted and the page is shown; its first
  // frame is the snapshot, so a stream opened again reads everything afresh.
  GatewayMusic.prototype.listen = function () {
    if (this.sse) {
      this.sse.close();
      this.sse = null;
    } else if (this.source) {
      this.source.close();
    }
    this.source = null;
    this.cancel(this.timer);
    this.timer = 0;
    const generation = ++this.generation;
    if (!this.wanted || !this.active || !this.EventSource) return;
    const onMessage = (message) => {
      let frame;
      try { frame = JSON.parse(message.data); } catch (_) { return; }
      if (!frame || typeof frame !== "object") return;
      if (frame.type === "snapshot" && frame.music && typeof frame.music === "object") {
        this.emit({ type: "connection", connected: true });
        this.emit({ type: "snapshot", music: frame.music });
      } else if (frame.type === "speaker" && frame.speaker && typeof frame.speaker.id === "string") {
        this.emit({ type: "speaker", speaker: frame.speaker });
      } else if (frame.type === "unavailable") this.emit({ type: "unavailable" });
    };
    if (global.ThermiteSse && typeof global.ThermiteSse.create === "function") {
      this.sse = global.ThermiteSse.create({
        url: () => this.base + "/api/v1/events/music",
        EventSource: this.EventSource,
        backoff: REOPEN_MS,
        setTimeout: this.later,
        clearTimeout: this.cancel,
        active: this.active,
        onSource: (source) => { if (generation === this.generation) this.source = source; },
        onOpen: () => { if (generation === this.generation) this.attempt = 0; },
        onMessage: (message) => { if (generation === this.generation) onMessage(message); },
        onError: () => { if (generation === this.generation) this.emit({ type: "connection", connected: false }); }
      });
      this.source = this.sse.open();
      return;
    }
    const source = this.source = new this.EventSource(this.base + "/api/v1/events/music");
    const current = () => generation === this.generation;
    source.onopen = () => { if (current()) this.attempt = 0; };
    source.onmessage = (message) => { if (current()) onMessage(message); };
    source.onerror = () => {
      if (!current()) return;
      this.emit({ type: "connection", connected: false });
      // A browser closes a stream for good on any non-200 answer (a 502
      // while the gateway restarts) and never retries it by itself.
      if (source.readyState === 2) this.reopen(generation);
    };
  };

  GatewayMusic.prototype.reopen = function (generation) {
    if (this.timer) return;
    const attempt = this.attempt;
    this.attempt += 1;
    this.timer = this.later(() => {
      this.timer = 0;
      if (generation === this.generation) this.listen();
    }, REOPEN_MS[Math.min(attempt, REOPEN_MS.length - 1)]);
    if (this.timer && this.timer.unref) this.timer.unref();
  };

  // A hidden page lets its stream go (an HTTP/1.1 origin has few
  // connections, the gateway a per-peer allowance) and opens it when shown.
  GatewayMusic.prototype.setActive = function (active) {
    const next = Boolean(active);
    if (next === this.active) return;
    this.active = next;
    this.listen();
  };

  GatewayMusic.prototype.stop = function () {
    this.close();
  };

  // The sample music is music-backend-mock.js's, which no phone loads: a page
  // loads it when asked for the sample (navigation.js), the terminal with
  // --mock. It is made here, of this file's parts, when first asked for.
  let Mock = null;
  function mockMusic() {
    if (Mock) return Mock;
    const make = global.ThermiteMocks && global.ThermiteMocks.music;
    if (!make) throw new Error("The sample music is not loaded. Open the page with ?backend=mock, which loads music-backend-mock.js.");
    Mock = make({ problem, listeners });
    return Mock;
  }

  global.MusicBackend = {
    REOPEN_MS: REOPEN_MS,
    SLOW_MS: SLOW_MS,
    GatewayMusic: GatewayMusic,
    get MockMusic() { return mockMusic(); },
    create: function (settings) {
      const config = settings || {};
      const access = config.access || (global.ThermiteAccess && global.ThermiteAccess.client) || null;
      const hosted = global.ThermiteNavigation ? global.ThermiteNavigation.hosted() : false;
      const kind = config.backend || (hosted && !(access && access.mock) ? "gateway" : "mock");
      return kind === "gateway" ? new GatewayMusic(config) : new (mockMusic())(config);
    }
  };
})(globalThis);
