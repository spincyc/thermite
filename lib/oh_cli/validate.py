"""Check instance/devices.yaml, and that what it generates agrees with it.

    thermite validate [--json] [--house FILE]

The house file must pass every check thermite generate runs (its devices,
integrations, routing and theme). Its conf/ is then built in memory and
written nowhere: every Thing in it must belong to a device in the house
file (the last segment of its UID is the device id) and use only that
device's address; every Item must be a room location (loc_<room>) or be
named after a device. thermite generate holds what it writes to the same,
so no conf/ is read here: whether the one on disk is current is
thermite generate --check's to say.

A device that nothing watches is named in a note, which is no problem and
changes no exit status: one an integration commands with no reading of
whether it answers (no Thing, probed port or read of the rule's own), so
that no check or alert could say it stopped, and one that has an address
and no reading at all. A name a device lists for an integration thermite
does not have yet (oh_integrations.PLANNED) is named the same way: it is
accepted and renders nothing.
"""

from __future__ import annotations

import sys

import oh_cli
import oh_inventory as inventory
import oh_metrics as metrics
import oh_render as render
from oh_conf import House

EXAMPLES = (
    ("thermite validate", "check the house file and what it generates"),
    ("thermite validate --json", "the same, as JSON"),
)

NOTES = """\
exit status:
  0  the house file is valid and what it generates agrees with it
  1  what it generates disagrees with it
  2  a wrong switch, or a house file that is missing, cannot be read or
     has a problem (nothing is built)
"""


PROGRAM = "thermite validate"


def invalid(error: inventory.ConfigError) -> dict:
    """The report on a house file that cannot be checked (status invalid)."""
    return {"devices": 0, "items": 0, "notes": [], "problems": error.problems or [str(error)],
            "status": "invalid", "things": 0}


def checked(house: House) -> dict:
    """The report on a house file that loaded: its conf/, built in memory,
    against it (status ok or problems). A ConfigError other than the
    build's disagreement with the house goes to the caller."""
    devices = len(house.devices)
    try:
        built, problems = render.build(house), []
    except render.Disagreement as error:
        built, problems = error.conf, error.problems
    things, items, _ = render.objects(built)
    notes = [metrics.unobserved_text(*found) for found in metrics.unobserved(house, built)]
    return {"devices": devices, "items": len(items), "notes": notes, "problems": problems,
            "status": "ok" if not problems else "problems", "things": len(things)}


def status(report: dict) -> int:
    return {"ok": 0, "problems": 1}.get(report["status"], 2)


def said(report: dict, program: str = PROGRAM) -> None:
    """The report as text: problems and notes on stderr, the count on stdout."""
    problems, notes = report["problems"], report["notes"]
    for problem in problems:
        print(f"{program}: {problem}", file=sys.stderr)
    for note in notes:
        print(f"{program}: note: {note}", file=sys.stderr)
    print(f"house file: {report['devices']} devices, conf: {report['things']} Things, "
          f"{report['items']} Items, {len(problems)} problems"
          + (f", {len(notes)} note{'s' if len(notes) > 1 else ''}" if notes else ""))


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("validate", __doc__.splitlines()[0], EXAMPLES, NOTES)
    oh_cli.add_house(parser)
    oh_cli.add_json(parser)
    arguments = parser.parse_args(argv)
    try:
        house = House.load(arguments.house)
    except inventory.ConfigError as error:
        if not arguments.json:
            raise
        oh_cli.emit(invalid(error))
        return 2
    report = checked(house)
    if arguments.json:
        oh_cli.emit(report)
    else:
        said(report)
    return status(report)
