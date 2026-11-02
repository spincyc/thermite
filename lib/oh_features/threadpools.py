"""Dispatch headroom for the pinned Google TV binding's blocking connections.

This is a capacity mitigation, not a bound on leaked connection tasks or a
replacement for fixing the binding's connection/disposal lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from oh_conf import Addon, Conf, House, RENDERED, uses
from oh_integrations import androidtv_googletv

NAME = "threadpools"
FILE = "services/threadpools.cfg"
CAPACITY = 32
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


def enabled(house: House) -> bool:
    return any(uses(device, androidtv_googletv.PHASE, androidtv_googletv.NAME) for device in house.devices)


def validate(data: dict) -> list[str]:
    return []


def render(house: House, conf: Conf) -> None:
    if not enabled(house):
        return
    conf.files[FILE] = (
        f"# {RENDERED}\n"
        "# Scheduling headroom while Google TV connections block thingHandler.\n"
        "# Capacity mitigation only: does not fix or bound connection-task leaks.\n"
        f"org.openhab.threadpool:thingHandler={CAPACITY}\n"
    )
    out = conf.yaml_file(ITEMS, ("Read-only scheduler telemetry; no device commands or private JVM fields.",))
    for point, label in POINTS.items():
        out.items[ITEM_NAMES[point]] = {
            "type": "Number", "label": label, "tags": ["Status"], "autoupdate": False,
            "metadata": {"stateDescription": {"config": {"readOnly": True}}},
        }
    conf.files[RULE] = SOURCE.read_text(encoding="utf-8")
    conf.add_addon(Addon("automation", "jsscripting", "read-only thingHandler dispatch telemetry"))
