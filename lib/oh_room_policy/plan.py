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
    """A route's target for one Item. An alternative (value, condition Item,
    condition value) is matched too while its condition Item is freshly
    observed at its condition value; a repair still writes only `value`."""
    item: str
    value: str
    resource: str
    allowed: tuple[str, ...]
    native_power: bool = False
    max_age: float = 30.0
    alternatives: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Observation:
    value: object
    observed_at: datetime | None
    basis: str = "native"


@dataclass(frozen=True)
class Plan:
    """`conditions` are Items the fields' alternatives depend on: observed
    with the fields, never repaired or compared for conflicts."""
    room: str
    target: str
    source_item: str
    fields: tuple[Field, ...]
    resources: tuple[str, ...]
    boundary_effects: tuple[tuple[str, str], ...]
    limitations: tuple[str, ...] = ()
    conditions: tuple[Field, ...] = ()

    def observed(self) -> tuple[str, ...]:
        """Every Item verify() reads: the fields', then the conditions'."""
        return tuple(dict.fromkeys(field.item for field in (*self.fields, *self.conditions)))


def expected_plan(house, room: str, choice: str) -> Plan:
    """Use routing's command generators; never duplicate physical wiring.

    Only the chosen physical path is verified. Parking an unused matrix
    for a local source does not add that matrix's health as a dependency.
    Boundary effects include one-shot actions/EDID, but fields never do.
    A room whose receiver syncs with a leader also accepts its zone on the
    leader's input while the leader's matrix output shows the source.
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

    # Policy verification and lifecycle ownership share one active path.
    # Boundary-scoped parking and imperative actions remain effects below.
    from oh_features.routing.lifecycle import active_requirements
    active = active_requirements(data, room, choice) if choice != "off" else None
    sync = raw.get("sync")
    synced = sync["choices"].get(choice) if sync else None
    leader_input = str(config["sources"][choice]["hdmi_input"]) if synced else None
    steps = (routing.power(data, room, choice) + routing.commands(data, room, choice) + inputs
             + [step for step in routing.after(data, room, choice) if step[0] in relevant_power or step[0] in allowed])
    fields = []
    for item, value in steps:
        ident = owner(item)
        if ident is None:
            raise PolicyError("unknown_controller_field")
        device = devices[ident]
        if active is not None and item not in active:
            continue
        accepted = set(map(str, allowed[item]))
        module = routing.switcher(device)
        if module is not None:
            accepted.update(str(n) for n in range(1, module.ports(device)[0] + 1))
        alternatives = ((synced["value"], sync["hdmi"], leader_input),) if synced and item == sync["input"] else ()
        fields.append(Field(item, str(value), "device:" + ident, tuple(sorted(accepted)), item in native_items,
                            alternatives=alternatives))
    # Keep command order, but a field occurring twice has one final target.
    by_item = {field.item: field for field in fields}
    fields = list(by_item.values())
    effects = raw["power"][choice] + raw["choices"][choice] + raw["inputs"][choice] + raw["after"][choice]
    edid = table["edid"]
    local_stage = any(choice in (entry.get(key, {}).get("sources") or {}) for key in ("receiver", "display"))
    local_switch = choice in (entry.get("hdmi_switch", {}).get("inputs") or {})
    source = config["sources"].get(choice, {})
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
    conditions = ()
    if synced:
        matrix = devices[config["hdmi_matrix"]]
        numbers = tuple(sorted(str(n) for n in range(1, routing.switcher(matrix).ports(matrix)[0] + 1)))
        conditions = (Field(sync["hdmi"], leader_input, "device:" + matrix["id"], numbers),)
    return Plan(room, choice, raw["item"], tuple(fields), tuple(sorted(resources)),
                tuple((item, str(value)) for item, value in effects), tuple(limits), conditions)


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


def _fresh(field: Field, observations: dict[str, Observation], now: datetime) -> str | None:
    """The field Item's fresh native reading as one of its allowed values."""
    reading = observations.get(field.item)
    valid = isinstance(reading, Observation) and reading.observed_at is not None
    if valid:
        try:
            at = utc(reading.observed_at)
            valid = 0 <= (now - at).total_seconds() <= field.max_age
        except PolicyError:
            valid = False
        valid = valid and reading.basis in (("native_power",) if field.native_power else ("native", "native_power"))
    return _value(reading.value, field.allowed) if valid else None


def verify(plan: Plan, observations: dict[str, Observation], now_utc: datetime) -> dict:
    """No ACK/receipt, stale/unknown reading, or passive endpoint is success.
    A field on an alternative matches while its condition is freshly
    observed at its value, is unknown while the condition is, and else
    mismatches."""
    now = utc(now_utc)
    conditions = {condition.item: _fresh(condition, observations, now) for condition in plan.conditions}
    matched, mismatched, unknown = [], [], []
    for field in plan.fields:
        value = _fresh(field, observations, now)
        if value is not None and value != field.value:
            held = {conditions.get(item) == wanted if conditions.get(item) is not None else None
                    for alternative, item, wanted in field.alternatives if alternative == value}
            value = field.value if True in held else None if None in held else value
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
