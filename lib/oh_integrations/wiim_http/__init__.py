"""WiiM players over their LinkPlay HTTP API (HTTP binding).

Items, per player: `<id>_status` (String: play, pause, stop...),
`<id>_volume` (Dimmer, 0-100), `<id>_mute` (Switch), `<id>_title` and
`<id>_artist` (String); under control, `<id>_input` (String: a switchmode
name, INPUTS) and `<id>_key` (String: a remote button id, KEYS) too.

Under observe only, every channel is READONLY and requests only
getPlayerStatus. Under control, volume, mute, input and key send
setPlayerCmd commands: the volume held to the player's volume_max
(devices.yaml; default DEFAULT_VOLUME_MAX), an input only when it is one of
INPUTS, a key only when it is one of KEYS. INCREASE and DECREASE send
nothing: the binding turns them into fixed strings, not a step from the
volume read. Title and Artist arrive hex-encoded UTF-8; hex-to-utf8.js beside
this file decodes them and is copied to conf/transform/.

The API has no command that wakes the player (only setShutdown and reboot),
so no Item switches its power.
"""

from __future__ import annotations

import json
import re

from oh_conf import Addon, House, YamlFile, options_text, point, retarget, uses

NAME = "wiim_http"
PHASE = "observe"
CONTROL = True
FILE = "yaml/wiim.yaml"
HEADER = (
    "WiiM Amp Pro players over their LinkPlay HTTP API (HTTP binding).",
    "The device serves HTTPS with a self-signed certificate, hence",
    "ignoreSSLErrors. A player under observe only has every channel READONLY",
    "and only getPlayerStatus requested; one under control also has volume,",
    "mute, input and key send setPlayerCmd commands, the volume held to its",
    "volume_max. Title and Artist arrive hex-encoded UTF-8; transform/",
    "hex-to-utf8.js decodes them.",
)
ADDONS = (
    Addon("binding", "http"),
    Addon("transformation", "jsonpath"),
    Addon("automation", "jsscripting", "JS(...) transformations for WiiM hex titles and commands"),
)
ASSETS = {"transform/hex-to-utf8.js": "hex-to-utf8.js"}
HEX_TEXT = "JS(hex-to-utf8.js)"
#: (channel and Item suffix, channel type, Item type, label, JSON field, tags, icon)
POINTS = (
    ("status", "string", "String", "Playback State", "status", ["Status", "MediaControl"], None),
    ("volume", "dimmer", "Dimmer", "Volume", "vol", ["Status", "SoundVolume"], "soundvolume"),
    ("mute", "switch", "Switch", "Mute", "mute", ["Status", "SoundVolume"], "soundvolume_mute"),
    ("title", "string", "String", "Title", "Title", ["Status", "Info"], None),
    ("artist", "string", "String", "Artist", "Artist", ["Status", "Info"], None),
)
HEX_FIELDS = {"Title", "Artist"}
#: The query of every request a controlled Thing polls; its baseURL is
#: BASE_URL, so each channel names its own command.
STATUS = "command=getPlayerStatus"
BASE_URL = "https://{ip}/httpapi.asp?"
#: Seconds from a command to its read-back: the binding asks for
#: getPlayerStatus again a second after a command (refreshAfterCommand).
READ_BACK = 1
#: The player's volume scale.
MAX_VOLUME = 100
#: The volume a player under control is held to unless devices.yaml sets
#: volume_max.
DEFAULT_VOLUME_MAX = 70
#: (switchmode name, label) of every input the remote offers: the Amp Pro's
#: (getStatusEx plm_support: line in, Bluetooth, optical, HDMI ARC) and the
#: network.
INPUTS = (("wifi", "Wi-Fi"), ("bluetooth", "Bluetooth"), ("line-in", "Line in"), ("optical", "Optical"),
          ("HDMI", "HDMI ARC"))
#: getPlayerStatus mode -> the switchmode name of that input (60 is line in
#: too in WiiM's own SDK). Every other mode is a network source (AirPlay,
#: DLNA, Spotify, a multiroom follower...), so it reads as wifi.
MODES = {"40": "line-in", "41": "bluetooth", "43": "optical", "49": "HDMI", "60": "line-in"}
#: Remote button id (web/remote/remotes.js) -> setPlayerCmd command. power
#: and input are not keys; any other id is dropped.
KEYS = {"previous": "prev", "play_pause": "onepause", "next": "next"}


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def volume_max(device: dict) -> int:
    """The highest volume the player may be sent."""
    return device.get("volume_max", DEFAULT_VOLUME_MAX)


def validate(device: dict) -> list[str]:
    # volume_max means another scale to denonmarantz_http, so only a player's is checked here.
    cap = device.get("volume_max")
    if uses(device, PHASE, NAME) and cap is not None and not (
            isinstance(cap, int) and not isinstance(cap, bool) and 0 <= cap <= MAX_VOLUME):
        return [f"{device.get('id')}: volume_max {cap!r} is not a whole number 0-{MAX_VOLUME}"]
    return []


def _lookup_js(table: dict[str, str], otherwise: str = "null") -> str:
    """An inline JS transformation: table[input] for a key of the table, else
    `otherwise` (JS)."""
    return (f"JS(|(function (table) {{ return Object.prototype.hasOwnProperty.call(table, input) ? table[input] : "
            f"{otherwise}; }})({json.dumps(table, separators=(',', ':'))}))")


def volume_js(cap: int) -> str:
    """An inline JS transformation: a volume command, rounded and held to
    `cap`; null (nothing sent) for anything but a number."""
    return f"JS(|/^\\d+(\\.\\d+)?$/.test(input) ? String(Math.min(Math.round(Number(input)), {cap})) : null)"


def input_js() -> str:
    """An inline JS transformation: an input command passed on only when it
    is one of INPUTS."""
    return _lookup_js({value: value for value, _ in INPUTS})


def mode_js() -> str:
    """An inline JS transformation: a getPlayerStatus mode -> the switchmode
    name of its input."""
    return _lookup_js(MODES, json.dumps("wifi"))


def key_js() -> str:
    """An inline JS transformation: a remote button id -> its setPlayerCmd
    command; null for any other."""
    return _lookup_js(KEYS)


def _channel(type_: str, label: str, field: str) -> dict:
    steps = [f"JSONPATH($.{field})"] + ([HEX_TEXT] if field in HEX_FIELDS else [])
    config: dict = {"mode": "READONLY", "stateTransformation": steps}
    if type_ == "switch":
        config.update(onValue="1", offValue="0")
    return {"type": type_, "label": label, "config": config}


def _controlled_channels(device: dict) -> dict:
    def command(name: str, transformation: str | None = None) -> dict:
        config: dict = {"commandExtension": f"command=setPlayerCmd:{name}%2$s"}
        if transformation:
            config["commandTransformation"] = [transformation]
        return config

    # The binding polls getPlayerStatus once for all these channels, and reads
    # it again a second after a command only if the first channel it sets up
    # (status) asks to; without that a change waits for the next refresh.
    channels = {}
    for channel, type_, _, label, field, _, _ in POINTS:
        body = _channel(type_, label, field)
        body["config"].update(stateExtension=STATUS, refreshAfterCommand=True)
        channels[channel] = body
    for channel, extra in (("volume", command("vol:", volume_js(volume_max(device)))), ("mute", command("mute:"))):
        config = channels[channel]["config"]
        del config["mode"]
        config.update(extra)
    channels["input"] = {"type": "string", "label": "Input", "config": {
        "stateExtension": STATUS, "stateTransformation": ["JSONPATH($.mode)", mode_js()],
        **command("switchmode:", input_js()), "refreshAfterCommand": True}}
    # The status query too, so that oh_render.read_only() turning the
    # channel READONLY polls nothing more.
    channels["key"] = {"type": "string", "label": "Remote Key", "config": {
        "mode": "WRITEONLY", "stateExtension": STATUS, **command("", key_js())}}
    return channels


def _control(ident: str, type_: str, label: str, tags: list[str], channel: str, *,
             icon: str | None = None, state: dict | None = None, options: str | None = None) -> dict:
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[ident], tags=tags, autoupdate=False, channel=channel)
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
    control = uses(device, "control", NAME)
    out.things[uid] = {
        "label": f"{house.label(device)} HTTP API",
        "location": house.room_label(device["room"]),
        "config": {
            "baseURL": BASE_URL.format(ip=device["ip"]) + ("" if control else STATUS),
            "refresh": 10,
            "timeout": 3000,
            "ignoreSSLErrors": True,
            "strictErrorHandling": True,
        },
        "channels": (_controlled_channels(device) if control else
                     {channel: _channel(type_, label, field) for channel, type_, _, label, field, _, _ in POINTS}),
    }
    if not control:
        for channel, _, item_type, label, _, tags, icon in POINTS:
            out.items[f"{ident}_{channel}"] = point(ident, item_type, label, tags, f"{uid}:{channel}", icon=icon)
        return
    items = out.items
    items[f"{ident}_status"] = point(ident, "String", "Playback State", ["Status", "MediaControl"],
                                     f"{uid}:status")
    items[f"{ident}_volume"] = _control(ident, "Dimmer", "Volume", ["Control", "SoundVolume"], f"{uid}:volume",
                                        icon="soundvolume", state={"min": 0, "max": volume_max(device)})
    items[f"{ident}_mute"] = _control(ident, "Switch", "Mute", ["Control", "SoundVolume"], f"{uid}:mute",
                                      icon="soundvolume_mute")
    items[f"{ident}_input"] = _control(ident, "String", "Input", ["Control", "Channel"], f"{uid}:input",
                                       options=options_text(INPUTS))
    for channel in ("title", "artist"):
        items[f"{ident}_{channel}"] = point(ident, "String", channel.title(), ["Status", "Info"], f"{uid}:{channel}")
    items[f"{ident}_key"] = _control(ident, "String", "Remote Key", ["Control"], f"{uid}:key")


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, "control", NAME):
        return None
    ident = device["id"]
    return {
        "key": f"{ident}_key",
        "mute": f"{ident}_mute",
        "volume": {"item": f"{ident}_volume", "max": volume_max(device), "step": 2, "unit": "", "offset": 0},
        "input": {"item": f"{ident}_input", "options": [list(pair) for pair in INPUTS]},
    }


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the player's Thing talking plain HTTP to HOST:PORT instead."""
    def change(thing: dict) -> None:
        config = thing["config"]
        config["baseURL"] = re.sub(r"^https?://[^/]+", f"http://{address}", config["baseURL"])
    return {**files, FILE: retarget(files[FILE], FILE, thing_uid(device), change)}
