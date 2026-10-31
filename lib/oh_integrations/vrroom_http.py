"""HDFury VRROOM HDMI switch over its web API.

One Number point, `<id>_input`: the input (0-3, the VRROOM's own numbers)
shown on TX0, read back every few seconds and switched by commanding the
Item. The VRROOM answers GET /ssi/infopage.ssi with its state as JSON
(`portseltx0` is TX0's input) and switches on GET /cmd?insel=<TX0> <TX1>,
as its own web page does; TX1 is sent as a copy of TX0 (4), which in
splitter mode it always is. Neither needs a login. A read-only String
`<id>_signal` reuses the same status response for measured input/output
video and audio, and separately labelled sink EDID capabilities.
"""

from __future__ import annotations

from oh_conf import Addon, House, YamlFile, uses

from . import _diagnostics as diag

NAME = "vrroom_http"
PHASE = "control"
FILE = "yaml/vrroom.yaml"
HEADER = (
    "HDMI switching over the HDFury VRROOM's web API (HTTP binding). TX0's",
    "input is polled with GET /ssi/infopage.ssi and switched with GET",
    "/cmd?insel=<input> 4. Items are autoupdate off: they show only what the",
    "switch reports.",
)
ADDONS = (
    Addon("binding", "http"),
    Addon("transformation", "jsonpath"),
    Addon("automation", "jsscripting", "the inline JS that builds matrix commands"),
)
ASSETS: dict[str, str] = {}
KINDS = {"hdmi_switch"}
#: The input numbers TX0 can show (RX0-RX3).
INPUTS = range(0, 4)
#: The insel value that makes one output copy the other.
COPY = 4
STATUS = "/ssi/infopage.ssi"
#: All that the Thing ever sends, named as lib/oh_matrix_sim's stand-in
#: records it: the status page and the insel command.
COMHEADS = frozenset({"get infopage.ssi", "insel"})
#: Seconds between polls, as for the matrices (oh_integrations._instr).
REFRESH = 2


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def url(address: str) -> str:
    """The web API URL of a VRROOM at HOST[:PORT]."""
    return f"http://{address}"


def input_item(device: dict) -> str:
    """The Item holding the input TX0 shows."""
    return f"{device['id']}_input"


def diagnostics(device: dict) -> dict:
    """Reported signals and sink capabilities, sharing the existing GET.

RX0/RX1 are the original signals for the TX0/TX1 processing paths, not
physical input sockets. See HDfury VRROOM manual, INFO section, page 19.
"""
    item = f"{device['id']}_signal"
    readings = []
    for output in range(2):
        for category, label, key in (
                ("video", "Input video", f"RX{output}"),
                ("video", "Output video", f"TX{output}"),
                ("audio", "Output audio", f"AUD{output}"),
                ("edid", "Sink video capabilities", f"SINK{output}"),
                ("edid", "Sink audio capabilities", f"EDIDA{output}")):
            readings.append({"category": category, "label": f"TX{output} {label.lower()}",
                             "item": item, "path": ["values", key],
                             "port": {"direction": "output", "number": output}})
    for label, key in (("Audio extractor output", "AUDOUT"), ("eARC return audio", "EARCRX")):
        readings.append({"category": "audio", "label": label, "item": item, "path": ["values", key]})
    return {"kind": NAME, "items": [item], "maxAgeSeconds": 15, "readings": readings}


def signal_js() -> str:
    fields = ["portseltx0", "portseltx1", "RX0", "RX1", "TX0", "TX1", "AUD0", "AUD1", "AUDOUT", "EARCRX",
              "SINK0", "SINK1", "EDIDA0", "EDIDA1"]
    return diag.json_snapshot(fields, required="portseltx0")


def files(devices: list[dict], house: House) -> dict[str, str]:
    return {"transform/vrroom-signal.js": diag.script_file(signal_js())}


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("kind") not in KINDS:
        return [f"{device.get('id')}: {NAME} drives an hdmi_switch, not a {device.get('kind')}"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    uid = thing_uid(device)
    out.things[uid] = {
        "label": f"{house.label(device)} web API",
        "location": house.room_label(device["room"]),
        "config": {
            "baseURL": url(device["ip"]),
            "refresh": REFRESH,
            "timeout": 3000,
            "delay": 100,
            "stateMethod": "GET",
            "commandMethod": "GET",
            # Offline and UNDEF when the switch stops answering, never stale.
            "strictErrorHandling": True,
        },
        "channels": {
            "input": {
                "type": "number",
                "label": "TX0 input",
                "config": {
                    "stateExtension": STATUS,
                    "stateTransformation": ["JSONPATH($.portseltx0)"],
                    # The binding escapes the space in "<input> 4" itself.
                    "commandExtension": "/cmd?insel=%2$s",
                    "commandTransformation": [f"JS(|parseInt(input) + ' {COPY}')"],
                    "refreshAfterCommand": True,
                },
            },
        },
    }
    out.items[input_item(device)] = {
        "type": "Number",
        "label": "TX0 input",
        "icon": "video",
        "groups": [device["id"]],
        "tags": ["Control"],
        "autoupdate": False,
        "channel": f"{uid}:input",
    }
    # Identical stateExtension: the HTTP binding reuses the input poll's
    # cached response instead of making a second request.
    out.things[uid]["channels"]["signal"] = diag.channel(
        "HDMI signal snapshot", "vrroom-signal.js", stateExtension=STATUS, refreshAfterCommand=True)
    out.items[diagnostics(device)["items"][0]] = diag.snapshot_item(device, uid, "signal", "HDMI signal snapshot")
