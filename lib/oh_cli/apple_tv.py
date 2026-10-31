"""Run the Apple TV helper for thermite (lib/oh_atv.py) on pinned pyatv.

    thermite apple-tv setup
    thermite apple-tv serve [--setup] [--listen HOST:PORT] [--secrets DIR] [--allow-remote]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import oh_atv as atv
import oh_cli
from oh_cli import AREA

# The helper's container sets it (deploy/openhab-atv.container).
VENV = Path(os.environ.get("OPENHAB_ATV_VENV") or AREA / "build/pyatv-venv")
REQUIREMENTS = AREA / "lib/requirements-pyatv.txt"
#: In the venv: the digest of the requirements it was built from.
STAMP = "requirements.sha256"

DESCRIPTION = """\
The runtime has no native Apple TV binding, so thermite drives the Apple TVs
through this helper: a small HTTP service that keeps one pyatv Companion
connection open per Apple TV and sends keys, power and app launches over it
(lib/oh_atv.py documents the API).
"""

SETUP = """\
`setup` builds build/pyatv-venv (or $OPENHAB_ATV_VENV) with the system
Python and installs pyatv and every dependency from
lib/requirements-pyatv.txt, pinned and hashed (--require-hashes, wheels
only); a venv built from other pins or another Python is rebuilt.
"""

SERVE = """\
`serve` re-executes itself in that venv and serves until SIGTERM or
Ctrl-C; with --setup it first runs `setup` when the venv is missing or
was built from other pins or another Python, as the helper's container in
production does at every start.
"""

NOTES = f"""\
pairing:
  Pairing happens from the thermite Pairing page, through this helper: the
  page asks for a code (POST /devices/<id>/pair), the Apple TV shows four
  digits, and the page sends them back. Nothing is typed here. The
  Companion credential that pairing yields is stored in --secrets DIR
  (default {atv.SECRETS.relative_to(AREA)}/<id>; directory 0700, file 0600,
  gitignored) and never logged or returned. Delete that file to unpair.

localhost only:
  The API has no authentication, and whoever reaches it can pair with and
  drive every Apple TV. thermite runs on the same host, so it listens on
  {atv.LISTEN} and refuses a non-loopback --listen unless --allow-remote
  is given.
"""

EXAMPLES = (
    ("thermite apple-tv setup", "build the venv with the pinned pyatv"),
    ("thermite apple-tv serve", f"serve on {atv.LISTEN}"),
    ("thermite apple-tv serve --setup", "build the venv first if it is missing or stale, as production does"),
)
SETUP_EXAMPLES = (("thermite apple-tv setup", "build build/pyatv-venv, or rebuild it after a pin change"),)
SERVE_EXAMPLES = (
    ("thermite apple-tv serve", f"serve on {atv.LISTEN} until Ctrl-C"),
    ("thermite apple-tv serve --setup --secrets /secrets", "as the helper's container runs it"),
)


def pins() -> str:
    """What a venv is built from: the pinned requirements, and this Python,
    whose upgrade (a new image) breaks a venv as surely as new pins."""
    return hashlib.sha256(REQUIREMENTS.read_bytes() + sys.version.encode("utf-8")).hexdigest()


def ready() -> bool:
    """The venv exists and was built from the current pins."""
    try:
        return (VENV / "bin/python").exists() and (VENV / STAMP).read_text(encoding="utf-8").strip() == pins()
    except FileNotFoundError:
        return False


def setup() -> int:
    built = (VENV / "bin/python").exists()
    if not built or not ready():
        subprocess.run([sys.executable, "-m", "venv", *(["--clear"] if built else []), str(VENV)], check=True)
    subprocess.run([str(VENV / "bin/python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--no-cache-dir", "--require-hashes", "--only-binary", ":all:", "-r", str(REQUIREMENTS)],
                   check=True)
    subprocess.run([str(VENV / "bin/python"), "-c",
                    "import pyatv, pyatv.const, importlib.metadata as m, sys; "
                    "print('pyatv', m.version('pyatv'), 'srptools', m.version('srptools'), "
                    "'ready in', sys.prefix, 'on Python', sys.version.split()[0])"],
                   check=True)
    (VENV / STAMP).write_text(pins() + "\n", encoding="utf-8")
    return 0


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def serve(arguments: argparse.Namespace) -> int:
    if not in_venv():
        if arguments.setup and not ready():
            setup()
        python = VENV / "bin/python"
        if not python.exists():
            print("thermite apple-tv: run `bin/thermite apple-tv setup` first", file=sys.stderr)
            return 2
        # sys.argv as given, not bin/thermite apple-tv: the helper's container may still run it as
        # bin/openhab-atv or as bin/thermite atv (oh_cli.LEGACY, oh_cli.ALIASES).
        os.execv(str(python), [str(python), sys.argv[0], *sys.argv[1:]])
    argv = ["--listen", arguments.listen, "--secrets", str(arguments.secrets)]
    if arguments.allow_remote:
        argv.append("--allow-remote")
    return atv.main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("apple-tv", "\n".join((DESCRIPTION, SETUP, SERVE)), EXAMPLES, NOTES)
    commands = oh_cli.actions(parser)
    oh_cli.action(commands, "setup", "create build/pyatv-venv (or $OPENHAB_ATV_VENV) with the pinned pyatv",
                  description=SETUP, examples=SETUP_EXAMPLES)
    serve_parser = oh_cli.action(commands, "serve", "run the helper (in build/pyatv-venv)",
                                 description=SERVE, examples=SERVE_EXAMPLES, notes=NOTES)
    serve_parser.add_argument("--setup", action="store_true",
                              help="run setup first if the venv is missing or was built from other pins "
                                   "or another Python")
    serve_parser.add_argument("--listen", default=atv.LISTEN, metavar="HOST:PORT",
                              help=f"address to serve on (default {atv.LISTEN})")
    serve_parser.add_argument("--secrets", type=Path, default=atv.SECRETS, metavar="DIR",
                              help="where pairings are stored (default var/secrets/pyatv)")
    serve_parser.add_argument("--allow-remote", action="store_true",
                              help="allow a non-loopback --listen address (no authentication: anyone who "
                                   "reaches it can pair with and drive the Apple TVs)")
    arguments = parser.parse_args(argv)
    if arguments.command == "setup":
        return setup()
    return serve(arguments)
