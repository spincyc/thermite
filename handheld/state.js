/*
 * The handheld state file (thermite handheld: ~/.local/state/thermite/handheld/
 * state.json): the enrolled remotes with their keys, each remote's own store
 * (what its session keeps: its last room), what each last reported of itself,
 * and the base's MAC and channel. Owner-only, mode 0600 in a 0700 directory,
 * as lib/oh_cli/handheld.py writes it. thermite handheld enroll and forget
 * change the remotes while the bridge runs, so the bridge writes back only
 * its own fields, onto what the file holds then, and reloads the remotes
 * when the file changes.
 *
 *   {"version": 1, "base": {"mac", "channel", "fw"} | null,
 *    "remotes": [{"name", "mac", "key" (64 hex digits), "board", "simulated"?,
 *                 "store": {"remote.room": ...}, "status": {"battery", "charging", "fw", "board"}}]}
 */
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const VERSION = 1;
const LIMIT = 1024 * 1024;
const MAC = /^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/;
const KEY = /^[0-9a-f]{64}$/;
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;
// What the bridge keeps of a remote's own report.
const KEPT = ["battery", "charging", "fw", "board"];
const WRITE_DELAY_MS = 500;

function empty() {
  return { version: VERSION, base: null, remotes: [] };
}

/* The file's data, {version, base, remotes}; empty when there is no file. Throws for one that is not private. */
function read(file) {
  let fd;
  try {
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  } catch (error) {
    if (error.code === "ENOENT") return empty();
    throw error;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || (stat.mode & 0o7777) !== 0o600 || (process.getuid && stat.uid !== process.getuid())
        || stat.size > LIMIT) {
      throw new Error(file + " must be a regular file of this user's, mode 0600");
    }
    const data = JSON.parse(fs.readFileSync(fd, "utf8"));
    if (!data || data.version !== VERSION || !Array.isArray(data.remotes)) throw new Error(file + " is not a handheld state file");
    return data;
  } finally {
    fs.closeSync(fd);
  }
}

/* The remotes the file holds that a bridge can use: {name, mac, key (Buffer), board, store, status}. */
function remotes(data) {
  const seen = new Set();
  return data.remotes.filter((entry) => entry && typeof entry.name === "string" && NAME.test(entry.name)
    && typeof entry.mac === "string" && MAC.test(entry.mac) && typeof entry.key === "string" && KEY.test(entry.key)
    && !seen.has(entry.mac) && seen.add(entry.mac))
    .map((entry) => ({
      name: entry.name, mac: entry.mac, key: Buffer.from(entry.key, "hex"), board: entry.board || "",
      store: entry.store && typeof entry.store === "object" && !Array.isArray(entry.store) ? entry.store : {},
      status: entry.status && typeof entry.status === "object" && !Array.isArray(entry.status) ? entry.status : {}
    }));
}

/* Replaces file with data, privately: a 0600 file in a 0700 directory, never half written. */
function write(file, data) {
  const directory = path.dirname(file);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || (stat.mode & 0o077) || (process.getuid && stat.uid !== process.getuid())) {
    throw new Error(directory + " must be a directory of this user's, mode 0700");
  }
  const next = path.join(directory, "." + path.basename(file) + "." + crypto.randomBytes(6).toString("hex"));
  const fd = fs.openSync(next, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  try {
    fs.writeSync(fd, JSON.stringify(data) + "\n");
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fs.renameSync(next, file);
  } catch (error) {
    try { fs.closeSync(fd); } catch (_) { /* closed */ }
    try { fs.unlinkSync(next); } catch (_) { /* gone */ }
    throw error;
  }
}

/*
 * The bridge's view of the file: each remote's store, the base, what each
 * remote reports, written back a moment after they change; and watch(),
 * which calls back with the remotes whenever someone else changes them.
 */
function open(file, options) {
  const log = (options && options.log) || (() => {});
  const stores = new Map();
  const statuses = new Map();
  let base = null;
  let timer = null;
  let failed = false;
  let watching = false;

  const data = read(file);
  base = data.base || null;

  function store(mac, initial) {
    const values = Object.assign({}, initial);
    stores.set(mac, values);
    return {
      get: (key) => (Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null),
      set: (key, value) => {
        if (values[key] === value) return;
        values[key] = value;
        later();
      }
    };
  }

  function status(mac, fields) {
    const kept = {};
    KEPT.forEach((name) => { if (fields[name] !== undefined) kept[name] = fields[name]; });
    const before = statuses.get(mac);
    if (before && JSON.stringify(before) === JSON.stringify(kept)) return;
    statuses.set(mac, kept);
    later();
  }

  function setBase(entry) {
    const next = { mac: entry.mac, channel: entry.channel, fw: entry.fw };
    if (base && base.mac === next.mac && base.channel === next.channel && base.fw === next.fw) return;
    base = next;
    later();
  }

  function later() {
    if (timer) return;
    timer = setTimeout(save, WRITE_DELAY_MS);
  }

  // Our fields onto what the file holds now: remotes enrolled or forgotten meanwhile stay so.
  function save() {
    timer = null;
    try {
      const current = read(file);
      current.base = base;
      current.remotes.forEach((entry) => {
        if (!entry || typeof entry.mac !== "string") return;
        if (stores.has(entry.mac)) entry.store = Object.assign({}, stores.get(entry.mac));
        if (statuses.has(entry.mac)) entry.status = Object.assign({}, entry.status, statuses.get(entry.mac));
      });
      write(file, current);
      failed = false;
    } catch (error) {
      if (!failed) log("cannot save " + file + ": " + error.message + "; keeping it in memory");
      failed = true;
    }
  }

  function watch(changed) {
    if (watching) return;
    watching = true;
    let last = signature();
    fs.watchFile(file, { interval: 1000, persistent: false }, () => {
      let next;
      try {
        next = read(file);
      } catch (error) {
        log("cannot read " + file + ": " + error.message);
        return;
      }
      const now = JSON.stringify(next.remotes.map((entry) => [entry.name, entry.mac, entry.key]));
      if (now === last) return;
      last = now;
      changed(remotes(next));
    });
  }

  function signature() {
    try {
      return JSON.stringify(read(file).remotes.map((entry) => [entry.name, entry.mac, entry.key]));
    } catch (_) {
      return "";
    }
  }

  return {
    remotes: () => remotes(data),
    base: () => base,
    store, status, setBase, watch,
    flush() {
      if (!timer) return;
      clearTimeout(timer);
      save();
    },
    close() {
      fs.unwatchFile(file);
      if (timer) {
        clearTimeout(timer);
        save();
      }
    }
  };
}

module.exports = { read, write, remotes, open, empty, VERSION, MAC, KEY, NAME };
