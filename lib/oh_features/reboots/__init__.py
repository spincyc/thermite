"""Nightly reboots: thermite restarts the devices the house file asks it to,
one at a time, at one time of night.

A device opts in with `reboot: nightly`. Only one whose integration under
control can restart it without cutting its power can: that integration
provides reboot(device) (oh_integrations). The top-level `reboots: {time:
"HH:MM"}` sets when, on the 24-hour clock in openHAB's time zone (host.yaml
`timezone`); without it, DEFAULT_TIME. A time in the hour that daylight
saving time skips or repeats may run no reboot, or two, that night.

conf/automation/js/reboots.js, reboots.js beside this file with TABLE filled
in, restarts each nightly device in turn, from the screen back to the
sources (ORDER), so that each reads the EDID of what follows it once that
has restarted. It skips a device that does not answer, or that its idle
readings do not show idle: a receiver that is on, a switch whose input has
a signal. A room's source is not read: a desk left on its computer
overnight is idle while the computer sleeps, which only the switch sees. It
commands the device's reboot Item, then waits for it to stop answering and
answer again, up to 5 minutes, and pauses 2 minutes before the next.

The String Item house_reboot takes a device id as a command: that device
restarts now, past the same checks, whether or not it is nightly
(thermite reboot DEVICE). Its state is a JSON report of each device's last
reboot: when, its outcome, why, and how long it took to answer again.
Nothing cuts a device's power; a device that does not answer again is left
to the health checks' alerts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import oh_integrations as integrations
from oh_conf import Addon, Conf, House, uses

NAME = "reboots"
ITEM = "house_reboot"
ITEMS = "yaml/reboots.yaml"
RULE = "automation/js/reboots.js"
SOURCE = Path(__file__).resolve().parent / "reboots.js"
MARK = "const TABLE = null;"
HOUSE_KEYS = ("reboots",)
DEVICE_KEYS = ("reboot",)
HOUSE_ITEMS = (ITEM,)
ITEM_ONLY_RULES = (RULE,)
#: The one value a device's `reboot` takes.
NIGHTLY = "nightly"
DEFAULT_TIME = "03:00"
TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
#: What every reboot Item is commanded with.
COMMAND = "reboot"
#: Device kinds by when they restart: the screen first, then what is
#: between it and the sources, then the sources (every other kind).
ORDER = ({"tv", "projector", "monitor"}, {"avr", "soundbar", "streaming_amp", "amp"},
         {"hdmi_switch", "hdmi_processor", "hdmi_matrix"})
HEADER = (
    "Nightly reboots (lib/oh_features/reboots): a device id commanded to",
    "house_reboot restarts that device now; its state reports each device's",
    "last reboot (conf/automation/js/reboots.js).",
)


def provider(device: dict):
    """The integration under control that can restart the device, or None."""
    return next((module for module in integrations.REGISTRY
                 if callable(getattr(module, "reboot", None)) and uses(device, "control", module.NAME)), None)


def time_of(data: dict) -> str:
    return ((data.get("reboots") or {}).get("time")) or DEFAULT_TIME


def validate(data: dict) -> list[str]:
    problems = []
    setting = data.get("reboots")
    if setting is not None:
        if not isinstance(setting, dict) or set(setting) - {"time"}:
            problems.append("reboots: takes only time, such as {time: \"03:00\"}")
        elif "time" in setting and not (isinstance(setting["time"], str) and TIME.fullmatch(setting["time"])):
            problems.append(f"reboots.time: {setting['time']!r} is not a 24-hour HH:MM time, such as \"03:00\"")
    able = sorted(module.NAME for module in integrations.REGISTRY if callable(getattr(module, "reboot", None)))
    for device in data.get("devices") or []:
        if not isinstance(device, dict) or "reboot" not in device:
            continue
        where = device.get("id") or "a device"
        if device["reboot"] != NIGHTLY:
            problems.append(f"{where}.reboot: {device['reboot']!r} is not {NIGHTLY}")
        elif provider(device) is None:
            problems.append(f"{where}.reboot: none of its integrations under control can restart it; "
                            f"these can: {', '.join(able)}")
    return problems


def rank(device: dict) -> int:
    return next((index for index, kinds in enumerate(ORDER) if device.get("kind") in kinds), len(ORDER))


def entry(device: dict) -> dict:
    spec = provider(device).reboot(device)
    return {"id": device["id"], "nightly": device.get("reboot") == NIGHTLY,
            "item": spec["item"], "answers": list(spec["answers"]), "things": list(spec.get("things", ())),
            "signs": list(spec.get("signs", ())), "idle": [dict(check) for check in spec["idle"]]}


def table(house: House) -> dict:
    """The rule's TABLE: every device thermite can restart, in ORDER, then
    in house-file order."""
    able = sorted((device for device in house.devices if provider(device)), key=rank)
    hour, minute = TIME.fullmatch(time_of(house.data)).groups()
    return {"item": ITEM, "command": COMMAND, "time": time_of(house.data), "cron": f"0 {int(minute)} {int(hour)} * * ?",
            "devices": [entry(device) for device in able]}


def rule(house: House) -> str:
    source = SOURCE.read_text(encoding="utf-8")
    if source.count(MARK) != 1:
        raise ValueError(f"{SOURCE} must contain the line {MARK!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[3])
    header = (f"// Generated by bin/thermite generate from {origin} and\n"
              "// instance/devices.yaml; do not edit.\n")
    return header + source.replace(MARK, "const TABLE = " + json.dumps(table(house), indent=1) + ";")


def render(house: House, conf: Conf) -> None:
    if not any(provider(device) for device in house.devices):
        return
    out = conf.yaml_file(ITEMS, HEADER)
    out.items[ITEM] = {"type": "String", "label": "Reboot a device", "icon": "switch", "autoupdate": False}
    conf.files[RULE] = rule(house)
    conf.add_addon(Addon("automation", "jsscripting", "the nightly reboots rule (reboots.js)"))
