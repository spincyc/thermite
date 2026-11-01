/* Same-origin identity client shared by the browser and terminal. No credential storage. */
(function (global) {
  "use strict";
  const MESSAGES = {
    login_required: "Sign in to thermite.", session_expired: "Your session has ended. Sign in again.",
    invalid_token: "Your terminal sign-in has ended. Run thermite access login again.",
    invalid_login: "The username or password was not accepted.", login_throttled: "Too many sign-in attempts. Try again later.",
    forbidden: "Your account does not allow this action.", room_locked: "Room scheduling blocks this action.",
    policy_locked: "Room scheduling blocks this action.", policy_unavailable: "Room policy is unavailable. Control is paused.",
    revision_conflict: "The schedule changed. Reload it before saving.", last_admin: "Keep at least one active administrator.",
    reserved_role: "Built-in roles cannot be changed.", role_in_use: "This role is assigned to a user.",
    invalid_password: "Choose a stronger password.", invalid_policy: "Check the schedule times, days and sources.",
    invalid_user: "Check the username, role and room grants.", invalid_role: "Check the role name and permissions.",
    csrf_failed: "The sign-in form expired. Reload and try again.", access_unavailable: "Access service unavailable.",
    access_not_configured: "Access is not configured at this address.", invalid_response: "The access service could not answer.",
    origin_mismatch: "The terminal credential belongs to a different address.", secure_connection_required: "Use your site’s HTTPS address to sign in.", connection_failed: "Could not reach thermite."
  };
  function problem(code, status) {
    const error = new Error(MESSAGES[code] || "The action could not be completed.");
    error.code = Object.prototype.hasOwnProperty.call(MESSAGES, code) ? code : "access_unavailable";
    error.status = status || 0;
    return error;
  }
  function returnTo(raw, origin) {
    const fallback = "/static/remote/home.html";
    let target; try { target = new URL(raw || fallback, origin + "/static/remote/"); } catch (_) { return fallback; }
    if (target.origin !== origin || target.username || target.password
        || !/^\/static\/remote\/(?:index\.html|home\.html|pair\.html|cameras\.html|topology\.html|security\.html|settings\.html|users\.html)?$/.test(target.pathname)) return fallback;
    const safe = new URLSearchParams();
    for (const key of ["room", "source"]) { const value = target.searchParams.get(key); if (value && /^[a-z][a-z0-9_]{0,99}$/.test(value)) safe.set(key, value); }
    const theme = target.searchParams.get("theme"); if (["dark", "light"].includes(theme)) safe.set("theme", theme);
    return target.pathname + (safe.size ? "?" + safe.toString() : "");
  }
  function principal(value) {
    if (!value || !Number.isInteger(value.id) || typeof value.username !== "string" || value.username.length > 150
        || /[\x00-\x1f\x7f-\x9f]/.test(value.username) || !Number.isInteger(value.role)
        || !Array.isArray(value.permissions) || value.rooms !== null && (!Array.isArray(value.rooms)
          || value.rooms.some((id) => typeof id !== "string" || !/^[a-z][a-z0-9_]{0,99}$/.test(id)))) throw problem("invalid_response");
    return { id: value.id, username: value.username, role: value.role, rooms: value.rooms,
      can_edit: value.can_edit === true, site: typeof value.site === "string" ? value.site : null,
      permissions: value.permissions.filter((permission) => ["site.read", "room.control", "settings.manage", "users.manage", "roles.manage", "pairing.manage"].includes(permission)) };
  }
  function create(options) {
    const config = options || {}, rawFetch = config.fetch || global.fetch.bind(global);
    const base = (config.base || "").replace(/\/$/, ""), origin = new URL(base || global.location.href).origin;
    const legacy = config.native === true, mock = config.mock === true;
    const token = config.token || null, listeners = [], policies = Object.create(null);  // lint: allow — credential supplied by the caller
    let user = null, csrf = null, loading = null, ended = false, policyRoom = null, policyTimer = null, policyGeneration = 0, authRevision = 0;
    let active = typeof document === "undefined" || document.visibilityState !== "hidden";
    const demo = { users: [{ id: 1, username: "Demo", role: 1, rooms: null, active: true }],
      roles: [{ id: 1, name: "Admin", reserved: true, permissions: ["site.read", "room.control", "settings.manage", "users.manage", "roles.manage", "pairing.manage"] },
        { id: 2, name: "User", reserved: true, permissions: ["site.read", "room.control"] }, { id: 3, name: "Viewer", reserved: true, permissions: ["site.read"] }], policies: {} };
    function demoRequest(path, method, value) {
      const policy = /^\/api\/v1\/rooms\/([^/]+)\/policy$/.exec(path);
      if (policy) {
        const id = policy[1], old = demo.policies[id] || { room: id, revision: 0, config: null, can_edit: true, effective: { allowed: true, locked: false } };
        if (method === "PUT") { if (value.revision !== old.revision) throw problem("revision_conflict", 409); demo.policies[id] = { ...old, revision: old.revision + 1, config: { ...value.config, room: id, timezone: "UTC", revision: old.revision + 1 } }; }
        return Promise.resolve(demo.policies[id] || old);
      }
      const match = /^\/api\/v1\/(users|roles)(?:\/(\d+))?$/.exec(path);
      if (match) {
        const kind = match[1], list = demo[kind], id = Number(match[2]);
        if (method === "GET") return Promise.resolve({ [kind]: JSON.parse(JSON.stringify(list)) });
        const clean = { ...value }; delete clean.password;
        if (method === "POST") { const item = { ...clean, id: Math.max(0, ...list.map((entry) => entry.id)) + 1, reserved: false }; list.push(item); return Promise.resolve(item); }
        const item = list.find((entry) => entry.id === id);
        if (!item) throw problem("invalid_response", 404);
        if (item.reserved) throw problem("reserved_role", 409);
        if (method === "DELETE") list.splice(list.indexOf(item), 1); else Object.assign(item, clean);
        return Promise.resolve(item);
      }
      if (path === "/api/v1/logout") return Promise.resolve({ ok: true });
      throw problem("access_not_configured", 404);
    }
    const notify = (type) => listeners.slice().forEach((fn) => fn({ type, user }));
    function lost(code) {
      user = null; csrf = null; ended = true; authRevision++; clearTimeout(policyTimer);
      Object.keys(policies).forEach((id) => delete policies[id]);
      notify("unauthorized");
      if (config.onUnauthorized) config.onUnauthorized(code);
    }
    async function raw(path, init) {
      const target = new URL(path, origin + "/");
      if (target.origin !== origin || target.username || target.password) throw problem("origin_mismatch");
      const request = { ...(init || {}), credentials: token ? "omit" : "same-origin", redirect: "error", cache: "no-store" };
      const headers = new Headers(request.headers || {});
      if (token) headers.set("Authorization", "Bearer " + token);
      if (!token && !["GET", "HEAD"].includes((request.method || "GET").toUpperCase()) && csrf) headers.set("X-CSRFToken", csrf);
      request.headers = headers;
      try { return await rawFetch(target.href, request); } catch (_) { throw problem("connection_failed"); }
    }
    function stale(revision) {
      if (revision === authRevision) return;
      const error = problem("session_expired", 401); error.stale = true; throw error;
    }
    async function data(path, init, check) {
      const revision = authRevision;
      const response = await raw(path, init);
      if (check) stale(revision);
      let value;
      if (check && response.status === 401) lost("session_expired");
      try { value = await response.json(); } catch (_) { throw problem(response.status === 401 ? "session_expired" : "invalid_response", response.status); }
      if (check && response.status !== 401) stale(revision);
      if (!response.ok) {
        if (check && response.status === 401 && !ended) lost(value && value.error);
        throw problem(response.status === 404 && path === "/api/v1/session" ? "access_not_configured" : value && value.error, response.status);
      }
      return value;
    }
    async function ready() {
      if (legacy) return null;
      if (mock) { user = { id: 1, username: "Demo", role: 1, rooms: null, can_edit: true,
        permissions: ["site.read", "room.control", "settings.manage", "users.manage", "roles.manage", "pairing.manage"] }; return user; }
      if (ended) throw problem("login_required", 401);
      if (user) return user;
      if (!loading) loading = (async () => {
        if (!token) {
          if (!global.location && !config.allowSession) throw problem("login_required", 401);
          const answer = await data("/api/v1/session"); csrf = answer.csrf;
        }
        user = principal(await data("/api/v1/me", null, true)); notify("change"); return user;
      })().catch((error) => { if (error.status === 401 && !ended && !error.stale) lost(error.code); throw error; })
        .finally(() => { loading = null; });
      return loading;
    }
    async function fetchAuth(url, init) {
      if (legacy || mock) return rawFetch(url, init);
      await ready();
      const target = new URL(url, origin), method = (init && init.method || "GET").toUpperCase();
      if (!client.has("pairing.manage") && (target.pathname === "/rest/items" && target.searchParams.get("metadata") === "pairing"
          || method === "POST" && target.pathname.startsWith("/rest/items/"))) throw problem("forbidden", 403);
      const revision = authRevision;
      const response = await raw(url, init);
      stale(revision);
      if (response.status === 401) { lost("session_expired"); throw problem("session_expired", 401); }
      return response;
    }
    async function json(path, method, value) {
      await ready();
      if (legacy) throw problem("access_not_configured", 404);
      if (mock) return (config.mockRequest || demoRequest)(path, method || "GET", value);
      const init = { method: method || "GET", headers: { Accept: "application/json" } };
      if (value !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(value); }
      return data(path, init, true);
    }
    const client = {
      native: legacy, mock, base, origin, ready, fetch: fetchAuth, json,
      current: () => user,
      subscribe(fn) { listeners.push(fn); return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); }; },
      has(permission) { return legacy || Boolean(user && user.permissions && user.permissions.includes(permission)); },
      canEdit() { return Boolean(user && user.can_edit); },
      canControl(room) {
        if (legacy || mock) return true;
        if (!client.has("room.control") || user.rooms !== null && !user.rooms.includes(room)) return false;
        return !(policies[room] && policies[room].effective && policies[room].effective.allowed === false);
      },
      policy(room) { return policies[room] || null; },
      async loadPolicy(room) { const value = await json("/api/v1/rooms/" + encodeURIComponent(room) + "/policy"); policies[room] = value; notify("policy"); return value; },
      async savePolicy(room, revision, value) {
        const saved = await json("/api/v1/rooms/" + encodeURIComponent(room) + "/policy", "PUT", { revision, config: value });
        policies[room] = { ...policies[room], ...saved }; notify("policy");
        await client.loadPolicy(room); return saved;
      },
      async command(room, action, value) {
        await ready();
        if (!client.canControl(room)) throw problem(client.has("room.control") ? "room_locked" : "forbidden", 403);
        try { return await json("/api/v1/rooms/" + encodeURIComponent(room) + "/commands", "POST", { action, value }); }
        catch (error) { if ([403, 423].includes(error.status)) client.loadPolicy(room).catch(() => {}); throw error; }
      },
      watchRoom(room) {
        policyRoom = room; clearTimeout(policyTimer); const generation = ++policyGeneration;
        if (legacy || mock || !room || !active || ended) return Promise.resolve(null);
        const refresh = () => {
          if (!active || ended || !policyRoom || generation !== policyGeneration) return;
          return client.loadPolicy(policyRoom).catch(() => null).finally(() => {
            if (active && !ended && generation === policyGeneration) { policyTimer = setTimeout(refresh, 15000); if (policyTimer.unref) policyTimer.unref(); }
          });
        };
        return refresh();
      },
      setActive(value) { active = Boolean(value); clearTimeout(policyTimer); policyGeneration++; if (active && policyRoom) client.watchRoom(policyRoom); },
      stop() { active = false; clearTimeout(policyTimer); policyGeneration++; },
      async allOff() { await ready(); if (!client.has("room.control")) throw problem("forbidden", 403); return json("/api/v1/all-off", "POST", {}); },
      async login(username, password) {
        const address = new URL(origin);
        if (address.protocol !== "https:" && !["127.0.0.1", "localhost", "[::1]"].includes(address.hostname)) throw problem("secure_connection_required");
        ended = false; user = null; authRevision++; const revision = authRevision;
        const session = await data("/api/v1/session"); csrf = session.csrf;
        const answer = await data("/api/v1/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
        stale(revision); user = principal(answer.user); csrf = answer.csrf; notify("change"); return user;
      },
      async logout() { try { await json("/api/v1/logout", "POST", {}); } finally { lost("login_required"); } },
      clear: lost,
      eventSource(Source) {
        return class AuthorizedEvents {
          constructor(url) {
            this.url = url; this.readyState = 0; this.closed = false; this.source = null;
            ready().then(() => {
              if (this.closed) return;
              const target = new URL(url, origin);
              if (target.origin !== origin) throw problem("origin_mismatch");
              const source = new Source(url, token ? { headers: { Authorization: "Bearer " + token } } : undefined);  // lint: allow — Bearer scheme prefix; token comes from private custody
              this.source = source;
              source.onopen = (event) => { this.readyState = 1; if (this.onopen) this.onopen(event); };
              source.onmessage = (event) => { if (this.onmessage) this.onmessage(event); };
              source.onerror = (event) => {
                this.readyState = source.readyState;
                if (event && [401, 403].includes(event.status)) lost("session_expired");
                else if (!token) data("/api/v1/me", null, true).catch(() => {});
                if (this.onerror) this.onerror(event);
              };
            }).catch(() => { this.readyState = 2; if (this.onerror) this.onerror({ status: 401 }); });
            this.unsubscribe = client.subscribe((event) => { if (event.type === "unauthorized") this.close(); });
          }
          close() { this.closed = true; this.readyState = 2; if (this.source) this.source.close(); if (this.unsubscribe) this.unsubscribe(); }
          addEventListener(type, fn) { this["on" + type] = fn; }
        };
      }
    };
    return client;
  }
  const exported = { create, problem, returnTo, MESSAGES, client: null };
  global.ThermiteAccess = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (typeof document === "undefined") return;
  const params = new URLSearchParams(location.search), nativeFetch = global.fetch.bind(global), NativeEvents = global.EventSource;
  const site = global.THERMITE_ACCESS_CONFIG;
  const siteNative = site && site.version === 1 && site.mode === "native";
  const client = exported.client = create({ fetch: nativeFetch, native: params.get("backend") !== "mock" && (params.get("backend") === "native" || siteNative), mock: params.get("backend") === "mock",
    onUnauthorized() {
      if (document.body && document.body.dataset.thermitePage === "access") return;
      document.querySelectorAll("main,.app").forEach((main) => { const message = document.createElement("p"); message.textContent = "Sign in to thermite to continue."; main.replaceChildren(message); });
      if (document.body && document.body.dataset.thermitePage !== "access") location.replace("access.html?next=" + encodeURIComponent(returnTo(location.href, location.origin)));
    } });
  if (!client.native && !client.mock) { global.fetch = client.fetch; if (NativeEvents) global.EventSource = client.eventSource(NativeEvents); }
  document.addEventListener("visibilitychange", () => client.setActive(document.visibilityState !== "hidden"));
  global.addEventListener("pagehide", () => client.setActive(false));
  global.addEventListener("pageshow", () => client.setActive(document.visibilityState !== "hidden"));
})(globalThis);
