"""Android and Google TVs and NVIDIA SHIELDs over the Android TV Remote
protocol (androidtv binding, googletv Thing).

The binding talks to the device's remote service on TLS port 6466, as the
Google TV phone app does. The device trusts it once paired: asked (the
Pairing page, web/remote/pair.html, or bin/thermite pair <id> --sandbox), it
shows a six-character code on its screen, and the code sent back makes the
binding's own certificate one the device accepts. The binding keeps that
certificate in its keystore in userdata (androidtv/googletv.<id>.keystore),
never in conf/ or Git, so an instance with a new userdata pairs again. An
unpaired Thing stays OFFLINE and shows nothing on the screen until asked.

The binding asks a paired device for no code, and makes new keys only when
the Thing starts without a keystore, so the rule pairs a paired device
again (AGAIN, sent once the owner confirms CONFIRM on the Pairing page) by
starting its Thing without one: it disables the Thing, renames the keystore
(KEYSTORE) to the same name with `.previous` after it, and enables the
Thing. The device refuses the new keys, the binding opens its pairing
connection, and the rule asks for a code. A code that pairs keeps the new
keystore, and the previous one is deleted. CANCEL, a refused code or no
pairing within the rule's REPAIR_MS of the last request disables the
Thing, moves the previous keystore back over the new one and enables the
Thing, so the old pairing works again. A previous keystore with no pairing
under way (openHAB restarted in the middle) is moved back the same way.

Each device is drawn with one of REMOTES (web/remote/remotes.js): a TV with
the Android TV remote, a SHIELD with its own. Sony's IP-control remote has
additional IRCC keys the Android TV remote service does not accept.
Items, per device:

- `<id>_android_power` (Switch): as the device reports it; a command that
  differs from the state is sent as its POWER key (the binding's power
  channel).
- TVs only: `<id>_android_mute` (Switch) the same way with MUTE, and
  `<id>_android_volume` (Dimmer, 0-100), the TV's volume as it reports it.
  The binding cannot set a volume, so the rule
  (conf/automation/js/androidtv_googletv.js, the .js file beside this one)
  turns a command on the channel's own scale (VOLUME) into one VOLUME_UP or
  VOLUME_DOWN press per step and drops any other. A SHIELD's sound is the
  room's audio device's.
- `<id>_android_app` (String): the app on screen, display-only: its Android
  package name, which playing() names (APP_NAMES); a home screen or the
  screen saver (IDLE_APPS) is nothing playing.
- `<id>_key` (String): a remote button id of its remote (keys()). The rule
  presses its Android key code (KEYCODES) or opens its app (APP_LINKS); any
  other id is dropped.
- `<id>_pairing` and `<id>_pairing_status` (oh_integrations._pairing): the
  rule hands REQUEST and the code to the binding's pincode channel, and
  keeps the status from the Thing's: ONLINE is paired; OFFLINE for want of
  a PIN is not paired. The binding asks for a code on PORT + 1, and drops
  the request silently when that port does not connect, so the rule tries
  it first, also before pairing again, and shows `closed` instead.
- `<id>_problem` and `<id>_androidtv_googletv_commands` (oh_integrations._commands): why
  the device did not take the rule's last command or cannot be read, as a
  code, and the rule's tally of its commands by outcome.

The rule hands its presses to the Thing's handler, as the binding's console
command does, so no Item links the binding's keycode, debug or pincode
channels: a client presses only what the rule allows. It holds
oh_conf.RULE_SENDS, and oh_render.read_only() guards every Item on a
channel that is not READONLY, the binding's included, so a read-only copy
commands nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from oh_conf import Addon, House, YamlFile, config as device_config, point, retarget, uses
from oh_shape import TEXT, Key

if TYPE_CHECKING:
    from oh_inventory import Device

from .. import _commands, _items, _pairing, _script

NAME = "androidtv_googletv"
PHASE = "control"
FILE = "yaml/googletv.yaml"
HEADER = (
    "Android and Google TVs and NVIDIA SHIELDs over the Android TV Remote",
    "protocol (androidtv binding, TLS port 6466), paired from the Pairing page;",
    "the binding's keystore in userdata holds the pairing. Power and mute are",
    "the binding's own channels; the rule conf/automation/js/androidtv_googletv_<device>.js",
    "presses the remote keys, steps the volume on its own scale, and pairs.",
    "Items are autoupdate off: they show only what the device reports.",
)
ADDONS = (Addon("binding", "androidtv"),
          Addon("automation", "jsscripting", "the Android TV key, volume and pairing rule (androidtv_googletv.js)"))
ASSETS: dict[str, str] = {}
PORT = 6466
#: A TCP probe of PORT would cost the device a TLS handshake it never gets;
#: its Thing's status shows it reachable.
PROBED = False
#: The binding keeps a TLS session open with each device, which the sandbox
#: leaves to the runtime.
SESSIONS = True
#: The remote protocol's port and its pairing port.
DISCOVER_PORTS = (PORT, 6467)
DISCOVER_MDNS = ("_androidtvremote2",)
#: The pinned binding's connections block openHAB's thingHandler pool, so a
#: house with one of these devices raises the pool to this many threads
#: (oh_features.threadpools); a capacity mitigation, not a fix.
HANDLER_POOL = 32
HANDLER_POOL_WHY = "Google TV connections block thingHandler"
#: Seconds from a command to its read-back: none, the TV reports a change at once.
READ_BACK = 0
RULE = "automation/js/androidtv_googletv.js"
SOURCE = Path(__file__).resolve().parent / "androidtv_googletv.js"
#: The volume as the binding's googletv volume channel reports it: whole
#: percent of the TV's own maximum. openHAB androidtv 5.2.3:
#: GoogleTVConnectionManager.setVolCurr() reports volCurr * 100 / volMax as
#: a PercentType and exposes neither volMax nor the TV's own level, so the
#: percentage is the nearest scale thermite has. The binding sets no volume.
VOLUME = {"min": 0, "max": 100, "step": 1, "unit": "%"}
TV = "android_tv"
SHIELD = "nvidia_shield"
#: The remote layout (web/remote/remotes.js) each kind of device is drawn with.
REMOTES = (TV, SHIELD)
DEVICE_KEYS = {
    "remote_layout": Key(TEXT, "remote layout: android_tv or nvidia_shield; inferred from make when omitted",
                         choices=REMOTES),
}
_SUPPORTED_KINDS = frozenset({"tv", "streamer", "console", "game_console", "media_player"})
#: Remote button id -> Android key code (KeyEvent), per remote. power is an
#: Item of its own, never a key. The volume rule presses VOLUME_UP and
#: VOLUME_DOWN itself. TV_INPUT (178), GUIDE (172) and SETTINGS (176) are
#: left out: a Sony TV ignores them from the remote service (tried on the
#: XBR-43X800E), though it takes CHANNEL_UP (166).
KEYCODES = {
    TV: {
        "home": 3,
        "back": 4,
        "up": 19,
        "down": 20,
        "left": 21,
        "right": 22,
        "ok": 23,
        "rewind": 89,
        "fast_forward": 90,
        "play": 126,
        "pause": 127,
        "channel_up": 166,
        "channel_down": 167,
    },
    SHIELD: {
        "home": 3,
        "back": 4,
        "up": 19,
        "down": 20,
        "left": 21,
        "right": 22,
        "ok": 23,
        "menu": 82,
        "play_pause": 85,
        "rewind": 89,
        "fast_forward": 90,
    },
}
#: Remote button id -> the app link the device opens (no Android key code
#: opens an app), sent as a RemoteAppLinkLaunchRequest, per remote.
APP_LINKS = {
    TV: {
        "netflix": "https://www.netflix.com/title",
        "youtube": "https://www.youtube.com",
    },
    SHIELD: {
        "netflix": "https://www.netflix.com/title",
    },
}
#: Android package -> the app's name, for what is playing (playing()). Any
#: other package is named by its second part (com.fubo.... is Fubo).
APP_NAMES = {
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "com.cbs.ott": "Paramount+",
    "com.crunchyroll.crunchyroid": "Crunchyroll",
    "com.disney.disneyplus": "Disney+",
    "com.fubo.firetv.screen": "Fubo",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "com.google.android.youtube.tvunplugged": "YouTube TV",
    "com.hulu.livingroomplus": "Hulu",
    "com.limelight": "Moonlight",
    "com.netflix.ninja": "Netflix",
    "com.nvidia.geforcenow": "GeForce NOW",
    "com.peacocktv.peacockandroid": "Peacock",
    "com.plexapp.android": "Plex",
    "com.sling": "Sling TV",
    "com.spotify.tv.android": "Spotify",
    "com.tubitv": "Tubi",
    "com.valvesoftware.steamlink": "Steam Link",
    "com.wbd.stream": "Max",
    "org.xbmc.kodi": "Kodi",
    "tv.pluto.android": "Pluto TV",
    "tv.twitch.android.app": "Twitch",
}
#: Packages on screen while nothing plays: home screens, the screen saver,
#: settings, the Play Store and voice search.
IDLE_APPS = ("com.android.tv.settings", "com.android.vending", "com.google.android.apps.tv.launcherx",
             "com.google.android.backdrop", "com.google.android.katniss", "com.google.android.leanbacklauncher",
             "com.google.android.tvlauncher")
#: The binding sends a debug-channel command starting with this as the raw
#: protocol message whose hex follows.
RAW = "GOOGLETV_RAW_"
#: RemoteMessage field 90, RemoteAppLinkLaunchRequest, as its protobuf key.
APP_LINK_FIELD = "d205"
#: The Pairing page's description of a device (oh_integrations._pairing),
#: but its label (pairing_label()) and what it asks before pairing a paired
#: one again (CONFIRM).
PAIRING = {
    "length": 6,
    "chars": "hex",
    "again": True,
    "hint": "No code? Make sure it is on and showing a picture, then ask for a new code.",
}
CONFIRM = ("Pair the {label} again? It shows a new code, and thermite cannot control it until that code is "
           "typed here. Cancel goes back to the pairing it has now.")
#: A Thing's keystore, under openHAB's userdata, as the binding names it
#: when the Thing sets no keystoreFileName (GoogleTVConnectionManager.initialize).
KEYSTORE = "androidtv/googletv.{id}.keystore"


def thing_uid(device: dict) -> str:
    return f"androidtv:googletv:{device['id']}"


def remote_of(device: dict) -> str:
    """The remote a device is drawn with: explicit config.androidtv_googletv.remote_layout,
    else a SHIELD's for NVIDIA devices, else a TV's."""
    explicit = device_config(device, NAME).get("remote_layout")
    if explicit in REMOTES:
        return explicit
    return SHIELD if device.get("make") == "NVIDIA" else TV


def is_tv(device: dict) -> bool:
    return remote_of(device) == TV


def keys(device: dict) -> dict[str, int | str]:
    """Remote button id -> what it presses on this device: a key code or an app link."""
    remote = remote_of(device)
    return {**KEYCODES[remote], **APP_LINKS[remote]}


def pairing_label(device: dict) -> str:
    """What the Pairing page calls the device."""
    if not is_tv(device):
        return "SHIELD"
    return f"{device['make']} TV" if device.get("make") else "TV"


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    where = device.get("id") or "a device"
    problems = []
    if device.get("kind") not in _SUPPORTED_KINDS:
        problems.append(f"{where}: {NAME} drives a TV or streamer, not a {device.get('kind')}")
    cfg = device_config(device, NAME)
    if "remote_layout" in cfg and cfg["remote_layout"] not in REMOTES:
        problems.append(f"{where}.config.{NAME}.remote_layout: {cfg['remote_layout']!r} "
                        f"must be {' or '.join(REMOTES)}")
    return problems


def _length(payload: bytes) -> bytes:
    if len(payload) > 127:
        # The binding reads every length as one byte.
        raise ValueError(f"{len(payload)} bytes is too long for one protocol message")
    return bytes([len(payload)])


def app_link_message(link: str) -> bytes:
    """The RemoteMessage that asks the device to open `link`."""
    inner = b"\x0a" + _length(link.encode("utf-8")) + link.encode("utf-8")
    return bytes.fromhex(APP_LINK_FIELD) + _length(inner) + inner


def presses(remote: str) -> dict[str, list[str]]:
    """Remote button id -> [Thing channel, command] the rule hands the binding."""
    table = {button: ["keycode", str(code)] for button, code in KEYCODES[remote].items()}
    table.update({button: ["debug", RAW + app_link_message(link).hex()]
                  for button, link in APP_LINKS[remote].items()})
    return table


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = thing_uid(device)
    port = (device.get("timing") or {}).get("port", PORT)
    out.things[uid] = {
        "label": f"{house.label(cast('Device', device))} Android TV remote",
        "location": house.room_label(device["room"]),
        "config": {"ipAddress": device["ip"], "googletvPort": port},
    }
    items = out.items
    items[f"{ident}_android_power"] = _items.control_point(
        ident, "Switch", "Power (Android TV)", ["Control", "Power"], f"{uid}:power", icon="television")
    if is_tv(device):
        items[f"{ident}_android_mute"] = _items.control_point(
            ident, "Switch", "Mute", ["Control", "SoundVolume"], f"{uid}:mute", icon="soundvolume_mute")
        items[f"{ident}_android_volume"] = _items.control_point(
            ident, "Dimmer", "Volume", ["Control", "SoundVolume"], f"{uid}:volume", icon="soundvolume",
            state={key: VOLUME[key] for key in ("min", "max", "step")})
    items[f"{ident}_android_app"] = point(ident, "String", "App", ["Status", "Info"], f"{uid}:app", guarded=True)
    items[f"{ident}_key"] = _items.control_point(ident, "String", "Remote Key", ["Control"])
    label = pairing_label(device)
    _pairing.render(device, house, out, NAME, label=label, confirm=CONFIRM.format(label=label), **PAIRING)
    _commands.render(device, out, NAME)


def pairing(device: dict) -> dict | None:
    """Pairing contract (oh_integrations._pairing) when controlled, else None."""
    if not uses(device, PHASE, NAME):
        return None
    label = pairing_label(device)
    return {
        **PAIRING,
        "integration": NAME,
        "label": label,
        "confirm": CONFIRM.format(label=label),
        "item": _pairing.item(device),
        "status": _pairing.status_item(device),
        "problem": problem(device),
    }


def manual_items(device: dict) -> int:
    """Number of manual command Items handled by the device's actor."""
    return 4 if is_tv(device) else 3


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's field (oh_integrations): power, driven by this
    integration's own rule with read-before-toggle safety."""
    return {f"{device['id']}_android_power": {
        "kind": "androidtv",
        "values": ["ON", "OFF"],
        "power": "toggle",
        "feedback": {"kind": "push"},
    }}


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, Thing UID, remote, volume scale (null
    for a device whose volume is not its own) and keystore (under userdata)."""
    return [{"id": d["id"], "thing": thing_uid(d), "remote": remote_of(d),
             "volume": dict(VOLUME) if is_tv(d) else None, "keystore": KEYSTORE.format(id=d["id"])} for d in devices]


def rule(devices: list[dict]) -> str:
    """androidtv_googletv.js with its tables filled in for these devices."""
    used = sorted({remote_of(d) for d in devices}, key=REMOTES.index)
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices),
                                       "PRESSES": {remote: presses(remote) for remote in used}})


#: One script context per device; its polls and commands stay ordered.
rule_path, files = _script.per_device(RULE, rule)


def power_item(device: dict) -> str | None:
    """The device's power Item; None unless controlled."""
    return f"{device['id']}_android_power" if uses(device, PHASE, NAME) else None


def playing(device: dict) -> dict:
    """What it plays (oh_features.playing): the app on screen, while on."""
    ident = device["id"]
    return {"power": power_item(device),
            "apps": [{"item": f"{ident}_android_app", "names": dict(APP_NAMES), "idle": list(IDLE_APPS),
                      "package": True}]}


def wake_item(device: dict) -> str | None:
    """The Item ON wakes the device with, when a room chooses it
    (oh_features.routing): its power Item, which presses the power key only
    while the device reports itself off."""
    return power_item(device)


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, PHASE, NAME):
        return None
    ident = device["id"]
    offered: dict = {"key": f"{ident}_key", "power": f"{ident}_android_power"}
    if is_tv(device):
        offered["mute"] = f"{ident}_android_mute"
        offered["volume"] = {"item": f"{ident}_android_volume", **VOLUME}
    return offered


def remote_layouts(device: dict | None = None) -> dict[str, dict]:
    """Button layout definitions for Android TV and NVIDIA SHIELD devices."""
    return {
        "nvidia_shield": {
            "label": "SHIELD remote",
            "corners": ["power", "menu", "back", "home"],
            "playback": ["rewind", "play_pause", "fast_forward"],
            "keys": ["netflix"],
        },
        "android_tv": {
            "label": "Android TV remote",
            "corners": ["power", None, "back", "home"],
            "playback": ["rewind", "play", "pause", "fast_forward"],
            "channel": True,
            "keys": ["netflix", "youtube"],
        },
    }


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the device's Thing talking to HOST:PORT instead."""
    host, _, port = address.rpartition(":")

    def change(thing: dict) -> None:
        thing["config"].update(ipAddress=host, googletvPort=int(port))
    return {**files, FILE: retarget(files[FILE], FILE, thing_uid(device), change)}
