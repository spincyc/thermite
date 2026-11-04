"""Move this host's runtime, Apple TV helper and metrics services to what
HEAD pins, or show what that would do.

    thermite upgrade [-n | --dry-run]
"""

from __future__ import annotations

import datetime
import argparse
import json
import os
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
rerun the same command to resume. Previous releases do not automatically undo
schema changes. --dry-run changes nothing and does not fetch: the remote
revision and exact host differences remain unknown until an approved run.
Run as your ordinary user. Use -v for details. Agents only use --dry-run.
"""

NOTES = """\
exit status:
  0  complete and verified
  1  declined or a step failed
  2  invalid inputs, missing installation, or run as root
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
        return json.loads(system.read_bytes(context.layout.record) or b"{}").get("commit")
    except ValueError:
        return None


def going_back(system: System, context: steps.Context, backup: steps.Backup, before: str | None,
               when: str) -> None:
    """Where the backup is, and how to put it and the previous units back."""
    complete = backup.completed(system, backup.file)
    if not complete and not before:
        return
    if complete:
        system.say(f"backup   {backup.file}; to restore it, under sudo:")
        for argv in steps.restore_argvs(context.layout, backup.file, when, backup.grafana):
            system.say(f"           {Command(argv, root=True).shown()}")
    if before:
        restore = "restore the backup, then " if complete else ""
        system.say(f"back     to go back to {before[:12]}, the commit deployed before: {restore}"
                   f"`git switch --detach {before[:12]}` and thermite upgrade there, which installs its units and "
                   "starts runtime on them (thermite install in a commit from before thermite upgrade)")


def upgrade(system: System, dry_run: bool, when: str | None = None, *, runtime_token_file: Path | None = None,
            run_approved: bool = False) -> int:
    when = when or stamp()
    context = steps.committed_context(AREA, system, runtime_token_file)
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
                runtime_token_file=arguments.runtime_token_file,
                host_args=["--verbose"] if oh_cli.switches.verbose else [], say=system.say)
        import oh_paths
        oh_paths.require_compatible()
        return upgrade(system, arguments.dry_run, runtime_token_file=arguments.runtime_token_file,
                       run_approved=arguments._run_approved)
    except subprocess.CalledProcessError as exc:
        print("thermite upgrade: an upgrade step failed; rerun the same command to resume", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite upgrade: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return 130

