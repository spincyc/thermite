/*
 * Shared optimistic hold windows and hold-to-repeat helper for web and
 * terminal remote sessions.
 */
(function (global) {
  "use strict";

  const DEFAULT_HOLD_MS = 1200;
  const DEFAULT_REPEAT_DELAY = 400;
  const DEFAULT_REPEAT_INTERVAL = 110;

  // Map-backed optimistic value hold: keeps a sent value for `ms` (with an
  // automatic expiry callback) while waiting for device read-back.
  function createHold(options) {
    const cfg = options || {};
    const now = cfg.now || (() => Date.now());
    const setT = cfg.setTimeout || ((fn, ms) => global.setTimeout(fn, ms));
    const clearT = cfg.clearTimeout || ((id) => global.clearTimeout(id));
    const defaultMs = cfg.duration !== undefined ? cfg.duration : DEFAULT_HOLD_MS;
    const onExpire = typeof cfg.onExpire === "function" ? cfg.onExpire : null;
    const entries = new Map();

    function keyOf(key) {
      return key === undefined ? "_" : String(key);
    }

    function clear(key) {
      if (key === undefined) {
        entries.forEach((entry) => { if (entry.timer !== null) clearT(entry.timer); });
        entries.clear();
        return;
      }
      const k = keyOf(key);
      const entry = entries.get(k);
      if (!entry) return;
      if (entry.timer !== null) clearT(entry.timer);
      entries.delete(k);
    }

    function set(key, value, ms) {
      const k = keyOf(key);
      clear(k);
      const wait = ms !== undefined ? Math.max(0, Number(ms) || 0) : defaultMs;
      const start = now();
      const entry = { value, at: start, until: start + wait, timer: null };
      entries.set(k, entry);
      if (wait > 0 || onExpire) {
        entry.timer = setT(() => {
          if (entries.get(k) !== entry) return;
          entries.delete(k);
          if (onExpire) onExpire(k, value);
        }, wait);
      }
      return entry;
    }

    function get(key) {
      const k = keyOf(key);
      const entry = entries.get(k);
      if (!entry) return null;
      if (now() >= entry.until) {
        clear(k);
        return null;
      }
      return entry;
    }

    function has(key) {
      return get(key) !== null;
    }

    function shown(key, live) {
      const entry = get(key);
      return entry !== null ? entry.value : live;
    }

    function match(key, live) {
      const entry = get(key);
      if (entry !== null && entry.value === live) {
        clear(key);
        return true;
      }
      return false;
    }

    function stop() {
      clear();
    }

    return { set, get, has, shown, match, clear, stop };
  }

  // Hold-to-repeat controller: fires once immediately, then repeats after
  // `delay` every `interval` ms; stops automatically on blur, tab hide, or
  // when `isValid(context)` returns false (e.g. disconnected/disabled button).
  function createRepeater(options) {
    const cfg = options || {};
    const delay = cfg.delay !== undefined ? cfg.delay : DEFAULT_REPEAT_DELAY;
    const interval = cfg.interval !== undefined ? cfg.interval : DEFAULT_REPEAT_INTERVAL;
    const setT = cfg.setTimeout || ((fn, ms) => global.setTimeout(fn, ms));
    const clearT = cfg.clearTimeout || ((id) => global.clearTimeout(id));
    const onFire = cfg.onFire || (() => {});
    const onStop = cfg.onStop || null;
    const isValid = cfg.isValid || (() => true);
    const win = cfg.window !== undefined ? cfg.window : (typeof global.addEventListener === "function" ? global : null);
    const doc = cfg.document !== undefined ? cfg.document : (global.document || null);
    let current = null;
    let disposed = false;

    function stop() {
      const prev = current;
      if (prev) {
        clearT(prev.timer);
        current = null;
      }
      if (onStop) onStop(prev ? prev.context : null);
    }

    function onVisibility() {
      if (doc && doc.visibilityState === "hidden") stop();
    }

    if (win && typeof win.addEventListener === "function") win.addEventListener("blur", stop);
    if (doc && typeof doc.addEventListener === "function") doc.addEventListener("visibilitychange", onVisibility);

    function start(context) {
      if (disposed) return;
      stop();
      if (!isValid(context)) return;
      const run = { context, timer: null };
      current = run;
      onFire(context);
      if (current !== run) return;
      run.timer = setT(function tick() {
        if (current !== run || !isValid(run.context)) {
          stop();
          return;
        }
        onFire(run.context);
        if (current === run) run.timer = setT(tick, interval);
      }, delay);
    }

    function active() {
      return current !== null;
    }

    function dispose() {
      disposed = true;
      stop();
      if (win && typeof win.removeEventListener === "function") win.removeEventListener("blur", stop);
      if (doc && typeof doc.removeEventListener === "function") doc.removeEventListener("visibilitychange", onVisibility);
    }

    return { start, stop, active, dispose };
  }

  const api = { createHold, createRepeater, DEFAULT_HOLD_MS, DEFAULT_REPEAT_DELAY, DEFAULT_REPEAT_INTERVAL };
  global.ThermiteHold = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
