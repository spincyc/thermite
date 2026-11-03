"""A fictional house that talks only to the test guest's loopback simulator."""
import json
from pathlib import Path
import sys

AREA = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AREA / "lib"))
import oh_public


def house():
    demo = oh_public.demo()
    devices = {device["id"]: device for device in demo["devices"]}
    display, matrix, computer = (devices[name] for name in ("example_tv", "example_matrix", "example_computer"))
    room = display["room"]
    display.update(ip="127.0.0.1", controls={"observe": ["sony_rest"]})
    matrix["ip"] = "127.0.0.2"
    # A source without an integration needs no listener or fabricated power
    # acknowledgement. Only the real matrix protocol changes its route.
    computer.update(ip="127.0.0.3", room=room)
    return {"version": 1, "access": {"mode": "authenticated"},
            "rooms": {room: demo["rooms"][room]}, "devices": [display, matrix, computer],
            "routing": {"hdmi_matrix": matrix["id"], "hdmi_off_input": 8, "usb_off_host": 4,
                        "sources": {computer["id"]: {"label": "Computer", "hdmi_input": 1}},
                        "rooms": {room: {"hdmi_output": 1, "sources": [computer["id"]],
                                         "display": {"device": display["id"], "passive": True}}}}}


if __name__ == "__main__":
    print(json.dumps(house(), indent=2))
