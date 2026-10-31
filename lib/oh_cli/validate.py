"""Check instance/devices.yaml, and that conf/ agrees with it.

    thermite validate [--json] [--house FILE] [--conf DIR]

Every Thing must belong to a device in the house file (the last segment of
its UID is the device id) and use only that device's address; every Item
must be a room location (loc_<room>) or be named after a device. Exit 1 on
any problem.
"""

from __future__ import annotations

import json
import sys

import oh_cli
import oh_inventory as inventory

EXAMPLES = (
    ("thermite validate", "check the house file and conf/ against it"),
    ("thermite validate --json", "the same, as JSON"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("validate", __doc__.splitlines()[0], EXAMPLES)
    oh_cli.add_house(parser)
    oh_cli.add_conf(parser)
    oh_cli.add_json(parser)
    arguments = parser.parse_args(argv)
    data = inventory.load(arguments.house)
    problems = inventory.validate(data)
    things, items, conf_problems = inventory.conf_objects(arguments.conf)
    problems += conf_problems
    if isinstance(data, dict):
        problems += inventory.cross_check(data, things, items)
    devices = len(data.get("devices") or []) if isinstance(data, dict) else 0
    if arguments.json:
        print(json.dumps({"devices": devices, "items": len(items), "problems": problems,
                          "status": "ok" if not problems else "problems",
                          "things": len(things)}, sort_keys=True))
    else:
        for problem in problems:
            print(f"thermite validate: {problem}", file=sys.stderr)
        print(f"house file: {devices} devices, conf: {len(things)} Things, "
              f"{len(items)} Items, {len(problems)} problems")
    return 1 if problems else 0
