"""Export the Control4 project read-only and compare it with devices.yaml.

    thermite control4
    thermite control4 setup
    thermite control4 fetch [--controller-ip IP] [--no-bindings]
    thermite control4 summary [FILE] [--house FILE]

Without an action it runs setup when the venv is missing and fetch when
var/control4 holds no export with network details yet, then summary.
setup builds build/control4-venv with the pinned pyControl4
(lib/requirements-control4.txt). fetch prompts for the Control4 account,
asks the Director for its items, their bindings and network details with GET
requests only and writes them to var/control4/items-<UTC>.json and its
.bindings.json and .details.json side files (0600); the password and tokens
are never written. summary reads the newest export (or FILE) without any network
access and writes var/control4/summary.md.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path

import oh_cli
import oh_control4 as control4
from oh_cli import AREA, PROGRAM_ROOT

VENV = AREA / "build/control4-venv"
REQUIREMENTS = PROGRAM_ROOT / "lib/requirements-control4.txt"
EXPORTS = AREA / "var/control4"
#: How to run this verb again, in the venv or in a child.
COMMAND = [str(PROGRAM_ROOT / "bin/thermite"), "--site", str(AREA), "control4"]

EXAMPLES = (
    ("thermite control4", "set up and fetch as needed, then the summary"),
    ("thermite control4 fetch", "take a new export (prompts for the Control4 account)"),
    ("thermite control4 summary", "summarize the newest export again, offline"),
)


def setup() -> int:
    if not (VENV / "bin/python").exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV / "bin/python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes",
                    "--only-binary", ":all:", "-r", str(REQUIREMENTS)], check=True)
    subprocess.run([str(VENV / "bin/python"), "-c",
                    "import pyControl4.account, pyControl4.director, importlib.metadata as m; "
                    "print('pyControl4', m.version('pyControl4'), 'ready in', __import__('sys').prefix)"],
                   check=True)
    return 0


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def fetch(arguments: argparse.Namespace) -> int:
    try:
        if arguments.controller_ip is None:
            import oh_inventory
            data = oh_inventory.load(oh_cli.HOUSE)
        else:
            data = {}
        arguments.controller_ip = control4.director_ip(data, arguments.controller_ip)
    except ValueError as error:
        print(f"thermite control4: {error}", file=sys.stderr)
        return 2
    if not in_venv():
        python = VENV / "bin/python"
        if not python.exists():
            print("thermite control4: run `thermite control4 setup` first", file=sys.stderr)
            return 2
        os.execv(str(python), [str(python), *COMMAND, "fetch", "--controller-ip", arguments.controller_ip,
                              *(["--no-bindings"] if arguments.no_bindings else [])])
    if not sys.stdin.isatty():
        print("thermite control4: fetch prompts for the Control4 account; run it in a terminal",
              file=sys.stderr)
        return 2
    username = input("Control4 account email: ").strip()
    password = getpass.getpass("Control4 account password: ")
    try:
        result = asyncio.run(control4.fetch(username, password, arguments.controller_ip,
                                            bindings=not arguments.no_bindings))
    except Exception as error:
        # The exception text can quote a server response, never the password.
        print(f"thermite control4: fetch failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        del password
    control4.private_dir(EXPORTS)
    stamp = control4.stamp()
    items_path = control4.write_private(EXPORTS / f"items-{stamp}.json", result["items"])
    if result["bindings"] is not None:
        control4.write_private(control4.bindings_path(items_path), result["bindings"])
    control4.write_private(control4.details_path(items_path), result["details"])
    info = result["controller"]
    print(f"controller {info['name']} ({info['commonName']}), OS {info['osVersion']}")
    print(f"{len(result['items'])} items, network details for {len(result['details'])} devices -> {items_path}")
    print("next: thermite control4 summary")
    return 0


def missing_steps(arguments: argparse.Namespace) -> int:
    steps = control4.pending_steps(VENV / "bin/python", EXPORTS)
    newest = control4.newest_export(EXPORTS)
    if "fetch" not in steps:
        print(f"using {newest.name}; "
              "`thermite control4 fetch` takes a new export", file=sys.stderr)
    elif newest is not None:
        print(f"{newest.name} has no network details; taking a new export", file=sys.stderr)
    for step in steps:
        print(f"== {step}", file=sys.stderr)
        if step == "setup":
            code = setup()
        elif step == "fetch":
            # A child keeps this process on the system Python, which has the
            # PyYAML the summary needs; the venv has only pyControl4.
            code = subprocess.run([sys.executable, *COMMAND, "fetch"]).returncode
        else:
            code = summarize(arguments)
        if code:
            return code
    return 0


def summarize(arguments: argparse.Namespace) -> int:
    import oh_inventory as inventory

    path = arguments.file or control4.newest_export(EXPORTS)
    if path is None:
        print(f"thermite control4: no items-*.json in {EXPORTS}; "
              "run `thermite control4` to set up and fetch one", file=sys.stderr)
        return 2
    items, bindings, details = control4.load_export(path)
    data = inventory.load(arguments.house)
    text = control4.markdown(Path(path), control4.devices(items, bindings, data, details), data)
    control4.private_dir(EXPORTS)
    out = EXPORTS / "summary.md"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text, end="")
    print(f"-> {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("control4", __doc__.splitlines()[0], EXAMPLES)
    parser.set_defaults(file=None, house=oh_cli.HOUSE)
    commands = oh_cli.actions(parser, required=False)
    oh_cli.action(commands, "setup", "create build/control4-venv with the pinned pyControl4",
                  examples=(("thermite control4 setup", "build the venv, or update its pins"),))
    fetch_parser = oh_cli.action(commands, "fetch", "read the Director's items (prompts for the account)",
                                 description="GET requests only; the password and tokens are never written.",
                                 examples=(
                                     ("thermite control4 fetch", "items, bindings and network details"),
                                     ("thermite control4 fetch --no-bindings", "without the IR ports"),
                                 ))
    fetch_parser.add_argument("--controller-ip", default=None, metavar="IP",
                              help="Director address; defaults to control4_director or the single controller in the inventory")
    fetch_parser.add_argument("--no-bindings", action="store_true",
                              help="skip the per-device GET of connections (IR ports)")
    summary_parser = oh_cli.action(commands, "summary", "rooms, classes and devices.yaml stubs", examples=(
        ("thermite control4 summary", "the newest export, into var/control4/summary.md"),
        ("thermite control4 summary var/control4/items-20250101T000000Z.json", "that export"),
    ))
    summary_parser.add_argument("file", nargs="?", type=Path, help="the export (default: the newest)")
    oh_cli.add_house(summary_parser)
    arguments = parser.parse_args(argv)
    # What follows the verb, to run it again in the venv.
    arguments.argv = list(sys.argv[2:] if argv is None else argv)
    if arguments.command == "setup":
        return setup()
    if arguments.command == "fetch":
        return fetch(arguments)
    if arguments.command == "summary":
        return summarize(arguments)
    return missing_steps(arguments)
