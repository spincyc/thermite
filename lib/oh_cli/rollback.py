"""Install an earlier site commit, as one install transaction; or, with
--restore-state, go back to the version and the state the last install or
upgrade set aside.

    thermite rollback --config DIR [-n | --dry-run] [--json] [--yes] [--restore-state | COMMIT]
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Callable

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA, revert
from oh_install import changeover, manifest, runner, steps
from oh_install.system import UNATTENDED, System

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
state as it was before the last upgrade, see --restore-state and
docs/installation.md#update-or-remove. Runtime data stays as it is.

--restore-state instead puts back the version that ran before the last
install or upgrade committed, as one install transaction of its own: its
files, units, native-port filter and confinement, and its state in runtime/
and access/ exactly as that upgrade set it aside. Everything written since
that upgrade is lost: accounts, pairings, credentials, the gateway's
identity changes, monitoring data. It says so and asks you to type the
installation root before anything stops. Inside the transaction the site's
branch goes back to the commit before the upgrade's pin commit, so that the
previous version checks its health with its own application; once it
commits, ~/.local/bin/thermite selects the previous application's command
again. It needs the site at that upgrade's pin commit with nothing
uncommitted, and is refused after an uninstall, a clean install or another
restore. (thermite revert did this before.)

--yes gives the run's one approval up front, for an unattended rollback: the
root commands run under sudo -n, nothing reads standard input, and any
other question is answered no. It is refused with --restore-state, which
asks you to type the installation root. --dry-run only reads: it reports
what it would do, and with --json prints that as one JSON object.
"""

NOTES = """\
exit status:
  0  committed and verified
  1  declined, refused before anything stopped, a check failed, or an earlier
     transaction was finished or closed instead of rolling back
  2  nothing to roll back to, an unknown commit, one that pins another
     application release, or no installed version; with --restore-state,
     also the site not at the upgrade's commit or with uncommitted changes,
     or the previous application environment gone
  3  rolled back: the version that ran runs again and its health passes
  4  rolled back, but its health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite rollback again
  130  interrupted; the next thermite rollback rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite rollback -n", "what installing the commit deployed before the current one would do"),
    ("thermite rollback", "install the commit deployed before the current one"),
    ("thermite rollback 3d22e36", "install that commit (thermite history lists them)"),
    ("thermite rollback -n --restore-state", "what going back to the version and state before the last upgrade "
                                             "would do"),
    ("thermite rollback --restore-state", "go back to it, after typing the installation root; state written "
                                          "since is lost"),
    ("thermite rollback -n --json", "the plan, as one JSON object"),
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


def roll_back(system: System, commit: str | None, dry_run: bool, plan: dict | None = None) -> int:
    """Roll back, or with `dry_run` show what that would do; a dry run given
    `plan` (a --json report) fills it in instead of showing the steps."""
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
            f"{wanted[:12]}..HEAD`), then thermite install, or thermite rollback --restore-state to the version "
            "before the last upgrade")
    try:
        installed = manifest.installed(system, context.installation.root)
    except manifest.ManifestError as exc:
        system.say(f"{exc}. {manifest.missing_recovery(context)}")
        return 2
    if installed is None or not installed["installed"]:
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
    if dry_run and plan is not None:
        code, checked = runner.plan(preflight, system)
        prepared, preparation, transaction = changeover.plan(
            system, changeover.install_work(context, None, verb="rollback"), first=len(preflight) + 1)
        plan.update(steps=[*checked, *preparation], transaction=transaction, commit=wanted,
                    deploy={"number": entry["number"], "deployed_at": entry["deployed_at"]} if entry else None)
        return prepared or code
    code = runner.run(preflight, system, AREA, dry_run=dry_run)
    if dry_run:
        return changeover.preview(context, system, changeover.install_work(context, None, verb="rollback"),
                                  first=len(preflight) + 1) or code
    if code:
        return code
    if not runner.confirm_run(system, "rollback", f"Run {changeover.command(AREA, 'rollback')} again."):
        return 1
    return changeover.run(context, system, lambda id: changeover.install_work(context, id, verb="rollback"),
                          installed=installed, before=running or head.head, after=wanted,
                          first=len(preflight) + 1, pinned=False)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("rollback", DESCRIPTION, EXAMPLES, NOTES)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("commit", nargs="?", metavar="COMMIT",
                       help="the commit to install (default: the one deployed before the current one)")
    which.add_argument("--restore-state", action="store_true",
                       help="go back to the version, and the state, the last install or upgrade set aside; "
                            "what was written since is lost")
    oh_cli.add_dry_run(parser)
    oh_cli.add_json(parser)
    oh_cli.add_yes(parser, "the rollback")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite rollback: run it as your user; it asks once before rolling back", file=sys.stderr)
        return 2
    if args.json and not args.dry_run:
        print("thermite rollback: --json reports what a dry run would do: give it with -n/--dry-run",
              file=sys.stderr)
        return 2
    if args.yes and args.restore_state:
        print("thermite rollback: --yes cannot approve --restore-state, which asks you to type the installation "
              "root", file=sys.stderr)
        return 2
    system = System()
    report = {"verb": "rollback", "restore_state": args.restore_state, "dry_run": True} if args.json else None
    # A restore runs as thermite revert did: its transaction and its scope
    # keep the verb "revert", which earlier releases read.
    verb = "revert" if args.restore_state else "rollback"
    body: Callable[[], int] = ((lambda: revert.revert(system, args.dry_run, plan=report)) if args.restore_state
                               else (lambda: roll_back(system, args.commit, args.dry_run, plan=report)))
    approval = UNATTENDED.set(args.yes)
    try:
        return changeover.reported(report, lambda: changeover.scoped(AREA, system, verb, body,
                                                                     dry_run=args.dry_run))
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite rollback: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. Run {changeover.command(AREA, verb)} again.")
        return changeover.INTERRUPTED
    finally:
        UNATTENDED.reset(approval)
