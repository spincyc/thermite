"""Static configuration TypedDicts for type-checked Thermite modules.

A leaf typing module (imports nothing of thermite's) so any module can type
the host file (instance/host.yaml) without creating import cycles.
tests/test_host.py holds HostFile to oh_host.SHAPE, as
tests/test_house_types.py holds HouseFile to oh_inventory.shape().
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class Gateway(TypedDict, total=False):
    port: int
    listen: list[str]


class MetricsHost(TypedDict, total=False):
    units: list[str]
    retention: str
    retention_size: str
    yoke_targets: list[str]
    node_exporter_uid: int
    prometheus_port: int
    node_exporter_port: int
    blackbox_port: int
    exporter_port: int
    grafana_port: int


class GuestHttp(TypedDict):
    port: NotRequired[int]
    listen: list[str]
    clients: list[str]
    names: NotRequired[list[str]]


class MusicHost(TypedDict, total=False):
    libraries: dict[str, str] | None
    port: int
    stream_port: int


class ShareHost(TypedDict):
    type: Literal["cifs", "nfs"]
    server: str
    share: NotRequired[str]
    export: NotRequired[str]
    where: str
    writable: NotRequired[bool]
    version: NotRequired[str]


class HostFile(TypedDict):
    version: NotRequired[Literal[1]]
    timezone: str
    root: NotRequired[str]
    http_port: NotRequired[int]
    https_port: NotRequired[int]
    uid: NotRequired[int]
    gid: NotRequired[int]
    gateway: NotRequired[Gateway | None]
    metrics: NotRequired[MetricsHost | None]
    guest_http: NotRequired[GuestHttp | None]
    music: NotRequired[MusicHost | None]
    shares: NotRequired[dict[str, ShareHost] | None]
