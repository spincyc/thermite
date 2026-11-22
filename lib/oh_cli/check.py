"""Check a private configuration directory: its house file, the build, and its Git content.

    thermite check [STEP ...] [--house FILE] [--json]
"""
from __future__ import annotations

import sys
from pathlib import Path

import oh_cli
import oh_inventory as inventory

PROGRAM = "thermite check"

#: Every step, in the order they run.
STEPS = ("house", "build", "lint")

DESCRIPTION = """\
Check a private configuration directory before committing or installing it.
Nothing is written and no service is changed. Each STEP is one of these, and
without one it runs all three; they always run in this order:

  house  the house file passes every check thermite generate runs, and its
         conf/, built in memory, agrees with it: every Thing belongs to a
         device in the house file and uses only that device's address, and
         every Item is a room location or named after a device. A device
         nothing watches is named in a note, which is no problem.
  build  conf/, metrics/ and deploy/ built in memory from the house file,
         the host file and versions.json, as thermite generate would write
         them, and none of it written.
  lint   the secret scan of every file Git has or would add in this area
         (a line that must carry a credential-looking word for a documented
         reason ends with `lint: allow`), and every `make TARGET` the docs
         cite must be one the Makefile defines.

A house file that is missing, cannot be read or has a problem stops it at
house: nothing can be built from it. A problem in the site's files (the
house file's, a host file or versions.json nothing can be generated from)
exits 1, as a step's finding does; 2 is kept for a check that could not
run: a wrong switch, or a file missing or unreadable. Otherwise each step
named runs, whatever the one before it found. What is on disk under
conf/, metrics/ and deploy/ is neither read nor touched: thermite
generate --check compares it, and thermite generate --prune removes what
nothing generates now.
"""

EXAMPLES = (
    ("thermite check --config /path/to/private-site", "check configuration before committing or installing"),
    ("thermite check house", "only the house file, and what it generates"),
    ("thermite check lint", "only the secret scan and the docs' make targets"),
    ("thermite check --json", "every step's report, as one JSON object"),
)

NOTES = """\
exit status:
  0  every step it ran found nothing wrong
  1  a step found a problem: the house file has one (it is not YAML, or
     fails a check; nothing after house runs), what it generates disagrees
     with it, a host file or versions.json holds what nothing can be
     generated from, the secret scan found something, or the docs name a
     make target the Makefile lacks
  2  it could not check: a wrong switch, or a house file, host file or
     versions.json that is missing or cannot be read (a missing or
     unreadable house file stops it at house)

--json's status is ok, problems or unreadable, for each exit status.
"""

#: --json's status for each exit status.
STATUSES = ("ok", "problems", "unreadable")


def failed(error: inventory.ConfigError) -> tuple[dict, int]:
    """The build's report of what stopped it: `problems` as its stderr
    names them; 2 when a file could not be read, 1 when one has a problem."""
    return {"error": str(error), "problems": error.lines()[:-1]}, unreadable(error)


def unreadable(error: inventory.ConfigError) -> int:
    """2 for a house file missing or unreadable, which check cannot read;
    1 for one that has a problem."""
    return 2 if isinstance(error, inventory.Unreadable) else 1


def built(house_path: Path) -> tuple[dict, int]:
    """Build everything thermite generate would write for the site, and
    write none of it: (the count of each, 0), or ({"error": ...}, 1) with
    what stopped it, as it would stop thermite generate (2: a file it could
    not read)."""
    import oh_deploy as deploy
    from oh_cli import generate
    from oh_conf import House
    try:
        house = House.load(house_path)
    except inventory.ConfigError as error:
        return failed(error)
    try:
        versions, host = deploy.load(oh_cli.AREA)
    except OSError as error:
        return {"error": f"{error}; nothing can be generated", "problems": []}, 2
    except ValueError as error:
        return {"error": f"{error}; nothing can be generated", "problems": []}, 1
    try:
        files, monitoring, units = generate.rendered(house, versions, host)
    except inventory.ConfigError as error:
        return failed(error)
    return {"conf": len(files), "metrics": len(monitoring), "units": len(units)}, 0


def said_built(report: dict) -> None:
    if "error" in report:
        for line in [*report["problems"], report["error"]]:
            print(f"{PROGRAM}: {line}", file=sys.stderr)
    else:
        print(f"build: {report['conf']} files of conf/, {report['metrics']} of metrics/, "
              f"{report['units']} unit files; none written")


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("check", DESCRIPTION, EXAMPLES, NOTES)
    # Checked below, not with choices=: before Python 3.12, argparse holds
    # the empty default of nargs="*" to its choices and refuses a plain check.
    parser.add_argument("steps", nargs="*", metavar="STEP", help="house, build or lint (default: all three)")
    oh_cli.add_house(parser)
    oh_cli.add_json(parser)
    arguments = parser.parse_args(argv)
    for step in arguments.steps:
        if step not in STEPS:
            parser.error(f"argument STEP: invalid choice: {step!r} (choose from {', '.join(map(repr, STEPS))})")
    from oh_cli import lint, validate
    from oh_conf import House
    chosen = [step for step in STEPS if step in arguments.steps] or list(STEPS)
    human = not arguments.json
    report: dict = {}
    codes = [0]
    if "house" in chosen:
        try:
            report["house"] = validate.checked(House.load(arguments.house))
        except inventory.ConfigError as error:
            report["house"] = validate.invalid(error)
            if human:
                for line in error.lines():
                    print(f"{PROGRAM}: {line}", file=sys.stderr)
            chosen = []
            codes.append(unreadable(error))
        else:
            codes.append(validate.status(report["house"]))
            if human:
                validate.said(report["house"], PROGRAM)
    if "build" in chosen:
        report["build"], code = built(arguments.house)
        codes.append(code)
        if human:
            said_built(report["build"])
    if "lint" in chosen:
        report["lint"] = lint.checked()
        codes.append(lint.status(report["lint"]))
        if human:
            lint.said(report["lint"], PROGRAM)
    code = max(codes)
    if arguments.json:
        oh_cli.emit({**report, "status": STATUSES[code]})
    return code
