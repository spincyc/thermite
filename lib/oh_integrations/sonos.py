"""Sonos Arc/Amp native channels; explicit control enables volume, keys and
sound settings.

Every controlled Arc or Amp has a remote (transport, Night and Speech, and
an Arc's TV audio) and an audio page (audio()): play mode (shuffle,
repeat), sound (loudness, bass, treble, night, speech; an Arc's subwoofer,
surround and height levels) and its status light, each a native channel
read back. The command rule only forwards allowlisted keys to native
channels; it never groups players or changes bonded-surround membership.
No power switch is invented.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from oh_conf import GUARD_PROFILE, Addon, House, YamlFile, options_text, point, uses, volume_js

NAME = "sonos"
PHASE = "observe"
CONTROL = True
FILE = "yaml/sonos.yaml"
HEADER = (
    "Sonos zone players that observe sonos, by UDN (found over UPnP, so",
    "openHAB must share the players' multicast domain). Bonded surrounds and",
    "subs are not zone players and get only a ping.",
)
ADDONS = (Addon("binding", "sonos"), Addon("automation", "jsscripting", "Sonos remote keys"))
ASSETS: dict[str, str] = {}
#: The binding derives a player's UDN from its MAC, so a device needs one.
NEEDS_MAC = True
DEVICE_KEYS = ("sonos_udn",)
#: The binding talks UPnP to the player's HTTP port.
PROBE_PORTS = (1400,)
DISCOVER_PORTS = (1400,)
DISCOVER_MDNS = ("_sonos",)
#: The binding subscribes to each player's UPnP events, which the sandbox
#: leaves to the runtime.
SESSIONS = True
#: Inventory model -> the binding's Thing type.
THING_TYPES = {"Arc": "Arc", "Amp": "Amp"}
#: A zone player's master volume as the binding's volume channel reports and
#: takes it: the player's own 0-100 in whole steps (UPnP RenderingControl
#: DesiredVolume). openHAB sonos 5.2.3: ZonePlayerHandler.setVolume() sends
#: DecimalType.intValue() as DesiredVolume and steps INCREASE/DECREASE by 1
#: within 0-100. A volume limit set in the Sonos app is the player's to apply.
VOLUME = {"min": 0, "max": 100, "step": 1}
#: Levels as the binding's channels report and take them, in whole steps
#: (openHAB sonos 5.2.3 channels.xml): bass, treble and height -10 to 10,
#: subwoofer and surround -15 to 15.
TONE = {"min": -10, "max": 10, "step": 1}
GAIN = {"min": -15, "max": 15, "step": 1}
#: (value, label) of the binding's repeat and surround music mode channels.
REPEAT = (("OFF", "Off"), ("ONE", "One"), ("ALL", "All"))
MUSIC_MODE = (("0", "Ambient"), ("1", "Full"))
#: Commanded settings under control: suffix -> (channel, Item type, label,
#: page section, a level's scale or a choice's options). Night and Speech
#: are the remote's keys too. Every model (THING_TYPES) has these...
SETTINGS = {"shuffle": ("shuffle", "Switch", "Shuffle", "playback", None),
            "repeat": ("repeat", "String", "Repeat", "playback", REPEAT),
            "loudness": ("loudness", "Switch", "Loudness", "sound", None),
            "bass": ("bass", "Number", "Bass", "sound", TONE),
            "treble": ("treble", "Number", "Treble", "sound", TONE),
            "night": ("nightmode", "Switch", "Night Mode", "sound", None),
            "speech": ("speechenhancement", "Switch", "Speech Enhancement", "sound", None),
            "led": ("led", "Switch", "Status Light", "device", None)}
#: ...and an Arc these too (Arc.xml; Amp.xml has none). A player with no
#: bonded subwoofer or surrounds reads them as nothing, and its page then
#: hides them.
ARC_SETTINGS = {"subwoofer": ("subwoofer", "Switch", "Subwoofer", "sound", None),
                "sub_gain": ("subwoofergain", "Number", "Subwoofer Level", "sound", GAIN),
                "surround": ("surround", "Switch", "Surround", "sound", None),
                "surround_tv": ("surroundtvlevel", "Number", "Surround TV Level", "sound", GAIN),
                "surround_music": ("surroundmusiclevel", "Number", "Surround Music Level", "sound", GAIN),
                "music_mode": ("surroundmusicmode", "String", "Surround Music Mode", "sound", MUSIC_MODE),
                "height": ("heightlevel", "Number", "Height Level", "sound", TONE)}
KEYS = {"previous": "PREVIOUS", "play_pause": "PLAY_PAUSE", "next": "NEXT", "night": "NIGHT", "speech": "SPEECH"}
#: The presses its audio page offers; Night and Speech are settings there.
TRANSPORT = ("previous", "play_pause", "next")

ARC_KEYS = {**KEYS, "tv": "TV"}
RULE = "automation/js/sonos_control.js"
SOURCE = Path(__file__).with_name("sonos_control.js")
MARK = "const DEVICES = null;"
READ_BACK = 60
#: Seconds between the controls check's stand-in polls of the settings;
#: each is read again a second after its command (refreshAfterCommand).
SETTINGS_REFRESH = 30


def settings(device: dict) -> dict:
    """Its model's commanded settings (SETTINGS, and ARC_SETTINGS on an Arc)."""
    return {**SETTINGS, **(ARC_SETTINGS if device.get("model") == "Arc" else {})}


def remote_of(device: dict) -> str:
    """An Arc's remote has its TV key; an Amp's has none (keys())."""
    return "sonos" if device.get("model") == "Arc" else "sonos_amp"


def keys(device: dict) -> dict:
    return ARC_KEYS if device.get("model") == "Arc" else KEYS


def udn(mac: str) -> str:
    """The UDN a Sonos player derives from its MAC address."""
    return "RINCON_" + mac.replace(":", "").upper() + "01400"


def relayed(device: dict) -> bool:
    """Whether the gateway relays this device's UPnP events: it has a UDN or
    lists sonos in any phase. oh_access.native_boundary.discover_callbacks
    maps these devices' event paths; oh_render points jUPnP at the relay
    when the house has any."""
    return bool(device.get("sonos_udn")) or any(NAME in (names or []) for names in (device.get("controls") or {}).values())


def validate(device: dict) -> list[str]:
    where = device.get("id") or "a device"
    problems = []
    if relayed(device):
        expected = udn(device.get("mac") or "")
        if device.get("sonos_udn") != expected:
            problems.append(f"{where}: sonos_udn should be {expected} for its mac")
    if uses(device, PHASE, NAME) and device.get("model") not in THING_TYPES:
        problems.append(f"{where}: sonos knows no Thing type for model {device.get('model')!r} "
                        f"(one of {', '.join(sorted(THING_TYPES))})")
    return problems


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = f"sonos:{THING_TYPES[device['model']]}:{ident}"
    out.things[uid] = {
        "label": house.label(device),
        "location": house.room_label(device["room"]),
        "config": {"udn": device["sonos_udn"], "refresh": 60},
    }
    items = out.items
    controlled = uses(device, "control", NAME)
    items[f"{ident}_state"] = point(ident, "String", "Playback State", ["Status", "MediaControl"],
                                    f"{uid}:state")
    items[f"{ident}_title"] = point(ident, "String", "Title", ["Status", "Info"], f"{uid}:currenttitle")
    items[f"{ident}_artist"] = point(ident, "String", "Artist", ["Status", "Info"], f"{uid}:currentartist")
    items[f"{ident}_album"] = point(ident, "String", "Album", ["Status", "Info"], f"{uid}:currentalbum")
    items[f"{ident}_volume"] = point(ident, "Dimmer", "Volume", ["Status", "SoundVolume"], f"{uid}:volume",
                                     guarded=not controlled, icon="soundvolume")
    items[f"{ident}_mute"] = point(ident, "Switch", "Mute", ["Status", "SoundVolume"], f"{uid}:mute",
                                   guarded=not controlled, icon="soundvolume_mute")
    if controlled:
        volume = items[f"{ident}_volume"]
        del volume["channel"]
        volume["channels"] = {f"{uid}:volume": {"profile": GUARD_PROFILE, "toItemScript": "|input",
                                                   "toHandlerScript": volume_js(VOLUME)}}
        volume["autoupdate"] = False
        volume["metadata"] = {"stateDescription": {"config": dict(VOLUME)}}
        items[f"{ident}_mute"]["autoupdate"] = False
        items[f"{ident}_player"] = point(ident, "Player", "Playback", ["Control", "MediaControl"], f"{uid}:control")
        items[f"{ident}_player"]["autoupdate"] = False
        for suffix, (channel, kind, label, _, values) in settings(device).items():
            items[f"{ident}_{suffix}"] = setting(ident, suffix, kind, label, f"{uid}:{channel}", values)
        if device["model"] == "Arc":
            items[f"{ident}_tv"] = point(ident, "String", "TV Audio", ["Control"], f"{uid}:playlinein")
            del items[f"{ident}_tv"]["channel"]
            expected = json.dumps(device["sonos_udn"])
            items[f"{ident}_tv"]["channels"] = {f"{uid}:playlinein": {"profile": GUARD_PROFILE,
                "toHandlerScript": f"|input === {expected} ? input : null"}}
            items[f"{ident}_tv"]["autoupdate"] = False
        items[f"{ident}_key"] = {"type": "String", "label": "Remote Key", "groups": [ident],
                                 "tags": ["Control"], "autoupdate": False}
    if device["model"] == "Arc":
        items[f"{ident}_codec"] = point(ident, "String", "TV Audio Format", ["Status", "Info"], f"{uid}:codec")
        items[f"{ident}_line_in"] = point(ident, "Switch", "TV Audio Connected", ["Status"], f"{uid}:linein")


def setting(ident: str, suffix: str, kind: str, label: str, channel: str, values) -> dict:
    """A commanded setting Item, read back. Only what its channel takes
    passes its link: a level on its scale, a choice among its options; a
    switch takes ON or OFF alone. A level is a Setpoint: oh_metrics keeps a
    Number tagged Control as a matrix's routing."""
    tags = {"Switch": ["Control", "MediaControl" if suffix in ("night", "speech") else "Enabled"],
            "String": ["Control", "Mode"], "Number": ["Setpoint", "Level"]}[kind]
    item = point(ident, kind, label, tags, channel)
    item["autoupdate"] = False
    if kind == "Switch":
        return item
    if kind == "Number":
        script = volume_js(values)
        item["metadata"] = {"stateDescription": {"config": dict(values)}}
    else:
        script = f"|{json.dumps([value for value, _ in values])}.indexOf(input) >= 0 ? input : null"
        item["metadata"] = {"stateDescription": {"config": {"options": options_text(values)}},
                            "commandDescription": {"config": {"options": options_text(values)}}}
    del item["channel"]
    item["channels"] = {channel: {"profile": GUARD_PROFILE, "toItemScript": "|input", "toHandlerScript": script}}
    return item


def files(devices: list[dict], house: House) -> dict[str, str]:
    controlled = [{"id": d["id"], "model": d["model"], "udn": d["sonos_udn"]} for d in devices
                  if uses(d, "control", NAME)]
    if not controlled:
        return {}
    return {RULE: SOURCE.read_text().replace(MARK, "const DEVICES = " + json.dumps(controlled, indent=2) + ";")}


def playing(device: dict) -> dict:
    """What it plays (oh_features.playing): its track, while PLAYING. TV
    audio and line-in have a blank title, which is nothing."""
    ident = device["id"]
    return {"titles": [{"title": f"{ident}_title", "artist": f"{ident}_artist",
                        "when": {"item": f"{ident}_state", "is": ["PLAYING"]}}]}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled. An
    Amp is never a room's endpoint (oh_site_contract targets soundbars), so
    its remote reaches no room unless a room names it its `audio`."""
    if not uses(device, "control", NAME) or device.get("model") not in THING_TYPES:
        return None
    ident = device["id"]
    return {"key": f"{ident}_key", "mute": f"{ident}_mute",
            "volume": {"item": f"{ident}_volume", **VOLUME}}


def audio(device: dict) -> dict | None:
    """Its audio page beside its remote() (oh_integrations audio()): None
    unless controlled."""
    if remote(device) is None:
        return None
    ident = device["id"]

    def shape(kind: str, values) -> dict:
        if kind == "Number":
            return {"kind": "number", **values}
        return {"kind": "enum", "options": [list(pair) for pair in values]} if kind == "String" else {"kind": "switch"}
    page = {"transport": [*TRANSPORT, *(["tv"] if device["model"] == "Arc" else [])],
            "state": {"item": f"{ident}_state", "playing": ["PLAYING"]},
            "track": {field: f"{ident}_{field}" for field in ("title", "artist", "album")},
            "settings": [{"item": f"{ident}_{suffix}", "label": label, "section": section, **shape(kind, values)}
                         for suffix, (_, kind, label, section, values) in settings(device).items()]}
    if device["model"] == "Arc":
        page["facts"] = [{"item": f"{ident}_line_in", "label": "TV Audio Connected"},
                         {"item": f"{ident}_codec", "label": "TV Audio Format"}]
    return page


def simulation_udn(device: dict) -> str:
    """A deterministic, locally administered fake identity, never a player's MAC."""
    return "RINCON_02" + hashlib.sha256(device["id"].encode()).hexdigest()[:10].upper() + "01400"


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """Test-only native Item-contract replay over a loopback HTTP stand-in.

    This is deliberately not credited as Sonos UPnP/SOAP protocol proof.
    Production render() keeps the real native Sonos channels unchanged.
    """
    ident = device["id"]
    old = f"sonos:{THING_TYPES[device['model']]}:{ident}"
    uid, settings_uid = f"http:url:{ident}", f"http:url:settings:{ident}"
    document = yaml.safe_load(files[FILE])
    thing = document["things"].pop(old)
    fields = {"state": "string", "title": "string", "artist": "string", "album": "string", "volume": "dimmer",
              "mute": "switch", "player": "string", "tv": "string", "codec": "string", "line_in": "switch"}
    # The HTTP binding posts every polled value on every refresh (the native
    # binding only on change), so the settings poll on a slower Thing of their
    # own, each read back after its command.
    setting_fields = {suffix: {"Switch": "switch", "Number": "number", "String": "string"}[kind]
                      for suffix, (_, kind, *_) in settings(device).items()}
    writable = {"volume", "mute", "player", "tv", *setting_fields}

    def channels(fields):
        out = {}
        for field, kind in fields.items():
            config = {"mode": "READWRITE" if field in writable else "READONLY",
                      "stateExtension": f"/state?point={field}"}
            if kind == "switch":
                # Required by HTTP 5.2.3 even on a read-only switch channel.
                config.update(onValue="ON", offValue="OFF")
            if field in ("player", "tv"):
                config["mode"] = "WRITEONLY"
            if field in writable:
                config["commandExtension"] = f"/command?point={field}&value=%2$s"
            if field in setting_fields:
                config["refreshAfterCommand"] = True
            out[field] = {"type": kind, "config": config}
        return out

    for thing_uid, polled, refresh in ((uid, fields, 1), (settings_uid, setting_fields, SETTINGS_REFRESH)):
        if polled:
            document["things"][thing_uid] = {"label": thing["label"] + " (Item-contract stand-in)",
                "location": thing.get("location", ""), "config": {"baseURL": f"http://{address}",
                 "refresh": refresh, "stateMethod": "GET", "commandMethod": "GET"}, "channels": channels(polled)}
    suffixes = {"control": "player", "playlinein": "tv", "linein": "line_in", "currenttitle": "title",
                "currentartist": "artist", "currentalbum": "album",
                **{channel: suffix for suffix, (channel, *_) in settings(device).items()}}
    for item in document["items"].values():
        def moved(channel):
            field = channel.removeprefix(old + ":")
            field = suffixes.get(field, field)
            if not channel.startswith(old + ":"):
                return channel
            return (settings_uid if field in setting_fields else uid) + ":" + field
        if "channel" in item:
            item["channel"] = moved(item["channel"])
        if "channels" in item:
            item["channels"] = {moved(k): v for k, v in item["channels"].items()}
        # The stand-in checks command values, not the native Player channel's
        # type adapter. Native channel declarations have a separate contract test.
        if item is document["items"].get(f"{ident}_player"):
            item["type"] = "String"
    out = {**files, FILE: yaml.safe_dump(document, sort_keys=False, allow_unicode=True)}
    addon_path = "services/addons.cfg"
    lines = []
    for line in out.get(addon_path, "").splitlines():
        if line.startswith("binding = "):
            names = {name.strip() for name in line.removeprefix("binding = ").split(",")}
            names.add("http")
            if not any(k.startswith("sonos:") for k in document["things"]):
                names.discard("sonos")
            line = "binding = " + ",".join(sorted(names))
        lines.append(line)
    if lines:
        out[addon_path] = "\n".join(lines) + "\n"
    return {path: text.replace(device["sonos_udn"], simulation_udn(device)) if isinstance(text, str) else text
            for path, text in out.items()}
