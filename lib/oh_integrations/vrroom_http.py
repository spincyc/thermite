"""HDFury VRROOM diagnostics over its web API.

One read-only String point, `<id>_signal`: what the VRROOM's telnet API
does not report, read from GET /ssi/infopage.ssi, its state as JSON, about
once a minute: measured input and output video and audio, separately
labelled sink EDID capabilities, each input's +5 V, the outputs' hot-plug
and the eARC and ARC states. No login is needed. The switch is controlled
over telnet (vrroom_telnet, under control); nothing here can command it.
"""

from __future__ import annotations

from oh_conf import Addon, House, YamlFile, uses

from . import _diagnostics as diag

NAME = "vrroom_http"
PHASE = "observe"
FILE = "yaml/vrroom.yaml"
HEADER = (
    "HDFury VRROOM diagnostics over its web API (HTTP binding): a read-only",
    "snapshot of GET /ssi/infopage.ssi, polled about once a minute. The switch",
    "is controlled over telnet (vrroom_telnet).",
)
ADDONS = (
    Addon("binding", "http"),
    Addon("automation", "jsscripting", "the VRROOM signal snapshot transform"),
)
ASSETS: dict[str, str] = {}
KINDS = {"hdmi_switch"}
STATUS = "/ssi/infopage.ssi"
#: Seconds between polls and the request timeout in ms. Nothing routed is
#: read here, so about once a minute; the snapshot's window (maxAgeSeconds)
#: covers two polls and a timeout.
REFRESH = 60
TIMEOUT = 2000
MAX_AGE = 2 * REFRESH + 5
#: The infopage fields a snapshot keeps.
FIELDS = ["portseltx0", "portseltx1", "RX0", "RX1", "TX0", "TX1", "AUD0", "AUD1", "AUDOUT", "EARCRX",
          "SINK0", "SINK1", "EDIDA0", "EDIDA1", "rx0in5v", "rx1in5v", "rx2in5v", "rx3in5v",
          "tx0hpd", "tx1hpd", "txaudhpd", "earcout5v", "earcst", "arcst"]


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def signal_item(device: dict) -> str:
    return f"{device['id']}_signal"


def diagnostics(device: dict) -> dict:
    """Reported signals and sink capabilities, +5 V, hot-plug and eARC.

RX0/RX1 are the original signals for the TX0/TX1 processing paths, not
physical input sockets. See HDfury VRROOM manual, INFO section, page 19.
"""
    item = signal_item(device)
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
        readings.append({"category": "video", "label": f"TX{output} hot-plug (as reported)", "item": item,
                         "path": ["values", f"tx{output}hpd"], "port": {"direction": "output", "number": output}})
    for number in range(4):
        readings.append({"category": "video", "label": f"Input {number} +5 V (as reported)", "item": item,
                         "path": ["values", f"rx{number}in5v"], "port": {"direction": "input", "number": number}})
    for label, key in (("Audio extractor output", "AUDOUT"), ("eARC return audio", "EARCRX"),
                       ("Audio output hot-plug (as reported)", "txaudhpd"),
                       ("eARC output +5 V (as reported)", "earcout5v"),
                       ("eARC state (as reported)", "earcst"), ("ARC state (as reported)", "arcst")):
        readings.append({"category": "audio", "label": label, "item": item, "path": ["values", key]})
    return {"kind": NAME, "items": [item], "maxAgeSeconds": MAX_AGE, "readings": readings}


def signal_js() -> str:
    return diag.json_snapshot(FIELDS, required="portseltx0")


def files(devices: list[dict], house: House) -> dict[str, str]:
    return {"transform/vrroom-signal.js": diag.script_file(signal_js())}


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("kind") not in KINDS:
        return [f"{device.get('id')}: {NAME} observes an hdmi_switch, not a {device.get('kind')}"]
    return []


def render(device: dict, house: House, out: YamlFile) -> None:
    uid = thing_uid(device)
    out.things[uid] = {
        "label": f"{house.label(device)} web API",
        "location": house.room_label(device["room"]),
        "config": {
            "baseURL": f"http://{device['ip']}",
            "refresh": REFRESH,
            "timeout": TIMEOUT,
            "delay": 100,
            "stateMethod": "GET",
            "commandMethod": "GET",
            # Offline and UNDEF when the switch stops answering, never stale.
            "strictErrorHandling": True,
        },
        "channels": {"signal": diag.channel("HDMI signal snapshot", "vrroom-signal.js", stateExtension=STATUS)},
    }
    out.items[diagnostics(device)["items"][0]] = diag.snapshot_item(device, uid, "signal", "HDMI signal snapshot")
