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

Every 30 seconds the rule also asks a few of the manual's diagnostic reads
(`GET IN<n> VID FMT INF`, `GET IN<n> SIG STA`, `GET OUT<n> SIG STA`,
`GET OUT<n> HPD`) on that same connection, kept as unparsed text in
`<id>_signal`. An HPD reply, `OUT<n> HPD 1` or `OUT<n> HPD 0` as the
house's matrix gives it at every read, is also each output's hot-plug Item
(oh_integrations._hotplug): whether a display is cabled to that output. The
reads take turns, so each output's comes about every 3 to 4 minutes, and
its Item is UNDEF from a failed read of the routing until the next. The
house's matrix answers about half of them later than
150 ms. Closing the connection after each late answer made a new session
every 30 seconds, and the matrix locked up 7 hours later (2026-11-20). So
a late or missing answer ends that cycle's reads, and `GET OUT0 VS`, which the
matrix answers after anything asked before it, clears the late answer from the
connection. Only a matrix that answers neither loses the connection.

A command `reboot` to `<id>_reboot` restarts the matrix as its web page's
Reboot button does: `SET RBT` over telnet, which restarts its controller in
about 4 seconds with its routes kept, then its network module's own restart,
`POST /do` `{"cmd": "c s reboot"}` on WEB_PORT, after which ping, telnet and
the web page answer again in about 19 seconds. Neither `SET RST` nor the web
page's `c s restorefactory` is ever sent: each resets the matrix to its
factory settings. The rule leaves the matrix alone for 30 seconds and then
reads it again. The house's matrix locked up twice in two weeks: ping still
answered, but telnet closed at once and the web page reset every request,
until its power was cut. So the house file can have it restart each night
(oh_features.reboots).

Items, per matrix:

- `<id>_output<N>_input` (Number): the input routed to output N; commanded
  with an input number.
- `<id>_reboot` (String): commanded `reboot`, restarts the matrix.
- `<id>_output<N>_hotplug` (Switch, read-only): ON while output N reports a
  display's hot-plug, OFF while it reports none (hotplug_item()).
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
import re

from oh_conf import Addon, House, YamlFile, config as device_config, uses
from oh_shape import LIST, NUMBER, Key
from .. import _commands, _hotplug, _script

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
DEVICE_KEYS = {
    "ports": Key(LIST, "number of [inputs, outputs] when different from model detection or 4x4",
                 each=Key(NUMBER, "positive port count")),
}
KINDS = {"hdmi_matrix"}
PORT = 23
#: Its web server, which takes the network module's restart.
WEB_PORT = 80
#: A TCP probe of PORT would cost the matrix a session and a listing.
PROBED = False
#: The rule's own read, every 10 s, says whether the matrix answers.
READS = True
#: Seconds between those reads (avpro_telnet.js POLL).
POLL_S = 10
INPUTS = OUTPUTS = 4
_MODEL_PORTS = re.compile(r"AC-MX-(\d)(\d)", re.IGNORECASE)
READBACK_INPUTS = tuple(str(number) for number in range(1, INPUTS + 1))
#: All that the rule ever sends, named as lib/oh_matrix_sim's stand-in
#: records it: routing, restart and optional read-only diagnostic queries.
COMHEADS = frozenset({"get vs", "set vs", "set rbt", "get vid fmt inf", "get sig sta", "get hpd"})
#: What the reboot Item is commanded with (oh_features.reboots.COMMAND).
REBOOT = "reboot"
RULE = "automation/js/avpro_telnet.js"
SOURCE = Path(__file__).resolve().parent / "avpro_telnet.js"


def ports(device: dict) -> tuple[int, int]:
    """(inputs, outputs)."""
    custom = device_config(device, NAME).get("ports")
    if (isinstance(custom, list) and len(custom) == 2
            and all(type(n) is int and 1 <= n <= 32 for n in custom)):
        return custom[0], custom[1]
    model = device.get("model")
    if isinstance(model, str):
        match = _MODEL_PORTS.search(model)
        if match:
            ins, outs = int(match.group(1)), int(match.group(2))
            if ins >= 1 and outs >= 1:
                return ins, outs
    return INPUTS, OUTPUTS


def switch_item(device: dict, output: int) -> str:
    """The Item holding the input routed to `output`."""
    return f"{device['id']}_output{output}_input"


def hotplug_item(device: dict, output: int) -> str:
    """The Item reporting whether a display is on `output` (_hotplug)."""
    return _hotplug.item(device, output)


def reboot_item(device: dict) -> str:
    return f"{device['id']}_reboot"


def reboot(device: dict) -> dict:
    """Nightly reboots (oh_features.reboots): the matrix answers while its
    outputs read, and they are UNDEF from its answer to SET RBT until it is
    read again. Nothing it reports shows that an output is in use, so it has
    no idle readings of its own; oh_features.reboots adds the Off of each
    room that shows one of its outputs (`rooms`)."""
    _, outputs = ports(device)
    return {"item": reboot_item(device), "answers": [switch_item(device, output) for output in range(1, outputs + 1)],
            "idle": [], "rooms": True}


def manual_items(device: dict) -> int:
    """Number of manual command Items handled by the matrix's actor (outputs + reboot)."""
    _, outputs = ports(device)
    return outputs + 1


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's fields (oh_integrations): each output's
    input, driven by this integration's own rule. Each is independent: a
    SET names its one output, so a lost answer leaves the others as they
    were."""
    inputs, outputs = ports(device)
    feedback = {"kind": "poll", "interval_s": POLL_S}
    return {switch_item(device, output): {"kind": "avpro", "values": [str(n) for n in range(1, inputs + 1)],
                                          "independent": True, "feedback": feedback}
            for output in range(1, outputs + 1)}


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    problems: list[str] = []
    where = device.get("id") or "a device"
    if device.get("kind") not in KINDS:
        problems.append(f"{where}: {NAME} drives an hdmi_matrix, not a {device.get('kind')}")
    cfg = device_config(device, NAME)
    if "ports" in cfg:
        custom = cfg["ports"]
        if (not isinstance(custom, list) or len(custom) != 2
                or any(type(n) is not int or not 1 <= n <= 32 for n in custom)):
            problems.append(f"{where}.config.{NAME}.ports: must be [inputs, outputs] of integers 1-32")
    return problems


def render(device: dict, house: House, out: YamlFile) -> None:
    _, outputs = ports(device)
    out.items[f"{device['id']}_signal"] = {
        "type": "String", "label": "Native HDMI diagnostic replies", "groups": [device["id"]],
        "tags": ["Status"], "autoupdate": False,
        "metadata": {"stateDescription": {"config": {"readOnly": True}}},
    }
    for output in range(1, outputs + 1):
        out.items[switch_item(device, output)] = {
            "type": "Number",
            "label": f"Output {output} input",
            "icon": "video",
            "groups": [device["id"]],
            "tags": ["Control"],
            "autoupdate": False,
        }
        out.items[hotplug_item(device, output)] = _hotplug.point(device, output)
    out.items[reboot_item(device)] = {
        "type": "String",
        "label": "Reboot",
        "groups": [device["id"]],
        "tags": ["Control"],
        "autoupdate": False,
        "metadata": {"commandDescription": {"config": {"options": f"{REBOOT}=Reboot"}}},
    }
    _commands.render(device, out, NAME)


def diagnostics(device: dict) -> dict:
    """Bounded native replies; components are not decoded without wire evidence."""
    inputs, outputs = ports(device)
    item = f"{device['id']}_signal"
    readings = []
    for direction, count in (("input", inputs), ("output", outputs)):
        fields = (("format", "video format"), ("presence", "signal status")) if direction == "input" else (
            ("presence", "signal status"), ("hpd", "hot-plug status"))
        for port in range(1, count + 1):
            for key, label in fields:
                readings.append({"category": "processing", "kind": "metadata", "item": item,
                                 "label": f"{direction.title()} {port} {label} reply (unparsed)",
                                 "path": ["values", direction, str(port), key],
                                 "port": {"direction": direction, "number": port}})
    return {"kind": NAME, "items": [item], "maxAgeSeconds": 40, "readings": readings}


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, host, telnet and web ports, and the numbers of inputs and outputs."""
    out = []
    for d in devices:
        ins, outs = ports(d)
        port = (d.get("timing") or {}).get("port", PORT)
        out.append({"id": d["id"], "host": d["ip"], "port": port, "webPort": WEB_PORT, "inputs": ins, "outputs": outs})
    return out


def rule(devices: list[dict]) -> str:
    """avpro_telnet.js with its device table filled in."""
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices)})


#: One script context per device; its polls and commands stay ordered.
rule_path, files = _script.per_device(RULE, rule)


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule talking to `device` at HOST:PORT instead; a
    reboot's web request goes to that HOST:PORT too, never to the matrix."""
    host, _, port = address.rpartition(":")
    return _script.aimed(files, (rule_path(device), RULE), device,
                         lambda entry: entry.update(host=host, port=int(port), webPort=int(port)))
