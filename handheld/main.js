/*
 * thermite handheld serve's program: the bridge between handheld remotes and
 * the house. It runs the page's scripts (tui/remote/shared.js), makes the
 * backend the terminal remote makes (the mock, or the gateway at --url over
 * node's fetch and events.js, as a Guest or with the personal credential
 * THERMITE_REMOTE_TOKEN_FILE names), one shared MusicBackend when the house has
 * speakers, one RemoteSession (with RemoteListen) per remote enrolled in the
 * state file (handheld/state.js), and the bridge (handheld/bridge.js) between
 * them and the base station (handheld/base.js).
 *
 *   node handheld/main.js (--url URL | --mock) --state FILE
 *     [--port DEVICE | --listen PATH] [--by-id DIR] [--metrics-file PATH]
 *     [--latency MS] [--verbose]
 *
 * Without --port or --listen it uses the Espressif serial port (in
 * /dev/serial/by-id) that answers thermite hello as a base. It logs what it
 * does, briefly, on stderr.
 */
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Shared = require("../tui/remote/shared");
const Credentials = require("../tui/remote/credentials");
const { EventSource } = require("../tui/remote/events");
const Base = require("./base");
const Bridge = require("./bridge");
const State = require("./state");

const VALUES = ["url", "state", "port", "listen", "latency", "by-id", "metrics-file"];
const FLAGS = { mock: "mock", verbose: "verbose" };
const METRICS_EVERY_MS = 15000;

function parse(argv) {
  const args = { mock: false, verbose: false };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index].startsWith("--") ? argv[index].slice(2) : "";
    if (FLAGS[name]) args[FLAGS[name]] = true;
    else if (VALUES.includes(name) && index + 1 < argv.length) {
      const key = name === "by-id" ? "byId" : name === "metrics-file" ? "metricsFile" : name;
      args[key] = argv[++index];
    } else throw new Error("unknown argument: " + argv[index]);
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

/* The gateway's command trail names bridge-level requests handheld/<host>
 * (each enrolled remote stamps handheld/<name> via access.forClient). */
function clientLabel(hostname) {
  const host = String(hostname || "").split(".")[0].toLowerCase().replace(/[^a-z0-9._-]/g, "");
  return "handheld/" + (/^[a-z0-9][a-z0-9._-]{0,62}$/.test(host) ? host : "bridge");
}

function hasSpeakers(house) {
  return Bridge.hasSpeakers(house);
}

/* Strict finite-charset Prometheus label for a remote name (never a MAC or key). */
function sanitizeMetricLabel(name) {
  const clean = String(name || "").replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 64);
  return clean || "unknown";
}

function formatMetrics(baseConnected, remotes) {
  const lines = [
    "# HELP thermite_handheld_base_connected Whether the handheld base station is connected (1) or not (0).",
    "# TYPE thermite_handheld_base_connected gauge",
    "thermite_handheld_base_connected " + (baseConnected ? "1" : "0")
  ];
  const entries = (remotes || []).map((remote) => ({
    label: sanitizeMetricLabel(remote.name),
    status: remote.status || {},
    lastSeen: remote.lastSeen,
    rssi: remote.rssi
  })).sort((a, b) => (a.label < b.label ? -1 : a.label > b.label ? 1 : 0));

  const battery = [];
  const charging = [];
  const lastSeen = [];
  const rssi = [];
  const seenLabels = new Set();
  entries.forEach((entry) => {
    if (seenLabels.has(entry.label)) return;
    seenLabels.add(entry.label);
    const label = entry.label;
    if (Number.isInteger(entry.status.battery) && entry.status.battery >= 0 && entry.status.battery <= 100) {
      battery.push('thermite_handheld_remote_battery_percent{remote="' + label + '"} ' + entry.status.battery);
    }
    if (typeof entry.status.charging === "boolean") {
      charging.push('thermite_handheld_remote_charging{remote="' + label + '"} ' + (entry.status.charging ? "1" : "0"));
    }
    if (typeof entry.lastSeen === "number" && Number.isFinite(entry.lastSeen) && entry.lastSeen > 0) {
      lastSeen.push('thermite_handheld_remote_last_seen_seconds{remote="' + label + '"} ' + Math.floor(entry.lastSeen / 1000));
    }
    if (Number.isInteger(entry.rssi) && entry.rssi >= -127 && entry.rssi <= 20) {
      rssi.push('thermite_handheld_remote_rssi_dbm{remote="' + label + '"} ' + entry.rssi);
    }
  });
  if (battery.length) {
    lines.push(
      "# HELP thermite_handheld_remote_battery_percent Last reported battery percentage of an enrolled handheld remote.",
      "# TYPE thermite_handheld_remote_battery_percent gauge",
      ...battery
    );
  }
  if (charging.length) {
    lines.push(
      "# HELP thermite_handheld_remote_charging Whether an enrolled handheld remote reported charging (1) or not (0).",
      "# TYPE thermite_handheld_remote_charging gauge",
      ...charging
    );
  }
  if (lastSeen.length) {
    lines.push(
      "# HELP thermite_handheld_remote_last_seen_seconds Unix timestamp in seconds when an enrolled remote was last heard.",
      "# TYPE thermite_handheld_remote_last_seen_seconds gauge",
      ...lastSeen
    );
  }
  if (rssi.length) {
    lines.push(
      "# HELP thermite_handheld_remote_rssi_dbm Last reported radio RSSI in dBm for an enrolled handheld remote.",
      "# TYPE thermite_handheld_remote_rssi_dbm gauge",
      ...rssi
    );
  }
  return lines.join("\n") + "\n";
}

function writeMetricsFile(filePath, text) {
  const resolved = path.resolve(filePath);
  const directory = path.dirname(resolved);
  fs.mkdirSync(directory, { recursive: true });
  const temp = path.join(directory, "." + path.basename(resolved) + "." + process.pid + "."
    + crypto.randomBytes(6).toString("hex") + ".tmp");
  const fd = fs.openSync(temp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o644);
  try {
    fs.writeSync(fd, text);
    fs.fchmodSync(fd, 0o644);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fs.renameSync(temp, resolved);
  } catch (error) {
    try { fs.closeSync(fd); } catch (_) { /* closed */ }
    try { fs.unlinkSync(temp); } catch (_) { /* gone */ }
    throw error;
  }
}

/*
 * The backend and what the bridge needs, without the base: what main()
 * runs and the tests drive. env may hold shared, fetch, EventSource,
 * environ, hostname and the session timers.
 */
function assemble(args, env) {
  const options = env || {};
  const shared = options.shared || Shared.load();
  // The sample house is files of its own, run only for --mock.
  if (args.mock) Shared.mocks();
  const Source = options.EventSource || EventSource;
  const environ = options.environ || process.env;
  const access = args.mock ? shared.Access.create({ mock: true, base: "http://127.0.0.1" })
    : shared.Access.create({ base: args.url, fetch: options.fetch || globalThis.fetch,
      getToken: () => Credentials.read(environ.THERMITE_REMOTE_TOKEN_FILE, args.url)?.token || null,  // lint: allow — read private credential only after authoritative discovery
      client: clientLabel(options.hostname || os.hostname()) });
  const Events = access && !access.native ? access.eventSource(Source) : Source;
  const backend = shared.Remote.create(shared.house, args.mock
    ? { backend: "mock", latency: args.latency != null ? Number(args.latency) : undefined }
    : { backend: "openhab", base: args.url, access, fetch: access.fetch, EventSource: Events });
  const musicBackend = hasSpeakers(shared.house) && shared.MusicBackend
    ? shared.MusicBackend.create(args.mock
      ? { backend: "mock", latency: args.latency != null ? Number(args.latency) : undefined, house: shared.house }
      : { backend: "gateway", access, base: args.url, EventSource: Events })
    : null;
  return { shared, access, backend, musicBackend };
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
  const envMetrics = typeof process.env.THERMITE_HANDHELD_METRICS_FILE === "string"
    ? process.env.THERMITE_HANDHELD_METRICS_FILE.trim() : "";
  const metricsPath = args.metricsFile || envMetrics || null;
  const parts = assemble(args);
  const access = parts.access;
  let base = null;
  let baseConnected = false;
  let metricsTimer = null;
  let metricsFailed = false;
  let finishing = false;
  let stopAccess = () => {};
  let bridge = null;

  const flushMetrics = () => {
    if (!metricsPath || !bridge) return;
    try {
      writeMetricsFile(metricsPath, formatMetrics(baseConnected, bridge.remotes()));
      metricsFailed = false;
    } catch (error) {
      if (!metricsFailed) log("cannot write metrics " + metricsPath + ": " + error.message);
      metricsFailed = true;
    }
  };

  bridge = Bridge.create({
    shared: parts.shared, backend: parts.backend, musicBackend: parts.musicBackend, access, log,
    send: (mac, frame, done) => (base ? base.send(mac, frame, done) : setImmediate(() => (done || (() => {}))(false))),
    onStatus: (mac, status) => { state.status(mac, status); flushMetrics(); },
    onActivity: () => { if (metricsPath) flushMetrics(); }
  });
  const finish = (status) => {
    if (finishing) process.exit(status);
    finishing = true;
    if (metricsTimer) clearInterval(metricsTimer);
    metricsTimer = null;
    baseConnected = false;
    flushMetrics();
    stopAccess();
    bridge.remotes().map((remote) => remote.mac).forEach((mac) => bridge.remove(mac));
    if (base) base.close();
    state.close();
    if (parts.musicBackend && parts.musicBackend.stop) parts.musicBackend.stop();
    if (parts.backend.stop) parts.backend.stop();
    const report = access && access.flushTelemetry ? access.flushTelemetry(true).catch(() => null) : null;
    if (!report) {
      process.exit(status);
      return;
    }
    const timer = setTimeout(() => process.exit(status), 1000);
    if (timer.unref) timer.unref();
    Promise.resolve(report).finally(() => {
      clearTimeout(timer);
      process.exit(status);
    });
  };
  ["SIGTERM", "SIGINT", "SIGHUP"].forEach((signal) => process.on(signal, () => finish(0)));
  if (access && !access.native && access.subscribe) {
    stopAccess = access.subscribe((event) => {
      if (event.type !== "unauthorized") return;
      log(event.guest
        ? "access settings changed on the gateway (unauthorized); exiting"
        : "sign-in ended or credential rejected (unauthorized); exiting");
      finish(1);
    });
  }
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
    flushMetrics();
  };
  const start = () => {
    if (finishing) return;
    load(state.remotes());
    if (!enrolled.size) log("no remote is enrolled yet: thermite handheld enroll --name NAME");
    state.watch(load);
    if (metricsPath) {
      flushMetrics();
      metricsTimer = setInterval(flushMetrics, METRICS_EVERY_MS);
      if (metricsTimer.unref) metricsTimer.unref();
    }
    let unknown = 0;
    let said = "";
    base = Base.connect({
      port: args.port, listen: args.listen, byId: args.byId, log,
      onLink: (link) => {
        baseConnected = Boolean(link);
        flushMetrics();
      },
      onBase: (entry) => {
        state.setBase(entry);
        const text = "base " + entry.mac + " on channel " + entry.channel + ", firmware " + entry.fw;
        if (text !== said) log(text);
        said = text;
      },
      onFrame: (mac, frame, rssi) => {
        if (bridge.receive(mac, frame, rssi) || bridge.remote(mac)) return;
        // A frame from a remote not enrolled: said once in a while, not for each frame.
        if (unknown++ % 50 === 0) log("dropped a frame from " + mac + ", which is not enrolled");
      }
    });
    log("serving " + (args.mock ? "the mock house" : args.url) + " to " + enrolled.size + " remote" + (enrolled.size === 1 ? "" : "s"));
  };
  if (access && !access.native && access.ready) {
    access.ready().then(start, (error) => {
      if (finishing) return;
      if (error && error.status === 401) {
        log("authorization failed: " + error.message);
        finish(1);
        return;
      }
      log("gateway access check failed (" + ((error && error.message) || error) + "); continuing");
      start();
    });
  } else {
    start();
  }
  return null;
}

if (require.main === module) {
  const status = main(process.argv.slice(2));
  if (status !== null) process.exitCode = status;
}

module.exports = { parse, assemble, main, clientLabel, hasSpeakers, sanitizeMetricLabel, formatMetrics, writeMetricsFile };
