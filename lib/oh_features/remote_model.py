"""The remote page's model (house.js), derived from the house file alone.

Labels, ordering and room members come from the house file; control Items
and limits from the integration contracts. literal() reads a generated
house.js back as data, never evaluating it.
"""
from __future__ import annotations

import json
import re

import oh_integrations as integrations
from oh_conf import House
from . import routing

GENERATED = "// Generated remote model; edit the house file, not this preview."
_TOKEN = re.compile(r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[A-Za-z_$][\w$]*|-?\d+(?:\.\d+)?|[{}\[\]:,;=.]')
_FORBIDDEN = {"__proto__", "prototype", "constructor"}


def literal(text: str) -> dict:
    """Read exactly one assignment of JSON-like data, never evaluate JavaScript."""
    tokens = []
    cursor = 0
    for match in _TOKEN.finditer(text):
        if match.start() != cursor:
            raise ValueError("remote presentation contains executable or unsupported syntax")
        cursor = match.end()
        token = match.group()
        if not token.isspace() and not token.startswith(("//", "/*")):
            tokens.append(token)
    if cursor != len(text):
        raise ValueError("remote presentation contains unsupported syntax")
    if tokens[:4] != ["globalThis", ".", "REMOTE_HOUSE", "="]:
        raise ValueError("remote presentation must assign only REMOTE_HOUSE")
    pos = 4

    def take():
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("incomplete remote presentation")
        item = tokens[pos]
        pos += 1
        return item

    def value():
        token = take()
        if token == "{":
            out = {}
            while tokens[pos] != "}":
                key = take()
                key = json.loads(key) if key.startswith('"') else key
                if not re.fullmatch(r"[\w$-]+", key) or key in _FORBIDDEN or key in out:
                    raise ValueError("unsafe or duplicate remote presentation key")
                if take() != ":":
                    raise ValueError("remote presentation accepts data properties only")
                out[key] = value()
                if tokens[pos] == "}":
                    break
                if take() != ",":
                    raise ValueError("invalid remote presentation object")
            take()
            return out
        if token == "[":
            out = []
            while tokens[pos] != "]":
                out.append(value())
                if tokens[pos] == "]":
                    break
                if take() != ",":
                    raise ValueError("invalid remote presentation array")
            take()
            return out
        if token.startswith('"') or token in {"null", "true", "false"} or re.fullmatch(r"-?\d+(?:\.\d+)?", token):
            return json.loads(token)
        raise ValueError("remote presentation accepts literal data only")

    try:
        result = value()
        if pos < len(tokens) and tokens[pos] == ";":
            pos += 1
        if pos != len(tokens) or not isinstance(result, dict):
            raise ValueError("remote presentation has extra statements")
        return result
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("invalid remote presentation data") from exc


def provider(device: dict) -> tuple[object | None, dict | None]:
    """The first integration (REGISTRY order) that gives the device a remote,
    and its control Items; (None, None) without one."""
    for module in integrations.REGISTRY:
        control = module.remote(device) if hasattr(module, "remote") else None
        if control:
            return module, control
    return None, None


def capabilities(device: dict) -> tuple[dict | None, str | None, str | None]:
    module, control = provider(device)
    if module is not None:
        layout = module.remote_of(device) if hasattr(module, "remote_of") else getattr(module, "REMOTE_LAYOUT", None)
        problem = module.problem(device) if hasattr(module, "problem") else None
        return control, layout, problem
    for module in integrations.REGISTRY:
        if hasattr(module, "remote_availability") and module.remote_availability(device) is not None:
            return None, module.remote_of(device), None
    return None, "pc" if device["kind"] == "computer" else None, None


def build(house: House) -> dict:
    data = house.data
    route = data.get("routing") or {}
    devices = {d["id"]: d for d in house.devices}
    passive = {(room.get("display") or {}).get("device") for room in (route.get("rooms") or {}).values()
               if (room.get("display") or {}).get("passive") is True}
    out = {"version": 1, "rooms": [], "sources": {}, "devices": {}, "initial": {}}
    for ident, device in devices.items():
        row = {"label": device.get("name") or device.get("model") or ident,
               "short": device.get("model") or device.get("name") or ident,
               "kind": device["kind"], "model": device.get("model") or ""}
        control, layout, problem = (None, None, None) if ident in passive else capabilities(device)
        if ident not in passive and layout:
            row["remote"] = layout
        if control:
            row["control"] = control
        if problem:
            row["problem"] = problem
        availability = next((entry for module in integrations.REGISTRY if hasattr(module, "remote_availability")
                             if (entry := module.remote_availability(device)) is not None), None)
        if availability is not None:
            row["availability"] = availability
        out["devices"][ident] = row
    for ident, spec in (route.get("sources") or {}).items():
        if ident not in devices:
            continue
        device = out["devices"][ident]
        row = {"label": spec["label"], "kind": device["kind"], "model": device["model"]}
        if device.get("remote"):
            row["remote"] = device["remote"]
        out["sources"][ident] = row
    entries = route.get("rooms") or {}
    for ident, entry in entries.items():
        physical = entry.get("room", ident)
        sources = list(entry["sources"])
        display = (entry.get("display") or {}).get("device")
        # The room's own `audio`, else its receiver unless on a zone (whose
        # volume is the main zone's), else its display (routing's docstring).
        receiver = entry.get("receiver") or {}
        candidates = [entry.get("audio"), None if receiver.get("zone") else receiver.get("device"), display]
        audio = next((d for d in candidates if d and "volume" in out["devices"][d].get("control", {})), None)
        covered = routing.covered_rooms(data, ident)
        members = [d for d, spec in devices.items() if spec["room"] in covered]
        row = {"id": ident, "label": routing.room_label(data, ident), "sources": sources,
               "display": display, "audio": audio, "devices": members,
               "routeProblem": routing.problem_item_name(ident)}
        if physical != ident:
            row["room"] = physical
        out["rooms"].append(row)
    return out


def script(house: House) -> str:
    return GENERATED + "\nglobalThis.REMOTE_HOUSE = " + json.dumps(build(house), ensure_ascii=True, indent=2) + ";\n"
