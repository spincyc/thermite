"""Hot-plug detect on an HDMI matrix's outputs (oh_integrations._hotplug):
the Items its integrations give each output, the routing table's checks of
the output each room names, and the problem the gateway and the remote let
through when a room's output finds no display.

A fictional house with documentation addresses: the Example Desk is on
output 1 of a BK-808 (bk808_http), through an HDMI switch that says nothing
of its outputs (vrroom_telnet); the Example Den is on output 4 of its own
AVPro (avpro_telnet). The production routing.js reads the Items in
tests/routing_script_harness.js, the AVPro's rule posts them in
tests/signal_collectors_harness.js and tests/integration_scripts_harness.js,
and the remote's backend words the problem in tests/backend_stream_harness.js.
"""
import copy
import re
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_integrations as integrations  # noqa: E402
import oh_render as render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import remote_activity, routing  # noqa: E402
from oh_integrations import _hotplug, _instr as instr, avpro_telnet, bk808_http  # noqa: E402
from tests.test_vrroom_telnet import HOUSE as DESK_HOUSE  # noqa: E402

DESK, DEN = "example_desk", "example_den"


def house():
    data = copy.deepcopy(DESK_HOUSE)
    data["rooms"][DEN] = {"label": "Example Den", "tag": "Room"}
    data["devices"] += [
        {"id": "example_avpro", "room": DEN, "kind": "hdmi_matrix", "make": "AVPro Edge", "model": "AC-MX-44X",
         "ip": "192.0.2.70", "controls": {"control": ["avpro_telnet"]}},
        {"id": "example_den_player", "room": DEN, "kind": "streamer", "make": "Apple", "model": "Example Player",
         "ip": "192.0.2.72"}]
    data["routing"]["sources"]["example_den_player"] = {"label": "Player"}
    data["routing"]["rooms"][DEN] = {"hdmi_switch": {"device": "example_avpro", "output": 4,
                                                     "inputs": {"example_den_player": 2}},
                                     "sources": ["example_den_player"]}
    return data


def device(data, ident):
    return next(d for d in data["devices"] if d["id"] == ident)


def point(ident, output):
    return {"type": "Switch", "label": f"Output {output} hot-plug", "icon": "video", "groups": [ident],
            "tags": ["Status"], "autoupdate": False, "metadata": {"stateDescription": {"config": {"readOnly": True}}}}


class Items(unittest.TestCase):
    def test_the_fictional_house_is_valid(self):
        self.assertEqual(render.validate(house()), [])

    def test_the_matrices_that_report_it_define_the_hook_alike(self):
        self.assertEqual({module.NAME for module in integrations.defining("hotplug_item")},
                         {avpro_telnet.NAME, bk808_http.NAME})
        for module in (avpro_telnet, bk808_http):
            self.assertEqual(module.hotplug_item({"id": "example"}, 3), "example_output3_hotplug")
        self.assertTrue(_hotplug.owns({"id": "example"}, "example_output12_hotplug"))
        for name in ("example_output0_hotplug", "example_output1_input", "other_example_output1_hotplug"):
            self.assertFalse(_hotplug.owns({"id": "example"}, name), name)

    def test_the_activity_feed_leaves_them_out(self):
        data = house()
        items = remote_activity.manifest(House(data), render.build(House(copy.deepcopy(data))))["items"]
        self.assertIn("example_matrix_output1_input", items)
        self.assertIn("example_avpro_output4_input", items)
        self.assertEqual([name for name in items if name.endswith("_hotplug")], [])

    def test_each_output_has_a_read_only_switch_the_bk808_polls_with_its_scaler(self):
        data = house()
        built = render.build(House(data))
        items = {name: item for file in built.yaml.values() for name, item in file.items.items()}
        matrix = device(data, "example_matrix")
        thing = built.yaml[bk808_http.FILE].things[bk808_http.thing_uid(matrix)]
        for output in range(1, bk808_http.OUTPUTS + 1):
            with self.subTest(output=output):
                channel = thing["channels"][f"output{output}_hotplug"]
                self.assertEqual(channel, {"type": "switch", "label": f"Output {output} hot-plug", "config": {
                    "mode": "READONLY", "stateContent": instr.body("get output status", language=0),
                    "stateTransformation": [f"JSONPATH($.allconnect[{output - 1}])"], "onValue": "1", "offValue": "0"}})
                # One request answers both: the binding polls it once.
                self.assertEqual(channel["config"]["stateContent"],
                                 thing["channels"][f"output{output}_scaler"]["config"]["stateContent"])
                self.assertEqual(items[f"example_matrix_output{output}_hotplug"],
                                 {**point("example_matrix", output),
                                  "channel": f"{bk808_http.thing_uid(matrix)}:output{output}_hotplug"})
        for output in range(1, 5):
            # The AVPro's rule posts them from its HPD reads: no channel.
            self.assertEqual(items[f"example_avpro_output{output}_hotplug"], point("example_avpro", output))


class Routing(unittest.TestCase):
    def test_each_room_checks_the_output_it_names_on_a_matrix_that_reports_it(self):
        table = routing.table(House(house()))
        desk, den = table["rooms"][DESK], table["rooms"][DEN]
        self.assertEqual(desk["hdmiHotplug"], "example_matrix_output1_hotplug")
        # The VRROOM says nothing of its output, and the den's AVPro is its switch.
        self.assertIsNone(desk["switch"]["hotplug"])
        self.assertIsNone(den["hdmiHotplug"])
        self.assertEqual(den["switch"]["hotplug"], "example_avpro_output4_hotplug")
        self.assertEqual(desk["diagnosis"]["checks"]["example_matrix_output1_hotplug"], {
            "device": "example_matrix", "label": "BK-808", "model": "BK-808", "action": routing.DETECT, "output": 1})
        self.assertEqual(den["diagnosis"]["checks"]["example_avpro_output4_hotplug"], {
            "device": "example_avpro", "label": "AC-MX-44X", "model": "AC-MX-44X", "action": routing.DETECT, "output": 4})
        self.assertEqual(table["hotplug"], {"reports": _hotplug.REPORTS, "absentMs": 1000 * _hotplug.ABSENT})
        self.assertGreaterEqual(_hotplug.REPORTS, 2)

    def test_the_gateway_passes_a_no_display_problem_with_its_output(self):
        data = house()
        contract = oh_site_contract.compile(House(data), render.build(House(copy.deepcopy(data))))
        table = routing.table(House(data))
        for room, output in ((DESK, 1), (DEN, 4)):
            with self.subTest(room=room):
                check = table["rooms"][room]["diagnosis"]
                spec = contract["states"][check["item"]]
                item = next(name for name, row in check["checks"].items() if row["action"] == routing.DETECT)
                problem = {"version": 1, **check["checks"][item], "item": item, "reason": "no_display", "reported": None}
                # The gateway keeps a field only when its schema takes the value
                # (oh_identity.runtime.json_value).
                for key, value in problem.items():
                    field = spec["fields"][key]
                    self.assertTrue(value in field.get("values", ()) or (field["kind"] == "number"
                                    and field["min"] <= value <= field["max"]), key)
                self.assertEqual(spec["fields"]["output"], {"kind": "enum", "values": [output]})

    def test_the_remote_words_every_action_and_reason_the_routing_gives(self):
        text = (AREA / "web/remote/backend.js").read_text()
        body = text[text.index("function sourceProblem("):]
        lists = re.findall(r"!\[([^\]]*)\]\.includes\(issue\.(action|reason)\)", body[:body.index("const device")])
        found = {name: [value.strip().strip('"') for value in values.split(",")] for values, name in lists}
        self.assertEqual(found, {"action": list(routing.ACTIONS), "reason": list(routing.REASONS)})


if __name__ == "__main__":
    unittest.main()
