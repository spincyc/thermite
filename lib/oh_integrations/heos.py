"""Native HEOS transport for a receiver already controlled by DenonMarantz.

Only the player's Control readback is linked, display-only. The rule sends
four fixed transport commands directly to that native channel after fresh
main-zone ON/NET reports. Power, input, mute and volume remain owned
by denonmarantz_http. No HEOS account, grouping, queue or media-URL control
is configured. Native HEOS may fetch artwork reported by a playing source.

A house has one HEOS bridge, however many HEOS receivers it has: the
binding connects to one player of the HEOS network as its bridge and
commands every player through it by player ID, and its README warns that a
second bridge can cause trouble with the connection. The bridge is hosted
on the HEOS receiver marked `heos_bridge: true` (at most one; prefer one
that stays powered and on the network, wired if it can be), else on the
first in the house file. Its UID, heos:bridge:<host>, names the host: the
bridge's connection is the host's HEOS CLI at the host's address, and the
inventory and metrics give every Thing to the device its UID ends with.
Each receiver's player, heos:player:<host>:<id>, is on that bridge, so it
is OFFLINE (BRIDGE_OFFLINE) while the bridge is.

`<id>_reboot` (String), commanded `reboot`, has the rule send
heos://system/reboot to the receiver's own HEOS CLI (TCP 1255), which is
answered and logged. The receiver restarts its network module, and with it
HEOS and the HTTP API; whether the rest of it restarts too is unconfirmed.
Only a receiver with `reboot: nightly` restarts unasked, at night while
every zone is off (reboot(), oh_features.reboots).
"""

from __future__ import annotations

import json
import ipaddress
import re
from pathlib import Path

from oh_conf import Addon, House, YamlFile, point, uses

from . import denonmarantz_http

NAME = "heos"
PHASE = "control"
FILE = "yaml/heos.yaml"
HEADER = ("Receiver HEOS transport only, through the native HEOS binding.",
          "Player IDs come from the local get_players response, not the receiver's volume scale.",
          "No account or media/group/volume/input control is configured.")
ADDONS = (Addon("binding", "heos"), Addon("automation", "jsscripting", "guarded HEOS transport keys"))
ASSETS: dict[str, str] = {}
#: heos_pid: the receiver's player ID; heos_bridge: true on the one HEOS
#: receiver that hosts the house's bridge (host()).
DEVICE_KEYS = ("heos_pid", "heos_bridge")
#: Optional music transport, never an HDMI routing stage: an unavailable HEOS
#: connection does not fail a routed source's working power/input path.
ROUTE_OPTIONAL = True
DISCOVER_MDNS = ("_heos",)
#: The bridge keeps a session open with its host receiver, which serves few;
#: the sandbox leaves it to the runtime.
SESSIONS = True
RULE = "automation/js/heos.js"
SOURCE = Path(__file__).with_name("heos.js")
KEYS = {"previous": "PREVIOUS", "play": "PLAY", "pause": "PAUSE", "next": "NEXT"}


def enabled(device: dict) -> bool:
    return uses(device, PHASE, NAME) and device.get("heos_pid") is not None


def host(devices: list[dict]) -> dict | None:
    """The receiver whose HEOS CLI the house's one bridge connects to: the
    HEOS device marked `heos_bridge: true`, else the first in the house
    file; None in a house without HEOS."""
    listed = [device for device in devices if uses(device, PHASE, NAME)]
    return next((device for device in listed if device.get("heos_bridge") is True), listed[0] if listed else None)


def bridge_uid(house: House) -> str:
    return f"heos:bridge:{host(house.devices)['id']}"


def thing_uid(device: dict, house: House) -> str:
    """The receiver's player, on the house's bridge."""
    return f"heos:player:{host(house.devices)['id']}:{device['id']}"


def playback_item(device: dict) -> str:
    return f"{device['id']}_heos_playback"


def reboot_item(device: dict) -> str:
    return f"{device['id']}_reboot"


def reboot(device: dict) -> dict:
    """Nightly reboots (oh_features.reboots): the receiver answers while
    denonmarantz_http reads it, and is idle while each zone with a power
    Item is off."""
    powers = [staged["power"] for zone in (None, 2) if (staged := denonmarantz_http.stage(device, zone))]
    return {"item": reboot_item(device), "answers": powers[:1], "things": [denonmarantz_http.thing_uid(device)],
            "idle": [{"item": power, "is": "OFF"} for power in powers]}


def validate(device: dict) -> list[str]:
    where = device.get("id") or "a device"
    if not uses(device, PHASE, NAME):
        return ([f"{where}.heos_bridge: only a device that uses heos can host the house's HEOS bridge"]
                if "heos_bridge" in device else [])
    problems = []
    if "heos_bridge" in device and device["heos_bridge"] is not True:
        problems.append(f"{where}.heos_bridge: must be true; leave it out on the other HEOS receivers")
    if device.get("kind") != "avr" or not uses(device, "control", "denonmarantz_http"):
        problems.append(f"{where}: heos transport needs an AVR controlled by denonmarantz_http")
    pid = device.get("heos_pid")
    if isinstance(pid, bool) or not re.fullmatch(r"-?[1-9][0-9]{0,9}", str(pid)) or not -2147483648 <= int(pid) <= 2147483647:
        problems.append(f"{where}.heos_pid: needs the nonzero signed player ID verified by local HEOS get_players")
    return problems


def validate_house(data: dict) -> list[str]:
    marked = [device["id"] for device in data.get("devices") or []
              if uses(device, PHASE, NAME) and device.get("heos_bridge") is True]
    if len(marked) < 2:
        return []
    return [f"heos_bridge is true on {', '.join(marked[:-1])} and {marked[-1]}; "
            "only one HEOS receiver can host the house's bridge"]


def render(device: dict, house: House, out: YamlFile) -> None:
    """The receiver's player and, with the first receiver rendered, the
    house's one bridge on its host, so that every player's bridge is in the
    same file."""
    ident, hub = device["id"], host(house.devices)
    out.things.setdefault(bridge_uid(house), {
        "isBridge": True, "label": f"HEOS connection via {house.label(hub)}",
        "location": house.room_label(hub["room"]), "config": {"ipAddress": hub["ip"], "heartbeat": 60},
    })
    uid = thing_uid(device, house)
    out.things[uid] = {"bridge": bridge_uid(house), "label": f"{house.label(device)} HEOS transport",
                       "location": house.room_label(device["room"]), "config": {"pid": str(device["heos_pid"])}}
    item = point(ident, "Player", "HEOS Playback", ["Status", "MediaControl"], f"{uid}:Control", guarded=True)
    item["autoupdate"] = False
    item["metadata"]["stateDescription"]["config"]["options"] = "PLAY=Playing,PAUSE=Paused or stopped"
    out.items[playback_item(device)] = item
    out.items[reboot_item(device)] = {
        "type": "String", "label": "Reboot", "groups": [ident], "tags": ["Control"], "autoupdate": False,
        "metadata": {"commandDescription": {"config": {"options": "reboot=Reboot"}}},
    }


def files(devices: list[dict], house: House) -> dict[str, str]:
    table = [{"id": d["id"], "thing": thing_uid(d, house), "receiver": denonmarantz_http.thing_uid(d),
              "host": str(ipaddress.IPv4Address(d["ip"])),
              "readURL": f"http://{ipaddress.IPv4Address(d['ip'])}:8080/goform/AppCommand.xml"} for d in devices]
    return {RULE: SOURCE.read_text().replace("const DEVICES = null;", "const DEVICES = " + json.dumps(table) + ";")}


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """The native proof reads only its receiver's explicit loopback stand-in."""
    host, _, port = address.rpartition(":")
    if not ipaddress.IPv4Address(host).is_loopback or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("HEOS fresh-read stand-in must use loopback HTTP")
    original = json.dumps(f"http://{ipaddress.IPv4Address(device['ip'])}:8080/goform/AppCommand.xml")
    replacement = json.dumps(f"http://{host}:{int(port)}/goform/AppCommand.xml")
    return {**files, RULE: files[RULE].replace(original, replacement)}
