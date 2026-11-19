"""Validate, build and lint a private configuration directory.

Nothing is written and no service is changed. The house file is validated
(thermite validate); then conf/, metrics/ and deploy/ are built in memory
from it, the host file and versions.json, as thermite generate would write
them; then the tracked files are linted (thermite lint). What is on disk
under conf/, metrics/ and deploy/ is neither read nor touched: thermite
generate --check compares it, and thermite generate --prune removes what
nothing generates now.
"""
import sys

import oh_cli


def built() -> int:
    """Build everything thermite generate would write for the site, and
    write none of it: 0, or 2 with what stopped it, as it would stop
    thermite generate."""
    import oh_deploy as deploy
    from oh_cli import generate
    from oh_conf import House
    house = House.load(oh_cli.HOUSE)
    try:
        versions, host = deploy.load(oh_cli.AREA)
    except (OSError, ValueError) as exc:
        print(f"thermite check: {exc}; nothing can be generated", file=sys.stderr)
        return 2
    files, monitoring, units = generate.rendered(house, versions, host)
    print(f"build: {len(files)} files of conf/, {len(monitoring)} of metrics/, {len(units)} unit files; none written")
    return 0


def main(argv=None):
    oh_cli.command("check", __doc__, (
        ("thermite check --config /path/to/private-site", "check configuration before committing or installing"),
    )).parse_args(argv)
    from oh_cli import validate, lint
    for step, args in ((validate.main, ([],)), (built, ()), (lint.main, ([],))):
        code = step(*args)
        if code:
            return code
    return 0
