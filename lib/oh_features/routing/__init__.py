"Room sources over the HDMI and USB matrices (docs/routing.md).\n\ninstance/devices.yaml `routing` says which HDMI matrix input and USB host\neach source is cabled to, which HDMI output and USB device ports each room\nhas (and, under `usb_only`, a source that moves only some of those ports),\nwhich rooms have an HDMI switch in front of their display (the matrix\non one of its inputs, sources cabled to the room alone on others; or one\noutput of a matrix of the room's own, as the example's AVPro), each\nroom's receiver and display (`receiver`, `display`: the device, the input\nthe signal before it arrives on, and sources cabled to it alone). A display\nmay instead be `{device: ..., passive: true}`: a configured endpoint with no\ncontrol integration, guessed input, zone, power command or readback. What else\na source or Off does there (`on_actions`, `off_actions`: a curtain and\nlights, say), which dedicated sources explicitly enter standby on Off\n(`sleep_sources`), and, under\n`edid`, whether a source's input follows the EDID of the room display it is\nturned on in. A room without an `hdmi_output` offers only sources cabled\nto it, and one whose id is not a room's names its room (`room`), as the\nexample beside the Office's desk screen does. This feature validates it\nand renders:\n\n- one Source selector Item per room, `loc_<room>_source` (Main UI's Overview\n  lists them, oh_features.overview);\n- conf/automation/js/routing.js: routing.js beside this file with TABLE\n  filled in. For every room and choice TABLE lists the port Items to\n  command (the HDMI output first, then the room's HDMI switch, then its USB\n  device ports), the power and input Items of its receiver and display, the\n  power Item that wakes the source itself and the actions a choice sends,\n  and which choice each input on the room's display, receiver, HDMI switch\n  and HDMI output means; under `edid`, the EDID Item of each source's\n  input and the EDID code each room's output gives its sources. The rule\n  only commands; the Source state is always read back from the room's\n  devices and the HDMI matrix.\n\nThe matrices are any devices whose integration can switch (`ports()` and\n`switch_item()`, as bk808_http and ukm404_http provide), the HDMI switches\nany devices whose integration has `INPUTS` and `input_item()`, as\nvrroom_http does, receivers and displays any whose integration has\n`stage()`, as denonmarantz_http and sony_adcp do, and EDIDs need an HDMI\nmatrix integration with `EDID`, `edid_item()` and `copy_edid()`, so this\nmodule knows rooms and sources, not protocols.\n"

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import oh_integrations as integrations
import oh_inventory as inventory
from oh_conf import Addon, Conf, House, options_text, uses

NAME = "routing"
ITEMS = "yaml/routing.yaml"
RULE = "automation/js/routing.js"
SOURCE = Path(__file__).resolve().parent / "routing.js"
MARK = "const TABLE = null;"
HEADER = (
    "One Source selector per room, from the inventory's routing",
    "(docs/routing.md). conf/automation/js/routing.js turns a command into",
    "commands to the matrices and the room's receiver and display; the state",
    "is read back from those, so autoupdate is off and the selector never",
    "runs ahead of them.",
)
#: The Source command and state of a room with nothing routed to it.
OFF = "off"
EDID_KEYS = {"idle"}
#: A room's devices that a choice powers and switches, in the order the
#: signal reaches them: the screen is the last.
STAGES = ("receiver", "display")
STAGE_KEYS = {"device", "zone", "input", "input_by_source", "sources"}
#: The room keys of what its other devices are sent on any source, and on
#: Off. Not `on` and `off`: YAML reads those as true and false.
ACTIONS = ("on_actions", "off_actions")
ROOM_KEYS = {"room", "label", "hdmi_output", "hdmi_audio_output", "hdmi_switch", "usb", "usb_only", "sources", "sleep_sources", "edid",
             *STAGES, *ACTIONS}


def item_name(room: str) -> str:
    return f"loc_{room}_source"


def problem_item_name(room: str) -> str:
    return f"loc_{room}_source_problem"


def home(data: dict, room: str) -> str:
    """The inventory room a routed room is in: its `room`, else its id."""
    return data["routing"]["rooms"][room].get("room", room)


def room_label(data: dict, room: str) -> str:
    """A routed room's name on its Source selector and the remote: its own
    label where its screen is not the whole room, else the room's."""
    return data["routing"]["rooms"][room].get("label") or inventory.room_label(data, room)


def _number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def switcher(device: dict) -> ModuleType | None:
    """The integration that switches this matrix, if it lists one."""
    return next((m for m in integrations.REGISTRY
                 if hasattr(m, "switch_item") and uses(device, m.PHASE, m.NAME)), None)


def switchers() -> list[str]:
    return [m.NAME for m in integrations.REGISTRY if hasattr(m, "switch_item")]


def hdmi_switcher(device: dict) -> ModuleType | None:
    """The integration that switches this HDMI switch, if it lists one."""
    return next((m for m in integrations.REGISTRY
                 if hasattr(m, "input_item") and uses(device, m.PHASE, m.NAME)), None)


def hdmi_switchers() -> list[str]:
    return [m.NAME for m in integrations.REGISTRY if hasattr(m, "input_item")]


def stager(device: dict) -> ModuleType | None:
    """The integration that powers and switches this receiver or display,
    if the device lists it under control."""
    return next((m for m in integrations.REGISTRY if hasattr(m, "stage") and uses(device, "control", m.NAME)), None)


def stagers() -> list[str]:
    return [m.NAME for m in integrations.REGISTRY if hasattr(m, "stage")]


def waker(device: dict) -> ModuleType | None:
    """The integration that wakes this source (wake_item()), if the device
    lists it under control."""
    return next((m for m in integrations.REGISTRY if hasattr(m, "wake_item") and uses(device, "control", m.NAME)),
                None)


def action_item(devices: dict, device_id: str) -> str:
    """The String Item an action is sent to: its integration's
    action_item(), or, while the device has none, <id>_action, the name that
    integration is to give it (routing.js skips an Item that is not there)."""
    device = devices[device_id]
    module = next((m for m in integrations.REGISTRY if hasattr(m, "action_item") and uses(device, "control", m.NAME)),
                  None)
    return module.action_item(device) if module else f"{device_id}_action"


def usb_host(source: dict, matrix_id: str, off_host: int) -> int:
    """The host a source is cabled to on one USB matrix (off_host if none)."""
    host = source.get("usb_host", off_host)
    return host.get(matrix_id, off_host) if isinstance(host, dict) else host


def validate(data: object) -> list[str]:
    """Every problem in the inventory's routing section (none if it has none)."""
    if not isinstance(data, dict) or data.get("routing") is None:
        return []
    routing = data["routing"]
    if not isinstance(routing, dict):
        return ["routing: must be a mapping"]
    devices = {d.get("id"): d for d in data.get("devices") or [] if isinstance(d, dict)}
    rooms = data.get("rooms") or {}
    problems: list[str] = []

    def matrix(where: str, device_id: object, kind: str) -> tuple[int, int] | None:
        device = devices.get(device_id)
        if device is None:
            problems.append(f"{where}: '{device_id}' is not a device id")
            return None
        if device.get("kind") != kind:
            problems.append(f"{where}: {device_id} is a {device.get('kind')}, not a {kind}")
            return None
        module = switcher(device)
        if module is None:
            problems.append(f"{where}: {device_id} lists no matrix integration "
                            f"({', '.join(switchers())}) under controls")
            return None
        return module.ports(device)

    def port(where: str, value: object, limit: int | None) -> bool:
        if not _number(value) or value < 1 or (limit is not None and value > limit):
            span = f"1-{limit}" if limit else "a positive number"
            problems.append(f"{where}: {value!r} is not {span}")
            return False
        return True

    hdmi = matrix("routing.hdmi_matrix", routing.get("hdmi_matrix"), "hdmi_matrix")
    hdmi_inputs, hdmi_outputs = hdmi or (None, None)
    port("routing.hdmi_off_input", routing.get("hdmi_off_input"), hdmi_inputs)

    rooms_section = routing.get("rooms")
    if not isinstance(rooms_section, dict) or not rooms_section:
        problems.append("routing.rooms: must map room ids to their outputs")
        rooms_section = {}
    usb_sizes: dict[str, tuple[int, int] | None] = {}
    for room, entry in rooms_section.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("usb", {}), dict):
            continue
        for matrix_id in entry.get("usb") or {}:
            if matrix_id not in usb_sizes:
                usb_sizes[matrix_id] = matrix(f"routing.rooms.{room}.usb", matrix_id, "usb_matrix")
    usb_hosts = min((size[0] for size in usb_sizes.values() if size), default=None)
    off_host = routing.get("usb_off_host")
    port("routing.usb_off_host", off_host, usb_hosts)

    edid_names = _edid_names(routing, devices, hdmi is not None, problems)

    sources = routing.get("sources")
    if not isinstance(sources, dict) or not sources:
        problems.append("routing.sources: must map device ids to their matrix input")
        sources = {}
    switched = {source for entry in rooms_section.values() if isinstance(entry, dict)
                for source in _local(entry)}
    inputs: dict[object, str] = {}
    for source, entry in sources.items():
        where = f"routing.sources.{source}"
        if source == OFF:
            problems.append(f"{where}: '{OFF}' is the Source command for nothing")
        elif source not in devices:
            problems.append(f"{where}: '{source}' is not a device id")
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        if not isinstance(entry.get("label"), str) or not entry["label"].strip():
            problems.append(f"{where}: missing label")
        if "hdmi_input" not in entry:
            if source not in switched:
                problems.append(f"{where}: needs an hdmi_input, or an input on a room's hdmi_switch, "
                                "receiver or display")
        elif port(f"{where}.hdmi_input", entry.get("hdmi_input"), hdmi_inputs):
            if entry["hdmi_input"] == routing.get("hdmi_off_input"):
                problems.append(f"{where}.hdmi_input: {entry['hdmi_input']} is hdmi_off_input")
            elif entry["hdmi_input"] in inputs:
                problems.append(f"{where}.hdmi_input: {entry['hdmi_input']} already used by "
                                f"{inputs[entry['hdmi_input']]}")
            else:
                inputs[entry["hdmi_input"]] = source
        if "usb_host" in entry:
            hosts = entry["usb_host"]
            if isinstance(hosts, dict) and not hosts:
                problems.append(f"{where}.usb_host: must be a host, or map USB matrices to hosts")
            for matrix_id, host in (hosts.items() if isinstance(hosts, dict) else [(None, hosts)]):
                at = f"{where}.usb_host" if matrix_id is None else f"{where}.usb_host.{matrix_id}"
                if matrix_id is not None and matrix_id not in usb_sizes:
                    problems.append(f"{at}: '{matrix_id}' is not the USB matrix of any room")
                    continue
                size = usb_sizes.get(matrix_id) if matrix_id is not None else None
                if port(at, host, size[0] if size else usb_hosts) and host == off_host:
                    problems.append(f"{at}: {host} is usb_off_host")
        for key in sorted(set(entry) - {"label", "hdmi_input", "usb_host"}):
            problems.append(f"{where}: unknown key '{key}'")

    outputs: dict[object, str] = {}
    usb_ports: dict[tuple[str, object], str] = {}
    switch_rooms: dict[object, str] = {}
    staged: dict[tuple, str] = {}
    for room, entry in rooms_section.items():
        where = f"routing.rooms.{room}"
        if isinstance(entry, dict) and "room" in entry:
            if entry["room"] not in rooms or entry["room"] == "unassigned":
                problems.append(f"{where}.room: {entry['room']!r} is not a room id")
            if "label" not in entry:
                problems.append(f"{where}: needs a label, as its id is not a room's")
        elif room not in rooms or room == "unassigned":
            problems.append(f"{where}: '{room}' is not a room id")
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        fed = "hdmi_output" in entry
        for key in ("hdmi_output", "hdmi_audio_output"):
            if key in entry and port(f"{where}.{key}", entry[key], hdmi_outputs):
                if entry[key] in outputs:
                    problems.append(f"{where}.{key}: {entry[key]} already used by {outputs[entry[key]]}")
                outputs.setdefault(entry[key], room)
        if "hdmi_audio_output" in entry:
            if not fed or "receiver" not in entry:
                problems.append(f"{where}.hdmi_audio_output: needs hdmi_output and receiver")
            if "hdmi_switch" in entry:
                problems.append(f"{where}.hdmi_audio_output: a split output cannot also use hdmi_switch")
        usb = entry.get("usb", {})
        if not isinstance(usb, dict):
            problems.append(f"{where}.usb: must map USB matrices to device ports")
            usb = {}
        for matrix_id, ports in usb.items():
            size = usb_sizes.get(matrix_id)
            if not isinstance(ports, list) or not ports:
                problems.append(f"{where}.usb.{matrix_id}: must list device ports")
                continue
            for value in ports:
                if port(f"{where}.usb.{matrix_id}", value, size[1] if size else None):
                    key = (matrix_id, value)
                    if key in usb_ports:
                        problems.append(f"{where}.usb.{matrix_id}: port {value} already used by "
                                        f"{usb_ports[key]}")
                    usb_ports.setdefault(key, room)
        offered = entry.get("sources")
        if not isinstance(offered, list) or not offered:
            problems.append(f"{where}.sources: must list the sources offered there")
            offered = []
        if "sleep_sources" in entry:
            sleepers = entry["sleep_sources"]
            if not isinstance(sleepers, list) or not sleepers:
                problems.append(f"{where}.sleep_sources: must list dedicated sources to put in standby")
            else:
                seen_sleepers = set()
                for source in sleepers:
                    if not isinstance(source, str) or source not in offered or source not in devices:
                        problems.append(f"{where}.sleep_sources: {source!r} is not an offered device")
                        continue
                    if source in seen_sleepers:
                        problems.append(f"{where}.sleep_sources: duplicate source {source!r}")
                    seen_sleepers.add(source)
                    if waker(devices[source]) is None:
                        problems.append(f"{where}.sleep_sources: {source!r} has no controlled power integration")
                    others = [other for other, spec in rooms_section.items() if other != room
                              and isinstance(spec, dict) and isinstance(spec.get("sources"), list)
                              and source in spec["sources"]]
                    if others:
                        problems.append(f"{where}.sleep_sources: {source!r} is shared with {', '.join(others)}")
        if "hdmi_switch" in entry:
            problems += _switch_problems(f"{where}.hdmi_switch", entry["hdmi_switch"], offered, sources, devices,
                                         switch_rooms, room, fed, routing.get("hdmi_matrix"))
        if "usb_only" in entry:
            problems += _usb_only_problems(f"{where}.usb_only", entry["usb_only"], offered, usb)
        upstream = fed or "hdmi_switch" in entry
        for key in STAGES:
            if key in entry:
                problems += _stage_problems(f"{where}.{key}", entry[key], offered, sources, devices, upstream,
                                            staged, room)
                upstream = True
        problems += [problem for key in ACTIONS if key in entry
                     for problem in _action_problems(f"{where}.{key}", entry[key], devices)]
        local = _local(entry)
        problems += [f"{where}.{at}: {source} is cabled to this room's {_local(entry, first=True)[source]} already"
                     for source, at in _local_twice(entry)]
        for source in offered:
            if source not in sources:
                problems.append(f"{where}.sources: '{source}' is not in routing.sources")
            elif not isinstance(sources[source], dict) or source in local:
                continue
            elif "hdmi_input" not in sources[source]:
                problems.append(f"{where}.sources: {source} has no hdmi_input and is not on this room's "
                                "hdmi_switch, receiver or display")
            elif not fed:
                problems.append(f"{where}.sources: {source} is on the HDMI matrix, and this room has no hdmi_output")
        if len(set(map(str, offered))) != len(offered):
            problems.append(f"{where}.sources: lists a source twice")
        if "edid" in entry and not fed:
            problems.append(f"{where}.edid: only applies with an hdmi_output")
        elif "edid" in entry:
            if routing.get("edid") is None:
                problems.append(f"{where}.edid: only applies with routing.edid")
            elif edid_names is not None and not (isinstance(entry["edid"], str) and entry["edid"] in edid_names):
                problems.append(f"{where}.edid: {entry['edid']!r} is not an EDID name of "
                                f"{routing.get('hdmi_matrix')}")
        if "label" in entry and (not isinstance(entry["label"], str) or not entry["label"].strip()):
            problems.append(f"{where}.label: must be a name")
        for key in sorted(set(entry) - ROOM_KEYS):
            problems.append(f"{where}: unknown key '{key}'")

    if routing.get("edid") is not None and hdmi is not None:
        fixed = (devices[routing["hdmi_matrix"]].get("settings") or {}).get("edid") or {}
        if isinstance(fixed, dict):
            for number, source in sorted(inputs.items()):
                key = number if number in fixed else "default" if "default" in fixed else None
                if key is not None:
                    problems.append(f"{routing['hdmi_matrix']}.settings.edid.{key}: routing.edid sets the "
                                    f"EDID of input {number} ({source}); fix only the inputs no source uses")
    for key in sorted(set(routing) - {"hdmi_matrix", "hdmi_off_input", "usb_off_host", "edid", "sources",
                                      "rooms"}):
        problems.append(f"routing: unknown key '{key}'")
    return problems


def _switch_inputs(room: dict) -> dict:
    """{source: switch input} of a room's HDMI switch, as far as it is a mapping."""
    switch = room.get("hdmi_switch")
    inputs = switch.get("inputs") if isinstance(switch, dict) else None
    return inputs if isinstance(inputs, dict) else {}


def _local(room: dict, first: bool = False) -> dict:
    """{source: where it is cabled} for the sources cabled to the room's
    HDMI switch, receiver or display alone, as far as those are mappings:
    the last place a source is listed (`first`: the first)."""
    out: dict = {}
    places = [("hdmi_switch", _switch_inputs(room))]
    places += [(key, (room[key].get("sources") if isinstance(room.get(key), dict) else None) or {}) for key in STAGES]
    for place, inputs in places:
        for source in inputs if isinstance(inputs, dict) else {}:
            if first:
                out.setdefault(source, place)
            else:
                out[source] = place
    return out


def _local_twice(room: dict) -> list[tuple[str, str]]:
    """(source, key) for each source listed under a second place in a room."""
    seen: set = set()
    twice = []
    for key in ("hdmi_switch", *STAGES):
        place = room.get(key)
        inputs = (place.get("inputs") if key == "hdmi_switch" else place.get("sources")) if isinstance(place, dict) \
            else None
        for source in inputs if isinstance(inputs, dict) else {}:
            if source in seen:
                twice.append((source, f"{key}.{'inputs' if key == 'hdmi_switch' else 'sources'}.{source}"))
            seen.add(source)
    return twice


def _stage_problems(where: str, spec: object, offered: list, sources: dict, devices: dict, upstream: bool,
                    used: dict, room: str) -> list[str]:
    """Problems with a room's receiver or display; records its device and
    zone in `used`. `upstream` is whether a signal reaches it from before:
    the matrix, the room's HDMI switch or its receiver."""
    if not isinstance(spec, dict):
        return [f"{where}: must be a mapping"]
    problems: list[str] = []
    device_id = spec.get("device")
    device = devices.get(device_id)
    if "passive" in spec:
        if spec["passive"] is not True:
            problems.append(f"{where}.passive: must be true; omit it for a controlled stage")
        if not where.endswith(".display"):
            problems.append(f"{where}.passive: only a display may be passive")
        if device is None:
            problems.append(f"{where}.device: '{device_id}' is not a device id")
        elif device.get("kind") not in {"monitor", "tv", "projector"}:
            problems.append(f"{where}.device: a passive display must be a monitor, tv or projector")
        elif (device.get("controls") or {}).get("control") or stager(device) is not None:
            problems.append(f"{where}.device: a passive display must not declare control integrations")
        if not upstream:
            problems.append(f"{where}: a passive display needs a configured upstream video path")
        for key in sorted(set(spec) - {"device", "passive"}):
            problems.append(f"{where}: passive display does not accept '{key}'")
        if device is not None:
            key = (device_id, None)
            if key in used:
                problems.append(f"{where}.device: {device_id} already used by {used[key]}")
            used.setdefault(key, room)
        return problems
    module = stager(device) if device is not None else None
    zone = spec.get("zone")
    found = None
    if device is None:
        problems.append(f"{where}.device: '{device_id}' is not a device id")
    elif module is None:
        problems.append(f"{where}.device: {device_id} lists no integration that powers and switches it "
                        f"({', '.join(stagers())}) under controls.control")
    elif zone is not None and (not _number(zone) or zone < 2):
        problems.append(f"{where}.zone: {zone!r} is not a zone number (2 or more; leave it out for the main zone)")
    else:
        found = module.stage(device, zone)
        if found is None:
            problems.append(f"{where}.zone: {device_id} has no zone {zone}")
    if device is not None:
        key = (device_id, zone)
        if key in used:
            problems.append(f"{where}.device: {device_id}{f' zone {zone}' if zone else ''} already used by {used[key]}")
        used.setdefault(key, room)
    values = found["inputs"] if found else None

    def value(at: str, input_: object) -> bool:
        if not isinstance(input_, str) or (values is not None and input_ not in values):
            span = f"one of {', '.join(values)}" if values else "an input name"
            problems.append(f"{at}: {input_!r} is not {span}")
            return False
        return True

    taken: dict[object, str] = {}
    if "input" in spec:
        if not upstream:
            problems.append(f"{where}.input: nothing reaches it from the HDMI matrix; leave it out")
        elif value(f"{where}.input", spec["input"]):
            taken[spec["input"]] = "input"
    elif upstream:
        problems.append(f"{where}.input: missing; name the input the signal before it arrives on")
    cabled = spec.get("sources", {})
    if not isinstance(cabled, dict):
        problems.append(f"{where}.sources: must map the sources cabled to it to its inputs")
        cabled = {}
    if not upstream and not cabled:
        problems.append(f"{where}: needs an input from the HDMI matrix or sources of its own")
    for source, input_ in cabled.items():
        at = f"{where}.sources.{source}"
        if source not in sources:
            problems.append(f"{at}: '{source}' is not in routing.sources")
        elif source not in offered:
            problems.append(f"{at}: {source} is not in this room's sources")
        if value(at, input_):
            if input_ in taken:
                problems.append(f"{at}: {input_} already used by {taken[input_]}")
            taken.setdefault(input_, source)
    overrides = spec.get("input_by_source", {})
    if not isinstance(overrides, dict):
        problems.append(f"{where}.input_by_source: must map offered upstream sources to inputs")
        overrides = {}
    for source, input_ in overrides.items():
        at = f"{where}.input_by_source.{source}"
        if not upstream or source not in offered or source not in sources or source in cabled:
            problems.append(f"{at}: must name an offered source upstream of this stage")
        if value(at, input_) and input_ in taken and taken[input_] != "input":
            problems.append(f"{at}: {input_} is already a locally cabled source input")
    for key in sorted(set(spec) - STAGE_KEYS):
        problems.append(f"{where}: unknown key '{key}'")
    return problems


def _action_problems(where: str, steps: object, devices: dict) -> list[str]:
    if not isinstance(steps, dict) or not steps:
        return [f"{where}: must map devices to the action each is sent"]
    problems: list[str] = []
    for device_id, action in steps.items():
        if device_id not in devices:
            problems.append(f"{where}.{device_id}: '{device_id}' is not a device id")
        if isinstance(action, bool):
            problems.append(f"{where}.{device_id}: {action!r} is not an action name (YAML reads on, off, yes "
                            "and no as true or false: quote it)")
        elif not isinstance(action, str) or not action.strip():
            problems.append(f"{where}.{device_id}: {action!r} is not an action name")
    return problems


def _switch_problems(where: str, switch: object, offered: list, sources: dict, devices: dict,
                     used: dict, room: str, fed: bool, hdmi_matrix: object) -> list[str]:
    """Problems with one room's hdmi_switch (an HDMI switch, or one
    `output` of an HDMI matrix other than routing.hdmi_matrix); records its
    device and output in `used`. `fed` is whether the room has an
    hdmi_output, which the switch's matrix_input then takes."""
    if not isinstance(switch, dict):
        return [f"{where}: must be a mapping"]
    problems: list[str] = []
    device_id = switch.get("device")
    device = devices.get(device_id)
    output = switch.get("output")
    numbers = None
    if device is None:
        problems.append(f"{where}.device: '{device_id}' is not a device id")
    elif "output" in switch:
        module = switcher(device)
        if device.get("kind") != "hdmi_matrix":
            problems.append(f"{where}.device: {device_id} is a {device.get('kind')}, not an hdmi_matrix, so it has "
                            "no output")
        elif device_id == hdmi_matrix:
            problems.append(f"{where}.device: {device_id} is routing.hdmi_matrix; a room takes it with hdmi_output")
        elif module is None:
            problems.append(f"{where}.device: {device_id} lists no matrix integration ({', '.join(switchers())}) "
                            "under controls")
        else:
            inputs, outputs = module.ports(device)
            if not _number(output) or not 1 <= output <= outputs:
                problems.append(f"{where}.output: {output!r} is not 1-{outputs}")
            numbers = range(1, inputs + 1)
    elif device.get("kind") != "hdmi_switch":
        problems.append(f"{where}.device: {device_id} is a {device.get('kind')}, not an hdmi_switch (for an HDMI "
                        "matrix, name its output)")
    elif hdmi_switcher(device) is None:
        problems.append(f"{where}.device: {device_id} lists no HDMI switch integration "
                        f"({', '.join(hdmi_switchers())}) under controls")
    else:
        numbers = hdmi_switcher(device).INPUTS
    key = (device_id, output if "output" in switch else None)
    shown = f"{device_id} output {output}" if "output" in switch else f"{device_id}"
    if device is not None and key in used:
        problems.append(f"{where}.device: {shown} already used by {used[key]}")
    elif device is not None:
        used[key] = room

    def number(at: str, value: object) -> bool:
        if not _number(value) or (numbers is not None and value not in numbers):
            span = f"{numbers[0]}-{numbers[-1]}" if numbers else "an input number"
            problems.append(f"{at}: {value!r} is not {span}")
            return False
        return True

    taken: dict[object, str] = {}
    if not fed:
        if "matrix_input" in switch:
            problems.append(f"{where}.matrix_input: nothing reaches it from the HDMI matrix, as this room has no "
                            "hdmi_output; leave it out")
    elif number(f"{where}.matrix_input", switch.get("matrix_input")):
        taken[switch["matrix_input"]] = "matrix_input"
    inputs = switch.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        problems.append(f"{where}.inputs: must map the sources cabled to it to its inputs")
        inputs = {}
    for source, value in inputs.items():
        at = f"{where}.inputs.{source}"
        if source not in sources:
            problems.append(f"{at}: '{source}' is not in routing.sources")
        elif source not in offered:
            problems.append(f"{at}: {source} is not in this room's sources")
        if number(at, value):
            if value in taken:
                problems.append(f"{at}: {value} already used by {taken[value]}")
            taken.setdefault(value, source)
    for name in sorted(set(switch) - {"device", "output", "matrix_input", "inputs"}):
        problems.append(f"{where}: unknown key '{name}'")
    return problems


def _usb_only_problems(where: str, only: object, offered: list, usb: dict) -> list[str]:
    """Problems with a room's usb_only: by source, the room's device ports
    it moves, by USB matrix."""
    if not isinstance(only, dict) or not only:
        return [f"{where}: must map sources to the device ports they move"]
    problems: list[str] = []
    for source, by_matrix in only.items():
        at = f"{where}.{source}"
        if source not in offered:
            problems.append(f"{at}: '{source}' is not in this room's sources")
        if not isinstance(by_matrix, dict) or not by_matrix:
            problems.append(f"{at}: must map USB matrices to device ports")
            continue
        for matrix_id, ports in by_matrix.items():
            room_ports = usb.get(matrix_id)
            if not isinstance(room_ports, list):
                problems.append(f"{at}.{matrix_id}: '{matrix_id}' is not a USB matrix of this room")
            elif not isinstance(ports, list) or not ports:
                problems.append(f"{at}.{matrix_id}: must list device ports")
            else:
                problems += [f"{at}.{matrix_id}: {value!r} is not a device port of this room" for value in ports
                             if not (_number(value) and value in room_ports)]
                if len(set(map(repr, ports))) != len(ports):
                    problems.append(f"{at}.{matrix_id}: lists a port twice")
    return problems


def _edid_names(routing: dict, devices: dict, hdmi_ok: bool, problems: list[str]) -> dict | None:
    """The HDMI matrix's EDID names, checking routing.edid (None if unknown)."""
    edid = routing.get("edid")
    if edid is None:
        return None
    module = switcher(devices[routing["hdmi_matrix"]]) if hdmi_ok else None
    names = None
    if module is not None and not all(hasattr(module, name) for name in ("EDID", "edid_item", "copy_edid")):
        problems.append(f"routing.edid: {module.NAME} cannot set an input's EDID")
    elif module is not None:
        names = module.EDID
    if not isinstance(edid, dict):
        problems.append("routing.edid: must be a mapping, such as {idle: <EDID name>} or {}")
        return names
    if "idle" in edid and names is not None and not (isinstance(edid["idle"], str) and edid["idle"] in names):
        problems.append(f"routing.edid.idle: {edid['idle']!r} is not an EDID name of {routing['hdmi_matrix']}")
    for key in sorted(set(edid) - EDID_KEYS):
        problems.append(f"routing.edid: unknown key '{key}'")
    return names


def _port_item(devices: dict, matrix_id: str, port: int) -> str:
    device = devices[matrix_id]
    return switcher(device).switch_item(device, port)


def switch_module(devices: dict, switch: dict) -> ModuleType:
    """The integration that switches a room's hdmi_switch: the HDMI
    switch's, or that of the matrix whose output it is."""
    device = devices[switch["device"]]
    return switcher(device) if "output" in switch else hdmi_switcher(device)


def _switch_item(devices: dict, switch: dict) -> str:
    device = devices[switch["device"]]
    module = switch_module(devices, switch)
    return module.switch_item(device, switch["output"]) if "output" in switch else module.input_item(device)


def commands(data: dict, room: str, choice: str) -> list[list]:
    """[[Item, value], ...] that put `choice` (a source id or OFF) on in
    `room`'s matrices: its HDMI output first (a room without one has
    none), then its HDMI switch if it has one, then each of its USB device
    ports. A source cabled to the room alone (its HDMI switch, receiver or
    display) turns the room's HDMI output off; any other choice puts the
    switch on the matrix, but Off leaves a switch the matrix does not reach
    (no matrix_input) as it is. A source with no host on a room's USB
    matrix parks those ports, and a source under the room's usb_only moves
    only the ports listed there: the room's others stay where they are."""
    routing = data["routing"]
    devices = {d["id"]: d for d in data["devices"]}
    entry = routing["rooms"][room]
    source = routing["sources"].get(choice, {}) if choice != OFF else {}
    switch = entry.get("hdmi_switch")
    local = switch is not None and choice in switch["inputs"]
    hdmi_input = routing["hdmi_off_input"] if choice in _local(entry) else source.get("hdmi_input",
                                                                                         routing["hdmi_off_input"])
    out = []
    for key in ("hdmi_output", "hdmi_audio_output"):
        if key in entry:
            out.append([_port_item(devices, routing["hdmi_matrix"], entry[key]), hdmi_input])
    if switch is not None and (local or "matrix_input" in switch):
        out.append([_switch_item(devices, switch), switch["inputs"][choice] if local else switch["matrix_input"]])
    only = (entry.get("usb_only") or {}).get(choice)
    for matrix_id, ports in (entry.get("usb") or {}).items():
        host = usb_host(source, matrix_id, routing["usb_off_host"])
        moved = ports if only is None else only.get(matrix_id, [])
        out += [[_port_item(devices, matrix_id, port), host] for port in moved]
    return out


def stages(data: dict, room: str) -> list[tuple[dict, dict]]:
    """(spec, stage()) of the room's receiver, then its display; one whose
    device lists no integration that powers it (validate() reports it) is
    left out. A passive display is always skipped, including when a device
    gains a provider later; enabling routing requires removing passive."""
    devices = {d["id"]: d for d in data["devices"]}
    entry = data["routing"]["rooms"][room]
    out = []
    for key in STAGES:
        if key in entry and entry[key].get("passive") is True:
            continue
        module = stager(devices[entry[key]["device"]]) if key in entry else None
        if module is not None:
            out.append((entry[key], module.stage(devices[entry[key]["device"]], entry[key].get("zone"))))
    return out


def wake(data: dict, choice: str) -> list[list]:
    """[[Item, "ON"]] that wakes the chosen source, when its integration
    can (an Apple TV, a SHIELD); none for Off. Dedicated-source standby is
    explicitly configured with sleep_sources and sent by after()."""
    device = next((d for d in data["devices"] if d["id"] == choice), None) if choice != OFF else None
    module = waker(device) if device is not None else None
    return [[module.wake_item(device), "ON"]] if module is not None else []


def power(data: dict, room: str, choice: str) -> list[list]:
    """[[Item, "ON"], ...] a choice sends before it routes: the display's
    power, then the receiver's, which take longest, then the source's own
    (wake()); none for Off."""
    if choice == OFF:
        return []
    return [[stage["power"], "ON"] for _, stage in reversed(stages(data, room))] + wake(data, choice)


def inputs(data: dict, room: str, choice: str) -> list[list]:
    """[[Item, input], ...] that put a choice on the room's screen once it
    is routed: the display's input, then the receiver's, as far as the
    choice passes through each. A source cabled to the display leaves the
    receiver's input alone; none for Off."""
    if choice == OFF:
        return []
    out = []
    for spec, stage in reversed(stages(data, room)):
        cabled = spec.get("sources") or {}
        if choice in cabled:
            out.append([stage["input"], cabled[choice]])
            break
        out.append([stage["input"], spec.get("input_by_source", {}).get(choice, spec["input"])])
    return out


def after(data: dict, room: str, choice: str) -> list[list]:
    """[[Item, value], ...] sent last: for Off, explicitly selected dedicated
    sources sleep before the receiver and display; then the room's actions."""
    devices = {d["id"]: d for d in data["devices"]}
    entry = data["routing"]["rooms"][room]
    out = ([[item, "OFF"] for source in entry.get("sleep_sources", []) for item, _ in wake(data, source)]
           + [[stage["power"], "OFF"] for _, stage in stages(data, room)]) if choice == OFF else []
    actions = entry.get(ACTIONS[1] if choice == OFF else ACTIONS[0]) or {}
    return out + [[action_item(devices, device_id), action] for device_id, action in actions.items()]


def edid_table(data: dict) -> dict | None:
    """What routing.js needs to follow the displays' EDIDs, or None without
    routing.edid: the EDID Item of each source's input, the EDID code each
    room's HDMI output gives its sources (a copy of its display unless the
    room names one), and the code of an input no room shows (None: left)."""
    routing = data["routing"]
    edid = routing.get("edid")
    if edid is None:
        return None
    device = next(d for d in data["devices"] if d["id"] == routing["hdmi_matrix"])
    module = switcher(device)
    rooms = routing["rooms"].values()
    return {
        "inputs": {str(s["hdmi_input"]): module.edid_item(device, s["hdmi_input"])
                   for s in routing["sources"].values() if "hdmi_input" in s},
        "outputs": {module.switch_item(device, e["hdmi_output"]):
                    module.EDID[e.get("edid", module.copy_edid(e["hdmi_output"]))] for e in rooms
                    if "hdmi_output" in e},
        "idle": module.EDID[edid["idle"]] if "idle" in edid else None,
    }


def table(data: dict) -> dict:
    """What routing.js needs: by room id, the EDID table, and the Source
    state of a room that is off. A room's stages are its display, then its
    receiver: the order the Source is read back in."""
    routing = data["routing"]
    devices = {d["id"]: d for d in data["devices"]}
    rooms = {}
    for room, entry in routing["rooms"].items():
        offered = list(entry["sources"])
        fed = "hdmi_output" in entry
        readback = {str(routing["hdmi_off_input"]): OFF} if fed else {}
        readback.update({str(routing["sources"][s]["hdmi_input"]): s for s in offered
                         if fed and "hdmi_input" in routing["sources"][s]})
        switch = entry.get("hdmi_switch")
        choices = [OFF, *offered]
        rooms[room] = {
            "item": item_name(room),
            "label": room_label(data, room),
            "hdmi": _port_item(devices, routing["hdmi_matrix"], entry["hdmi_output"]) if fed else None,
            "choices": {choice: commands(data, room, choice) for choice in choices},
            "power": {choice: power(data, room, choice) for choice in choices},
            "inputs": {choice: inputs(data, room, choice) for choice in choices},
            "after": {choice: after(data, room, choice) for choice in choices},
            "readback": readback,
            "switch": None if switch is None else {
                "item": _switch_item(devices, switch),
                "matrix": str(switch["matrix_input"]) if "matrix_input" in switch else None,
                "readback": {str(number): source for source, number in switch["inputs"].items()},
            },
            "stages": [{"power": stage["power"], "input": stage["input"], "feed": spec.get("input"),
                        "readback": {value: source for source, value in (spec.get("sources") or {}).items()}}
                       for spec, stage in reversed(stages(data, room))],
        }
        if "hdmi_audio_output" in entry:
            rooms[room]["audioHdmi"] = _port_item(devices, routing["hdmi_matrix"], entry["hdmi_audio_output"])
        for raw_stage, (spec, _) in zip(rooms[room]["stages"], reversed(stages(data, room))):
            if spec.get("input_by_source"):
                raw_stage["bySource"] = dict(spec["input_by_source"])
        checks = {}
        raw = rooms[room]
        for item, action in ([(raw[key], "read matrix output input") for key in ("hdmi", "audioHdmi") if raw.get(key)]) + (
                [(raw["switch"]["item"], "read selected input")] if raw["switch"] else []) + [
                (stage[field], action) for stage in raw["stages"]
                for field, action in (("power", "read power"), ("input", "read selected input"))]:
            ident = next((ident for ident in sorted(devices, key=len, reverse=True) if item.startswith(ident + "_")), None)
            if ident:
                device = devices[ident]
                checks[item] = {"device": ident, "label": device.get("name") or device.get("model") or ident,
                                "model": device.get("model") or "", "action": action}
        raw["diagnosis"] = {"item": problem_item_name(room), "checks": checks}
    return {"rooms": rooms, "edid": edid_table(data), "off": OFF}


def options(data: dict, room: str) -> str:
    sources = data["routing"]["sources"]
    offered = data["routing"]["rooms"][room]["sources"]
    return options_text([(OFF, "Off")] + [(s, sources[s]["label"]) for s in offered])


def source_item(data: dict, room: str) -> dict:
    choices = options(data, room)
    # A Location with two screens (the Office's desk and TV) names each.
    shared = sum(home(data, other) == home(data, room) for other in data["routing"]["rooms"]) > 1
    return {
        "type": "String",
        "label": f"{room_label(data, room)} source" if shared else "Source",
        "icon": "video",
        "groups": [f"loc_{home(data, room)}"],
        "tags": ["Control"],
        "autoupdate": False,
        "metadata": {
            "stateDescription": {"config": {"options": choices}},
            "commandDescription": {"config": {"options": choices}},
        },
    }


def _label(conf: Conf, name: str, labels: dict[int, str]) -> None:
    """Show a port Item's values as what is cabled to each."""
    item = conf.item(name)
    if item is not None:
        text = options_text(sorted(labels.items()))
        item.setdefault("metadata", {})["stateDescription"] = {"config": {"options": text}}


def _label_ports(conf: Conf, device: dict, labels: dict[int, str]) -> None:
    """Show each port Item of a matrix as the source cabled to its value."""
    module = switcher(device)
    for port in range(1, module.ports(device)[1] + 1):
        _label(conf, module.switch_item(device, port), labels)


def rule(data: dict) -> str:
    source = SOURCE.read_text(encoding="utf-8")
    if source.count(MARK) != 1:
        raise ValueError(f"{SOURCE} must contain the line {MARK!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[3])
    header = (f"// Generated by bin/thermite generate from {origin} and the routing in\n"
              "// instance/devices.yaml; do not edit.\n")
    filled = "const TABLE = " + json.dumps(table(data), indent=1) + ";"
    return header + source.replace(MARK, filled)


def render(house: House, conf: Conf) -> None:
    data = house.data
    if data.get("routing") is None:
        return
    routing = data["routing"]
    devices = {d["id"]: d for d in house.devices}
    out = conf.yaml_file(ITEMS, HEADER)
    for room in routing["rooms"]:
        out.items[item_name(room)] = source_item(data, room)
        out.items[problem_item_name(room)] = {
            "type": "String", "label": "Source readback problem", "groups": [f"loc_{home(data, room)}"],
            "tags": ["Status"], "autoupdate": False,
            "metadata": {"stateDescription": {"config": {"readOnly": True}}},
        }
    sources = routing["sources"].values()
    _label_ports(conf, devices[routing["hdmi_matrix"]],
                 {routing["hdmi_off_input"]: "Off",
                  **{s["hdmi_input"]: s["label"] for s in sources if "hdmi_input" in s}})
    for entry in routing["rooms"].values():
        if "hdmi_switch" in entry:
            switch = entry["hdmi_switch"]
            labels = {switch["matrix_input"]: "HDMI matrix"} if "matrix_input" in switch else {}
            labels.update({n: routing["sources"][s]["label"] for s, n in switch["inputs"].items()})
            _label(conf, _switch_item(devices, switch), labels)
    off_host = routing["usb_off_host"]
    for matrix_id in dict.fromkeys(m for e in routing["rooms"].values() for m in e.get("usb") or {}):
        labels = {off_host: "None"}
        labels.update({usb_host(s, matrix_id, off_host): s["label"] for s in sources
                       if usb_host(s, matrix_id, off_host) != off_host})
        _label_ports(conf, devices[matrix_id], labels)
    conf.files[RULE] = rule(data)
    conf.add_addon(Addon("automation", "jsscripting", "the room Source rule (routing.js)"))
