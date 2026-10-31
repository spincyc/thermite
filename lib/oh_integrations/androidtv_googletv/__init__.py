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
  turns a command into one VOLUME_UP or VOLUME_DOWN press per step, held to
  the TV's volume_max (devices.yaml; default DEFAULT_VOLUME_MAX). A SHIELD's
  sound is the room's audio device's.
- `<id>_android_app` (String): the app on screen, display-only.
- `<id>_key` (String): a remote button id of its remote (keys()). The rule
  presses its Android key code (KEYCODES) or opens its app (APP_LINKS); any
  other id is dropped.
- `<id>_pairing` and `<id>_pairing_status` (oh_integrations._pairing): the
  rule hands REQUEST and the code to the binding's pincode channel, and
  keeps the status from the Thing's: ONLINE is paired; OFFLINE for want of
  a PIN is not paired. The binding asks for a code on PORT + 1, and drops
  the request silently when that port does not connect, so the rule tries
  it first, also before pairing again, and shows `closed` instead.

The rule hands its presses to the Thing's handler, as the binding's console
command does, so no Item links the binding's keycode, debug or pincode
channels: a client presses only what the rule allows. It holds
oh_conf.RULE_SENDS, and oh_render.read_only() keeps the Items on the
binding's channels display-only (COMMANDING_BINDINGS), so a read-only copy
commands nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from oh_conf import RULE_SENDS, Addon, House, YamlFile, point, retarget, uses

from .. import _pairing

NAME = "androidtv_googletv"
PHASE = "control"
FILE = "yaml/googletv.yaml"
HEADER = (
    "Android and Google TVs and NVIDIA SHIELDs over the Android TV Remote",
    "protocol (androidtv binding, TLS port 6466), paired from the Pairing page;",
    "the binding's keystore in userdata holds the pairing. Power and mute are",
    "the binding's own channels; the rule conf/automation/js/androidtv_googletv.js",
    "presses the remote keys, steps the volume, held to volume_max, and pairs.",
    "Items are autoupdate off: they show only what the device reports.",
)
ADDONS = (Addon("binding", "androidtv"),
          Addon("automation", "jsscripting", "the Android TV key, volume and pairing rule (androidtv_googletv.js)"))
ASSETS: dict[str, str] = {}
PORT = 6466
#: Seconds from a command to its read-back: none, the TV reports a change at once.
READ_BACK = 0
RULE = "automation/js/androidtv_googletv.js"
SOURCE = Path(__file__).resolve().parent / "androidtv_googletv.js"
DEVICES_MARK = "const DEVICES = null;"
PRESSES_MARK = "const PRESSES = null;"
#: The TV's volume scale, as the binding reports it.
MAX_VOLUME = 100
#: The volume a TV is held to unless devices.yaml sets volume_max.
DEFAULT_VOLUME_MAX = 40
TV = "android_tv"
SHIELD = "nvidia_shield"
#: The remote layout (web/remote/remotes.js) each kind of device is drawn with.
REMOTES = (TV, SHIELD)
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
VOLUME_UP = 24
VOLUME_DOWN = 25
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
#: The binding sends a debug-channel command starting with this as the raw
#: protocol message whose hex follows.
RAW = "GOOGLETV_RAW_"
#: RemoteMessage field 90, RemoteAppLinkLaunchRequest, as its protobuf key.
APP_LINK_FIELD = "d205"
#: The code a device shows while it pairs: six hexadecimal digits.
CODE = re.compile(r"[0-9A-Fa-f]{6}")
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


def pair_command(device: dict, code: str | None = None) -> str:
    """The openHAB console command that asks the device to show a pairing
    code (no code) or sends it the code it shows."""
    if code is not None and not CODE.fullmatch(code):
        raise ValueError(f"{code!r} is not the six letters and digits (0-9, A-F) a TV shows")
    return f"openhab:androidtv {thing_uid(device)} pincode {code.upper() if code else 'REQUEST'}"


def remote_of(device: dict) -> str:
    """The remote a device is drawn with: a SHIELD's, else a TV's."""
    return SHIELD if device.get("make") == "NVIDIA" else TV


def is_tv(device: dict) -> bool:
    return remote_of(device) == TV


def keys(device: dict) -> dict[str, int | str]:
    """Remote button id -> what it presses on this device: a key code or an app link."""
    remote = remote_of(device)
    return {**KEYCODES[remote], **APP_LINKS[remote]}


def volume_max(device: dict) -> int:
    """The highest volume the TV may be stepped to."""
    return device.get("volume_max", DEFAULT_VOLUME_MAX)


def pairing_label(device: dict) -> str:
    """What the Pairing page calls the device."""
    if not is_tv(device):
        return "SHIELD"
    return f"{device['make']} TV" if device.get("make") else "TV"


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        # volume_max means another scale to denonmarantz_http, so only a TV's is checked here.
        return []
    problems = []
    if device.get("kind") != "tv" and device.get("make") != "NVIDIA":
        problems.append(f"{device.get('id')}: {NAME} drives a TV or an NVIDIA SHIELD, not a "
                        f"{device.get('make')} {device.get('kind')}")
    cap = device.get("volume_max")
    if cap is not None and not is_tv(device):
        problems.append(f"{device.get('id')}: volume_max is for a TV; a SHIELD's sound is the room's audio device's")
    elif cap is not None and not (isinstance(cap, int) and not isinstance(cap, bool) and 0 <= cap <= MAX_VOLUME):
        problems.append(f"{device.get('id')}: volume_max {cap!r} is not a whole number 0-{MAX_VOLUME}")
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


def _control(ident: str, type_: str, label: str, tags: list[str], channel: str, *,
             icon: str | None = None, state: dict | None = None) -> dict:
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[ident], tags=tags, autoupdate=False, channel=channel)
    if state:
        item["metadata"] = {"stateDescription": {"config": state}}
    return item


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = thing_uid(device)
    out.things[uid] = {
        "label": f"{house.label(device)} Android TV remote",
        "location": house.room_label(device["room"]),
        "config": {"ipAddress": device["ip"], "googletvPort": PORT},
    }
    items = out.items
    items[f"{ident}_android_power"] = _control(ident, "Switch", "Power (Android TV)", ["Control", "Power"],
                                               f"{uid}:power", icon="television")
    if is_tv(device):
        items[f"{ident}_android_mute"] = _control(ident, "Switch", "Mute", ["Control", "SoundVolume"],
                                                  f"{uid}:mute", icon="soundvolume_mute")
        items[f"{ident}_android_volume"] = _control(ident, "Dimmer", "Volume", ["Control", "SoundVolume"],
                                                    f"{uid}:volume", icon="soundvolume",
                                                    state={"min": 0, "max": volume_max(device)})
    items[f"{ident}_android_app"] = point(ident, "String", "App", ["Status", "Info"], f"{uid}:app", guarded=True)
    items[f"{ident}_key"] = {"type": "String", "label": "Remote Key", "groups": [ident], "tags": ["Control"],
                             "autoupdate": False}
    label = pairing_label(device)
    _pairing.render(device, house, out, NAME, label=label, confirm=CONFIRM.format(label=label), **PAIRING)


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, Thing UID, remote, volume limit (null
    for a device whose volume is not its own) and keystore (under userdata)."""
    return [{"id": d["id"], "thing": thing_uid(d), "remote": remote_of(d),
             "max": volume_max(d) if is_tv(d) else None, "keystore": KEYSTORE.format(id=d["id"])} for d in devices]


def _line(name: str, value: object) -> str:
    return f"const {name} = {json.dumps(value, separators=(',', ':'))};"


def rule(devices: list[dict]) -> str:
    """androidtv_googletv.js with its tables filled in for these devices."""
    source = SOURCE.read_text(encoding="utf-8")
    for mark in (DEVICES_MARK, PRESSES_MARK, RULE_SENDS):
        if source.split("\n").count(mark) != 1:
            raise ValueError(f"{SOURCE} must contain the line {mark!r} exactly once")
    origin = SOURCE.relative_to(SOURCE.parents[3])
    header = (f"// Generated by bin/thermite generate from {origin} and\n"
              "// instance/devices.yaml; do not edit.\n")
    used = sorted({remote_of(d) for d in devices}, key=REMOTES.index)
    return header + (source.replace(DEVICES_MARK, _line("DEVICES", table(devices)))
                     .replace(PRESSES_MARK, _line("PRESSES", {remote: presses(remote) for remote in used})))


def files(devices: list[dict], house: House) -> dict[str, str]:
    """The conf files rendered from all of this integration's devices."""
    return {RULE: rule(devices)}


def power_item(device: dict) -> str | None:
    """The device's power Item; None unless controlled."""
    return f"{device['id']}_android_power" if uses(device, PHASE, NAME) else None


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
        offered["volume"] = {"item": f"{ident}_android_volume", "max": volume_max(device), "step": 1, "unit": "",
                             "offset": 0}
    return offered


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the device's Thing talking to HOST:PORT instead."""
    host, _, port = address.rpartition(":")

    def change(thing: dict) -> None:
        thing["config"].update(ipAddress=host, googletvPort=int(port))
    return {**files, FILE: retarget(files[FILE], FILE, thing_uid(device), change)}
