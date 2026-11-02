/*
 * The page's own scripts in this process: web/remote/house.js, remotes.js,
 * backend.js, session.js and pair.js run here as classic scripts, as the
 * page runs them, so the terminal remote and the page share one house, one
 * set of remotes, one backend, one session and one pairing backend. Nothing
 * of them is copied.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "..", "web", "remote");
const SCRIPTS = ["access.js", "house.js", "remotes.js", "activity.js", "diagnostics.js", "backend.js", "session.js", "pair.js"];

function load(dir) {
  const from = dir || WEB;
  SCRIPTS.forEach((name) => {
    const generated = process.env.THERMITE_REMOTE_HOUSE_FILE || path.join(__dirname, "..", "..", "conf", "html", "remote", "house.js");
    const file = !dir && name === "house.js" && fs.existsSync(generated) ? generated : path.join(from, name);
    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
  });
  return {
    Access: globalThis.ThermiteAccess,
    house: globalThis.REMOTE_HOUSE,
    layouts: globalThis.REMOTE_LAYOUTS,
    Remote: globalThis.RemoteBackend,
    Session: globalThis.RemoteSession,
    Pairing: globalThis.PairingBackend
  };
}

module.exports = { WEB, SCRIPTS, load };
