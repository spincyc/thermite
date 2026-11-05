"""Uninstall thermite from this host as one install transaction; --purge
also removes its state.

    thermite uninstall --config DIR [-n | --dry-run] [--purge]
"""

from __future__ import annotations

import os
import sys

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import bridge, changeover, manifest, runner, steps
from oh_install.system import System

RERUN = "Run thermite uninstall again to go on from here."

DESCRIPTION = """\
Undo thermite install on this host as one install transaction, as apt
remove does: Thermite stops, and the install engine, as root, sets aside
everything it installed outside the installation root (the units and
quadlets and their links, the native-port filter and its loaded table, the
node exporter's confinement) and reloads systemd. Its state stays where it
is: runtime/ (conf, userdata, secrets, the deploy records and backups,
Prometheus' samples, Grafana's data) and access/ (the gateway's identity,
TLS key and releases), recorded in /etc/thermite/installed.json as not
installed, so thermite install finds it again. The images, the thermite
user and group and the packages stay too. What was set aside stays in the
installation root's .transaction/ until the next transaction commits.

--purge, as apt purge does, also removes runtime/ and access/ whole, the
installed manifest, Thermite's images and every transaction, and the site's
var/transaction; the site itself, with var/gateway/authority, stays. It
asks you to type the installation root before anything stops. Until the
commit, any failure rolls back in the same run and Thermite runs again.

One y/n question approves this run, including its sudo commands. --dry-run
only reads: it reports what it would do. An agent uses only that: agents
never run sudo.
"""

NOTES = """\
exit status:
  0  committed, or nothing is installed here
  1  declined, refused before anything stopped, or an earlier transaction
     was finished or closed instead of uninstalling
  2  Thermite's files without its installed manifest, or run as root
  3  rolled back: Thermite runs again and its health passes
  4  rolled back, but its health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite uninstall again
  130  interrupted; the next thermite uninstall rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite uninstall -n", "what uninstalling would do here; runs nothing"),
    ("thermite uninstall", "stop thermite and remove what it installed, keeping its state"),
    ("thermite uninstall -n --purge", "what removing the state too would do"),
)


def kept(context: steps.Context, purge: bool) -> str:
    root = context.installation.root
    if purge:
        return ("Kept: the thermite user and group; the packages; the site with its var/gateway/authority; and this "
                "clone. thermite install installs Thermite afresh.")
    return (f"Kept: Thermite's state under {root}, runtime/ and access/ (accounts, pairings, the gateway's identity "
            f"and TLS key, history, monitoring data), recorded in {manifest.INSTALLED} so that thermite install "
            "finds it again; the images (`sudo podman images` lists them); the thermite user and group; the "
            "packages; and this clone. thermite uninstall --purge removes the state and the images too.")


def uninstall(system: System, dry_run: bool, purge: bool) -> int:
    if not dry_run:
        found = changeover.resolve(AREA, system, "uninstall")
        if found is not None:
            return changeover.REFUSED if found == changeover.COMMITTED else found
    context = steps.committed_context(AREA, system)
    root = context.installation.root
    try:
        installed = manifest.installed(system, root)
    except manifest.ManifestError as exc:
        system.say(f"{exc}. {manifest.missing_recovery(context)}")
        return 2
    bridged = installed is None and bridge.needed(system)
    if installed is None and not bridged:
        if manifest.thermite_present(context, system):
            system.say(manifest.missing_recovery(context))
            return 2
        system.say("Thermite is not installed here; there is nothing to uninstall.")
        return 0
    if installed is not None and not installed["installed"] and not purge:
        system.say(f"Thermite is already uninstalled here; its state stays under {root} "
                   "(thermite uninstall --purge removes it).")
        return 0
    what = "uninstalling and purging" if purge else "uninstalling"
    system.say(f"{'What ' + what if dry_run else what.capitalize()} thermite on this host, {context.hostname}, "
               f"{'would do' if dry_run else 'step by step'}:")
    if dry_run:
        return changeover.preview(context, system, changeover.uninstall_work(context, None, purge=purge), first=1)
    system.say(f"This run stops Thermite, removes what it installed, and "
               f"{'removes' if purge else 'keeps'} its state under {root}.")
    if not runner.confirm_run(system, "purge" if purge else "uninstall", RERUN):
        return 1
    code = changeover.run(context, system, lambda id: changeover.uninstall_work(context, id, purge=purge),
                          installed=installed, bridged=bridged, before=None, first=1, pinned=False)
    if code == changeover.COMMITTED:
        system.say(kept(context, purge))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("uninstall", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--purge", action="store_true",
                        help="also remove runtime/ and access/, the images and every transaction")
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite uninstall: run it as your user; it asks once before applying the uninstall",
              file=sys.stderr)
        return 2
    system = System()
    try:
        return uninstall(system, arguments.dry_run, arguments.purge)
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite uninstall: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return changeover.INTERRUPTED
