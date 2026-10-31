"""Deploy the commit deployed before the current one, or a given one.

    thermite rollback [COMMIT] [-n | --dry-run] [--force] [--state DIR]
"""

from __future__ import annotations

import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_cli.deploy import VAR, print_changes, print_metrics

DESCRIPTION = """\
Put back an earlier commit's conf/ and Apple TV helper, as helm rollback
does: without COMMIT, the commit deployed before the one deployed now
(thermite history lists them); with it, that commit. It deploys as
thermite deploy does, as you, through the openhab group, and records a
rollback in the history.

A commit this host's history holds ran here before, so it needs no fresh
boot test. Any other commit needs thermite test boot to have passed on its
conf/ here, or --force.

A deploy never changes the systemd units. When the commit's deploy/ holds
other units than the installed ones (an older runtime pin, or a helper
unit from before the apple-tv verb, which runs `atv serve`), it warns and
names what the owner installs for that commit to run as it did.
"""

EXAMPLES = (
    ("thermite rollback -n", "what rolling back to the previous deploy would change"),
    ("thermite rollback", "deploy the commit deployed before the current one"),
    ("thermite rollback 3d22e36", "deploy that commit (thermite history lists them)"),
)

NOTES = """\
exit status:
  0  rolled back (or, with --dry-run, it could)
  2  it could not: nothing to roll back to, an unknown commit, no boot test
     on a commit that never ran here (without --force), no write access
"""


def target(entries: list[dict], record: dict, commit: str | None) -> tuple[str, dict | None]:
    """(the commit to deploy, its latest entry in the history if it has one)."""
    if commit is None:
        current = record.get("commit")
        if not current:
            raise deploy.DeployError("nothing is deployed here yet, so there is nothing to roll back to "
                                     "(thermite deploy)")
        end = next((i for i, entry in enumerate(entries) if entry["current"]), len(entries))
        for entry in reversed(entries[:end]):
            if entry["commit"] != current:
                return entry["commit"], entry
        raise deploy.DeployError(f"the history holds no deploy before {current[:12]}'s: name the COMMIT")
    full = deploy.resolve(AREA, commit)
    return full, next((entry for entry in reversed(entries) if entry["commit"] == full), None)


def warn_units(commit: str, installed: dict[str, Path] | None = None) -> list[str]:
    differ = deploy.unit_differences(AREA, commit, installed)
    if not differ:
        return []
    names = ", ".join(Path(name).name for name in differ)
    return [f"warning  the installed {names} {'is' if len(differ) == 1 else 'are'} not {commit[:12]}'s, and a "
            "deploy changes no unit.",
            f"         For {commit[:12]} to run as it did, the owner installs its units: "
            f"`git switch --detach {commit[:12]}`,",
            "         then thermite upgrade there (thermite install in a commit from before it; "
            "docs/deploy.md, Rolling back)."]


def roll_back(args, layout: deploy.State, installed: dict[str, Path] | None = None, host: dict | None = None) -> int:
    record = deploy.read_record(layout)
    entries = deploy.deploys(layout)
    commit, entry = target(entries, record, args.commit)
    ran_here = entry is not None or commit == record.get("commit")
    current = record.get("commit")
    print(f"{'would roll back' if args.dry_run else 'rollback'} {current[:12] if current else 'nothing'} -> "
          f"{commit[:12]}" + (f", deploy #{entry['number']} of {entry['deployed_at']}" if entry else ""))
    if ran_here:
        print("note    it ran here before, so it needs no fresh thermite test boot")
    for line in warn_units(commit, installed):
        print(line)
    result = deploy.deploy(AREA, layout, VAR, commit, force=args.force or ran_here, dry_run=args.dry_run,
                           how="rollback", host=host)
    print(f"{'dry run ' if args.dry_run else 'deploy  '}{result.commit[:12]} {result.subject}")
    print_changes("conf", result.conf)
    print_changes("helper", result.atv_app)
    print_metrics(result, layout)
    if not args.dry_run and result.atv_app:
        print(f"helper  changed: openhab-atv-restart.path restarts openhab-atv.service ({layout.atv_restart})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("rollback", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("commit", nargs="?", metavar="COMMIT",
                        help="the commit to deploy (default: the one deployed before the current one)")
    oh_cli.add_dry_run(parser)
    parser.add_argument("--force", action="store_true",
                        help="deploy a commit that never ran here without a passed thermite test boot")
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the state root (default: instance/host.yaml state_root)")
    args = parser.parse_args(argv)
    host = deploy.load(AREA)[1]
    layout = deploy.State(args.state) if args.state else deploy.state(host)
    try:
        return roll_back(args, layout, host=host)
    except deploy.DeployError as exc:
        print(f"thermite rollback: {exc}", file=sys.stderr)
        return 2
