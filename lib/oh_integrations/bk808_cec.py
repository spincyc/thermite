"""Optional fixed-input BK-808 CEC requests, without player-state claims."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re

from oh_conf import Addon, House, YamlFile, point, retarget, uses
from . import _instr as instr
from . import globalcache_ir as ir

NAME = "bk808_cec"
PHASE = "control"
NEEDS_IP = False
FILE = "yaml/bk808-cec.yaml"
HEADER = ("Optional source CEC requests through an explicitly wired BK-808 input.",
          "Transport replies are not player power/playback observations.")
ADDONS = (Addon("binding", "http"), Addon("automation", "jsscripting", "finite source CEC keys"))
ASSETS: dict[str, str] = {}
RULE = "automation/js/bk808_cec.js"
SOURCE = Path(__file__).with_suffix(".js")
KEYS = {"power_on": 1, "power_off": 2, "up": 3, "left": 4, "ok": 5, "right": 6,
        "menu": 7, "down": 8, "back": 9, "previous": 10, "play": 11, "next": 12,
        "rewind": 13, "pause": 14, "fast_forward": 15, "stop": 16}
# Source CEC indices17..19 are intentionally unexposed: room audio remains
# with its configured receiver, not the disc player's CEC volume controls.
POLL = 60


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    config = device.get("cec")
    if not isinstance(config, dict) or set(config) != {"matrix", "input"}:
        return [f"{device['id']}: cec needs exactly matrix and input"]
    if (device.get("make") != "Sony" or device.get("model") != ir.MODEL or device.get("kind") != "disc_player"
            or not isinstance(config["matrix"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*", config["matrix"])
            or type(config["input"]) is not int or not 1 <= config["input"] <= 8):
        return [f"{device['id']}: source CEC needs a Sony UBP-X800M2 and explicit BK-808 input1..8"]
    if uses(device, "control", "ir"):
        try:
            if ir.commissioned(device) is not None:
                return [f"{device['id']}: select one configured IR or CEC provider, never both"]
        except ir.ProfileError:
            return [f"{device['id']}: invalid alternative IR profile"]
    return []


def validate_house(data: dict) -> list[str]:
    devices = {device["id"]: device for device in data.get("devices", [])}
    routing = data.get("routing") or {}
    errors = []
    for device in devices.values():
        if not uses(device, PHASE, NAME) or validate(device):
            continue
        config = device["cec"]
        matrix = devices.get(config["matrix"])
        source = (routing.get("sources") or {}).get(device["id"], {})
        if (not matrix or matrix.get("kind") != "hdmi_matrix" or matrix.get("model") != "BK-808"
                or not uses(matrix, "control", "bk808_http")):
            errors.append(f"{device['id']}: cec matrix must be a controlled BK-808")
        if routing.get("hdmi_matrix") != config["matrix"] or source.get("hdmi_input") != config["input"]:
            errors.append(f"{device['id']}: cec input must match its configured HDMI source wiring")
    return errors


def enabled(device: dict) -> bool:
    return uses(device, PHASE, NAME) and isinstance(device.get("cec"), dict)


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}_cec:{device['cec']['matrix']}"


def request(device: dict, button: str) -> str:
    if button not in KEYS or validate(device):
        raise ValueError("CEC request needs a configured source and known player key")
    ports = [int(i == device["cec"]["input"] - 1) for i in range(8)]
    return json.dumps({"comhead": "cec command", "language": 0, "object": 0, "port": ports, "index": KEYS[button]}, separators=(",", ":"))


def render(device: dict, house: House, out: YamlFile) -> None:
    problems = validate(device) + validate_house(house.data)
    if problems:
        raise ValueError("; ".join(problems))
    matrix = next(d for d in house.devices if d["id"] == device["cec"]["matrix"])
    uid, ident = thing_uid(device), device["id"]
    out.things[uid] = {"label": f"{house.label(device)} CEC transport (not player feedback)",
        "location": house.room_label(device["room"]),
        "config": {"baseURL": instr.url(matrix["ip"]), "refresh": POLL, "timeout": 2000, "delay": 100,
                   "stateMethod": "POST", "commandMethod": "POST", "contentType": "application/json", "strictErrorHandling": True},
        "channels": {"transport": {"type": "string", "config": {"mode": "READONLY", "stateContent": instr.body("get video status", language=0)}},
                     "command": {"type": "string", "config": {"mode": "WRITEONLY"}}}}
    out.items[f"{ident}_cec_transport"] = point(ident, "String", "Matrix transport reply; not player state", ["Status"], f"{uid}:transport", guarded=True)
    out.items[f"{ident}_cec_native"] = point(ident, "String", "Internal CEC request", ["Status"], f"{uid}:command", guarded=True)
    out.items[f"{ident}_key"] = {"type": "String", "label": "Source CEC key", "groups": [ident], "tags": ["Control"], "autoupdate": False}


def files(devices: list[dict], house: House) -> dict[str, str]:
    if not devices:
        return {}
    if validate_house(house.data):
        raise ValueError("CEC wiring must validate before rendering command rules")
    table = [{"id": d["id"], "thing": thing_uid(d), "requests": {button: request(d, button) for button in KEYS}} for d in devices]
    return {RULE: SOURCE.read_text().replace("const DEVICES = null;", "const DEVICES = " + json.dumps(table) + ";")}


def remote(device: dict) -> dict | None:
    return {"key": f"{device['id']}_key"} if enabled(device) else None


def remote_of(device: dict) -> str:
    return "sony_bluray_cec"


def remote_availability(device: dict) -> dict | None:
    return {"keyButtons": list(KEYS), "note": "CEC requests only; player state is not reported"} if enabled(device) else None


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    host, _, port = address.rpartition(":")
    if not ipaddress.IPv4Address(host).is_loopback or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("CEC stand-in must be loopback HTTP")
    return {**files, FILE: retarget(files[FILE], FILE, thing_uid(device), lambda thing: thing["config"].update(baseURL=instr.url(address)))}
