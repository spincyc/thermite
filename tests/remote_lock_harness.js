"use strict";
// Real shared access/backend/session/terminal code against a fake gateway:
// lock display, control gating, Guest refusals and policy refresh behaviour.
const assert = require("node:assert/strict");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared");
console.log = console.info = () => {};
const tick = () => new Promise(resolve => setImmediate(resolve));
const origin = "https://site.example:13600", csrf = "C".repeat(32);
const json = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => value, text: async () => JSON.stringify(value),
  clone() { return json(value, status); }, headers: { get: () => "application/json", getSetCookie: () => [] } });
const text = value => ({ ok: true, status: 200, text: async () => value, json: async () => { throw new SyntaxError("text"); }, headers: { get: () => "text/plain", getSetCookie: () => [] } });
const admin = { id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true,
  permissions: ["site.read", "room.control", "camera.view", "settings.manage", "users.manage", "pairing.manage"] };
const guest = { id: 0, username: "Guest", admin: false, can_edit: false, is_guest: true, auth_enabled: true, permissions: ["site.read", "room.control"] };

function gateway(options) {
  const site = { principal: options.principal, policies: {}, calls: [], commands: [], failPolicy: false };
  const policy = room => site.policies[room] || (site.policies[room] = { room, revision: 0, config: null, manual_lock: null,
    can_edit: site.principal.can_edit, can_lock: site.principal.can_edit, enforcement: { state: "unknown" },
    effective: { allowed: true, locked: false, reason: "unlocked", desired: null, until: null, evaluated_at: String(Date.now()) } });
  site.fetch = async (url, init = {}) => {
    const target = new URL(url), route = target.pathname, method = (init.method || "GET").toUpperCase();
    site.calls.push(method + " " + route);
    if (route === "/api/v1/security") return json({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, csrf });
    if (route === "/api/v1/session") return json({ csrf });
    if (route === "/api/v1/me") return json(site.principal);
    let match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/policy$/.exec(route);
    if (match) {
      if (site.failPolicy) return json({ error: "policy_unavailable" }, 503);
      return json({ ...policy(match[1]), effective: { ...policy(match[1]).effective, evaluated_at: String(Date.now()) } });
    }
    match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/lock$/.exec(route);
    if (match) {
      const body = JSON.parse(init.body), saved = policy(match[1]);
      if (body.revision !== saved.revision) return json({ error: "revision_conflict" }, 409);
      if (options.refuseLock) return json({ error: "room_state_unavailable" }, 409);
      site.policies[match[1]] = { ...saved, revision: saved.revision + 1,
        manual_lock: body.locked ? { desired: "off", started_at: new Date().toISOString() } : null,
        effective: { ...saved.effective, allowed: !body.locked, locked: body.locked, reason: body.locked ? "manual_lock" : "unlocked" } };
      return json(site.policies[match[1]]);
    }
    match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/commands$/.exec(route);
    if (match) { site.commands.push(match[1]); return json({ accepted: true }, 202); }
    if (route.startsWith("/unifi/media/")) return json({ error: "login_required" }, 401);
    if (/^\/rest\/items\/[^/]+\/state$/.test(route)) return text(route.includes("_source") ? "off" : "NULL");
    return json({ error: "not_found" }, 404);
  };
  return site;
}

class QuietEvents {
  constructor() { this.readyState = 0; setImmediate(() => { if (this.readyState !== 2) { this.readyState = 1; if (this.onopen) this.onopen({}); } }); }
  close() { this.readyState = 2; }
}

function assemble(site, room) {
  const shared = Shared.load();
  const access = shared.Access.create({ base: origin, fetch: site.fetch, policyInterval: 3600000 });
  const parts = Main.assemble(Main.parse(["--url", origin, "--room", room, "--still"]), {
    shared, access, fetch: site.fetch, EventSource: QuietEvents, environ: {},
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {}
  });
  const toasts = [];
  parts.session.subscribe(event => { if (event.type === "toast") toasts.push(event.title + ": " + event.body); });
  return { shared, access, parts, toasts };
}

(async () => {
  const proof = {};
  const room = Shared.load().house.rooms[0].id;

  // Administrator: K locks the current room from the terminal, the view offers
  // the button with its key, and the session gates every ordinary control.
  const site = gateway({ principal: admin });
  const { access, parts, toasts } = assemble(site, room);
  await parts.controller.start(room);
  for (let i = 0; i < 20 && access.policyStatus(room) !== "ready"; i++) await tick();
  for (let i = 0; i < 20 && parts.session.roomLock().disabled; i++) await tick();
  let lock = parts.session.roomLock();
  assert.equal(lock.status, "ready"); assert.equal(lock.canManage, true); assert.equal(lock.disabled, false, JSON.stringify(lock));
  assert.equal(parts.session.controlBlock(), null);
  let frame = parts.controller.frame(149, 40);
  assert(!frame.focusables.some(hit => hit.kind === "room-lock"), "the remote shows the lock state, not its control");
  parts.controller.key("i", { name: "i" });
  frame = parts.controller.frame(149, 40);
  assert(frame.focusables.some(hit => hit.kind === "room-lock"), "terminal lock button under Info");
  parts.controller.key(null, { name: "escape" });
  parts.controller.key("k", { name: "k" });
  for (let i = 0; i < 20 && !parts.session.roomLock().manual; i++) await tick();
  lock = parts.session.roomLock();
  assert.equal(lock.manual, true); assert.equal(lock.text, "Locked · until unlocked"); assert.equal(lock.label, "Unlock room");
  assert(site.calls.includes("PUT /api/v1/rooms/" + room + "/lock"));
  assert.match(parts.session.controlBlock(), /locked until an administrator unlocks it/);
  const before = site.commands.length;
  assert.equal(parts.session.chooseDigit("0"), null, "digit refused while locked");
  assert.equal(parts.session.pressKey(["ok"]), null, "key refused while locked");
  await tick();
  assert.equal(site.commands.length, before, "no command sent while locked");
  assert(toasts.some(entry => entry.startsWith("Read-only: This room is locked")), toasts.join(" / "));
  proof.terminalLockKey = proof.sharedGating = true;

  // A schedule lock shows its end; a refused toggle stays beside the control.
  site.policies[room] = { ...site.policies[room], revision: 9, manual_lock: null,
    effective: { allowed: false, locked: true, reason: "room_locked", desired: "off", until: new Date(Date.now() + 3600000).toISOString() } };
  await access.loadPolicy(room);
  assert.match(parts.session.roomLock().text, /^Locked by schedule until /);
  proof.scheduleEnd = true;
  parts.controller.stop();

  const refusing = gateway({ principal: admin, refuseLock: true });
  const second = assemble(refusing, room);
  await second.parts.controller.start(room);
  for (let i = 0; i < 20 && second.parts.session.roomLock().disabled; i++) await tick();
  assert.equal(await second.parts.session.toggleRoomLock(room), false);
  assert.match(second.parts.session.roomLock().error, /reports a current source/);
  second.parts.controller.stop();
  proof.inlineError = true;

  // Guests: no lock control; an account-only resource is refused without
  // ending the Guest's access; one failed poll keeps the last answer briefly.
  const guests = gateway({ principal: guest });
  const third = assemble(guests, room);
  await third.parts.controller.start(room);
  for (let i = 0; i < 20 && third.access.policyStatus(room) !== "ready"; i++) await tick();
  assert.equal(third.parts.session.roomLock().canManage, false);
  frame = third.parts.controller.frame(149, 40);
  assert(!frame.focusables.some(hit => hit.kind === "room-lock"));
  let unauthorized = 0;
  third.access.subscribe(event => { if (event.type === "unauthorized") unauthorized++; });
  await assert.rejects(third.access.fetch(origin + "/unifi/media/image/camera"), error => error.code === "login_required");
  assert.equal(unauthorized, 0); assert.equal((await third.access.ready()).is_guest, true);
  proof.guestRefusalKeepsAccess = true;
  let notices = 0;
  third.access.subscribe(event => { if (event.type === "policy") notices++; });
  await third.access.loadPolicy(room); await third.access.loadPolicy(room);
  assert.equal(notices, 0, "unchanged policy reads do not re-render");
  guests.failPolicy = true;
  await assert.rejects(third.access.loadPolicy(room));
  assert.equal(third.access.policyStatus(room), "ready", "one failed read keeps the last answer");
  assert.equal(third.parts.session.controlBlock(), null);
  proof.policyRefresh = true;
  third.parts.controller.stop();

  process.stdout.write(JSON.stringify(proof));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
