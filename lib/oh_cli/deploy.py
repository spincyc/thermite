"""Install a commit's runtime conf and Apple TV helper on this host.

    thermite deploy [REV] [-n | --dry-run] [--force] [--state DIR]
"""

from __future__ import annotations

import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA

VAR = AREA / "var"

DESCRIPTION = """\
Production runtime runs on the host thermite install ran on, from its state
directory (the host file's state_root). Run this there. It installs one
commit into the state, as you, without root:

  - the commit's conf/ into <state>/conf, which runtime reloads by itself
    (no restart);
  - the Apple TV helper's files (bin/thermite and the code and pins
    its apple-tv verb runs) into <state>/atv/app; when they change it writes
    <state>/deploy/atv-restart, and openhab-atv-restart.path restarts
    openhab-atv.service;
  - the commit's metrics/ into <state>/metrics and the metrics exporter's
    code into <state>/exporter/app, once thermite install or upgrade has
    made them. Prometheus and blackbox_exporter reload a changed
    configuration (POST /-/reload; one not running yet reads it when it
    starts), Grafana rereads its dashboards by itself, and a change to the
    exporter's code or Grafana's settings writes <state>/deploy/metrics-restart,
    and openhab-metrics-restart.path restarts those two. A commit from
    before the metrics (no metrics/ or no exporter) leaves both as the last
    deploy installed them;
  - a record in <state>/deploy/deployed.json and a line in
    <state>/deploy/history (thermite history lists them).

It installs the commit as Git has it, not the working tree. HEAD needs a
clean working tree, and the commit's conf/ must have passed thermite test
boot on this host. It writes only what differs, deletes only files
an earlier deploy installed, and leaves alone anything else in the state.

You need to be in the openhab group. Changing the units themselves (an
runtime upgrade, a new image) needs root, so thermite upgrade does it:
thermite status says when an installed unit differs from deploy/. To put
back the commit deployed before, use thermite rollback.
"""

EXAMPLES = (
    ("thermite deploy", "install HEAD, once thermite test boot has passed on it"),
    ("thermite deploy --dry-run", "what that would change; writes nothing"),
    ("thermite deploy 3d22e36", "install an older commit; thermite rollback does it for one deployed here"),
)


def print_changes(label: str, changes: deploy.Changes) -> None:
    print(f"{label:<8}{changes.summary()}")
    for kind in ("added", "changed", "removed"):
        for path in getattr(changes, kind):
            print(f"          {kind:<8}{path}")


def print_metrics(result: deploy.Deployed, layout: deploy.State) -> None:
    """The metrics/ and exporter changes, and how the running services took them."""
    if not result.with_metrics:
        print(f"metrics not installed yet ({layout.metrics} is missing): thermite upgrade installs the metrics "
              "services, then a deploy fills it")
        return
    if result.metrics_kept:
        print(f"metrics kept as deployed: {result.commit[:12]} predates the installed metrics/notification stack, so "
              f"{layout.metrics} and {layout.exporter_app} keep what the last deploy installed")
        return
    print_changes("metrics", result.metrics)
    print_changes("exporter", result.exporter_app)
    for line in result.reloads:
        print(f"metrics {line}")


def apply(args, host: dict) -> int:
    layout = deploy.State(args.state) if args.state else deploy.state(host)
    result = deploy.deploy(AREA, layout, VAR, args.rev, force=args.force, dry_run=args.dry_run, host=host)
    short = result.commit[:12]
    print(f"{'dry run ' if args.dry_run else 'deploy  '}{short} {result.subject}")
    print_changes("conf", result.conf)
    print_changes("helper", result.atv_app)
    print_metrics(result, layout)
    if args.dry_run:
        return 0
    if result.conf:
        print("runtime reloads the changed files by itself; follow it with "
              f"bin/thermite sandbox logs -f --userdata {layout.userdata}")
    if result.atv_app:
        print(f"helper  changed: openhab-atv-restart.path restarts openhab-atv.service ({layout.atv_restart})")
    if not result.record["boot_checked"]:
        print("note    installed without passing thermite test boot (--force)")
    if result.legacy_atv:
        print(f"note    kept {deploy.LEGACY_ATV_PROGRAM} in the helper: the installed quadlet still runs it "
              "until the owner installs the new one (make openhab-upgrade)")
    if not deploy.pushed(AREA, result.commit):
        print(f"note    {short} is on no remote branch yet; push it so GitHub has what runs")
    stale = [name for name, _, how in deploy.installed_units(AREA) if how == "differs"]
    if stale:
        print(f"note    the installed {', '.join(stale)} differ from this checkout's; the owner installs them "
              "(thermite status)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("deploy", DESCRIPTION, EXAMPLES)
    parser.add_argument("rev", nargs="?", default="HEAD", metavar="REV", help="the commit (default: HEAD)")
    oh_cli.add_dry_run(parser)
    parser.add_argument("--force", action="store_true",
                        help="install a conf thermite test boot has not passed on here")
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the state root (default: instance/host.yaml state_root)")
    args = parser.parse_args(argv)
    _, host = deploy.load(AREA)
    try:
        return apply(args, host)
    except deploy.DeployError as exc:
        print(f"thermite deploy: {exc}", file=sys.stderr)
        return 2
