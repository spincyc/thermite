"""Provider capabilities and private per-device routing executor rendering.

Wiring remains in integration Things. HTTP actors resolve the current native
Thing/channel configuration, so simulator retargeting cannot leave real URLs
in a second generated table.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from oh_conf import House, YamlFile, GUARD_PROFILE, RULE_SENDS, Addon, uses
from oh_integrations import (_io, bk808_http, ukm404_http, vrroom_http, denonmarantz_http,
                             pyatv_http, sony_ip_control, sony_adcp, avpro_telnet)

SOURCE = Path(__file__).with_suffix(".js")
MODULE = "automation/js/node_modules/thermite-routing-io/index.js"
ITEMS = "yaml/routing_io.yaml"
EVENT_ITEM = "house_routing_io_events"
STATUS_ITEM = "house_routing_io_status"
HTTP = (bk808_http, ukm404_http, vrroom_http)
CUSTOM = (denonmarantz_http, pyatv_http, sony_ip_control, sony_adcp, avpro_telnet)
CUSTOM_MARK = "const ROUTING_EXECUTOR = null;"
#: The longest settle a field may declare (lifecycle.js SETTLE_MAX_MS).
SETTLE_MAX_MS = 300000


def _provider(device):
    return next((module for module in (*HTTP, *CUSTOM)
                 if uses(device, "control", module.NAME)), None)


def _settle(module, item: str, settle: dict, allowed: list) -> dict:
    """A field's declared settle, {value: ms}: how long an accepted write of
    each value may take to show in a read, and a transition toward it may
    last (lifecycle.js, Settle)."""
    for value, ms in settle.items():
        if value not in allowed or type(ms) is not int or not 0 < ms <= SETTLE_MAX_MS:
            raise ValueError(f"{module.NAME} declares an invalid settle for {item}: {value}={ms!r}")
    return dict(settle)


def capabilities(data: dict) -> dict:
    """Each executor field, by Item. An integration's SETTLE, {point:
    {value: ms}}, gives the field `<id>_<point>` its `settle`."""
    result = {}
    for device in data.get("devices", []):
        module = _provider(device)
        if module is None:
            continue
        ident = device["id"]
        fields = {}
        if module in HTTP:
            out = YamlFile(())
            module.render(device, House(data), out)
            for item, definition in out.items.items():
                channel = definition.get("channel", "")
                if "Control" not in definition.get("tags", []):
                    continue
                allowed = ([str(n) for n in range(1, 48)] if item.endswith("_edid") else
                           [str(n) for n in module.INPUTS] if module is vrroom_http else
                           [str(n) for n in range(1, module.ports(device)[0] + 1)])
                uid, _, name = channel.rpartition(":")
                fields[item] = {"kind": "http", "thing": uid, "channel": name, "allowed": allowed,
                                "ack": "readback" if module is vrroom_http else "result"}
        elif module is denonmarantz_http:
            for zone in (None, 2):
                stage = module.stage(device, zone)
                if stage:
                    for point, allowed in (("power", ["ON", "OFF"]), ("input", stage["inputs"])):
                        fields[stage[point]] = {"kind": "denon", "thing": module.thing_uid(device),
                                               "zone": zone or 1, "point": point, "allowed": list(allowed)}
        elif module is pyatv_http:
            fields[f"{ident}_power"] = {"kind": "pyatv", "allowed": ["ON", "OFF"]}
        elif module is sony_ip_control:
            fields[f"{ident}_power"] = {"kind": "sony", "allowed": ["ON", "OFF"]}
            fields[f"{ident}_input"] = {"kind": "sony", "allowed": [f"hdmi{n}" for n in module.HDMI]}
        elif module is sony_adcp:
            fields[f"{ident}_power"] = {"kind": "adcp", "allowed": ["ON", "OFF"]}
            fields[f"{ident}_input"] = {"kind": "adcp", "allowed": [value for value, _ in module.INPUTS]}
        elif module is avpro_telnet:
            inputs, outputs = module.ports(device)
            for output in range(1, outputs + 1):
                fields[module.switch_item(device, output)] = {"kind": "avpro", "allowed": [str(n) for n in range(1, inputs + 1)]}
        for point, settle in getattr(module, "SETTLE", {}).items():
            item = f"{ident}_{point}"
            if item in fields:
                fields[item]["settle"] = _settle(module, item, settle, fields[item]["allowed"])
        for item, field in fields.items():
            result[item] = {"key": ident, "provider": module.NAME, "terminal": True,
                            "probe": True, "field": field}
    return result


def _guard(item):
    links = item.pop("channels", None)
    channel = item.pop("channel", None)
    if links is None:
        links = {channel: {}} if channel else {}
    for uid, previous in links.items():
        links[uid] = {"profile": GUARD_PROFILE, "toItemScript": previous.get("toItemScript", "|input")}
    if links:
        item["channels"] = links


def configure(house: House, conf, descriptor: dict) -> dict:
    if not descriptor:
        return descriptor
    caps = capabilities(house.data)
    resources = descriptor.get("resources", {})
    selected = set(resources)
    unknown = selected - set(caps)
    if unknown:
        raise ValueError("routing lifecycle has no ordered executor for " + ", ".join(sorted(unknown)))
    actors = {}
    for item in sorted(selected):
        cap = caps[item]
        key = cap["key"]
        row = actors.setdefault(key, {"wake": f"{key}_routing_io", "resources": [], "fields": {}})
        row["resources"].append(item)
        row["fields"][item] = cap["field"]
    # Every control of an opted-in device uses its actor, including explicit
    # commands to unmanaged ports. Merely listing a field grants no automatic
    # cleanup or route claim: `resources` is the coordinator's narrower scope.
    devices = {device["id"]: device for device in house.data.get("devices", [])}
    for key, row in actors.items():
        row["fields"].update({item: cap["field"] for item, cap in caps.items() if cap["key"] == key})
        # Denon's native volume/mute and finite menu/sound-mode keys do not
        # mutate route power/input. HEOS transport is its separate effect and
        # protocol domain; retain that listener and its shared key Item.
    # Each device has finite per-property pending manual proposals plus one
    # flight. Stable manual effect pins are separate from temporary holds.
    legacy_fields = {pyatv_http: 3, sony_ip_control: 6, sony_adcp: 3, avpro_telnet: avpro_telnet.OUTPUTS}
    manual_fields = sum(max(len(row["fields"]), legacy_fields.get(_provider(devices[key]), 0))
                        for key, row in actors.items())
    descriptor["maxHolds"] = max(descriptor.get("maxHolds", 0), manual_fields + 2 * len(resources) + 17 * len(actors))
    epoch = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    io = {"version": 1, "epoch": epoch, "wake": EVENT_ITEM, "status": STATUS_ITEM, "actors": actors}
    descriptor["io"] = io
    out = conf.yaml_file(ITEMS, ("Private routing actor wakeups; command values never carry operation payloads.",))
    for name in [EVENT_ITEM, *(a["wake"] for a in actors.values())]:
        out.items[name] = {"type": "String", "label": "Routing executor wakeup", "autoupdate": False}
    out.items[STATUS_ITEM] = {"type": "String", "label": "Routing executor activity", "autoupdate": False,
                              "metadata": {"stateDescription": {"config": {"readOnly": True}}}}
    conf.files[MODULE] = _io.embed(SOURCE.read_text(encoding="utf-8"))
    for key, actor in actors.items():
        device = devices[key]
        provider = _provider(device)
        conf.executor_controls.update(actor["fields"])
        kinds = {field["kind"] for field in actor["fields"].values()}
        config = {"key": key, "io": io, "fields": actor["fields"]}
        encoded = json.dumps(config, separators=(",", ":"))
        if provider in CUSTOM:
            path = provider.rule_path(device)
            text = conf.files.get(path, "")
            if text.splitlines().count(CUSTOM_MARK) != 1:
                raise ValueError("routing executor adapter is absent: " + path)
            conf.files[path] = text.replace(CUSTOM_MARK, "const ROUTING_EXECUTOR = " + encoded + ";")
        else:
            if not kinds <= {"http"}:
                raise ValueError("routing executor adapter is absent: " + key)
            conf.files[f"automation/js/thermite_routing_io_{key}.js"] = (
                "// Generated by bin/thermite generate; private ordered device actor.\n"
                + RULE_SENDS + "\nconst CONFIG = " + encoded + ";\n"
                "require('thermite-routing-io').httpActor(CONFIG, {readonly: !SEND});\n")
        for item, field in actor["fields"].items():
            native = conf.item(item)
            if native is None:
                raise ValueError("routing executor Item is absent: " + item)
            _guard(native)
            if field["kind"] == "http":
                thing = next((file.things[field["thing"]] for file in conf.yaml.values() if field["thing"] in file.things), None)
                if thing is None:
                    raise ValueError("routing executor Thing is absent")
                thing["channels"][field["channel"]].setdefault("config", {})["mode"] = "READONLY"
    conf.add_addon(Addon("automation", "jsscripting", "ordered per-device routing lifecycle actors"))
    return descriptor
