"Configured site devices and AV/USB connections for the remote's map.\n\nThis is configuration, not discovered wiring or live state. Video paths come\nfrom diagnostics, including its local-source bypasses and passive observers.\nPorts retain their native numbering: an edge's fromPort is the preceding\ndevice's configured output, and toPort is the following device's input.\nUnknown ports are omitted rather than inferred from a model or receiver zone.\n\nUSB edges describe only known source-to-matrix host cables. A route's usb\nentries identify the matrix's device ports assigned to that source, respecting\nusb_only; an off/uncabled host is omitted. The inventory does not identify the\ndownstream peripherals, so no matrix-to-peripheral cable is invented.\n\nRooms group devices by their inventory location. A logical routed room such\nas example names that physical location in location and shares its device\ngroup. Nodes retain their physical room IDs; routes retain the logical IDs\nused by the confirmed Source Items. No addresses, credentials, provider Items\nor live readings are copied into this public manifest.\n"

from __future__ import annotations

import copy
import hashlib
import json

from oh_conf import Conf, House

from . import remote_diagnostics, routing


def _edge_id(kind: str, source: str, target: str, ports: dict) -> str:
    """A stable identity independent of route order or display labels."""
    identity = json.dumps([kind, source, target, ports], sort_keys=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _usb(config: dict, room: dict, source_id: str) -> list[dict]:
    """Only configured USB assignments for this choice, never parked hosts."""
    source = config.get("sources", {}).get(source_id, {})
    only = (room.get("usb_only") or {}).get(source_id)
    result = []
    for matrix, ports in sorted((room.get("usb") or {}).items()):
        moved = ports if only is None else only.get(matrix, [])
        host = routing.usb_host(source, matrix, config["usb_off_host"])
        if moved and host != config["usb_off_host"]:
            result.append({"device": matrix, "host": host, "ports": sorted(moved)})
    return result


def manifest(house: House, conf: Conf) -> dict:
    """Public, deterministic topology of a validated house's configuration."""
    config = house.data.get("routing") or {}
    routed_rooms = config.get("rooms") or {}
    paths = remote_diagnostics.manifest(house, conf)["rooms"]
    nodes = []
    for device in sorted(house.devices, key=lambda entry: entry["id"]):
        node = {"id": device["id"], "label": house.label(device), "room": device["room"], "kind": device["kind"]}
        for field in ("make", "model"):
            if device.get(field):
                node[field] = device[field]
        nodes.append(node)

    rooms = []
    for ident in sorted(set(house.rooms) | set(routed_rooms)):
        location = routing.home(house.data, ident) if ident in routed_rooms else ident
        rooms.append({
            "id": ident,
            "label": routing.room_label(house.data, ident) if ident in routed_rooms else house.room_label(ident),
            "location": location,
            "devices": [node["id"] for node in nodes if node["room"] == location],
        })

    routes, edges = {}, {}

    def edge(kind, source, target, room, choice, **ports):
        ident = _edge_id(kind, source, target, ports)
        if ident not in edges:
            edges[ident] = {"id": ident, "from": source, "to": target, "kind": kind, **ports,
                            "rooms": set(), "sources": set()}
        edges[ident]["rooms"].add(room)
        edges[ident]["sources"].add(choice)

    for room_id, room in sorted(paths.items()):
        routes[room_id] = {}
        for source_id, path in sorted(room["sources"].items()):
            route = {"label": path["label"], "nodes": copy.deepcopy(path["nodes"])}
            if path.get("audioNodes"):
                route["audioNodes"] = copy.deepcopy(path["audioNodes"])
            for kind, chain in (("video", path["nodes"]), ("audio", path.get("audioNodes", []))):
                for before, after in zip(chain, chain[1:]):
                    ports = {}
                    if before.get("output") is not None:
                        ports["fromPort"] = before["output"]
                    if after.get("input") is not None:
                        ports["toPort"] = after["input"]
                    edge(kind, before["device"], after["device"], room_id, source_id, **ports)
            usb = _usb(config, routed_rooms[room_id], source_id)
            if usb:
                route["usb"] = usb
                for connection in usb:
                    edge("usb", source_id, connection["device"], room_id, source_id, toPort=connection["host"])
            routes[room_id][source_id] = route

    result = []
    for ident, value in sorted(edges.items()):
        result.append({**value, "rooms": sorted(value["rooms"]), "sources": sorted(value["sources"])})
    return {"version": 1, "nodes": nodes, "rooms": rooms, "routes": routes, "edges": result}
