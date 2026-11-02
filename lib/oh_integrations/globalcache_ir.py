"""Verified disc-player IR profiles through native Global Caché iTach IP2IR.

No endpoint or code is inferred. Unconfigured IR devices remain dormant.
Only a configured iTach and individually verified profile keys are rendered;
IR acknowledgements are never device power or playback observations.
"""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re

from oh_conf import Addon, House, LIB, YamlFile, point, retarget, uses

NAME = "ir"
PHASE = "control"
NEEDS_IP = False
FILE = "yaml/ir.yaml"
HEADER = ("Verified IR keys only; no device power or playback state is inferred.",
          "Unconfigured devices remain on their existing Control4 IR route.")
ADDONS = (Addon("binding", "globalcache"), Addon("transformation", "map"),
          Addon("automation", "jsscripting", "finite verified IR dispatch"))
ASSETS: dict[str, str] = {}
from oh_paths import SITE
INSTANCE = SITE / "instance"
RULE = "automation/js/globalcache_ir.js"
SOURCE = Path(__file__).with_suffix(".js")
KEYS = frozenset({"power", "eject", "back", "home", "up", "down", "left", "right", "ok", "top_menu",
                  "popup_menu", "options", "display", "audio", "subtitle", "previous", "rewind", "play",
                  "pause", "stop", "fast_forward", "next", "netflix", "favorite", "bluetooth",
                  "red", "green", "yellow", "blue"})
MODEL = "UBP-X800M2"
HARDWARE = "iTach IP2IR"


class ProfileError(ValueError):
    """A finite, safe profile validation failure."""


def code(value: object) -> str:
    """Uncompressed Global Caché frequency,repeat,offset,pulse-pairs."""
    if not isinstance(value, str) or len(value) > 6000 or not re.fullmatch(r"[0-9]{1,6}(?:,[0-9]{1,6})+", value):
        raise ProfileError("IR code must contain only comma-separated decimal integers")
    numbers = [int(v) for v in value.split(",")]
    if not 5 <= len(numbers) <= 523 or (len(numbers) - 3) % 2:
        raise ProfileError("IR code needs 1 to 260 complete on/off pulse pairs")
    frequency, repeats, offset, *pulses = numbers
    if not 15000 <= frequency <= 500000 or not 1 <= repeats <= 5:
        raise ProfileError("IR frequency must be 15000..500000 Hz and repeats 1..5")
    if offset < 1 or offset > len(pulses) or offset % 2 != 1 or any(not 1 <= p <= 65535 for p in pulses):
        raise ProfileError("IR offset must address an on pulse and pulse lengths must be 1..65535")
    if sum(pulses) * repeats > frequency * 2:
        raise ProfileError("IR code exceeds the two-second transmission bound")
    return ",".join(map(str, numbers))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError("IR profile contains a duplicate field")
        result[key] = value
    return result


def load_profile(path: Path, model: str = MODEL) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ProfileError("IR profile is larger than 64 KiB")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProfileError("IR profile is not readable UTF-8 JSON") from error
    if not isinstance(data, dict) or set(data) != {"version", "make", "model", "source", "commands"}:
        raise ProfileError("IR profile needs exactly version, make, model, source and commands")
    if type(data["version"]) is not int or data["version"] != 1 or data["make"] != "Sony" or data["model"] != model or model != MODEL:
        raise ProfileError("IR profile must be version 1 for Sony UBP-X800M2")
    source = data["source"]
    if not isinstance(source, str) or not source.strip() or len(source) > 300 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in source):
        raise ProfileError("IR profile needs a bounded plain-text learning or verified-code provenance")
    entries = data["commands"]
    if not isinstance(entries, dict) or not entries or len(entries) > len(KEYS):
        raise ProfileError("IR profile needs a finite command map")
    verified, pending = {}, []
    for key, entry in entries.items():
        if key not in KEYS or not isinstance(entry, dict) or set(entry) != {"code", "verified"} or type(entry["verified"]) is not bool:
            raise ProfileError("IR profile has an unknown key or malformed verification entry")
        normalized = code(entry["code"])
        if entry["verified"]:
            verified[key] = normalized
        else:
            pending.append(key)
    return {"model": model, "commands": verified, "pending": sorted(pending), "missing": sorted(KEYS - verified.keys())}


def commissioned(device: dict) -> dict | None:
    config = device.get("ir")
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) != {"model", "ip", "connector", "profile"}:
        raise ProfileError("ir needs exactly model, ip, connector and profile")
    if config["model"] != HARDWARE or device.get("kind") != "disc_player" or device.get("model") != MODEL:
        raise ProfileError("this IR integration supports a Sony UBP-X800M2 through an iTach IP2IR")
    if not isinstance(config["ip"], str):
        raise ProfileError("IR emitter needs an explicit IPv4 address")
    try:
        address = ipaddress.IPv4Address(config["ip"])
    except (ipaddress.AddressValueError, TypeError) as error:
        raise ProfileError("IR emitter needs an explicit IPv4 address") from error
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ProfileError("IR emitter must be a unicast address")
    if type(config["connector"]) is not int or config["connector"] not in (1, 2, 3):
        raise ProfileError("iTach IP2IR connector must be 1, 2 or 3")
    name = config["profile"]
    if not isinstance(name, str) or not re.fullmatch(r"ir/[A-Za-z0-9_-]+\.json", name):
        raise ProfileError("IR profile must name an ir/<profile>.json file under instance/")
    root = INSTANCE.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ProfileError("IR profile must remain under instance/")
    profile = load_profile(path, device["model"])
    if not profile["commands"]:
        raise ProfileError("IR profile has no verified commands; leave the emitter unconfigured")
    return {"ip": str(address), "connector": config["connector"], **profile}


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    try:
        commissioned(device)
        return []
    except ProfileError as error:
        return [f"{device.get('id', 'device')}: {error}"]


def thing_uid(device: dict) -> str:
    return f"globalcache:itachIR:{device['id']}"


def addons(devices: list[dict]) -> tuple:
    """An inventory placeholder must not install or start an IR binding."""
    return ADDONS if any(commissioned(device) is not None for device in devices) else ()


def map_name(device: dict) -> str:
    return f"ir-{device['id']}.map"


def render(device: dict, house: House, out: YamlFile) -> None:
    profile = commissioned(device)
    if profile is None:
        return
    ident, uid = device["id"], thing_uid(device)
    out.things[uid] = {"label": f"{house.label(device)} verified IR emitter", "location": house.room_label(device["room"]),
                       "config": {"ipAddress": profile["ip"], "mapFilename": map_name(device)}}
    channel = f"{uid}:ir-m1#c{profile['connector']}"
    out.items[f"{ident}_ir_native"] = point(ident, "String", "IR transport (no device feedback)", ["Status"], channel, guarded=True)
    out.items[f"{ident}_key"] = {"type": "String", "label": "Verified IR key", "groups": [ident],
                                 "tags": ["Control"], "autoupdate": False}


def files(devices: list[dict], house: House) -> dict[str, str]:
    table, result = [], {}
    for device in devices:
        profile = commissioned(device)
        if profile is None:
            continue
        keys = sorted(profile["commands"])
        table.append({"id": device["id"], "thing": thing_uid(device), "channel": f"ir-m1#c{profile['connector']}", "keys": keys})
        result[f"transform/{map_name(device)}"] = "\n".join(f"{key}={profile['commands'][key]}" for key in keys) + "\n"
    if table:
        result[RULE] = SOURCE.read_text().replace("const DEVICES = null;", "const DEVICES = " + json.dumps(table) + ";")
    return result


def remote(device: dict) -> dict | None:
    if not uses(device, PHASE, NAME) or commissioned(device) is None:
        return None
    return {"key": f"{device['id']}_key"}


def remote_of(device: dict) -> str:
    return "sony_bluray"


def keys(device: dict) -> dict[str, str]:
    profile = commissioned(device)
    return profile["commands"] if profile else {}


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """The fixed native port can be tested only on an explicit loopback host."""
    host, _, port = address.rpartition(":")
    if not ipaddress.IPv4Address(host).is_loopback or port != "4998":
        raise ProfileError("IR stand-in must use loopback TCP 4998")
    text = retarget(files[FILE], FILE, thing_uid(device), lambda thing: thing["config"].update(ipAddress=host))
    return {**files, FILE: text}


def remote_availability(device: dict) -> dict | None:
    if not uses(device, PHASE, NAME) or device.get("model") != MODEL:
        return None
    profile = commissioned(device)
    return {"keyButtons": sorted(profile["commands"]) if profile else [],
            "note": "Only verified IR keys are available; device state is not reported" if profile else
                    "IR emitter and verified profile are not configured"}


def dry_packet(code_: str, connector: int, counter: int = 1) -> str:
    if type(connector) is not int or connector not in (1, 2, 3) or type(counter) is not int or not 0 <= counter <= 65535:
        raise ProfileError("IR dry-run needs a valid connector and bounded transmission ID")
    return f"sendir,1:{connector},{counter},{code(code_)}\r"


def transport_addresses(device: dict) -> dict[str, list[str]]:
    """Only this native IR Thing may use its explicitly validated emitter."""
    if not uses(device, PHASE, NAME):
        return {}
    profile = commissioned(device)
    return {thing_uid(device): [profile["ip"]]} if profile else {}
