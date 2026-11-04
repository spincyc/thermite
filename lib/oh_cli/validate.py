"""Check instance/devices.yaml, and that conf/ agrees with it.

    thermite validate [--json] [--house FILE] [--conf DIR]

The house file must pass every check thermite generate runs (its devices,
integrations, routing and theme). Every Thing in conf/ must belong to a
device in the house file (the last segment of its UID is the device id)
and use only that device's address; every Item must be a room location
(loc_<room>) or be named after a device.
"""

from __future__ import annotations

import json
import sys

import oh_cli
import oh_inventory as inventory
import oh_render as render

EXAMPLES = (
    ("thermite validate", "check the house file and conf/ against it"),
    ("thermite validate --json", "the same, as JSON"),
)

NOTES = """\
exit status:
  0  the house file is valid and conf/ agrees with it
  1  conf/ is missing, empty or disagrees with the house file
  2  a wrong switch, or a house file that is missing, cannot be read or
     has a problem (conf/ is not checked)
"""


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("validate", __doc__.splitlines()[0], EXAMPLES, NOTES)
    oh_cli.add_house(parser)
    oh_cli.add_conf(parser)
    oh_cli.add_json(parser)
    arguments = parser.parse_args(argv)
    try:
        data = inventory.checked(arguments.house, render.validate)
    except inventory.ConfigError as error:
        if not arguments.json:
            raise
        oh_cli.emit({"devices": 0, "items": 0, "problems": error.problems or [str(error)], "status": "invalid",
                     "things": 0})
        return 2
    devices = len(data.get("devices") or [])
    things, items, problems = inventory.conf_objects(arguments.conf)
    if not arguments.conf.is_dir():
        problems.append(f"{arguments.conf} does not exist; run thermite generate")
    elif not things and not items:
        problems.append(f"{arguments.conf} defines no Things or Items; run thermite generate")
    else:
        problems += inventory.cross_check(data, things, items)
    if arguments.json:
        oh_cli.emit({"devices": devices, "items": len(items), "problems": problems,
                     "status": "ok" if not problems else "problems", "things": len(things)})
    else:
        for problem in problems:
            print(f"thermite validate: {problem}", file=sys.stderr)
        print(f"house file: {devices} devices, conf: {len(things)} Things, "
              f"{len(items)} Items, {len(problems)} problems")
    return 1 if problems else 0
