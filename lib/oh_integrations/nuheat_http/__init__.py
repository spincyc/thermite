"""NuHeat thermostats through the localhost climate helper (bin/thermite climate,
lib/oh_climate/daemon.py).

OAuth tokens and per-device commissioning contracts (serial numbers, verified
raw temperature encoding, raw min/max limits, writable modes, and verified mode
codes) stay strictly inside private 0600 helper state, never in devices.yaml or
conf/. The generated per-device rule conf/automation/js/nuheat_http_<id>.js
polls GET http://127.0.0.1:13629/devices/<id> every 60 s and posts mode and
setpoint commands to POST /devices/<id>/mode.
"""

from __future__ import annotations

from pathlib import Path

from oh_conf import Addon, House, YamlFile, uses
from oh_ports import CLIMATE_PORT
from oh_shape import NUMBER, TEXT, Key
import oh_units

from .. import _commands, _script

NAME = "nuheat_http"
PHASE = "control"
NEEDS_IP = False
FILE = "yaml/climate.yaml"
HELPER_SERVICE = oh_units.CLIMATE
HEADER = (
    "NuHeat thermostats through the localhost climate helper (bin/thermite climate),",
    "which holds OAuth tokens and private commissioning contracts in 0600 service",
    "state: the rule conf/automation/js/nuheat_http_<device>.js polls it for",
    "status and sends mode and setpoint commands. Items are autoupdate off.",
)
ADDONS = (Addon("automation", "jsscripting", "the NuHeat climate rule (nuheat_http.js)"),)
ASSETS: dict[str, str] = {}
PROBED = False
READS = True
ROUTE_OPTIONAL = True
MONITORED = True
POLL_S = 60
READ_BACK = 0
HELPER = f"http://127.0.0.1:{CLIMATE_PORT}"
RULE = "automation/js/nuheat_http.js"
SOURCE = Path(__file__).resolve().parent / "nuheat_http.js"
MODES = ("Auto", "Hold", "Manual")
_DEFAULT_STEP = 100
_SUPPORTED_KINDS = frozenset({"thermostat"})
_MODE_OPTIONS = "Auto=Auto,Hold=Hold,Manual=Manual"
_WRITE_STATE_OPTIONS = (
    "idle=Idle,acknowledged=Acknowledged,confirmed=Confirmed,"
    "different=Different,indeterminate=Indeterminate"
)
_STATUS_OPTIONS = (
    "ok=OK,uncommissioned=Uncommissioned,offline=Offline,"
    "stale=Stale,error=Fault,unavailable=Unavailable"
)

DEVICE_KEYS = {
    "scale": Key(TEXT, "display temperature unit for the remote UI", choices=("F", "C")),
    "step": Key(NUMBER, "setpoint step size in raw units for UI +/- buttons"),
}


def _config(device: dict) -> dict:
    cfg = (device.get("config") or {}).get(NAME)
    return cfg if isinstance(cfg, dict) else {}


def validate(device: dict) -> list[str]:
    problems: list[str] = []
    ident = device.get("id")
    cfg = _config(device)
    if "scale" in cfg and cfg["scale"] not in ("F", "C"):
        problems.append(f"{ident}: config.{NAME}.scale must be 'F' or 'C'")
    if "step" in cfg:
        step = cfg["step"]
        if type(step) is not int or step <= 0:
            problems.append(f"{ident}: config.{NAME}.step must be a positive integer")
    if uses(device, PHASE, NAME) and device.get("kind") not in _SUPPORTED_KINDS:
        problems.append(f"{ident}: {NAME} drives a thermostat, not a {device.get('kind')}")
    return problems


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    readonly = {"stateDescription": {"config": {"readOnly": True}}}
    out.items[f"{ident}_temperature"] = {
        "type": "Number", "label": "Temperature", "icon": "temperature",
        "groups": [ident], "tags": ["Measurement", "Temperature"],
        "autoupdate": False, "metadata": readonly,
    }
    out.items[f"{ident}_heating_call"] = {
        "type": "Switch", "label": "Heating", "icon": "fire",
        "groups": [ident], "tags": ["Status"],
        "autoupdate": False, "metadata": readonly,
    }
    out.items[f"{ident}_hold_until"] = {
        "type": "DateTime", "label": "Hold Until", "icon": "time",
        "groups": [ident], "tags": ["Status"],
        "autoupdate": False, "metadata": readonly,
    }
    out.items[f"{ident}_online"] = {
        "type": "Switch", "label": "Online", "icon": "network",
        "groups": [ident], "tags": ["Status"],
        "autoupdate": False, "metadata": readonly,
    }
    out.items[f"{ident}_has_error"] = {
        "type": "Switch", "label": "Fault", "icon": "error",
        "groups": [ident], "tags": ["Alarm"],
        "autoupdate": False, "metadata": readonly,
    }
    out.items[f"{ident}_stale"] = {
        "type": "Switch", "label": "Stale Reading", "icon": "time",
        "groups": [ident], "tags": ["Status"],
        "autoupdate": False, "metadata": readonly,
    }
    out.items[f"{ident}_write_state"] = {
        "type": "String", "label": "Write State",
        "groups": [ident], "tags": ["Status"],
        "autoupdate": False,
        "metadata": {"stateDescription": {"config": {"readOnly": True, "options": _WRITE_STATE_OPTIONS}}},
    }
    out.items[f"{ident}_climate_status"] = {
        "type": "String", "label": "Climate Status",
        "groups": [ident], "tags": ["Status"],
        "autoupdate": False,
        "metadata": {"stateDescription": {"config": {"readOnly": True, "options": _STATUS_OPTIONS}}},
    }
    out.items[f"{ident}_climate_mode"] = {
        "type": "String", "label": "Climate Mode", "icon": "heating",
        "groups": [ident], "tags": ["Control"],
        "autoupdate": False,
        "metadata": {
            "stateDescription": {"config": {"options": _MODE_OPTIONS}},
            "commandDescription": {"config": {"options": _MODE_OPTIONS}},
        },
    }
    out.items[f"{ident}_climate_setpoint"] = {
        "type": "Number", "label": "Target Setpoint", "icon": "temperature",
        "groups": [ident], "tags": ["Setpoint", "Temperature"],
        "autoupdate": False,
    }
    _commands.render(device, out, NAME)


def fields(device: dict) -> dict[str, dict]:
    """Declare mode field with 60 s poll feedback; setpoint is numeric and
    accounted for via manual_items(device)."""
    return {
        f"{device['id']}_climate_mode": {
            "kind": "nuheat",
            "values": list(MODES),
            "feedback": {"kind": "poll", "interval_s": POLL_S},
        },
    }


def manual_items(device: dict) -> int:
    """Two manual command Items per thermostat: climate_mode and climate_setpoint."""
    return 2


def problem(device: dict) -> str | None:
    return _commands.problem_item(device["id"]) if uses(device, PHASE, NAME) else None


def climate(device: dict) -> dict | None:
    """Room climate descriptor for the remote UI and gateway contract."""
    if not uses(device, PHASE, NAME):
        return None
    ident = device["id"]
    cfg = _config(device)
    scale = cfg.get("scale")
    step = cfg.get("step", _DEFAULT_STEP)
    return {
        "device": ident,
        "label": device.get("name") or device.get("model") or ident,
        "scale": scale if scale in ("F", "C") else None,
        "step": step if type(step) is int and step > 0 else _DEFAULT_STEP,
        "items": {
            "temperature": f"{ident}_temperature",
            "setpoint": f"{ident}_climate_setpoint",
            "mode": f"{ident}_climate_mode",
            "heating": f"{ident}_heating_call",
            "hold_until": f"{ident}_hold_until",
            "online": f"{ident}_online",
            "error": f"{ident}_has_error",
            "stale": f"{ident}_stale",
            "write_state": f"{ident}_write_state",
            "status": f"{ident}_climate_status",
            "problem": _commands.problem_item(ident),
        },
        "modes": list(MODES),
    }


def table(devices: list[dict]) -> list[dict]:
    """The rule's device table: id and localhost helper URL."""
    return [{"id": d["id"], "helper": HELPER} for d in devices]


def rule(devices: list[dict]) -> str:
    """nuheat_http.js with its device table filled in."""
    return _script.rule(SOURCE, NAME, {"DEVICES": table(devices)})


#: One script context per thermostat; polls and commands stay serialized.
rule_path, files = _script.per_device(RULE, rule)


def aim(files: dict[str, str], device: dict, address: str) -> dict[str, str]:
    """Point the thermostat rule at a stand-in helper at HOST:PORT."""
    return _script.aimed(files, (rule_path(device), RULE), device,
                         lambda entry: entry.update(helper=f"http://{address}"))
