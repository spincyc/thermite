"""Reachability by ping (network binding, pingdevice)."""

from __future__ import annotations

from oh_conf import Addon, House, YamlFile, point

NAME = "network_ping"
PHASE = "observe"
FILE = "yaml/network.yaml"
HEADER = (
    "Reachability of every inventory device that observes network_ping",
    "(network binding pingdevice; ICMP, plus ARP where arping is installed).",
    "No macAddress is configured, so the Wake-on-LAN action has nothing to send.",
)
ADDONS = (Addon("binding", "network"),)
ASSETS: dict[str, str] = {}


def validate(device: dict) -> list[str]:
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    ident = device["id"]
    uid = f"network:pingdevice:{ident}"
    out.things[uid] = {
        "label": f"{house.label(device)} ping",
        "location": house.room_label(device["room"]),
        "config": {"hostname": device["ip"]},
    }
    out.items[f"{ident}_online"] = point(ident, "Switch", "Online", ["Status"], f"{uid}:online",
                                         icon="network")
    out.items[f"{ident}_last_seen"] = point(ident, "DateTime", "Last Seen", ["Status", "Timestamp"],
                                            f"{uid}:lastseen", fmt="%1$tY-%1$tm-%1$td %1$tH:%1$tM:%1$tS",
                                            icon="time")
