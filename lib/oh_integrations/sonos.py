"""Sonos Arc/Amp native channels; explicit control enables volume and keys.

Arc remotes expose TV audio, transport, Night and Speech. The command rule
only forwards allowlisted keys to native channels; it never groups players
or changes bonded-surround membership. No power switch is invented.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from oh_conf import GUARD_PROFILE, Addon, House, YamlFile, point, uses

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
#: Inventory model -> the binding's Thing type.
THING_TYPES = {"Arc": "Arc", "Amp": "Amp"}
MAX_VOLUME = 100
DEFAULT_VOLUME_MAX = 100
KEYS = {"previous": "PREVIOUS", "play_pause": "PLAY_PAUSE", "next": "NEXT", "night": "NIGHT", "speech": "SPEECH"}
ARC_KEYS = {**KEYS, "tv": "TV"}
RULE = "automation/js/sonos_control.js"
SOURCE = Path(__file__).with_name("sonos_control.js")
MARK = "const DEVICES = null;"
READ_BACK = 60


def volume_max(device: dict) -> int:
    return device.get("volume_max", DEFAULT_VOLUME_MAX)


def volume_js(cap: int) -> str:
    return f"|/^\\d+(\\.\\d+)?$/.test(input) ? String(Math.min(Math.round(Number(input)), {cap})) : null"


def keys(device: dict) -> dict:
    return ARC_KEYS if device.get("model") == "Arc" else KEYS


def udn(mac: str) -> str:
    """The UDN a Sonos player derives from its MAC address."""
    return "RINCON_" + mac.replace(":", "").upper() + "01400"


def validate(device: dict) -> list[str]:
    where = device.get("id") or "a device"
    problems = []
    used = any(NAME in (names or []) for names in (device.get("controls") or {}).values())
    if device.get("sonos_udn") or used:
        expected = udn(device.get("mac") or "")
        if device.get("sonos_udn") != expected:
            problems.append(f"{where}: sonos_udn should be {expected} for its mac")
    if uses(device, PHASE, NAME) and device.get("model") not in THING_TYPES:
        problems.append(f"{where}: sonos knows no Thing type for model {device.get('model')!r} "
                        f"(one of {', '.join(sorted(THING_TYPES))})")
    cap = device.get("volume_max")
    if used and cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= MAX_VOLUME):
        problems.append(f"{where}: volume_max {cap!r} is not a whole number 0-{MAX_VOLUME}")
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
    items[f"{ident}_volume"] = point(ident, "Dimmer", "Volume", ["Status", "SoundVolume"], f"{uid}:volume",
                                     guarded=not controlled, icon="soundvolume")
    items[f"{ident}_mute"] = point(ident, "Switch", "Mute", ["Status", "SoundVolume"], f"{uid}:mute",
                                   guarded=not controlled, icon="soundvolume_mute")
    if controlled:
        volume = items[f"{ident}_volume"]
        del volume["channel"]
        volume["channels"] = {f"{uid}:volume": {"profile": GUARD_PROFILE, "toItemScript": "|input",
                                                   "toHandlerScript": volume_js(volume_max(device))}}
        volume["autoupdate"] = False
        volume["metadata"] = {"stateDescription": {"config": {"min": 0, "max": volume_max(device), "step": 1}}}
        items[f"{ident}_mute"]["autoupdate"] = False
        for suffix, kind, label, channel in [("player", "Player", "Playback", "control"),
                                             ("night", "Switch", "Night Mode", "nightmode"),
                                             ("speech", "Switch", "Speech Enhancement", "speechenhancement")]:
            items[f"{ident}_{suffix}"] = point(ident, kind, label, ["Control", "MediaControl"], f"{uid}:{channel}")
            items[f"{ident}_{suffix}"]["autoupdate"] = False
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


def files(devices: list[dict], house: House) -> dict[str, str]:
    controlled = [{"id": d["id"], "model": d["model"], "udn": d["sonos_udn"]} for d in devices
                  if uses(d, "control", NAME)]
    if not controlled:
        return {}
    return {RULE: SOURCE.read_text().replace(MARK, "const DEVICES = " + json.dumps(controlled, indent=2) + ";")}


def remote(device: dict) -> dict | None:
    """The existing Arc remotes; standalone Amps retain native Item control."""
    if not uses(device, "control", NAME) or device.get("model") != "Arc":
        return None
    ident = device["id"]
    return {"key": f"{ident}_key", "mute": f"{ident}_mute",
            "volume": {"item": f"{ident}_volume", "max": volume_max(device), "step": 1, "unit": "%", "offset": 0}}


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
    uid = f"http:url:{ident}"
    document = yaml.safe_load(files[FILE])
    thing = document["things"].pop(old)
    fields = {"state": "string", "title": "string", "artist": "string", "volume": "dimmer", "mute": "switch",
              "player": "string", "night": "switch", "speech": "switch", "tv": "string",
              "codec": "string", "line_in": "switch"}
    writable = {"volume", "mute", "player", "night", "speech", "tv"}
    channels = {}
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
        channels[field] = {"type": kind, "config": config}
    document["things"][uid] = {"label": thing["label"] + " (Item-contract stand-in)",
        "location": thing.get("location", ""), "config": {"baseURL": f"http://{address}", "refresh": 1,
         "stateMethod": "GET", "commandMethod": "GET"}, "channels": channels}
    suffixes = {"control": "player", "playlinein": "tv", "nightmode": "night",
                "speechenhancement": "speech", "linein": "line_in", "currenttitle": "title", "currentartist": "artist"}
    for item in document["items"].values():
        def moved(channel):
            field = channel.removeprefix(old + ":")
            return uid + ":" + suffixes.get(field, field) if channel.startswith(old + ":") else channel
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
