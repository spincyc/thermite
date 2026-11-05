/*
 * thermite handheld serve's program: the bridge between handheld remotes and
 * the house. It runs the page's scripts (tui/remote/shared.js), makes the
 * backend the terminal remote makes (the mock, or the gateway at --url over
 * node's fetch and events.js, as a Guest or with the personal credential
 * THERMITE_REMOTE_TOKEN_FILE names), one RemoteSession per remote enrolled in
 * the state file (handheld/state.js), and the bridge (handheld/bridge.js)
 * between them and the base station (handheld/base.js).
 *
 *   node handheld/main.js (--url URL | --mock) --state FILE
 *     [--port DEVICE | --listen PATH] [--by-id DIR] [--latency MS] [--verbose]
 *
 * Without --port or --listen it uses the Espressif serial port (in
 * /dev/serial/by-id) that answers thermite hello as a base. It logs what it
 * does, briefly, on stderr.
 */
"use strict";

const os = require("os");
const path = require("path");
const Shared = require("../tui/remote/shared");
const Credentials = require("../tui/remote/credentials");
const { EventSource } = require("../tui/remote/events");
const Base = require("./base");
const Bridge = require("./bridge");
const State = require("./state");

const VALUES = ["url", "state", "port", "listen", "latency", "by-id"];
const FLAGS = { mock: "mock", verbose: "verbose" };
// How often the lock of a room a remote shows, other than the one the
// shared access client watches, is read again.
const POLICY_EVERY_MS = 5000;

function parse(argv) {
  const args = { mock: false, verbose: false };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index].startsWith("--") ? argv[index].slice(2) : "";
    if (FLAGS[name]) args[FLAGS[name]] = true;
    else if (VALUES.includes(name) && index + 1 < argv.length) args[name === "by-id" ? "byId" : name] = argv[++index];
    else throw new Error("unknown argument: " + argv[index]);
  }
  if (args.mock === Boolean(args.url)) throw new Error("needs one of --url URL and --mock");
  if (!args.state) throw new Error("needs --state FILE");
  if (args.port && args.listen) throw new Error("--port and --listen are mutually exclusive");
  if (args.latency != null && (!args.mock || !/^[0-9]+$/.test(args.latency))) throw new Error("--latency MS is for --mock");
  if (args.url) {
    const url = new URL(args.url);
    if (!/^https?:$/.test(url.protocol)) throw new Error("--url is an http or https URL");
    args.url = args.url.replace(/\/+$/, "");
  }
  return args;
}

/* The gateway's command trail names this bridge tui/handheld.<host>: a
 * terminal client (the gateway knows only web/ and tui/ labels), on the
 * host's first DNS label. */
function clientLabel(hostname) {
  const host = String(hostname || "").split(".")[0].toLowerCase();
  return "tui/handheld" + (/^[a-z0-9][a-z0-9-]{0,50}$/.test(host) ? "." + host : "");
}

/*
 * The backend and what the bridge needs, without the base: what main()
 * runs and the tests drive. env may hold shared, fetch, EventSource,
 * environ, hostname and the session timers.
 */
function assemble(args, env) {
  const options = env || {};
  const shared = options.shared || Shared.load();
  const Source = options.EventSource || EventSource;
  const environ = options.environ || process.env;
  const access = args.mock ? shared.Access.create({ mock: true, base: "http://127.0.0.1" })
    : shared.Access.create({ base: args.url, fetch: options.fetch || globalThis.fetch,
      getToken: () => Credentials.read(environ.THERMITE_REMOTE_TOKEN_FILE, args.url)?.token || null,  // lint: allow — read private credential only after authoritative discovery
      client: clientLabel(options.hostname || os.hostname()) });
  const backend = shared.Remote.create(shared.house, args.mock
    ? { backend: "mock", latency: args.latency != null ? Number(args.latency) : undefined }
    : { backend: "openhab", base: args.url, access, fetch: access.fetch, EventSource: access.eventSource(Source) });
  return { shared, access, backend };
}

function main(argv) {
  let args;
  try {
    args = parse(argv);
  } catch (error) {
    process.stderr.write("thermite handheld: " + error.message + "\n");
    return 2;
  }
  const log = (line) => process.stderr.write("thermite handheld: " + line + "\n");
  // The mock's log of what it would have sent, with --verbose only.
  if (!args.verbose) console.log = console.info = () => {};
  let state;
  try {
    state = State.open(path.resolve(args.state), { log });
  } catch (error) {
    log(error.message);
    return 2;
  }
  const parts = assemble(args);
  let base = null;
  const bridge = Bridge.create({
    shared: parts.shared, backend: parts.backend, log,
    send: (mac, frame, done) => base.send(mac, frame, done),
    onStatus: (mac, status) => state.status(mac, status)
  });
  const enrolled = new Map();
  const load = (list) => {
    const wanted = new Map(list.map((entry) => [entry.mac, entry]));
    enrolled.forEach((entry, mac) => {
      const next = wanted.get(mac);
      if (next && next.key.equals(entry.key) && next.name === entry.name) return;
      bridge.remove(mac);
      enrolled.delete(mac);
      if (!next) log(entry.name + " (" + mac + ") forgotten");
    });
    wanted.forEach((entry, mac) => {
      if (enrolled.has(mac)) return;
      enrolled.set(mac, entry);
      bridge.add({ name: entry.name, mac, key: entry.key, store: state.store(mac, entry.store), status: entry.status });
      log(entry.name + " (" + mac + ") enrolled");
    });
  };
  load(state.remotes());
  if (!enrolled.size) log("no remote is enrolled yet: thermite handheld enroll --name NAME");
  state.watch(load);
  // The shared access client follows one room's lock; the others a remote shows are read too.
  const access = parts.access;
  const policies = setInterval(() => {
    if (!access || access.native || access.mock || !access.authEnabled()) return;
    const rooms = new Set(bridge.remotes().filter((remote) => remote.ready && remote.awake)
      .map((remote) => remote.session.current().id));
    rooms.forEach((room) => access.loadPolicy(room).catch(() => {}));
  }, POLICY_EVERY_MS);
  policies.unref();
  let unknown = 0;
  let said = "";
  base = Base.connect({
    port: args.port, listen: args.listen, byId: args.byId, log,
    onBase: (entry) => {
      state.setBase(entry);
      const text = "base " + entry.mac + " on channel " + entry.channel + ", firmware " + entry.fw;
      if (text !== said) log(text);
      said = text;
    },
    onFrame: (mac, frame) => {
      if (bridge.receive(mac, frame) || bridge.remote(mac)) return;
      // A frame from a remote not enrolled: said once in a while, not for each frame.
      if (unknown++ % 50 === 0) log("dropped a frame from " + mac + ", which is not enrolled");
    }
  });
  let finishing = false;
  const finish = (status) => {
    if (finishing) process.exit(status);
    finishing = true;
    base.close();
    state.close();
    if (parts.backend.stop) parts.backend.stop();
    process.exit(status);
  };
  ["SIGTERM", "SIGINT", "SIGHUP"].forEach((signal) => process.on(signal, () => finish(0)));
  log("serving " + (args.mock ? "the mock house" : args.url) + " to " + enrolled.size + " remote" + (enrolled.size === 1 ? "" : "s"));
  return null;
}

if (require.main === module) {
  const status = main(process.argv.slice(2));
  if (status !== null) process.exitCode = status;
}

module.exports = { parse, assemble, main, clientLabel };
