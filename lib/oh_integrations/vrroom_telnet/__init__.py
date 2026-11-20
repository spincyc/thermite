"""HDFury VRROOM HDMI switches over telnet, TCP 2222.

The VRROOM takes one lowercase ASCII command per line on its telnet port,
with no login, and answers each command it knows with one line: `get insel`
with `insel <tx0> <tx1>`, the input (0-3, the VRROOM's own numbers) each
output shows, 4 for an output copying the other. A command it does not know
gets no answer at all. With its ipinterrupt setting on, it also says `int`
on the connection whenever a stream changes. No binding speaks it, so a
rule does: conf/automation/js/vrroom_telnet_<device>.js, vrroom_telnet.js
beside this file with the device table filled in. It keeps one connection
open per switch, turns ipinterrupt on when a new connection finds it off,
every 10 seconds asks `get insel` and posts TX0's input, changed or not,
since the room policy worker counts a reading not updated for 30 seconds as
unknown, and asks again within a second of an `int`. A command to the input
Item is sent as `set insel <n> 4` (TX1 a copy of TX0, as it always is in
splitter mode), whose answer it waits for before reading back: the new
`insel` line, or `insel err` when the switch refuses it. A
switch that does not answer, or closes the connection, gets UNDEF and is
reconnected after 10, 20 and 40 seconds, then every minute.

Items, per switch:

- `<id>_input` (Number): the input TX0 shows; commanded with an input
  number. A room's HDMI switch in the routing (oh_features.routing) is
  this Item (INPUTS, input_item()), which the routing executor drives
  through the rule (fields()).
- `<id>_reboot` (String): commanded `reboot`, sends `set reboot`, logged.
  The VRROOM restarts without losing its settings and answers again in
  about 15 s, with no power cycle. It is the cure when its web server
  hangs (resets every HTTP request) while telnet still answers. Only a
  switch with `reboot: nightly` restarts unasked, at night while its input
  has no signal (reboot(), oh_features.reboots), which vrroom_http reports.
- `<id>_problem` and `<id>_vrroom_telnet_commands` (oh_integrations._commands): why
  the switch did not take the rule's last command or cannot be read, as a
  code, and the rule's tally of its commands by outcome.

The wording of the answers (`insel %d %d`, `insel err`, `ipinterrupt on`,
`ipinterrupt err`, `reboot`) comes from strings in the VRROOM's firmware,
not from a recorded session: beyond `get insel` answered `insel 1 4`, no
answer has been recorded word for word. So the rule takes an answer only by its pattern, skips any
other line while it waits, and counts no answer by the deadline as unknown.

The read-only `<id>_telnet_signal` snapshot collects the documented native
video/audio status strings on the same connection. Its separate bounded
poll never opens a connection or changes routing health. The web API
(vrroom_http, under observe) supplies additional +5 V, hot-plug and eARC
observations when it is available.

Its command list sends notices to "the current open IP connection", one, so
nothing else connects to it: the rule holds oh_conf.RULE_SENDS, and the
copy oh_render.read_only() makes for the sandbox and the boot check, which
run beside the live runtime, connects to no switch; the metrics probe no
port (PROBED). `thermite sandbox start --live` holds a second session.
"""

from __future__ import annotations

from pathlib import Path

from oh_conf import Addon, House, YamlFile, uses
from .. import _commands, _script, vrroom_http

NAME = "vrroom_telnet"
PHASE = "control"
FILE = "yaml/vrroom_telnet.yaml"
HEADER = (
    "HDFury VRROOM HDMI switches over telnet (TCP 2222), with no binding: the",
    "rule conf/automation/js/vrroom_telnet_<device>.js polls each switch's input",
    "and posts these Items, and turns their commands into set lines. Items are",
    "autoupdate off: they show only what the switch reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the HDMI switch rule (vrroom_telnet.js)"),)
ASSETS: dict[str, str] = {}
KINDS = {"hdmi_switch"}
PORT = 2222
#: The command list names one "current open IP connection" for its notices;
#: a TCP probe of PORT could take that session.
PROBED = False
#: The rule's own read of the input says whether the switch answers.
READS = True
#: The input numbers TX0 can show (RX0-RX3).
INPUTS = range(0, 4)
READBACK_INPUTS = tuple(str(number) for number in INPUTS)
#: Seconds between polls (vrroom_telnet.js POLL); a change notice is read
#: within a second.
POLL_S = 10
#: All that the rule ever sends, named as lib/oh_matrix_sim's stand-in
#: records it: routing, setup/reboot and optional native status reads.
COMHEADS = frozenset({"get insel", "set insel", "get ipinterrupt", "set ipinterrupt", "set reboot", "get status", "get opmode"})
#: What a new connection sends to set itself up: it moves no input.
SETUP_COMHEADS = frozenset({"get ipinterrupt", "set ipinterrupt"})
#: What the reboot Item is commanded with.
REBOOT = "reboot"
RULE = "automation/js/vrroom_telnet.js"
SOURCE = Path(__file__).resolve().parent / "vrroom_telnet.js"


def diagnostics(device: dict) -> dict:
    """FW 0.63 native status: RX paths are processing paths, not sockets."""
    item = f"{device['id']}_telnet_signal"
    readings = []
    for output in range(2):
        selector = {"path": ["values", f"portseltx{output}"], "node": "input"}
        for category, label, key, kind in (
                ("video", "input video", f"RX{output}", "format"),
                ("video", "output video", f"TX{output}", "format"),
                ("audio", "output audio", f"AUD{output}", "format"),
                ("edid", "sink capabilities", f"SINK{output}", "capability"),
                ("processing", "source name", f"SPD{output}", "metadata")):
            entry: dict = {"category": category, "label": f"TX{output} {label}", "item": item,
                           "path": ["values", key], "kind": kind,
                           "port": {"direction": "output", "number": output}}
            if key.startswith(("TX", "AUD", "SINK")):
                entry["transports"] = ["hdmi"]
            if category in {"video", "audio"}:
                entry["media"] = category
            if kind in {"format", "metadata"}:
                entry["selector"] = selector
                entry["noSignal"] = ["no signal"]
                entry["unknown"] = ["no info", "n/a"]
            readings.append(entry)
    # AUDOUT can be sourced from eARC. Without its source selector it is a
    # device report, not evidence for the active HDMI/optical signal path.
    readings.append({"category": "processing", "label": "HDMI audio output report", "item": item,
                     "path": ["values", "AUDOUT"], "kind": "metadata", "noSignal": ["no signal"],
                     "transports": ["hdmi"], "unknown": ["no info", "n/a"]})
    return {"kind": NAME, "items": [item], "maxAgeSeconds": 40, "readings": readings}


def input_item(device: dict) -> str:
    """The Item holding the input TX0 shows."""
    return f"{device['id']}_input"


def reboot_item(device: dict) -> str:
    return f"{device['id']}_reboot"


def reboot(device: dict) -> dict:
    """Nightly reboots (oh_features.reboots): the switch answers while its
    input reads, and is idle while the input TX0 shows has no signal, as
    its web page (vrroom_http) reports it. A web page that does not answer
    while telnet does is the VRROOM's hang that a restart cures, so its
    missing report does not hold the restart back."""
    return {"item": reboot_item(device), "answers": [input_item(device)],
            "idle": [{"item": vrroom_http.signal_item(device), "field": "RX0", "is": "no signal",
                      "maxAge": vrroom_http.MAX_AGE, "unreadIsIdle": True}]}


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's field (oh_integrations): TX0's input, driven by
    this integration's own rule. Its actor also takes the reboot Item's
    commands."""
    return {input_item(device): {"kind": "vrroom", "values": [str(n) for n in INPUTS], "manual": 2}}


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    if device.get("kind") not in KINDS:
        return [f"{device.get('id')}: {NAME} drives an hdmi_switch, not a {device.get('kind')}"]
    if "reboot" in device and not uses(device, vrroom_http.PHASE, vrroom_http.NAME):
        return [f"{device.get('id')}.reboot: needs {vrroom_http.NAME} under {vrroom_http.PHASE}, "
                "which tells when the switch's input has no signal"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    out.items[f"{device['id']}_telnet_signal"] = {
        "type": "String", "label": "Native HDMI signal snapshot", "groups": [device["id"]],
        "tags": ["Status"], "autoupdate": False,
        "metadata": {"stateDescription": {"config": {"readOnly": True}}},
    }
    out.items[input_item(device)] = {
        "type": "Number",
        "label": "TX0 input",
        "icon": "video",
        "groups": [device["id"]],
        "tags": ["Control"],
        "autoupdate": False,
    }
    out.items[reboot_item(device)] = {
        "type": "String",
        "label": "Reboot",
        "groups": [device["id"]],
        "tags": ["Control"],
        "autoupdate": False,
        "metadata": {"commandDescription": {"config": {"options": f"{REBOOT}=Reboot"}}},
    }
    _commands.render(device, out, NAME)


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, host, port and the number of inputs."""
    return [{"id": d["id"], "host": d["ip"], "port": PORT, "inputs": len(INPUTS)} for d in devices]


def rule(devices: list[dict]) -> str:
    """vrroom_telnet.js with its device table filled in."""
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices)})


#: One script context per device; its polls and commands stay ordered.
rule_path, files = _script.per_device(RULE, rule)


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule talking to `device` at HOST:PORT instead."""
    host, _, port = address.rpartition(":")
    return _script.aimed(files, (rule_path(device),), device, lambda entry: entry.update(host=host, port=int(port)))
