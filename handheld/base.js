/*
 * The base station's lines (handheld/PROTOCOL.md, Base station lines) over
 * whatever carries them: the base's USB serial port (a tty), or a Unix socket
 * a simulated base connects to. parse() reads one line; Link speaks them over
 * one connected stream; connect() keeps a Link up, reopening the tty (or
 * waiting for the next simulator) with a backoff when it goes away.
 *
 *   const base = connect({port | listen | probe: true, log, onBase, onFrame})
 *   base.send(mac, frame, done)   done(ok) once the base says sent ok or fail
 *   base.close()
 */
"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");
const tty = require("tty");
const { spawnSync } = require("child_process");

const PREFIX = "thermite ";
const MAC = /^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
// The longest line a base sends: rx with a 1470-byte frame, base64.
const LINE_MAX = 2200;
const ID_MAX = 65535;
// A send the base never answers counts as not delivered after this long.
const SENT_WAIT_MS = 5000;
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];
const PROBE_MS = 1500;
const BY_ID = "/dev/serial/by-id";
const ESPRESSIF = /espressif/i;

function integer(text, min, max) {
  if (!/^-?[0-9]{1,6}$/.test(text)) return null;
  const value = Number(text);
  return value >= min && value <= max ? value : null;
}

/*
 * One line, without its newline: {type: "base", protocol, fw, mac, channel},
 * {type: "sent", id, ok}, {type: "rx", mac, rssi, frame}, {type: "error",
 * words}, {type: "log", text} for a line that is not ours, or {type:
 * "malformed", line} for a thermite line this bridge cannot read.
 */
function parse(line) {
  const text = line.endsWith("\r") ? line.slice(0, -1) : line;
  if (!text.startsWith(PREFIX)) return { type: "log", text: text };
  const words = text.slice(PREFIX.length).split(" ");
  const bad = { type: "malformed", line: text };
  switch (words[0]) {
    case "base": {
      if (words.length !== 5) return bad;
      const protocol = integer(words[1], 0, 999);
      const channel = integer(words[4], 1, 13);
      if (protocol === null || channel === null || !MAC.test(words[3]) || !/^[\x21-\x7e]{1,32}$/.test(words[2])) return bad;
      return { type: "base", protocol: protocol, fw: words[2], mac: words[3], channel: channel };
    }
    case "sent": {
      const id = words.length === 3 ? integer(words[1], 1, ID_MAX) : null;
      if (id === null || (words[2] !== "ok" && words[2] !== "fail")) return bad;
      return { type: "sent", id: id, ok: words[2] === "ok" };
    }
    case "rx": {
      if (words.length !== 4 || !MAC.test(words[1]) || !BASE64.test(words[3]) || words[3].length % 4) return bad;
      const rssi = integer(words[2], -127, 20);
      if (rssi === null) return bad;
      const frame = Buffer.from(words[3], "base64");
      return frame.length ? { type: "rx", mac: words[1], rssi: rssi, frame: frame } : bad;
    }
    case "error":
      return { type: "error", words: words.slice(1).join(" ") };
    default:
      return bad;
  }
}

/*
 * The lines over one stream: send ids 1-65535, wrapping, each answered by
 * one sent; what the stream carries comes back through handlers.
 */
class Link {
  constructor(stream, handlers) {
    this.stream = stream;
    this.handlers = handlers || {};
    this.pending = new Map();
    this.next = 1;
    this.buffer = "";
    this.closed = false;
    this.sweep = setInterval(() => this.expire(), 1000);
    if (this.sweep.unref) this.sweep.unref();
    stream.setEncoding("latin1");
    stream.on("data", (chunk) => this.data(chunk));
    const end = (error) => this.close(error);
    stream.on("end", () => end(null));
    stream.on("close", () => end(null));
    stream.on("error", (error) => end(error));
    // A stream a probe paused flows again.
    stream.resume();
  }

  data(chunk) {
    this.buffer += chunk;
    let at;
    while ((at = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, at);
      this.buffer = this.buffer.slice(at + 1);
      if (line.length <= LINE_MAX) this.line(line);
      else this.handler("malformed", { type: "malformed", line: line.slice(0, 80) + "…" });
    }
    // A line that never ends is noise; keep only what could still be one.
    if (this.buffer.length > LINE_MAX) this.buffer = "";
  }

  line(line) {
    const entry = parse(line);
    if (entry.type === "sent") {
      const waiting = this.pending.get(entry.id);
      if (!waiting) return;
      this.pending.delete(entry.id);
      waiting.done(entry.ok);
      return;
    }
    this.handler(entry.type, entry);
  }

  handler(type, entry) {
    const handler = this.handlers[type];
    if (handler) handler(entry);
  }

  write(line) {
    if (this.closed) return false;
    this.stream.write(line + "\n");
    return true;
  }

  hello() { return this.write("thermite hello"); }

  channel(channel) { return this.write("thermite channel " + channel); }

  send(mac, frame, done) {
    const finish = done || (() => {});
    if (this.closed) {
      setImmediate(() => finish(false));
      return null;
    }
    let id = this.next;
    // Wrapping skips an id still waiting for its sent.
    for (let tries = 0; this.pending.has(id) && tries < ID_MAX; tries += 1) id = id % ID_MAX + 1;
    if (this.pending.has(id)) {
      setImmediate(() => finish(false));
      return null;
    }
    this.next = id % ID_MAX + 1;
    this.pending.set(id, { done: finish, at: Date.now() });
    this.write("thermite send " + id + " " + mac + " " + frame.toString("base64"));
    return id;
  }

  // Sends the base never answered, oldest first, are not delivered.
  expire() {
    const before = Date.now() - SENT_WAIT_MS;
    for (const [id, waiting] of this.pending) {
      if (waiting.at > before) break;
      this.pending.delete(id);
      waiting.done(false);
    }
  }

  close(error) {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.sweep);
    const waiting = Array.from(this.pending.values());
    this.pending.clear();
    waiting.forEach((entry) => entry.done(false));
    try {
      this.stream.destroy();
    } catch (_) {
      // already gone
    }
    this.handler("close", { error: error || null });
  }
}

/*
 * A tty opened for the base: one duplex stream, raw, without becoming our
 * controlling terminal. stty's raw also stops the tty turning each newline
 * we write into CR LF, which node's own raw mode keeps; without stty (not
 * Linux) the lines go out ending CR LF.
 */
function openTty(device) {
  const fd = fs.openSync(device, fs.constants.O_RDWR | fs.constants.O_NOCTTY);
  try {
    if (!tty.isatty(fd)) throw new Error(device + " is not a serial port");
    const stream = new tty.ReadStream(fd);
    const stty = spawnSync("stty", ["-F", device, "raw", "-echo"], { stdio: "ignore", timeout: 5000 });
    if (stty.status !== 0) stream.setRawMode(true);
    return stream;
  } catch (error) {
    try {
      fs.closeSync(fd);
    } catch (_) {
      // the stream owns it
    }
    throw error;
  }
}

/*
 * Espressif serial ports by their stable names, sorted, but those another
 * process of ours has open (thermite handheld enroll talking to a remote):
 * a probe would read the lines it waits for.
 */
function candidates(directory) {
  const where = directory || BY_ID;
  let names;
  try {
    names = fs.readdirSync(where).filter((name) => ESPRESSIF.test(name)).sort();
  } catch (_) {
    return [];
  }
  const busy = opened();
  return names.map((name) => path.join(where, name)).filter((device) => {
    try {
      return !busy.has(fs.realpathSync(device));
    } catch (_) {
      return false;
    }
  });
}

/* The files this user's other processes hold open, as /proc shows them. */
function opened() {
  const files = new Set();
  let processes;
  try {
    processes = fs.readdirSync("/proc").filter((name) => /^[0-9]+$/.test(name) && Number(name) !== process.pid);
  } catch (_) {
    return files;
  }
  processes.forEach((pid) => {
    let fds;
    try {
      fds = fs.readdirSync("/proc/" + pid + "/fd");
    } catch (_) {
      return;
    }
    fds.forEach((fd) => {
      try {
        files.add(fs.readlinkSync("/proc/" + pid + "/fd/" + fd));
      } catch (_) {
        // closed meanwhile
      }
    });
  });
  return files;
}

/*
 * Keeps one Link to the base up. options: port (a tty), listen (a Unix
 * socket path), or probe (the Espressif port that answers thermite base);
 * log(line), onBase(base line), onFrame(mac, frame, rssi), onLink(link|null);
 * byId (the directory probed), open (a tty opener, for tests).
 */
function connect(options) {
  const log = options.log || (() => {});
  const open = options.open || openTty;
  let link = null;
  let closed = false;
  let failures = 0;
  let timer = null;
  let server = null;
  let quiet = false;

  const handlers = {
    base: (entry) => {
      if (entry.protocol !== 1) {
        log("base " + entry.mac + " speaks protocol " + entry.protocol + "; this bridge speaks 1");
        return;
      }
      if (options.onBase) options.onBase(entry);
    },
    rx: (entry) => { if (options.onFrame) options.onFrame(entry.mac, entry.frame, entry.rssi); },
    error: (entry) => log("base refused a line: " + entry.words),
    malformed: (entry) => log("base sent a line this bridge cannot read: " + entry.line.slice(0, 120)),
    log: () => {}
  };

  function attach(stream, where) {
    const next = new Link(stream, Object.assign({}, handlers, {
      close: (entry) => {
        if (link === next) {
          link = null;
          if (options.onLink) options.onLink(null);
        }
        if (closed) return;
        log("base " + where + " closed" + (entry.error ? ": " + entry.error.message : ""));
        if (!server) retry();
      }
    }));
    if (link) link.close();
    link = next;
    failures = 0;
    quiet = false;
    log("base " + where + " connected");
    if (options.onLink) options.onLink(next);
    next.hello();
    return next;
  }

  function retry() {
    if (closed || timer) return;
    const wait = BACKOFF_MS[Math.min(failures, BACKOFF_MS.length - 1)];
    failures += 1;
    timer = setTimeout(() => { timer = null; attempt(); }, wait);
    if (timer.unref && options.unref) timer.unref();
  }

  function attempt() {
    if (closed) return;
    if (options.port) {
      try {
        attach(open(options.port), "on " + options.port);
      } catch (error) {
        if (!quiet) log("cannot open " + options.port + ": " + error.message + "; trying again");
        quiet = true;
        retry();
      }
      return;
    }
    probe(candidates(options.byId), open, PROBE_MS).then((found) => {
      if (closed) {
        if (found) found.stream.destroy();
        return;
      }
      if (!found) {
        if (!quiet) log("no base answers on " + (options.byId || BY_ID) + "/*Espressif*; plug one in, or pass --port");
        quiet = true;
        retry();
        return;
      }
      const next = attach(found.stream, "on " + found.device);
      // The base line that answered the probe.
      handlers.base(found.base);
      return next;
    });
  }

  if (options.listen) {
    try {
      if (fs.lstatSync(options.listen).isSocket()) fs.unlinkSync(options.listen);
    } catch (_) {
      // none there
    }
    server = net.createServer((socket) => attach(socket, "on " + options.listen));
    server.on("error", (error) => log("cannot listen on " + options.listen + ": " + error.message));
    server.listen(options.listen, () => {
      try {
        fs.chmodSync(options.listen, 0o600);
      } catch (_) {
        // the directory's permissions still hold
      }
      log("waiting for a simulated base on " + options.listen);
      if (options.onListening) options.onListening();
    });
  } else {
    attempt();
  }

  return {
    send(mac, frame, done) {
      if (!link) {
        setImmediate(() => (done || (() => {}))(false));
        return null;
      }
      return link.send(mac, frame, done);
    },
    channel(channel) { return link ? link.channel(channel) : false; },
    link: () => link,
    close() {
      closed = true;
      clearTimeout(timer);
      if (link) link.close();
      if (server) {
        server.close();
        try {
          fs.unlinkSync(options.listen);
        } catch (_) {
          // gone already
        }
      }
    }
  };
}

/*
 * The first of devices whose port answers thermite hello with a base line:
 * {device, stream, base}, the others closed again; null when none does.
 */
function probe(devices, open, wait) {
  const tryOne = (index) => {
    if (index >= devices.length) return Promise.resolve(null);
    const device = devices[index];
    let stream;
    try {
      stream = open(device);
    } catch (_) {
      return tryOne(index + 1);
    }
    return new Promise((resolve) => {
      let buffer = "";
      let done = false;
      const finish = (base) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        stream.removeListener("data", data);
        stream.removeListener("error", fail);
        if (base) {
          stream.pause();
          resolve({ device, stream, base });
        } else {
          stream.destroy();
          resolve(tryOne(index + 1));
        }
      };
      const data = (chunk) => {
        buffer += chunk;
        let at;
        while ((at = buffer.indexOf("\n")) >= 0) {
          const entry = parse(buffer.slice(0, at));
          buffer = buffer.slice(at + 1);
          if (entry.type === "base") return finish(entry);
        }
        if (buffer.length > LINE_MAX) buffer = "";
      };
      const fail = () => finish(null);
      const timer = setTimeout(fail, wait);
      stream.setEncoding("latin1");
      stream.on("data", data);
      stream.on("error", fail);
      stream.write("thermite hello\n");
    });
  };
  return tryOne(0);
}

module.exports = { parse, Link, connect, probe, candidates, openTty, LINE_MAX, ID_MAX, SENT_WAIT_MS, BACKOFF_MS };
