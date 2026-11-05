#!/usr/bin/env python3
"""Run the remote's qemu board in Espressif's QEMU (../README.md, "QEMU").

    run.py BUILD SOCKET [--window] [--console]
    run.py screenshot BUILD FILE.png

Run it in ESP-IDF's environment (export.sh) with the qemu-xtensa tool
installed. BUILD is the qemu board's build directory. SOCKET is the Unix
socket `thermite handheld serve --listen` made: UART1 joins it, and the
firmware is the base and the remote at once.

UART0, the console, is a pseudo-terminal this script serves at BUILD/console,
for `thermite handheld enroll --port BUILD/console` and test input (PROTOCOL.md,
"Remote console lines"); --console puts it on this terminal instead. QEMU's
own pty backend passes nothing in this setup, so the script relays a socket.
--window shows the display; without it the display still runs, on SDL's
offscreen driver, because QEMU's panel draws only when a display refreshes.

BUILD/qemu_flash.bin persists across runs, enrolment included: each run
rewrites only the images flash_args lists. `screenshot` saves the running
board's display as a PNG.
"""

import argparse
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import tty
import zlib
from pathlib import Path

MONITOR = "qemu-monitor.sock"
CONSOLE = "qemu-console.sock"
LINK = "console"


def monitor(build: Path, command: str) -> str:
    """One QEMU monitor command, and what the monitor answered."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(build / MONITOR))
        connection.settimeout(0.5)
        connection.sendall(command.encode() + b"\n")
        answer, deadline = b"", time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            answer += chunk
            if answer.rstrip().endswith(b"(qemu)") and answer.count(b"(qemu)") > 1:
                break
        return answer.decode(errors="replace")


def png(ppm: bytes) -> bytes:
    """A binary PPM (P6, 8-bit) as a PNG: this QEMU's screendump writes only PPM."""
    fields = ppm.split(maxsplit=4)
    if fields[0] != b"P6" or fields[3] != b"255":
        raise ValueError("not an 8-bit binary PPM")
    width, height, pixels = int(fields[1]), int(fields[2]), fields[4]
    rows = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


def screenshot(build: Path, out: Path) -> int:
    out = out.resolve()
    ppm = build / "qemu-screen.ppm"
    ppm.unlink(missing_ok=True)
    answer = monitor(build, f"screendump {ppm}")
    for _ in range(20):
        if ppm.is_file() and ppm.stat().st_size:
            time.sleep(0.1)  # let QEMU finish writing it
            out.write_bytes(png(ppm.read_bytes()))
            ppm.unlink()
            print(out)
            return 0
        time.sleep(0.1)
    print(f"run.py: no screenshot; QEMU said: {answer.strip()[-200:]}", file=sys.stderr)
    return 1


def flash_image(build: Path) -> None:
    """BUILD/qemu_flash.bin: made whole the first time, then only the listed images rewritten."""
    lines = (build / "flash_args").read_text().splitlines()
    size = next(word for line in lines for word in line.split() if word.endswith("MB"))
    image = build / "qemu_flash.bin"
    if not image.is_file():
        subprocess.run(["esptool.py", "--chip", "esp32s3", "merge_bin", "--fill-flash-size", size,
                        "-o", str(image), "@flash_args"], cwd=build, check=True, stdout=subprocess.DEVNULL)
        return
    with image.open("r+b") as flash:
        for line in lines:
            if line.startswith("0x"):
                offset, name = line.split(maxsplit=1)
                flash.seek(int(offset, 16))
                flash.write((build / name).read_bytes())


def efuses(build: Path) -> Path:
    """QEMU's default eFuses for the esp32s3, as idf.py qemu writes them."""
    path = build / "qemu_efuse.bin"
    if not path.is_file():
        sys.path.insert(0, os.path.join(os.environ["IDF_PATH"], "tools"))
        from idf_py_actions.qemu_ext import QEMU_TARGETS

        path.write_bytes(QEMU_TARGETS["esp32s3"].default_efuse)
    return path


def relay(process: subprocess.Popen, build: Path) -> None:
    """Serve QEMU's console socket as a pseudo-terminal at BUILD/console until QEMU exits.
    The relay keeps the terminal's own end open, so a program that opens and closes it
    (enrolment) leaves it usable; console output nobody reads is dropped."""
    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    link = build / LINK
    link.unlink(missing_ok=True)
    link.symlink_to(os.ttyname(slave))
    print(f"console: {link} ({os.ttyname(slave)})", flush=True)
    path = str(build / CONSOLE)
    connection = socket.socket(socket.AF_UNIX)
    while process.poll() is None:
        try:
            connection.connect(path)
            break
        except OSError:
            time.sleep(0.05)
    try:
        while process.poll() is None:
            ready, _, _ = select.select([connection, master], [], [], 0.5)
            if connection in ready:
                data = connection.recv(4096)
                if not data:
                    break
                try:
                    os.write(master, data)
                except BlockingIOError:
                    pass
            if master in ready:
                try:
                    connection.sendall(os.read(master, 4096))
                except OSError:
                    pass
    finally:
        link.unlink(missing_ok=True)
        connection.close()


def run(build: Path, base: Path, window: bool, console: bool) -> int:
    if "CONFIG_THERMITE_BOARD_QEMU=y" not in (build / "sdkconfig").read_text():
        print(f"run.py: {build} is not a qemu board build", file=sys.stderr)
        return 2
    if not shutil.which("qemu-system-xtensa"):
        print("run.py: no qemu-system-xtensa: run ESP-IDF's export.sh, after idf_tools.py install qemu-xtensa",
              file=sys.stderr)
        return 2
    flash_image(build)
    (build / CONSOLE).unlink(missing_ok=True)
    serial0 = "mon:stdio" if console else f"unix:{build / CONSOLE},server=on,wait=on"
    environment = dict(os.environ)
    if not window:
        environment["SDL_VIDEODRIVER"] = "offscreen"
    command = ["qemu-system-xtensa", "-M", "esp32s3", "-m", "32M",
               "-drive", f"file={build / 'qemu_flash.bin'},if=mtd,format=raw",
               "-drive", f"file={efuses(build)},if=none,format=raw,id=efuse",
               "-global", "driver=nvram.esp32s3.efuse,property=drive,value=efuse",
               "-global", "driver=timer.esp32s3.timg,property=wdt_disable,value=true",
               "-nic", "none", "-display", "sdl",
               "-monitor", f"unix:{build / MONITOR},server=on,wait=off",
               "-serial", serial0, "-serial", f"unix:{base}"]
    process = subprocess.Popen(command, env=environment)
    stop = lambda *_: process.terminate()  # noqa: E731
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if not console:
        relay(process, build)
    return process.wait()


def main(argv: list[str]) -> int:
    if argv[:1] == ["screenshot"]:
        if len(argv) != 3:
            print("usage: run.py screenshot BUILD FILE.png", file=sys.stderr)
            return 2
        return screenshot(Path(argv[1]).resolve(), Path(argv[2]))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("build", type=Path)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--window", action="store_true", help="show the display in a window")
    parser.add_argument("--console", action="store_true", help="the console on this terminal")
    arguments = parser.parse_args(argv)
    return run(arguments.build.resolve(), arguments.socket.resolve(), arguments.window, arguments.console)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
