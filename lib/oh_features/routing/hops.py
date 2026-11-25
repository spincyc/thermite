"""Single hop-graph compiler for routed rooms (Stage 3a room model).

A routed room's signal path for (house, room, choice) compiles once into a
typed, ordered `RoomPath` of hops (matrix outputs, cascaded HDMI switches,
stages such as receiver zones / soundbars / displays, explicit audio branches,
USB device ports, source wake/sleep power, and room actions) plus relations
(`sync`, `edid`, `carries`).

Every routing consumer derives from this module rather than re-parsing room
wiring independently:
- `oh_features.routing` & `routing.js` (steps, table, per-room holdMs, readback)
- `oh_features.routing.lifecycle` (active requirements & lifecycle descriptor)
- `oh_room_policy.plan` (verified fields, conditions, effects, limitations)
- `oh_features.remote_diagnostics` & `remote_topology` (signal paths & USB)
- `oh_features.reboots` (device idle room dependencies)
- `oh_testing.routing` (routing check expectations & readback evaluation)

This module is strictly generic and names no device vendor or integration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Literal, Union

import oh_integrations as integrations
from oh_conf import House, config as device_config, uses

#: Source command/state for a room with nothing routed to it.
OFF = "off"
#: Source state when some room stages are on while the path is incomplete.
PARTIAL = "partial"
#: Minimum seconds routing.js holds a room's readback after a choice.
HOLD = 15
#: Stage roles in upstream-to-downstream signal order.
STAGE_ROLES: tuple[Literal["receiver", "soundbar", "display"], ...] = ("receiver", "soundbar", "display")
#: Allowed `carries` values for a room source.
CARRIES: tuple[Literal["display", "audio", "both"], ...] = ("display", "audio", "both")


@dataclass(frozen=True)
class MatrixOutputHop:
    """Switching an output on an HDMI matrix (video or split audio)."""
    kind: Literal["matrix_output"] = field(default="matrix_output", init=False)
    device: str
    output: int
    item: str
    input_value: int
    park_value: int
    active: bool
    hotplug_item: str | None = None
    role: Literal["video", "audio"] = "video"
    scaler_item: str | None = None
    scaler_value: int | None = None
    scaler_default: int | None = None
    poll_s: int | float = 0


@dataclass(frozen=True)
class SwitchHop:
    """Switching an HDMI switch or downstream matrix output (supports cascades)."""
    kind: Literal["switch"] = field(default="switch", init=False)
    device: str
    output: int | None
    item: str
    input_value: int | None
    park_value: int | None
    active: bool
    hotplug_item: str | None = None
    inputs_map: tuple[tuple[str, int], ...] = ()
    poll_s: int | float = 0
    index: int = 0


@dataclass(frozen=True)
class StageHop:
    """A powered/switched stage in the room chain: receiver zone, soundbar, or display."""
    kind: Literal["stage"] = field(default="stage", init=False)
    role: Literal["receiver", "soundbar", "display"]
    device: str
    zone: int | None
    power_item: str | None
    input_item: str | None
    input_value: str | None
    feed_input: str | None
    by_source: tuple[tuple[str, str], ...] = ()
    local_sources: tuple[tuple[str, str], ...] = ()
    inputs_allowed: tuple[str, ...] = ()
    active: bool = False
    passive: bool = False
    read_back_s: int = 0
    follows: str | None = None
    sync_with: str | None = None
    index: int = 0


@dataclass(frozen=True)
class AudioBranchHop:
    """Explicit or unattached audio leg to a room's separate audio endpoint."""
    kind: Literal["audio_branch"] = field(default="audio_branch", init=False)
    device: str
    from_device: str | None
    from_port: str | int | None
    transport: str | None
    input_item: str | None
    input_value: str | None


@dataclass(frozen=True)
class UsbPortHop:
    """USB matrix device-port routing for a room."""
    kind: Literal["usb_port"] = field(default="usb_port", init=False)
    device: str
    port: int
    item: str
    host_value: int
    park_value: int
    unused_policy: Literal["preserve", "park"]
    active: bool
    moved: bool
    poll_s: int | float = 0


@dataclass(frozen=True)
class SourceWakeHop:
    """Waking a chosen source on selection, or sleeping dedicated sources on Off."""
    kind: Literal["source_wake"] = field(default="source_wake", init=False)
    device: str
    item: str
    sleep_on_off: bool
    value: Literal["ON", "OFF"]


@dataclass(frozen=True)
class ActionHop:
    """Room-level `on_actions` / `off_actions` command."""
    kind: Literal["action"] = field(default="action", init=False)
    device: str
    item: str
    value: str


@dataclass(frozen=True)
class SyncRelation:
    """Receiver-zone sync relation between a follower room and its leader."""
    leader: str
    leader_hdmi_device: str
    leader_hdmi_item: str
    leader_hdmi_output: int
    leader_input_value: str
    follower_input_item: str
    source_hdmi_input: int
    plan_name: str


@dataclass(frozen=True)
class EdidRelation:
    """EDID claim for a matrix source on a room's output."""
    source_edid_item: str
    source_hdmi_input: int
    output_item: str
    preset_code: int
    room_default_code: int
    override_code: int | None
    unsynced: bool


Hop = Union[MatrixOutputHop, SwitchHop, StageHop, AudioBranchHop, UsbPortHop, SourceWakeHop, ActionHop]


@dataclass(frozen=True)
class RoomPath:
    """Single compiled hop-list representation of `(house, room, choice)`."""
    room: str
    choice: str
    source_item: str
    hops: tuple[Hop, ...]
    carries: Literal["both", "display", "audio"] = "both"
    sync: SyncRelation | None = None
    edid: EdidRelation | None = None
    is_local: bool = False
    local_origin: str | None = None
    hold_ms: int = HOLD * 1000
    passive_devices: tuple[str, ...] = ()

    def matrix_hops(self) -> tuple[MatrixOutputHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, MatrixOutputHop))

    def switch_hops(self) -> tuple[SwitchHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, SwitchHop))

    def stage_hops(self) -> tuple[StageHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, StageHop))

    def active_stage_hops(self) -> tuple[StageHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, StageHop) and hop.active)

    def audio_branch_hops(self) -> tuple[AudioBranchHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, AudioBranchHop))

    def usb_hops(self) -> tuple[UsbPortHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, UsbPortHop))

    def wake_hops(self) -> tuple[SourceWakeHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, SourceWakeHop))

    def action_hops(self) -> tuple[ActionHop, ...]:
        return tuple(hop for hop in self.hops if isinstance(hop, ActionHop))

    def audio_scaler_steps(self) -> list[list[Any]]:
        out: list[list[Any]] = []
        for hop in self.matrix_hops():
            if hop.scaler_item and hop.scaler_value is not None:
                out.append([hop.scaler_item, hop.scaler_value])
        return out

    def power_steps(self) -> list[list[Any]]:
        """[[Item, "ON"], ...] sent first for a source choice; empty for Off."""
        if self.choice == OFF:
            return []
        out: list[list[Any]] = [
            [hop.power_item, "ON"]
            for hop in reversed(self.stage_hops())
            if not hop.passive and hop.power_item and hop.active
        ]
        out.extend([hop.item, "ON"] for hop in self.wake_hops() if hop.value == "ON" and hop.item)
        return out

    def command_steps(self) -> list[list[Any]]:
        """[[Item, value], ...] for matrix outputs, audio scaler, switches, and USB ports."""
        out: list[list[Any]] = []
        for mhop in self.matrix_hops():
            if mhop.scaler_item and mhop.scaler_value is not None:
                out.append([mhop.scaler_item, mhop.scaler_value])
            if mhop.item:
                out.append([mhop.item, mhop.input_value])
        for swhop in self.switch_hops():
            if swhop.item and swhop.input_value is not None:
                out.append([swhop.item, swhop.input_value])
        for uhop in self.usb_hops():
            if uhop.item and uhop.moved:
                out.append([uhop.item, uhop.host_value])
        return out

    def input_steps(self, *, synced: bool = False) -> list[list[Any]]:
        """[[Item, input], ...] for active stages downstream-to-upstream; empty for Off."""
        if self.choice == OFF:
            return []
        sync = self.sync if synced else None
        out: list[list[Any]] = []
        for shop in reversed(self.stage_hops()):
            if shop.active and not shop.passive and shop.input_item and shop.input_value is not None:
                value = sync.leader_input_value if (sync and shop.input_item == sync.follower_input_item) else shop.input_value
                out.append([shop.input_item, value])
        return out

    def sleep_items(self) -> list[str]:
        """Dedicated source power Items slept on Off before turning stages off."""
        if self.choice != OFF:
            return []
        return [whop.item for whop in self.wake_hops() if whop.sleep_on_off and whop.value == "OFF" and whop.item]

    def after_steps(self) -> list[list[Any]]:
        """[[Item, value], ...] sent last: sleep_sources first, then stage power Off, then actions."""
        out: list[list[Any]] = []
        if self.choice == OFF:
            for whop in self.wake_hops():
                if whop.sleep_on_off and whop.value == "OFF" and whop.item:
                    out.append([whop.item, "OFF"])
            for shop in reversed(self.stage_hops()):
                if not shop.passive and shop.power_item:
                    out.append([shop.power_item, "OFF"])
        for ahop in self.action_hops():
            out.append([ahop.item, ahop.value])
        return out

    def active_requirements(self) -> dict[str, str]:
        """The selected path's required Item states, excluding parking and bypassed hops."""
        if self.choice == OFF:
            return {}
        result: dict[str, str] = {}
        for shop in reversed(self.stage_hops()):
            if shop.active and not shop.passive and shop.power_item:
                result[shop.power_item] = "ON"
        for whop in self.wake_hops():
            if whop.value == "ON" and whop.item:
                result[whop.item] = "ON"
        for mhop in self.matrix_hops():
            if not mhop.active:
                continue
            if mhop.scaler_item and mhop.scaler_value is not None:
                result[mhop.scaler_item] = str(mhop.scaler_value)
            if mhop.item:
                result[mhop.item] = str(mhop.input_value)
        for swhop in self.switch_hops():
            if swhop.active and swhop.item and swhop.input_value is not None:
                result[swhop.item] = str(swhop.input_value)
        for uhop in self.usb_hops():
            if uhop.active and uhop.item:
                result[uhop.item] = str(uhop.host_value)
        for shop in reversed(self.stage_hops()):
            if shop.active and not shop.passive and shop.input_item and shop.input_value is not None:
                result[shop.input_item] = str(shop.input_value)
        return result

    def policy_verified_steps(self) -> list[tuple[str, str, str]]:
        """[(item, value, device_id), ...] verified by room policy for this choice."""
        out: list[tuple[str, str, str]] = []
        if self.choice == OFF:
            for mhop in self.matrix_hops():
                if mhop.scaler_item and mhop.scaler_value is not None:
                    out.append((mhop.scaler_item, str(mhop.scaler_value), mhop.device))
                if mhop.item:
                    out.append((mhop.item, str(mhop.input_value), mhop.device))
            for swhop in self.switch_hops():
                if swhop.item and swhop.input_value is not None:
                    out.append((swhop.item, str(swhop.input_value), swhop.device))
            for uhop in self.usb_hops():
                if uhop.item and uhop.moved:
                    out.append((uhop.item, str(uhop.host_value), uhop.device))
            for whop in self.wake_hops():
                if whop.sleep_on_off and whop.value == "OFF" and whop.item:
                    out.append((whop.item, "OFF", whop.device))
            for shop in reversed(self.stage_hops()):
                if not shop.passive and shop.power_item:
                    out.append((shop.power_item, "OFF", shop.device))
            return out
        for shop in reversed(self.stage_hops()):
            if shop.active and not shop.passive and shop.power_item:
                out.append((shop.power_item, "ON", shop.device))
        for whop in self.wake_hops():
            if whop.value == "ON" and whop.item:
                out.append((whop.item, "ON", whop.device))
        for mhop in self.matrix_hops():
            if not mhop.active:
                continue
            if mhop.scaler_item and mhop.scaler_value is not None:
                out.append((mhop.scaler_item, str(mhop.scaler_value), mhop.device))
            if mhop.item:
                out.append((mhop.item, str(mhop.input_value), mhop.device))
        for swhop in self.switch_hops():
            if swhop.active and swhop.item and swhop.input_value is not None:
                out.append((swhop.item, str(swhop.input_value), swhop.device))
        for uhop in self.usb_hops():
            if uhop.active and uhop.item:
                out.append((uhop.item, str(uhop.host_value), uhop.device))
        for shop in reversed(self.stage_hops()):
            if shop.active and not shop.passive and shop.input_item and shop.input_value is not None:
                out.append((shop.input_item, str(shop.input_value), shop.device))
        return out


@dataclass(frozen=True)
class HopGraph:
    """All compiled RoomPaths for a house, indexed by `rooms[room][choice]`."""
    rooms: dict[str, dict[str, RoomPath]]
    room_hold_ms: dict[str, int]

    def path(self, room: str, choice: str) -> RoomPath:
        return self.rooms[room][choice]


# ---------------------------------------------------------------------------
# Pure integration & shape helpers (no vendor names)
# ---------------------------------------------------------------------------

def source_item_name(room: str) -> str:
    return f"loc_{room}_source"


def action_item(device_id: str) -> str:
    return f"{device_id}_action"


def sync_plan(source: str, leader: str) -> str:
    return f"{source}@{leader}"


def switcher(device: Any) -> ModuleType | None:
    return next((m for m in integrations.defining("switch_item") if uses(device, m.PHASE, m.NAME)), None)


def hdmi_switcher(device: Any) -> ModuleType | None:
    return next((m for m in integrations.defining("input_item") if uses(device, m.PHASE, m.NAME)), None)


def switch_module(devices: Mapping[str, Mapping[str, Any]], switch: Mapping[str, Any]) -> ModuleType | None:
    device = devices.get(switch["device"]) if isinstance(switch.get("device"), str) else None
    if device is None:
        return None
    return switcher(device) if "output" in switch else hdmi_switcher(device)


def stager(device: Any) -> ModuleType | None:
    return next((m for m in integrations.defining("stage") if uses(device, "control", m.NAME)), None)


def waker(device: Any) -> ModuleType | None:
    return next((m for m in integrations.defining("wake_item") if uses(device, "control", m.NAME)), None)


def port_item(devices: Mapping[str, Mapping[str, Any]], matrix_id: str, port: int) -> str:
    device = devices.get(matrix_id)
    module = switcher(device) if device is not None else None
    return str(module.switch_item(device, port)) if module is not None else ""


def switch_item(devices: Mapping[str, Mapping[str, Any]], switch: Mapping[str, Any]) -> str:
    device = devices.get(switch["device"]) if isinstance(switch.get("device"), str) else None
    module = switch_module(devices, switch) if device is not None else None
    if module is None:
        return ""
    if "output" in switch:
        return str(module.switch_item(device, switch["output"]))
    return str(module.input_item(device))


def hotplug_item(devices: Mapping[str, Mapping[str, Any]], matrix_id: str, port: int) -> str | None:
    device = devices.get(matrix_id)
    module = switcher(device) if device is not None else None
    if module is None or module.hotplug_item is None:
        return None
    value = module.hotplug_item(device, port)
    return str(value) if value is not None else None


def source_wake_item(devices: Mapping[str, Mapping[str, Any]], source: str) -> str | None:
    device = devices.get(source)
    module = waker(device) if device is not None else None
    if module is None:
        return None
    value = module.wake_item(device)
    return str(value) if value is not None else None


def usb_host(source: dict[str, Any], matrix_id: str, off_host: int) -> int:
    host = source.get("usb_host", off_host)
    if isinstance(host, dict):
        return int(host.get(matrix_id, off_host))
    return int(host)


def carried(entry: dict[str, Any], source: str) -> Literal["both", "display", "audio"]:
    mapping = entry.get("carries")
    value = mapping.get(source) if isinstance(mapping, dict) else None
    if value == "display":
        return "display"
    if value == "audio":
        return "audio"
    return "both"


def room_switches(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """All HDMI switches of a room ordered upstream-to-downstream."""
    if isinstance(entry.get("hdmi_switches"), list):
        return [sw for sw in entry["hdmi_switches"] if isinstance(sw, dict)]
    if isinstance(entry.get("hdmi_switch"), dict):
        return [entry["hdmi_switch"]]
    return []


def room_displays(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """All displays of a room in house order."""
    if isinstance(entry.get("displays"), list):
        return [disp for disp in entry["displays"] if isinstance(disp, dict)]
    if isinstance(entry.get("display"), dict):
        return [entry["display"]]
    return []


def room_stage_specs(entry: dict[str, Any]) -> list[tuple[Literal["receiver", "soundbar", "display"], dict[str, Any]]]:
    """Every stage spec of a room in upstream-to-downstream signal order."""
    out: list[tuple[Literal["receiver", "soundbar", "display"], dict[str, Any]]] = []
    if isinstance(entry.get("receiver"), dict):
        out.append(("receiver", entry["receiver"]))
    if isinstance(entry.get("soundbar"), dict):
        out.append(("soundbar", entry["soundbar"]))
    for disp in room_displays(entry):
        out.append(("display", disp))
    return out


def local_sources(entry: Mapping[str, Any], *, first: bool = False) -> dict[str, str]:
    """{source: place label} for sources cabled locally to a room's switches or stages."""
    out: dict[str, str] = {}
    places: list[tuple[str, dict[str, Any]]] = []
    if isinstance(entry.get("hdmi_switches"), list):
        for idx, sw in enumerate(entry["hdmi_switches"]):
            if isinstance(sw, dict) and isinstance(sw.get("inputs"), dict):
                places.append((f"hdmi_switches[{idx}]", sw["inputs"]))
    elif isinstance(entry.get("hdmi_switch"), dict):
        inputs = entry["hdmi_switch"].get("inputs")
        if isinstance(inputs, dict):
            places.append(("hdmi_switch", inputs))
    for role in ("receiver", "soundbar"):
        spec = entry.get(role)
        if isinstance(spec, dict) and isinstance(spec.get("sources"), dict):
            places.append((role, spec["sources"]))
    if isinstance(entry.get("displays"), list):
        for idx, disp in enumerate(entry["displays"]):
            if isinstance(disp, dict) and isinstance(disp.get("sources"), dict):
                places.append((f"displays[{idx}]", disp["sources"]))
    elif isinstance(entry.get("display"), dict):
        srcs = entry["display"].get("sources")
        if isinstance(srcs, dict):
            places.append(("display", srcs))
    for place, mapping in places:
        for source in mapping:
            if first:
                out.setdefault(source, place)
            else:
                out[source] = place
    return out


def synced_sources(entry: Mapping[str, Any], leader: Mapping[str, Any], sources: Mapping[str, Any]) -> dict[str, str]:
    """{source: leader receiver's input} for matrix sources shared by follower and leader."""
    raw_lead = leader.get("receiver")
    lead: dict[str, Any] = raw_lead if isinstance(raw_lead, dict) else {}
    raw_by_source = lead.get("input_by_source")
    by_source: dict[str, Any] = raw_by_source if isinstance(raw_by_source, dict) else {}
    offered = [entry.get("sources"), leader.get("sources")]
    if "input" not in lead or not all(isinstance(names, list) for names in offered):
        return {}
    local = {**local_sources(entry), **local_sources(leader)}
    assert isinstance(offered[0], list) and isinstance(offered[1], list)
    return {
        source: str(by_source.get(source, lead["input"]))
        for source in offered[0]
        if source in offered[1] and source not in local and isinstance(sources.get(source), dict)
        and "hdmi_input" in sources[source]
    }


def _audio_device_input_item(devices: Mapping[str, Mapping[str, Any]], device_id: str) -> str | None:
    device = devices.get(device_id)
    if device is None:
        return None
    for module in integrations.defining("remote"):
        control = module.remote(device)
        if control:
            inp = control.get("input")
            if isinstance(inp, dict) and isinstance(inp.get("item"), str):
                return inp["item"]
            return None
    return None


def _compute_hold_ms(stage_hops: list[StageHop]) -> int:
    slowest = max((hop.read_back_s for hop in stage_hops if not hop.passive), default=0)
    return 1000 * max(HOLD, slowest + 2)


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

def compile_room_path_data(
    data: Mapping[str, Any],
    room: str,
    choice: str,
    *,
    devices: Mapping[str, Mapping[str, Any]] | None = None,
) -> RoomPath:
    """Compile one `(room, choice)` path from a raw or validated house dict."""
    routing_cfg = data.get("routing") or {}
    rooms_cfg = routing_cfg.get("rooms") or {}
    sources_cfg = routing_cfg.get("sources") or {}
    entry = rooms_cfg[room]
    if devices is None:
        devices = {
            d["id"]: d
            for d in (data.get("devices") or [])
            if isinstance(d, dict) and isinstance(d.get("id"), str)
        }
    raw_source = sources_cfg.get(choice) if choice != OFF else None
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    switches = room_switches(entry)
    displays = room_displays(entry)
    receiver_spec = entry.get("receiver") if isinstance(entry.get("receiver"), dict) else None
    soundbar_spec = entry.get("soundbar") if isinstance(entry.get("soundbar"), dict) else None

    # 1. Determine where `choice` enters the room's signal chain.
    is_local = False
    local_origin: str | None = None
    local_display_idx: int | None = None
    local_switch_idx: int | None = None
    local_at_soundbar = False
    local_at_receiver = False

    if choice != OFF:
        for idx, disp in enumerate(displays):
            if choice in (disp.get("sources") or {}):
                is_local = True
                local_origin = f"display:{idx}"
                local_display_idx = idx
                break
        if not is_local and soundbar_spec is not None and choice in (soundbar_spec.get("sources") or {}):
            is_local = True
            local_origin = "soundbar"
            local_at_soundbar = True
        if not is_local and receiver_spec is not None and choice in (receiver_spec.get("sources") or {}):
            is_local = True
            local_origin = "receiver"
            local_at_receiver = True
        if not is_local:
            for idx in range(len(switches) - 1, -1, -1):
                if choice in (switches[idx].get("inputs") or {}):
                    is_local = True
                    local_origin = f"switch:{idx}"
                    local_switch_idx = idx
                    break

    hops: list[Hop] = []

    # 2. Primary HDMI matrix output hops (video, and optional split audio + scaler).
    hdmi_matrix_id = routing_cfg.get("hdmi_matrix")
    hdmi_off = routing_cfg.get("hdmi_off_input")
    park_val = int(hdmi_off) if isinstance(hdmi_off, int) and not isinstance(hdmi_off, bool) else 0
    source_hdmi = source.get("hdmi_input")
    matrix_active = choice != OFF and not is_local and isinstance(source_hdmi, int) and not isinstance(source_hdmi, bool)
    matrix_input_val = int(source_hdmi) if matrix_active and source_hdmi is not None else park_val

    matrix_dev: Any = devices.get(hdmi_matrix_id) if isinstance(hdmi_matrix_id, str) else None
    matrix_mod = switcher(matrix_dev) if matrix_dev is not None else None
    matrix_poll_s = matrix_mod.POLL_S if matrix_mod is not None else 0

    video_out_item: str | None = None
    if isinstance(hdmi_matrix_id, str) and "hdmi_output" in entry and isinstance(entry["hdmi_output"], int):
        out_port = int(entry["hdmi_output"])
        video_out_item = port_item(devices, hdmi_matrix_id, out_port)
        hops.append(MatrixOutputHop(
            device=hdmi_matrix_id,
            output=out_port,
            item=video_out_item,
            input_value=matrix_input_val,
            park_value=park_val,
            active=matrix_active,
            hotplug_item=hotplug_item(devices, hdmi_matrix_id, out_port),
            role="video",
            poll_s=matrix_poll_s,
        ))

    if isinstance(hdmi_matrix_id, str) and "hdmi_audio_output" in entry and isinstance(entry["hdmi_audio_output"], int):
        audio_port = int(entry["hdmi_audio_output"])
        scaler_item_name: str | None = None
        scaler_val: int | None = None
        scaler_default: int | None = None
        if "hdmi_audio_scaler" in entry and matrix_dev is not None and matrix_mod is not None and matrix_mod.SCALER is not None:
            fixed_name = device_config(matrix_dev, matrix_mod.NAME).get("scaler")
            if isinstance(fixed_name, str) and fixed_name in matrix_mod.SCALER:
                scaler_default = int(matrix_mod.SCALER[fixed_name])
                chosen_name = entry["hdmi_audio_scaler"].get(choice, fixed_name) if isinstance(entry["hdmi_audio_scaler"], dict) else fixed_name
                if isinstance(chosen_name, str) and chosen_name in matrix_mod.SCALER:
                    scaler_val = int(matrix_mod.SCALER[chosen_name])
                    scaler_item_name = str(matrix_mod.scaler_item(matrix_dev, audio_port))
        hops.append(MatrixOutputHop(
            device=hdmi_matrix_id,
            output=audio_port,
            item=port_item(devices, hdmi_matrix_id, audio_port),
            input_value=matrix_input_val,
            park_value=park_val,
            active=matrix_active,
            hotplug_item=hotplug_item(devices, hdmi_matrix_id, audio_port),
            role="audio",
            scaler_item=scaler_item_name,
            scaler_value=scaler_val,
            scaler_default=scaler_default,
            poll_s=matrix_poll_s,
        ))

    # 3. Cascaded HDMI switch / downstream matrix hops (upstream to downstream).
    for idx, sw in enumerate(switches):
        sw_dev_id = sw.get("device")
        if not isinstance(sw_dev_id, str):
            continue
        sw_out = sw.get("output") if isinstance(sw.get("output"), int) and not isinstance(sw.get("output"), bool) else None
        sw_park = sw.get("matrix_input") if isinstance(sw.get("matrix_input"), int) and not isinstance(sw.get("matrix_input"), bool) else None
        raw_sw_inputs = sw.get("inputs")
        sw_inputs_raw: dict[str, Any] = raw_sw_inputs if isinstance(raw_sw_inputs, dict) else {}
        sw_inputs_tuple = tuple(
            (str(src), int(num))
            for src, num in sw_inputs_raw.items()
            if isinstance(num, int) and not isinstance(num, bool)
        )
        sw_active = False
        sw_cmd_val: int | None = sw_park
        if choice != OFF and local_display_idx is None and not local_at_soundbar and not local_at_receiver:
            if local_switch_idx is not None:
                if idx == local_switch_idx:
                    sw_active = True
                    sw_cmd_val = int(sw_inputs_raw[choice])
                elif idx > local_switch_idx:
                    sw_active = sw_park is not None
                    sw_cmd_val = sw_park
                else:
                    sw_active = False
                    sw_cmd_val = sw_park
            else:
                # Signal originates upstream on the primary HDMI matrix.
                sw_active = sw_park is not None
                sw_cmd_val = sw_park
        sw_mod = switch_module(devices, sw)
        sw_poll = sw_mod.POLL_S if sw_mod is not None else 0
        sw_hotplug = hotplug_item(devices, sw_dev_id, sw_out) if sw_out is not None else None
        hops.append(SwitchHop(
            device=sw_dev_id,
            output=sw_out,
            item=switch_item(devices, sw),
            input_value=sw_cmd_val,
            park_value=sw_park,
            active=sw_active,
            hotplug_item=sw_hotplug,
            inputs_map=sw_inputs_tuple,
            poll_s=sw_poll,
            index=idx,
        ))

    # 4. Stage hops in upstream-to-downstream order: receiver -> soundbar -> displays.
    stage_hops: list[StageHop] = []
    passive_devices: list[str] = []

    def build_stage(
        role: Literal["receiver", "soundbar", "display"],
        spec: dict[str, Any],
        active: bool,
        index: int = 0,
    ) -> StageHop | None:
        dev_id = spec.get("device")
        if not isinstance(dev_id, str):
            return None
        passive = spec.get("passive") is True
        if passive:
            passive_devices.append(dev_id)
        dev = devices.get(dev_id)
        mod = None if (passive or dev is None) else stager(dev)
        raw_zone = spec.get("zone")
        zone = int(raw_zone) if isinstance(raw_zone, int) and not isinstance(raw_zone, bool) else None
        info = mod.stage(dev, zone) if mod is not None else None
        read_back_s = int(mod.READ_BACK) if mod is not None else 0
        feed = spec.get("input") if isinstance(spec.get("input"), str) else None
        raw_by_src = spec.get("input_by_source")
        by_src_raw: dict[str, Any] = raw_by_src if isinstance(raw_by_src, dict) else {}
        raw_local = spec.get("sources")
        local_raw: dict[str, Any] = raw_local if isinstance(raw_local, dict) else {}
        input_val: str | None = None
        if active and not passive:
            if choice in local_raw and isinstance(local_raw[choice], str):
                input_val = local_raw[choice]
            else:
                override = by_src_raw.get(choice)
                input_val = override if isinstance(override, str) else feed
        sync_target = spec.get("sync_with") if isinstance(spec.get("sync_with"), str) else None
        return StageHop(
            role=role,
            device=dev_id,
            zone=zone,
            power_item=str(info["power"]) if info else None,
            input_item=str(info["input"]) if info else None,
            input_value=input_val,
            feed_input=feed,
            by_source=tuple((str(k), str(v)) for k, v in by_src_raw.items() if isinstance(v, str)),
            local_sources=tuple((str(k), str(v)) for k, v in local_raw.items() if isinstance(v, str)),
            inputs_allowed=tuple(str(v) for v in info["inputs"]) if info else (),
            active=active,
            passive=passive,
            read_back_s=read_back_s,
            follows=str(info["follows"]) if (info and "follows" in info) else None,
            sync_with=sync_target,
            index=index,
        )

    if receiver_spec is not None:
        rec_active = choice != OFF and local_display_idx is None and not local_at_soundbar
        rec_hop = build_stage("receiver", receiver_spec, rec_active)
        if rec_hop is not None:
            stage_hops.append(rec_hop)
            hops.append(rec_hop)

    if soundbar_spec is not None:
        sb_active = choice != OFF and local_display_idx is None
        sb_hop = build_stage("soundbar", soundbar_spec, sb_active)
        if sb_hop is not None:
            stage_hops.append(sb_hop)
            hops.append(sb_hop)

    for idx, disp_spec in enumerate(displays):
        disp_active = choice != OFF and (local_display_idx is None or local_display_idx == idx)
        disp_hop = build_stage("display", disp_spec, disp_active, index=idx)
        if disp_hop is not None:
            stage_hops.append(disp_hop)
            hops.append(disp_hop)

    # 5. Explicit / unattached audio branch hop.
    room_carries: Literal["both", "display", "audio"] = carried(entry, choice) if choice != OFF else "both"
    audio_dev = entry.get("audio") if isinstance(entry.get("audio"), str) else None
    if choice != OFF and audio_dev and room_carries != "display":
        on_path = any(
            isinstance(hop, (MatrixOutputHop, SwitchHop, StageHop)) and hop.active and hop.device == audio_dev
            for hop in hops
        )
        if not on_path:
            raw_link = entry.get("audio_link")
            link: dict[str, Any] = raw_link if isinstance(raw_link, dict) else {}
            raw_origin = link.get("from")
            origin: dict[str, Any] = raw_origin if isinstance(raw_origin, dict) else {}
            from_dev = origin.get("device") if isinstance(origin.get("device"), str) else None
            from_port = origin.get("port") if from_dev is not None else None
            transport = link.get("transport") if (from_dev is not None and isinstance(link.get("transport"), str)) else None
            audio_input = entry.get("audio_input") if isinstance(entry.get("audio_input"), str) else None
            audio_input_item = _audio_device_input_item(devices, audio_dev) if audio_input is not None else None
            hops.append(AudioBranchHop(
                device=audio_dev,
                from_device=from_dev,
                from_port=from_port,
                transport=transport,
                input_item=audio_input_item,
                input_value=audio_input,
            ))

    # 6. USB device-port hops.
    usb_cfg = entry.get("usb") if isinstance(entry.get("usb"), dict) else {}
    usb_off = routing_cfg.get("usb_off_host")
    usb_off_val = int(usb_off) if isinstance(usb_off, int) and not isinstance(usb_off, bool) else 0
    unused_raw = entry.get("usb_unused", "preserve")
    unused_policy: Literal["preserve", "park"] = "park" if unused_raw == "park" else "preserve"
    usb_only_cfg = entry.get("usb_only") if isinstance(entry.get("usb_only"), dict) else {}
    only_for_choice = usb_only_cfg.get(choice) if (choice != OFF and isinstance(usb_only_cfg.get(choice), dict)) else None
    for usb_matrix_id, ports in usb_cfg.items():
        if not isinstance(usb_matrix_id, str) or not isinstance(ports, list):
            continue
        valid_ports = [int(p) for p in ports if isinstance(p, int) and not isinstance(p, bool)]
        usb_dev = devices.get(usb_matrix_id)
        usb_mod = switcher(usb_dev) if usb_dev is not None else None
        usb_poll = usb_mod.POLL_S if usb_mod is not None else 0
        host_val = usb_off_val if choice == OFF else usb_host(source, usb_matrix_id, usb_off_val)
        if only_for_choice is None:
            moved_ports = valid_ports
        else:
            raw_moved = only_for_choice.get(usb_matrix_id, [])
            moved_ports = [int(p) for p in raw_moved if isinstance(p, int) and not isinstance(p, bool)] if isinstance(raw_moved, list) else []
        for p in moved_ports:
            hops.append(UsbPortHop(
                device=usb_matrix_id,
                port=p,
                item=port_item(devices, usb_matrix_id, p),
                host_value=host_val,
                park_value=usb_off_val,
                unused_policy=unused_policy,
                active=(choice != OFF and host_val != usb_off_val),
                moved=True,
                poll_s=usb_poll,
            ))
        for p in valid_ports:
            if p in moved_ports:
                continue
            hops.append(UsbPortHop(
                device=usb_matrix_id,
                port=p,
                item=port_item(devices, usb_matrix_id, p),
                host_value=usb_off_val,
                park_value=usb_off_val,
                unused_policy=unused_policy,
                active=False,
                moved=(unused_policy == "park"),
                poll_s=usb_poll,
            ))

    # 7. Source wake / sleep hops.
    sleep_list = [s for s in (entry.get("sleep_sources") or []) if isinstance(s, str)] if isinstance(entry.get("sleep_sources"), list) else []
    if choice != OFF:
        wake_it = source_wake_item(devices, choice)
        if wake_it:
            hops.append(SourceWakeHop(device=choice, item=wake_it, sleep_on_off=(choice in sleep_list), value="ON"))
    else:
        for sleeper in sleep_list:
            wake_it = source_wake_item(devices, sleeper)
            if wake_it:
                hops.append(SourceWakeHop(device=sleeper, item=wake_it, sleep_on_off=True, value="OFF"))

    # 8. Room action hops (`on_actions` / `off_actions`).
    action_key = "off_actions" if choice == OFF else "on_actions"
    actions_cfg = entry.get(action_key) if isinstance(entry.get(action_key), dict) else {}
    for dev_id, act_val in actions_cfg.items():
        if isinstance(dev_id, str) and isinstance(act_val, str):
            hops.append(ActionHop(device=dev_id, item=action_item(dev_id), value=act_val))

    # 9. Sync relation (receiver zone sync).
    sync_rel: SyncRelation | None = None
    if choice != OFF and receiver_spec is not None and isinstance(receiver_spec.get("sync_with"), str):
        leader_id = receiver_spec["sync_with"]
        leader_entry = rooms_cfg.get(leader_id)
        if isinstance(leader_entry, dict) and isinstance(leader_entry.get("hdmi_output"), int):
            shared_map = synced_sources(entry, leader_entry, sources_cfg)
            if choice in shared_map and isinstance(hdmi_matrix_id, str) and isinstance(source_hdmi, int):
                rec_stage = next((h for h in stage_hops if h.role == "receiver"), None)
                follower_inp = rec_stage.input_item if (rec_stage and rec_stage.input_item) else ""
                leader_out = int(leader_entry["hdmi_output"])
                sync_rel = SyncRelation(
                    leader=leader_id,
                    leader_hdmi_device=hdmi_matrix_id,
                    leader_hdmi_item=port_item(devices, hdmi_matrix_id, leader_out),
                    leader_hdmi_output=leader_out,
                    leader_input_value=shared_map[choice],
                    follower_input_item=follower_inp,
                    source_hdmi_input=int(source_hdmi),
                    plan_name=sync_plan(choice, leader_id),
                )

    # 10. EDID relation (matrix source EDID claim).
    edid_rel: EdidRelation | None = None
    if (
        choice != OFF
        and not is_local
        and routing_cfg.get("edid") is not None
        and matrix_active
        and video_out_item is not None
        and matrix_dev is not None
        and matrix_mod is not None
        and matrix_mod.EDID is not None
        and isinstance(source_hdmi, int)
        and isinstance(entry.get("hdmi_output"), int)
    ):
        edid_item = str(matrix_mod.edid_item(matrix_dev, int(source_hdmi)))
        default_name = entry.get("edid", matrix_mod.copy_edid(int(entry["hdmi_output"])))
        if isinstance(default_name, str) and default_name in matrix_mod.EDID:
            room_default = int(matrix_mod.EDID[default_name])
            overrides_cfg = entry.get("edid_by_source") if isinstance(entry.get("edid_by_source"), dict) else {}
            override_name = overrides_cfg.get(choice)
            override_code = int(matrix_mod.EDID[override_name]) if (isinstance(override_name, str) and override_name in matrix_mod.EDID) else None
            preset = override_code if override_code is not None else room_default
            edid_rel = EdidRelation(
                source_edid_item=edid_item,
                source_hdmi_input=int(source_hdmi),
                output_item=video_out_item,
                preset_code=preset,
                room_default_code=room_default,
                override_code=override_code,
                unsynced=(sync_rel is not None),
            )

    return RoomPath(
        room=room,
        choice=choice,
        source_item=source_item_name(room),
        hops=tuple(hops),
        carries=room_carries,
        sync=sync_rel,
        edid=edid_rel,
        is_local=is_local,
        local_origin=local_origin,
        hold_ms=_compute_hold_ms(stage_hops),
        passive_devices=tuple(passive_devices),
    )


def compile_room_path(house: House, room: str, choice: str) -> RoomPath:
    """Compile one `(room, choice)` path from a validated `House`."""
    return compile_room_path_data(house.data, room, choice, devices=house.by_id)


def compile_house_data(data: Mapping[str, Any]) -> HopGraph:
    """Compile all rooms and choices from a raw house mapping."""
    routing_cfg = data.get("routing")
    if not isinstance(routing_cfg, dict):
        return HopGraph(rooms={}, room_hold_ms={})
    rooms_cfg = routing_cfg.get("rooms")
    if not isinstance(rooms_cfg, dict):
        return HopGraph(rooms={}, room_hold_ms={})
    devices = {
        d["id"]: d
        for d in (data.get("devices") or [])
        if isinstance(d, dict) and isinstance(d.get("id"), str)
    }
    compiled: dict[str, dict[str, RoomPath]] = {}
    holds: dict[str, int] = {}
    for room, entry in rooms_cfg.items():
        if not isinstance(room, str) or not isinstance(entry, dict):
            continue
        offered = [s for s in (entry.get("sources") or []) if isinstance(s, str)] if isinstance(entry.get("sources"), list) else []
        room_paths: dict[str, RoomPath] = {}
        for choice in (OFF, *offered):
            room_paths[choice] = compile_room_path_data(data, room, choice, devices=devices)
        compiled[room] = room_paths
        holds[room] = room_paths[OFF].hold_ms
    return HopGraph(rooms=compiled, room_hold_ms=holds)


def compile_house(house: House) -> HopGraph:
    """Compile every routed room and choice of a validated `House`."""
    routing_cfg = house.data.get("routing")
    if not isinstance(routing_cfg, dict):
        return HopGraph(rooms={}, room_hold_ms={})
    rooms_cfg = routing_cfg.get("rooms")
    if not isinstance(rooms_cfg, dict):
        return HopGraph(rooms={}, room_hold_ms={})
    compiled: dict[str, dict[str, RoomPath]] = {}
    holds: dict[str, int] = {}
    for room, entry in rooms_cfg.items():
        offered = list(entry["sources"])
        room_paths = {
            choice: compile_room_path_data(house.data, room, choice, devices=house.by_id)
            for choice in (OFF, *offered)
        }
        compiled[room] = room_paths
        holds[room] = room_paths[OFF].hold_ms
    return HopGraph(rooms=compiled, room_hold_ms=holds)


def device_rooms(
    data: Mapping[str, Any],
    device_id: str,
    *,
    include_video: bool = False,
    include_stages: bool = False,
) -> list[str]:
    """Routed room ids whose hop graph uses `device_id`, in house order.

    Always matches USB matrix hops; `include_video` adds primary HDMI matrix
    and switch hops (including cascaded `hdmi_switches`); `include_stages` adds
    stage hops (`receiver`, `soundbar`, `display`, `displays`).
    """
    routing_cfg = data.get("routing")
    if not isinstance(routing_cfg, dict):
        return []
    rooms_cfg = routing_cfg.get("rooms")
    if not isinstance(rooms_cfg, dict):
        return []
    devices = {
        d["id"]: d
        for d in (data.get("devices") or [])
        if isinstance(d, dict) and isinstance(d.get("id"), str)
    }
    matched: list[str] = []
    for room, entry in rooms_cfg.items():
        if not isinstance(room, str) or not isinstance(entry, dict):
            continue
        path = compile_room_path_data(data, room, OFF, devices=devices)
        for hop in path.hops:
            if isinstance(hop, UsbPortHop) and hop.device == device_id:
                matched.append(room)
                break
            if include_video and isinstance(hop, (MatrixOutputHop, SwitchHop)) and hop.device == device_id:
                matched.append(room)
                break
            if include_stages and isinstance(hop, StageHop) and hop.device == device_id:
                matched.append(room)
                break
    return matched


# ---------------------------------------------------------------------------
# Canonical readback evaluator (mirrors routing.js shown() exactly)
# ---------------------------------------------------------------------------

def _switches_of(room_table: dict[str, Any]) -> list[dict[str, Any]]:
    switches = room_table.get("switches")
    if isinstance(switches, list):
        return switches
    single = room_table.get("switch")
    return [single] if isinstance(single, dict) else []


def _known_stage_input(stage: dict[str, Any], value: str) -> bool:
    return (
        value == stage.get("feed")
        or value in (stage.get("readback") or {})
        or value in (stage.get("bySource") or {}).values()
    )


def evaluate_readback(
    room_table: dict[str, Any],
    reported_state: Callable[[str], str | None],
    *,
    off: str = OFF,
    partial: str = PARTIAL,
) -> str:
    """Evaluate a room's shown Source state from current Item readings.

    Matches `routing.js` `shown(room)` semantics:
    - All stages OFF -> `off`
    - Mix of ON and OFF stages -> source if a local source's active stages are all
      ON and bypass every OFF stage, else `partial`
    - One stage OFF and another unread -> `"UNDEF"`
    - Stages ON and disagreeing on known inputs -> `partial`
    - Controlled stages ON while HDMI matrix is parked at `off` -> `partial`
    - Unrecognized input or unread device -> `"UNDEF"`
    """
    stages: list[dict[str, Any]] = list(room_table.get("stages") or [])
    sync = room_table.get("sync")

    def selected(choice: str, inputs: list[list[Any]] | None = None) -> str:
        expected_steps = room_table["inputs"].get(choice, []) if inputs is None else inputs
        expected_map = {item: str(val) for item, val in expected_steps}
        for stage in stages:
            if stage["input"] not in expected_map:
                continue
            power = reported_state(stage["power"])
            if power == "OFF":
                return partial
            if room_table.get("audioHdmi") and power != "ON":
                return "UNDEF"
        for item, expected in expected_steps:
            value = reported_state(item)
            if value is None:
                return "UNDEF"
            if value != str(expected):
                stage_match = next((s for s in stages if s["input"] == item), None)
                if stage_match is not None and _known_stage_input(stage_match, value):
                    return partial
                return "UNDEF"
        return choice

    if stages:
        powers = [(stage, reported_state(stage["power"])) for stage in stages]
        if all(power == "OFF" for _, power in powers):
            return off
        any_off = any(power == "OFF" for _, power in powers)
        any_on = any(power == "ON" for _, power in powers)
        if any_off:
            if any_on:
                for stage, power in powers:
                    if power != "ON":
                        continue
                    inp = reported_state(stage["input"])
                    if inp is not None and inp in stage["readback"]:
                        candidate = stage["readback"][inp]
                        req_inputs = {item for item, _ in room_table["inputs"].get(candidate, [])}
                        blocked_by_off = any(p == "OFF" and s["input"] in req_inputs for s, p in powers)
                        if not blocked_by_off and selected(candidate) == candidate:
                            return candidate
                return partial
            return "UNDEF"
        if room_table.get("audioHdmi") and any(power != "ON" for _, power in powers):
            return "UNDEF"

    local_match: str | None = None
    for stage in stages:
        inp = reported_state(stage["input"])
        if inp is None:
            return "UNDEF"
        if inp in stage["readback"]:
            candidate = stage["readback"][inp]
            if local_match is not None and local_match != candidate:
                return partial
            result = selected(candidate)
            if result != candidate:
                return result
            local_match = candidate
            same_role = [s for s in stages if s is not stage and s.get("role") and s.get("role") == stage.get("role")]
            if not same_role:
                return candidate
            continue
        if sync and stage["input"] == sync["input"] and inp in {s["value"] for s in sync["choices"].values()}:
            leader_val = reported_state(sync["hdmi"])
            choice = room_table["readback"].get(leader_val, "") if leader_val is not None else None
            route = sync["choices"].get(choice) if choice is not None else None
            if route is None or route["value"] != inp or choice is None:
                return "UNDEF"
            return selected(str(choice), route["inputs"])
        if inp != stage.get("feed") and inp not in (stage.get("bySource") or {}).values():
            return "UNDEF"
    if local_match is not None:
        # If another stage of the same role is ON and not part of local_match,
        # the displays disagree on the active source -> partial.
        req_inputs = {item for item, _ in room_table["inputs"].get(local_match, [])}
        if any(s["input"] not in req_inputs and reported_state(s["power"]) == "ON"
               and s.get("role") == "display" for s in stages):
            return partial
        return local_match

    for sw in _switches_of(room_table):
        current = reported_state(sw["item"])
        if current is not None and current in sw["readback"]:
            return selected(sw["readback"][current])
        if current is None or current != sw["matrix"]:
            return "UNDEF"

    hdmi_item = room_table.get("hdmi")
    if not hdmi_item:
        return "UNDEF"
    current = reported_state(hdmi_item)
    if current is None or current not in room_table["readback"]:
        return "UNDEF"
    audio_hdmi = room_table.get("audioHdmi")
    if audio_hdmi:
        audio_cur = reported_state(audio_hdmi)
        if audio_cur != current:
            return "UNDEF"
    choice = room_table["readback"][current]
    if choice == off and stages and any(reported_state(stage["power"]) == "ON" for stage in stages):
        return partial
    return selected(choice)
