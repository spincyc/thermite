"""All off: one command that turns every room and every controlled device off.

The Switch Item house_all_off takes ON from the remote page's All off button
(and, later, from a wall button or a scene). conf/automation/js/all_off.js,
all_off.js beside this file with TABLE filled in, then sends every room's
Source Item Off (oh_features.routing) and one power Item of each device under
control OFF. That is the power a room's Source turns it off with (its
integration's stage()) where it has one, else its remote() power: a Sony
TV's is its sony_ip_control power, which sets standby. Devices openHAB does
not control yet are left to Control4.
"""

from __future__ import annotations

from pathlib import Path

import oh_integrations as integrations
from oh_conf import Addon, Conf, House, fill, indented

from .. import routing

NAME = "all_off"
ITEM = "house_all_off"
ITEMS = "yaml/all_off.yaml"
RULE = "automation/js/all_off.js"
SOURCE = Path(__file__).resolve().parent / "all_off.js"
HEADER = (
    "All off (lib/oh_features/all_off): ON turns every room's Source Off and",
    "every device openHAB controls off (conf/automation/js/all_off.js).",
    "autoupdate is off: the Item only takes the command and keeps no state.",
)


def validate(data: dict) -> list[str]:
    return []


def commands(house: House) -> list[list[str]]:
    """[[Item, command], ...]: every room's Source Off, then one power Item
    of each device under control OFF, in inventory order."""
    out = [[routing.item_name(room), routing.OFF] for room in (house.data.get("routing") or {}).get("rooms") or {}]
    for device in house.devices:
        stager = routing.stager(device)
        if stager:
            powers = [staged["power"] for zone in (None, 2, 3, 4)
                      if (staged := stager.stage(device, zone))]
            if powers:
                out.extend([power, "OFF"] for power in dict.fromkeys(powers))
                continue
        for module in integrations.defining("remote"):
            control = module.remote(device)
            if control and control.get("power"):
                out.append([control["power"], "OFF"])
                break
    return out


def rule(house: House) -> str:
    table = {"item": ITEM, "commands": commands(house)}
    return fill(SOURCE.read_text(encoding="utf-8"), {"TABLE": table}, SOURCE, encode=indented)


def render(house: House, conf: Conf) -> None:
    if not commands(house):
        return
    out = conf.yaml_file(ITEMS, HEADER)
    out.items[ITEM] = {"type": "Switch", "label": "All off", "icon": "switch", "autoupdate": False}
    conf.files[RULE] = rule(house)
    conf.add_addon(Addon("automation", "jsscripting", "the All off rule (all_off.js)"))
