"""Update the application, then move this host's services to what the site
pins, or show what that would do.

    thermite upgrade --config DIR [-n | --dry-run] [--source DIR] [--revision REV]
                     [--native-timeout SECONDS] [--allow-downgrade]
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import subprocess
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import bridge, changeover, manifest, ownership, progress, runner, steps
from oh_install.system import System

RERUN = "Run thermite upgrade again to go on from here."

DESCRIPTION = """\
Upgrade the complete Thermite installation from the official GitHub main branch.
Select your committed private site with --config DIR. Use --revision REV to
choose a reviewed branch, tag or commit, or --source DIR for a clean local
application checkout. Native component versions still come from the site's
versions.json; commit desired changes there before running this command.

One approval covers fetching and validating the application, preparing its
private Python environment, validating and committing the site release pin,
then installing the new version as one install transaction: native runtime
and helper services, monitoring, the HTTPS gateway, policy worker, verified
callbacks and identity schema. Existing authority, accounts, credentials and
TLS certificates are preserved.

Everything that can be done while Thermite runs is done first: configuration,
packages, the boot test, pulling the pinned images and checking there is room
to set the installed version aside; too little room refuses before anything
stops. Then the install engine, as root, keeps thermite.target from booting,
Thermite stops and the installed version is set aside. The new version is
installed and started; only when its health passes does the transaction
commit. Any failure from there rolls back in the same run: the installed
version, its files and state as they were, runs again, and the site's pin
commit is undone. The previous version stays set aside until the next
upgrade commits.

The previous owner command remains selected until the transaction commits.
If a run is interrupted, or its rollback cannot finish, the next thermite
upgrade first finishes or rolls back that transaction, then stops and says
so, with that outcome's exit status (1 when it finished a commit or closed
a transaction that never began); thermite status names it. Previous releases do not undo schema changes,
and upgrade refuses to pin a release older than the site's unless
--allow-downgrade is given after deliberate recovery. --native-timeout sets
the one native readiness allowance (300-7200 seconds).

--dry-run changes nothing and does not fetch: the remote revision and its exact
host differences remain unknown until an approved run. When the site pins the
release running it, it prints that release's provisional host plan for this
host. Run as your ordinary user. Use -v for details. Agents only use --dry-run.
"""

NOTES = """\
exit status:
  0  committed and verified, or nothing differed and the installed version verified
  1  declined, refused before anything stopped, a check failed, or an earlier
     transaction was finished or closed instead of upgrading
  2  invalid inputs, an older release without --allow-downgrade, missing
     installation or manifest, or run as root
  3  rolled back: the installed version runs again and its health passes
  4  rolled back, but the reinstated version's health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite upgrade again
  130  interrupted; thermite upgrade rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite upgrade -n", "what upgrading would do here; runs nothing"),
    ("thermite upgrade", "upgrade, asking once for the whole run; rolls back by itself on failure"),
)


def upgrade(system: System, dry_run: bool, *, runtime_token_file: Path | None = None,
            run_approved: bool = False, native_timeout: int | None = None, site_before: str | None = None) -> int:
    context = steps.committed_context(AREA, system, runtime_token_file)
    if native_timeout is not None:
        context = dataclasses.replace(context, native_timeout=native_timeout)
    if not dry_run:
        found = changeover.resolve(AREA, system, "upgrade")
        if found is not None:
            # This run stopped there and installed nothing, even when it
            # finished an earlier transaction's commit: never success.
            return changeover.REFUSED if found == changeover.COMMITTED else found
    try:
        installed = manifest.installed(system, context.installation.root)
    except manifest.ManifestError as exc:
        print(f"thermite upgrade: {exc}. {manifest.missing_recovery(context)}", file=sys.stderr)
        return 2
    bridged = installed is None and bridge.needed(system)
    if installed is None and not bridged:
        if manifest.thermite_present(context, system):
            print(f"thermite upgrade: {manifest.missing_recovery(context)}", file=sys.stderr)
        else:
            print("thermite upgrade: Thermite is not installed on this host yet: thermite install", file=sys.stderr)
        return 2
    system.say(f"{'What upgrading' if dry_run else 'Upgrading'} thermite on this host, {context.hostname}, to what "
               f"{context.head[:12]} pins, {'would do' if dry_run else 'step by step'}:")
    code = runner.run([steps.Preconditions(context), steps.Committed(context)], system, AREA,
                      dry_run=dry_run, rerun=RERUN)
    if dry_run:
        return changeover.preview(context, system, changeover.install_work(context, None, kind="upgrade"),
                                  first=3) or code
    if code:
        return code
    if changeover.unchanged(context, system, installed):
        system.say(f"Nothing differs from the installed version (transaction {installed['transaction']}); "
                   "checking its health.")
        return runner.run([steps.Health(context)], system, AREA, first=3, rerun=RERUN)
    if bridged:
        system.say("This is the first upgrade with the install engine: the installed version is described from "
                   f"{ownership.MANIFEST} and its fixed paths, and set aside like any other.")
    if not run_approved and not runner.confirm_run(system, "upgrade", RERUN):
        return 1
    return changeover.run(context, system, lambda id: changeover.install_work(context, id, kind="upgrade"),
                          installed=installed, bridged=bridged, before=site_before if site_before else None,
                          first=3)


def host_phase(system: System, arguments) -> int:
    """The upgrade the application workflow approved (changeover.pinned)."""
    import oh_paths

    def body(before: str | None) -> int:
        oh_paths.require_compatible()
        return upgrade(system, arguments.dry_run, runtime_token_file=arguments.runtime_token_file,
                       run_approved=arguments._run_approved, native_timeout=arguments.native_timeout,
                       site_before=before)
    return changeover.pinned(system, AREA, arguments._site_before, arguments.dry_run, body)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("upgrade", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--runtime-token-file", type=Path,
                        help="advanced: import an existing internal runtime API token from your private file")
    parser.add_argument("--source", type=Path, help="upgrade from a clean application checkout instead of the official repository")
    parser.add_argument("--revision", help="application Git revision (default: official main)")
    parser.add_argument("--native-timeout", type=steps.native_timeout_argument, metavar="SECONDS",
                        help="one native readiness allowance shared by runtime startup, gateway commissioning "
                             "and final settlement (default 900)")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="pin a release older than the site's committed one, after restoring compatible state")
    parser.add_argument("--_host-phase", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_run-approved", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_site-before", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite upgrade: run it as your user; it asks once before applying the upgrade",
              file=sys.stderr)
        return 2
    system = System()
    try:
        if not arguments._host_phase:
            if arguments._run_approved:
                raise ValueError("run approval is only valid inside the application workflow")
            if not arguments.dry_run:
                found = changeover.resolve(AREA, system, "upgrade")
                if found is not None:
                    return changeover.REFUSED if found == changeover.COMMITTED else found
            import oh_application_update
            return oh_application_update.upgrade(
                AREA, source=arguments.source, revision=arguments.revision,
                dry_run=arguments.dry_run, approve=lambda: runner.confirm_run(system, "upgrade", RERUN),
                runtime_token_file=arguments.runtime_token_file, allow_downgrade=arguments.allow_downgrade,
                host_args=(["--native-timeout", str(arguments.native_timeout)]
                           if arguments.native_timeout is not None else []) +
                          (["--verbose"] if oh_cli.switches.verbose else []), say=system.say,
                stage=lambda title: progress.stage(system, title))
        return host_phase(system, arguments)
    except subprocess.CalledProcessError as exc:
        print(f"thermite upgrade: {changeover.escaped(AREA, 'upgrade', exc)}", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite upgrade: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return changeover.INTERRUPTED

