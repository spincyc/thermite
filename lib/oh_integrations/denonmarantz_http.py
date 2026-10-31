"""Denon and Marantz receivers over their HTTP API (denonmarantz binding).

Items, per receiver:

- `<id>_main_power` (Switch), `<id>_main_volume` (Dimmer: the receiver's own
  0-98 scale, 80 being 0 dB), `<id>_main_input` (String: an SI source code,
  INPUTS), `<id>_zone2_power` (Switch) and `<id>_zone2_input` (String: the
  same codes, or SOURCE while zone 2 plays the main zone's input).
- Under control, `<id>_main_mute` (Switch) and `<id>_key` (String: a remote
  button id, KEYS) too.
- On a verified SIGNAL_MODELS model, read-only `<id>_audio_signal` and
  `<id>_video_signal` String snapshots poll the advertised GetAudioInfo and
  GetVideoInfo methods separately from the control binding, every 10 s.
  They describe the main zone only, with controller acquisition timestamps.

Under observe only, every Item is display-only (oh_conf.guard). Under control
the Items command the receiver, and four links run a transform:JS profile:
the volume's holds every command to the receiver's volume_max (devices.yaml,
in the Item's units; default DEFAULT_VOLUME_MAX) and drops INCREASE and
DECREASE, which the binding would send as MVUP and MVDOWN with no limit; the
two inputs' send only an INPUTS value; the key's turns a button id into its
receiver command on the general#command channel and drops any other. A
room's receiver in the routing is one zone of it (stage()): routing.js
powers it and sets its input. The binding has no volume
limit of its own: it fixes mainVolumeMax at 98 and offers no Thing
parameter for it.
"""

from __future__ import annotations

import json

from oh_conf import GUARD_PROFILE, Addon, House, YamlFile, options_text, point, retarget, uses

from . import _diagnostics as diag

NAME = "denonmarantz_http"
PHASE = "observe"
CONTROL = True
FILE = "yaml/marantz.yaml"
HEADER = (
    "Marantz receivers over the HTTP API only. Each accepts one telnet",
    "client at a time and Control4 may hold it, so telnetEnabled is false;",
    "set explicitly, because an unset value makes the binding auto-detect and",
    "fall back to telnet. Models from 2016 on serve the API on port 8080.",
    "A receiver under control has its main zone commanded: its volume through",
    "a profile that holds it to volume_max, its input and remote keys",
    "through profiles that send only known values.",
)
ADDONS = (Addon("binding", "denonmarantz"), Addon("binding", "http"),
          Addon("automation", "jsscripting", "read-only receiver signal snapshots"))
ASSETS: dict[str, str] = {}
#: Seconds between the binding's polls of the HTTP API, the shortest it
#: takes (a Thing polling more often goes OFFLINE as a configuration error).
#: It reads nothing after a command, so a command is read back at the next
#: poll: READ_BACK. Telnet would report at once, but Control4 may hold the
#: receiver's one telnet connection.
POLL = 5
READ_BACK = POLL
#: The binding's zoneCount: the main zone plus up to three more.
ZONES = range(1, 5)
DEFAULT_ZONES = 2
#: The receiver's volume scale, 0-98 in steps of 0.5; 80 is 0 dB.
MAX_VOLUME = 98
DB_OFFSET = -80
#: The volume a receiver under control is held to unless devices.yaml sets
#: volume_max: -10 dB.
DEFAULT_VOLUME_MAX = 70
#: (SI source code, label) of every input the remote offers, as the
#: CINEMA 60 and CINEMA 30 name them (Deviceinfo.xml).
INPUTS = (
    ("SAT/CBL", "CBL/SAT"), ("MPLAY", "Media Player"), ("BD", "Blu-ray"), ("GAME1", "Game"),
    ("AUX1", "AUX1"), ("AUX2", "AUX2"), ("TV", "TV Audio"), ("CD", "CD"), ("PHONO", "Phono"),
    ("TUNER", "Tuner"), ("NET", "HEOS Music"), ("BT", "Bluetooth"),
)
#: Models with more than INPUTS' one game input, by devices.yaml model.
GAME_INPUTS = {"CINEMA 30": (("GAME1", "Game 1"), ("GAME2", "Game 2"))}
#: A source the receiver may report under another name than the one sent.
INPUT_ALIASES = {"GAME": "GAME1"}
#: The input of a zone other than the main one that plays whatever the main
#: zone plays: such a zone reports it, and takes it as a command (Z2SOURCE).
ZONE_SOURCE = ("SOURCE", "Main Zone Source")
#: Remote button id (web/remote/remotes.js) -> receiver command (Denon AVR
#: control protocol). power and input are Items of their own, never keys;
#: any other id, previous, play_pause and next among them, is dropped (from
#: 2022 the receivers take network transport over the HEOS CLI only).
#: sound_pure is Direct, what the receiver's own PURE button selects first:
#: Pure Direct (MSPURE DIRECT) also darkens the display, and the binding
#: sends a command's space as '+', which no one has shown these models take.
KEYS = {
    "back": "MNRTN",
    "settings": "MNMEN ON",
    "up": "MNCUP",
    "down": "MNCDN",
    "left": "MNCLT",
    "right": "MNCRT",
    "ok": "MNENT",
    "info": "MNINF",
    "options": "MNOPT",
    "sound_movie": "MSMOVIE",
    "sound_music": "MSMUSIC",
    "sound_game": "MSGAME",
    "sound_pure": "MSDIRECT",
}

# Only models whose advertised AppCommand0300 methods and response fields
# have been checked. Older receivers may not implement these read APIs.
SIGNAL_MODELS = frozenset({"CINEMA 60", "CINEMA 30"})
SIGNAL_POLL = 10
SIGNAL_PATH = "/goform/AppCommand0300.xml"
SIGNAL_FIELDS = {
    "audio": ("GetAudioInfo", {"inputmode": ("audio", "Audio input mode"),
                              "output": ("audio", "Audio destination"),
                              "signal": ("audio", "Input audio format"),
                              "sound": ("processing", "Sound mode"),
                              "fs": ("audio", "Sample rate")}),
    "video": ("GetVideoInfo", {"videooutput": ("processing", "HDMI output selection"),
                              "hdmisigin": ("video", "Input video"),
                              "hdmisigout": ("video", "Output video")}),
}


def signal_body(kind: str) -> str:
    """Exactly the advertised read-only request's known parameters."""
    method, fields = SIGNAL_FIELDS[kind]
    params = "\n".join(f'   <param name="{name}"></param>' for name in fields)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<tx>\n <cmd id="3">\n'
            f'  <name>{method}</name>\n  <list>\n{params}\n  </list>\n </cmd>\n</tx>\n')


def signal_js(kind: str) -> str:
    method, fields = SIGNAL_FIELDS[kind]
    return diag.xml_snapshot(method, list(fields))


def files(devices: list[dict], house: House) -> dict[str, str]:
    if not any(diagnostics(device) for device in devices):
        return {}
    return {f"transform/marantz-{kind}-signal.js": diag.script_file(signal_js(kind)) for kind in SIGNAL_FIELDS}


def diagnostics(device: dict) -> dict | None:
    """Main-zone measured formats and processing settings; no zone-2 claim."""
    if device.get("model") not in SIGNAL_MODELS:
        return None
    items = [f"{device['id']}_{kind}_signal" for kind in SIGNAL_FIELDS]
    readings = [{"category": category, "label": label, "item": f"{device['id']}_{kind}_signal",
                 "path": ["values", field], "zone": "main"}
                for kind, (_, fields) in SIGNAL_FIELDS.items() for field, (category, label) in fields.items()]
    return {"kind": NAME, "items": items, "maxAgeSeconds": 30, "readings": readings}


def _render_signal(device: dict, house: House, out: YamlFile) -> None:
    if diagnostics(device) is None:
        return
    uid = f"http:url:{device['id']}"
    out.things[uid] = {
        "label": f"{house.label(device)} read-only signal API",
        "location": house.room_label(device["room"]),
        "config": {"baseURL": f"http://{device['ip']}:8080{SIGNAL_PATH}", "refresh": SIGNAL_POLL,
                   "timeout": 3000, "delay": 100, "stateMethod": "POST", "contentType": "application/xml",
                   "strictErrorHandling": True},
        "channels": {kind: diag.channel(f"Main-zone {kind} snapshot", f"marantz-{kind}-signal.js",
                                       stateContent=signal_body(kind))
                     for kind in SIGNAL_FIELDS},
    }
    for kind in SIGNAL_FIELDS:
        out.items[f"{device['id']}_{kind}_signal"] = diag.snapshot_item(
            device, uid, kind, f"Main-zone {kind} signal snapshot")


def thing_uid(device: dict) -> str:
    return f"denonmarantz:avr:{device['id']}"


def volume_max(device: dict) -> float:
    """The highest volume the main zone may be sent, in the Item's units."""
    return device.get("volume_max", DEFAULT_VOLUME_MAX)


def inputs(device: dict, zone: int | None = None) -> list[tuple[str, str]]:
    """(value, label) of the inputs the receiver's remote offers for the
    main zone (zone None), or for zone `zone`: those and ZONE_SOURCE."""
    games = GAME_INPUTS.get(device.get("model"))
    out: list[tuple[str, str]] = []
    for value, label in INPUTS:
        out += list(games) if games and value == "GAME1" else [(value, label)]
    return out if zone is None else [*out, ZONE_SOURCE]


def validate(device: dict) -> list[str]:
    problems = []
    zones = device.get("zones")
    if zones is not None and (not isinstance(zones, int) or isinstance(zones, bool) or zones not in ZONES):
        problems.append(f"{device.get('id')}: zones {zones!r} is not {ZONES.start}-{ZONES.stop - 1}")
    # volume_max means another scale to wiim_http, so only a receiver's is checked here.
    cap = device.get("volume_max")
    if uses(device, PHASE, NAME) and cap is not None and not (
            isinstance(cap, (int, float)) and not isinstance(cap, bool)
            and 0 <= cap <= MAX_VOLUME and float(cap * 2).is_integer()):
        problems.append(f"{device.get('id')}: volume_max {cap!r} is not 0-{MAX_VOLUME} in steps of 0.5")
    return problems


def _lookup_js(table: dict[str, str], otherwise: str = "null") -> str:
    """Inline JS: table[input] for a key of the table, else `otherwise` (JS)."""
    return (f"|(function (table) {{ return Object.prototype.hasOwnProperty.call(table, input) ? table[input] : "
            f"{otherwise}; }})({json.dumps(table, separators=(',', ':'))})")


def volume_js(cap: float) -> str:
    """Inline JS: a volume command held to `cap`; null (nothing sent) for
    anything but a number, INCREASE and DECREASE among them."""
    return f"|/^\\d+(\\.\\d+)?$/.test(input) ? String(Math.min(Number(input), {json.dumps(cap)})) : null"


def input_js(device: dict, zone: int | None = None) -> str:
    """Inline JS: an input command passed on only when it is one of the
    zone's inputs()."""
    return _lookup_js({value: value for value, _ in inputs(device, zone)})


def input_state_js() -> str:
    """Inline JS: the reported source, named as inputs() names it."""
    return _lookup_js(INPUT_ALIASES, "input")


def key_js() -> str:
    """Inline JS: a remote button id -> its receiver command; null for any other."""
    return _lookup_js(KEYS)


def _control(ident: str, type_: str, label: str, tags: list[str], channel: str, *,
             link: dict | None = None, icon: str | None = None, state: dict | None = None,
             options: str | None = None) -> dict:
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[ident], tags=tags, autoupdate=False)
    if link is None:
        item["channel"] = channel
    else:
        item["channels"] = {channel: {"profile": GUARD_PROFILE, **link}}
    metadata: dict = {}
    if state or options:
        metadata["stateDescription"] = {"config": {**(state or {}), **({"options": options} if options else {})}}
    if options:
        metadata["commandDescription"] = {"config": {"options": options}}
    if metadata:
        item["metadata"] = metadata
    return item


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = thing_uid(device)
    out.things[uid] = {
        "label": house.label(device),
        "location": house.room_label(device["room"]),
        "config": {
            "host": device["ip"],
            "telnetEnabled": False,
            "httpPort": 8080,
            "httpPollingInterval": POLL,
            "zoneCount": device.get("zones", DEFAULT_ZONES),
        },
    }
    items = out.items
    _render_signal(device, house, out)
    if not uses(device, "control", NAME):
        items[f"{ident}_main_power"] = point(ident, "Switch", "Main Zone Power", ["Status", "Power"],
                                             f"{uid}:mainZone#power", guarded=True, icon="receiver")
        items[f"{ident}_main_volume"] = point(ident, "Dimmer", "Main Zone Volume", ["Status", "SoundVolume"],
                                              f"{uid}:mainZone#volume", guarded=True, icon="soundvolume")
        items[f"{ident}_main_input"] = point(ident, "String", "Main Zone Input", ["Status", "Channel"],
                                             f"{uid}:mainZone#input", guarded=True)
        items[f"{ident}_zone2_power"] = point(ident, "Switch", "Zone 2 Power", ["Status", "Power"],
                                              f"{uid}:zone2#power", guarded=True, icon="receiver")
        items[f"{ident}_zone2_input"] = point(ident, "String", "Zone 2 Input", ["Status", "Channel"],
                                              f"{uid}:zone2#input", guarded=True)
        return
    cap = volume_max(device)
    items[f"{ident}_main_power"] = _control(ident, "Switch", "Main Zone Power", ["Control", "Power"],
                                            f"{uid}:mainZone#power", icon="receiver")
    items[f"{ident}_main_volume"] = _control(
        ident, "Dimmer", "Main Zone Volume", ["Control", "SoundVolume"], f"{uid}:mainZone#volume",
        link={"toItemScript": "|input", "commandFromItemScript": volume_js(cap)}, icon="soundvolume",
        state={"min": 0, "max": cap})
    items[f"{ident}_main_mute"] = _control(ident, "Switch", "Main Zone Mute", ["Control", "SoundVolume"],
                                           f"{uid}:mainZone#mute", icon="soundvolume_mute")
    items[f"{ident}_main_input"] = _control(
        ident, "String", "Main Zone Input", ["Control", "Channel"], f"{uid}:mainZone#input",
        link={"toItemScript": input_state_js(), "commandFromItemScript": input_js(device)},
        options=options_text(inputs(device)))
    items[f"{ident}_zone2_power"] = _control(ident, "Switch", "Zone 2 Power", ["Control", "Power"],
                                             f"{uid}:zone2#power", icon="receiver")
    items[f"{ident}_zone2_input"] = _control(
        ident, "String", "Zone 2 Input", ["Control", "Channel"], f"{uid}:zone2#input",
        link={"toItemScript": input_state_js(), "commandFromItemScript": input_js(device, 2)},
        options=options_text(inputs(device, 2)))
    # No toItemScript: the command channel's state never reaches the Item.
    items[f"{ident}_key"] = _control(ident, "String", "Remote Key", ["Control"], f"{uid}:general#command",
                                     link={"commandFromItemScript": key_js()})


def stage(device: dict, zone: int | None) -> dict | None:
    """The power and input Items of one zone (None: the main zone) and the
    inputs it takes, for a room whose receiver it is (oh_features.routing);
    None for a zone the receiver lacks."""
    ident = device["id"]
    if zone is None:
        return {"power": f"{ident}_main_power", "input": f"{ident}_main_input",
                "inputs": [value for value, _ in inputs(device)]}
    if zone == 2 and device.get("zones", DEFAULT_ZONES) >= 2:
        return {"power": f"{ident}_zone2_power", "input": f"{ident}_zone2_input",
                "inputs": [value for value, _ in inputs(device, zone)]}
    return None


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    return {
        "key": f"{ident}_key",
        "power": f"{ident}_main_power",
        "mute": f"{ident}_main_mute",
        "volume": {"item": f"{ident}_main_volume", "max": volume_max(device), "step": 1, "unit": "dB",
                   "offset": DB_OFFSET},
        "input": {"item": f"{ident}_main_input", "options": [list(pair) for pair in inputs(device)]},
    }


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the receiver's Thing talking to HOST:PORT instead."""
    host, _, port = address.rpartition(":")

    def change(thing: dict) -> None:
        thing["config"].update(host=host, httpPort=int(port))
    text = retarget(files[FILE], FILE, thing_uid(device), change)
    if diagnostics(device) is not None:
        text = retarget(text, FILE, f"http:url:{device['id']}", lambda thing: thing["config"].update(
            baseURL=f"http://{address}{SIGNAL_PATH}"))
    return {**files, FILE: text}
