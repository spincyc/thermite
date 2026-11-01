"""Read-only signal descriptions and physical paths for the shared remote.

The manifest contains Item names and field descriptions, never live values or
addresses. Only providers' explicit diagnostics() Items may be fetched; these
snapshots are deliberately excluded from the activity stream.
"""

from __future__ import annotations

import copy

import oh_integrations as integrations
from oh_conf import Conf, House, uses

from . import routing


def providers(house: House) -> dict:
    """The integration-owned, read-only diagnostics schema per device."""
    out = {}
    for device in house.devices:
        for module in integrations.REGISTRY:
            describe = getattr(module, "diagnostics", None)
            enabled = uses(device, module.PHASE, module.NAME) or (getattr(module, "CONTROL", False) and uses(device, "control", module.NAME))
            if describe and enabled and (description := describe(device)):
                out[device["id"]] = copy.deepcopy(description)
                break
    return out


def _rooms(house: House) -> dict:
    """Configured signal paths before passive observers are inserted."""
    config = house.data.get("routing") or {}
    devices = {device["id"]: device for device in house.devices}
    rooms = {}
    sources = config.get("sources") or {}
    for room_id, room in (config.get("rooms") or {}).items():
        paths = {}
        for source_id in room.get("sources") or sources:
            if source_id not in sources:
                continue
            nodes = []

            def node(ident, role, **ports):
                nodes.append({"device": ident, "label": house.label(devices[ident]), "role": role, **ports})

            node(source_id, "source")
            # A local connection joins the chain at that stage. Everything
            # before it is bypassed, including a parked master-matrix output.
            local_display = source_id in (room.get("display", {}).get("sources") or {})
            local_receiver = source_id in (room.get("receiver", {}).get("sources") or {})
            switch = room.get("hdmi_switch") or {}
            local_switch = source_id in (switch.get("inputs") or {})
            if not (local_display or local_receiver or local_switch) and "hdmi_output" in room:
                source_port = sources[source_id].get("hdmi_input")
                if source_port is not None:
                    node(config["hdmi_matrix"], "matrix", input=source_port, output=room["hdmi_output"])
            if switch and not (local_display or local_receiver):
                port = switch.get("inputs", {}).get(source_id, switch.get("matrix_input"))
                if port is not None:
                    # A simple switch's main output is TX0; matrix outputs
                    # keep their explicitly configured native numbering.
                    node(switch["device"], "switch", input=port, output=switch.get("output", 0))
            for role in ("receiver", "display"):
                stage = room.get(role)
                if not stage or (role == "receiver" and local_display):
                    continue
                if stage.get("passive") is True:
                    node(stage["device"], role)
                    continue
                port = (stage.get("sources") or {}).get(source_id,
                        (stage.get("input_by_source") or {}).get(source_id, stage.get("input")))
                node(stage["device"], role, input=port, zone=stage.get("zone") or "main")
            path = {"label": sources[source_id]["label"], "nodes": nodes}
            if room.get("hdmi_audio_output") is not None and not (local_display or local_receiver or local_switch):
                # Two HDMI cables carry the same source: video bypasses the
                # receiver, while a separate matrix output feeds its audio.
                path["audioNodes"] = [copy.deepcopy(nodes[0]),
                                      {**copy.deepcopy(nodes[1]), "output": room["hdmi_audio_output"]},
                                      *[copy.deepcopy(node) for node in nodes if node["role"] == "receiver"]]
                path["nodes"] = [node for node in nodes if node["role"] != "receiver"]
            paths[source_id] = path
        rooms[room_id] = {"label": routing.room_label(house.data, room_id), "sources": paths}
    return rooms


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
    house = House(data)
    try:
        rooms = _rooms(house)
        edges = {(a["device"], b["device"]) for room in rooms.values() for path in room["sources"].values()
                 for chain in (path["nodes"], path.get("audioNodes", [])) for a, b in zip(chain, chain[1:])}
        routed = {node["device"] for room in rooms.values() for path in room["sources"].values()
                  for node in path["nodes"] + path.get("audioNodes", [])}
        described = providers(house)
    except (KeyError, TypeError, AttributeError):
        # The owning routing/inventory validators report malformed paths.
        edges, routed, described = None, set(), {}
    seen_devices, seen_edges = set(), set()
    for index, observer in enumerate(observers):
        where = f"diagnostics.observers[{index}]"
        if not isinstance(observer, dict) or set(observer) != {"device", "after", "before"}:
            problems.append(f"{where}: must name device, after and before only")
            continue
        unknown = [key for key in ("device", "after", "before")
                   if not isinstance(observer[key], str) or observer[key] not in devices]
        if unknown:
            problems.extend(f"{where}.{key}: must name a known device" for key in unknown)
            continue
        ident, after, before = (observer[key] for key in ("device", "after", "before"))
        if len({ident, after, before}) != 3:
            problems.append(f"{where}: observer and both endpoints must be different devices")
        if ident in seen_devices:
            problems.append(f"{where}.device: observer is already placed")
        if (after, before) in seen_edges:
            problems.append(f"{where}: edge already has an observer")
        seen_devices.add(ident)
        seen_edges.add((after, before))
        if ident in routed:
            problems.append(f"{where}.device: observer must not also be a routing device")
        if ident not in described:
            problems.append(f"{where}.device: must have an enabled diagnostics provider")
        if edges is not None and (after, before) not in edges:
            problems.append(f"{where}: after and before must be an existing adjacent signal-path edge")
    return problems


def manifest(house: House, conf: Conf) -> dict:
    """Configured HDMI paths; a client selects one using confirmed readback."""
    descriptions = providers(house)
    devices = {device["id"]: device for device in house.devices}
    # A descriptor cannot create an arbitrary REST read or refer to an Item
    # its integration did not render.
    for ident, description in descriptions.items():
        description["items"] = [name for name in description.get("items", []) if conf.item(name)]
        description["readings"] = [reading for reading in description.get("readings", [])
                                   if reading.get("item") in description["items"]]
        description["label"] = house.label(devices[ident])
    rooms = _rooms(house)
    observers = {(entry["after"], entry["before"]): entry["device"]
                 for entry in (house.data.get("diagnostics") or {}).get("observers", [])}
    for room in rooms.values():
        for path in room["sources"].values():
            for key in ("nodes", "audioNodes"):
                if key not in path:
                    continue
                nodes, observed = path[key], []
                for index, current in enumerate(nodes):
                    observed.append(current)
                    if index + 1 < len(nodes) and (ident := observers.get((current["device"], nodes[index + 1]["device"]))):
                        observed.append({"device": ident, "label": house.label(devices[ident]), "role": "observer"})
                path[key] = observed
    return {"version": 1, "devices": descriptions, "rooms": rooms}
