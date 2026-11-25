"""Secret-free camera catalogue shared by the web and TUI."""

from __future__ import annotations

import re

import oh_integrations as integrations
from oh_conf import Conf, House

STATUS_GROUP = "house_camera_status"
#: Bounded relative native media path; no query, fragment, traversal or whitespace.
_MEDIA_PATH = re.compile(r"/[A-Za-z0-9_:@./-]+")
_MAX_MEDIA_PATH = 256


def _valid_media_path(value: object) -> bool:
    if not isinstance(value, str) or not 1 < len(value) <= _MAX_MEDIA_PATH:
        return False
    if not _MEDIA_PATH.fullmatch(value) or "//" in value or value.endswith("/"):
        return False
    return ".." not in value.split("/")


def validate(data: dict) -> list[str]:
    problems: list[str] = []
    for module in integrations.defining("cameras"):
        if module.validate_house:
            problems += module.validate_house(data)
    return problems


def manifest(house: House, conf: Conf | None = None) -> dict:
    """The camera catalogue; built once per conf (Conf.derived) when given one."""
    def make() -> dict:
        entries = []
        for device in house.devices:
            entry = next(
                (found for module in integrations.defining("cameras")
                 if (found := module.cameras(device, house)) is not None),
                None,
            )
            if entry is not None:
                entries.append(entry)
        return {"version": 1, "statusGroup": STATUS_GROUP, "pollSeconds": 5, "cameras": entries}
    return make() if conf is None else conf.derived("remote_cameras", make)


def media(manifest: dict) -> dict:
    """{native media path: {"kind": "snapshot" | "sdp"}} of a manifest's
    cameras; ValueError for a path that is not a safe relative native path."""
    found = {}
    for camera in manifest["cameras"]:
        cam_media = camera.get("media") or {}
        snapshot = cam_media.get("snapshot")
        if snapshot is not None:
            if not _valid_media_path(snapshot):
                raise ValueError("Camera snapshot must be an exact native path")
            found[snapshot] = {"kind": "snapshot"}
        for value in (cam_media.get("streams") or {}).values():
            if not _valid_media_path(value):
                raise ValueError("Camera stream must be an exact native path")
            found[value] = {"kind": "sdp"}
    return found
