"""This host's deploys, numbered, the latest last.

    thermite history [--last N] [--details] [--json] [--state DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA

DESCRIPTION = """\
List, read-only, what thermite install, upgrade and rollback deployed on
this host, from <state>/deploy/history: numbered from the first deploy,
oldest first and the latest last, one line each: the number, when (UTC),
the commit, who deployed it, whether thermite test boot had passed on its
conf/ here, and its subject, followed by how it came when not a plain
deploy (install, upgrade or rollback). * marks the one deployed now.
thermite rollback deploys the one before it.

--details adds what each deploy recorded: the files it added (+), changed
(~) and removed (-) in each tree, what became of the reloads and restarts
they asked for, each HTTPS gateway release an install or upgrade then
activated, with the release it replaced, and each upgrade transaction that
rolled it back. Upgrades before the install engine also show the state and
identity backups they took; nothing writes or reads those any more. --json has all of it, with up to 50 paths of each kind
per tree and exact counts.

Deploys from before the history recorded the boot test show ? there,
except the one deployed now, which deployed.json describes; older deploys
have no details. Each deploy also writes a start and an end line to the
journal: journalctl -t thermite-deploy.
"""

EXAMPLES = (
    ("thermite history", "the last 10 deploys here"),
    ("thermite history --last 1 --details", "what the latest one changed, reloaded and activated"),
    ("thermite history --last 0", "every one"),
    ("thermite history --json", "the same, as JSON"),
    ("thermite history --state /tmp/state", "the deploys of a runtime state somewhere else"),
)

BOOT = {True: "passed", False: "not passed", None: "?"}
#: How --details names each tree a deploy syncs.
TREES = {"conf": "conf", "atv_app": "helper", "metrics": "metrics", "exporter_app": "exporter"}
#: Paths --details names for each tree; --json has the rest.
SHOWN = 10
INDENT = " " * 6


def when(entry: dict) -> str:
    return entry["deployed_at"].replace("T", " ").removesuffix("+00:00")


def _name(path: str | None) -> str:
    return Path(path).name if path else "none"


def details(entry: dict) -> list[str]:
    """What the history recorded about one deploy besides its line."""
    out = []
    changes = entry.get("changes", {})
    for tree in sorted(changes, key=lambda name: (list(TREES).index(name) if name in TREES else len(TREES), name)):
        found = changes[tree]
        counts = found.get("counts", {})
        paths = [f"{sign}{path}" for sign, kind in (("+", "added"), ("~", "changed"), ("-", "removed"))
                 for path in found.get(kind, ())]
        total = sum(counts.get(kind, 0) for kind in ("added", "changed", "removed"))
        listed = " ".join(paths[:SHOWN]) + (f" and {total - SHOWN} more" if total > SHOWN else "")
        out.append(f"{TREES.get(tree, tree):<9}{counts.get('added', 0)} added, {counts.get('changed', 0)} changed, "
                   f"{counts.get('removed', 0)} removed" + (f": {listed}" if listed else ""))
    if entry.get("metrics_kept"):
        out.append("metrics  kept as deployed: the commit predates the installed metrics")
    if "reloads" in entry:
        said = [f"{outcome.get('unit', '-')} {outcome.get('outcome', '?')}"
                + (f" (HTTP {outcome['status']})" if outcome.get("outcome") == "refused" else "")
                for outcome in entry["reloads"]]
        out.append("reloads  " + ("; ".join(said) or "none needed"))
    if entry.get("application"):
        application = entry["application"]
        out.append(f"release  application {application.get('version', '?')}"
                   + (f" ({application['digest'][:12]})" if application.get("digest") else ""))
    if entry.get("backup"):
        out.append(f"backup   {entry['backup']}")
    for event in entry.get("gateway", ()):
        at = (event.get("at") or "").replace("T", " ").removesuffix("+00:00")
        out.append(f"gateway  {at} release {_name(event.get('release'))}, replacing "
                   f"{_name(event.get('previous_release'))}" + (f"; identity backup {event['backup']}"
                                                                  if event.get("backup") else ""))
    for event in entry.get("rolled_back", ()):
        at = (event.get("at") or "").replace("T", " ").removesuffix("+00:00")
        restored = event.get("restored")
        undone = event.get("commit")
        out.append(f"rolled back  {at} transaction {event.get('transaction')}"
                   + (f" of {undone[:12]}, which never deployed here" if undone and undone != entry["commit"]
                      else f", deploying {restored[:12]} again" if restored else ""))
    return out


def lines(entries: list[dict], *, with_details: bool = False) -> list[str]:
    """The table: a heading, then one line per deploy (and, with_details,
    what each recorded besides, indented under it)."""
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
        if with_details:
            out.extend(INDENT + line for line in details(entry))
    return out


def show(layout: deploy.State, last: int, as_json: bool, with_details: bool = False) -> int:
    entries = deploy.deploys(layout)
    shown = entries[-last:] if last else entries
    if as_json:
        json.dump(shown, sys.stdout, indent=2)
        print()
        return 0
    if not entries:
        print(f"no deploys yet ({layout.history} does not exist or is empty)")
        return 0
    for line in lines(shown, with_details=with_details):
        print(line)
    if len(shown) < len(entries):
        print(f"({len(entries) - len(shown)} earlier; thermite history --last 0 lists every one)")
    return 0


def record_gateway(layout: deploy.State, event: str) -> int:
    """The installer's record of a gateway activation (oh_install.steps.gateway_history)."""
    try:
        value = json.loads(event)
        if not isinstance(value, dict):
            raise ValueError("not a JSON object")
        deploy.record_gateway(layout, value)
    except (ValueError, OSError, deploy.DeployError) as exc:
        print(f"thermite history: could not record the gateway release in {layout.history}: {exc}", file=sys.stderr)
        return 2
    return 0


def record_rollback(layout: deploy.State, event: str) -> int:
    """The upgrade's record of an install transaction it rolled back (oh_install.changeover)."""
    try:
        value = json.loads(event)
        if not isinstance(value, dict):
            raise ValueError("not a JSON object")
        deploy.record_rollback(layout, value)
    except (ValueError, OSError, deploy.DeployError) as exc:
        print(f"thermite history: could not record the rollback in {layout.history}: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("history", DESCRIPTION, EXAMPLES)
    parser.add_argument("--last", type=int, default=10, metavar="N",
                        help="the last N deploys; 0 for every one (default: 10)")
    parser.add_argument("--details", action="store_true",
                        help="also what each deploy changed, reloaded, backed up and activated")
    oh_cli.add_json(parser)
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the runtime state (default: runtime/ in instance/host.yaml's root, /srv/thermite)")
    parser.add_argument("--_record-gateway", metavar="JSON", help=argparse.SUPPRESS)
    parser.add_argument("--_record-rollback", metavar="JSON", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.last < 0:
        parser.error("--last takes 0 or more")
    layout = deploy.State(args.state) if args.state else deploy.state(deploy.load(AREA)[1])
    if args._record_gateway is not None:
        return record_gateway(layout, args._record_gateway)
    if args._record_rollback is not None:
        return record_rollback(layout, args._record_rollback)
    return show(layout, args.last, args.json, args.details)
