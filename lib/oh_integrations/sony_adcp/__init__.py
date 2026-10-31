"Sony projectors over ADCP (Advanced Display Control Protocol), TCP 53595.\n\nNo binding speaks ADCP (openHAB's sonyprojector binding speaks PJ Talk on\n53484, which the VPL-VW915ES has closed), so a rule does:\nconf/automation/js/sony_adcp.js, sony_adcp.js beside this file with the\ndevice table filled in. Every 15 seconds it opens one short connection\nper projector, reads `power_status ?`, `input ?` and, while the projector is\non, `picture_mode ?`, and posts them to the Items; a projector that does not\nanswer gets UNDEF. A device that also lists sony_adcp under control has its\npower, input and key Items turned into ADCP lines (LINES) on a connection of\ntheir own. The projector greets with NOKEY when ADCP authentication is off,\nas the example's is; the rule does not implement the password handshake and\nreports a projector that asks for one.\n\nItems, per device:\n\n- `<id>_power` (Switch): ON while on or starting up, OFF in standby or\n  cooling down; commanded ON / OFF under control.\n- `<id>_power_state` (String): power_status as the projector reports it.\n- `<id>_input` (String): the input shown (INPUTS); commanded with one under\n  control.\n- `<id>_picture_mode` (String): the picture mode, UNDEF in standby.\n- `<id>_key` (String, control only): a remote button id (KEYS) to press.\n\nUnder observe only, every Item is display-only and the rule listens to none\nof them. The rule holds oh_conf.RULE_SENDS, so oh_render.read_only() makes it\nread the projectors and send them nothing.\n"

from __future__ import annotations

import json
from pathlib import Path

from oh_conf import RULE_SENDS, Addon, House, YamlFile, options_text, uses

NAME = "sony_adcp"
PHASE = "observe"
CONTROL = True
FILE = "yaml/projector.yaml"
HEADER = (
    "Sony projectors over ADCP (TCP 53595), with no binding: the rule",
    "conf/automation/js/sony_adcp.js polls each projector and posts these",
    "Items, and under control turns their commands into ADCP lines. Items",
    "are autoupdate off: they show only what the projector reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the projector rule (sony_adcp.js)"),)
ASSETS: dict[str, str] = {}
KINDS = {"projector"}
PORT = 53595
#: Seconds from a command to its read-back: none, the command's own
#: connection reads the projector again.
READ_BACK = 0
RULE = "automation/js/sony_adcp.js"
SOURCE = Path(__file__).resolve().parent / "sony_adcp.js"
#: The rule's markers: each line is replaced by its filled-in constant.
DEVICES_MARK = "const DEVICES = null;"
KEYS_MARK = "const KEYS = null;"
INPUTS_MARK = "const INPUTS = null;"
#: (value, label) of every input the remote offers: the VW915ES has two.
INPUTS = (("hdmi1", "HDMI 1"), ("hdmi2", "HDMI 2"))
#: power_status values, their label, and whether the power Switch is ON.
POWER_STATES = {
    "standby": ("Standby", False),
    "startup": ("Starting", True),
    "on": ("On", True),
    "cooling1": ("Cooling", False),
    "cooling2": ("Cooling", False),
}
#: Remote button id (web/remote/remotes.js) -> ADCP key code, each one the
#: VW915ES lists for `key ? --info`. power and input are Items of their own,
#: never keys; any other id is dropped. lens is the remote's POSITION button,
#: which recalls a stored picture position; lens_control would open focus,
#: zoom and shift, where the arrow keys move the lens itself.
KEYS = {
    "menu": "menu",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "ok": "enter",
    "picture": "picmode",
    "aspect": "aspect",
    "lens": "lens_position",
}
#: The lines a command sends: the power Switch, an input, a key.
LINES = {
    "power": {"ON": 'power "on"', "OFF": 'power "off"'},
    "input": {value: f'input "{value}"' for value, _ in INPUTS},
    "key": {button: f'key "{code}"' for button, code in KEYS.items()},
}


def validate(device: dict) -> list[str]:
    if (uses(device, PHASE, NAME) or uses(device, "control", NAME)) and device.get("kind") not in KINDS:
        return [f"{device.get('id')}: {NAME} drives a projector, not a {device.get('kind')}"]
    return []


def _item(ident: str, type_: str, label: str, tags: list[str], control: bool, *,
          options: str | None = None, icon: str | None = None) -> dict:
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[ident], tags=tags, autoupdate=False)
    state: dict = {}
    if options:
        state["options"] = options
    if not control:
        state["readOnly"] = True
    metadata: dict = {}
    if state:
        metadata["stateDescription"] = {"config": state}
    if control and options:
        metadata["commandDescription"] = {"config": {"options": options}}
    if metadata:
        item["metadata"] = metadata
    return item


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    control = uses(device, "control", NAME)
    states = options_text((value, label) for value, (label, _) in POWER_STATES.items())
    out.items[f"{ident}_power"] = _item(ident, "Switch", "Power", ["Control" if control else "Status", "Power"],
                                        control, icon="projector")
    out.items[f"{ident}_power_state"] = _item(ident, "String", "Power State", ["Status", "Power"], False,
                                              options=states)
    out.items[f"{ident}_input"] = _item(ident, "String", "Input", ["Control" if control else "Status", "Channel"],
                                        control, options=options_text(INPUTS), icon="video")
    out.items[f"{ident}_picture_mode"] = _item(ident, "String", "Picture Mode", ["Status", "Mode"], False)
    if control:
        out.items[f"{ident}_key"] = {"type": "String", "label": "Remote Key", "groups": [ident],
                                     "tags": ["Control"], "autoupdate": False}


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, host, port and whether it is controlled."""
    return [{"id": d["id"], "host": d["ip"], "port": PORT, "control": uses(d, "control", NAME)} for d in devices]


def _line(name: str, value: object) -> str:
    return f"const {name} = {json.dumps(value, separators=(',', ':'))};"


def rule(devices: list[dict]) -> str:
    """sony_adcp.js with its tables filled in for these devices."""
    source = SOURCE.read_text(encoding="utf-8")
    for mark in (DEVICES_MARK, KEYS_MARK, INPUTS_MARK):
        if source.split("\n").count(mark) != 1:
            raise ValueError(f"{SOURCE} must contain the line {mark!r} exactly once")
    if source.split("\n").count(RULE_SENDS) != 1:
        raise ValueError(f"{SOURCE} must contain the line {RULE_SENDS!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[3])
    header = (f"// Generated by bin/thermite generate from {origin} and\n"
              "// instance/devices.yaml; do not edit.\n")
    filled = (source.replace(DEVICES_MARK, _line("DEVICES", table(devices)))
              .replace(KEYS_MARK, _line("KEYS", KEYS))
              .replace(INPUTS_MARK, _line("INPUTS", [value for value, _ in INPUTS])))
    return header + filled


def files(devices: list[dict], house: House) -> dict[str, str]:
    """The conf files rendered from all of this integration's devices."""
    return {RULE: rule(devices)}


def stage(device: dict, zone: int | None) -> dict | None:
    """The projector's power and input Items and its inputs, for a room
    whose display it is (oh_features.routing); it has no zones."""
    if zone is not None:
        return None
    return {"power": f"{device['id']}_power", "input": f"{device['id']}_input", "inputs": [v for v, _ in INPUTS]}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    return {
        "key": f"{ident}_key",
        "power": f"{ident}_power",
        "input": {"item": f"{ident}_input", "options": [list(pair) for pair in INPUTS]},
    }


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule talking to `device` at HOST:PORT instead."""
    host, _, port = address.rpartition(":")
    prefix = "const DEVICES = "
    lines = files[RULE].split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    entries = json.loads(lines[index][len(prefix):-1])
    matching = [entry for entry in entries if entry["id"] == device["id"]]
    if not matching:
        raise ValueError(f"{RULE} has no device {device['id']}")
    for entry in matching:
        entry.update(host=host, port=int(port))
    lines[index] = _line("DEVICES", entries)
    return {**files, RULE: "\n".join(lines)}
