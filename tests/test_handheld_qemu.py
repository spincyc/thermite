"""The handheld remote's real firmware in Espressif's QEMU, against the bridge.

Opt-in: run in ESP-IDF's environment, with the qemu-xtensa tool installed, and
set THERMITE_QEMU_BUILD to a qemu board build directory (handheld/firmware/README.md,
"QEMU"). The test enrols the emulated remote over its console as thermite
handheld enroll does a real one, drives it with the console's test input, and
checks what reached the mock house and that the display can be captured. It
leaves the build's flash image enrolled to its own, discarded, state.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

AREA = Path(__file__).resolve().parents[1]
BUILD = os.environ.get("THERMITE_QEMU_BUILD")
RUN = AREA / "handheld/firmware/qemu/run.py"
#: The second bubble on the fictional house's first room, Example West: Player.
PLAYER_BUBBLE = (124, 62)


@unittest.skipUnless(BUILD and shutil.which("qemu-system-xtensa") and shutil.which("node"),
                     "set THERMITE_QEMU_BUILD to a qemu board build, in ESP-IDF's environment")
class Qemu(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="thermite-qemu-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.build = Path(BUILD).resolve()
        self.environment = {**os.environ, "XDG_STATE_HOME": str(self.root / "state")}
        self.log = self.root / "serve.log"
        socket = self.root / "base.sock"
        self.start([sys.executable, str(AREA / "bin/thermite"), "handheld", "serve", "--mock", "-v",
                    "--listen", str(socket)], self.log)
        self.assertTrue(self.wait(socket.is_socket, 10), self.log.read_text())
        self.start([sys.executable, str(RUN), str(self.build), str(socket)], self.root / "qemu.log")
        self.console = self.build / "console"
        self.assertTrue(self.wait(self.console.exists, 30), (self.root / "qemu.log").read_text())
        # The firmware's base announces itself as it boots; enrolment reads it from the bridge's state.
        state = self.root / "state/thermite/handheld/state.json"
        self.assertTrue(self.wait(lambda: state.is_file() and '"base"' in state.read_text(), 60),
                        self.log.read_text())

    def start(self, argv, log):
        with log.open("w") as out:
            process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env=self.environment,
                                       start_new_session=True)

        def stop():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        self.addCleanup(stop)

    def wait(self, condition, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def sent(self, command):
        """Whether the mock house logged the command (serve -v)."""
        return f"[remote mock] command {command}" in self.log.read_text()

    def say(self, *lines):
        with open(self.console, "w") as console:
            for line in lines:
                console.write(line + "\n")
                console.flush()
                time.sleep(0.3)

    def test_the_firmware_enrols_and_drives_the_house(self):
        enrol = subprocess.run([sys.executable, str(AREA / "bin/thermite"), "handheld", "enroll", "--name", "qemu",
                                "--port", str(self.console)], env=self.environment, capture_output=True, text=True,
                               timeout=60)
        self.assertEqual(enrol.returncode, 0, enrol.stderr)
        self.assertIn("enrolled qemu", enrol.stdout + enrol.stderr)

        # The remote restarts enrolled and draws its first screen: tap Player until it lands.
        x, y = PLAYER_BUBBLE
        chosen = lambda: self.sent("loc_example_west_source example_player")  # noqa: E731
        for _ in range(10):
            self.say(f"thermite touch {x} {y}", "thermite touch up")
            if self.wait(chosen, 5):
                break
        self.assertTrue(chosen(), self.log.read_text())

        self.say("thermite key right")
        self.assertTrue(self.wait(lambda: self.sent("example_player_key right"), 15), self.log.read_text())
        self.say("thermite key volume_up")
        self.assertTrue(self.wait(lambda: self.sent("example_tv_volume"), 15), self.log.read_text())

        shot = self.root / "screen.png"
        capture = subprocess.run([sys.executable, str(RUN), "screenshot", str(self.build), str(shot)],
                                 env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(capture.returncode, 0, capture.stderr)
        self.assertEqual(shot.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
