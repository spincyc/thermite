"""Install thermite on this host, or show what that would do.

    thermite install [--dry-run]
"""

from __future__ import annotations

import importlib
import os
import sys

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import runner, steps
from oh_install.system import System

DESCRIPTION = """\
Install thermite on this host, the one you run it on. Run
it as your user, from a clone of this repository, wherever that clone is:
it is safe to run again at any time. It finds this host's name and address
and your user as it runs; instance/host.yaml holds only what is the same on
any host (the state root, the ports, the openhab ids, the time zone).

Each step first checks whether it is done and skips it if so:
preconditions, packages, the openhab user and group with you in it, the
state directory and its directories, the boot check, the deploy of HEAD,
the systemd units, the services, runtime answering, the API token, and a
read-only check at the end. A command that needs root is shown before it
runs, under sudo, and runs only if you answer y. After a no, a failure or
Ctrl-C, run it again and it goes on from there.

--dry-run only reads: it reports each step and what it would run. An
agent uses only that: agents never run sudo.
"""

EXAMPLES = (
    ("thermite install --dry-run", "each step, and what it would run here; runs nothing"),
    ("thermite install", "install, asking before each sudo command; safe to rerun"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("install", DESCRIPTION, EXAMPLES)
    oh_cli.add_dry_run(parser)
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite install: run it as your user; it asks before each command it runs with sudo", file=sys.stderr)
        return 2
    system = System()
    try:
        return install(system, arguments.dry_run)
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {runner.RERUN}")
        return 130


def install(system: System, dry_run: bool) -> int:
    try:
        import yaml  # noqa: F401
    except ImportError:
        system.say("PyYAML is missing, and instance/host.yaml needs it: the packages come first.")
        code = runner.run([steps.Packages()], system, AREA, dry_run=dry_run)
        if code or dry_run:
            return code or 1
        importlib.invalidate_caches()
    versions, host = deploy.load(AREA)
    context = steps.Context(AREA, versions, host, deploy.resolve(AREA, "HEAD"),
                            deploy.tree_digest(deploy.read_tree(AREA / "conf")), system.hostname(), system.address())
    where = f"{context.hostname} ({context.address})" if context.address else context.hostname
    system.say(f"{'What installing' if dry_run else 'Installing'} thermite (runtime {versions['openhab']['version']}) "
               f"on this host, {where}, as {system.login()}, at {context.head[:12]}, "
               f"{'would do' if dry_run else 'step by step'}:")
    *install, verify = steps.install(context)
    code = runner.run(install, system, AREA, dry_run=dry_run)
    if code and not dry_run:
        return code
    if not dry_run:
        system.say(f"thermite runs on {context.hostname}: Main UI at {context.lan_url}/ and the remote at "
                   f"{context.lan_url}/static/remote/. Pair the TVs, SHIELDs and Apple TVs at "
                   f"{context.lan_url}/static/remote/pair.html (docs/deploy.md).")
        if host["gid"] not in system.process_groups():
            system.say("Log out and in again, so that your shell is in the openhab group for later deploys.")
    return runner.run([verify], system, AREA, dry_run=dry_run, first=len(install) + 1) or code
