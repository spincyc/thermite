"""The device inventory (instance/devices.yaml) and its agreement with conf/.

The inventory is the single list of devices openHAB may talk to. conf/ names
its Things and Items after inventory ids so that this module can prove every
Thing belongs to a known device at that device's address.
"""

from __future__ import annotations

import difflib
import ipaddress
import re
from pathlib import Path

import yaml

from oh_semantic import KINDS, LOCATION_TAGS

ID = re.compile(r"^[a-z][a-z0-9_]*$")
MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")

ROOM_KEYS = {"label", "tag", "in"}
#: Every key devices.yaml may have at its top level; the features and
#: integrations that read each one validate what is under it.
TOP_KEYS = frozenset({"version", "rooms", "devices", "routing", "later", "services", "access", "theme",
                      "protect", "diagnostics", "control4_rooms", "control4_stale", "control4_director"})
#: Every key a device may have: its identity and address, the fields
#: integrations read (each validates its own: volume_max, sonos_udn, ...),
#: and owner documentation no program reads: notes, and the Control4 driver
#: name `thermite control4 summary` suggests.
DEVICE_KEYS = frozenset({"id", "room", "kind", "name", "make", "model", "ip", "mac", "via", "controls", "c4_item",
                         "volume_max", "sonos_udn", "heos_pid", "zones", "settings", "protect", "cec", "ir",
                         "sony_speaker_audio", "notes", "control4"})
#: Device keys whose value is text.
TEXT_KEYS = ("id", "room", "kind", "name", "make", "model", "ip", "mac", "via", "notes", "control4")
#: Integrations that derive a device's identity from its MAC, so need one.
NEEDS_MAC = frozenset({"sonos"})

DSL_THING = re.compile(
    r"^\s*(?:Thing|Bridge)\s+([\w-]+:[\w-]+(?::[\w-]+)+)(.*)$", re.MULTILINE)
DSL_ITEM = re.compile(
    r"^\s*(?:Switch|String|Number(?::\w+)?|Dimmer|Contact|Rollershutter|Color|"
    r"DateTime|Location|Player|Image|Call|Group(?::[\w:()]+)?)\s+(\w+)",
    re.MULTILINE)


class ConfigError(ValueError):
    """A configuration file thermite cannot use: missing, unreadable, not
    YAML, or with problems. str() is `FILE[:LINE:COLUMN]: message`; the
    dispatcher (oh_cli) prints each of `lines()` and exits 2."""

    def __init__(self, path: Path | str | None, message: str, line: int | None = None,
                 column: int | None = None, problems: list[str] = ()) -> None:
        self.path, self.message, self.line, self.column = path, message, line, column
        self.problems = list(problems)
        where = ":".join(str(part) for part in (path, line, column) if part is not None)
        super().__init__(f"{where}: {message}" if where else message)

    def lines(self) -> list[str]:
        """What to print: each problem, then the summary."""
        return [f"{self.path}: {problem}" if self.path else problem for problem in self.problems] + [str(self)]


def load(path: Path) -> object:
    """A YAML file's content; ConfigError when it cannot be read or parsed."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(path, f"cannot be read: {getattr(error, 'strerror', None) or error}") from None
    try:
        return yaml.safe_load(text)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        raise ConfigError(path, f"is not YAML: {error.problem or error.context}",
                          mark.line + 1 if mark else None, mark.column + 1 if mark else None) from None
    except yaml.YAMLError as error:
        raise ConfigError(path, f"is not YAML: {error}") from None


def checked(path: Path, problems=None) -> dict:
    """A house file loaded and validated (by `problems`, default
    oh_render.validate); ConfigError naming every problem otherwise."""
    if problems is None:
        import oh_render
        problems = oh_render.validate
    data = load(path)
    found = problems(data)
    if found:
        raise ConfigError(path, f"{len(found)} problem{'s' if len(found) != 1 else ''}; nothing done", problems=found)
    return data


def hint(word: object, choices) -> str:
    """'; did you mean X?' when one of `choices` is close to `word`."""
    close = difflib.get_close_matches(str(word), [str(choice) for choice in choices], n=1)
    return f"; did you mean {close[0]}?" if close else ""


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
        if "tag" in entry and (not isinstance(entry["tag"], str) or entry["tag"] not in LOCATION_TAGS):
            problems.append(f"{where}: tag {entry['tag']!r} is not an openHAB Location tag")
        if "in" in entry:
            parent = rooms.get(entry["in"]) if isinstance(entry["in"], str) else None
            if entry["in"] == room or not isinstance(parent, dict):
                problems.append(f"{where}: in {entry['in']!r} is not another room id")
            elif "tag" in entry and "tag" not in parent:
                problems.append(f"{where}: is a Location, so the room it is in needs a tag too")
        for key in sorted(set(entry) - ROOM_KEYS):
            problems.append(f"{where}: unknown key '{key}'")
    for room in rooms:
        trail = [room]
        while isinstance(rooms.get(trail[-1]), dict) and isinstance(rooms[trail[-1]].get("in"), str) \
                and rooms[trail[-1]]["in"] in rooms:
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
    import oh_access_mode

    if not isinstance(data, dict) or data.get("version") != 1:
        return ["devices.yaml: version must be 1"]
    problems = [f"unknown top-level key '{key}'{hint(key, TOP_KEYS)}" for key in data if key not in TOP_KEYS]
    for key, kind, what in (("rooms", dict, "a mapping of room ids"), ("devices", list, "a list of devices"),
                            ("routing", dict, "a mapping"), ("later", list, "a list of devices"),
                            ("services", list, "a list of service ids"), ("control4_rooms", dict, "a mapping")):
        if data.get(key) is not None and not isinstance(data[key], kind):
            problems.append(f"{key} must be {what}")
    if problems:
        return problems
    problems = validate_rooms(data.get("rooms") or {}) + oh_access_mode.validate(data)
    rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
    allowed = oh_integrations.allowed()
    seen: dict[str, dict[str, str]] = {"id": {}, "ip": {}, "mac": {}, "c4_item": {}}

    def unique(kind: str, value: str, where: str) -> None:
        if value in seen[kind]:
            problems.append(
                f"{where}: {kind} {value} already used by {seen[kind][value]}")
        else:
            seen[kind][value] = where

    devices = data.get("devices") or []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            problems.append(f"devices[{index}]: must be a mapping with at least id, room and kind")
            continue
        where = str(device.get("id") or f"devices[{index}]")
        for key in device:
            if key not in DEVICE_KEYS:
                problems.append(f"{where}: unknown key '{key}'{hint(key, DEVICE_KEYS)}")
        wrong = [key for key in TEXT_KEYS if device.get(key) is not None and not isinstance(device[key], str)]
        for key in wrong:
            problems.append(f"{where}: {key} {device[key]!r} must be text")
        if wrong:
            continue
        # A device reached through a gateway (IR, serial, relay) has no
        # address of its own; it names the gateway instead. A MAC is only
        # for dhcp-reservations and discover, which name devices lacking one.
        for key in ("id", "room", "kind") if device.get("via") else ("id", "room", "kind", "ip"):
            if not device.get(key):
                problems.append(f"{where}: missing {key}")
        if device.get("id"):
            if not ID.match(device["id"]):
                problems.append(f"{where}: id is not snake_case")
            unique("id", device["id"], where)
        if device.get("room") and device["room"] not in rooms:
            problems.append(f"{where}: room '{device['room']}' is not in rooms{hint(device['room'], rooms)}")
        if device.get("kind") and device["kind"] not in KINDS:
            problems.append(f"{where}: unknown kind '{device['kind']}'{hint(device['kind'], KINDS)} "
                            "(lib/oh_semantic.py KINDS)")
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
            problems.append(f"{where}: controls must be a mapping of phase to integration names")
            continue
        for phase, names in controls.items():
            if phase not in allowed:
                problems.append(f"{where}: unknown controls phase '{phase}'{hint(phase, allowed)}")
                continue
            if names is not None and not isinstance(names, list):
                problems.append(f"{where}: controls.{phase} must be a list of integration names")
                continue
            for name in names or []:
                if not isinstance(name, str) or name not in allowed[phase]:
                    other = [p for p in allowed if p != phase and isinstance(name, str) and name in allowed[p]]
                    problems.append(f"{where}: unknown {phase} integration {name!r}"
                                    + (f"; it is a {other[0]} integration" if other else hint(name, allowed[phase])))
                elif name in NEEDS_MAC and not device.get("mac"):
                    problems.append(f"{where}: {name} needs the device's mac")
    for device in devices:
        if isinstance(device, dict) and device.get("via") and device["via"] not in seen["id"]:
            problems.append(f"{device.get('id')}: via '{device['via']}' is not a device id"
                            f"{hint(device['via'], seen['id'])}")
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
        if not isinstance(room, str) or room not in rooms:
            problems.append(f"control4_rooms: '{name}' maps to '{room}', which is not in rooms")
    for index, entry in enumerate(data.get("later") or []):
        if not isinstance(entry, dict):
            problems.append(f"later[{index}]: must be a mapping with at least an id")
        elif entry.get("id"):
            unique("id", str(entry["id"]), f"later {entry['id']}")
    for index, service in enumerate(data.get("services") or []):
        if not isinstance(service, str):
            problems.append(f"services[{index}]: {service!r} must be a service id")
    return problems


def reservations(data: dict) -> list[tuple[str, str, str]]:
    """(UniFi alias, MAC, IP) for every device that has its own address and a MAC."""
    rows = [(d["id"].replace("_", "-"), d["mac"], d["ip"])
            for d in data.get("devices") or [] if d.get("ip") and d.get("mac")]
    return sorted(rows, key=lambda row: tuple(int(part) for part in row[2].split(".")))


def without_mac(data: dict) -> list[str]:
    """Ids of devices with their own address but no mac: no reservation, and
    discover cannot match them."""
    return sorted(d["id"] for d in data.get("devices") or [] if d.get("ip") and not d.get("mac"))


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
HOUSE_ITEMS = frozenset({"house_all_off", "house_camera_status", "house_routing_io_events", "house_routing_io_status"})


def cross_check(data: dict, things: dict, items: dict) -> list[str]:
    """Problems where conf/ disagrees with the inventory."""
    from oh_integrations import REGISTRY  # Runtime-only: providers import inventory through oh_conf.
    from oh_features.threadpools import ITEM_NAMES as HANDLER_ITEMS

    devices = {d["id"]: d for d in data.get("devices") or [] if d.get("id")}
    house_items = HOUSE_ITEMS | frozenset(HANDLER_ITEMS.values())
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
        allowed = {home}
        owners = 0
        for module in REGISTRY:
            if not hasattr(module, "transport_addresses"):
                continue
            try:
                declared = module.transport_addresses(device)
                if uid not in declared:
                    continue
                values = declared[uid]
                if (not isinstance(values, (list, tuple, set, frozenset)) or not values
                        or any(not isinstance(value, str) or str(ipaddress.IPv4Address(value)) != value for value in values)):
                    raise ValueError("invalid transport addresses")
                owners += 1
                allowed = set(values)
            except (ValueError, OSError):
                problems.append(f"{thing['file']}: Thing {uid}: invalid {module.NAME} transport-address declaration")
        if owners > 1:
            problems.append(f"{thing['file']}: Thing {uid}: conflicting transport-address declarations")
            allowed = {home}
        for address in sorted(addresses(thing["body"]) - allowed):
            problems.append(
                f"{thing['file']}: Thing {uid} uses {address}, but "
                + (f"its approved transport addresses are {sorted(allowed)}" if owners == 1 else f"{last} is at {home}"))
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
        if name in devices or any(name.startswith(d + "_") for d in devices) or name in house_items:
            continue
        problems.append(f"{item['file']}: Item {name} is neither loc_<room>[_<point>], "
                        "named after a device id, nor one of HOUSE_ITEMS")
    for device_id in sorted(with_things - set(items)):
        problems.append(f"{device_id}: has Things but no equipment group Item "
                        f"named {device_id}")
    return problems
