/*
 * The page's own scripts in this process: web/remote/house.js, remotes.js,
 * backend.js, audio-session.js, session.js, pair.js, music-backend.js,
 * music-session.js and listen-session.js run here as classic scripts, as
 * the page runs them, so the terminal remote and the page share one house,
 * one set of remotes, one backend, one session, one pairing backend and
 * one Listen over the device's own session and Music Assistant's. Nothing
 * of them is copied. The sample's files (*-mock.js) run only for --mock.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "..", "web", "remote");
const SCRIPTS = ["navigation.js", "access.js", "house.js", "dom.js", "sse.js", "hold.js", "alerts.js", "remotes.js", "activity.js", "diagnostics.js", "backend.js", "audio-session.js", "session.js", "pair.js",
  "music-backend.js", "music-session.js", "listen-session.js"];

// The sample house, its music, its pairing and its demo account
// (web/remote/*-mock.js) are run only where a mock is asked for: thermite
// tui --mock, and a test. The scripts above make their mocks of them when
// first asked, so these may run before or after load().
const MOCKS = ["access-mock.js", "alerts-mock.js", "backend-mock.js", "music-backend-mock.js", "pair-mock.js"];

function mocks() {
  MOCKS.forEach((name) => {
    const file = path.join(WEB, name);
    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
  });
}

function load(dir) {
  const from = dir || WEB;
  const explicit = process.env.THERMITE_REMOTE_HOUSE_FILE;
  if (!dir && explicit && !fs.existsSync(explicit)) throw new Error("The selected site's remote model is missing. Run thermite generate or reopen the remote.");
  SCRIPTS.forEach((name) => {
    const generated = explicit || path.join(__dirname, "..", "..", "conf", "html", "remote", "house.js");
    const file = !dir && name === "house.js" && fs.existsSync(generated) ? generated : path.join(from, name);
    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
  });
  const layouts = globalThis.RemoteLayouts ? globalThis.RemoteLayouts.forHouse(globalThis.REMOTE_HOUSE) : globalThis.REMOTE_LAYOUTS;
  return {
    Access: globalThis.ThermiteAccess,
    Alerts: globalThis.RemoteAlerts,
    Dom: globalThis.ThermiteDom,
    Sse: globalThis.ThermiteSse,
    Hold: globalThis.ThermiteHold,
    Layouts: globalThis.RemoteLayouts,
    house: globalThis.REMOTE_HOUSE,
    layouts: layouts,
    Remote: globalThis.RemoteBackend,
    Audio: globalThis.RemoteAudio,
    Session: globalThis.RemoteSession,
    Pairing: globalThis.PairingBackend,
    Music: globalThis.RemoteMusic,
    Listen: globalThis.RemoteListen,
    MusicBackend: globalThis.MusicBackend
  };
}

module.exports = { WEB, SCRIPTS, MOCKS, load, mocks };
