"use strict";
// The installer's terminal (tui/installer) against the real core
// (iso/installer.py serve) on a fake live system: every step from welcome to
// done, the core's refusals shown under their fields, the taken host name,
// Wi-Fi, the erase confirmation, progress, a failed run, the lock the
// console and SSH share, and every frame at several sizes in both modes.
// No blessed: frames are read as text, as tui/remote's tests read them.
//
//   node iso_installer_tui_harness.js PYTHON CORE_SCRIPT ROOT
//
// CORE_SCRIPT core ROOT SCENARIO serves one session on stdin/stdout.
const assert = require("node:assert/strict"), fs = require("node:fs"), { spawn } = require("node:child_process");
const Core = require("../tui/installer/core"), Controller = require("../tui/installer/controller");
const Theme = require("../tui/remote/theme");

const [python, script, root] = process.argv.slice(2);
const SECRETS = ["owner-secret-1", "root-secret-1", "wifi-secret-1"];
const SIZES = [[120, 40], [80, 24], [60, 20], [40, 12]];
const look = Theme.load({});
const known = new Set(Object.keys(look.modes.dark).concat(["bold", "underline"]));
const proof = { frames: 0, texts: {}, unknownStyles: [], leaked: [], overflow: [] };

function core(scenario) {
  const child = spawn(python, ["-B", script, "core", root, scenario], { stdio: ["pipe", "pipe", "inherit"] });
  return Core.create(child);
}

// Every frame drawn at every size and in both modes: what it shows, that its
// styles are the theme's and that no secret is ever on screen.
function look_at(controller, label) {
  let text = "";
  ["dark", "light"].forEach((mode) => {
    controller.ui.theme = mode;
    SIZES.forEach(([width, height]) => {
      const { grid } = controller.frame(width, height);
      proof.frames += 1;
      const shown = grid.text();
      if (grid.rows.length !== height || grid.rows.some((row) => row.length !== width)) proof.overflow.push(label);
      grid.rows.forEach((row) => row.forEach((cell) => {
        String(cell.style).split("+").forEach((name) => {
          if (!known.has(name) && !/^(meter\.\d+|torch\..*)$/.test(name)) proof.unknownStyles.push(label + ": " + name);
        });
      }));
      SECRETS.forEach((secret) => { if (shown.includes(secret)) proof.leaked.push(label); });
      if (width === 120) text = shown;
    });
  });
  controller.ui.theme = "dark";
  proof.texts[label] = text;
  return text;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
async function until(test, what) {
  for (let tries = 0; tries < 500; tries += 1) {
    if (test()) return;
    await settle();
  }
  throw new Error("timed out waiting for " + what);
}

function keys(controller) {
  const press = (name, extra) => controller.key(extra && extra.ch, Object.assign({ name, full: name }, extra || {}));
  return {
    press,
    type: (text) => Array.from(text).forEach((ch) => controller.key(ch, { name: ch === " " ? "space" : ch, full: ch })),
    clear: () => controller.key(null, { name: "u", full: "C-u", ctrl: true }),
    enter: async () => {
      press("enter");
      await settle();
      await until(() => !controller.ui.busy, "the core");
    }
  };
}

async function installs() {
  const quits = [];
  let quitTried = false;
  const controller = Controller.create({ core: core("installs"), look, label: "tty1", still: true, now: () => 0,
    onQuit: () => quits.push("quit"),
    onChange: () => {
      if (!controller || controller.step().id !== "install") return;
      if (!quitTried) {                              // Ctrl-C while installing
        quitTried = true;
        controller.key("c", { name: "c", full: "C-c", ctrl: true });
        proof.quitRefusedWhileInstalling = !quits.length && /cannot be stopped/.test(controller.ui.message.text);
      }
      if (controller.ui.progress && controller.ui.progress.phases.length > 2 && !proof.texts.install) {
        look_at(controller, "install");
      }
    } });
  const k = keys(controller), ui = controller.ui, step = () => controller.step().id;
  await controller.start();
  let text = look_at(controller, "welcome");
  proof.welcome = text.includes("thermite installer") && text.includes("Example CPU") && text.includes("sda 128 GB")
    && text.includes("192.0.2.10") && ui.answers.disk === "/dev/sda";

  await k.enter();
  proof.claimed = step() === "network";
  text = look_at(controller, "network");
  proof.network = text.includes("enp1s0") && text.includes("192.0.2.10") && text.includes("wlan0");
  k.press("r", { full: "C-r", ctrl: true });
  await until(() => ui.wifi.networks.length && !ui.busy, "a Wi-Fi scan");
  text = look_at(controller, "wifi");
  proof.wifiListed = text.includes("Example Home") && text.includes("Example Guest");
  await k.enter();                                   // Example Home needs its passphrase
  proof.wifiAsks = controller.ui.focus === 1;
  k.type(SECRETS[2]);
  look_at(controller, "wifi passphrase");
  await k.enter();
  proof.wifiJoined = ui.answers.wifi_ssid === "Example Home" && ui.message && ui.message.tone === "ok";
  look_at(controller, "wifi joined");
  await k.enter();                                   // Continue
  await until(() => ui.probe, "the host name probe");
  proof.hostnameSuggested = step() === "hostname" && ui.answers.hostname === "thermite-2";
  text = look_at(controller, "hostname");
  proof.hostnameSaysWhy = /thermite\.local[\s\S]*answers/.test(text) && text.includes("thermite-2");
  k.clear(); k.type("-bad-");
  await k.enter();
  text = look_at(controller, "hostname refused");
  proof.hostnameRefused = step() === "hostname" && Boolean(ui.errors.hostname) && text.includes(ui.errors.hostname.slice(0, 30));
  k.clear(); k.type("thermite");
  await k.enter();
  proof.takenWarns = step() === "hostname" && /already answers/.test(ui.message.text);
  look_at(controller, "hostname taken");
  await k.enter();
  proof.takenKept = step() === "owner" && ui.answers.hostname === "thermite";

  k.type("thermite"); k.press("tab"); k.type(SECRETS[0]); k.press("tab"); k.type("different");
  await k.enter(); await k.enter(); await k.enter();
  text = look_at(controller, "owner refused");
  proof.ownerRefused = step() === "owner" && /service account/.test(ui.errors.owner || "")
    && text.includes("service account");
  proof.passwordsDiffer = Boolean(ui.errors.owner_password) && text.includes(ui.errors.owner_password.slice(0, 30));
  proof.secretsMasked = !text.includes("different") && text.includes("•");
  ui.focus = 0; k.clear(); k.type("casey");
  ui.focus = 2; k.clear(); k.type(SECRETS[0]);
  ui.focus = 0;
  await k.enter(); await k.enter(); await k.enter();
  proof.ownerAccepted = step() === "root";

  k.type(SECRETS[0]); k.press("tab"); k.type(SECRETS[0]);
  await k.enter(); await k.enter();
  proof.rootMustDiffer = step() === "root" && Boolean(ui.errors.root_password);
  look_at(controller, "root refused");
  ui.focus = 0; k.clear(); k.type(SECRETS[1]); ui.focus = 1; k.clear(); k.type(SECRETS[1]);
  await k.enter();
  proof.rootAccepted = step() === "region";

  k.type("berl");
  text = look_at(controller, "region search");
  proof.searchFilters = text.includes("Europe/Berlin") && !text.includes("America/New_York");
  await k.enter();                                   // chooses, moves to the language
  proof.zoneChosen = ui.answers.timezone === "Europe/Berlin" && ui.focus === 1;
  look_at(controller, "region");
  await k.enter(); await k.enter();
  proof.regionAccepted = step() === "disk";

  text = look_at(controller, "disk");
  proof.diskReasons = text.includes("not offered: smaller than 32 GB")
    && text.includes("not offered: holds the installation medium");
  k.press("down");
  proof.onlyEligible = ui.answers.disk === "/dev/sda";
  await k.enter();
  k.press("down"); k.press("tab"); k.press("right"); k.press("left");
  look_at(controller, "system");
  proof.systemChoices = ui.answers.filesystem === "ext4" && ui.answers.kernel === "linux-lts";
  k.press("shift-tab", { full: "S-tab", shift: true }); k.press("up");
  await k.enter(); await k.enter();
  proof.systemAccepted = step() === "review" && ui.answers.filesystem === "btrfs";

  text = look_at(controller, "review");
  proof.reviewSummary = ["thermite", "casey", "Europe/Berlin", "/dev/sda", "Btrfs", "linux-lts", "Example Home", "serial EXAMPLE0001"]
    .every((part) => text.includes(part)) && /erase/i.test(text);
  k.type("sdb");
  await k.enter();
  proof.confirmRefused = step() === "review" && Boolean(ui.errors.confirm);
  look_at(controller, "review refused");
  k.clear(); k.type("sda");
  const seen = [];
  controller.ui.progress = null;
  k.press("enter");
  await until(() => step() === "done", "the end of the installation");
  seen.push(...ui.progress.phases.map((phase) => phase.percent));
  proof.progress = seen.length > 3 && seen.every((value, index) => !index || value >= seen[index - 1])
    && seen[seen.length - 1] === 100 && ui.progress.lines.some((line) => line.startsWith("Thermite:"));
  proof.installDrawn = /Installing|%/.test(proof.texts.install || "");
  text = look_at(controller, "done");
  proof.done = Boolean(ui.result) && /restart/i.test(text);
  const mac = ["02", "00", "00", "00", "00", "10"].join(":");  // the fixture's demo MAC, no literal
  proof.doneAddress = text.includes("thermite.local") && text.includes("192.0.2.10") && text.includes(mac + " (enp1s0");
  k.press("escape");
  proof.doneStays = step() === "done";
  await k.enter();
  proof.reboots = !ui.message;
  k.press("c", { full: "C-c", ctrl: true });
  proof.quits = quits.length === 1;
}

async function fails() {
  const controller = Controller.create({ core: core("fails"), look, label: "ssh", still: true, now: () => 0 });
  const k = keys(controller), ui = controller.ui;
  await controller.start();
  await k.enter();
  Object.assign(ui.answers, { hostname: "kitchen", owner: "casey", owner_password: SECRETS[0],
    owner_password_confirm: SECRETS[0], root_password: SECRETS[1], root_password_confirm: SECRETS[1],
    confirm: "sda" });
  ui.step = Controller.STEPS.findIndex((step) => step.id === "review");
  ui.focus = 0;
  k.press("enter");
  await until(() => controller.step().id === "done", "the failed installation");
  const text = look_at(controller, "failed");
  proof.failureShown = !ui.result && Boolean(ui.failure) && text.includes(ui.failure.slice(0, 30));
  k.press("escape");
  proof.failureReturnsToReview = controller.step().id === "review" && ui.answers.confirm === "";
}

// Wi-Fi beyond a listed WPA network: a hidden one, an open one installed
// end to end, and a review that checks the network the system is joined to.
async function wifi() {
  const live = core("wifi");
  const controller = Controller.create({ core: live, look, label: "tty1", still: true, now: () => 0 });
  const k = keys(controller), ui = controller.ui, step = () => controller.step().id;
  const reach = async (id) => {                     // System's two Enters lead to the review
    ui.step = Controller.STEPS.findIndex((entry) => entry.id === "system");
    ui.focus = 0;
    await k.enter(); await k.enter();
    await until(() => step() === id && !ui.busy, id);
    await settle();
  };
  await controller.start();
  await k.enter();
  k.press("r", { full: "C-r", ctrl: true });
  await until(() => ui.wifi.networks.length && !ui.busy, "a Wi-Fi scan");
  proof.spacedName = ui.wifi.networks.some((entry) => entry.ssid === "Example  Loft");
  ["down", "down", "down", "down"].forEach((name) => k.press(name));
  let text = look_at(controller, "wifi hidden");
  await k.enter();
  proof.hiddenAsksName = ui.focus === 1 && text.includes("A hidden network");
  k.type("Example Attic");
  look_at(controller, "wifi hidden name");
  await k.enter();
  k.type(SECRETS[2]);
  await k.enter();
  proof.hiddenJoined = ui.answers.wifi_ssid === "Example Attic" && ui.message.tone === "ok" && !ui.wifi.wifi_passphrase;
  ui.focus = 0;
  ["up", "up", "up"].forEach((name) => k.press(name));
  await k.enter();                                   // open: joined at once
  text = look_at(controller, "wifi open");
  proof.openJoined = ui.answers.wifi_ssid === "Example Guest" && ui.message.tone === "ok"
    && text.includes("joins Example Guest (open)");
  await k.enter();                                   // Continue
  proof.openContinues = step() === "hostname";
  Object.assign(ui.answers, { hostname: "kitchen", owner: "casey", owner_password: SECRETS[0],
    owner_password_confirm: SECRETS[0], root_password: SECRETS[1], root_password_confirm: SECRETS[1] });
  await reach("review");
  text = look_at(controller, "review wifi");
  proof.reviewShowsWifi = text.includes("Example Guest (open)");
  // The live system joins another network behind the review's back.
  await live.request("wifi-connect", { device: "wlan0", ssid: "Example Home", passphrase: SECRETS[2] });
  k.type("sda");
  await k.enter();
  text = look_at(controller, "review wifi changed");
  proof.reviewChecksWifi = step() === "review" && /joined to Example Home, not Example Guest/.test(ui.errors.wifi_ssid || "")
    && text.replace(/[│\s]+/g, " ").includes("Esc goes back to Network");
  while (step() !== "network") k.press("escape");
  await until(() => ui.network.wifi.ssid === "Example Home", "the network as it is now");
  ui.focus = 0; ui.wifi.cursor = 1;
  await k.enter();
  proof.wifiChangeable = ui.answers.wifi_ssid === "Example Guest";
  await reach("review");
  ui.answers.confirm = "";
  k.type("sda");
  k.press("enter");
  await until(() => step() === "done", "the installation over open Wi-Fi");
  proof.openInstalled = Boolean(ui.result);
}

async function locked() {
  const first = Controller.create({ core: core("shared"), look, label: "tty1", still: true });
  const second = Controller.create({ core: core("shared"), look, label: "ssh", still: true });
  await first.start(); await second.start();
  await keys(first).enter();
  await keys(second).enter();
  const text = look_at(second, "in use");
  proof.lockShared = first.step().id === "network" && second.step().id === "welcome"
    && /^In use by tty1 since /.test(second.ui.message.text) && text.includes("In use by tty1");
}

(async () => {
  await installs();
  await fails();
  await wifi();
  await locked();
  assert.ok(proof.frames > 100);
  fs.writeSync(1, JSON.stringify(proof));  // whole before exit, even to a pipe
  process.exit(0);
})().catch((error) => {
  const shown = Object.assign({}, proof, { texts: undefined });
  process.stderr.write(String(error && error.stack || error) + "\n" + JSON.stringify(shown) + "\n"
    + Object.entries(proof.texts).filter(([label]) => !process.env.SHOW || process.env.SHOW.split(",").includes(label)).map(([label, text]) => label + ":\n" + text).join("") + "\n");
  process.exit(1);
});
