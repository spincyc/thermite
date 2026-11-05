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
(and disable the path units and the boot-time native port boundary),
remove the unit files thermite install installed and its unit manifest,
and systemctl daemon-reload. The services come from quadlets, which systemd
generates from those files, so removing them is what keeps them from
starting at the next boot.

It keeps the runtime state, runtime/ in the installation root (conf,
userdata, secrets, the deploy records and backups, Prometheus' samples,
Grafana's data and the metrics secrets), the thermite user and group, the
packages, images and host node-exporter confinement configuration, and says
so: thermite install puts runtime and the metrics back on the same state.
--purge, as apt purge does, also deletes the runtime state; before that step
it shows the path and asks you to type it. The HTTPS gateway, its worker and
callback units, and their identity state (accounts, TLS key) in the
installation root's access/ are kept, with the commands that remove them and
then the emptied installation root; so are the loaded native port filter and
its rule file.

One y/n question approves this uninstall run, including its sudo commands.
Compact status shows each stage; -v/--verbose shows details and commands.
Interactive commands keep the terminal, with the spinner paused. Each step
first checks whether it is done and skips it if so. --dry-run only reads:
it reports each step and what it would run. An agent uses only that:
agents never run sudo.
"""

EXAMPLES = (
    ("thermite uninstall -n", "what uninstalling would do here; runs nothing"),
    ("thermite uninstall", "stop thermite and the metrics and remove their units, keeping the state"),
    ("thermite uninstall -n --purge", "what deleting the state too would do"),
)


def kept(context: steps.Context, purge: bool) -> str:
    from oh_access.staging import ACCESS_UNITS
    from oh_install.ingress import RULE_FILE
    installation = context.installation
    state = "" if purge else f"the runtime state, {installation.runtime} (thermite uninstall --purge deletes it); "
    access = installation.access
    units = " ".join(ACCESS_UNITS)
    return (f"Kept: {state}the thermite user and group; the packages; the images (`sudo podman images` lists them, "
            "`sudo podman rmi IMAGE` deletes one); host node-exporter confinement configuration; and this clone.\n"
            f"Also kept: the HTTPS gateway ({units}) and its identity state in {access}: accounts, sessions and the "
            "TLS key. Back that directory up first, then remove them with "
            f"`sudo systemctl disable --now {units}`, "
            + " ".join(f"`sudo rm /etc/systemd/system/{name}`" for name in ACCESS_UNITS)
            + f", `sudo systemctl daemon-reload` and `sudo rm -rf --one-file-system {access}`. Once runtime/ and "
            f"access/ are both gone, `sudo rmdir {installation.root}` removes the installation root, "
            "a btrfs subvolume or a plain directory.\n"
            f"The native port filter stays loaded until reboot: `sudo nft delete table inet thermite_native` "
            f"unloads it and `sudo rm {RULE_FILE}` removes its rules.\n"
            "thermite install installs runtime, the gateway and the metrics again.")


def uninstall(system: System, dry_run: bool, purge: bool) -> int:
    from oh_cli.install import refuse_open
    if refuse_open(system, dry_run):
        return 2
    versions, host = deploy.load(AREA)
    context = steps.Context(AREA, versions, host, "", "", system.hostname(), system.address())
    what = "uninstalling and purging" if purge else "uninstalling"
    system.say(f"{'What ' + what if dry_run else what.capitalize()} runtime on this host, {context.hostname}, "
               f"{'would do' if dry_run else 'step by step'}:")
    if not dry_run:
        system.say(f"This run stops runtime and monitoring, removes their units, and "
                   f"{'deletes' if purge else 'keeps'} the runtime state at {context.layout.root}.")
        if not runner.confirm_run(system, "uninstall", RERUN):
            return 1
    code = runner.run(steps.uninstall(context, purge), system, AREA, dry_run=dry_run, rerun=RERUN,
                      root_confirmed=not dry_run)
    if not code or dry_run:
        system.say(kept(context, purge))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("uninstall", DESCRIPTION, EXAMPLES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--purge", action="store_true",
                        help="also delete the runtime state: conf, userdata, secrets, deploy records, backups, metrics")
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite uninstall: run it as your user; it asks once before applying the uninstall",
              file=sys.stderr)
        return 2
    system = System()
    try:
        return uninstall(system, arguments.dry_run, arguments.purge)
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return 130
