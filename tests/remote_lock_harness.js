"use strict";
// Real shared access/backend/session/terminal code against a fake gateway:
// lock display, control gating, an unknown lock that gates nothing, the
// padlock bubble, locking on a source, the Off face, Guest refusals, policy
// refresh behaviour and every room's lock in the room lists (terminal and
// browser Home).
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
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
const ROOMS = Shared.load().house.rooms.map(({ id }) => id);

function gateway(options) {
  // sources: what each room's Source Item reads; locks: every lock body sent;
  // summary: what GET /api/v1/locks says of a room instead of its policy;
  // policyGate: a promise every policy read waits for.
  const site = { principal: options.principal, policies: {}, calls: [], commands: [], locks: [], sources: {}, summary: {},
    failPolicy: false, failLocks: false, refuseCommands: false, policyGate: null };
  const policy = room => site.policies[room] || (site.policies[room] = { room, revision: 0, config: null, manual_lock: null,
    can_edit: site.principal.can_edit, can_lock: site.principal.can_edit, enforcement: { state: "unknown" },
    effective: { allowed: true, locked: false, reason: "unlocked", desired: null, until: null, evaluated_at: String(Date.now()) } });
  site.fetch = async (url, init = {}) => {
    const target = new URL(url), route = target.pathname, method = (init.method || "GET").toUpperCase();
    site.calls.push(method + " " + route);
    if (route === "/api/v1/security") return json({ enabled: true, revision: 1, bootstrap_required: false, setup_available: false, csrf });
    if (route === "/api/v1/session") return json({ csrf });
    if (route === "/api/v1/me") return json(site.principal);
    if (route === "/api/v1/locks") {
      if (site.failLocks) return json({ error: "policy_unavailable" }, 503);
      return json({ rooms: Object.fromEntries(ROOMS.map(id => [id, site.summary[id] || { locked: policy(id).effective.locked,
        manual: Boolean(policy(id).manual_lock), until: policy(id).effective.until, reason: policy(id).effective.reason }])) });
    }
    let match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/policy$/.exec(route);
    if (match) {
      if (site.policyGate) await site.policyGate;
      if (site.failPolicy) return json({ error: "policy_unavailable" }, 503);
      return json({ ...policy(match[1]), effective: { ...policy(match[1]).effective, evaluated_at: String(Date.now()) } });
    }
    match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/lock$/.exec(route);
    if (match) {
      const body = JSON.parse(init.body), saved = policy(match[1]), reads = site.sources[match[1]] || "off";
      site.locks.push(body);
      if (body.revision !== saved.revision) return json({ error: "revision_conflict" }, 409);
      if (options.refuseLock) return json({ error: "room_state_unavailable" }, 409);
      if (body.desired !== undefined && body.desired !== reads) return json({ error: "room_state_changed" }, 409);
      site.policies[match[1]] = { ...saved, revision: saved.revision + 1,
        manual_lock: body.locked ? { desired: reads, started_at: new Date().toISOString() } : null,
        effective: { ...saved.effective, allowed: !body.locked, locked: body.locked, reason: body.locked ? "manual_lock" : "unlocked" } };
      return json(site.policies[match[1]]);
    }
    match = /^\/api\/v1\/rooms\/([a-z0-9_]+)\/commands$/.exec(route);
    if (match) {
      const body = JSON.parse(init.body);
      site.commands.push(match[1] + " " + body.action + "=" + body.value);
      return site.refuseCommands ? json({ error: "room_locked" }, 423) : json({ accepted: true }, 202);
    }
    if (route.startsWith("/unifi/media/")) return json({ error: "login_required" }, 401);
    match = /^\/rest\/items\/loc_([a-z0-9_]+)_source\/state$/.exec(route);
    if (match) return text(site.sources[match[1]] || "off");
    if (/^\/rest\/items\/[^/]+\/state$/.test(route)) return text("NULL");
    return json({ error: "not_found" }, 404);
  };
  return site;
}

// Just enough DOM for browser Home's room rows.
class Element {
  constructor(tag, namespaceURI = "http://www.w3.org/1999/xhtml") { Object.assign(this, { tag, namespaceURI, children: [], attributes: {}, dataset: {}, textContent: "" }); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  hasAttribute(name) { return name in this.attributes; }
  toggleAttribute(name, force) { if (force) this.attributes[name] = ""; else delete this.attributes[name]; return Boolean(force); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  get lastChild() { return this.children.at(-1) || null; }
}

class QuietEvents {
  constructor() { this.readyState = 0; setImmediate(() => { if (this.readyState !== 2) { this.readyState = 1; if (this.onopen) this.onopen({}); } }); }
  close() { this.readyState = 2; }
}

// Timers the test fires itself: the session's and the terminal's.
function clock() {
  const timers = new Map();
  let next = 1;
  return { timers, setTimeout: (fn, ms) => { timers.set(next, { fn, ms }); return next++; }, clearTimeout: id => { timers.delete(id); },
    fire(ms) { for (const [id, timer] of timers) if (timer.ms === ms) { timers.delete(id); timer.fn(); } } };
}

function assemble(site, room, options) {
  const shared = Shared.load(), timers = clock();
  const access = shared.Access.create({ base: origin, fetch: site.fetch, policyInterval: 3600000, ...options });
  const parts = Main.assemble(Main.parse(["--url", origin, "--room", room, "--still"]), {
    shared, access, fetch: site.fetch, EventSource: QuietEvents, environ: {},
    setInterval: () => 0, clearInterval() {}, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout
  });
  const toasts = [];
  parts.session.subscribe(event => { if (event.type === "toast") toasts.push(event.title + ": " + event.body); });
  return { shared, access, parts, toasts, timers };
}

// What the room's Source Item reports next, as openHAB's event stream says it.
function report(parts, site, room, value) {
  site.sources[room] = value;
  parts.backend.onMessage(JSON.stringify({ topic: "openhab/items/loc_" + room + "_source/stateupdated", type: "ItemStateUpdatedEvent",
    payload: JSON.stringify({ type: "String", value }) }));
}

const bubble = (session, id) => session.bubbles(session.controls()).find(item => item.id === id);
const bubbleHit = (frame, id) => frame.grid.hits.find(entry => entry.hit.kind === "bubble" && entry.hit.item.id === id);
// The text a panel shows, by its region.
function panelText(frame, key) {
  const region = frame.regions.find(entry => entry.key === key);
  return frame.grid.rows.slice(region.y, region.y + region.h).map(row => row.slice(region.x, region.x + region.w).map(cell => cell.ch).join("")).join("\n");
}
async function settle(condition) { for (let i = 0; i < 40 && !condition(); i++) await tick(); }
const pause = () => new Promise(resolve => setTimeout(resolve, 5));

(async () => {
  const proof = {};
  const room = ROOMS[0];

  // Administrator: K locks the current room from the terminal, the view offers
  // the button with its key, and the session gates every ordinary control.
  const site = gateway({ principal: admin });
  const { access, parts, toasts, timers } = assemble(site, room);
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

  // The padlock: the bubble lit for what a locked room shows (Off here)
  // carries it in place of its LED and says so; every other bubble, device
  // bubbles too, is inert; the remote has no lock text row.
  const session = parts.session, label = session.current().label;
  const lit = bubble(session, "off");
  assert.equal(session.bubbleState(lit, session.state(room)), "current");
  assert.deepEqual(session.bubbleLock(lit, "current"), { locked: true, inert: false });
  assert.equal(session.bubbleLabel(lit, "current"), "Off, the room is off. Locked · until unlocked");
  const others = session.bubbles(session.controls()).filter(item => item.id !== "off");
  assert(others.some(item => item.choose) && others.some(item => !item.choose), others.map(item => item.id).join());
  others.forEach(item => assert.deepEqual(session.bubbleLock(item, session.bubbleState(item, session.state(room))), { locked: false, inert: true }, item.id));
  assert.equal(session.lockNotice(), "", "the padlock says it; no words");
  frame = parts.controller.frame(149, 40);
  const row = panelText(frame, "sources");
  assert.match(row, /0 \u26bf Off /); assert.equal(row.split("\u26bf").length, 2, "one padlock");
  assert(others.every(item => bubbleHit(frame, item.id).hit.disabled) && !bubbleHit(frame, "off").hit.disabled);
  assert(!/Locked|Read-only/.test(panelText(frame, "remote")), "no lock text row on the remote");
  proof.padlockBubble = proof.othersInert = proof.noLockRow = true;
  // Locked on Off: no remote either, and a device bubble cannot bring one up.
  assert(session.remoteHidden() && !frame.focusables.some(hit => ["key", "slider"].includes(hit.kind)));
  session.tapBubble(bubble(session, "example_tv"));
  assert(session.remoteHidden());
  proof.lockedOffNoRemote = true;
  // Tapping the lit bubble explains, on either client; nothing is sent.
  let cell = bubbleHit(frame, "off");
  parts.controller.mouse({ action: "mousedown", button: "left", x: cell.x + 2, y: cell.y });
  parts.controller.mouse({ action: "mouseup", button: "left", x: cell.x + 2, y: cell.y });
  assert.equal(parts.controller.ui.toast.title + ": " + parts.controller.ui.toast.body, "Read-only: This room is locked until an administrator unlocks it.");
  session.tapBubble(lit);
  assert.equal(toasts.at(-1), "Read-only: This room is locked until an administrator unlocks it.");
  assert.equal(site.commands.length, before);
  // No bubble lit (the source is unknown): the words go on the Read-only line.
  report(parts, site, room, "UNDEF");
  assert.equal(session.lockNotice(), "This room is locked until an administrator unlocks it.");
  assert.match(panelText(parts.controller.frame(149, 40), "remote"), /Read-only · This room is locked until/);
  report(parts, site, room, "off");
  assert.equal(session.lockNotice(), "");
  proof.litTapExplains = proof.unlitLockInWords = true;

  // A schedule lock shows its end, in the tap's explanation too; only its lit
  // bubble offers to keep it until unlocked.
  site.policies[room] = { ...site.policies[room], revision: 9, manual_lock: null,
    effective: { allowed: false, locked: true, reason: "room_locked", desired: "off", until: new Date(Date.now() + 3600000).toISOString() } };
  await access.loadPolicy(room);
  assert.match(session.roomLock().text, /^Locked by schedule until /);
  const until = session.roomLock().until;
  assert(until);
  session.tapBubble(lit);
  assert.equal(toasts.at(-1), "Read-only: This room is locked by its schedule until " + until + ".");
  assert.equal(session.bubbleLabel(lit, "current"), "Off, the room is off. Locked by schedule until " + until);
  assert.deepEqual(session.lockMenu(lit).map(entry => entry.label), ["Lock until unlocked"]);
  assert.deepEqual(session.lockMenu(bubble(session, "example_player")), []);
  assert.equal(await session.lockMenu(lit)[0].run(), true);
  assert.deepEqual(site.locks.at(-1), { revision: 9, locked: true, desired: "off" });
  assert.equal(session.roomLock().manual, true);
  proof.scheduleEnd = proof.keepScheduleLock = true;

  // An unlocked room offers to lock on any source or Off; devices have no menu.
  assert.equal(await session.toggleRoomLock(room), true);
  assert.deepEqual(session.lockMenu(bubble(session, "off")).map(entry => entry.label), ["Lock off"]);
  assert.deepEqual(session.lockMenu(bubble(session, "example_player")).map(entry => entry.label), ["Lock on Player"]);
  assert.deepEqual(session.lockMenu(bubble(session, "example_tv")), []);

  // Off: no remote, the Off bubble selected, until a device bubble is tapped;
  // choosing Off again, or the room going off, hides it again.
  assert.equal(session.target(), null); assert(session.remoteHidden());
  assert.equal(session.selectedBubble(session.controls()), "off");
  assert(!parts.controller.frame(149, 40).focusables.some(hit => ["key", "slider"].includes(hit.kind)));
  assert.equal(await session.press("mute"), null, "no hidden key acts");
  session.tapBubble(bubble(session, "example_tv"));
  assert.equal(session.target().id, "example_tv"); assert(!session.remoteHidden());
  assert.equal(session.selectedBubble(session.controls()), "example_tv");
  assert(parts.controller.frame(149, 40).focusables.some(hit => hit.kind === "key"), "a device picked while off shows its buttons");
  session.tapBubble(bubble(session, "off"));
  assert(session.remoteHidden() && session.target() === null);
  report(parts, site, room, "example_player");
  assert.equal(session.target().id, "example_player"); assert.equal(session.selectedBubble(session.controls()), "example_player");
  session.tapBubble(bubble(session, "example_tv"));
  report(parts, site, room, "off");
  assert(session.remoteHidden(), "the room going off drops the picked remote");
  assert.equal(site.commands.length, before, "nothing sent");
  proof.offNoRemote = proof.offDevicePick = proof.offReturn = true;

  // Locking on what the room shows sends no choice.
  assert.equal(await session.lockOn("off", room), true);
  assert.deepEqual(site.locks.at(-1), { revision: site.locks.at(-1).revision, locked: true, desired: "off" });
  assert.equal(site.commands.length, before);
  assert.equal(toasts.at(-1), label + ": Room locked off until you unlock it.");
  assert.equal(await session.toggleRoomLock(room), true);
  proof.lockOnCurrent = true;

  // Another source is chosen first and locked once the room reports it; the
  // lock control reads busy meanwhile and a second request is ignored.
  let locking = session.lockOn("example_player", room);
  await settle(() => site.commands.length > before);
  assert.deepEqual(site.commands.slice(before), [room + " source=example_player"]);
  assert.equal(session.roomLock().busy, true); assert.equal(session.roomLock().label, "Saving…");
  assert.equal(await session.lockOn("example_computer", room), false, "a second request is ignored");
  assert.equal(await session.toggleRoomLock(room), false);
  assert.equal(site.commands.length, before + 1);
  report(parts, site, room, "example_player");
  assert.equal(await locking, true);
  assert.equal(site.locks.at(-1).desired, "example_player");
  assert.equal(access.policy(room).manual_lock.desired, "example_player");
  assert.equal(toasts.at(-1), label + ": Room locked on Player until you unlock it.");
  assert.equal(await session.toggleRoomLock(room), true);
  proof.lockOnSwitch = true;

  // The server reads something else by the time it locks: refused, kept beside the control.
  let locks = site.locks.length;
  locking = session.lockOn("example_computer", room);
  await settle(() => session.state(room).pending === "example_computer");
  report(parts, site, room, "example_computer"); site.sources[room] = "example_player";
  assert.equal(await locking, false);
  assert.equal(site.locks.at(-1).desired, "example_computer");
  assert.equal(session.roomLock().manual, false);
  assert.equal(session.roomLock().error, "The room changed before it could be locked. Check what it shows and try again.");
  assert.equal(toasts.at(-1), label + ": " + session.roomLock().error);
  report(parts, site, room, "example_computer");
  // The room settles on anything else: no lock is asked for.
  locks = site.locks.length;
  locking = session.lockOn("example_player", room);
  await settle(() => session.state(room).pending === "example_player");
  report(parts, site, room, "UNDEF");
  assert.equal(await locking, false);
  assert.equal(site.locks.length, locks);
  assert.equal(session.roomLock().error, "The room did not confirm Player, so it was not locked.");
  report(parts, site, room, "example_computer");
  proof.lockOnMismatch = true;

  // Nothing reported within LOCK_WAIT_MS: given up, nothing locked.
  locking = session.lockOn("off", room);
  await settle(() => session.state(room).pending === "off");
  await tick();
  assert.equal(session.roomLock().busy, true);
  timers.fire(parts.shared.Session.LOCK_WAIT_MS);
  assert.equal(await locking, false);
  assert.equal(site.locks.length, locks);
  assert.equal(session.roomLock().error, "The room did not report Off within 45 seconds, so it was not locked.");
  assert.equal(session.roomLock().busy, false);
  // The room is still switching: another lock is refused with the reason.
  assert.equal(await session.lockOn("example_computer", room), false);
  assert.equal(toasts.at(-1), label + ": Wait for the room to finish switching and report its source.");
  report(parts, site, room, "off");
  proof.lockOnTimeout = proof.midSwitchRefused = true;

  // A refused choice says so, and nothing is locked.
  site.refuseCommands = true;
  assert.equal(await session.lockOn("example_player", room), false);
  assert.equal(session.roomLock().error, "Could not switch to Player, so the room was not locked. This room is locked.");
  assert.equal(site.locks.length, locks);
  site.refuseCommands = false;
  proof.lockOnRefused = true;
  parts.controller.stop();

  const refusing = gateway({ principal: admin, refuseLock: true });
  const second = assemble(refusing, room);
  await second.parts.controller.start(room);
  for (let i = 0; i < 20 && second.parts.session.roomLock().disabled; i++) await tick();
  assert.equal(await second.parts.session.toggleRoomLock(room), false);
  assert.match(second.parts.session.roomLock().error, /reports a current source/);
  second.parts.controller.stop();
  proof.inlineError = true;

  // The terminal's menu: a right-click on a source, or o on a focused bubble,
  // opens it as a small sheet; arrows and Enter choose, Escape closes.
  const menus = gateway({ principal: admin });
  const fourth = assemble(menus, room), controller = fourth.parts.controller;
  await controller.start(room);
  await settle(() => !fourth.parts.session.roomLock().disabled);
  frame = controller.frame(149, 40);
  cell = bubbleHit(frame, "example_player");
  controller.mouse({ action: "mousedown", button: "right", x: cell.x, y: cell.y });
  controller.mouse({ action: "mouseup", button: "right", x: cell.x, y: cell.y });
  assert.equal(controller.ui.overlay.kind, "lock-menu");
  frame = controller.frame(149, 40);
  assert.deepEqual(frame.focusables.map(hit => hit.label), ["Lock on Player"]);
  assert.equal(menus.commands.length, 0, "a right-click does not choose the source");
  controller.key(null, { name: "escape" });
  assert.equal(controller.ui.overlay, null);
  controller.frame(149, 40);
  assert.equal(controller.ui.focus, "bubble:example_player", "focus returns to the bubble");
  proof.terminalMenuMouse = true;
  controller.key(null, { name: "left" });
  controller.key("o", { name: "o" });
  frame = controller.frame(149, 40);
  assert.equal(controller.ui.overlay.kind, "lock-menu");
  assert.deepEqual(frame.focusables.map(hit => hit.label), ["Lock off"]);
  controller.key(null, { name: "down" });
  controller.key(null, { name: "enter" });
  await settle(() => fourth.parts.session.roomLock().manual);
  assert.deepEqual(menus.locks.at(-1), { revision: 0, locked: true, desired: "off" });
  assert.equal(controller.ui.overlay, null);
  controller.key("o", { name: "o" });
  assert.deepEqual(controller.frame(149, 40).focusables.map(hit => hit.label), ["Unlock room"]);
  controller.key(null, { name: "escape" });
  controller.key("?", { name: "?" });
  assert.match(panelText(controller.frame(149, 40), "sheet:help"), /o\s+Lock menu of the selected source or Off/);
  controller.key(null, { name: "escape" });
  controller.stop();
  proof.terminalMenuKey = true;

  // Guests: no lock control; an account-only resource is refused without
  // ending the Guest's access; one failed poll keeps the last answer briefly.
  const guests = gateway({ principal: guest });
  const third = assemble(guests, room);
  await third.parts.controller.start(room);
  for (let i = 0; i < 20 && third.access.policyStatus(room) !== "ready"; i++) await tick();
  assert.equal(third.parts.session.roomLock().canManage, false);
  frame = third.parts.controller.frame(149, 40);
  assert(!frame.focusables.some(hit => hit.kind === "room-lock"));
  // No lock menu for anyone else: a right-click is an ordinary click, o explains.
  const guestSession = third.parts.session;
  guestSession.bubbles(guestSession.controls()).forEach(item => assert.deepEqual(guestSession.lockMenu(item), [], item.id));
  cell = bubbleHit(frame, "off");
  third.parts.controller.mouse({ action: "mousedown", button: "right", x: cell.x, y: cell.y });
  assert.equal(third.parts.controller.ui.overlay, null);
  third.parts.controller.key("o", { name: "o" });
  assert.equal(third.parts.controller.ui.overlay, null);
  assert.match(third.parts.controller.ui.toast.body, /Sign in as an administrator/);
  third.parts.controller.key("?", { name: "?" });
  assert(!/Lock menu/.test(panelText(third.parts.controller.frame(149, 40), "sheet:help")));
  third.parts.controller.key(null, { name: "escape" });
  assert(guestSession.remoteHidden() && !third.parts.controller.frame(149, 40).focusables.some(hit => hit.kind === "key"));
  proof.nonAdminNoMenu = true;
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

  // An unknown lock holds nothing back. While the first read is out the
  // remote says nothing and greys nothing, and a choice is sent; an
  // administrator's K waits for the lock to be known.
  const pending = gateway({ principal: admin });
  let answer; pending.policyGate = new Promise(resolve => { answer = resolve; });
  const fifth = assemble(pending, room), waiting = fifth.parts.session;
  await fifth.parts.controller.start(room);
  assert.equal(fifth.access.policyStatus(room), "checking");
  assert.equal(waiting.controlBlock(), null); assert.equal(waiting.lockNotice(), ""); assert.equal(waiting.roomLock().text, "");
  frame = fifth.parts.controller.frame(149, 40);
  waiting.bubbles(waiting.controls()).forEach(item => assert(!bubbleHit(frame, item.id).hit.disabled, item.id));
  assert.doesNotMatch(panelText(frame, "remote"), /Read-only|lock status/i);
  proof.checkingQuiet = true;
  const first = waiting.current().options[1].id;
  assert.equal(waiting.chooseDigit("1"), first);
  await settle(() => pending.commands.length);
  assert.deepEqual(pending.commands, [room + " source=" + first]);
  proof.checkingSends = true;
  fifth.parts.controller.key("k", { name: "k" });
  assert.equal(fifth.parts.controller.ui.toast.body, "Room lock status is not known yet.");
  assert.equal(pending.locks.length, 0);
  answer(); await settle(() => fifth.access.policyStatus(room) === "ready");
  assert.equal(waiting.roomLock().canManage, true);
  fifth.parts.controller.stop();
  proof.adminWaitsForLock = true;

  // A failed read past the grace forgets a known lock: the remote says the
  // lock is unknown, not Read-only, and sends. The server refuses a locked
  // room; the refusal says so and reads the lock again, which greys the room.
  const flaky = gateway({ principal: guest });
  const sixth = assemble(flaky, room, { policyGrace: 1 }), blind = sixth.parts.session;
  await sixth.parts.controller.start(room);
  await settle(() => sixth.access.policyStatus(room) === "ready");
  flaky.policies[room] = { ...flaky.policies[room], revision: 4, manual_lock: { desired: "off" },
    effective: { allowed: false, locked: true, reason: "manual_lock", desired: "off", until: null } };
  await sixth.access.loadPolicy(room);
  assert.match(blind.controlBlock(), /locked until an administrator unlocks it/);
  flaky.failPolicy = true; await pause();
  await assert.rejects(sixth.access.loadPolicy(room));
  assert.equal(sixth.access.policyStatus(room), "unavailable");
  assert.equal(blind.controlBlock(), null); assert.equal(blind.roomLock().locked, false);
  assert.equal(blind.lockNotice(), "Room lock status unknown"); assert.equal(blind.roomLock().text, "Room lock status unknown");
  frame = sixth.parts.controller.frame(149, 40);
  assert.match(panelText(frame, "remote"), /Room lock status unknown/); assert.doesNotMatch(panelText(frame, "remote"), /Read-only/);
  blind.bubbles(blind.controls()).forEach(item => assert(!bubbleHit(frame, item.id).hit.disabled, item.id));
  proof.unknownSaid = true;
  flaky.failPolicy = false; flaky.refuseCommands = true;
  const policyReads = () => flaky.calls.filter(call => call === "GET /api/v1/rooms/" + room + "/policy").length, readsBefore = policyReads();
  assert.equal(await blind.choose(first), null);
  assert.deepEqual(flaky.commands, [room + " source=" + first]);
  assert.equal(sixth.toasts.at(-1), "Could not choose: This room is locked.");
  proof.unknownSends = true;
  await settle(() => sixth.access.policyStatus(room) === "ready");
  assert(policyReads() > readsBefore, "a refusal reads the lock again");
  assert.match(blind.controlBlock(), /locked until an administrator unlocks it/);
  assert.equal(blind.lockNotice(), "");
  frame = sixth.parts.controller.frame(149, 40);
  assert(bubbleHit(frame, first).hit.disabled, "a known lock greys the room again");
  proof.refusalRereads = true;

  // A server that cannot evaluate the room's saved policy answers locked
  // (policy_unavailable) and refuses every command: controls are paused, and
  // the padlock and the tap say so rather than blaming a schedule.
  flaky.policies[room] = { ...flaky.policies[room], revision: 5, manual_lock: null,
    effective: { allowed: false, locked: true, reason: "policy_unavailable", desired: null, until: null } };
  await sixth.access.loadPolicy(room);
  assert.equal(blind.controlBlock(), "Room lock status is unavailable, so controls are paused.");
  assert.equal(blind.roomLock().text, "Room lock status unavailable · controls paused");
  const sent = flaky.commands.length;
  blind.tapBubble(bubble(blind, "off"));
  assert.equal(sixth.toasts.at(-1), "Read-only: Room lock status is unavailable, so controls are paused.");
  await assert.rejects(sixth.access.command(room, "source", "off"), error => error.code === "policy_unavailable");
  assert.equal(flaky.commands.length, sent);
  sixth.parts.controller.stop();
  proof.serverUnavailablePaused = true;

  // Room lists: another room's lock comes from the summary of every room, read
  // only while a list is on show; the terminal's room list and Home show ⚿
  // after its source and say the lock in the row's label.
  const lists = gateway({ principal: guest }), other = ROOMS[1];
  lists.summary[other] = { locked: true, manual: true, until: null, reason: "manual_lock" };
  const listed = assemble(lists, room), watched = [], watch = listed.access.watchLocks;
  listed.access.watchLocks = on => { watched.push(on); return watch(on); };
  await listed.parts.controller.start(room);
  await settle(() => listed.access.lockSummary(other) && listed.access.policyStatus(room) === "ready");
  const lister = listed.parts.session, otherLabel = lister.room(other).label, roomLabel = lister.room(room).label;
  assert(lists.calls.includes("GET /api/v1/locks"));
  assert.deepEqual(lister.roomLocked(other), { locked: true, text: "Locked · until unlocked" });
  assert.deepEqual(lister.roomLocked(room), { locked: false, text: "" });
  const rowOf = (text, label) => text.split("\n").find(line => line.includes(label));
  const hitOf = (kind, id) => frame.grid.hits.find(entry => entry.hit.kind === kind && entry.hit.room === id).hit.label;
  frame = listed.parts.controller.frame(149, 40);
  assert.match(rowOf(panelText(frame, "rooms"), otherLabel), /Off \u26bf/);
  assert.doesNotMatch(rowOf(panelText(frame, "rooms"), roomLabel), /\u26bf/);
  assert.equal(hitOf("room", other), otherLabel + ", Off. Locked · until unlocked");
  assert.equal(hitOf("room", room), roomLabel + ", Off");
  proof.terminalRoomList = true;
  listed.parts.controller.key(null, { name: "f1" });
  frame = listed.parts.controller.frame(149, 40);
  assert.match(rowOf(frame.grid.text(), otherLabel), /Off \u26bf/);
  assert.doesNotMatch(rowOf(frame.grid.text(), roomLabel), /\u26bf/);
  assert.equal(hitOf("home-room", other), otherLabel + ", Off. Locked · until unlocked. Open Remotes");
  assert.equal(hitOf("home-room", room), roomLabel + ", Off. Open Remotes");
  proof.terminalHome = true;
  listed.parts.controller.key(null, { name: "f3" });
  listed.parts.controller.key(null, { name: "f2" });
  assert.deepEqual(watched, [true, true, false, true], "Security shows no room list");
  proof.listsOnlyPoll = true;

  // The current room follows its detailed policy, as its remote does, over a
  // summary that disagrees; another room follows whichever was read last.
  lists.summary[room] = { locked: true, manual: true, until: null, reason: "manual_lock" };
  await listed.access.loadLocks();
  assert.deepEqual(lister.roomLocked(room), { locked: false, text: "" });
  lists.summary[room] = { locked: false, manual: false, until: null, reason: "unlocked" };
  lists.policies[room] = { ...lists.policies[room], revision: 3,
    effective: { allowed: false, locked: true, reason: "room_locked", desired: "off", until: new Date(Date.now() + 3600000).toISOString() } };
  await listed.access.loadLocks(); await pause(); await listed.access.loadPolicy(room);
  assert.match(lister.roomLock().text, /^Locked by schedule until /);
  assert.deepEqual(lister.roomLocked(room), { locked: true, text: lister.roomLock().text });
  lister.selectRoom(other);
  await settle(() => listed.access.policy(other));
  assert.deepEqual(lister.roomLocked(other), { locked: false, text: "" }, "the new current room follows its own policy");
  assert.equal(lister.roomLocked(room).locked, true, "a policy read since the summary");
  assert.match(lister.roomLocked(room).text, /^Locked by schedule until /);
  await pause(); await listed.access.loadLocks();
  assert.deepEqual(lister.roomLocked(room), { locked: false, text: "" }, "a summary read since the policy");
  proof.currentRoomPolicy = proof.fresherPolicy = true;

  // Browser Home, run as the page runs it: the padlock follows the reported
  // source, its label is the lock in words; an unlocked room shows none.
  const homeShared = Shared.load(), homeAccess = homeShared.Access.create({ base: origin, fetch: lists.fetch, policyInterval: 3600000 });
  const homeBackend = homeShared.Remote.create(homeShared.house, { backend: "openhab", base: origin, access: homeAccess,
    fetch: homeAccess.fetch, EventSource: homeAccess.eventSource(QuietEvents) });
  const tbody = new Element("tbody"), notice = new Element("p"), homeCalls = lists.calls.length;
  const page = { URLSearchParams, REMOTE_HOUSE: homeShared.house, RemoteSession: homeShared.Session, ThermiteAccess: { client: homeAccess },
    RemoteBackend: { create: () => homeBackend }, ThermiteShell: { href: () => "#" }, location: { search: "" }, addEventListener() {},
    document: { visibilityState: "visible", querySelector: selector => ({ "[data-home-rooms]": tbody, "[data-home-status]": notice })[selector] || null,
      createElement: tag => new Element(tag), createElementNS: (namespace, tag) => new Element(tag, namespace), addEventListener() {} } };
  vm.createContext(page); vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/remote/home.js"), "utf8"), page);
  await settle(() => tbody.children.some(row => row.hasAttribute("data-locked")));
  assert(lists.calls.slice(homeCalls).includes("GET /api/v1/locks"), "Home reads every room's lock");
  const homeRow = id => tbody.children[ROOMS.indexOf(id)], padlockOf = row => row.children[1].children[0].lastChild;
  assert(homeRow(other).hasAttribute("data-locked") && !homeRow(room).hasAttribute("data-locked"));
  assert.equal(padlockOf(homeRow(other)).getAttribute("aria-label"), "Locked · until unlocked");
  assert.equal(padlockOf(homeRow(other)).getAttribute("role"), "img");
  assert.equal(padlockOf(homeRow(other)).children[0].getAttribute("href"), "#i-lock");
  assert.equal(padlockOf(homeRow(room)).getAttribute("aria-label"), "");
  assert.equal(tbody.children.length, ROOMS.length, "no column or row added");
  proof.browserHome = true;
  homeBackend.stop();
  listed.parts.controller.stop();

  // The summary is read now and again every interval while the client is
  // active and a list watches it; a hidden page pauses it as it does a policy.
  const polled = gateway({ principal: guest }), wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const awaitReads = async condition => { for (const end = Date.now() + 2000; !condition() && Date.now() < end;) await wait(5); };
  const polling = Shared.load().Access.create({ base: origin, fetch: polled.fetch, policyInterval: 5 });
  const reads = () => polled.calls.filter(call => call === "GET /api/v1/locks").length;
  let lockNotices = 0;
  polling.subscribe(event => { if (event.type === "locks") lockNotices++; });
  await polling.ready(); await wait(20);
  assert.equal(reads(), 0, "nothing reads every room's lock until a list asks");
  await polling.watchLocks(true); await awaitReads(() => reads() >= 3);
  assert(reads() >= 3, "reads " + reads());
  assert.equal(lockNotices, 1, "an unchanged summary does not re-render");
  polling.setActive(false);
  let settled = reads(); await wait(40);
  assert.equal(reads(), settled, "a hidden page reads nothing");
  polling.setActive(true); await awaitReads(() => reads() > settled);
  assert(reads() > settled, "showing the page reads again");
  polling.watchLocks(false); settled = reads(); await wait(40);
  assert.equal(reads(), settled, "no list, no reads");
  polled.failLocks = true;
  await assert.rejects(polling.loadLocks());
  assert.deepEqual(polling.lockSummary(room), { locked: false, manual: false, until: null, reason: "unlocked" }, "one failed read keeps the last answer");
  polling.stop();
  proof.summaryPolled = proof.summaryPaused = true;

  process.stdout.write(JSON.stringify(proof));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
