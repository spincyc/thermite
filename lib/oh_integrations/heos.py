"""Native HEOS transport for a receiver already controlled by DenonMarantz.

Only the player's Control readback is linked, display-only. The rule sends
four fixed transport commands directly to that native channel after fresh
main-zone ON/NET reports. Power, input, mute and capped volume remain owned
by denonmarantz_http. No HEOS account, grouping, queue or media-URL control
is configured. Native HEOS may fetch artwork reported by a playing source.
"""

from __future__ import annotations

import json
import ipaddress
import re
from pathlib import Path

from oh_conf import Addon, House, YamlFile, point, uses

NAME = "heos"
PHASE = "control"
FILE = "yaml/heos.yaml"
HEADER = ("Receiver HEOS transport only, through the native HEOS binding.",
          "Player IDs come from the local get_players response, not the receiver's volume scale.",
          "No account or media/group/volume/input control is configured.")
ADDONS = (Addon("binding", "heos"), Addon("automation", "jsscripting", "guarded HEOS transport keys"))
ASSETS: dict[str, str] = {}
RULE = "automation/js/heos.js"
SOURCE = Path(__file__).with_name("heos.js")
KEYS = {"previous": "PREVIOUS", "play": "PLAY", "pause": "PAUSE", "next": "NEXT"}


def enabled(device: dict) -> bool:
    return uses(device, PHASE, NAME) and device.get("heos_pid") is not None


def bridge_uid(device: dict) -> str:
    return f"heos:bridge:{device['id']}"


def thing_uid(device: dict) -> str:
    return f"heos:player:{device['id']}:{device['id']}"


def playback_item(device: dict) -> str:
    return f"{device['id']}_heos_playback"


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    where = device.get("id") or "a device"
    problems = []
    if device.get("kind") != "avr" or not uses(device, "control", "denonmarantz_http"):
        problems.append(f"{where}: heos transport needs an AVR controlled by denonmarantz_http")
    pid = device.get("heos_pid")
    if isinstance(pid, bool) or not re.fullmatch(r"-?[1-9][0-9]{0,9}", str(pid)) or not -2147483648 <= int(pid) <= 2147483647:
        problems.append(f"{where}.heos_pid: needs the nonzero signed player ID verified by local HEOS get_players")
    return problems


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    out.things[bridge_uid(device)] = {
        "isBridge": True, "label": f"{house.label(device)} HEOS connection",
        "location": house.room_label(device["room"]), "config": {"ipAddress": device["ip"], "heartbeat": 60},
    }
    uid = thing_uid(device)
    out.things[uid] = {"bridge": bridge_uid(device), "label": f"{house.label(device)} HEOS transport",
                       "location": house.room_label(device["room"]), "config": {"pid": str(device["heos_pid"])}}
    item = point(ident, "Player", "HEOS Playback", ["Status", "MediaControl"], f"{uid}:Control", guarded=True)
    item["autoupdate"] = False
    item["metadata"]["stateDescription"]["config"]["options"] = "PLAY=Playing,PAUSE=Paused or stopped"
    out.items[playback_item(device)] = item


def files(devices: list[dict], house: House) -> dict[str, str]:
    table = [{"id": d["id"], "thing": thing_uid(d), "receiver": f"denonmarantz:avr:{d['id']}",
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
