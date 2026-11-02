/* Real authenticated browser acceptance against a disposable dummy-house guest. */
"use strict";

const dns = require("dns").promises;
const fs = require("fs");
const net = require("net");
const path = require("path");

class CheckFailure extends Error {}
function check(condition, message) { if (!condition) throw new CheckFailure(message); }

function parse(argv) {
  const values = { admin: "admin", room: "example_west", source: "example_computer", timeout: 180 };
  const names = new Set(["url", "ca-file", "admin-password-file", "screenshot", "trust-helper", "admin", "room", "source", "timeout"]);
  const supplied = new Set();
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === "--help") return null;
    const name = argv[index].slice(2);
    check(argv[index].startsWith("--") && names.has(name) && !supplied.has(name)
      && index + 1 < argv.length && !argv[index + 1].startsWith("--"), "unknown, duplicate or incomplete browser argument");
    supplied.add(name); values[name] = argv[++index];
  }
  for (const name of ["url", "ca-file", "admin-password-file", "screenshot", "trust-helper"])
    check(typeof values[name] === "string" && values[name], "a required browser argument is missing");
  values.timeout = Number(values.timeout);
  check(Number.isFinite(values.timeout) && values.timeout > 0 && values.timeout <= 3600, "invalid browser timeout");
  check(/^[a-z][a-z0-9_]{0,79}$/.test(values.room) && /^[a-z][a-z0-9_]{0,79}$/.test(values.source)
    && values.source !== "off", "invalid dummy room or source id");
  check(typeof values.admin === "string" && values.admin.length > 0 && values.admin.length <= 150
    && !/[\x00-\x1f\x7f-\x9f]/.test(values.admin), "invalid administrator name");
  const url = new URL(values.url);
  check(url.protocol === "https:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash,
  "gateway must be an explicit HTTPS localhost origin");
  values.url = url.origin;
  check(path.isAbsolute(values["trust-helper"]), "trust helper must be an absolute path");
  for (const name of ["ca-file", "trust-helper"]) {
    values[name] = fs.realpathSync(values[name]);
    check(fs.statSync(values[name]).isFile(), "CA and trust helper must be regular files");
  }
  values["admin-password-file"] = path.resolve(values["admin-password-file"]);
  values.screenshot = path.resolve(values.screenshot);
  check(path.extname(values.screenshot).toLowerCase() === ".png", "screenshot must be a PNG under .scratch");
  // Runtime evidence belongs in ignored scratch state, never in a source tree.
  // Resolve the existing parent so a symlink cannot escape that destination.
  const parent = fs.realpathSync(path.dirname(values.screenshot));
  check(parent.split(path.sep).includes(".scratch"), "screenshot parent must already exist under .scratch");
  values.screenshot = path.join(parent, path.basename(values.screenshot));
  check(!fs.existsSync(values.screenshot), "screenshot destination must be new");
  check(process.env.NODE_TLS_REJECT_UNAUTHORIZED !== "0", "TLS verification must remain enabled");
  return values;
}

function passwordFile(filename) {
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const info = fs.fstatSync(descriptor);
    check(info.isFile() && info.uid === process.geteuid() && (info.mode & 0o777) === 0o600
      && info.nlink === 1 && info.size > 0 && info.size <= 4096, "administrator password file must be owned, regular, private and bounded");
    const buffer = Buffer.alloc(info.size + 1);
    try {
      const size = fs.readSync(descriptor, buffer, 0, buffer.length, 0);
      check(size === info.size, "administrator password file changed during reading");
      const value = buffer.subarray(0, size).toString("utf8").replace(/[\r\n]+$/, "");
      check(value.length >= 12 && value.length <= 1024 && !/[\r\n\u0000]/.test(value), "invalid administrator password file");
      return value;
    } finally { buffer.fill(0); }
  } finally { fs.closeSync(descriptor); }
}

async function run(args, state) {
  state.phase = "localhost resolution";
  const addresses = await dns.lookup(new URL(args.url).hostname.replace(/^\[|\]$/g, ""), { all: true });
  check(addresses.length && addresses.every(({ address }) => address === "::1"
    || net.isIPv4(address) && address.startsWith("127.")), "localhost resolved outside loopback");

  state.phase = "isolated browser launch";
  const helper = require(args["trust-helper"]);
  check(typeof helper.launchTrustedContext === "function", "trust helper must export launchTrustedContext");
  const context = state.context = await helper.launchTrustedContext(args["ca-file"], {
    ignoreHTTPSErrors: false, viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce",
    timeout: Math.min(args.timeout * 1000, 60000),
  });
  context.setDefaultTimeout(Math.min(args.timeout * 1000, 30000));
  context.setDefaultNavigationTimeout(Math.min(args.timeout * 1000, 30000));
  const page = await context.newPage();
  const commandPath = "/api/v1/rooms/" + args.room + "/commands";
  const sourceItem = "loc_" + args.room + "_source";
  const sourceTab = (source) => page.locator('[data-targets] [data-choose][data-device="' + source + '"]');
  const roomButton = () => page.locator('.panel--rooms [data-action="room"][data-room="' + args.room + '"]');
  const timeout = () => Math.max(1, Math.min(30000, state.deadline - Date.now()));

  async function nativeReadback() {
    // Leave time for the gateway's own five-second native request and HTTPS
    // overhead, while retaining the caller's overall acceptance deadline.
    const requestTimeout = Math.max(1, Math.min(30000, state.deadline - Date.now()));
    return page.evaluate(async ({ item, requestTimeout }) => {
      const response = await fetch("/rest/items/" + item + "/state", { cache: "no-store", signal: AbortSignal.timeout(requestTimeout) });
      if (!response.ok) return null;
      const value = (await response.text()).trim();
      return value.length <= 128 ? value : null;
    }, { item: sourceItem, requestTimeout });
  }
  async function confirmed(source) {
    await page.locator('[data-targets] [data-choose][data-device="' + source + '"][data-state="current"]').waitFor({ timeout: timeout() });
    const until = Math.min(state.deadline, Date.now() + 30000);
    do {
      if (await nativeReadback() === source) return;
      await new Promise((resolve) => setTimeout(resolve, 250));
    } while (Date.now() < until);
    throw new CheckFailure("native source readback did not confirm the browser choice");
  }
  async function choose(source) {
    const [response] = await Promise.all([
      page.waitForResponse((answer) => {
        const request = answer.request(), target = new URL(answer.url());
        if (target.origin !== args.url || target.pathname !== commandPath || request.method() !== "POST") return false;
        try { const value = request.postDataJSON(); return value.action === "source" && value.value === source; }
        catch (_) { return false; }
      }, { timeout: timeout() }),
      sourceTab(source).click(),
    ]);
    check(response.status() === 202 && (await response.json()).accepted === true, "browser source command was not accepted");
    await confirmed(source);
  }

  state.phase = "verified HTTPS login page";
  const loginResponse = await page.goto(args.url + "/static/remote/access.html", { waitUntil: "domcontentloaded" });
  check(loginResponse && loginResponse.status() === 200, "browser login page was unavailable");
  const tls = await loginResponse.securityDetails();
  check(tls && /^TLS 1\.[23]$/.test(tls.protocol), "browser did not report verified gateway TLS");
  await page.locator("[data-login]").waitFor();
  check(await page.evaluate(async () => {
    const client = globalThis.ThermiteAccess?.client;
    if (!client || client.native || client.mock) return false;
    // Join the same readiness promise as access-ui.js before submitting its
    // form. A 401 is expected in this new, isolated browser profile.
    try { await client.ready(); } catch (_) {}
    return true;
  }), "login page is not using authenticated access");

  state.phase = "administrator sign in";
  let password = passwordFile(args["admin-password-file"]); // lint: allow — reads a private file; no embedded credential
  try {
    await page.locator('[data-login] [name="username"]').fill(args.admin);
    await page.locator('[data-login] [name="password"]').fill(password);
    const [response] = await Promise.all([
      page.waitForResponse((answer) => new URL(answer.url()).origin === args.url
        && new URL(answer.url()).pathname === "/api/v1/session" && answer.request().method() === "POST"),
      page.locator('[data-login] button[type="submit"]').click(),
    ]);
    check(response.ok(), "administrator sign in was rejected");
  } finally { password = ""; }
  await page.waitForURL(args.url + "/static/remote/home.html");
  await page.locator('body[data-thermite-page="home"] [data-home-rooms] tr').first().waitFor();
  await page.waitForFunction((admin) => {
    const client = globalThis.ThermiteAccess?.client, user = client?.current();
    return globalThis.THERMITE_ACCESS_CONFIG?.mode === "authenticated" && !client.native && !client.mock
      && user?.username === admin && user.can_edit === true && client.has("users.manage") && client.has("room.control");
  }, args.admin, { timeout: timeout() });

  state.phase = "room navigation";
  await page.locator('.thermite-navigation [data-view="remotes"]').click();
  await page.locator('html.is-ready body[data-thermite-page="remotes"]').waitFor();
  check(await page.evaluate(({ room, source }) => {
    const house = globalThis.REMOTE_HOUSE, entry = house?.rooms.find((value) => value.id === room);
    return Boolean(entry && entry.sources.includes(source) && house.sources[source]);
  }, { room: args.room, source: args.source }), "selected dummy room or source is absent from the installed UI");
  await roomButton().click();
  check(await roomButton().getAttribute("aria-current") === "true", "browser did not select the requested room");
  await page.waitForFunction(({ room }) => globalThis.ThermiteAccess.client.policy(room)?.effective?.locked === false,
    { room: args.room }, { timeout: timeout() });

  state.phase = "source commands and native readback";
  // Establish Off before the source click: choosing a current source sends no
  // command. All mutations below therefore originate in genuine UI clicks.
  const initial = await nativeReadback();
  check(initial === "off" || initial === args.source, "dummy room has no recognized native source readback");
  if (initial !== "off") await choose("off");
  else await confirmed("off");
  await choose(args.source);
  await choose("off");
  await choose(args.source);

  state.phase = "administrator Users and roles page";
  await page.locator('.thermite-navigation [data-view="users"]').click();
  await page.locator('body[data-thermite-page="users"]').waitFor();
  await page.getByRole("heading", { name: "Users & roles", exact: true }).waitFor();
  await page.locator("[data-users] tbody tr").first().waitFor();
  await page.locator("[data-roles] tbody tr").first().waitFor();
  check(await page.evaluate((admin) => {
    const users = [...document.querySelectorAll("[data-users] tbody tr")].map((row) => [...row.querySelectorAll("td")].map((cell) => cell.textContent));
    const roles = [...document.querySelectorAll("[data-roles] tbody tr")].map((row) => [...row.querySelectorAll("td")].map((cell) => cell.textContent));
    return users.some((row) => row[0] === admin && row[1] === "Admin" && row[3] === "Active")
      && roles.some((row) => row[0] === "Admin" && row[1].includes("Manage users") && row[1].includes("Manage roles"));
  }, args.admin), "administrator Users and roles data was unavailable");
  await page.getByRole("button", { name: "Add user", exact: true }).waitFor();

  state.phase = "remote screenshot navigation";
  await page.locator('.thermite-navigation [data-view="remotes"]').click();
  await page.locator('html.is-ready body[data-thermite-page="remotes"]').waitFor();
  state.phase = "remote screenshot room selection";
  await roomButton().click();
  state.phase = "remote screenshot source confirmation";
  await confirmed(args.source);
  check(await page.locator('input[type="password"], [data-login], dialog[open]').count() === 0,
    "application screenshot is not on the populated remote view");
  state.phase = "remote screenshot font readiness";
  await page.evaluate(() => document.fonts.ready.then(() => true));
  // Screenshot bytes come from the actual app. No DOM replacement, masking,
  // synthetic state or timestamp fixture is used; the file stays in .scratch.
  state.phase = "remote PNG capture";
  const screenshot = await page.screenshot({ type: "png", fullPage: true, animations: "disabled" });
  state.phase = "private PNG save";
  fs.writeFileSync(args.screenshot, screenshot, { flag: "wx", mode: 0o600 });
  return { version: 1, passed: true, tls_verified: true, tls_protocol: tls.protocol,
    browser_version: context.browser().version(), admin_login: true, room_selected: true,
    source_command: true, off_command: true, native_readback: true, admin_users: true, screenshot: true };
}

async function main() {
  const state = { context: null, phase: "arguments", deadline: 0 };
  let watchdog;
  try {
    const args = parse(process.argv.slice(2));
    if (!args) {
      process.stdout.write("Usage: node check_browser.cjs --url HTTPS_LOCALHOST --ca-file CERT --admin-password-file FILE --screenshot SCRATCH.png --trust-helper ABSOLUTE_LAUNCH_CJS [--admin admin] [--room example_west] [--source example_computer] [--timeout 180]\n\nRuns on the external browser host. The helper must export async launchTrustedContext(caFile, options), preserve TLS verification and isolate CA trust. Screenshot parent must exist under .scratch; password file must be owned mode 0600. Only run against the disposable dummy house: source controls change its route.\n");
      return 0;
    }
    // Playwright debug logging can echo fill arguments. Disable it before the
    // external helper loads Playwright, and never print upstream exceptions.
    for (const name of ["DEBUG", "DEBUG_FILE", "PWDEBUG"]) delete process.env[name];
    for (const name of ["log", "info", "warn", "error"]) console[name] = () => {};
    state.deadline = Date.now() + args.timeout * 1000;
    watchdog = setTimeout(() => {
      process.stdout.write(JSON.stringify({ passed: false, error: "browser acceptance deadline expired" }) + "\n", () => process.exit(1));
    }, args.timeout * 1000);
    process.stdout.write(JSON.stringify(await run(args, state)) + "\n");
    return 0;
  } catch (error) {
    process.stdout.write(JSON.stringify({ passed: false, error: error instanceof CheckFailure
      ? error.message : "browser acceptance failed during " + state.phase }) + "\n");
    return 1;
  } finally {
    clearTimeout(watchdog);
    if (state.context) await state.context.close().catch(() => {});
  }
}

if (require.main === module) main().then((status) => {
  process.stdout.write("", () => process.exit(status));
});
