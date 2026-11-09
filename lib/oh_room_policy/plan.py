"""Compile existing routing contracts into native verification and repairs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

import oh_integrations as integrations
from oh_features import routing

from .model import PolicyError
from .schedule import utc


@dataclass(frozen=True)
class Field:
    """A route's target for one Item. Any fresh, definite reading other than
    `value` is a mismatch, whether or not a route uses it. An alternative
    (value, condition Item, condition value) is matched too while its
    condition Item is freshly observed at its condition value; a repair
    still writes only `value`."""
    item: str
    value: str
    resource: str
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
    with the fields, never repaired or compared for conflicts.

    A follower's route that can sync with `leader` claims its source's EDID
    only while it is not synced: its `unsynced_effects` are boundary effects
    except beside its leader's route for the same target (conflicts())."""
    room: str
    target: str
    source_item: str
    fields: tuple[Field, ...]
    resources: tuple[str, ...]
    boundary_effects: tuple[tuple[str, str], ...]
    limitations: tuple[str, ...] = ()
    conditions: tuple[Field, ...] = ()
    leader: str | None = None
    unsynced_effects: tuple[tuple[str, str], ...] = ()

    def observed(self) -> tuple[str, ...]:
        """Every Item verify() reads: the fields', then the conditions'."""
        return tuple(dict.fromkeys(field.item for field in (*self.fields, *self.conditions)))


def native_power_observations(devices) -> dict[str, str]:
    """{power Item: the Item holding when it was read} for every native
    power reading an integration declares (native_power_observation()). A
    field on such an Item is verified only by a native power reading
    (Field.native_power); the policy worker caches the same readings
    (oh_access.worker)."""
    out = {}
    for device in devices:
        for module in integrations.REGISTRY:
            hook = getattr(module, "native_power_observation", None)
            descriptor = hook(device) if hook else None
            if descriptor:
                out[descriptor["item"]] = descriptor["reported_at"]
    return out


def expected_plan(house, room: str, choice: str) -> Plan:
    """Use routing's command generators; never duplicate physical wiring.

    Only the chosen physical path is verified. Parking an unused matrix
    for a local source does not add that matrix's health as a dependency.
    Boundary effects include one-shot actions/EDID, but fields never do.
    A room whose receiver syncs with a leader also accepts its zone on the
    leader's input while the leader's matrix output shows the source, and
    its source's EDID is an unsynced effect (Plan).
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
    native_items = set(native_power_observations(devices.values()))
    # The room's controlled Items: its stages, the sources it wakes and every
    # port any of its choices commands. Only their after-steps are verified.
    controlled = {item for _, stage in stage_list for item in (stage["power"], stage["input"])}
    for source in [choice, *entry.get("sleep_sources", [])]:
        controlled.update(item for item, _ in routing.wake(data, source))
    for target in ["off", *entry["sources"]]:
        controlled.update(item for item, _ in routing.commands(data, room, target))

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
             + [step for step in routing.after(data, room, choice)
                if step[0] in relevant_power or step[0] in controlled])
    fields = []
    for item, value in steps:
        ident = owner(item)
        if ident is None:
            raise PolicyError("unknown_controller_field")
        if active is not None and item not in active:
            continue
        alternatives = ((synced["value"], sync["hdmi"], leader_input),) if synced and item == sync["input"] else ()
        fields.append(Field(item, str(value), "device:" + ident, item in native_items, alternatives=alternatives))
    # Keep command order, but a field occurring twice has one final target.
    by_item = {field.item: field for field in fields}
    fields = list(by_item.values())
    effects = raw["power"][choice] + raw["choices"][choice] + raw["inputs"][choice] + raw["after"][choice]
    edid = table["edid"]
    local_stage = any(choice in (entry.get(key, {}).get("sources") or {}) for key in ("receiver", "display"))
    local_switch = choice in (entry.get("hdmi_switch", {}).get("inputs") or {})
    source = config["sources"].get(choice, {})
    claim = []
    if choice != "off" and not local_stage and not local_switch and edid and source.get("hdmi_input") is not None:
        name = edid["inputs"].get(str(source["hdmi_input"]))
        if name and raw["hdmi"] in edid["outputs"]:
            claim = [[name, edid["outputs"][raw["hdmi"]]]]
    # A synced route claims no EDID: the follower shows its leader's output.
    unsynced = claim if synced else []
    effects = effects + ([] if synced else claim)
    resources = {field.resource for field in fields}
    for item, _ in effects + unsynced:
        if ident := owner(item):
            resources.add("device:" + ident)
    limits = ["passive_endpoint:" + entry[key]["device"] for key in ("receiver", "display")
              if entry.get(key, {}).get("passive") is True]
    conditions = ()
    if synced:
        conditions = (Field(sync["hdmi"], leader_input, "device:" + config["hdmi_matrix"]),)
    return Plan(room, choice, raw["item"], tuple(fields), tuple(sorted(resources)),
                tuple((item, str(value)) for item, value in effects), tuple(limits), conditions,
                sync["leader"] if synced else None, tuple((item, str(value)) for item, value in unsynced))


#: A decimal number as a Number Item reports it: "3", "3.0", "-0.50".
_NUMBER = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?")


def canonical(value: object) -> str | None:
    """A reported value as verify() compares it, or None when it says
    nothing definite: not a string, NULL, UNDEF, empty or longer than 128
    characters. A decimal number takes its plainest form, so a Number Item's
    "3.0" is "3"; any other text compares exactly. The native cache stores
    readings in this form, so it never holds more than 128 characters."""
    if not isinstance(value, str) or value in ("NULL", "UNDEF", "") or len(value) > 128:
        return None
    number = _NUMBER.fullmatch(value)
    if number is None:
        return value
    sign, whole, fraction = number.groups()
    fraction = (fraction or "").rstrip("0")
    text = (whole.lstrip("0") or "0") + ("." + fraction if fraction else "")
    return "-" + text if sign and text != "0" else text


def _fresh(field: Field, observations: dict[str, Observation], now: datetime) -> str | None:
    """The field Item's fresh native reading (canonical()), or None."""
    reading = observations.get(field.item)
    valid = isinstance(reading, Observation) and reading.observed_at is not None
    if valid:
        try:
            at = utc(reading.observed_at)
            valid = 0 <= (now - at).total_seconds() <= field.max_age
        except PolicyError:
            valid = False
        valid = valid and reading.basis in (("native_power",) if field.native_power else ("native", "native_power"))
    return canonical(reading.value) if valid else None


def verify(plan: Plan, observations: dict[str, Observation], now_utc: datetime) -> dict:
    """No ACK/receipt or stale/unknown reading is success. Any fresh,
    definite reading other than the target mismatches, even one no route
    uses (a TV's home screen). A field on an alternative matches while its
    condition is freshly observed at its value, is unknown while the
    condition is, and else mismatches.

    A passive endpoint has no field: it cannot be read or repaired. The
    state covers the fields alone (`scope`), so a room with one is verified
    once they all read back; its limitation is reported in every state."""
    now = utc(now_utc)
    conditions = {condition.item: _fresh(condition, observations, now) for condition in plan.conditions}
    matched, mismatched, unknown = [], [], []
    for field in plan.fields:
        value, target = _fresh(field, observations, now), canonical(field.value)
        if value is not None and value != target:
            held = {conditions.get(item) == canonical(wanted) if conditions.get(item) is not None else None
                    for alternative, item, wanted in field.alternatives if canonical(alternative) == value}
            value = target if True in held else None if None in held else value
        if value is None:
            unknown.append(field.item)
        elif value != target:
            mismatched.append(field.item)
        else:
            matched.append(field.item)
    state = "degraded" if mismatched else "unknown" if unknown or not plan.fields else "verified"
    return {"state": state, "matched": matched, "mismatched": mismatched, "unknown": unknown,
            "limitations": list(plan.limitations), "scope": "configured_native_controls"}


def repair_plan(plan: Plan, verification: dict) -> list[tuple[str, str]]:
    """Known, fresh mismatches only; no Source, EDID or one-shot action."""
    mismatch = set(verification["mismatched"])
    return [(field.item, field.value) for field in plan.fields if field.item in mismatch]


def _effects(plan: Plan, plans: list[Plan]) -> dict[str, str]:
    """A plan's boundary effects among `plans`: a follower beside its
    leader's route for the same target syncs, so it claims no EDID."""
    synced = any(other.room == plan.leader and other.target == plan.target for other in plans)
    return dict(plan.boundary_effects if synced else (*plan.boundary_effects, *plan.unsynced_effects))


def conflicts(plan: Plan, protected: list[Plan], *, boundary: bool = False) -> list[str]:
    """Reject incompatible shared fields, including boundary-only EDID effects."""
    compared = [plan, *protected]
    wanted = _effects(plan, compared) if boundary else {field.item: field.value for field in plan.fields}
    conflict = set()
    for other in protected:
        if other.room == plan.room:
            continue
        constraints = _effects(other, compared) if boundary else {field.item: field.value for field in other.fields}
        if any(item in constraints and constraints[item] != value for item, value in wanted.items()):
            conflict.add(other.room)
    return sorted(conflict)
