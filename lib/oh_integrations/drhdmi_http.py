"""Dr. HDMI 8K read-only link status and advertised EDID capabilities.

FW 1.5's own web page reads GET /ssi/edidpage.ssi. Its link counters and
flags are retained as reported; they do not decode the active video format.
RX/TX EDID fields describe advertised capabilities, not measured video/audio.
No EDID, HPD, power, configuration or other command is exposed.
"""

from __future__ import annotations

from oh_conf import Addon, House, YamlFile, uses

from . import _diagnostics as diag

NAME = "drhdmi_http"
PHASE = "observe"
FILE = "yaml/drhdmi.yaml"
HEADER = (
    "Dr. HDMI 8K passive link observation and advertised EDID capabilities.",
    "Only GET /ssi/edidpage.ssi is polled; the channel and Item are read-only.",
    "Raw link codes/clocks are not a measurement of resolution, HDR or HDCP.",
)
ADDONS = (Addon("binding", "http"), Addon("automation", "jsscripting", "read-only Dr HDMI signal snapshots"))
ASSETS: dict[str, str] = {}
STATUS = "/ssi/edidpage.ssi"
REFRESH = 2
FIELDS = ["rxhpd", "txhpd", "rx5v", "frlrate", "tmdsclk", "tmdsclkf", "tmdsratio", "scramble",
          "edidmode", "edidcustomtable", "edidoverflow"] + [
              side + field for side in ("rx", "tx") for field in (
                  "edidname", "edidvideoformat", "edidvideocaps", "edidaudiodolby", "edidaudiodts", "edidaudiopcm")]


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def validate(device: dict) -> list[str]:
    if uses(device, PHASE, NAME) and device.get("kind") != "hdmi_processor":
        return [f"{device.get('id')}: {NAME} observes an hdmi_processor"]
    return []


def diagnostics(device: dict) -> dict:
    item = f"{device['id']}_signal"
    fields = [("video", label, field) for label, field in (
        ("RX HPD (0 absent, 1 present)", "rxhpd"), ("TX HPD (0 absent, 1 present)", "txhpd"),
        ("Source 5V (0 absent, 1 present)", "rx5v"), ("Reported FRL rate code", "frlrate"),
        ("Reported TMDS clock (raw)", "tmdsclk"), ("Reported TMDS fine clock (raw)", "tmdsclkf"),
        ("Reported TMDS ratio code", "tmdsratio"), ("Scrambling flag", "scramble"))]
    for side, label in (("rx", "Source-facing"), ("tx", "Sink-facing")):
        for title, field in (("EDID name", "edidname"), ("advertised video formats", "edidvideoformat"),
                             ("advertised video capabilities", "edidvideocaps"),
                             ("advertised Dolby formats", "edidaudiodolby"),
                             ("advertised DTS formats", "edidaudiodts"),
                             ("advertised PCM formats", "edidaudiopcm")):
            fields.append(("edid", f"{label} {title}", side + field))
    fields.extend(("processing", label, field) for label, field in (
        ("Reported EDID mode code", "edidmode"), ("Stored custom EDID bank selection", "edidcustomtable"),
        ("Reported EDID overflow flags", "edidoverflow")))
    return {"kind": NAME, "items": [item], "maxAgeSeconds": 15,
            "readings": [{"category": category, "label": label, "item": item, "path": ["values", field]}
                         for category, label, field in fields]}


def signal_js() -> str:
    return diag.json_snapshot(FIELDS, required="rxhpd")


def files(devices: list[dict], house: House) -> dict[str, str]:
    return {"transform/drhdmi-signal.js": diag.script_file(signal_js())}


def render(device: dict, house: House, out: YamlFile) -> None:
    uid = thing_uid(device)
    out.things[uid] = {
        "label": f"{house.label(device)} status", "location": house.room_label(device["room"]),
        "config": {"baseURL": f"http://{device['ip']}", "refresh": REFRESH, "timeout": 3000,
                   "stateMethod": "GET", "strictErrorHandling": True},
        "channels": {"signal": diag.channel("HDMI link and EDID snapshot", "drhdmi-signal.js", stateExtension=STATUS)},
    }
    out.items[diagnostics(device)["items"][0]] = diag.snapshot_item(device, uid, "signal", "HDMI link and EDID snapshot")
