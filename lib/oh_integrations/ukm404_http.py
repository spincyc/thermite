"""4x4 USB 3.2 Gen 1 matrix (OREI UKM-404 family) over its JSON API.

One Number point per device port, `<id>_device<N>_host`: the host it is
attached to, read back every few seconds, and moved by commanding the Item.
The matrix cannot detach a port; parking it on a host with nothing cabled
does that.
"""

from __future__ import annotations

from oh_conf import House, YamlFile, uses

from . import _instr as instr

NAME = "ukm404_http"
PHASE = "control"
FILE = "yaml/ukm404.yaml"
HEADER = (
    "USB matrix switching over the UKM-404's JSON API (HTTP binding). Each",
    "device port's host is polled with one `get_matrix_status` request per",
    "Thing and moved with `host_switch`. Items are autoupdate off: they show",
    "only what the matrix reports.",
)
ADDONS = instr.ADDONS
ASSETS: dict[str, str] = {}
HOSTS = DEVICES = 4
KINDS = {"usb_matrix"}
#: All that the Thing ever sends.
COMHEADS = frozenset({"get_matrix_status", "host_switch"})
thing_uid = instr.thing_uid
url = instr.url


def ports(device: dict) -> tuple[int, int]:
    """(inputs, outputs): hosts and device ports."""
    return HOSTS, DEVICES


def switch_item(device: dict, output: int) -> str:
    """The Item holding the host device port `output` is attached to."""
    return f"{device['id']}_device{output}_host"


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
    out.things[uid] = thing
