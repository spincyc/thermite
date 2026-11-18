"""AVPro Edge HDMI matrices (the AC-MX-44X) over telnet, TCP 23.

The matrix takes one CRLF-terminated ASCII line per request on its telnet
port, with no login while its telnet login is off, as the example's
is. `GET OUT0 VS` answers one `OUT<n> VS IN<m>` line per output, and `SET
OUT<n> VS IN<m>` routes input m to output n: the line the matrix's own web
page sends to switch. On connect it lists every output's input and other
settings unasked. No binding speaks it, so a rule does:
conf/automation/js/avpro_telnet_<device>.js, avpro_telnet.js beside this file with
the device table filled in. It keeps one connection open per matrix, as
AVPro's own control drivers do: it waits for that listing to end, and every
10 seconds asks `GET OUT0 VS` and posts each output's input, changed or not,
since the room policy worker counts a reading not updated for 30 seconds as
unknown. A command to an output's Item is sent as SET on that connection,
followed by the same read. A matrix that does not answer, or closes the connection, gets UNDEF
and is reconnected after 10, 20 and 40 seconds, then every minute.

Items, per matrix:

- `<id>_output<N>_input` (Number): the input routed to output N; commanded
  with an input number.
- `<id>_problem` and `<id>_avpro_telnet_commands` (oh_integrations._commands): why
  the matrix did not take the rule's last command or cannot be read, as a
  code, and the rule's tally of its commands by outcome.

A room's HDMI switch in the routing (oh_features.routing) may be one of its
outputs, as the example TV's is (ports(), switch_item()). The matrix
serves few telnet sessions, and Control4 may keep one, so nothing else
connects to it: the rule holds oh_conf.RULE_SENDS, and the copy
oh_render.read_only() makes for the sandbox and the boot check, which run
beside the live runtime, connects to no matrix; the metrics probe no port
(PROBED). `thermite sandbox start --live` holds a second session.
"""

from __future__ import annotations

from pathlib import Path

from oh_conf import Addon, House, YamlFile, uses
from .. import _commands, _script

NAME = "avpro_telnet"
PHASE = "control"
FILE = "yaml/avpro.yaml"
HEADER = (
    "AVPro Edge HDMI matrices over telnet (TCP 23), with no binding: the rule",
    "conf/automation/js/avpro_telnet_<device>.js polls each matrix's routing and",
    "posts these Items, and turns their commands into SET lines. Items are",
    "autoupdate off: they show only what the matrix reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the HDMI matrix rule (avpro_telnet.js)"),)
ASSETS: dict[str, str] = {}
KINDS = {"hdmi_matrix"}
PORT = 23
#: A TCP probe of PORT would cost the matrix a session and a listing.
PROBED = False
#: The rule's own read, every 10 s, says whether the matrix answers.
READS = True
INPUTS = OUTPUTS = 4
READBACK_INPUTS = tuple(str(number) for number in range(1, INPUTS + 1))
#: All that the rule ever sends, named as lib/oh_matrix_sim's stand-in
#: records it: the routing read and the switch.
COMHEADS = frozenset({"get vs", "set vs"})
RULE = "automation/js/avpro_telnet.js"
SOURCE = Path(__file__).resolve().parent / "avpro_telnet.js"


def ports(device: dict) -> tuple[int, int]:
    """(inputs, outputs)."""
    return INPUTS, OUTPUTS


def switch_item(device: dict, output: int) -> str:
    """The Item holding the input routed to `output`."""
    return f"{device['id']}_output{output}_input"


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's fields (oh_integrations): each output's
    input, driven by this integration's own rule. Each is independent: a
    SET names its one output, so a lost answer leaves the others as they
    were."""
    inputs, outputs = ports(device)
    return {switch_item(device, output): {"kind": "avpro", "values": [str(n) for n in range(1, inputs + 1)],
                                          "independent": True}
            for output in range(1, outputs + 1)}


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("kind") not in KINDS:
        return [f"{device.get('id')}: {NAME} drives an hdmi_matrix, not a {device.get('kind')}"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    for output in range(1, OUTPUTS + 1):
        out.items[switch_item(device, output)] = {
            "type": "Number",
            "label": f"Output {output} input",
            "icon": "video",
            "groups": [device["id"]],
            "tags": ["Control"],
            "autoupdate": False,
        }
    _commands.render(device, out, NAME)


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, host, port and the numbers of inputs and outputs."""
    return [{"id": d["id"], "host": d["ip"], "port": PORT, "inputs": INPUTS, "outputs": OUTPUTS} for d in devices]


def rule(devices: list[dict]) -> str:
    """avpro_telnet.js with its device table filled in."""
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices)})


#: One script context per device; its polls and commands stay ordered.
rule_path, files = _script.per_device(RULE, rule)


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule talking to `device` at HOST:PORT instead."""
    host, _, port = address.rpartition(":")
    return _script.aimed(files, (rule_path(device), RULE), device,
                         lambda entry: entry.update(host=host, port=int(port)))
