"""Handheld remotes: the bridge to the house, and enrolling remotes.

    thermite handheld serve [--url URL] [--ca-file FILE] [--mock] [--port DEVICE | --listen PATH] [--state FILE]
    thermite handheld enroll --name NAME [--port DEVICE] [--base-port DEVICE] [--state FILE]
    thermite handheld enroll --simulator --name NAME [--key-file FILE] [--mac MAC] [--state FILE]
    thermite handheld list [--state FILE] [--json]
    thermite handheld forget NAME [--port DEVICE] [--state FILE]

serve resolves the gateway as thermite tui does (oh_cli.tui) and runs
handheld/main.js; enroll and forget speak the remote's console lines
(handheld/PROTOCOL.md) over its USB serial port.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import secrets
import select
import shutil
import stat
import sys
import termios
import time
import tty
from pathlib import Path

import oh_cli
from oh_cli import remote, tui

MAIN = oh_cli.PROGRAM_ROOT / "handheld/main.js"
VERSION = 1
#: Where Espressif boards' USB serial ports have stable names.
BY_ID = "/dev/serial/by-id"
ESPRESSIF = re.compile(r"espressif", re.IGNORECASE)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
#: A simulated remote's MAC unless --mac names another: locally administered,
#: so no device's. (In two parts: the export refuses any whole MAC literal.)
SIMULATED_MAC = "02:00:00:00:00:" + "01"
#: How long a remote or base has to answer a console line, in seconds.
ANSWER = 3.0
PROBE = 1.5
LINE_MAX = 4096
STATE_LIMIT = 1024 * 1024

DESCRIPTION = f"""\
Handheld remotes: ESP32-S3 remotes that reach the house by radio (ESP-NOW)
through a base station on a USB port of the computer that runs serve.

serve is the bridge: like thermite tui it opens the house on a Thermite
gateway, from any computer, and runs one remote session for each enrolled
remote, sending each the screen its session shows and turning its buttons
into the same commands the web and terminal remotes send. Guests control
unlocked rooms without a login; after thermite access login for the same
address, that account's personal credential is used. It needs Node.js.

enroll gives a remote connected by USB a fresh key and the base's address,
and keeps both in the state file; --simulator enrolls a simulated remote
instead and writes its key file. list shows the enrolled remotes and forget
removes one. The state file holds the remotes' keys: it is private (mode
0600) and keys are never printed.

Without --url serve opens {tui.DEFAULT_URL}, as thermite tui does, and
trusts the gateway's certificate the same way (thermite help tui).
"""

NOTES = """\
the base:
  Without --port, serve uses the Espressif USB serial port in
  /dev/serial/by-id that answers thermite hello as a base, and enroll the
  one that answers as a remote. serve reopens the base when it is
  unplugged and plugged in again. --listen PATH makes a Unix socket a
  simulated base connects to instead. enroll gives a remote the base's MAC
  and channel as serve last saw them, or as --base-port's base says them.
"""

EXAMPLES = (
    ("thermite handheld serve", f"the bridge for the house on {tui.DEFAULT_URL}, on the base plugged in here"),
    ("thermite handheld serve --mock --listen /tmp/thermite-base.sock",
     "the mock house, for a simulated base and remote on that socket"),
    ("thermite handheld enroll --name lounge", "enroll the remote connected by USB as lounge"),
    ("thermite handheld enroll --simulator --name sim", "enroll a simulated remote; its key file is printed"),
    ("thermite handheld list", "the enrolled remotes, their boards, batteries and rooms"),
    ("thermite handheld forget lounge --port /dev/ttyACM1", "forget lounge, here and on the remote itself"),
)


class Problem(Exception):
    """Why the command cannot go on, as the lines to print."""

    def __init__(self, *lines: str):
        super().__init__(lines[0])
        self.lines = lines


def state_file() -> Path:
    return tui._home("XDG_STATE_HOME", ".local/state") / "thermite/handheld/state.json"


def models() -> Path:
    return tui._home("XDG_CACHE_HOME", ".cache") / "thermite/handheld/models"


# --------------------------------------------------------------------------
# the state file


def empty() -> dict:
    return {"version": VERSION, "base": None, "remotes": []}


def load(path: Path) -> dict:
    """The state file's data; empty when there is none. Raises Problem."""
    from oh_access import AccessError
    from oh_access.files import read_private

    if not (path.exists() or path.is_symlink()):
        return empty()
    import json

    try:
        data = json.loads(read_private(path, STATE_LIMIT))
    except (AccessError, OSError):
        raise Problem(f"{path} must be a regular file of this user's, mode 0600") from None
    except ValueError:
        raise Problem(f"{path} is not a handheld state file") from None
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("remotes"), list):
        raise Problem(f"{path} is not a handheld state file")
    return data


def private_parent(path: Path) -> None:
    """The state file's directory, created owner-only (0700) if missing. Raises Problem."""
    from oh_access import AccessError
    from oh_access.files import private_directory

    try:
        private_directory(path.parent)
    except (AccessError, OSError) as exc:
        raise Problem(f"cannot use {path.parent}: {exc}", "it must be this user's directory, mode 0700") from None


def save(path: Path, data: dict) -> None:
    """Replace the state file, privately: mode 0600 in a 0700 directory."""
    from oh_access import AccessError
    from oh_access.files import json_private

    try:
        json_private(path, data)
    except (AccessError, OSError) as exc:
        raise Problem(f"cannot write {path}: {exc}",
                      "its directory must be this user's, mode 0700, and the file mode 0600") from None


def find(data: dict, name: str) -> dict | None:
    return next((entry for entry in data["remotes"] if isinstance(entry, dict) and entry.get("name") == name), None)


def add(data: dict, entry: dict) -> str | None:
    """Put entry in data's remotes; the name it replaces, if any. Raises Problem
    when its MAC belongs to a remote of another name."""
    for other in data["remotes"]:
        if isinstance(other, dict) and other.get("mac") == entry["mac"] and other.get("name") != entry["name"]:
            raise Problem(f"{entry['mac']} is enrolled as {other.get('name')}",
                          f"forget it first: thermite handheld forget {other.get('name')}")
    before = find(data, entry["name"])
    if before is not None:
        entry = {**entry, "store": before.get("store", {})} if before.get("mac") == entry["mac"] else entry
        data["remotes"] = [other for other in data["remotes"] if other is not before]
    data["remotes"].append(entry)
    return before.get("mac") if before is not None else None


def write_key(path: Path, key: bytes) -> None:
    """The simulator's key file: 64 hex digits and a newline, mode 0600."""
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(key.hex() + "\n")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise Problem(f"{path} is not a regular file")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# the console lines over USB serial


class Console:
    """A remote's or base's USB serial port, raw, read a line at a time."""

    def __init__(self, device: str):
        self.device = device
        try:
            self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise Problem(f"cannot open {device}: {exc.strerror}",
                          "is it plugged in, and is this user allowed to use it (the uucp or dialout group)?") from None
        if not os.isatty(self.fd):
            os.close(self.fd)
            raise Problem(f"{device} is not a serial port")
        try:
            tty.setraw(self.fd, termios.TCSANOW)
        except termios.error:
            os.close(self.fd)
            raise Problem(f"cannot set up {device} as a serial port") from None
        self.buffer = b""

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def write(self, line: str) -> None:
        data = (line + "\n").encode("ascii")
        # The words after the second (an enrolment's key) are not traced.
        oh_cli.trace(f"{self.device} <- " + " ".join(line.split(" ")[:2]))
        deadline = time.monotonic() + ANSWER
        while data:
            try:
                data = data[os.write(self.fd, data):]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise Problem(f"{self.device} does not take what is written to it") from None
                select.select([], [self.fd], [], 0.1)
            except OSError as exc:
                raise Problem(f"{self.device} went away: {exc.strerror}") from None

    def line(self, deadline: float) -> str | None:
        """The next line, without its newline; None at the deadline."""
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], left)
            if not ready:
                return None
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise Problem(f"{self.device} went away: {exc.strerror}") from None
            if not chunk:
                raise Problem(f"{self.device} went away")
            self.buffer += chunk
            if len(self.buffer) > LINE_MAX and b"\n" not in self.buffer:
                self.buffer = b""
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode("ascii", "replace").rstrip("\r")

    def ask(self, line: str, answers: tuple[str, ...], wait: float = ANSWER) -> list[str] | None:
        """Send line; the words of the first thermite line that starts with
        one of answers, or None when none comes in time. Log lines are
        skipped; thermite error raises Problem."""
        self.write(line)
        deadline = time.monotonic() + wait
        while (text := self.line(deadline)) is not None:
            if not text.startswith("thermite "):
                continue
            words = text.split(" ")[1:]
            if words[0] == "error":
                raise Problem(f"{self.device} refused it: {' '.join(words[1:])}")
            if words[0] in answers:
                return words
        return None


def remote_line(words: list[str]) -> dict | None:
    """thermite remote 1 <fw> <mac> <board> <0|1>, as a dict; None if malformed."""
    if len(words) != 6 or words[0] != "remote" or words[1] != "1" or not MAC.fullmatch(words[3]) \
            or words[5] not in ("0", "1"):
        return None
    return {"fw": words[2], "mac": words[3], "board": words[4], "enrolled": words[5] == "1"}


def base_line(words: list[str]) -> dict | None:
    """thermite base 1 <fw> <mac> <channel>, as a dict; None if malformed."""
    if len(words) != 5 or words[0] != "base" or words[1] != "1" or not MAC.fullmatch(words[3]) \
            or not words[4].isdigit() or not 1 <= int(words[4]) <= 13:
        return None
    return {"fw": words[2], "mac": words[3], "channel": int(words[4])}


def hello(console: Console, kind: str, wait: float = ANSWER) -> dict | None:
    words = console.ask("thermite hello", ("remote", "base"), wait)
    if words is None or words[0] != kind:
        return None
    return remote_line(words) if kind == "remote" else base_line(words)


def opened() -> set[str]:
    """The files this user's other processes hold open, as /proc shows them."""
    files = set()
    for directory in glob.glob("/proc/[0-9]*/fd"):
        if directory == f"/proc/{os.getpid()}/fd":
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            try:
                files.add(os.readlink(os.path.join(directory, name)))
            except OSError:
                pass
    return files


def probe(kind: str) -> tuple[str, dict]:
    """The Espressif serial port that answers thermite hello as kind ("remote"
    or "base"), and what it said; a port another process has open (serve's
    base) is left alone, as asking it would take lines meant for that
    process. Raises Problem."""
    devices = sorted(path for path in glob.glob(os.path.join(BY_ID, "*")) if ESPRESSIF.search(os.path.basename(path)))
    busy = opened()
    for device in devices:
        if os.path.realpath(device) in busy:
            continue
        try:
            with Console(device) as console:
                answer = hello(console, kind, PROBE)
        except Problem:
            continue
        if answer is not None:
            return device, answer
    raise Problem(f"no {kind} answers on {BY_ID} (Espressif USB ports: {len(devices)})",
                  f"connect it by USB, or name its port with --{'port' if kind == 'remote' else 'base-port'} DEVICE")


def ask_remote(device: str | None) -> tuple[str, dict]:
    if device is None:
        return probe("remote")
    with Console(device) as console:
        answer = hello(console, "remote")
    if answer is None:
        raise Problem(f"{device} does not answer thermite hello as a handheld remote",
                      "is the thermite remote firmware on it? (handheld/firmware/README.md)")
    return device, answer


# --------------------------------------------------------------------------
# the actions


def cmd_serve(arguments) -> int:
    state = arguments.state or state_file()
    environment = dict(os.environ)
    environment.pop("THERMITE_REMOTE_HOUSE_FILE", None)
    environment.pop("THERMITE_REMOTE_TOKEN_FILE", None)
    if arguments.mock:
        backend = ["--mock"]
    else:
        try:
            url = tui.gateway(arguments.url)
        except ValueError as exc:
            raise Problem(str(exc)) from None
        try:
            ca, source = tui.trust(url, arguments.ca_file)
            content = tui.fetch(url, ca, source)
        except tui.Problem as problem:
            raise Problem(*problem.lines) from None
        try:
            model = remote.cached_model(content, models())
        except (ValueError, OSError):
            raise Problem(f"{url} serves a house model this thermite cannot read",
                          "update thermite here or on the server, so both run the same release") from None
        if arguments.ca_file is not None and ca:
            tui.remember(url, ca)
        from oh_access import client

        environment["THERMITE_REMOTE_HOUSE_FILE"] = str(model)
        credential = client.credential_path(url) if url.startswith("https:") else None
        environment = remote.gateway_environment(environment, credential, ca)
        backend = ["--url", url]
    node = shutil.which("node")
    if not node:
        raise Problem("needs Node.js (Arch: pacman -S nodejs)")
    argv = [node, str(MAIN), *backend, "--state", str(Path(state).expanduser().absolute())]
    if arguments.port:
        argv += ["--port", arguments.port]
    if arguments.listen:
        argv += ["--listen", str(Path(arguments.listen).expanduser().absolute())]
    if oh_cli.switches.verbose:
        argv.append("--verbose")
    oh_cli.trace_run(argv)
    os.execve(node, argv, environment)
    return 0


def checked_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise Problem(f"--name {name}: a name is 1 to 32 letters, digits, dots, dashes or underscores, "
                      "starting with a letter or digit")
    return name


def cmd_enroll(arguments) -> int:
    name = checked_name(arguments.name)
    path = arguments.state or state_file()
    data = load(path)
    key = secrets.token_bytes(32)
    if arguments.simulator:
        mac = (arguments.mac or SIMULATED_MAC).lower()
        if not MAC.fullmatch(mac):
            raise Problem(f"--mac {arguments.mac}: a MAC is six pairs of hex digits joined by colons")
        key_file = Path(arguments.key_file or path.parent / f"{name}.key").expanduser().absolute()
        entry = {"name": name, "mac": mac, "key": key.hex(), "board": "simulator", "simulated": True,
                 "key_file": str(key_file), "store": {}, "status": {}}
        replaced = add(data, entry)
        private_parent(path)
        write_key(key_file, key)
        try:
            save(path, data)
        except Problem:
            key_file.unlink(missing_ok=True)
            raise
        print(f"enrolled {name} ({mac}), a simulated remote{' again' if replaced else ''}")
        print(f"its key is in {key_file}; give that file to the simulator")
        return 0
    base = None
    if arguments.base_port:
        with Console(arguments.base_port) as console:
            base = hello(console, "base")
        if base is None:
            raise Problem(f"{arguments.base_port} does not answer thermite hello as a base")
    elif isinstance(data.get("base"), dict) and MAC.fullmatch(str(data["base"].get("mac", ""))):
        base = data["base"]
    if base is None:
        raise Problem("no base is known yet",
                      "run thermite handheld serve once with the base plugged in, or pass --base-port DEVICE")
    channel = int(base.get("channel") or 1)
    device, answer = ask_remote(arguments.port)
    entry = {"name": name, "mac": answer["mac"], "key": key.hex(), "board": answer["board"],
             "store": {}, "status": {"fw": answer["fw"], "board": answer["board"]}}
    add({"remotes": list(data["remotes"])}, dict(entry))
    with Console(device) as console:
        words = console.ask(f"thermite enroll {key.hex()} {base['mac']} {channel}", ("enrolled",))
    if words is None:
        raise Problem(f"{device} did not confirm the enrolment", "try again; the remote keeps its old one until it does")
    # The device holds the key; the bridge learns it from the state file.
    data = load(path)
    add(data, entry)
    save(path, data)
    print(f"enrolled {name} ({answer['mac']}, {answer['board']}) on base {base['mac']}, channel {channel}")
    return 0


def cmd_list(arguments) -> int:
    path = arguments.state or state_file()
    data = load(path)
    rows = []
    for entry in data["remotes"]:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
        store = entry.get("store") if isinstance(entry.get("store"), dict) else {}
        rows.append({"name": entry.get("name"), "mac": entry.get("mac"), "board": entry.get("board") or "",
                     "simulated": entry.get("simulated") is True, "fw": status.get("fw"),
                     "battery": status.get("battery"), "charging": status.get("charging"),
                     "room": store.get("remote.room")})
    base = data.get("base") if isinstance(data.get("base"), dict) else None
    if arguments.json:
        oh_cli.emit({"state": str(path), "base": base, "remotes": rows})
        return 0
    if base:
        print(f"base {base.get('mac')} on channel {base.get('channel')}, firmware {base.get('fw')}")
    else:
        print("no base seen yet (thermite handheld serve records it)")
    if not rows:
        print(f"no remote is enrolled in {path}")
        return 0
    table = [("NAME", "MAC", "BOARD", "BATTERY", "ROOM")]
    for row in rows:
        battery = "" if row["battery"] is None else f"{row['battery']}%" + (" charging" if row["charging"] else "")
        table.append((row["name"], row["mac"], row["board"],
                      battery, row["room"] or ""))
    widths = [max(len(str(line[column])) for line in table) for column in range(len(table[0]))]
    for line in table:
        print("  ".join(str(value).ljust(width) for value, width in zip(line, widths)).rstrip())
    return 0


def cmd_forget(arguments) -> int:
    path = arguments.state or state_file()
    data = load(path)
    entry = find(data, arguments.name)
    if entry is None:
        raise Problem(f"no remote named {arguments.name} is enrolled", "thermite handheld list shows them")
    if arguments.port:
        with Console(arguments.port) as console:
            answer = hello(console, "remote")
            if answer is None:
                raise Problem(f"{arguments.port} does not answer thermite hello as a handheld remote")
            if answer["mac"] != entry.get("mac"):
                raise Problem(f"{arguments.port} is {answer['mac']}, not {arguments.name} ({entry.get('mac')})")
            if console.ask("thermite forget", ("forgotten",)) is None:
                raise Problem(f"{arguments.port} did not confirm that it forgot its enrolment")
    data = load(path)
    data["remotes"] = [other for other in data["remotes"] if not (isinstance(other, dict) and other.get("name") == arguments.name)]
    save(path, data)
    key_file = entry.get("key_file")
    if entry.get("simulated") and isinstance(key_file, str):
        try:
            if stat.S_ISREG(os.lstat(key_file).st_mode):
                os.unlink(key_file)
        except OSError:
            pass
    print(f"forgot {arguments.name} ({entry.get('mac')})" + (", here and on the remote" if arguments.port else ""))
    return 0


def parser() -> argparse.ArgumentParser:
    parser = oh_cli.command("handheld", DESCRIPTION, EXAMPLES, NOTES)
    commands = oh_cli.actions(parser)
    state_help = "the state file (default: ~/.local/state/thermite/handheld/state.json, under $XDG_STATE_HOME)"

    serve = oh_cli.action(commands, "serve", "run the bridge between the base and the house",
                          examples=[example for example in EXAMPLES if " serve" in example[0]])
    serve.add_argument("--url", metavar="URL",
                       help=f"the gateway: https://HOST:PORT, HOST:PORT or HOST (default: {tui.DEFAULT_URL}, "
                            "or a selected site's gateway.port)")
    serve.add_argument("--ca-file", type=Path, metavar="FILE",
                       help=f"trust this copy of the server's {tui.AUTHORITY}; remembered for that address")
    serve.add_argument("--mock", action="store_true", help="the remote page's mock house, which sends nothing")
    where = serve.add_mutually_exclusive_group()
    where.add_argument("--port", metavar="DEVICE", help="the base's USB serial port (default: the one that answers)")
    where.add_argument("--listen", metavar="PATH", help="a Unix socket for a simulated base to connect to")
    serve.add_argument("--state", type=Path, metavar="FILE", help=state_help)

    enroll = oh_cli.action(commands, "enroll", "enroll a remote, or a simulated one",
                           examples=[example for example in EXAMPLES if " enroll" in example[0]])
    enroll.add_argument("--name", required=True, metavar="NAME", help="what to call the remote: letters, digits, . _ -")
    enroll.add_argument("--port", metavar="DEVICE",
                        help="the remote's USB serial port (default: the Espressif port that answers as a remote)")
    enroll.add_argument("--base-port", metavar="DEVICE",
                        help="read the base's MAC and channel from it (default: what serve last saw)")
    enroll.add_argument("--simulator", action="store_true", help="enroll a simulated remote: no device")
    enroll.add_argument("--key-file", type=Path, metavar="FILE",
                        help="with --simulator: where to write its key (default: NAME.key beside the state file)")
    enroll.add_argument("--mac", metavar="MAC",
                        help="with --simulator: its MAC (default: the locally administered one ending 00:01)")
    enroll.add_argument("--state", type=Path, metavar="FILE", help=state_help)

    listing = oh_cli.action(commands, "list", "list the enrolled remotes and the base",
                            examples=[example for example in EXAMPLES if " list" in example[0]])
    listing.add_argument("--state", type=Path, metavar="FILE", help=state_help)
    oh_cli.add_json(listing)

    forget = oh_cli.action(commands, "forget", "forget an enrolled remote",
                           examples=[example for example in EXAMPLES if " forget" in example[0]])
    forget.add_argument("name", metavar="NAME", help="the remote's name (thermite handheld list)")
    forget.add_argument("--port", metavar="DEVICE", help="also erase the enrolment on the remote connected by USB here")
    forget.add_argument("--state", type=Path, metavar="FILE", help=state_help)
    return parser


def main(argv: list[str] | None = None) -> int:
    options = parser()
    arguments = options.parse_args(argv)
    if arguments.command == "serve" and arguments.mock and (arguments.url or arguments.ca_file):
        options.error("--mock takes no --url or --ca-file")
    if arguments.command == "enroll":
        if arguments.simulator and (arguments.port or arguments.base_port):
            options.error("--simulator takes no --port or --base-port")
        if not arguments.simulator and (arguments.key_file or arguments.mac):
            options.error("--key-file and --mac are for --simulator")
    if getattr(arguments, "state", None) is not None:
        arguments.state = Path(arguments.state).expanduser().absolute()
    try:
        return {"serve": cmd_serve, "enroll": cmd_enroll, "list": cmd_list, "forget": cmd_forget}[arguments.command](arguments)
    except Problem as problem:
        for line in problem.lines:
            print(f"thermite handheld: {line}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
