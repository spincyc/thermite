/*
 * The terminal's localStorage: what the session keeps (remote.room and
 * each mode's, remote.mode, remote.volume and each room's speaker),
 * Listen's (audio.device, music.speaker, music.tab) and the theme
 * (remote.theme), in one JSON file
 * (bin/thermite remote: var/tui-remote/state.json). A file that cannot be
 * read starts empty; one that cannot be written keeps the values in memory.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SAVE_DELAY_MS = 50;
let counter = 0;

function fileStore(file, options) {
  const delay = options && options.debounceMs != null ? Number(options.debounceMs) : SAVE_DELAY_MS;
  let data = {};
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed;
  } catch (error) {
    data = {};
  }
  let timer = null;
  let dirty = false;
  let detached = false;

  function writeNow() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (!dirty) return;
    dirty = false;
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      counter += 1;
      const next = file + "." + process.pid + "." + counter + ".tmp";
      fs.writeFileSync(next, JSON.stringify(data, null, 2) + "\n");
      fs.renameSync(next, file);
    } catch (error) {
      // read-only or full: the value holds for this run
    }
  }

  function onExit() {
    detached = true;
    writeNow();
  }

  process.once("exit", onExit);

  function flush() {
    writeNow();
  }

  function stop() {
    writeNow();
    if (!detached) {
      process.removeListener("exit", onExit);
      detached = true;
    }
  }

  return {
    get: (key) => (Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null),
    set: (key, value) => {
      if (Object.prototype.hasOwnProperty.call(data, key) && data[key] === value) return;
      data[key] = value;
      dirty = true;
      if (delay <= 0) {
        writeNow();
        return;
      }
      if (timer) clearTimeout(timer);
      timer = setTimeout(writeNow, delay);
      if (timer && typeof timer.unref === "function") timer.unref();
    },
    flush,
    stop
  };
}

module.exports = { fileStore };
