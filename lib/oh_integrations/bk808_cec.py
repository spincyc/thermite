"""Optional fixed-input BK-808 CEC requests, without player-state claims."""
from __future__ import annotations

import ipaddress
from pathlib import Path
import re

from oh_conf import Addon, House, YamlFile, config as device_config, retarget, uses
from oh_shape import NUMBER, TEXT, Key
from . import _commands, _items, _script, bk808_http
from . import _instr as instr
from . import globalcache_ir as ir

NAME = "bk808_cec"
PHASE = "control"
NEEDS_IP = False
NEEDS = (bk808_http,)
FILE = "yaml/bk808-cec.yaml"
HEADER = ("Optional source CEC requests through an explicitly wired BK-808 input.",
          "Transport replies are not player power/playback observations.")
ADDONS = (Addon("binding", "http"), Addon("automation", "jsscripting", "finite source CEC keys"))
ASSETS: dict[str, str] = {}
DEVICE_KEYS = {
    "matrix": Key(TEXT, "the device id of the HDMI matrix; derived from routing.hdmi_matrix when omitted"),
    "input": Key(NUMBER, "the matrix input the source is on; derived from routing.sources.<id>.hdmi_input when omitted"),
}
RULE = "automation/js/bk808_cec.js"
SOURCE = Path(__file__).with_suffix(".js")
KEYS = {"power_on": 1, "power_off": 2, "up": 3, "left": 4, "ok": 5, "right": 6,
        "menu": 7, "down": 8, "back": 9, "previous": 10, "play": 11, "next": 12,
        "rewind": 13, "pause": 14, "fast_forward": 15, "stop": 16}
# Source CEC indices17..19 are intentionally unexposed: room audio remains
# with its configured receiver, not the disc player's CEC volume controls.
_SUPPORTED_KINDS = frozenset({"disc_player", "streamer", "tuner", "game_console"})


def _resolved(device: dict, data: dict | None = None) -> tuple[str | None, int | None]:
    cfg = device_config(device, NAME)
    matrix = cfg.get("matrix")
    hdmi_input = cfg.get("input")
    if data is not None:
        routing = data.get("routing") if isinstance(data.get("routing"), dict) else {}
        if matrix is None:
            matrix = routing.get("hdmi_matrix")
        if hdmi_input is None:
            sources = routing.get("sources") if isinstance(routing.get("sources"), dict) else {}
            entry = sources.get(device.get("id"))
            if isinstance(entry, dict):
                hdmi_input = entry.get("hdmi_input")
    return (matrix if isinstance(matrix, str) else None,
            hdmi_input if type(hdmi_input) is int else None)


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    where = device.get("id") or "a device"
    if device.get("kind") not in _SUPPORTED_KINDS:
        return [f"{where}: source CEC does not support kind {device.get('kind')!r}"]
    raw_section = device.get("config")
    raw = raw_section.get(NAME) if isinstance(raw_section, dict) else None
    if raw is not None:
        if not isinstance(raw, dict):
            return [f"{where}.config.{NAME}: must be a mapping"]
        extra = sorted(set(raw) - {"matrix", "input"})
        if extra:
            return [f"{where}.config.{NAME}: unknown key(s) {', '.join(extra)}"]
        if "matrix" in raw and (not isinstance(raw["matrix"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*", raw["matrix"])):
            return [f"{where}.config.{NAME}.matrix: must be a matrix device id"]
        if "input" in raw and (type(raw["input"]) is not int or not 1 <= raw["input"] <= 32):
            return [f"{where}.config.{NAME}.input: must be a positive matrix input number"]
    if uses(device, "control", "ir"):
        try:
            if ir.commissioned(device, require_emitter=False) is not None:
                return [f"{where}: select one configured IR or CEC provider, never both"]
        except ir.ProfileError:
            return [f"{where}: invalid alternative IR profile"]
    return []


def validate_house(data: dict) -> list[str]:
    devices = {device["id"]: device for device in data.get("devices", [])}
    routing = data.get("routing") or {}
    errors = []
    for device in devices.values():
        if not uses(device, PHASE, NAME) or validate(device):
            continue
        matrix_id, hdmi_input = _resolved(device, data)
        if not matrix_id or hdmi_input is None:
            errors.append(f"{device['id']}: bk808_cec needs matrix and input (configured or derived from routing)")
            continue
        matrix = devices.get(matrix_id)
        source = (routing.get("sources") or {}).get(device["id"], {})
        if (not matrix or matrix.get("kind") != "hdmi_matrix"
                or not uses(matrix, "control", "bk808_http")):
            errors.append(f"{device['id']}: cec matrix must be a controlled bk808_http hdmi_matrix")
            continue
        max_inputs, _ = bk808_http.ports(matrix)
        if not 1 <= hdmi_input <= max_inputs:
            errors.append(f"{device['id']}: cec input {hdmi_input} is outside 1..{max_inputs}")
        if routing.get("hdmi_matrix") != matrix_id or source.get("hdmi_input") != hdmi_input:
            errors.append(f"{device['id']}: cec input must match its configured HDMI source wiring")
    return errors


def resolve(device: dict, data: dict) -> dict | None:
    """Derive omitted `matrix` and `input` from `routing`."""
    if not uses(device, PHASE, NAME):
        return None
    cfg = device_config(device, NAME)
    if "matrix" in cfg and "input" in cfg:
        return None
    matrix_id, hdmi_input = _resolved(device, data)
    if matrix_id is None or hdmi_input is None:
        return None
    merged = {**cfg, "matrix": matrix_id, "input": hdmi_input}
    return {**device, "config": {**(device.get("config") or {}), NAME: merged}}


def enabled(device: dict) -> bool:
    if not uses(device, PHASE, NAME):
        return False
    matrix_id, hdmi_input = _resolved(device)
    return matrix_id is not None and hdmi_input is not None


def thing_uid(device: dict) -> str:
    matrix_id, _ = _resolved(device)
    if matrix_id is None:
        raise ValueError("CEC device has no resolved matrix")
    return bk808_http.thing_uid({"id": matrix_id})


def transport(device: dict) -> str | None:
    """The shared BK-808 matrix Thing the rule sends this device's CEC
    through. Its status is what says whether a key can reach this device
    (oh_metrics.signals())."""
    return thing_uid(device) if enabled(device) else None


def request(device: dict, button: str) -> str:
    _, hdmi_input = _resolved(device)
    if button not in KEYS or hdmi_input is None or validate(device):
        raise ValueError("CEC request needs a configured source and known player key")
    width = max(8, hdmi_input)
    ports = [int(i == hdmi_input - 1) for i in range(width)]
    return instr.body("cec command", language=0, object=0, port=ports, index=KEYS[button])


def render(device: dict, house: House, out: YamlFile) -> None:
    problems = validate(device) + validate_house(house.data)
    if problems:
        raise ValueError("; ".join(problems))
    ident = device["id"]
    out.items[f"{ident}_key"] = _items.control_point(ident, "String", "Source CEC key", ["Control"])
    _commands.render(device, out, NAME)


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if enabled(device) else None


def files(devices: list[dict], house: House) -> dict[str, str]:
    if not devices:
        return {}
    if validate_house(house.data):
        raise ValueError("CEC wiring must validate before rendering command rules")
    resolved = [resolve(d, house.data) or d for d in devices]
    table = [{"id": d["id"], "thing": thing_uid(d), "requests": {button: request(d, button) for button in KEYS}} for d in resolved]
    return {RULE: _script.rule(SOURCE, NAME, {"DEVICES": table}, connects=False)}


def remote(device: dict) -> dict | None:
    return {"key": f"{device['id']}_key"} if enabled(device) else None


def remote_of(device: dict) -> str:
    return "sony_bluray_cec"


def remote_layouts(device: dict | None = None) -> dict[str, dict]:
    """Button layout definition for CEC-controlled Blu-ray players."""
    return {
        "sony_bluray_cec": {
            "label": "Blu-ray CEC remote",
            "corners": ["power", "eject", "back", "home"],
            "top": ["power_on", "power_off", "menu"],
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


def remote_availability(device: dict) -> dict | None:
    return {"keyButtons": list(KEYS), "note": "CEC requests only; player state is not reported"} if enabled(device) else None


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    host, _, port = address.rpartition(":")
    if not ipaddress.IPv4Address(host).is_loopback or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("CEC stand-in must be loopback HTTP")
    return {**files, bk808_http.FILE: retarget(
        files[bk808_http.FILE], bk808_http.FILE, thing_uid(device),
        lambda thing: thing["config"].update(baseURL=instr.url(address)))}
