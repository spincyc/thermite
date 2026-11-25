"""The device inventory (instance/devices.yaml) and its agreement with conf/.

The inventory is the single list of devices openHAB may talk to. conf/ names
its Things and Items after inventory ids so that this module can prove every
Thing belongs to a known device at that device's address.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import ipaddress
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

import yaml

import oh_control4
from oh_semantic import KINDS, LOCATION_TAGS
from oh_shape import LIST, MAPPING, NUMBER, TEXT, Key, fits, mismatches, of_kind, unknown, words

if TYPE_CHECKING:
    from oh_conf import House

ID = re.compile(r"^[a-z][a-z0-9_]*$")
MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")

#: The shape of the house file, declared (oh_shape): validate() derives its
#: checks of key names and kinds of value from these, and the docs site its
#: reference page and schema (declarations(), shape()), so a key is written
#: down once. ROOM is a room's keys.
ROOM = {
    "label": Key(TEXT, "the room's name, as people read it", required=True),
    "tag": Key(TEXT, "its openHAB Location tag (Room, Kitchen, Building and so on), which places it in the semantic model"),
    "in": Key(TEXT, "the room id of the room or building it is part of"),
}
#: What a device's controls list under a phase.
INTEGRATION_NAMES = Key(LIST, "integration names", each=Key(TEXT, "the name of an integration"),
                        must="a list of integration names", empty=True)
#: Per-device timing and endpoint override keys (design/device-contract.md).
TIMING_KEYS = {
    "settle": Key(MAPPING, "per-value settle time overrides in ms", by="value",
                  each=Key(NUMBER, "milliseconds (1..300000)")),
    "poll_s": Key(NUMBER, "poll interval override in seconds"),
    "port": Key(NUMBER, "TCP/HTTP port override"),
}
#: Reusable device fields that a profile in `profiles` may set as defaults.
PROFILE_FIELD_NAMES = ("kind", "make", "model", "controls", "config", "timing", "monitor", "reboot", "notes", "control4")
#: The keys any device may have: its identity, address and controls, how
#: closely monitoring watches it (monitor), timing overrides (timing),
#: profile inheritance (profile), and owner documentation no program reads:
#: notes, and the Control4 driver name `thermite control4 summary` suggests.
#: Integrations declare their fields under device.config.<integration>;
#: features and Control4 declare cross-cutting device keys directly.
DEVICE = {
    "id": Key(TEXT, "its name in thermite: snake_case and its own; every Item of the device starts with it", required=True),
    "room": Key(TEXT, "the room id of the room it is in", required=True),
    "kind": Key(TEXT, "what it is (tv, avr, streamer, hdmi_matrix and so on), which gives it its place in the "
                      "semantic model", required=True),
    "profile": Key(TEXT, "the id of a profile in profiles to inherit defaults from"),
    "name": Key(TEXT, "what people call it, where that is more than its model"),
    "make": Key(TEXT, "who makes it"),
    "model": Key(TEXT, "its model"),
    "ip": Key(TEXT, "its own IPv4 address; every device has one unless it has via"),
    "mac": Key(TEXT, "its MAC address, lowercase with colons, for dhcp-reservations and discover"),
    "via": Key(TEXT, "the device id of the gateway that reaches it (IR, serial, a relay), for a device with no "
                     "address of its own"),
    "controls": Key(MAPPING, "the integrations that read it (observe) and command it (control)", by="phase",
                    must="a mapping of phase to integration names", each=INTEGRATION_NAMES),
    "timing": Key(MAPPING, "per-device timing and endpoint overrides", keys=TIMING_KEYS),
    "notes": Key(TEXT, "whatever its owner wants to remember; no program reads it"),
    "control4": Key(TEXT, "the Control4 driver name that thermite control4 summary suggested; no program reads it"),
    # required lets a device's failed control checks count against control
    # health and alert; optional still shows them on the dashboards but
    # never counts them.
    "monitor": Key(TEXT, "how closely it is watched: optional keeps a device known to be broken out of control "
                         "health and its alerts, and required is the same as leaving it out",
                   choices=("required", "optional")),
}
#: A device, with the core's keys; shape() gives it every integration's too.
A_DEVICE = Key(MAPPING, "a device", keys=DEVICE)
#: The top-level keys this module and the house-wide remote read.
#: Integrations, features and Control4 (oh_control4) declare theirs
#: (HOUSE_KEYS: routing is the routing feature's); the module that reads
#: each key validates what is under it.
TOP = {
    "version": Key(NUMBER, "the format of the file: 2", required=True, choices=(2,)),
    "include": Key(LIST, "relative YAML files under instance/ merged into this house file",
                   each=Key(TEXT, "a relative path under instance/"), must="a list of relative paths", empty=True),
    "profiles": Key(MAPPING, "reusable device profiles", by="profile id",
                    each=Key(MAPPING, "a device profile"), must="a mapping of profile ids", empty=True),
    "rooms": Key(MAPPING, "the rooms of the house", by="room id", each=Key(MAPPING, "a room", keys=ROOM),
                 must="a mapping of room ids", empty=True),
    "devices": Key(LIST, "the devices of the house", each=A_DEVICE, must="a list of devices"),
    "later": Key(LIST, "devices not set up yet: their ids are kept for them and discover knows them, and nothing "
                       "is generated for them", each=Key(MAPPING, "a device to be: an id, and whatever is known of it"),
                 must="a list of devices", empty=True),
    "services": Key(LIST, "the ids of Things in conf/ that belong to no device, which validate then accepts",
                    each=Key(TEXT, "a service id"), must="a list of service ids", empty=True),
    "theme": Key(TEXT, "the colors of the remote: a built-in theme's name, or a theme file under instance/"),
    "diagnostics": Key(MAPPING, "what else is known about the picture's path", keys={
        "observers": Key(LIST, "devices that report on the signal passing between two others", each=Key(
            MAPPING, "an observer and the two devices it sits between", keys={
                "device": Key(TEXT, "the observer's device id", required=True),
                "after": Key(TEXT, "the device id the signal comes from", required=True),
                "before": Key(TEXT, "the device id the signal goes on to", required=True),
                "after_port": Key((TEXT, NUMBER), "the upstream output when that device pair has several connections"),
                "before_port": Key((TEXT, NUMBER), "the downstream input when that device pair has several connections"),
                "after_zone": Key((TEXT, NUMBER), "the upstream receiver zone when the device pair is ambiguous"),
                "before_zone": Key((TEXT, NUMBER), "the downstream receiver zone when the device pair is ambiguous")}))}),
}


#: The same shape as the type checker reads it: the house file, a room and
#: a device as mappings with named keys, so that a reader in a checked
#: module (pyproject.toml, [tool.mypy]) that misspells a key, or takes a
#: number for text, is told before a test runs. A type cannot be derived
#: from declarations that are gathered as the program runs, so this is a
#: second writing of them, and tests/test_house_types.py holds it to the
#: first: every key the declarations name, this module's and each
#: integration's, with its kind, and no other. A key that may stand empty
#: (Key.empty) may be None here. What House (oh_conf) hands out is these.
Room = TypedDict("Room", {"label": str, "tag": NotRequired[str], "in": NotRequired[str]})  # `in` is a Python word


class DeviceTiming(TypedDict, total=False):
    settle: dict[str, int]
    poll_s: int | float
    port: int


class MatrixSettings(TypedDict, total=False):
    edid: dict[int | str, str]
    scaler: Literal["bypass", "8k-to-4k", "to-1080p", "auto", "audio-only"]
    hdr: Literal["bypass", "hdr-to-sdr", "auto"]
    hdcp: Literal["1.4", "2.2", "follow-sink", "follow-source"]
    ports: list[int]


class MatrixPorts(TypedDict, total=False):
    ports: list[int]


class ReceiverConfig(TypedDict, total=False):
    zones: int
    inputs: list[str]
    has_diagnostics: bool


class HeosConfig(TypedDict, total=False):
    pid: int | str
    bridge: bool


class SonosConfig(TypedDict, total=False):
    udn: str
    model_profile: Literal["soundbar", "amp"]


class WiimConfig(TypedDict, total=False):
    model_profile: Literal["amp_pro", "mini", "generic"]


class TvConfig(TypedDict, total=False):
    speaker_audio: bool
    inputs: list[str]


class ProjectorConfig(TypedDict, total=False):
    inputs: list[str]


class AndroidTvConfig(TypedDict, total=False):
    remote_layout: Literal["android_tv", "nvidia_shield"]


class Camera(TypedDict, total=False):
    device_id: str
    package_camera: bool


class Infrared(TypedDict, total=False):
    via: str
    connector: int
    profile: str
    model: str
    ip: str


class Cec(TypedDict, total=False):
    matrix: str
    input: int


class DeviceConfig(TypedDict, total=False):
    sony_ip_control: TvConfig
    androidtv_googletv: AndroidTvConfig
    sonos: SonosConfig
    denonmarantz_http: ReceiverConfig
    heos: HeosConfig
    wiim_http: WiimConfig
    sony_adcp: ProjectorConfig
    bk808_http: MatrixSettings
    avpro_telnet: MatrixPorts
    unifi_protect: Camera
    ir: Infrared
    bk808_cec: Cec


class DeviceProfile(TypedDict, total=False):
    extends: str
    kind: str
    make: str
    model: str
    controls: dict[str, list[str] | None]
    config: DeviceConfig
    timing: DeviceTiming
    monitor: Literal["required", "optional"]
    reboot: Literal["nightly", "when_hung", "nightly_and_when_hung"]
    notes: str
    control4: str


class Device(TypedDict):
    id: str
    room: str
    kind: str
    profile: NotRequired[str]
    name: NotRequired[str]
    make: NotRequired[str]
    model: NotRequired[str]
    ip: NotRequired[str]
    mac: NotRequired[str]
    via: NotRequired[str]
    controls: NotRequired[dict[str, list[str] | None]]
    config: NotRequired[DeviceConfig]
    timing: NotRequired[DeviceTiming]
    notes: NotRequired[str]
    control4: NotRequired[str]
    monitor: NotRequired[Literal["required", "optional"]]
    # What features and Control4 declare directly on a device (DEVICE_KEYS).
    reboot: NotRequired[Literal["nightly", "when_hung", "nightly_and_when_hung"]]
    control4_item: NotRequired[int | list[int]]


class Observer(TypedDict):
    device: str
    after: str
    before: str
    after_port: NotRequired[str | int]
    before_port: NotRequired[str | int]
    after_zone: NotRequired[str | int]
    before_zone: NotRequired[str | int]


class Diagnostics(TypedDict, total=False):
    observers: list[Observer]


class Protect(TypedDict):
    host: str
    port: NotRequired[int]


class Reboots(TypedDict, total=False):
    time: str


class Control4(TypedDict, total=False):
    director: str
    rooms: dict[str, str]
    stale: list[int]


class RoutingEdid(TypedDict, total=False):
    idle: str


class RoutingSource(TypedDict):
    label: str
    hdmi_input: NotRequired[int]
    usb_host: NotRequired[int | dict[str, int]]
    idle: NotRequired[Literal["preserve", "standby"]]


class RoutingSwitch(TypedDict):
    device: str
    output: NotRequired[int]
    matrix_input: NotRequired[int]
    inputs: NotRequired[dict[str, int]]


class RoutingAudioOrigin(TypedDict):
    device: str
    port: str | int


RoutingAudioLink = TypedDict("RoutingAudioLink", {
    "from": RoutingAudioOrigin,
    "transport": Literal["hdmi", "optical", "coaxial", "analog", "arc", "earc"],
})


class RoutingStage(TypedDict):
    device: str
    zone: NotRequired[int]
    input: NotRequired[str]
    input_by_source: NotRequired[dict[str, str]]
    sources: NotRequired[dict[str, str]]
    sync_with: NotRequired[str]


class RoutingDisplay(TypedDict):
    device: str
    input: NotRequired[str]
    input_by_source: NotRequired[dict[str, str]]
    sources: NotRequired[dict[str, str]]
    passive: NotRequired[bool]


class RoutingRoom(TypedDict):
    room: NotRequired[str]
    label: NotRequired[str]
    hdmi_output: NotRequired[int]
    hdmi_audio_output: NotRequired[int]
    hdmi_audio_scaler: NotRequired[dict[str, str]]
    hdmi_switch: NotRequired[RoutingSwitch]
    hdmi_switches: NotRequired[list[RoutingSwitch]]
    usb: NotRequired[dict[str, list[int]]]
    usb_only: NotRequired[dict[str, dict[str, list[int]]]]
    usb_unused: NotRequired[Literal["preserve", "park"]]
    sources: list[str]
    sleep_sources: NotRequired[list[str]]
    edid: NotRequired[str]
    edid_by_source: NotRequired[dict[str, str]]
    audio: NotRequired[str]
    carries: NotRequired[dict[str, Literal["display", "audio", "both"]]]
    audio_input: NotRequired[str]
    audio_link: NotRequired[RoutingAudioLink]
    receiver: NotRequired[RoutingStage]
    soundbar: NotRequired[RoutingStage]
    display: NotRequired[RoutingDisplay]
    displays: NotRequired[list[RoutingDisplay]]
    on_actions: NotRequired[dict[str, str]]
    off_actions: NotRequired[dict[str, str]]


class Routing(TypedDict):
    hdmi_matrix: NotRequired[str]
    hdmi_off_input: NotRequired[int]
    usb_off_host: NotRequired[int]
    edid: NotRequired[RoutingEdid]
    sources: dict[str, RoutingSource]
    rooms: dict[str, RoutingRoom]


class HouseFile(TypedDict):
    version: Literal[2]
    include: NotRequired[list[str] | None]
    profiles: NotRequired[dict[str, DeviceProfile] | None]
    rooms: NotRequired[dict[str, Room] | None]
    devices: NotRequired[list[Device]]
    later: NotRequired[list[dict[str, Any]] | None]
    services: NotRequired[list[str] | None]
    theme: NotRequired[str]
    diagnostics: NotRequired[Diagnostics]
    # What the integrations, the features and Control4 declare (HOUSE_KEYS).
    protect: NotRequired[Protect]
    routing: NotRequired[Routing | None]
    reboots: NotRequired[Reboots]
    control4: NotRequired[Control4]


class ConfigError(ValueError):
    """A configuration file thermite cannot use: missing, unreadable, not
    YAML, or with problems. str() is `FILE[:LINE:COLUMN]: message`; the
    dispatcher (oh_cli) prints each of `lines()` and exits 2."""

    def __init__(self, path: Path | str | None, message: str, line: int | None = None,
                 column: int | None = None, problems: list[str] | tuple[str, ...] = ()) -> None:
        self.path, self.message, self.line, self.column = path, message, line, column
        self.problems = list(problems)
        where = ":".join(str(part) for part in (path, line, column) if part is not None)
        super().__init__(f"{where}: {message}" if where else message)

    def lines(self) -> list[str]:
        """What to print: each problem, then the summary."""
        return [f"{self.path}: {problem}" if self.path else problem for problem in self.problems] + [str(self)]


#: What the YAML 1.2 core schema reads a plain (unquoted) scalar as when it
#: is not text: https://yaml.org/spec/1.2.2/#1032-tag-resolution
CORE = tuple((f"tag:yaml.org,2002:{name}", re.compile(pattern)) for name, pattern in (
    ("null", r"null|Null|NULL|~|"),
    ("bool", r"true|True|TRUE|false|False|FALSE"),
    ("int", r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+"),
    ("float", r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"),
))
STR = "tag:yaml.org,2002:str"
#: Digits after a zero: octal in YAML 1.1 (010 is 8, and 08 is text),
#: decimal in YAML 1.2, and often a code that is text to whoever wrote it.
LEADING_ZERO = re.compile(r"[-+]?0[0-9_]+")


def unclear(value: str, read: str) -> tuple[str, str] | None:
    """(kind, what it is and what to write) for a plain scalar that does not
    mean one thing; None for one that does. `read` is the tag PyYAML, a YAML
    1.1 reader, gives it: what every earlier thermite made of it. It means
    one thing when the YAML 1.2 core schema reads it the same."""
    core = next((tag for tag, pattern in CORE if pattern.fullmatch(value)), STR)
    quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if read.endswith(":merge"):
        return "merge key", "<< merges another mapping into this one in YAML 1.1 only: write its keys out here"
    if read.endswith(":value"):
        return "value key", f"= is a special key in YAML 1.1 only: write {quoted} in quotes"
    if LEADING_ZERO.fullmatch(value):
        return "leading zero", (f"{value} starts with a zero, which makes it octal in YAML 1.1 and decimal in YAML "
                                f"1.2: write the number without it, or {quoted} in quotes for text")
    if read == core:
        return None
    if read.endswith(":bool"):
        return "truth word", (f"{value} is true or false in YAML 1.1 and a word in YAML 1.2: write true or false, "
                              f"or {quoted} in quotes for the word")
    if read.endswith(":timestamp"):
        return "date", f"{value} is a date in YAML 1.1 and text in YAML 1.2: write {quoted} in quotes"
    if ":" in value:
        return "base 60", f"{value} is a number in base 60 in YAML 1.1 and text in YAML 1.2: write {quoted} in quotes"
    return "number form", (f"{value} is a number in one version of YAML and text in the other: write a plain "
                           f"decimal number, or {quoted} in quotes for text")


class StrictLoader(yaml.SafeLoader):
    """How a site's own files, the house file and the host file, are read:
    PyYAML's safe loader, which reads YAML 1.1, kept to what means one
    thing. A plain scalar loads when YAML 1.1 and the YAML 1.2 core schema
    read it the same (text, null, true and false, a decimal number); one
    they read differently (on, 010, 1_000, 12:30, 2026-11-18)
    is noted in `refused` with what to write. So are a tag, half of a
    character, and a key written twice in one mapping, of which a YAML
    reader keeps the last. A file this loads therefore holds what
    yaml.safe_load made of it in every earlier release, and what a person
    reading it sees."""

    def __init__(self, stream) -> None:
        super().__init__(stream)
        #: (line, column, kind, sentence) of each refusal.
        self.refused: list[tuple[int, int, str, str]] = []

    def refuse(self, mark: object, kind: str, sentence: str) -> None:
        # PyYAML's annotations allow an event or a node no mark; this
        # reader, the one written in Python, gives each its own.
        assert isinstance(mark, yaml.Mark)
        self.refused.append((mark.line + 1, mark.column + 1, kind, sentence))

    def compose_node(self, parent, index):
        event = self.peek_event()
        node = super().compose_node(parent, index)
        if isinstance(event, yaml.AliasEvent):
            return node  # checked where its anchor is
        if event.tag is not None:
            tag = event.tag.replace("tag:yaml.org,2002:", "!!")
            self.refuse(event.start_mark, "tag", f"{tag} is a tag, which this file does not use: remove it, "
                                                 "and put text in quotes")
        elif isinstance(event, yaml.ScalarEvent):
            # Only a plain scalar is resolved: a quoted one is text.
            found = unclear(event.value, node.tag) if event.implicit[0] and node is not None else None
            if found:
                self.refuse(event.start_mark, *found)
            elif any("\ud800" <= character <= "\udfff" for character in event.value):
                self.refuse(event.start_mark, "half character",
                            "a pair of \\u escapes (\\uD83D\\uDCFA, say) is one character in JSON and two halves "
                            "of one in YAML: write the character itself")
        return node

    def construct_mapping(self, node, deep=False):
        first: dict[object, yaml.Node] = {}
        for key_node, _ in node.value if isinstance(node, yaml.MappingNode) else ():
            key = self.construct_object(key_node, deep=True)
            try:
                earlier = first.setdefault(key, key_node).start_mark
            except TypeError:
                continue  # a list or a mapping as a key: PyYAML refuses it below
            if earlier is not key_node.start_mark:
                where = (f"line {earlier.line + 1}" if earlier.line != key_node.start_mark.line
                         else f"this line, column {earlier.column + 1}")
                self.refuse(key_node.start_mark, "duplicate key",
                            f"key {key!r} is written twice in this mapping (first on {where}): keep one")
        return super().construct_mapping(node, deep=deep)


def read(text: str | bytes) -> tuple[object, list[tuple[int, int, str, str]]]:
    """The text of a house file or a host file as (content, refusals). Each
    refusal is the (line, column, kind, sentence) of something StrictLoader
    does not take,
    in file order; with any, the content is not what the file says and is
    not to be used. Raises yaml.YAMLError for text that is not YAML."""
    loader = StrictLoader(text)
    try:
        content = loader.get_single_data()
    except yaml.YAMLError:
        if not loader.refused:
            raise
        # What followed from something refused: << given no mapping to merge, say.
        content = None
    finally:
        loader.dispose()
    return content, sorted(loader.refused)


class Unreadable(ConfigError):
    """A configuration file that is missing or that the system would not
    read: nothing in it is wrong, as far as anyone can tell."""


def load(path: Path) -> object:
    """A house file's content; ConfigError when it cannot be read (Unreadable,
    when the system would not read it), is not YAML, or holds what
    StrictLoader refuses, each named with its line and what to write. Its
    shape is not checked: oh_conf.House.load is the reader of a house file
    (tests/test_house_loader.py)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise Unreadable(path, f"cannot be read: {error.strerror or error}") from None
    except UnicodeDecodeError as error:
        raise ConfigError(path, f"cannot be read: {error}") from None
    try:
        content, refused = read(text)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        raise ConfigError(path, f"is not YAML: {error.problem or error.context}",
                          mark.line + 1 if mark else None, mark.column + 1 if mark else None) from None
    except yaml.YAMLError as error:
        raise ConfigError(path, f"is not YAML: {error}") from None
    if refused:
        raise ConfigError(path, f"{len(refused)} problem{'s' if len(refused) != 1 else ''}; nothing done",
                          problems=[f"line {line}: {sentence}" for line, _, _, sentence in refused])
    return content


def hint(word: object, choices) -> str:
    """'; did you mean X?' when one of `choices` is close to `word`."""
    close = difflib.get_close_matches(str(word), [str(choice) for choice in choices], n=1)
    return f"; did you mean {close[0]}?" if close else ""


def room_label(data: HouseFile | dict, room: str) -> str:
    """A room's display label (its id if it has none)."""
    entry = (data.get("rooms") or {}).get(room)
    return str(entry.get("label") or room) if isinstance(entry, dict) else room


def validate_rooms(rooms: object) -> list[str]:
    """Problems in the rooms mapping: ids, labels, Location tags, nesting."""
    if not isinstance(rooms, dict):
        return [f"rooms must map room ids to {{{', '.join(ROOM)}}}"]
    problems: list[str] = []
    for room, entry in rooms.items():
        where = f"rooms.{room}"
        if not ID.match(str(room)):
            problems.append(f"room id '{room}' is not snake_case")
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping with at least a label")
            continue
        if not isinstance(entry.get("label"), str) or not entry["label"].strip():
            problems.append(f"{where}: missing label")
        if "tag" in entry and (not isinstance(entry["tag"], str) or entry["tag"] not in LOCATION_TAGS):
            problems.append(f"{where}: tag {entry['tag']!r} is not an openHAB Location tag")
        if "in" in entry:
            parent = rooms.get(entry["in"]) if isinstance(entry["in"], str) else None
            if entry["in"] == room or not isinstance(parent, dict):
                problems.append(f"{where}: in {entry['in']!r} is not another room id")
            elif "tag" in entry and "tag" not in parent:
                problems.append(f"{where}: is a Location, so the room it is in needs a tag too")
        for key in sorted(unknown(ROOM, entry)):
            problems.append(f"{where}: unknown key '{key}'")
    for room in rooms:
        trail = [room]
        while isinstance(rooms.get(trail[-1]), dict) and isinstance(rooms[trail[-1]].get("in"), str) \
                and rooms[trail[-1]]["in"] in rooms:
            trail.append(rooms[trail[-1]]["in"])
            if trail[-1] in trail[:-1]:
                if trail[-1] == room and len(trail) > 2:
                    problems.append(f"rooms.{room}: in forms a cycle: {' -> '.join(trail)}")
                break
    return problems


def declarations() -> tuple[dict[str, Key], dict[str, Key], dict[tuple[str, str], str]]:
    """(top-level keys, device keys, who reads each) of devices.yaml: this
    module's, and those the integrations, the features and Control4
    declare (HOUSE_KEYS, DEVICE_KEYS). Integration DEVICE_KEYS are nested
    under device.config.<integration>; feature and Control4 DEVICE_KEYS sit
    directly on a device. The readers are by (hook, key), for the keys that
    are not this module's."""
    # Imported here: the integration modules import lib/oh_conf.py, which
    # imports this module.
    import oh_features
    import oh_integrations

    top, device, readers = dict(TOP), dict(DEVICE), {}
    config_keys: dict[str, Key] = {}
    for module in oh_integrations.REGISTRY:
        name = module.NAME
        house = oh_integrations.declared("HOUSE_KEYS", (module,))
        fields = oh_integrations.declared("DEVICE_KEYS", (module,))
        for key, declaration in house.items():
            if top.setdefault(key, declaration) is not declaration:
                raise ValueError(f"HOUSE_KEYS: {name} declares {key}, which another module declares too")
            readers["HOUSE_KEYS", key] = name
        if fields:
            config_keys[name] = Key(MAPPING, f"{name} integration settings", keys=fields)
            readers["CONFIG_KEYS", name] = name
    device["config"] = Key(MAPPING, "integration-specific device settings, keyed by integration name", keys=config_keys)
    for module, name in (*((m, m.NAME) for m in oh_features.REGISTRY), (oh_control4, "control4")):
        house = oh_integrations.declared("HOUSE_KEYS", (module,))
        fields = oh_integrations.declared("DEVICE_KEYS", (module,))
        for hook, found, into in (("HOUSE_KEYS", house, top), ("DEVICE_KEYS", fields, device)):
            for key, declaration in found.items():
                if into.setdefault(key, declaration) is not declaration:
                    raise ValueError(f"{hook}: {name} declares {key}, which another module declares too")
                readers[hook, key] = name
    profile_keys: dict[str, Key] = {
        "extends": Key(TEXT, "the id of a parent profile in profiles to inherit defaults from"),
    }
    for field_name in PROFILE_FIELD_NAMES:
        if field_name in device:
            decl = device[field_name]
            profile_keys[field_name] = dataclasses.replace(decl, required=False) if decl.required else decl
    top["profiles"] = dataclasses.replace(top["profiles"], each=Key(MAPPING, "a device profile", keys=profile_keys))
    return top, device, readers


def keys() -> tuple[frozenset, frozenset]:
    """(top-level keys, device keys) devices.yaml may have."""
    top, device, _ = declarations()
    return frozenset(top), frozenset(device)


def shape() -> Key:
    """The whole house file, as one declaration."""
    top, device, _ = declarations()
    devices = dataclasses.replace(top["devices"], each=dataclasses.replace(A_DEVICE, keys=device))
    return Key(MAPPING, "the house file", keys={**top, "devices": devices})


_CORE_TOP = frozenset({"version", "include", "profiles", "rooms", "devices", "later", "services", "control4"})
_CORE_DEVICE = frozenset(DEVICE) | {"control4_item"}
_SETTLE_MAX_MS = 300000
_POLL_MAX_S = 3600


def expand_includes(data: object, base_dir: Path, *, source: Path | str | None = None) -> object:
    """Merge relative YAML files named in top-level `include:` under `base_dir`.

    Refuses absolute paths, `..` traversal, symlinks escaping `base_dir`,
    missing/unreadable files, non-mapping fragments, cycles, duplicate IDs in
    `profiles`/`rooms`/`routing.sources`/`routing.rooms`, and conflicting
    duplicate definitions of other keys. Strips `include` after expansion.
    """
    if not isinstance(data, dict) or "include" not in data:
        return data
    base = Path(base_dir).resolve()
    root_label = Path(source).name if source is not None else "devices.yaml"
    root_stack = [Path(source).resolve()] if source is not None else []
    merged: dict[str, Any] = {}
    top_origins: dict[str, str] = {}
    routing_origins: dict[str, str] = {}
    dict_origins: dict[str, dict[str, str]] = {
        "profiles": {},
        "rooms": {},
        "routing.sources": {},
        "routing.rooms": {},
    }

    def fail(message: str) -> None:
        raise ConfigError(source, message, problems=[message])

    def merge_mapping_ids(target: dict[str, Any], incoming: dict[str, Any], section: str, label: str) -> None:
        origins = dict_origins[section]
        for entry_id, entry_value in incoming.items():
            key_text = str(entry_id)
            if key_text in origins:
                fail(f"{section}.{key_text} is defined in both {origins[key_text]} and {label}")
            origins[key_text] = label
            target[entry_id] = copy.deepcopy(entry_value)

    def merge_file(mapping: dict[str, Any], label: str, stack: list[Path], *, is_root: bool) -> None:
        includes = mapping.get("include")
        if "include" in mapping and includes is not None and not isinstance(includes, list):
            fail("include must be a list of relative paths" if is_root else f"{label}: include must be a list of relative paths")
        for key, value in mapping.items():
            if key == "include":
                continue
            if key == "version":
                if not is_root and (type(value) is not int or value != 2):
                    fail(f"{label}: version must be 2")
                if "version" in merged:
                    if type(merged["version"]) is not type(value) or merged["version"] != value:
                        fail(f"version conflicts between {top_origins['version']} and {label}")
                else:
                    merged["version"] = value
                    top_origins["version"] = label
                continue
            if key in ("profiles", "rooms"):
                if value is None:
                    merged.setdefault(key, None)
                    continue
                if not isinstance(value, dict):
                    fail(f"{key} must be {TOP[key].must}" if is_root else f"{label}: {key} must be {TOP[key].must}")
                if not isinstance(merged.get(key), dict):
                    merged[key] = {}
                merge_mapping_ids(merged[key], value, key, label)
                continue
            if key in ("devices", "later", "services"):
                if value is None:
                    if key == "devices" and not is_root:
                        fail(f"{label}: devices must be a list of devices")
                    merged.setdefault(key, None)
                    continue
                if not isinstance(value, list):
                    fail(f"{key} must be {TOP[key].must}" if is_root else f"{label}: {key} must be {TOP[key].must}")
                if not isinstance(merged.get(key), list):
                    merged[key] = []
                merged[key].extend(copy.deepcopy(value))
                continue
            if key == "routing":
                if value is None:
                    merged.setdefault("routing", None)
                    continue
                if not isinstance(value, dict):
                    fail("routing must be a mapping" if is_root else f"{label}: routing must be a mapping")
                if not isinstance(merged.get("routing"), dict):
                    merged["routing"] = {}
                target_routing = merged["routing"]
                for rkey, rval in value.items():
                    if rkey in ("sources", "rooms"):
                        if rval is None:
                            target_routing.setdefault(rkey, None)
                            continue
                        if not isinstance(rval, dict):
                            fail(f"{label}: routing.{rkey} must be a mapping")
                        if not isinstance(target_routing.get(rkey), dict):
                            target_routing[rkey] = {}
                        merge_mapping_ids(target_routing[rkey], rval, f"routing.{rkey}", label)
                    else:
                        if rval is None:
                            target_routing.setdefault(rkey, None)
                            continue
                        if rkey in routing_origins:
                            if target_routing[rkey] != rval:
                                fail(f"routing.{rkey} conflicts between {routing_origins[rkey]} and {label}")
                        else:
                            routing_origins[rkey] = label
                            target_routing[rkey] = copy.deepcopy(rval)
                continue
            if value is None:
                merged.setdefault(key, None)
                continue
            if key in top_origins:
                if merged[key] != value:
                    fail(f"{key} conflicts between {top_origins[key]} and {label}")
            else:
                top_origins[key] = label
                merged[key] = copy.deepcopy(value)

        for rel in includes or []:
            if not isinstance(rel, str) or not rel.strip():
                fail(f"{label}: include entry {rel!r} must be a relative path")
            rel_path = Path(rel)
            if rel_path.is_absolute() or rel.startswith(("/", "\\")) or ".." in rel_path.parts:
                fail(f"{label}: include path {rel!r} must be a relative path under instance/")
            try:
                resolved = (base / rel_path).resolve()
            except OSError as error:
                fail(f"{label}: include path {rel!r} cannot be resolved: {error}")
            if not resolved.is_relative_to(base) or resolved == base:
                fail(f"{label}: include path {rel!r} escapes instance/")
            if resolved in stack:
                trail = [p.relative_to(base).as_posix() if p.is_relative_to(base) else p.name for p in stack]
                fail(f"include cycle: {' -> '.join([*trail, rel])}")
            if not resolved.is_file():
                fail(f"{label}: included file {rel!r} does not exist")
            try:
                child = load(resolved)
            except ConfigError as error:
                child_problems = [f"{rel}: {p}" for p in (error.problems or [error.message])]
                raise ConfigError(source, f"{len(child_problems)} problem{'s' if len(child_problems) != 1 else ''}; nothing done",
                                  problems=child_problems) from None
            if not isinstance(child, dict):
                fail(f"{rel}: included file must be a YAML mapping")
            assert isinstance(child, dict)
            merge_file(child, rel, [*stack, resolved], is_root=False)

    merge_file(data, root_label, root_stack, is_root=True)
    return merged


def _validate_timing(timing: object, where: str) -> list[str]:
    """Validate a device's or profile's `timing` mapping."""
    if timing is None:
        return []
    if not isinstance(timing, dict):
        return [f"{where}.timing: must be a mapping"]
    problems: list[str] = []
    for key in unknown(TIMING_KEYS, timing):
        problems.append(f"{where}.timing: unknown key '{key}'{hint(key, TIMING_KEYS)}")
    settle = timing.get("settle")
    if settle is not None:
        if not isinstance(settle, dict):
            problems.append(f"{where}.timing.settle: must be a mapping of value to milliseconds")
        else:
            for value, ms in settle.items():
                if not isinstance(value, str) or not value:
                    problems.append(f"{where}.timing.settle: key {value!r} must be text")
                if type(ms) is not int or not 1 <= ms <= _SETTLE_MAX_MS:
                    problems.append(f"{where}.timing.settle.{value}: must be an integer from 1 to {_SETTLE_MAX_MS}")
    poll_s = timing.get("poll_s")
    if poll_s is not None:
        if type(poll_s) not in (int, float) or not math.isfinite(poll_s) or not 0 < poll_s <= _POLL_MAX_S:
            problems.append(f"{where}.timing.poll_s: must be a positive number up to {_POLL_MAX_S}")
    port = timing.get("port")
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            problems.append(f"{where}.timing.port: must be an integer from 1 to 65535")
    return problems


def _merge_device_fields(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deterministically merge `override` onto `base` (profile -> child profile or device)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key == "extends":
            continue
        if key == "controls" and isinstance(value, dict) and isinstance(out.get("controls"), dict):
            merged_controls = dict(out["controls"])
            for phase, names in value.items():
                merged_controls[phase] = copy.deepcopy(names)
            out["controls"] = merged_controls
        elif key == "config" and isinstance(value, dict) and isinstance(out.get("config"), dict):
            merged_config = dict(out["config"])
            for integ, sub in value.items():
                if isinstance(sub, dict) and isinstance(merged_config.get(integ), dict):
                    merged_config[integ] = {**merged_config[integ], **copy.deepcopy(sub)}
                else:
                    merged_config[integ] = copy.deepcopy(sub)
            out["config"] = merged_config
        elif key == "timing" and isinstance(value, dict) and isinstance(out.get("timing"), dict):
            merged_timing = dict(out["timing"])
            for tkey, tval in value.items():
                if tkey == "settle" and isinstance(tval, dict) and isinstance(merged_timing.get("settle"), dict):
                    merged_timing["settle"] = {**merged_timing["settle"], **copy.deepcopy(tval)}
                else:
                    merged_timing[tkey] = copy.deepcopy(tval)
            out["timing"] = merged_timing
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_profiles(data: dict) -> list[str]:
    """Validate top-level `profiles:` definitions, `extends:` references and cycles."""
    profiles = data.get("profiles")
    if profiles is None:
        return []
    if not isinstance(profiles, dict):
        return [f"profiles must be {TOP['profiles'].must}"]
    top_keys, _, _ = declarations()
    profile_decl = top_keys["profiles"].each
    assert profile_decl is not None and profile_decl.keys is not None
    profile_keys = profile_decl.keys
    problems: list[str] = []
    for pid, entry in profiles.items():
        where = f"profiles.{pid}"
        if not ID.match(str(pid)):
            problems.append(f"profile id '{pid}' is not snake_case")
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        for key in sorted(unknown(profile_keys, entry)):
            problems.append(f"{where}: unknown key '{key}'{hint(key, profile_keys)}")
        parent = entry.get("extends")
        if parent is not None:
            if not isinstance(parent, str):
                problems.append(f"{where}: extends {parent!r} must be text")
            elif parent == pid:
                problems.append(f"{where}: extends '{parent}' cannot refer to itself")
            elif parent not in profiles:
                problems.append(f"{where}: extends '{parent}' is not in profiles{hint(parent, profiles)}")
        for key, decl in profile_keys.items():
            if key in entry and entry[key] is not None:
                if key == "timing":
                    problems += _validate_timing(entry[key], where)
                else:
                    problems += mismatches(decl, entry[key], f"{where}.{key}")
    for pid in profiles:
        trail = [pid]
        while isinstance(profiles.get(trail[-1]), dict) and isinstance(profiles[trail[-1]].get("extends"), str) \
                and profiles[trail[-1]]["extends"] in profiles:
            nxt = profiles[trail[-1]]["extends"]
            trail.append(nxt)
            if nxt in trail[:-1]:
                if nxt == pid and len(trail) > 2:
                    problems.append(f"profiles.{pid}: extends forms a cycle: {' -> '.join(trail)}")
                break
    return problems


def resolve_profiles(data: dict) -> dict:
    """Return a working copy of `data` with `device.profile` defaults merged in.

    Returns `data` unchanged (zero copy) when no `profiles` are declared and no
    device references a `profile`. Raises ConfigError on invalid profiles,
    unknown profiles, or `extends` cycles. Never mutates `data`.
    """
    if not isinstance(data, dict):
        return data
    profiles = data.get("profiles")
    devices = data.get("devices")
    has_ref = isinstance(devices, list) and any(isinstance(d, dict) and d.get("profile") is not None for d in devices)
    if not profiles and not has_ref:
        return data
    problems = validate_profiles(data)
    if problems:
        raise ConfigError(None, "invalid profiles: " + "; ".join(problems), problems=problems)
    profiles_map = profiles if isinstance(profiles, dict) else {}
    memo: dict[str, dict[str, Any]] = {}

    def resolve_one(pid: str) -> dict[str, Any]:
        if pid in memo:
            return memo[pid]
        entry = profiles_map[pid]
        parent = entry.get("extends")
        base = resolve_one(parent) if isinstance(parent, str) else {}
        memo[pid] = _merge_device_fields(base, entry)
        return memo[pid]

    for pid in profiles_map:
        resolve_one(pid)

    if not isinstance(devices, list):
        return data
    resolved_devices: list[Any] = []
    missing_problems: list[str] = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict) or device.get("profile") is None:
            resolved_devices.append(copy.deepcopy(device))
            continue
        where = str(device.get("id") or f"devices[{index}]")
        ref = device["profile"]
        if not isinstance(ref, str):
            missing_problems.append(f"{where}: profile {ref!r} must be text")
            continue
        if ref not in memo:
            missing_problems.append(f"{where}: profile '{ref}' is not in profiles{hint(ref, memo)}")
            continue
        resolved_devices.append(_merge_device_fields(memo[ref], device))
    if missing_problems:
        raise ConfigError(None, "invalid profiles: " + "; ".join(missing_problems), problems=missing_problems)
    return {**data, "devices": resolved_devices}


def extension_mismatches(data: dict) -> list[str]:
    """Shape mismatches for present declared top-level and device extension keys."""
    top_keys, device_keys, _ = declarations()
    problems: list[str] = []
    for key, declaration in top_keys.items():
        if key not in _CORE_TOP and data.get(key) is not None:
            problems += mismatches(declaration, data[key], key)
    for index, device in enumerate(data.get("devices") or []):
        if not isinstance(device, dict):
            continue
        where = str(device.get("id") or f"devices[{index}]")
        for key, declaration in device_keys.items():
            if key not in _CORE_DEVICE and key in device and device[key] is not None:
                problems += mismatches(declaration, device[key], f"{where}.{key}")
    return problems


def validate(data: object, *, check_extensions: bool = True, instance_dir: Path | None = None) -> list[str]:
    """Every structural problem in a parsed devices.yaml.

    Problems that concern one integration's or one feature's fields are
    found by lib/oh_render.py, which knows them all.
    """
    # Imported here: the integration modules import lib/oh_conf.py, which
    # imports this module.
    import oh_integrations

    if instance_dir is not None and isinstance(data, dict) and "include" in data:
        try:
            data = expand_includes(data, instance_dir)
        except ConfigError as error:
            return list(error.problems) or [error.message]

    top_keys, device_keys, _ = declarations()
    if not isinstance(data, dict) or type(data.get("version")) is not int or not fits(top_keys["version"], data.get("version")):
        return [f"devices.yaml: version must be {words(top_keys['version'])}"]
    problems = [f"unknown top-level key '{key}'{hint(key, top_keys)}" for key in unknown(top_keys, data)]
    for key, declaration in top_keys.items():
        if declaration.must and data.get(key) is not None and not of_kind(declaration, data[key]):
            problems.append(f"{key} must be {declaration.must}")
    # A section left empty (`later:` above entries that are all commented
    # out) is the same as none. Not devices: code reads it as
    # data.get("devices", []) and data["devices"], which a null breaks.
    if "devices" in data and data["devices"] is None:
        problems.append("devices must be a list of devices; write `devices: []` for a house with none")
    if "include" in data and isinstance(data["include"], list):
        for index, entry in enumerate(data["include"]):
            if not isinstance(entry, str) or not entry.strip():
                problems.append(f"include[{index}]: {entry!r} must be a relative path")
    if problems:
        return problems
    problems = validate_rooms(data.get("rooms") or {})
    profile_problems = validate_profiles(data)
    problems += profile_problems
    rooms = data.get("rooms")
    rooms = rooms if isinstance(rooms, dict) else {}
    allowed = oh_integrations.allowed()
    needs_mac = {module.NAME for module in oh_integrations.REGISTRY if module.NEEDS_MAC}
    config_decl = device_keys["config"]
    assert config_decl.keys is not None
    config_keys = config_decl.keys
    seen: dict[str, dict[str, str]] = {"id": {}, "ip": {}, "mac": {}}

    def unique(kind: str, value: str, where: str) -> None:
        if value in seen[kind]:
            problems.append(
                f"{where}: {kind} {value} already used by {seen[kind][value]}")
        else:
            seen[kind][value] = where

    raw_devices = data.get("devices") or []
    # Check per-device profile references before resolving, then resolve on a
    # working copy so missing required fields (such as kind or controls) can be
    # satisfied by a profile.
    raw_profiles = data.get("profiles")
    profiles_map: dict[str, Any] = raw_profiles if isinstance(raw_profiles, dict) else {}
    ref_bad = False
    for index, raw in enumerate(raw_devices):
        if not isinstance(raw, dict):
            continue
        where = str(raw.get("id") or f"devices[{index}]")
        if "profile" in raw and raw["profile"] is not None:
            if not isinstance(raw["profile"], str):
                problems.append(f"{where}: profile {raw['profile']!r} must be text")
                ref_bad = True
            elif raw["profile"] not in profiles_map:
                problems.append(f"{where}: profile '{raw['profile']}' is not in profiles{hint(raw['profile'], profiles_map)}")
                ref_bad = True
    if not profile_problems and not ref_bad:
        resolved_data = resolve_profiles(data)
    else:
        resolved_data = data

    devices = resolved_data.get("devices") or []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            problems.append(f"devices[{index}]: must be a mapping with at least id, room and kind")
            continue
        where = str(device.get("id") or f"devices[{index}]")
        for key in unknown(device_keys, device):
            problems.append(f"{where}: unknown key '{key}'{hint(key, device_keys)}")
        wrong = [key for key, declaration in DEVICE.items()
                 if declaration.kind == TEXT and device.get(key) is not None and not of_kind(declaration, device[key])]
        for key in wrong:
            if key == "profile" and ref_bad:
                continue
            problems.append(f"{where}: {key} {device[key]!r} must be text")
        if wrong:
            continue
        # A device reached through a gateway (IR, serial, relay) has no
        # address of its own; it names the gateway instead. A MAC is only
        # for dhcp-reservations and discover, which name devices lacking one.
        for key in (*(key for key, declaration in DEVICE.items() if declaration.required),
                    *(() if device.get("via") else ("ip",))):
            if not device.get(key):
                problems.append(f"{where}: missing {key}")
        if device.get("id"):
            if not ID.match(device["id"]):
                problems.append(f"{where}: id is not snake_case")
            unique("id", device["id"], where)
        if device.get("room") and device["room"] not in rooms:
            problems.append(f"{where}: room '{device['room']}' is not in rooms{hint(device['room'], rooms)}")
        if device.get("kind") and device["kind"] not in KINDS:
            problems.append(f"{where}: unknown kind '{device['kind']}'{hint(device['kind'], KINDS)} "
                            "(lib/oh_semantic.py KINDS)")
        if device.get("ip"):
            try:
                ipaddress.IPv4Address(device["ip"])
            except ValueError:
                problems.append(f"{where}: ip '{device['ip']}' is not IPv4")
            unique("ip", device["ip"], where)
        if device.get("mac"):
            if not MAC.match(device["mac"]):
                problems.append(
                    f"{where}: mac '{device['mac']}' is not lowercase aa:bb:..")
            unique("mac", device["mac"], where)
        if device.get("monitor") is not None and not fits(DEVICE["monitor"], device["monitor"]):
            problems.append(f"{where}: monitor must be {words(DEVICE['monitor'])}")
        if device.get("timing") is not None:
            problems += _validate_timing(device["timing"], where)
        if "config" in device and device["config"] is not None:
            cfg = device["config"]
            if not isinstance(cfg, dict):
                problems.append(f"{where}.config: must be a mapping")
            else:
                for integ in unknown(config_keys, cfg):
                    problems.append(f"{where}.config: unknown integration '{integ}'{hint(integ, config_keys)}")
                for integ, sub in cfg.items():
                    if integ in config_keys and sub is not None and not isinstance(sub, dict):
                        problems.append(f"{where}.config.{integ}: must be a mapping")
        controls = device.get("controls")
        if controls is None:
            controls = {}
        if not of_kind(DEVICE["controls"], controls):
            problems.append(f"{where}: controls must be {DEVICE['controls'].must}")
            continue
        for phase, names in controls.items():
            if phase not in allowed:
                problems.append(f"{where}: unknown controls phase '{phase}'{hint(phase, allowed)}")
                continue
            if names is not None and not of_kind(INTEGRATION_NAMES, names):
                problems.append(f"{where}: controls.{phase} must be {INTEGRATION_NAMES.must}")
                continue
            for name in names or []:
                if not isinstance(name, str) or name not in allowed[phase]:
                    other = [p for p in allowed if p != phase and isinstance(name, str) and name in allowed[p]]
                    problems.append(f"{where}: unknown {phase} integration {name!r}"
                                    + (f"; it is a {other[0]} integration" if other else hint(name, allowed[phase])))
                elif name in needs_mac and not device.get("mac"):
                    problems.append(f"{where}: {name} needs the device's mac")
    by_id = {d["id"]: d for d in devices if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"]}
    for device in devices:
        if not isinstance(device, dict) or not device.get("via"):
            continue
        via = device["via"]
        if not isinstance(via, str):
            continue
        ident = device.get("id")
        if via == ident:
            problems.append(f"{ident}: via '{via}' cannot refer to itself")
        elif via not in seen["id"]:
            problems.append(f"{ident}: via '{via}' is not a device id{hint(via, seen['id'])}")
    for ident in by_id:
        trail = [ident]
        while isinstance(by_id[trail[-1]].get("via"), str) and by_id[trail[-1]]["via"] in by_id:
            nxt = by_id[trail[-1]]["via"]
            trail.append(nxt)
            if nxt in trail[:-1]:
                if nxt == ident and len(trail) > 2:
                    problems.append(f"{ident}: via forms a cycle: {' -> '.join(trail)}")
                break
    problems += oh_control4.validate(resolved_data)
    for index, entry in enumerate(resolved_data.get("later") or []):
        if not isinstance(entry, dict):
            problems.append(f"later[{index}]: must be a mapping with at least an id")
        elif entry.get("id"):
            unique("id", str(entry["id"]), f"later {entry['id']}")
    for index, service in enumerate(resolved_data.get("services") or []):
        if not isinstance(service, str):
            problems.append(f"services[{index}]: {service!r} must be a service id")
    if check_extensions:
        problems += extension_mismatches(resolved_data)
    return problems


def reservations(house: House) -> list[tuple[str, str, str]]:
    """(UniFi alias, MAC, IP) for every device that has its own address and a MAC."""
    rows = [(d["id"].replace("_", "-"), d["mac"], d["ip"])
            for d in house.devices if d.get("ip") and d.get("mac")]
    return sorted(rows, key=lambda row: tuple(int(part) for part in row[2].split(".")))


def without_mac(house: House) -> list[str]:
    """Ids of devices with their own address but no mac: no reservation, and
    discover cannot match them."""
    return sorted(d["id"] for d in house.devices if d.get("ip") and not d.get("mac"))


def addresses(value: object) -> set[str]:
    """Every IPv4 address appearing anywhere in a Thing definition."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found |= addresses(key) | addresses(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            found |= addresses(inner)
    elif isinstance(value, str):
        found |= set(IPV4.findall(value))
    return found


#: Items of the whole house rather than of a room or a device that the
#: house-wide All off (oh_features.all_off) and routing render; integrations
#: and other features declare theirs (HOUSE_ITEMS).
HOUSE_ITEMS = frozenset({"house_all_off", "house_routing_io_events", "house_routing_io_status"})


def cross_check(house: House, things: dict, items: dict) -> list[str]:
    """Problems where a conf/ disagrees with the house it was built from:
    its Things by UID and Items by name, each with the file that defines
    it and its body (oh_render.objects, whose build() holds every conf to
    this)."""
    # Runtime-only: providers import inventory through oh_conf.
    import oh_features
    from oh_integrations import REGISTRY, declared, defining

    devices = house.by_id
    house_items = HOUSE_ITEMS | frozenset(declared("HOUSE_ITEMS", (*REGISTRY, *oh_features.REGISTRY)))
    services = set(house.data.get("services") or [])
    routed = house.data.get("routing")
    routed_rooms = routed.get("rooms") if isinstance(routed, dict) else None
    rooms = set(house.rooms) | set(routed_rooms if isinstance(routed_rooms, dict) else {})
    problems: list[str] = []
    with_things: set[str] = set()
    for uid, thing in sorted(things.items()):
        last = uid.split(":")[-1]
        if last in services:
            continue
        device = devices.get(last)
        if device is None:
            problems.append(
                f"{thing['file']}: Thing {uid}: '{last}' is not a device id in "
                "instance/devices.yaml")
            continue
        with_things.add(last)
        via = device.get("via")
        home = device.get("ip") or (devices[via].get("ip") if via in devices else None)
        own = {home} if home else set()
        allowed = set(own)
        owners = 0
        for module in defining("transport_addresses"):
            try:
                stated = module.transport_addresses(device)
                if uid not in stated:
                    continue
                values = stated[uid]
                if (not isinstance(values, (list, tuple, set, frozenset)) or not values
                        or any(not isinstance(value, str) or str(ipaddress.IPv4Address(value)) != value for value in values)):
                    raise ValueError("invalid transport addresses")
                owners += 1
                allowed = set(values)
            except (ValueError, OSError):
                problems.append(f"{thing['file']}: Thing {uid}: invalid {module.NAME} transport-address declaration")
        if owners > 1:
            problems.append(f"{thing['file']}: Thing {uid}: conflicting transport-address declarations")
            allowed = set(own)
        for address in sorted(addresses(thing["body"]) - allowed):
            problems.append(
                f"{thing['file']}: Thing {uid} uses {address}, but "
                + (f"its approved transport addresses are {sorted(allowed)}" if owners == 1 else f"{last} is at {home}"))
    network_devices = {uid.rsplit(":", 1)[-1] for uid in things if uid.startswith("network:")}
    for device_id, device in sorted(devices.items()):
        observe = (device.get("controls") or {}).get("observe") or []
        if "network_ping" in observe and device_id not in network_devices:
            problems.append(f"{device_id}: observes network_ping but has no "
                            "network Thing")

    def _matches_prefix(candidate: str, ids: set[str] | dict[str, Device]) -> bool:
        if candidate in ids:
            return True
        pos = candidate.find("_")
        while pos != -1:
            if candidate[:pos] in ids:
                return True
            pos = candidate.find("_", pos + 1)
        return False

    for name, item in sorted(items.items()):
        if name.startswith("loc_") and _matches_prefix(name[4:], rooms):
            continue
        if name in house_items or _matches_prefix(name, devices):
            continue
        problems.append(f"{item['file']}: Item {name} is neither loc_<room>[_<point>], "
                        "named after a device id, nor one of HOUSE_ITEMS")
    for device_id in sorted(with_things - set(items)):
        problems.append(f"{device_id}: has Things but no equipment group Item "
                        f"named {device_id}")
    return problems
