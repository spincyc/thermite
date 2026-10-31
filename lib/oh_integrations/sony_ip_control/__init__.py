"""Sony BRAVIA TVs over Sony's IP control, with its "Normal" authentication:
power, HDMI input, volume, mute, the remote's keys and pairing.

sony_rest reads a TV's power, which needs no authentication. A TV that also
lists sony_ip_control under control is commanded by a rule:
conf/automation/js/sony_ip_control.js, sony_ip_control.js beside this file
with its tables filled in. It turns commands to the TV's Items into the
TV's own requests (system setPowerStatus, avContent setPlayContent, audio
setAudioVolume and setAudioMute, and IRCC for the remote's keys) and reads
them back at once, and every 15 seconds it reads the input, volume and mute
of each TV that is on.

The TV takes those requests only from a client it has registered. Pairing
(the Pairing page, web/remote/pair.html, or bin/thermite pair <id>) asks it
to register openHAB (accessControl actRegister, without a PIN): the TV shows
four digits. The same request with those digits as HTTP Basic credentials
registers openHAB, and the TV answers with an `auth` cookie, good for about
two weeks, that every later request carries. The rule keeps the client id,
the PIN and the cookie with its expiry in openHAB's userdata
(USERDATA_DIR/<id>.json, mode 0600, in a 0700 directory), never in Items,
conf/, Git or the log, so an instance with a new userdata pairs again.
Before the cookie runs out, or when the TV stops taking it, the rule
registers again with the stored PIN, as pybravia does, for a new cookie
without a new PIN on screen; a TV that refuses that is unpaired until it is
paired again. On the TV: Settings, Network (and Internet), Home network, IP
control: Authentication "Normal", and Remote Start on so that it answers,
and turns on, from standby (the menu path varies by model).

Items, per TV under control:

- `<id>_power` (sony_rest's Switch): commanded ON and OFF; read by
  sony_rest's HTTP Thing, and by the rule after a command.
- `<id>_input` (String): hdmi1 to hdmi4 while the TV shows that HDMI input,
  tv while it shows its own apps, home screen or tuner, UNDEF in standby or
  unread; commanded with hdmi1 to hdmi4.
- `<id>_volume` (Dimmer, 0-100) and `<id>_mute` (Switch): the TV speaker's
  volume and mute as it reports them, UNDEF in standby; a volume command is
  held to the TV's volume_max (devices.yaml; default DEFAULT_VOLUME_MAX).
  `sony_speaker_audio: false` omits these Items and their commands and
  readback when a TV's speaker API is unavailable. It defaults to true and
  does not affect a room's separate audio device, input, power or IRCC keys.
- `<id>_key` (String): a remote button id of the sony_tv remote
  (web/remote/remotes.js); the rule sends its IRCC code (KEYS); any other
  id is dropped.
- `<id>_pairing` and `<id>_pairing_status` (oh_integrations._pairing):
  REQUEST makes the TV show four digits, the digits sent back pair it.
- `<id>_pairing_result` (String): a fresh result id, final status and safe
  registration failure detail for each pairing command, without its PIN.
  Clients wait for this acknowledgement so a queued command cannot be
  mistaken for an earlier attempt. HTTP 200 with a Sony API error fails;
  error 1 is shown as Internal Server Error. Raw TV messages are never copied.
  Clients keep the last failure visible after its transient failed status
  expires; starting another attempt or a successful pairing clears it.
- `<id>_problem` (String): why openHAB cannot command the TV (not paired,
  a registration it refuses, no answer), as the remote page shows it after
  the TV's label; UNDEF while nothing is wrong (problem()).

A room whose display the TV is (oh_features.routing) powers and switches it
through these (stage()). The rule holds oh_conf.RULE_SENDS, so
oh_render.read_only() makes it read the TVs and send them nothing, pairing
and renewing included.
"""

from __future__ import annotations

import json
from pathlib import Path

from oh_conf import RULE_SENDS, Addon, House, YamlFile, options_text, retarget, uses

from .. import _pairing, sony_rest

NAME = sony_rest.CONTROLLER
PHASE = "control"
FILE = "yaml/sony_ip_control.yaml"
HEADER = (
    "Sony BRAVIA TVs over Sony's IP control, paired from the Pairing page: the",
    "rule conf/automation/js/sony_ip_control.js turns the power, input, volume,",
    "mute, key and pairing commands into the TV's requests, with the cookie the",
    "TV gave it, which it keeps in userdata, never in conf/. autoupdate is off:",
    "the Items show only what the TV reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the Sony TV rule (sony_ip_control.js)"),)
ASSETS: dict[str, str] = {}
#: The modules whose files the rule needs beside its own: sony_rest's Thing
#: reads the power Item the rule commands.
NEEDS = (sony_rest,)
#: Seconds from a command to its read-back: none, the rule reads it back at once.
READ_BACK = 0
RULE = "automation/js/sony_ip_control.js"
SOURCE = Path(__file__).resolve().parent / "sony_ip_control.js"
DEVICES_MARK = "const DEVICES = null;"
PRESSES_MARK = "const PRESSES = null;"
#: Where below openHAB's userdata the rule keeps each TV's registration:
#: USERDATA_DIR/<id>.json.
USERDATA_DIR = "sony_ip_control"
#: The HDMI inputs every one of these TVs has.
HDMI = (1, 2, 3, 4)
#: (value, label) of every input a command may name.
INPUTS = tuple((f"hdmi{n}", f"HDMI {n}") for n in HDMI)
#: The input state while the TV shows something of its own.
OWN = ("tv", "TV (apps, home, tuner)")
#: The TV's volume scale, as getVolumeInformation reports it.
MAX_VOLUME = 100
#: The volume a TV is held to unless devices.yaml sets volume_max.
DEFAULT_VOLUME_MAX = 40
#: The remote layout (web/remote/remotes.js) these TVs are drawn with.
REMOTE = "sony_tv"
#: Remote button id -> the IRCC command it sends, by the name the TV's
#: system getRemoteControllerInfo gives it. power is an Item of its own,
#: never a key; volume and mute are Items too.
IRCC_NAMES = {
    "home": "Home",
    "settings": "AndroidMenu",
    "input": "TvInput",
    "guide": "GGuide",
    "info": "Display",
    "action_menu": "ActionMenu",
    "back": "Return",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "ok": "Confirm",
    "rewind": "Rewind",
    "play": "Play",
    "pause": "Pause",
    "fast_forward": "Forward",
    "previous": "Prev",
    "next": "Next",
    "stop": "Stop",
    "channel_up": "ChannelUp",
    "channel_down": "ChannelDown",
    "tv": "Tv",
    "exit": "Exit",
    "options": "Options",
    "audio": "MediaAudioTrack",
    "subtitle": "SubTitle",
    "netflix": "Netflix",
    "youtube": "YouTube",
    **{f"digit_{n}": f"Num{n}" for n in range(10)},
    "digit_dot": "DOT",
    "digit_enter": "Enter",
    "red": "Red",
    "green": "Green",
    "yellow": "Yellow",
    "blue": "Blue",
    "help": "Help",
}
#: The IRCC codes of those commands, the same on all six of the house's
#: TVs (XBR-43X800E, XR-75X95J, XBR-85X900F, XR-77A80L, K-85XR90 and
#: XBR-75X950G, read with getRemoteControllerInfo on 2026-10-30).
IRCC_CODES = {
    "Home": "AAAAAQAAAAEAAABgAw==",
    "AndroidMenu": "AAAAAgAAAMQAAABPAw==",
    "TvInput": "AAAAAQAAAAEAAAAlAw==",
    "GGuide": "AAAAAQAAAAEAAAAOAw==",
    "Display": "AAAAAQAAAAEAAAA6Aw==",
    "ActionMenu": "AAAAAgAAAMQAAABLAw==",
    "Return": "AAAAAgAAAJcAAAAjAw==",
    "Up": "AAAAAQAAAAEAAAB0Aw==",
    "Down": "AAAAAQAAAAEAAAB1Aw==",
    "Left": "AAAAAQAAAAEAAAA0Aw==",
    "Right": "AAAAAQAAAAEAAAAzAw==",
    "Confirm": "AAAAAQAAAAEAAABlAw==",
    "Rewind": "AAAAAgAAAJcAAAAbAw==",
    "Play": "AAAAAgAAAJcAAAAaAw==",
    "Pause": "AAAAAgAAAJcAAAAZAw==",
    "Forward": "AAAAAgAAAJcAAAAcAw==",
    "Prev": "AAAAAgAAAJcAAAA8Aw==",
    "Next": "AAAAAgAAAJcAAAA9Aw==",
    "Stop": "AAAAAgAAAJcAAAAYAw==",
    "ChannelUp": "AAAAAQAAAAEAAAAQAw==",
    "ChannelDown": "AAAAAQAAAAEAAAARAw==",
    "Tv": "AAAAAQAAAAEAAAAkAw==",
    "Exit": "AAAAAQAAAAEAAABjAw==",
    "Options": "AAAAAgAAAJcAAAA2Aw==",
    "MediaAudioTrack": "AAAAAQAAAAEAAAAXAw==",
    "SubTitle": "AAAAAgAAAJcAAAAoAw==",
    "Netflix": "AAAAAgAAABoAAAB8Aw==",
    "YouTube": "AAAAAgAAAMQAAABHAw==",
    "Num0": "AAAAAQAAAAEAAAAJAw==",
    "Num1": "AAAAAQAAAAEAAAAAAw==",
    "Num2": "AAAAAQAAAAEAAAABAw==",
    "Num3": "AAAAAQAAAAEAAAACAw==",
    "Num4": "AAAAAQAAAAEAAAADAw==",
    "Num5": "AAAAAQAAAAEAAAAEAw==",
    "Num6": "AAAAAQAAAAEAAAAFAw==",
    "Num7": "AAAAAQAAAAEAAAAGAw==",
    "Num8": "AAAAAQAAAAEAAAAHAw==",
    "Num9": "AAAAAQAAAAEAAAAIAw==",
    "DOT": "AAAAAgAAAJcAAAAdAw==",
    "Enter": "AAAAAQAAAAEAAAALAw==",
    "Red": "AAAAAgAAAJcAAAAlAw==",
    "Green": "AAAAAgAAAJcAAAAmAw==",
    "Yellow": "AAAAAgAAAJcAAAAnAw==",
    "Blue": "AAAAAgAAAJcAAAAkAw==",
    "Help": "AAAAAgAAAMQAAABNAw==",
}
#: Remote button id -> the IRCC code the rule sends.
KEYS = {button: IRCC_CODES[name] for button, name in IRCC_NAMES.items()}
#: The Pairing page's description of a Sony TV (oh_integrations._pairing),
#: but its label (pairing_label()). Registering again is safe: a paired TV
#: keeps its cookie until the new one comes.
PAIRING = {
    "length": 4,
    "chars": "digits",
    "again": True,
    "hint": ("No code? Turn the TV on. Check Control remotely is on (Settings, Network & Internet, "
             "Remote device settings) and IP control Authentication is Normal (Home network, IP control). "
             "If already enabled, restart the TV and ask again. Menu names vary by model."),
}


def volume_max(device: dict) -> int:
    """The highest volume the TV may be set to."""
    return device.get("volume_max", DEFAULT_VOLUME_MAX)


def speaker_audio(device: dict) -> bool:
    """Whether this TV exposes its own speaker volume/mute controls and reads."""
    return device.get("sony_speaker_audio", True)


def pairing_label(device: dict) -> str:
    """What the Pairing page calls the TV."""
    return f"{device['make']} TV" if device.get("make") else "TV"


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    ident = device.get("id")
    problems = []
    if device.get("kind") != "tv":
        problems.append(f"{ident}: {NAME} drives a TV, not a {device.get('kind')}")
    if not uses(device, sony_rest.PHASE, sony_rest.NAME):
        problems.append(f"{ident}: {NAME} under control needs {sony_rest.NAME} under {sony_rest.PHASE}, "
                        "which reads its power")
    if uses(device, "control", "androidtv_googletv"):
        problems.append(f"{ident}: {NAME} drives its power, volume, mute and keys; androidtv_googletv would too")
    cap = device.get("volume_max")
    if cap is not None and not (isinstance(cap, int) and not isinstance(cap, bool) and 0 <= cap <= MAX_VOLUME):
        problems.append(f"{ident}: volume_max {cap!r} is not a whole number 0-{MAX_VOLUME}")
    if not isinstance(speaker_audio(device), bool):
        problems.append(f"{ident}: sony_speaker_audio must be true or false")
    return problems


def _control(ident: str, type_: str, label: str, tags: list[str], *, icon: str | None = None,
             metadata: dict | None = None) -> dict:
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[ident], tags=tags, autoupdate=False)
    if metadata:
        item["metadata"] = metadata
    return item


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    items = out.items
    items[f"{ident}_input"] = _control(ident, "String", "Input", ["Control", "Channel"], icon="video", metadata={
        "stateDescription": {"config": {"options": options_text((*INPUTS, OWN))}},
        "commandDescription": {"config": {"options": options_text(INPUTS)}}})
    if speaker_audio(device):
        items[f"{ident}_volume"] = _control(ident, "Dimmer", "Volume", ["Control", "SoundVolume"], icon="soundvolume",
                                            metadata={"stateDescription": {"config": {"min": 0,
                                                                                      "max": volume_max(device)}}})
        items[f"{ident}_mute"] = _control(ident, "Switch", "Mute", ["Control", "SoundVolume"], icon="soundvolume_mute")
    items[f"{ident}_key"] = _control(ident, "String", "Remote Key", ["Control"])
    _pairing.render(device, house, out, NAME, label=pairing_label(device), result=pairing_result(device), **PAIRING)
    items[pairing_result(device)] = {"type": "String", "label": "Pairing result", "groups": [ident],
                                     "tags": ["Status"], "autoupdate": False,
                                     "metadata": {"stateDescription": {"config": {"readOnly": True}}}}
    items[problem(device)] = {"type": "String", "label": "Problem", "icon": "error", "groups": [ident],
                              "tags": ["Status"]}


def pairing_result(device: dict) -> str:
    """A unique, credential-free acknowledgement of each completed pairing command."""
    return f"{device['id']}_pairing_result"


def problem(device: dict) -> str | None:
    """The Item that says why openHAB cannot command the TV, for the remote
    page (web/remote/house.js devices.<id>.problem); None for a TV this
    does not command."""
    return f"{device['id']}_problem" if uses(device, PHASE, NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: address, inputs and optional speaker controls."""
    return [{"id": d["id"], "address": d["ip"], "inputs": list(HDMI), "max": volume_max(d),
             "speakerAudio": speaker_audio(d)} for d in devices]


def _line(name: str, value: object) -> str:
    return f"const {name} = {json.dumps(value, separators=(',', ':'))};"


def rule(devices: list[dict]) -> str:
    """sony_ip_control.js with its tables filled in."""
    source = SOURCE.read_text(encoding="utf-8")
    for mark in (DEVICES_MARK, PRESSES_MARK, RULE_SENDS):
        if source.split("\n").count(mark) != 1:
            raise ValueError(f"{SOURCE} must contain the line {mark!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[3])
    header = (f"// Generated by bin/thermite generate from {origin} and\n"
              "// instance/devices.yaml; do not edit.\n")
    return header + (source.replace(DEVICES_MARK, _line("DEVICES", table(devices)))
                     .replace(PRESSES_MARK, _line("PRESSES", KEYS)))


def files(devices: list[dict], house: House) -> dict[str, str]:
    """The conf files rendered from all of this integration's devices."""
    return {RULE: rule(devices)}


def stage(device: dict, zone: int | None) -> dict | None:
    """The TV's power and input Items and its inputs, for a room whose
    display it is (oh_features.routing); it has no zones."""
    if zone is not None:
        return None
    return {"power": sony_rest.power_item(device), "input": f"{device['id']}_input",
            "inputs": [value for value, _ in INPUTS]}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, PHASE, NAME):
        return None
    ident = device["id"]
    control = {"key": f"{ident}_key", "power": sony_rest.power_item(device)}
    if speaker_audio(device):
        control.update(mute=f"{ident}_mute",
                       volume={"item": f"{ident}_volume", "max": volume_max(device), "step": 1, "unit": "", "offset": 0})
    return control


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule and sony_rest's Thing talking to the TV at
    HOST:PORT instead."""
    prefix = "const DEVICES = "
    lines = files[RULE].split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    entries = json.loads(lines[index][len(prefix):-1])
    matching = [entry for entry in entries if entry["id"] == device["id"]]
    if not matching:
        raise ValueError(f"{RULE} has no device {device['id']}")
    for entry in matching:
        entry["address"] = address
    lines[index] = _line("DEVICES", entries)

    def change(thing: dict) -> None:
        thing["config"]["baseURL"] = sony_rest.base_url(address)
    out = {**files, RULE: "\n".join(lines)}
    if sony_rest.FILE in out:
        out[sony_rest.FILE] = retarget(out[sony_rest.FILE], sony_rest.FILE, sony_rest.thing_uid(device), change)
    return out
