"""Install thermite on this host, or show what that would do.

    thermite install --config DIR [-n | --dry-run [--json]] [--clean | --yes] [--restart-runtime]
                     [--token-file FILE] [--native-timeout SECONDS] [--allow-downgrade]
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
from oh_install import changeover, manifest, progress, runner, steps
from oh_install.system import UNATTENDED, System

DESCRIPTION = """\
Deploy the complete Thermite installation using committed private configuration.
Run as your ordinary owner with --config DIR. The current application release
is prepared in a private environment; --source DIR and --revision REV select
a clean application checkout instead. The application validates the site and
commits its release requirement before applying the host workflow.

One approval covers application setup, packages, the service account, native
runtime and helpers, monitoring, HTTPS gateway, policy worker, verified device
callbacks, schema migrations and readiness checks.
The native ports are restricted to this host. An existing gateway keeps its
identity, accounts, certificates and callback routes; a fresh gateway creates
its TLS certificate and starts with Security off. Enable Security on its page
to bootstrap the first administrator with owner proof.

Everything that can be done while Thermite runs is done first. Then the
host changes as one install transaction: the install engine, as root, keeps
thermite.target from booting, Thermite stops and the installed version, if
any, is set aside; the new one is installed and started, and the
transaction commits once its health passes. Any failure from there rolls
back in the same run: the version that ran before runs again, and the
site's pin commit is undone. When nothing differs from the installed
version, install only checks its health. Thermite's files without
/etc/thermite/installed.json refuse the install with the recovery steps.

The command in ~/.local/bin is switched only after the commit. Keep that
directory first on PATH; a successful run warns when it is not. If a run is
interrupted, or its rollback cannot finish, the next install, upgrade or
uninstall first finishes or rolls back that transaction, then stops.
Application setup never changes OS Python. Install refuses to pin a release
older than the site's committed one unless --allow-downgrade is given, after
deliberately restoring state that the newer release may have migrated.

--clean installs inside the same transaction with fresh native state: it
moves the state trees under runtime/ aside and clears the local
runtime/monitoring credentials, and deletes them only at the commit; it
requires typing the exact state path. Logs, history, old backups and the
gateway's identity/TLS state are retained.
--yes gives the run's one approval up front, for an unattended install from a
system unit with standard input at /dev/null: it covers what that approval
covers, the root commands run under sudo -n, nothing reads standard input,
and any other question is answered no. It needs sudo without a password,
and is refused with --clean, which asks you to type the state path.
--restart-runtime installs even when nothing differs, starting everything fresh.
--native-timeout sets the one native readiness allowance (300-7200 seconds).
--dry-run fetches, commits and changes nothing. When the site pins the release
running it, it also prints that release's provisional host plan for this host;
otherwise the host plan is resolved after approval. With --json it prints
what it would do as one JSON object: the application and its revision, the
notes, and each step of the host plan (its title, whether it is done, what
it would do or run) with the install transaction's summary. Use -v for
details. Agents only use --dry-run; they never run sudo.
"""

NOTES = """\
exit status:
  0  committed and verified, or nothing differed and the installed version verified
  1  declined, refused before anything stopped, a check failed, or an earlier
     transaction was finished or closed instead of installing
  2  invalid inputs, an older release without --allow-downgrade, Thermite's
     files without its installed manifest, or run as root
  3  rolled back: the version that ran before runs again and its health passes
     (or nothing was installed before, and nothing is now)
  4  rolled back, but the reinstated version's health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite install again
  130  interrupted; the next install rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite install --config /path/to/private-site", "install this site's configuration on the host"),
    ("thermite install --config /path/to/private-site --dry-run", "review the plan without changing services"),
    ("thermite install --dry-run", "each step, and what it would run here; runs nothing"),
    ("thermite install -n --json", "the same, as one JSON object"),
    ("thermite install", "install with one approval for the whole run; rolls back by itself on failure"),
    ("thermite install --clean --dry-run", "review a complete native-state reset and fresh installation"),
    ("thermite install --yes --config /path/to/private-site", "install unattended, approved up front, asking nothing"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("install", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--clean", action="store_true",
                        help="delete native state and local API/Grafana credential copies, then reinstall; pairings are lost")
    parser.add_argument("--yes", action="store_true",
                        help="approve the whole run up front, unattended: nothing reads standard input, and any "
                             "other question is answered no")
    parser.add_argument("--restart-runtime", action="store_true",
                        help="restart runtime once after deployment, then run normal readiness checks")
    add_token_file(parser)
    parser.add_argument("--source", type=Path, help="use a clean application checkout for this deployment")
    parser.add_argument("--revision", help="application Git revision to deploy from --source")
    parser.add_argument("--native-timeout", type=steps.native_timeout_argument, metavar="SECONDS",
                        help="one native readiness allowance shared by runtime startup, gateway commissioning "
                             "and final settlement (default 900)")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="pin a release older than the site's committed one, after restoring compatible state")
    oh_cli.add_json(parser)
    parser.add_argument("--_host-phase", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_run-approved", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_site-before", help=argparse.SUPPRESS)
    arguments = parser.parse_args(host_phase(argv))
    if os.geteuid() == 0:
        print("thermite install: run it as your user; it asks once before applying the installation", file=sys.stderr)
        return 2
    if not json_with_dry_run("install", arguments):
        return 2
    if arguments.yes and arguments.clean:
        print("thermite install: --yes cannot approve a clean install, which asks you to type the state path",
              file=sys.stderr)
        return 2
    approval = UNATTENDED.set(arguments.yes)
    try:
        return run(System(), arguments)
    finally:
        UNATTENDED.reset(approval)


def json_with_dry_run(verb: str, arguments: argparse.Namespace) -> bool:
    """Whether --json, if given, comes with the dry run it reports; it is
    refused, and said so, otherwise."""
    if arguments.json and not arguments.dry_run:
        print(f"thermite {verb}: --json reports what a dry run would do: give it with -n/--dry-run", file=sys.stderr)
        return False
    return True


def add_token_file(parser: argparse.ArgumentParser) -> None:
    """--token-file FILE, as `runtime_token_file`, and --runtime-token-file,
    its spelling before, for thermite install and thermite upgrade."""
    parser.add_argument("--token-file", type=Path, metavar="FILE", dest="runtime_token_file",
                        help="advanced: import an existing internal runtime API token from your private file")
    oh_cli.add_renamed(parser, "--runtime-token-file", "--token-file")


def host_phase(argv: list[str] | None) -> list[str] | None:
    """`argv`, with a host phase's --runtime-token-file read as --token-file
    without the warning: the application workflow hands it on by that
    spelling, the one every release it may hand over to reads
    (oh_application_update.apply)."""
    if argv is None or "--_host-phase" not in argv:
        return argv
    old = "--runtime-token-file"
    return ["--token-file" if word == old else "--token-file" + word[len(old):] if word.startswith(old + "=")
            else word for word in argv]


def rerun(verb: str) -> str:
    """How the owner goes on after a stop."""
    return f"Run {changeover.command(AREA, verb)} again to go on from here."


def run(system: System, arguments: argparse.Namespace, verb: str = "install") -> int:
    """thermite install or thermite upgrade (`verb`), or its host phase,
    with `arguments` parsed: the two differ in what they may be given and
    in what install() does for each."""
    given = vars(arguments)
    # --json comes only with --dry-run (json_with_dry_run): the report of what it would do.
    report = {"verb": verb, "dry_run": True} if given.get("json") else None
    try:
        if not arguments._host_phase:
            if arguments._run_approved:
                raise ValueError("run approval is only valid inside the application workflow")
            if not arguments.dry_run:
                found = changeover.scoped(AREA, system, verb, lambda: changeover.resolve(AREA, system, verb))
                if found is not None:
                    return changeover.REFUSED if found == changeover.COMMITTED else found
            import oh_application_update
            return changeover.reported(report, lambda: getattr(oh_application_update, verb)(
                AREA, source=arguments.source, revision=arguments.revision,
                dry_run=arguments.dry_run, approve=lambda: runner.confirm_run(system, verb, rerun(verb)),
                runtime_token_file=arguments.runtime_token_file, allow_downgrade=arguments.allow_downgrade,
                host_args=[flag for flag, name in (("--clean", "clean"), ("--yes", "yes"),
                                                   ("--restart-runtime", "restart_runtime")) if given.get(name)] +
                          (["--native-timeout", str(arguments.native_timeout)]
                           if arguments.native_timeout is not None else []) +
                          (["--verbose"] if oh_cli.switches.verbose else []), say=system.say,
                stage=lambda title: progress.stage(system, title), report=report))
        import oh_paths

        def body(before: str | None) -> int:
            oh_paths.require_compatible()
            return install(system, arguments.dry_run, verb=verb, clean=given.get("clean", False),
                           runtime_token_file=arguments.runtime_token_file,
                           restart_runtime=given.get("restart_runtime", False),
                           run_approved=arguments._run_approved, native_timeout=arguments.native_timeout,
                           site_before=before, plan=report)
        return changeover.reported(report, lambda: changeover.scoped(
            AREA, system, verb,
            lambda: changeover.pinned(system, AREA, arguments._site_before, arguments.dry_run, body),
            dry_run=arguments.dry_run))
    except subprocess.CalledProcessError as exc:
        print(f"thermite {verb}: {changeover.escaped(AREA, verb, exc)}", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite {verb}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {rerun(verb)}")
        return changeover.INTERRUPTED


def approve(system: System, context: steps.Context, clean: bool) -> bool:
    if context.restart_runtime:
        system.say("The runtime starts fresh with the rest of Thermite inside the install transaction.")
    if clean:
        system.say(f"A clean install deletes the state under {context.layout.root} and local credential copies "
                   "once it commits; it asks you to type that path before it begins.")
    if oh_cli.switches.verbose:
        system.say("This install run includes packages, service-account setup, permissions, the boot check, then "
                   "one install transaction: Thermite stops, the installed version is set aside, the new one is "
                   "installed and started, and it commits once its health passes, or rolls back.")
    return runner.confirm_run(system, "clean install" if clean else "install")


def install(system: System, dry_run: bool, *, verb: str = "install", clean: bool = False,
            runtime_token_file: Path | None = None, restart_runtime: bool = False, run_approved: bool = False,
            native_timeout: int | None = None, site_before: str | None = None, plan: dict | None = None) -> int:
    """The host phase of thermite install and of thermite upgrade (`verb`):
    finish any open transaction, read the committed site and what is
    installed, run the checks that need nothing stopped, then install the
    version as one transaction, or only check its health when nothing
    differs. An upgrade needs an installation and has no --clean or
    --restart-runtime; an install also makes the first one. A dry run
    given `plan` (a --json report) fills it in instead of showing the steps."""
    upgrade = verb == "upgrade"
    again = rerun(verb)
    if clean and runtime_token_file is not None:
        system.say("A clean installation creates new runtime credentials; an existing token cannot be imported into it.")
        return 2
    try:
        import yaml  # noqa: F401
    except ImportError:
        system.say("PyYAML is missing from this Python environment. Install the application dependencies, "
                   "then rerun: the site must be validated before any host changes.")
        if dry_run and plan is not None:
            plan["steps"] = runner.plan([steps.Packages()], system)[1]
        elif dry_run:
            runner.run([steps.Packages()], system, AREA, dry_run=True)
        return 2
    if not dry_run:
        found = changeover.resolve(AREA, system, verb)
        if found is not None:
            # This run stopped there and installed nothing, even when it
            # finished an earlier transaction's commit: never success.
            return changeover.REFUSED if found == changeover.COMMITTED else found
    context = steps.committed_context(AREA, system, runtime_token_file)
    if native_timeout is not None:
        context = dataclasses.replace(context, native_timeout=native_timeout)
    if restart_runtime:
        context = dataclasses.replace(context, restart_runtime=True)
    try:
        installed = manifest.installed(system, context.installation.root)
    except manifest.ManifestError as exc:
        system.say(f"{exc}. {manifest.missing_recovery(context)}")
        return 2
    present = [] if installed is not None else manifest.thermite_present(context, system)
    if upgrade and installed is None:
        system.say(manifest.missing_recovery(context) if present else
                   "Thermite is not installed on this host yet: thermite install")
        return 2
    # The state under the installation root alone does not refuse an install:
    # the recovery it prints ends with this command, on the state that
    # stayed. The preconditions prove the root Thermite's, and the install
    # engine sets its state aside as found.
    if any(path != context.installation.root for path in present):
        system.say(manifest.missing_recovery(context))
        return 2
    kind = "upgrade" if upgrade else "clean" if clean else "install"
    if upgrade:
        title = (f"{'What upgrading' if dry_run else 'Upgrading'} thermite on this host, {context.hostname}, to "
                 f"what {context.head[:12]} pins, {'would do' if dry_run else 'step by step'}:")
    else:
        where = f"{context.hostname} ({context.address})" if context.address else context.hostname
        title = (f"{'What installing' if dry_run else 'Installing'} thermite (runtime "
                 f"{context.versions['openhab']['version']}) on this host, {where}, as {system.login()}, at "
                 f"{context.head[:12]}, {'would do' if dry_run else 'step by step'}:")
    system.say(title)
    preflight = steps.install_preflight(context)
    if dry_run and plan is not None:
        code, checked = runner.plan(preflight, system)
        prepared, preparation, transaction = changeover.plan(
            system, changeover.install_work(context, None, kind=kind), first=len(preflight) + 1)
        plan.update(title=title, commit=context.head, steps=[*checked, *preparation], transaction=transaction)
        return prepared or code
    code = runner.run(preflight, system, AREA, dry_run=dry_run, rerun=again)
    if dry_run:
        return changeover.preview(context, system, changeover.install_work(context, None, kind=kind),
                                  first=len(preflight) + 1) or code
    if code:
        return code
    if not clean and not restart_runtime and changeover.unchanged(context, system, installed):
        system.say(f"Nothing differs from the installed version (transaction {installed['transaction']}); "
                   "switching on any of its rules found disabled and checking its health.")
        return runner.run([steps.RuleSwitches(context), steps.Health(context)], system, AREA,
                          first=len(preflight) + 1, rerun=again)
    if not run_approved and not (runner.confirm_run(system, verb, again) if upgrade
                                 else approve(system, context, clean)):
        return 1
    code = changeover.run(context, system, lambda id: changeover.install_work(context, id, kind=kind),
                          installed=installed, before=site_before or None, first=len(preflight) + 1)
    if code == changeover.COMMITTED and not upgrade:
        completion(context, system)
        if context.host["gid"] not in system.process_groups():
            system.say("Log out and in again, so that your shell is in the thermite group for later deploys.")
    return code


def completion(context: steps.Context, system: System) -> None:
    system.say("Thermite's HTTPS gateway is installed. Use the verified gateway URL and trust certificate "
               "printed above. Security can be enabled from its Security page; ordinary controls work while it is off.")
