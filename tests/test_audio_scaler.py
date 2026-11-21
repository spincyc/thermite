"""The scaler of a room's audio output, by source (routing
`hdmi_audio_scaler`): a computer whose picture the receiver need not convert
gets audio only there, and every other choice gives the output the matrix's
fixed scaler back.

A synthetic one-room house with fictional ids and documentation addresses:
the Example Lounge's picture leaves the matrix on output 1 for its TV and
its sound on output 2 for its receiver. The JavaScript runs the production
routing.js and lifecycle planner (tests/audio_scaler_harness.js).
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_conf import House  # noqa: E402
import oh_matrix_sim  # noqa: E402
import oh_render  # noqa: E402
from oh_cli import matrix_settings  # noqa: E402
from oh_features import routing  # noqa: E402
from oh_integrations import _instr as instr, bk808_http  # noqa: E402
from oh_room_policy import expected_plan  # noqa: E402
from oh_testing import routing as routing_check  # noqa: E402

LOUNGE = "example_lounge"
COMPUTER, DISC, PLAYER = "example_computer", "example_disc", "example_player"
OUT1, OUT2 = "example_matrix_output1_input", "example_matrix_output2_input"
SCALER2, USB1 = "example_matrix_output2_scaler", "example_usb_device1_host"
WHERE = f"routing.rooms.{LOUNGE}.hdmi_audio_scaler"


def device(ident, kind, number, **extra):
    return {"id": ident, "room": LOUNGE, "kind": kind, "name": ident.replace("_", " ").title(),
            "ip": f"192.0.2.{number}", **extra}


HOUSE = {
    "version": 1,
    "rooms": {LOUNGE: {"label": "Example Lounge", "tag": "Room"}},
    "devices": [
        device("example_matrix", "hdmi_matrix", 20, model="HDMI matrix", controls={"control": ["bk808_http"]},
               settings={"scaler": "auto"}),
        device("example_usb", "usb_matrix", 21, model="UKM-404", controls={"control": ["ukm404_http"]}),
        device("example_avr", "avr", 22, make="Marantz", model="CINEMA 30",
               controls={"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}),
        device("example_tv", "tv", 23, make="Sony", model="Example TV",
               controls={"observe": ["sony_rest"], "control": ["sony_ip_control"]}),
        device(PLAYER, "streamer", 24, make="Apple", model="Example Player"),
        device(COMPUTER, "computer", 25, model="Computer"),
        device(DISC, "disc_player", 26, make="Sony", model="Example Disc"),
    ],
    "routing": {
        "hdmi_matrix": "example_matrix", "hdmi_off_input": 8, "usb_off_host": 4,
        "sources": {PLAYER: {"label": "Player"}, COMPUTER: {"label": "Computer", "hdmi_input": 1, "usb_host": 1},
                    DISC: {"label": "Disc", "hdmi_input": 2}},
        "rooms": {
            # The player is cabled to the receiver alone; the TV takes it from
            # the receiver's HDMI out. usb_unused: park turns the lifecycle on.
            LOUNGE: {"hdmi_output": 1, "hdmi_audio_output": 2, "hdmi_audio_scaler": {COMPUTER: "audio-only"},
                     "usb": {"example_usb": [1]}, "usb_unused": "park", "sources": [COMPUTER, DISC, PLAYER],
                     "receiver": {"device": "example_avr", "input": "AUX2", "sources": {PLAYER: "MPLAY"}},
                     "display": {"device": "example_tv", "input": "hdmi1", "input_by_source": {PLAYER: "hdmi2"}}},
        },
    },
}


def house(change=None):
    data = copy.deepcopy(HOUSE)
    if change:
        change(data["routing"]["rooms"], data)
    return data


def matrix(data):
    return next(d for d in data["devices"] if d["id"] == "example_matrix")


def rendered_table(data):
    """The TABLE routing.js is rendered with, as the runtime receives it."""
    text = oh_render.render(House(data))[routing.RULE]
    start = text.index("const TABLE = ") + len("const TABLE = ")
    return json.JSONDecoder().raw_decode(text, start)[0]


class Validation(unittest.TestCase):
    def test_the_synthetic_house_is_valid(self):
        self.assertEqual(oh_render.validate(house()), [])

    def test_malformed_matrix_scaler_returns_a_validation_problem(self):
        for value in ([], {}):
            with self.subTest(value=value):
                data = house()
                matrix(data)["settings"]["scaler"] = value
                problems = oh_render.validate(data)
                self.assertIn(f"example_matrix.settings.scaler: {value!r} is not one of "
                              "bypass, 8k-to-4k, to-1080p, auto, audio-only", problems)

    def test_each_rule_has_a_clear_problem(self):
        def scaler(value):
            return lambda rooms, _: rooms[LOUNGE].update(hdmi_audio_scaler=value)

        def unoffered(rooms, _):
            rooms[LOUNGE]["sources"].remove(DISC)
            rooms[LOUNGE]["hdmi_audio_scaler"] = {DISC: "audio-only"}

        def without_scaler(_, data):
            matrix(data)["controls"]["control"] = ["avpro_telnet"]
            matrix(data).pop("settings")
            data["routing"]["hdmi_off_input"] = 4

        names = "bypass, 8k-to-4k, to-1080p, auto, audio-only"
        cases = {
            "no audio output": (lambda rooms, _: rooms[LOUNGE].pop("hdmi_audio_output"),
                                f"{WHERE}: needs hdmi_audio_output, the output whose scaler it sets"),
            "not a mapping": (scaler([COMPUTER]), f"{WHERE}: must map sources to a scaler name"),
            "empty": (scaler({}), f"{WHERE}: must map sources to a scaler name"),
            "not a scaler": (scaler({COMPUTER: "loud"}),
                             f"{WHERE}.{COMPUTER}: 'loud' is not a scaler name of example_matrix ({names})"),
            "a local source": (scaler({PLAYER: "audio-only"}),
                               f"{WHERE}.{PLAYER}: '{PLAYER}' is not one of this room's sources from the HDMI matrix"),
            "not offered": (unoffered,
                            f"{WHERE}.{DISC}: '{DISC}' is not one of this room's sources from the HDMI matrix"),
            "no fixed scaler": (lambda _, data: matrix(data).pop("settings"),
                                f"{WHERE}: needs example_matrix.settings.scaler, the scaler the output gets back "
                                "for the room's other choices"),
            "integration without scalers": (without_scaler, f"{WHERE}: avpro_telnet cannot set an output's scaler"),
        }
        for name, (change, problem) in cases.items():
            with self.subTest(name):
                self.assertIn(problem, routing.validate(house(change)))


class Commands(unittest.TestCase):
    def test_the_scaler_is_set_before_the_audio_output_and_given_back_for_every_other_choice(self):
        data = House(house())
        self.assertEqual(routing.commands(data, LOUNGE, COMPUTER), [[OUT1, 1], [SCALER2, 4], [OUT2, 1], [USB1, 1]])
        self.assertEqual(routing.commands(data, LOUNGE, DISC), [[OUT1, 2], [SCALER2, 3], [OUT2, 2], [USB1, 4]])
        for choice in (routing.OFF, PLAYER):
            with self.subTest(choice):
                self.assertEqual(routing.commands(data, LOUNGE, choice), [[OUT1, 8], [SCALER2, 3], [OUT2, 8], [USB1, 4]])
        self.assertEqual(routing.routed_scalers(data, "example_matrix"), {2: LOUNGE})
        self.assertEqual(routing.routed_scalers(data, "example_usb"), {})

    def test_other_choices_restore_the_configured_fixed_scaler(self):
        data = house()
        matrix(data)["settings"]["scaler"] = "to-1080p"
        config = House(data)
        for choice in (DISC, PLAYER, routing.OFF):
            with self.subTest(choice):
                self.assertEqual(dict(routing.commands(config, LOUNGE, choice))[SCALER2], 2)

    def test_without_it_no_route_sets_a_scaler(self):
        plain = House(house(lambda rooms, _: rooms[LOUNGE].pop("hdmi_audio_scaler")))
        example = House(json.loads((AREA / "instance/devices.yaml").read_text()))
        for data in (plain, example):
            for raw in routing.table(data)["rooms"].values():
                steps = [item for choice in raw["choices"].values() for item, _ in choice]
                self.assertFalse([item for item in steps if item.endswith("_scaler")])
        self.assertEqual(routing.routed_scalers(plain, "example_matrix"), {})


class Descriptor(unittest.TestCase):
    def test_matrix_routes_require_the_scaler_and_the_others_release_it(self):
        descriptor = routing.table(House(house()))["lifecycle"]
        resource = descriptor["resources"][SCALER2]
        self.assertEqual((resource["key"], resource["values"], resource["release"]),
                         ("example_matrix", ["0", "1", "2", "3", "4"], "3"))
        self.assertIs(resource["independent"], True)
        self.assertIs(resource["exclusive"], False)
        self.assertEqual(descriptor["resources"][OUT2]["requires"], {"1": {SCALER2: "4"}, "2": {SCALER2: "3"}})
        choices = descriptor["endpoints"][LOUNGE]["choices"]
        self.assertEqual(choices[COMPUTER]["requirements"][SCALER2], "4")
        self.assertEqual(choices[DISC]["requirements"][SCALER2], "3")
        for choice in (routing.OFF, PLAYER):
            with self.subTest(choice):
                self.assertNotIn(SCALER2, choices[choice]["requirements"])
                self.assertEqual(choices[choice]["release"][SCALER2], "3")


class Policy(unittest.TestCase):
    def test_the_room_policy_verifies_the_scaler_of_a_matrix_route_and_of_off(self):
        def fields(choice):
            return {field.item: field.value for field in expected_plan(house(), LOUNGE, choice).fields}
        self.assertEqual(fields(COMPUTER)[SCALER2], "4")
        self.assertEqual(fields(DISC)[SCALER2], "3")
        self.assertEqual(fields(routing.OFF)[SCALER2], "3")
        # A source cabled to the receiver parks the matrix: no matrix field.
        self.assertNotIn(SCALER2, fields(PLAYER))


class Matrix(unittest.TestCase):
    def test_each_output_has_a_scaler_item_set_with_set_video_scaler(self):
        data = house()
        built = oh_render.build(House(data))
        file = built.yaml[bk808_http.FILE]
        thing = file.things[bk808_http.thing_uid(matrix(data))]
        for output in range(1, bk808_http.OUTPUTS + 1):
            with self.subTest(output):
                item = bk808_http.scaler_item(matrix(data), output)
                self.assertIn(item, file.items)
                config = thing["channels"][f"output{output}_scaler"]["config"]
                self.assertEqual(config["stateContent"], instr.body("get output status", language=0))
                self.assertEqual(config["stateTransformation"], [f"JSONPATH($.allscaler[{output - 1}])"])
                self.assertIn(f"comhead:'set video scaler',language:0,scaler:[{output},parseInt(input)]",
                              config["commandTransformation"][0])
        self.assertIn("set video scaler", bk808_http.COMHEADS)
        self.assertEqual(bk808_http.fields(matrix(data))[SCALER2],
                         {"kind": "http", "thing": bk808_http.thing_uid(matrix(data)), "channel": "output2_scaler",
                          "values": ["0", "1", "2", "3", "4"], "ack": "result", "independent": True})

    def test_matrix_settings_leaves_a_routed_scaler_alone(self):
        device = matrix(house())
        device["settings"]["hdr"] = "auto"
        status = oh_matrix_sim.BK808.default_status()
        changed = [what for what, _ in bk808_http.plan(device, status)]
        self.assertIn("output 2 scaler bypass -> auto", changed)
        kept = [what for what, _ in bk808_http.plan(device, status, frozenset({2}))]
        self.assertIn("output 2 hdr bypass -> auto", kept)
        self.assertEqual(kept, [what for what in changed if not what.startswith("output 2 scaler ")])

        sim = oh_matrix_sim.BK808()
        sim.answer({"comhead": "set video scaler", "language": 0, "scaler": [2, 4]})
        lines = []
        code = matrix_settings.run(device, bk808_http, "192.0.2.20", True, lambda _, body: sim.answer(json.loads(body)),
                                   pause=0, log=lines.append, routed={2: LOUNGE})
        self.assertEqual(code, 0, lines)
        self.assertIn(f"example_matrix (Example Matrix, 192.0.2.20): output 2 scaler left to the routing ({LOUNGE})",
                      lines)
        self.assertEqual([sim.scaler(output) for output in range(1, 9)], [3, 4, 3, 3, 3, 3, 3, 3])


class RoutingCheck(unittest.TestCase):
    """thermite test routing expects the scaler a route sets."""

    def test_the_check_reads_each_routed_scaler_from_its_stand_in(self):
        with mock.patch.object(routing_check.sim, "serve", return_value=None):
            check = routing_check.Check(House(house()), log=lambda *_: None)
        self.assertEqual(check.scalers, {SCALER2: ("example_matrix", 2)})
        stand_in = check.sims["example_matrix"]
        stand_in.answer({"comhead": "set video scaler", "language": 0, "scaler": [2, 4]})
        stand_in.set(2, 1)
        self.assertEqual({item: check.matrices()[item] for item in (OUT2, SCALER2)}, {OUT2: 1, SCALER2: 4})


@unittest.skipUnless(shutil.which("node"), "node is required for the production routing script")
class Runtime(unittest.TestCase):
    def test_the_scaler_follows_the_choice_with_and_without_the_lifecycle(self):
        tables = {"lifecycle": rendered_table(house()),
                  "plain": rendered_table(house(lambda rooms, _: rooms[LOUNGE].pop("usb_unused")))}
        self.assertIn("lifecycle", tables["lifecycle"])
        self.assertNotIn("lifecycle", tables["plain"])
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/audio_scaler_harness.js"),
                                 str(routing.SOURCE), str(routing.SOURCE.with_name("lifecycle.js"))],
                                input=json.dumps(tables), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["passed"], report["failed"]), (5, 0))


if __name__ == "__main__":
    unittest.main()
