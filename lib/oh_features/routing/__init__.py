"""Room sources over the HDMI and USB matrices. routing.js beside this file
describes the rendered behaviour; lifecycle_io.js describes ordered
execution, quarantine, recovery and journals.

instance/devices.yaml `routing` says which HDMI matrix input and USB host
each source is cabled to, which HDMI output and USB device ports each room
has (and, under `usb_only`, a source that moves only some of those ports),
which rooms have an HDMI switch in front of their display (the matrix
on one of its inputs, sources cabled to the room alone on others; or one
output of a matrix of the room's own, as the example's AVPro), each
room's receiver and display (`receiver`, `display`: the device, the input
the signal before it arrives on, and sources cabled to it alone). A display
may instead be `{device: ..., passive: true}`: a configured endpoint with no
control integration, guessed input, zone, power command or readback. What else
a source or Off does there (`on_actions`, `off_actions`: a curtain and
lights, say), which dedicated sources explicitly enter standby on Off
(`sleep_sources`), and, under
`edid`, whether a source's input follows the EDID of the room display it is
turned on in. A room can name an EDID for an individual matrix source in
`edid_by_source`. That EDID takes precedence while the room uses its own
matrix feed, including when another room selects the same source.
A room whose sound leaves the matrix on an output of its own
(`hdmi_audio_output`) may name, by source, what that output's scaler does
while the room shows the source (`hdmi_audio_scaler`: audio-only for a
computer whose picture its receiver need not convert); every other choice,
Off included, gives the output the matrix's fixed scaler
(`settings.scaler`) back. A room without an `hdmi_output` offers only sources cabled
to it, and one whose id is not a room's names its room (`room`), as the
example beside the Office's desk screen does. A house with no HDMI matrix
leaves out `hdmi_matrix` and `hdmi_off_input`, and one with no USB matrix
`usb_off_host`: each room then offers only the sources cabled to it.

A room's Volume and Mute on the remote drive its `audio` device when it
names one: a soundbar or streaming amplifier in a room it covers
(covered_rooms(): its own, its display's and, under a Building, every room
nested in those), or its own receiver or display. Without `audio` they drive
its receiver, unless that is on a zone of its own (a receiver's volume is its
main zone's), else its display: the first of those with a volume thermite
drives, or nothing. An optional `audio_link` records the physical branch to
that device: `from: {device, port}` and its `transport`. Without it, knowing
the volume owner does not establish a cable or its upstream attachment.

Rooms may take different zones of one receiver (`zone`). A source cabled to
the receiver is on one of its inputs, which each zone can select, so any of
those rooms may offer it by listing it under its receiver's `sources` on that
input: the rooms then share it, as they share a matrix source. Each room
switches and powers only its own zone, Off included, so neither takes the
other's picture away; the source is never one room's dedicated source
(`sleep_sources`), and under `idle: standby` the lifecycle planner idles it
only once no room uses it. It is never synced (below). Across the rooms on
one device, such a source is on one input, and that input carries it alone:
no other source, and not the signal another room's zone takes from before it.

A receiver may name `sync_with: <room>`, its leader: the room whose
receiver is another zone of the same device. While both rooms show the same
source from the HDMI matrix (one both offer and neither has cabled to it
alone), this room's zone takes the leader's input for that source (the
leader receiver's `input`, or its `input_by_source`), so both screens show
one picture. Both rooms need an `hdmi_output` and neither an
`hdmi_audio_output`; the leader needs no `hdmi_switch` and no `sync_with` of
its own. A synced route is the room's own for that source with its zone on
the leader's input: its own matrix output stays on the source, so going
back is only an input change. Under `edid` a synced route claims no EDID:
the source keeps the leader's, and gets the follower's only once the sync
ends while the follower stays on it. Off is never synced. routing.js moves a
follower off the leader's output before the leader's next choice is
claimed (break before), and onto it only once the leader chose the same
source and its output shows it (make after); a follower turned off or
switched elsewhere at its own devices is left alone. Under the lifecycle
planner a synced route leases the leader's output at its source, the
reference count: the leader cannot move it while a follower is synced, and
it is parked only once the leader released it and neither room holds it.
The follower never writes it.

This feature validates the routing and renders:

- one Source selector Item per room, `loc_<room>_source` (Main UI's Overview
  lists them, oh_features.overview);
- conf/automation/js/routing.js: routing.js beside this file with TABLE
  filled in. For every room and choice TABLE lists the port Items to
  command (the HDMI output first, then the room's HDMI switch, then its USB
  device ports), the power and input Items of its receiver and display, the
  power Item that wakes the source itself and the actions a choice sends,
  and which choice each input on the room's display, receiver, HDMI switch
  and HDMI output means; under `edid`, the EDID Item of each source's
  input and the EDID code each room's output gives its sources; for a room
  whose receiver has `sync_with`, the synced route of each such source
  (`sync`), which its leader lists under `followers`. The rule
  only commands; the Source state is always read back from the room's
  devices and the HDMI matrix.

The matrices are any devices whose integration can switch (`ports()` and
`switch_item()`, as bk808_http and ukm404_http provide), the HDMI switches
any devices whose integration has `INPUTS` and `input_item()`, as
vrroom_telnet does, receivers and displays any whose integration has
`stage()`, as denonmarantz_http and sony_adcp do, EDIDs need an HDMI
matrix integration with `EDID`, `edid_item()` and `copy_edid()`, and
scalers one with `SCALER` and `scaler_item()`, so this module knows rooms
and sources, not protocols. A matrix whose integration has `hotplug_item()`
(oh_integrations._hotplug) also says whether a display is cabled to the
output a room names, its HDMI output or its HDMI switch: while it keeps
saying none with the room's devices on, the room's Source is unknown and its
readback problem names that output, instead of believing the route.

A matrix or HDMI switch that misses one read does not make a room's Source
unknown: routing.js holds the input the room's output or switch Item last
read for up to MISSED of its integration's polls (`POLL_S`), until the next
read or a second one that finds nothing (TABLE `reads`), never for a
choice made since that read. Only the Source and its readback problem wait;
the planner and the EDIDs see the Item as it is.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import oh_integrations as integrations
import oh_inventory as inventory
from oh_integrations import _hotplug
from oh_conf import Addon, Conf, House, fill, indented, options_text, uses
from oh_shape import LIST, MAPPING, NUMBER, TEXT, TRUTH, Key, unknown

NAME = "routing"
ITEMS = "yaml/routing.yaml"
RULE = "automation/js/routing.js"
SOURCE = Path(__file__).resolve().parent / "routing.js"
HEADER = (
    "One Source selector per room, from the inventory's routing.",
    "conf/automation/js/routing.js turns a command into",
    "commands to the matrices and the room's receiver and display; the state",
    "is read back from those, so autoupdate is off and the selector never",
    "runs ahead of them.",
)
#: The Source command and state of a room with nothing routed to it.
OFF = "off"
#: Routing telemetry: JSON counts routing.js posts (its METRICS_ITEM).
METRICS_ITEM = "house_routing_metrics"
HOUSE_ITEMS = (METRICS_ITEM,)
#: A room's devices that a choice powers and switches, in the order the
#: signal reaches them: the screen is the last.
STAGES = ("receiver", "display")
#: Kinds of network player that are a room's sound where they stand, as a
#: soundbar is (targets()).
PLAYERS = ("streaming_amp", "audio_streamer")
#: The room keys of what its other devices are sent on any source, and on
#: Off. Not `on` and `off`: YAML reads those as true and false.
ACTIONS = ("on_actions", "off_actions")
#: What a source's signal carries to a room (its `carries`): a picture
#: alone, sound alone, or both, as for a source it leaves out.
CARRIES = ("display", "audio", "both")
SIGNAL_TRANSPORTS = ("hdmi", "optical", "coaxial", "analog", "arc", "earc")

#: The shape of `routing`, declared (oh_shape): the checks of its key names
#: below, and its part of the house-file reference page and schema, come
#: from here. What each value must be beyond its kind (a port the matrix
#: has, a device that can switch) is validate()'s, in its own words.
_INPUT = Key(TEXT, "an input of the device, by the name its integration gives it")
#: A room's receiver or display.
STAGE = {
    "device": Key(TEXT, "its device id", required=True),
    "zone": Key(NUMBER, "the receiver's zone this room listens to: 2 or more, and left out for the main zone"),
    "input": Key(TEXT, "the input the signal before it arrives on: from the HDMI matrix, the room's switch or its receiver"),
    "input_by_source": Key(MAPPING, "another input for some of the sources that reach it that way", by="source", each=_INPUT),
    "sources": Key(MAPPING, "the sources cabled to it alone, each on its input", by="source", each=_INPUT),
    "sync_with": Key(TEXT, "the room whose receiver is another zone of this one: while both show the same matrix source, "
                           "this zone takes that room's input, so both screens show one picture"),
}
#: A display may instead be passive, and then has only its device.
DISPLAY = {**STAGE, "passive": Key(TRUTH, "whether thermite leaves the display alone: true for one it sends nothing "
                                          "to and reads nothing from, which then has only its device here")}
_ACTIONS = {name: Key(MAPPING, what, by="device id", each=Key(TEXT, "the name of one of the device's actions, in quotes"))
            for name, what in zip(ACTIONS, ("what other devices are sent when the room turns to any source: a curtain "
                                            "and lights, say", "what they are sent when it turns Off"), strict=True)}
ROOM = {
    "room": Key(TEXT, "the room it is in, for a routed room that is not a room of its own (a desk's screen in an office)"),
    "label": Key(TEXT, "its name on the remote, where that is not its room's"),
    "hdmi_output": Key(NUMBER, "the HDMI matrix output its screen is on; without one it offers only sources cabled to it"),
    "hdmi_audio_output": Key(NUMBER, "a second matrix output, to its receiver, for a room whose picture and sound "
                                     "leave the matrix apart"),
    "hdmi_audio_scaler": Key(MAPPING, "what the scaler of its hdmi_audio_output does while it shows some of its "
                                      "sources from the HDMI matrix (audio-only for a computer whose picture its "
                                      "receiver need not convert); any other choice, Off included, gives the output "
                                      "the matrix's settings.scaler back", by="source",
                             each=Key(TEXT, "one of the HDMI matrix's scaler names")),
    "hdmi_switch": Key(MAPPING, "the HDMI switch in front of its display, or one output of a matrix of its own", keys={
        "device": Key(TEXT, "the switch's device id", required=True),
        "output": Key(NUMBER, "which output of a matrix of its own the room is on"),
        "matrix_input": Key(NUMBER, "the switch input the house's HDMI matrix arrives on"),
        "inputs": Key(MAPPING, "the sources cabled to the switch, each on its input", by="source",
                      each=Key(NUMBER, "an input of the switch"))}),
    "usb": Key(MAPPING, "its USB device ports (keyboard, camera), by USB matrix", by="device id of a USB matrix",
               each=Key(LIST, "the matrix's device ports that are this room's", each=Key(NUMBER, "a device port"))),
    "usb_only": Key(MAPPING, "sources that move only some of those ports", by="source", each=Key(
        MAPPING, "the ports that source moves, by USB matrix", by="device id of a USB matrix",
        each=Key(LIST, "device ports", each=Key(NUMBER, "a device port")))),
    "usb_unused": Key(TEXT, "what becomes of the ports a source does not move: preserve leaves them where they "
                            "are (as when left out), park moves them to usb_off_host", choices=("preserve", "park")),
    "sources": Key(LIST, "the sources it offers, in the order its selector shows them", required=True,
                   each=Key(TEXT, "a source: a key of routing.sources")),
    "sleep_sources": Key(LIST, "sources of this room alone that are put in standby when it turns Off",
                         each=Key(TEXT, "a source")),
    "edid": Key(TEXT, "the EDID its output gives the sources shown on it, under routing.edid: one of the matrix's "
                      "EDID names"),
    "edid_by_source": Key(MAPPING, "the EDID each named matrix source presents while this room uses its own feed, "
                                   "even when another room selects the same source", by="source",
                          each=Key(TEXT, "one of the HDMI matrix's EDID names")),
    "audio": Key(TEXT, "the device its Volume and Mute drive: a soundbar or streaming amplifier in a room it "
                       "covers, or its own receiver or display"),
    "carries": Key(MAPPING, "what a source's signal carries to it where that is not both picture and sound: "
                            "display for a picture alone (a computer whose sound stays with it), audio for sound "
                            "alone; a display source's path leaves out the room's audio device, which plays none of it",
                   by="source", each=Key(TEXT, "what the source carries", choices=CARRIES)),
    "audio_input": Key(TEXT, "the input of its audio device that its route feeds (optical, for a streaming "
                             "amplifier on its HDMI switch's audio out); without it, any sound that device plays "
                             "with no track named is taken to come from the route"),
    "audio_link": Key(MAPPING, "the known physical audio branch to its audio device, independent of volume control", keys={
        "from": Key(MAPPING, "the device and output connector feeding the audio device", required=True, keys={
            "device": Key(TEXT, "the upstream device id", required=True),
            "port": Key((TEXT, NUMBER), "the native output connector name or number", required=True)}),
        "transport": Key(TEXT, "the cable's audio transport", required=True, choices=SIGNAL_TRANSPORTS)}),
    "receiver": Key(MAPPING, "its receiver", keys=STAGE),
    "display": Key(MAPPING, "its display", keys=DISPLAY),
    **_ACTIONS,
}
SOURCE_KEYS = {
    "label": Key(TEXT, "its name on the rooms' Source selectors", required=True),
    "hdmi_input": Key(NUMBER, "the HDMI matrix input it is cabled to; a source without one is cabled to a room"),
    "usb_host": Key((NUMBER, MAPPING), "the USB matrix host port its computer is on, or one for each USB matrix",
                    by="device id of a USB matrix", each=Key(NUMBER, "a host port")),
    "idle": Key(TEXT, "what becomes of it when no room shows it: preserve leaves it on (as when left out), "
                      "standby puts it in standby", choices=("preserve", "standby")),
}
HOUSE_KEYS = {"routing": Key(MAPPING, "which source each room can show, and how it gets there", must="a mapping", empty=True, keys={
    "hdmi_matrix": Key(TEXT, "the device id of the HDMI matrix the rooms share; a house without one leaves it "
                             "and hdmi_off_input out"),
    "hdmi_off_input": Key(NUMBER, "the matrix input a room's output is turned to for Off: one with nothing on it"),
    "usb_off_host": Key(NUMBER, "the USB matrix host port a room's device ports are parked on: one with no computer on it"),
    "edid": Key(MAPPING, "each source's matrix input presents the EDID of the display it is turned on in; {} for that alone",
                keys={"idle": Key(TEXT, "the EDID an input presents while no room shows its source: one of the "
                                        "matrix's EDID names")}),
    "sources": Key(MAPPING, "the sources, by the device id of each", required=True, by="device id",
                   each=Key(MAPPING, "a source", keys=SOURCE_KEYS)),
    "rooms": Key(MAPPING, "the routed rooms, by room id (or a name of its own, with room)", required=True,
                 by="room id", each=Key(MAPPING, "a routed room", keys=ROOM)),
})}


def item_name(room: str) -> str:
    return f"loc_{room}_source"


def problem_item_name(room: str) -> str:
    return f"loc_{room}_source_problem"


def home(house: House, room: str) -> str:
    """The inventory room a routed room is in: its `room`, else its id."""
    return house.data["routing"]["rooms"][room].get("room", room)


def covered_rooms(house: House, room: str) -> set:
    """The inventory rooms a routed room covers: its own (`home`), its
    display's and, when its own is a Building, every room nested in those
    (rooms.<id>.in), however deep. This is inventory containment, not an
    inference from names: the site contract lets the routed room command
    their devices, and the remote lists them as the room's."""
    rooms = house.rooms
    devices = house.by_id
    entry = house.data["routing"]["rooms"][room]
    covered = {home(house, room)}
    display = entry.get("display") if isinstance(entry.get("display"), dict) else {}
    shown = _known(devices, display.get("device"))
    if shown is not None:
        covered.add(shown.get("room"))
    if (rooms.get(home(house, room)) or {}).get("tag") == "Building":
        while additions := {ident for ident, spec in rooms.items()
                            if isinstance(spec, dict) and spec.get("in") in covered} - covered:
            covered |= additions
    return covered


def targets(house: House, room: str) -> dict[str, str]:
    """The devices a routed room's remote commands, each with its role:
    "own" for the room's own sound: its `audio`, a soundbar in its home
    room (a streaming amp or audio streamer too, unless the routed room
    is a screen in a larger room), and a TV or projector there that no
    other routed room shows; "shared" for a source it shows, a stage that
    is not passive, which other rooms may use too (a receiver's other
    zone), and such a soundbar or amp in a room it covers only as a
    Building. A room locked
    refuses every command to what it owns, from anywhere; a command to what
    it shares only when the command would disturb its route
    (oh_identity.runtime)."""
    routed = house.data["routing"]["rooms"]
    entry = routed[room]
    devices = house.devices
    shown_by: dict = {}
    for other, spec in routed.items():
        shown_by.setdefault((spec.get("display") or {}).get("device"), set()).add(other)
    covered = covered_rooms(house, room)
    shared = set(entry["sources"])
    shared.update(spec.get("device") for stage in STAGES if (spec := entry.get(stage)) and not spec.get("passive"))
    # Only endpoint audio devices join the explicit roles: an aliased TV
    # screen does not gain the desk amplifier beside it.
    sound = {d["id"]: d["room"] for d in devices if d.get("room") in covered
             and (d.get("kind") == "soundbar" or d.get("kind") in PLAYERS and "room" not in entry)}
    shared.update(ident for ident, place in sound.items() if place != home(house, room))
    own = {entry.get("audio")} | {ident for ident, place in sound.items() if place == home(house, room)}
    # An inventory can have a controlled TV in a room whose route is audio only.
    own.update(d["id"] for d in devices if d.get("room") == home(house, room) and d.get("kind") in {"tv", "projector"}
               and not shown_by.get(d["id"], {room}) - {room})
    roles = {**{ident: "shared" for ident in shared}, **{ident: "own" for ident in own}}
    roles.pop(None, None)
    if (entry.get("display") or {}).get("passive"):
        roles.pop(entry["display"].get("device"), None)
    return roles


def guard_rooms(house: House, device_id: str) -> dict[str, str]:
    """The routed rooms whose remotes command a device, each with the
    device's role there (targets())."""
    rooms = (house.data.get("routing") or {}).get("rooms") or {}
    return {room: role for room in rooms if (role := targets(house, room).get(device_id))}


def carried(entry: dict, source: str) -> str:
    """What a source's signal carries to a routed room: its `carries`,
    else both."""
    value = (entry.get("carries") or {}).get(source) if isinstance(entry.get("carries"), dict) else None
    return value if value in CARRIES else "both"


def room_label(house: House, room: str) -> str:
    """A routed room's name on its Source selector and the remote: its own
    label where its screen is not the whole room, else the room's."""
    return house.data["routing"]["rooms"][room].get("label") or inventory.room_label(house.data, room)


def _number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _known(table: dict, name: object) -> dict | None:
    """table[name] for a name straight from the house file, None when it is
    not one of the table's: a list or a mapping written where an id belongs
    names nothing, and would make table.get(name) raise."""
    return table.get(name) if isinstance(name, str) else None


def switcher(device: dict) -> ModuleType | None:
    """The integration that switches this matrix, if it lists one."""
    return next((m for m in integrations.defining("switch_item") if uses(device, m.PHASE, m.NAME)), None)


def switchers() -> list[str]:
    return [m.NAME for m in integrations.defining("switch_item")]


def hdmi_switcher(device: dict) -> ModuleType | None:
    """The integration that switches this HDMI switch, if it lists one."""
    return next((m for m in integrations.defining("input_item") if uses(device, m.PHASE, m.NAME)), None)


def hdmi_switchers() -> list[str]:
    return [m.NAME for m in integrations.defining("input_item")]


def stager(device: dict) -> ModuleType | None:
    """The integration that powers and switches this receiver or display,
    if the device lists it under control."""
    return next((m for m in integrations.defining("stage") if uses(device, "control", m.NAME)), None)


def stagers() -> list[str]:
    return [m.NAME for m in integrations.defining("stage")]


def waker(device: dict) -> ModuleType | None:
    """The integration that wakes this source (wake_item()), if the device
    lists it under control."""
    return next((m for m in integrations.defining("wake_item") if uses(device, "control", m.NAME)), None)


def action_item(devices: dict, device_id: str) -> str:
    """The String Item an action is sent to: <id>_action, the name the
    device's integration is to give it. No integration renders one yet, and
    routing.js skips an Item that is not there."""
    return f"{device_id}_action"


def readback_inputs(devices) -> list[str]:
    """Every input value the devices' integrations read back
    (READBACK_INPUTS): what a room's route diagnosis may report."""
    return list(dict.fromkeys(value for device in devices for module in integrations.REGISTRY
                              if uses(device, module.PHASE, module.NAME) for value in module.READBACK_INPUTS))


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
        device = _known(devices, device_id)
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

    # A house with no HDMI matrix leaves out hdmi_matrix and hdmi_off_input;
    # a source's hdmi_input, a room's hdmi_output or routing.edid needs both.
    hdmi = None
    if _on_matrix(routing):
        hdmi = matrix("routing.hdmi_matrix", routing.get("hdmi_matrix"), "hdmi_matrix")
        port("routing.hdmi_off_input", routing.get("hdmi_off_input"), hdmi[0] if hdmi else None)
    hdmi_inputs, hdmi_outputs = hdmi or (None, None)

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
    if usb_sizes or off_host is not None:
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
        if "idle" in entry:
            idle = entry["idle"]
            if not isinstance(idle, str) or idle not in {"preserve", "standby"}:
                problems.append(f"{where}.idle: must be preserve or standby")
            elif idle == "standby":
                from .lifecycle_io import capabilities
                device = devices.get(source)
                module = waker(device) if device is not None else None
                cap = capabilities(devices.values()).get(module.wake_item(device)) if module else None
                if not cap or not cap.get("terminal") or not cap.get("probe"):
                    problems.append(f"{where}.idle: standby requires controlled absolute power, ordered execution and fresh readback")
                if any(source in (room.get("sleep_sources") or []) for room in rooms_section.values()
                       if isinstance(room, dict) and isinstance(room.get("sleep_sources", []), list)):
                    problems.append(f"{where}.idle: remove this source from sleep_sources before enabling standby")
        for key in sorted(unknown(SOURCE_KEYS, entry)):
            problems.append(f"{where}: unknown key '{key}'")

    outputs: dict[object, str] = {}
    usb_ports: dict[tuple[str, object], str] = {}
    switch_rooms: dict[object, str] = {}
    staged: dict[tuple, str] = {}
    source_edids: dict[str, tuple[str, str]] = {}
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
        if "usb_unused" in entry:
            policy = entry["usb_unused"]
            if not isinstance(policy, str) or policy not in {"preserve", "park"}:
                problems.append(f"{where}.usb_unused: must be preserve or park")
            if not usb:
                problems.append(f"{where}.usb_unused: requires managed USB ports")
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
            if not isinstance(source, str) or source not in sources:
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
        if "hdmi_audio_scaler" in entry:
            problems += _audio_scaler_problems(f"{where}.hdmi_audio_scaler", entry, offered, sources, routing,
                                               devices, hdmi is not None)
        if "edid_by_source" in entry:
            at = f"{where}.edid_by_source"
            overrides = entry["edid_by_source"]
            if not fed:
                problems.append(f"{at}: needs hdmi_output")
            if routing.get("edid") is None:
                problems.append(f"{at}: needs routing.edid")
            if not isinstance(overrides, dict) or not overrides:
                problems.append(f"{at}: must map sources to EDID names")
            else:
                for source, name in overrides.items():
                    definition = sources.get(source)
                    if (source not in offered or source in local or not isinstance(definition, dict)
                            or "hdmi_input" not in definition):
                        problems.append(f"{at}.{source}: '{source}' is not an offered HDMI matrix source")
                        continue
                    if edid_names is None:
                        continue  # routing.edid or the matrix already has a problem
                    if not isinstance(name, str) or name not in edid_names:
                        problems.append(f"{at}.{source}: {name!r} is not an EDID name of {routing['hdmi_matrix']}")
                        continue
                    other, previous = source_edids.setdefault(source, (room, name))
                    if edid_names[previous] != edid_names[name]:
                        problems.append(f"{at}.{source}: {name!r} conflicts with {previous!r} in room {other}; "
                                        "a matrix input presents one EDID to every viewer")
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
        if "audio" in entry:
            problems += _audio_problems(where, data, room, devices)
        if "carries" in entry:
            problems += _carries_problems(f"{where}.carries", entry["carries"], offered)
        if "audio_input" in entry:
            problems += _audio_input_problems(where, entry, devices)
        if "audio_link" in entry:
            problems += _audio_link_problems(where, entry, devices, data, room)
        for key in sorted(unknown(ROOM, entry)):
            problems.append(f"{where}: unknown key '{key}'")
    problems += _wiring_problems(rooms_section)
    problems += _sync_problems(rooms_section, sources, devices)

    if routing.get("edid") is not None and hdmi is not None:
        fixed = (devices[routing["hdmi_matrix"]].get("settings") or {}).get("edid") or {}
        if isinstance(fixed, dict):
            for number, source in sorted(inputs.items()):
                key = number if number in fixed else "default" if "default" in fixed else None
                if key is not None:
                    problems.append(f"{routing['hdmi_matrix']}.settings.edid.{key}: routing.edid sets the "
                                    f"EDID of input {number} ({source}); fix only the inputs no source uses")
    for key in sorted(unknown(HOUSE_KEYS["routing"].keys, routing)):
        problems.append(f"routing: unknown key '{key}'")
    if not problems and any(source.get("idle") == "standby" for source in sources.values()):
        contract = table(House.unchecked(data))["lifecycle"]
        for source, coverage in contract.get("coverage", {}).items():
            if sources[source].get("idle") == "standby" and not coverage["complete"]:
                problems.append(f"routing.sources.{source}.idle: standby requires complete observable consumer coverage; "
                                + ", ".join(coverage.get("unsupported", [])))
    return problems


def _audio_problems(where: str, data: dict, room: str, devices: dict) -> list[str]:
    """Problems with a room's `audio`: the device its Volume and Mute drive,
    which must offer a volume thermite controls and be in a room the routed
    room covers, or be its own receiver or display. A receiver's zone is not
    the zone its volume moves (the main zone's), so it cannot be one."""
    from .. import remote_model  # it imports this module

    entry = data["routing"]["rooms"][room]
    ident = entry["audio"]
    device = devices.get(ident) if isinstance(ident, str) else None
    if device is None:
        return [f"{where}.audio: {ident!r} is not a device id" + inventory.hint(ident, devices)]
    if "volume" not in (remote_model.provider(device)[1] or {}):
        return [f"{where}.audio: {ident} offers no volume thermite controls (an integration under controls.control "
                "that drives one)"]
    stages = {spec["device"]: spec for key in STAGES
              if isinstance(spec := entry.get(key), dict) and isinstance(spec.get("device"), str)}
    if ident in stages and stages[ident].get("zone") is not None:
        return [f"{where}.audio: {ident} is this room's receiver on zone {stages[ident]['zone']}, and its volume is "
                "the main zone's"]
    if ident not in stages and device.get("room") not in covered_rooms(House.unchecked(data), room):
        return [f"{where}.audio: {ident} is in {device.get('room')}, not in a room {room} covers"]
    return []


def _audio_scaler_problems(where: str, entry: dict, offered: list, sources: dict, routing: dict, devices: dict,
                           hdmi_ok: bool) -> list[str]:
    """Problems with a room's hdmi_audio_scaler: by source the room takes
    from the HDMI matrix, one of the matrix's scaler names. The matrix needs
    a fixed scaler (its settings.scaler) for the room's other choices to give
    the output back."""
    scalers = entry["hdmi_audio_scaler"]
    if "hdmi_audio_output" not in entry:
        return [f"{where}: needs hdmi_audio_output, the output whose scaler it sets"]
    if not isinstance(scalers, dict) or not scalers:
        return [f"{where}: must map sources to a scaler name"]
    if not hdmi_ok:
        return []  # routing.hdmi_matrix says what is wrong
    device = devices[routing["hdmi_matrix"]]
    module = switcher(device)
    if module.SCALER is None:
        return [f"{where}: {module.NAME} cannot set an output's scaler"]
    problems = []
    settings = device.get("settings")
    fixed = settings.get("scaler") if isinstance(settings, dict) else None
    if not (isinstance(fixed, str) and fixed in module.SCALER):
        problems.append(f"{where}: needs {device['id']}.settings.scaler, the scaler the output gets back for the "
                        "room's other choices")
    local = _local(entry)
    for source, name in scalers.items():
        if source not in offered or source in local or not (isinstance(sources.get(source), dict)
                                                            and "hdmi_input" in sources[source]):
            problems.append(f"{where}.{source}: '{source}' is not one of this room's sources from the HDMI matrix")
        if not (isinstance(name, str) and name in module.SCALER):
            problems.append(f"{where}.{source}: {name!r} is not a scaler name of {device['id']} "
                            f"({', '.join(module.SCALER)})")
    return problems


def _carries_problems(where: str, carries: object, offered: list) -> list[str]:
    """Problems with a room's `carries`: what each source it offers carries."""
    if not isinstance(carries, dict) or not carries:
        return [f"{where}: must map the room's sources to what each carries ({', '.join(CARRIES)})"]
    problems = []
    for source, value in carries.items():
        if source not in offered:
            problems.append(f"{where}.{source}: '{source}' is not in this room's sources")
        if not isinstance(value, str) or value not in CARRIES:
            problems.append(f"{where}.{source}: {value!r} is not {', '.join(CARRIES[:-1])} or {CARRIES[-1]}")
    return problems


def _audio_input_problems(where: str, entry: dict, devices: dict) -> list[str]:
    """Problems with a room's `audio_input`: one of the inputs its `audio`
    device reads (its control's input), the one the room's route feeds."""
    from .. import remote_model  # it imports this module

    if "audio" not in entry:
        return [f"{where}.audio_input: needs audio, the device whose input it is"]
    device = _known(devices, entry["audio"])
    if device is None:
        return []  # _audio_problems says so
    options = ((remote_model.provider(device)[1] or {}).get("input") or {}).get("options") or []
    values = [str(option[0]) for option in options]
    if not values:
        return [f"{where}.audio_input: {device['id']} reads no input thermite controls"]
    if entry["audio_input"] not in values:
        return [f"{where}.audio_input: {entry['audio_input']!r} is not an input of {device['id']} "
                f"({', '.join(values)})"]
    return []


def signal_port(value: object) -> bool:
    """A native connector/zone identifier; booleans and fractional ports are not IDs."""
    return ((isinstance(value, int) and not isinstance(value, bool) and value >= 0)
            or (isinstance(value, str) and bool(value.strip()) and len(value) <= 128 and value.isprintable()))


def _audio_link_problems(where: str, entry: dict, devices: dict, data: dict, room_id: str) -> list[str]:
    """Explicit audio attachment must be on each source's applicable physical route."""
    where += ".audio_link"
    link = entry["audio_link"]
    if not isinstance(link, dict) or set(link) != {"from", "transport"}:
        return [f"{where}: must name from and transport only"]
    origin = link["from"]
    if not isinstance(origin, dict) or set(origin) != {"device", "port"}:
        return [f"{where}.from: must name device and port only"]
    problems = []
    if "audio" not in entry:
        problems.append(f"{where}: needs audio, the device the link feeds")
    if _known(devices, origin["device"]) is None:
        problems.append(f"{where}.from.device: must name a known device")
    if origin["device"] == entry.get("audio"):
        problems.append(f"{where}.from.device: cannot be the audio endpoint itself")
    if not signal_port(origin["port"]):
        problems.append(f"{where}.from.port: must be a nonempty connector name or a nonnegative integer")
    if link["transport"] not in SIGNAL_TRANSPORTS:
        problems.append(f"{where}.transport: must be one of {', '.join(SIGNAL_TRANSPORTS)}")
    if entry.get("audio") and any(isinstance(stage := entry.get(role), dict) and stage.get("device") == entry["audio"]
                                  for role in STAGES):
        problems.append(f"{where}: audio is already a routing stage; its route describes that connection")
    if problems:
        return problems
    from .. import remote_diagnostics

    config = data["routing"]
    unlinked = {key: value for key, value in entry.items() if key != "audio_link"}
    rooms = {**config["rooms"], room_id: unlinked}
    house = House.unchecked({**data, "routing": {**config, "rooms": rooms}})
    try:
        described = remote_diagnostics._rooms(house)
    except (KeyError, TypeError, AttributeError, IndexError):
        return problems  # The owning routing checks report malformed paths.
    for source, original in described[room_id]["sources"].items():
        for variant, path in [("normal", original), *(original.get("variants") or {}).items()]:
            if path["carries"] == "display":
                continue
            chain = path.get("audioNodes", path["nodes"])
            if not any(node["device"] == origin["device"] for node in chain):
                problems.append(f"{where}.from.device: is not on {source}'s audio route ({variant})")
            elif path.get("audioNodes") and origin["device"] != chain[-1]["device"]:
                problems.append(f"{where}.from.device: must extend {source}'s existing split audio leg ({variant})")
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
    device = _known(devices, device_id)
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
    if device is not None and (zone is None or _number(zone)):
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
    for key in sorted(unknown(STAGE, spec)):
        problems.append(f"{where}: unknown key '{key}'")
    return problems


def _wiring_problems(rooms: dict) -> list[str]:
    """Problems with a source cabled to a device whose zones several rooms
    take (the module docstring says what holds). A room's own inputs are
    _stage_problems()'s, and two rooms' signals from before their zones
    _sync_problems()'s."""
    problems: list[str] = []
    carried: dict[tuple, tuple] = {}  # (device, input) -> (its signal, cabled source or None, room, where)
    placed: dict[tuple, tuple] = {}  # (device, source) -> (its input, room, where)
    for room, entry in rooms.items():
        for key in STAGES:
            spec = entry.get(key) if isinstance(entry, dict) else None
            if not isinstance(spec, dict) or not isinstance(spec.get("device"), str):
                continue  # _stage_problems() says what is wrong with it
            where, device = f"routing.rooms.{room}.{key}", spec["device"]
            overrides, cabled = (spec.get(name) if isinstance(spec.get(name), dict) else {}
                                 for name in ("input_by_source", "sources"))
            wiring = [(f"{where}.input", spec.get("input"), None)]
            wiring += [(f"{where}.input_by_source.{source}", value, None) for source, value in overrides.items()]
            wiring += [(f"{where}.sources.{source}", value, source) for source, value in cabled.items()]
            for at, value, source in wiring:
                if not isinstance(value, str):
                    continue
                signal = source or f"{room}'s signal"
                held, local, other, there = carried.setdefault((device, value), (signal, source, room, at))
                if other != room and held != signal and (source or local):
                    problems.append(f"{at}: {device} has {held} on {value!r} ({there})")
                elif source:
                    first, other, there = placed.setdefault((device, source), (value, room, at))
                    if other != room and first != value:
                        problems.append(f"{at}: {device} has {source} on {first!r} ({there})")
    return problems


def _synced(entry: dict, leader: dict, sources: dict) -> dict:
    """{source: the leader receiver's input for it} for each source a
    follower room (`entry`) and its leader both take from the HDMI matrix,
    as far as both are mappings: offered by both and cabled to neither alone."""
    lead = leader.get("receiver") if isinstance(leader.get("receiver"), dict) else {}
    by_source = lead.get("input_by_source") if isinstance(lead.get("input_by_source"), dict) else {}
    offered = [entry.get("sources"), leader.get("sources")]
    if "input" not in lead or not all(isinstance(names, list) for names in offered):
        return {}
    local = {**_local(entry), **_local(leader)}
    return {source: by_source.get(source, lead["input"]) for source in offered[0]
            if source in offered[1] and source not in local and isinstance(sources.get(source), dict)
            and "hdmi_input" in sources[source]}


def _sync_problems(rooms: dict, sources: dict, devices: dict) -> list[str]:
    """Problems with each receiver's sync_with (the module docstring says
    what a follower and its leader need)."""
    problems: list[str] = []
    followed = {}
    for room, entry in rooms.items():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("display"), dict) and "sync_with" in entry["display"]:
            problems.append(f"routing.rooms.{room}.display.sync_with: only a receiver can sync with another room's")
        if isinstance(entry.get("receiver"), dict) and "sync_with" in entry["receiver"]:
            followed[room] = entry["receiver"]["sync_with"]
    for room, leader in followed.items():
        where = f"routing.rooms.{room}.receiver.sync_with"
        entry = rooms[room]
        other = rooms.get(leader) if isinstance(leader, str) and leader != room else None
        if not isinstance(other, dict):
            problems.append(f"{where}: {leader!r} is not another room under routing.rooms")
            continue
        spec, lead = entry["receiver"], other.get("receiver")
        found = []
        if "hdmi_output" not in entry or "hdmi_output" not in other:
            found.append(f"{room} and {leader} both need an hdmi_output")
        found += [f"{name} splits its audio (hdmi_audio_output), so its receiver does not show its picture"
                  for name in (room, leader) if "hdmi_audio_output" in rooms[name]]
        if "hdmi_switch" in other:
            found.append(f"{leader} has an hdmi_switch, so its matrix output does not decide what its receiver shows")
        if leader in followed:
            found.append(f"{leader} syncs with {followed[leader]!r} itself; a leader cannot follow another room")
        if (not isinstance(lead, dict) or lead.get("device") != spec.get("device")
                or lead.get("zone") == spec.get("zone")):
            found.append(f"{leader} needs a receiver on another zone of {spec.get('device')}")
        problems += [f"{where}: {text}" for text in found]
        device = _known(devices, spec.get("device"))
        module = stager(device) if device is not None and not found else None
        stage = module.stage(device, spec.get("zone")) if module is not None else None
        if stage is None:
            continue  # what is wrong is said above, or by the receiver's own checks
        shared = _synced(entry, other, sources)
        if not shared:
            problems.append(f"{where}: {room} and {leader} share no source from the HDMI matrix")
        own = [spec.get("input")]
        for key in ("input_by_source", "sources"):
            if isinstance(spec.get(key), dict):
                own.extend(spec[key].values())
        zone = f"{spec['device']} zone {spec['zone']}" if spec.get("zone") else f"{spec['device']}'s main zone"
        for source, value in shared.items():
            if value not in stage["inputs"]:
                problems.append(f"{where}: {zone} cannot select {value!r}, {leader}'s input for {source}")
            elif value in own:
                problems.append(f"{where}: {value!r}, {leader}'s input for {source}, is already an input of {zone} "
                                "in this room")
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
    device = _known(devices, device_id)
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
    for name in sorted(unknown(ROOM["hdmi_switch"].keys, switch)):
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


def _on_matrix(routing: dict) -> bool:
    """Whether the routing section names or cables anything to an HDMI matrix."""
    sources = routing.get("sources") if isinstance(routing.get("sources"), dict) else {}
    rooms = routing.get("rooms") if isinstance(routing.get("rooms"), dict) else {}
    return (any(key in routing for key in ("hdmi_matrix", "hdmi_off_input", "edid"))
            or any(isinstance(entry, dict) and "hdmi_input" in entry for entry in sources.values())
            or any(isinstance(entry, dict) and ("hdmi_output" in entry or "hdmi_audio_output" in entry)
                   for entry in rooms.values()))


def _edid_names(routing: dict, devices: dict, hdmi_ok: bool, problems: list[str]) -> dict | None:
    """The HDMI matrix's EDID names, checking routing.edid (None if unknown)."""
    edid = routing.get("edid")
    if edid is None:
        return None
    module = switcher(devices[routing["hdmi_matrix"]]) if hdmi_ok else None
    names = None
    if module is not None and module.EDID is None:
        problems.append(f"routing.edid: {module.NAME} cannot set an input's EDID")
    elif module is not None:
        names = module.EDID
    if not isinstance(edid, dict):
        problems.append("routing.edid: must be a mapping, such as {idle: <EDID name>} or {}")
        return names
    if "idle" in edid and names is not None and not (isinstance(edid["idle"], str) and edid["idle"] in names):
        problems.append(f"routing.edid.idle: {edid['idle']!r} is not an EDID name of {routing['hdmi_matrix']}")
    for key in sorted(unknown(HOUSE_KEYS["routing"].keys["edid"].keys, edid)):
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


def _hotplug_item(devices: dict, matrix_id: str, port: int) -> str | None:
    """The Item saying whether a display is on that output of the matrix,
    None for a matrix whose integration does not say (hotplug_item())."""
    device = devices[matrix_id]
    module = switcher(device)
    return module.hotplug_item(device, port) if module.hotplug_item else None


def audio_scaler(house: House, room: str, choice: str) -> list[list]:
    """[[Item, code]] that sets the scaler of the room's hdmi_audio_output
    for `choice`: its hdmi_audio_scaler for the choice, else the matrix's
    fixed scaler; none for a room without hdmi_audio_scaler."""
    routing = house.data["routing"]
    entry = routing["rooms"][room]
    if "hdmi_audio_scaler" not in entry:
        return []
    device = house.by_id[routing["hdmi_matrix"]]
    module = switcher(device)
    name = entry["hdmi_audio_scaler"].get(choice, device["settings"]["scaler"])
    return [[module.scaler_item(device, entry["hdmi_audio_output"]), module.SCALER[name]]]


def routed_scalers(house: House, matrix_id: str) -> dict[int, str]:
    """{output: room} of each output of a matrix whose scaler the routing
    sets (hdmi_audio_scaler), which thermite matrix-settings leaves alone."""
    routing = house.data.get("routing") or {}
    if routing.get("hdmi_matrix") != matrix_id:
        return {}
    return {entry["hdmi_audio_output"]: room for room, entry in (routing.get("rooms") or {}).items()
            if "hdmi_audio_scaler" in entry}


def commands(house: House, room: str, choice: str) -> list[list]:
    """[[Item, value], ...] that put `choice` (a source id or OFF) on in
    `room`'s matrices: its HDMI output first (a room without one has
    none), then the scaler of its audio output (audio_scaler()) and that
    output, then its HDMI switch if it has one, then each of its USB device
    ports. A source cabled to the room alone (its HDMI switch, receiver or
    display) turns the room's HDMI output off; any other choice puts the
    switch on the matrix, but Off leaves a switch the matrix does not reach
    (no matrix_input) as it is. A source with no host on a room's USB
    matrix parks those ports, and a source under the room's usb_only moves
    only the ports listed there: the room's others stay where they are."""
    routing = house.data["routing"]
    devices = house.by_id
    entry = routing["rooms"][room]
    source = routing["sources"].get(choice, {}) if choice != OFF else {}
    switch = entry.get("hdmi_switch")
    local = switch is not None and choice in switch["inputs"]
    off = routing.get("hdmi_off_input")
    hdmi_input = off if choice in _local(entry) else source.get("hdmi_input", off)
    out = []
    if "hdmi_output" in entry:
        out.append([_port_item(devices, routing["hdmi_matrix"], entry["hdmi_output"]), hdmi_input])
    if "hdmi_audio_output" in entry:
        out += audio_scaler(house, room, choice)
        out.append([_port_item(devices, routing["hdmi_matrix"], entry["hdmi_audio_output"]), hdmi_input])
    if switch is not None and (local or "matrix_input" in switch):
        out.append([_switch_item(devices, switch), switch["inputs"][choice] if local else switch["matrix_input"]])
    only = (entry.get("usb_only") or {}).get(choice)
    for matrix_id, ports in (entry.get("usb") or {}).items():
        host = usb_host(source, matrix_id, routing["usb_off_host"])
        moved = ports if only is None else only.get(matrix_id, [])
        out += [[_port_item(devices, matrix_id, port), host] for port in moved]
        if only is not None and entry.get("usb_unused", "preserve") == "park":
            out += [[_port_item(devices, matrix_id, port), routing["usb_off_host"]]
                    for port in ports if port not in moved]
    return out


def stages(house: House, room: str) -> list[tuple[dict, dict]]:
    """(spec, stage()) of the room's receiver, then its display; one whose
    device lists no integration that powers it (validate() reports it) is
    left out. A passive display is always skipped, including when a device
    gains a provider later; enabling routing requires removing passive."""
    devices = house.by_id
    entry = house.data["routing"]["rooms"][room]
    out = []
    for key in STAGES:
        if key in entry and entry[key].get("passive") is True:
            continue
        module = stager(devices[entry[key]["device"]]) if key in entry else None
        if module is not None:
            out.append((entry[key], module.stage(devices[entry[key]["device"]], entry[key].get("zone"))))
    return out


def wake(house: House, choice: str) -> list[list]:
    """[[Item, "ON"]] that wakes the chosen source, when its integration
    can (an Apple TV, a SHIELD); none for Off. Dedicated-source standby is
    explicitly configured with sleep_sources and sent by after()."""
    device = house.by_id.get(choice) if choice != OFF else None
    module = waker(device) if device is not None else None
    return [[module.wake_item(device), "ON"]] if module is not None else []


def power(house: House, room: str, choice: str) -> list[list]:
    """[[Item, "ON"], ...] a choice sends before it routes: the display's
    power, then the receiver's, which take longest, then the source's own
    (wake()); none for Off."""
    if choice == OFF:
        return []
    return [[stage["power"], "ON"] for _, stage in reversed(stages(house, room))] + wake(house, choice)


def inputs(house: House, room: str, choice: str) -> list[list]:
    """[[Item, input], ...] that put a choice on the room's screen once it
    is routed: the display's input, then the receiver's, as far as the
    choice passes through each. A source cabled to the display leaves the
    receiver's input alone; none for Off."""
    if choice == OFF:
        return []
    out = []
    for spec, stage in reversed(stages(house, room)):
        cabled = spec.get("sources") or {}
        if choice in cabled:
            out.append([stage["input"], cabled[choice]])
            break
        out.append([stage["input"], spec.get("input_by_source", {}).get(choice, spec["input"])])
    return out


def sync_sources(house: House, room: str) -> dict[str, str]:
    """{source: the input of the room's receiver zone that shows it from
    its leader's matrix output} for a room whose receiver has sync_with:
    each source both rooms take from the HDMI matrix. Empty without one."""
    rooms = house.data["routing"]["rooms"]
    leader = (rooms[room].get("receiver") or {}).get("sync_with")
    return {} if leader is None else _synced(rooms[room], rooms[leader], house.data["routing"]["sources"])


def sync_plan(source: str, leader: str) -> str:
    """The lifecycle route of a follower's choice of `source` while synced
    with `leader`; the room's Source choice stays `source`."""
    return f"{source}@{leader}"


def synced_inputs(house: House, room: str, source: str) -> list[list]:
    """inputs() of a follower's synced route: its receiver zone on the
    leader's input for the source, its display as on its own route."""
    receiver = house.data["routing"]["rooms"][room]["receiver"]
    item = next(stage["input"] for spec, stage in stages(house, room) if spec is receiver)
    value = sync_sources(house, room)[source]
    return [[name, value if name == item else target] for name, target in inputs(house, room, source)]


def after(house: House, room: str, choice: str) -> list[list]:
    """[[Item, value], ...] sent last: for Off, explicitly selected dedicated
    sources sleep before the receiver and display; then the room's actions."""
    devices = house.by_id
    entry = house.data["routing"]["rooms"][room]
    out = ([[item, "OFF"] for source in entry.get("sleep_sources", []) for item, _ in wake(house, source)]
           + [[stage["power"], "OFF"] for _, stage in stages(house, room)]) if choice == OFF else []
    actions = entry.get(ACTIONS[1] if choice == OFF else ACTIONS[0]) or {}
    return out + [[action_item(devices, device_id), action] for device_id, action in actions.items()]


def edid_table(house: House) -> dict | None:
    """What routing.js needs to follow the displays' EDIDs, or None without
    routing.edid: the EDID Item of each source's input, the EDID code each
    room's HDMI output gives its sources, and the code of an input no room
    shows (None: left). Source overrides are indexed by input number, then
    by output Item, and take precedence while that output shows the input."""
    routing = house.data["routing"]
    edid = routing.get("edid")
    if edid is None:
        return None
    device = house.by_id[routing["hdmi_matrix"]]
    module = switcher(device)
    rooms = routing["rooms"].values()
    table = {
        "inputs": {str(s["hdmi_input"]): module.edid_item(device, s["hdmi_input"])
                   for s in routing["sources"].values() if "hdmi_input" in s},
        "outputs": {module.switch_item(device, e["hdmi_output"]):
                    module.EDID[e.get("edid", module.copy_edid(e["hdmi_output"]))] for e in rooms
                    if "hdmi_output" in e},
        "idle": module.EDID[edid["idle"]] if "idle" in edid else None,
    }
    overrides: dict[str, dict[str, int]] = {}
    for entry in rooms:
        for source, name in entry.get("edid_by_source", {}).items():
            number = str(routing["sources"][source]["hdmi_input"])
            item = module.switch_item(device, entry["hdmi_output"])
            overrides.setdefault(number, {})[item] = module.EDID[name]
    if overrides:
        table["overrides"] = overrides
    return table


#: A readback problem's action while the room's output finds no display.
DETECT = "detect the display"
#: What a readback problem may say was being done (routing.js unavailable()).
ACTIONS = ("read selected input", "read matrix output input", "read power", DETECT)
#: Why it failed: no current reading, an input the room does not offer, or no
#: display on the room's output (oh_integrations._hotplug).
REASONS = ("unavailable", "unsupported_input", "no_display")


#: How many of a matrix's or an HDMI switch's polls (POLL_S) routing.js holds
#: the input a room's Item last read once a read finds nothing: past the next
#: poll, which answers one poll after the miss less the miss's own timeout,
#: and short of the one after, so a second miss shows as one at once did.
MISSED = 1.5


def reads(house: House, rooms: dict) -> dict[str, int]:
    """routing.js's TABLE.reads: {Item: ms} of each room's HDMI output, audio
    output and HDMI switch Item, and how long one missed read of it holds
    the input it read before (MISSED of its integration's POLL_S)."""
    routing = house.data["routing"]
    devices = house.by_id
    out = {}
    for room, entry in routing["rooms"].items():
        raw = rooms[room]
        held = [(raw[key], switcher(devices[routing["hdmi_matrix"]])) for key in ("hdmi", "audioHdmi") if raw.get(key)]
        if raw["switch"]:
            held.append((raw["switch"]["item"], switch_module(devices, entry["hdmi_switch"])))
        out.update({item: round(1000 * MISSED * module.POLL_S) for item, module in held})
    return out


#: Seconds routing.js holds a room's Source after a choice while its
#: receiver or display does not show it yet (RECENT_MS): at least this, a
#: receiver being read seconds after the matrix.
HOLD = 15


def hold_ms(house: House) -> int:
    """routing.js's RECENT_MS: HOLD, or the slowest read-back (READ_BACK)
    of a room's receiver or display and 2 s, if longer."""
    devices = house.by_id
    rooms = ((house.data.get("routing") or {}).get("rooms") or {}).values()
    staged = {(entry.get(role) or {}).get("device") for entry in rooms for role in ("receiver", "display")}
    slowest = max((module.READ_BACK for ident in staged if ident in devices
                   for module in [stager(devices[ident])] if module), default=0)
    return 1000 * max(HOLD, slowest + 2)


def table(house: House) -> dict:
    """What routing.js needs: by room id, the EDID table, and the Source
    state of a room that is off. A room's stages are its display, then its
    receiver: the order the Source is read back in."""
    routing = house.data["routing"]
    devices = house.by_id
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
            "label": room_label(house, room),
            "hdmi": _port_item(devices, routing["hdmi_matrix"], entry["hdmi_output"]) if fed else None,
            "hdmiHotplug": _hotplug_item(devices, routing["hdmi_matrix"], entry["hdmi_output"]) if fed else None,
            "choices": {choice: commands(house, room, choice) for choice in choices},
            "power": {choice: power(house, room, choice) for choice in choices},
            "inputs": {choice: inputs(house, room, choice) for choice in choices},
            "after": {choice: after(house, room, choice) for choice in choices},
            "readback": readback,
            "switch": None if switch is None else {
                "item": _switch_item(devices, switch),
                "matrix": str(switch["matrix_input"]) if "matrix_input" in switch else None,
                "readback": {str(number): source for source, number in switch["inputs"].items()},
                "hotplug": _hotplug_item(devices, switch["device"], switch["output"]) if "output" in switch else None,
            },
            "stages": [{"power": stage["power"], "input": stage["input"], "feed": spec.get("input"),
                        "readback": {value: source for source, value in (spec.get("sources") or {}).items()}}
                       for spec, stage in reversed(stages(house, room))],
        }
        if "hdmi_audio_output" in entry:
            rooms[room]["audioHdmi"] = _port_item(devices, routing["hdmi_matrix"], entry["hdmi_audio_output"])
        for raw_stage, (spec, _) in zip(rooms[room]["stages"], reversed(stages(house, room)), strict=True):
            if spec.get("input_by_source"):
                raw_stage["bySource"] = dict(spec["input_by_source"])
        sync = sync_sources(house, room)
        if sync:
            leader = entry["receiver"]["sync_with"]
            rooms[room]["sync"] = {
                "leader": leader,
                "hdmi": _port_item(devices, routing["hdmi_matrix"], routing["rooms"][leader]["hdmi_output"]),
                "input": next(stage["input"] for spec, stage in stages(house, room) if spec is entry["receiver"]),
                "choices": {source: {"plan": sync_plan(source, leader), "value": value,
                                     "inputs": synced_inputs(house, room, source)} for source, value in sync.items()},
            }
        checks = {}
        raw = rooms[room]
        for item, action in ([(raw[key], "read matrix output input") for key in ("hdmi", "audioHdmi") if raw.get(key)]) + (
                [(raw["switch"]["item"], "read selected input")] if raw["switch"] else []) + [
                (stage[field], action) for stage in raw["stages"]
                for field, action in (("power", "read power"), ("input", "read selected input"))] + (
                [(raw["sync"]["hdmi"], "read matrix output input")] if "sync" in raw else []):
            ident = next((ident for ident in sorted(devices, key=len, reverse=True) if item.startswith(ident + "_")), None)
            if ident:
                device = devices[ident]
                checks[item] = {"device": ident, "label": device.get("name") or device.get("model") or ident,
                                "model": device.get("model") or "", "action": action}
        for item, matrix_id, output in ([(raw["hdmiHotplug"], routing.get("hdmi_matrix"), entry.get("hdmi_output"))] + (
                [(raw["switch"]["hotplug"], switch["device"], switch.get("output"))] if raw["switch"] else [])):
            if item:
                device = devices[matrix_id]
                checks[item] = {"device": matrix_id, "label": device.get("name") or device.get("model") or matrix_id,
                                "model": device.get("model") or "", "action": DETECT, "output": output}
        raw["diagnosis"] = {"item": problem_item_name(room), "checks": checks,
                            "reported": readback_inputs(devices[row["device"]] for row in checks.values())}
    for room, raw in rooms.items():
        if "sync" in raw:
            rooms[raw["sync"]["leader"]].setdefault("followers", []).append(room)
    result = {"rooms": rooms, "edid": edid_table(house), "off": OFF, "holdMs": hold_ms(house),
              "hotplug": {"reports": _hotplug.REPORTS, "absentMs": 1000 * _hotplug.ABSENT},
              "reads": reads(house, rooms)}
    if lifecycle_enabled(house):
        from .lifecycle import compile_descriptor
        from .lifecycle_io import capabilities
        result["lifecycle"] = compile_descriptor(house, result, capabilities(house.devices))
    return result


def lifecycle_enabled(house: House) -> bool:
    routing = house.data.get("routing") or {}
    return (any(room.get("usb_unused") == "park" for room in routing.get("rooms", {}).values())
            or any(source.get("idle") == "standby" for source in routing.get("sources", {}).values()))


def options(house: House, room: str) -> str:
    sources = house.data["routing"]["sources"]
    offered = house.data["routing"]["rooms"][room]["sources"]
    return options_text([(OFF, "Off")] + [(s, sources[s]["label"]) for s in offered])


def source_item(house: House, room: str) -> dict:
    choices = options(house, room)
    # A Location with two screens (the Office's desk and TV) names each.
    shared = sum(home(house, other) == home(house, room) for other in house.data["routing"]["rooms"]) > 1
    return {
        "type": "String",
        "label": f"{room_label(house, room)} source" if shared else "Source",
        "icon": "video",
        "groups": [f"loc_{home(house, room)}"],
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


def rule(house: House, contract: dict | None = None) -> str:
    return fill(SOURCE.read_text(encoding="utf-8"), {"TABLE": table(house) if contract is None else contract}, SOURCE,
                part="the routing in", encode=indented)


def render(house: House, conf: Conf) -> None:
    routing = house.data.get("routing")
    if routing is None:
        return
    devices = house.by_id
    out = conf.yaml_file(ITEMS, HEADER)
    for room in routing["rooms"]:
        out.items[item_name(room)] = source_item(house, room)
        out.items[problem_item_name(room)] = {
            "type": "String", "label": "Source readback problem", "groups": [f"loc_{home(house, room)}"],
            "tags": ["Status"], "autoupdate": False,
            "metadata": {"stateDescription": {"config": {"readOnly": True}}},
        }
    out.items[METRICS_ITEM] = {"type": "String", "label": "Routing telemetry", "autoupdate": False,
                               "metadata": {"stateDescription": {"config": {"readOnly": True}}}}
    sources = routing["sources"].values()
    if "hdmi_matrix" in routing:
        _label_ports(conf, devices[routing["hdmi_matrix"]],
                     {routing["hdmi_off_input"]: "Off",
                      **{s["hdmi_input"]: s["label"] for s in sources if "hdmi_input" in s}})
    for entry in routing["rooms"].values():
        if "hdmi_switch" in entry:
            switch = entry["hdmi_switch"]
            labels = {switch["matrix_input"]: "HDMI matrix"} if "matrix_input" in switch else {}
            labels.update({n: routing["sources"][s]["label"] for s, n in switch["inputs"].items()})
            _label(conf, _switch_item(devices, switch), labels)
    off_host = routing.get("usb_off_host")
    for matrix_id in dict.fromkeys(m for e in routing["rooms"].values() for m in e.get("usb") or {}):
        labels = {off_host: "None"}
        labels.update({usb_host(s, matrix_id, off_host): s["label"] for s in sources
                       if usb_host(s, matrix_id, off_host) != off_host})
        _label_ports(conf, devices[matrix_id], labels)
    contract = table(house)
    if "lifecycle" in contract:
        from .lifecycle_io import configure
        configure(house, conf, contract["lifecycle"])
        conf.files["automation/js/node_modules/thermite-routing-lifecycle/index.js"] = (
            SOURCE.with_name("lifecycle.js").read_text(encoding="utf-8"))
        for room in routing["rooms"]:
            out.items[f"loc_{room}_source_lifecycle"] = {
                "type": "String", "label": "Route lifecycle", "autoupdate": False,
                "metadata": {"stateDescription": {"config": {"readOnly": True}}},
            }
    conf.files[RULE] = rule(house, contract)
    conf.add_addon(Addon("automation", "jsscripting", "the room Source rule (routing.js)"))
