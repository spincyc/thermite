"""Compile server authority from inventory/providers, never browser presentation.

This module is pure: rendering and compilation make no network requests and
import no identity framework. Only factory() lazily imports RuntimeContract.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import oh_integrations as integrations
from oh_conf import Conf, House, is_guarded, parse_options
from oh_features import remote, remote_activity, remote_cameras, remote_diagnostics, routing

PRIVATE = re.compile(r"(?:^|_)(?:password|passwd|secret|token|psk|credential|authorization|auth)(?:_|$)", re.I)
TEXT = {"kind": "text", "max_length": 192, "pattern": r"[^\x00-\x1f\x7f-\x9f]{1,192}"}
PAIR_FIELDS = {"device", "integration", "label", "model", "room", "order", "status", "length", "chars", "again", "hint", "confirm", "result"}


def readable(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)) and not PRIVATE.search(name) and (
        "_pairing" not in name or name.endswith(("_pairing_status", "_pairing_result")))


def capabilities(device: dict):
    for module in integrations.REGISTRY:
        control = module.remote(device) if hasattr(module, "remote") else None
        if control:
            return module, copy.deepcopy(control)
    return None, {}


def keys(module, device):
    provider = getattr(module, "command_keys", None) or getattr(module, "keys", None)
    values = provider(device) if callable(provider) else getattr(module, "KEYS", {})
    return sorted(key for key in values if isinstance(key, str)) if isinstance(values, (dict, tuple, list, set)) else []


def _state(name, item):
    if not readable(name):
        return None
    if name.endswith("_pairing_status"):
        return {"kind": "pairing_status"}
    if name.endswith("_pairing_result"):
        return {"kind": "pairing_result"}
    if name.endswith("_problem"):
        return {"kind": "problem"}
    description = (item.get("metadata", {}).get("stateDescription", {}).get("config") or {})
    command = item.get("metadata", {}).get("commandDescription", {}).get("config") or {}
    options = [value for value, _ in parse_options(description.get("options") or command.get("options") or "") if value]
    if options:
        return {"kind": "enum", "values": options}
    kind = item.get("type", "")
    if kind == "Switch":
        return {"kind": "switch", "values": ["ON", "OFF"]}
    if kind == "Contact":
        return {"kind": "enum", "values": ["OPEN", "CLOSED"]}
    if kind == "Dimmer":
        return {"kind": "number", "min": 0, "max": 100}
    if kind.startswith("Number"):
        return {"kind": "number", "min": description.get("min", -1e13), "max": description.get("max", 1e13)}
    if kind == "DateTime":
        return {"kind": "datetime"}
    if kind == "String" and "Status" in item.get("tags", ()) and {"Info", "App"} & set(item.get("tags", ())):
        return copy.deepcopy(TEXT)
    # A declared String without a finite recipe is deliberately unreadable.
    return None


def _json_leaf(states, item, path, leaf):
    spec = states.setdefault(item, {"kind": "json", "fields": {
        "version": {"kind": "number", "min": 1, "max": 1},
        "observedAt": {"kind": "number", "min": 0, "max": 1e13}}})
    fields = spec["fields"]
    for key in path[:-1]:
        fields = fields.setdefault(str(key), {"kind": "object", "fields": {}})["fields"]
    fields[str(path[-1])] = copy.deepcopy(leaf)


def compile(house: House, conf: Conf, *, asset_root: Path | None = None) -> dict:
    """Serializable RuntimeContract kwargs, derived only from server-side facts."""
    devices = {device["id"]: device for device in house.devices}
    route = house.data.get("routing") or {}
    room_specs = route.get("rooms") or {}
    table = routing.table(house.data) if room_specs else {"rooms": {}, "edid": None, "off": "off"}
    all_items = {name: item for file in conf.yaml.values() for name, item in file.items.items()}
    owner = {name: next((ident for ident in item.get("groups", ()) if ident in devices), None)
             for name, item in all_items.items()}
    states = {name: spec for name, item in all_items.items() if (spec := _state(name, item)) is not None}
    native = {ident: capabilities(device) for ident, device in devices.items()}
    # A known provider's keys may be observed without granting them in any room.
    for ident, (module, control) in native.items():
        item = control.get("key")
        if module and item in all_items and (allowed := keys(module, devices[ident])):
            states[item] = {"kind": "enum", "values": allowed}
    display_owners = {}
    for ident, spec in room_specs.items():
        display_owners.setdefault((spec.get("display") or {}).get("device"), set()).add(ident)
    consumers = {ident: {room for room, spec in room_specs.items() if ident in spec["sources"]}
                 for ident in devices}
    plans, item_rooms = {}, {}
    for room, spec in table["rooms"].items():
        plans[room] = {}
        for choice in spec["choices"]:
            steps = [step for field in ("power", "choices", "inputs", "after") for step in spec[field][choice]]
            plans[room][choice] = steps
            for name, _ in steps:
                item_rooms.setdefault(name, set()).add(room)

    def resources(names):
        return sorted({"device:" + owner[name] for name in names if owner.get(name)})

    def affected_device(ident):
        result = set(consumers.get(ident, ()))
        for room, spec in room_specs.items():
            if any((spec.get(stage) or {}).get("device") == ident for stage in routing.STAGES):
                result.add(room)
        return result

    rooms = {}
    for room, entry in room_specs.items():
        actions = {}
        display = entry.get("display") or {}
        physical = {routing.home(house.data, room)}
        if display.get("device") in devices:
            physical.add(devices[display["device"]].get("room"))
        # A building-level routed zone includes its explicitly nested rooms.
        # This is inventory containment, not an inference from names or UI hints.
        if house.rooms.get(routing.home(house.data, room), {}).get("tag") == "Building":
            changed = True
            while changed:
                additions = {ident for ident, spec in house.rooms.items() if spec.get("in") in physical}
                changed = not additions <= physical
                physical.update(additions)
        targets = set(entry["sources"])
        targets.update(spec.get("device") for stage in routing.STAGES if (spec := entry.get(stage)) and not spec.get("passive"))
        # Only endpoint audio devices join the explicit source/display/receiver roles.
        # An aliased TV screen does not gain the desk amplifier beside it.
        targets.update(ident for ident, device in devices.items() if device.get("room") in physical
                       and (device.get("kind") == "soundbar" or device.get("kind") == "streaming_amp" and "room" not in entry))
        # Legacy inventories can have a controlled TV in a room whose route is audio-only.
        targets.update(ident for ident, device in devices.items() if device.get("room") == routing.home(house.data, room)
                       and device.get("kind") in {"tv", "projector"} and not display_owners.get(ident, {room}) - {room})
        targets.discard(None)
        if display.get("passive"):
            targets.discard(display.get("device"))
        for ident in sorted(targets):
            device = devices[ident]
            module, control = native[ident]
            if not module:
                continue
            zone = next((entry[stage].get("zone") for stage in routing.STAGES
                         if stage in entry and entry[stage].get("device") == ident), None)
            if zone is not None:
                # A zone stage supplies exact power/input; main-zone remote controls do not.
                staged = module.stage(device, zone) if hasattr(module, "stage") else None
                control = {point: staged[point] for point in ("power", "input") if staged and staged.get(point)}
                if staged:
                    control["input"] = {"item": staged["input"], "options": [[value, value] for value in staged["inputs"]]}
            for point, descriptor in control.items():
                if point not in {"power", "input", "key", "volume", "mute"}:
                    continue
                item = descriptor.get("item") if isinstance(descriptor, dict) else descriptor
                if item not in all_items or is_guarded(all_items[item]) or not readable(item):
                    continue
                if point in {"power", "mute"}:
                    spec = {"kind": "switch", "values": ["ON", "OFF"]}
                elif point == "input":
                    spec = {"kind": "enum", "values": [pair[0] for pair in descriptor["options"]]}
                elif point == "key":
                    allowed = keys(module, device)
                    if not allowed:
                        continue
                    spec = {"kind": "key", "values": allowed}
                else:
                    limit = descriptor["max"]
                    native_volume = descriptor.get("native") or {}
                    spec = {"kind": "number", "min": native_volume.get("min", 0), "max": limit,
                            "step": native_volume.get("step", descriptor["step"])}
                    states[item] = {"kind": "number", "min": native_volume.get("min", 0),
                                    "max": native_volume.get("max", getattr(module, "MAX_VOLUME", 100))}
                spec.update(item=item, resources=["device:" + ident])
                affected = item_rooms.get(item, set()) | consumers.get(ident, set())
                if point == "key":
                    affected |= affected_device(ident)
                spec["affected_rooms"] = sorted(affected | {room})
                actions[ident + "." + point] = spec
                if point != "volume":
                    states[item] = {"kind": "enum" if point == "key" else spec["kind"], "values": spec.get("values", [])}
                states[item]["commands"] = True
                if point == "volume" and (mode := descriptor.get("displayMode", {}).get("item")) and mode in all_items:
                    states[mode] = {"kind": "enum", "values": ["absolute", "relative"]}
        # Extra actions must exist as generated Items and carry finite generated options.
        for ident in set((entry.get("on_actions") or {})) | set(entry.get("off_actions") or {}):
            item = routing.action_item(devices, ident)
            descriptor = states.get(item)
            if item not in all_items or not descriptor or descriptor.get("kind") != "enum" or is_guarded(all_items[item]):
                continue
            actions["control." + ident] = {**descriptor, "item": item, "affected_rooms": sorted(item_rooms.get(item, {room})),
                                           "resources": ["device:" + ident]}
        affected_by_value = {}
        for choice, steps in plans[room].items():
            affected = {room}
            for item, _ in steps:
                affected |= item_rooms.get(item, set())
            if choice != "off" and table.get("edid"):
                affected |= consumers.get(choice, set())
            affected_by_value[choice] = sorted(affected)
        actions["source"] = {"item": routing.item_name(room), "kind": "enum", "values": ["off", *entry["sources"]],
                             "resources": resources(name for steps in plans[room].values() for name, _ in steps),
                             "affected_by_value": affected_by_value}
        rooms[room] = {"sources": list(entry["sources"]), "source_item": routing.item_name(room), "actions": actions}
        diagnosis = table["rooms"][room].get("diagnosis")
        if diagnosis and diagnosis.get("item") in all_items:
            checks = diagnosis.get("checks", {})
            states[diagnosis["item"]] = {"kind": "json", "fields": {
                "version": {"kind": "number", "min": 1, "max": 1},
                "device": {"kind": "enum", "values": sorted({row["device"] for row in checks.values()})},
                "label": {"kind": "enum", "values": sorted({row.get("label", "") for row in checks.values()})},
                "model": {"kind": "enum", "values": sorted({row.get("model", "") for row in checks.values()})},
                "item": {"kind": "enum", "values": list(checks)},
                "action": {"kind": "enum", "values": ["read selected input", "read matrix output input", "read power"]},
                "reason": {"kind": "enum", "values": ["unavailable", "unsupported_input"]},
                "reported": {"kind": "enum", "nullable": True, "values": [None, "tv", *[str(x) for x in range(100)], *["hdmi" + str(x) for x in range(1, 9)]]}}}

    for description in remote_diagnostics.providers(house).values():
        for reading in description.get("readings", ()):
            item, path = reading.get("item"), reading.get("path")
            if item in all_items and isinstance(path, list) and path:
                _json_leaf(states, item, path, TEXT)
    pairings = {}
    for name, item in all_items.items():
        metadata = item.get("metadata", {}).get("pairing")
        if metadata:
            config = {key: copy.deepcopy(value) for key, value in metadata.get("config", {}).items() if key in PAIR_FIELDS}
            pairings[name] = {"value": metadata.get("value"), "config": config}
    cameras = remote_cameras.manifest(house, conf)
    camera_items, media = set(), {}
    for camera in cameras["cameras"]:
        camera_items.update(name for name in camera.get("items", {}).values() if name in states)
        snapshot = camera["media"]["snapshot"]
        if not re.fullmatch(r"/unifi/media/image/[A-Za-z0-9:_-]+", snapshot):
            raise ValueError("Camera snapshot must be an exact native path")
        media[snapshot] = {"kind": "snapshot"}
        for value in camera["media"]["streams"].values():
            if not re.fullmatch(r"/unifi/media/play/[A-Za-z0-9:_-]+", value):
                raise ValueError("Camera stream must be an exact native path")
            media[value] = {"kind": "sdp"}
    groups = {cameras["statusGroup"]: sorted(camera_items)} if cameras["cameras"] else {}
    # Subscription approval is not write authority. Derived controller commands
    # remain observable while only the typed room action catalogue can write.
    for name, description in remote_activity.manifest(house, conf)["items"].items():
        if not readable(name):
            continue
        if name not in states:
            options = list(description.get("options", {}))
            states[name] = {"kind": "enum", "values": options} if options else {"kind": "redacted"}
        if description.get("commands") and "_pairing" not in name:
            states[name]["commands"] = True
    assets = {name: {"private": name == "house.js" or name.endswith(".json")
                    or name.endswith(".html") and name != "access.html"} for name in remote.FILES}
    return {"rooms": rooms, "states": states, "routing": table, "house": copy.deepcopy(house.data), "assets": assets,
            "asset_root": str(Path(asset_root).resolve()) if asset_root else None, "pairings": pairings, "groups": groups, "media": media}


def factory(house: House, conf: Conf, **kwargs):
    """Lazy framework import: pure inventory/export tooling never needs Django."""
    from oh_identity.runtime import RuntimeContract
    return RuntimeContract(**compile(house, conf, **kwargs))
