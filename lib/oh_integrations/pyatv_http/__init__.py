"""Apple TVs through the Apple TV helper (bin/thermite apple-tv, lib/oh_atv.py).

openHAB has no Apple TV binding, and tvOS takes remote control only over
Apple's Companion protocol, an encrypted session that needs a HAP pairing
first, which pyatv speaks. So a small helper on the openHAB host keeps one
pyatv connection open per Apple TV and takes plain HTTP requests on
localhost only (HELPER) from the rule conf/automation/js/pyatv_http.js
(pyatv_http.js beside this file, with its tables filled in). Every POLL the
rule asks the helper for each Apple TV's state, which the helper answers at
once from what the Apple TV last pushed, and posts it; a command's answer
carries the state too, so it is read back at once.

Items, per Apple TV:

- `<id>_power` (Switch): ON while awake, OFF asleep, UNDEF while the helper
  or the Apple TV does not answer; ON and OFF wake it or put it to sleep.
- `<id>_key` (String): a remote button id: a Siri Remote button (PRESSES)
  or an app it opens (APPS); any other id is dropped.
- `<id>_pairing` and `<id>_pairing_status` (oh_integrations._pairing): REQUEST
  makes the Apple TV show four digits, and the digits sent back pair the
  helper with it. The helper keeps what the pairing gives it in its own
  secrets directory (atv/secrets/<id> in production's state,
  var/secrets/pyatv/<id> for a test instance), never in conf/ or Git, so a
  helper with an empty one pairs again.

The rule holds oh_conf.RULE_SENDS, so oh_render.read_only() makes it read
the Apple TVs and send them nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from oh_conf import RULE_SENDS, Addon, House, YamlFile, uses

from .. import _pairing

NAME = "pyatv_http"
PHASE = "control"
FILE = "yaml/appletv.yaml"
HEADER = (
    "Apple TVs through the Apple TV helper (bin/thermite apple-tv) on this host,",
    "which keeps one pyatv Companion connection per Apple TV: the rule",
    "conf/automation/js/pyatv_http.js asks it for their state and sends it",
    "their power, key and pairing commands. Items are autoupdate off: they",
    "show only what the helper reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the Apple TV rule (pyatv_http.js)"),)
ASSETS: dict[str, str] = {}
#: Where the rule reaches the helper: on the openHAB host itself, never the
#: LAN (bin/thermite apple-tv serve listens on 127.0.0.1 only).
HELPER = "http://127.0.0.1:8099"
#: Seconds from a command to its read-back: none, the helper answers it with the new state.
READ_BACK = 0
RULE = "automation/js/pyatv_http.js"
SOURCE = Path(__file__).resolve().parent / "pyatv_http.js"
#: The rule's markers: each line is replaced by its filled-in constant.
DEVICES_MARK = "const DEVICES = null;"
PRESSES_MARK = "const PRESSES = null;"
APPS_MARK = "const APPS = null;"
#: Remote button id (web/remote/remotes.js) -> the helper's key (a pyatv
#: RemoteControl method). power is an Item of its own, never a key. back is
#: the Siri Remote's Back (menu) button and tv its TV button (home).
PRESSES = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "ok": "select",
    "back": "menu",
    "tv": "home",
    "play_pause": "play_pause",
}
#: Remote button id -> the bundle id of the app it opens, as the Office
#: Apple TV lists them (pyatv app_list).
APPS = {
    "netflix": "com.netflix.Netflix",
    "youtube": "com.google.ios.youtube",
}
KEYS: dict[str, str] = {**PRESSES, **APPS}
#: The Pairing page's description of an Apple TV (oh_integrations._pairing).
PAIRING = {
    "label": "Apple TV",
    "length": 4,
    "chars": "digits",
    "again": True,
    "hint": ("No code? On the Apple TV open Settings, then AirPlay and HomeKit, stay on that screen and ask "
             "again. Still nothing: restart the Apple TV (Settings, System, Restart)."),
}


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("make") != "Apple":
        return [f"{device.get('id')}: {NAME} drives an Apple TV, not a {device.get('make')} {device.get('kind')}"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    out.items[f"{ident}_power"] = {"type": "Switch", "label": "Power", "icon": "screen", "groups": [ident],
                                   "tags": ["Control", "Power"], "autoupdate": False}
    out.items[f"{ident}_key"] = {"type": "String", "label": "Remote Key", "groups": [ident], "tags": ["Control"],
                                 "autoupdate": False}
    _pairing.render(device, house, out, NAME, **PAIRING)


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, address and the helper that drives it."""
    return [{"id": d["id"], "host": d["ip"], "helper": HELPER} for d in devices]


def _line(name: str, value: object) -> str:
    return f"const {name} = {json.dumps(value, separators=(',', ':'))};"


def rule(devices: list[dict]) -> str:
    """pyatv_http.js with its tables filled in for these devices."""
    source = SOURCE.read_text(encoding="utf-8")
    for mark in (DEVICES_MARK, PRESSES_MARK, APPS_MARK, RULE_SENDS):
        if source.split("\n").count(mark) != 1:
            raise ValueError(f"{SOURCE} must contain the line {mark!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[3])
    header = (f"// Generated by bin/thermite generate from {origin} and\n"
              "// instance/devices.yaml; do not edit.\n")
    return header + (source.replace(DEVICES_MARK, _line("DEVICES", table(devices)))
                     .replace(PRESSES_MARK, _line("PRESSES", PRESSES))
                     .replace(APPS_MARK, _line("APPS", APPS)))


def files(devices: list[dict], house: House) -> dict[str, str]:
    """The conf files rendered from all of this integration's devices."""
    return {RULE: rule(devices)}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, PHASE, NAME):
        return None
    ident = device["id"]
    return {"key": f"{ident}_key", "power": f"{ident}_power"}


def wake_item(device: dict) -> str | None:
    """The Item ON wakes the Apple TV with, when a room chooses it
    (oh_features.routing); None unless controlled."""
    return f"{device['id']}_power" if uses(device, PHASE, NAME) else None


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule asking the helper at HOST:PORT for `device`,
    as the Apple TV at 127.0.0.1 (a stand-in helper answers for any)."""
    prefix = "const DEVICES = "
    lines = files[RULE].split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    entries = json.loads(lines[index][len(prefix):-1])
    matching = [entry for entry in entries if entry["id"] == device["id"]]
    if not matching:
        raise ValueError(f"{RULE} has no device {device['id']}")
    for entry in matching:
        entry.update(host="127.0.0.1", helper=f"http://{address}")
    lines[index] = _line("DEVICES", entries)
    return {**files, RULE: "\n".join(lines)}
