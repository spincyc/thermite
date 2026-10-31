"""This host's deploys, numbered, the latest last.

    thermite history [--last N] [--json] [--state DIR]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA

DESCRIPTION = """\
List, read-only, what thermite deploy and thermite rollback installed on
this host, from <state>/deploy/history: numbered from the first deploy,
oldest first and the latest last, one line each: the number, when (UTC),
the commit, who deployed it, whether thermite test boot had passed on its
conf/ here, and its subject. * marks the one deployed now; a rollback says
so after its subject. thermite rollback deploys the one before it.

Deploys from before the history recorded the boot test show ? there,
except the one deployed now, which deployed.json describes.
"""

EXAMPLES = (
    ("thermite history", "the last 10 deploys here"),
    ("thermite history --last 0", "every one"),
    ("thermite history --json", "the same, as JSON"),
    ("thermite history --state /tmp/state", "the deploys of a state root somewhere else"),
)

BOOT = {True: "passed", False: "not passed", None: "?"}


def when(entry: dict) -> str:
    return entry["deployed_at"].replace("T", " ").removesuffix("+00:00")


def lines(entries: list[dict]) -> list[str]:
    """The table: a heading, then one line per deploy."""
    if not entries:
        return []
    number = max(len(str(entry["number"])) for entry in entries)
    by = max([len("by"), *(len(entry["by"]) for entry in entries)])
    time = max(len(when(entry)) for entry in entries)
    out = [f"  {'#':>{number}}  {'deployed (UTC)':<{time}}  {'commit':<12}  {'by':<{by}}  {'boot test':<10}  subject"]
    for entry in entries:
        mark = "*" if entry["current"] else " "
        subject = entry["subject"] + (f" ({entry['how']})" if entry["how"] != "deploy" else "")
        out.append(f"{mark} {entry['number']:>{number}}  {when(entry):<{time}}  {entry['commit'][:12]}  "
                   f"{entry['by']:<{by}}  {BOOT[entry['boot_checked']]:<10}  {subject}".rstrip())
    return out


def show(layout: deploy.State, last: int, as_json: bool) -> int:
    entries = deploy.deploys(layout)
    shown = entries[-last:] if last else entries
    if as_json:
        json.dump(shown, sys.stdout, indent=2)
        print()
        return 0
    if not entries:
        print(f"no deploys yet ({layout.history} does not exist or is empty)")
        return 0
    for line in lines(shown):
        print(line)
    if len(shown) < len(entries):
        print(f"({len(entries) - len(shown)} earlier; thermite history --last 0 lists every one)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("history", DESCRIPTION, EXAMPLES)
    parser.add_argument("--last", type=int, default=10, metavar="N",
                        help="the last N deploys; 0 for every one (default: 10)")
    oh_cli.add_json(parser)
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the state root (default: instance/host.yaml state_root)")
    args = parser.parse_args(argv)
    if args.last < 0:
        parser.error("--last takes 0 or more")
    layout = deploy.State(args.state) if args.state else deploy.state(deploy.load(AREA)[1])
    return show(layout, args.last, args.json)
