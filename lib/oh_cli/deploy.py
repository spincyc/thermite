"""Install a commit's runtime conf and Apple TV helper on this host.

    thermite deploy [REV] [-n | --dry-run] [--force] [--state DIR]
"""

from __future__ import annotations

import sys
import argparse
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA

VAR = AREA / "var"

DESCRIPTION = """\
Compatibility alias for thermite install. Deployments apply the complete
application, native services and HTTPS gateway workflow. Use --config DIR
to select committed private configuration. Use thermite upgrade for a newer
application release. The old partial-conf deployment switches are reserved
for the installer's internal phase and are not a separate deployment path.
"""

EXAMPLES = (
    ("thermite install --config /path/to/private-site", "deploy everything with one approval"),
    ("thermite upgrade --config /path/to/private-site", "update the application and every service"),
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


def deployment_selection(rev: str, *, area: Path | None = None) -> tuple[str, dict, dict | None]:
    """Resolve once, so host facts and derived output share one revision."""
    from oh_paths import SITE_FILE
    area = AREA if area is None else Path(area)
    commit = deploy.resolve(area, rev)
    if deploy.export(area, commit, (SITE_FILE,)):
        _, host, requirement = deploy.committed_inputs(area, commit)
        if requirement and requirement["version"] == 2:
            return commit, host, requirement
    return commit, deploy.load(area)[1], None


def deployment_host(rev: str, *, area: Path | None = None) -> dict:
    """A config-only revision chooses its destination from committed data."""
    return deployment_selection(rev, area=area)[1]


def apply(args, host: dict) -> int:
    layout = deploy.State(args.state) if args.state else deploy.state(host)
    result = deploy.deploy(AREA, layout, VAR, args.rev, force=args.force, dry_run=args.dry_run, host=host,
                           ownership_approved=getattr(args, "yes", False))
    short = result.commit[:12]
    print(f"{'dry run ' if args.dry_run else 'deploy  '}{short} {result.subject}")
    print_changes("conf", result.conf)
    print_changes("helper", result.atv_app)
    print_metrics(result, layout)
    if args.dry_run:
        return 0
    if result.conf:
        print("runtime reloads regular configuration; shared script-library updates need a controlled runtime restart. Follow it with "
              f"bin/thermite sandbox logs -f --userdata {layout.userdata}")
    if result.atv_app:
        print(f"helper  changed: thermite-atv-restart.path restarts thermite-atv.service ({layout.atv_restart})")
    if not result.record["boot_checked"]:
        print("note    installed without passing thermite test boot (--force)")
    if not deploy.pushed(AREA, result.commit):
        print(f"note    {short} is not on a remote branch; back up this configuration in your private repository")
    stale = [name for name, _, how in deploy.installed_units(AREA, rev=result.commit) if how == "differs"]
    if stale:
        print(f"note    the installed {', '.join(stale)} differ from this checkout's; the owner installs them "
              "(thermite status)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("deploy", DESCRIPTION, EXAMPLES)
    parser.add_argument("rev", nargs="?", default="HEAD", metavar="REV", help="the commit (default: HEAD)")
    oh_cli.add_dry_run(parser)
    parser.add_argument("--yes", action="store_true",
                        help="approve the ownership handoff for this invocation; install/upgrade use their run approval")
    parser.add_argument("--force", action="store_true",
                        help="install a conf thermite test boot has not passed on here")
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the runtime state (default: runtime/ in instance/host.yaml's root, /srv/thermite)")
    parser.add_argument("--_configuration-phase", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not args._configuration_phase:
            if args.rev != "HEAD" or args.force or args.state or args.yes:
                raise deploy.DeployError("deployment now uses the complete install workflow; select a committed private site "
                                         "with --config DIR and run thermite install (or thermite upgrade for a new application)")
            from oh_cli.install import main as install
            return install(["--dry-run"] if args.dry_run else [])
        import oh_paths
        oh_paths.require_compatible()
        args.rev, host, _ = deployment_selection(args.rev)
        return apply(args, host)
    except deploy.DeployError as exc:
        print(f"thermite deploy: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nthermite deploy: interrupted. Run the same thermite deploy command again to continue.", file=sys.stderr)
        return 130
