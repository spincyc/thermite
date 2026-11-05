"""Install an earlier site commit, as one install transaction.

    thermite rollback --config DIR [COMMIT] [-n | --dry-run]
"""

from __future__ import annotations

import os
import shlex
import sys

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import bridge, changeover, manifest, runner, steps
from oh_install.system import System

DESCRIPTION = """\
Install an earlier site commit on this host as one install transaction:
without COMMIT, the commit deployed before the one deployed now (thermite
history lists them); with it, that commit. Everything the commit pins is
installed as thermite install installs HEAD: its configuration with a boot
test, its units, native versions and images, the native-port filter and the
HTTPS gateway; the transaction commits only once its health passes, and any
failure rolls back to the version that runs now. The deploy history records
it as a rollback.

The site's branch does not move: the next thermite install or upgrade
installs HEAD again. To keep the earlier configuration, revert the site to
it in a new commit. The commit must pin the application release that runs
this command; for one pinned by another release, or to bring back the
state as it was before the last upgrade, see thermite revert and
docs/installation.md#update-or-remove. Runtime data stays as it is.
"""

NOTES = """\
exit status:
  0  committed and verified
  1  declined, refused before anything stopped, a check failed, or an earlier
     transaction was finished or closed instead of rolling back
  2  nothing to roll back to, an unknown commit, one that pins another
     application release, or no installed version
  3  rolled back: the version that ran runs again and its health passes
  4  rolled back, but its health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite rollback again
  130  interrupted; the next thermite rollback rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite rollback -n", "what installing the commit deployed before the current one would do"),
    ("thermite rollback", "install the commit deployed before the current one"),
    ("thermite rollback 3d22e36", "install that commit (thermite history lists them)"),
)


def target(entries: list[dict], record: dict, commit: str | None) -> tuple[str, dict | None]:
    """(the commit to install, its latest entry in the history if it has one)."""
    if commit is None:
        current = record.get("commit")
        if not current:
            raise deploy.DeployError("nothing is deployed here yet, so there is nothing to roll back to "
                                     "(thermite install)")
        end = next((i for i, entry in enumerate(entries) if entry["current"]), len(entries))
        for entry in reversed(entries[:end]):
            if entry["commit"] != current:
                return entry["commit"], entry
        raise deploy.DeployError(f"the history holds no deploy before {current[:12]}'s: name the COMMIT")
    full = deploy.resolve(AREA, commit)
    return full, next((entry for entry in reversed(entries) if entry["commit"] == full), None)


def roll_back(system: System, commit: str | None, dry_run: bool) -> int:
    if not dry_run:
        found = changeover.resolve(AREA, system, "rollback")
        if found is not None:
            return changeover.REFUSED if found == changeover.COMMITTED else found
    head = steps.committed_context(AREA, system)
    layout = head.layout
    wanted, entry = target(deploy.deploys(layout), deploy.read_record(layout), commit)
    context = steps.committed_context(AREA, system, revision=wanted)
    if (context.application or {}).get("digest") != (head.application or {}).get("digest"):
        raise deploy.DeployError(
            f"{wanted[:12]} pins another application release than {head.head[:12]} does; installing it needs that "
            f"release: revert the site to it in a new commit (`git -C {shlex.quote(str(AREA))} revert --no-edit "
            f"{wanted[:12]}..HEAD`), then thermite install, or thermite revert to the version before the last "
            "upgrade")
    try:
        installed = manifest.installed(system, context.installation.root)
    except manifest.ManifestError as exc:
        system.say(f"{exc}. {manifest.missing_recovery(context)}")
        return 2
    bridged = installed is None and bridge.needed(system)
    if (installed is None or not installed["installed"]) and not bridged:
        if manifest.thermite_present(context, system):
            system.say(manifest.missing_recovery(context))
        else:
            system.say("Thermite is not installed here, so there is nothing to roll back: thermite install.")
        return 2
    running = installed["release"].get("site") if installed else None
    system.say(f"{'What rolling back' if dry_run else 'Rolling back'} thermite on this host, {context.hostname}, to "
               f"{wanted[:12]}" + (f", deploy #{entry['number']} of {entry['deployed_at']}" if entry else "")
               + f", {'would do' if dry_run else 'step by step'}:")
    preflight = [steps.Preconditions(context), steps.Committed(context, head=head.head)]
    code = runner.run(preflight, system, AREA, dry_run=dry_run)
    if dry_run:
        return changeover.preview(context, system, changeover.install_work(context, None, verb="rollback"),
                                  first=len(preflight) + 1) or code
    if code:
        return code
    if not runner.confirm_run(system, "rollback", f"Run {changeover.command(AREA, 'rollback')} again."):
        return 1
    return changeover.run(context, system, lambda id: changeover.install_work(context, id, verb="rollback"),
                          installed=installed, bridged=bridged, before=running or head.head, after=wanted,
                          first=len(preflight) + 1, pinned=False)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("rollback", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("commit", nargs="?", metavar="COMMIT",
                        help="the commit to install (default: the one deployed before the current one)")
    oh_cli.add_dry_run(parser)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite rollback: run it as your user; it asks once before rolling back", file=sys.stderr)
        return 2
    system = System()
    try:
        return roll_back(system, args.commit, args.dry_run)
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite rollback: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. Run {changeover.command(AREA, 'rollback')} again.")
        return changeover.INTERRUPTED
