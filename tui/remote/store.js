/*
 * The terminal's localStorage: what the session keeps (remote.room,
 * remote.volume) and the theme (remote.theme), in one JSON file
 * (bin/thermite remote: var/tui-remote/state.json). A file that cannot be
 * read starts empty; one that cannot be written keeps the values in memory.
 */
"use strict";

const fs = require("fs");
const path = require("path");

function fileStore(file) {
  let data = {};
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed;
  } catch (error) {
    data = {};
  }
  return {
    get: (key) => (Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null),
    set: (key, value) => {
      data[key] = value;
      try {
        fs.mkdirSync(path.dirname(file), { recursive: true });
        const next = file + ".tmp";
        fs.writeFileSync(next, JSON.stringify(data, null, 2) + "\n");
        fs.renameSync(next, file);
      } catch (error) {
        // read-only or full: the value holds for this run
      }
    }
  };
}

module.exports = { fileStore };
