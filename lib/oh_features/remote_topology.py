"""Configured site devices and AV/USB connections for the remote's map.

This is configuration, not discovered wiring or live state. Video paths come
from diagnostics, including its local-source bypasses and passive observers.
Ports retain their native numbering: an edge's fromPort is the preceding
device's configured output, and toPort is the following device's input.
Control selectors carry toPortKind=selector; they are not verified connector
numbers. Unknown ports are omitted rather than inferred from a receiver zone.
Audio endpoints without an explicit attachment have no invented cable edge.

USB edges describe only known source-to-matrix host cables. A route's usb
entries identify the matrix's device ports assigned to that source, respecting
usb_only; an off/uncabled host is omitted. The inventory does not identify the
downstream peripherals, so no matrix-to-peripheral cable is invented.

Rooms group devices by their inventory location. A routed room whose routing
entry names another `room` (routing.home) gives that physical room as its
location and shares its device group. Nodes retain their physical room IDs;
routes retain the logical IDs used by the confirmed Source Items. No addresses, credentials, provider Items
or live readings are copied into this public manifest.
"""

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
        location = routing.home(house, ident) if ident in routed_rooms else ident
        rooms.append({
            "id": ident,
            "label": routing.room_label(house, ident) if ident in routed_rooms else house.room_label(ident),
            "location": location,
            "devices": [node["id"] for node in nodes if node["room"] == location],
        })

    routes: dict = {}
    edges: dict = {}

    def edge(kind, source, target, room, choice, **ports):
        ident = _edge_id(kind, source, target, ports)
        if ident not in edges:
            edges[ident] = {"id": ident, "from": source, "to": target, "kind": kind, **ports,
                            "rooms": set(), "sources": set()}
        edges[ident]["rooms"].add(room)
        edges[ident]["sources"].add(choice)

    def route_path(path):
        result = {"label": path["label"], "nodes": copy.deepcopy(path["nodes"]), "carries": path["carries"]}
        for key in ("audioNodes", "audioEndpoints", "usb"):
            if path.get(key):
                result[key] = copy.deepcopy(path[key])
        if path.get("variants"):
            result["variants"] = {key: route_path(value) for key, value in path["variants"].items()}
        return result

    for room_id, room in sorted(paths.items()):
        routes[room_id] = {}
        for source_id, path in sorted(room["sources"].items()):
            route = route_path(path)
            for variant in remote_diagnostics.variants(path):
                for kind, chain in (("video", variant["nodes"]), ("audio", variant.get("audioNodes", []))):
                    for before, after in zip(chain, chain[1:], strict=False):
                        ports = {}
                        if before.get("output") is not None:
                            ports["fromPort"] = before["output"]
                        if after.get("input") is not None:
                            ports["toPort"] = after["input"]
                            if after.get("inputKind") == "selector":
                                ports["toPortKind"] = "selector"
                        for key, node in (("fromZone", before), ("toZone", after)):
                            if node.get("zone") is not None:
                                ports[key] = node["zone"]
                        if before.get("outputTransport"):
                            ports["transport"] = before["outputTransport"]
                        edge(kind, before["device"], after["device"], room_id, source_id, **ports)
            for connection in route.get("usb", []):
                edge("usb", source_id, connection["device"], room_id, source_id, toPort=connection["host"])
            routes[room_id][source_id] = route

    result = []
    for _, value in sorted(edges.items()):
        result.append({**value, "rooms": sorted(value["rooms"]), "sources": sorted(value["sources"])})
    return {"version": 1, "nodes": nodes, "rooms": rooms, "routes": routes, "edges": result}
