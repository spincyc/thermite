/*
 * Shared EventSource (SSE) lifecycle helper for thermite clients:
 * generation-fenced open/close, exponential reconnect backoff on terminal
 * closes (readyState === 2), optional visibility-change pause/resume, and
 * optional liveness watchdog.
 */
(function (global) {
  "use strict";

  const REOPEN_MS = [1000, 2000, 5000, 15000, 30000];

  function create(options) {
    const cfg = options || {};
    const urlOf = typeof cfg.url === "function" ? cfg.url : () => String(cfg.url || "");
    const getCtor = typeof cfg.EventSource === "function"
      ? () => cfg.EventSource
      : () => (typeof global.EventSource === "function" ? global.EventSource : null);
    const backoff = Array.isArray(cfg.backoff) && cfg.backoff.length ? cfg.backoff : REOPEN_MS;
    const livenessMs = Number(cfg.livenessMs) > 0 ? Number(cfg.livenessMs) : 0;
    const setT = cfg.setTimeout || ((fn, ms) => global.setTimeout(fn, ms));
    const clearT = cfg.clearTimeout || ((id) => global.clearTimeout(id));
    const doc = cfg.watchVisibility ? (cfg.document || global.document || null) : null;

    let currentSource = null;
    let generation = 0;
    let attempt = 0;
    let reopenTimer = 0;
    let livenessTimer = 0;
    let wanted = false;
    let active = cfg.active !== false;
    let stopped = false;

    function clearTimer(handle) {
      if (handle) clearT(handle);
      return 0;
    }

    function armLiveness(gen) {
      livenessTimer = clearTimer(livenessTimer);
      if (!livenessMs || stopped || !wanted || !active) return;
      livenessTimer = setT(() => {
        livenessTimer = 0;
        if (gen !== generation || stopped || !wanted || !active) return;
        if (cfg.onStale) cfg.onStale();
        reopen(gen, 0);
      }, livenessMs);
      if (livenessTimer && livenessTimer.unref) livenessTimer.unref();
    }

    function teardown() {
      reopenTimer = clearTimer(reopenTimer);
      livenessTimer = clearTimer(livenessTimer);
      if (currentSource) {
        try { currentSource.close(); } catch (_) { /* ignore */ }
      }
      currentSource = null;
    }

    function reopen(gen, explicitMs) {
      if (reopenTimer || gen !== generation || stopped || !wanted || !active) return;
      const delay = explicitMs !== undefined
        ? Math.max(0, Number(explicitMs) || 0)
        : backoff[Math.min(attempt, backoff.length - 1)];
      if (explicitMs === undefined) attempt += 1;
      reopenTimer = setT(() => {
        reopenTimer = 0;
        if (gen === generation && wanted && active && !stopped) connect();
      }, delay);
      if (reopenTimer && reopenTimer.unref) reopenTimer.unref();
    }

    function connect() {
      teardown();
      const gen = ++generation;
      const Ctor = getCtor();
      if (!wanted || !active || stopped || !Ctor) {
        if (cfg.onIdle) cfg.onIdle(gen);
        return null;
      }
      const source = currentSource = new Ctor(urlOf());
      if (cfg.onSource) cfg.onSource(source, gen);
      const isCurrent = () => gen === generation && !stopped;
      armLiveness(gen);
      source.onopen = (event) => {
        if (!isCurrent()) return;
        attempt = 0;
        armLiveness(gen);
        if (cfg.onOpen) cfg.onOpen(event, gen, source);
      };
      source.onmessage = (event) => {
        if (!isCurrent()) return;
        armLiveness(gen);
        if (cfg.onMessage) cfg.onMessage(event, gen, source);
      };
      source.onerror = (event) => {
        if (!isCurrent()) return;
        if (cfg.onError) cfg.onError(event, gen, source);
        if (source.readyState === 2) reopen(gen);
      };
      return source;
    }

    function open() {
      if (stopped) return null;
      wanted = true;
      return connect();
    }

    function close() {
      wanted = false;
      generation += 1;
      teardown();
    }

    function setActive(next) {
      const value = Boolean(next);
      if (value === active || stopped) return;
      active = value;
      if (wanted) connect();
    }

    function onVisibility() {
      if (!doc) return;
      setActive(doc.visibilityState !== "hidden");
    }

    if (doc && typeof doc.addEventListener === "function") {
      doc.addEventListener("visibilitychange", onVisibility);
    }

    function stop() {
      stopped = true;
      wanted = false;
      generation += 1;
      teardown();
      if (doc && typeof doc.removeEventListener === "function") {
        doc.removeEventListener("visibilitychange", onVisibility);
      }
    }

    function touch() {
      if (!stopped && wanted && active && currentSource) armLiveness(generation);
    }

    function resetBackoff() {
      attempt = 0;
    }

    return {
      open,
      close,
      reconnect: (delayMs) => reopen(generation, delayMs),
      setActive,
      stop,
      touch,
      resetBackoff,
      source: () => currentSource,
      generation: () => generation,
      isCurrent: (gen) => gen === generation && !stopped
    };
  }

  const api = { create, REOPEN_MS };
  global.ThermiteSse = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
