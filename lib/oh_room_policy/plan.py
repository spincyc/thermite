"""Compile existing routing contracts into native verification and repairs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from oh_features import routing

from .model import PolicyError
from .schedule import utc


@dataclass(frozen=True)
class Field:
    item: str
    value: str
    resource: str
    allowed: tuple[str, ...]
    native_power: bool = False
    max_age: float = 30.0


@dataclass(frozen=True)
class Observation:
    value: object
    observed_at: datetime | None
    basis: str = "native"


@dataclass(frozen=True)
class Plan:
    room: str
    target: str
    source_item: str
    fields: tuple[Field, ...]
    resources: tuple[str, ...]
    boundary_effects: tuple[tuple[str, str], ...]
    limitations: tuple[str, ...] = ()


def expected_plan(house, room: str, choice: str) -> Plan:
    """Use routing's command generators; never duplicate physical wiring.

    Only the chosen physical path is verified. Parking an unused matrix
    for a local source does not add that matrix's health as a dependency.
    Boundary effects include one-shot actions/EDID, but fields never do.
    """
    data = house.data if hasattr(house, "data") else house
    if not isinstance(data, dict) or "routing" not in data or "devices" not in data:
        raise PolicyError("physical_contract_required")
    config = data["routing"]
    if room not in config["rooms"] or choice not in ["off", *config["rooms"][room]["sources"]]:
        raise PolicyError("invalid_room_source")
    entry = config["rooms"][room]
    devices = {device["id"]: device for device in data["devices"]}
    for source in entry.get("sleep_sources", []):
        if any(other != room and source in spec["sources"] for other, spec in config["rooms"].items()):
            raise PolicyError("unsafe_shared_source_sleep")
    table = routing.table(data)
    raw = table["rooms"][room]
    stage_list = list(routing.stages(data, room))
    inputs = routing.inputs(data, room, choice)
    input_items = {name for name, _ in inputs}
    relevant_power = {stage["power"] for _, stage in stage_list if choice == "off" or stage["input"] in input_items}
    native_items = set()
    for ident, device in devices.items():
        module = routing.waker(device)
        if module is not None and module.NAME == "pyatv_http":
            native_items.add(module.wake_item(device))
    allowed = {}
    for _, stage in stage_list:
        allowed[stage["power"]] = ("ON", "OFF")
        allowed[stage["input"]] = tuple(map(str, stage["inputs"]))
    for source in [choice, *entry.get("sleep_sources", [])]:
        for item, _ in routing.wake(data, source):
            allowed[item] = ("ON", "OFF")
    for target in ["off", *entry["sources"]]:
        for item, value in routing.commands(data, room, target):
            allowed.setdefault(item, set()).add(str(value))

    def owner(item):
        return next((ident for ident in sorted(devices, key=len, reverse=True) if item.startswith(ident + "_")), None)

    local_stage = any(choice in (entry.get(key, {}).get("sources") or {}) for key in ("receiver", "display"))
    local_switch = choice in (entry.get("hdmi_switch", {}).get("inputs") or {})
    source = config["sources"].get(choice, {})
    wake_items = {name for name, _ in routing.wake(data, choice)}
    steps = (routing.power(data, room, choice) + routing.commands(data, room, choice) + inputs
             + [step for step in routing.after(data, room, choice) if step[0] in relevant_power or step[0] in allowed])
    fields = []
    for item, value in steps:
        ident = owner(item)
        if ident is None:
            raise PolicyError("unknown_controller_field")
        device = devices[ident]
        if choice != "off":
            if item.endswith("_power") and item not in relevant_power | wake_items:
                continue
            if ident == config.get("hdmi_matrix") and (local_stage or local_switch):
                continue
            if ident == entry.get("hdmi_switch", {}).get("device") and local_stage:
                continue
            if device.get("kind") == "usb_matrix":
                host = source.get("usb_host")
                if host is None or isinstance(host, dict) and ident not in host:
                    continue
        accepted = set(map(str, allowed[item]))
        module = routing.switcher(device)
        if module is not None:
            accepted.update(str(n) for n in range(1, module.ports(device)[0] + 1))
        fields.append(Field(item, str(value), "device:" + ident, tuple(sorted(accepted)), item in native_items))
    # Keep command order, but a field occurring twice has one final target.
    by_item = {field.item: field for field in fields}
    fields = list(by_item.values())
    effects = raw["power"][choice] + raw["choices"][choice] + raw["inputs"][choice] + raw["after"][choice]
    edid = table["edid"]
    if choice != "off" and not local_stage and not local_switch and edid and source.get("hdmi_input") is not None:
        name = edid["inputs"].get(str(source["hdmi_input"]))
        if name and raw["hdmi"] in edid["outputs"]:
            effects = effects + [[name, edid["outputs"][raw["hdmi"]]]]
    resources = {field.resource for field in fields}
    for item, _ in effects:
        if ident := owner(item):
            resources.add("device:" + ident)
    limits = ["passive_endpoint:" + entry[key]["device"] for key in ("receiver", "display")
              if entry.get(key, {}).get("passive") is True]
    return Plan(room, choice, raw["item"], tuple(fields), tuple(sorted(resources)),
                tuple((item, str(value)) for item, value in effects), tuple(limits))


def _value(value: object, allowed: tuple[str, ...]) -> str | None:
    if type(value) not in (str, int, float) or str(value) in ("NULL", "UNDEF", ""):
        return None
    text = str(value)
    if len(text) > 128:
        return None
    if text in allowed:
        return text
    try:
        numeric = Decimal(text)
        if numeric.is_finite():
            return next((candidate for candidate in allowed if Decimal(candidate) == numeric), None)
    except InvalidOperation:
        pass
    return None


def verify(plan: Plan, observations: dict[str, Observation], now_utc: datetime) -> dict:
    """No ACK/receipt, stale/unknown reading, or passive endpoint is success."""
    now = utc(now_utc)
    matched, mismatched, unknown = [], [], []
    for field in plan.fields:
        reading = observations.get(field.item)
        valid = isinstance(reading, Observation) and reading.observed_at is not None
        if valid:
            try:
                at = utc(reading.observed_at)
                valid = 0 <= (now - at).total_seconds() <= field.max_age
            except PolicyError:
                valid = False
            valid = valid and reading.basis in (("native_power",) if field.native_power else ("native", "native_power"))
        value = _value(reading.value, field.allowed) if valid else None
        if value is None:
            unknown.append(field.item)
        elif value != field.value:
            mismatched.append(field.item)
        else:
            matched.append(field.item)
    state = "degraded" if mismatched or plan.limitations else "unknown" if unknown or not plan.fields else "verified"
    return {"state": state, "matched": matched, "mismatched": mismatched, "unknown": unknown,
            "limitations": list(plan.limitations), "scope": "configured_native_controls"}


def repair_plan(plan: Plan, verification: dict) -> list[tuple[str, str]]:
    """Known, fresh mismatches only; no Source, EDID or one-shot action."""
    mismatch = set(verification["mismatched"])
    return [(field.item, field.value) for field in plan.fields if field.item in mismatch]


def conflicts(plan: Plan, protected: list[Plan], *, boundary: bool = False) -> list[str]:
    """Reject incompatible shared fields, including boundary-only EDID effects."""
    wanted = dict(plan.boundary_effects) if boundary else {field.item: field.value for field in plan.fields}
    conflict = set()
    for other in protected:
        if other.room == plan.room:
            continue
        constraints = dict(other.boundary_effects) if boundary else {field.item: field.value for field in other.fields}
        if any(item in constraints and constraints[item] != value for item, value in wanted.items()):
            conflict.add(other.room)
    return sorted(conflict)
