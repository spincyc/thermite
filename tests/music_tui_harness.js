"use strict";
// The terminal remote's Music screen (y from Remotes) on the Music page's
// session (web/remote/music-session.js) and the mock's invented speakers,
// in the public example house. Prints what the screen drew and sent;
// nothing leaves the process. It waits for what it expects to show, never
// for a fixed time.
const assert = require("node:assert/strict");
const Main = require("../tui/remote/main"), Shared = require("../tui/remote/shared"), Grid = require("../tui/remote/grid");
console.log = console.info = () => {};
const WIDTH = 110, HEIGHT = 70;

// The remote on the mock, the music backend each Music session makes kept
// (with settings over the mock's own: a house without music, a Guest).
function assemble(settings = {}) {
  const shared = Shared.load(Shared.WEB);
  const parts = { backends: [] };
  const create = shared.MusicBackend.create;
  shared.MusicBackend = Object.assign({}, shared.MusicBackend, { create: (options) => {
    const backend = create(Object.assign({}, options, settings));
    parts.backends.push(backend);
    return backend;
  } });
  Object.assign(parts, Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]),
    { shared, setInterval: () => 0, clearInterval() {}, onQuit: () => { parts.quit = true; } }));
  return parts;
}

const draw = (parts) => parts.controller.frame(WIDTH, HEIGHT);
const text = (parts) => draw(parts).grid.text();
const backend = (parts) => parts.backends[parts.backends.length - 1];
const session = (parts) => parts.controller.context().music.session;
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
const toast = (parts) => parts.controller.ui.toast && [parts.controller.ui.toast.title, parts.controller.ui.toast.body];

async function main(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  // y opens Music from Remotes only.
  proof.reach = { home: key(parts, "y") };
  key(parts, null, { name: "f2" });
  proof.reach.pairing = (key(parts, null, { name: "f6" }), key(parts, "y"));
  key(parts, null, { name: "escape" });
  proof.hint = text(parts).split("\n").slice(-1)[0].includes("y Music");
  controller.key("?", { name: "?" });
  proof.help = text(parts).includes("Music (Remotes)");
  key(parts, null, { name: "escape" });
  proof.reach.remote = key(parts, "y");
  await shows(parts, "Harbour Lights");
  let shown = text(parts);
  proof.drawn = ["Demo · invented speakers and music. Nothing plays.", "Speakers", "Kitchen Sonos Amp", "Kitchen WiiM Mini", "Den",
    "Study", "Patio", "Not available", "Now playing", "Kitchen Sonos Amp · Sonos", "The Lanterns · Harbour Lights", "‖ Pause",
    "Shuffle", "Repeat off", "Volume", "+ 32", "Not grouped", "Group…", "TIDAL is not signed in.", "Sign in to TIDAL", "Library",
    "● Search", "○ Browse", "○ Queue", "type / to search", "● All", "● Everywhere", "› Music"].filter((words) => !shown.includes(words));
  proof.focus = controller.ui.focus;
  // The page's keys, each read back by the mock.
  key(parts, "+");
  await shows(parts, "+ 34");
  key(parts, "p");
  await shows(parts, "▶ Play");
  key(parts, "s");
  await shows(parts, "Shuffle on");
  key(parts, "r");
  await shows(parts, "Repeat all");
  key(parts, ">");
  await shows(parts, "Signal Fires");
  key(parts, "]");
  proof.next = session(parts).selected();
  key(parts, "[");
  proof.back = session(parts).selected();
  proof.sent = backend(parts).sent.slice();
  // The position: ← and → move it 10 s while it has the focus.
  focusOn(parts, "music-progress");
  const before = session(parts).view().progress.elapsed;
  key(parts, null, { name: "right" });
  await until(parts, () => backend(parts).sent.some((entry) => entry[2] === "seek"), "a seek");
  proof.seek = [backend(parts).sent[backend(parts).sent.length - 1][3] - before, controller.ui.focus];
  // Search: what is typed goes in, q included; Enter searches.
  key(parts, "/");
  proof.searchFocus = controller.ui.focus;
  for (const ch of "harbq") key(parts, ch);
  proof.typed = [session(parts).library().search.query, controller.ui.screen];
  key(parts, null, { name: "backspace" });
  key(parts, null, { name: "enter" });
  await shows(parts, "Low Tide · The Lanterns · Harbour Lights");
  shown = text(parts);
  proof.results = ["Tracks", "Albums", "Library  3:41"].filter((words) => !shown.includes(words));
  // A track offers how to play it; Play next goes to the speaker's queue.
  focusOn(parts, "music-item:search:track:0");
  key(parts, null, { name: "enter" });
  shown = text(parts);
  proof.sheet = { kind: controller.ui.overlay && controller.ui.overlay.kind, title: controller.ui.overlay && controller.ui.overlay.title,
    offers: ["Play now", "Play next", "Add to queue", "On Kitchen Sonos Amp. Play now replaces the", "The Lanterns · Harbour Lights · Library · 3:41"]
      .filter((words) => !shown.includes(words)) };
  key(parts, null, { name: "down" });
  key(parts, null, { name: "enter" });
  await until(parts, () => backend(parts).sent.some((entry) => entry[0] === "play"), "a play");
  await until(parts, () => toast(parts), "a toast");
  proof.played = { sent: backend(parts).sent.filter((entry) => entry[0] === "play"), toast: toast(parts), closed: controller.ui.overlay === null };
  // Esc clears what was typed, then leaves for Remotes; y comes back.
  focusOn(parts, "music-search");
  key(parts, null, { name: "escape" });
  proof.cleared = [session(parts).library().search.query, controller.ui.screen];
  // Browse: a folder opens, Backspace goes back.
  focusOn(parts, "music-tab:browse");
  key(parts, null, { name: "enter" });
  await shows(parts, "NAS");
  focusOn(parts, "music-item:browse:0");
  key(parts, null, { name: "enter" });
  await shows(parts, "Mira Okafor");
  proof.browsed = session(parts).library().browse.path;
  key(parts, null, { name: "backspace" });
  proof.browsedBack = session(parts).library().browse.path;
  // The queue, its current track marked.
  focusOn(parts, "music-tab:queue");
  key(parts, null, { name: "space" });
  await until(parts, () => session(parts).library().queue.items.length, "the queue");
  proof.queue = session(parts).library().queue.items.filter((item) => item.current).map((item) => item.name);
  // Grouping: a Sonos and a WiiM without AirPlay do not group; the sheet says why.
  key(parts, "g");
  proof.group = { kind: controller.ui.overlay && controller.ui.overlay.kind, title: controller.ui.overlay && controller.ui.overlay.title };
  focusOn(parts, "music-pick:study_amp");
  key(parts, null, { name: "space" });
  focusOn(parts, "music-pick:den_one");
  key(parts, null, { name: "enter" });
  proof.group.ticked = text(parts).includes("[x] Den") && text(parts).includes("[x] Study");
  focusOn(parts, "music-join");
  key(parts, null, { name: "enter" });
  await shows(parts, "Den joined Kitchen Sonos Amp.");
  shown = text(parts);
  proof.group.result = ["Study cannot join Kitchen Sonos Amp: Sonos and WiiM", "In the group", "Kitchen Sonos Amp · leads", "Remove",
    "End", "Ungroup all"].filter((words) => !shown.includes(words));
  proof.group.toast = toast(parts);
  key(parts, null, { name: "escape" });
  shown = text(parts);
  proof.group.drawn = ["╭ Group · Signal Fires · The Lanterns", "│ ● Den", "With Den", "Group volume"].filter((words) => !shown.includes(words));
  key(parts, "g");
  focusOn(parts, "music-end");
  key(parts, null, { name: "enter" });
  await until(parts, () => !session(parts).view().group, "the group ended");
  key(parts, null, { name: "escape" });
  // Locked by Music Assistant's gateway: refused, and the toast says why.
  backend(parts).simulateLock(["kitchen_amp"]);
  const sent = backend(parts).sent.length;
  key(parts, "p");
  await until(parts, () => toast(parts) && toast(parts)[0] === "Kitchen Sonos Amp: Pause", "the refusal");
  proof.locked = { toast: toast(parts), tried: backend(parts).sent.slice(sent), drawn: text(parts).includes("A locked room is using") };
  backend(parts).simulateLock([]);
  // An Admin's TIDAL sign-in shows the code to approve.
  focusOn(parts, "music-tidal-start");
  key(parts, null, { name: "enter" });
  await shows(parts, "MOCK7QX2");
  proof.tidal = ["Approve this code at link.tidal.com on any device.", "Code  MOCK7QX2", "https://link.tidal.com/MOCK7QX2", "Cancel"]
    .filter((words) => !text(parts).includes(words));
  proof.reach.esc = key(parts, null, { name: "escape" });
  proof.reach.again = key(parts, "y");
  await shows(parts, "Harbour Lights");
  proof.reach.f8 = key(parts, null, { name: "f8" });
  controller.stop();
}

// A grouping Music Assistant took but no read-back confirmed: the sheet says
// who was asked, not that they were refused.
async function unconfirmed(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  key(parts, null, { name: "f2" });
  key(parts, "y");
  await shows(parts, "Harbour Lights");
  key(parts, "g");
  focusOn(parts, "music-pick:den_one");
  key(parts, null, { name: "space" });
  backend(parts).simulateUnconfirmed(["den_one"]);
  focusOn(parts, "music-join");
  key(parts, null, { name: "enter" });
  await until(parts, () => toast(parts), "the toast");
  // The sheet's lines (inside its border), not the status line's toast.
  const shown = text(parts).split("\n").filter((line) => line.includes("\u2502")).join("\n");
  proof.unconfirmed = { toast: toast(parts), refused: shown.includes("could not join") || shown.includes("cannot join"),
    said: shown.replace(/[\u2502\s]+/g, " ").includes("Den was asked to join; Music Assistant has not confirmed it yet.") };
  controller.stop();
}

// A volume dragged with the mouse: let go, it tells the session the slide ended there.
async function dragged(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  key(parts, null, { name: "f2" });
  key(parts, "y");
  await shows(parts, "Harbour Lights");
  const told = [], music = session(parts), setVolume = music.setVolume;
  music.setVolume = (value, final) => { told.push([value, Boolean(final)]); return setVolume(value, final); };
  const entry = draw(parts).grid.hits.find((hit) => hit.hit.kind === "music-volume");
  controller.mouse({ action: "mousedown", button: "left", x: entry.x, y: entry.y });
  controller.mouse({ action: "mousemove", button: "left", x: entry.x + entry.w - 1, y: entry.y });
  controller.mouse({ action: "mouseup", button: "left", x: entry.x + entry.w - 1, y: entry.y });
  proof.dragged = told;
  controller.stop();
}

// A narrow terminal: everything still fits, the header says where it is.
async function narrow(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  key(parts, null, { name: "f2" });
  key(parts, "y");
  await until(parts, () => controller.frame(44, 60).grid.text().includes("Harbour Lights"), "drawn narrow");
  const shown = controller.frame(44, 60).grid.text();
  proof.narrow = { header: shown.split("\n")[0].includes("Remotes / Music"), keys: shown.includes("‖ Pause"),
    volume: /Volume\n? *−/.test(shown) || shown.includes("Volume") };
  controller.stop();
}

// A group member's room locked: the speaker's own volume still goes, Play does not.
async function member(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  // The demo's access knows no locks, and a session ignores it; this one says the Den is locked.
  let den = false;
  const lockSummary = parts.access.lockSummary;
  Object.assign(parts.access, { mock: false, lockSummary: (room) => (den && room === "den" ? { locked: true } : lockSummary(room)) });
  key(parts, null, { name: "f2" });
  key(parts, "y");
  await shows(parts, "Harbour Lights");
  session(parts).join(["den_one"]);
  await shows(parts, "With Den");
  den = true;
  await shows(parts, "Group locked by Den");
  const sent = backend(parts).sent.length;
  key(parts, "+");
  key(parts, "p");
  await until(parts, () => toast(parts) && toast(parts)[1] === "Locked by Den", "the lock's toast");
  proof.member = { sent: backend(parts).sent.slice(sent), toast: toast(parts) };
  controller.stop();
}

// The search field gone under the typing: a q typed next does not quit.
async function vanished(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  key(parts, null, { name: "f2" });
  key(parts, "y");
  await shows(parts, "type / to search");
  for (const ch of "/lo") key(parts, ch);
  backend(parts).simulateUnavailable();
  await shows(parts, "Music Assistant is not answering yet.");
  proof.vanished = { focus: controller.ui.focus };
  key(parts, "q");
  proof.vanished.typed = [Boolean(parts.quit), controller.ui.screen];
  // Back again (the search with it) under the typing: the field has the focus again, and takes q.
  backend(parts).recover();
  await shows(parts, "Low Tide · The Lanterns");
  proof.vanished.back = controller.ui.focus;
  key(parts, "q");
  proof.vanished.query = [Boolean(parts.quit), session(parts).library().search.query];
  // Another key, and q is the screen's again.
  key(parts, null, { name: "down" });
  key(parts, "q");
  proof.vanished.after = Boolean(parts.quit);
  controller.stop();
  // Gone under the typing, then the sign-in ends: on Account, q quits as anywhere.
  const again = assemble();
  await again.controller.start();
  await until(again, () => again.controller.ui.ready, "ready");
  key(again, null, { name: "f2" });
  key(again, "y");
  await shows(again, "type / to search");
  for (const ch of "/lo") key(again, ch);
  backend(again).simulateUnavailable();
  await shows(again, "Music Assistant is not answering yet.");
  key(again, "q");
  again.backend.emit({ type: "access", unauthorized: true, guest: false });
  await until(again, () => again.controller.ui.screen === "account", "the account screen");
  key(again, "q");
  proof.vanished.account = Boolean(again.quit);
  again.controller.stop();
}

// A title in CJK with an emoji and a decomposed accent takes the columns a
// terminal gives it: its row ends at the panel's edge, as the others do.
async function widths(proof) {
  const parts = assemble();
  const { controller } = parts;
  await controller.start();
  await until(parts, () => controller.ui.ready, "ready");
  key(parts, null, { name: "f2" });
  key(parts, "y");
  await shows(parts, "Slow Rivers");
  const mock = backend(parts), queue = mock.queueOf("study_amp");
  mock.items[queue.items[queue.current]].name = "東京の夜 🎵 Cafe\u0301";
  mock.simulateDrop();
  await shows(parts, "東京の夜");
  const grid = draw(parts).grid;
  const rows = grid.rows.map((row) => row.map((cell) => cell.ch).join(""));
  const at = rows.findIndex((row) => row.includes("東京の夜"));
  proof.widths = { row: rows[at], next: rows[at + 1], cells: grid.rows[at].length,
    halves: grid.rows[at].filter((cell) => cell.ch === "").length };
  // Cut to a width, or half drawn over: a wide character is never split.
  const cut = new Grid.Grid(8, 1);
  cut.put(0, 0, "ab東京cd");
  cut.put(3, 0, "X");
  proof.widths.cut = [Grid.fit("東京東京東京", 5), Grid.fit("東京東京東京", 4), Grid.wrap("東京東京東京", 5), cut.text()];
  controller.stop();
}

// No music on this host; a Guest without titles.
async function others(proof) {
  for (const [name, settings, words] of [["absent", { configured: false }, "This thermite host has no music."],
    ["guest", { guest: true }, "Sign in to thermite to search, browse and see the queue."]]) {
    const parts = assemble(settings);
    const { controller } = parts;
    await controller.start();
    await until(parts, () => controller.ui.ready, "ready");
    key(parts, null, { name: "f2" });
    key(parts, "y");
    // The gateway tells a Guest no when a search asks.
    if (name === "guest") {
      await shows(parts, "type / to search");
      for (const ch of "/h") key(parts, ch);
      key(parts, null, { name: "enter" });
    }
    await shows(parts, words);
    const shown = text(parts);
    proof[name] = { said: shown.includes(words), speakers: shown.includes("Speakers"), titles: shown.includes("Harbour Lights"),
      playing: shown.includes("Kitchen Sonos Amp") && /Kitchen Sonos Amp\s+Playing/.test(shown) };
    if (name === "absent") for (const ch of ["p", "+", "]", "/", "g"]) key(parts, ch);
    proof[name].sent = backend(parts).sent;
    controller.stop();
  }
}

(async () => {
  const proof = {};
  await main(proof);
  await unconfirmed(proof);
  await dragged(proof);
  await narrow(proof);
  await member(proof);
  await vanished(proof);
  await widths(proof);
  await others(proof);
  process.stdout.write(JSON.stringify(proof) + "\n");
})().then(() => process.exit(0), (error) => { process.stderr.write(error.stack + "\n"); process.exit(1); });
