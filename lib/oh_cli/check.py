"""Generate, validate and lint a private configuration directory.

No service is changed. Generated configuration stays in the site's ignored
output, where a file an earlier generate wrote that nothing generates now is
removed; any other file there stops the check.
"""
import oh_cli


def main(argv=None):
    oh_cli.command("check", __doc__, (
        ("thermite check --config /path/to/private-site", "check configuration before committing or installing"),
    )).parse_args(argv)
    from oh_cli import generate, validate, lint
    for command, args in ((generate, ["--prune"]), (validate, []), (lint, [])):
        code = command.main(args)
        if code:
            return code
    return 0
