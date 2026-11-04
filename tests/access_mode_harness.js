/* Exercise the browser/terminal authority boundary with finite fake gateway I/O. */
"use strict";
const assert = require("assert/strict"), fs = require("fs"), path = require("path"), vm = require("vm");
const Access = require("../web/remote/access"), Navigation = require("../web/remote/navigation");
const Administration = require("../tui/remote/administration"), { Grid } = require("../tui/remote/grid");
const tick = () => new Promise(resolve => setImmediate(resolve));
const origin = "https://site.example:13600", csrfValue = "C".repeat(32), csrfName = "thermite_0123456789abcdef_csrf";
const confirmation = "F".repeat(16);
const mode = (enabled, revision = 1) => ({ enabled, revision, bootstrap_required: !enabled, setup_available: false, csrf: csrfValue });
const guest = { id: 0, username: "Guest", admin: false, can_edit: false, is_guest: true, auth_enabled: false, permissions: ["site.read", "room.control", "camera.view"] };
const admin = { ...guest, id: 1, username: "Admin", admin: true, can_edit: true, is_guest: false, auth_enabled: true, permissions: [...guest.permissions, "settings.manage", "users.manage", "pairing.manage"] };
const response = (value, status = 200, cookies = []) => ({ ok: status >= 200 && status < 300, status, json: async () => value, headers: { getSetCookie: () => cookies } });
const house = { rooms: [{ id: "den", label: "Den", sources: [] }], sources: {} };
function textOf(node) { return [node.textContent || "", ...(node.children || []).map(textOf)].join(" "); }
class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.handlers = {}; this.attrs = {}; this.dataset = {}; this.textContent = ""; this.value = ""; }
  setAttribute(key, value) { this.attrs[key] = String(value); this[key === "class" ? "className" : key] = String(value); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  prepend(child) { this.children.unshift(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  get firstElementChild() { return this.children[0]; }
  get elements() { return Object.fromEntries(this.all().filter(child => child.name).map(child => [child.name, child])); }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  querySelectorAll(selector) { return this.all().filter(child => selector === 'input[type="password"]' ? child.tag === "input" && child.type === "password" : selector === "button" ? child.tag === "button" : selector === '[type="submit"]' ? child.type === "submit" : false); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.focused = true; }
}
async function browser(page, client) {
  const host = new Element(), status = new Element(), login = new Element("form"), title = new Element("h1"), hidden = new Element();
  const redirects = [], mapping = { "[data-security]": host, "[data-access-status]": status, "[data-login]": login, ".access-login h1": title };
  const context = { ThermiteAccess: { client }, ThermiteNavigation: Navigation, REMOTE_HOUSE: house, URLSearchParams,
    location: { origin, search: "", replace: value => redirects.push(value), reload: () => redirects.push("reload") },
    document: { body: { dataset: { thermitePage: page } }, createElement: tag => new Element(tag),
      querySelector: selector => mapping[selector], querySelectorAll: () => [hidden] } };
  vm.createContext(context); vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/remote/access-ui.js"), "utf8"), context);
  await tick(); return { host, status, login, title, hidden, redirects };
}
(async () => {
  const calls = []; let tokens = 0;
  const off = Access.create({ base: origin, getToken: () => { tokens++; throw Error("off mode must not read a token"); }, fetch: async (url, init) => {
    const route = new URL(url).pathname; calls.push({ route, init });
    if (route === "/api/v1/security") return response(mode(false), 200, ["arbitrary=value; Path=/", "thermite_0123456789abcdef_session=private; Path=/", csrfName + "=" + csrfValue + "; Path=/; Secure"]);
    if (route === "/api/v1/me") return response(guest);
    if (route === "/api/v1/rooms/den/commands") {
      assert.equal(init.headers.get("Cookie"), csrfName + "=" + csrfValue);
      assert.equal(init.headers.get("X-CSRFToken"), csrfValue);
      assert.equal(init.headers.get("Referer"), origin + "/");
      assert.equal(init.headers.get("Origin"), null); assert.equal(init.headers.get("Authorization"), null);
      return response({ ok: true });
    }
    throw Error("Unexpected route: " + route);
  } });
  assert.equal((await off.ready()).id, 0); assert.equal(tokens, 0); assert.equal(off.native, false); assert(off.canControl("den"));
  await off.command("den", "source", "off");
  await assert.rejects(off.loadPolicy("den"), error => error.code === "auth_disabled");
  await assert.rejects(off.json("/api/v1/users"), error => error.code === "auth_disabled");
  await off.watchRoom("den"); assert.equal(calls.length, 3, "no auth-only feature requests off");
  for (const id of ["settings", "users", "pairing", "system"]) assert(!Navigation.allowed(Navigation.pages.find(page => page.id === id), off), id);
  assert(Navigation.allowed(Navigation.pages.find(page => page.id === "security"), off));
  await assert.rejects(off.fetch("https://outside.example/rest/items"), error => error.code === "origin_mismatch");

  let attempts = 0;
  const unavailable = Access.create({ base: origin, fetch: async () => { attempts++; return response({ error: "security_unavailable" }, 503); } });
  await assert.rejects(unavailable.fetch(origin + "/rest/items"), error => error.code === "security_unavailable");
  assert.equal(attempts, 1); assert.equal(unavailable.native, false);
  const missing = Access.create({ base: origin, fetch: async () => response({}, 404) });
  await assert.rejects(missing.ready(), error => error.code === "access_not_configured");
  const guestCalls = [];
  const unsigned = Access.create({ base: origin, fetch: async (url, init) => {
    const route = new URL(url).pathname; guestCalls.push(route);
    assert.equal(init.headers.get("Authorization"), null);
    if (route === "/api/v1/security") return response(mode(true));
    if (route === "/api/v1/session") return response({ csrf: csrfValue });
    if (route === "/api/v1/me") return response({ ...guest, auth_enabled: true, permissions: ["site.read", "room.control"] });
    if (route.endsWith("/policy")) return response({ room: "den", revision: 0, manual_lock: null, can_lock: false,
      effective: { allowed: true, locked: false } });
    if (route.endsWith("/commands")) { assert.equal(init.headers.get("X-CSRFToken"), csrfValue); return response({ ok: true }); }
    throw Error("Unexpected guest route: " + route);
  } });
  assert.equal((await unsigned.ready()).is_guest, true);
  assert.equal(unsigned.canEdit(), false); assert.equal(unsigned.canControl("den"), false, "wait for current lock status");
  await unsigned.loadPolicy("den"); assert(unsigned.canControl("den"));
  await unsigned.command("den", "source", "off");
  await assert.rejects(unsigned.setRoomLock("den", 0, true), error => error.code === "forbidden");
  assert(!guestCalls.some(route => route.endsWith("/lock")), "guest cannot send a lock mutation");

  const personal = "thm_" + "T".repeat(32);
  const bearer = Access.create({ base: origin, token: personal, fetch: async (url, init) => {
    if (new URL(url).pathname === "/api/v1/security") { assert.equal(init.headers.get("Authorization"), null); return response(mode(true)); }
    assert.equal(init.headers.get("Authorization"), "Bearer " + personal);
    assert.equal(init.headers.get("Cookie"), null); assert.equal(init.headers.get("X-CSRFToken"), null);
    assert.equal(init.headers.get("Referer"), null); assert.equal(init.headers.get("Origin"), null);
    return response(admin);
  } });
  await bearer.ready(); assert(bearer.canEdit());
  const invalid = Access.create({ base: origin, token: personal, fetch: async (url, init) => {
    if (new URL(url).pathname === "/api/v1/security") { assert.equal(init.headers.get("Authorization"), null); return response(mode(false)); }
    assert.equal(init.headers.get("Authorization"), "Bearer " + personal); return response({ error: "invalid_token" }, 401);
  } });
  await assert.rejects(invalid.ready(), error => error.code === "invalid_token"); assert.equal(invalid.current(), null);

  let enabled = true, revision = 1, release; const events = [];
  const changing = Access.create({ base: origin, allowSession: true, fetch: async (url, init) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/security" && init.method === "POST") { enabled = false; revision++; return response(mode(enabled, revision)); }
    if (route === "/api/v1/security") return response(mode(enabled, revision));
    if (route === "/api/v1/session") return response({ csrf: csrfValue });
    if (route === "/api/v1/me") return response(enabled ? admin : guest);
    if (route.endsWith("/policy")) return response({ room: "den", revision: 1, effective: { allowed: false, locked: true }, can_edit: true });
    if (route.endsWith("/pending")) return new Promise(resolve => { release = resolve; });
    throw Error("Unexpected route: " + route);
  } });
  changing.subscribe(event => events.push(event.type)); await changing.ready(); await changing.loadPolicy("den");
  assert(!changing.canControl("den")); const pending = changing.fetch(origin + "/pending"); await tick();
  const transition = await changing.setSecurity({ enabled: false, revision: 1, password: confirmation });
  assert.equal(transition.enabled, false); assert.equal(changing.current(), null); assert.equal(changing.policy("den"), null);
  release(response({ obsolete: true })); await assert.rejects(pending, error => error.stale === true);
  assert(events.includes("unauthorized")); await changing.ready(); assert(changing.canControl("den"));
  enabled = true; revision++; await changing.loadSecurity(); assert.equal(changing.current(), null); assert.equal(changing.authEnabled(), true);

  let principalReply; const late = Access.create({ base: origin, allowSession: true, fetch: async (url, init) => {
    if (new URL(url).pathname === "/api/v1/security") return response(mode(init.method !== "POST", init.method === "POST" ? 2 : 1));
    if (new URL(url).pathname === "/api/v1/session") return response({ csrf: csrfValue });
    return new Promise(resolve => { principalReply = resolve; });
  } });
  const reading = late.ready(); await tick(); await late.setSecurity({ enabled: false, revision: 1, password: confirmation });
  principalReply(response(admin)); await assert.rejects(reading, error => error.stale === true); assert.equal(late.current(), null);

  const recoveryCalls = [];
  const expired = Access.create({ base: origin, allowSession: true, fetch: async (url, init) => {
    const route = new URL(url).pathname; recoveryCalls.push(route);
    if (route === "/api/v1/security") return response(mode(false));
    if (route === "/api/v1/me") return response({ error: "session_expired" }, 401);
    if (route === "/api/v1/session") return response({ csrf: csrfValue }, 200, [csrfName + "=" + csrfValue]);
    assert.equal(route, "/api/v1/logout"); assert.equal(init.headers.get("X-CSRFToken"), csrfValue); return response({ ok: true });
  } });
  await assert.rejects(expired.ready()); await expired.logout();
  assert.deepEqual(recoveryCalls, ["/api/v1/security", "/api/v1/me", "/api/v1/session", "/api/v1/logout"]);

  let submitted = null;
  const bootstrap = await browser("security", { native: false, loadSecurity: async () => mode(false),
    ready: () => { throw Error("public setup must not require a session"); }, setSecurity: async value => { submitted = value; return mode(true, 2); } });
  const form = bootstrap.host.children.find(child => child.tag === "form"); assert(form);
  assert.equal(form.elements.password.minLength, 12); assert.equal(form.elements.password.autocomplete, "new-password");
  assert(form.elements.setup_code.required); assert.equal(form.elements.confirm_password.type, "password");
  assert(textOf(bootstrap.host).includes("/path/to/access/config.json"));
  form.elements.username.value = "owner"; form.elements.password.value = "Q".repeat(16); form.elements.setup_code.value = "S".repeat(43);
  form.elements.confirm_password.value = "different"; await form.handlers.submit({ preventDefault() {} }); assert.equal(submitted, null);
  form.elements.confirm_password.value = form.elements.password.value; await form.handlers.submit({ preventDefault() {} });
  assert.equal(submitted.enabled, true); assert.equal(submitted.revision, 1); assert.equal(submitted.username, "owner"); assert.equal(submitted.setup_code.length, 43);
  assert.equal(form.elements.password.value, ""); assert.equal(form.elements.confirm_password.value, ""); assert.equal(form.elements.setup_code.value, "");
  assert.deepEqual(bootstrap.redirects, ["/account/?next=%2Fsecurity%2F"]);
  const reenable = await browser("security", { native: false, loadSecurity: async () => ({ ...mode(false, 3), bootstrap_required: false }) });
  const reenableForm = reenable.host.children.find(child => child.tag === "form");
  assert(reenableForm.elements.username); assert(!reenableForm.elements.setup_code); assert(!reenableForm.elements.confirm_password);
  assert.equal(reenableForm.elements.password.autocomplete, "current-password");
  const member = await browser("security", { native: false, loadSecurity: async () => mode(true), ready: async () => ({ ...admin, admin: false, can_edit: false }), canEdit: () => false, current: () => ({ username: "Member", is_guest: false }) });
  assert(!member.host.children.some(child => child.tag === "form")); assert(textOf(member.host).includes("Switch to an administrator account"));
  const settings = await browser("settings", off); assert(settings.status.textContent.includes("Enable authentication")); assert(settings.hidden.hidden);
  const native = await browser("security", { native: true }); assert(native.status.textContent.includes("Configure and open the thermite gateway"));
  const guestAccount = await browser("access", unsigned);
  assert(guestAccount.login.handlers.submit, "guest gets sign-in form for elevation");
  assert(!textOf(guestAccount.login).includes("Signed in as Guest"));
  const staleAccount = await browser("access", { native: false, loadSecurity: async () => mode(false), ready: async () => { throw Access.problem("session_expired", 401); }, logout: async () => {} });
  const clear = staleAccount.login.children.find(child => child.textContent === "Clear expired sign-in"); assert(clear);
  await clear.handlers.click(); assert.deepEqual(staleAccount.redirects, ["reload"]);

  const terminal = Administration.create(off, house, () => {}); await terminal.open("security");
  let grid = new Grid(100, null, "base"); terminal.content(grid, 100, { ui: {} });
  assert(grid.text().includes("Authentication is off")); assert(grid.text().includes(origin + "/security/")); assert(grid.text().includes("thermite access setup"));
  await terminal.open("settings", "den"); grid = new Grid(100, null, "base"); terminal.content(grid, 100, { ui: {} });
  assert(grid.text().includes("Enable authentication in Security")); assert.equal(terminal.state.form, null);
  const offlineTerminal = Administration.create(unavailable, house, () => {}); await offlineTerminal.open("account");
  grid = new Grid(100, null, "base"); offlineTerminal.content(grid, 100, { ui: {} });
  assert(grid.text().includes("Authentication status unavailable")); assert(!grid.text().includes("Authentication is off"));
  const guestTerminal = Administration.create(unsigned, house, () => {}); await guestTerminal.open("account");
  grid = new Grid(100, null, "base"); guestTerminal.content(grid, 100, { ui: {} });
  assert(grid.text().includes("Guest · no sign-in required"));
  assert(grid.text().includes("thermite access login"));

  // The plain-HTTP Guest listener: Guest only, no session or CSRF, sign-in on the HTTPS site.
  const plain = "http://198.51.100.2:13626", secure = "https://house.example.test:13600", plainCalls = [];
  const listener = Access.create({ base: plain, guest: true, secure, fetch: async (url, init) => {
    const route = new URL(url).pathname; plainCalls.push(route);
    if (route === "/api/v1/security") { const { csrf, ...status } = mode(true); return response(status); }
    if (route === "/api/v1/me") return response({ ...guest, auth_enabled: true, permissions: ["site.read", "room.control"] });
    if (route === "/api/v1/rooms/den/policy") return response({ room: "den", revision: 0, can_lock: false, effective: { allowed: true, locked: false } });
    if (route === "/api/v1/rooms/den/commands") {
      assert.equal(init.headers.get("Content-Type"), "application/json");
      assert.equal(init.headers.get("X-CSRFToken"), null); assert.equal(init.headers.get("Cookie"), null);
      assert.equal(init.headers.get("Authorization"), null);
      return response({ ok: true });
    }
    throw Error("Unexpected route on the Guest listener: " + route);
  } });
  assert.equal((await listener.ready()).is_guest, true); assert.equal(listener.guestOnly, true);
  assert.equal(listener.secureSite(), secure);
  await listener.loadPolicy("den"); await listener.command("den", "source", "off");
  await assert.rejects(listener.login("owner", "unused"), error => error.code === "secure_connection_required");
  assert.deepEqual(plainCalls, ["/api/v1/security", "/api/v1/me", "/api/v1/rooms/den/policy", "/api/v1/rooms/den/commands"]);
  assert.deepEqual(Navigation.pages.filter(page => Navigation.allowed(page, listener)).map(page => page.id), ["home", "remotes"]);
  assert.equal(Access.create({ base: plain, guest: true, secure: "http://house.example.test:13600", fetch: async () => response(guest) }).secureSite(), null);
  for (const value of ["javascript:alert(1)", secure + "/account/", "https://user@house.example.test:13600", secure + "?next=x", "", null])
    assert.equal(Access.secureOrigin(value), null, String(value));
  assert.equal(Access.secureOrigin(secure + "/"), secure);
  assert.equal(Access.create({ base: origin, guest: true, mock: true, fetch: async () => response(guest) }).guestOnly, false);
  process.stdout.write(JSON.stringify({ enabledGuest: true, guestGateway: true, terminalCsrf: true, noFallback: true, transitionFence: true, bootstrapForms: true, staleRecovery: true, terminalSecurity: true, guestListener: true }));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
