"""The device inventory (instance/devices.yaml) and its agreement with conf/.

The inventory is the single list of devices openHAB may talk to. conf/ names
its Things and Items after inventory ids so that this module can prove every
Thing belongs to a known device at that device's address.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import yaml

from oh_semantic import KINDS, LOCATION_TAGS

ID = re.compile(r"^[a-z][a-z0-9_]*$")
MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")

ROOM_KEYS = {"label", "tag", "in"}

DSL_THING = re.compile(
    r"^\s*(?:Thing|Bridge)\s+([\w-]+:[\w-]+(?::[\w-]+)+)(.*)$", re.MULTILINE)
DSL_ITEM = re.compile(
    r"^\s*(?:Switch|String|Number(?::\w+)?|Dimmer|Contact|Rollershutter|Color|"
    r"DateTime|Location|Player|Image|Call|Group(?::[\w:()]+)?)\s+(\w+)",
    re.MULTILINE)


def load(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def room_label(data: dict, room: str) -> str:
    """A room's display label (its id if it has none)."""
    entry = (data.get("rooms") or {}).get(room)
    return str(entry.get("label") or room) if isinstance(entry, dict) else room


def validate_rooms(rooms: object) -> list[str]:
    """Problems in the rooms mapping: ids, labels, Location tags, nesting."""
    if not isinstance(rooms, dict):
        return ["rooms must map room ids to {label, tag, in}"]
    problems: list[str] = []
    for room, entry in rooms.items():
        where = f"rooms.{room}"
        if not ID.match(str(room)):
            problems.append(f"room id '{room}' is not snake_case")
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping with at least a label")
            continue
        if not isinstance(entry.get("label"), str) or not entry["label"].strip():
            problems.append(f"{where}: missing label")
        if "tag" in entry and entry["tag"] not in LOCATION_TAGS:
            problems.append(f"{where}: tag {entry['tag']!r} is not an openHAB Location tag")
        if "in" in entry:
            parent = rooms.get(entry["in"])
            if entry["in"] == room or not isinstance(parent, dict):
                problems.append(f"{where}: in {entry['in']!r} is not another room id")
            elif "tag" in entry and "tag" not in parent:
                problems.append(f"{where}: is a Location, so the room it is in needs a tag too")
        for key in sorted(set(entry) - ROOM_KEYS):
            problems.append(f"{where}: unknown key '{key}'")
    for room in rooms:
        trail = [room]
        while isinstance(rooms.get(trail[-1]), dict) and rooms[trail[-1]].get("in") in rooms:
            trail.append(rooms[trail[-1]]["in"])
            if trail[-1] in trail[:-1]:
                if trail[-1] == room and len(trail) > 2:
                    problems.append(f"rooms.{room}: in forms a cycle: {' -> '.join(trail)}")
                break
    return problems


def validate(data: object) -> list[str]:
    """Every structural problem in a parsed devices.yaml.

    Problems that concern one integration's or one feature's fields are
    found by lib/oh_render.py, which knows them all.
    """
    # Imported here: the integration modules import lib/oh_conf.py, which
    # imports this module.
    import oh_integrations

    if not isinstance(data, dict) or data.get("version") != 1:
        return ["devices.yaml: version must be 1"]
    problems = validate_rooms(data.get("rooms") or {})
    rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
    allowed = oh_integrations.allowed()
    seen: dict[str, dict[str, str]] = {"id": {}, "ip": {}, "mac": {}, "c4_item": {}}

    def unique(kind: str, value: str, where: str) -> None:
        if value in seen[kind]:
            problems.append(
                f"{where}: {kind} {value} already used by {seen[kind][value]}")
        else:
            seen[kind][value] = where

    for index, device in enumerate(data.get("devices") or []):
        where = str(device.get("id") or f"devices[{index}]")
        # A device reached through a gateway (IR, serial, relay) has no
        # address of its own; it names the gateway instead.
        required = ("id", "room", "kind") if device.get("via") else (
            "id", "room", "kind", "ip", "mac")
        for key in required:
            if not device.get(key):
                problems.append(f"{where}: missing {key}")
        if device.get("id"):
            if not ID.match(device["id"]):
                problems.append(f"{where}: id is not snake_case")
            unique("id", device["id"], where)
        if device.get("room") and device["room"] not in rooms:
            problems.append(f"{where}: room '{device['room']}' is not in rooms")
        if device.get("kind") and device["kind"] not in KINDS:
            problems.append(f"{where}: unknown kind '{device['kind']}' (lib/oh_semantic.py KINDS)")
        if device.get("ip"):
            try:
                ipaddress.IPv4Address(device["ip"])
            except ValueError:
                problems.append(f"{where}: ip '{device['ip']}' is not IPv4")
            unique("ip", device["ip"], where)
        if device.get("mac"):
            if not MAC.match(device["mac"]):
                problems.append(
                    f"{where}: mac '{device['mac']}' is not lowercase aa:bb:..")
            unique("mac", device["mac"], where)
        c4_items = device.get("c4_item")
        if c4_items is not None:
            c4_items = c4_items if isinstance(c4_items, list) else [c4_items]
            for c4_item in c4_items:
                if isinstance(c4_item, bool) or not isinstance(c4_item, int) or c4_item <= 0:
                    problems.append(f"{where}: c4_item {c4_item!r} is not a Control4 item id")
                else:
                    unique("c4_item", str(c4_item), where)
        controls = device.get("controls")
        if controls is None:
            controls = {}
        if not isinstance(controls, dict):
            problems.append(f"{where}: controls must be a mapping")
            continue
        for phase, names in controls.items():
            if phase not in allowed:
                problems.append(f"{where}: unknown controls phase '{phase}'")
                continue
            for name in names or []:
                if name not in allowed[phase]:
                    problems.append(
                        f"{where}: unknown {phase} integration '{name}'")
    for device in data.get("devices") or []:
        if device.get("via") and device["via"] not in seen["id"]:
            problems.append(f"{device.get('id')}: via '{device['via']}' is not a device id")
    stale = data.get("control4_stale") or []
    if not isinstance(stale, list):
        problems.append("control4_stale must be a list of Control4 item ids")
        stale = []
    for c4_item in stale:
        if isinstance(c4_item, bool) or not isinstance(c4_item, int) or c4_item <= 0:
            problems.append(f"control4_stale: {c4_item!r} is not a Control4 item id")
        else:
            unique("c4_item", str(c4_item), "control4_stale")
    for name, room in (data.get("control4_rooms") or {}).items():
        if room not in rooms:
            problems.append(f"control4_rooms: '{name}' maps to '{room}', which is not in rooms")
    for entry in data.get("later") or []:
        if entry.get("id"):
            unique("id", entry["id"], f"later {entry['id']}")
    return problems


def reservations(data: dict) -> list[tuple[str, str, str]]:
    """(UniFi alias, MAC, IP) for every device that has its own address."""
    rows = [(d["id"].replace("_", "-"), d["mac"], d["ip"])
            for d in data.get("devices") or [] if d.get("ip") and d.get("mac")]
    return sorted(rows, key=lambda row: tuple(int(part) for part in row[2].split(".")))


def conf_objects(conf: Path) -> tuple[dict, dict, list[str]]:
    """Things and Items defined under conf/, keyed by UID and name.

    YAML (conf/yaml/) is the primary format. DSL files are read only far
    enough to name what they define, so that nothing escapes the checks by
    being written in the other syntax.
    """
    things: dict[str, dict] = {}
    items: dict[str, dict] = {}
    problems: list[str] = []
    conf = Path(conf)

    def add(table: dict, key: str, where: str, body: object, kind: str) -> None:
        if key in table:
            problems.append(
                f"{where}: {kind} {key} also defined in {table[key]['file']}")
        table[key] = {"file": where, "body": body}

    for path in sorted((conf / "yaml").glob("*.y*ml")):
        where = str(path.relative_to(conf.parent))
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            problems.append(f"{where}: YAML does not parse: {error}")
            continue
        if document is None:
            continue
        if not isinstance(document, dict):
            problems.append(f"{where}: top level must be a mapping")
            continue
        for uid, body in (document.get("things") or {}).items():
            add(things, str(uid), where, body or {}, "Thing")
        for name, body in (document.get("items") or {}).items():
            add(items, str(name), where, body or {}, "Item")
    for path in sorted((conf / "things").glob("*.things")):
        where = str(path.relative_to(conf.parent))
        for match in DSL_THING.finditer(path.read_text(encoding="utf-8")):
            add(things, match.group(1), where, match.group(2), "Thing")
    for path in sorted((conf / "items").glob("*.items")):
        where = str(path.relative_to(conf.parent))
        for match in DSL_ITEM.finditer(path.read_text(encoding="utf-8")):
            add(items, match.group(1), where, None, "Item")
    return things, items, problems


def addresses(value: object) -> set[str]:
    """Every IPv4 address appearing anywhere in a Thing definition."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found |= addresses(key) | addresses(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            found |= addresses(inner)
    elif isinstance(value, str):
        found |= set(IPV4.findall(value))
    return found


#: Items of the whole house rather than of a room or a device: All off
#: (oh_features.all_off).
HOUSE_ITEMS = frozenset({"house_all_off", "house_camera_status"})


def cross_check(data: dict, things: dict, items: dict) -> list[str]:
    """Problems where conf/ disagrees with the inventory."""
    devices = {d["id"]: d for d in data.get("devices") or [] if d.get("id")}
    services = set(data.get("services") or [])
    rooms = set(data.get("rooms") or {})
    problems: list[str] = []
    with_things: set[str] = set()
    for uid, thing in sorted(things.items()):
        last = uid.split(":")[-1]
        if last in services:
            continue
        device = devices.get(last)
        if device is None:
            problems.append(
                f"{thing['file']}: Thing {uid}: '{last}' is not a device id in "
                "instance/devices.yaml")
            continue
        with_things.add(last)
        home = device.get("ip") or devices.get(device.get("via"), {}).get("ip")
        for address in sorted(addresses(thing["body"]) - {home}):
            problems.append(
                f"{thing['file']}: Thing {uid} uses {address}, but {last} is "
                f"at {home}")
    for device_id, device in sorted(devices.items()):
        observe = (device.get("controls") or {}).get("observe") or []
        if "network_ping" in observe and not any(
                uid.startswith("network:") and uid.endswith(":" + device_id)
                for uid in things):
            problems.append(f"{device_id}: observes network_ping but has no "
                            "network Thing")
    for name, item in sorted(items.items()):
        if name.startswith("loc_") and any(name[4:] == room or name[4:].startswith(room + "_")
                                           for room in rooms):
            continue
        if name in devices or any(name.startswith(d + "_") for d in devices) or name in HOUSE_ITEMS:
            continue
        problems.append(f"{item['file']}: Item {name} is neither loc_<room>[_<point>], "
                        "named after a device id, nor one of HOUSE_ITEMS")
    for device_id in sorted(with_things - set(items)):
        problems.append(f"{device_id}: has Things but no equipment group Item "
                        f"named {device_id}")
    return problems
