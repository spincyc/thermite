"""OREI BK-808 8x8 HDMI matrix (an HDCVT HDP-MXC88A) over its JSON API.

Number points for each port, read back at each poll (_instr.REFRESH) and
set by commanding the Item:

- `<id>_output<N>_input`: the input routed to output N;
- `<id>_input<N>_edid`: the EDID code input N presents to its source (EDID
  below), which the routing (oh_features.routing) sets as rooms turn sources
  on and off;
- `<id>_output<N>_scaler`: what output N's scaler does (SCALER below), which
  the routing sets only on the audio output of a room that names a scaler
  for some of its sources (routing.rooms.<room>.hdmi_audio_scaler).

A read-only Switch `<id>_output<N>_hotplug` (hotplug_item()) is ON while
output N reports a display connected and OFF while it reports none
(`allconnect` of `get output status`, the request its scaler channel already
polls), at each poll (oh_integrations._hotplug).

Read-only String snapshots `<id>_input_signal` and `<id>_output_signal`
describe presented EDIDs, sink connections and output processing settings.
They never infer a negotiated resolution or HDR format from an EDID preset.
The input snapshot reuses its existing poll; output settings add the known
`get output status` read to each polling cycle.

bin/thermite matrix-settings applies the matrix's fixed settings from
devices.yaml. For an audio output with a source-specific scaler, routing
sets the scaler and restores the configured default on every other choice.
"""

from __future__ import annotations

from oh_conf import House, YamlFile, options_text, uses
from oh_shape import MAPPING, TEXT, Key

from . import _instr as instr
from . import _diagnostics as diag
from . import _hotplug

NAME = "bk808_http"
PHASE = "control"
FILE = "yaml/bk808.yaml"
HEADER = (
    "HDMI matrix routing over the BK-808's JSON API (HTTP binding). Each",
    "output's input is polled with `get video status` and switched with",
    "`video switch`; each input's EDID is polled with `get input status` and",
    "set with `set edid`; each output's scaler is polled with `get output",
    "status` and set with `set video scaler`. Items are autoupdate off: they",
    "show only what the matrix reports.",
)
ADDONS = instr.ADDONS
ASSETS: dict[str, str] = {}
INPUTS = OUTPUTS = 8
READBACK_INPUTS = tuple(str(number) for number in range(1, INPUTS + 1))
POLL_S = instr.REFRESH
KINDS = {"hdmi_matrix"}
#: All that the Thing ever sends: it reads and sets routing, input EDIDs and
#: output scalers.
COMHEADS = frozenset({"get video status", "video switch", "get input status", "get output status", "set edid",
                      "set video scaler"})
thing_uid = instr.thing_uid
url = instr.url

_FORMATS = ("1080p", "4k30", "4k60-420", "4k60-444", "1080p-hdr", "4k30-hdr", "4k60-420-hdr",
            "4k60-444-hdr", "4k120-420-hdr", "4k120-444-hdr", "8k-frl10-hdr", "8k-frl12-hdr")
#: The matrix's EDID codes by name, in the order of its web page's list:
#: format and audio (2ch, 5.1ch, 7.1ch) for 1-36, then user EDIDs and copies
#: of an output's display EDID.
EDID = {f"{fmt}-{audio}": 1 + 3 * index + offset
        for index, fmt in enumerate(_FORMATS) for offset, audio in enumerate(("2ch", "5.1ch", "7.1ch"))}
EDID.update({f"user-{n}": 36 + n for n in range(1, 4)})
EDID.update({f"copy-output-{n}": 39 + n for n in range(1, OUTPUTS + 1)})
SCALER = {"bypass": 0, "8k-to-4k": 1, "to-1080p": 2, "auto": 3, "audio-only": 4}
HDR = {"bypass": 0, "hdr-to-sdr": 1, "auto": 2}
HDCP = {"1.4": 1, "2.2": 2, "follow-sink": 3, "follow-source": 4}
OUTPUT_SETTINGS = {
    # setting: (codes, status field, write comhead, write field)
    "scaler": (SCALER, "allscaler", "set video scaler", "scaler"),
    "hdr": (HDR, "allhdr", "set hdr conversion", "hdr"),
    "hdcp": (HDCP, "allhdcp", "tx hdcp", "hdcp"),
}
DEVICE_KEYS = {"settings": Key(MAPPING, "the matrix's fixed settings, which thermite matrix-settings applies once", keys={
    "edid": Key(MAPPING, "the EDID each input presents", by="input number, or default", each=Key(TEXT, "an EDID name")),
    "scaler": Key(TEXT, "the default scaler for every output, including the value restored after a "
                        "source-specific hdmi_audio_scaler", choices=tuple(SCALER)),
    "hdr": Key(TEXT, "the outputs' HDR conversion", choices=tuple(HDR)),
    "hdcp": Key(TEXT, "the HDCP version the outputs ask for, in quotes", choices=tuple(HDCP))})}


def ports(device: dict) -> tuple[int, int]:
    """(inputs, outputs)."""
    return INPUTS, OUTPUTS


def switch_item(device: dict, output: int) -> str:
    """The Item holding the input routed to `output`."""
    return f"{device['id']}_output{output}_input"


def edid_item(device: dict, input_: int) -> str:
    """The Item holding the EDID code `input_` presents."""
    return f"{device['id']}_input{input_}_edid"


def copy_edid(output: int) -> str:
    """The EDID name that copies the display on `output`."""
    return f"copy-output-{output}"


def scaler_item(device: dict, output: int) -> str:
    """The Item holding the scaler code of `output`."""
    return f"{device['id']}_output{output}_scaler"


def hotplug_item(device: dict, output: int) -> str:
    """The Item reporting whether a display is on `output` (_hotplug)."""
    return _hotplug.item(device, output)


def fields(device: dict) -> dict[str, dict]:
    """The routing executor's fields (oh_integrations): each output's input
    and scaler code and each input's EDID code, written and read through the
    Thing's own channel; the matrix acknowledges a write with {"result": 1}. Each is
    independent: a request names its one port, so a lost answer leaves the
    other ports as they were."""
    uid = thing_uid(device)
    out = {switch_item(device, output): {"kind": "http", "thing": uid, "channel": f"output{output}_input",
                                         "values": [str(n) for n in range(1, INPUTS + 1)], "ack": "result",
                                         "independent": True}
           for output in range(1, OUTPUTS + 1)}
    codes = [str(code) for code in sorted(EDID.values())]
    out.update({edid_item(device, input_): {"kind": "http", "thing": uid, "channel": f"input{input_}_edid",
                                            "values": codes, "ack": "result", "independent": True}
                for input_ in range(1, INPUTS + 1)})
    scalers = [str(code) for code in sorted(SCALER.values())]
    out.update({scaler_item(device, output): {"kind": "http", "thing": uid, "channel": f"output{output}_scaler",
                                              "values": scalers, "ack": "result", "independent": True}
                for output in range(1, OUTPUTS + 1)})
    return out


def diagnostics(device: dict) -> dict:
    """Read-back EDIDs and output settings; these are not negotiated video."""
    input_item, output_item = f"{device['id']}_input_signal", f"{device['id']}_output_signal"
    readings = [{"category": "edid", "kind": "capability", "label": f"Input {port} presented EDID", "item": input_item,
                 "path": ["values", "edid", str(port)], "port": {"direction": "input", "number": port}}
                for port in range(1, INPUTS + 1)]
    for port in range(1, OUTPUTS + 1):
        for key, label in (("connected", "sink connection"), ("scaler", "scaler setting"),
                           ("hdr", "HDR processing setting"), ("hdcp", "HDCP setting")):
            readings.append({"category": "processing", "label": f"Output {port} {label}", "item": output_item,
                             "kind": "link" if key == "connected" else "setting",
                             "path": ["values", key, str(port)], "port": {"direction": "output", "number": port}})
    return {"kind": NAME, "items": [input_item, output_item], "maxAgeSeconds": 15, "readings": readings}


def signal_js(direction: str) -> str:
    if direction == "input":
        return diag.json_snapshot({"edid": {"source": "edid", "options": {code: name for name, code in EDID.items()}}},
                                  required="edid")
    if direction != "output":
        raise ValueError("signal direction must be input or output")
    fields = {key: {"source": source, "options": {code: name for name, code in codes.items()}}
              for key, (codes, source, *_) in OUTPUT_SETTINGS.items()}
    fields["connected"] = {"source": "allconnect", "options": {0: "disconnected", 1: "connected"}}
    return diag.json_snapshot(fields, required="allconnect")


def files(devices: list[dict], house: House) -> dict[str, str]:
    return {f"transform/bk808-{direction}-signal.js": diag.script_file(signal_js(direction))
            for direction in ("input", "output")}


def _edid_port(key: object) -> int | None:
    if isinstance(key, int) and not isinstance(key, bool) and 1 <= key <= INPUTS:
        return key
    if isinstance(key, str) and key in {str(n) for n in range(1, INPUTS + 1)}:
        return int(key)
    return None


def validate(device: dict) -> list[str]:
    if not uses(device, PHASE, NAME):
        return []
    where = device.get("id") or "a device"
    problems = []
    if device.get("kind") not in KINDS:
        problems.append(f"{where}: {NAME} drives an hdmi_matrix, not a {device.get('kind')}")
    settings = device.get("settings")
    if settings is None:
        return problems
    if not isinstance(settings, dict):
        return problems + [f"{where}.settings: must be a mapping"]
    edid = settings.get("edid") or {}
    if not isinstance(edid, dict):
        problems.append(f"{where}.settings.edid: must map inputs (or default) to EDID names")
        edid = {}
    seen_ports: dict[int, object] = {}
    for key, name in edid.items():
        port = None if key == "default" else _edid_port(key)
        if key != "default" and port is None:
            problems.append(f"{where}.settings.edid: {key!r} is neither default nor an input 1-{INPUTS}")
        elif port is not None:
            if port in seen_ports:
                problems.append(f"{where}.settings.edid: input {port} is set more than once")
            else:
                seen_ports[port] = key
        if name not in EDID:
            problems.append(f"{where}.settings.edid.{key}: {name!r} is not an EDID name "
                            f"(such as 4k60-444-hdr-2ch or copy-output-1)")
    for setting, (codes, *_) in OUTPUT_SETTINGS.items():
        if setting in settings and not (isinstance(settings[setting], str) and settings[setting] in codes):
            problems.append(f"{where}.settings.{setting}: {settings[setting]!r} is not one of "
                            f"{', '.join(codes)}")
    for key in sorted(set(settings) - {"edid", *OUTPUT_SETTINGS}):
        problems.append(f"{where}.settings: unknown key '{key}'")
    return problems


def _point(device: dict, label: str, channel: str, icon: str) -> dict:
    return {
        "type": "Number",
        "label": label,
        "icon": icon,
        "groups": [device["id"]],
        "tags": ["Control"],
        "autoupdate": False,
        "channel": f"{thing_uid(device)}:{channel}",
    }


def render(device: dict, house: House, out: YamlFile) -> None:
    thing = instr.thing(device, house)
    for output in range(1, OUTPUTS + 1):
        channel = f"output{output}_input"
        command = f"JSON.stringify({{comhead:'video switch',language:0,source:[{output},parseInt(input)]}})"
        thing["channels"][channel] = instr.number_channel(f"Output {output} input", "get video status",
                                                          "allsource", output, command)
        out.items[switch_item(device, output)] = _point(device, f"Output {output} input", channel, "video")
    scalers = options_text((code, name) for name, code in SCALER.items())
    for output in range(1, OUTPUTS + 1):
        channel = f"output{output}_scaler"
        command = f"JSON.stringify({{comhead:'set video scaler',language:0,scaler:[{output},parseInt(input)]}})"
        thing["channels"][channel] = instr.number_channel(f"Output {output} scaler", "get output status",
                                                          "allscaler", output, command)
        item = _point(device, f"Output {output} scaler", channel, "screen")
        item["metadata"] = {"stateDescription": {"config": {"options": scalers}}}
        out.items[scaler_item(device, output)] = item
    for output in range(1, OUTPUTS + 1):
        channel = f"output{output}_hotplug"
        # The same request as the scaler's: the binding polls it once for both.
        thing["channels"][channel] = {"type": "switch", "label": f"Output {output} hot-plug", "config": {
            "mode": "READONLY", "stateContent": instr.body("get output status", language=0),
            "stateTransformation": [f"JSONPATH($.allconnect[{output - 1}])"], "onValue": "1", "offValue": "0"}}
        out.items[hotplug_item(device, output)] = _hotplug.point(device, output, f"{thing_uid(device)}:{channel}")
    names = options_text((code, name) for name, code in EDID.items())
    for input_ in range(1, INPUTS + 1):
        channel = f"input{input_}_edid"
        command = f"JSON.stringify({{comhead:'set edid',language:0,edid:[{input_},parseInt(input)]}})"
        thing["channels"][channel] = instr.number_channel(f"Input {input_} EDID", "get input status",
                                                          "edid", input_, command)
        item = _point(device, f"Input {input_} EDID", channel, "screen")
        item["metadata"] = {"stateDescription": {"config": {"options": names}}}
        out.items[edid_item(device, input_)] = item
    out.things[thing_uid(device)] = thing
    for direction in ("input", "output"):
        channel = f"{direction}_signal"
        thing["channels"][channel] = diag.channel(
            f"HDMI {direction} snapshot", f"bk808-{direction}-signal.js",
            stateContent=instr.body(f"get {direction} status", language=0), refreshAfterCommand=direction == "input")
        out.items[f"{device['id']}_{channel}"] = diag.snapshot_item(
            device, thing_uid(device), channel, f"HDMI {direction} snapshot")


# ---------------------------------------------------------------- fixed settings

STATUS = ("get input status", "get output status")


def desired(device: dict) -> dict:
    """The settings devices.yaml asks for, as device codes."""
    settings = device.get("settings") or {}
    edid = settings.get("edid") or {}
    out: dict = {"edid": {}}
    for port in range(1, INPUTS + 1):
        name = edid.get(port, edid.get(str(port), edid.get("default")))
        if name is not None:
            out["edid"][port] = EDID[name]
    for setting, (codes, *_) in OUTPUT_SETTINGS.items():
        if setting in settings:
            out[setting] = codes[settings[setting]]
    return out


def _name(codes: dict, code: object) -> str:
    return next((name for name, value in codes.items() if value == code), f"code {code}")


def plan(device: dict, status: dict[str, dict], routed_scalers: frozenset[int] = frozenset()) -> list[tuple[str, str]]:
    """(what changes, request body) for each differing fixed setting.
    Routing owns the scaler on each output in `routed_scalers`."""
    want = desired(device)
    changes = []
    edids = status["get input status"]["edid"]
    for port, code in want["edid"].items():
        if edids[port - 1] != code:
            changes.append((f"input {port} EDID {_name(EDID, edids[port - 1])} -> {_name(EDID, code)}",
                            instr.body("set edid", language=0, edid=[port, code])))
    outputs = status["get output status"]
    for setting, (codes, field, comhead, key) in OUTPUT_SETTINGS.items():
        if setting not in want:
            continue
        for port in range(1, OUTPUTS + 1):
            if setting == "scaler" and port in routed_scalers:
                continue
            current = outputs[field][port - 1]
            if current != want[setting]:
                changes.append((f"output {port} {setting} {_name(codes, current)} -> "
                                f"{_name(codes, want[setting])}",
                                instr.body(comhead, language=0, **{key: [port, want[setting]]})))
    return changes
