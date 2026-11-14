"""Google Cast receivers by address (chromecast binding).

What a receiver plays, as its cast session reports it (playing()): an app
that supports Cast Connect, such as Disney+, sends its title even when
started on the device itself; many apps, such as Netflix, send none. A Sony
TV names its own inputs as app TV ("Game console HDMI 4"), and a SHIELD its
screen saver as Backdrop: neither is something playing.
"""

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
#: Cast's setup HTTP port and its control channel.
DISCOVER_PORTS = (8008, 8009)
DISCOVER_MDNS = ("_googlecast",)
#: A TCP probe of the control channel (8009, TLS) would cost the receiver a
#: handshake it never gets; its Thing's status shows it reachable.
PROBED = False
#: The binding keeps a TLS session open with each receiver, which the sandbox
#: leaves to the runtime.
SESSIONS = True
#: (Item suffix, type, label, tags, channel, icon); volume is read-write.
POINTS = (
    ("cast_app", "String", "Cast App", ["Status", "App"], "appName", None),
    ("cast_idling", "Switch", "Cast Idling", ["Status"], "idling", None),
    ("cast_status", "String", "Cast Status", ["Status", "Info"], "statustext", None),
    ("cast_title", "String", "Cast Title", ["Status", "Info"], "title", None),
    ("cast_artist", "String", "Cast Artist", ["Status", "Info"], "artist", None),
    ("cast_series", "String", "Cast Series", ["Status", "Info"], "seriesTitle", None),
    ("cast_season", "Number", "Cast Season", ["Status", "Info"], "seasonNumber", None),
    ("cast_episode", "Number", "Cast Episode", ["Status", "Info"], "episodeNumber", None),
)
#: App names a receiver reports while nothing plays (playing()).
IDLE_APPS = ("Backdrop", "TV")


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


def playing(device: dict) -> dict:
    """What it plays (oh_features.playing): its cast session's app and title."""
    ident = device["id"]
    return {"apps": [{"item": f"{ident}_cast_app", "idle": list(IDLE_APPS)}],
            "titles": [{"title": f"{ident}_cast_title", "artist": f"{ident}_cast_artist",
                        "series": f"{ident}_cast_series", "season": f"{ident}_cast_season",
                        "episode": f"{ident}_cast_episode"}]}
