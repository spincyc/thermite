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
from pathlib import Path
import re

import yaml

from oh_conf import Addon, House, YamlFile, config as device_config, options_text, point, uses, volume_js
from oh_shape import TEXT, Key
from . import _commands, _items, _script

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
#: Either device.mac or explicit config.sonos.udn supplies the player's UDN.
NEEDS_MAC = False
MODEL_PROFILES = ("soundbar", "amp")
SOUNDBAR_MODELS = frozenset({"Arc", "Arc Ultra", "Beam", "Playbar", "Playbase", "Ray"})
_UDN = re.compile(r"^RINCON_[0-9A-F]{12}01400$")
DEVICE_KEYS = {
    "udn": Key(TEXT, "the player's UDN, by which its events are told from another's: RINCON_, its "
                     "mac in capitals without the colons, then 01400; derived from mac when omitted"),
    "model_profile": Key(TEXT, "capability profile: soundbar (TV input, subwoofer/surround settings) or amp; "
                               "inferred from model when omitted", choices=MODEL_PROFILES),
}
#: The binding talks UPnP to the player's HTTP port.
PROBE_PORTS = (1400,)
DISCOVER_PORTS = (1400,)
DISCOVER_MDNS = ("_sonos",)
#: The binding subscribes to each player's UPnP events, which the sandbox
#: leaves to the runtime.
SESSIONS = True
#: Capability profile -> the binding's Thing type.
THING_TYPES = {"soundbar": "Arc", "amp": "Amp"}
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
#: An Arc's TV audio format (channel codec) while its TV sends no sound: no
#: signal, or a signal of silence, which a TV that is off may keep sending.
#: On its TV input its state reads PLAYING all the same.
TV_SILENT = ("noSignal", "silence")

ARC_KEYS = {**KEYS, "tv": "TV"}
RULE = "automation/js/sonos_control.js"
SOURCE = Path(__file__).with_name("sonos_control.js")
READ_BACK = 60
#: Seconds between the controls check's stand-in polls of the settings;
#: each is read again a second after its command (refreshAfterCommand).
SETTINGS_REFRESH = 30


def _model_profile(device: dict) -> str:
    """Capability profile: 'soundbar' or 'amp'."""
    explicit = device_config(device, NAME).get("model_profile")
    if explicit in MODEL_PROFILES:
        return explicit
    return "soundbar" if device.get("model") in SOUNDBAR_MODELS else "amp"


def _is_soundbar(device: dict) -> bool:
    return _model_profile(device) == "soundbar"


def _thing_type(device: dict) -> str:
    return THING_TYPES[_model_profile(device)]


def settings(device: dict) -> dict:
    """Its model profile's commanded settings (SETTINGS, and ARC_SETTINGS on a soundbar)."""
    return {**SETTINGS, **(ARC_SETTINGS if _is_soundbar(device) else {})}


def remote_of(device: dict) -> str:
    """A soundbar's remote has its TV key; an amp's has none (keys())."""
    return "sonos" if _is_soundbar(device) else "sonos_amp"


def keys(device: dict) -> dict:
    return ARC_KEYS if _is_soundbar(device) else KEYS


def udn(target: str | dict) -> str:
    """The player's UDN: from a MAC string, or for a device dict from explicit
    config.sonos.udn else derived from device['mac']."""
    if isinstance(target, dict):
        explicit = device_config(target, NAME).get("udn")
        if isinstance(explicit, str) and explicit:
            return explicit
        mac = target.get("mac")
        if isinstance(mac, str) and mac:
            return "RINCON_" + mac.replace(":", "").upper() + "01400"
        return ""
    return "RINCON_" + target.replace(":", "").upper() + "01400"


def relayed(device: dict) -> bool:
    """Whether the gateway relays this device's UPnP events: it has a UDN or
    lists sonos in any phase. oh_access.native_boundary.discover_callbacks
    maps these devices' event paths; oh_render points jUPnP at the relay
    when the house has any."""
    return bool(device_config(device, NAME).get("udn")) or any(
        NAME in (names or []) for names in (device.get("controls") or {}).values())


def validate(device: dict) -> list[str]:
    where = device.get("id") or "a device"
    problems = []
    cfg = device_config(device, NAME)
    if "model_profile" in cfg and cfg["model_profile"] not in MODEL_PROFILES:
        problems.append(f"{where}.config.sonos.model_profile: {cfg['model_profile']!r} must be soundbar or amp")
    if relayed(device):
        explicit = cfg.get("udn")
        mac = device.get("mac")
        if explicit is not None:
            if not isinstance(explicit, str) or not _UDN.fullmatch(explicit):
                problems.append(f"{where}.config.sonos.udn: {explicit!r} must match RINCON_<12-UPPER-HEX>01400")
            elif isinstance(mac, str) and mac and explicit != udn(mac):
                problems.append(f"{where}: config.sonos.udn should be {udn(mac)} for its mac")
        elif not mac:
            problems.append(f"{where}: sonos requires either config.sonos.udn or the device's mac")
    return problems


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    player_udn = udn(device)
    soundbar = _is_soundbar(device)
    uid = f"sonos:{_thing_type(device)}:{ident}"
    out.things[uid] = {
        "label": house.label(device),
        "location": house.room_label(device["room"]),
        "config": {"udn": player_udn, "refresh": 60},
    }
    items = out.items
    controlled = uses(device, "control", NAME)
    items[f"{ident}_state"] = point(ident, "String", "Playback State", ["Status", "MediaControl"],
                                    f"{uid}:state")
    items[f"{ident}_title"] = point(ident, "String", "Title", ["Status", "Info"], f"{uid}:currenttitle")
    items[f"{ident}_artist"] = point(ident, "String", "Artist", ["Status", "Info"], f"{uid}:currentartist")
    items[f"{ident}_album"] = point(ident, "String", "Album", ["Status", "Info"], f"{uid}:currentalbum")
    if controlled:
        items[f"{ident}_volume"] = _items.control_point(
            ident, "Dimmer", "Volume", ["Control", "SoundVolume"], f"{uid}:volume", icon="soundvolume",
            link={"toItemScript": "|input", "commandFromItemScript": volume_js(VOLUME)}, state=dict(VOLUME))
        items[f"{ident}_mute"] = _items.control_point(
            ident, "Switch", "Mute", ["Control", "SoundVolume"], f"{uid}:mute", icon="soundvolume_mute")
        items[f"{ident}_player"] = _items.control_point(
            ident, "Player", "Playback", ["Control", "MediaControl"], f"{uid}:control")
        for suffix, (channel, kind, label, _, values) in settings(device).items():
            items[f"{ident}_{suffix}"] = setting(ident, suffix, kind, label, f"{uid}:{channel}", values)
        if soundbar:
            items[f"{ident}_tv"] = _items.control_point(
                ident, "String", "TV Audio", ["Control"], f"{uid}:playlinein",
                link={"commandFromItemScript": _items.enum_guard_js([player_udn])})
        items[f"{ident}_key"] = _items.control_point(ident, "String", "Remote Key", ["Control"])
        _commands.render(device, out, NAME)
    else:
        items[f"{ident}_volume"] = point(ident, "Dimmer", "Volume", ["Status", "SoundVolume"], f"{uid}:volume",
                                         guarded=True, icon="soundvolume")
        items[f"{ident}_mute"] = point(ident, "Switch", "Mute", ["Status", "SoundVolume"], f"{uid}:mute",
                                       guarded=True, icon="soundvolume_mute")
    if soundbar:
        items[f"{ident}_codec"] = point(ident, "String", "TV Audio Format", ["Status", "Info"], f"{uid}:codec")
        items[f"{ident}_line_in"] = point(ident, "Switch", "TV Audio Connected", ["Status"], f"{uid}:linein")


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, "control", NAME) else None


def setting(ident: str, suffix: str, kind: str, label: str, channel: str, values) -> dict:
    """A commanded setting Item, read back. Only what its channel takes
    passes its link: a level on its scale, a choice among its options; a
    switch takes ON or OFF alone. A level is a Setpoint: oh_metrics keeps a
    Number tagged Control as a matrix's routing."""
    tags = {"Switch": ["Control", "MediaControl" if suffix in ("night", "speech") else "Enabled"],
            "String": ["Control", "Mode"], "Number": ["Setpoint", "Level"]}[kind]
    if kind == "Switch":
        return _items.control_point(ident, kind, label, tags, channel)
    if kind == "Number":
        return _items.control_point(
            ident, kind, label, tags, channel,
            link={"toItemScript": "|input", "commandFromItemScript": volume_js(values)}, state=dict(values))
    return _items.control_point(
        ident, kind, label, tags, channel,
        link={"toItemScript": "|input", "commandFromItemScript": _items.enum_guard_js([value for value, _ in values])},
        options=options_text(values))


def files(devices: list[dict], house: House) -> dict[str, str]:
    controlled = [{"id": d["id"], "model": _thing_type(d), "udn": udn(d)} for d in devices
                  if uses(d, "control", NAME)]
    if not controlled:
        return {}
    return {RULE: _script.rule(SOURCE, NAME, {"DEVICES": controlled}, connects=False)}


def playing(device: dict) -> dict:
    """What it plays (oh_features.playing): its track, while PLAYING. TV
    audio and line-in have a blank title, which is nothing."""
    ident = device["id"]
    return {"titles": [{"title": f"{ident}_title", "artist": f"{ident}_artist",
                        "when": {"item": f"{ident}_state", "is": ["PLAYING"]}}]}


def music(device: dict) -> dict | None:
    """What Music Assistant plays it as (oh_integrations music()): None unless
    controlled. Music Assistant's sonos provider names a player by its UDN."""
    if not uses(device, "control", NAME):
        return None
    return {"provider": "sonos", "udn": udn(device)}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled. An
    Amp is never a room's endpoint (oh_site_contract targets soundbars), so
    its remote reaches no room unless a room names it its `audio`."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    return {"key": f"{ident}_key", "mute": f"{ident}_mute",
            "volume": {"item": f"{ident}_volume", **VOLUME}}


def remote_layouts(device: dict | None = None) -> dict[str, dict]:
    """Button layout definitions for Sonos soundbars and standalone Amps."""
    return {
        "sonos": {
            "label": "Audio controls",
            "playback": ["previous", "play_pause", "next"],
            "keyGroups": [{"label": "Audio", "keys": ["tv", "night", "speech"], "columns": 3, "primary": True}],
        },
        "sonos_amp": {
            "label": "Audio controls",
            "playback": ["previous", "play_pause", "next"],
            "keyGroups": [{"label": "Audio", "keys": ["night", "speech"], "columns": 2, "primary": True}],
        },
    }


def diagnostics(device: dict) -> dict | None:
    """Native TV codec facts, with no invented acquisition timestamp.

    The binding exposes scalar Items. Reading them does not establish when
    the speaker measured them; silence and a missing signal are distinct.
    """
    if not (uses(device, PHASE, NAME) or uses(device, "control", NAME)) or not _is_soundbar(device):
        return None
    ident = device["id"]
    return {"kind": NAME, "items": [f"{ident}_codec", f"{ident}_line_in"], "readings": [
        {"item": f"{ident}_codec", "label": "TV input audio format", "category": "audio", "kind": "format",
         "scalar": True, "media": "audio", "noSignal": ["noSignal"], "silence": ["silence"],
         "unknown": ["Unknown"]},
        {"item": f"{ident}_line_in", "label": "TV audio connection", "category": "processing", "kind": "link",
         "scalar": True, "media": "audio"}]}


def audio(device: dict) -> dict | None:
    """Its audio page beside its remote() (oh_integrations audio()): None
    unless controlled. A soundbar whose TV audio format is TV_SILENT plays
    nothing unless it names a track."""
    if remote(device) is None:
        return None
    ident = device["id"]
    soundbar = _is_soundbar(device)

    def shape(kind: str, values) -> dict:
        if kind == "Number":
            return {"kind": "number", **values}
        return {"kind": "enum", "options": [list(pair) for pair in values]} if kind == "String" else {"kind": "switch"}
    page = {"transport": [*TRANSPORT, *(["tv"] if soundbar else [])],
            "state": {"item": f"{ident}_state", "playing": ["PLAYING"],
                      **({"silent": {"item": f"{ident}_codec", "is": list(TV_SILENT)}} if soundbar else {})},
            "track": {field: f"{ident}_{field}" for field in ("title", "artist", "album")},
            "settings": [{"item": f"{ident}_{suffix}", "label": label, "section": section, **shape(kind, values)}
                         for suffix, (_, kind, label, section, values) in settings(device).items()]}
    if soundbar:
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
    old = f"sonos:{_thing_type(device)}:{ident}"
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
    player_udn = udn(device)
    return {path: text.replace(player_udn, simulation_udn(device)) if isinstance(text, str) else text
            for path, text in out.items()}
