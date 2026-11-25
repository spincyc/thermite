"""Sony projectors over ADCP (Advanced Display Control Protocol), TCP 53595.

No binding speaks ADCP (openHAB's sonyprojector binding speaks PJ Talk on
53484, which the VPL-VW915ES has closed), so a rule does:
conf/automation/js/sony_adcp_<device>.js, sony_adcp.js beside this file with the
device table filled in. It keeps one ADCP connection open per projector,
and every 15 seconds, well inside the projector's 60-second connection
time-out, reads `power_status ?`, `input ?` and, while the projector is on,
`picture_mode ?` on it and posts them to the Items, changed or not, so a
reading stays fresh for the room policy. A device that also lists
sony_adcp under control has its power, input and key Items turned into ADCP
lines on that connection. A projector that does not answer, or
closes the connection while answering, gets UNDEF and is reconnected after
15 and 30 seconds, then every minute; a connection it closed or lost while
idle (its time-out, a standby, a power cycle) is replaced at once, and a
first request a projector waking from low-power standby leaves unanswered is
asked again.
The projector greets each connection with NOKEY when ADCP authentication is
off, as the example's is; the rule does not implement the password handshake
and reports a projector that asks for one.

Items, per device:

- `<id>_power` (Switch): ON while on or starting up, OFF in standby or
  cooling down; commanded ON / OFF under control.
- `<id>_power_state` (String): power_status as the projector reports it.
- `<id>_input` (String): the input shown (INPUTS); commanded with one under
  control.
- `<id>_picture_mode` (String): the picture mode, UNDEF in standby.
- `<id>_video_signal` (String): a read-only timing snapshot from `signal ?`,
  bracketed by actual input reads while on. A separate 30-second poll uses
  only the existing connection, with a one-second actor budget; it does
  not affect control health. Missing optional replies close the connection
  so a late untagged answer cannot satisfy a later control read.
- `<id>_key` (String, control only): a remote button id (KEYS) to press.
- `<id>_problem` and `<id>_sony_adcp_commands` (control only;
  oh_integrations._commands): why the projector did not take the rule's
  last command or cannot be read, as a code, and the rule's tally of its
  commands by outcome.

Under observe only, every Item is display-only and the rule listens to none
of them. The projector answers one request at a time, across controllers
too, and Control4 may drive it as well, so nothing else connects to it: the
rule holds oh_conf.RULE_SENDS, and the copy oh_render.read_only() makes for
the sandbox and the boot check, which run beside the live runtime, connects
to no projector; the metrics probe no port (PROBED).
"""

from __future__ import annotations

from pathlib import Path

from oh_conf import Addon, House, YamlFile, config as device_config, options_text, uses
from oh_shape import LIST, TEXT, Key
from .. import _commands, _script

NAME = "sony_adcp"
PHASE = "observe"
CONTROL = True
FILE = "yaml/projector.yaml"
HEADER = (
    "Sony projectors over ADCP (TCP 53595), with no binding: the rule",
    "conf/automation/js/sony_adcp_<device>.js polls each projector and posts these",
    "Items, and under control turns their commands into ADCP lines. Items",
    "are autoupdate off: they show only what the projector reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the projector rule (sony_adcp.js)"),)
ASSETS: dict[str, str] = {}
DEVICE_KEYS = {
    "inputs": Key(LIST, "inputs the projector offers (e.g. hdmi1, hdmi2); defaults to hdmi1 and hdmi2",
                  each=Key(TEXT, "an input name such as hdmi1")),
}
KINDS = {"projector"}
PORT = 53595
#: A TCP probe of PORT would cost the projector a session, reset with its greeting unread.
PROBED = False
#: The rule's own read of the projector's status says whether it answers.
READS = True
#: Seconds between polls of the projector (sony_adcp.js POLL).
POLL_S = 15
#: Seconds from a command to its read-back: none, the command's session
#: reads the projector again on the same connection.
READ_BACK = 0
RULE = "automation/js/sony_adcp.js"
SOURCE = Path(__file__).resolve().parent / "sony_adcp.js"
#: (value, label) of every input the remote offers: the VW915ES has two.
INPUTS = (("hdmi1", "HDMI 1"), ("hdmi2", "HDMI 2"))
READBACK_INPUTS = tuple(value for value, _ in INPUTS)
REMOTE_LAYOUT = "sony_projector"
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


_DEFAULT_INPUT_LABELS = dict(INPUTS)


def _device_inputs(device: dict) -> tuple[tuple[str, str], ...]:
    custom = device_config(device, NAME).get("inputs")
    if isinstance(custom, list) and custom and all(isinstance(v, str) and v for v in custom):
        out = []
        for value in custom:
            if value in _DEFAULT_INPUT_LABELS:
                label = _DEFAULT_INPUT_LABELS[value]
            elif value.startswith("hdmi") and value[4:].isdigit():
                label = f"HDMI {value[4:]}"
            else:
                label = value
            out.append((value, label))
        return tuple(out)
    return INPUTS


def validate(device: dict) -> list[str]:
    if not (uses(device, PHASE, NAME) or uses(device, "control", NAME)):
        return []
    where = device.get("id") or "a device"
    problems: list[str] = []
    if device.get("kind") not in KINDS:
        problems.append(f"{where}: {NAME} drives a projector, not a {device.get('kind')}")
    cfg = device_config(device, NAME)
    if "inputs" in cfg:
        custom = cfg["inputs"]
        if (not isinstance(custom, list) or not custom
                or any(not isinstance(v, str) or not v.strip() or v != v.strip() or "," in v or '"="' in v for v in custom)):
            problems.append(f"{where}.config.{NAME}.inputs: must be a non-empty list of input names")
        elif len(set(custom)) != len(custom):
            problems.append(f"{where}.config.{NAME}.inputs: must not repeat an input")
    return problems


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
    device_inputs = _device_inputs(device)
    out.items[f"{ident}_input"] = _item(ident, "String", "Input", ["Control" if control else "Status", "Channel"],
                                        control, options=options_text(device_inputs), icon="video")
    out.items[f"{ident}_picture_mode"] = _item(ident, "String", "Picture Mode", ["Status", "Mode"], False)
    out.items[f"{ident}_video_signal"] = _item(ident, "String", "Input video snapshot", ["Status"], False)
    if control:
        out.items[f"{ident}_key"] = {"type": "String", "label": "Remote Key", "groups": [ident],
                                     "tags": ["Control"], "autoupdate": False}
        _commands.render(device, out, NAME)


def diagnostics(device: dict) -> dict:
    """Selected-input timing from ADCP signal ?, not picture/HDR settings."""
    item = f"{device['id']}_video_signal"
    return {"kind": NAME, "items": [item], "maxAgeSeconds": 40, "readings": [
        {"category": "video", "media": "video", "label": "Input video timing", "kind": "format", "item": item,
         "path": ["values", "signal"], "unknown": ["Invalid"],
         "selector": {"path": ["values", "input"], "node": "input"}}]}


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, "control", NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, host, port and whether it is controlled."""
    return [{"id": d["id"], "host": d["ip"], "port": (d.get("timing") or {}).get("port", PORT),
             "control": uses(d, "control", NAME)} for d in devices]


def rule(devices: list[dict]) -> str:
    """sony_adcp.js with its tables filled in for these devices."""
    values = list(dict.fromkeys(v for d in devices for v, _ in _device_inputs(d))) if devices else [v for v, _ in INPUTS]
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices), "KEYS": KEYS, "INPUTS": values})


#: One script context per device; its polls and commands stay ordered.
rule_path, files = _script.per_device(RULE, rule)


def manual_items(device: dict) -> int:
    """Number of manual command Items handled by the projector's actor (power, input, key)."""
    return 3


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's fields (oh_integrations): power and input,
    driven by this integration's own rule. Its actor also takes manual
    commands to the key Item.

    Power's settle, in ms: how long an accepted `power "on"` may take to
    show in power_status and startup may last, and the same for
    `power "off"` and cooling. The VW915ES has stayed in standby for 6 s or
    more after an accepted `power "on"` and reported on by 21 s. A read that
    shows the value ends the wait early; startup and cooling1/cooling2 are
    read as transitions (sony_adcp.js routingProvider). A command declined
    or ignored during a transition the other way is busy for a tenth of
    the settle toward its end, then sent again."""
    ident = device["id"]
    feedback = {"kind": "poll", "interval_s": POLL_S}
    power_reports = {raw: {"value": "ON" if on else "OFF", "transition": raw in ("startup", "cooling1", "cooling2")}
                     for raw, (_, on) in POWER_STATES.items()}
    return {
        f"{ident}_power": {
            "kind": "adcp", "values": ["ON", "OFF"], "power": "absolute",
            "settle": {"ON": 60000, "OFF": 30000}, "feedback": feedback, "reports": power_reports,
        },
        f"{ident}_input": {
            "kind": "adcp", "values": [value for value, _ in _device_inputs(device)], "feedback": feedback,
        },
    }


def stage(device: dict, zone: int | None) -> dict | None:
    """The projector's power and input Items and its inputs, for a room
    whose display it is (oh_features.routing); it has no zones."""
    if zone is not None:
        return None
    return {"power": f"{device['id']}_power", "input": f"{device['id']}_input",
            "inputs": [v for v, _ in _device_inputs(device)]}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    return {
        "key": f"{ident}_key",
        "power": f"{ident}_power",
        "input": {"item": f"{ident}_input", "options": [list(pair) for pair in _device_inputs(device)]},
    }


def remote_layouts(device: dict | None = None) -> dict[str, dict]:
    """Button layout definition for ADCP projectors."""
    return {
        "sony_projector": {
            "label": "Projector remote",
            "corners": ["power", "input", "menu", None],
            "keys": ["picture", "aspect", "lens"],
        },
    }


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule talking to `device` at HOST:PORT instead."""
    host, _, port = address.rpartition(":")
    return _script.aimed(files, (rule_path(device), RULE), device,
                         lambda entry: entry.update(host=host, port=int(port)))
