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
  GetVideoInfo methods separately from the control binding, every 30 s.
  They describe the main zone only, with controller acquisition timestamps.

Under observe only, every Item is display-only (oh_conf.guard). Under control
the Items command the receiver, and four links run a transform:JS profile:
the volume's sends only a value on the receiver's own scale (VOLUME) and
drops INCREASE and DECREASE, which the binding would send as MVUP and
MVDOWN; the two inputs' send only an INPUTS value; the key's turns a button
id into its receiver command on the general#command channel and drops any
other. A room's receiver in the routing is one zone of it (stage()):
routing.js powers it and sets its input. Under the routing lifecycle the
zones' power and input are the routing executor's fields (fields()), which
a rule of the receiver's own, denonmarantz_http.js, writes and reads back
over the HTTP API (rule_path()). A volume limit set on the receiver itself
is the receiver's to apply; thermite sets none.
"""

from __future__ import annotations

import json
from pathlib import Path

from oh_conf import GUARD_PROFILE, Addon, House, YamlFile, options_text, point, retarget, uses, volume_js

from . import _diagnostics as diag
from . import _io

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
    "a profile that sends only values on the receiver's own 0-98 scale, its",
    "input and remote keys through profiles that send only known values.",
)
ADDONS = (Addon("binding", "denonmarantz"), Addon("binding", "http"),
          Addon("automation", "jsscripting", "read-only receiver signal snapshots"))
ASSETS: dict[str, str] = {}
DEVICE_KEYS = ("zones",)
#: Seconds between the binding's polls of the HTTP API. The receiver closes
#: every connection, and each poll opens one for each of its requests, so it
#: polls three times less often than the binding allows (5 s). It reads
#: nothing after a command, so a command is read back at the next poll:
#: READ_BACK, which the remote waits for (remote()'s readBack), as routing.js
#: does for a choice (routing.hold_ms()). The routing lifecycle reads power
#: and input back itself after switching them (denonmarantz_http.js).
#: Telnet would report at once, but Control4 may hold the receiver's one
#: telnet connection.
POLL = 15
#: The binding's next poll starts POLL s after the last one ended, and its
#: requests, each a new connection, take a few seconds more.
READ_BACK = POLL + 3
#: The binding's zoneCount: the main zone plus up to three more.
ZONES = range(1, 5)
DEFAULT_ZONES = 2
#: The main zone's volume as the binding's mainZone#volume channel reports
#: and takes it: the receiver's own absolute scale, 0-98 in steps of 0.5
#: (MV00-MV98, MV695 for 69.5), 80 being 0 dB. openHAB denonmarantz 5.2.3:
#: DenonMarantzConfiguration.MAX_VOLUME = 98, which the HTTP connector sets
#: as mainVolumeMax because MVMAX is only read over telnet; DenonMarantzState
#: setMainVolume() reports the scale unchanged; DenonMarantzConnector
#: percentToDenonValue() rounds to 0.5 and toDenonValue() writes MVnn[5]. A
#: CINEMA 60 and CINEMA 30 report <Max>980</Max> (tenths) for MainZone in
#: get_config?type=12 (read 2026-11-04).
VOLUME = {"min": 0, "max": 98, "step": 0.5}
#: The relative (dB) display: the absolute value plus DB_OFFSET.
DB_OFFSET = -80
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
#: stage() declares it as the zone's `follows` input.
ZONE_SOURCE = ("SOURCE", "Main Zone Source")
#: The routing actor of one receiver (rule_path()), rendered for each one
#: under control; its markers are filled in by files() and by the routing
#: lifecycle (oh_features.routing.lifecycle_io).
RULE = "automation/js/denonmarantz_http.js"
SOURCE = Path(__file__).with_suffix(".js")
ALIASES_MARK = "const INPUT_ALIASES = null;"
#: Every SI code a zone may report as its selected input (room diagnostics).
READBACK_INPUTS = tuple(dict.fromkeys([code for code, _ in INPUTS] + [code for pairs in GAME_INPUTS.values() for code, _ in pairs]
                                      + [ZONE_SOURCE[0]]))
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
SIGNAL_POLL = 30
SIGNAL_PATH = "/goform/AppCommand0300.xml"
VOLUME_DISPLAY_PATH = "/ajax/globals/get_config?type=12"
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


def rule_path(device: dict) -> str:
    """The receiver's routing actor: one script context per receiver."""
    return _io.script_path(RULE, device)


def rule() -> str:
    """The routing actor's source, its input aliases filled in."""
    source = SOURCE.read_text(encoding="utf-8")
    if source.splitlines().count(ALIASES_MARK) != 1:
        raise ValueError(f"{SOURCE} must contain the line {ALIASES_MARK!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[2])
    header = (f"// Generated by bin/thermite generate from {origin} and\n"
              "// instance/devices.yaml; do not edit.\n")
    return header + source.replace(ALIASES_MARK, "const INPUT_ALIASES = "
                                   + json.dumps(INPUT_ALIASES, separators=(",", ":")) + ";")


def files(devices: list[dict], house: House) -> dict[str, str]:
    out = {rule_path(device): rule() for device in devices if uses(device, "control", NAME)}
    if any(diagnostics(device) for device in devices):
        out.update({f"transform/marantz-{kind}-signal.js": diag.script_file(signal_js(kind)) for kind in SIGNAL_FIELDS})
        out["transform/marantz-volume-display.js"] = diag.script_file(volume_display_js())
    return out


def volume_display_uid(device: dict) -> str:
    return f"http:url:volume_display:{device['id']}"


def volume_display_js() -> str:
    """Select only the main zone's verified display enum, never a zone-2 fallback."""
    return r'''|(function () {
  if (typeof input !== 'string' || input.length > 65536 || /<!/i.test(input)) return 'UNDEF';
  var root = /^\s*(?:<\?xml[^?]*\?>\s*)?<listGlobals\b[^>]*>([\s\S]*)<\/listGlobals>\s*$/.exec(input);
  if (!root) return 'UNDEF';
  var main = root[1].match(/<MainZone\b[^>]*>[\s\S]*?<\/MainZone>/g);
  if (!main || main.length !== 1 || (root[1].match(/<MainZone\b/g) || []).length !== 1) return 'UNDEF';
  var scales = main[0].match(/<VolumeScale\b[^>]*>\s*[12]\s*<\/VolumeScale>/g);
  if (!scales || scales.length !== 1 || (main[0].match(/<VolumeScale\b/g) || []).length !== 1) return 'UNDEF';
  return />\s*1\s*</.test(scales[0]) ? 'absolute' : 'relative';
})()'''


def diagnostics(device: dict) -> dict | None:
    """Main-zone measured formats and processing settings; no zone-2 claim."""
    if device.get("model") not in SIGNAL_MODELS:
        return None
    items = [f"{device['id']}_{kind}_signal" for kind in SIGNAL_FIELDS]
    readings = [{"category": category, "label": label, "item": f"{device['id']}_{kind}_signal",
                 "path": ["values", field], "zone": "main", "media": kind}
                for kind, (_, fields) in SIGNAL_FIELDS.items() for field, (category, label) in fields.items()]
    return {"kind": NAME, "items": items, "maxAgeSeconds": SIGNAL_POLL + 5, "readings": readings}


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
    display_uid = volume_display_uid(device)
    out.things[display_uid] = {
        "label": f"{house.label(device)} read-only volume display",
        "location": house.room_label(device["room"]),
        "config": {"baseURL": f"http://{device['ip']}:11080{VOLUME_DISPLAY_PATH}", "refresh": SIGNAL_POLL,
                   "timeout": 3000, "delay": 0, "stateMethod": "GET", "strictErrorHandling": True},
        "channels": {"scale": diag.channel("Main-zone volume display", "marantz-volume-display.js")},
    }
    out.items[f"{device['id']}_volume_scale"] = point(device["id"], "String", "Volume display",
        ["Status", "SoundVolume"], f"{display_uid}:scale", guarded=True)


def thing_uid(device: dict) -> str:
    return f"denonmarantz:avr:{device['id']}"


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
    return problems


def _lookup_js(table: dict[str, str], otherwise: str = "null") -> str:
    """Inline JS: table[input] for a key of the table, else `otherwise` (JS)."""
    return (f"|(function (table) {{ return Object.prototype.hasOwnProperty.call(table, input) ? table[input] : "
            f"{otherwise}; }})({json.dumps(table, separators=(',', ':'))})")


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
    items[f"{ident}_main_power"] = _control(ident, "Switch", "Main Zone Power", ["Control", "Power"],
                                            f"{uid}:mainZone#power", icon="receiver")
    items[f"{ident}_main_volume"] = _control(
        ident, "Dimmer", "Main Zone Volume", ["Control", "SoundVolume"], f"{uid}:mainZone#volume",
        link={"toItemScript": "|input", "commandFromItemScript": volume_js(VOLUME)}, icon="soundvolume",
        state=dict(VOLUME))
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
    None for a zone the receiver lacks. Zone 2 `follows` the main zone's
    input on ZONE_SOURCE."""
    ident = device["id"]
    if zone is None:
        return {"power": f"{ident}_main_power", "input": f"{ident}_main_input",
                "inputs": [value for value, _ in inputs(device)]}
    if zone == 2 and device.get("zones", DEFAULT_ZONES) >= 2:
        return {"power": f"{ident}_zone2_power", "input": f"{ident}_zone2_input",
                "inputs": [value for value, _ in inputs(device, zone)], "follows": ZONE_SOURCE[0]}
    return None


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's fields (oh_integrations): each zone's power
    and input (stage()), which the receiver's own rule (rule_path()) writes
    and reads back over the HTTP API at the Thing's current address."""
    out = {}
    for zone in (None, 2):
        staged = stage(device, zone)
        if staged:
            for point, values in (("power", ["ON", "OFF"]), ("input", staged["inputs"])):
                out[staged[point]] = {"kind": "denon", "thing": thing_uid(device), "zone": zone or 1, "point": point,
                                      "values": list(values)}
    return out


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    control = {
        "key": f"{ident}_key",
        "power": f"{ident}_main_power",
        "mute": f"{ident}_main_mute",
        # The number the receiver shows: its absolute scale, or dB when its
        # own display is set to relative (the volume_scale Item says which).
        "volume": {"item": f"{ident}_main_volume", **VOLUME,
                   "displayMode": {"item": f"{ident}_volume_scale" if diagnostics(device) else None,
                       "modes": {"absolute": {},
                                 "relative": {"unit": "dB", "offset": DB_OFFSET, "precision": 1,
                                              "minimum": "---.- dB"}}}},
        "input": {"item": f"{ident}_main_input", "options": [list(pair) for pair in inputs(device)]},
        "readBack": READ_BACK,
    }
    from . import heos
    if heos.enabled(device):
        control["playback"] = {"item": heos.playback_item(device), "label": "HEOS playback",
                               "input": "NET", "activate": "HEOS Music"}
    return control


def command_keys(device: dict) -> dict[str, str]:
    """Composed remote capabilities; KEYS remains the receiver's wire map.

    HEOS consumes its own finite transport keys on the shared command Item.
    Site authority must include those keys when that provider is configured.
    """
    from . import heos
    return {**KEYS, **(heos.KEYS if heos.enabled(device) else {})}


def remote_of(device: dict) -> str:
    """Keep legacy receivers' controls unchanged when HEOS is not commissioned."""
    from . import heos
    return "marantz_heos" if heos.enabled(device) else "marantz_avr"


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the receiver's Thing talking to HOST:PORT instead."""
    host, _, port = address.rpartition(":")

    def change(thing: dict) -> None:
        thing["config"].update(host=host, httpPort=int(port))
    text = retarget(files[FILE], FILE, thing_uid(device), change)
    if diagnostics(device) is not None:
        text = retarget(text, FILE, f"http:url:{device['id']}", lambda thing: thing["config"].update(
            baseURL=f"http://{address}{SIGNAL_PATH}"))
        text = retarget(text, FILE, volume_display_uid(device), lambda thing: thing["config"].update(
            baseURL=f"http://{address}{VOLUME_DISPLAY_PATH}"))
    return {**files, FILE: text}
