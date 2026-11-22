"""Hot-plug detect (HPD) on the outputs of an HDMI matrix, shared by the
integrations whose matrices report it.

Whatever takes HDMI in (a display, a receiver, a switch's input) raises the
hot-plug line of the output it is cabled to once it is there and ready to
be read. So an output with nothing on it reads none, whatever it is switched
to, and the matrix confirms a switch of it all the same: the house's AVPro
confirmed every switch of the output the house file named for a room's TV
(2026-11-21) while that output read no hot-plug at any read and the TV was
cabled to another.

An integration whose matrix reports it defines hotplug_item() (the hook in
oh_integrations._hooks) as item(), and renders point() for each output,
linked to its Thing's channel or posted by its rule: ON when a read finds
the line raised, OFF when it finds none, at every read, changed or not, and
UNDEF when the matrix cannot be read. One read alone means little: a display
drops the line for a moment as it wakes or changes mode, and the house's
BK-808 reports an output disconnected for a single poll now and then. So the
routing (routing.js) takes a room's display for missing only once REPORTS
reads in a row, the last at least ABSENT seconds after the first, found no
hot-plug on the room's output while every device the room powers reported
on. It then shows the room's Source as unknown, and says why, instead of
the source the output is switched to, until a read finds the line again.
"""

from __future__ import annotations

import re

#: Reads in a row that find no hot-plug before a display counts as missing.
REPORTS = 2
#: Seconds, at least, from the first of those reads to the last.
ABSENT = 60


def item(device: dict, output: int) -> str:
    """The Switch Item reporting the hot-plug of the matrix's `output`."""
    return f"{device['id']}_output{output}_hotplug"


def owns(device: dict, name: str) -> bool:
    """Whether `name` is item()'s Item for one of the device's outputs."""
    return re.fullmatch(rf"{re.escape(device['id'])}_output[1-9][0-9]*_hotplug", name) is not None


def point(device: dict, output: int, channel: str | None = None) -> dict:
    """item()'s Item: read-only, autoupdate off, linked to `channel` (a
    Thing's channel UID) when a binding reads it rather than a rule."""
    entry: dict = {"type": "Switch", "label": f"Output {output} hot-plug", "icon": "video", "groups": [device["id"]],
                   "tags": ["Status"], "autoupdate": False,
                   "metadata": {"stateDescription": {"config": {"readOnly": True}}}}
    if channel is not None:
        entry["channel"] = channel
    return entry
