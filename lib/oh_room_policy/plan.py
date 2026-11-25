"""Compile existing routing contracts into native verification and repairs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re

import oh_integrations as integrations
from oh_conf import House, uses
from oh_features import routing

from .model import PolicyError
from .schedule import utc

#: Default freshness window for a field whose integration declares no feedback.
DEFAULT_MAX_AGE = 30.0
#: Freshness window for a push-driven field (updates on change and reconnect).
PUSH_MAX_AGE = 300.0


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
    max_age: float = DEFAULT_MAX_AGE
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
    lifecycle_item: str | None = None

    def observed(self) -> tuple[str, ...]:
        """Every Item verify() reads: the fields', the conditions', and the lifecycle item if any."""
        items = [field.item for field in (*self.fields, *self.conditions)]
        if self.lifecycle_item:
            items.append(self.lifecycle_item)
        return tuple(dict.fromkeys(items))


def native_power_observations(devices) -> dict[str, str]:
    """{power Item: the Item holding when it was read} for every native
    power reading an integration declares (native_power_observation()). A
    field on such an Item is verified only by a native power reading
    (Field.native_power); the policy worker caches the same readings
    (oh_access.worker)."""
    out = {}
    for device in devices:
        for module in integrations.defining("native_power_observation"):
            descriptor = module.native_power_observation(device)
            if descriptor:
                out[descriptor["item"]] = descriptor["reported_at"]
    return out


def _max_age(feedback: object) -> float:
    """Derive Field.max_age from a Stage 2c fields(device) feedback contract."""
    if not isinstance(feedback, dict):
        return DEFAULT_MAX_AGE
    kind = feedback.get("kind")
    if kind == "poll":
        interval = feedback.get("interval_s")
        if type(interval) in (int, float) and interval > 0:
            return float(2 * interval + 10)
    elif kind == "push":
        return PUSH_MAX_AGE
    elif kind == "none":
        return 0.0
    return DEFAULT_MAX_AGE


def _field_max_ages(devices) -> dict[str, float]:
    """{Item: Field.max_age} derived generically from fields(device) feedback
    with per-device `device.timing` overrides applied."""
    from oh_features.routing import lifecycle_io

    out: dict[str, float] = {}
    for device in devices:
        for module in integrations.defining("fields"):
            if not uses(device, "control", module.NAME):
                continue
            declared = module.fields(device) or {}
            for item, field in declared.items():
                effective = lifecycle_io.apply_timing(device, field) if isinstance(field, dict) else None
                out[item] = _max_age(effective.get("feedback") if isinstance(effective, dict) else None)
    return out


def _as_house(house) -> House:
    """Wrap a validated contract's plain mapping without re-validating it."""
    return house if hasattr(house, "data") else House.unchecked(house)


def compile_plans(house, rooms) -> dict[str, dict[str, Plan]]:
    """Compile every room's route plans once, building routing.table(house) a single time."""
    data = house.data if hasattr(house, "data") else house
    if isinstance(data, dict) and "routing" in data and "devices" in data:
        house_obj = _as_house(house)
        table = routing.table(house_obj)
    else:
        house_obj, table = house, None
    return {room: {choice: expected_plan(house_obj, room, choice, table=table)
                   for choice in ("off", *spec["sources"])}
            for room, spec in rooms.items()}


def expected_plan(house, room: str, choice: str, *, table: dict | None = None) -> Plan:
    """Compile policy verification and boundary effects from the hop graph.

    Only the chosen physical path is verified. Parking an unused matrix
    for a local source does not add that matrix's health as a dependency.
    Boundary effects include one-shot actions/EDID, but fields never do.
    A room whose receiver syncs with a leader also accepts its zone on the
    leader's input while the leader's matrix output shows the source, and
    its source's EDID is an unsynced effect (Plan).
    """
    from oh_features.routing import hops

    data = house.data if hasattr(house, "data") else house
    if not isinstance(data, dict) or "routing" not in data or "devices" not in data:
        raise PolicyError("physical_contract_required")
    config = data["routing"]
    if room not in config["rooms"] or choice not in ["off", *config["rooms"][room]["sources"]]:
        raise PolicyError("invalid_room_source")
    entry = config["rooms"][room]
    # The gateway hands over the mapping its contract carries, which the
    # contract's compiler took from a loaded house.
    house = _as_house(house)
    devices = house.by_id
    for source in entry.get("sleep_sources", []):
        if any(other != room and source in spec["sources"] for other, spec in config["rooms"].items()):
            raise PolicyError("unsafe_shared_source_sleep")
    if table is None:
        table = routing.table(house)
    room_path = hops.compile_room_path(house, room, choice)
    native_items = set(native_power_observations(devices.values()))
    max_ages = _field_max_ages(devices.values())

    def owner(item: str) -> str | None:
        return next((ident for ident in sorted(devices, key=len, reverse=True) if item.startswith(ident + "_")), None)

    sync = room_path.sync
    leader_input = str(sync.source_hdmi_input) if sync else None
    fields: list[Field] = []
    for item, value, hop_device in room_path.policy_verified_steps():
        ident = hop_device if hop_device in devices else owner(item)
        if ident is None:
            raise PolicyError("unknown_controller_field")
        alternatives = (
            ((sync.leader_input_value, sync.leader_hdmi_item, leader_input),)
            if sync and leader_input is not None and item == sync.follower_input_item
            else ()
        )
        fields.append(Field(
            item, str(value), "device:" + ident, item in native_items,
            max_age=max_ages.get(item, DEFAULT_MAX_AGE), alternatives=alternatives,
        ))
    # Keep command order, but a field occurring twice has one final target.
    by_item = {field.item: field for field in fields}
    fields = list(by_item.values())

    effects = (
        room_path.power_steps()
        + room_path.command_steps()
        + room_path.input_steps()
        + room_path.after_steps()
    )
    claim = [[room_path.edid.source_edid_item, room_path.edid.preset_code]] if room_path.edid else []
    unsynced = claim if (room_path.edid and room_path.edid.unsynced) else []
    effects = effects + ([] if unsynced else claim)

    resources = {field.resource for field in fields}
    for item, _ in effects + unsynced:
        if ident := owner(item):
            resources.add("device:" + ident)
    limits = ["passive_endpoint:" + dev for dev in room_path.passive_devices]
    conditions: tuple[Field, ...] = ()
    if sync and leader_input is not None:
        conditions = (Field(
            sync.leader_hdmi_item, leader_input, "device:" + sync.leader_hdmi_device,
            max_age=max_ages.get(sync.leader_hdmi_item, DEFAULT_MAX_AGE),
        ),)
    lifecycle_item = f"loc_{room}_source_lifecycle" if "lifecycle" in table else None
    return Plan(
        room, choice, room_path.source_item, tuple(fields), tuple(sorted(resources)),
        tuple((item, str(value)) for item, value in effects), tuple(limits), conditions,
        sync.leader if sync else None, tuple((item, str(value)) for item, value in unsynced),
        lifecycle_item,
    )


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


def route_in_flight(plan: Plan, observations: dict[str, Observation], now_utc: datetime, *, max_age: float = 60.0) -> bool:
    """Stage 3b fence: whether the room's lifecycle Item reports a route
    currently running. While running, the policy worker defers repairs
    rather than firing premature repairs against an in-flight route switch."""
    item = plan.lifecycle_item or f"loc_{plan.room}_source_lifecycle"
    reading = observations.get(item)
    if not isinstance(reading, Observation) or reading.observed_at is None:
        return False
    try:
        now = utc(now_utc)
        at = utc(reading.observed_at)
    except PolicyError:
        return False
    if not 0 <= (now - at).total_seconds() <= max_age:
        return False
    raw = reading.value
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return False
    elif isinstance(raw, dict):
        data = raw
    else:
        return False
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    if status is not None:
        return status == "running"
    # Legacy lifecycle snapshot without an explicit status field.
    return data.get("pathReady") is False and not data.get("fault")


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
