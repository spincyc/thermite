"""Install thermite on this host, or show what that would do.

    thermite install --config DIR [--dry-run] [--clean] [--restart-runtime]
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import runner, steps
from oh_install.system import System

DESCRIPTION = """\
Install thermite on this host, the one you run it on. Run
it as your user with --config DIR to select committed private configuration
from any working directory. A complete source checkout also works:
it is safe to run again at any time. It finds this host's name and address
and your user as it runs; instance/host.yaml holds only what is the same on
any host (the state root, ports, service UID and GID, and time zone).

Before changing this host, the installer checks that every change is committed.
Config-only sites render the committed inputs with their pinned application;
complete checkouts also require current generated files. Each later step checks
whether it is done and skips it if so:
preconditions, packages, the thermite user and group with you in it, the
state directories, the boot check, the systemd units, stopped native-state
preparation and ownership repair, the deploy of HEAD, services, private runtime access, and a
read-only check at the end. One y/n question approves this entire run,
including its sudo commands and deployment ownership handoff. Compact
status shows each stage; -v/--verbose shows details and commands. Interactive
commands keep the terminal, with the spinner paused. After a no, a failure
or Ctrl-C, run it again and it goes on from there, asking once for the new run.

--clean first removes the installed units and deletes the native state root,
including users, pairings, tokens, history, monitoring stores and backups.
It also clears this host's local runtime and monitoring credentials before reinstalling.
The deletion still requires typing the exact state path. Source/site
configuration and separately staged access identity/TLS state are retained.

--restart-runtime requests one fresh runtime start after deployment, within
the same approval and followed by the normal readiness checks. Use it when
updating shared script libraries; it deletes no runtime state.

--dry-run leaves the site and host unchanged: transient configuration planning
is cleaned up, and it reports each step and what it would run. An
agent uses only that: agents never run sudo.
"""

EXAMPLES = (
    ("thermite install --config /path/to/private-site", "install this site's configuration on the host"),
    ("thermite install --config /path/to/private-site --dry-run", "review the plan without changing services"),
    ("thermite install --dry-run", "each step, and what it would run here; runs nothing"),
    ("thermite install", "install with one approval for the whole run; safe to rerun"),
    ("thermite install --clean --dry-run", "review a complete native-state reset and fresh installation"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("install", DESCRIPTION, EXAMPLES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--clean", action="store_true",
                        help="delete native state and local API/Grafana credential copies, then reinstall; pairings are lost")
    parser.add_argument("--restart-runtime", action="store_true",
                        help="restart runtime once after deployment, then run normal readiness checks")
    parser.add_argument("--runtime-token-file", type=Path,
                        help="advanced: import an existing internal runtime API token from your private file")
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite install: run it as your user; it asks once before applying the installation", file=sys.stderr)
        return 2
    system = System()
    try:
        return install(system, arguments.dry_run, clean=arguments.clean, runtime_token_file=arguments.runtime_token_file,
                       restart_runtime=arguments.restart_runtime)
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
                   "deployment and its ownership handoff, service starts/restarts, credential setup and health checks.")
    return runner.confirm_run(system, "clean install" if clean else "install")


def install(system: System, dry_run: bool, *, clean: bool = False, runtime_token_file: Path | None = None,
            restart_runtime: bool = False) -> int:
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
    context = steps.committed_context(AREA, system, runtime_token_file)
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
        if not approve(system, context, clean):
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
    """Distinguish native runtime readiness from the separately staged frontend."""
    import oh_access_mode
    import oh_inventory

    if oh_cli.switches.verbose:
        system.say(f"Advanced runtime administration on {context.hostname}: {context.lan_url}/settings/")
    house = oh_inventory.load(context.area / "instance/devices.yaml") or {}
    if oh_access_mode.mode(house) == "native":
        system.say(f"Thermite: {context.lan_url}/ ; pairing: "
                   f"{context.lan_url}/pairing/ ; advanced tools: System.")
    else:
        system.say("Set up HTTPS and accounts before using the authenticated web and terminal remotes. "
                   "Follow docs/installation.md (Set up HTTPS and accounts); thermite access --help lists the commands.")
