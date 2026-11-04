"""Secret-free native Protect camera catalogue shared by the web and TUI."""

from __future__ import annotations

import ipaddress

from oh_conf import Conf, House, uses
from oh_integrations import unifi_protect


def validate(data: dict) -> list[str]:
    cameras = [d for d in data.get("devices", []) if uses(d, unifi_protect.PHASE, unifi_protect.NAME)]
    if not cameras:
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
    ids = [d.get("protect", {}).get("device_id") for d in cameras if isinstance(d.get("protect"), dict)]
    if len(ids) != len(set(ids)):
        problems.append("protect.device_id must identify one camera only")
    return problems


def manifest(house: House, conf: Conf | None = None) -> dict:
    """The camera catalogue; built once per conf (Conf.derived) when given one."""
    def make():
        return {"version": 1, "statusGroup": unifi_protect.STATUS_GROUP, "pollSeconds": 5,
                "cameras": [unifi_protect.descriptor(device, house) for device in house.devices
                            if uses(device, unifi_protect.PHASE, unifi_protect.NAME)]}
    return make() if conf is None else conf.derived("remote_cameras", make)


def media(manifest: dict) -> dict:
    """{native media path: {"kind": "snapshot" | "sdp"}} of a manifest's
    cameras; ValueError for a path that is not exactly the integration's."""
    found = {}
    for camera in manifest["cameras"]:
        snapshot = camera["media"]["snapshot"]
        if not unifi_protect.SNAPSHOT_PATH.fullmatch(snapshot):
            raise ValueError("Camera snapshot must be an exact native path")
        found[snapshot] = {"kind": "snapshot"}
        for value in camera["media"]["streams"].values():
            if not unifi_protect.STREAM_PATH.fullmatch(value):
                raise ValueError("Camera stream must be an exact native path")
            found[value] = {"kind": "sdp"}
    return found
