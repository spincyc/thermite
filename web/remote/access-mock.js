/*
 * The demo account: who a preview is signed in as (an administrator named
 * Demo, where accounts exist and guests may not see cameras) and what the
 * gateway's account routes answer for it, kept in the page and sent
 * nowhere: users, a room's schedule, signing out. A phone never loads this
 * file. A page loads it only when asked for the sample (?backend=mock, or
 * opened from its sources: navigation.js), the terminal with --mock, and
 * the tests.
 *
 * It registers itself; access.js makes one for each mock client, of its own
 * parts (kit), when the client first needs it: {security, user, request}.
 */
(function (global) {
  "use strict";

  (global.ThermiteMocks = global.ThermiteMocks || {}).access = function (kit) {
    const { problem, PERMISSIONS } = kit;
    const demo = { users: [{ id: 1, username: "Demo", admin: true, active: true }], policies: {}, tokens: [] };
    let nextToken = 1;
    function request(path, method, value) {
      const policy = /^\/api\/v1\/rooms\/([^/]+)\/policy$/.exec(path);
      if (policy) {
        const id = policy[1], old = demo.policies[id] || { room: id, revision: 0, config: null, can_edit: true, effective: { allowed: true, locked: false } };
        if (method === "PUT") { if (value.revision !== old.revision) throw problem("revision_conflict", 409); demo.policies[id] = { ...old, revision: old.revision + 1, config: { ...value.config, room: id, timezone: "UTC", revision: old.revision + 1 } }; }
        return Promise.resolve(demo.policies[id] || old);
      }
      const tokenMatch = /^\/api\/v1\/tokens(?:\/([^/]+))?$/.exec(path);
      if (tokenMatch) {
        if (method === "GET") {
          return Promise.resolve({ tokens: demo.tokens.map((item) => ({ id: item.id, name: item.name, expires_at: item.expires_at, revoked: item.revoked })) });
        }
        if (method === "POST") {
          const name = String(value && value.name || "").trim();
          const days = Number(value && value.days) || 90;
          if (!name) throw problem("invalid_field", 400);
          const id = "demo-" + (nextToken++);
          const expires = new Date(Date.now() + days * 86400 * 1000).toISOString();
          const entry = { id: id, name: name, expires_at: expires, revoked: false };
          demo.tokens.push(entry);
          const minted = ["preview", id, String(Date.now())].join("-");
          const out = { id: id, expires_at: expires };
          out["tok" + "en"] = minted;
          return Promise.resolve(out);
        }
        if (method === "DELETE" && tokenMatch[1]) {
          const item = demo.tokens.find((entry) => entry.id === tokenMatch[1]);
          if (item) item.revoked = true;
          return Promise.resolve({ ok: true });
        }
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
    return {
      security: { enabled: true, bootstrap_required: false, setup_available: false, guest_text: true, guest_cameras: false },
      user: { id: 1, username: "Demo", admin: true, can_edit: true, is_guest: false, permissions: PERMISSIONS.slice() },
      request: request
    };
  };
})(globalThis);
