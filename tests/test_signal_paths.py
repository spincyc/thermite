"""Signal wiring, route variants and observation scope, with fictional devices."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
from oh_features import remote_diagnostics, remote_topology, routing  # noqa: E402


class Conf:
    def __init__(self, missing=()):
        self.missing = missing
        self.cache = {}

    def item(self, name):
        return name not in self.missing

    def derived(self, key, build):
        if key not in self.cache:
            self.cache[key] = build()
        return self.cache[key]


def house_data():
    devices = [{"id": ident, "room": "desk", "kind": kind, "controls": {}}
               for ident, kind in (("pc", "computer"), ("player", "streaming_player"),
                                   ("matrix", "hdmi_matrix"), ("switch", "hdmi_switch"),
                                   ("monitor", "monitor"), ("tv", "tv"), ("amp", "streaming_amp"),
                                   ("avr", "avr"), ("observer", "hdmi_processor"))]
    devices[-1]["controls"] = {"observe": ["drhdmi_http"]}
    return {"version": 1, "rooms": {"desk": {"label": "Desk"}, "lounge": {"label": "Lounge"}},
            "devices": devices,
            "routing": {"hdmi_matrix": "matrix", "sources": {"pc": {"label": "PC"},
                                                                  "player": {"label": "Player", "hdmi_input": 2}},
                        "rooms": {"desk": {"hdmi_output": 6, "sources": ["pc", "player"],
                                           "hdmi_switch": {"device": "switch", "matrix_input": 1, "inputs": {"pc": 0}},
                                           "display": {"device": "monitor", "passive": True},
                                           "audio": "amp", "audio_input": "optical"}}}}


def paths(data):
    return remote_diagnostics._rooms(House.unchecked(data))["desk"]["sources"]


class SignalPaths(unittest.TestCase):
    def test_optical_fork_keeps_hdmi_output_and_native_input_zero(self):
        data = house_data()
        room = data["routing"]["rooms"]["desk"]
        room["audio_link"] = {"from": {"device": "switch", "port": "optical"}, "transport": "optical"}
        path = paths(data)["pc"]
        self.assertEqual([node["device"] for node in path["nodes"]], ["pc", "switch", "monitor"])
        self.assertEqual([node["device"] for node in path["audioNodes"]], ["pc", "switch", "amp"])
        self.assertEqual((path["nodes"][1]["input"], path["nodes"][1]["output"]), (0, 0))
        self.assertEqual((path["audioNodes"][1]["output"], path["audioNodes"][1]["outputTransport"]), ("optical", "optical"))
        self.assertEqual(path["audioNodes"][-1]["inputKind"], "selector")
        self.assertEqual(path["audioNodes"][-1]["inputTransport"], "optical")
        self.assertEqual(routing._audio_link_problems("routing.rooms.desk", room, House.unchecked(data).by_id, data, "desk"), [])
        topology = remote_topology.manifest(House.unchecked(data), Conf())
        optical = [edge for edge in topology["edges"] if edge["from"] == "switch" and edge["to"] == "amp"]
        self.assertEqual(len(optical), 1)
        self.assertEqual((optical[0]["fromPort"], optical[0]["transport"]), ("optical", "optical"))

    def test_unknown_audio_attachment_is_visible_without_a_fabricated_cable(self):
        data = house_data()
        path = paths(data)["pc"]
        self.assertNotIn("audioNodes", path)
        self.assertEqual(path["audioEndpoints"][0]["wiring"], "unknown")
        self.assertEqual(path["audioEndpoints"][0]["device"], "amp")
        topology = remote_topology.manifest(House.unchecked(data), Conf())
        self.assertFalse(any(edge["to"] == "amp" for edge in topology["edges"]))
        del data["routing"]["rooms"]["desk"]["audio_input"]
        self.assertNotIn("input", paths(data)["pc"]["audioEndpoints"][0])
        data["routing"]["rooms"]["desk"]["carries"] = {"pc": "display"}
        self.assertNotIn("audioEndpoints", paths(data)["pc"])

    def test_sync_variant_uses_leader_cable_without_changing_source_identity(self):
        data = house_data()
        data["routing"]["rooms"] = {
            "desk": {"hdmi_output": 1, "sources": ["player"], "receiver": {"device": "avr", "input": "BD"},
                     "display": {"device": "monitor", "passive": True}},
            "lounge": {"hdmi_output": 2, "sources": ["player"],
                       "receiver": {"device": "avr", "input": "AUX2", "zone": 2, "sync_with": "desk"},
                       "display": {"device": "tv", "input": "hdmi2"}}}
        house = House.unchecked(data)
        room = remote_diagnostics.manifest(house, Conf())["rooms"]["lounge"]
        self.assertEqual(room["routeStateItem"], "loc_lounge_source_lifecycle")
        self.assertEqual(list(room["sources"]), ["player"])
        normal = room["sources"]["player"]
        synced = normal["variants"]["synced"]
        self.assertEqual((normal["nodes"][1]["output"], normal["nodes"][2]["input"]), (2, "AUX2"))
        self.assertEqual((synced["nodes"][1]["output"], synced["nodes"][2]["input"]), (1, "BD"))
        self.assertEqual(synced["nodes"][2]["zone"], 2)
        absent = remote_diagnostics.manifest(house, Conf(["loc_lounge_source_lifecycle"]))["rooms"]["lounge"]
        self.assertNotIn("routeStateItem", absent)
        self.assertIn("variants", absent["sources"]["player"])

    def test_split_and_receiver_local_routes_keep_the_observer_only_on_its_cable(self):
        data = house_data()
        data["routing"]["rooms"]["desk"] = {
            "sources": ["pc", "player"], "hdmi_output": 8, "hdmi_audio_output": 3,
            "receiver": {"device": "avr", "input": "AUX2", "sources": {"pc": "BD"}},
            "display": {"device": "tv", "input": "hdmi3", "input_by_source": {"pc": "hdmi4"}}}
        data["diagnostics"] = {"observers": [{"device": "observer", "after": "avr", "before": "tv"}]}
        self.assertEqual(remote_diagnostics.validate(data), [])
        found = remote_diagnostics.manifest(House.unchecked(data), Conf())["rooms"]["desk"]["sources"]
        self.assertEqual([node["device"] for node in found["pc"]["nodes"]], ["pc", "avr", "observer", "tv"])
        self.assertEqual([node["device"] for node in found["player"]["nodes"]], ["player", "matrix", "tv"])
        self.assertEqual(found["player"]["nodes"][1]["output"], 8)
        self.assertEqual(found["player"]["audioNodes"][1]["output"], 3)

    def test_ambiguous_observer_device_pair_requires_and_honors_port_qualifiers(self):
        data = house_data()
        data["routing"]["rooms"]["desk"] = {
            "sources": ["pc", "player"], "hdmi_output": 1,
            "receiver": {"device": "avr", "input": "AUX2", "sources": {"pc": "BD"}},
            "display": {"device": "tv", "input": "hdmi3", "input_by_source": {"pc": "hdmi4"}}}
        observer = {"device": "observer", "after": "avr", "before": "tv"}
        data["diagnostics"] = {"observers": [observer]}
        self.assertTrue(any("ambiguous" in problem for problem in remote_diagnostics.validate(data)))
        observer["before_port"] = "hdmi4"
        self.assertEqual(remote_diagnostics.validate(data), [])
        found = remote_diagnostics.manifest(House.unchecked(data), Conf())["rooms"]["desk"]["sources"]
        self.assertIn("observer", [node["device"] for node in found["pc"]["nodes"]])
        self.assertNotIn("observer", [node["device"] for node in found["player"]["nodes"]])

    def test_audio_link_rejects_nonexistent_or_bypassed_attachment(self):
        data = house_data()
        room = data["routing"]["rooms"]["desk"]
        room["audio_link"] = {"from": {"device": "matrix", "port": 2}, "transport": "optical"}
        problems = routing._audio_link_problems("routing.rooms.desk", room, House.unchecked(data).by_id, data, "desk")
        self.assertTrue(any("pc's audio route" in problem for problem in problems))
        del room["sources"]  # Graph callers also support the all-sources fallback.
        problems = routing._audio_link_problems("routing.rooms.desk", room, House.unchecked(data).by_id, data, "desk")
        self.assertTrue(any("pc's audio route" in problem for problem in problems))
        for bad in (False, -1, 0.5, "", "\n"):
            room["audio_link"]["from"]["port"] = bad
            self.assertTrue(routing._audio_link_problems("room", room, House.unchecked(data).by_id, data, "desk"))

    def test_audio_link_rejects_invalid_contract_and_preserves_an_existing_audio_leg(self):
        data = house_data()
        room = data["routing"]["rooms"]["desk"]
        for bad in ({}, {"from": "switch", "transport": "optical"},
                    {"from": {"device": "switch", "port": "optical"}, "transport": "wireless"},
                    {"from": {"device": "amp", "port": "optical"}, "transport": "optical"}):
            room["audio_link"] = bad
            self.assertTrue(routing._audio_link_problems("room", room, House.unchecked(data).by_id, data, "desk"))
        room.pop("hdmi_switch")
        room.update(sources=["player"], hdmi_audio_output=3, receiver={"device": "avr", "input": "AUX2"})
        room["audio_link"] = {"from": {"device": "matrix", "port": "optical"}, "transport": "optical"}
        problems = routing._audio_link_problems("room", room, House.unchecked(data).by_id, data, "desk")
        self.assertTrue(any("existing split audio leg" in problem for problem in problems))
        room["audio_link"]["from"]["device"] = "avr"
        self.assertEqual(routing._audio_link_problems("room", room, House.unchecked(data).by_id, data, "desk"), [])
        self.assertEqual([node["device"] for node in paths(data)["player"]["audioNodes"]], ["player", "matrix", "avr", "amp"])

    def test_audio_attachment_is_checked_against_every_reported_variant(self):
        data = house_data()
        room = data["routing"]["rooms"]["desk"]
        room.pop("hdmi_switch")
        room.update(sources=["player"], receiver={"device": "avr", "input": "AUX2", "zone": 2, "sync_with": "lounge"})
        data["routing"]["rooms"]["lounge"] = {
            "hdmi_output": 1, "sources": ["player"], "receiver": {"device": "avr", "input": "BD"}}
        room["audio_link"] = {"from": {"device": "matrix", "port": "optical"}, "transport": "optical"}
        self.assertEqual(routing._audio_link_problems("room", room, House.unchecked(data).by_id, data, "desk"), [])
        # A provider of configured paths must not silently lose the attachment
        # on an alternative path, even when the source identity stays the same.
        link = room.pop("audio_link")
        described = remote_diagnostics._rooms(House.unchecked(data))
        room["audio_link"] = link
        alternate = described["desk"]["sources"]["player"]["variants"]["synced"]
        alternate["nodes"] = [node for node in alternate["nodes"] if node["device"] != "matrix"]
        with mock.patch.object(remote_diagnostics, "_rooms", return_value=described):
            problems = routing._audio_link_problems("room", room, House.unchecked(data).by_id, data, "desk")
        self.assertEqual(len(problems), 1)
        self.assertIn("(synced)", problems[0])

    def test_multiple_integrations_retain_independent_items_and_freshness(self):
        data = house_data()
        device = data["devices"][3]
        device["controls"] = {"observe": ["first", "second"]}

        def module(name, age):
            return SimpleNamespace(NAME=name, PHASE="observe", CONTROL=False, diagnostics=lambda device: {
                "kind": name, "items": [name + "_signal"], "maxAgeSeconds": age,
                "readings": [{"item": name + "_signal", "category": "audio", "path": ["values", "audio"]}]})

        with mock.patch.object(remote_diagnostics.integrations, "defining", return_value=[module("first", 10), module("second", 60)]):
            descriptor = remote_diagnostics.providers(House.unchecked(data))["switch"]
        self.assertEqual(descriptor["items"], ["first_signal", "second_signal"])
        self.assertEqual([(reading["provider"], reading["maxAgeSeconds"]) for reading in descriptor["readings"]],
                         [("first", 10), ("second", 60)])
        self.assertEqual(len(descriptor["providers"]), 2)


if __name__ == "__main__":
    unittest.main()
