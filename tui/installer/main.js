/*
 * The Thermite installer in a terminal: the live medium's console, or an
 * SSH login with the image's key. It starts the installer core
 * (iso/installer.py serve) and draws its steps with tui/remote's terminal
 * (app.js), theme and styles, as thermite tui looks.
 *
 *   node tui/installer/main.js --modules DIR --core FILE [--python FILE]
 *     [--label NAME] [--core-log FILE] [--still]
 *
 * DIR is the node_modules blessed is unpacked in; the colors and the torch
 * come from THERMITE_REMOTE_LOOK (tui/remote/theme.js).
 */
"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const App = require("../remote/app");
const Theme = require("../remote/theme");
const Core = require("./core");
const Controller = require("./controller");

const VALUES = ["modules", "core", "python", "label", "core-log"];

function parse(argv) {
  const args = { python: "python3", label: "console", still: false };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index].startsWith("--") ? argv[index].slice(2) : "";
    if (name === "still") args.still = true;
    else if (VALUES.includes(name) && index + 1 < argv.length) args[name] = argv[++index];
    else throw new Error("unknown argument: " + argv[index]);
  }
  if (!args.modules || !args.core) throw new Error("needs --modules DIR and --core FILE");
  return args;
}

function main(argv) {
  let args;
  try {
    args = parse(argv);
  } catch (error) {
    process.stderr.write("thermite installer: " + error.message + "\n");
    return 2;
  }
  const blessed = require(path.resolve(args.modules, "blessed"));
  const errors = args["core-log"] ? fs.openSync(args["core-log"], "a", 0o600) : "ignore";
  const child = spawn(args.python, ["-I", "-B", args.core, "serve"], { stdio: ["pipe", "pipe", errors] });
  const core = Core.create(child);
  let app = null;
  const finish = (status) => {
    if (app) app.destroy();
    core.stop();
    process.exit(status);
  };
  const controller = Controller.create({
    core: core, look: Theme.load(), label: args.label, still: args.still,
    onChange: () => { if (app) app.schedule(); }, onQuit: () => finish(0)
  });
  process.on("uncaughtException", (error) => {
    if (app) app.destroy();
    process.stderr.write(String((error && error.stack) || error) + "\n");
    process.exit(1);
  });
  ["SIGTERM", "SIGHUP"].forEach((signal) => process.on(signal, () => finish(0)));
  app = App.run({ blessed: blessed, controller: controller });
  controller.start();
  return null;
}

if (require.main === module) {
  const status = main(process.argv.slice(2));
  if (status !== null) process.exitCode = status;
}

module.exports = { parse, main };
