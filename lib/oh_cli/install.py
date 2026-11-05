"""Install thermite on this host, or show what that would do.

    thermite install --config DIR [--dry-run] [--clean] [--restart-runtime]
                     [--native-timeout SECONDS] [--allow-downgrade]
"""

from __future__ import annotations

import dataclasses
import argparse
import os
import sys
import subprocess
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import runner, steps
from oh_install.system import System

DESCRIPTION = """\
Deploy the complete Thermite installation using committed private configuration.
Run as your ordinary owner with --config DIR. The current application release
is prepared in a private environment; --source DIR and --revision REV select
a clean application checkout instead. The application validates the site and
commits its release requirement before applying the host workflow.

One approval covers application setup, packages, the service account, native
runtime and helpers, monitoring, HTTPS gateway, policy worker, verified device
callbacks, private-state backups, schema migrations and readiness checks.
The native ports are restricted to this host. An existing gateway keeps its
identity, accounts, certificates and callback routes; a fresh gateway creates
its TLS certificate and starts with Security off. Enable Security on its page
to bootstrap the first administrator with owner proof.

The command in ~/.local/bin is switched only after every check passes. Keep
that directory first on PATH; a successful run warns when it is not. Rerun
after a failure to resume; prior environments, releases and backups are
retained. Application setup never changes OS Python. Install refuses to pin a
release older than the site's committed one unless --allow-downgrade is given,
after deliberately restoring state that the newer release may have migrated.

--clean deletes the native state and local runtime/monitoring credentials;
it requires typing the exact state path. Gateway identity/TLS state is retained.
--restart-runtime requests one fresh native start and its readiness checks.
--native-timeout sets the one native readiness allowance (300-7200 seconds).
--dry-run fetches, commits and changes nothing. When the site pins the release
running it, it also prints that release's provisional host plan for this host;
otherwise the host plan is resolved after approval. Use -v for details. Agents
only use --dry-run; they never run sudo.
"""

NOTES = """\
exit status:
  0  complete and verified
  1  declined or a step failed
  2  invalid inputs, an older release without --allow-downgrade, or run as root
  130  interrupted; the running step stopped its own commands first
"""

EXAMPLES = (
    ("thermite install --config /path/to/private-site", "install this site's configuration on the host"),
    ("thermite install --config /path/to/private-site --dry-run", "review the plan without changing services"),
    ("thermite install --dry-run", "each step, and what it would run here; runs nothing"),
    ("thermite install", "install with one approval for the whole run; safe to rerun"),
    ("thermite install --clean --dry-run", "review a complete native-state reset and fresh installation"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("install", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--clean", action="store_true",
                        help="delete native state and local API/Grafana credential copies, then reinstall; pairings are lost")
    parser.add_argument("--restart-runtime", action="store_true",
                        help="restart runtime once after deployment, then run normal readiness checks")
    parser.add_argument("--runtime-token-file", type=Path,
                        help="advanced: import an existing internal runtime API token from your private file")
    parser.add_argument("--source", type=Path, help="use a clean application checkout for this deployment")
    parser.add_argument("--revision", help="application Git revision to deploy from --source")
    parser.add_argument("--native-timeout", type=steps.native_timeout_argument, metavar="SECONDS",
                        help="one native readiness allowance shared by runtime startup, gateway commissioning "
                             "and final settlement (default 900)")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="pin a release older than the site's committed one, after restoring compatible state")
    parser.add_argument("--_host-phase", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_run-approved", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite install: run it as your user; it asks once before applying the installation", file=sys.stderr)
        return 2
    system = System()
    try:
        if not arguments._host_phase:
            if arguments._run_approved:
                raise ValueError("run approval is only valid inside the application workflow")
            import oh_application_update
            return oh_application_update.install(
                AREA, source=arguments.source, revision=arguments.revision,
                dry_run=arguments.dry_run, approve=lambda: runner.confirm_run(system, "install"),
                runtime_token_file=arguments.runtime_token_file, allow_downgrade=arguments.allow_downgrade,
                host_args=(["--clean"] if arguments.clean else []) +
                          (["--restart-runtime"] if arguments.restart_runtime else []) +
                          (["--native-timeout", str(arguments.native_timeout)]
                           if arguments.native_timeout is not None else []) +
                          (["--verbose"] if oh_cli.switches.verbose else []), say=system.say)
        import oh_paths
        oh_paths.require_compatible()
        return install(system, arguments.dry_run, clean=arguments.clean, runtime_token_file=arguments.runtime_token_file,
                       restart_runtime=arguments.restart_runtime, run_approved=arguments._run_approved,
                       native_timeout=arguments.native_timeout)
    except subprocess.CalledProcessError as exc:
        print("thermite install: a deployment step failed; rerun the same command to resume", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite install: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {runner.RERUN}")
        return 130


def approve(system: System, context: steps.Context, clean: bool) -> bool:
    if context.restart_runtime:
        system.say("Runtime will restart once after deployment, then pass the normal readiness checks.")
    if clean:
        if oh_cli.switches.verbose:
            system.say(f"This clean install DELETES {context.layout.root}: accounts, pairings, credentials, history, "
                       "monitoring data, backups and manually installed binding repairs. It also clears "
                       f"{context.token_file}, {context.grafana_password_file} and {context.runtime_credentials_file}. Source configuration and "
                       "separately staged access identity/TLS state are retained.")
        else:
            system.say(f"Deletes all runtime data in {context.layout.root} and local credentials.")
    if oh_cli.switches.verbose:
        system.say("This install run includes packages, service-account setup, permissions, boot checks, systemd units, "
                   "deployment and its ownership handoff, stopping all of Thermite when anything it runs on changes and "
                   "starting it again, credential setup and health checks.")
    return runner.confirm_run(system, "clean install" if clean else "install")


def refuse_open(system: System, dry_run: bool) -> bool:
    """An upgrade's install transaction that has not finished comes first."""
    from oh_install import changeover
    pending = changeover.open_transaction(AREA / "var")
    if pending and not dry_run:
        system.say(f"An upgrade's install transaction, {pending}, has not finished: run "
                   f"{changeover.upgrade_command(AREA)} first; it commits or rolls it back.")
    return bool(pending) and not dry_run


def install(system: System, dry_run: bool, *, clean: bool = False, runtime_token_file: Path | None = None,
            restart_runtime: bool = False, run_approved: bool = False, native_timeout: int | None = None) -> int:
    if clean and runtime_token_file is not None:
        system.say("A clean installation creates new runtime credentials; an existing token cannot be imported into it.")
        return 2
    try:
        import yaml  # noqa: F401
    except ImportError:
        system.say("PyYAML is missing from this Python environment. Install the application dependencies, "
                   "then rerun: the site must be validated before any host changes.")
        if dry_run:
            runner.run([steps.Packages()], system, AREA, dry_run=True)
        return 2
    if refuse_open(system, dry_run):
        return 2
    context = steps.committed_context(AREA, system, runtime_token_file)
    if native_timeout is not None:
        context = dataclasses.replace(context, native_timeout=native_timeout)
    if restart_runtime:
        context = dataclasses.replace(context, restart_runtime=True)
    versions, host = context.versions, context.host
    where = f"{context.hostname} ({context.address})" if context.address else context.hostname
    system.say(f"{'What installing' if dry_run else 'Installing'} thermite (runtime {versions['openhab']['version']}) "
               f"on this host, {where}, as {system.login()}, at {context.head[:12]}, "
               f"{'would do' if dry_run else 'step by step'}:")
    preflight = steps.install_preflight(context)
    code = runner.run(preflight, system, AREA, dry_run=dry_run)
    if code and not dry_run:
        return code
    approved = False
    if not dry_run:
        if not run_approved and not approve(system, context, clean):
            return 1
        approved = True
    reset = steps.clean_install(context) if clean else []
    reset_code = runner.run(reset, system, AREA, dry_run=dry_run, first=len(preflight) + 1,
                            root_confirmed=approved)
    if reset_code and not dry_run:
        return reset_code
    if clean and dry_run:
        system.say("The remaining checks describe the current host. The actual run rechecks every step after "
                   "deletion, recreates missing state, and provisions private runtime access automatically.")
    *install, verify = steps.install(context, preflight=False)
    first = len(preflight) + len(reset) + 1
    body_code = runner.run(install, system, AREA, dry_run=dry_run, first=first, root_confirmed=approved)
    code = code or reset_code or body_code
    if code and not dry_run:
        return code
    if not dry_run:
        completion(context, system)
        if host["gid"] not in system.process_groups():
            system.say("Log out and in again, so that your shell is in the thermite group for later deploys.")
    return runner.run([verify], system, AREA, dry_run=dry_run, first=first + len(install),
                      root_confirmed=approved) or code


def completion(context: steps.Context, system: System) -> None:
    system.say("Thermite's HTTPS gateway is installed. Use the verified gateway URL and trust certificate "
               "printed above. Security can be enabled from its Security page; ordinary controls work while it is off.")
