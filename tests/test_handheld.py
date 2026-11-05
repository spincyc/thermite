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
        fixtures = sorted((AREA / "handheld/firmware/host/fixtures").glob("*.json"))
        self.assertTrue(fixtures)
        for path in fixtures:
            data = json.loads(path.read_text(encoding="utf-8"))
            for message in data if isinstance(data, list) else [data]:
                if message.get("screen"):
                    screen = message["screen"]
                    self.assertEqual(sorted(screen), sorted(sony["fields"]), path.name)
                    for spec in screen["buttons"].values():
                        self.assertLessEqual(set(spec), {"label", "icon", "text", "repeat"}, path.name)
                        self.assertNotIn("", (spec.get("icon"), spec.get("text")), path.name)
                        self.assertIsNot(spec.get("repeat"), False, path.name)
                if message.get("audio"):
                    self.assertEqual(sorted(message["audio"]), sorted(sony["audioFields"]), path.name)

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

    def test_every_case_ran(self):
        self.assertEqual(len(self.cases), 19)
        failed = {name: result["error"] for name, result in self.cases.items() if not result["ok"]}
        self.assertEqual(failed, {})


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
        for action, switches in (("serve", ("--url", "--ca-file", "--mock", "--port", "--listen", "--state")),
                                 ("enroll", ("--name", "--port", "--base-port", "--simulator", "--key-file", "--mac")),
                                 ("list", ("--json",)), ("forget", ("NAME", "--port"))):
            status, out, _ = self.run_main(action, "--help")
            self.assertEqual(status, 0)
            for switch in switches:
                self.assertIn(switch, out, action)

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
                                    "--state", str(self.state), *argv],
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
            status, _, err = self.run_main("serve", "--url", "192.0.2.50", "--port", "/dev/ttyACM0")
        self.assertEqual(status, 0, err)
        url = "https://192.0.2.50:" + str(tui.default_port())
        self.assertEqual(launched["argv"], ["/usr/bin/node", str(handheld.MAIN), "--url", url, "--state", str(self.state),
                                            "--port", "/dev/ttyACM0"])
        model = Path(launched["environment"]["THERMITE_REMOTE_HOUSE_FILE"])
        self.assertEqual(model.read_bytes(), CONTENT)
        self.assertNotIn("THERMITE_REMOTE_TOKEN_FILE", launched["environment"])

    def test_a_gateway_that_cannot_be_read_stops_before_node(self):
        with mock.patch.object(tui, "trust", return_value=(None, "the system's")), \
                mock.patch.object(tui, "fetch", side_effect=tui.Problem("nothing answers at https://192.0.2.50")), \
                mock.patch.object(handheld.os, "execve") as execve:
            status, _, err = self.run_main("serve", "--url", "192.0.2.50")
        self.assertEqual(status, 2)
        self.assertIn("thermite handheld: nothing answers", err)
        execve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
