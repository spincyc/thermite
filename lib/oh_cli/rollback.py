"""Put back the configuration of the site commit deployed before, or a given one.

    thermite rollback [COMMIT] [-n | --dry-run] [--force] [--state DIR]
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_cli.deploy import VAR, deployment_selection, print_changes, print_metrics

DESCRIPTION = """\
Put back an earlier site commit's runtime configuration (conf/), Apple TV
helper files and monitoring configuration (metrics/), rendered with that
commit's exact cached application release: without COMMIT, the commit
deployed before the one deployed now (thermite history lists them); with it,
that commit. It deploys as you, through the thermite group, with an explicit
ownership handoff to thermite:thermite, and records a rollback in the history.

Only those files change. Rollback does not select another application for the
owner command, change systemd units or native versions, or restore runtime
data, the HTTPS gateway or identity state; docs/installation.md#update-or-remove
and docs/packaging.md#update-and-roll-back describe that recovery.

A commit this host's history holds ran here before, so it needs no fresh boot
test, provided its configuration renders to the exact digest recorded then;
the deploy checks that digest again before writing. Any other commit needs
thermite test boot to have passed on its conf/ here, or --force.

When the commit's deploy/ holds other units than the installed ones (an older
runtime pin, for example), it warns and names how the owner installs them.
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
    site = shlex.quote(str(AREA))
    return [f"warning  the installed {names} {'is' if len(differ) == 1 else 'are'} not {commit[:12]}'s, and a "
            "rollback changes no unit.",
            f"         For {commit[:12]} to run as it did, return the site to its inputs in a new commit "
            f"(`git -C {site} revert --no-edit {commit[:12]}..HEAD`),",
            f"         then `thermite install --config {site}`, with that commit's release if it pins another "
            "(docs/installation.md#update-or-remove)."]


def roll_back(args, layout: deploy.State, installed: dict[str, Path] | None = None, host: dict | None = None) -> int:
    record = deploy.read_record(layout)
    entries = deploy.deploys(layout)
    commit, entry = target(entries, record, args.commit)
    previous = entry if entry is not None else record if commit == record.get("commit") else None
    ran_here = previous is not None
    # The boot-test waiver holds only for the exact configuration recorded
    # then: deploy.deploy refuses to write when the final digest differs.
    expected_digest = None
    from oh_paths import SITE_FILE
    if ran_here and deploy.export(AREA, commit, (SITE_FILE,)):
        _, _, requirement = deploy.committed_inputs(AREA, commit)
        if requirement and requirement["version"] == 2:
            files, _ = deploy.prepared_files(AREA, commit, VAR)
            conf = {name.removeprefix("conf/"): blob for name, blob in files.items() if name.startswith("conf/")}
            # A commit-only receipt cannot prove that this derived output ran.
            ran_here = previous.get("conf_digest") == deploy.tree_digest(conf)
            if ran_here and not args.force:
                expected_digest = previous["conf_digest"]
    current = record.get("commit")
    print(f"{'would roll back' if args.dry_run else 'rollback'} {current[:12] if current else 'nothing'} -> "
          f"{commit[:12]}" + (f", deploy #{entry['number']} of {entry['deployed_at']}" if entry else ""))
    if ran_here:
        print("note    it ran here before, so it needs no fresh thermite test boot")
    for line in warn_units(commit, installed):
        print(line)
    verified = {"expected_conf_digest": expected_digest} if expected_digest is not None else {}
    result = deploy.deploy(AREA, layout, VAR, commit, force=args.force or ran_here, dry_run=args.dry_run,
                           how="rollback", host=host, marker=deploy.journal_marker, **verified)
    print(f"{'dry run ' if args.dry_run else 'deploy  '}{result.commit[:12]} {result.subject}")
    print_changes("conf", result.conf)
    print_changes("helper", result.atv_app)
    print_metrics(result, layout)
    if not args.dry_run and result.atv_app:
        print(f"helper  changed: thermite-atv-restart.path restarts thermite-atv.service ({layout.atv_restart})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("rollback", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("commit", nargs="?", metavar="COMMIT",
                        help="the commit to deploy (default: the one deployed before the current one)")
    oh_cli.add_dry_run(parser)
    parser.add_argument("--force", action="store_true",
                        help="deploy a commit that never ran here without a passed thermite test boot")
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the runtime state (default: runtime/ in instance/host.yaml's root, /srv/thermite)")
    args = parser.parse_args(argv)
    try:
        selected, host, _ = deployment_selection(args.commit or "HEAD")
        if args.commit is not None:
            args.commit = selected
        layout = deploy.State(args.state) if args.state else deploy.state(host)
        return roll_back(args, layout, host=host)
    except deploy.DeployError as exc:
        print(f"thermite rollback: {exc}", file=sys.stderr)
        return 2
