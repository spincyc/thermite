"""Dispatch headroom for bindings whose blocking connections hold thingHandler.

An integration whose binding blocks openHAB's thingHandler pool declares
HANDLER_POOL (the threads it needs) and HANDLER_POOL_WHY (oh_integrations);
a house with a device listing one gets the larger pool and read-only
telemetry of it. This is a capacity mitigation, not a bound on leaked
connection tasks or a replacement for fixing a binding's connection/disposal
lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import oh_integrations as integrations
from oh_conf import Addon, Conf, House, RENDERED, uses

NAME = "threadpools"
FILE = "services/threadpools.cfg"
ITEMS = "yaml/threadpools.yaml"
RULE = "automation/js/thermite_handler_pool.js"
SOURCE = Path(__file__).with_name("threadpools.js")
POINTS = {
    "active": "Active device dispatch workers",
    "core": "Device dispatch worker capacity",
    "queued": "Queued device dispatch tasks (including future work)",
    "overdue": "Device dispatch probe delay (seconds)",
    "observed": "Device dispatch observation (Unix seconds)",
}
ITEM_NAMES = {point: f"thermite_handler_{point}" for point in POINTS}
HOUSE_ITEMS = tuple(ITEM_NAMES.values())
#: The telemetry rule reads the pool and updates its Items only.
ITEM_ONLY_RULES = (RULE,)


def needs(house: House) -> list:
    """The integrations with HANDLER_POOL that a device of the house lists."""
    return [module for module in integrations.REGISTRY if hasattr(module, "HANDLER_POOL")
            and any(uses(device, module.PHASE, module.NAME) for device in house.devices)]


def enabled(house: House) -> bool:
    return bool(needs(house))


def validate(data: dict) -> list[str]:
    return []


def render(house: House, conf: Conf) -> None:
    modules = needs(house)
    if not modules:
        return
    why = " and ".join(dict.fromkeys(module.HANDLER_POOL_WHY for module in modules))
    conf.files[FILE] = (
        f"# {RENDERED}\n"
        f"# Scheduling headroom while {why}.\n"
        "# Capacity mitigation only: does not fix or bound connection-task leaks.\n"
        f"org.openhab.threadpool:thingHandler={max(module.HANDLER_POOL for module in modules)}\n"
    )
    out = conf.yaml_file(ITEMS, ("Read-only scheduler telemetry; no device commands or private JVM fields.",))
    for point, label in POINTS.items():
        out.items[ITEM_NAMES[point]] = {
            "type": "Number", "label": label, "tags": ["Status"], "autoupdate": False,
            "metadata": {"stateDescription": {"config": {"readOnly": True}}},
        }
    conf.files[RULE] = SOURCE.read_text(encoding="utf-8")
    conf.add_addon(Addon("automation", "jsscripting", "read-only thingHandler dispatch telemetry"))
