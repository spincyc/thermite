"""Public remote data derived from inventory; optional legacy presentation only.

The old source preview is a data object, not executable configuration. Its
labels, ordering and explicit device subsets can remain presentation hints.
Control Items and limits always come from the current integration contracts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

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


def presentation(path: Path) -> dict:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {} if not text or text.startswith(GENERATED) else literal(text)


def capabilities(device: dict) -> tuple[dict | None, str | None, str | None]:
    for module in integrations.REGISTRY:
        control = module.remote(device) if hasattr(module, "remote") else None
        if control is None:
            continue
        layouts = {"sony_ip_control": "sony_tv", "sony_adcp": "sony_projector", "pyatv_http": "apple_tv",
                   "denonmarantz_http": "marantz_avr", "wiim_http": "wiim", "sonos": "sonos"}
        layout = module.remote_of(device) if hasattr(module, "remote_of") else layouts.get(module.NAME)
        problem = module.problem(device) if hasattr(module, "problem") else None
        return control, layout, problem
    return None, "pc" if device["kind"] == "computer" else None, None


def build(house: House, hints: dict | None = None) -> dict:
    hints = hints or {}
    data = house.data
    route = data.get("routing") or {}
    devices = {d["id"]: d for d in house.devices}
    passive = {(room.get("display") or {}).get("device") for room in (route.get("rooms") or {}).values()
               if (room.get("display") or {}).get("passive") is True}
    old_devices = hints.get("devices") or {}
    out = {"version": 1, "rooms": [], "sources": {}, "devices": {}, "initial": {}}
    for ident, device in devices.items():
        hint = old_devices.get(ident) or {}
        row = {"label": hint.get("label") or device.get("name") or device.get("model") or ident,
               "short": hint.get("short") or device.get("model") or device.get("name") or ident,
               "kind": device["kind"], "model": hint.get("model") or device.get("model") or ""}
        control, layout, problem = (None, None, None) if ident in passive else capabilities(device)
        if ident not in passive and (hint.get("remote") or layout):
            row["remote"] = hint.get("remote") or layout
        if control:
            row["control"] = control
        if problem:
            row["problem"] = problem
        out["devices"][ident] = row
    for ident, spec in (route.get("sources") or {}).items():
        if ident not in devices:
            continue
        hint = (hints.get("sources") or {}).get(ident) or {}
        device = out["devices"][ident]
        row = {"label": hint.get("label") or spec["label"], "kind": device["kind"], "model": hint.get("model") or device["model"]}
        if hint.get("remote") or device.get("remote"):
            row["remote"] = hint.get("remote") or device["remote"]
        out["sources"][ident] = row
    entries = route.get("rooms") or {}
    old_rooms = {r["id"]: r for r in hints.get("rooms") or [] if isinstance(r, dict) and "id" in r}
    order = [r for r in old_rooms if r in entries] + [r for r in entries if r not in old_rooms]
    for ident in order:
        entry, hint = entries[ident], old_rooms.get(ident) or {}
        physical = entry.get("room", ident)
        offered = entry["sources"]
        sources = [s for s in hint.get("sources") or [] if s in offered]
        sources += [s for s in offered if s not in sources]
        display = (entry.get("display") or {}).get("device")
        if not display and hint.get("display") in devices:
            display = hint["display"]
        audio = hint.get("audio") if hint.get("audio") in devices else None
        if not audio:
            candidates = [(entry.get("receiver") or {}).get("device"), display]
            audio = next((d for d in candidates if d and "volume" in out["devices"][d].get("control", {})), None)
        members = [d for d in hint.get("devices") or [] if d in devices]
        if "devices" not in hint:
            members = [d for d, spec in devices.items() if spec["room"] == physical]
        row = {"id": ident, "label": routing.room_label(data, ident), "sources": sources,
               "display": display, "audio": audio, "devices": members,
               "routeProblem": routing.problem_item_name(ident)}
        if physical != ident:
            row["room"] = physical
        selected_order = [s for s in hint.get("order") or [] if s in sources or s == "off"]
        if selected_order:
            row["order"] = selected_order + [s for s in sources if s not in selected_order]
        controls = []
        for control in hint.get("controls") or []:
            if (not isinstance(control, dict) or control.get("device") not in devices
                    or not all(isinstance(control.get(k), str) for k in ("id", "label"))):
                continue
            actions = [list(pair) for pair in control.get("actions") or []
                       if isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) for v in pair)]
            controls.append({"id": control["id"], "label": control["label"], "device": control["device"], "actions": actions})
        if controls:
            row["controls"] = controls
        out["rooms"].append(row)
        initial = (hints.get("initial") or {}).get(ident)
        if initial in sources:
            out["initial"][ident] = initial
    return out


def script(house: House, hints: dict | None = None) -> str:
    return GENERATED + "\nglobalThis.REMOTE_HOUSE = " + json.dumps(build(house, hints), ensure_ascii=True, indent=2) + ";\n"
