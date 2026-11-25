"""Read-only cameras through the pinned native UniFi Protect binding.

Only monitoring channels are linked. The authenticated controller and NVR
bridges and cameras live in the runtime's private JSONDB, provisioned by
`thermite cameras configure`; credentials never belong to generated files.
The NVR always has autoManageToken=false, so an absent token cannot create
an API key on the console.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import TYPE_CHECKING, cast

from oh_conf import Addon, House, YamlFile, config as device_config, point, uses
from oh_shape import MAPPING, NUMBER, TEXT, TRUTH, Key

if TYPE_CHECKING:
    from oh_inventory import Device

NAME = "unifi_protect"
PHASE = "observe"  # read-only authenticated observer; sends no control commands
FILE = "yaml/cameras.yaml"
HEADER = (
    "UniFi Protect cameras: read-only connection, recording, motion and doorbell status.",
    "Native WebRTC and snapshots are served by the runtime, without camera tokens in browsers.",
    "The native Things are provisioned by thermite cameras configure, allowing staged WebRTC;",
    "no console credentials or API keys are generated here.",
)
ADDONS = (Addon("binding", "unifi", "native Protect status, snapshots and WebRTC"),
          Addon("automation", "jsscripting", "timestamp received camera motion events"))
ASSETS: dict[str, str] = {}
CONTROLLER_UID = "unifi:controller:protect_console"
NVR_UID = "unifi:nvr:protect"
STATUS_GROUP = "house_camera_status"
RULE = "automation/js/unifi_protect.js"
DEVICE_KEYS = {
    "device_id": Key(TEXT, "the camera's 24-character Protect id"),
    "package_camera": Key(TRUTH, "whether a doorbell has a second camera, for packages"),
}
#: The console's host and port (oh_features.remote_cameras validates them).
HOUSE_KEYS = {"protect": Key(MAPPING, "the UniFi Protect console the cameras are on; its credentials are not kept here", keys={
    "host": Key(TEXT, "the console's IPv4 address", required=True),
    "port": Key(NUMBER, "the console's HTTPS port; 443 when left out")})}
HOUSE_ITEMS = (STATUS_GROUP,)
#: The motion-timestamp rule only updates Items.
ITEM_ONLY_RULES = (RULE,)
#: The NVR bridge and camera Things live in runtime JSONDB, not generated YAML.
RUNTIME_THINGS = True
#: Cameras and their NVR bridge are always-on monitored health checks.
MONITORED = True
DEVICE_ID = re.compile(r"[a-f0-9]{24}")
CHANNELS = {
    "online": ("Switch", "Connected", None),
    "recording": ("Switch", "Recording", "is-recording"),
    "motion": ("Contact", "Motion", "motion-contact"),
    "lastMotion": ("DateTime", "Last motion received", None),
}
DOORBELL_CHANNELS = {
    "ring": ("Switch", "Ringing", "is-ringing"),
    "lastRing": ("DateTime", "Last ring", "last-ring"),
}
SUFFIXES = {"lastMotion": "last_motion", "lastRing": "last_ring"}


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    where = device.get("id")
    config = device_config(device, NAME)
    if device.get("kind") not in {"camera", "doorbell"}:
        return [f"{where}: {NAME} requires kind camera or doorbell"]
    if not config or not DEVICE_ID.fullmatch(str(config.get("device_id", ""))):
        return [f"{where}: config.unifi_protect.device_id must be the camera's 24-character Protect id"]
    problems = []
    for name in set(config) - {"device_id", "package_camera"}:
        problems.append(f"{where}: unknown config.unifi_protect field {name!r}")
    if "package_camera" in config and type(config["package_camera"]) is not bool:
        problems.append(f"{where}: config.unifi_protect.package_camera must be true or false")
    if config.get("package_camera") and device.get("kind") != "doorbell":
        problems.append(f"{where}: only a doorbell may have a package camera")
    return problems


def validate_house(data: dict) -> list[str]:
    cameras_found = [d for d in data.get("devices", []) if uses(d, PHASE, NAME)]
    if not cameras_found:
        return []
    config = data.get("protect")
    if not isinstance(config, dict):
        return ["protect: cameras need the console's host and optional port"]
    problems = []
    try:
        ipaddress.IPv4Address(config.get("host"))
    except (ValueError, TypeError):
        problems.append("protect.host must be the console's IPv4 address")
    if type(config.get("port", 443)) is not int or not 1 <= config.get("port", 443) <= 65535:
        problems.append("protect.port must be an integer from 1 to 65535")
    if set(config) - {"host", "port"}:
        problems.append("protect accepts only host and port; credentials belong to runtime JSONDB")
    ids = [device_config(d, NAME).get("device_id") for d in cameras_found
           if device_config(d, NAME).get("device_id") is not None]
    if len(ids) != len(set(ids)):
        problems.append("config.unifi_protect.device_id must identify one camera only")
    return problems


def thing_uid(device: dict) -> str:
    return f"unifi:camera:protect:{device['id']}"


def transport(device: dict) -> str | None:
    """Shared Protect NVR bridge Thing through which cameras report status."""
    return NVR_UID if uses(device, PHASE, NAME) else None


def item_names(device: dict) -> dict[str, str]:
    channels = {**CHANNELS, **(DOORBELL_CHANNELS if device["kind"] == "doorbell" else {})}
    return {name: f"{device['id']}_{SUFFIXES.get(name, name)}" for name in channels}


def render(device: dict, house: House, out: YamlFile) -> None:
    uid = thing_uid(device)
    out.items.setdefault(STATUS_GROUP, {"type": "Group", "label": "Camera status", "autoupdate": False,
                                       "metadata": {"stateDescription": {"config": {"readOnly": True}}}})
    channels = {**CHANNELS, **(DOORBELL_CHANNELS if device["kind"] == "doorbell" else {})}
    for key, name in item_names(device).items():
        kind, label, channel = channels[key]
        out.items[name] = (point(device["id"], kind, label, ["Status"], f"{uid}:{channel}") if channel else
                           {"type": kind, "label": label, "groups": [device["id"]],
                            "tags": ["Status", "Timestamp"] if kind == "DateTime" else ["Status"]})
        out.items[name].update(autoupdate=False, metadata={"stateDescription": {"config": {"readOnly": True}}})
        out.items[name]["groups"].append(STATUS_GROUP)
    # Pinned 5.2.3 documents is-connected but never creates/updates it.
    # Keep its real state channel private to the receive-only availability rule.
    out.items[f"{device['id']}_connection"] = point(device["id"], "String", "Reported connection", ["Status"],
                                                     f"{uid}:device-state")
    out.items[f"{device['id']}_connection"].update(
        autoupdate=False, metadata={"stateDescription": {"config": {"readOnly": True}}})


def native_thing(device: dict, house: House, *, webrtc: bool = False) -> dict:
    """Editable native Thing DTO; no credentials, no writable channel Items."""
    cfg = device_config(device, NAME)
    return {"UID": thing_uid(device), "thingTypeUID": "unifi:camera", "bridgeUID": NVR_UID,
            "label": device.get("name") or house.label(cast("Device", device)), "location": house.room_label(device["room"]),
            "configuration": {"deviceId": cfg["device_id"], "enableWebRTC": webrtc}}


def files(devices: list[dict], house: House) -> dict[str, str]:
    """Only observe native event triggers; never issue a camera command."""
    table = [{"thing": thing_uid(d), "channel": f"{thing_uid(d)}:motion-start",
              "item": item_names(d)["lastMotion"], "connection": f"{d['id']}_connection",
              "online": item_names(d)["online"]} for d in devices]
    script = """// Generated by bin/thermite generate; do not edit.
const {rules, triggers, items, things, time} = require('openhab');
const CAMERAS = %s;
for (const camera of CAMERAS) {
  rules.JSRule({id: camera.item, name: 'Camera motion received',
    description: 'Timestamp an observed native Protect motion event; no camera commands.',
    triggers: [triggers.ChannelEventTrigger(camera.channel)],
    execute: () => { const item = items.getItem(camera.item, true); if (item !== null) item.postUpdate(time.ZonedDateTime.now().toString()); }});
  rules.JSRule({id: camera.online + '_observer', name: 'Camera availability received',
    description: 'Combine native connection and bridge availability; no camera commands.',
    triggers: [triggers.ItemStateChangeTrigger(camera.connection), triggers.ThingStatusChangeTrigger(camera.thing),
      triggers.ThingStatusChangeTrigger('unifi:nvr:protect'), triggers.SystemStartlevelTrigger(100)],
    execute: () => {
      let state = 'UNDEF';
      try {
        const status = things.getThing(camera.thing).status;
        const bridge = things.getThing('unifi:nvr:protect').status;
        if (status === 'OFFLINE' || bridge === 'OFFLINE') state = 'OFF';
        else if (status === 'ONLINE' && bridge === 'ONLINE') {
          const conn = items.getItem(camera.connection, true);
          const reported = conn === null ? 'NULL' : conn.state;
          state = reported === 'CONNECTED' ? 'ON' : reported === 'DISCONNECTED' ? 'OFF' : 'UNDEF';
        }
      } catch (_) { /* the managed Things may not have been configured yet */ }
      const online = items.getItem(camera.online, true);
      if (online !== null && String(online.state) !== state) online.postUpdate(state);
    }});
}
""" % json.dumps(table, separators=(",", ":"))
    return {RULE: script,
            "services/unifiprotect.cfg": "# Native Protect media, on the LAN or VPN.\nuseStun = false\ndownloadBinaries = true\n"}


def descriptor(device: dict, house: House) -> dict:
    uid = thing_uid(device)
    qualities = ["high", "medium", "low"]
    if device_config(device, NAME).get("package_camera"):
        qualities.append("package")
    return {
        "id": device["id"], "label": device.get("name") or house.label(cast("Device", device)),
        "room": device["room"], "location": house.room_label(device["room"]),
        "kind": device["kind"], "model": device.get("model") or "UniFi camera",
        "doorbell": device["kind"] == "doorbell", "packageCamera": "package" in qualities,
        "thing": uid, "items": item_names(device),
        "media": {"snapshot": f"/unifi/media/image/{uid}",
                  "streams": {quality: f"/unifi/media/play/{uid}:{quality}" for quality in qualities}},
    }


def cameras(device: dict, house: House) -> dict | None:
    """Public camera descriptor for the remote Cameras page, or None."""
    if not uses(device, PHASE, NAME):
        return None
    return descriptor(device, house)
