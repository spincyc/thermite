"""Main UI's home screen: an Overview page and no model tabs.

Main UI is for settings; everyone else uses the remote (oh_features.remote).
Its home screen shows the page whose uid is `overview` and, under it, tabs
listing the semantic model (Locations, Equipment, Properties) unless the
page whose uid is `home` hides them. Both go into yaml/pages.yaml:

- home: all three model tabs hidden. They list every point Item, which helps
  no one choosing a source, and Settings has the model for whoever needs it.
- overview: when the house has routing, a link to the remote and each room's
  Source selector; then each room's devices, online or not, from the network
  ping Items (devices without one are left out).

A page made in Main UI under either uid wins over its file: openHAB refuses
the second and logs "It exists already" (oh_sandbox.MODEL_PATTERNS).
"""

from __future__ import annotations

from oh_conf import Conf, House, label_item, layout_page, list_card_column

from . import remote, remote_cameras
from .routing import item_name, room_label

NAME = "overview"
FILE = "yaml/pages.yaml"
HEADER = (
    "Main UI's home screen (lib/oh_features/overview.py): the Overview page and",
    "the model tabs hidden. YAML pages are read-only in Main UI.",
)
MODEL_TABS = ("locations", "equipment", "properties")
COLUMN = {"width": "100", "medium": "50"}


def validate(data: dict) -> list[str]:
    return []


def remote_link() -> dict:
    return {"component": "oh-list-item", "config": {
        "title": "Open the remote",
        "footer": "Rooms, sources and buttons. On a phone, add it to the home screen.",
        "icon": "f7:tv",
        "action": "url",
        "actionUrl": remote.URL,
        "actionUrlSameWindow": True,
    }}


def online_item(item: str, title: str) -> dict:
    """A device's name with an Online, Offline or Unknown badge."""
    state = f"items.{item}.state"
    return {"component": "oh-list-item", "config": {
        "title": title,
        "badge": f"={state} === 'ON' ? 'Online' : {state} === 'OFF' ? 'Offline' : 'Unknown'",
        "badgeColor": f"={state} === 'ON' ? 'green' : {state} === 'OFF' ? 'red' : 'gray'",
    }}


def render(house: House, conf: Conf) -> None:
    columns = []
    routing = house.data.get("routing")
    if routing is not None:
        columns.append(list_card_column(dict(COLUMN), "Remote", [remote_link()]))
        columns.append(list_card_column(dict(COLUMN), "Source by room", [
            label_item(item_name(room), room_label(house.data, room), action="options", actionItem=item_name(room))
            for room in house.rooms if room in routing["rooms"]]))
    if remote_cameras.manifest(house)["cameras"]:
        columns.append(list_card_column(dict(COLUMN), "Cameras", [{"component": "oh-list-item", "config": {
            "title": "Open cameras", "footer": "Protect status, snapshots and live video.", "icon": "f7:camera",
            "action": "url", "actionUrl": remote.URL + "cameras.html", "actionUrlSameWindow": True}}]))
    for room in house.rooms:
        entries = [online_item(f"{device['id']}_online", house.label(device)) for device in house.devices
                   if device["room"] == room and conf.item(f"{device['id']}_online")]
        if entries:
            columns.append(list_card_column(dict(COLUMN), house.room_label(room), entries))
    out = conf.yaml_file(FILE, HEADER)
    out.pages["home"] = {"component": "oh-home-page", "config": {"hiddenModelTabs": list(MODEL_TABS)}}
    out.pages["overview"] = layout_page("Overview", columns)
