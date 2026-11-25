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

from oh_conf import Addon, House, YamlFile, config as device_config, instance, point, retarget, uses
from oh_shape import NUMBER, TEXT, Key
from . import _commands, _script

NAME = "ir"
PHASE = "control"
NEEDS_IP = False
FILE = "yaml/ir.yaml"
HEADER = ("Verified IR keys only; no device power or playback state is inferred.",
          "Unconfigured devices remain on their existing Control4 IR route.")
ADDONS = (Addon("binding", "globalcache"), Addon("transformation", "map"),
          Addon("automation", "jsscripting", "finite verified IR dispatch"))
ASSETS: dict[str, str] = {}
DEVICE_KEYS = {
    "via": Key(TEXT, "the device id of a Global Caché emitter device to inherit ip and model from"),
    "connector": Key(NUMBER, "which of the emitter's connectors points at the device: 1, 2 or 3"),
    "profile": Key(TEXT, "the file of verified IR commands, as ir/<name>.json under instance/"),
    "model": Key(TEXT, "the emitter's model when not inherited from via"),
    "ip": Key(TEXT, "the emitter's IPv4 address when not inherited from via"),
}
#: The iTach's TCP API.
DISCOVER_PORTS = (4998,)
RULE = "automation/js/globalcache_ir.js"
SOURCE = Path(__file__).with_suffix(".js")
KEYS = frozenset({"power", "eject", "back", "home", "up", "down", "left", "right", "ok", "top_menu",
                  "popup_menu", "options", "display", "audio", "subtitle", "previous", "rewind", "play",
                  "pause", "stop", "fast_forward", "next", "netflix", "favorite", "bluetooth",
                  "red", "green", "yellow", "blue"})
HARDWARE = "iTach IP2IR"
_SUPPORTED_KINDS = frozenset({"disc_player", "tv", "avr", "streamer", "tuner", "projector", "soundbar"})
_CONFIG_KEYS = frozenset(DEVICE_KEYS)


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


def load_profile(path: Path, model: str | None = None, make: str | None = None) -> dict:
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
    if type(data["version"]) is not int or data["version"] != 1:
        raise ProfileError("IR profile must be version 1")
    prof_make, prof_model = data["make"], data["model"]
    if (not isinstance(prof_make, str) or not prof_make.strip() or len(prof_make) > 100
            or not isinstance(prof_model, str) or not prof_model.strip() or len(prof_model) > 100):
        raise ProfileError("IR profile needs non-empty make and model strings")
    if make is not None and prof_make != make:
        raise ProfileError(f"IR profile make {prof_make!r} does not match device make {make!r}")
    if model is not None and prof_model != model:
        raise ProfileError(f"IR profile model {prof_model!r} does not match device model {model!r}")
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
    return {"make": prof_make, "model": prof_model, "commands": verified,
            "pending": sorted(pending), "missing": sorted(KEYS - verified.keys())}


def _raw_ir_config(device: dict) -> object:
    section = device.get("config")
    return section.get(NAME) if isinstance(section, dict) else None


def _emitter_ref(device: dict, cfg: dict) -> str | None:
    via = cfg.get("via")
    if isinstance(via, str) and via:
        return via
    dev_via = device.get("via")
    return dev_via if isinstance(dev_via, str) and dev_via else None


def _resolved_cfg(device: dict, devices: dict[str, dict] | None = None) -> dict | None:
    raw = _raw_ir_config(device)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileError("config.ir must be a mapping")
    unknown_keys = sorted(set(raw) - _CONFIG_KEYS)
    if unknown_keys:
        raise ProfileError(f"config.ir has unknown key(s): {', '.join(unknown_keys)}")
    cfg = dict(raw)
    if "via" in cfg and (not isinstance(cfg["via"], str) or not cfg["via"]):
        raise ProfileError("config.ir.via must name an emitter device id")
    if devices is not None:
        via = _emitter_ref(device, cfg)
        if via is not None and ("ip" not in cfg or "model" not in cfg or "via" in cfg):
            if via == device.get("id"):
                raise ProfileError("config.ir.via cannot refer to the device itself")
            emitter = devices.get(via)
            if emitter is None:
                raise ProfileError(f"config.ir.via {via!r} is not a device id")
            if "ip" not in cfg and emitter.get("ip"):
                cfg["ip"] = emitter["ip"]
            if "model" not in cfg and emitter.get("model"):
                cfg["model"] = emitter["model"]
    return cfg


def commissioned(device: dict, *, devices: dict[str, dict] | None = None, require_emitter: bool = True) -> dict | None:
    cfg = _resolved_cfg(device, devices=devices)
    if cfg is None:
        return None
    if device.get("kind") not in _SUPPORTED_KINDS:
        raise ProfileError(f"this IR integration does not support kind {device.get('kind')!r}")
    if "connector" not in cfg or "profile" not in cfg:
        raise ProfileError("config.ir needs connector and profile")
    if type(cfg["connector"]) is not int or cfg["connector"] not in (1, 2, 3):
        raise ProfileError("iTach IP2IR connector must be 1, 2 or 3")
    name = cfg["profile"]
    if not isinstance(name, str) or not re.fullmatch(r"ir/[A-Za-z0-9_-]+\.json", name):
        raise ProfileError("IR profile must name an ir/<profile>.json file under instance/")
    files = instance()
    if files is None:
        raise ProfileError("an IR profile is read from beside the house file, and this house was read from none")
    root = files.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ProfileError("IR profile must remain under instance/")
    profile = load_profile(path, model=device.get("model"), make=device.get("make"))
    if not profile["commands"]:
        raise ProfileError("IR profile has no verified commands; leave the emitter unconfigured")
    ip_value = cfg.get("ip")
    model_value = cfg.get("model")
    if not require_emitter and (ip_value is None or model_value is None) and _emitter_ref(device, cfg) is not None:
        # Emitter ip/model will be resolved from `via` in validate_house() / resolve().
        if model_value is not None and model_value != HARDWARE:
            raise ProfileError(f"this IR integration supports an {HARDWARE} emitter")
        if ip_value is not None:
            _validate_ip(ip_value)
        return {"ip": ip_value or "", "connector": cfg["connector"], **profile}
    if model_value != HARDWARE:
        raise ProfileError(f"this IR integration supports an {HARDWARE} emitter")
    address = _validate_ip(ip_value)
    return {"ip": address, "connector": cfg["connector"], **profile}


def _validate_ip(value: object) -> str:
    if not isinstance(value, str):
        raise ProfileError("IR emitter needs an explicit IPv4 address")
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as error:
        raise ProfileError("IR emitter needs an explicit IPv4 address") from error
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ProfileError("IR emitter must be a unicast address")
    return str(address)


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    try:
        commissioned(device, require_emitter=False)
        return []
    except ProfileError as error:
        return [f"{device.get('id', 'device')}: {error}"]


def validate_house(data: dict) -> list[str]:
    devices = {d["id"]: d for d in data.get("devices") or [] if isinstance(d, dict) and d.get("id")}
    problems: list[str] = []
    for device in devices.values():
        if not uses(device, PHASE, NAME):
            continue
        try:
            commissioned(device, devices=devices, require_emitter=True)
        except ProfileError as error:
            problems.append(f"{device.get('id', 'device')}: {error}")
    return problems


def resolve(device: dict, data: dict) -> dict | None:
    """Inherit omitted emitter ip/model from `config.ir.via` or `device.via`."""
    if not uses(device, PHASE, NAME):
        return None
    cfg = device_config(device, NAME)
    if not cfg or ("ip" in cfg and "model" in cfg):
        return None
    devices = {d["id"]: d for d in data.get("devices") or [] if isinstance(d, dict) and d.get("id")}
    resolved = _resolved_cfg(device, devices=devices)
    if resolved is None or resolved == cfg:
        return None
    return {**device, "config": {**(device.get("config") or {}), NAME: resolved}}


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
    _commands.render(device, out, NAME)


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) and commissioned(device) is not None else None


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
        result[RULE] = _script.rule(SOURCE, NAME, {"DEVICES": table}, connects=False)
    return result


def remote(device: dict) -> dict | None:
    if not uses(device, PHASE, NAME) or commissioned(device) is None:
        return None
    return {"key": f"{device['id']}_key"}


def remote_of(device: dict) -> str:
    return "sony_bluray"


def remote_layouts(device: dict | None = None) -> dict[str, dict]:
    """Button layout definition for IR-controlled Blu-ray players."""
    return {
        "sony_bluray": {
            "label": "Blu-ray remote",
            "wide": True,
            "corners": ["power", "eject", "back", "home"],
            "playback": ["previous", "rewind", "play", "pause", "stop", "fast_forward", "next"],
            "keyGroups": [
                {"label": "Apps", "keys": ["netflix", "favorite"], "columns": 2, "primary": True},
                {"label": "Disc menus", "keys": ["top_menu", "popup_menu", "options", "display"], "columns": 2},
                {"label": "Audio", "keys": ["audio", "subtitle"], "columns": 2},
                {"label": "Wireless audio", "keys": ["bluetooth"], "columns": 1},
                {"label": "Color keys", "keys": ["red", "green", "yellow", "blue"], "columns": 4},
            ],
        },
    }


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
    if not uses(device, PHASE, NAME):
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
