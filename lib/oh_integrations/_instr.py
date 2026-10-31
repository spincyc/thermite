"""The JSON API at /cgi-bin/instr shared by the HDMI (bk808_http) and USB
(ukm404_http) matrices, and the HTTP binding Thing that polls it.

Both answer `POST /cgi-bin/instr` with a JSON body whose `comhead` names the
command, and neither checks a login. A comhead the firmware does not know
gets no answer at all and stalls its single-threaded CGI for the next
request too, so only the comheads in COMHEADS, each seen working on these
units, are ever sent.
"""

from __future__ import annotations

import json
import urllib.request

from oh_conf import Addon, House

PATH = "/cgi-bin/instr"
ADDONS = (
    Addon("binding", "http"),
    Addon("transformation", "jsonpath"),
    Addon("automation", "jsscripting", "the inline JS that builds matrix commands"),
)
#: Seconds between polls: how soon a change made elsewhere (Control4, the
#: front panel, the web page) shows up. One request per poll per Thing.
REFRESH = 2
COMHEADS = frozenset({
    "get video status", "video switch", "get input status", "get output status",
    "set edid", "set video scaler", "set hdr conversion", "tx hdcp",
    "get_matrix_status", "host_switch",
})


def body(comhead: str, **fields: object) -> str:
    """A request body, compact as the vendor's web pages send it."""
    if comhead not in COMHEADS:
        raise ValueError(f"comhead {comhead!r} is not known to work on these matrices")
    return json.dumps({"comhead": comhead, **fields}, separators=(",", ":"))


def thing_uid(device: dict) -> str:
    return f"http:url:{device['id']}"


def url(address: str) -> str:
    """The API URL of a matrix at HOST[:PORT]."""
    return f"http://{address}{PATH}"


def thing(device: dict, house: House) -> dict:
    """The HTTP Thing for one matrix; its channels poll and command it."""
    return {
        "label": f"{house.label(device)} JSON API",
        "location": house.room_label(device["room"]),
        "config": {
            "baseURL": url(device["ip"]),
            "refresh": REFRESH,
            "timeout": 3000,
            # Space requests 100 ms apart; the CGI serves one at a time.
            "delay": 100,
            "stateMethod": "POST",
            "commandMethod": "POST",
            "contentType": "application/json",
            # Offline and UNDEF when the matrix stops answering, never stale.
            "strictErrorHandling": True,
        },
        "channels": {},
    }


def number_channel(label: str, status: str, field: str, port: int, command: str) -> dict:
    """A number channel: entry `port` of `field` in the `status` reply, set by
    sending `command` (inline JS building the body from the commanded
    number)."""
    return {
        "type": "number",
        "label": label,
        "config": {
            "stateContent": body(status, language=0),
            "stateTransformation": [f"JSONPATH($.{field}[{port - 1}])"],
            "commandTransformation": [f"JS(|{command})"],
            "refreshAfterCommand": True,
        },
    }


def request(address: str, text: str, timeout: float = 5) -> dict:
    """POST one body to a matrix and return its JSON answer."""
    comhead = json.loads(text).get("comhead")
    if comhead not in COMHEADS:
        raise ValueError(f"comhead {comhead!r} is not known to work on these matrices")
    req = urllib.request.Request(f"http://{address}{PATH}", data=text.encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))
