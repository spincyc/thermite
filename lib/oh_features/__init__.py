"""House-wide features: conf/ that comes from the whole house, not one device.

Each module provides:

    NAME      a short name for messages
    validate(data) -> list[str]
              problems in the parts of devices.yaml it reads
    render(house, conf)
              adds its files, Items and pages to conf (an oh_conf.Conf)

Features run after every integration, in REGISTRY order, so they can read
the point Items the integrations made (oh_conf.Conf.points).
"""

from __future__ import annotations

from . import all_off, model, overview, remote, routing

REGISTRY = (model, routing, all_off, remote, overview)
