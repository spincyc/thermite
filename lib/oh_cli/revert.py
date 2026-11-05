"""Go back to the version the last install or upgrade set aside, with the
state it had then.

    thermite revert --config DIR [-n | --dry-run]
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_install import changeover, manifest, runner, steps
from oh_install.model import Check, Command, Step, Task
from oh_install.system import System

DESCRIPTION = """\
Put back the version that ran before the last install or upgrade committed,
as one install transaction of its own: its files, units, native-port filter
and confinement, and its state in runtime/ and access/ exactly as that
upgrade set it aside; its gateway release, images and application
environment were kept for this. Everything written since that upgrade is
lost: accounts, pairings, credentials, the gateway's identity changes,
monitoring data. Revert says so and asks you to type the installation root
before anything stops.

Once the revert commits, the site's branch goes back to the commit before
the upgrade's pin commit, and ~/.local/bin/thermite selects the previous
application's command again. Any failure before the commit rolls back in
the same run, and the version that runs now runs again.

A revert needs the site at that upgrade's pin commit with nothing
uncommitted, and the upgrade's own record in the site's var/transaction. It
is refused after an uninstall, a clean install or another revert, and for
the first transaction on a host installed before the install engine.
thermite rollback installs an earlier site commit without touching state.
"""

NOTES = """\
exit status:
  0  committed: the previous version runs again, with its state
  1  declined, or refused before anything stopped (also when an earlier
     transaction was finished or closed instead of reverting)
  2  nothing to revert to, the site is not at the upgrade's commit, or the
     previous application environment is gone
  3  rolled back: the version that ran runs again and its health passes
  4  rolled back, but its health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite revert again
  130  interrupted; the next thermite revert rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite revert -n", "what reverting to the version before the last upgrade would do"),
    ("thermite revert", "revert, after typing the installation root; state written since is lost"),
)


class PutBack(Step):
    """The operator's own files as the reverted upgrade found them: the
    runtime token, Grafana password, runtime credentials and gateway
    profile that go with the state that comes back."""

    title = "Put the operator's files back"
    always = True
    stop_on_failure = True

    def __init__(self, transaction: changeover.Transaction):
        self.transaction = transaction

    def check(self, system: System) -> Check:
        return Check(False, f"as transaction {self.transaction.id} set them aside")

    def actions(self, system: System) -> list:
        return [Task("copy the operator's files back", self.perform)]

    def perform(self, system: System) -> int:
        self.transaction.put_back()
        return 0


class Healthy(Step):
    """The previous version's health, through its own command."""

    title = "Health of the previous version"
    always = True
    stop_on_failure = True

    def __init__(self, argv: tuple[str, ...]):
        self.argv = argv

    def check(self, system: System) -> Check:
        return Check(False, "health --production --installation, as the previous application checks it")

    def actions(self, system: System) -> list:
        return [Command(self.argv, show_output=True)]


def environment(digest: str | None, home: Path | None = None) -> Path | None:
    """The prepared application environment of release `digest`, if kept."""
    import json
    store = (Path.home() if home is None else home) / ".local/share/thermite/application"
    if not digest:
        return None
    for record in sorted(store.glob("*.json")):
        try:
            saved = json.loads(record.read_text())
            name, found = saved["environment"], saved["release"]["digest"]
        except (OSError, ValueError, TypeError, KeyError):
            continue
        command = store / str(name) / "bin/thermite"
        if found == digest and Path(name).name == name and command.is_file() and not command.is_symlink():
            return store / name
    return None


def refusal(context: steps.Context, system: System) -> tuple[str | None, dict | None]:
    """Why there is nothing to revert to here (None when there is), and the
    reverted transaction's record."""
    try:
        installed = manifest.installed(system, context.installation.root)
    except manifest.ManifestError as exc:
        return f"{exc}. {manifest.missing_recovery(context)}", None
    if installed is None or not installed["installed"]:
        return "Thermite is not installed here, so there is nothing to revert", None
    kept = installed["kept"]
    try:
        record = changeover.Transaction.load(context.var, kept).record
    except (OSError, ValueError):
        return (f"{context.var}/transaction/{kept}/record.json, the record of the transaction to revert, is "
                "missing: only the site that ran it can revert it"), None
    if record.get("phase") != "committed" or record.get("kind") not in ("install", "upgrade"):
        return (f"transaction {kept} is a{'n' if str(record.get('kind'))[:1] in 'aeiou' else ''} {record.get('kind')}"
                f" ({record.get('phase')}); only a committed install or upgrade keeps a version to revert to"), None
    if not record.get("installed") or record.get("bridged"):
        return (f"the version before transaction {kept} was not installed by a transaction, so it cannot be "
                "reverted to; thermite rollback installs an earlier site commit"), None
    before, after = record.get("head_before"), record.get("head_after")
    if not before or not after:
        return f"transaction {kept} does not record the site commits around it", None
    if context.head != after:
        return (f"the site is at {context.head[:12]}, not {after[:12]}, the commit transaction {kept} installed; "
                "a revert puts the site back to the commit before that one"), None
    if environment(record["application"].get("previous")) is None:
        return (f"the previous application, {str(record['application'].get('previous'))[:12]}, has no prepared "
                "environment any more"), None
    return None, record


def revert(system: System, dry_run: bool) -> int:
    if not dry_run:
        found = changeover.resolve(AREA, system, "revert")
        if found is not None:
            return changeover.REFUSED if found == changeover.COMMITTED else found
    context = steps.committed_context(AREA, system)
    problem, record = refusal(context, system)
    if problem:
        system.say(f"Nothing was changed: {problem}.")
        return 2
    kept = changeover.Transaction.load(context.var, record["id"])
    before, after = record["head_before"], record["head_after"]
    command = environment(record["application"]["previous"]) / "bin/thermite"
    root = context.installation.root
    system.say(f"{'What reverting' if dry_run else 'Reverting'} thermite on this host, {context.hostname}, to the "
               f"version before transaction {kept.id} (site {before[:12]}), "
               f"{'would do' if dry_run else 'step by step'}:")
    system.say(f"Everything Thermite wrote since transaction {kept.id} committed is lost: the state under {root} "
               "goes back to how it was before that upgrade.")
    work = changeover.Work(
        "revert", "revert", [steps.Account(context)], [PutBack(kept)],
        [steps.StartTarget(context), changeover.Authorize(),
         Healthy((str(command), "--site", str(AREA), "health", "--production", "--installation", "--settle",
                  str(changeover.SETTLE), "--revision", before))],
        None, items=lambda system: [], pull=False, reinstate=True,
        confirm=changeover.Confirm(
            "Confirm the revert", root,
            f"This revert loses everything written under {root} since transaction {kept.id}: accounts, pairings, "
            "credentials, history, monitoring data. Until it commits a rollback keeps all of it."),
        after=lambda system, transaction: reverted(system, transaction, command, before, after))
    if dry_run:
        return changeover.preview(context, system, work, first=1)
    if not runner.confirm_run(system, "revert", f"Run {changeover.command(AREA, 'revert')} again."):
        return 1
    return changeover.run(context, system, lambda id: work, installed=manifest.installed(system, root),
                          bridged=False, before=after, after=before, first=1, pinned=False)


def reverted(system: System, transaction: changeover.Transaction, command: Path, before: str, after: str) -> None:
    """The operator's side of a committed revert: the site's pin and the
    owner command go back with the version."""
    import oh_application_update
    import oh_source_upgrade
    site = shlex.quote(str(AREA))
    try:
        if oh_source_upgrade.reset_pin(AREA, expected=after, previous=before):
            system.say(f"Site: back at {before[:12]}; the pin commit {after[:12]} is no longer on its branch.")
    except (OSError, ValueError) as exc:
        system.say(f"The site still pins the reverted release ({exc}). To pin the version that runs: "
                   f"git -C {site} revert --no-edit {after[:12]}")
    try:
        launcher = oh_application_update.select(Path.home(), command)
        system.say(f"{launcher} selects the previous application again.")
    except (OSError, ValueError) as exc:
        system.say(f"{Path.home() / '.local/bin/thermite'} still selects the reverted application ({exc}); "
                   f"use {command} directly.")


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("revert", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite revert: run it as your user; it asks before reverting", file=sys.stderr)
        return 2
    system = System()
    try:
        return revert(system, arguments.dry_run)
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite revert: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. Run {changeover.command(AREA, 'revert')} again.")
        return changeover.INTERRUPTED
