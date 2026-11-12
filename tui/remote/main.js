/*
 * bin/thermite remote's program: the house remote in a terminal. It runs
 * the page's scripts (shared.js), makes the backend the page would make
 * (the mock, or openHAB at --url over node's fetch and events.js), a
 * RemoteSession on a file store, the Pairing screen's PairingBackend, the
 * controller, and the terminal (app.js) that draws it.
 *
 *   node tui/remote/main.js --modules DIR (--url URL | --mock) [--state FILE]
 *     [--pair] [--room ID] [--choose SOURCE] [--mode dark|light]
 *     [--volume slider|buttons] [--still] [--log|--no-log] [--latency MS] [--matrix-down]
 *
 * DIR is the node_modules blessed is installed in (build/tui-remote). The
 * colors, and the torch, come from THERMITE_REMOTE_LOOK (theme.js); --still
 * or THERMITE_REMOTE_STILL stops what moves.
 */
"use strict";

const path = require("path");
const App = require("./app");
const Controller = require("./controller");
const Shared = require("./shared");
const Theme = require("./theme");
const { EventSource } = require("./events");
const { fileStore } = require("./store");
const Cameras = require("./cameras");
const Day = require("./day");
const Topology = require("./topology");
const Credentials = require("./credentials");
const System = require("../../web/remote/system");
const fs = require("fs");
const { spawn } = require("child_process");

function openBrowser(url) {
  const executable = [path.join(process.env.HOME || "", ".local/bin/xdg-open"), "/usr/bin/xdg-open"]
    .find((candidate) => { try { fs.accessSync(candidate, fs.constants.X_OK); return true; } catch (_) { return false; } });
  if (!executable) return Promise.resolve(false);
  return new Promise((resolve) => {
    const child = spawn(executable, [url], { stdio: "ignore" });
    child.once("error", () => resolve(false));
    child.once("exit", (code) => resolve(code === 0));
  });
}

const VALUES = ["modules", "state", "url", "room", "choose", "mode", "volume", "latency"];
const FLAGS = { mock: "mock", pair: "pair", "matrix-down": "matrixDown", still: "still", native: "native" };
const STILL = "THERMITE_REMOTE_STILL";
const CHOICES = { mode: ["dark", "light"], volume: ["slider", "buttons"] };

function parse(argv) {
  const args = { mock: false, pair: false, matrixDown: false, still: false };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index].startsWith("--") ? argv[index].slice(2) : "";
    if (name === "log" || name === "no-log") {
      const visible = name === "log";
      if (args.logVisible != null && args.logVisible !== visible) throw new Error("--log and --no-log are mutually exclusive");
      args.logVisible = visible;
    } else if (FLAGS[name]) args[FLAGS[name]] = true;
    else if (VALUES.includes(name) && index + 1 < argv.length) args[name] = argv[++index];
    else throw new Error("unknown argument: " + argv[index]);
  }
  if (args.mock && args.native) throw new Error("--native requires --url");
  if (args.mock === Boolean(args.url)) throw new Error("needs one of --url URL and --mock");
  Object.keys(CHOICES).forEach((name) => {
    if (args[name] != null && !CHOICES[name].includes(args[name])) {
      throw new Error("--" + name + " is one of " + CHOICES[name].join(", "));
    }
  });
  if (args.latency != null && !/^[0-9]+$/.test(args.latency)) throw new Error("--latency is a number of ms");
  if (!args.mock && (args.latency != null || args.matrixDown)) throw new Error("--latency and --matrix-down are for --mock");
  if (args.url) {
    const url = new URL(args.url);
    if (!/^https?:$/.test(url.protocol)) throw new Error("--url is an http or https URL");
    args.url = args.url.replace(/\/+$/, "");
  }
  return args;
}

function memoryStore() {
  const data = {};
  return { get: (key) => (key in data ? data[key] : null), set: (key, value) => { data[key] = value; } };
}

/*
 * Everything but the terminal: what main() runs, and what the tests drive.
 * env may hold shared (Shared.load()), store, look (Theme.load()), fetch,
 * EventSource, onChange, onQuit, environ (process.env, for
 * THERMITE_REMOTE_STILL) and the timers (now, setTimeout, clearTimeout,
 * setInterval, clearInterval), for the session and the controller alike.
 */
/* The gateway's command trail names this terminal tui/<host>: the host's
 * first DNS label, or tui/terminal when it has none usable. */
function clientLabel(hostname) {
  const host = String(hostname || "").split(".")[0].toLowerCase();
  return "tui/" + (/^[a-z0-9][a-z0-9-]{0,62}$/.test(host) ? host : "terminal");
}

function assemble(args, env) {
  const options = env || {};
  const shared = options.shared || Shared.load();
  const store = options.store || (args.state ? fileStore(args.state) : memoryStore());
  const Source = options.EventSource || EventSource;
  const environ = options.environ || process.env;
  const access = args.mock ? shared.Access.create({ mock: true, base: "http://127.0.0.1" }) : options.access || shared.Access.create({ base: args.url, fetch: options.fetch || globalThis.fetch,
    getToken: () => Credentials.read(environ.THERMITE_REMOTE_TOKEN_FILE, args.url)?.token || null, native: args.native === true,  // lint: allow — read private credential only after authoritative discovery
    client: clientLabel(options.hostname || require("os").hostname()) });
  const request = access && !access.native ? access.fetch : options.fetch;
  const Events = access && !access.native ? access.eventSource(Source) : Source;
  const timers = {};
  ["now", "setTimeout", "clearTimeout", "setInterval", "clearInterval"].forEach((name) => {
    if (options[name]) timers[name] = options[name];
  });
  const backend = shared.Remote.create(shared.house, args.mock
    ? { backend: "mock", latency: args.latency != null ? Number(args.latency) : undefined, answering: !args.matrixDown }
    : { backend: "openhab", base: args.url, access, fetch: request, EventSource: Events });
  const session = shared.Session.create(Object.assign({
    backend: backend, house: shared.house, layouts: shared.layouts, store: store, volume: args.volume
  }, timers));
  const createPairing = () => shared.Pairing.create(Object.assign({
    backend: args.mock ? "mock" : "openhab", base: args.url || "", fetch: request, EventSource: Events, location: null,
    native: !access || access.native === true
  }, timers.setTimeout ? { setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout } : {},
  timers.now ? { now: timers.now } : {}));
  const controller = Controller.create(Object.assign({
    session: session, Session: shared.Session, Pairing: shared.Pairing, Remote: shared.Remote, access,
    layouts: shared.layouts, house: shared.house, store: store, backend: backend, createPairing: createPairing,
    createCameras: () => Cameras.create({ base: args.url || "", mock: args.mock, fetch: request, ...timers }),
    createTopology: () => Topology.create({ base: args.url || "", mock: args.mock, fetch: request,
      manifest: options.topologyManifest }),
    // Room history is the gateway's: none through --native.
    createDay: (changed) => Day.create({ mock: args.mock, house: shared.house, changed,
      request: options.dayRequest || (access && !access.native && !args.mock
        ? (target, init) => access.json(target, "GET", undefined, init) : null),
      ...timers }),
    system: System.create({ access, mock: args.mock, base: args.url || "http://localhost", fetch: request || globalThis.fetch,
      changed: options.onChange, ...(options.systemManifest ? { manifest: options.systemManifest } : {}),
      ...(options.setTimeout ? { setTimeout: options.setTimeout } : {}), ...(options.clearTimeout ? { clearTimeout: options.clearTimeout } : {}) }),
    openSystem: url => Promise.resolve((options.openURL || openBrowser)(url)).then(opened => ({ url, opened })),
    openTopology: (room, source) => {
      const url = Topology.url(args.url, room, source, args.mock, args.native);
      return Promise.resolve((options.openURL || openBrowser)(url)).then((opened) => ({ url, opened }));
    },
    openCamera: (ident) => {
      const url = Cameras.url(args.url, ident, args.mock, args.native);
      return Promise.resolve((options.openURL || openBrowser)(url)).then((opened) => ({ url, opened }));
    },
    target: args.mock ? { label: "mock", mock: true } : { label: new URL(args.url).host, mock: false },
    // A plain launch is the house landing page. Explicit remote targets and
    // --pair remain useful direct entries; tests can select a starting page.
    screen: args.pair ? "pairing" : options.screen || (args.room || args.choose ? "remote" : "home"),
    theme: args.mode,
    // For this run, as --mode is; otherwise the a key's choice, kept in the store.
    still: args.still || Boolean((options.environ || process.env)[STILL]) || null,
    logVisible: args.logVisible,
    look: options.look || Theme.load(),
    onChange: options.onChange,
    onQuit: options.onQuit
  }, timers));
  return { access, shared: shared, store: store, backend: backend, session: session, controller: controller };
}

function main(argv) {
  let args;
  try {
    args = parse(argv);
  } catch (error) {
    process.stderr.write("thermite remote: " + error.message + "\n");
    return 2;
  }
  if (!args.modules) {
    process.stderr.write("thermite remote: needs --modules DIR, where blessed is installed\n");
    return 2;
  }
  const blessed = require(path.resolve(args.modules, "blessed"));
  // What the shared scripts write to the console would land on the screen.
  // Their warnings and errors are written out once the terminal is given
  // back; the mock's log of what it would have sent is dropped, the log
  // panel shows the same.
  const kept = [];
  ["warn", "error"].forEach((name) => {
    console[name] = (...parts) => kept.push(parts.map((part) => (part && part.stack) || String(part)).join(" "));
  });
  console.log = console.info = () => {};
  let app = null;
  let parts = null;
  let finishing = false;
  const finish = (status) => {
    // A second request, or a failure while finishing, exits at once.
    if (finishing) process.exit(status);
    finishing = true;
    if (parts) parts.controller.stop();
    if (parts && parts.backend.stop) parts.backend.stop();
    if (app) app.destroy();
    if (kept.length) process.stderr.write(kept.slice(-20).join("\n") + "\n");
    // What the remote gave up on since its last report gets a second to leave.
    const report = parts && parts.access && parts.access.flushTelemetry ? parts.access.flushTelemetry(true).catch(() => null) : null;
    Promise.race([report, new Promise((resolve) => setTimeout(resolve, 1000))]).then(() => process.exit(status));
  };
  process.on("uncaughtException", (error) => {
    kept.push(String((error && error.stack) || error));
    finish(1);
  });
  ["SIGTERM", "SIGINT", "SIGHUP"].forEach((signal) => process.on(signal, () => finish(0)));
  parts = assemble(args, {
    onChange: () => {
      if (app) app.schedule();
    },
    onQuit: () => finish(0)
  });
  app = App.run({ blessed: blessed, controller: parts.controller });
  parts.controller.start(args.room, args.choose);
  return null;
}

if (require.main === module) {
  const status = main(process.argv.slice(2));
  if (status !== null) process.exitCode = status;
}

module.exports = { parse, assemble, main, clientLabel, STILL };
