"""Nightly reboots: thermite restarts the devices the house file asks it to,
one at a time, at one time of night, or when one hangs.

A device opts in with `reboot: nightly`, `reboot: when_hung`, or
`reboot: nightly_and_when_hung` for both. Only one whose integration under
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
a signal. A room's source is not read for those: a desk left on its
computer overnight is idle while the computer sleeps, which only the switch
sees. A USB matrix shows nothing of its use, and its restart drops every
keyboard and mouse on it, so it is idle only while each room whose device
ports it switches (routing `usb`) is Off; one no room lists cannot opt in,
and a port no room lists is not read. An HDMI matrix that reports nothing
of its use (avpro_telnet) is idle likewise only while each room that shows
one of its outputs (routing `hdmi_switch`) is Off; an output no room lists
is not read. It
commands the device's reboot Item, then waits for it to stop answering and
answer again, up to 5 minutes, and pauses 2 minutes before the next. A
device with a console (a USB matrix, whose network module keeps answering
while its controller restarts in half a second) is back instead once its
console says it started and it still answers a poll later: the rule
listens there from before the command, sending nothing, and skips the
device if it cannot.

The String Item house_reboot takes a device id as a command: that device
restarts now, past the same checks, whether or not it is nightly
(thermite reboot DEVICE). Its state is a JSON report of each device's last
reboot: when, its outcome, why, and how long it took to answer again.
Nothing cuts a device's power; a device that does not answer again is left
to the health checks' alerts.

A device `when_hung` restarts by itself once part of it has hung while the
rest still answers: its reboot() spec names `hung`, the Things that go
OFFLINE then and for how long they must stay so (a VRROOM's web API, five
missed polls, while telnet still answers). The rule looks each minute.
Only while each room that shows it (routing `hdmi_switch`) or whose ports
it switches (`usb`) is Off, so a restart never blanks a room in use; one
no room lists cannot opt in. A device restarted within HUNG_GAP_MS for any
reason is left alone, so one a restart does not cure restarts at most
hourly; its report says `by: hung`.
"""

from __future__ import annotations

import re
from pathlib import Path

import oh_integrations as integrations
from oh_conf import RULE_SENDS, Addon, Conf, House, fill, indented, uses
from oh_shape import MAPPING, TEXT, Key
from oh_integrations import _io

from .. import routing

NAME = "reboots"
ITEM = "house_reboot"
ITEMS = "yaml/reboots.yaml"
RULE = "automation/js/reboots.js"
SOURCE = Path(__file__).resolve().parent / "reboots.js"
HOUSE_KEYS = {"reboots": Key(MAPPING, "when the devices marked reboot: nightly restart", keys={
    "time": Key(TEXT, "the time of day, as 24-hour HH:MM in quotes; 03:00 when left out")})}
#: The values a device's `reboot` takes.
NIGHTLY = "nightly"
WHEN_HUNG = "when_hung"
BOTH = "nightly_and_when_hung"
CHOICES = (NIGHTLY, WHEN_HUNG, BOTH)
DEVICE_KEYS = {"reboot": Key(TEXT, "when thermite restarts it, while it is idle: each night, once part of it "
                             "has hung while the rest answers, or both", choices=CHOICES)}
HOUSE_ITEMS = (ITEM,)
DEFAULT_TIME = "03:00"
TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
#: What every reboot Item is commanded with.
COMMAND = "reboot"
#: Device kinds by when they restart: the screen first, then what is
#: between it and the sources, then the sources (every other kind).
ORDER = ({"tv", "projector", "monitor"}, {"avr", "soundbar", "streaming_amp", "audio_streamer", "amp"},
         {"hdmi_switch", "hdmi_processor", "hdmi_matrix"})
HEADER = (
    "Nightly reboots (lib/oh_features/reboots): a device id commanded to",
    "house_reboot restarts that device now; its state reports each device's",
    "last reboot (conf/automation/js/reboots.js).",
)


def provider(device: dict):
    """The integration under control that can restart the device, or None."""
    return next((module for module in integrations.defining("reboot") if uses(device, "control", module.NAME)), None)


def nightly(device: dict) -> bool:
    return device.get("reboot") in (NIGHTLY, BOTH)


def when_hung(device: dict) -> bool:
    return device.get("reboot") in (WHEN_HUNG, BOTH)


#: What shows a device idle for a restart when hung: its rooms' Off.
HUNG_ROOMS = {"rooms": True}


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
    able = sorted(module.NAME for module in integrations.defining("reboot"))
    for device in data.get("devices") or []:
        if not isinstance(device, dict) or "reboot" not in device:
            continue
        where = device.get("id") or "a device"
        if device["reboot"] not in CHOICES:
            problems.append(f"{where}.reboot: {device['reboot']!r} is not {', '.join(CHOICES[:-1])} or {CHOICES[-1]}")
        elif provider(device) is None:
            problems.append(f"{where}.reboot: none of its integrations under control can restart it; "
                            f"these can: {', '.join(able)}")
        elif not device.get("id"):
            continue
        elif when_hung(device) and "hung" not in (spec := provider(device).reboot(device)):
            problems.append(f"{where}.reboot: {provider(device).NAME} cannot tell when it hangs, "
                            f"so it can only be {NIGHTLY}")
        elif when_hung(device) and not rooms_off(data, device, HUNG_ROOMS):
            problems.append(f"{where}.reboot: nothing would show it idle; a device restarts when hung only while "
                            "the rooms that show it (routing.rooms.<room>.hdmi_switch) or whose ports it switches "
                            "(routing.rooms.<room>.usb) are Off, and no room lists it")
        elif nightly(device) and not (spec := provider(device).reboot(device))["idle"] \
                and not rooms_off(data, device, spec):
            problems.append(f"{where}.reboot: nothing would show it idle; " + (
                "an HDMI matrix restarts nightly only while the rooms that show its outputs "
                "(routing.rooms.<room>.hdmi_switch) are Off" if spec.get("rooms") else
                "a USB matrix restarts nightly only while the rooms whose ports it switches "
                "(routing.rooms.<room>.usb) are Off") + ", and no room lists it")
    return problems


def rank(device: dict) -> int:
    return next((index for index, kinds in enumerate(ORDER) if device.get("kind") in kinds), len(ORDER))


def rooms_off(data: dict, device: dict, spec: dict | None = None) -> list[dict]:
    """Idle readings for a matrix with none of its own: each room whose
    device ports it switches (routing `usb`) is Off, and, when its reboot()
    `spec` says `rooms`, each room that shows one of its outputs (routing
    `hdmi_switch`). Ports and outputs no room lists are not read."""
    routed = data.get("routing")
    rooms = routed.get("rooms") if isinstance(routed, dict) else None
    shown = bool(spec and spec.get("rooms"))

    def serves(room: dict) -> bool:
        usb, switch = room.get("usb"), room.get("hdmi_switch")
        return (isinstance(usb, dict) and device["id"] in usb) or \
            (shown and isinstance(switch, dict) and switch.get("device") == device["id"])
    return [{"item": routing.item_name(room), "is": routing.OFF}
            for room, entry in (rooms if isinstance(rooms, dict) else {}).items()
            if isinstance(entry, dict) and serves(entry)]


def entry(house: House, device: dict) -> dict:
    spec = provider(device).reboot(device)
    hung = {"hung": {"things": list(spec["hung"]["things"]), "seconds": spec["hung"]["seconds"],
                     "idle": rooms_off(house.data, device, HUNG_ROOMS)}} if when_hung(device) and "hung" in spec else {}
    return {"id": device["id"], "nightly": nightly(device),
            "item": spec["item"], "answers": list(spec["answers"]), "things": list(spec.get("things", ())),
            "signs": list(spec.get("signs", ())),
            "idle": [dict(check) for check in spec["idle"]] + rooms_off(house.data, device, spec),
            **({"console": dict(spec["console"])} if "console" in spec else {}), **hung}


def table(house: House) -> dict:
    """The rule's TABLE: every device thermite can restart, in ORDER, then
    in house-file order."""
    able = sorted((device for device in house.devices if provider(device)), key=rank)
    hour, minute = TIME.fullmatch(time_of(house.data)).groups()
    return {"item": ITEM, "command": COMMAND, "time": time_of(house.data), "cron": f"0 {int(minute)} {int(hour)} * * ?",
            "devices": [entry(house, device) for device in able]}


def rule(house: House) -> str:
    return fill(_io.embed(SOURCE.read_text(encoding="utf-8")), {"TABLE": table(house)}, SOURCE,
                keep=(RULE_SENDS,), encode=indented)


def render(house: House, conf: Conf) -> None:
    if not any(provider(device) for device in house.devices):
        return
    out = conf.yaml_file(ITEMS, HEADER)
    out.items[ITEM] = {"type": "String", "label": "Reboot a device", "icon": "switch", "autoupdate": False}
    conf.files[RULE] = rule(house)
    conf.add_addon(Addon("automation", "jsscripting", "the nightly reboots rule (reboots.js)"))
