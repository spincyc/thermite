"""Capture the actual web and terminal clients with the public demo inventory.

See capture-docs.md for the one-time browser setup and repeatable command.
No real site configuration is loaded and no runtime or device is contacted.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import pty
import select
import socketserver
import struct
import subprocess
import sys
import termios
import threading
import time
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))


def capture_terminal(work: Path, modules: Path, environment: dict) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 154, 0, 0))
    command = ["node", str(ROOT / "tui/remote/main.js"), "--modules", str(modules),
               "--mock", "--room", "example_west", "--choose", "example_player",
               "--latency", "10", "--still", "--no-log", "--volume", "slider",
               "--state", str(work / "terminal-state.json")]
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                               cwd=ROOT, env=environment, start_new_session=True)
    os.close(slave)
    output = bytearray()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
            if process.poll() is not None:
                raise RuntimeError("terminal exited before capture: " + output.decode(errors="replace"))
        (work / "terminal.ansi").write_bytes(output)
    finally:
        if process.poll() is None:
            os.write(master, b"q")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        os.close(master)
    if process.returncode:
        raise RuntimeError(f"terminal exited with status {process.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", type=Path, required=True,
                        help="scratch node_modules containing playwright, @xterm/xterm and blessed")
    parser.add_argument("--fonts", type=Path,
                        help="optional directory containing the DejaVu font family on a minimal capture host")
    args = parser.parse_args()
    modules = args.modules.resolve()
    work = ROOT / ".scratch/docs-visuals/capture"
    work.mkdir(parents=True, exist_ok=True)
    (work / "terminal-state.json").unlink(missing_ok=True)
    import oh_brand
    from oh_cli.remote import look
    import oh_public
    import oh_render

    inventory = oh_public.demo()
    inventory_bytes = (json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (work / "demo-house.json").write_bytes(inventory_bytes)
    for name, content in oh_render.render(inventory).items():
        if not name.startswith("html/remote/"):
            continue
        target = work / "web" / name.removeprefix("html/remote/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
    environment = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor",
                   "THERMITE_REMOTE_COLOR": "truecolor", "THERMITE_REMOTE_LOOK": look(oh_brand.load()),
                   "THERMITE_REMOTE_HOUSE_FILE": str(work / "web/house.js")}
    environment.pop("THERMITE_REMOTE_TOKEN_FILE", None)
    environment.pop("NO_COLOR", None)
    environment.pop("FORCE_COLOR", None)
    if args.fonts:
        fonts = args.fonts.resolve()
        if not fonts.is_dir():
            parser.error("--fonts must be an existing directory containing DejaVu fonts")
        aliases = {"system-ui": "DejaVu Sans", "sans-serif": "DejaVu Sans",
                   "Arial": "DejaVu Sans", "serif": "DejaVu Serif", "monospace": "DejaVu Sans Mono"}
        rules = "".join('<alias binding="strong"><family>' + name + '</family><prefer><family>'
                        + font + '</family></prefer></alias>' for name, font in aliases.items())
        config = work / "fonts.conf"
        config.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir>'
                          + escape(str(fonts)) + '</dir><cachedir>' + escape(str(work / "font-cache"))
                          + '</cachedir>' + rules + '<match target="pattern"><edit name="family" '
                          'mode="append_last" binding="weak"><string>DejaVu Sans</string></edit></match></fontconfig>')
        environment["FONTCONFIG_FILE"] = str(config)
    capture_terminal(work, modules, environment)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=work, **kw)

        def log_message(self, *a):
            pass

    # Only a loopback file server is reachable during capture.
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_address[1])
        destination = ROOT / "site/assets/screenshots"
        destination.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["node", str(ROOT / "site/capture-docs.cjs"), str(modules),
                            str(work), str(destination), url], check=True, cwd=ROOT, env=environment)
        finally:
            server.shutdown()
            thread.join()
    files = {}
    for name in ("web-remote.png", "terminal-remote.png"):
        content = (destination / name).read_bytes()
        width, height = struct.unpack(">II", content[16:24])
        files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "width": width, "height": height}
    manifest = {"version": 1, "mode": "mock", "inventory": "oh_public.demo()",
                "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
                "capture": "site/capture-docs.py; Chromium web render and actual PTY output rendered by xterm.js",
                "files": files}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
