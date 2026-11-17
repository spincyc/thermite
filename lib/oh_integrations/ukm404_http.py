"""4x4 USB 3.2 Gen 1 matrix (OREI UKM-404 family) over its JSON API.

One Number point per device port, `<id>_device<N>_host`: the host it is
attached to, read back at each poll (_instr.REFRESH), and moved by
commanding the Item. The matrix cannot detach a port; parking it on a host
with nothing cabled does that.

`<id>_reboot` (String), commanded `reboot`, sends `set_system_reboot`, as
the Reboot button on the matrix's own System page does: its controller
restarts, in about half a second, and keeps each port's host. Only a matrix
with `reboot: nightly` restarts unasked, at night while every room whose
device ports it switches is Off (reboot(), oh_features.reboots).
"""

from __future__ import annotations

import math

from oh_conf import House, YamlFile, uses

from . import _instr as instr

NAME = "ukm404_http"
PHASE = "control"
FILE = "yaml/ukm404.yaml"
HEADER = (
    "USB matrix switching over the UKM-404's JSON API (HTTP binding). Each",
    "device port's host is polled with one `get_matrix_status` request per",
    "Thing and moved with `host_switch`; `set_system_reboot` restarts it.",
    "Items are autoupdate off: they show only what the matrix reports.",
)
ADDONS = instr.ADDONS
ASSETS: dict[str, str] = {}
HOSTS = DEVICES = 4
KINDS = {"usb_matrix"}
#: All that the Thing ever sends.
COMHEADS = frozenset({"get_matrix_status", "host_switch", "set_system_reboot"})
#: The reboot channel's command: the vendor page's body for `reboot`, and no
#: request for anything else (the HTTP binding sends nothing for a null
#: transformation, 5.2.3), since a body the matrix does not know stalls it.
REBOOT = "JS(|input === 'reboot' ? JSON.stringify({comhead:'set_system_reboot'}) : null)"
#: A state transformation that keeps nothing of a reply.
NOTHING = "JS(|null)"
#: The TCP port where the controller prints what it does, and the line it
#: prints once it has started. The network module in front of it keeps
#: answering while it restarts.
CONSOLE_PORT = 8000
READY = "initialization finished!"
#: Seconds after which its Items have been polled again: one poll and its
#: timeout.
POLLED = instr.REFRESH + math.ceil(instr.TIMEOUT / 1000)
thing_uid = instr.thing_uid
url = instr.url


def ports(device: dict) -> tuple[int, int]:
    """(inputs, outputs): hosts and device ports."""
    return HOSTS, DEVICES


def switch_item(device: dict, output: int) -> str:
    """The Item holding the host device port `output` is attached to."""
    return f"{device['id']}_device{output}_host"


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's fields (oh_integrations): each device port's
    host, written and read through the Thing's own channel; the matrix
    acknowledges a write with {"result": 1}. Each is independent: a
    host_switch names its one port, so a lost answer leaves the other ports
    as they were."""
    return {switch_item(device, port): {"kind": "http", "thing": thing_uid(device), "channel": f"device{port}_host",
                                        "values": [str(n) for n in range(1, HOSTS + 1)], "ack": "result",
                                        "independent": True}
            for port in range(1, DEVICES + 1)}


def reboot_item(device: dict) -> str:
    return f"{device['id']}_reboot"


def reboot(device: dict) -> dict:
    """Nightly reboots (oh_features.reboots): the matrix answers while its
    ports read, and restarts too briefly for that to show, so its console
    says when it has; it is back once they still read a poll later. Nothing it reports shows that a port is in use, so it
    has no idle readings of its own; oh_features.reboots adds its rooms'."""
    return {"item": reboot_item(device), "answers": [switch_item(device, 1)], "things": [thing_uid(device)],
            "idle": [], "console": {"host": device.get("ip"), "port": CONSOLE_PORT, "ready": READY, "polled": POLLED}}


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("kind") not in KINDS:
        return [f"{device.get('id')}: {NAME} drives a usb_matrix, not a {device.get('kind')}"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = thing_uid(device)
    thing = instr.thing(device, house)
    for port in range(1, DEVICES + 1):
        channel = f"device{port}_host"
        # The vendor page's "all devices" form sends device 5; never use it.
        command = f"JSON.stringify({{comhead:'host_switch',device:[{port},parseInt(input)]}})"
        thing["channels"][channel] = instr.number_channel(f"Device port {port} host", "get_matrix_status",
                                                          "all_host", port, command)
        out.items[switch_item(device, port)] = {
            "type": "Number",
            "label": f"Device port {port} host",
            "icon": "switch",
            "groups": [ident],
            "tags": ["Control"],
            "autoupdate": False,
            "channel": f"{uid}:{channel}",
        }
    # Write-only, yet with the ports' poll and a state that keeps nothing:
    # oh_render.read_only() turning it READONLY then polls nothing more.
    thing["channels"]["reboot"] = {"type": "string", "label": "Reboot", "config": {
        "mode": "WRITEONLY", "stateContent": instr.body("get_matrix_status", language=0),
        "stateTransformation": [NOTHING], "commandTransformation": [REBOOT]}}
    out.items[reboot_item(device)] = {
        "type": "String", "label": "Reboot", "groups": [ident], "tags": ["Control"], "autoupdate": False,
        "channel": f"{uid}:reboot", "metadata": {"commandDescription": {"config": {"options": "reboot=Reboot"}}},
    }
    out.things[uid] = thing
