"""Poll timing of the HTTP binding Things of the HDMI and USB matrices
(oh_integrations._instr), the HDMI switch and the Dr. HDMI.

A fictional house with documentation addresses. openHAB's HTTP binding
starts each poll on a fixed delay and does not wait for the last one, so a
timeout longer than the refresh lets one poll overlap the next on a
one-at-a-time CGI. A stuck matrix CGI sends its headers and no body, then
closes at about 6 s; the binding counts any 2xx as a success, so a timeout
that long shows a dead matrix as ONLINE.
"""
import copy
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_render  # noqa: E402
from oh_conf import House, YamlFile, linked  # noqa: E402
from oh_integrations import bk808_http, drhdmi_http, ukm404_http, vrroom_http  # noqa: E402
from oh_room_policy import expected_plan  # noqa: E402

MODULES = (bk808_http, ukm404_http, vrroom_http, drhdmi_http)
#: When a stuck matrix CGI closes a request it never answered.
STUCK_CGI_MS = 6000
#: The longest refresh of a Thing whose Items a route is verified by.
ROUTED_REFRESH = 20
CINEMA, LOUNGE = "example_cinema", "example_lounge"
COMPUTER, DISC, GAME = "example_computer", "example_disc", "example_game"


def device(ident, room, kind, number, **extra):
    return {"id": ident, "room": room, "kind": kind, "name": ident.replace("_", " ").title(),
            "ip": f"192.0.2.{number}", **extra}


HOUSE = {
    "version": 1,
    "rooms": {CINEMA: {"label": "Example Cinema", "tag": "Room"}, LOUNGE: {"label": "Example Lounge", "tag": "Room"}},
    "devices": [
        device("example_matrix", CINEMA, "hdmi_matrix", 20, make="BZBGEAR", model="BK-808",
               controls={"control": ["bk808_http"]}),
        device("example_usb", CINEMA, "usb_matrix", 21, model="UKM-404", controls={"control": ["ukm404_http"]}),
        device("example_switch", CINEMA, "hdmi_switch", 22, make="HDFury", model="VRROOM",
               controls={"control": ["vrroom_http"]}),
        device("example_probe", LOUNGE, "hdmi_processor", 23, make="HDFury", model="Dr.HDMI",
               controls={"observe": ["drhdmi_http"]}),
        device(COMPUTER, CINEMA, "computer", 26, model="Computer"),
        device(DISC, CINEMA, "disc_player", 27, make="Sony", model="Example Disc"),
        device(GAME, CINEMA, "game_console", 28, make="Sony", model="Example Game"),
    ],
    "routing": {
        "hdmi_matrix": "example_matrix", "hdmi_off_input": 8, "usb_off_host": 4, "edid": {"idle": "1080p-2ch"},
        "sources": {COMPUTER: {"label": "Computer", "hdmi_input": 1, "usb_host": 1},
                    DISC: {"label": "Disc", "hdmi_input": 2}, GAME: {"label": "Game"}},
        "rooms": {
            CINEMA: {"hdmi_output": 1, "usb": {"example_usb": [1]}, "sources": [COMPUTER, DISC, GAME],
                     "hdmi_switch": {"device": "example_switch", "matrix_input": 1, "inputs": {GAME: 2}}},
            LOUNGE: {"hdmi_output": 2, "usb": {"example_usb": [2]}, "sources": [COMPUTER, DISC]},
        },
    },
}


def rendered(data):
    """{Thing UID: (module, Thing)} and {Item: Thing UID} of these modules."""
    house, things, items = House(data), {}, {}
    for entry in data["devices"]:
        for module in MODULES:
            if module.NAME in sum(entry.get("controls", {}).values(), []):
                out = YamlFile(())
                module.render(entry, house, out)
                things.update({uid: (module, thing) for uid, thing in out.things.items()})
                for name, item in out.items.items():
                    for link in linked(item):
                        items[name] = link.rpartition(":")[0]
    return things, items


def requests(thing):
    """The distinct state requests one poll of the Thing makes (the binding
    shares one request between channels of equal URL and content)."""
    return {(c["config"].get("stateExtension"), c["config"].get("stateContent"))
            for c in thing["channels"].values() if c.get("config", {}).get("mode") != "WRITEONLY"}


class PollTiming(unittest.TestCase):
    def setUp(self):
        self.assertEqual(oh_render.validate(copy.deepcopy(HOUSE)), [])
        self.things, self.items = rendered(copy.deepcopy(HOUSE))
        self.assertEqual({module for module, _ in self.things.values()}, set(MODULES))

    def test_each_poll_ends_before_the_next_and_before_a_stuck_cgi_closes(self):
        for uid, (module, thing) in self.things.items():
            with self.subTest(thing=uid):
                config = thing["config"]
                refresh_ms, timeout, delay = config["refresh"] * 1000, config["timeout"], config.get("delay", 0)
                self.assertLess(timeout, STUCK_CGI_MS)
                self.assertLess(timeout, refresh_ms)
                # The last of a poll's requests leaves (n - 1) delays after the
                # first; it, too, is answered or abandoned before the next poll.
                self.assertLess(timeout + (len(requests(thing)) - 1) * delay, refresh_ms)
                # Else the binding raises the refresh itself, with a warning.
                self.assertLessEqual(len(requests(thing)) * delay, refresh_ms)

    def test_routed_items_are_polled_within_the_policy_window(self):
        fields = {field.item: field for room, entry in HOUSE["routing"]["rooms"].items()
                  for choice in ("off", *entry["sources"])
                  for plan in [expected_plan(copy.deepcopy(HOUSE), room, choice)]
                  for field in (*plan.fields, *plan.conditions)}
        polled = {}
        for field in fields.values():
            uid = self.items.get(field.item)
            if uid is None:
                continue
            module, thing = self.things[uid]
            polled[module] = polled.get(module, set()) | {field.item}
            with self.subTest(item=field.item):
                refresh, timeout = thing["config"]["refresh"], thing["config"]["timeout"] / 1000
                self.assertLessEqual(refresh, ROUTED_REFRESH)
                self.assertLess(refresh + timeout, field.max_age)
        self.assertEqual(set(polled), {bk808_http, ukm404_http, vrroom_http})

    def test_signal_snapshots_stay_fresh_between_polls(self):
        for module in (bk808_http, vrroom_http, drhdmi_http):
            entry = next(d for d in HOUSE["devices"] if module.NAME in sum(d["controls"].values(), []))
            window = module.diagnostics(entry)["maxAgeSeconds"]
            for item in module.diagnostics(entry)["items"]:
                with self.subTest(item=item):
                    config = self.things[self.items[item]][1]["config"]
                    self.assertLess(config["refresh"] + config["timeout"] / 1000, window)


if __name__ == "__main__":
    unittest.main()
