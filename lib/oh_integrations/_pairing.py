"""Pairing: the Items of a device that trusts openHAB only once a code shown
on its own screen is typed back, shared by the integrations whose devices
pair that way (androidtv_googletv, pyatv_http).

Each renders, per device it controls:

- `<id>_pairing` (String, autoupdate off): REQUEST makes the device show a
  code; a command matching the code (length and chars) is sent to the device
  as the code it shows. Its `pairing` metadata tells the Pairing page
  (web/remote/pair.html) about the device, and is how the page finds every
  device to pair: the value is the integration's NAME, the config holds
  device, label, model, room, order (the room's place in devices.yaml),
  status (the status Item), length and chars of the code, again (whether a
  paired device can be paired again), hint (what to do when no code
  appears) and, for a device whose pairing a new one replaces, confirm: what
  the page asks before pairing it again. Such a device is sent AGAIN rather
  than REQUEST once that is confirmed, so only a confirmed request replaces
  its pairing, and CANCEL while it pairs again goes back to the pairing it
  had (status `repairing` until it pairs or goes back). Optional result
  names an Item carrying a unique command acknowledgement and safe error
  details, when the integration provides one.
- `<id>_pairing_status` (String, display-only): one of STATES, as the
  integration's rule last saw the device.

The integration's rule turns the commands into the device's own pairing
steps and keeps the status; with SEND false (oh_render.read_only) it sends
nothing and only reports.
"""

from __future__ import annotations

import re

from oh_conf import House, options_text

REQUEST = "REQUEST"
AGAIN = "AGAIN"
CANCEL = "CANCEL"
#: Status -> its label (what the page and Main UI show).
STATES = {
    "paired": "Paired",
    "unpaired": "Not paired",
    "code_shown": "Showing a code",
    "repairing": "Pairing again",
    "checking": "Checking",
    "failed": "Not accepted",
    "closed": "Pairing port closed",
    "unreachable": "Not answering",
    "unavailable": "Cannot pair now",
}
#: The characters a code is made of: name -> regular expression class.
CHARS = {"digits": "0-9", "hex": "0-9A-Fa-f"}


def item(device: dict) -> str:
    return f"{device['id']}_pairing"


def status_item(device: dict) -> str:
    return f"{device['id']}_pairing_status"


def code_pattern(length: int, chars: str) -> re.Pattern:
    """What a code typed back must match."""
    return re.compile(f"[{CHARS[chars]}]{{{length}}}")


def spec(device: dict, house: House, integration: str, *, label: str, length: int, chars: str, again: bool,
         hint: str, confirm: str = "", result: str = "") -> dict:
    """The `pairing` metadata config of the device's pairing Item; confirm
    only when there is something to confirm."""
    if chars not in CHARS:
        raise ValueError(f"chars {chars!r} is not one of {', '.join(CHARS)}")
    room = device.get("room") or "unassigned"
    rooms = list(house.rooms)
    if confirm and not again:
        raise ValueError("confirm is what is asked before pairing again; this device is not paired again")
    config = {
        "device": device["id"],
        "integration": integration,
        "label": label,
        "model": device.get("model") or "",
        "room": house.room_label(room),
        "order": rooms.index(room) if room in rooms else len(rooms),
        "status": status_item(device),
        "length": length,
        "chars": chars,
        "again": again,
        "hint": hint,
    }
    if confirm:
        config["confirm"] = confirm
    if result:
        config["result"] = result
    return config


def render(device: dict, house: House, out, integration: str, **fields) -> None:
    """Add the device's pairing Items to out (an oh_conf.YamlFile)."""
    ident = device["id"]
    config = spec(device, house, integration, **fields)
    out.items[item(device)] = {
        "type": "String", "label": "Pairing", "icon": "lock", "groups": [ident], "tags": ["Control"],
        "autoupdate": False,
        "metadata": {"pairing": {"value": integration, "config": config}},
    }
    options = options_text(STATES.items())
    out.items[status_item(device)] = {
        "type": "String", "label": "Pairing Status", "icon": "lock", "groups": [ident], "tags": ["Status"],
        "autoupdate": False,
        "metadata": {"stateDescription": {"config": {"readOnly": True, "options": options}}},
    }
