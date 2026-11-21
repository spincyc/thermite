"""Update the application, then move this host's services to what the site
pins, or show what that would do.

    thermite upgrade --config DIR [-n | --dry-run] [--yes] [--token-file FILE] [--source DIR]
                     [--revision REV] [--native-timeout SECONDS] [--allow-downgrade]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import oh_cli
from oh_cli import install
from oh_install import steps
from oh_install.system import UNATTENDED, System

DESCRIPTION = """\
Upgrade the complete Thermite installation from the official GitHub main branch.
Select your committed private site with --config DIR. Use --revision REV to
choose a reviewed branch, tag or commit, or --source DIR for a clean local
application checkout. Native component versions still come from the site's
versions.json; commit desired changes there before running this command.

One approval covers fetching and validating the application, preparing its
private Python environment, validating and committing the site release pin,
then installing the new version as one install transaction: native runtime
and helper services, monitoring, the HTTPS gateway, policy worker, verified
callbacks and identity schema. Existing authority, accounts, credentials and
TLS certificates are preserved.

Everything that can be done while Thermite runs is done first: configuration,
packages, the boot test, pulling the pinned images and checking there is room
to set the installed version aside; too little room refuses before anything
stops. Then the install engine, as root, keeps thermite.target from booting,
Thermite stops and the installed version is set aside. The new version is
installed and started; only when its health passes does the transaction
commit. Any failure from there rolls back in the same run: the installed
version, its files and state as they were, runs again, and the site's pin
commit is undone. The previous version stays set aside until the next
upgrade commits.

The previous owner command remains selected until the transaction commits.
If a run is interrupted, or its rollback cannot finish, the next thermite
upgrade first finishes or rolls back that transaction, then stops and says
so, with that outcome's exit status (1 when it finished a commit or closed
a transaction that never began); thermite status names it. Previous releases do not undo schema changes,
and upgrade refuses to pin a release older than the site's unless
--allow-downgrade is given after deliberate recovery. --native-timeout sets
the one native readiness allowance (300-7200 seconds).

--yes gives the run's one approval up front, for an unattended upgrade: it
covers what that approval covers, the root commands run under sudo -n,
nothing reads standard input, and any other question is answered no. It
needs sudo without a password. --token-file imports an existing internal
runtime API token from a private file, for recovery when no usable copy of
it remains.

--dry-run changes nothing and does not fetch: the remote revision and its exact
host differences remain unknown until an approved run. When the site pins the
release running it, it prints that release's provisional host plan for this
host. Run as your ordinary user. Use -v for details. Agents only use --dry-run.
"""

NOTES = """\
exit status:
  0  committed and verified, or nothing differed and the installed version verified
  1  declined, refused before anything stopped, a check failed, or an earlier
     transaction was finished or closed instead of upgrading
  2  invalid inputs, an older release without --allow-downgrade, missing
     installation or manifest, or run as root
  3  rolled back: the installed version runs again and its health passes
  4  rolled back, but the reinstated version's health does not pass; it keeps running
  5  the rollback did not finish: Thermite stays stopped; run thermite upgrade again
  130  interrupted; thermite upgrade rolls the open transaction back first
"""

EXAMPLES = (
    ("thermite upgrade -n", "what upgrading would do here; runs nothing"),
    ("thermite upgrade", "upgrade, asking once for the whole run; rolls back by itself on failure"),
    ("thermite upgrade --yes", "upgrade unattended, approved up front, asking nothing"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("upgrade", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_dry_run(parser)
    oh_cli.add_yes(parser, "the whole run")
    install.add_token_file(parser)
    parser.add_argument("--source", type=Path, help="upgrade from a clean application checkout instead of the official repository")
    parser.add_argument("--revision", help="application Git branch, tag or commit, its hash abbreviated or whole "
                        "(default: official main)")
    parser.add_argument("--native-timeout", type=steps.native_timeout_argument, metavar="SECONDS",
                        help="one native readiness allowance shared by runtime startup, gateway commissioning "
                             "and final settlement (default 900)")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="pin a release older than the site's committed one, after restoring compatible state")
    parser.add_argument("--_host-phase", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_run-approved", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_site-before", help=argparse.SUPPRESS)
    arguments = parser.parse_args(install.host_phase(argv))
    if os.geteuid() == 0:
        print("thermite upgrade: run it as your user; it asks once before applying the upgrade",
              file=sys.stderr)
        return 2
    approval = UNATTENDED.set(arguments.yes)
    try:
        return install.run(System(), arguments, "upgrade")
    finally:
        UNATTENDED.reset(approval)
