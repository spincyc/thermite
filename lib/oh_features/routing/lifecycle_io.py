"""Executor fields and private per-device routing executor rendering.

Each integration that the routing executor drives declares its fields
through fields(device) (oh_integrations); nothing here names one. Wiring
remains in integration Things. HTTP actors resolve the current native
Thing/channel configuration, so simulator retargeting cannot leave real URLs
in a second generated table.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from oh_conf import House, GUARD_PROFILE, RULE_SENDS, Addon, uses
import oh_integrations as integrations
from oh_integrations import _io

SOURCE = Path(__file__).with_suffix(".js")
MODULE = "automation/js/node_modules/thermite-routing-io/index.js"
ITEMS = "yaml/routing_io.yaml"
EVENT_ITEM = "house_routing_io_events"
STATUS_ITEM = "house_routing_io_status"
#: The line an integration's own rule (rule_path()) carries for its actor's
#: configuration.
CUSTOM_MARK = "const ROUTING_EXECUTOR = null;"
#: The longest settle a field may declare (lifecycle.js SETTLE_MAX_MS).
SETTLE_MAX_MS = 300000


def _provider(device):
    """The integration whose fields() the executor drives for a device: one
    the device lists under control."""
    return next((module for module in integrations.REGISTRY
                 if hasattr(module, "fields") and uses(device, "control", module.NAME)), None)


def _settle(module, item: str, settle: dict, allowed: list) -> dict:
    """A field's declared settle, {value: ms}: how long an accepted write of
    each value may take to show in a read, and a transition toward it may
    last (lifecycle.js, Settle)."""
    for value, ms in settle.items():
        if value not in allowed or type(ms) is not int or not 0 < ms <= SETTLE_MAX_MS:
            raise ValueError(f"{module.NAME} declares an invalid settle for {item}: {value}={ms!r}")
    return dict(settle)


def capabilities(data: dict) -> dict:
    """Each executor field, by Item, from its integration's fields(). The
    field the actor receives is the declaration with its `values` as
    `allowed`; `manual` stays here (configure())."""
    result = {}
    for device in data.get("devices", []):
        module = _provider(device)
        if module is None:
            continue
        for item, declared in module.fields(device).items():
            field = {"allowed" if key == "values" else key: value for key, value in declared.items() if key != "manual"}
            field["allowed"] = list(field["allowed"])
            if "settle" in field:
                field["settle"] = _settle(module, item, field["settle"], field["allowed"])
            if "independent" in field and type(field["independent"]) is not bool:
                raise ValueError(f"{module.NAME} declares an invalid independent for {item}: {field['independent']!r}")
            result[item] = {"key": device["id"], "provider": module.NAME, "terminal": True, "probe": True,
                            "field": field, **({"manual": declared["manual"]} if "manual" in declared else {})}
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
        # Only declared fields join the actor. A device's other Items (its
        # volume, menu keys or a separate transport protocol) do not move a
        # route's power or input and keep their own command paths.
    # Each device has finite per-property pending manual proposals plus one
    # flight: one per field, or the `manual` Items its actor takes commands
    # on. Stable manual effect pins are separate from temporary holds.
    manual_fields = sum(max([len(row["fields"]), *(caps[item].get("manual", 0) for item in row["fields"])])
                        for row in actors.values())
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
        # `http` fields are the generic HTTP actor's (lifecycle_io.js
        # httpActor); any other kind is driven by the integration's own rule.
        if kinds == {"http"}:
            conf.files[f"automation/js/thermite_routing_io_{key}.js"] = (
                "// Generated by bin/thermite generate; private ordered device actor.\n"
                + RULE_SENDS + "\nconst CONFIG = " + encoded + ";\n"
                "require('thermite-routing-io').httpActor(CONFIG, {readonly: !SEND});\n")
        else:
            path = provider.rule_path(device) if "http" not in kinds and hasattr(provider, "rule_path") else key
            text = conf.files.get(path, "")
            if text.splitlines().count(CUSTOM_MARK) != 1:
                raise ValueError("routing executor adapter is absent: " + path)
            conf.files[path] = text.replace(CUSTOM_MARK, "const ROUTING_EXECUTOR = " + encoded + ";")
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
