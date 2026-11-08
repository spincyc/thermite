"""Native HEOS: one bridge for the whole house, hosted on one receiver,
with every HEOS receiver's player on it. No device is contacted.

The HEOS binding connects to one player of the HEOS network as its bridge
and finds every other player through it; its README warns that a second
bridge can cause trouble with the connection. The house is the public
example plus fictional receivers at documentation addresses.
"""
import copy
import json
from pathlib import Path
import re
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_render  # noqa: E402
from oh_conf import House, YamlFile, linked  # noqa: E402
from oh_integrations import heos  # noqa: E402
from oh_metrics import exporter  # noqa: E402

WEST, EAST, THIRD = "example_west_avr", "example_east_avr", "example_third_avr"
ADDRESS = {WEST: "192.0.2.21", EAST: "192.0.2.22", THIRD: "192.0.2.23"}
PID = {WEST: 101, EAST: "-202", THIRD: 303}


def receiver(ident, room, *, use_heos=True, **extra):
    control = ["denonmarantz_http", "heos"] if use_heos else ["denonmarantz_http"]
    device = {"id": ident, "room": room, "kind": "avr", "make": "Marantz", "model": "Example AVR",
              "ip": ADDRESS[ident], "controls": {"observe": ["denonmarantz_http"], "control": control}, **extra}
    if use_heos:
        device["heos_pid"] = PID[ident]
    return device


def house(*receivers):
    """The example house with these receivers added after its devices."""
    data = oh_inventory.load(AREA / "instance/devices.yaml")
    data["devices"] += [copy.deepcopy(device) for device in receivers]
    return data


def two(**marks):
    """Two HEOS receivers, west first; marks: {id: extra device fields}."""
    return house(receiver(WEST, "example_west", **marks.get(WEST, {})),
                 receiver(EAST, "example_east", **marks.get(EAST, {})))


def things(data):
    problems = oh_render.validate(data)
    if problems:
        raise AssertionError(problems)
    return oh_render.build(data).yaml[heos.FILE].things


def bridges(found):
    return {uid: thing for uid, thing in found.items() if thing.get("isBridge")}


class OneBridgePerHouse(unittest.TestCase):
    def test_two_receivers_share_one_bridge_on_the_first(self):
        found = things(two())
        self.assertEqual(list(bridges(found)), [f"heos:bridge:{WEST}"])
        bridge = found[f"heos:bridge:{WEST}"]
        self.assertEqual(bridge["config"], {"ipAddress": ADDRESS[WEST], "heartbeat": 60})
        players = {uid: thing for uid, thing in found.items() if not thing.get("isBridge")}
        self.assertEqual({uid: (thing["bridge"], thing["config"]) for uid, thing in players.items()}, {
            f"heos:player:{WEST}:{WEST}": (f"heos:bridge:{WEST}", {"pid": "101"}),
            f"heos:player:{WEST}:{EAST}": (f"heos:bridge:{WEST}", {"pid": "-202"}),
        })
        self.assertNotIn(ADDRESS[EAST], json.dumps(found), "no HEOS connection to the other receiver")

    def test_each_receiver_keeps_its_own_playback_item_and_player(self):
        conf = oh_render.build(two())
        items = conf.yaml[heos.FILE].items
        for ident in (WEST, EAST):
            with self.subTest(receiver=ident):
                item = items[heos.playback_item({"id": ident})]
                self.assertEqual(linked(item), [f"heos:player:{WEST}:{ident}:Control"])
                self.assertFalse(item["autoupdate"])

    def test_heos_bridge_moves_the_bridge_to_the_marked_receiver(self):
        found = things(two(**{EAST: {"heos_bridge": True}}))
        self.assertEqual(list(bridges(found)), [f"heos:bridge:{EAST}"])
        self.assertEqual(found[f"heos:bridge:{EAST}"]["config"]["ipAddress"], ADDRESS[EAST])
        self.assertEqual(sorted(uid for uid in found if uid.startswith("heos:player:")),
                         [f"heos:player:{EAST}:{EAST}", f"heos:player:{EAST}:{WEST}"])
        self.assertTrue(all(thing["bridge"] == f"heos:bridge:{EAST}" for uid, thing in found.items()
                            if not thing.get("isBridge")))

    def test_the_host_is_the_first_heos_receiver_not_the_first_receiver(self):
        data = house(receiver(THIRD, "example_west", use_heos=False), receiver(EAST, "example_east"),
                     receiver(WEST, "example_west"))
        found = things(data)
        self.assertEqual(list(bridges(found)), [f"heos:bridge:{EAST}"])
        self.assertEqual(heos.host(House(data).devices)["id"], EAST)

    def test_a_single_receiver_hosts_its_own_bridge(self):
        found = things(house(receiver(WEST, "example_west")))
        self.assertEqual(list(found), [f"heos:bridge:{WEST}", f"heos:player:{WEST}:{WEST}"])

    def test_a_player_rendered_alone_brings_the_bridge_it_is_on(self):
        data = two()
        out = YamlFile(heos.HEADER)
        heos.render(data["devices"][-1], House(data), out)
        self.assertEqual(list(out.things), [f"heos:bridge:{WEST}", f"heos:player:{WEST}:{EAST}"])
        self.assertEqual(out.things[f"heos:bridge:{WEST}"]["config"]["ipAddress"], ADDRESS[WEST])

    def test_the_rule_drives_each_receiver_through_its_player_on_the_shared_bridge(self):
        rule = oh_render.render(two())[heos.RULE]
        table = json.loads(re.search(r"^const DEVICES = (.*);$", rule, re.M).group(1))
        self.assertEqual([(row["id"], row["thing"], row["receiver"]) for row in table], [
            (WEST, f"heos:player:{WEST}:{WEST}", f"denonmarantz:avr:{WEST}"),
            (EAST, f"heos:player:{WEST}:{EAST}", f"denonmarantz:avr:{EAST}"),
        ])
        self.assertEqual([row["readURL"] for row in table],
                         [f"http://{ADDRESS[ident]}:8080/goform/AppCommand.xml" for ident in (WEST, EAST)])

    def test_every_thing_belongs_to_the_device_whose_address_it_uses(self):
        data = two()
        conf = oh_render.build(data)
        found = {uid: {"file": path, "body": body}
                 for path, file in conf.yaml.items() for uid, body in file.things.items()}
        items = {name: {"file": path, "body": body}
                 for path, file in conf.yaml.items() for name, body in file.items.items()}
        self.assertEqual(oh_inventory.cross_check(data, found, items), [])

    def test_the_sandbox_copy_addresses_the_bridge_nowhere(self):
        files = oh_render.unaddressed(oh_render.read_only(oh_render.render(two())))
        for address in ADDRESS.values():
            self.assertNotIn(address, files[heos.FILE])


class HostSelection(unittest.TestCase):
    def problems(self, data):
        return oh_render.validate(data)

    def test_only_one_receiver_may_host_the_bridge(self):
        found = self.problems(two(**{WEST: {"heos_bridge": True}, EAST: {"heos_bridge": True}}))
        self.assertEqual(found, [f"heos_bridge is true on {WEST} and {EAST}; "
                                 "only one HEOS receiver can host the house's bridge"])
        data = house(*(receiver(ident, "example_west", heos_bridge=True) for ident in (WEST, EAST, THIRD)))
        self.assertEqual(self.problems(data), [f"heos_bridge is true on {WEST}, {EAST} and {THIRD}; "
                                               "only one HEOS receiver can host the house's bridge"])

    def test_heos_bridge_needs_a_device_that_uses_heos(self):
        data = house(receiver(WEST, "example_west"), receiver(EAST, "example_east", use_heos=False, heos_bridge=True))
        next(device for device in data["devices"] if device["id"] == "example_tv")["heos_bridge"] = True
        self.assertEqual(self.problems(data), [
            "example_tv.heos_bridge: only a device that uses heos can host the house's HEOS bridge",
            f"{EAST}.heos_bridge: only a device that uses heos can host the house's HEOS bridge"])

    def test_heos_bridge_is_true_or_left_out(self):
        for value in (False, "yes", 1, None):
            with self.subTest(value=value):
                self.assertEqual(self.problems(two(**{EAST: {"heos_bridge": value}})),
                                 [f"{EAST}.heos_bridge: must be true; leave it out on the other HEOS receivers"])


class Metrics(unittest.TestCase):
    def setUp(self):
        self.data = two(**{EAST: {"heos_bridge": True}})
        self.map = oh_metrics.exporter_map(self.data)

    def test_the_bridge_lands_on_its_host_and_each_player_on_its_receiver(self):
        heos_things = {ident: [uid for uid in self.map["devices"][ident]["things"] if uid.startswith("heos:")]
                       for ident in (WEST, EAST)}
        self.assertEqual(heos_things, {EAST: [f"heos:bridge:{EAST}", f"heos:player:{EAST}:{EAST}"],
                                       WEST: [f"heos:player:{EAST}:{WEST}"]})
        every = [uid for device in self.map["devices"].values() for uid in device["things"]]
        self.assertEqual(len(every), len(set(every)), "no Thing is counted for two devices")

    def test_a_receiver_sees_its_player_offline_while_the_bridge_on_another_is(self):
        status = {f"heos:bridge:{EAST}": ("OFFLINE", "COMMUNICATION_ERROR"),
                  f"heos:player:{EAST}:{EAST}": ("OFFLINE", "BRIDGE_OFFLINE"),
                  f"heos:player:{EAST}:{WEST}": ("OFFLINE", "BRIDGE_OFFLINE"),
                  f"denonmarantz:avr:{EAST}": ("OFFLINE", "COMMUNICATION_ERROR"),
                  f"denonmarantz:avr:{WEST}": ("ONLINE", "NONE")}
        answer = [{"UID": uid, "statusInfo": {"status": value, "statusDetail": detail}}
                  for uid, (value, detail) in status.items()]
        samples = exporter.collect(self.map, lambda path, _: answer if path == exporter.THINGS_PATH else [])
        online = {(labels["device"], labels["thing"]): value for name, labels, value in samples
                  if name == "thermite_thing_online" and labels["thing"].startswith("heos:")}
        self.assertEqual(online, {(EAST, f"heos:bridge:{EAST}"): 0.0, (EAST, f"heos:player:{EAST}:{EAST}"): 0.0,
                                  (WEST, f"heos:player:{EAST}:{WEST}"): 0.0})

    def test_heos_never_gates_a_route_health_check(self):
        for check in self.map["health_checks"]:
            self.assertFalse(str(check["labels"].get("thing", "")).startswith("heos:"), check)


if __name__ == "__main__":
    unittest.main()
