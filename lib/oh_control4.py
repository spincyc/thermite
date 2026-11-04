"""Read-only export of the Control4 project, and its comparison with devices.yaml.

The Director knows every device Control4 drives, including those a network
scan cannot see (IR, serial, relay, Zigbee lighting and keypads). `fetch`
asks it for its item list, each device's bindings (the IR, serial, relay and
AV wiring) and network details through pyControl4 with the owner's Control4
account; `summary` turns those into rooms, device classes and devices.yaml
stubs without any network access.

Only GET requests ever reach the Director: the Director session refuses any
other method before it is sent. The account password and every token stay in
memory; only the Director's answers are written, under var/control4/.

This module must import without PyYAML or pyControl4: `fetch` runs in the
pyControl4 venv (no PyYAML) and `summary` runs on the system Python (no
pyControl4).
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

STRUCTURE = {"root", "site", "building", "floor", "room"}
ENTITY = {"device", "proxy"}
# typeName is preferred; these are the numeric types pyControl4 users rely on
# (Home Assistant treats type 7 as a controllable proxy).
TYPE_NAMES = {8: "room", 7: "proxy", 6: "device"}

#: (class, pattern), first match wins. Matched against the item's `proxy`
#: first, then `control`, then `categories`; never against the free-text name.
CLASSES = (
    ("thermostat", r"thermostat"),
    ("lock", r"(?:^|_)lock(?:$|_)|doorlock|door_lock"),
    ("remote", r"voiceinput|^remote$"),
    ("keypad", r"keypad"),
    ("dimmer", r"dimmer"),
    ("light", r"^light|(?:^|_)lights?(?:$|_|v2)|lighting"),
    ("blind", r"blind|shade|motorization"),
    ("relay", r"relay"),
    ("contact", r"contact"),
    ("tv", r"^tv$|^tv_|television"),
    ("receiver", r"receiver|^avr$"),
    ("amplifier", r"amplifier|^amp$"),
    ("projector", r"projector"),
    ("disc_player", r"^dvd|^cd$|blu_?ray|bluray|disc"),
    ("tuner", r"satellite|^cable|tuner|^dvr"),
    ("switcher", r"avswitch|av_switch|switcher|matrix"),
    ("media_player", r"media_player|media_service|digital_audio|streamer"),
    ("camera", r"camera"),
    ("security", r"security|alarm"),
    ("intercom", r"intercom|door_?station"),
    ("controller", r"^controller$|^director$"),
    ("power", r"wattbox|^pdu$|power_?strip"),
)
CLASS_PATTERNS = tuple((name, re.compile(pattern)) for name, pattern in CLASSES)

#: devices.yaml `kind` for a class; a class not listed keeps its own name.
KINDS = {"receiver": "avr", "amplifier": "amp", "tuner": "tuner", "remote": "control4_remote",
         "controller": "control4_controller", "media_player": "streamer"}

#: Binding classes that are a control wire from a controller port into the
#: device (on the device's Input side), and the devices.yaml control each is.
CONTROL_LINKS = {"IR_OUT": "ir", "RS_232": "serial", "RELAY": "relay", "CONTACT_SENSOR": "contact"}
#: Proxies of Control4 software drivers: UI buttons, room-control pages,
#: music services, channel lists and integration helpers. A device whose
#: every proxy is one of these (or whose driver is listed), with no network
#: details and no control wire, has nothing physical behind it.
VIRTUAL_PROXY = re.compile(
    r"^(?:uibutton|media_service|rf_[a-z_]+|control4_digitalaudio|room_control_keypad|"
    r"chowmain_web_to_variables|driverworks_generic_tcp_command|applebridge|control4_remote_hub)$")
VIRTUAL_DRIVERS = {"sonosnetwork.c4z"}

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
MAC_SEPARATED = re.compile(r"(?<![0-9A-Fa-f])((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
MAC_BARE = re.compile(r"^[0-9A-Fa-f]{12}$")
# A Sonos player id embeds the player's MAC: RINCON_<12 hex digits><port>.
RINCON = re.compile(r"RINCON_([0-9A-Fa-f]{12})")
# Control4 and Sonos network ids end in the device's MAC ("controller-020000000021",
# "Zoneplayer-Sonos-020000000022"). The last group of an RFC 4122 UUID looks
# the same, so an id holding one yields no MAC.
ID_MAC = re.compile(r"[-_]([0-9A-Fa-f]{12})$")
RFC4122 = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")
ADDRESS_VARIABLE = re.compile(
    r"(?i)(?:^|_)(?:ip|ip_?address|mac|mac_?address|address|host|host_?name|uuid|udn|rincon)(?:_|$)")
SECRET_VARIABLE = re.compile(r"(?i)pass|secret|token|key|auth|cred|user|login|pin(?:$|_)")


# -- fetch -----------------------------------------------------------------

def director_ip(inventory: dict, explicit: str | None = None) -> str:
    """Resolve an explicit endpoint or a controller selected by private config."""
    import ipaddress
    if explicit is not None:
        return str(ipaddress.ip_address(explicit))
    selected = inventory.get("control4_director")
    devices = [device for device in inventory.get("devices", [])
               if device.get("kind") == "control4_controller" and device.get("ip")
               and (selected is None or device.get("id") == selected)]
    if len(devices) != 1:
        raise ValueError("select a controller with control4_director in devices.yaml or --controller-ip")
    return str(ipaddress.ip_address(devices[0]["ip"]))


def stamp(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(datetime.UTC)
    return now.strftime("%Y%m%dT%H%M%SZ")


def private_dir(path: Path) -> Path:
    """Create path (and missing parents) as 0700 and tighten it if it exists."""
    path = Path(path)
    for parent in reversed([path, *path.parents]):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, data: object) -> Path:
    """Write JSON to a new file created 0600; refuse to overwrite."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return Path(path)


def get_only_trace():
    """An aiohttp TraceConfig that aborts any request that is not a GET."""
    import aiohttp

    async def refuse(session, context, params):
        if params.method.upper() != "GET":
            raise RuntimeError(f"refusing {params.method} {params.url}: the Director export is read-only")

    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(refuse)
    return trace


def controller_entry(controllers: object) -> dict:
    """The one controller pyControl4 returns for the account."""
    if isinstance(controllers, dict) and controllers.get("controllerCommonName"):
        return controllers
    raise RuntimeError("the Control4 account lists no controller (controllerCommonName missing)")


def address_variables(variables: object) -> dict[str, object]:
    """The variables of one device that carry an address, by name.

    Kept: a name that says address, or a value holding an IPv4 address, MAC
    or Sonos player id. Never kept: a credential-looking name.
    """
    kept: dict[str, object] = {}
    for entry in variables if isinstance(variables, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("varName") or entry.get("name") or "")
        if not name or SECRET_VARIABLE.search(name):
            continue
        value = entry.get("value")
        text = str(value)
        if ADDRESS_VARIABLE.search(name) or IPV4.search(text) or MAC_SEPARATED.search(text) or RINCON.search(text):
            kept[name] = value
    return kept


async def _read(call) -> object:
    try:
        return await call
    except Exception as error:
        return {"error": type(error).__name__}


async def fetch(username: str, password: str, ip: str, bindings: bool = True) -> dict:
    """Items, network details and (optionally) bindings from the Director, read-only.

    Calls, all in pyControl4 2.0.2: C4Account.get_account_bearer_token,
    get_account_controllers, get_controller_os_version and
    get_director_bearer_token against apis.control4.com; then
    C4Director.get_all_item_info (GET /api/v1/items) and, per device,
    get_item_bindings, get_item_network and get_item_variables
    (GET /api/v1/items/<id>/bindings, /network, /variables) against the
    Director. Of the variables only address_variables() is kept.
    """
    import aiohttp
    from pyControl4.account import C4Account
    from pyControl4.director import C4Director

    account = C4Account(username, password)
    await account.get_account_bearer_token()
    controller = controller_entry(await account.get_account_controllers())
    try:
        os_version = await account.get_controller_os_version(controller["href"])
    except Exception as error:  # informational only
        os_version = f"unknown ({type(error).__name__})"
    director_token = (await account.get_director_bearer_token(controller["controllerCommonName"]))["token"]
    del account
    # The Director presents a self-signed certificate; pyControl4 itself
    # connects with certificate verification off, and so does this session.
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False),
                                     trace_configs=[get_only_trace()]) as session:
        director = C4Director(ip, director_token, session_no_verify_ssl=session)
        del director_token

        async def refused(*_args, **_kwargs):
            raise RuntimeError("the Director export is read-only")

        director.send_post_request = refused
        items = await director.get_all_item_info()
        found: dict[str, object] = {}
        details: dict[str, dict] = {}
        for item in items:
            if item_type(item) != "device":
                continue
            key, uris = str(item["id"]), item.get("URIs") or {}
            if bindings:
                found[key] = await _read(director.get_item_bindings(item["id"]))
            entry: dict[str, object] = {}
            if "network" in uris:
                entry["network"] = await _read(director.get_item_network(item["id"]))
            if "variables" in uris:
                raw = await _read(director.get_item_variables(item["id"]))
                entry["variables"] = address_variables(raw) if isinstance(raw, list) else raw
            if entry:
                details[key] = entry
    return {"controller": {"name": controller.get("name"),
                           "commonName": controller.get("controllerCommonName"),
                           "osVersion": os_version},
            "items": items, "bindings": found if bindings else None, "details": details}


# -- summary ---------------------------------------------------------------

def newest_export(directory: Path) -> Path | None:
    exports = sorted(Path(directory).glob("items-*Z.json"))
    return exports[-1] if exports else None


def pending_steps(venv_python: Path, exports: Path) -> list[str]:
    """The export steps still to run, in order; summary always runs."""
    steps = [] if Path(venv_python).exists() else ["setup"]
    newest = newest_export(exports)
    # Every fetch writes a details side file, so an export without one was
    # taken before network details were read and can match by name only.
    if newest is None or not details_path(newest).exists():
        steps.append("fetch")
    return steps + ["summary"]


def bindings_path(items_path: Path) -> Path:
    return Path(items_path).with_name(Path(items_path).stem + ".bindings.json")


def details_path(items_path: Path) -> Path:
    return Path(items_path).with_name(Path(items_path).stem + ".details.json")


def item_type(item: dict) -> str:
    name = str(item.get("typeName") or "").lower()
    if name:
        return name
    return TYPE_NAMES.get(item.get("type"), f"type {item.get('type')}")


def snake(text: object) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    if value and not value[0].isalpha():
        value = "x_" + value
    return value or "unnamed"


def normalized(text: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def fields(item: dict, key: str) -> list[str]:
    value = item.get(key)
    if isinstance(value, (list, tuple)):
        return [str(v).lower() for v in value]
    return [str(value).lower()] if value not in (None, "") else []


def classify(items: list[dict]) -> str | None:
    """The class of a device from its own and its proxies' fields, or None."""
    for key in ("proxy", "control", "categories"):
        for item in items:
            for value in fields(item, key):
                for name, pattern in CLASS_PATTERNS:
                    if pattern.search(value):
                        return "dimmer" if name == "light" and dimmable(items) else name
    return None


def dimmable(items: list[dict]) -> bool:
    """A light_v2 proxy serves switches and dimmers alike; only explicit data decides."""
    for item in items:
        capabilities = item.get("capabilities")
        if isinstance(capabilities, dict) and str(capabilities.get("dimmer")).lower() in ("true", "1"):
            return True
        if any("dimmer" in value for value in fields(item, "control")):
            return True
    return False


def addresses(value: object, key: str = "") -> tuple[set[str], set[str]]:
    """(IPv4 addresses, MACs) appearing anywhere in an item."""
    ips: set[str] = set()
    macs: set[str] = set()
    if isinstance(value, dict):
        for inner_key, inner in value.items():
            more = addresses(inner, str(inner_key))
            ips |= more[0]
            macs |= more[1]
    elif isinstance(value, (list, tuple)):
        for inner in value:
            more = addresses(inner, key)
            ips |= more[0]
            macs |= more[1]
    elif isinstance(value, str):
        ips |= {ip for ip in IPV4.findall(value) if all(int(p) < 256 for p in ip.split("."))}
        macs |= {m.lower().replace("-", ":") for m in MAC_SEPARATED.findall(value)}
        bare = [value] if "mac" in key.lower() and MAC_BARE.match(value) else []
        if key.lower() == "uuid" and not RFC4122.search(value):
            bare += ID_MAC.findall(value)
        for low in (b.lower() for b in bare + RINCON.findall(value)):
            macs.add(":".join(low[i:i + 2] for i in range(0, 12, 2)))
    return ips, macs


def control_links(bindings: object, names: dict) -> list[dict]:
    """The control wires into one device: IR, serial, relay and contact.

    Director bindings list each port of the device; a wire from a controller
    port is an Input binding of a CONTROL_LINKS class with a connection whose
    id is the controller and whose bindingName is its port ("IR OUT 2").
    """
    links: list[dict] = []
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, dict) or str(binding.get("inputoutput")).lower() != "input":
            continue
        control = CONTROL_LINKS.get(str(binding.get("bindingClass") or "").upper())
        for connection in (binding.get("connections") or []) if control else []:
            if isinstance(connection, dict):
                target = connection.get("id")
                links.append({"control": control, "port": str(connection.get("bindingName") or "?"),
                              "target_id": target, "target": names.get(target, str(connection.get("name") or target))})
    return links


def describe_link(link: dict) -> str:
    return f"{link['control']} {link['port']} on {link['target']} [{link['target_id']}]"


def is_virtual(group: list[dict], links: list[dict], ips: set[str], details: dict) -> bool:
    """A Control4 software driver: nothing physical to put in devices.yaml."""
    proxies = {p for m in group for p in fields(m, "proxy")}
    software = (bool(proxies) and all(VIRTUAL_PROXY.match(p) for p in proxies)) \
        or driver_of(group).lower() in VIRTUAL_DRIVERS
    networked = any("network" in (m.get("URIs") or {}) or "network" in details.get(str(m.get("id")), {})
                    for m in group)
    return software and not links and not ips and not networked


def model_of(items: list[dict]) -> tuple[str, str]:
    for item in items:
        if item.get("manufacturer") or item.get("model"):
            return str(item.get("manufacturer") or ""), str(item.get("model") or "")
    return "", ""


def driver_of(items: list[dict]) -> str:
    for item in items:
        for key in ("protocolFilename", "filename", "driverFilename"):
            if item.get(key):
                return str(item[key])
    return ""


def match_inventory(device: dict, inventory_devices: list[dict]) -> list[str]:
    """Inventory ids this Control4 device is, by address first, then name/model."""
    by_address = [d["id"] for d in inventory_devices
                  if (d.get("ip") and d["ip"] in device["ips"])
                  or (d.get("mac") and d["mac"].lower() in device["macs"])]
    if by_address:
        return sorted(set(by_address))
    if device["ips"] or device["macs"]:
        # Its address belongs to no inventory device, so it is none of those
        # with an address of their own, whatever their model.
        inventory_devices = [d for d in inventory_devices if not d.get("ip") and not d.get("mac")]
    model = normalized(device["model"])
    name = normalized(device["name"])
    make = normalized(device["make"])
    candidates = [d for d in inventory_devices if model and normalized(d.get("model")) == model
                  and (not make or not d.get("make") or normalized(d.get("make")) == make)]
    if len(candidates) > 1:
        narrowed = [d for d in candidates if name and name in (normalized(d.get("name")), normalized(d["id"]))]
        if not narrowed:
            narrowed = [d for d in candidates if d.get("room") == device["room_id"]]
        candidates = narrowed or candidates
    if not candidates and name:
        candidates = [d for d in inventory_devices if name in (normalized(d.get("name")), normalized(d["id"]))]
    return sorted(d["id"] for d in candidates)


def devices(items: list[dict], bindings: dict | None = None, inventory: dict | None = None,
            details: dict | None = None) -> dict:
    """Rooms, classified devices and inventory matches from a Director item list."""
    inventory = inventory or {}
    details = details or {}
    inventory_devices = [d for d in inventory.get("devices") or [] if d.get("id")]
    # devices.yaml c4_item settles a match; such a device matches nothing else.
    # A list links one device to several Control4 items (a TV, its IR driver,
    # its UI button): that is no ambiguity. Two devices claiming one item is.
    linked: dict[object, list[str]] = {}
    for d in inventory_devices:
        if d.get("c4_item") is not None:
            for c4 in d["c4_item"] if isinstance(d["c4_item"], list) else [d["c4_item"]]:
                linked.setdefault(c4, []).append(d["id"])
    unlinked = [d for d in inventory_devices if d.get("c4_item") is None]
    stale = set(inventory.get("control4_stale") or [])
    by_id = {item.get("id"): item for item in items if isinstance(item, dict)}
    names = {key: str(item.get("name") or key) for key, item in by_id.items()}

    def ancestors(item: dict):
        seen = set()
        parent = by_id.get(item.get("parentId"))
        while parent is not None and parent.get("id") not in seen:
            seen.add(parent.get("id"))
            yield parent
            parent = by_id.get(parent.get("parentId"))

    def room_of(item: dict) -> dict | None:
        if item.get("roomId") in by_id:
            return by_id[item["roomId"]]
        for parent in ancestors(item):
            if item_type(parent) == "room":
                return parent
        if item.get("roomName"):
            return {"id": None, "name": item["roomName"]}
        return None

    rooms: dict[str, dict] = {}
    room_ids = {normalized(entry.get("label") or key): key
                for key, entry in (inventory.get("rooms") or {}).items()}
    room_ids.update({normalized(key): key for key in inventory.get("rooms") or {}})
    room_ids.update({normalized(name): key for name, key in (inventory.get("control4_rooms") or {}).items()})

    def room_entry(room: dict | None) -> dict:
        if room is None:
            key, label = "unassigned", "(no room)"
            floor = ""
        else:
            label = str(room.get("name") or room.get("id"))
            floor = next((str(p.get("name")) for p in ancestors(room) if item_type(p) == "floor"), "")
            key = room_ids.get(normalized(label), snake(label))
        entry = rooms.setdefault(key, {"id": key, "name": label, "floor": floor, "devices": []})
        return entry

    for item in items:
        if isinstance(item, dict) and item_type(item) == "room":
            room_entry(item)

    entities = [i for i in items if isinstance(i, dict) and item_type(i) in ENTITY]
    entity_ids = {i.get("id") for i in entities}
    children: dict[object, list[dict]] = {}
    tops: list[dict] = []
    for item in entities:
        top = item
        for parent in ancestors(item):
            if parent.get("id") in entity_ids:
                top = parent
        if top is item:
            tops.append(item)
        else:
            children.setdefault(top.get("id"), []).append(item)

    other_types: dict[str, int] = {}
    for item in items:
        if isinstance(item, dict):
            kind = item_type(item)
            if kind not in STRUCTURE and kind not in ENTITY:
                other_types[kind] = other_types.get(kind, 0) + 1

    found: list[dict] = []
    for top in sorted(tops, key=lambda i: (str(i.get("name")), str(i.get("id")))):
        group = [top, *children.get(top.get("id"), [])]
        room = room_entry(room_of(top))
        ips: set[str] = set()
        macs: set[str] = set()
        for member in group:
            for source in (member, details.get(str(member.get("id")))):
                more = addresses(source)
                ips |= more[0]
                macs |= more[1]
        make, model = model_of(group)
        links: list[dict] = []
        for member in group:
            # A device and its proxies can each report the same port.
            for link in control_links((bindings or {}).get(str(member.get("id"))), names):
                if link not in links:
                    links.append(link)
        kind = classify(group)
        if kind is None:
            kind = next((c for c in ("relay", "contact") if any(link["control"] == c for link in links)), None)
        device = {
            "c4_id": top.get("id"), "name": str(top.get("name") or top.get("id")),
            "class": kind, "proxies": sorted({p for m in group for p in fields(m, "proxy")}),
            "make": make, "model": model, "driver": driver_of(group),
            "room_id": room["id"], "room": room["name"],
            "ips": ips, "macs": macs, "links": links,
            "virtual": is_virtual(group, links, ips, details),
            "stale": top.get("id") in stale,
        }
        if top.get("id") in linked:
            device["matches"] = sorted(set(linked[top.get("id")]))
        else:
            device["matches"] = [] if device["virtual"] or device["stale"] else match_inventory(device, unlinked)
        room["devices"].append(device)
        found.append(device)
    matches_of = {d["c4_id"]: d["matches"] for d in found}
    for device in found:
        gateways = {i for link in device["links"] for i in matches_of.get(link["target_id"], [])}
        device["via"] = gateways.pop() if len(gateways) == 1 else None
    matched_by: dict[str, list] = {}
    for device in found:
        for ident in device["matches"]:
            matched_by.setdefault(ident, []).append(device["c4_id"])
    # Several Control4 devices on one inventory device are shared only when a
    # guess put them there; the items its own c4_item names are its own.
    claimed = {ident for idents in linked.values() for ident in idents}
    return {"rooms": rooms, "devices": found, "other_types": other_types, "has_details": bool(details),
            "shared_matches": {i: ids for i, ids in sorted(matched_by.items()) if len(ids) > 1 and i not in claimed},
            "inventory_unlisted": sorted(d["id"] for d in inventory_devices if d["id"] not in matched_by)}


def stub(device: dict, taken: set[str], rooms: dict) -> str:
    """A suggested devices.yaml entry for a Control4 device the inventory lacks."""
    base = snake(device["name"])
    if not base.startswith(device["room_id"]) and device["room_id"] != "unassigned":
        base = f"{device['room_id']}_{base}"
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}_{n}", n + 1
    taken.add(ident)
    kind = KINDS.get(device["class"], device["class"] or "unknown")
    lines = [f"  - id: {ident}", f"    room: {device['room_id']}"
             + ("" if device["room_id"] in rooms else "  # TODO: new room; add it to rooms"),
             f"    kind: {kind}"]
    if device["make"]:
        lines.append(f"    make: {json.dumps(device['make'])}")
    if device["model"]:
        lines.append(f"    model: {json.dumps(device['model'])}")
    lines.append(f"    name: {json.dumps(device['name'])}")
    ips, macs = sorted(device["ips"]), sorted(device["macs"])
    wires = "; ".join(describe_link(link) for link in device["links"])
    if ips:
        lines.append(f"    ip: {ips[0]}" + (f"  # also {', '.join(ips[1:])}" if ips[1:] else ""))
        lines.append(f"    mac: {macs[0]}" if macs else "    mac: TODO")
        lines += ["    controls:", "      observe: [network_ping]" + (f"  # Control4 also wires: {wires}" if wires else "")]
    elif device["links"]:
        controls = ", ".join(sorted({link["control"] for link in device["links"]}))
        lines.append(f"    via: {device['via'] or 'TODO_gateway_id'}  # Control4: {wires}")
        lines += ["    controls:", f"      control: [{controls}]"]
    else:
        lines.append("    controls: {}  # TODO: no address or control wire in the export")
    if device["driver"]:
        lines.append(f"    control4: {device['driver']}")
    return "\n".join(lines)


def markdown(source: Path, result: dict, inventory: dict) -> str:
    rooms = inventory.get("rooms") or {}
    taken = {d.get("id") for d in inventory.get("devices") or []} | {
        e.get("id") for e in inventory.get("later") or []}
    physical = [d for d in result["devices"] if not d["virtual"]]
    software = [d for d in result["devices"] if d["virtual"]]
    out = [f"# Control4 project summary", "", f"Source: `{source.name}`. "
           f"{len(physical)} devices and {len(software)} software drivers in {len(result['rooms'])} rooms."]
    if not result.get("has_details"):
        out += ["", "This export has no network details, so devices match by name and model only. "
                "`thermite control4 fetch` takes an export that has them."]
    out += ["", "## Rooms", ""]
    for room in sorted(result["rooms"].values(), key=lambda r: (r["floor"], r["name"])):
        where = f"{room['floor']} / {room['name']}" if room["floor"] else room["name"]
        out.append(f"### {where} (`{room['id']}`)")
        out.append("")
        present = [d for d in room["devices"] if not d["virtual"]]
        if not present:
            out += ["(no devices)" if not room["devices"] else "(software drivers only)", ""]
            continue
        for d in present:
            facts = [d["class"] or "other"]
            if d["proxies"]:
                facts.append("proxy " + ", ".join(d["proxies"]))
            if d["make"] or d["model"]:
                facts.append(" ".join(x for x in (d["make"], d["model"]) if x))
            if d["driver"]:
                facts.append(d["driver"])
            if d["ips"] or d["macs"]:
                facts.append(", ".join(sorted(d["ips"]) + sorted(d["macs"])))
            facts += [describe_link(link) for link in d["links"]]
            if d["stale"]:
                facts.append("inventory: stale (control4_stale)")
            else:
                facts.append("inventory: " + (", ".join(d["matches"]) if d["matches"] else "none")
                             + (f" (via {d['via']})" if d["via"] else ""))
            out.append(f"- {d['name']} [{d['c4_id']}]: " + "; ".join(facts))
        out.append("")
    out += ["## Control4 software drivers", "", "UI buttons, room-control pages, music services, channel lists "
            "and integration helpers: nothing physical behind them, so nothing to add to devices.yaml.", ""]
    out += [f"- {d['name']} [{d['c4_id']}] in {d['room']}: proxy {', '.join(d['proxies']) or 'none'}"
            f"{'; ' + d['driver'] if d['driver'] else ''}" for d in software] or ["(none)"]
    out.append("")
    others = [d for d in physical if d["class"] is None]
    out += ["## Other", "", "Devices whose proxy, control and categories match no class in "
            "lib/oh_control4.py; classify them by hand.", ""]
    out += [f"- {d['name']} [{d['c4_id']}] in {d['room']}: proxy {', '.join(d['proxies']) or 'none'}"
            f"{'; ' + d['driver'] if d['driver'] else ''}" for d in others] or ["(none)"]
    if result["other_types"]:
        out += ["", "Non-device items: " + ", ".join(
            f"{k} {v}" for k, v in sorted(result["other_types"].items())) + "."]
    # A software driver matches only through c4_item, and may be linked to
    # one device (its UI button) or, wrongly, to several.
    ambiguous = [d for d in result["devices"] if len(d["matches"]) > 1]
    shared = result.get("shared_matches") or {}
    if ambiguous or shared:
        out += ["", "## Ambiguous matches", ""]
        out += [f"- {d['name']} [{d['c4_id']}] in {d['room']}: {', '.join(d['matches'])}" for d in ambiguous]
        names = {d["c4_id"]: f"{d['name']} [{d['c4_id']}] in {d['room']}" for d in result["devices"]}
        out += [f"- {ident} is matched by {len(ids)} Control4 devices: {'; '.join(names[i] for i in ids)}"
                for ident, ids in shared.items()]
    missing = [d for d in physical if not d["matches"] and not d["stale"]]
    out += ["", "## Not in devices.yaml", "",
            "Suggested stubs; check every field, then add them under `devices:`.", ""]
    if missing:
        out += ["```yaml", *[stub(d, taken, rooms) for d in missing], "```"]
    else:
        out.append("(none)")
    out += ["", "## In devices.yaml but not in the Control4 project", ""]
    out += [f"- {i}" for i in result["inventory_unlisted"]] or ["(none)"]
    return "\n".join(out) + "\n"


def load_export(path: Path) -> tuple[list[dict], dict | None, dict | None]:
    """(items, bindings, details) of one export; a missing side file is None."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(items, dict) and "items" in items:
        items = items["items"]
    sides = []
    for side in (bindings_path(path), details_path(path)):
        sides.append(json.loads(side.read_text(encoding="utf-8")) if side.exists() else None)
    return items, sides[0], sides[1]
