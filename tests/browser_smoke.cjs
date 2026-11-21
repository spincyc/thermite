#!/usr/bin/env node
/*
 * The pages of a generated house in a real browser, at a phone's and a
 * desktop's size, behind the gateway's Content-Security-Policy header:
 *
 *   node tests/browser_smoke.cjs SITE_DIR CSP [--shots DIR] [--pages a.html,b.html]
 *
 * SITE_DIR is conf/html/remote of `thermite generate`; CSP the header's
 * value (oh_identity.hardening). For every page and size it prints, as
 * JSON on stdout, what tests/browser_smoke.py asserts on:
 *
 *   frame     whether the page drew: the shell's bar or sidebar, and words
 *   csp       Content-Security-Policy violations the page caused
 *   errors    uncaught errors and console errors
 *   sideways  how far the page scrolls sideways, in px
 *   wide      boxes that scroll sideways without saying where more is (data-more)
 *   small     at a phone's size, controls a thumb is meant to hit that are under 44 px
 *   under     fixed boxes in sight that overlap the bar or sidebar without being
 *             above it; Remotes is first made to show a toast
 *   mocks     the sample's files (*-mock.js) the page downloaded
 *   states    on the phone, what the page shows once opened (Remotes' room
 *             information): whether it opened, and its small, wide, sideways
 *
 * and under "plain" the mocks of the page opened once more as a phone opens
 * it, without ?backend=mock: none.
 *
 * It drives the system's Chromium over the DevTools protocol with Node's
 * own WebSocket (Node 22 or later): no package is installed. Exit status 77
 * with the reason on stderr when there is no Chromium or no WebSocket. The
 * pages are opened with the sample backend (?backend=mock) from a server of
 * this process on loopback; the browser resolves no other host, and nothing
 * but GET and HEAD is served.
 *
 * Bounded: every wait has a deadline, each page's script heap is capped, the
 * browser is closed, and killed if it does not go, when the run ends however
 * it ends, and the whole run gives up after RUN_MS.
 *
 * THERMITE_CHROMIUM names the browser to use instead of the first of NAMES
 * on PATH; THERMITE_CHROMIUM_ARGS adds arguments (a container may need
 * --no-sandbox).
 */
"use strict";
const fs = require("node:fs"), http = require("node:http"), os = require("node:os"), path = require("node:path");
const { spawn } = require("node:child_process");

const RUN_MS = 120000, START_MS = 20000, CALL_MS = 10000, LOAD_MS = 15000, SETTLE_MS = 500, TOAST_MS = 300;
const SIZES = { phone: [390, 844], desktop: [1440, 900] };
// What a page shows only once something is opened, on the phone, where it
// is a sheet over the page: [name, what opens it, whether it is open]. Each
// is probed as the page was. The sample cannot show a room's lock menu, its
// Lock room button or a room's own controls: its demo account is given no
// lock to manage and its house has no room controls.
const STATES = { "index.html": [["info", "document.querySelector('#info-open').click()", "document.querySelector('#info-sheet').open === true"]] };
const STATE_MS = 400;
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".webmanifest": "application/manifest+json" };
const NAMES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"];

function chromium() {
  const named = process.env.THERMITE_CHROMIUM;
  if (named) return fs.existsSync(named) ? named : null;
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    for (const name of NAMES) {
      const file = path.join(dir, name);
      try { fs.accessSync(file, fs.constants.X_OK); return file; } catch (_) { /* not here */ }
    }
  }
  return null;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function within(promise, ms, what) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(what + " took over " + ms + " ms")), ms); })])
    .finally(() => clearTimeout(timer));
}

// The site as the gateway serves it: every answer carries its policy.
function serve(root, policy) {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    const name = url.pathname.startsWith("/static/remote/") ? url.pathname.slice("/static/remote/".length) : null;
    const file = name && /^[A-Za-z0-9._-]+$/.test(name) ? path.join(root, name) : null;
    // Each file asked for, so a look can say which of the sample's it downloaded.
    if (name) server.asked.push(name);
    const headers = { "Content-Security-Policy": policy, "Cache-Control": "no-store" };
    if (!["GET", "HEAD"].includes(request.method) || !file || !fs.existsSync(file)) {
      response.writeHead(404, Object.assign({ "Content-Type": "application/json" }, headers));
      response.end('{"error":"not_found"}');
      return;
    }
    response.writeHead(200, Object.assign({ "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" }, headers));
    response.end(request.method === "HEAD" ? undefined : fs.readFileSync(file));
  });
  server.asked = [];
  return new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", () => resolve(server)); });
}

// Chromium, headless, with a profile of its own; resolves to its DevTools address.
function launch(binary, profile) {
  const child = spawn(binary, ["--headless=new", "--remote-debugging-port=0", "--user-data-dir=" + profile, "--no-first-run",
    "--no-default-browser-check", "--disable-extensions", "--disable-background-networking", "--disable-component-update", "--disable-sync",
    "--disable-gpu", "--mute-audio", "--hide-scrollbars", "--js-flags=--max-old-space-size=256",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    ...(process.env.THERMITE_CHROMIUM_ARGS || "").split(" ").filter(Boolean), "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
  let first = "", recent = "";
  const address = new Promise((resolve, reject) => {
    child.stderr.on("data", (chunk) => {
      // A crash can print a long register dump after the startup error.
      if (first.length < 1600) first = (first + chunk).slice(0, 1600);
      recent = (recent + chunk).slice(-4000);
      const found = /DevTools listening on (ws:\/\/\S+)/.exec(recent);
      if (found) resolve(found[1]);
    });
    child.once("close", (code, signal) => reject(new Error(binary + " ended before it listened (" + (signal || "status " + code) + "): " + first.trim())));
    child.once("error", reject);
  });
  return { child, address: within(address, START_MS, "starting Chromium"), exited: new Promise((resolve) => child.once("exit", resolve)) };
}

// One DevTools connection: send(method, params, session) and the events of each session.
function devtools(address) {
  const socket = new WebSocket(address), pending = new Map(), heard = [];
  let next = 0;
  socket.addEventListener("message", (message) => {
    const data = JSON.parse(message.data);
    if (data.id !== undefined && pending.has(data.id)) {
      const { resolve, reject } = pending.get(data.id); pending.delete(data.id);
      if (data.error) reject(new Error(data.error.message)); else resolve(data.result);
    } else if (data.method) heard.forEach((listener) => listener(data));
  });
  const opened = new Promise((resolve, reject) => { socket.addEventListener("open", resolve); socket.addEventListener("error", () => reject(new Error("DevTools refused the connection"))); });
  return {
    opened: within(opened, CALL_MS, "connecting to DevTools"),
    send(method, params, sessionId) {
      const id = ++next;
      const answer = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
      socket.send(JSON.stringify(Object.assign({ id, method, params: params || {} }, sessionId ? { sessionId } : {})));
      return within(answer, CALL_MS, method).finally(() => pending.delete(id));
    },
    listen(listener) { heard.push(listener); return () => heard.splice(heard.indexOf(listener), 1); },
    close() { try { socket.close(); } catch (_) { /* already closed */ } },
  };
}

// What a page is asked once it has loaded. It runs in the page.
function probe() {
  const TARGET = 43.5;
  const shown = (element) => {
    const box = element.getBoundingClientRect(), style = getComputedStyle(element);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && !element.closest("[inert]");
  };
  const name = (element) => (element.innerText || element.getAttribute("aria-label") || element.getAttribute("title") || element.tagName)
    .replace(/\s+/g, " ").trim().slice(0, 32) + " <" + element.tagName.toLowerCase() + (element.className && typeof element.className === "string" ? "." + element.className.split(/\s+/)[0] : "") + ">";
  // A control a thumb is meant to hit; a link inside a sentence is read, not aimed at.
  const small = [];
  if (window.innerWidth < 820) {
    document.querySelectorAll("button, a[href], summary, select, input:not([type=hidden]), [role=button], [role=switch], [role=radio], [role=tab]").forEach((element) => {
      if (!shown(element) || element.disabled) return;
      if (element.tagName === "A" && getComputedStyle(element).display === "inline" && element.parentElement
        && element.parentElement.innerText.trim().length > element.innerText.trim().length) return;
      // A checkbox or radio is hit through its label.
      const hit = /^(checkbox|radio)$/.test(element.type || "") && element.closest("label") ? element.closest("label") : element;
      const box = hit.getBoundingClientRect();
      if (box.width < TARGET || box.height < TARGET) small.push(name(element) + " " + Math.round(box.width) + "x" + Math.round(box.height));
    });
  }
  // A box that scrolls sideways says where more is out of sight: its page keeps
  // data-more on it (left, right or both), and the styles draw the cue from that.
  const wide = [];
  document.querySelectorAll("body *").forEach((element) => {
    if (!shown(element) || element.scrollWidth <= element.clientWidth + 1) return;
    if (!/auto|scroll/.test(getComputedStyle(element).overflowX)) return;
    const more = (element.scrollLeft > 2 ? "left" : "") + (element.scrollLeft + element.clientWidth < element.scrollWidth - 2 ? "right" : "");
    if (element.getAttribute("data-more") !== (more === "leftright" ? "both" : more)) wide.push(name(element) + " " + element.scrollWidth + ">" + element.clientWidth);
  });
  // The shell (shell.css): a bar across the top under 1180 px, a sidebar from
  // there. Whatever is fixed where it is must be painted above it, as a toast
  // once was not.
  const under = [], bar = document.querySelector(".thermite-shell");
  if (bar && shown(bar)) {
    const top = bar.getBoundingClientRect(), level = Number(getComputedStyle(bar).zIndex) || 0;
    document.querySelectorAll("body *").forEach((element) => {
      const style = getComputedStyle(element);
      if (style.position !== "fixed" || style.visibility === "hidden" || Number(style.opacity) < 0.05 || bar.contains(element) || element.contains(bar)) return;
      const box = element.getBoundingClientRect();
      if (box.width > 0 && box.height > 0 && box.top < top.bottom && box.bottom > top.top && box.left < top.right && box.right > top.left
        && (Number(style.zIndex) || 0) <= level) under.push(name(element));
    });
  }
  const main = document.querySelector("main, .app");
  return { frame: Boolean(bar && shown(bar) && main && main.innerText.trim()), sideways: Math.max(0, document.scrollingElement.scrollWidth - window.innerWidth),
    small, wide, under, csp: window.__csp || [], toast: Boolean(document.querySelector(".toast.is-visible")) };
}

// One page at one size, asked for the sample house. Without `sample` it is
// opened as a phone opens it and only what it downloaded is told: there is
// no gateway behind it to draw anything from.
async function look(tools, server, page, size, shots, sample = true) {
  const origin = "http://127.0.0.1:" + server.address().port;
  const mocks = () => [...new Set(server.asked.filter((name) => name.endsWith("-mock.js")))].sort();
  const [width, height] = SIZES[size], phone = size === "phone";
  const { targetId } = await tools.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await tools.send("Target.attachToTarget", { targetId, flatten: true });
  const errors = [];
  let loaded;
  const fired = new Promise((resolve) => { loaded = resolve; });
  const stop = tools.listen((event) => {
    if (event.sessionId !== sessionId) return;
    if (event.method === "Page.loadEventFired") loaded();
    if (event.method === "Runtime.exceptionThrown") errors.push(String(event.params.exceptionDetails.exception?.description || event.params.exceptionDetails.text).slice(0, 200));
    if (event.method === "Log.entryAdded" && event.params.entry.level === "error") errors.push(String(event.params.entry.text).slice(0, 200));
  });
  try {
    for (const domain of ["Page", "Runtime", "Log"]) await tools.send(domain + ".enable", {}, sessionId);
    await tools.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: phone }, sessionId);
    await tools.send("Emulation.setTouchEmulationEnabled", phone ? { enabled: true, maxTouchPoints: 5 } : { enabled: false }, sessionId);
    await tools.send("Page.addScriptToEvaluateOnNewDocument", { source:
      "window.__csp = []; document.addEventListener('securitypolicyviolation', (event) => window.__csp.push(event.violatedDirective + ' ' + event.blockedURI));" }, sessionId);
    server.asked.length = 0;
    await tools.send("Page.navigate", { url: origin + "/static/remote/" + page + (sample ? "?backend=mock" : "") }, sessionId);
    await within(fired, LOAD_MS, "loading " + page);
    await wait(SETTLE_MS);
    if (!sample) return { mocks: mocks() };
    // A key no page acts on but Remotes, whose sample room is off: it answers
    // with a toast, which the probe then finds where the bar is or clear of it.
    for (const type of ["keyDown", "keyUp"]) await tools.send("Input.dispatchKeyEvent", { type, key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 }, sessionId);
    await wait(TOAST_MS);
    const answer = await tools.send("Runtime.evaluate", { expression: "(" + probe + ")()", returnByValue: true }, sessionId);
    if (answer.exceptionDetails) throw new Error("the probe failed on " + page + ": " + answer.exceptionDetails.text);
    if (shots) {
      const shot = await tools.send("Page.captureScreenshot", { format: "png" }, sessionId);
      fs.writeFileSync(path.join(shots, page.replace(/\.html$/, "") + "-" + size + ".png"), Buffer.from(shot.data, "base64"));
    }
    const states = {};
    for (const [name, open, opened] of phone ? STATES[page] || [] : []) {
      const run = (expression) => tools.send("Runtime.evaluate", { expression, returnByValue: true }, sessionId);
      await run(open);
      await wait(STATE_MS);
      const [shown, seen] = [await run(opened), await run("(" + probe + ")()")];
      if (seen.exceptionDetails) throw new Error("the probe failed on " + page + " with " + name + " open: " + seen.exceptionDetails.text);
      if (shots) {
        const shot = await tools.send("Page.captureScreenshot", { format: "png" }, sessionId);
        fs.writeFileSync(path.join(shots, page.replace(/\.html$/, "") + "-" + name + "-" + size + ".png"), Buffer.from(shot.data, "base64"));
      }
      const { small, wide, sideways } = seen.result.value;
      states[name] = { opened: shown.result?.value === true, small, wide, sideways };
    }
    return Object.assign(answer.result.value, { errors, mocks: mocks(), states });
  } finally {
    stop();
    await tools.send("Target.closeTarget", { targetId }).catch(() => {});
  }
}

async function main() {
  const [root, policy, ...more] = process.argv.slice(2);
  const option = (flag) => (more.includes(flag) ? more[more.indexOf(flag) + 1] : null);
  if (!root || !policy || !fs.existsSync(path.join(root, "index.html"))) { process.stderr.write("usage: browser_smoke.cjs SITE_DIR CSP [--shots DIR] [--pages a.html,b.html]\n"); return 2; }
  const binary = chromium();
  if (!binary) { process.stderr.write("no Chromium found: none of " + NAMES.join(", ") + " is on PATH and THERMITE_CHROMIUM names none\n"); return 77; }
  if (typeof WebSocket !== "function") { process.stderr.write("this Node has no WebSocket (Node 22 or later is needed)\n"); return 77; }
  const pages = option("--pages") ? option("--pages").split(",") : fs.readdirSync(root).filter((name) => name.endsWith(".html")).sort();
  const shots = option("--shots");
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "thermite-smoke-"));
  const server = await serve(path.resolve(root), policy);
  const browser = launch(binary, profile);
  let tools = null;
  // The browser first, then its profile: while it lives it writes there.
  const end = async () => {
    if (tools) tools.close();
    try { browser.child.kill("SIGKILL"); } catch (_) { /* gone */ }
    server.close(); server.closeAllConnections();
    await within(browser.exited, 3000, "ending Chromium").catch(() => {});
    try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3 }); } catch (_) { /* left in the temporary directory */ }
  };
  const giveUp = setTimeout(() => { process.stderr.write("the run took over " + RUN_MS + " ms\n"); end().finally(() => process.exit(1)); }, RUN_MS);
  try {
    tools = devtools(await browser.address);
    await tools.opened;
    const out = { browser: binary, pages: {} };
    for (const page of pages) {
      out.pages[page] = {};
      for (const size of Object.keys(SIZES)) out.pages[page][size] = await look(tools, server, page, size, shots);
      out.pages[page].plain = await look(tools, server, page, "phone", null, false);
    }
    // Asked to close, the browser takes its other processes with it.
    await tools.send("Browser.close").catch(() => {});
    await within(browser.exited, 3000, "closing Chromium").catch(() => {});
    process.stdout.write(JSON.stringify(out));
    return 0;
  } finally {
    clearTimeout(giveUp);
    await end();
  }
}

main().then((status) => { process.exitCode = status; }, (error) => { process.stderr.write(String(error.stack || error).slice(0, 2000) + "\n"); process.exitCode = 1; });
