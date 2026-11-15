"""WiiM players over their LinkPlay HTTP API (HTTP binding).

Two Things per player. http:url:<id> polls what plays every REFRESH seconds
(getPlayerStatus, EQGetBand, getMetaInfo); http:url:device:<id> polls the
player itself every DEVICE_REFRESH seconds (getStatusEx, getSubLPF,
LED_SWITCH_GET). Its Items (readings()), plain Numbers in UNITS:

    playing   <id>_status (play, pause, stop, load...), _title, _artist,
              _album, _duration, _service (the streaming service: vendor,
              else the network mode's name; empty for an input),
              _sample_rate, _bit_depth
    sound     _volume (0-100), _mute, _input (a switchmode name, inputs()),
              _play_mode (PLAY_MODES), _eq (EQ on), _eq_preset (EQ_PRESETS),
              _sub_level (dB, SUB_LEVEL)
    device    _firmware, _update (a newer firmware waits), _cpu_temperature,
              _board_temperature, _remote_battery (its Bluetooth remote's
              charge), _internet, _sub (a subwoofer is plugged in),
              _sub_crossover, _sub_phase, _light (the status light)

less those its model has no request for (MODELS: a Mini has neither a
subwoofer output nor temperatures), and, under control, _key (a remote
button id, KEYS). Under observe only, every channel is READONLY. Under
control, volume, mute, input, play mode, EQ, EQ preset, sub level, light
and key send commands, each only a value it lists (a volume only on the
player's own scale, VOLUME); INCREASE and DECREASE send nothing: the
binding turns them into fixed strings, not a step from the value read.
Every command reads its request again a second later
(refreshAfterCommand); a key reads getPlayerStatus, so what plays shows a
second after a press. EQ follows the input, so it can show the
last input's for up to REFRESH seconds after a switch.

A Number Item is plain, its unit in UNITS: the gateway passes plain numbers
only (oh_identity.runtime.project).

Title, Artist and Album arrive hex-encoded UTF-8; hex-to-utf8.js beside
this file decodes them and is copied to conf/transform/. EQ is the
current input's (EQGetBand); EQGetStat answers "Failed" on the EQ v2
firmware the Amp Pro runs, and getPlayerStatus's eq is stale on it.

A state transformation returns "" for "no value", never null: openHAB core
5.2.3 leaves an Item at its last state when a transformation yields nothing
(AbstractTransformingChannelHandler.process), and makes an empty or
unparsable number UNDEF (NumberChannelHandler.toState). A command
transformation returns null to send nothing.

The API has no command that wakes the player (only setShutdown and reboot),
so no Item switches its power.

Why openHAB's HTTP binding and not its LinkPlay binding (5.2.3, evaluated
2026-11-14): the LinkPlay Thing takes only a UPnP UDN, never an address, and
learns of changes only through UPnP events, which thermite's callback relay
passes for Sonos alone; it reads the input, EQ and output settings once at
start-up and the HDMI input as NULL; several of its channels do nothing; and
none of it can run in the sandbox or against oh_av_sim on loopback. Revisit
when the relay carries LinkPlay events and that binding refreshes what it
reads.

Read from a WiiM Amp Pro on firmware Linkplay.5.2.833845 (2026-11-14):
EQGetList (EQ_PRESETS), getAudioInputEnable (MODELS), getSubLPF (plugged,
cross 90, phase 0, level 8) and getPlayerStatus == getPlayerStatusEx field
for field. A Mini on Linkplay.4.6.833047 (2026-11-15) lists the same EQ
presets and answers getPlayerStatus, EQGetBand, getMetaInfo and
LED_SWITCH_GET alike. Command forms: WiiM's "HTTP API for WiiM Products"
v1.2, and pywiim and python-linkplay where WiiM documents none (setSubLPF,
LED_SWITCH_SET, EQGetBand).
"""

from __future__ import annotations

import json
import re

from oh_conf import Addon, House, YamlFile, options_text, point, retarget, uses, volume_js

NAME = "wiim_http"
PHASE = "observe"
CONTROL = True
FILE = "yaml/wiim.yaml"
HEADER = (
    "WiiM players over their LinkPlay HTTP API (HTTP binding): one Thing",
    "for what plays, polled often, and one for the player itself, polled",
    "seldom. The device serves HTTPS with a self-signed certificate, hence",
    "ignoreSSLErrors. A player under observe only has every channel",
    "READONLY; one under control also sends commands, each only a value it",
    "lists. Title, Artist and Album arrive hex-encoded UTF-8;",
    "transform/hex-to-utf8.js decodes them.",
)
ADDONS = (
    Addon("binding", "http"),
    Addon("transformation", "jsonpath"),
    Addon("automation", "jsscripting", "JS(...) transformations for WiiM hex titles and commands"),
)
ASSETS = {"transform/hex-to-utf8.js": "hex-to-utf8.js"}
REMOTE_LAYOUT = "wiim"
DISCOVER_MDNS = ("_linkplay",)
HEX_TEXT = "JS(hex-to-utf8.js)"
BASE_URL = "https://{ip}/httpapi.asp?"
#: The requests each Thing polls.
STATUS = "getPlayerStatus"
EQ_BAND = "EQGetBand"
META = "getMetaInfo"
DEVICE_STATUS = "getStatusEx"
SUB = "getSubLPF"
LIGHT = "LED_SWITCH_GET"
#: A TCP probe of its HTTPS port would cost the player a TLS handshake it
#: never gets; its Thing's status shows it reachable.
PROBED = False
#: Seconds from a command to its read-back: the binding asks again a second
#: after a command (refreshAfterCommand).
READ_BACK = 1
#: Seconds between polls of what plays: how soon a change made in the WiiM
#: app or on its own remote shows. Each poll is three HTTPS requests, which
#: the player has answered at this pace since 2026-11-14.
REFRESH = 30
#: Seconds between polls of the player itself: firmware, temperatures, the
#: subwoofer and the light change seldom, and a command reads them back at
#: once. Its Thing's status is one of the device's health checks
#: (oh_metrics), so this is also how late that check sees the player go and
#: come back.
DEVICE_REFRESH = 60
#: The player's volume as getPlayerStatus reports it (vol) and setPlayerCmd
#: takes it: 0-100 in whole steps. LinkPlay HTTP API, as openHAB linkplay
#: 5.2.3 LinkPlayHTTPClient documents it: "setPlayerCmd:vol:{value} - Set
#: volume (0-100)". An Amp Pro reports "vol":"38" and getStatusEx
#: "max_volume":"100" (read 2026-11-04); a lower maximum set in the WiiM
#: app is the player's to apply.
VOLUME = {"min": 0, "max": 100, "step": 1}
#: (switchmode name, label) of the network, every player's first input.
NETWORK = ("wifi", "Wi-Fi")
#: Every model thermite knows, by its inventory `model`: its device kind,
#: the inputs it has besides the network, as the remote offers them, and
#: the readings it has no request for. An Amp Pro (read 2026-11-14) has line in, Bluetooth,
#: optical and HDMI ARC (getAudioInputEnable; plm_support 0x2b00416), a
#: subwoofer output and two temperature sensors. A Mini (read 2026-11-15,
#: firmware Linkplay.4.6.833047) has line in and Bluetooth (plm_support
#: 0x300006; getAudioInputEnable is an unknown command to it), and neither
#: a subwoofer output (getSubLPF answers without "plugged") nor a
#: temperature in getStatusEx.
MODELS = {
    "Amp Pro": {"kind": "streaming_amp",
                "inputs": (("bluetooth", "Bluetooth"), ("line-in", "Line in"), ("optical", "Optical"),
                           ("HDMI", "HDMI ARC")),
                "lacks": frozenset()},
    "Mini": {"kind": "audio_streamer",
             "inputs": (("bluetooth", "Bluetooth"), ("line-in", "Line in")),
             "lacks": frozenset({"sub", "sub_level", "sub_crossover", "sub_phase", "cpu_temperature",
                                 "board_temperature"})},
}
#: getPlayerStatus mode -> the switchmode name of that input (60 is line in
#: too in WiiM's own SDK). Every other mode is a network source (AirPlay,
#: DLNA, Spotify, a multiroom follower...), so it reads as wifi.
MODES = {"40": "line-in", "41": "bluetooth", "43": "optical", "49": "HDMI", "60": "line-in"}
#: getPlayerStatus mode -> the service a network mode plays when `vendor`
#: is empty (WiiM's and Arylic's mode tables).
SERVICES = {"1": "AirPlay", "2": "DLNA", "31": "Spotify", "32": "TIDAL", "36": "Qobuz", "99": "Multiroom"}
#: (loop value, label): how a queue plays, as getPlayerStatus reads it and
#: setPlayerCmd:loopmode takes it. Sources agree on these five (WiiM's read
#: table, Arylic, openHAB's LinkPlay binding, pywiim); WiiM's own write
#: table differs, so a command is read back, never assumed.
PLAY_MODES = (("4", "In order"), ("0", "Repeat all"), ("1", "Repeat one"), ("3", "Shuffle"),
              ("2", "Shuffle and repeat"))
#: A mode the player may report (Arylic's table; Spotify reports it for
#: repeat one) but is never sent.
PLAY_MODES_READ = PLAY_MODES + (("5", "Shuffle and repeat one"),)
#: The EQ presets EQGetList names on an Amp Pro (read 2026-11-14), each
#: loaded by EQLoad:<name> on the current input.
EQ_PRESETS = ("Flat", "Acoustic", "Bass Booster", "Bass Reducer", "Classical", "Dance", "Deep", "Electronic", "Game",
              "Hip-Hop", "Jazz", "Latin", "Loudness", "Lounge", "Movie", "Piano", "Pop", "R&B", "Rock",
              "Small Speakers", "Spoken Word", "Treble Booster", "Treble Reducer", "Vocal Booster")
#: The subwoofer output's level, as getSubLPF reads it and setSubLPF:level
#: takes it: dB, whole steps.
SUB_LEVEL = {"min": -15, "max": 15, "step": 1}
#: Remote button id (web/remote/remotes.js) -> setPlayerCmd command. power
#: and input are not keys; any other id is dropped.
KEYS = {"previous": "prev", "play_pause": "onepause", "next": "next"}


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def device_uid(device: dict) -> str:
    """The second Thing's UID; its last segment is the device id, as every
    Thing's is (oh_inventory.cross_check, oh_metrics.exporter_map)."""
    return f"http:url:device:{device['id']}"


def validate(device: dict) -> list[str]:
    if not (uses(device, PHASE, NAME) or uses(device, "control", NAME)):
        return []
    ident, model = device.get("id") or "a device", device.get("model")
    if model not in MODELS:
        return [f"{ident}: wiim_http knows no model {model!r} (one of {', '.join(sorted(MODELS))})"]
    if device.get("kind") != MODELS[model]["kind"]:
        return [f"{ident}: a WiiM {model} is kind {MODELS[model]['kind']}, not {device.get('kind')!r}"]
    return []


def inputs(device: dict) -> tuple:
    """(switchmode name, label) of every input the player has, the network
    first."""
    return (NETWORK, *MODELS[device["model"]]["inputs"])


def has(device: dict, suffix: str) -> bool:
    """Whether the player's model has a request for this reading."""
    return suffix not in MODELS[device["model"]]["lacks"]


def _lookup_js(table: dict[str, str], otherwise: str = "null") -> str:
    """An inline JS transformation: table[input] for a key of the table, else
    `otherwise` (JS)."""
    return (f"JS(|(function (table) {{ return Object.prototype.hasOwnProperty.call(table, input) ? table[input] : "
            f"{otherwise}; }})({json.dumps(table, separators=(',', ':'))}))")


def input_js(device: dict) -> str:
    """An inline JS transformation: an input command passed on only when it
    is one of the player's inputs()."""
    return _lookup_js({value: value for value, _ in inputs(device)})


def mode_js() -> str:
    """An inline JS transformation: a getPlayerStatus mode -> the switchmode
    name of its input."""
    return _lookup_js(MODES, json.dumps("wifi"))


def key_js() -> str:
    """An inline JS transformation: a remote button id -> its setPlayerCmd
    command; null for any other."""
    return _lookup_js(KEYS)


def play_mode_js() -> str:
    """An inline JS transformation: a play mode command passed on only when
    it is one of PLAY_MODES."""
    return _lookup_js({value: value for value, _ in PLAY_MODES})


def eq_preset_js() -> str:
    """An inline JS transformation: an EQ preset name -> its form in the
    request, null for any other. The binding builds the URL with
    java.net.URI, which quotes a space but not "&", and turns "%%" into "%"
    (openHAB http Util.uriFromString), so "R&B" goes as "R%%26B" to arrive
    as R%26B."""
    return _lookup_js({name: name.replace("&", "%%26") for name in EQ_PRESETS})


#: State transformations that give "" for no value (module docstring).
SECONDS = "JS(|(function (n) { return input !== '' && isFinite(n) && n >= 0 ? String(Math.floor(n / 1000)) : ''; })(Number(input)))"
#: A sensor reads -998 while the player stands by.
CELSIUS = "JS(|(function (n) { return input !== '' && isFinite(n) && n > -100 && n < 200 ? String(n) : ''; })(Number(input)))"


def service_js() -> str:
    """An inline JS transformation of getPlayerStatus: the service a network
    mode plays (vendor, else SERVICES); "" for an input."""
    return ("JS(|(function (d, inputs, services) { var mode = String(d.mode); "
            "if (Object.prototype.hasOwnProperty.call(inputs, mode)) return ''; "
            "var vendor = String(d.vendor || '').trim(); "
            "return vendor || (Object.prototype.hasOwnProperty.call(services, mode) ? services[mode] : ''); })"
            f"(JSON.parse(input), {json.dumps(MODES, separators=(',', ':'))}, "
            f"{json.dumps(SERVICES, separators=(',', ':'))}))")


#: getStatusEx -> its Bluetooth remote's charge in %, while one is connected
#: (BleRemoteControl only says the player takes one).
REMOTE_BATTERY = ("JS(|(function (d) { return d.BleRemoteConnected === '1' && /^\\d+$/.test(String(d.BleRemoteBatterylevel)) "
                  "? String(d.BleRemoteBatterylevel) : ''; })(JSON.parse(input)))")
#: A key's channel reads getPlayerStatus, so that a press reads it again a
#: second later, and keeps nothing of it: the Item stays NULL.
NOTHING = "JS(|null)"
#: The unit of each plain Number Item: its Item's display pattern and its
#: page's (audio()).
UNITS = {"duration": "s", "sample_rate": "Hz", "bit_depth": "bit", "cpu_temperature": "°C",
         "board_temperature": "°C", "remote_battery": "%", "sub_level": "dB", "sub_crossover": "Hz", "sub_phase": "°"}


def readings() -> tuple:
    """Every Item a player gets, as (suffix, Thing ("player" or "device"),
    request, channel type, Item type, label, tags, state transformation
    steps, channel config). The tags are the observed Item's; a controlled
    one swaps Status for Control (commands())."""
    switch = {"onValue": "1", "offValue": "0"}
    return (
        ("status", "player", STATUS, "string", "String", "Playback State", ["Status", "MediaControl"],
         ["JSONPATH($.status)"], {}),
        ("volume", "player", STATUS, "dimmer", "Dimmer", "Volume", ["Status", "SoundVolume"], ["JSONPATH($.vol)"], {}),
        ("mute", "player", STATUS, "switch", "Switch", "Mute", ["Status", "SoundVolume"], ["JSONPATH($.mute)"], switch),
        ("input", "player", STATUS, "string", "String", "Input", ["Status", "Channel"], ["JSONPATH($.mode)", mode_js()],
         {}),
        ("title", "player", STATUS, "string", "String", "Title", ["Status", "Info"], ["JSONPATH($.Title)", HEX_TEXT], {}),
        ("artist", "player", STATUS, "string", "String", "Artist", ["Status", "Info"], ["JSONPATH($.Artist)", HEX_TEXT],
         {}),
        ("album", "player", STATUS, "string", "String", "Album", ["Status", "Info"], ["JSONPATH($.Album)", HEX_TEXT], {}),
        ("duration", "player", STATUS, "number", "Number", "Duration", ["Status", "Duration"],
         ["JSONPATH($.totlen)", SECONDS], {}),
        ("service", "player", STATUS, "string", "String", "Service", ["Status", "App"], [service_js()], {}),
        ("play_mode", "player", STATUS, "string", "String", "Play Mode", ["Status", "Mode"], ["JSONPATH($.loop)"], {}),
        ("eq", "player", EQ_BAND, "switch", "Switch", "EQ", ["Status", "Enabled"], ["JSONPATH($.EQStat)"],
         {"onValue": "On", "offValue": "Off"}),
        ("eq_preset", "player", EQ_BAND, "string", "String", "EQ Preset", ["Status", "Mode"], ["JSONPATH($.Name)"], {}),
        ("sample_rate", "player", META, "number", "Number", "Sample Rate", ["Status", "Frequency"],
         ["JSONPATH($.metaData.sampleRate)"], {}),
        ("bit_depth", "player", META, "number", "Number", "Bit Depth", ["Status", "Info"],
         ["JSONPATH($.metaData.bitDepth)"], {}),
        ("firmware", "device", DEVICE_STATUS, "string", "String", "Firmware", ["Status", "Info"],
         ["JSONPATH($.firmware)"], {}),
        ("update", "device", DEVICE_STATUS, "switch", "Switch", "Firmware Update Waiting", ["Status", "Info"],
         ["JSONPATH($.VersionUpdate)"], switch),
        ("cpu_temperature", "device", DEVICE_STATUS, "number", "Number", "CPU Temperature",
         ["Measurement", "Temperature"], ["JSONPATH($.temperature_cpu)", CELSIUS], {}),
        ("board_temperature", "device", DEVICE_STATUS, "number", "Number", "Board Temperature",
         ["Measurement", "Temperature"], ["JSONPATH($.temperature_tmp102)", CELSIUS], {}),
        ("remote_battery", "device", DEVICE_STATUS, "number", "Number", "Remote Battery",
         ["Measurement", "StateOfCharge"], [REMOTE_BATTERY], {}),
        ("internet", "device", DEVICE_STATUS, "switch", "Switch", "Internet", ["Status", "Info"],
         ["JSONPATH($.internet)"], switch),
        ("sub", "device", SUB, "switch", "Switch", "Subwoofer Connected", ["Status", "Info"], ["JSONPATH($.plugged)"],
         switch),
        ("sub_level", "device", SUB, "number", "Number", "Subwoofer Level", ["Status", "Level"], ["JSONPATH($.level)"],
         {}),
        ("sub_crossover", "device", SUB, "number", "Number", "Subwoofer Crossover", ["Status", "Frequency"],
         ["JSONPATH($.cross)"], {}),
        ("sub_phase", "device", SUB, "number", "Number", "Subwoofer Phase", ["Status", "Info"], ["JSONPATH($.phase)"],
         {}),
        ("light", "device", LIGHT, "switch", "Switch", "Status Light", ["Status", "Light"], [], switch),
    )


def commands(device: dict) -> dict:
    """suffix -> (commandExtension, command transformation or None, state
    description, options) of every reading a controlled player commands."""
    sent = {
        "volume": ("command=setPlayerCmd:vol:%2$s", f"JS({volume_js(VOLUME)})", dict(VOLUME), None),
        "mute": ("command=setPlayerCmd:mute:%2$s", None, None, None),
        "input": ("command=setPlayerCmd:switchmode:%2$s", input_js(device), None, (inputs(device),) * 2),
        "play_mode": ("command=setPlayerCmd:loopmode:%2$s", play_mode_js(), None, (PLAY_MODES_READ, PLAY_MODES)),
        "eq": ("command=EQ%2$s", None, None, None),
        "eq_preset": ("command=EQLoad:%2$s", eq_preset_js(), None,
                      (tuple((name, name) for name in EQ_PRESETS),) * 2),
        "sub_level": ("command=setSubLPF:level:%2$s", f"JS({volume_js(SUB_LEVEL)})", {**SUB_LEVEL, "pattern": "%d dB"},
                      None),
        "light": ("command=LED_SWITCH_SET:%2$s", None, None, None),
    }
    return {suffix: spec for suffix, spec in sent.items() if has(device, suffix)}


def pattern(suffix: str) -> str | None:
    """A plain Number's display pattern: its value and its unit, a % escaped
    (a Java format string)."""
    return f"%d {UNITS[suffix].replace('%', '%%')}" if suffix in UNITS else None


def _channel(type_: str, label: str, request: str, steps: list[str], config: dict, control: bool,
             commanded: bool) -> dict:
    body: dict = {"stateExtension": f"command={request}"}
    if steps:
        body["stateTransformation"] = list(steps)
    body.update(config)
    if not commanded:
        body["mode"] = "READONLY"
    if control:
        # The binding keeps one cache per request, made by the first channel
        # that reads it; its refreshAfterCommand is the one that counts.
        body["refreshAfterCommand"] = True
    return {"type": type_, "label": label, "config": body}


def _control(ident: str, type_: str, label: str, tags: list[str], channel: str, *, icon: str | None = None,
             state: dict | None = None, options: tuple | None = None) -> dict:
    """A commanded Item: options (read, sent) as (value, label) pairs; the
    state description lists what it may read, the command description what
    it may be sent."""
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[ident], tags=tags, autoupdate=False, channel=channel)
    metadata: dict = {}
    if state or options:
        metadata["stateDescription"] = {"config": {**(state or {}),
                                                   **({"options": options_text(options[0])} if options else {})}}
    if options:
        metadata["commandDescription"] = {"config": {"options": options_text(options[1])}}
    if metadata:
        item["metadata"] = metadata
    return item


ICONS = {"volume": "soundvolume", "mute": "soundvolume_mute"}


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    control = uses(device, "control", NAME)
    sent = commands(device) if control else {}
    channels: dict[str, dict] = {"player": {}, "device": {}}
    for suffix, thing, request, type_, item_type, label, tags, steps, config in readings():
        if not has(device, suffix):
            continue
        body = _channel(type_, label, request, steps, config, control, suffix in sent)
        if suffix in sent:
            extension, transformation, _, _ = sent[suffix]
            body["config"]["commandExtension"] = extension
            if transformation:
                body["config"]["commandTransformation"] = [transformation]
        channels[thing][suffix] = body
        uid = (thing_uid if thing == "player" else device_uid)(device)
        if suffix in sent:
            _, _, state, options = sent[suffix]
            # A commanded number is a Setpoint: oh_metrics keeps a Number
            # tagged Control as a matrix's routing.
            role = "Setpoint" if item_type == "Number" else "Control"
            out.items[f"{ident}_{suffix}"] = _control(ident, item_type, label, [role, *tags[1:]],
                                                      f"{uid}:{suffix}", icon=ICONS.get(suffix), state=state,
                                                      options=options)
        else:
            out.items[f"{ident}_{suffix}"] = point(ident, item_type, label, tags, f"{uid}:{suffix}",
                                                   icon=ICONS.get(suffix),
                                                   fmt=pattern(suffix))
    if control:
        # The binding reads a request again after a command only through a
        # channel that reads it (HttpThingHandler 5.2.3: a WRITEONLY channel
        # has no cache to refresh), so a key reads the status and keeps
        # nothing of it; oh_render.read_only() turning it READONLY polls
        # nothing more.
        channels["player"]["key"] = {"type": "string", "label": "Remote Key", "config": {
            "stateExtension": f"command={STATUS}", "stateTransformation": [NOTHING],
            "commandExtension": "command=setPlayerCmd:%2$s", "commandTransformation": [key_js()],
            "refreshAfterCommand": True}}
        out.items[f"{ident}_key"] = _control(ident, "String", "Remote Key", ["Control"], f"{thing_uid(device)}:key")
    for thing, uid, label, refresh in (("player", thing_uid(device), "HTTP API", REFRESH),
                                       ("device", device_uid(device), "Device", DEVICE_REFRESH)):
        out.things[uid] = {
            "label": f"{house.label(device)} {label}",
            "location": house.room_label(device["room"]),
            "config": {"baseURL": BASE_URL.format(ip=device["ip"]), "refresh": refresh, "timeout": 3000,
                       "ignoreSSLErrors": True, "strictErrorHandling": True},
            "channels": channels[thing],
        }


def playing(device: dict) -> dict:
    """What it plays (oh_features.playing): the service a network source
    plays, and its track while its status is play. A field it does not know
    reads Unknown, which is nothing."""
    ident = device["id"]
    playing = {"item": f"{ident}_status", "is": ["play"]}
    return {"apps": [{"item": f"{ident}_service", "when": playing}],
            "titles": [{"title": f"{ident}_title", "artist": f"{ident}_artist",
                        "when": playing}]}


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    return {
        "key": f"{ident}_key",
        "mute": f"{ident}_mute",
        "volume": {"item": f"{ident}_volume", **VOLUME},
        "input": {"item": f"{ident}_input", "options": [list(pair) for pair in inputs(device)]},
    }


#: Its page's settings (audio()): (suffix, section) in the page's order.
SETTINGS = (("play_mode", "playback"), ("eq", "sound"), ("eq_preset", "sound"), ("sub_level", "sound"),
            ("light", "device"))
#: Its page's read-only facts about the player itself...
FACTS = ("cpu_temperature", "board_temperature", "firmware", "update", "internet", "remote_battery", "sub",
         "sub_crossover", "sub_phase")
#: ...of which these drift with every poll: no activity.
QUIET = {"cpu_temperature", "board_temperature", "remote_battery"}


def audio(device: dict) -> dict | None:
    """Its audio page beside its remote() (oh_integrations audio()): None
    unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    labels = {suffix: label for suffix, _, _, _, _, label, *_ in readings()}

    def entry(suffix: str, **shape) -> dict:
        unit = {"unit": UNITS[suffix]} if suffix in UNITS else {}
        return {"item": f"{ident}_{suffix}", "label": labels[suffix], **shape, **unit}
    shapes = {
        # Loop has a meaning only for a queue the network plays.
        "play_mode": {"kind": "enum", "options": [list(pair) for pair in PLAY_MODES],
                      "reads": [list(pair) for pair in PLAY_MODES_READ[len(PLAY_MODES):]],
                      "when": {"item": f"{ident}_input", "is": ["wifi"]}},
        "eq": {"kind": "switch"},
        "eq_preset": {"kind": "enum", "options": [[name, name] for name in EQ_PRESETS]},
        # Only a plugged subwoofer has a level to set.
        "sub_level": {"kind": "number", **SUB_LEVEL, "when": {"item": f"{ident}_sub", "is": ["ON"]}},
        "light": {"kind": "switch"},
    }
    return {
        "transport": list(KEYS),
        "state": {"item": f"{ident}_status", "playing": ["play"]},
        "track": {field: f"{ident}_{field}" for field in ("title", "artist", "album", "service", "duration",
                                                           "sample_rate", "bit_depth")},
        "settings": [{**entry(suffix, **shapes[suffix]), "section": section} for suffix, section in SETTINGS
                     if has(device, suffix)],
        "facts": [entry(suffix, **({"quiet": True} if suffix in QUIET else {})) for suffix in FACTS
                  if has(device, suffix)],
    }


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the player's Things talking plain HTTP to HOST:PORT instead."""
    def change(thing: dict) -> None:
        config = thing["config"]
        config["baseURL"] = re.sub(r"^https?://[^/]+", f"http://{address}", config["baseURL"])
    text = retarget(files[FILE], FILE, thing_uid(device), change)
    return {**files, FILE: retarget(text, FILE, device_uid(device), change)}
