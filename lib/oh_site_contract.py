"""Compile server authority from inventory/providers, never browser presentation.

This module is pure: rendering and compilation make no network requests and
import no identity framework. Only factory() lazily imports RuntimeContract.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from oh_conf import Conf, House, is_guarded, parse_options
from oh_features import remote, remote_activity, remote_cameras, remote_diagnostics, remote_model, remote_system, routing

PRIVATE = re.compile(r"(?:^|_)(?:password|passwd|secret|token|psk|credential|authorization|auth)(?:_|$)", re.I)
TEXT = {"kind": "text", "max_length": 192, "pattern": r"[^\x00-\x1f\x7f-\x9f]{1,192}"}
PAIR_FIELDS = {"device", "integration", "label", "model", "room", "order", "status", "length", "chars", "again", "hint", "confirm", "result"}


def readable(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)) and not PRIVATE.search(name) and (
        "_pairing" not in name or name.endswith(("_pairing_status", "_pairing_result")))


def capabilities(device: dict):
    """remote_model.provider(), with a copy of its control Items ({} without one)."""
    module, control = remote_model.provider(device)
    return module, copy.deepcopy(control or {})


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
        # Its declared scale, else openHAB's own Dimmer range; never rescaled.
        return {"kind": "number", "min": description.get("min", 0), "max": description.get("max", 100)}
    if kind.startswith("Number"):
        return {"kind": "number", "min": description.get("min", -1e13), "max": description.get("max", 1e13)}
    if kind == "DateTime":
        return {"kind": "datetime"}
    if kind == "String" and "Status" in item.get("tags", ()) and {"Info", "App"} & set(item.get("tags", ())):
        # A title, an artist, an app: free text a Guest sees unless an Admin
        # turned that off (oh_identity.security.guests_see_text).
        return {**copy.deepcopy(TEXT), "free_text": True}
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


def history(house: House, conf: Conf, rooms: dict, metrics_port: int | None = None) -> dict:
    """The room history the gateway serves (oh_identity.history): the House
    board's Rooms through the day timeline (oh_metrics.dashboards
    room_history), each zone named by its routing room, and the loopback
    port of the house's Prometheus, which the gateway asks."""
    import oh_metrics
    import oh_ports
    from oh_metrics import dashboards

    spec = dashboards.room_history(oh_metrics.exporter_map(house.data, conf))
    by_item = {room["source_item"]: ident for ident, room in rooms.items()}
    zones = [{"room": by_item[zone["item"]], **zone} for zone in spec["zones"] if zone["item"] in by_item]
    return {"port": oh_ports.checked("prometheus", metrics_port or oh_ports.DEFAULTS["prometheus"]),
            "zones": zones, "values": spec["values"], "labels": spec["labels"]}


def compile(house: House, conf: Conf, *, asset_root: Path | None = None, metrics_port: int | None = None) -> dict:
    """Serializable RuntimeContract kwargs, derived only from server-side facts.
    metrics_port is the house's Prometheus port (host.yaml), else its default."""
    devices = {device["id"]: device for device in house.devices}
    route = house.data.get("routing") or {}
    room_specs = route.get("rooms") or {}
    table = routing.table(house.data) if room_specs else {"rooms": {}, "edid": None, "off": "off"}
    all_items = {name: item for file in conf.yaml.values() for name, item in file.items.items()}

    def commandable(name):
        item = all_items.get(name)
        if item is None:
            return False
        readonly = ((item.get("metadata") or {}).get("stateDescription") or {}).get("config", {}).get("readOnly") is True
        return not is_guarded(item) or name in conf.executor_controls and not readonly
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
        # A building-level routed zone includes its explicitly nested rooms.
        physical = routing.covered_rooms(house.data, room)
        targets = set(entry["sources"])
        targets.add(entry.get("audio"))
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
                if not commandable(item) or not readable(item):
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
                    # The device's own scale, as its integration declares it
                    # (VOLUME): commands and readback alike, never capped or rescaled.
                    spec = {"kind": "number", "min": descriptor["min"], "max": descriptor["max"],
                            "step": descriptor["step"]}
                    states[item] = {"kind": "number", "min": descriptor["min"], "max": descriptor["max"]}
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
            if not commandable(item) or not descriptor or descriptor.get("kind") != "enum":
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
                "reported": {"kind": "enum", "nullable": True, "values": [None, *diagnosis["reported"]]}}}

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
    camera_items, media = set(), remote_cameras.media(cameras)
    for camera in cameras["cameras"]:
        camera_items.update(name for name in camera.get("items", {}).values() if name in states)
    groups = {cameras["statusGroup"]: sorted(camera_items)} if cameras["cameras"] else {}
    # Subscription approval is not write authority. Derived controller commands
    # remain observable while only the typed room action catalogue can write.
    activity = remote_activity.manifest(house, conf)["items"]
    for name, description in activity.items():
        if not readable(name):
            continue
        if name not in states:
            options = list(description.get("options", {}))
            states[name] = {"kind": "enum", "values": options} if options else dict(TEXT)
        if states[name]["kind"] == "text":
            # Free text (a playback state, a transport's reply) is free text
            # whichever recipe it came by.
            states[name]["free_text"] = True
        if description.get("commands") and "_pairing" not in name:
            states[name]["commands"] = True
    assets = {name: {"private": name == "house.js" or name.endswith(".json")
                    or name.endswith(".html") and name != "access.html",
                    **({"admin": True} if name in remote_system.ADMIN_ASSETS else {})} for name in remote.FILES}
    return {"rooms": rooms, "states": states, "routing": table, "house": copy.deepcopy(house.data), "assets": assets,
            "asset_root": str(Path(asset_root).resolve()) if asset_root else None, "pairings": pairings, "groups": groups, "media": media,
            "views": views(house, rooms, activity, pairings), "history": history(house, conf, rooms, metrics_port)}


def views(house: House, rooms: dict, activity: dict, pairings: dict) -> dict:
    """The Items each client event stream view carries, by verb (oh_identity
    topics.VIEWS). The remote takes its rooms' Sources as stateupdated, since
    a choice ends at the next read-back even when it repeats the value, and
    every other Item it reads (house.js controls, problems, route problems and
    what each device plays) and the activity feed's Items as statechanged,
    with the feed's commands.
    Home and Topology take only the rooms' Sources; Pairing its status and
    result Items, whose repeated updates answer a code. The gateway keeps only
    public topics of these."""
    model = remote_model.build(house)
    sources = sorted(room["source_item"] for room in rooms.values())
    read = {room["routeProblem"] for room in model["rooms"]}
    for device in model["devices"].values():
        control = device.get("control") or {}
        read.update(control[field] for field in ("power", "mute") if isinstance(control.get(field), str))
        read.update(control[field]["item"] for field in ("volume", "input", "playback") if isinstance(control.get(field), dict))
        read.add(((control.get("volume") or {}).get("displayMode") or {}).get("item"))
        read.add(device.get("problem"))
        read.update((device.get("playing") or {}).values())
    read.update(name for name, description in activity.items() if description.get("states"))
    read.discard(None)
    status = {config[field] for spec in pairings.values() for config in (spec["config"],)
              for field in ("status", "result") if isinstance(config.get(field), str)}
    return {"remote": {"stateupdated": sources, "statechanged": sorted(read - set(sources)),
                       "command": sorted(name for name, description in activity.items() if description.get("commands"))},
            "home": {"statechanged": sources},
            "pair": {"stateupdated": sorted(status)}}


def factory(house: House, conf: Conf, **kwargs):
    """Lazy framework import: pure inventory/export tooling never needs Django."""
    from oh_identity.runtime import RuntimeContract
    return RuntimeContract(**compile(house, conf, **kwargs))
