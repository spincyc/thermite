"""Move this host's runtime, Apple TV helper and metrics services to what
HEAD pins, or show what that would do.

    thermite upgrade [-n | --dry-run]
"""

from __future__ import annotations

import datetime
import json
import os
import sys

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import runner, steps
from oh_install.model import Command
from oh_install.system import System

RERUN = "Run thermite upgrade again to go on from here."

DESCRIPTION = """\
Move runtime, the Apple TV helper and the metrics services (Prometheus,
node_exporter, blackbox_exporter, the runtime exporter, Grafana) on this
host to the versions versions.json pins and the units deploy/ holds, as of
HEAD. thermite install must have run here first. Run it as your user, from
the clone install ran from; it is safe to run again at any time. A host
installed before the metrics existed gets them here.

Each step first checks whether it is done and skips it if so:
preconditions (deploy/ is what thermite generate writes, and this area is
committed); what changes: each installed unit against deploy/, and the
runtime image the installed unit runs against the pinned one (when nothing
differs it says so and stops); the boot test of HEAD's conf/; when
runtime's own units change, a backup, with runtime stopped, of the state's
conf, userdata and Grafana's data into
<state>/backups/<UTC time>-openhab-<installed version>.tar.gz (without
userdata/cache and userdata/tmp, which runtime rebuilds, and without
Prometheus' samples); the metrics directories; the changed units and
systemctl daemon-reload; the services whose units changed restarted; the
deploy of HEAD, if it is not what is deployed; runtime answering; the API
token and Grafana's admin password installed for the metrics services,
which are then (re)started; Prometheus with every target up and Grafana
answering; and a read-only check (thermite health --production and
thermite status).

Before any changes, one y/n question approves the whole upgrade run,
including its sudo commands. Each command is still shown before it runs.
After a no, a failure or Ctrl-C, run it again and it goes on from there,
asking once again. --dry-run only reads: it reports each step and what it
would run. An agent uses only that: agents never run sudo.
"""

NOTES = """\
going back:
  It prints where the backup is and the commands that restore it. To go
  back to the commit deployed before, restore the backup (runtime stays
  stopped), then run thermite upgrade from that commit, which installs its
  units and starts runtime on them (thermite install, in a commit from
  before thermite upgrade).

exit status:
  0  upgraded, or nothing differs
  1  a step failed, the owner said no, or a precondition does not hold
  2  runtime is not installed here yet (thermite install), or run as root
"""

EXAMPLES = (
    ("thermite upgrade -n", "what upgrading would do here; runs nothing"),
    ("thermite upgrade", "upgrade, asking once for the whole run; safe to rerun"),
)


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def installed(system: System, context: steps.Context) -> bool:
    return (system.stat(context.layout.root) is not None
            and system.stat(deploy.INSTALLED[deploy.QUADLET]) is not None)


def deployed(system: System, context: steps.Context) -> str | None:
    try:
        return json.loads(system.read_bytes(context.layout.record) or b"{}").get("commit")
    except ValueError:
        return None


def going_back(system: System, context: steps.Context, backup: steps.Backup, before: str | None,
               when: str) -> None:
    """Where the backup is, and how to put it and the previous units back."""
    if system.stat(backup.file) is None and not before:
        return
    if system.stat(backup.file) is not None:
        system.say(f"backup   {backup.file}; to restore it, under sudo:")
        for argv in steps.restore_argvs(context.layout, backup.file, when, backup.grafana):
            system.say(f"           {Command(argv, root=True).shown()}")
    if before:
        system.say(f"back     to go back to {before[:12]}, the commit deployed before: restore the backup, then "
                   f"`git switch --detach {before[:12]}` and thermite upgrade there, which installs its units and "
                   "starts runtime on them (thermite install in a commit from before thermite upgrade)")


def upgrade(system: System, dry_run: bool, when: str | None = None) -> int:
    when = when or stamp()
    versions, host = deploy.load(AREA)
    context = steps.Context(AREA, versions, host, deploy.resolve(AREA, "HEAD"),
                            deploy.tree_digest(deploy.read_tree(AREA / "conf")), system.hostname(), system.address())
    if not installed(system, context):
        print(f"thermite upgrade: runtime is not installed on this host yet ({context.layout.root} or "
              f"{deploy.INSTALLED[deploy.QUADLET]} is missing): thermite install", file=sys.stderr)
        return 2
    before = deployed(system, context)
    system.say(f"{'What upgrading' if dry_run else 'Upgrading'} thermite on this host, {context.hostname}, to what "
               f"{context.head[:12]} pins, {'would do' if dry_run else 'step by step'}:")
    code = runner.run([steps.Committed(context)], system, AREA, dry_run=dry_run, rerun=RERUN)
    if code and not dry_run:
        return code
    plan = steps.plan(context, system)
    if plan.current:
        system.say(f"Up to date: runtime {plan.pinned_version}, the Apple TV helper and the metrics services run on "
                   "the units deploy/ holds. thermite deploy installs a newer conf/ and metrics/.")
        return code
    rest = steps.upgrade(context, plan, when)
    if not dry_run:
        system.say(f"Upgrade: {plan.describe()}. This run includes the boot test, any needed backup, "
                   "sudo commands, service restarts, deployment and health checks.")
        if not system.interactive():
            system.say(f"Stopped; this upgrade needs you at a terminal. {RERUN}")
            return 1
        if system.ask("Apply this upgrade for the whole run? [y/N] ").strip().lower() not in ("y", "yes"):
            system.say(f"Stopped; no changes were made. {RERUN}")
            return 1
    status = runner.run(rest, system, AREA, dry_run=dry_run, first=2, rerun=RERUN,
                        root_confirmed=not dry_run)
    if dry_run:
        return status or code
    backup = next(step for step in rest if isinstance(step, steps.Backup))
    if status:
        system.say("The upgrade did not finish. If runtime is not right, go back:")
        going_back(system, context, backup, before, when)
    elif system.stat(backup.file) is not None:
        going_back(system, context, backup, None, when)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("upgrade", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite upgrade: run it as your user; it asks once before applying the upgrade",
              file=sys.stderr)
        return 2
    system = System()
    try:
        return upgrade(system, arguments.dry_run)
    except deploy.DeployError as exc:
        print(f"thermite upgrade: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {RERUN}")
        return 130

