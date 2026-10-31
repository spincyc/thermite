"""Stop thermite on this host and remove its units; --purge also deletes its
state.

    thermite uninstall [-n | --dry-run] [--purge]
"""

from __future__ import annotations

import os
import sys

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import runner, steps
from oh_install.system import System

RERUN = "Run thermite uninstall again to go on from here."

DESCRIPTION = """\
Undo thermite install on this host, as apt remove does: stop runtime, the
Apple TV helper, the metrics services (Prometheus, node_exporter,
blackbox_exporter, the runtime exporter, Grafana) and their restart paths
(and disable the path units), remove the unit files thermite install
installed, and systemctl daemon-reload. The services come from quadlets,
which systemd generates from those files, so removing them is what keeps
them from starting at the next boot.

It keeps the state root (conf, userdata, secrets, the deploy records and
backups, Prometheus' samples, Grafana's data and the metrics secrets), the
openhab user and group, the packages and the images, and says so: thermite
install puts runtime and the metrics back on the same state. --purge, as apt
purge does, also deletes the state root; before that step it shows the path
and asks you to type it, on top of the y before each sudo command.

Each step first checks whether it is done and skips it if so. --dry-run
only reads: it reports each step and what it would run. An agent uses only
that: agents never run sudo.
"""

EXAMPLES = (
    ("thermite uninstall -n", "what uninstalling would do here; runs nothing"),
    ("thermite uninstall", "stop thermite and the metrics and remove their units, keeping the state"),
    ("thermite uninstall -n --purge", "what deleting the state too would do"),
)


def kept(context: steps.Context, purge: bool) -> str:
    state = "" if purge else f"the state root, {context.layout.root} (thermite uninstall --purge deletes it); "
    return (f"Kept: {state}the openhab user and group; the packages; the images (`sudo podman images` lists them, "
            "`sudo podman rmi IMAGE` deletes one); and this clone. thermite install installs runtime and the metrics "
            "again.")


def uninstall(system: System, dry_run: bool, purge: bool) -> int:
    versions, host = deploy.load(AREA)
    context = steps.Context(AREA, versions, host, "", "", system.hostname(), system.address())
    what = "uninstalling and purging" if purge else "uninstalling"
    system.say(f"{'What ' + what if dry_run else what.capitalize()} runtime on this host, {context.hostname}, "
               f"{'would do' if dry_run else 'step by step'}:")
    code = runner.run(steps.uninstall(context, purge), system, AREA, dry_run=dry_run, rerun=RERUN)
    if not code or dry_run:
        system.say(kept(context, purge))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("uninstall", DESCRIPTION, EXAMPLES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--purge", action="store_true",
                        help="also delete the state root: conf, userdata, secrets, deploy records, backups, metrics")
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite uninstall: run it as your user; it asks before each command it runs with sudo",
              file=sys.stderr)
        return 2
    system = System()
    try:
        return uninstall(system, arguments.dry_run, arguments.purge)
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return 130
