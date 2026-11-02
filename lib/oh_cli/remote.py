"""Run the house remote in a terminal (tui/remote/) on the pinned blessed.

    thermite remote [--url URL | --sandbox | --mock] [--pair] [--room ID] [--theme NAME|FILE] ...
    thermite remote --setup
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import oh_cli
from oh_cli import AREA, PROGRAM_ROOT

TUI = PROGRAM_ROOT / "tui/remote"
BUILD = Path(os.environ.get("OPENHAB_REMOTE_TUI_BUILD") or AREA / "build/tui-remote")
STATE = AREA / "var/tui-remote/state.json"
#: In BUILD: the digest of the lock its node_modules were installed from.
STAMP = "lock.sha256"
PINNED = ("package.json", "package-lock.json")
#: How main.js gets its colors and the torch (tui/remote/theme.js).
LOOK = "THERMITE_REMOTE_LOOK"

DESCRIPTION = """\
The thermite terminal starts on a landing page with Remotes, Security,
Cameras and Site views. Remotes contains rooms, sources, device controls,
Signal and audio, and a menu for pairing TVs and streamers. Security shows
connection status; an unconfigured system is labelled Not connected.
It runs the page's own house.js, remotes.js,
backend.js, session.js and pair.js from web/remote/ under node, so both
front ends drive thermite the same way (tui/remote/README.md).

By default it uses the authenticated site (THERMITE_SITE_URL, or
https://localhost:9443). A site with access.mode: native instead uses its
production runtime without login; --native explicitly selects that testing
mode for this run. Choosing a source really switches the room. --sandbox talks to
the sandbox on 127.0.0.1:18080 and --mock to the page's mock backend,
which sends nothing.

Its colors are a thermite theme's, as the remote page's are: the house
file's `theme` (thermite's own without one), or --theme; --dark (the
default) and --light pick its mode, and t switches it. Where the window has
the space, Home shows a small, wide and shallow bowl of flaming thermite
beside its title: a fine contour with brief sparks, still with --still. Remotes has no
flame animation. It draws in
24-bit color where the terminal shows it (COLORTERM=truecolor, known terminals, tmux
with RGB) and in xterm's 256 colors otherwise; THERMITE_REMOTE_COLOR=256,
truecolor or none overrides that, and --no-color or NO_COLOR draws without
color. --still, or THERMITE_REMOTE_STILL, stops what moves; a switches it.

The log pane is shown when the window has room. --no-log hides it and
gives its rows to the controls; --log shows it for this run. L switches it
and keeps the choice. Hiding the pane keeps collecting the same action
and status messages, so they are there when it is shown again.

The first run (and --setup) installs the pinned blessed from
tui/remote/package-lock.json into build/tui-remote with npm ci
(--ignore-scripts, integrity-checked); a lock change installs it again.
"""

NOTES = """\
keys:
  as on the page: arrows D-pad, Enter OK, Backspace or Esc Back, + and -
  volume, M mute, P play/pause, 0 Off, 1-9 a source, [ and ] the room;
  and Tab to move the focus, Space to press it, r rooms, i info,
  v volume slider, t theme, a motion, L log pane,
  F1 Home, F2 Remotes, F3 Security, F4 Cameras, F5 Site;
  within Remotes, F6 or Ctrl-P opens Pair devices;
  ? help, q quit.
"""

EXAMPLES = (
    ("thermite remote", "thermite landing page on this host; Remotes choices really switch rooms"),
    ("thermite remote --mock", "the page's mock backend, which sends nothing"),
    ("thermite remote --sandbox --room living_room", "the sandbox, starting in the living room"),
    ("thermite remote --pair", "start on the Pairing screen"),
    ("thermite remote --url http://thermite.example:8080 --light", "thermite on another host, light"),
    ("thermite remote --mock --theme contrast", "in the high-contrast theme"),
    ("thermite remote --no-log", "give the log pane's rows to the controls; L shows it again"),
    ("thermite remote --setup", "only install the pinned blessed, then exit"),
)


def lock_digest(tui: Path = TUI) -> str:
    return hashlib.sha256((tui / "package-lock.json").read_bytes()).hexdigest()


def modules(build: Path = BUILD) -> Path:
    return build / "node_modules"


def ready(build: Path = BUILD, tui: Path = TUI) -> bool:
    """blessed is installed in build from the current lock."""
    try:
        stamp = (build / STAMP).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    return (modules(build) / "blessed/package.json").is_file() and stamp == lock_digest(tui)


def setup(build: Path = BUILD, tui: Path = TUI, npm: str | None = None) -> int:
    npm = npm or shutil.which("npm")
    if not npm:
        print("thermite remote: npm is needed once, to install blessed (Arch: pacman -S npm)", file=sys.stderr)
        return 2
    build.mkdir(parents=True, exist_ok=True)
    (build / STAMP).unlink(missing_ok=True)
    for name in PINNED:
        shutil.copyfile(tui / name, build / name)
    oh_cli.trace_run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"], build)
    subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"],
                   cwd=build, check=True)
    (build / STAMP).write_text(lock_digest(tui) + "\n", encoding="utf-8")
    print(f"blessed ready in {modules(build)}")
    return 0


def native_mode(arguments: argparse.Namespace) -> bool:
    """An explicit target cannot inherit a different local site's testing mode."""
    if arguments.mock:
        return False
    if getattr(arguments, "native", False) or arguments.sandbox:
        return True
    if arguments.url or os.environ.get("THERMITE_SITE_URL"):
        return False
    import oh_access_mode
    import oh_inventory

    return oh_access_mode.mode(oh_inventory.load(oh_cli.HOUSE) or {}) == "native"


def target(arguments: argparse.Namespace) -> list[str]:
    """Which backend main.js uses: --mock, or a thermite controller's URL."""
    if arguments.mock:
        return ["--mock"]
    if arguments.url:
        return ["--url", arguments.url.rstrip("/")]
    if arguments.sandbox:
        return ["--url", oh_cli.SANDBOX_URL]
    if not native_mode(arguments):
        return ["--url", os.environ.get("THERMITE_SITE_URL", "https://localhost:9443")]
    import oh_deploy as deploy

    return ["--url", deploy.production_url(deploy.load(AREA)[1])]


def node_argv(arguments: argparse.Namespace, node: str = "node", build: Path = BUILD) -> list[str]:
    argv = [node, str(TUI / "main.js"), "--modules", str(modules(build)), "--state", str(arguments.state)]
    argv += target(arguments)
    if native_mode(arguments):
        argv.append("--native")
    if arguments.pair:
        argv.append("--pair")
    if arguments.still:
        argv.append("--still")
    if arguments.log_visible is not None:
        argv.append("--log" if arguments.log_visible else "--no-log")
    for flag, name in (("room", "room"), ("choose", "choose"), ("mode", "mode"), ("volume", "volume"),
                       ("latency", "latency")):
        value = getattr(arguments, name)
        if value is not None:
            argv += [f"--{flag}", str(value)]
    if arguments.matrix_down:
        argv.append("--matrix-down")
    return argv


def palette(spec: str | None, house: Path = oh_cli.HOUSE):
    """--theme's theme, else the house file's (oh_features.remote.palette);
    raises oh_brand.PaletteError."""
    import oh_brand as brand

    if spec:
        return brand.load(spec)
    import oh_inventory as inventory
    from oh_features import remote as page

    return page.palette(inventory.load(house) or {})


def look(chosen) -> str:
    """LOOK's value: the theme as oh_brand writes it, and the torch's paths
    and how to sample them, which tui/remote/torch.js draws and flickers."""
    import oh_brand as brand

    return json.dumps({**json.loads(brand.as_json(chosen)), "torch": brand.spec()})


def parser() -> argparse.ArgumentParser:
    parser = oh_cli.command("remote", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_target(parser, "production", "sandbox", "url", "mock", default="production")
    parser.add_argument("--native", action="store_true",
                        help="explicitly use the native runtime instead of site authentication (owner/testing only)")
    parser.add_argument("--setup", action="store_true",
                        help="only install the pinned blessed into build/tui-remote, then exit")
    parser.add_argument("--pair", action="store_true", help="start on the Pairing screen")
    parser.add_argument("--room", metavar="ID", help="start in this room (else the last one used, then the first)")
    parser.add_argument("--choose", metavar="SOURCE",
                        help="choose this source in the room at start; on thermite this really switches it")
    parser.add_argument("--theme", metavar="NAME|FILE",
                        help="the colors: a built-in theme's name or a theme file "
                             "(default: the house file's theme, else thermite)")
    oh_cli.add_mode(parser, default=None, note=" for this run (t switches and keeps it)")
    parser.add_argument("--volume", choices=("slider", "buttons"),
                        help="the volume as a slider or as the + and - keys (v switches and keeps it)")
    parser.add_argument("--still", action="store_true",
                        help="nothing moves (as THERMITE_REMOTE_STILL=1 does); a switches it and keeps it")
    logging = parser.add_mutually_exclusive_group()
    logging.add_argument("--log", dest="log_visible", action="store_true",
                         help="show the log pane when the window has room, for this run (L switches and keeps it)")
    logging.add_argument("--no-log", dest="log_visible", action="store_false",
                         help="hide the log pane and give its rows to the controls, for this run")
    parser.set_defaults(log_visible=None)
    parser.add_argument("--latency", type=int, metavar="MS", help="mock only: how long a choice takes (default 600)")
    parser.add_argument("--matrix-down", action="store_true",
                        help="mock only: start with the HDMI matrix not answering")
    parser.add_argument("--state", type=Path, default=STATE, metavar="FILE",
                        help="where the last room, volume mode, theme mode, motion and log visibility are kept "
                             "(default var/tui-remote/state.json)")
    return parser


def launch_environment(arguments, chosen):
    """Only credential/CA paths cross to Node; personal tokens stay in files."""
    environment = {**os.environ, LOOK: look(chosen),
                   "THERMITE_REMOTE_HOUSE_FILE": str(AREA / "conf/html/remote/house.js")}
    if arguments.mock or native_mode(arguments):
        environment.pop("THERMITE_REMOTE_TOKEN_FILE", None)
        return environment
    from oh_access import client
    url = target(arguments)[1]
    path, _ = client.load(url)
    environment["THERMITE_REMOTE_TOKEN_FILE"] = str(path)
    ca = client.saved_ca(path)
    if ca:
        environment["NODE_EXTRA_CA_CERTS"] = ca
    return environment


def main(argv: list[str] | None = None) -> int:
    options = parser()
    arguments = options.parse_args(argv)
    if oh_cli.switches.quiet:
        options.error("--quiet would hide the remote itself")
    if (arguments.latency is not None or arguments.matrix_down) and not arguments.mock:
        options.error("--latency and --matrix-down are for --mock")
    if arguments.setup:
        return setup()
    import oh_brand as brand

    try:
        chosen = palette(arguments.theme)
    except brand.PaletteError as exc:
        print(f"thermite remote: {exc}", file=sys.stderr)
        return 2
    problems = brand.contrast_problems(chosen)
    if problems:
        print(f"thermite remote: {chosen.name} is hard to read in places: {'; '.join(problems)}", file=sys.stderr)
    node = shutil.which("node")
    if not node:
        print("thermite remote: needs Node.js (Arch: pacman -S nodejs npm)", file=sys.stderr)
        return 2
    if not ready():
        print("thermite remote: installing the pinned blessed into build/tui-remote (once)", file=sys.stderr)
        status = setup()
        if status:
            return status
    try:
        command = node_argv(arguments, node)
        environment = launch_environment(arguments, chosen)
    except ValueError:
        print("thermite remote: log in with thermite access login, or explicitly choose --native for owner/testing access", file=sys.stderr)
        return 2
    oh_cli.trace_run(command)
    os.execve(node, command, environment)
    return 0
