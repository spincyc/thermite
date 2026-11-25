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
import math
from pathlib import Path

from oh_conf import House, GUARD_PROFILE, RULE_SENDS, Addon, fill, uses
import oh_integrations as integrations
from oh_integrations import _commands, _io

SOURCE = Path(__file__).with_suffix(".js")
MODULE = "automation/js/node_modules/thermite-routing-io/index.js"
ITEMS = "yaml/routing_io.yaml"
EVENT_ITEM = "house_routing_io_events"
STATUS_ITEM = "house_routing_io_status"
#: The constant an integration's own rule (rule_path()) takes its actor's
#: configuration in.
CUSTOM = "ROUTING_EXECUTOR"
#: The longest settle a field may declare (lifecycle.js SETTLE_MAX_MS).
SETTLE_MAX_MS = 300000
#: Allowed values for a field's `power` contract term.
POWER_KINDS = frozenset({"absolute", "toggle", "wake_only"})
#: Allowed `feedback.kind` values.
FEEDBACK_KINDS = frozenset({"poll", "push", "none"})


def _providers(device) -> list:
    """Every integration whose fields() the executor drives for a device:
    each the device lists under control."""
    return [module for module in integrations.defining("fields") if uses(device, "control", module.NAME)]


def _settle(module, item: str, settle: dict, values: list) -> dict:
    """A field's declared settle, {value: ms}: how long an accepted write of
    each value may take to show in a read, and a transition toward it may
    last (lifecycle.js, Settle)."""
    if not isinstance(settle, dict):
        raise ValueError(f"{module.NAME} declares an invalid settle for {item}: {settle!r}")
    for value, ms in settle.items():
        if value not in values or type(ms) is not int or not 0 < ms <= SETTLE_MAX_MS:
            raise ValueError(f"{module.NAME} declares an invalid settle for {item}: {value}={ms!r}")
    return dict(settle)


def _feedback(module, item: str, feedback: object) -> dict:
    """Validate a field's `feedback` declaration:
    {"kind": "poll", "interval_s": N} | {"kind": "push"} | {"kind": "none"}."""
    if not isinstance(feedback, dict) or feedback.get("kind") not in FEEDBACK_KINDS:
        raise ValueError(f"{module.NAME} declares an invalid feedback for {item}: {feedback!r}")
    kind = feedback["kind"]
    if kind == "poll":
        interval = feedback.get("interval_s")
        if (set(feedback) != {"kind", "interval_s"} or type(interval) not in (int, float)
                or not math.isfinite(interval) or interval <= 0):
            raise ValueError(f"{module.NAME} declares an invalid feedback for {item}: {feedback!r}")
        return {"kind": "poll", "interval_s": interval}
    if set(feedback) != {"kind"}:
        raise ValueError(f"{module.NAME} declares an invalid feedback for {item}: {feedback!r}")
    return {"kind": kind}


def apply_timing(device: dict, field: dict) -> dict:
    """Return a copy of `field` with per-device `device.timing` overrides merged in.

    `timing.settle` overrides matching values in `field["values"]` (leaving other
    values and other fields untouched); `timing.poll_s` overrides `interval_s`
    when `field["feedback"]["kind"] == "poll"`.
    """
    timing = device.get("timing")
    if not isinstance(timing, dict):
        return dict(field)
    out = dict(field)
    values = out.get("values")
    settle_override = timing.get("settle")
    if isinstance(settle_override, dict) and isinstance(values, (list, tuple)):
        matched = {val: ms for val, ms in settle_override.items() if val in values}
        if matched:
            base_settle = out.get("settle") if isinstance(out.get("settle"), dict) else {}
            out["settle"] = {**base_settle, **matched}
    poll_s = timing.get("poll_s")
    feedback = out.get("feedback")
    if (type(poll_s) in (int, float) and math.isfinite(poll_s) and poll_s > 0
            and isinstance(feedback, dict) and feedback.get("kind") == "poll"):
        out["feedback"] = {"kind": "poll", "interval_s": poll_s}
    return out


def _reports(module, item: str, reports: object) -> dict:
    """Validate a field's `reports` mapping: {raw: {"value": str, "transition": bool}}."""
    if not isinstance(reports, dict):
        raise ValueError(f"{module.NAME} declares an invalid reports for {item}: {reports!r}")
    out = {}
    for raw, entry in reports.items():
        if (not isinstance(raw, str) or not isinstance(entry, dict)
                or set(entry) != {"value", "transition"}
                or not isinstance(entry["value"], str)
                or type(entry["transition"]) is not bool):
            raise ValueError(f"{module.NAME} declares an invalid reports for {item}: {raw!r}={entry!r}")
        out[raw] = {"value": entry["value"], "transition": entry["transition"]}
    return out


def _power(module, item: str, power: object) -> str:
    """Validate a field's `power` kind: absolute | toggle | wake_only."""
    if not isinstance(power, str) or power not in POWER_KINDS:
        raise ValueError(f"{module.NAME} declares an invalid power for {item}: {power!r}")
    return power


def _manual_count(device, providers: list, field_count: int) -> int:
    """How many manual command slots a device's actor needs, from its
    providers' device-level manual_items(device) hooks."""
    total = 0
    for module in providers:
        if module.manual_items:
            count = module.manual_items(device)
            if type(count) is not int or count < 1:
                raise ValueError(f"{module.NAME} declares an invalid manual_items for {device['id']}: {count!r}")
            total += count
    return max(field_count, total)


def capabilities(devices) -> dict:
    """Each executor field of `devices`, by Item, from every integration's
    fields() listed under control. Two integrations declaring the same Item
    is an explicit error."""
    result = {}
    for device in devices:
        for module in _providers(device):
            manual = None
            if module.manual_items:
                manual = module.manual_items(device)
                if type(manual) is not int or manual < 1:
                    raise ValueError(f"{module.NAME} declares an invalid manual_items for {device['id']}: {manual!r}")
            for item, declared in module.fields(device).items():
                if item in result:
                    other = result[item]["provider"]
                    raise ValueError(f"{module.NAME} and {other} both declare field {item}")
                if "manual" in declared:
                    raise ValueError(f"{module.NAME} declares per-field manual for {item}; "
                                     "declare manual_items(device) on the integration instead")
                field = apply_timing(device, declared)
                if "values" not in field or not isinstance(field["values"], (list, tuple)) or not field["values"] \
                        or any(not isinstance(v, str) for v in field["values"]):
                    raise ValueError(f"{module.NAME} declares an invalid values for {item}: {field.get('values')!r}")
                field["values"] = list(field["values"])
                if "feedback" not in field:
                    raise ValueError(f"{module.NAME} declares no feedback for {item}")
                field["feedback"] = _feedback(module, item, field["feedback"])
                if "reports" in field:
                    field["reports"] = _reports(module, item, field["reports"])
                if "power" in field:
                    field["power"] = _power(module, item, field["power"])
                if "settle" in field:
                    field["settle"] = _settle(module, item, field["settle"], field["values"])
                if "independent" in field and type(field["independent"]) is not bool:
                    raise ValueError(f"{module.NAME} declares an invalid independent for {item}: {field['independent']!r}")
                result[item] = {"key": device["id"], "provider": module.NAME, "terminal": True, "probe": True,
                                **({"manual": manual} if manual is not None else {}), "field": field}
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
    caps = capabilities(house.devices)
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
    devices = house.by_id
    for key, row in actors.items():
        row["fields"].update({item: cap["field"] for item, cap in caps.items() if cap["key"] == key})
        # Only declared fields join the actor. A device's other Items (its
        # volume, menu keys or a separate transport protocol) do not move a
        # route's power or input and keep their own command paths.
    # Each device has finite per-property pending manual proposals plus one
    # flight: one per field, or the device-level `manual_items(device)` count
    # across its providers. Stable manual effect pins are separate from
    # temporary holds.
    actor_providers = {
        key: [m for m in _providers(devices[key]) if m.NAME in {caps[item]["provider"] for item in row["fields"]}]
        for key, row in actors.items()
    }
    manual_fields = sum(_manual_count(devices[key], actor_providers[key], len(row["fields"]))
                        for key, row in actors.items())
    descriptor["maxHolds"] = max(descriptor.get("maxHolds", 0), manual_fields + 2 * len(resources) + 17 * len(actors))
    epoch = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    io = {"version": 1, "epoch": epoch, "wake": EVENT_ITEM, "status": STATUS_ITEM, "actors": actors}
    descriptor["io"] = io
    out = conf.yaml_file(ITEMS, ("Private routing actor wakeups; command values never carry operation payloads.",
                                 "The problem and command tally Items (oh_integrations._commands) of a device",
                                 "only its routing actor commands."))
    for name in [EVENT_ITEM, *(a["wake"] for a in actors.values())]:
        out.items[name] = {"type": "String", "label": "Routing executor wakeup", "autoupdate": False}
    out.items[STATUS_ITEM] = {"type": "String", "label": "Routing executor activity", "autoupdate": False,
                              "metadata": {"stateDescription": {"config": {"readOnly": True}}}}
    conf.files[MODULE] = _io.embed(SOURCE.read_text(encoding="utf-8"))
    for key, actor in actors.items():
        device = devices[key]
        providers = actor_providers[key]
        conf.executor_controls.update(actor["fields"])
        kinds = {field["kind"] for field in actor["fields"].values()}
        config = {"key": key, "io": io, "fields": actor["fields"]}
        encoded = json.dumps(config, separators=(",", ":"))
        # `http` fields are the generic HTTP actor's (lifecycle_io.js
        # httpActor); any other kind is driven by the integration's own rule.
        if kinds == {"http"}:
            conf.files[f"automation/js/thermite_routing_io_{key}.js"] = _io.shared(
                "// Generated by bin/thermite generate; private ordered device actor.\n"
                + RULE_SENDS + "\n" + _io.DEVICE_MARK + "\nconst CONFIG = " + encoded + ";\n"
                "require('thermite-routing-io').httpActor(CONFIG, {readonly: !SEND, "
                "record: thermiteDevice.recorder(CONFIG.key)});\n", providers[0].NAME)
        else:
            custom_providers = [p for p in providers if any(
                actor["fields"][item]["kind"] != "http" for item in actor["fields"] if caps[item]["provider"] == p.NAME)]
            path = (custom_providers[0].rule_path(device)
                    if len(custom_providers) == 1 and "http" not in kinds and custom_providers[0].rule_path else key)
            try:
                # The rule has its header already: its integration rendered it.
                conf.files[path] = fill(conf.files.get(path, ""), {CUSTOM: config}, path, header=False)
            except ValueError as error:
                raise ValueError("routing executor adapter is absent: " + path) from error
        # The actor hands each command it concludes to its script's command
        # record (lifecycle_io.js, env.record): the device's problem Item and
        # the script's tally, rendered here for a device whose integration
        # has none, since without an actor its commands go to its binding.
        for provider in providers:
            for name, item in _commands.items(device, provider.NAME).items():
                if conf.item(name) is None:
                    out.items[name] = item
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
