"use strict";
// Listen on the terminal remote's Remotes screen (tui/remote/listen.js) on
// the shared sessions (web/remote/listen-session.js over audio-session.js
// and music-session.js) and the mock backends. argv: "fixture", whose
// house (THERMITE_REMOTE_HOUSE_FILE) has a routed room that owns a WiiM
// (ROOM, OWNED), a room of two WiiMs alone (DEN: MINI, FREE) and a room of
// a Sonos Arc alone (LOUNGE: ARC); or "plain", the public example, which
// has no speakers. Prints what the screen drew and sent; nothing leaves
// the process. It waits for what it expects to draw, never for a fixed time.
const assert = require("node:assert/strict");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared"), Grid = require("../tui/remote/grid");
console.log = console.info = () => {};
const [mode, ROOM, OWNED, DEN, MINI, FREE, LOUNGE, ARC] = process.argv.slice(2);
const WIDTH = 110, HEIGHT = 70;

// The remote on the mock, its device commands recorded and still taken,
// the music backend kept (with settings over the mock's own: a host
// without music, a Guest), env over the remote's own (a clock), and an
// access client whose lockSummary says the rooms in locks are locked.
function assemble(options = {}) {
  const shared = options.shared || Shared.load();
  const parts = { backends: [], locks: new Set(options.locks || []) };
  const create = shared.MusicBackend.create;
  shared.MusicBackend = Object.assign({}, shared.MusicBackend, { create: (settings) => {
    const backend = create(Object.assign({}, settings, options.music));
    parts.backends.push(backend);
    return backend;
  } });
  if (options.locking) {
    const Access = shared.Access;
    shared.Access = Object.assign({}, Access, { create: (settings) => Object.assign(Object.create(Access.create(settings)), {
      mock: false, lockSummary: (room) => (parts.locks.has(room) ? { locked: true, manual: true, until: null, reason: "manual_lock" } : null) }) });
  }
  Object.assign(parts, Main.assemble(Main.parse(["--mock", "--still", "--latency", "0", "--volume", "slider"]),
    { shared, setInterval: () => 0, clearInterval() {}, onQuit: () => { parts.quit = true; }, screen: "remote", ...options.env }));
  const command = parts.backend.command.bind(parts.backend);
  parts.sent = [];
  parts.backend.command = (device, action, value) => { parts.sent.push([device, action, String(value)]); return command(device, action, value); };
  return parts;
}

let size = [WIDTH, HEIGHT];
const draw = (parts) => parts.controller.frame(size[0], size[1]);
const text = (parts) => draw(parts).grid.text();
const music = (parts) => parts.backends[parts.backends.length - 1];
const toast = (parts) => parts.controller.ui.toast && [parts.controller.ui.toast.title, parts.controller.ui.toast.body];
// Polls until the predicate holds (drawing first, as the terminal would); fails after a while.
async function until(parts, predicate, what) {
  for (let i = 0; i < 400; i++) {
    draw(parts);
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("Never: " + what + "\n" + text(parts));
}
const shows = (parts, words) => until(parts, () => text(parts).includes(words), JSON.stringify(words));
function key(parts, ch, event) {
  draw(parts);
  parts.controller.key(ch, event || { name: ch });
  draw(parts);
  return parts.controller.ui.screen;
}
// The arrows reach the control: up to the first, then down to it.
function focusOn(parts, target) {
  for (let i = 0; i < 120; i++) key(parts, null, { name: "up" });
  for (let i = 0; i < 120 && parts.controller.ui.focus !== target; i++) key(parts, null, { name: "down" });
  assert.equal(parts.controller.ui.focus, target);
}
// Tab to the remote pane.
function toRemote(parts) {
  for (let i = 0; i < 8 && parts.controller.ui.pane !== "remote"; i++) key(parts, null, { name: "tab" });
  assert.equal(parts.controller.ui.pane, "remote");
}
// A click on the hit of this key, at its first cell (or dx in).
function click(parts, hitKey, dx = 0, action = "mousedown") {
  const entry = draw(parts).grid.hits.find((hit) => hit.hit.key === hitKey);
  assert.ok(entry, "no " + hitKey);
  parts.controller.mouse({ action, button: "left", x: entry.x + dx, y: entry.y });
  return entry;
}
async function open(options, room) {
  const parts = assemble(options);
  await parts.controller.start(room);
  await until(parts, () => parts.controller.ui.ready, "ready");
  return parts;
}
const lines = (shown, from, to) => shown.split("\n").slice(from, to).join("\n");

// ---- Example East, which watches and listens -----------------------------

async function mixed(proof) {
  const parts = await open({}, ROOM);
  const { controller, session } = parts;
  // Watch first, Watch | Listen in the remote's edge, and y in the footer; y listens.
  let shown = text(parts);
  proof.watch = { mode: session.mode(), switch: /Example East ├─┤ +Watch │ ♪ Listen +├/.test(shown),
    footer: shown.split("\n").slice(-1)[0].includes("y Listen") };
  controller.key("?", { name: "?" });
  proof.watch.help = ["Watch or listen", "Source", "Signal & audio"].every((words) => text(parts).includes(words));
  key(parts, null, { name: "escape" });
  // The old screens' keys open nothing; F6 and F8 still go.
  proof.reach = { u: key(parts, "u"), Y: (key(parts, "y"), session.mode()) };
  await shows(parts, "Wi-Fi ▾");
  proof.reach.again = (key(parts, "Y"), session.mode());
  key(parts, "y");
  proof.reach.f6 = key(parts, null, { name: "f6" });
  proof.reach.back = key(parts, null, { name: "escape" });
  proof.reach.kept = session.mode();
  proof.reach.homeY = (key(parts, null, { name: "f1" }), key(parts, "y"));
  key(parts, null, { name: "f2" });
  proof.reach.f8 = (key(parts, "u"), key(parts, null, { name: "f8" }));
  key(parts, null, { name: "f2" });
  // Music Assistant read again: the library is its.
  await shows(parts, " Library ");
  shown = text(parts);
  proof.drawn = ["Amp Pro", "Nothing playing", "▶ Play", "⤮", "|◀", "▶|", "↻", " Group ", " Wi-Fi ▾ ", " Library ", "Amp Pro · 60", "Mute",
    "1-9 speaker"].filter((words) => !shown.includes(words));
  proof.footer = shown.split("\n").slice(-1)[0];
  controller.key("?", { name: "?" });
  shown = text(parts);
  proof.help = ["Watch or listen", "Speaker", "Shuffle", "Repeat", "Group", "Search", "Step a volume, or move 10 s on the",
    "Info: the speaker's power"].filter((words) => !shown.includes(words));
  // The help's rows, a key and what it does: r the rooms, R repeat, Space once (play/pause).
  const rows = shown.split("\n").map((line) => line.match(/│ {2}([^│ ]+(?: [^│ ]+)*) {2,}([^│]*?) *│/)).filter(Boolean).map((match) => [match[1], match[2]]);
  proof.helpKeys = { r: rows.filter((row) => row[0] === "r").map((row) => row[1]), R: rows.filter((row) => row[0] === "R").map((row) => row[1]),
    space: rows.filter((row) => row[0].split(" ").includes("Space")).map((row) => row[0]) };
  key(parts, null, { name: "escape" });
  toRemote(parts);
  proof.focus = controller.ui.focus;
  // The page's keys to the speaker: its volume and Mute its own, play Music Assistant's.
  key(parts, "+");
  await shows(parts, "Amp Pro · 61");
  key(parts, "m");
  await shows(parts, "61 · muted");
  key(parts, "p");
  await until(parts, () => music(parts).sent.length, "a play");
  proof.music = music(parts).sent.slice();
  // The volume: ← → on the focused slider, a click on it, a held + repeats.
  // A slide sends at most one level at a time: each waits for the last.
  const sentLevel = (value) => until(parts, () => parts.sent.some((entry) => entry[1] === "volume" && entry[2] === value), "volume " + value);
  await sentLevel("61");
  focusOn(parts, "listen-volume");
  key(parts, null, { name: "left" });
  await sentLevel("60");
  const pressed = parts.sent.length;
  key(parts, null, { name: "space" });
  proof.spaceOnLevel = parts.sent.slice(pressed);
  const slider = click(parts, "listen-volume", 0);
  parts.controller.mouse({ action: "mouseup", button: "left", x: slider.x, y: slider.y });
  await sentLevel("0");
  click(parts, "listen-key:volume_up", 1);
  await until(parts, () => parts.sent.filter((entry) => entry[1] === "volume").length >= 5, "the held + repeating");
  const up = draw(parts).grid.hits.find((hit) => hit.hit.key === "listen-key:volume_up");
  parts.controller.mouse({ action: "mouseup", button: "left", x: up.x, y: up.y });
  proof.sent = parts.sent.slice();
  // The input: a list from its chip.
  focusOn(parts, "listen-inputs");
  key(parts, null, { name: "enter" });
  proof.inputs = { kind: controller.ui.overlay && controller.ui.overlay.kind, focus: controller.ui.focus };
  key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  await shows(parts, "Bluetooth ▾");
  proof.inputSent = parts.sent.filter((entry) => entry[1] === "input");
  proof.inputs.closed = [controller.ui.overlay, controller.ui.focus];
  key(parts, null, { name: "enter" });
  key(parts, null, { name: "up" });
  key(parts, null, { name: "enter" });
  await shows(parts, "Wi-Fi ▾");
  // Info: the speaker's settings and facts, each reached with the arrows.
  key(parts, "i");
  shown = text(parts);
  proof.info = ["Amp Pro", "Playback", "Play Mode", "In order ▾", "Sound", "EQ Preset", "Flat ▾", "Subwoofer Level", " −  0 dB  + ",
    "Device", "Status Light", "About this device", "Subwoofer Connected", "Yes"].filter((words) => !shown.includes(words));
  const settings = parts.sent.length;
  focusOn(parts, "audio-set:" + OWNED + "_eq");
  key(parts, null, { name: "enter" });
  focusOn(parts, "audio-set:" + OWNED + "_sub_level");
  key(parts, null, { name: "right" }); key(parts, null, { name: "right" }); key(parts, null, { name: "left" });
  focusOn(parts, "audio-set:" + OWNED + "_eq_preset");
  key(parts, null, { name: "enter" });
  proof.choice = { kind: controller.ui.overlay.kind, focus: controller.ui.focus, lists: text(parts).includes("Bass Booster") };
  key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  await shows(parts, "Acoustic ▾");
  proof.choice.back = [controller.ui.overlay && controller.ui.overlay.kind, controller.ui.focus];
  shown = text(parts);
  proof.choice.set = { eq: /EQ +on/.test(shown), level: shown.includes(" 1 dB ") };
  key(parts, null, { name: "escape" });
  proof.settings = parts.sent.slice(settings);
  // y watches again; Watch's caption names the Amp: a click listens to it.
  key(parts, "y");
  proof.caption = session.mode();
  key(parts, "1");
  await until(parts, () => draw(parts).grid.hits.some((hit) => hit.hit.key === "listen-to"), "the Amp's caption");
  click(parts, "listen-to");
  proof.caption = [proof.caption, session.mode()];
  controller.stop();
}

// ---- Fixture Den: two speakers alone --------------------------------------

async function den(proof) {
  const parts = await open({}, DEN);
  const { controller, session } = parts;
  await shows(parts, "Harbour Lights");
  let shown = text(parts);
  proof.den = { mode: session.mode(), switch: shown.includes("♪ Listen"), footer: shown.split("\n").slice(-1)[0].includes("y Watch"),
    bubbles: /1 ● Mini +2 ○ Amp Pro/.test(shown), listed: /Fixture Den +Playing/.test(shown),
    drawn: ["The Lanterns · Harbour Lights", "‖ Pause", "1:0", "4:05", " Group ", " Library "].filter((words) => !shown.includes(words)) };
  // Digits, and ← → off the remote, choose a speaker; [ ] the room.
  key(parts, "2");
  proof.den.digit = session.listen.selected();
  key(parts, null, { name: "left" });
  proof.den.arrow = session.listen.selected();
  key(parts, "]");
  proof.den.room = session.current().id;
  key(parts, "[");
  // The Den Amp's Info: a short choice drawn as its options, each a press.
  key(parts, "2");
  key(parts, "i");
  await shows(parts, "Play Mode");
  const kinds = new Set(draw(parts).grid.hits.map((hit) => hit.hit.kind));
  proof.den.kinds = ["audio-switch", "audio-level", "audio-step", "audio-choose", "audio-option"].filter((kind) => !kinds.has(kind));
  const asked = parts.sent.length;
  focusOn(parts, "audio-option:" + FREE + "_play_mode:0");
  key(parts, null, { name: "enter" });
  await until(parts, () => parts.sent.length > asked, "the play mode");
  proof.den.info = parts.sent.slice(asked);
  key(parts, null, { name: "escape" });
  key(parts, "1");
  // The position: ← and → move it 10 s while it has the focus; a click seeks there.
  toRemote(parts);
  focusOn(parts, "listen-progress");
  const before = session.listen.view().progress.elapsed;
  key(parts, null, { name: "right" });
  await until(parts, () => music(parts).sent.some((entry) => entry[2] === "seek"), "a seek");
  proof.seek = [music(parts).sent[music(parts).sent.length - 1][3] - before, controller.ui.focus];
  click(parts, "listen-progress", 0);
  await until(parts, () => music(parts).sent.filter((entry) => entry[2] === "seek").length === 2, "a click's seek");
  proof.seek.push(music(parts).sent[music(parts).sent.length - 1][3]);
  // r opens the rooms, as in Watch, and repeats nothing; Esc closes them.
  const transport = music(parts).sent.length;
  key(parts, "r");
  proof.rooms = { sheet: controller.ui.overlay && controller.ui.overlay.kind, focus: controller.ui.focus,
    repeat: session.listen.view().transport.find((entry) => entry.id === "repeat").mode };
  key(parts, null, { name: "escape" });
  proof.rooms.closed = [controller.ui.overlay, session.current().id];
  // Shuffle, R repeat all then one (↻1), the next track.
  key(parts, "s");
  await until(parts, () => session.listen.view().transport.find((entry) => entry.id === "shuffle").on, "shuffle on");
  key(parts, "R");
  await until(parts, () => session.listen.view().transport.find((entry) => entry.id === "repeat").on, "repeat on");
  key(parts, "R");
  await shows(parts, "↻1");
  key(parts, ">");
  await shows(parts, "Signal Fires");
  proof.transport = music(parts).sent.slice(transport);
  // The library: / goes to its search, where q types; Enter searches.
  key(parts, "/");
  proof.search = { kind: controller.ui.overlay && controller.ui.overlay.kind, focus: controller.ui.focus };
  for (const ch of "harbq") key(parts, ch);
  proof.search.typed = [session.listen.music.library().search.query, Boolean(parts.quit)];
  key(parts, null, { name: "backspace" });
  key(parts, null, { name: "enter" });
  await shows(parts, "Low Tide · The Lanterns · Harbour Lights");
  shown = text(parts);
  proof.search.results = ["Tracks", "Albums", "● All", "● Everywhere", "○ Library", "Library  3:41"].filter((words) => !shown.includes(words));
  // A track offers how to play it, over the library; Play next, and the library is back.
  focusOn(parts, "music-item:search:track:0");
  key(parts, null, { name: "enter" });
  shown = text(parts);
  proof.item = { kind: controller.ui.overlay.kind, title: controller.ui.overlay.title,
    offers: ["Play now", "Play next", "Add to queue", "On Fixture Den Mini. Play now replaces the queue."].filter((words) => !shown.replace(/[│\s]+/g, " ").includes(words)) };
  key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  await until(parts, () => music(parts).sent.some((entry) => entry[0] === "play"), "a play");
  await until(parts, () => toast(parts), "a toast");
  proof.item.played = { sent: music(parts).sent.filter((entry) => entry[0] === "play"), toast: toast(parts),
    back: [controller.ui.overlay && controller.ui.overlay.kind, controller.ui.focus] };
  // Esc clears what was typed, then closes the library.
  focusOn(parts, "music-search");
  key(parts, null, { name: "escape" });
  proof.cleared = [session.listen.music.library().search.query, controller.ui.overlay && controller.ui.overlay.kind];
  // Browse: a folder opens, Backspace goes back; the queue marks what plays.
  focusOn(parts, "music-tab:browse");
  key(parts, null, { name: "enter" });
  await shows(parts, "NAS");
  focusOn(parts, "music-item:browse:0");
  key(parts, null, { name: "enter" });
  await shows(parts, "Mira Okafor");
  proof.browsed = session.listen.music.library().browse.path;
  key(parts, null, { name: "backspace" });
  proof.browsedBack = session.listen.music.library().browse.path;
  focusOn(parts, "music-tab:queue");
  key(parts, null, { name: "space" });
  await until(parts, () => session.listen.music.library().queue.items.length, "the queue");
  proof.queue = session.listen.music.library().queue.items.filter((item) => item.current).map((item) => item.name);
  // An Admin's TIDAL sign-in shows the code to approve.
  focusOn(parts, "music-tidal-start");
  key(parts, null, { name: "enter" });
  await shows(parts, "MOCK7QX2");
  proof.tidal = ["Approve this code at link.tidal.com on any device.", "Code  MOCK7QX2", "https://link.tidal.com/MOCK7QX2", "Cancel"]
    .filter((words) => !text(parts).includes(words));
  key(parts, null, { name: "escape" });
  proof.closed = controller.ui.overlay;
  // Locked by Music Assistant's gateway: refused, and the toast says why.
  music(parts).simulateLock([session.listen.selected()]);
  const sent = music(parts).sent.length;
  key(parts, "p");
  await until(parts, () => toast(parts) && toast(parts)[0].endsWith(": Pause"), "the refusal");
  proof.refused = { toast: toast(parts), tried: music(parts).sent.slice(sent) };
  music(parts).simulateLock([]);
  controller.stop();
}

// ---- grouping ------------------------------------------------------------

async function grouping(proof) {
  const parts = await open({}, ROOM);
  const { controller, session } = parts;
  key(parts, "y");
  await until(parts, () => session.listen.view() && session.listen.view().canGroup, "Music Assistant's speaker");
  // A WiiM without AirPlay and a Sonos do not group; the sheet says why.
  key(parts, "g");
  proof.group = { kind: controller.ui.overlay && controller.ui.overlay.kind, title: controller.ui.overlay && controller.ui.overlay.title };
  focusOn(parts, "music-pick:" + ARC);
  key(parts, null, { name: "space" });
  focusOn(parts, "music-pick:" + MINI);
  key(parts, null, { name: "space" });
  proof.group.ticked = text(parts).includes("[x] Fixture Den Mini") && text(parts).includes("[x] Fixture Lounge");
  focusOn(parts, "music-join");
  key(parts, null, { name: "enter" });
  await shows(parts, "joined Example East.");
  let shown = text(parts);
  proof.group.result = ["Fixture Lounge cannot join Example East: Sonos and WiiM", "In the group", "Example East · leads", "Remove", "End",
    "Volume", "Ungroup all"].filter((words) => !shown.includes(words));
  proof.group.toast = toast(parts);
  // The group's volume in the sheet: ← → step it.
  focusOn(parts, "listen-group-volume");
  const level = session.listen.music.grouping().volume.value;
  key(parts, null, { name: "right" });
  await until(parts, () => music(parts).sent.some((entry) => entry[0] === "volume" && entry[2] !== "level"), "a group volume");
  proof.group.stepped = [level, music(parts).sent.filter((entry) => entry[0] === "volume").slice(-1)[0]];
  key(parts, null, { name: "escape" });
  shown = text(parts);
  proof.group.drawn = ["With Fixture Den Mini", "Group −"].filter((words) => !shown.includes(words));
  // The group's volume on the face: dragged, let go, it ends the slide there.
  const told = [], player = session.listen.music, setGroupVolume = player.setGroupVolume;
  player.setGroupVolume = (value, final) => { told.push([value, Boolean(final)]); return setGroupVolume(value, final); };
  const entry = click(parts, "listen-group-volume", 0);
  parts.controller.mouse({ action: "mousemove", button: "left", x: entry.x + entry.w - 1, y: entry.y });
  parts.controller.mouse({ action: "mouseup", button: "left", x: entry.x + entry.w - 1, y: entry.y });
  proof.group.dragged = told;
  key(parts, "g");
  focusOn(parts, "music-end");
  key(parts, null, { name: "enter" });
  await until(parts, () => !session.listen.view().group, "the group ended");
  key(parts, null, { name: "escape" });
  // A grouping Music Assistant took but no read-back confirmed: the sheet says who was asked.
  controller.ui.toast = null;
  key(parts, "g");
  focusOn(parts, "music-pick:" + FREE);
  key(parts, null, { name: "space" });
  music(parts).simulateUnconfirmed([FREE]);
  focusOn(parts, "music-join");
  key(parts, null, { name: "enter" });
  await until(parts, () => toast(parts), "the toast");
  shown = text(parts).split("\n").filter((line) => line.includes("│")).join("\n").replace(/[│\s]+/g, " ");
  proof.unconfirmed = { toast: toast(parts), refused: shown.includes("could not join") || shown.includes("cannot join"),
    said: shown.includes("Fixture Den Amp Pro was asked to join; Music Assistant has not confirmed it yet.") };
  controller.stop();
}

// ---- locks ---------------------------------------------------------------

// Example East locked: its Amp, in Listen, shows the lock as Watch does and
// refuses each command with it; the Den's speakers still answer.
async function locked(proof) {
  const parts = await open({ locking: true, locks: [ROOM] }, ROOM);
  const { controller, session } = parts;
  key(parts, "y");
  await until(parts, () => session.listen.view() && session.listen.view().canGroup, "Music Assistant's speaker");
  let shown = text(parts);
  proof.lock = { said: shown.includes("Read-only · Locked by Example East"), padlock: shown.includes("⚿ Amp Pro"), toasts: [] };
  toRemote(parts);
  for (const ch of ["p", "+", "m"]) {
    key(parts, ch);
    proof.lock.toasts.push(toast(parts) && toast(parts).join(": "));
    parts.controller.ui.toast = null;
  }
  proof.lock.pressed = Object.keys(controller.ui.pressed);
  key(parts, "i");
  focusOn(parts, "audio-set:" + OWNED + "_eq");
  key(parts, null, { name: "enter" });
  proof.lock.toasts.push(toast(parts) && toast(parts).join(": "));
  key(parts, null, { name: "escape" });
  proof.lock.sent = parts.sent.slice();
  proof.lock.music = music(parts).sent.slice();
  session.selectRoom(DEN, true);
  await shows(parts, "1 ● Mini");
  controller.ui.toast = null;
  shown = text(parts);
  proof.lock.den = !shown.includes("Locked by");
  key(parts, "+");
  await until(parts, () => parts.sent.length > proof.lock.sent.length, "the Den's volume");
  proof.lock.other = parts.sent.slice(proof.lock.sent.length);
  // The Den's Mini grouped with the Amp: the lock holds what moves the group, not its own volume.
  parts.locks.clear();
  session.listen.music.join([OWNED]);
  await shows(parts, "With Example East");
  parts.locks.add(ROOM);
  await shows(parts, "Group locked by Example East");
  const sent = parts.sent.length, played = music(parts).sent.length;
  key(parts, "+");
  await until(parts, () => parts.sent.length > sent, "the Mini's own volume");
  key(parts, "p");
  proof.member = { sent: parts.sent.slice(sent), toast: toast(parts), played: music(parts).sent.slice(played) };
  toRemote(parts);
  focusOn(parts, "listen-key:next");
  key(parts, null, { name: "enter" });
  proof.member.pressed = [Boolean(controller.ui.pressed["listen-key:play_pause"]), Boolean(controller.ui.pressed["listen-key:next"])];
  key(parts, "g");
  proof.member.sheet = controller.ui.overlay && controller.ui.overlay.kind;
  proof.member.groupToast = toast(parts);
  controller.stop();
}

// ---- the search field gone under the typing ------------------------------

async function vanished(proof) {
  const typedOff = async (parts) => {
    await shows(parts, "Harbour Lights");
    key(parts, "/");
    for (const ch of "lo") key(parts, ch);
    music(parts).simulateUnavailable();
    await shows(parts, "Music Assistant is not answering yet.");
  };
  const parts = await open({}, DEN);
  const { controller } = parts;
  await typedOff(parts);
  proof.vanished = { focus: controller.ui.focus };
  key(parts, "q");
  proof.vanished.typed = [Boolean(parts.quit), controller.ui.screen];
  // Back again (the search with it) under the typing: the field has the focus again, and takes q.
  music(parts).recover();
  await shows(parts, "Low Tide · The Lanterns");
  proof.vanished.back = controller.ui.focus;
  key(parts, "q");
  proof.vanished.query = [Boolean(parts.quit), parts.listen.music.library().search.query];
  // Another key, and q is the remote's again.
  key(parts, null, { name: "down" });
  key(parts, "q");
  proof.vanished.after = Boolean(parts.quit);
  controller.stop();
  // Gone under the typing, then the sign-in ends: on Account, q quits as anywhere.
  const again = await open({}, DEN);
  await typedOff(again);
  key(again, "q");
  again.backend.emit({ type: "access", unauthorized: true, guest: false });
  await until(again, () => again.controller.ui.screen === "account", "the account screen");
  key(again, "q");
  proof.vanished.account = Boolean(again.quit);
  again.controller.stop();
  // Gone under the typing, and a q typed long after: the remote's again (a clock run on, not waited for).
  let ahead = 0;
  const later = await open({ env: { now: () => Date.now() + ahead } }, DEN);
  await typedOff(later);
  ahead += 2000;
  key(later, "q");
  proof.vanished.lapsed = [Boolean(later.quit)];
  ahead += 4000;
  key(later, "q");
  proof.vanished.lapsed.push(Boolean(later.quit));
  later.controller.stop();
}

// ---- widths and sizes ----------------------------------------------------

// A title in CJK with an emoji and a decomposed accent takes the columns a
// terminal gives it: its row ends at the panel's edge, as the others do.
async function widths(proof) {
  const parts = await open({}, DEN);
  await shows(parts, "Harbour Lights");
  const mock = music(parts), queue = mock.queueOf(MINI);
  mock.items[queue.items[queue.current]].name = "東京の夜 🎵 Café 東京の夜 🎵 東京の夜 東京の夜 東京の夜 東京の夜 東京の夜";
  mock.simulateDrop();
  await shows(parts, "東京の夜");
  const grid = draw(parts).grid;
  const rows = grid.rows.map((row) => row.map((cell) => cell.ch).join(""));
  const at = rows.findIndex((row) => row.includes("東京の夜"));
  proof.widths = { rows: [rows[at], rows[at + 1]], cells: grid.rows[at].length };
  // Cut to a width, or half drawn over: a wide character is never split.
  const cut = new Grid.Grid(8, 1);
  cut.put(0, 0, "ab東京cd");
  cut.put(3, 0, "X");
  proof.widths.cut = [Grid.fit("東京東京東京", 5), Grid.fit("東京東京東京", 4), Grid.wrap("東京東京東京", 5), cut.text()];
  parts.controller.stop();
}

// 80×24 and a narrow 44: the face still draws, nothing past the edges; a
// query in wide characters longer than the field shows its end, the caret with it.
async function sizes(proof) {
  proof.sizes = {};
  for (const [width, height] of [[80, 24], [44, 60]]) {
    size = [width, height];
    const parts = await open({}, ROOM);
    // Narrow, the rooms open as a sheet.
    if (parts.controller.ui.overlay) key(parts, null, { name: "escape" });
    key(parts, "y");
    await until(parts, () => parts.listen.view() && parts.listen.view().canGroup, "Music Assistant's speaker");
    toRemote(parts);
    const seen = new Set();
    for (let i = 0; i < 30; i++) { text(parts).split("\n").forEach((line) => seen.add(line)); key(parts, null, { name: "down" }); }
    const shown = [...seen].join("\n");
    proof.sizes[width] = { keys: shown.includes("▶ Play"), chips: shown.includes(" Library "), volume: shown.includes("Amp Pro · 60"),
      mute: shown.includes("Mute"), switch: shown.includes("♪ Listen"), wide: [...seen].filter((line) => Array.from(line).length > width).length };
    if (width === 44) {
      key(parts, "/");
      for (const ch of "東京".repeat(10)) key(parts, ch);
      const field = text(parts).split("\n").find((line) => line.includes("Search ") && !line.includes("Browse"));
      proof.sizes[width].caret = [field.includes("…"), field.includes("京▏")];
    }
    parts.controller.stop();
  }
  size = [WIDTH, HEIGHT];
}

// ---- no music; a Guest ---------------------------------------------------

async function others(proof) {
  for (const [name, settings, words] of [["absent", { configured: false }, "This thermite host has no music."],
    ["guest", { guest: true }, "Sign in to thermite to search, browse and see the queue."]]) {
    const parts = await open({ music: settings }, DEN);
    // The gateway tells a Guest no when a search asks.
    if (name === "guest") {
      await shows(parts, " Library ");
      for (const ch of "/h") key(parts, ch);
      key(parts, null, { name: "enter" });
    }
    await shows(parts, words);
    const shown = text(parts);
    const hits = draw(parts).grid.hits.map((hit) => hit.hit.key);
    proof[name] = { said: shown.includes(words), titles: shown.includes("Harbour Lights"), chips: ["listen-group", "listen-library"].filter((id) => hits.includes(id)),
      playing: /Fixture Den +Playing/.test(shown) };
    key(parts, null, { name: "escape" });
    if (name === "absent") for (const ch of ["p", "+", "/", "g"]) key(parts, ch);
    proof[name].sheet = parts.controller.ui.overlay;
    proof[name].sent = music(parts).sent;
    proof[name].device = parts.sent;
    parts.controller.stop();
  }
}

// ---- the public example: no speakers --------------------------------------

async function plain(proof) {
  const parts = await open({ shared: Shared.load(Shared.WEB) }, "example_west");
  const { controller, session } = parts;
  const shown = text(parts);
  proof.rooms = session.rooms().map((room) => room.id);
  proof.music = parts.music;
  proof.footer = shown.split("\n").slice(-1)[0];
  proof.switch = shown.includes("Listen");
  key(parts, "y");
  proof.mode = session.mode();
  for (const ch of ["/", "g", "s"]) key(parts, ch);
  proof.sheet = controller.ui.overlay;
  controller.stop();
}

// ---- the edges a review found ---------------------------------------------

async function edges(proof) {
  proof.edges = {};
  const ready = (parts) => until(parts, () => parts.listen.view() && parts.listen.view().canGroup, "Music Assistant's speaker");
  // Wide, Info sits beside Listen's remote: its controls take the keys, not the room's or the speaker's.
  size = [150, 50];
  let parts = await open({}, ROOM);
  key(parts, "y");
  await ready(parts);
  click(parts, "audio-set:" + OWNED + "_sub_level", 5);
  key(parts, null, { name: "right" });
  await until(parts, () => parts.sent.length, "the level");
  key(parts, null, { name: "down" });
  const moved = parts.controller.ui.focus;
  focusOn(parts, "audio-set:" + OWNED + "_eq");
  key(parts, null, { name: "enter" });
  await until(parts, () => parts.sent.length > 1, "EQ on");
  key(parts, null, { name: "space" });
  await until(parts, () => parts.sent.length > 2, "EQ off");
  proof.edges.info = { pane: parts.controller.ui.pane, moved: moved !== "audio-set:" + OWNED + "_sub_level", room: parts.session.current().id,
    mode: parts.session.mode(), sent: parts.sent, music: music(parts).sent };
  parts.controller.stop();
  size = [WIDTH, HEIGHT];
  // ↓ from Watch's sources to a room of speakers lands on its speakers; to one of one speaker, on the rooms.
  parts = await open({}, ROOM);
  for (let i = 0; i < 6 && parts.controller.ui.pane !== "sources"; i++) key(parts, null, { name: "tab" });
  key(parts, null, { name: "down" });
  await shows(parts, "1 ● Mini");
  proof.edges.walk = [parts.session.current().id, parts.controller.ui.pane];
  key(parts, null, { name: "down" });
  await shows(parts, "Fixture Lounge Arc");
  proof.edges.walk.push(parts.session.current().id, parts.controller.ui.pane);
  parts.session.selectRoom(ROOM, true);
  // The remote's title, active, keeps its ├ before Watch | Listen.
  key(parts, "1");
  await until(parts, () => draw(parts).grid.hits.some((hit) => hit.hit.kind === "slider"), "Watch's volume");
  toRemote(parts);
  proof.edges.title = text(parts).split("\n").find((line) => line.includes("Watch │"));
  // Watch's slider drawn, then y: a click or the wheel where it was reaches Listen, not Watch's volume.
  await new Promise((resolve) => setTimeout(resolve, 100));
  const before = parts.sent.length, stale = draw(parts).grid.hits.find((hit) => hit.hit.kind === "slider");
  const slid = [], slide = parts.session.slide;
  parts.session.slide = (...args) => { slid.push(args); return slide(...args); };
  parts.controller.key("y", { name: "y" });
  parts.controller.mouse({ action: "wheelup", x: stale.x + 2, y: stale.y });
  parts.controller.mouse({ action: "mousedown", button: "left", x: stale.x + 2, y: stale.y });
  parts.controller.mouse({ action: "mouseup", button: "left", x: stale.x + 2, y: stale.y });
  await new Promise((resolve) => setTimeout(resolve, 100));
  proof.edges.stale = { mode: parts.session.mode(), slid, sent: parts.sent.slice(before) };
  parts.controller.stop();
  // A locked speaker's volume dragged: refused once, not at each move.
  parts = await open({ locking: true, locks: [ROOM] }, ROOM);
  key(parts, "y");
  await ready(parts);
  const toasts = [];
  parts.listen.subscribe((event) => { if (event.type === "toast") toasts.push(event.title + ": " + event.body); });
  const slider = click(parts, "listen-volume", 0);
  for (let dx = 1; dx < 5; dx++) parts.controller.mouse({ action: "mousemove", button: "left", x: slider.x + dx, y: slider.y });
  parts.controller.mouse({ action: "mouseup", button: "left", x: slider.x + 5, y: slider.y });
  proof.edges.drag = { toasts, sent: parts.sent };
  // K in a room of speakers alone: it has no lock to take.
  parts.session.selectRoom(DEN, true);
  await shows(parts, "1 ● Mini");
  parts.controller.ui.toast = null;
  key(parts, "K");
  proof.edges.lock = toast(parts);
  parts.controller.stop();
  // Narrow, the speakers' row shows whole bubbles, the chosen one in sight, and ‹ › where more are.
  size = [26, 40];
  parts = await open({}, DEN);
  if (parts.controller.ui.overlay) key(parts, null, { name: "escape" });
  await shows(parts, "1 ● Mini");
  const row = () => text(parts).split("\n").find((line) => line.includes("‹"));
  proof.edges.bubbles = [row()];
  key(parts, "2");
  proof.edges.bubbles.push(row());
  parts.controller.stop();
  size = [WIDTH, HEIGHT];
}

(async () => {
  const proof = {};
  if (mode === "plain") await plain(proof);
  else {
    await mixed(proof);
    await den(proof);
    await grouping(proof);
    await locked(proof);
    await vanished(proof);
    await widths(proof);
    await sizes(proof);
    await others(proof);
    await edges(proof);
  }
  process.stdout.write(JSON.stringify(proof) + "\n");
})().then(() => process.exit(0), (error) => { process.stderr.write(error.stack + "\n"); process.exit(1); });
