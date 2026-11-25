"""thermite handheld: the bridge (handheld/*.js) and its command.

The bridge runs on the page's own session and mock backend with simulated
remotes (tests/handheld_bridge_harness.js). The command enrolls fake remotes
on pseudo-terminals that speak the console lines, and serve runs end to end
against a simulated base (tests/handheld_sim_harness.js) and a fake base on
a pseudo-terminal. Nothing reaches a network or a device.
"""
from pathlib import Path
import contextlib
import io
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
from oh_cli import handheld, tui

NODE = shutil.which("node")


def mac(last):
    """A fictional, locally administered MAC."""
    return "02:00:00:00:00:" + last


REMOTE_MAC = mac("a1")
BASE_MAC = mac("b1")
MODEL = {"version": 1, "rooms": [{"id": "synthetic_room", "label": "Synthetic room", "sources": []}],
         "sources": {}, "devices": {}, "initial": {}}
CONTENT = ("globalThis.REMOTE_HOUSE = " + json.dumps(MODEL) + ";\n").encode()


@unittest.skipUnless(NODE, "Node is required for the actual shared session and backend")
class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([NODE, str(AREA / "tests/handheld_bridge_harness.js"), str(AREA)],
                             text=True, capture_output=True, timeout=60)
        if run.returncode:
            raise AssertionError(run.stderr or run.stdout)
        cls.cases = json.loads(run.stdout)["cases"]

    def case(self, name):
        result = self.cases[name]
        self.assertTrue(result["ok"], result.get("error"))
        return result

    def test_link_reproduces_the_vectors(self):
        self.assertEqual(self.case("link_reproduces_vectors"), {"ok": True, "frames": 7, "rejects": 7})

    def test_hello_is_answered_with_sync_then_the_screen(self):
        self.assertEqual(self.case("hello_sync_screen_off_room")["bubbles"][0], "off")
        self.case("op_before_the_session_is_ready_is_not_acked")

    def test_operations_reach_the_session(self):
        self.assertEqual(self.case("bubble_then_key_reaches_the_session")["toast"]["title"], "Play/pause")
        self.case("all_off_takes_two_presses")
        self.case("input_list_round_trip")

    def test_screens_follow_the_layouts_as_the_terminal_places_them(self):
        apple = self.case("apple_tv_layout")
        self.assertEqual(apple["beside"], ["play_pause"])
        self.assertEqual(apple["pad"], {"corners": ["power", None, "back", "tv"]})
        sony = self.case("sony_tv_layout")
        self.assertTrue(sony["channel"])
        self.assertLess(sony["bytes"], 8192)
        self.case("note_layout_has_no_keys")
        self.case("hello_sync_screen_off_room")

    def test_the_simulators_fixtures_have_the_bridges_shape(self):
        """The firmware's render fixtures (handheld/firmware/host/fixtures) are
        written by hand: they must keep the fields the bridge sends, and button
        specs without the defaults the bridge leaves out."""
        sony = self.case("sony_tv_layout")
        listen = self.case("watch_and_listen_mode_switching")
        fixtures = sorted((AREA / "handheld/firmware/host/fixtures").glob("*.json"))
        self.assertTrue(fixtures)
        for path in fixtures:
            data = json.loads(path.read_text(encoding="utf-8"))
            for message in data if isinstance(data, list) else [data]:
                if message.get("screen"):
                    screen = message["screen"]
                    expected = listen["listenFields"] if screen.get("mode") == "listen" else sony["fields"]
                    self.assertEqual(sorted(screen), sorted(expected), path.name)
                    for spec in screen["buttons"].values():
                        self.assertLessEqual(set(spec), {"label", "icon", "text", "repeat"}, path.name)
                        self.assertNotIn("", (spec.get("icon"), spec.get("text")), path.name)
                        self.assertIsNot(spec.get("repeat"), False, path.name)
                if message.get("audio"):
                    self.assertEqual(sorted(message["audio"]), sorted(sony["audioFields"]), path.name)

    def test_a_screen_says_what_has_failed_for_as_long_as_it_lasts(self):
        """On the gateway's backend: a choice never read back is marked on its
        bubble and named in the problem line, and while the house is not heard
        no light or bubble says what was read before."""
        result = self.case("what_has_failed_reaches_the_screen")
        self.assertEqual(result["heard"], ["ok", "current", ""])
        led, player, problem = result["unconfirmed"]
        self.assertEqual((led, player), ("ok", "current"), "the room still shows what it last read")
        self.assertIn("Computer was not confirmed", problem)
        self.assertEqual(result["lost"], ["stale", "stale", "Not connected to thermite. Reconnecting\u2026", "Player"])
        self.assertEqual(result["again"], ["current", "idle", ""])

    def test_audio_alone_sends_audio(self):
        result = self.case("audio_change_sends_audio_not_screen")
        self.assertEqual(result["after"], result["before"] + 1)

    def test_dedupe_sleep_delivery_and_restarts(self):
        for name in ("dedupe_acks_repeats_without_reapplying", "sleep_then_wake", "sent_fail_marks_asleep",
                     "restarted_bridge_keeps_the_id", "forget_stops_the_session"):
            with self.subTest(name):
                self.case(name)

    def test_remotes_are_independent_and_strangers_dropped(self):
        self.assertEqual(self.case("room_and_two_remotes"), {"ok": True, "a": "example_east", "b": "example_west"})
        self.assertEqual(self.case("unknown_mac_dropped")["dropped"], 1)

    def test_base_lines_and_reconnection(self):
        self.case("line_protocol")
        self.assertEqual(self.case("reconnects_with_backoff_and_probes")["attempts"], 3)

    def test_listen_mode_and_speaker_only_house(self):
        self.case("watch_and_listen_mode_switching")
        self.assertEqual(self.case("speaker_only_house"), {"ok": True, "room": "garden", "mode": "listen"})

    def test_multi_remote_locks_and_per_remote_attribution(self):
        result = self.case("multi_remote_locks_and_per_remote_attribution")
        self.assertEqual(result["attributed"], ["handheld/lounge", "handheld/den"])

    def test_health_metrics_textfile_formatter_and_atomic_write(self):
        self.assertGreater(self.case("health_metrics_textfile")["lines"], 5)

    def test_every_case_ran(self):
        self.assertEqual(len(self.cases), 24)
        failed = {name: result["error"] for name, result in self.cases.items() if not result["ok"]}
        self.assertEqual(failed, {})

    def _run_node(self, script, *args, timeout=20):
        return subprocess.run([NODE, "-e", script, str(AREA), *args],
                              text=True, capture_output=True, timeout=timeout)

    def test_hello_replay_protection(self):
        script = r"""
const assert = require("assert/strict");
const path = require("path");
const area = process.argv[1];
const Shared = require(path.join(area, "tui/remote/shared"));
const Link = require(path.join(area, "handheld/link"));
const Bridge = require(path.join(area, "handheld/bridge"));

console.info = console.log = () => {};
const shared = Shared.load();
Shared.mocks();

(async () => {
  const key = Buffer.alloc(32, 0xc3);
  const mac = "02:00:00:00:00:" + "c3";
  const backend = shared.Remote.create(shared.house, { backend: "mock", latency: 2 });
  const remoteEnd = new Link.RemoteEnd({ key });
  const inbox = [];
  let failNext = 0;
  let sentFrames = 0;
  const bridge = Bridge.create({
    shared: { Session: shared.Session, house: shared.house, layouts: shared.layouts },
    backend,
    send: (to, frame, done) => {
      sentFrames += 1;
      const fail = failNext > 0;
      if (fail) failNext -= 1;
      setImmediate(() => {
        done(!fail);
        if (!fail) {
          const r = remoteEnd.receive(frame);
          if (r && r.message) inbox.push(r.message);
        }
      });
    }
  });
  bridge.add({ name: "living", mac, key, store: { get: () => null, set: () => {} } });
  await new Promise((r) => setTimeout(r, 30));

  // 1. Cold start HELLO adopts immediately and pushes screen.
  const firstHello = remoteEnd.hello({ have: "", battery: 80, charging: false, board: "sim", fw: "1.0", w: 240, h: 320 });
  assert.equal(bridge.receive(mac, firstHello), true);
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(inbox.some((m) => m.screen), "cold start should push screen");
  const activeSession = bridge.remote(mac).end.session;
  assert.ok(activeSession !== null);
  assert.equal(bridge.remote(mac).status.battery, 80);

  // 2. Immediate replay of the exact same HELLO nonce is dropped outright.
  const framesBeforeReplay = sentFrames;
  assert.equal(bridge.receive(mac, firstHello), false);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sentFrames, framesBeforeReplay, "seen HELLO replay must send no SYNC");
  assert.equal(bridge.remote(mac).end.session, activeSession);

  // 3. Captured HELLO with an unseen nonce (from a prior run) with poisoned status
  //    stays pending, never tears down activeSession, never poisons status, and even
  //    a failed pending SYNC does not mark the active session asleep.
  const attacker = new Link.RemoteEnd({ key });
  const capturedHello = attacker.hello({ have: "poison", battery: 1, charging: true, board: "evil", fw: "0.0", w: 240, h: 320 });
  failNext = 1; // simulate pending SYNC failing delivery
  assert.equal(bridge.receive(mac, capturedHello), true);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(bridge.remote(mac).end.session, activeSession, "active session overwritten by pending HELLO");
  assert.ok(bridge.remote(mac).end.pending !== null, "pending session should exist");
  assert.equal(bridge.remote(mac).status.battery, 80, "status poisoned by unpromoted HELLO");
  assert.equal(bridge.remote(mac).awake, true, "failed pending SYNC marked active session asleep");

  // Replaying that same captured HELLO a second time is now dropped by seenHellos.
  const framesBeforeSecond = sentFrames;
  assert.equal(bridge.receive(mac, capturedHello), false);
  assert.equal(sentFrames, framesBeforeSecond);

  // Active session continues working undisturbed.
  inbox.length = 0;
  remoteEnd.send({ op: 501, key: ["volume_up"] }).forEach((f) => assert.equal(bridge.receive(mac, f), true));
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(inbox.some((m) => m.ack === 501), "active session DATA not acked after HELLO replay");
  assert.equal(bridge.remote(mac).end.session, activeSession);

  // 4. Legitimate remote re-HELLO + authenticated DATA promotes pending atomically.
  inbox.length = 0;
  const freshHello = remoteEnd.hello({ have: "", battery: 42, charging: true, board: "sim", fw: "1.1", w: 240, h: 320 });
  assert.equal(bridge.receive(mac, freshHello), true);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(bridge.remote(mac).end.session, activeSession, "should not promote before authenticated DATA");
  assert.equal(bridge.remote(mac).status.battery, 80, "status should not commit before authenticated DATA");

  remoteEnd.send({ op: 502 }).forEach((f) => assert.equal(bridge.receive(mac, f), true));
  await new Promise((r) => setTimeout(r, 30));
  assert.notEqual(bridge.remote(mac).end.session, activeSession, "pending session not promoted on authenticated DATA");
  assert.equal(bridge.remote(mac).end.pending, null, "pending should clear after promotion");
  assert.equal(bridge.remote(mac).status.battery, 42, "HELLO status not committed on promotion");
  assert.ok(inbox.some((m) => m.ack === 502 && m.screen), "promoted session did not ack and send screen");
  process.exit(0);
})().catch((err) => { console.error(err); process.exit(1); });
"""
        run = self._run_node(script)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_empty_routed_house_is_guarded(self):
        script = r"""
const assert = require("assert/strict");
const path = require("path");
const area = process.argv[1];
const Shared = require(path.join(area, "tui/remote/shared"));
const Link = require(path.join(area, "handheld/link"));
const Bridge = require(path.join(area, "handheld/bridge"));
const Screen = require(path.join(area, "handheld/screen"));

console.info = console.log = () => {};
const shared = Shared.load();
Shared.mocks();

(async () => {
  const house = { version: 1, rooms: [], sources: {}, devices: {}, initial: {} };
  const key = Buffer.alloc(32, 0xd4);
  const mac = "02:00:00:00:00:" + "d4";
  const backend = shared.Remote.create(house, { backend: "mock", latency: 2 });
  const remoteEnd = new Link.RemoteEnd({ key });
  const inbox = [];
  const logs = [];
  const errors = [];
  const bridge = Bridge.create({
    shared: { Session: shared.Session, house, layouts: shared.layouts },
    backend,
    log: (line) => logs.push(line),
    onError: (err) => errors.push(err),
    send: (to, frame, done) => {
      setImmediate(() => {
        done(true);
        const r = remoteEnd.receive(frame);
        if (r && r.message) inbox.push(r.message);
      });
    }
  });
  bridge.add({ name: "empty-remote", mac, key, store: { get: () => null, set: () => {} } });
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(errors.length, 0, errors.map(String).join("\n"));
  assert.equal(bridge.remote(mac).ready, true);
  assert.ok(logs.some((line) => line.includes("no routed rooms")), logs.join("\n"));

  assert.equal(bridge.receive(mac, remoteEnd.hello({ have: "", battery: 50, charging: false, board: "sim", fw: "1.0", w: 240, h: 320 })), true);
  await new Promise((r) => setTimeout(r, 30));
  const msg = inbox.find((m) => m.screen);
  assert.ok(msg, "expected empty-house screen");
  assert.equal(msg.screen.room, "");
  assert.deepEqual(msg.screen.rooms, []);
  assert.deepEqual(msg.screen.bubbles, []);
  assert.equal(msg.screen.hidden, true);
  assert.equal(msg.screen.short, "No rooms");
  assert.equal(msg.audio, null);
  const fields = Object.assign({}, msg.screen);
  delete fields.id;
  assert.equal(msg.screen.id, Screen.digest(JSON.stringify(fields)));

  // Operations on an empty house must ack without throwing TypeError.
  inbox.length = 0;
  remoteEnd.send({ op: 1, key: ["volume_up"], bubble: "off", room: "missing", input: "hdmi1" })
    .forEach((f) => bridge.receive(mac, f));
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(errors.length, 0, errors.map(String).join("\n"));
  assert.ok(inbox.some((m) => m.ack === 1), "op on empty house was not acked");
  process.exit(0);
})().catch((err) => { console.error(err); process.exit(1); });
"""
        run = self._run_node(script)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)

    def test_unauthorized_exits_nonzero_and_access_ready_precedes_session_start(self):
        with tempfile.TemporaryDirectory(prefix="handheld-auth-", dir=os.environ.get("TMPDIR") or str(AREA / ".scratch")) as tmp:
            root = Path(tmp)
            os.chmod(root, 0o700)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            state_file = state_dir / "state.json"
            handheld.save(state_file, {
                "version": 1, "base": None,
                "remotes": [{"name": "r1", "mac": REMOTE_MAC, "key": "aa" * 32, "board": "sim", "store": {}, "status": {}}]
            })
            house_file = root / "house.js"
            model = {"version": 1,
                     "rooms": [{"id": "synthetic_room", "label": "Synthetic room", "sources": [], "devices": []}],
                     "sources": {}, "devices": {}, "initial": {}}
            house_file.write_text("globalThis.REMOTE_HOUSE = " + json.dumps(model) + ";\n", encoding="utf-8")

            # Case A: startup 401 on /api/v1/me exits non-zero and logs unauthorized.
            script_startup = r"""
const path = require("path");
const [area, stateFile, sockFile, houseFile] = process.argv.slice(1);
process.env.THERMITE_REMOTE_HOUSE_FILE = houseFile;
const reply = (value, status) => {
  const body = JSON.stringify(value);
  return { ok: status < 300, status, headers: { get: () => "application/json", getSetCookie: () => [] },
    text: async () => body, json: async () => JSON.parse(body), clone: () => reply(value, status) };
};
globalThis.fetch = async (url) => {
  const route = new URL(url).pathname;
  if (route === "/api/v1/security") return reply({ bootstrap_required: false, setup_available: false, guest_text: true, guest_cameras: false, csrf: "C".repeat(32) }, 200);
  if (route === "/api/v1/session") return reply({ csrf: "C".repeat(32) }, 200);
  if (route === "/api/v1/me") return reply({ error: "invalid_token" }, 401);
  return reply({ error: "not_found" }, 404);
};
const Main = require(path.join(area, "handheld/main"));
Main.main(["--url", "http://127.0.0.1:9", "--state", stateFile, "--listen", sockFile]);
"""
            run = self._run_node(script_startup, str(state_file), str(root / "a.sock"), str(house_file))
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn("unauthorized", run.stderr.lower())

            # Case B: access.ready() completes before session.start(), and mid-run 401 exits 1 & flushes telemetry.
            script_midrun = r"""
const path = require("path");
const [area, stateFile, sockFile, houseFile] = process.argv.slice(1);
process.env.THERMITE_REMOTE_HOUSE_FILE = houseFile;
const order = [];
let telemetryPosted = false;
const reply = (value, status) => {
  const body = JSON.stringify(value);
  return { ok: status < 300, status, headers: { get: () => "application/json", getSetCookie: () => [] },
    text: async () => body, json: async () => JSON.parse(body), clone: () => reply(value, status) };
};
const Events = require(path.join(area, "tui/remote/events"));
let activeEs = null;
Events.EventSource = class {
  constructor(url) {
    this.readyState = 0;
    activeEs = this;
    setImmediate(() => {
      if (this.readyState === 2) return;
      this.readyState = 1;
      if (this.onopen) this.onopen({});
      if (this.onmessage && url.endsWith("/api/v1/events/remote")) {
        this.onmessage({ data: JSON.stringify({ type: "snapshot", view: "remote", states: { loc_synthetic_room_source: "off" } }) });
      }
    });
  }
  close() { this.readyState = 2; }
};
let meDone = false;
let policyBeforeReady = false;
globalThis.fetch = async (url, init) => {
  const route = new URL(url).pathname;
  order.push(route);
  if (route === "/api/v1/security") return reply({ bootstrap_required: false, setup_available: false, guest_text: true, guest_cameras: false, csrf: "C".repeat(32) }, 200);
  if (route === "/api/v1/session") return reply({ csrf: "C".repeat(32) }, 200);
  if (route === "/api/v1/me") {
    await new Promise((r) => setTimeout(r, 20));
    meDone = true;
    return reply({ id: 1, username: "example", admin: false, can_edit: true, is_guest: false, permissions: ["site.read", "room.control"] }, 200);
  }
  if (route === "/api/v1/telemetry" && init && init.method === "POST") {
    telemetryPosted = true;
    return reply({ ok: true }, 200);
  }
  if (route.endsWith("/policy")) {
    if (!meDone) policyBeforeReady = true;
    // Policy succeeded after access.ready(); now revoke credentials on the event stream
    // so stream_error telemetry is queued and lost() fires unauthorized -> finish(1) flushes it.
    setImmediate(() => {
      if (activeEs && activeEs.onerror) activeEs.onerror({ status: 401 });
    });
    return reply({ room: "synthetic_room", revision: 1, manual_lock: false, can_lock: true, can_edit: true,
      effective: { allowed: true, locked: false, until: null, reason: "" }, enforcement: "enforced" }, 200);
  }
  return reply({ error: "not_found" }, 404);
};
const origExit = process.exit.bind(process);
process.exit = (code) => {
  process.stdout.write(JSON.stringify({ code, meDone, policyBeforeReady, telemetryPosted, order }) + "\n");
  origExit(code);
};
const Main = require(path.join(area, "handheld/main"));
Main.main(["--url", "http://127.0.0.1:9", "--state", stateFile, "--listen", sockFile]);
"""
            run = self._run_node(script_midrun, str(state_file), str(root / "b.sock"), str(house_file))
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn("unauthorized", run.stderr.lower())
            info = json.loads(run.stdout.strip().splitlines()[-1])
            self.assertTrue(info["meDone"])
            self.assertFalse(info["policyBeforeReady"], info)
            self.assertIn("/api/v1/me", info["order"])
            self.assertLess(info["order"].index("/api/v1/me"), info["order"].index("/api/v1/rooms/synthetic_room/policy"))
            self.assertTrue(info["telemetryPosted"], info)


class Scratch(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR") or AREA / ".scratch")
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="handheld-", dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        os.chmod(self.root, 0o700)
        self.environment = {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_CACHE_HOME": str(self.root / "cache"),
                            "XDG_STATE_HOME": str(self.root / "state")}
        self.enterContext(mock.patch.dict(os.environ, self.environment))
        os.environ.pop(oh_cli.URL_VARIABLE, None)
        self.state = self.root / "state/thermite/handheld/state.json"

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = oh_cli.main(["handheld", *argv])
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def data(self):
        return json.loads(self.state.read_text())

    def seed(self, data):
        handheld.private_parent(self.state)
        handheld.save(self.state, data)


class Command(Scratch):
    def test_registered_as_a_use_verb_without_a_site(self):
        self.assertEqual(oh_cli.VERBS["handheld"].group, "use")
        self.assertFalse(oh_cli.VERBS["handheld"].site)
        status, out, _ = self.run_main("--help")
        self.assertEqual(status, 0)
        for action in ("serve", "enroll", "list", "forget"):
            self.assertIn(action, out)

    def test_action_help_names_the_switches(self):
        for action, switches in (("serve", ("--url", "--ca-file", "--mock", "--port", "--listen", "--metrics-file", "--state-file")),
                                 ("enroll", ("--name", "--port", "--base-port", "--simulator", "--key-file", "--mac",
                                             "--state-file")),
                                 ("list", ("--json", "--state-file")), ("forget", ("NAME", "--port", "--state-file"))):
            status, out, _ = self.run_main(action, "--help")
            self.assertEqual(status, 0)
            for switch in switches:
                self.assertIn(switch, out, action)

    def test_every_action_takes_state_file_and_the_old_state_with_a_warning(self):
        other = self.root / "elsewhere/state.json"
        status, _, err = self.run_main("enroll", "--simulator", "--name", "sim", "--state-file", str(other))
        self.assertEqual((status, err), (0, ""))
        self.assertFalse(self.state.exists())
        status, out, err = self.run_main("list", "--json", "--state-file", str(other))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual([remote["name"] for remote in json.loads(out)["remotes"]], ["sim"])
        status, old, err = self.run_main("list", "--json", "--state", str(other))
        self.assertEqual((status, old), (0, out))
        self.assertIn("thermite handheld list: --state is now --state-file; --state goes in the next release", err)
        status, _, err = self.run_main("forget", "sim", "--state", str(other))
        self.assertEqual(status, 0, err)
        self.assertIn("thermite handheld forget: --state is now --state-file", err)
        self.assertEqual(json.loads(other.read_text())["remotes"], [])
        for action in ("serve", "enroll", "list", "forget"):
            with self.subTest(action=action):
                _, help_text, _ = self.run_main(action, "--help")
                self.assertNotIn("--state FILE", help_text)

    def test_completion_offers_the_actions(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(oh_cli.main(["completion", "bash"]), 0)
        self.assertIn("handheld", out.getvalue())
        self.assertIn("--simulator", out.getvalue())

    def test_wrong_switches_are_usage_errors(self):
        for argv in (("serve", "--port", "/dev/null", "--listen", "x"), ("serve", "--mock", "--url", "thermite"),
                     ("enroll",), ("enroll", "--name", "a", "--simulator", "--port", "/dev/null"),
                     ("enroll", "--name", "a", "--key-file", "k"), ("forget",), ("bogus",)):
            with self.subTest(argv=argv):
                status, _, _ = self.run_main(*argv)
                self.assertEqual(status, 2)

    def test_names_and_macs_are_checked(self):
        for argv in (("--name", "-x"), ("--name", "a" * 33), ("--name", "two words"),
                     ("--name", "ok", "--mac", "02:00:00:00:00"), ("--name", "ok", "--mac", "zz:00:00:00:00:01")):
            with self.subTest(argv=argv):
                status, _, err = self.run_main("enroll", "--simulator", *argv)
                self.assertEqual(status, 2, err)
        self.assertFalse(self.state.exists())

    def test_the_default_state_file_follows_xdg_state_home(self):
        self.assertEqual(handheld.state_file(), self.state)


class Simulator(Scratch):
    def test_enrolment_writes_a_private_key_file_and_state_and_prints_no_key(self):
        status, out, err = self.run_main("enroll", "--simulator", "--name", "sim")
        self.assertEqual(status, 0, err)
        key_file = self.state.parent / "sim.key"
        key = key_file.read_text()
        self.assertRegex(key, r"^[0-9a-f]{64}\n$")
        for path, mode in ((key_file, 0o600), (self.state, 0o600), (self.state.parent, 0o700)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        data = self.data()
        self.assertEqual(data["version"], 1)
        [entry] = data["remotes"]
        self.assertEqual((entry["name"], entry["mac"], entry["key"], entry["board"], entry["simulated"]),
                         ("sim", handheld.SIMULATED_MAC, key.strip(), "simulator", True))
        self.assertNotIn(key.strip(), out + err)
        self.assertIn(str(key_file), out)
        for argv in (("list",), ("list", "--json")):
            status, out, err = self.run_main(*argv)
            self.assertEqual(status, 0, err)
            self.assertIn("sim", out)
            self.assertNotIn(key.strip(), out + err)
        listed = json.loads(self.run_main("list", "--json")[1])
        self.assertEqual(listed["remotes"][0]["mac"], handheld.SIMULATED_MAC)
        self.assertNotIn("key", listed["remotes"][0])

    def test_a_chosen_key_file_and_mac(self):
        key_file = self.root / "sim/remote.key"
        status, _, err = self.run_main("enroll", "--simulator", "--name", "desk", "--mac", mac("0A"),
                                       "--key-file", str(key_file))
        self.assertEqual(status, 0, err)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(self.data()["remotes"][0]["mac"], mac("0a"))
        self.assertEqual(self.data()["remotes"][0]["key"], key_file.read_text().strip())

    def test_enrolling_again_keeps_the_room_and_a_taken_mac_is_refused(self):
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "sim")[0], 0)
        data = self.data()
        data["remotes"][0]["store"] = {"remote.room": "example_east"}
        handheld.save(self.state, data)
        first = data["remotes"][0]["key"]
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "sim")[0], 0)
        [entry] = self.data()["remotes"]
        self.assertNotEqual(entry["key"], first)
        self.assertEqual(entry["store"], {"remote.room": "example_east"})
        status, _, err = self.run_main("enroll", "--simulator", "--name", "other")
        self.assertEqual(status, 2)
        self.assertIn("forget it first", err)

    def test_forget_removes_it_and_its_key_file(self):
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "sim")[0], 0)
        status, out, err = self.run_main("forget", "sim")
        self.assertEqual(status, 0, err)
        self.assertEqual(self.data()["remotes"], [])
        self.assertFalse((self.state.parent / "sim.key").exists())
        self.assertEqual(self.run_main("forget", "sim")[0], 2)

    def test_a_state_file_that_is_not_private_is_refused(self):
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "sim")[0], 0)
        os.chmod(self.state, 0o644)
        status, _, err = self.run_main("list")
        self.assertEqual(status, 2)
        self.assertIn("mode 0600", err)

    @unittest.skipUnless(NODE, "Node runs handheld/state.js")
    def test_state_lock_serializes_python_and_node_and_recovers_stale(self):
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "first", "--mac", mac("11"))[0], 0)
        lock = handheld.lock_path(self.state)
        self.assertFalse(lock.exists())

        # 1. Python holds .state.lock while Node state.open().flush() writes base:
        #    Node must wait and preserve Python's concurrent remote addition.
        held = threading.Event()
        errors = []

        def hold_python():
            try:
                with handheld.state_lock(self.state):
                    held.set()
                    time.sleep(0.15)
                    data = handheld.load(self.state)
                    handheld.add(data, {"name": "second", "mac": mac("22"), "key": "bb" * 32,
                                        "board": "simulator", "simulated": True, "store": {}, "status": {}})
                    handheld.save(self.state, data)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=hold_python)
        thread.start()
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        node_save = subprocess.run([
            NODE, "-e",
            "const State = require(process.argv[1]); const s = State.open(process.argv[2]); "
            "s.setBase({mac: process.argv[3], channel: 9, fw: '1.0'}); s.flush(); s.close();",
            str(AREA / "handheld/state.js"), str(self.state), BASE_MAC
        ], text=True, capture_output=True, timeout=15)
        elapsed = time.monotonic() - started
        thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(node_save.returncode, 0, node_save.stderr)
        self.assertGreaterEqual(elapsed, 0.1)
        data = self.data()
        self.assertEqual({r["name"] for r in data["remotes"]}, {"first", "second"})
        self.assertEqual(data["base"], {"mac": BASE_MAC, "channel": 9, "fw": "1.0"})
        self.assertFalse(lock.exists())

        # 2. Node holds .state.lock while Python enrolls a third remote:
        #    Python must wait and preserve Node's write.
        node_holder = subprocess.Popen([
            NODE, "-e",
            "const State = require(process.argv[1]); "
            "State.withLock(process.argv[2], () => { "
            "  process.stdout.write('LOCKED\\n'); "
            "  const cell = new Int32Array(new SharedArrayBuffer(4)); Atomics.wait(cell, 0, 0, 150); "
            "  const d = State.read(process.argv[2]); d.base = {mac: process.argv[3], channel: 11, fw: '2.0'}; "
            "  State.write(process.argv[2], d); "
            "});",
            str(AREA / "handheld/state.js"), str(self.state), BASE_MAC
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(node_holder.stdout.readline().strip(), "LOCKED")
        status, _, err = self.run_main("enroll", "--simulator", "--name", "third", "--mac", mac("33"))
        _, node_err = node_holder.communicate(timeout=15)
        self.assertEqual(node_holder.returncode, 0, node_err)
        self.assertEqual(status, 0, err)
        data = self.data()
        self.assertEqual({r["name"] for r in data["remotes"]}, {"first", "second", "third"})
        self.assertEqual(data["base"], {"mac": BASE_MAC, "channel": 11, "fw": "2.0"})
        self.assertFalse(lock.exists())

        # 3. Stale .state.lock (dead PID) is recovered cleanly by both Python and Node.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        lock.write_text(f"{dead.pid}\n", encoding="ascii")
        os.chmod(lock, 0o600)
        with handheld.state_lock(self.state, timeout=1.0):
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

        lock.write_text(f"{dead.pid}\n", encoding="ascii")
        os.chmod(lock, 0o600)
        node_recover = subprocess.run([
            NODE, "-e",
            "const State = require(process.argv[1]); State.withLock(process.argv[2], () => {}, {timeoutMs: 1000});",
            str(AREA / "handheld/state.js"), str(self.state)
        ], text=True, capture_output=True, timeout=10)
        self.assertEqual(node_recover.returncode, 0, node_recover.stderr)
        self.assertFalse(lock.exists())


class FakeDevice:
    """A remote's or base's console on a pseudo-terminal: answer(line) gives the lines it replies."""

    def __init__(self, answer):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.answer = answer
        self.heard = []
        self.stop = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        buffer = b""
        while not self.stop:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode()
                self.heard.append(text)
                for reply in self.answer(text):
                    os.write(self.master, (reply + "\r\n").encode())

    def close(self):
        self.stop = True
        self.thread.join(2)
        os.close(self.master)
        os.close(self.slave)


def remote_console(address=REMOTE_MAC, enroll=("thermite enrolled",)):
    def answer(line):
        if line == "thermite hello":
            return ["I (120) remote: console up", f"thermite remote 1 0.1.0 {address} waveshare-s3-lcd28 0"]
        if line.startswith("thermite enroll "):
            return list(enroll)
        if line == "thermite forget":
            return ["thermite forgotten"]
        return ["thermite error unknown"]
    return answer


class Usb(Scratch):
    def device(self, answer):
        device = FakeDevice(answer)
        self.addCleanup(device.close)
        return device

    def test_enroll_gives_the_remote_a_fresh_key_and_the_base_serve_saw(self):
        self.seed({"version": 1, "base": {"mac": BASE_MAC, "channel": 6, "fw": "0.1.0"}, "remotes": []})
        remote = self.device(remote_console())
        status, out, err = self.run_main("enroll", "--name", "lounge", "--port", remote.path)
        self.assertEqual(status, 0, err)
        enroll = [line for line in remote.heard if line.startswith("thermite enroll ")]
        self.assertEqual(len(enroll), 1)
        match = re.fullmatch(r"thermite enroll ([0-9a-f]{64}) (\S+) (\d+)", enroll[0])
        self.assertEqual(match.group(2, 3), (BASE_MAC, "6"))
        [entry] = self.data()["remotes"]
        self.assertEqual((entry["name"], entry["mac"], entry["key"], entry["board"]),
                         ("lounge", REMOTE_MAC, match.group(1), "waveshare-s3-lcd28"))
        self.assertNotIn(match.group(1), out + err)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)

    def test_enroll_reads_the_base_from_its_port_and_finds_the_remote_by_id(self):
        base = self.device(lambda line: [f"thermite base 1 0.2.0 {BASE_MAC} 11"] if line == "thermite hello" else [])
        remote = self.device(remote_console())
        by_id = self.root / "by-id"
        by_id.mkdir()
        (by_id / "usb-Other_UART-if00").symlink_to(base.path)
        (by_id / "usb-Espressif_USB_JTAG_serial_debug_unit_AA-if00").symlink_to(base.path)
        (by_id / "usb-Espressif_USB_JTAG_serial_debug_unit_BB-if00").symlink_to(remote.path)
        with mock.patch.object(handheld, "BY_ID", str(by_id)):
            status, out, err = self.run_main("enroll", "--name", "lounge", "--base-port", base.path)
        self.assertEqual(status, 0, err)
        self.assertIn(f"on base {BASE_MAC}, channel 11", out)
        self.assertTrue(any(line.endswith(f" {BASE_MAC} 11") for line in remote.heard))

    def test_probing_leaves_a_port_another_process_holds_alone(self):
        remote = self.device(remote_console())
        by_id = self.root / "by-id"
        by_id.mkdir()
        (by_id / "usb-Espressif_USB_JTAG_serial_debug_unit_BB-if00").symlink_to(remote.path)
        with mock.patch.object(handheld, "BY_ID", str(by_id)), \
                mock.patch.object(handheld, "opened", return_value={os.path.realpath(remote.path)}):
            with self.assertRaises(handheld.Problem) as raised:
                handheld.probe("remote")
        self.assertIn("no remote answers", str(raised.exception))
        self.assertEqual(remote.heard, [])

    def test_enroll_without_a_known_base_or_a_confirmation_fails(self):
        remote = self.device(remote_console(enroll=("thermite error flash full",)))
        status, _, err = self.run_main("enroll", "--name", "lounge", "--port", remote.path)
        self.assertEqual(status, 2)
        self.assertIn("no base is known", err)
        self.seed({"version": 1, "base": {"mac": BASE_MAC, "channel": 1, "fw": "0.1.0"}, "remotes": []})
        status, _, err = self.run_main("enroll", "--name", "lounge", "--port", remote.path)
        self.assertEqual(status, 2)
        self.assertIn("flash full", err)
        self.assertEqual(self.data()["remotes"], [])

    def test_a_port_that_is_no_remote(self):
        self.seed({"version": 1, "base": {"mac": BASE_MAC, "channel": 1, "fw": "0.1.0"}, "remotes": []})
        silent = self.device(lambda line: ["just logging"])
        with mock.patch.object(handheld, "ANSWER", 0.3):
            status, _, err = self.run_main("enroll", "--name", "lounge", "--port", silent.path)
        self.assertEqual(status, 2)
        self.assertIn("does not answer", err)
        status, _, err = self.run_main("enroll", "--name", "lounge", "--port", str(self.root / "missing"))
        self.assertEqual(status, 2)
        self.assertIn("cannot open", err)

    def test_forget_with_a_port_erases_the_remote_too(self):
        self.seed({"version": 1, "base": None, "remotes": [
            {"name": "lounge", "mac": REMOTE_MAC, "key": "ab" * 32, "board": "waveshare-s3-lcd28", "store": {}}]})
        other = self.device(remote_console(address=mac("99")))
        status, _, err = self.run_main("forget", "lounge", "--port", other.path)
        self.assertEqual(status, 2)
        self.assertIn("not lounge", err)
        self.assertEqual(len(self.data()["remotes"]), 1)
        remote = self.device(remote_console())
        status, out, err = self.run_main("forget", "lounge", "--port", remote.path)
        self.assertEqual(status, 0, err)
        self.assertIn("thermite forget", remote.heard)
        self.assertEqual(self.data()["remotes"], [])


@unittest.skipUnless(NODE, "Node runs the bridge")
class Serve(Scratch):
    def serve(self, *argv):
        process = subprocess.Popen([sys.executable, str(AREA / "bin/thermite"), "handheld", "serve", "--mock",
                                    "--state-file", str(self.state), *argv],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, **self.environment})

        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.addCleanup(stop)
        return process

    def finish(self, process):
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=10)
        return err

    def wait_for(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def test_a_simulated_base_on_the_socket_reaches_the_house(self):
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "sim")[0], 0)
        socket_path = self.root / "base.sock"
        process = self.serve("--listen", str(socket_path))
        self.assertTrue(self.wait_for(socket_path.is_socket), process.stderr.read() if process.poll() is not None else "")
        self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
        run = subprocess.run([NODE, str(AREA / "tests/handheld_sim_harness.js"), str(AREA), str(socket_path),
                              str(self.state.parent / "sim.key"), handheld.SIMULATED_MAC],
                             text=True, capture_output=True, timeout=30)
        seen = json.loads(run.stdout)
        self.assertIsNone(seen["error"], run.stderr)
        self.assertTrue(seen["synced"])
        self.assertEqual(seen["acks"], [1])
        self.assertEqual([screen["room"] for screen in seen["screens"]], ["example_west", "example_east"])
        err = self.finish(process)
        self.assertIn(f"thermite handheld: sim ({handheld.SIMULATED_MAC}) enrolled", err)
        self.assertIn("cannot read: thermite nonsense", err)
        data = self.data()
        self.assertEqual(data["base"], {"mac": mac("fe"), "channel": 1, "fw": "sim"})
        entry = data["remotes"][0]
        self.assertEqual(entry["store"]["remote.room"], "example_east")
        self.assertEqual((entry["status"]["battery"], entry["status"]["charging"]), (55, True))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertNotIn(entry["key"], err)

    def test_serve_metrics_file_writes_prometheus_gauges_when_enabled(self):
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "sim")[0], 0)
        socket_path = self.root / "base.sock"
        metrics_path = self.root / "metrics/thermite-handheld.prom"
        process = self.serve("--listen", str(socket_path), "--metrics-file", str(metrics_path))
        self.assertTrue(self.wait_for(socket_path.is_socket), process.stderr.read() if process.poll() is not None else "")
        run = subprocess.run([NODE, str(AREA / "tests/handheld_sim_harness.js"), str(AREA), str(socket_path),
                              str(self.state.parent / "sim.key"), handheld.SIMULATED_MAC],
                             text=True, capture_output=True, timeout=30)
        seen = json.loads(run.stdout)
        self.assertIsNone(seen["error"], run.stderr)
        self.assertTrue(self.wait_for(lambda: metrics_path.is_file() and 'battery_percent{remote="sim"} 55' in metrics_path.read_text()))
        text = metrics_path.read_text(encoding="utf-8")
        self.assertEqual(stat.S_IMODE(metrics_path.stat().st_mode), 0o644)
        self.assertIn('thermite_handheld_remote_battery_percent{remote="sim"} 55\n', text)
        self.assertIn('thermite_handheld_remote_charging{remote="sim"} 1\n', text)
        self.assertIn('thermite_handheld_remote_rssi_dbm{remote="sim"} -42\n', text)
        self.assertRegex(text, r'thermite_handheld_remote_last_seen_seconds\{remote="sim"\} [0-9]+\n')
        self.assertNotIn(handheld.SIMULATED_MAC, text)
        self.assertNotIn(self.data()["remotes"][0]["key"], text)
        self.finish(process)
        # Clean shutdown flushes base_connected 0 without stray temp files.
        after = metrics_path.read_text(encoding="utf-8")
        self.assertIn("thermite_handheld_base_connected 0\n", after)
        self.assertEqual([p.name for p in metrics_path.parent.iterdir()], ["thermite-handheld.prom"])

    def test_a_remote_enrolled_while_serving_is_picked_up(self):
        socket_path = self.root / "base.sock"
        process = self.serve("--listen", str(socket_path))
        self.assertTrue(self.wait_for(socket_path.is_socket))
        self.assertEqual(self.run_main("enroll", "--simulator", "--name", "late")[0], 0)
        run = subprocess.run([NODE, str(AREA / "tests/handheld_sim_harness.js"), str(AREA), str(socket_path),
                              str(self.state.parent / "late.key"), handheld.SIMULATED_MAC],
                             text=True, capture_output=True, timeout=30)
        self.assertIsNone(json.loads(run.stdout)["error"], run.stderr)
        err = self.finish(process)
        self.assertIn("no remote is enrolled yet", err)
        self.assertIn(f"late ({handheld.SIMULATED_MAC}) enrolled", err)

    def test_the_base_on_a_serial_port(self):
        master, slave = os.openpty()
        device = os.ttyname(slave)
        process = self.serve("--port", device)
        buffer = b""
        deadline = time.monotonic() + 10
        try:
            while b"\n" not in buffer and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    buffer += os.read(master, 4096)
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        # stty raw: exactly the line, no CR the tty would add.
        self.assertEqual(buffer, b"thermite hello\n")
        os.write(master, b"rst:0x15 (USB_UART_CHIP_RESET)\r\nthermite base 1 0.3.0 " + BASE_MAC.encode() + b" 6\r\n")
        self.assertTrue(self.wait_for(lambda: self.state.exists() and (self.data().get("base") or {}).get("channel") == 6))
        self.assertEqual(self.data()["base"], {"mac": BASE_MAC, "channel": 6, "fw": "0.3.0"})
        # Unplugged: the bridge says so and keeps trying.
        os.close(master)
        os.close(slave)
        time.sleep(1.0)
        err = self.finish(process)
        self.assertIn(f"base on {device} connected", err)
        self.assertIn(f"base on {device} closed", err)


class Gateway(Scratch):
    """serve on a gateway: the model and trust as thermite tui reads them, then node."""

    def test_serve_runs_main_js_with_the_gateways_model(self):
        launched = {}

        def execve(path, argv, environment):
            launched.update(path=path, argv=argv, environment=environment)
        with mock.patch.object(tui, "trust", return_value=(None, "the system's")), \
                mock.patch.object(tui, "fetch", return_value=CONTENT), \
                mock.patch.object(handheld.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(handheld.os, "execve", side_effect=execve):
            status, _, err = self.run_main("serve", "--url", "192.0.2.50", "--port", "/dev/ttyACM0",
                                           "--metrics-file", str(self.root / "hh.prom"))
        self.assertEqual(status, 0, err)
        url = "https://192.0.2.50:" + str(tui.default_port())
        self.assertEqual(launched["argv"], ["/usr/bin/node", str(handheld.MAIN), "--url", url, "--state", str(self.state),
                                            "--port", "/dev/ttyACM0", "--metrics-file", str(self.root / "hh.prom")])
        model = Path(launched["environment"]["THERMITE_REMOTE_HOUSE_FILE"])
        self.assertEqual(model.read_bytes(), CONTENT)
        self.assertNotIn("THERMITE_REMOTE_TOKEN_FILE", launched["environment"])

    def test_serve_without_url_opens_the_server_thermite_url_names(self):
        launched = {}
        os.environ[oh_cli.URL_VARIABLE] = "ssh://ann@192.0.2.60"
        with mock.patch.object(tui, "installed_profile", return_value=None), \
                mock.patch.object(tui, "trust", return_value=(None, "the system's")) as trust, \
                mock.patch.object(tui, "fetch", return_value=CONTENT), \
                mock.patch.object(handheld.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(handheld.os, "execve", side_effect=lambda *args: launched.update(argv=args[1])):
            status, _, err = self.run_main("serve", "--listen", str(self.root / "base.sock"))
            self.assertEqual(status, 0, err)
            url = "https://192.0.2.60:" + str(tui.default_port())
            trust.assert_called_once_with(url, None)
            self.assertEqual(launched["argv"][2:4], ["--url", url])
            status, _, err = self.run_main("serve", "--url", "192.0.2.50", "--listen", str(self.root / "base.sock"))
            self.assertEqual(status, 0, err)
            self.assertEqual(launched["argv"][2:4], ["--url", "https://192.0.2.50:" + str(tui.default_port())])
        os.environ[oh_cli.URL_VARIABLE] = "http://192.0.2.60"
        status, _, err = self.run_main("serve")
        self.assertEqual(status, 2)
        self.assertIn("thermite handheld: THERMITE_URL=http://192.0.2.60: plain http", err)

    def test_a_gateway_that_cannot_be_read_stops_before_node(self):
        with mock.patch.object(tui, "trust", return_value=(None, "the system's")), \
                mock.patch.object(tui, "fetch", side_effect=tui.Problem("nothing answers at https://192.0.2.50")), \
                mock.patch.object(handheld.os, "execve") as execve:
            status, _, err = self.run_main("serve", "--url", "192.0.2.50")
        self.assertEqual(status, 2)
        self.assertIn("thermite handheld: nothing answers", err)
        execve.assert_not_called()

    @unittest.skipUnless(NODE, "Node runs handheld/bridge.js and handheld/base.js")
    def test_bridge_skips_compute_while_asleep_and_base_short_circuits_empty_candidates(self):
        script = r"""
const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Shared = require('./tui/remote/shared');
const Link = require('./handheld/link');
const Bridge = require('./handheld/bridge');
const Screen = require('./handheld/screen');
const Base = require('./handheld/base');
const shared = Shared.load();
Shared.mocks();
const origScreen = Screen.screen;
let screenCalls = 0;
Screen.screen = (...args) => { screenCalls += 1; return origScreen(...args); };
const backend = shared.Remote.create(shared.house, { backend: 'mock', latency: 2 });
const key = Buffer.alloc(32, 0x55);
const mac = '02:00:00:00:00:' + '55';
const end = new Link.RemoteEnd({ key });
const bridge = Bridge.create({
  shared: { Session: shared.Session, house: shared.house, layouts: shared.layouts },
  backend,
  log() {},
  send: (m, frame, done) => { setImmediate(() => { done(true); end.receive(frame); }); }
});
const store = { data: {}, get(k) { return k in this.data ? this.data[k] : null; }, set(k, v) { this.data[k] = v; } };
bridge.add({ name: 'test', mac, key, store });
(async () => {
  await new Promise((r) => setTimeout(r, 30));
  // Remote has not woken yet: house state changes must not call Screen.screen().
  assert.equal(bridge.remote(mac).awake, false);
  const before = screenCalls;
  backend.choose(shared.house.rooms[0].id, 'example_player');
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(screenCalls, before, 'asleep remote must not compute screen JSON/hash');
  // Waking the remote computes the screen once.
  bridge.receive(mac, end.hello({ have: '', battery: 80, charging: false, board: 'sim', fw: '0.1', w: 240, h: 320 }));
  await new Promise((r) => setTimeout(r, 40));
  assert.ok(screenCalls > before, 'waking remote computes screen');
  Screen.screen = origScreen;
  // base.js candidates() on an empty dir short-circuits without crawling /proc.
  const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'base-empty-'));
  const origReaddir = fs.readdirSync;
  let procReads = 0;
  fs.readdirSync = (p, ...rest) => { if (String(p).startsWith('/proc')) procReads += 1; return origReaddir(p, ...rest); };
  const found = Base.candidates(emptyDir);
  fs.readdirSync = origReaddir;
  fs.rmSync(emptyDir, { recursive: true, force: true });
  assert.deepEqual(found, []);
  assert.equal(procReads, 0, 'empty candidates directory must not scan /proc');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        run = subprocess.run([NODE, "-e", script], cwd=AREA, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)


if __name__ == "__main__":
    unittest.main()
