/* Bounded System metadata shared with the terminal. No device commands or polling. */
(function (global) {
  "use strict";
  const IDS = new Set(["openhab", "grafana", "prometheus", "alertmanager", "ntfy"]);
  const unavailable = "System information is unavailable.";
  const MAX_BYTES = 32768, TIMEOUT_MS = 8000;
  const text = (value, size) => typeof value === "string" && value.length <= size && !/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/.test(value);
  const loopback = host => host === "localhost" || host === "[::1]" || /^127\./.test(host);
  function endpoint(tool, base) {
    let target;
    if (typeof tool.url === "string") {
      if (!tool.url || /[\s\\]/.test(tool.url) || tool.url.startsWith("//")) throw new Error(unavailable);
      target = new URL(tool.url, base);
      if (!/^https?:\/\//.test(tool.url) && !tool.url.startsWith("/")) throw new Error(unavailable);
    } else {
      if (!["http", "https"].includes(tool.scheme) || !Number.isInteger(tool.port) || tool.port < 1 || tool.port > 65535
          || typeof tool.path !== "string" || !tool.path.startsWith("/") || tool.path.startsWith("//")
          || /[\s\\?#]/.test(tool.path)) throw new Error(unavailable);
      target = new URL(base);
      if (tool.localOnly) target.hostname = "127.0.0.1";
      target.protocol = tool.scheme + ":"; target.port = String(tool.port); target.pathname = tool.path;
    }
    if (!["http:", "https:"].includes(target.protocol) || target.username || target.password || target.search || target.hash
        || tool.localOnly && !loopback(target.hostname)
        || !tool.localOnly && loopback(target.hostname) && (typeof tool.url === "string" || target.hostname !== base.hostname)) throw new Error(unavailable);
    return target;
  }
  function catalogue(raw, origin) {
    const base = new URL(origin);
    if (!raw || raw.version !== 1 || !raw.runtime || !text(raw.runtime.version, 80) || !raw.runtime.version
        || !Array.isArray(raw.tools) || raw.tools.length > IDS.size) throw new Error(unavailable);
    const seen = new Set(), tools = raw.tools.map(tool => {
      if (!tool || !IDS.has(tool.id) || seen.has(tool.id) || !text(tool.label, 80) || !tool.label
          || !text(tool.description, 240) || typeof tool.localOnly !== "boolean") throw new Error(unavailable);
      seen.add(tool.id);
      const target = endpoint(tool, base);
      const port = Number(target.port || (target.protocol === "https:" ? 443 : 80));
      const host = base.hostname;
      const forwarding = tool.localOnly && /^[a-z0-9][a-z0-9.:-]*$/i.test(host)
        ? "ssh -N -L " + port + ":127.0.0.1:" + port + " " + host : null;
      return { id: tool.id, label: tool.label, description: tool.description, url: target.href,
        localOnly: tool.localOnly, forwarding };
    });
    return { version: 1, runtime: { version: raw.runtime.version }, tools };
  }
  function canView(access) { return Boolean(access && (access.native || access.canEdit())); }
  async function discard(response) {
    try { await response.body?.cancel(); } catch (_) {}
  }
  async function read(response) {
    if (!response.ok || Number(response.headers?.get("Content-Length")) > MAX_BYTES
        || typeof response.body?.getReader !== "function") {
      await discard(response);
      throw new Error(unavailable);
    }
    const reader = response.body.getReader(), chunks = []; let size = 0;
    try {
      for (;;) {
        const part = await reader.read(); if (part.done) break;
        size += part.value.byteLength;
        if (size > MAX_BYTES) throw new Error(unavailable);
        chunks.push(part.value);
      }
    } catch (error) { await reader.cancel().catch(() => {}); throw error; }
    finally { reader.releaseLock(); }
    const body = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  }
  function create(options = {}) {
    const access = options.access, base = options.base || global.location?.origin || "http://localhost";
    const request = options.fetch || global.fetch, changed = options.changed || (() => {});
    const later = options.setTimeout || global.setTimeout, cancel = options.clearTimeout || global.clearTimeout;
    const state = { loading: false, loaded: false, error: "", manifest: null };
    let generation = 0, active = null;
    function close() {
      generation++; if (active) { active.controller?.abort(); active.cancel(); } active = null;
      state.loading = false; state.loaded = false; state.manifest = null; state.error = ""; changed();
    }
    async function open() {
      close(); const turn = generation;
      state.loading = true; changed(); let timeout;
      try {
        const Abort = options.AbortController || global.AbortController;
        const controller = Abort ? new Abort() : null;
        const cancelled = new Promise((_, reject) => {
          active = { controller, cancel: () => reject(new Error(unavailable)) };
          timeout = later(() => { controller?.abort(); reject(new Error(unavailable)); }, TIMEOUT_MS);
        });
        if (access) await Promise.race([access.ready(), cancelled]);
        if (turn !== generation) return;
        if (!canView(access)) { state.error = "Only administrators can view System."; return; }
        if (options.manifest || options.mock) {
          state.manifest = catalogue(options.manifest || { version: 1, runtime: { version: "Preview" }, tools: [] }, base);
          state.loaded = true; return;
        }
        const loading = (async () => {
          const response = await request(new URL("/static/remote/system.json", base).href,
            { method: "GET", cache: "no-store", headers: { Accept: "application/json" }, signal: controller?.signal });
          if (turn !== generation || controller?.signal.aborted) { await discard(response); throw new Error(unavailable); }
          return catalogue(await read(response), base);
        })();
        const manifest = await Promise.race([loading, cancelled]);
        if (turn !== generation) return;
        state.manifest = manifest; state.loaded = true;
      } catch (_) { if (turn === generation) state.error = unavailable; }
      finally { if (timeout != null) cancel(timeout); if (turn === generation) { state.loading = false; active = null; changed(); } }
    }
    access?.subscribe?.(event => { if (event.type === "unauthorized") close(); });
    return { state, open, close, canView: () => canView(access) };
  }
  const api = { catalogue, canView, create, MAX_BYTES, TIMEOUT_MS };
  global.ThermiteSystem = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || document.body?.dataset.thermitePage !== "system") return;
  const status = document.querySelector("[data-system-status]"), version = document.querySelector("[data-system-version]");
  const tools = document.querySelector("[data-system-tools]"), advanced = document.querySelector("[data-system-advanced]");
  const access = global.ThermiteAccess?.client;
  const client = create({ access, mock: access?.mock, base: location.origin,
    fetch: access && !access.native && !access.mock ? access.fetch : global.fetch.bind(global), changed: render });
  function node(tag, value) { const item = document.createElement(tag); if (value != null) item.textContent = value; return item; }
  function render() {
    const state = client.state;
    status.textContent = state.loading ? "Reading system information…" : state.error || "Configured system information; this is not a live health check.";
    version.textContent = state.manifest ? state.manifest.runtime.version : "Not available";
    tools.replaceChildren(); advanced.hidden = !state.loaded;
    for (const tool of state.manifest?.tools || []) {
      const row = node("section"), title = node("h3", tool.label); row.append(title, node("p", tool.description));
      if (tool.localOnly) {
        row.append(node("p", "On the Thermite host. Open there, or forward its port over SSH:"), node("code", tool.url));
        if (tool.forwarding) row.append(node("p", tool.forwarding));
      } else {
        const link = node("a", "Open " + tool.label); link.href = tool.url; link.rel = "noopener noreferrer";
        row.append(link);
      }
      tools.append(row);
    }
  }
  document.querySelector("[data-system-refresh]").addEventListener("click", () => client.open());
  const pause = () => { advanced.open = false; client.close(); status.textContent = "System information paused while this page is hidden."; };
  const resume = () => { if (document.visibilityState !== "hidden" && !client.state.loading && !client.state.loaded) client.open(); };
  global.addEventListener("pagehide", pause);
  global.addEventListener("pageshow", resume);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") pause(); else resume(); });
  resume();
})(globalThis);
