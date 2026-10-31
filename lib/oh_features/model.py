"""The semantic model: a Location per room, an Equipment group per device."""

from __future__ import annotations

from oh_conf import Conf, House
from oh_semantic import KINDS

NAME = "model"
FILE = "yaml/model.yaml"
HEADER = (
    "Semantic model: one Location per room in instance/devices.yaml and one",
    "Equipment group per device, named by its id. Point Items live in the",
    "per-integration files and join their device's group. A room without a",
    "tag (such as unassigned) is a plain Group, deliberately not a Location.",
)


def location(room: str) -> str:
    return f"loc_{room}"


def validate(data: dict) -> list[str]:
    return []


def render(house: House, conf: Conf) -> None:
    used: set[str] = set()
    for device in house.devices:
        room = device["room"]
        while room in house.rooms and room not in used:
            used.add(room)
            room = house.rooms[room].get("in")
    out = conf.yaml_file(FILE, HEADER)
    for room, entry in house.rooms.items():
        if room not in used:
            continue
        item: dict = {"type": "Group", "label": entry["label"]}
        if entry.get("in"):
            item["groups"] = [location(entry["in"])]
        if entry.get("tag"):
            item["tags"] = [entry["tag"]]
        out.items[location(room)] = item
    for device in house.devices:
        out.items[device["id"]] = {
            "type": "Group",
            "label": house.label(device),
            "groups": [location(device["room"])],
            "tags": [KINDS[device["kind"]]],
        }
