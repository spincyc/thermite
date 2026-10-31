"""Google Cast receivers by address (chromecast binding)."""

from __future__ import annotations

from oh_conf import Addon, House, YamlFile, point

NAME = "cast"
PHASE = "observe"
FILE = "yaml/cast.yaml"
HEADER = (
    "Google Cast receivers (Sony Google TVs, NVIDIA SHIELDs) by address.",
    "Only read-only channels are linked, plus volume as a display; control,",
    "stop, mute and playuri stay unlinked.",
)
ADDONS = (Addon("binding", "chromecast"),)
ASSETS: dict[str, str] = {}
#: (Item suffix, type, label, tags, channel, icon); volume is read-write.
POINTS = (
    ("cast_app", "String", "Cast App", ["Status", "App"], "appName", None),
    ("cast_idling", "Switch", "Cast Idling", ["Status"], "idling", None),
    ("cast_status", "String", "Cast Status", ["Status", "Info"], "statustext", None),
    ("cast_title", "String", "Cast Title", ["Status", "Info"], "title", None),
    ("cast_artist", "String", "Cast Artist", ["Status", "Info"], "artist", None),
)


def validate(device: dict) -> list[str]:
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = f"chromecast:chromecast:{ident}"
    out.things[uid] = {
        "label": f"{house.label(device)} Cast",
        "location": house.room_label(device["room"]),
        "config": {"ipAddress": device["ip"], "port": 8009, "refreshRate": 10},
    }
    for suffix, type_, label, tags, channel, icon in POINTS:
        out.items[f"{ident}_{suffix}"] = point(ident, type_, label, tags, f"{uid}:{channel}", icon=icon)
    out.items[f"{ident}_cast_volume"] = point(ident, "Dimmer", "Cast Volume", ["Status", "SoundVolume"],
                                              f"{uid}:volume", guarded=True, icon="soundvolume")
