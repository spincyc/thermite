/* Same-origin identity client shared by the browser and terminal. No credential storage. */
(function (global) {
  "use strict";
  const MESSAGES = {
    login_required: "Sign in to thermite.", session_expired: "Your session has ended. Sign in again.",
    invalid_token: "Your terminal sign-in has ended. Run thermite access login again.",
    invalid_login: "The username or password was not accepted.", login_throttled: "Too many sign-in attempts. Try again later.",
    forbidden: "Your account does not allow this action.", room_locked: "This room is locked.",
    policy_locked: "This room is locked.", policy_unavailable: "Room lock status is unavailable. Try again in a moment.",
    device_locked: "A locked room is using this device or route, so it cannot change now.",
    room_state_unavailable: "Wait until the room reports a current source or Off before locking it.",
    room_state_changed: "The room changed before it could be locked. Check what it shows and try again.",
    runtime_unavailable: "The room could not be read. Try again when it is connected.",
    revision_conflict: "The room policy changed. Review its current state and try again.", last_admin: "Keep at least one active administrator.",
    invalid_password: "Choose a stronger password.", invalid_policy: "Check the schedule times, days and sources.",
    invalid_user: "Check the username and password.",
    csrf_failed: "The sign-in form expired. Reload and try again.", access_unavailable: "Access service unavailable.",
    auth_disabled: "Enable authentication in Security to use accounts, pairing, locks and schedules.",
    authentication_disabled: "Enable authentication in Security to use accounts, pairing, locks and schedules.",
    security_unavailable: "Authentication settings are unavailable. Try again when the access service is ready.",
    authorization_changed: "Your account permissions changed. Sign in again.",
    invalid_security: "Check the authentication settings and try again.",
    setup_required: "Generate a setup code on the thermite host before creating the first administrator.",
    invalid_setup_code: "The setup code was not accepted or has expired. Generate a new code on the thermite host.",
    access_not_configured: "Access is not configured at this address.", invalid_response: "The access service could not answer.",
    client_timeout: "thermite did not answer in time. Try again in a moment.",
    command_unconfirmed: "thermite did not answer in time. The command may still take effect; check before trying again.",
    origin_mismatch: "The terminal credential belongs to a different address.", secure_connection_required: "Use your site’s HTTPS address to sign in.", connection_failed: "Could not reach thermite."
  };
  // The gateway sends a stream frame at least this often: a heartbeat,
  // {"type":"heartbeat"}, while it has nothing else (oh_identity.streams.HEARTBEAT).
  const STREAM_HEARTBEAT_MS = 5000;
  const HEARTBEAT_FRAME = '{"type":"heartbeat"}';
  // How long a command (a room command, a lock switch) may go unanswered
  // before it is aborted and reported as unconfirmed. The gateway answers one
  // in well under a second, and its own call to openHAB gives up after 5 s
  // at each step (oh_identity.transport); 15 s is long past a healthy answer
  // and about as long as a choice waits to be read back (backend.js
  // confirmMs). All off, up to two waves of rooms on the gateway's four
  // command workers (oh_identity.batches), has twice as long.
  const COMMAND_DEADLINE_MS = 15000;
  function problem(code, status) {
    const error = new Error(MESSAGES[code] || "The action could not be completed.");
    error.code = Object.prototype.hasOwnProperty.call(MESSAGES, code) ? code : "access_unavailable";
    error.status = status || 0;
    return error;
  }
  function returnTo(raw, origin) {
    const navigation = global.ThermiteNavigation || (typeof require === "function" ? require("./navigation") : null);
    const fallback = "/";
    let target; try { target = new URL(raw || fallback, origin + "/static/remote/"); } catch (_) { return fallback; }
    const path = navigation?.canonical(target.pathname);
    if (target.origin !== origin || target.username || target.password
        || !path || path === "/account/") return fallback;
    const safe = new URLSearchParams();
    for (const key of ["room", "source"]) { const value = target.searchParams.get(key); if (value && /^[a-z][a-z0-9_]{0,99}$/.test(value)) safe.set(key, value); }
    const theme = target.searchParams.get("theme"); if (["dark", "light"].includes(theme)) safe.set("theme", theme);
    return path + (safe.size ? "?" + safe.toString() : "");
  }
  // Everyone controls unlocked rooms; accounts add camera media; administrators manage the site.
  const PERMISSIONS = ["site.read", "room.control", "camera.view", "settings.manage", "users.manage", "pairing.manage"];
  // An https: origin and nothing more, or null.
  function secureOrigin(value) {
    let url; try { url = new URL(value); } catch (_) { return null; }
    return url.protocol === "https:" && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash ? url.origin : null;
  }
  function principal(value) {
    if (!value || !Number.isInteger(value.id) || typeof value.username !== "string" || value.username.length > 150
        || /[\x00-\x1f\x7f-\x9f]/.test(value.username) || !Array.isArray(value.permissions)) throw problem("invalid_response");
    return { id: value.id, username: value.username, admin: value.admin === true,
      can_edit: value.can_edit === true, is_guest: value.is_guest === true, auth_enabled: value.auth_enabled !== false, site: typeof value.site === "string" ? value.site : null,
      permissions: value.permissions.filter((permission) => PERMISSIONS.includes(permission)) };
  }
  // This client's label for the gateway's command trail (X-Thermite-Client):
  // web/<tab id>, kept for the tab's session, or the terminal's own
  // tui/<host>. The gateway logs it as a claim, never as authority.
  const CLIENT = /^(web|tui)\/[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
  function clientLabel(value) {
    if (typeof value === "string" && CLIENT.test(value)) return value;
    let id = null;
    try { id = global.sessionStorage.getItem("thermite.tab"); } catch (_) { /* no storage: one ID per page */ }
    if (typeof id !== "string" || !/^[0-9a-f]{8}$/.test(id)) {
      const bytes = new Uint8Array(4);
      if (global.crypto?.getRandomValues) global.crypto.getRandomValues(bytes);
      else bytes.forEach((_, index) => { bytes[index] = Math.floor(Math.random() * 256); });
      id = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
      try { global.sessionStorage.setItem("thermite.tab", id); } catch (_) { /* no storage */ }
    }
    return "web/" + id;
  }
  function create(options) {
    const config = options || {}, rawFetch = config.fetch || global.fetch.bind(global);
    const label = clientLabel(config.client);
    const base = (config.base || "").replace(/\/$/, ""), origin = new URL(base || global.location.href).origin;
    const legacy = config.native === true, mock = config.mock === true;
    // The plain-HTTP Guest listener: a Guest only, signing in on the HTTPS site.
    const guestOnly = !legacy && !mock && config.guest === true, secure = guestOnly ? secureOrigin(config.secure) : null;
    let token = config.token || null;  // lint: allow — credential supplied by the caller
    const listeners = [], policies = Object.create(null), policyReads = Object.create(null), policyWrites = new Set(), policyFailures = new Set();
    const policyAt = Object.create(null), POLICY_EVERY = config.policyInterval || 5000, POLICY_GRACE = config.policyGrace || 15000;
    // What a room's lock display depends on; evaluated_at changes on every read.
    const policySignature = (value) => JSON.stringify([value.revision, value.manual_lock, value.can_lock, value.can_edit,
      { ...value.effective, evaluated_at: null }, value.enforcement]);
    let security = null, discovering = null, csrfCookie = null, probing = null;
    let user = null, csrf = null, loading = null, ended = false, policyRoom = null, authRevision = 0;
    let active = typeof document === "undefined" || document.visibilityState !== "hidden";
    // The open event streams, each with its liveness watchdog (eventSource).
    const streams = new Set(), streamLiveness = config.streamLiveness || 3 * STREAM_HEARTBEAT_MS;
    const commandDeadline = config.commandDeadline || COMMAND_DEADLINE_MS;
    // Every room's lock in one read, for the room lists. The watched room's
    // detailed policy (policies) still decides whether its controls are held back.
    let lockRooms = null, locksAt = 0, locksWatched = false, lockReads = 0;
    const policyPoll = poller(() => client.loadPolicy(policyRoom), () => Boolean(policyRoom));
    const lockPoll = poller(() => client.loadLocks(), () => locksWatched);
    // Reads now and again POLICY_EVERY after each answer, while the client is
    // active and wanted() holds; start() replaces any earlier loop.
    function poller(read, wanted) {
      let timer = null, generation = 0;
      const stop = () => { clearTimeout(timer); generation++; };
      return {
        stop,
        start() {
          stop(); const own = generation;
          const live = () => own === generation && active && !ended && wanted();
          if (legacy || mock || !client.authEnabled() || !live()) return Promise.resolve(null);
          const refresh = () => live() ? read().catch(() => null).finally(() => {
            if (live()) { timer = setTimeout(refresh, POLICY_EVERY); if (timer.unref) timer.unref(); }
          }) : undefined;
          return refresh();
        }
      };
    }
    // Client telemetry: what this client gave up on (timeouts, failed,
    // refused or silent event streams), which the gateway never sees. Counted
    // here (1000 a kind, the latest 16 waits in seconds) and posted to
    // /api/v1/telemetry after the next answered request, at most every 15 s
    // and one at a time, or as a beacon when the page hides. 204 clears what
    // was sent, other 4xx drop it, 408/429/5xx and network failures keep it
    // for later. A telemetry POST's own failure is never recorded.
    const TELEMETRY = "/api/v1/telemetry", KINDS = ["policy_timeout", "locks_timeout", "command_timeout", "stream_error", "stream_refused", "snapshot_timeout"];
    // The gateway counts reports by kind (oh_identity.telemetry): web or tui, never a
    // client's own label (tui/<host>, which names it in the command trail).
    const telemetryClient = config.client ? String(config.client).split("/")[0] : typeof document === "undefined" ? "tui" : "web",
      now = config.now || Date.now;
    let telemetry = Object.create(null), hopProtocol = "unknown", reporting = false, reportAfter = 0, throttledUntil = 0;
    const normalHop = (value) => { const hop = String(value || "").toLowerCase();
      return ["h2", "h2c"].includes(hop) ? "h2" : /^http\/1\.[01]$/.test(hop) ? "http/1.1" : /^h3(-|$)/.test(hop) ? "h3" : "unknown"; };
    // The request's own PerformanceResourceTiming, else the latest same-origin one.
    function nextHop(url) {
      const latest = (entries) => entries.filter((entry) => entry.nextHopProtocol).pop()?.nextHopProtocol;
      try {
        const timing = global.performance;
        return normalHop(url && latest(timing.getEntriesByName(new URL(url, origin + "/").href))
          || latest(timing.getEntriesByType("resource").filter((entry) => String(entry.name).startsWith(origin + "/"))));
      } catch (_) { return "unknown"; }
    }
    // seconds: from starting the request (queueing included) until giving up; omitted when meaningless.
    function record(kind, seconds, url, protocol) {
      if (legacy || mock || !KINDS.includes(kind)) return;
      const entry = telemetry[kind] || (telemetry[kind] = { count: 0, waits: [] });
      entry.count = Math.min(1000, entry.count + 1);
      if (Number.isFinite(seconds)) entry.waits = entry.waits.concat(Math.round(Math.min(600, Math.max(0, seconds)) * 1000) / 1000).slice(-16);
      hopProtocol = protocol ? normalHop(protocol) : nextHop(url);
    }
    // Counts kind once if promise is still unsettled after ms; never cancels it.
    function overdue(kind, path, promise, ms) {
      const started = now(), timer = setTimeout(() => record(kind, (now() - started) / 1000, path), ms);
      if (timer.unref) timer.unref();
      return promise.finally(() => clearTimeout(timer));
    }
    function restore(events) {
      for (const [kind, sent] of Object.entries(events)) {
        const entry = telemetry[kind] || { count: 0, waits: [] };
        telemetry[kind] = { count: Math.min(1000, sent.count + entry.count), waits: sent.waits.concat(entry.waits).slice(-16) };
      }
    }
    async function flushTelemetry(leaving) {
      if (legacy || mock || !Object.keys(telemetry).length || now() < throttledUntil || !leaving && (reporting || now() < reportAfter)) return false;
      const events = telemetry, report = { client: telemetryClient, protocol: hopProtocol, events };
      telemetry = Object.create(null);
      // ASCII only, and about 1.1 KB at most (six kinds of 1000 with 16 waits
      // of 600 s), inside the gateway's 2048 bytes; the tests hold it there.
      const body = JSON.stringify(report);
      if (leaving) {
        try { if (global.navigator.sendBeacon(new URL(TELEMETRY, origin).href, new Blob([body], { type: "application/json" })) === true) return true; } catch (_) { /* fetch below */ }
      } else { reporting = true; reportAfter = now() + 15000; }
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timer = setTimeout(() => controller?.abort(), 10000);
      if (timer.unref) timer.unref();
      try {
        const response = await raw(TELEMETRY, { method: "POST", keepalive: leaving === true, headers: { "Content-Type": "application/json" }, body,
          ...(controller ? { signal: controller.signal } : {}) }).catch(() => null);
        if (response && response.status < 400) return true;
        if (response && response.status < 500 && ![408, 429].includes(response.status)) return false;
        restore(events);
        if (response?.status === 429) {
          const wait = Number(response.headers?.get?.("Retry-After"));
          throttledUntil = now() + (Number.isFinite(wait) ? Math.min(3600, Math.max(15, wait)) : 60) * 1000;
        }
        return false;
      } finally { clearTimeout(timer); if (!leaving) reporting = false; }
    }
    const demo = { users: [{ id: 1, username: "Demo", admin: true, active: true }], policies: {} };
    function demoRequest(path, method, value) {
      const policy = /^\/api\/v1\/rooms\/([^/]+)\/policy$/.exec(path);
      if (policy) {
        const id = policy[1], old = demo.policies[id] || { room: id, revision: 0, config: null, can_edit: true, effective: { allowed: true, locked: false } };
        if (method === "PUT") { if (value.revision !== old.revision) throw problem("revision_conflict", 409); demo.policies[id] = { ...old, revision: old.revision + 1, config: { ...value.config, room: id, timezone: "UTC", revision: old.revision + 1 } }; }
        return Promise.resolve(demo.policies[id] || old);
      }
      const match = /^\/api\/v1\/users(?:\/(\d+))?$/.exec(path);
      if (match) {
        const list = demo.users, id = Number(match[1]);
        if (method === "GET") return Promise.resolve({ users: JSON.parse(JSON.stringify(list)) });
        const clean = { ...value }; delete clean.password;
        if (method === "POST") { const item = { admin: false, active: true, ...clean, id: Math.max(0, ...list.map((entry) => entry.id)) + 1 }; list.push(item); return Promise.resolve(item); }
        const item = list.find((entry) => entry.id === id);
        if (!item) throw problem("invalid_response", 404);
        if (method === "DELETE") item.active = false; else Object.assign(item, clean);
        return Promise.resolve(item);
      }
      if (path === "/api/v1/logout") return Promise.resolve({ ok: true });
      throw problem("access_not_configured", 404);
    }
    const notify = (type, extra) => listeners.slice().forEach((fn) => fn({ type, user, security, ...extra }));
    function lost(code) {
      const guest = Boolean(user && user.is_guest);
      user = null; csrf = null; ended = true; authRevision++; policyPoll.stop(); lockPoll.stop();
      Object.keys(policies).forEach((id) => delete policies[id]); policyFailures.clear(); lockRooms = null;
      notify("unauthorized", { guest });
      if (config.onUnauthorized) config.onUnauthorized(code);
    }
    async function raw(path, init, publicRequest) {
      const target = new URL(path, origin + "/");
      if (target.origin !== origin || target.username || target.password) throw problem("origin_mismatch");
      const request = { ...(init || {}), credentials: token && !publicRequest ? "omit" : "same-origin", redirect: "error", cache: "no-store" };
      const headers = new Headers(request.headers || {});
      headers.set("X-Thermite-Client", label);
      if (token && !publicRequest) headers.set("Authorization", "Bearer " + token);
      const bearerRequest = token && !publicRequest;
      if (!bearerRequest && !["GET", "HEAD"].includes((request.method || "GET").toUpperCase()) && csrf) headers.set("X-CSRFToken", csrf);
      // Node fetch has no cookie jar. Retain only the gateway's CSRF cookie,
      // in memory, for anonymous same-origin controls; never browser sessions.
      if (!global.location) {
        headers.delete("Cookie");
        if (!bearerRequest && csrfCookie) headers.set("Cookie", csrfCookie);
        if (!bearerRequest && !["GET", "HEAD"].includes((request.method || "GET").toUpperCase())) headers.set("Referer", origin + "/");
      }
      request.headers = headers;
      let response;
      try { response = await rawFetch(target.href, request); } catch (_) { throw problem("connection_failed"); }
      if (response.ok && target.pathname !== TELEMETRY) flushTelemetry().catch(() => {});
      if (!global.location && ["/api/v1/security", "/api/v1/session"].includes(target.pathname)) {
        const cookies = response.headers?.getSetCookie?.() || [];
        for (const cookie of cookies.slice(0, 8)) {
          if (typeof cookie !== "string" || cookie.length > 1024) continue;
          const match = /^(thermite_[a-f0-9]{16}_csrf=[A-Za-z0-9]{32}(?:[A-Za-z0-9]{32})?)(?:;|$)/.exec(cookie);
          if (match) csrfCookie = match[1];
        }
      }
      return response;
    }
    function stale(revision) {
      if (revision === authRevision) return;
      const error = problem("session_expired", 401); error.stale = true; throw error;
    }
    // A Guest asking for an account-only resource has lost nothing; any other
    // 401 means the credential or the authentication switch changed.
    const guestRefusal = (value) => Boolean(user && user.is_guest && value && value.error === "login_required");
    async function data(path, init, check, publicRequest) {
      const revision = authRevision;
      const response = await raw(path, init, publicRequest);
      if (check) stale(revision);
      let value = null;
      try { value = await response.json(); } catch (_) { /* reported below */ }
      if (check) stale(revision);
      if (check && response.status === 401 && !guestRefusal(value)) lost("session_expired");
      if (value === null) throw problem(response.status === 401 ? "session_expired" : "invalid_response", response.status);
      if (!response.ok) throw problem(response.status === 404 && ["/api/v1/session", "/api/v1/security"].includes(path) ? "access_not_configured" : value.error, response.status);
      return value;
    }
    function securityStatus(value) {
      if (!value || typeof value.enabled !== "boolean" || !Number.isInteger(value.revision) || value.revision < 0
          || typeof value.bootstrap_required !== "boolean" || typeof value.setup_available !== "boolean") throw problem("invalid_response");
      return { enabled: value.enabled, revision: value.revision, bootstrap_required: value.bootstrap_required,
        setup_available: value.setup_available };
    }
    async function loadSecurity() {
      if (legacy) return null;
      if (mock) return security || (security = { enabled: true, revision: 0, bootstrap_required: false, setup_available: false });
      if (!discovering) discovering = (async () => {
        const revision = authRevision, answer = await data("/api/v1/security", null, false, true); stale(revision);
        const next = securityStatus(answer), changed = security && security.revision !== next.revision;
        security = next;
        if (changed) lost("session_expired");
        if (!next.enabled) ended = false;
        csrf = answer.csrf || null; notify("security"); return security;
      })().finally(() => { discovering = null; });
      return discovering;
    }
    async function ready() {
      if (legacy) return null;
      if (mock) { await loadSecurity(); user = { id: 1, username: "Demo", admin: true, can_edit: true, is_guest: false, auth_enabled: true, permissions: PERMISSIONS.slice() }; return user; }
      if (ended) throw problem("login_required", 401);
      if (user) return user;
      if (!loading) loading = (async () => {
        const mode = security || await loadSecurity(), revision = authRevision;
        if (mode.enabled && !token && config.getToken) token = config.getToken();
        if (mode.enabled && !token && !guestOnly) {
          const answer = await data("/api/v1/session"); csrf = answer.csrf;
        }
        const answer = principal(await data("/api/v1/me", null, true)); stale(revision);
        if (answer.auth_enabled !== mode.enabled) { lost("session_expired"); throw problem("session_expired", 401); }
        user = answer; notify("change"); return user;
      })().catch((error) => { if (error.status === 401 && !ended && !error.stale) lost(error.code); throw error; })
        .finally(() => { loading = null; });
      return loading;
    }
    async function fetchAuth(url, init) {
      if (legacy || mock) return rawFetch(url, init);
      await ready();
      const target = new URL(url, config.base ? origin + "/" : global.document?.baseURI || origin + "/"), method = (init && init.method || "GET").toUpperCase();
      if (!client.has("pairing.manage") && (target.pathname === "/rest/items" && target.searchParams.get("metadata") === "pairing"
          || method === "POST" && target.pathname.startsWith("/rest/items/"))) throw problem("forbidden", 403);
      const revision = authRevision;
      const response = await raw(target.href, init);
      stale(revision);
      if (response.status === 401) {
        const body = user && user.is_guest && typeof response.clone === "function" ? await response.clone().json().catch(() => null) : null;
        stale(revision);
        if (guestRefusal(body)) throw problem("login_required", 401);
        lost("session_expired"); throw problem("session_expired", 401);
      }
      return response;
    }
    async function json(path, method, value, options) {
      await ready();
      if (legacy) throw problem("access_not_configured", 404);
      if (!client.authEnabled() && (/^\/api\/v1\/(users|tokens)(\/|$)/.test(path) || /\/(policy|lock)$/.test(path))) throw problem("auth_disabled", 403);
      if (mock) return (config.mockRequest || demoRequest)(path, method || "GET", value);
      const init = { ...(options || {}), method: method || "GET", headers: { Accept: "application/json" } };
      if (value !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(value); }
      return data(path, init, true);
    }
    // A JSON request that gives up after ms: its fetch is aborted, it rejects
    // with code and status 0 (no server answer), and kind, when given, is
    // counted. Nothing waits on it longer than ms, however the gateway fares.
    function bounded(path, method, value, ms, code, kind) {
      const controller = typeof AbortController === "function" ? new AbortController() : null, started = now();
      let timer;
      return Promise.race([
        json(path, method, value, controller ? { signal: controller.signal } : {}),
        new Promise((_, reject) => { timer = setTimeout(() => {
          controller?.abort(); if (kind) record(kind, (now() - started) / 1000, path); reject(problem(code));
        }, ms); })
      ]).finally(() => clearTimeout(timer));
    }
    // A lock read that gives up after policyTimeout, so one hung request cannot
    // stall its poll. Giving up is client_timeout, counted as kind; the caller
    // shows the lock as unavailable.
    const timedGet = (path, kind) => bounded(path, "GET", undefined, config.policyTimeout || 8000, "client_timeout", kind);
    // A command aborted after commandDeadline rejects as command_unconfirmed:
    // the gateway may still have sent it, so the caller says so, never that it failed.
    const commandRequest = (path, method, value, ms) => bounded(path, method, value, ms || commandDeadline, "command_unconfirmed");
    const client = {
      native: legacy, mock, guestOnly, base, origin, ready, fetch: fetchAuth, json, loadSecurity, record, flushTelemetry,
      // The HTTPS site's origin, which the Guest listener names; else null.
      secureSite: () => secure,
      security: () => security,
      authEnabled: () => mock || Boolean(security && security.enabled),
      current: () => user,
      subscribe(fn) { listeners.push(fn); return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); }; },
      has(permission) { return legacy || Boolean(user && user.permissions && user.permissions.includes(permission)); },
      canEdit() { return client.authEnabled() && Boolean(user && user.can_edit); },
      // Only a known lock holds a command back. While a room's lock is unknown
      // (not read yet, or unreadable past the grace) commands are sent: the
      // server checks each one under the room lock and refuses it if locked.
      canControl(room) {
        if (legacy || mock) return true;
        return !client.authEnabled() || policies[room]?.effective?.allowed !== false;
      },
      policy(room) { return client.authEnabled() ? policies[room] || null : null; },
      // "checking" until the first answer for a room; a failed read stays "unavailable" until one succeeds.
      // Neither is a lock: canControl holds nothing back for them.
      policyStatus(room) { return !client.authEnabled() ? "off" : policies[room] ? "ready" : policyFailures.has(room) ? "unavailable" : "checking"; },
      async loadPolicy(room) {
        const sequence = policyReads[room] = (policyReads[room] || 0) + 1, authority = authRevision;
        try {
          const value = await timedGet("/api/v1/rooms/" + encodeURIComponent(room) + "/policy", "policy_timeout");
          stale(authority);
          if (sequence !== policyReads[room] || policyWrites.has(room)) return policies[room] || null;
          if (value.room !== room || !Number.isInteger(value.revision) || typeof value.effective?.allowed !== "boolean"
              || typeof value.effective?.locked !== "boolean") throw problem("invalid_response");
          if (policies[room] && policies[room].revision > value.revision) return policies[room];
          const changed = !policies[room] || policySignature(policies[room]) !== policySignature(value);
          const recovered = policyFailures.delete(room);
          policies[room] = value; policyAt[room] = Date.now();
          if (changed || recovered) notify("policy");
          return value;
        } catch (error) {
          // One slow or failed read keeps the last answer briefly; the server
          // still checks every command against the current policy.
          if (sequence === policyReads[room] && !policyWrites.has(room) && !error.stale
              && (!policies[room] || Date.now() - (policyAt[room] || 0) > POLICY_GRACE)) {
            delete policies[room]; policyFailures.add(room); notify("policy");
          }
          throw error;
        }
      },
      // GET /api/v1/locks: {rooms: {id: {locked, manual, until, reason}}}.
      async loadLocks() {
        const sequence = ++lockReads, authority = authRevision;
        try {
          const value = await timedGet("/api/v1/locks", "locks_timeout");
          stale(authority);
          if (sequence !== lockReads) return lockRooms;
          const rooms = value?.rooms, next = Object.create(null);
          if (!rooms || typeof rooms !== "object" || Array.isArray(rooms)) throw problem("invalid_response");
          for (const [room, lock] of Object.entries(rooms)) {
            if (!/^[a-z][a-z0-9_]{0,99}$/.test(room) || typeof lock?.locked !== "boolean") throw problem("invalid_response");
            next[room] = { locked: lock.locked, manual: lock.manual === true, until: typeof lock.until === "string" ? lock.until : null,
              reason: typeof lock.reason === "string" ? lock.reason : "" };
          }
          const changed = JSON.stringify(next) !== JSON.stringify(lockRooms);
          lockRooms = next; locksAt = Date.now();
          if (changed) notify("locks");
          return lockRooms;
        } catch (error) {
          // As with a policy, one failed read keeps the last answer briefly.
          if (sequence === lockReads && !error.stale && lockRooms && Date.now() - locksAt > POLICY_GRACE) { lockRooms = null; notify("locks"); }
          throw error;
        }
      },
      // A room's lock for the room lists: its detailed policy when read since
      // the summary, else the summary; null while neither is known.
      lockSummary(room) {
        const policy = client.policy(room);
        if (policy && (policyAt[room] || 0) >= locksAt) {
          return { locked: policy.effective.locked === true, manual: Boolean(policy.manual_lock),
            until: policy.effective.until || null, reason: policy.effective.reason || "" };
        }
        return client.authEnabled() && lockRooms?.[room] || null;
      },
      async savePolicy(room, revision, value) {
        const saved = await json("/api/v1/rooms/" + encodeURIComponent(room) + "/policy", "PUT", { revision, config: value });
        policies[room] = { ...policies[room], ...saved }; notify("policy");
        await client.loadPolicy(room); return saved;
      },
      // desired, when given, is what the caller saw: the server locks only if the room still reads it.
      async setRoomLock(room, revision, locked, desired) {
        await ready();
        const authority = authRevision;
        if (!client.authEnabled()) throw problem("auth_disabled", 403);
        if (!client.canEdit() || policies[room]?.can_lock !== true) throw problem("forbidden", 403);
        if (policyWrites.has(room)) return null;
        policyWrites.add(room); policyReads[room] = (policyReads[room] || 0) + 1;
        try {
          const saved = await commandRequest("/api/v1/rooms/" + encodeURIComponent(room) + "/lock", "PUT",
            desired === undefined ? { revision, locked } : { revision, locked, desired });
          stale(authority);
          if (saved.room !== room || !Number.isInteger(saved.revision) || typeof saved.effective?.allowed !== "boolean"
              || typeof saved.effective?.locked !== "boolean") throw problem("invalid_response");
          policies[room] = saved; policyAt[room] = Date.now(); policyFailures.delete(room); notify("policy"); return saved;
        } catch (error) {
          // A rejected/uncertain write never changes the displayed switch optimistically.
          policyWrites.delete(room);
          if (!error.stale && !ended) await client.loadPolicy(room).catch(() => {});
          throw error;
        } finally { policyWrites.delete(room); }
      },
      async command(room, action, value) {
        await ready();
        if (!client.canControl(room)) throw problem(policies[room]?.effective?.reason === "policy_unavailable" ? "policy_unavailable" : "room_locked", 403);
        // A refusal means the lock shown may be out of date: read it again.
        const path = "/api/v1/rooms/" + encodeURIComponent(room) + "/commands";
        try { return await overdue("command_timeout", path, commandRequest(path, "POST", { action, value }), config.commandTimeout || 8000); }
        catch (error) { if ([403, 423].includes(error.status) || error.code === "policy_unavailable") client.loadPolicy(room).catch(() => {}); throw error; }
      },
      watchRoom(room) { policyRoom = room; return policyPoll.start(); },
      // A room list on show asks for every room's lock; nothing is read otherwise.
      watchLocks(on) { locksWatched = Boolean(on); return lockPoll.start(); },
      // A page shown again (visibilitychange or pageshow, even with no hide
      // between) checks its streams at once: one that went silent while the
      // page was away, as a phone's can, is reopened now, not a watchdog later.
      setActive(value) {
        const next = Boolean(value);
        active = next; policyPoll.start(); lockPoll.start();
        streams.forEach((stream) => stream.watch());
      },
      stop() { active = false; policyPoll.stop(); lockPoll.stop(); streams.forEach((stream) => stream.watch()); },
      async allOff() { await ready(); return commandRequest("/api/v1/all-off", "POST", {}, 2 * commandDeadline); },
      async login(username, password) {
        const address = new URL(origin);
        if (guestOnly || address.protocol !== "https:" && !["127.0.0.1", "localhost", "[::1]"].includes(address.hostname)) throw problem("secure_connection_required");
        if (!(await loadSecurity()).enabled) throw problem("auth_disabled", 403);
        ended = false; user = null; authRevision++; const revision = authRevision;
        const session = await data("/api/v1/session"); csrf = session.csrf;
        const answer = await data("/api/v1/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
        stale(revision); user = principal(answer.user); csrf = answer.csrf; notify("change"); return user;
      },
      async logout() {
        try {
          if (legacy) throw problem("access_not_configured", 404);
          if (mock) return await demoRequest("/api/v1/logout");
          if (!token) { const session = await data("/api/v1/session"); csrf = session.csrf; }
          return await data("/api/v1/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }, true);
        } finally { token = null; lost("login_required"); }
      },
      async setSecurity(value) {
        if (legacy || mock) throw problem("access_not_configured", 404);
        await loadSecurity();
        const answer = await data("/api/v1/security", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }, true);
        security = securityStatus(answer); token = null; lost("session_expired");
        csrf = answer.csrf || null; if (!security.enabled) ended = false;
        notify("security"); return security;
      },
      clear: lost,
      // The gateway's event streams, each behind the client's credential and
      // a liveness watchdog. The gateway sends a frame at least every
      // STREAM_HEARTBEAT_MS, so a stream that shows none for streamLiveness
      // (three heartbeats) while the page is shown is dead, even when the
      // EventSource has not noticed (a phone back from the background, a
      // network that dropped without a word). It is closed, counted as a
      // stream_error and opened again at once; its listener sees an error
      // (readyState CONNECTING), then the new stream's open and snapshot, as
      // after a browser's own reconnection. Heartbeats reach no listener.
      eventSource(Source) {
        return class AuthorizedEvents {
          constructor(url) {
            this.url = url; this.readyState = 0; this.closed = false; this.source = null; this.heard = now(); this.timer = null;
            ready().then(() => {
              if (this.closed) return;
              const target = new URL(url, origin);
              if (target.origin !== origin) throw problem("origin_mismatch");
              streams.add(this);
              this.connect();
            }).catch((error) => { this.readyState = 2; if (this.onerror) this.onerror({ status: error.status || 0 }); });
            this.unsubscribe = client.subscribe((event) => { if (event.type === "unauthorized") this.close(); });
          }
          connect() {
            const url = this.url, source = new Source(url, token ? { headers: { Authorization: "Bearer " + token } } : undefined);  // lint: allow — Bearer scheme prefix; token comes from private custody
            this.source = source; this.heard = now(); this.watch();
            const current = () => this.source === source && !this.closed;
            let started = now(), opened = false;
            source.onopen = (event) => {
              if (!current()) return;
              this.readyState = 1; opened = true; this.heard = now();
              if (this.onopen) this.onopen(event);
            };
            source.onmessage = (event) => {
              if (!current()) return;
              this.heard = now();
              if (event?.data !== HEARTBEAT_FRAME && this.onmessage) this.onmessage(event);
            };
            source.onerror = (event) => {
              if (!current()) return;
              this.readyState = source.readyState;
              // Only the terminal's EventSource sees a status: 429 is too_many_streams.
              if (active) record(event?.status === 429 ? "stream_refused" : "stream_error",
                opened || event?.status === 429 ? undefined : (now() - started) / 1000, url, source.protocol);
              started = now(); opened = false;
              // A stream closed for good is its listener's to reopen; one reconnecting is watched.
              if (source.readyState === 2) { clearTimeout(this.timer); this.timer = null; }
              if (event && [401, 403].includes(event.status)) lost("session_expired");
              // One credential probe per outage, however many streams report it.
              else if (!token && !probing) probing = data("/api/v1/me", null, true).catch(() => {}).finally(() => { probing = null; });
              if (this.onerror) this.onerror(event);
            };
          }
          // Wakes when the stream would have been silent for streamLiveness,
          // and reconnects it if it was; never while the page is hidden.
          watch() {
            clearTimeout(this.timer); this.timer = null;
            if (this.closed || !this.source || this.source.readyState === 2 || !active) return;
            const left = streamLiveness - (now() - this.heard);
            if (left <= 0) { this.stalled(); return; }
            this.timer = setTimeout(() => { this.timer = null; this.watch(); }, left);
            // A watchdog alone need not keep a terminal process alive.
            if (this.timer.unref) this.timer.unref();
          }
          stalled() {
            const source = this.source;
            this.source = null; this.readyState = 0; source.close();
            record("stream_error", undefined, this.url, source.protocol);
            if (this.onerror) this.onerror({ type: "error", status: 0, stalled: true });
            if (!this.closed) this.connect();
          }
          close() {
            this.closed = true; this.readyState = 2; clearTimeout(this.timer); this.timer = null; streams.delete(this);
            if (this.source) this.source.close();
            if (this.unsubscribe) this.unsubscribe();
          }
          addEventListener(type, fn) { this["on" + type] = fn; }
        };
      }
    };
    return client;
  }
  const exported = { create, problem, returnTo, secureOrigin, MESSAGES, STREAM_HEARTBEAT_MS, COMMAND_DEADLINE_MS, client: null };
  global.ThermiteAccess = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (typeof document === "undefined") return;
  const params = new URLSearchParams(location.search), nativeFetch = global.fetch.bind(global), NativeEvents = global.EventSource;
  const site = global.THERMITE_ACCESS_CONFIG;
  const siteNative = site && site.version === 1 && site.mode === "native";
  const siteGuest = site && site.version === 1 && site.mode === "guest";
  const client = exported.client = create({ fetch: nativeFetch, native: params.get("backend") !== "mock" && (params.get("backend") === "native" || siteNative), mock: params.get("backend") === "mock",
    guest: siteGuest, secure: siteGuest ? site.secure : null,
    onUnauthorized() {
      if (document.body && ["access", "security"].includes(document.body.dataset.thermitePage)) return;
      // A Guest listener page has no Account page to go to; a reload admits the Guest again.
      const text = client.guestOnly ? "Guest access changed. Reload this page to continue." : "Sign in to thermite to continue.";
      document.querySelectorAll("main,.app").forEach((main) => { const message = document.createElement("p"); message.textContent = text; main.replaceChildren(message); });
      if (!client.guestOnly && document.body && document.body.dataset.thermitePage !== "access") location.replace("/account/?next=" + encodeURIComponent(returnTo(location.href, location.origin)));
    } });
  if (!client.native && !client.mock) { global.fetch = client.fetch; if (NativeEvents) global.EventSource = client.eventSource(NativeEvents); }
  document.addEventListener("visibilitychange", () => {
    client.setActive(document.visibilityState !== "hidden");
    if (document.visibilityState === "hidden") client.flushTelemetry(true).catch(() => {});
  });
  global.addEventListener("pagehide", () => client.setActive(false));
  global.addEventListener("pageshow", () => client.setActive(document.visibilityState !== "hidden"));
})(globalThis);
