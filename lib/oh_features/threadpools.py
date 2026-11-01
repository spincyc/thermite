"""Dispatch headroom for the pinned Google TV binding's blocking connections.

This is a capacity mitigation, not a bound on leaked connection tasks or a
replacement for fixing the binding's connection/disposal lifecycle.
"""

from __future__ import annotations

from oh_conf import Conf, House, RENDERED, uses
from oh_integrations import androidtv_googletv

NAME = "threadpools"
FILE = "services/threadpools.cfg"
CAPACITY = 32


def validate(data: dict) -> list[str]:
    return []


def render(house: House, conf: Conf) -> None:
    if not any(uses(device, androidtv_googletv.PHASE, androidtv_googletv.NAME) for device in house.devices):
        return
    conf.files[FILE] = (
        f"# {RENDERED}\n"
        "# Scheduling headroom while Google TV connections block thingHandler.\n"
        "# Capacity mitigation only: does not fix or bound connection-task leaks.\n"
        f"org.openhab.threadpool:thingHandler={CAPACITY}\n"
    )
