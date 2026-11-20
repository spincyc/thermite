"""Read-only signal descriptions and physical paths for the shared remote.

The manifest contains Item names and field descriptions, never live values or
addresses. Reads name providers' explicit diagnostics() Items or a rendered
public route-lifecycle Item. Signal snapshots stay outside the activity stream.
"""

from __future__ import annotations

import copy

import oh_integrations as integrations
from oh_conf import Conf, House, uses

from . import routing


def providers(house: House) -> dict:
    """Combine every enabled integration's read-only descriptions per device."""
    out = {}
    for device in house.devices:
        descriptions = []
        for module in integrations.defining("diagnostics"):
            enabled = uses(device, module.PHASE, module.NAME) or (module.CONTROL and uses(device, "control", module.NAME))
            if enabled and (description := module.diagnostics(device)):
                descriptions.append(copy.deepcopy(description))
        if not descriptions:
            continue
        combined = copy.deepcopy(descriptions[0])
        combined["items"] = list(dict.fromkeys(name for description in descriptions for name in description.get("items", [])))
        combined["providers"] = [{"kind": description["kind"], "items": description.get("items", []),
                                  "maxAgeSeconds": description.get("maxAgeSeconds", 15)} for description in descriptions]
        combined["readings"] = [{**reading, "provider": description["kind"],
                                  "maxAgeSeconds": reading.get("maxAgeSeconds", description.get("maxAgeSeconds", 15))}
                                 for description in descriptions for reading in description.get("readings", [])]
        out[device["id"]] = combined
    return out


def _path(house: House, room: dict, source_id: str) -> dict:
    """One configured route variant; control selectors are not connector IDs."""
    config = house.data.get("routing") or {}
    devices = house.by_id
    sources = config.get("sources") or {}
    nodes = []

    def node(ident, role, **ports):
        nodes.append({"device": ident, "label": house.label(devices[ident]), "role": role, **ports})

    node(source_id, "source")
    local_display = source_id in (room.get("display", {}).get("sources") or {})
    local_receiver = source_id in (room.get("receiver", {}).get("sources") or {})
    switch = room.get("hdmi_switch") or {}
    local_switch = source_id in (switch.get("inputs") or {})
    if not (local_display or local_receiver or local_switch) and "hdmi_output" in room:
        source_port = sources[source_id].get("hdmi_input")
        if source_port is not None:
            node(config["hdmi_matrix"], "matrix", input=source_port, inputKind="port", output=room["hdmi_output"])
    if switch and not (local_display or local_receiver):
        port = switch.get("inputs", {}).get(source_id, switch.get("matrix_input"))
        if port is not None:
            node(switch["device"], "switch", input=port, inputKind="port", output=switch.get("output", 0))
    for role in ("receiver", "display"):
        stage = room.get(role)
        if not stage or (role == "receiver" and local_display):
            continue
        if stage.get("passive") is True:
            node(stage["device"], role)
            continue
        port = (stage.get("sources") or {}).get(source_id,
                (stage.get("input_by_source") or {}).get(source_id, stage.get("input")))
        node(stage["device"], role, input=port, inputKind="selector", zone=stage.get("zone") or "main")
    path = {"label": sources[source_id]["label"], "nodes": nodes, "carries": routing.carried(room, source_id)}
    if room.get("hdmi_audio_output") is not None and not (local_display or local_receiver or local_switch):
        path["audioNodes"] = [copy.deepcopy(nodes[0]),
                              {**copy.deepcopy(nodes[1]), "output": room["hdmi_audio_output"]},
                              *[copy.deepcopy(node) for node in nodes if node["role"] == "receiver"]]
        path["nodes"] = [node for node in nodes if node["role"] != "receiver"]
    audio = room.get("audio")
    if audio and path["carries"] != "display" and not any(node["device"] == audio for node in path_nodes(path)):
        endpoint = {"device": audio, "label": house.label(devices[audio]), "role": "audio"}
        if room.get("audio_input") is not None:
            endpoint.update(input=room["audio_input"], inputKind="selector")
        link = room.get("audio_link") or {}
        attached = False
        for chain in (path.get("audioNodes", []), path["nodes"]):
            for index, current in enumerate(chain):
                if current["device"] == (link.get("from") or {}).get("device"):
                    branch = copy.deepcopy(chain[:index + 1])
                    branch[-1].update(output=link["from"]["port"], outputTransport=link["transport"])
                    path["audioNodes"] = [*branch, {**endpoint, "inputTransport": link["transport"]}]
                    attached = True
                    break
            if attached:
                break
        if not attached:
            path["audioEndpoints"] = [{**endpoint, "wiring": "unknown"}]
    return path


def variants(path: dict) -> list[dict]:
    """The normal route and every declared alternative, without recursion."""
    return [path, *(path.get("variants") or {}).values()]


def path_nodes(path: dict) -> list[dict]:
    return path["nodes"] + path.get("audioNodes", []) + path.get("audioEndpoints", [])


def _usb(house: House, room_id: str, source_id: str) -> list[dict]:
    """The room's DEVICE ports assigned to this source's HOST on each matrix."""
    config = house.data["routing"]
    room = config["rooms"][room_id]
    source = config["sources"][source_id]
    only = (room.get("usb_only") or {}).get(source_id)
    assignments = []
    for matrix, ports in sorted((room.get("usb") or {}).items()):
        moved = ports if only is None else only.get(matrix, [])
        host = routing.usb_host(source, matrix, config["usb_off_host"])
        if moved and host != config["usb_off_host"]:
            assignments.append({"device": matrix, "label": house.label(house.by_id[matrix]),
                                "host": host, "ports": sorted(moved)})
    return assignments


def _rooms(house: House) -> dict:
    """Configured route variants before passive observers are inserted."""
    config = house.data.get("routing") or {}
    rooms = {}
    sources = config.get("sources") or {}
    for room_id, room in (config.get("rooms") or {}).items():
        paths = {}
        synced = routing.sync_sources(house, room_id)
        for source_id in room.get("sources") or sources:
            if source_id not in sources:
                continue
            path = _path(house, room, source_id)
            if source_id in synced:
                leader = config["rooms"][room["receiver"]["sync_with"]]
                alternate = {**room, "hdmi_output": leader["hdmi_output"],
                             "receiver": {**room["receiver"], "input": synced[source_id], "input_by_source": {}}}
                path["variants"] = {"synced": _path(house, alternate, source_id)}
            paths[source_id] = path
        rooms[room_id] = {"label": routing.room_label(house, room_id), "sources": paths}
        if synced:
            rooms[room_id]["routeStateItem"] = f"loc_{room_id}_source_lifecycle"
    return rooms


_OBSERVER_MATCH = {"after_port": (0, "output"), "before_port": (1, "input"),
                   "after_zone": (0, "zone"), "before_zone": (1, "zone")}


def _edge(before: dict, after: dict) -> tuple:
    return (before["device"], before.get("output"), before.get("zone"),
            after["device"], after.get("input"), after.get("zone"))


def _matches(observer: dict, before: dict, after: dict) -> bool:
    nodes = (before, after)
    return (observer["after"] == before["device"] and observer["before"] == after["device"]
            and all(nodes[side].get(field) == observer[key]
                    for key, (side, field) in _OBSERVER_MATCH.items() if key in observer))


def validate(data: dict) -> list[str]:
    """Observers annotate one existing adjacent edge; they never route it."""
    config = data.get("diagnostics")
    if config is None:
        return []
    if not isinstance(config, dict):
        return ["diagnostics: must be a mapping"]
    problems = [f"diagnostics: unknown field {name!r}" for name in config if name != "observers"]
    observers = config.get("observers", [])
    if not isinstance(observers, list):
        return problems + ["diagnostics.observers: must be a list"]
    devices = {device["id"]: device for device in data.get("devices") or [] if isinstance(device, dict) and device.get("id")}
    house = House.unchecked(data)
    try:
        rooms = _rooms(house)
        paths = [variant for room in rooms.values() for path in room["sources"].values() for variant in variants(path)]
        edges = [(a, b) for path in paths for chain in (path["nodes"], path.get("audioNodes", []))
                 for a, b in zip(chain, chain[1:], strict=False)]
        routed = {node["device"] for path in paths for node in path_nodes(path)}
        described = providers(house)
    except (KeyError, TypeError, AttributeError, IndexError):
        # The owning routing/inventory validators report malformed paths.
        edges, routed, described = None, set(), {}
    seen_devices, seen_edges = set(), set()
    for index, observer in enumerate(observers):
        where = f"diagnostics.observers[{index}]"
        required = {"device", "after", "before"}
        if (not isinstance(observer, dict) or not required <= set(observer)
                or set(observer) - required - set(_OBSERVER_MATCH)):
            problems.append(f"{where}: must name device, after and before, with optional port and zone qualifiers")
            continue
        unknown = [key for key in ("device", "after", "before")
                   if not isinstance(observer[key], str) or observer[key] not in devices]
        if unknown:
            problems.extend(f"{where}.{key}: must name a known device" for key in unknown)
            continue
        invalid = [key for key in _OBSERVER_MATCH if key in observer and not routing.signal_port(observer[key])]
        if invalid:
            problems.extend(f"{where}.{key}: must be a nonempty connector/zone name or a nonnegative integer" for key in invalid)
            continue
        ident, after, before = (observer[key] for key in ("device", "after", "before"))
        if len({ident, after, before}) != 3:
            problems.append(f"{where}: observer and both endpoints must be different devices")
        if ident in seen_devices:
            problems.append(f"{where}.device: observer is already placed")
        matched = {_edge(a, b) for a, b in edges or [] if _matches(observer, a, b)}
        if matched & seen_edges:
            problems.append(f"{where}: edge already has an observer")
        seen_devices.add(ident)
        seen_edges.update(matched)
        if ident in routed:
            problems.append(f"{where}.device: observer must not also be a routing device")
        if ident not in described:
            problems.append(f"{where}.device: must have an enabled diagnostics provider")
        if edges is not None and not matched:
            problems.append(f"{where}: after and before must be an existing adjacent signal-path edge")
        elif len(matched) > 1:
            problems.append(f"{where}: device pair is ambiguous; qualify its ports or zones")
    return problems


def manifest(house: House, conf: Conf) -> dict:
    """Configured AV paths and USB assignments; confirmed readback selects the AV variant.
    Built once per conf (Conf.derived)."""
    return conf.derived("remote_diagnostics", lambda: _manifest(house, conf))


def _manifest(house: House, conf: Conf) -> dict:
    descriptions = providers(house)
    devices = house.by_id
    # A descriptor cannot create an arbitrary REST read or refer to an Item
    # its integration did not render.
    for ident, description in descriptions.items():
        description["items"] = [name for name in description.get("items", []) if conf.item(name)]
        description["readings"] = [reading for reading in description.get("readings", [])
                                   if reading.get("item") in description["items"]]
        for provider in description.get("providers", []):
            provider["items"] = [name for name in provider["items"] if name in description["items"]]
        description["label"] = house.label(devices[ident])
    rooms = _rooms(house)
    observers = (house.data.get("diagnostics") or {}).get("observers", [])
    for room_id, room in rooms.items():
        if room.get("routeStateItem") and not conf.item(room["routeStateItem"]):
            del room["routeStateItem"]
        for source_id, path in room["sources"].items():
            if usb := _usb(house, room_id, source_id):
                path["usb"] = usb
            for variant in variants(path):
                for key in ("nodes", "audioNodes"):
                    if key not in variant:
                        continue
                    nodes, observed = variant[key], []
                    for index, current in enumerate(nodes):
                        observed.append(current)
                        if index + 1 < len(nodes):
                            for observer in observers:
                                if _matches(observer, current, nodes[index + 1]):
                                    ident = observer["device"]
                                    inserted = {"device": ident, "label": house.label(devices[ident]), "role": "observer"}
                                    if current.get("outputTransport"):
                                        inserted["outputTransport"] = current["outputTransport"]
                                    observed.append(inserted)
                    variant[key] = observed
    return {"version": 1, "devices": descriptions, "rooms": rooms}
