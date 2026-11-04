"""Update the application, then move this host's services to what the site
pins, or show what that would do.

    thermite upgrade --config DIR [-n | --dry-run] [--source DIR] [--revision REV]
                     [--native-timeout SECONDS] [--allow-downgrade]
"""

from __future__ import annotations

import dataclasses
import datetime
import argparse
import json
import os
import shlex
import sys
import subprocess
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import runner, steps
from oh_install import ownership
import oh_units
from oh_install.model import Command
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
then updating native runtime and helper services, monitoring, the HTTPS gateway,
policy worker, verified callbacks and identity schema. No pip, manual pin commit,
access staging or separate migration command is needed. Existing authority,
accounts, credentials and TLS certificates are preserved. Gateway and worker
are stopped before the complete identity backup and any schema migration.

The previous owner command remains selected until every readiness check passes.
An interrupted run retains its candidate, site commit, prior release and backup;
rerun the printed Resume command, which names the fetched commit. Ctrl-C lets
the running step stop its own commands first. Meanwhile the previous owner
command runs site commands with the site's pinned release, verified from its
var/application-releases. Previous releases do not undo schema changes, and
upgrade refuses to pin a release older than the site's unless --allow-downgrade
is given after deliberate recovery. --native-timeout sets the one native
readiness allowance (300-7200 seconds).

--dry-run changes nothing and does not fetch: the remote revision and its exact
host differences remain unknown until an approved run. When the site pins the
release running it, it prints that release's provisional host plan for this
host. Run as your ordinary user. Use -v for details. Agents only use --dry-run.
"""

NOTES = """\
exit status:
  0  complete and verified
  1  declined or a step failed
  2  invalid inputs, an older release without --allow-downgrade, missing
     installation, or run as root
  130  interrupted; the running step stopped its own commands first
"""

EXAMPLES = (
    ("thermite upgrade -n", "what upgrading would do here; runs nothing"),
    ("thermite upgrade", "upgrade, asking once for the whole run; safe to rerun"),
)


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def installed(system: System, context: steps.Context) -> bool:
    return (system.stat(context.layout.root) is not None
            and ownership.inspect(context, system).runtime_bound)


def deployed(system: System, context: steps.Context) -> str | None:
    try:
        record = json.loads(system.read_bytes(context.layout.record) or b"{}")
    except ValueError:
        return None
    return record.get("commit") if isinstance(record, dict) else None


def previous_release(context: steps.Context, before: str) -> tuple[str | None, list[str]]:
    """The release the site pinned at `before`, if it differs, and the commits changing it since."""
    from oh_paths import SITE_FILE
    try:
        blob = deploy.export(context.area, before, (SITE_FILE,)).get(SITE_FILE)
        previous = json.loads(blob.data)["release"] if blob is not None else None
        if previous is None or previous == (context.application or {}).get("digest"):
            return None, []
        pins = subprocess.run(["git", "-C", str(context.area), "log", "--format=%H", f"{before}..HEAD", "--",
                               f":(literal){SITE_FILE}"], capture_output=True, text=True, check=True).stdout.split()
    except (deploy.DeployError, ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError):
        return None, []
    return previous, pins


def going_back(system: System, context: steps.Context, backup: steps.Backup, before: str | None,
               when: str) -> None:
    """Where the backup is, and how to put it, the previous release and its units back.

    Upgrade always fetches the latest application, so going back means
    returning the site to the previous release requirement and installing that
    exact cached release, which also installs its units.
    """
    complete = backup.completed(system, backup.file)
    if not complete and not before:
        return
    if complete:
        system.say(f"backup   {backup.file}; to restore it, under sudo:")
        for argv in steps.restore_argvs(context.layout, backup.file, when, backup.grafana):
            system.say(f"           {Command(argv, root=True).shown()}")
    if before:
        site = shlex.quote(str(context.area))
        restore = "restore the backup, then " if complete else ""
        previous, pins = previous_release(context, before)
        if previous is None:
            system.say(f"back     to go back to {before[:12]}, the site commit deployed before: {restore}revert the "
                       f"site commits since (`git -C {site} revert --no-edit {before[:12]}..HEAD`), then "
                       f"`thermite install --config {site}`; see docs/installation.md#update-or-remove")
            return
        system.say(f"back     to go back to {before[:12]}, the site commit deployed before, and its application "
                   f"release {previous[:12]}: {restore}return the site to that release and install its cached copy:")
        if len(pins) == 1:
            system.say(f"           git -C {site} revert --no-edit {pins[0][:12]}")
        else:
            system.say(f"           git -C {site} restore --source {before[:12]} -- thermite-site.json && "
                       f"git -C {site} commit -m 'Return to Thermite release {previous[:12]}' -- thermite-site.json")
        launcher = context.var / "application-releases" / previous / "bin/thermite"
        system.say(f"           python3 {shlex.quote(str(launcher))} install --config {site}")
        system.say("         upgrade fetches the latest application, so it cannot go back; that install also "
                   "installs the release's units (docs/installation.md#update-or-remove)")


def upgrade(system: System, dry_run: bool, when: str | None = None, *, runtime_token_file: Path | None = None,
            run_approved: bool = False, native_timeout: int | None = None) -> int:
    when = when or stamp()
    context = steps.committed_context(AREA, system, runtime_token_file)
    if native_timeout is not None:
        context = dataclasses.replace(context, native_timeout=native_timeout)
    problems = ownership.inspect(context, system).problems
    if problems:
        system.say("Cannot safely upgrade: " + "; ".join(problems))
        return 1
    if not installed(system, context):
        print(f"thermite upgrade: runtime is not installed on this host yet ({context.layout.root} or "
              f"{deploy.INSTALLED[deploy.QUADLET]} is missing): thermite install", file=sys.stderr)
        return 2
    before = deployed(system, context)
    system.say(f"{'What upgrading' if dry_run else 'Upgrading'} thermite on this host, {context.hostname}, to what "
               f"{context.head[:12]} pins, {'would do' if dry_run else 'step by step'}:")
    code = runner.run([steps.Preconditions(context), steps.Committed(context)], system, AREA,
                      dry_run=dry_run, rerun=RERUN)
    if code and not dry_run:
        return code
    plan = steps.plan(context, system)
    rest = steps.upgrade(context, plan, when)
    for step in rest:
        if isinstance(step, steps.Deploy):
            # The history records an upgrade, with the state backup it took.
            step.how = "upgrade"
            step.backup = next((found for found in rest if isinstance(found, steps.Backup)), None)
    if not dry_run:
        system.say(f"Upgrade: {plan.describe()}. This run includes the boot test, any needed backup, "
                   "sudo commands, service restarts, deployment and health checks.")
        if not run_approved and not runner.confirm_run(system, "upgrade", RERUN):
            return 1
    status = runner.run(rest, system, AREA, dry_run=dry_run, first=3, rerun=RERUN,
                        root_confirmed=not dry_run)
    if dry_run:
        return status or code
    backup = next(step for step in rest if isinstance(step, steps.Backup))
    if status:
        system.say("The upgrade did not finish. If runtime is not right, go back:")
        going_back(system, context, backup, before, when)
    elif backup.completed(system, backup.file):
        going_back(system, context, backup, None, when)
    return status


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
            import oh_application_update
            return oh_application_update.upgrade(
                AREA, source=arguments.source, revision=arguments.revision,
                dry_run=arguments.dry_run, approve=lambda: runner.confirm_run(system, "upgrade", RERUN),
                runtime_token_file=arguments.runtime_token_file, allow_downgrade=arguments.allow_downgrade,
                host_args=(["--native-timeout", str(arguments.native_timeout)]
                           if arguments.native_timeout is not None else []) +
                          (["--verbose"] if oh_cli.switches.verbose else []), say=system.say)
        import oh_paths
        oh_paths.require_compatible()
        return upgrade(system, arguments.dry_run, runtime_token_file=arguments.runtime_token_file,
                       run_approved=arguments._run_approved, native_timeout=arguments.native_timeout)
    except subprocess.CalledProcessError as exc:
        print("thermite upgrade: an upgrade step failed; rerun the same command to resume", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite upgrade: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return 130

