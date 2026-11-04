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
import oh_ports
from oh_cli import AREA, PROGRAM_ROOT

TUI = PROGRAM_ROOT / "tui/remote"
BUILD = Path(os.environ.get("OPENHAB_REMOTE_TUI_BUILD") or AREA / "build/tui-remote")
STATE = AREA / "var/tui-remote/state.json"
#: In BUILD: the digest of the lock its node_modules were installed from.
STAMP = "lock.sha256"
PINNED = ("package.json", "package-lock.json")
#: How main.js gets its colors and the torch (tui/remote/theme.js).
LOOK = "THERMITE_REMOTE_LOOK"
MODEL = "conf/html/remote/house.js"
MODEL_LIMIT = 2 * 1024 * 1024


class RemoteModelError(ValueError):
    """The selected site's model is unavailable; never substitute demo controls."""

DESCRIPTION = """\
The thermite terminal starts on a landing page with Remotes, Security,
Cameras and Site views. Remotes contains rooms, sources, device controls,
Signal and audio, and a menu for pairing TVs and streamers. Security shows
connection status; an unconfigured system is labelled Not connected.
It runs the page's own house.js, remotes.js,
backend.js, session.js and pair.js from web/remote/ under node, so both
front ends drive thermite the same way (tui/remote/README.md).

By default it uses the authenticated site: THERMITE_SITE_URL, the installed
gateway's verified address, or https://localhost on the site's HTTPS port
(instance/host.yaml's gateway.port, 13600 by default). A site with
access.mode: native instead uses its
production runtime without login; --native explicitly selects that testing
mode for this run. Choosing a source really switches the room. --sandbox talks to
the sandbox on 127.0.0.1:13615 and --mock to the page's mock backend,
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
    ("thermite remote --sandbox --room ROOM", "the sandbox, starting in that room"),
    ("thermite remote --pair", "start on the Pairing screen"),
    ("thermite remote --url http://thermite.example:13601 --light", "thermite on another host, light"),
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


def setup(build: Path = BUILD, tui: Path = TUI, npm: str | None = None, verb: str = "remote") -> int:
    npm = npm or shutil.which("npm")
    if not npm:
        print(f"thermite {verb}: npm is needed once, to install blessed (Arch: pacman -S npm)", file=sys.stderr)
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
    from oh_access import client
    if client.deployment(AREA) is not None:
        return False
    import oh_access_mode
    import oh_inventory

    return oh_access_mode.mode(oh_inventory.load(oh_cli.HOUSE) or {}) == "native"


def gateway_port(area: Path | None = None) -> int:
    """The HTTPS application port the site's instance/host.yaml configures
    (gateway.port), or the product default for an area without a host file.
    Raises ValueError (oh_deploy.HostError) for a host file it cannot use."""
    path = Path(oh_cli.AREA if area is None else area) / "instance/host.yaml"
    if not path.is_file():
        return oh_ports.ACCESS_PORT
    import oh_deploy

    return oh_ports.gateway_port(oh_deploy.load_host(path))


def target(arguments: argparse.Namespace) -> list[str]:
    """Which backend main.js uses: --mock, or a thermite controller's URL."""
    if arguments.mock:
        return ["--mock"]
    if arguments.url:
        return ["--url", arguments.url.rstrip("/")]
    if arguments.sandbox:
        return ["--url", oh_cli.SANDBOX_URL]
    if not native_mode(arguments):
        if os.environ.get("THERMITE_SITE_URL"):
            return ["--url", os.environ["THERMITE_SITE_URL"]]
        from oh_access import client
        profile = client.deployment(AREA)
        if profile:
            return ["--url", profile["url"]]
        try:
            port = gateway_port(AREA)
        except ValueError as exc:
            raise RemoteModelError(f"cannot read this site's gateway port: {exc}") from None
        return ["--url", f"https://localhost:{port}"]
    import oh_deploy as deploy

    return ["--url", deploy.production_url(deploy.load(AREA)[1])]


def main_js(node: str, build: Path, state: Path, backend: list[str], arguments: argparse.Namespace) -> list[str]:
    """main.js's command line: blessed in build, the state file, the backend
    (--url URL [--native], or --mock), then the view switches add_view()
    adds (and the mock's, where the parser has them)."""
    argv = [node, str(TUI / "main.js"), "--modules", str(modules(build)), "--state", str(state), *backend]
    if arguments.pair:
        argv.append("--pair")
    if arguments.still:
        argv.append("--still")
    if arguments.log_visible is not None:
        argv.append("--log" if arguments.log_visible else "--no-log")
    for flag, name in (("room", "room"), ("choose", "choose"), ("mode", "mode"), ("volume", "volume"),
                       ("latency", "latency")):
        value = getattr(arguments, name, None)
        if value is not None:
            argv += [f"--{flag}", str(value)]
    if getattr(arguments, "matrix_down", False):
        argv.append("--matrix-down")
    return argv


def node_argv(arguments: argparse.Namespace, node: str = "node", build: Path = BUILD) -> list[str]:
    backend = target(arguments) + (["--native"] if native_mode(arguments) else [])
    return main_js(node, build, arguments.state, backend, arguments)


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
    add_view(parser, "the house file's theme, else thermite")
    parser.add_argument("--latency", type=int, metavar="MS", help="mock only: how long a choice takes (default 600)")
    parser.add_argument("--matrix-down", action="store_true",
                        help="mock only: start with the HDMI matrix not answering")
    parser.add_argument("--state", type=Path, default=STATE, metavar="FILE",
                        help="where the last room, volume mode, theme mode, motion and log visibility are kept "
                             "(default var/tui-remote/state.json)")
    return parser


def add_view(parser: argparse.ArgumentParser, theme: str) -> None:
    """What the terminal shows at start (--pair, --room, --choose, --theme
    defaulting to `theme`, --dark, --light, --volume, --still, --log,
    --no-log): thermite remote's and thermite tui's alike."""
    parser.add_argument("--pair", action="store_true", help="start on the Pairing screen")
    parser.add_argument("--room", metavar="ID", help="start in this room (else the last one used, then the first)")
    parser.add_argument("--choose", metavar="SOURCE",
                        help="choose this source in the room at start; on thermite this really switches it")
    parser.add_argument("--theme", metavar="NAME|FILE",
                        help=f"the colors: a built-in theme's name or a theme file (default: {theme})")
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


def cached_model(content: bytes, directory: Path) -> Path:
    """A private, content-addressed copy of a remote model in directory, once
    it reads as data (never evaluated) of the shape main.js needs; raises
    ValueError or OSError."""
    from oh_access.files import atomic_private, private_directory, read_private
    from oh_features import remote_model

    if len(content) > MODEL_LIMIT:
        raise ValueError("remote model exceeds its limit")
    model = remote_model.literal(content.decode("utf-8"))
    if (model.get("version") != 1 or not isinstance(model.get("rooms"), list)
            or not all(isinstance(model.get(key), dict) for key in ("sources", "devices", "initial"))):
        raise ValueError("invalid remote model")
    path = private_directory(directory) / (hashlib.sha256(content).hexdigest() + ".js")
    if path.exists() or path.is_symlink():
        if read_private(path, MODEL_LIMIT) == content:
            return path
    atomic_private(path, content)
    return path


def model_file(arguments) -> Path | None:
    """Use committed schema-two input; legacy sites keep their generated model."""
    generated = AREA / MODEL
    if arguments.mock:
        return generated if generated.is_file() else None
    from oh_paths import descriptor
    import oh_deploy as deploy

    try:
        selected = descriptor(AREA)
        if selected is None or selected["version"] != 2:
            if not generated.is_file():
                raise RemoteModelError("no generated remote model for this site; run thermite generate")
            return generated
        prepared, _ = deploy.prepared_files(AREA, deploy.resolve(AREA, "HEAD"), AREA / "var")
        return cached_model(prepared[MODEL].data, AREA / "var/tui-remote/models")
    except RemoteModelError:
        raise
    except (OSError, ValueError, KeyError, deploy.DeployError):
        raise RemoteModelError("cannot prepare this site's remote model; check its committed configuration, "
                               "pinned application and private model cache") from None


def launch_environment(arguments, chosen):
    """Only credential/CA paths cross to Node; personal tokens stay in files."""
    environment = {**os.environ, LOOK: look(chosen)}
    environment.pop("THERMITE_REMOTE_HOUSE_FILE", None)
    model = model_file(arguments)
    if model is not None:
        environment["THERMITE_REMOTE_HOUSE_FILE"] = str(model)
    if arguments.mock or native_mode(arguments):
        environment.pop("THERMITE_REMOTE_TOKEN_FILE", None)
        return environment
    from oh_access import client
    url = target(arguments)[1]
    path = client.credential_path(url)
    profile = client.deployment(AREA)
    ca = profile["ca_file"] if profile and profile["url"] == client.origin(url) else client.saved_ca(path)
    return gateway_environment(environment, path, ca)


def gateway_environment(environment: dict, credential: Path | None, ca: str | None) -> dict:
    """environment for main.js on an HTTPS gateway: the personal credential
    file thermite access login saved, if there is one, and the CA to trust."""
    environment.pop("THERMITE_REMOTE_TOKEN_FILE", None)
    # The gateway may explicitly allow anonymous ordinary controls. Let the
    # shared client discover that state; a missing credential is never native mode.
    if credential is not None and (credential.exists() or credential.is_symlink()):
        environment["THERMITE_REMOTE_TOKEN_FILE"] = str(credential)
    if ca:
        environment["NODE_EXTRA_CA_CERTS"] = ca
    return environment


def colors(verb: str, load):
    """The theme load() returns, with a warning where it is hard to read;
    None, having said why, when it cannot be loaded."""
    import oh_brand as brand

    try:
        chosen = load()
    except brand.PaletteError as exc:
        print(f"thermite {verb}: {exc}", file=sys.stderr)
        return None
    problems = brand.contrast_problems(chosen)
    if problems:
        print(f"thermite {verb}: {chosen.name} is hard to read in places: {'; '.join(problems)}", file=sys.stderr)
    return chosen


class LaunchError(Exception):
    """Why main.js cannot start, printed after `thermite VERB: `."""


def launch(verb: str, prepare, build: Path = BUILD) -> int:
    """What thermite remote and thermite tui share once their switches are
    read: Node.js, the pinned blessed in build (installed once), then main.js
    in this process's place. prepare(node) returns main.js's command line and
    environment, or raises LaunchError."""
    node = shutil.which("node")
    if not node:
        print(f"thermite {verb}: needs Node.js (Arch: pacman -S nodejs npm)", file=sys.stderr)
        return 2
    if not ready(build):
        shown = build.relative_to(AREA) if build.is_relative_to(AREA) else build
        print(f"thermite {verb}: installing the pinned blessed into {shown} (once)", file=sys.stderr)
        status = setup(build, verb=verb)
        if status:
            return status
    try:
        command, environment = prepare(node)
    except LaunchError as exc:
        print(f"thermite {verb}: {exc}", file=sys.stderr)
        return 2
    oh_cli.trace_run(command)
    os.execve(node, command, environment)
    return 0


def main(argv: list[str] | None = None) -> int:
    options = parser()
    arguments = options.parse_args(argv)
    if oh_cli.switches.quiet:
        options.error("--quiet would hide the remote itself")
    if (arguments.latency is not None or arguments.matrix_down) and not arguments.mock:
        options.error("--latency and --matrix-down are for --mock")
    if arguments.setup:
        return setup()
    chosen = colors("remote", lambda: palette(arguments.theme))
    if chosen is None:
        return 2

    def prepare(node: str) -> tuple[list[str], dict]:
        try:
            return node_argv(arguments, node), launch_environment(arguments, chosen)
        except RemoteModelError as exc:
            raise LaunchError(str(exc)) from None
        except ValueError:
            raise LaunchError("log in with thermite access login, or explicitly choose --native "
                              "for owner/testing access") from None

    return launch("remote", prepare)
