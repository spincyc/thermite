"use strict";
// The terminal remote's Audio screen (u from Remotes) on the Audio page's
// session (web/remote/audio-session.js) and the mock backend. argv: the
// mode, then two WiiM amplifiers and a room. "fixture": the model in
// THERMITE_REMOTE_HOUSE_FILE has both, the first the room's audio, so its
// lock is the room's; "plain": the public example, which has none. Prints
// what the screen drew and sent; nothing leaves the process.
const assert = require("node:assert/strict");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared");
console.log = console.info = () => {};
const settle = () => new Promise(resolve => setTimeout(resolve, 25));
const [mode, OWNED, FREE, ROOM] = process.argv.slice(2);
const WIDTH = 120, HEIGHT = 60;

// The remote on the mock, its device commands recorded and still taken.
function assemble(shared) {
  const parts = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), { shared, setInterval: () => 0, clearInterval() {} });
  const command = parts.backend.command.bind(parts.backend);
  parts.sent = [];
  parts.backend.command = (device, action, value) => { parts.sent.push([device, action, String(value)]); return command(device, action, value); };
  return parts;
}

const draw = (parts) => parts.controller.frame(WIDTH, HEIGHT);
const text = (parts) => draw(parts).grid.text();
async function key(parts, ch, event) {
  draw(parts);
  parts.controller.key(ch, event || { name: ch });
  await settle();
  return parts.controller.ui.screen;
}
// The arrows reach the control: up to the first, then down to it.
async function focusOn(parts, target) {
  for (let i = 0; i < 80; i++) { draw(parts); parts.controller.key(null, { name: "up" }); }
  for (let i = 0; i < 80 && parts.controller.ui.focus !== target; i++) { draw(parts); parts.controller.key(null, { name: "down" }); }
  draw(parts);
  assert.equal(parts.controller.ui.focus, target);
}
const kinds = (parts) => draw(parts).focusables.filter((hit) => hit.pane === "audio").map((hit) => hit.kind);

async function fixture(proof) {
  const parts = assemble();
  const { controller, session } = parts;
  await controller.start(); await settle();
  draw(parts);
  // u opens Audio from Remotes only; F6 stays with Remotes and F8 goes anywhere.
  proof.reach = { home: await key(parts, "u"), homeF6: await key(parts, null, { name: "f6" }), homeF8: await key(parts, null, { name: "f8" }) };
  proof.reach.usersU = await key(parts, "u");
  await key(parts, null, { name: "f3" }); proof.reach.security = await key(parts, "u");
  await key(parts, null, { name: "f4" }); proof.reach.cameras = await key(parts, "u");
  await key(parts, null, { name: "f2" });
  proof.reach.remoteF6 = await key(parts, null, { name: "f6" });
  proof.reach.pairingU = await key(parts, "u");
  proof.reach.pairingEsc = await key(parts, null, { name: "escape" });
  proof.reach.remoteCtrlP = await key(parts, "\u0010", { name: "p", ctrl: true });
  await key(parts, null, { name: "escape" });
  proof.reach.remoteF8 = await key(parts, null, { name: "f8" });
  await key(parts, null, { name: "f2" });
  proof.reach.remoteCtrlU = await key(parts, "\u0015", { name: "u", ctrl: true });
  proof.hint = text(parts).split("\n").slice(-1)[0].includes("u Audio");
  parts.controller.key("?", { name: "?" });
  proof.hint = proof.hint && text(parts).includes("Audio (Remotes)");
  await key(parts, null, { name: "escape" });
  // From a room whose audio device has a page, u opens on it.
  session.selectRoom(ROOM, true);
  proof.reach.remote = await key(parts, "u");
  proof.opened = controller.context().audio.session.selected();
  const audio = controller.context().audio.session;
  let shown = text(parts);
  proof.drawn = ["Sample Track", "Sample Artist · Sample Album", " Preview ", " 44.1 kHz · 16-bit ", " 4:05 ", "‖ Pause", "Volume",
    "+ 60", " Mute ", "● Wi-Fi", "○ Bluetooth", "Playback", "Play Mode", "In order ▾", "Sound", "EQ Preset", "Flat ▾",
    "Subwoofer Level", " −  0 dB  + ", "Device", "Status Light", "About this device", "Subwoofer Connected", "Yes"]
    .filter((words) => !shown.includes(words));
  proof.places = ["Example East", "Fixture Den"].every((place) => shown.includes(place));
  proof.kinds = [...new Set(kinds(parts))].sort();
  proof.focus = controller.ui.focus;
  // The page's keys, each read back by the mock.
  await key(parts, "+");
  await key(parts, "m");
  await key(parts, "p");
  shown = text(parts);
  proof.keys = { volume: shown.includes("+ 61"), muted: shown.includes("61 muted"), played: shown.includes("▶ Play") };
  await focusOn(parts, "audio-set:" + OWNED + "_eq");
  await key(parts, null, { name: "enter" });
  await focusOn(parts, "audio-set:" + OWNED + "_sub_level");
  await key(parts, null, { name: "right" }); await key(parts, null, { name: "right" }); await key(parts, null, { name: "left" });
  // Space on a level presses nothing, play/pause included.
  const pressed = parts.sent.length;
  await key(parts, null, { name: "space" });
  proof.spaceOnLevel = parts.sent.slice(pressed);
  await focusOn(parts, "audio-volume");
  await key(parts, null, { name: "left" });
  await focusOn(parts, "audio-set:" + OWNED + "_eq_preset");
  await key(parts, null, { name: "enter" });
  proof.sheet = { kind: controller.ui.overlay && controller.ui.overlay.kind, focus: controller.ui.focus, lists: text(parts).includes("Bass Booster") };
  await key(parts, null, { name: "down" });
  await key(parts, null, { name: "enter" });
  proof.sheet.closed = controller.ui.overlay === null;
  proof.sheet.focusBack = controller.ui.focus;
  shown = text(parts);
  proof.keys.set = { eq: /EQ +on/.test(shown), level: shown.includes(" 1 dB "), preset: shown.includes("Acoustic ▾"), volume: shown.includes("+ 60") };
  await focusOn(parts, "audio-option:input:bluetooth");
  await key(parts, null, { name: "space" });
  proof.keys.set.input = text(parts).includes("● Bluetooth");
  // Play Mode shows only on Wi-Fi.
  proof.keys.set.whenHides = !text(parts).includes("Play Mode");
  // ] the next device: a short choice is a row of its options.
  await key(parts, "]");
  proof.next = audio.selected();
  shown = text(parts);
  proof.nextDrawn = shown.includes("Fixture Den · Amp Pro") && shown.includes("○ Repeat all");
  await focusOn(parts, "audio-option:" + FREE + "_play_mode:0");
  await key(parts, null, { name: "space" });
  proof.nextChose = text(parts).includes("● Repeat all");
  await key(parts, "[");
  proof.back = audio.selected();
  parts.controller.key("?", { name: "?" });
  shown = text(parts);
  proof.help = ["Previous/next device", "Play/pause", "Step the volume or a level"].every((words) => shown.includes(words));
  await key(parts, null, { name: "escape" });
  proof.reach.audioU = await key(parts, "u");
  proof.reach.audioF6 = await key(parts, null, { name: "f6" });
  proof.reach.audioEsc = await key(parts, null, { name: "escape" });
  proof.reach.again = await key(parts, "u");
  proof.reach.audioF8 = await key(parts, null, { name: "f8" });
  proof.sent = parts.sent;
  controller.stop();
}

// A room lock that holds the owned amplifier: the screen says so, and the
// session refuses each command with a toast; the other one still answers.
async function locked(proof) {
  const shared = Shared.load();
  const Access = shared.Access;
  shared.Access = Object.assign({}, Access, { create: (options) => Object.assign(Object.create(Access.create(options)), {
    mock: false, lockSummary: (room) => room === ROOM ? { locked: true, manual: true, until: null, reason: "manual_lock" } : null }) });
  const parts = assemble(shared);
  const { controller, session } = parts;
  const toasts = [];
  await controller.start(); await settle();
  draw(parts);
  await key(parts, null, { name: "f2" });
  session.selectRoom(ROOM, true);
  await key(parts, "u");
  const shown = text(parts);
  proof.lock = { said: shown.includes("Locked by Example East"), padlock: shown.includes("⚿ Amp Pro") };
  for (const ch of ["p", "+", "m"]) { await key(parts, ch); toasts.push(controller.ui.toast && controller.ui.toast.title + ": " + controller.ui.toast.body); }
  await focusOn(parts, "audio-set:" + OWNED + "_eq");
  await key(parts, null, { name: "enter" });
  toasts.push(controller.ui.toast && controller.ui.toast.title + ": " + controller.ui.toast.body);
  proof.lock.toasts = toasts;
  proof.lock.sent = parts.sent.slice();
  await key(parts, "]");
  await key(parts, "+");
  proof.lock.other = parts.sent.slice(proof.lock.sent.length);
  controller.stop();
}

// The public example has no audio device: Remotes offers no u, and Audio says why.
async function plain(proof) {
  const parts = assemble(Shared.load(Shared.WEB));
  const { controller } = parts;
  await controller.start(); await settle();
  draw(parts);
  await key(parts, null, { name: "f2" });
  proof.hint = text(parts).split("\n").slice(-1)[0].includes("u Audio");
  proof.screen = await key(parts, "u");
  for (const ch of ["p", "+", "m", "]"]) await key(parts, ch);
  await key(parts, null, { name: "enter" });
  proof.empty = text(parts).includes("No speaker or streamer here has an audio page yet.");
  proof.sent = parts.sent;
  proof.back = await key(parts, null, { name: "escape" });
  controller.stop();
}

(async () => {
  const proof = {};
  if (mode === "plain") await plain(proof);
  else { await fixture(proof); await locked(proof); }
  process.stdout.write(JSON.stringify(proof) + "\n");
})().then(() => process.exit(0), (error) => { process.stderr.write(error.stack + "\n"); process.exit(1); });
