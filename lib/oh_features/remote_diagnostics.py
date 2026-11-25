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
from .routing import hops


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


def _path_from_hop(house: House, room_path: hops.RoomPath, *, synced: bool = False) -> dict:
    """Render one RoomPath (or its synced variant) into the diagnostics path shape."""
    config = house.data.get("routing") or {}
    devices = house.by_id
    sources = config.get("sources") or {}
    source_id = room_path.choice
    sync = room_path.sync if synced else None

    def make_node(ident: str, role: str, **ports) -> dict:
        return {"device": ident, "label": house.label(devices[ident]), "role": role, **ports}

    nodes: list[dict] = [make_node(source_id, "source")]
    audio_matrix_hop = next((h for h in room_path.matrix_hops() if h.role == "audio" and h.active), None)
    video_matrix_hop = next((h for h in room_path.matrix_hops() if h.role == "video" and h.active), None)
    split_audio = audio_matrix_hop is not None and sync is None

    if sync is not None:
        # Synced variant routes via the leader's matrix output and drops follower switches.
        if video_matrix_hop is not None:
            nodes.append(make_node(
                video_matrix_hop.device, "matrix",
                input=sync.source_hdmi_input, inputKind="port", output=sync.leader_hdmi_output,
            ))
    else:
        if video_matrix_hop is not None:
            nodes.append(make_node(
                video_matrix_hop.device, "matrix",
                input=video_matrix_hop.input_value, inputKind="port", output=video_matrix_hop.output,
            ))
        for shop in room_path.switch_hops():
            if shop.active and shop.input_value is not None:
                nodes.append(make_node(
                    shop.device, "switch",
                    input=shop.input_value, inputKind="port",
                    output=shop.output if shop.output is not None else 0,
                ))

    receiver_nodes: list[dict] = []
    active_displays: list[dict] = []
    for stage in room_path.active_stage_hops():
        if stage.passive:
            stage_node = make_node(stage.device, stage.role)
        else:
            sel = sync.leader_input_value if (sync is not None and stage.role == "receiver") else stage.input_value
            stage_node = make_node(stage.device, stage.role, input=sel, inputKind="selector", zone=stage.zone or "main")
        if stage.role == "receiver":
            if split_audio:
                receiver_nodes.append(stage_node)
            else:
                nodes.append(stage_node)
        elif stage.role == "soundbar":
            nodes.append(stage_node)
        else:
            active_displays.append(stage_node)

    display_branches: list[list[dict]] = []
    if active_displays:
        prefix = copy.deepcopy(nodes)
        nodes.append(active_displays[0])
        for extra_display in active_displays[1:]:
            display_branches.append([*copy.deepcopy(prefix), extra_display])

    path: dict = {"label": sources[source_id]["label"], "nodes": nodes, "carries": room_path.carries}
    if display_branches:
        path["displayBranches"] = display_branches
    if split_audio and audio_matrix_hop is not None and video_matrix_hop is not None:
        matrix_audio_node = make_node(
            audio_matrix_hop.device, "matrix",
            input=audio_matrix_hop.input_value, inputKind="port", output=audio_matrix_hop.output,
        )
        path["audioNodes"] = [copy.deepcopy(nodes[0]), matrix_audio_node, *receiver_nodes]

    for branch_hop in room_path.audio_branch_hops():
        endpoint: dict = {"device": branch_hop.device, "label": house.label(devices[branch_hop.device]), "role": "audio"}
        if branch_hop.input_value is not None:
            endpoint.update(input=branch_hop.input_value, inputKind="selector")
        attached = False
        if branch_hop.from_device is not None:
            chains = [path.get("audioNodes", []), path["nodes"], *path.get("displayBranches", [])]
            for chain in chains:
                for index, current in enumerate(chain):
                    if current["device"] == branch_hop.from_device:
                        branch = copy.deepcopy(chain[:index + 1])
                        branch[-1].update(output=branch_hop.from_port, outputTransport=branch_hop.transport)
                        path["audioNodes"] = [*branch, {**endpoint, "inputTransport": branch_hop.transport}]
                        attached = True
                        break
                if attached:
                    break
        if not attached:
            path["audioEndpoints"] = [{**endpoint, "wiring": "unknown"}]
    return path


def _path(house: House, room: dict, source_id: str) -> dict:
    """One configured route variant; control selectors are not connector IDs."""
    config = house.data.get("routing") or {}
    rooms = config.get("rooms") or {}
    room_id = next((ident for ident, spec in rooms.items() if spec is room), None)
    if room_id is not None:
        room_path = hops.compile_room_path(house, room_id, source_id)
        return _path_from_hop(house, room_path)
    scratch = {**house.data, "routing": {**config, "rooms": {**rooms, "_tmp": room}}}
    room_path = hops.compile_room_path_data(scratch, "_tmp", source_id, devices=house.by_id)
    return _path_from_hop(house, room_path)


def variants(path: dict) -> list[dict]:
    """The normal route and every declared alternative, without recursion."""
    return [path, *(path.get("variants") or {}).values()]


def path_nodes(path: dict) -> list[dict]:
    extra = [node for branch in path.get("displayBranches", []) for node in branch]
    return path["nodes"] + extra + path.get("audioNodes", []) + path.get("audioEndpoints", [])


def _usb(house: House, room_id: str, source_id: str) -> list[dict]:
    """The room's DEVICE ports assigned to this source's HOST on each matrix."""
    room_path = hops.compile_room_path(house, room_id, source_id)
    by_matrix: dict[str, tuple[int, list[int]]] = {}
    for uhop in room_path.usb_hops():
        if not uhop.active:
            continue
        if uhop.device not in by_matrix:
            by_matrix[uhop.device] = (uhop.host_value, [])
        by_matrix[uhop.device][1].append(uhop.port)
    return [
        {"device": matrix, "label": house.label(house.by_id[matrix]), "host": host, "ports": sorted(ports)}
        for matrix, (host, ports) in sorted(by_matrix.items())
    ]


def _rooms(house: House) -> dict:
    """Configured route variants before passive observers are inserted."""
    config = house.data.get("routing") or {}
    rooms = {}
    sources = config.get("sources") or {}
    for room_id, room in (config.get("rooms") or {}).items():
        paths = {}
        has_synced = False
        for source_id in room.get("sources") or sources:
            if source_id not in sources:
                continue
            room_path = hops.compile_room_path(house, room_id, source_id)
            path = _path_from_hop(house, room_path)
            if room_path.sync is not None:
                has_synced = True
                path["variants"] = {"synced": _path_from_hop(house, room_path, synced=True)}
            paths[source_id] = path
        rooms[room_id] = {"label": routing.room_label(house, room_id), "sources": paths}
        if has_synced:
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
        edges = [(a, b) for path in paths
                 for chain in (path["nodes"], *path.get("displayBranches", ()), path.get("audioNodes", ()))
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
                def insert_observers(nodes: list[dict]) -> list[dict]:
                    observed = []
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
                    return observed

                for key in ("nodes", "audioNodes"):
                    if key in variant:
                        variant[key] = insert_observers(variant[key])
                if "displayBranches" in variant:
                    variant["displayBranches"] = [insert_observers(branch) for branch in variant["displayBranches"]]
    return {"version": 1, "devices": descriptions, "rooms": rooms}
