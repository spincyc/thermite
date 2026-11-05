// The handheld bridge (handheld/bridge.js) on the page's own session and
// mock backend, with a fake base and simulated remotes (link.js RemoteEnd):
// the handshake, every operation, dedupe, screens and audio as PROTOCOL.md
// derives them, sleep and delivery, two remotes, and the base's lines.
// Prints {cases: {name: {ok, ...}}}; nothing reaches a network or a device.
"use strict";
const assert = require("assert/strict");
const path = require("path");
const { Duplex } = require("stream");
const area = path.resolve(process.argv[2]);
const Shared = require(path.join(area, "tui/remote/shared"));
const Link = require(path.join(area, "handheld/link"));
const Bridge = require(path.join(area, "handheld/bridge"));
const Screen = require(path.join(area, "handheld/screen"));
const Base = require(path.join(area, "handheld/base"));
const vectors = require(path.join(area, "handheld/vectors.json"));

console.info = console.log = () => {};
const shared = Shared.load();
const layouts = shared.layouts;
const LATENCY = 4;
const turn = () => new Promise((resolve) => setImmediate(resolve));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function settle(ms) {
  await wait(ms === undefined ? LATENCY * 4 : ms);
  for (let index = 0; index < 5; index += 1) await turn();
}

// Locally administered MACs of fictional remotes and bases.
const mac = (last) => "02:00:00:00:00:" + last;
const KEY_A = Buffer.alloc(32, 0xa1);
const KEY_B = Buffer.alloc(32, 0xb2);
const MAC_A = mac("0a");
const MAC_B = mac("0b");
const STATUS = { have: "", battery: 80, charging: false, board: "simulator", fw: "0.1.0", w: 240, h: 320 };

// A house with a TV whose input thermite chooses, for the input list.
function inputHouse() {
  return {
    version: 1,
    rooms: [{ id: "lab", label: "Lab", sources: ["lab_box"], display: "lab_tv", audio: "lab_tv", devices: ["lab_tv", "lab_box"] }],
    sources: { lab_box: { label: "Box", kind: "streamer", model: "Box", remote: "apple_tv" } },
    devices: {
      lab_tv: { label: "Lab TV", short: "TV", kind: "tv", model: "TV", remote: "sony_tv",
        control: { key: "lab_tv_key", power: "lab_tv_power",
          input: { item: "lab_tv_input", options: [["hdmi1", "HDMI 1"], ["hdmi2", "HDMI 2"]] } } },
      lab_box: { label: "Lab Box", short: "Box", kind: "streamer", model: "Box", remote: "apple_tv", control: { key: "lab_box_key" } }
    },
    initial: { lab: "lab_box" }
  };
}

// What the backend was asked to do, as the mock would send it.
function spy(backend) {
  const calls = [];
  ["press", "choose", "allOff", "setInput", "setVolume"].forEach((name) => {
    const original = backend[name].bind(backend);
    backend[name] = (...args) => { calls.push([name, ...args]); return original(...args); };
  });
  return calls;
}

function memoryStore() {
  const data = {};
  return { data, get: (key) => (key in data ? data[key] : null), set: (key, value) => { data[key] = value; } };
}

// The base in memory: every frame for a remote reaches its handheld, unless the test says it failed.
function world(house) {
  const backend = shared.Remote.create(house, { backend: "mock", latency: LATENCY });
  const calls = spy(backend);
  const handhelds = new Map();
  const sent = [];
  const base = { failNext: 0, frames: 0 };
  const logs = [];
  const bridge = Bridge.create({
    shared: { Session: shared.Session, house, layouts }, backend, log: (line) => logs.push(line),
    send: (mac, frame, done) => {
      base.frames += 1;
      sent.push(mac);
      const fail = base.failNext > 0;
      if (fail) base.failNext -= 1;
      setImmediate(() => {
        done(!fail);
        const handheld = handhelds.get(mac);
        if (!fail && handheld) handheld.deliver(frame);
      });
    }
  });
  function handheld(mac, key, store) {
    const entry = new Handheld(bridge, mac, key);
    handhelds.set(mac, entry);
    bridge.add({ name: "remote-" + mac.slice(-2), mac, key, store: store || memoryStore() });
    return entry;
  }
  return { backend, calls, bridge, handheld, base, sent, logs };
}

class Handheld {
  constructor(bridge, mac, key) {
    this.bridge = bridge;
    this.mac = mac;
    this.end = new Link.RemoteEnd({ key });
    this.inbox = [];
    this.synced = 0;
    this.op = 100;
    this.screen = null;
    this.audio = undefined;
  }

  deliver(frame) {
    const result = this.end.receive(frame);
    if (!result) return;
    if (result.synced) this.synced += 1;
    if (result.message) {
      this.inbox.push(result.message);
      if (result.message.screen) this.screen = result.message.screen;
      if (Object.prototype.hasOwnProperty.call(result.message, "audio")) this.audio = result.message.audio;
    }
  }

  hello(fields) {
    return this.bridge.receive(this.mac, this.end.hello(Object.assign({}, STATUS, { have: this.have() }, fields)));
  }

  have() { return this.screen ? this.screen.id : ""; }

  // One operation: its fields, with a new op number unless given.
  send(fields) {
    const message = Object.assign({ op: fields.op !== undefined ? fields.op : ++this.op }, fields);
    this.end.send(message).forEach((frame) => this.bridge.receive(this.mac, frame));
    return message.op;
  }

  take() {
    const messages = this.inbox;
    this.inbox = [];
    return messages;
  }

  acks() { return this.take().filter((message) => message.ack !== undefined).map((message) => message.ack); }
}

async function connected(state, mac, key, store) {
  const handheld = state.handheld(mac, key, store);
  await settle();
  handheld.hello();
  await settle();
  return handheld;
}

// The rows, beside and buttons the screen must have for a layout, from remotes.js itself.
function expectedLayout(remote) {
  const rows = [];
  if (remote.top) rows.push({ label: "", keys: remote.top, columns: 0 });
  const playback = remote.playback || [];
  if (playback.length && (!remote.corners || playback.length > 3)) rows.push({ label: "", keys: playback, columns: 0 });
  const groups = remote.keyGroups || [];
  groups.filter((group) => group.primary).concat(groups.filter((group) => !group.primary))
    .forEach((group) => rows.push({ label: group.label, keys: group.keys, columns: group.columns || 0 }));
  const beside = remote.corners && playback.length <= 3 ? playback : [];
  return { rows, beside, corners: remote.corners, channel: Boolean(remote.channel) };
}

function checkButtons(screen) {
  const ids = new Set(["volume_up", "volume_down", "mute"]);
  if (screen.pad) screen.pad.corners.filter(Boolean).concat(["up", "down", "left", "right", "ok"]).forEach((id) => ids.add(id));
  screen.rows.forEach((row) => row.keys.forEach((id) => ids.add(id)));
  screen.beside.forEach((id) => ids.add(id));
  if (screen.channel) ["channel_up", "channel_down"].forEach((id) => ids.add(id));
  assert.deepEqual(new Set(Object.keys(screen.buttons)), ids);
  Object.keys(screen.buttons).forEach((id) => {
    const spec = layouts.buttons[id];
    const wanted = { label: spec.label };
    if (spec.icon) wanted.icon = spec.icon;
    if (spec.text) wanted.text = spec.text;
    if (spec.repeat) wanted.repeat = true;
    assert.deepEqual(screen.buttons[id], wanted);
  });
}

function checkId(screen) {
  const fields = Object.assign({}, screen);
  delete fields.id;
  assert.equal(screen.id, Screen.digest(JSON.stringify(fields)));
  assert.equal(Object.keys(screen)[0], "id");
}

const cases = {};
async function run(name, body) {
  try {
    cases[name] = Object.assign({ ok: true }, await body());
  } catch (error) {
    cases[name] = { ok: false, error: String((error && error.stack) || error) };
  }
}

(async () => {
  await run("link_reproduces_vectors", async () => {
    const key = Buffer.from(vectors.key, "hex");
    vectors.frames.forEach((entry) => {
      const frame = Link.seal(key, entry.direction, {
        type: entry.type, message: entry.message, index: entry.index, count: entry.count,
        session: BigInt("0x" + entry.session), counter: entry.counter, plaintext: Buffer.from(entry.plaintext, "utf8")
      });
      assert.equal(frame.toString("hex"), entry.frame, entry.name);
    });
    vectors.rejects.forEach((entry) => assert.equal(Link.open(key, entry.direction, Buffer.from(entry.frame, "hex")), null, entry.name));
    return { frames: vectors.frames.length, rejects: vectors.rejects.length };
  });

  await run("hello_sync_screen_off_room", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    assert.equal(remote.synced, 1);
    const messages = remote.take();
    assert.equal(messages.length, 1);
    const screen = messages[0].screen;
    assert.ok(screen);
    checkId(screen);
    assert.equal(messages[0].audio, null);
    assert.equal(screen.room, shared.house.rooms[0].id);
    assert.equal(screen.hidden, true);
    assert.deepEqual([screen.remote, screen.pad, screen.rows, screen.beside, screen.channel], [null, null, [], [], false]);
    assert.equal(screen.selected, "off");
    assert.equal(screen.led, "off");
    assert.equal(screen.allOff, false);
    assert.deepEqual(screen.rooms.map((room) => room.id), shared.house.rooms.map((room) => room.id));
    assert.deepEqual(Object.keys(screen.bubbles[0]), ["id", "label", "state", "lock", "inert"]);
    assert.deepEqual(screen.bubbles.map((bubble) => bubble.id).slice(0, 1), ["off"]);
    assert.equal(screen.bubbles[0].state, "current");
    checkButtons(screen);
    return { bubbles: screen.bubbles.map((bubble) => bubble.id), bytes: JSON.stringify(screen).length };
  });

  await run("bubble_then_key_reaches_the_session", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.take();
    const room = shared.house.rooms[0].id;
    const op = remote.send({ bubble: "example_player" });
    await settle();
    const messages = remote.take();
    assert.deepEqual(messages.filter((message) => message.ack !== undefined).map((message) => message.ack), [op]);
    assert.deepEqual(state.calls.filter((call) => call[0] === "choose"), [["choose", room, "example_player"]]);
    const screen = remote.screen;
    assert.equal(screen.hidden, false);
    assert.equal(screen.short, "Player");
    assert.equal(screen.selected, "example_player");
    assert.equal(screen.bubbles.find((bubble) => bubble.id === "example_player").state, "current");
    assert.ok(remote.audio && remote.audio.short === "Example TV");
    const before = state.calls.length;
    const key = remote.send({ key: ["play_pause", "play"] });
    await settle();
    const after = remote.take();
    assert.deepEqual(state.calls.slice(before), [["press", room, "example_player", "play_pause"]]);
    assert.ok(after.some((message) => message.ack === key));
    const toast = after.find((message) => message.toast);
    assert.ok(toast && toast.toast.title === "Play/pause", JSON.stringify(after));
    assert.deepEqual(Object.keys(toast.toast), ["title", "body", "tone"]);
    return { toast: toast.toast };
  });

  await run("apple_tv_layout", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.send({ bubble: "example_player" });
    await settle();
    const screen = remote.screen;
    checkId(screen);
    const layout = layouts.remotes.apple_tv;
    const expected = expectedLayout(layout);
    assert.deepEqual(screen.remote, { label: layout.label, note: "" });
    assert.deepEqual(screen.pad, { corners: expected.corners });
    assert.deepEqual(screen.rows, expected.rows);
    assert.deepEqual(screen.beside, expected.beside);
    assert.equal(screen.channel, expected.channel);
    checkButtons(screen);
    return { pad: screen.pad, rows: screen.rows, beside: screen.beside, channel: screen.channel };
  });

  await run("sony_tv_layout", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.send({ bubble: "example_player" });
    await settle();
    // A device bubble brings up its own remote and switches nothing.
    const before = state.calls.length;
    remote.send({ bubble: "example_tv" });
    await settle();
    assert.equal(state.calls.length, before);
    const screen = remote.screen;
    checkId(screen);
    const layout = layouts.remotes.sony_tv;
    const expected = expectedLayout(layout);
    assert.equal(screen.selected, "example_tv");
    assert.deepEqual(screen.remote, { label: layout.label, note: "" });
    assert.deepEqual(screen.pad, { corners: expected.corners });
    assert.deepEqual(screen.rows, expected.rows);
    assert.deepEqual(screen.beside, []);
    assert.equal(screen.channel, true);
    assert.ok(screen.buttons.channel_up && screen.buttons.channel_down);
    checkButtons(screen);
    const bytes = Buffer.byteLength(JSON.stringify({ ack: 4294967295, screen, audio: remote.audio }));
    assert.ok(bytes <= Link.MESSAGE_MAX, bytes);
    return { rows: screen.rows.map((row) => row.label + ":" + row.keys.length), channel: screen.channel, bytes,
      fields: Object.keys(screen), audioFields: Object.keys(remote.audio) };
  });

  await run("note_layout_has_no_keys", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.send({ bubble: "example_computer" });
    await settle();
    const screen = remote.screen;
    assert.equal(screen.hidden, false);
    assert.equal(screen.remote.label, layouts.remotes.pc.label);
    assert.equal(screen.remote.note, layouts.remotes.pc.note.replace("{device}", "Example Computer"));
    assert.deepEqual([screen.pad, screen.rows, screen.beside, screen.channel], [null, [], [], false]);
    return { note: screen.remote.note };
  });

  await run("audio_change_sends_audio_not_screen", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.send({ bubble: "example_player" });
    await settle();
    remote.take();
    const id = remote.screen.id;
    const level = remote.audio.value;
    remote.send({ key: ["volume_up"] });
    await settle(LATENCY * 6);
    const messages = remote.take();
    assert.ok(messages.some((message) => message.audio), JSON.stringify(messages));
    assert.ok(!messages.some((message) => message.screen), JSON.stringify(messages));
    assert.equal(remote.screen.id, id);
    assert.equal(remote.audio.value, level + 1);
    assert.deepEqual(Object.keys(remote.audio), ["short", "value", "min", "max", "step", "muted", "disabled", "text", "caption"]);
    return { before: level, after: remote.audio.value };
  });

  await run("all_off_takes_two_presses", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.send({ bubble: "example_player" });
    await settle();
    remote.send({ allOff: true });
    await settle();
    assert.equal(remote.screen.allOff, true);
    assert.equal(state.calls.filter((call) => call[0] === "allOff").length, 0);
    remote.send({ allOff: true });
    await settle();
    assert.equal(state.calls.filter((call) => call[0] === "allOff").length, 1);
    assert.equal(remote.screen.allOff, false);
    await settle(LATENCY * 4);
    assert.equal(remote.screen.hidden, true);
    return { sent: 1 };
  });

  await run("input_list_round_trip", async () => {
    const house = inputHouse();
    const state = world(house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.take();
    remote.send({ bubble: "lab_tv" });
    await settle();
    assert.equal(remote.screen.remote.label, layouts.remotes.sony_tv.label);
    remote.send({ key: ["input"] });
    await settle();
    const list = remote.take().find((message) => message.inputs);
    assert.ok(list, "no input list");
    assert.deepEqual(list.inputs, { title: "TV input", options: [
      { value: "hdmi1", label: "HDMI 1", inUse: true }, { value: "hdmi2", label: "HDMI 2", inUse: false }] });
    const before = state.calls.length;
    remote.send({ input: "hdmi9" });
    await settle();
    assert.equal(state.calls.length, before, "an input the list lacks is not chosen");
    remote.send({ input: "hdmi2" });
    await settle();
    assert.deepEqual(state.calls.slice(before), [["setInput", "lab", "lab_tv", "hdmi2"]]);
    return { options: list.inputs.options.length };
  });

  await run("room_and_two_remotes", async () => {
    const state = world(shared.house);
    const storeA = memoryStore();
    const storeB = memoryStore();
    const a = await connected(state, MAC_A, KEY_A, storeA);
    const b = await connected(state, MAC_B, KEY_B, storeB);
    const [west, east] = shared.house.rooms.map((room) => room.id);
    a.send({ room: east });
    await settle();
    assert.equal(a.screen.room, east);
    assert.equal(b.screen.room, west);
    assert.deepEqual([storeA.data["remote.room"], storeB.data["remote.room"]], [east, west]);
    // An unknown room changes nothing (the session would fall back to the first).
    a.send({ room: "nowhere" });
    await settle();
    assert.equal(a.screen.room, east);
    // A switch on one remote shows on the other's room list.
    b.send({ bubble: "example_player" });
    await settle();
    assert.equal(a.screen.rooms.find((room) => room.id === west).short, "Player");
    assert.equal(a.screen.room, east);
    return { a: a.screen.room, b: b.screen.room };
  });

  await run("dedupe_acks_repeats_without_reapplying", async () => {
    const state = world(shared.house);
    const remote = await connected(state, MAC_A, KEY_A);
    remote.send({ bubble: "example_player" });
    await settle();
    remote.take();
    const before = state.calls.length;
    remote.send({ op: 7000, key: ["menu", "home"] });
    remote.send({ op: 7000, key: ["menu", "home"] });
    await settle();
    // The same op after a new session (HELLO) is still a repeat.
    remote.hello();
    await settle();
    remote.send({ op: 7000, key: ["menu", "home"] });
    await settle();
    const acks = remote.acks();
    assert.deepEqual(acks, [7000, 7000, 7000]);
    assert.equal(state.calls.slice(before).filter((call) => call[0] === "press").length, 1);
    // Thirty-two later ops push 7000 out of memory: it applies again.
    for (let op = 7001; op <= 7032; op += 1) remote.send({ op });
    remote.send({ op: 7000, key: ["menu", "home"] });
    await settle();
    assert.equal(state.calls.slice(before).filter((call) => call[0] === "press").length, 2);
    const ring = new Bridge.Applied();
    for (let op = 0; op < 40; op += 1) ring.add(op);
    assert.equal(ring.set.size, Bridge.REMEMBERED);
    return { acks: acks.length };
  });

  await run("sleep_then_wake", async () => {
    const state = world(shared.house);
    const a = await connected(state, MAC_A, KEY_A);
    const b = await connected(state, MAC_B, KEY_B);
    a.take();
    const old = a.screen.id;
    const op = a.send({ sleep: true });
    await settle();
    assert.deepEqual(a.take(), [{ ack: op }]);
    const frames = state.base.frames;
    // The room changes from another remote: nothing reaches the sleeping one.
    b.send({ bubble: "example_player" });
    await settle();
    assert.deepEqual(a.take(), []);
    assert.equal(state.sent.slice(frames).filter((mac) => mac === MAC_A).length, 0);
    // Waking with the old screen: the whole screen and audio.
    const wake = a.send({ have: old });
    await settle();
    let messages = a.take();
    assert.equal(messages.length, 1);
    assert.equal(messages[0].ack, wake);
    assert.ok(messages[0].screen && messages[0].screen.id !== old);
    assert.ok(Object.prototype.hasOwnProperty.call(messages[0], "audio"));
    // Asleep again, nothing changes, and it wakes showing the current screen: only the ack.
    a.send({ sleep: true });
    await settle();
    a.take();
    const again = a.send({ have: a.screen.id });
    await settle();
    messages = a.take();
    assert.deepEqual(messages, [{ ack: again }]);
    return { woke: wake };
  });

  await run("restarted_bridge_keeps_the_id", async () => {
    const first = world(shared.house);
    const remote = await connected(first, MAC_A, KEY_A);
    const id = remote.screen.id;
    // Another bridge on the same house: the remote says HELLO showing that screen.
    const second = world(shared.house);
    const again = second.handheld(MAC_A, KEY_A);
    again.screen = remote.screen;
    await settle();
    again.hello();
    await settle();
    const messages = again.take();
    assert.ok(!messages.some((message) => message.screen), JSON.stringify(messages));
    assert.equal(again.synced, 1);
    return { id };
  });

  await run("unknown_mac_dropped", async () => {
    const state = world(shared.house);
    await connected(state, MAC_A, KEY_A);
    const stranger = new Link.RemoteEnd({ key: KEY_A });
    const frames = state.base.frames;
    assert.equal(state.bridge.receive(mac("99"), stranger.hello(STATUS)), false);
    // A known MAC with a frame sealed under another key is dropped too.
    const forged = new Link.RemoteEnd({ key: KEY_B });
    assert.equal(state.bridge.receive(MAC_A, forged.hello(STATUS)), false);
    await settle();
    assert.equal(state.base.frames, frames);
    assert.equal(state.bridge.dropped(), 1);
    return { dropped: state.bridge.dropped() };
  });

  await run("sent_fail_marks_asleep", async () => {
    const state = world(shared.house);
    const a = await connected(state, MAC_A, KEY_A);
    const b = await connected(state, MAC_B, KEY_B);
    a.take();
    state.base.failNext = 1;
    const op = a.send({ key: ["volume_up"] });
    await settle();
    assert.equal(state.bridge.remote(MAC_A).awake, false);
    const frames = state.sent.length;
    b.send({ bubble: "example_player" });
    await settle();
    assert.equal(state.sent.slice(frames).filter((mac) => mac === MAC_A).length, 0);
    // It speaks again, reporting nothing it shows: the whole screen.
    a.send({ op: op });
    await settle();
    const messages = a.take();
    assert.ok(messages.some((message) => message.ack === op && message.screen), JSON.stringify(messages));
    return { awake: state.bridge.remote(MAC_A).awake };
  });

  await run("forget_stops_the_session", async () => {
    const state = world(shared.house);
    const a = await connected(state, MAC_A, KEY_A);
    assert.equal(state.bridge.remove(MAC_A), true);
    const frames = state.base.frames;
    a.send({ bubble: "example_player" });
    await settle();
    assert.equal(state.base.frames, frames);
    assert.equal(state.bridge.remote(MAC_A), null);
    return {};
  });

  await run("line_protocol", async () => {
    const parse = Base.parse;
    assert.deepEqual(parse("I (1234) boot: ESP-IDF v5.5"), { type: "log", text: "I (1234) boot: ESP-IDF v5.5" });
    assert.deepEqual(parse("thermite base 1 0.1.0 " + mac("b1") + " 6\r"),
      { type: "base", protocol: 1, fw: "0.1.0", mac: mac("b1"), channel: 6 });
    assert.deepEqual(parse("thermite base 1 sim " + mac("fe") + " 1"),
      { type: "base", protocol: 1, fw: "sim", mac: mac("fe"), channel: 1 });
    assert.deepEqual(parse("thermite sent 65535 ok"), { type: "sent", id: 65535, ok: true });
    assert.deepEqual(parse("thermite sent 1 fail"), { type: "sent", id: 1, ok: false });
    const rx = parse("thermite rx " + mac("0a") + " -61 AQID");
    assert.deepEqual([rx.type, rx.mac, rx.rssi, Array.from(rx.frame)], ["rx", mac("0a"), -61, [1, 2, 3]]);
    assert.deepEqual(parse("thermite error unknown command"), { type: "error", words: "unknown command" });
    assert.equal(parse("thermite").type, "log");
    const malformed = ["thermite base 1 0.1.0 " + mac("B1") + " 6", "thermite base 1 0.1.0 " + mac("b1"),
      "thermite sent 0 ok", "thermite sent 65536 ok", "thermite sent 5 maybe", "thermite rx " + mac("0a") + " -61 AQI",
      "thermite rx " + mac("0a") + " loud AQID", "thermite rx " + mac("0a") + " -61 !!!!", "thermite sideways",
      "thermite base 1 0.1.0 " + mac("b1") + " 99", "thermite base 1 0.1.0 " + mac("b1") + " 14"];
    malformed.forEach((line) => assert.equal(parse(line).type, "malformed", line));

    // One Link over an in-memory stream: ids wrap, one sent each, logs and noise ignored.
    const written = [];
    const stream = new Duplex({ read() {}, write(chunk, encoding, done) { written.push(String(chunk)); done(); } });
    const seen = { base: [], rx: [], malformed: 0, log: 0, closed: 0 };
    const link = new Base.Link(stream, {
      base: (entry) => seen.base.push(entry), rx: (entry) => seen.rx.push(entry),
      malformed: () => { seen.malformed += 1; }, log: () => { seen.log += 1; }, close: () => { seen.closed += 1; }
    });
    link.next = Base.ID_MAX;
    const results = [];
    const first = link.send(MAC_A, Buffer.from([1, 2, 3]), (ok) => results.push(["first", ok]));
    const second = link.send(MAC_A, Buffer.from([4]), (ok) => results.push(["second", ok]));
    assert.deepEqual([first, second], [Base.ID_MAX, 1]);
    assert.equal(written[0], "thermite send 65535 " + MAC_A + " AQID\n");
    stream.push("boot log line\nthermite base 1 0.2.0 " + mac("b1") + " 3\r\nthermite sent 65535 ok\n");
    stream.push("thermite sent 65535 fail\nthermite rx " + MAC_B + " -40 BQY=\nthermite bogus\nthermite sent 1 fa");
    stream.push("il\n" + "x".repeat(Base.LINE_MAX + 10) + "\nthermite sent 9 ok\n");
    await turn();
    assert.deepEqual(results, [["first", true], ["second", false]]);
    assert.equal(seen.base.length, 1);
    assert.equal(seen.rx.length, 1);
    assert.deepEqual(Array.from(seen.rx[0].frame), [5, 6]);
    assert.equal(seen.malformed, 2);
    assert.equal(seen.log, 1);
    // Closing fails what still waits.
    link.send(MAC_A, Buffer.from([7]), (ok) => results.push(["third", ok]));
    stream.destroy();
    await settle(5);
    assert.deepEqual(results[2], ["third", false]);
    assert.equal(seen.closed, 1);
    return { written: written.length };
  });

  await run("op_before_the_session_is_ready_is_not_acked", async () => {
    const state = world(shared.house);
    const remote = state.handheld(MAC_A, KEY_A);
    // HELLO is answered at once; an operation waits for the session to be drawn.
    remote.hello();
    remote.send({ op: 1, key: ["volume_up"] });
    assert.equal(state.bridge.remote(MAC_A).ready, false);
    await settle();
    const messages = remote.take();
    assert.equal(remote.synced, 1);
    assert.ok(!messages.some((message) => message.ack === 1), JSON.stringify(messages));
    assert.ok(messages.some((message) => message.screen));
    assert.equal(state.calls.filter((call) => call[0] === "press").length, 0);
    return {};
  });

  await run("reconnects_with_backoff_and_probes", async () => {
    const fs = require("fs");
    const os = require("os");
    // A port that fails to open, then opens, then goes away: the bridge reopens it.
    const opened = [];
    const logs = [];
    let attempts = 0;
    const port = () => {
      const written = [];
      const stream = new Duplex({ read() {}, write(chunk, encoding, done) { written.push(String(chunk)); done(); } });
      stream.written = written;
      return stream;
    };
    const base = Base.connect({
      port: "/dev/fake-base", log: (line) => logs.push(line), onBase: (entry) => opened.push(entry.mac),
      open: () => {
        attempts += 1;
        if (attempts === 1) throw new Error("no such device");
        const stream = port();
        setImmediate(() => stream.push("thermite base 1 0.1.0 " + mac("b" + attempts) + " 1\n"));
        return stream;
      }
    });
    await settle(Base.BACKOFF_MS[0] + 100);
    assert.equal(attempts, 2);
    assert.deepEqual(opened, [mac("b2")]);
    assert.equal(base.link().stream.written[0], "thermite hello\n");
    // Sends while unplugged are not delivered.
    base.link().stream.destroy();
    await turn();
    const result = await new Promise((resolve) => base.send(MAC_A, Buffer.from([1]), resolve));
    assert.equal(result, false);
    await settle(Base.BACKOFF_MS[0] + 100);
    assert.equal(attempts, 3);
    assert.deepEqual(opened, [mac("b2"), mac("b3")]);
    base.close();

    // Probing: only Espressif ports, and only one that answers as a base.
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "handheld-by-id-"));
    ["usb-Espressif_USB_JTAG_serial_debug_unit_AA-if00", "usb-Espressif_USB_JTAG_serial_debug_unit_BB-if00",
      "usb-Other_Serial-if00"].forEach((name) => fs.writeFileSync(path.join(directory, name), ""));
    const asked = [];
    const answers = { AA: "thermite remote 1 0.1.0 " + mac("0a") + " waveshare 1\n", BB: "noise\nthermite base 1 0.1.0 " + mac("bb") + " 6\n" };
    const found = await Base.probe(Base.candidates(directory), (device) => {
      asked.push(path.basename(device));
      const stream = port();
      const id = /unit_([A-Z]+)/.exec(device)[1];
      stream._write = (chunk, encoding, done) => { if (String(chunk) === "thermite hello\n") stream.push(answers[id]); done(); };
      return stream;
    }, 200);
    fs.rmSync(directory, { recursive: true });
    assert.equal(path.basename(found.device), "usb-Espressif_USB_JTAG_serial_debug_unit_BB-if00");
    assert.equal(found.base.mac, mac("bb"));
    assert.deepEqual(asked, ["usb-Espressif_USB_JTAG_serial_debug_unit_AA-if00", "usb-Espressif_USB_JTAG_serial_debug_unit_BB-if00"]);
    // The port the probe paused flows again once a Link reads it.
    const heard = [];
    const link = new Base.Link(found.stream, { rx: (entry) => heard.push(entry.mac) });
    found.stream.push("thermite rx " + mac("0a") + " -50 AQID\n");
    await settle(5);
    assert.deepEqual(heard, [mac("0a")]);
    link.close();
    return { attempts, logs: logs.length };
  });

  process.stdout.write(JSON.stringify({ cases }) + "\n");
  // The mock's timers would keep node alive a moment longer.
  process.exit(0);
})();
