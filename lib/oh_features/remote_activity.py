"""The activity feed's public Item descriptions, from managed AV configuration.

Values come from typed events; this manifest contains labels and allowed
options only. Pairing commands and credentials are never subscribed to.
"""

from __future__ import annotations

import re

import oh_integrations as integrations
from oh_conf import Conf, House, parse_options

from . import routing
from .remote_diagnostics import providers

SECRET = re.compile(r"(?:^|_)(?:password|passwd|passphrase|secret|token|psk|authorization|credential|auth)(?:_|$)", re.I)
CONTROLLERS = {"hdmi_matrix", "hdmi_switch", "usb_matrix"}


def manifest(house: House, conf: Conf) -> dict:
    """Safe descriptions of controlled AV devices, routing and its controllers."""
    items = {}
    snapshots = {name for description in providers(house).values() for name in description.get("items", [])}
    active = []
    volumes = {}
    for device in house.devices:
        controls = [module.remote(device) for module in integrations.REGISTRY if hasattr(module, "remote")]
        if device.get("kind") in CONTROLLERS or routing.stager(device) or any(controls):
            active.append(device)
        control = next((entry for entry in controls if entry), {})
        volumes[device["id"]] = control.get("volume")

    def add(name, item, device=None, room=None):
        # Signal acquisitions refresh even when the measured fields do not.
        # They belong to the on-demand diagnostics view, not activity SSE.
        if name in snapshots:
            return
        if SECRET.search(name) or not item or item.get("type") == "DateTime" or item.get("type", "").startswith("Group"):
            return
        kind = "redacted"
        if "_pairing" in name:
            if name.endswith("_pairing_status"):
                kind = "pairing_status"
            elif name.endswith("_pairing_result"):
                kind = "pairing_result"
            else:
                return
        meta = item.get("metadata") or {}
        state = (meta.get("stateDescription") or {}).get("config") or {}
        command = (meta.get("commandDescription") or {}).get("config") or {}
        options = {value: label or value for value, label in parse_options(command.get("options") or state.get("options") or "")
                   if value}
        if kind == "redacted":
            if name.endswith("_problem"):
                kind = "problem"
            elif name.endswith("_key"):
                kind = "key"
            elif item.get("type") == "Switch":
                kind = "switch"
            elif options:
                kind = "enum"
            elif item.get("type") == "Dimmer":
                kind = "volume"
        desc = {"label": item.get("label") or name, "device": house.label(device) if device else None,
                "room": house.room_label(room or device.get("room")) if room or (device and device.get("room")) else None,
                "kind": kind, "states": kind != "key" and name != "house_all_off",
                "commands": "_pairing" not in name and ("Control" in (item.get("tags") or [])
                                                          or item.get("autoupdate") is False)}
        if options:
            desc["options"] = options
        if kind == "volume":
            volume = volumes.get((device or {}).get("id")) or {}
            desc.update(commandMax=volume.get("max", state.get("max", 100)), offset=volume.get("offset", 0),
                        unit=volume.get("unit", ""))
        items[name] = desc

    for device in active:
        for name, item in conf.points(device["id"]):
            add(name, item, device)
    for room in (house.data.get("routing") or {}).get("rooms") or {}:
        name = routing.item_name(room)
        add(name, conf.item(name), room=room)
    add("house_all_off", conf.item("house_all_off"))

    # These actions can be configured before their integration is implemented.
    # Their exact Items/choices remain useful if another automation commands them.
    devices = {device["id"]: device for device in house.devices}
    for room, entry in ((house.data.get("routing") or {}).get("rooms") or {}).items():
        for field in ("on_actions", "off_actions"):
            for ident, value in (entry.get(field) or {}).items():
                name = f"{ident}_action"
                if name not in items:
                    add(name, {"type": "String", "label": "Action", "tags": ["Control"]}, devices[ident], room)
                items[name]["kind"] = "enum"
                items[name].setdefault("options", {})[value] = str(value).replace("_", " ")
    return {"version": 1, "items": items}
