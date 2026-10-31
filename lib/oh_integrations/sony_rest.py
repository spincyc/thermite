"""Sony BRAVIA power state over its REST API (HTTP binding).

getPowerStatus answers without authentication, so reading needs no
credential. The channel is READONLY: a TV that also lists CONTROLLER
(oh_integrations.sony_ip_control) under control gets its power Item
commandable, and that integration's rule sends the commands.
"""

from __future__ import annotations

from oh_conf import Addon, House, YamlFile, point, uses

NAME = "sony_rest"
PHASE = "observe"
#: The control integration that commands these TVs.
CONTROLLER = "sony_ip_control"
FILE = "yaml/sony.yaml"
HEADER = (
    "Sony BRAVIA power state over the REST API (HTTP binding). getPowerStatus",
    "on /sony/system answers without authentication, so reading needs no",
    "credential; the channel is READONLY and nothing else is requested. A TV",
    "under sony_ip_control control takes power commands on the same Item: its",
    "rule sends them.",
)
ADDONS = (Addon("binding", "http"), Addon("transformation", "jsonpath"))
ASSETS: dict[str, str] = {}
POWER_REQUEST = '{"method":"getPowerStatus","id":1,"params":[],"version":"1.0"}'


def validate(device: dict) -> list[str]:
    return []


def power_item(device: dict) -> str:
    return f"{device['id']}_power"


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def base_url(address: str) -> str:
    """The Thing's URL for the TV at HOST[:PORT]."""
    return f"http://{address}/sony/system"


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = thing_uid(device)
    out.things[uid] = {
        "label": f"{house.label(device)} REST",
        "location": house.room_label(device["room"]),
        "config": {
            "baseURL": base_url(device["ip"]),
            "refresh": 15,
            "timeout": 3000,
            "stateMethod": "POST",
            "contentType": "application/json",
            "strictErrorHandling": True,
        },
        "channels": {
            "power": {
                "type": "switch",
                "label": "Power",
                "config": {
                    "mode": "READONLY",
                    "stateContent": POWER_REQUEST,
                    "stateTransformation": ["JSONPATH($.result[0].status)"],
                    "onValue": "active",
                    "offValue": "standby",
                },
            },
        },
    }
    item = point(ident, "Switch", "Power", ["Status", "Power"], f"{uid}:power", icon="television")
    if uses(device, "control", CONTROLLER):
        item.update(tags=["Control", "Power"], autoupdate=False)
    out.items[power_item(device)] = item
