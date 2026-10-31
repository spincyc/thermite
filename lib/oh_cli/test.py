"""Test conf/ on a test runtime on 127.0.0.1:18081, never production.

    thermite test [--timeout SECONDS] [--house FILE]
                     boot, routing and controls, one after the other
    thermite test boot|routing|controls [SWITCHES]
                     one of them, with its own switches
"""

from __future__ import annotations

import argparse
import sys

import oh_cli
from oh_testing import boot, controls, routing

#: In the order a bare `thermite test` runs them: they share one instance.
ACTIONS = {"boot": boot, "routing": routing, "controls": controls}

DESCRIPTION = """\
Test conf/ on a test runtime, the pinned distribution on 127.0.0.1:18081,
never production: `boot` boots conf/ and reports how it loads (thermite
deploy installs only a conf/ that passed it); `routing` and `controls`
generate a conf from the house file that drives simulated matrices and
devices on 127.0.0.1, and test the room Source selectors and the remote
page's controls end to end.

Without an action it runs all three, in that order, since they share the
test runtime, reports each, and exits with the worst status. Then
--timeout applies to each, and --house to routing and controls (boot tests
conf/ itself). An action's own switches go after it: thermite help test boot.
"""

EXAMPLES = (
    ("thermite test", "boot, routing and controls, one after the other"),
    ("thermite test boot", "before a deploy: does conf/ load cleanly?"),
    ("thermite test routing", "every room's Source selector, against simulated matrices"),
    ("thermite test controls --timeout 600", "the remote page's controls; give a slow host longer to boot"),
)

NOTES = """\
exit status:
  0  every test passed
  1  a test failed
  2  runtime could not be booted or queried
"""


def defaults(module) -> argparse.Namespace:
    """The switches of one action as it runs without any."""
    parser = argparse.ArgumentParser(add_help=False)
    module.add_arguments(parser)
    return parser.parse_args([])


def run_one(module, args: argparse.Namespace) -> int:
    try:
        return module.run(args)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2


def run_all(timeout: float, house, log=print) -> int:
    results = {}
    for name, module in ACTIONS.items():
        log(f"== thermite test {name}")
        sys.stdout.flush()
        args = defaults(module)
        args.timeout = timeout
        if house is not None and hasattr(args, "house"):
            args.house = house
        results[name] = run_one(module, args)
    log("\ntest: " + ", ".join(f"{name} " + ("ok" if code == 0 else f"FAIL (exit {code})")
                               for name, code in results.items()))
    return max(results.values())


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    parser = oh_cli.command("test", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_timeout(parser, 300, "each test's runtime", dest="all_timeout")
    oh_cli.add_house(parser, None, dest="all_house")
    commands = oh_cli.actions(parser, required=False)
    for name, module in ACTIONS.items():
        action = oh_cli.action(commands, name, module.SUMMARY, description=module.DESCRIPTION,
                               examples=module.EXAMPLES, notes=module.NOTES)
        module.add_arguments(action)
    args = parser.parse_args(argv)
    if args.command is None:
        return run_all(args.all_timeout, args.all_house)
    before = [word for word in argv[:argv.index(args.command)] if not oh_cli.is_global(word)]
    if before:
        parser.error(f"an action's switches go after it: thermite test {args.command} {' '.join(before)}")
    return ACTIONS[args.command].run(args)
