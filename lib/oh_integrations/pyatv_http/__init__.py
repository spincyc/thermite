"""Apple TVs through the Apple TV helper (bin/thermite apple-tv, lib/oh_atv.py).

openHAB has no Apple TV binding, and tvOS takes remote control only over
Apple's Companion protocol, an encrypted session that needs a HAP pairing
first, which pyatv speaks. So a small helper on the openHAB host keeps one
pyatv connection open per Apple TV and takes plain HTTP requests on
localhost only (HELPER) from the rule conf/automation/js/pyatv_http_<device>.js
(pyatv_http.js beside this file, with its tables filled in). Every POLL the
rule asks the helper for each Apple TV's state, which the helper answers at
once from what the Apple TV last pushed, and posts it; a command's answer
carries the state too, so it is read back at once.

Items, per Apple TV:

- `<id>_power` (Switch): the last native report, ON awake, OFF asleep,
  UNDEF while unavailable or unknown; ON and OFF wake it or put it to sleep.
- `<id>_power_reported_at` (DateTime, read only): when the helper acquired
  its current native power snapshot or push. Polls and command acknowledgements
  retain that time; no timestamp means power cannot be freshly verified.
- `<id>_key` (String): a remote button id: a Siri Remote button (PRESSES)
  or an app it opens (APPS); any other id is dropped.
- `<id>_pairing` and `<id>_pairing_status` (oh_integrations._pairing): REQUEST
  makes the Apple TV show four digits, and the digits sent back pair the
  helper with it. The helper keeps what the pairing gives it in its own
  secrets directory (atv/secrets/<id> in production's state,
  var/secrets/pyatv/<id> for a test instance), never in conf/ or Git, so a
  helper with an empty one pairs again.
- `<id>_problem` and `<id>_pyatv_http_commands` (oh_integrations._commands): why
  the Apple TV did not take the rule's last command or cannot be read, as a
  code, and the rule's tally of its commands by outcome.

The rule holds oh_conf.RULE_SENDS, so oh_render.read_only() makes it read
the Apple TVs and send them nothing.
"""

from __future__ import annotations

from pathlib import Path

from oh_conf import Addon, House, YamlFile, uses
import oh_units
from .. import _commands, _script

from .. import _pairing

NAME = "pyatv_http"
PHASE = "control"
FILE = "yaml/appletv.yaml"
HELPER_SERVICE = oh_units.ATV
HEADER = (
    "Apple TVs through the Apple TV helper (bin/thermite apple-tv) on this host,",
    "which keeps one pyatv Companion connection per Apple TV: the rule",
    "conf/automation/js/pyatv_http_<device>.js asks it for their state and sends it",
    "their power, key and pairing commands. Items are autoupdate off: they",
    "show only what the helper reports.",
)
ADDONS = (Addon("automation", "jsscripting", "the Apple TV rule (pyatv_http.js)"),)
ASSETS: dict[str, str] = {}
REMOTE_LAYOUT = "apple_tv"
#: No fixed port to probe: the helper reaches an Apple TV on the Companion
#: port it announces over mDNS, which changes.
PROBED = False
#: The rule's own read of the helper says whether the Apple TV is paired and
#: connected.
READS = True
#: AirPlay, which every Apple TV announces and answers on.
DISCOVER_PORTS = (7000,)
DISCOVER_MDNS = ("_airplay", "_raop", "AirPlay", "AirTunes")
#: Where the rule reaches the helper: on the openHAB host itself, never the
#: LAN (bin/thermite apple-tv serve listens on 127.0.0.1 only).
from oh_ports import HELPER_PORT

HELPER = f"http://127.0.0.1:{HELPER_PORT}"
#: Seconds between polls of the Apple TV helper (pyatv_http.js POLL).
POLL_S = 5
#: Seconds from a command to its read-back: none, the helper returns its native
#: cached state. An acknowledgement does not imply a new power observation.
READ_BACK = 0
RULE = "automation/js/pyatv_http.js"
SOURCE = Path(__file__).resolve().parent / "pyatv_http.js"
#: Remote button id (web/remote/remotes.js) -> the helper's key (a pyatv
#: RemoteControl method). power is an Item of its own, never a key. back is
#: the Siri Remote's Back (menu) button and tv its TV button (home).
PRESSES = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "ok": "select",
    "back": "menu",
    "tv": "home",
    "play_pause": "play_pause",
}
#: Remote button id -> the bundle id of the app it opens, as the Office
#: Apple TV lists them (pyatv app_list).
APPS = {
    "netflix": "com.netflix.Netflix",
    "youtube": "com.google.ios.youtube",
}
KEYS: dict[str, str] = {**PRESSES, **APPS}
#: The Pairing page's description of an Apple TV (oh_integrations._pairing).
PAIRING = {
    "label": "Apple TV",
    "length": 4,
    "chars": "digits",
    "again": True,
    "hint": ("No code? On the Apple TV open Settings, then AirPlay and HomeKit, stay on that screen and ask "
             "again. Still nothing: restart the Apple TV (Settings, System, Restart)."),
}


_SUPPORTED_KINDS = frozenset({"streamer", "tv"})


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("kind") not in _SUPPORTED_KINDS:
        return [f"{device.get('id')}: {NAME} drives a streamer or tv, not a {device.get('kind')}"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    out.items[f"{ident}_power"] = {"type": "Switch", "label": "Power", "icon": "screen", "groups": [ident],
                                   "tags": ["Control", "Power"], "autoupdate": False}
    out.items[f"{ident}_power_reported_at"] = {
        "type": "DateTime", "label": "Native power reported", "groups": [ident], "tags": ["Status"],
        "autoupdate": False, "metadata": {"stateDescription": {"config": {"readOnly": True}}},
    }
    out.items[f"{ident}_key"] = {"type": "String", "label": "Remote Key", "groups": [ident], "tags": ["Control"],
                                 "autoupdate": False}
    _pairing.render(device, house, out, NAME, **PAIRING)
    _commands.render(device, out, NAME)


def problem(device: dict) -> str | None:
    """The device's problem Item (oh_integrations._commands) when this rule
    commands it, else None."""
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) else None


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id, address and the helper that drives it."""
    return [{"id": d["id"], "host": d["ip"], "helper": HELPER} for d in devices]


def rule(devices: list[dict]) -> str:
    """pyatv_http.js with its tables filled in for these devices."""
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices), "PRESSES": PRESSES, "APPS": APPS})


#: One script context per device; its polls and commands stay ordered.
rule_path, files = _script.per_device(RULE, rule)


def remote(device: dict) -> dict | None:
    """What the remote page drives (av-contract): None unless controlled."""
    if not uses(device, PHASE, NAME):
        return None
    ident = device["id"]
    return {"key": f"{ident}_key", "power": f"{ident}_power"}


def remote_layouts(device: dict | None = None) -> dict[str, dict]:
    """Button layout definition for Apple TV."""
    return {
        "apple_tv": {
            "label": "Siri Remote",
            "corners": ["power", None, "back", "tv"],
            "playback": ["play_pause"],
            "keyGroups": [{"label": "Apps", "keys": ["netflix", "youtube"], "columns": 2, "primary": True}],
        },
    }


def wake_item(device: dict) -> str | None:
    """The Item ON wakes the Apple TV with, when a room chooses it
    (oh_features.routing); None unless controlled."""
    return f"{device['id']}_power" if uses(device, PHASE, NAME) else None


def pairing(device: dict) -> dict | None:
    """Pairing contract (oh_integrations._pairing) when controlled, else None."""
    if not uses(device, PHASE, NAME):
        return None
    return {
        **PAIRING,
        "integration": NAME,
        "item": _pairing.item(device),
        "status": _pairing.status_item(device),
        "problem": problem(device),
    }


def manual_items(device: dict) -> int:
    """Number of manual command Items handled by the Apple TV's actor (power, key, pairing)."""
    return 3


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's field (oh_integrations): power, driven by
    this integration's own rule. Its actor also takes manual commands to
    the key and pairing Items."""
    return {f"{device['id']}_power": {
        "kind": "pyatv", "values": ["ON", "OFF"], "power": "absolute",
        "feedback": {"kind": "poll", "interval_s": POLL_S},
        "reports": {"on": {"value": "ON", "transition": False}, "off": {"value": "OFF", "transition": False}},
    }}


def native_power_observation(device: dict) -> dict | None:
    """Native power with its helper acquisition time, never a command ACK time."""
    if not uses(device, PHASE, NAME):
        return None
    return {"item": f"{device['id']}_power", "reported_at": f"{device['id']}_power_reported_at"}


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """`files` with the rule asking the helper at HOST:PORT for `device`,
    as the Apple TV at 127.0.0.1 (a stand-in helper answers for any)."""
    return _script.aimed(files, (rule_path(device), RULE), device,
                         lambda entry: entry.update(host="127.0.0.1", helper=f"http://{address}"))
