"""Install thermite on this host, or show what that would do.

    thermite install --config DIR [--dry-run] [--clean | --yes] [--restart-runtime]
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
from oh_install import bridge, changeover, manifest, ownership, progress, runner, steps
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
otherwise the host plan is resolved after approval. Use -v for details. Agents
only use --dry-run; they never run sudo.
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
    parser.add_argument("--_site-before", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("thermite install: run it as your user; it asks once before applying the installation", file=sys.stderr)
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


def run(system: System, arguments: argparse.Namespace) -> int:
    """thermite install, or its host phase, with `arguments` parsed."""
    try:
        if not arguments._host_phase:
            if arguments._run_approved:
                raise ValueError("run approval is only valid inside the application workflow")
            if not arguments.dry_run:
                found = changeover.resolve(AREA, system, "install")
                if found is not None:
                    return changeover.REFUSED if found == changeover.COMMITTED else found
            import oh_application_update
            return oh_application_update.install(
                AREA, source=arguments.source, revision=arguments.revision,
                dry_run=arguments.dry_run, approve=lambda: runner.confirm_run(system, "install"),
                runtime_token_file=arguments.runtime_token_file, allow_downgrade=arguments.allow_downgrade,
                host_args=(["--clean"] if arguments.clean else []) + (["--yes"] if arguments.yes else []) +
                          (["--restart-runtime"] if arguments.restart_runtime else []) +
                          (["--native-timeout", str(arguments.native_timeout)]
                           if arguments.native_timeout is not None else []) +
                          (["--verbose"] if oh_cli.switches.verbose else []), say=system.say,
                stage=lambda title: progress.stage(system, title))
        import oh_paths

        def body(before: str | None) -> int:
            oh_paths.require_compatible()
            return install(system, arguments.dry_run, clean=arguments.clean,
                           runtime_token_file=arguments.runtime_token_file, restart_runtime=arguments.restart_runtime,
                           run_approved=arguments._run_approved, native_timeout=arguments.native_timeout,
                           site_before=before)
        return changeover.pinned(system, AREA, arguments._site_before, arguments.dry_run, body)
    except subprocess.CalledProcessError as exc:
        print(f"thermite install: {changeover.escaped(AREA, 'install', exc)}", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite install: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        system.say(f"\nInterrupted. {runner.RERUN}")
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


def install(system: System, dry_run: bool, *, clean: bool = False, runtime_token_file: Path | None = None,
            restart_runtime: bool = False, run_approved: bool = False, native_timeout: int | None = None,
            site_before: str | None = None) -> int:
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
    if not dry_run:
        found = changeover.resolve(AREA, system, "install")
        if found is not None:
            # This run stopped there and installed nothing: never success.
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
    bridged = installed is None and bridge.needed(system)
    if installed is None and not bridged and manifest.thermite_present(context, system):
        system.say(manifest.missing_recovery(context))
        return 2
    kind = "clean" if clean else "install"
    versions = context.versions
    where = f"{context.hostname} ({context.address})" if context.address else context.hostname
    system.say(f"{'What installing' if dry_run else 'Installing'} thermite (runtime {versions['openhab']['version']}) "
               f"on this host, {where}, as {system.login()}, at {context.head[:12]}, "
               f"{'would do' if dry_run else 'step by step'}:")
    preflight = steps.install_preflight(context)
    code = runner.run(preflight, system, AREA, dry_run=dry_run)
    if dry_run:
        return changeover.preview(context, system, changeover.install_work(context, None, kind=kind),
                                  first=len(preflight) + 1) or code
    if code:
        return code
    if not clean and not restart_runtime and changeover.unchanged(context, system, installed):
        system.say(f"Nothing differs from the installed version (transaction {installed['transaction']}); "
                   "checking its health.")
        return runner.run([steps.Health(context)], system, AREA, first=len(preflight) + 1)
    if bridged:
        system.say("This is the first install transaction here: the installed version is described from "
                   f"{ownership.MANIFEST} and its fixed paths, and set aside like any other.")
    if not run_approved and not approve(system, context, clean):
        return 1
    code = changeover.run(context, system, lambda id: changeover.install_work(context, id, kind=kind),
                          installed=installed, bridged=bridged, before=site_before or None, first=len(preflight) + 1)
    if code == changeover.COMMITTED:
        completion(context, system)
        if context.host["gid"] not in system.process_groups():
            system.say("Log out and in again, so that your shell is in the thermite group for later deploys.")
    return code


def completion(context: steps.Context, system: System) -> None:
    system.say("Thermite's HTTPS gateway is installed. Use the verified gateway URL and trust certificate "
               "printed above. Security can be enabled from its Security page; ordinary controls work while it is off.")
