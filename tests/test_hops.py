"""Single hop-graph room model (Stage 3a): compiler, schema extensions,
cross-consumer consistency, partial readback, sleep_sources ordering,
per-field lifecycle adoption, per-room holdMs, and 0-HDMI-matrix houses."""
from __future__ import annotations

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
import oh_render  # noqa: E402
from oh_features import reboots, remote_diagnostics, remote_model, remote_topology, routing  # noqa: E402
from oh_features.routing import hops, lifecycle, lifecycle_io  # noqa: E402
from oh_room_policy import expected_plan  # noqa: E402
from oh_testing import routing as routing_check  # noqa: E402

CINEMA = "example_cinema"
DUAL = "example_dual"
LOUNGE = "example_lounge"
FAST = "example_fast"
SCALED = "example_scaled"

COMPUTER = "example_computer"
DISC = "example_disc"
LOCAL_UP = "example_up_local"
LOCAL_DOWN = "example_down_local"
LOCAL_DISP = "example_disp_local"
LOCAL_BAR = "example_bar_local"
SLEEPER = "example_sleeper"


def device(ident: str, room: str, kind: str, octet: int, **extra) -> dict:
    return {
        "id": ident,
        "room": room,
        "kind": kind,
        "name": ident.replace("_", " ").title(),
        "ip": f"192.0.2.{octet}",
        **extra,
    }


TV_SPEC = {"make": "Sony", "model": "Example TV", "controls": {"observe": ["sony_rest"], "control": ["sony_ip_control"]}}
BAR_SPEC = {
    "make": "Marantz", "model": "Example Soundbar",
    "config": {"denonmarantz_http": {"inputs": ["BD", "MPLAY"]}},
    "controls": {"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]},
}


def full_house(change=None) -> dict:
    """Synthetic multi-room house exercising every Stage 3a hop type:
    - primary HDMI matrix + split audio + scaler + EDID
    - cascaded HDMI switches (upstream downstream-matrix + downstream switch)
    - receiver + soundbar + single display + dual displays
    - USB matrix + dedicated sleep_sources + on/off actions + zone sync
    """
    data = {
        "version": 2,
        "rooms": {
            CINEMA: {"label": "Example Cinema", "tag": "Room"},
            DUAL: {"label": "Example Dual", "tag": "Room"},
            LOUNGE: {"label": "Example Lounge", "tag": "Room"},
            FAST: {"label": "Example Fast", "tag": "Room"},
            SCALED: {"label": "Example Scaled", "tag": "Room"},
        },
        "devices": [
            device("example_matrix", CINEMA, "hdmi_matrix", 20, make="BZBGEAR", model="BK-808",
                   config={"bk808_http": {"scaler": "bypass"}},
                   controls={"control": ["bk808_http"]}),
            device("example_usb", CINEMA, "usb_matrix", 21, model="UKM-404",
                   controls={"control": ["ukm404_http"]}),
            device("example_avpro", DUAL, "hdmi_matrix", 22, make="AVPro Edge", model="AC-MX-44X",
                   controls={"control": ["avpro_telnet"]}),
            device("example_vrroom", DUAL, "hdmi_switch", 23, make="HDFury", model="VRROOM",
                   controls={"control": ["vrroom_telnet"]}),
            device("example_avr", CINEMA, "avr", 24, make="Marantz", model="CINEMA 30",
                   config={"denonmarantz_http": {"zones": 2}},
                   controls={"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}),
            device("example_bar", CINEMA, "soundbar", 25, **copy.deepcopy(BAR_SPEC)),
            device("example_screen", CINEMA, "tv", 26, **copy.deepcopy(TV_SPEC)),
            device("example_tv_left", DUAL, "tv", 27, **copy.deepcopy(TV_SPEC)),
            device("example_tv_right", DUAL, "tv", 28, **copy.deepcopy(TV_SPEC)),
            device("example_lounge_tv", LOUNGE, "tv", 29, **copy.deepcopy(TV_SPEC)),
            device("example_fast_tv", FAST, "tv", 30, **copy.deepcopy(TV_SPEC)),
            device("example_scaled_tv", SCALED, "tv", 39, **copy.deepcopy(TV_SPEC)),
            device("example_scaled_avr", SCALED, "avr", 44, make="Marantz", model="CINEMA 30",
                   controls={"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}),
            device("example_curtain", CINEMA, "curtain", 31),
            device(COMPUTER, CINEMA, "computer", 32, model="Computer"),
            device(DISC, CINEMA, "disc_player", 33, make="Sony", model="Example Disc"),
            device(LOCAL_UP, DUAL, "streamer", 34, model="Upstream Streamer"),
            device(LOCAL_DOWN, DUAL, "game_console", 35, model="Downstream Console"),
            device(LOCAL_DISP, DUAL, "streamer", 36, model="Display Streamer"),
            device(LOCAL_BAR, CINEMA, "streamer", 37, model="Bar Streamer"),
            device(SLEEPER, CINEMA, "streamer", 38, make="Apple", model="Example Streamer",
                   controls={"control": ["pyatv_http"]}),
        ],
        "routing": {
            "hdmi_matrix": "example_matrix",
            "hdmi_off_input": 8,
            "usb_off_host": 4,
            "edid": {"idle": "4k60-444-hdr-2ch"},
            "sources": {
                COMPUTER: {"label": "Computer", "hdmi_input": 1, "usb_host": 1},
                DISC: {"label": "Disc", "hdmi_input": 2},
                LOCAL_UP: {"label": "Upstream Box"},
                LOCAL_DOWN: {"label": "Downstream Box"},
                LOCAL_DISP: {"label": "Left TV Box"},
                LOCAL_BAR: {"label": "Bar Box"},
                SLEEPER: {"label": "Sleeper", "hdmi_input": 3},
            },
            "rooms": {
                CINEMA: {
                    "hdmi_output": 1,
                    "usb": {"example_usb": [1]},
                    "sources": [COMPUTER, DISC, SLEEPER, LOCAL_BAR],
                    "sleep_sources": [SLEEPER],
                    "receiver": {"device": "example_avr", "input": "BD"},
                    "soundbar": {"device": "example_bar", "input": "BD", "sources": {LOCAL_BAR: "MPLAY"}},
                    "display": {"device": "example_screen", "input": "hdmi1"},
                    "on_actions": {"example_curtain": "close"},
                    "off_actions": {"example_curtain": "open"},
                },
                DUAL: {
                    "hdmi_output": 3,
                    "hdmi_switches": [
                        {"device": "example_avpro", "output": 4, "matrix_input": 1, "inputs": {LOCAL_UP: 2}},
                        {"device": "example_vrroom", "matrix_input": 0, "inputs": {LOCAL_DOWN: 1}},
                    ],
                    "usb": {"example_usb": [2]},
                    "sources": [COMPUTER, LOCAL_UP, LOCAL_DOWN, LOCAL_DISP],
                    "displays": [
                        {"device": "example_tv_left", "input": "hdmi1", "sources": {LOCAL_DISP: "hdmi2"}},
                        {"device": "example_tv_right", "input": "hdmi1"},
                    ],
                },
                LOUNGE: {
                    "hdmi_output": 4,
                    "sources": [COMPUTER, DISC],
                    "receiver": {"device": "example_avr", "zone": 2, "input": "AUX2", "sync_with": CINEMA},
                    "display": {"device": "example_lounge_tv", "input": "hdmi2"},
                },
                FAST: {
                    "hdmi_output": 5,
                    "sources": [COMPUTER],
                    "display": {"device": "example_fast_tv", "input": "hdmi1"},
                },
                SCALED: {
                    "hdmi_output": 6,
                    "hdmi_audio_output": 7,
                    "hdmi_audio_scaler": {DISC: "to-1080p"},
                    "sources": [COMPUTER, DISC],
                    "receiver": {"device": "example_scaled_avr", "input": "BD"},
                    "display": {"device": "example_scaled_tv", "input": "hdmi1"},
                },
            },
        },
    }
    if change is not None:
        change(data)
    return data


def zero_matrix_house() -> dict:
    """Valid house with NO primary hdmi_matrix (only local switch & display sources)."""
    return {
        "version": 2,
        "rooms": {
            FAST: {"label": "Example Fast", "tag": "Room"},
        },
        "devices": [
            device("example_vrroom", FAST, "hdmi_switch", 40, make="HDFury", model="VRROOM",
                   controls={"control": ["vrroom_telnet"]}),
            device("example_fast_tv", FAST, "tv", 41, **copy.deepcopy(TV_SPEC)),
            device(LOCAL_DOWN, FAST, "game_console", 42, model="Console"),
            device(LOCAL_DISP, FAST, "streamer", 43, model="Streamer"),
        ],
        "routing": {
            "sources": {
                LOCAL_DOWN: {"label": "Console"},
                LOCAL_DISP: {"label": "Streamer"},
            },
            "rooms": {
                FAST: {
                    "hdmi_switch": {"device": "example_vrroom", "inputs": {LOCAL_DOWN: 1}},
                    "sources": [LOCAL_DOWN, LOCAL_DISP],
                    "display": {"device": "example_fast_tv", "input": "hdmi1", "sources": {LOCAL_DISP: "hdmi2"}},
                },
            },
        },
    }


def run_routing_js_shown(table: dict, room_id: str, states: dict[str, str]) -> str:
    """Evaluate production routing.js `shown(TABLE.rooms[room_id])` under node."""
    script = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.sourcePath, 'utf8');
const items = new Map();
for (const [k, v] of Object.entries(input.states)) {
  items.set(k, { state: String(v), sendCommand() {}, postUpdate(nv) { this.state = String(nv); } });
}
const getItem = (name, nullable) => items.get(name) || (nullable ? null : { state: 'NULL', sendCommand() {}, postUpdate() {} });
const modules = {
  openhab: {
    items: { getItem },
    rules: { JSRule() {} },
    triggers: { ItemCommandTrigger: () => ({}), ItemStateChangeTrigger: () => ({}), ItemStateUpdateTrigger: () => ({}) },
  },
  '@runtime': { lifecycleTracker: { addDisposeHook() {} } },
};
const ctx = vm.createContext({
  require: (n) => modules[n] || (() => { throw new Error('unexpected ' + n); })(),
  console: { info() {}, warn() {}, error() {} },
  setTimeout: () => 0, clearTimeout() {}, Date, JSON, Map, Set, Object, String, Number, Error, Math,
});
const tableNoLc = Object.assign({}, input.table, { lifecycle: null });
vm.runInContext(source.replace('const TABLE = null;', 'const TABLE = ' + JSON.stringify(tableNoLc) + ';'), ctx);
process.stdout.write(JSON.stringify({ shown: ctx.shown(tableNoLc.rooms[input.room]) }));
"""
    proc = subprocess.run(
        [shutil.which("node"), "-e", script],
        input=json.dumps({"sourcePath": str(routing.SOURCE), "table": table, "room": room_id, "states": states}),
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)["shown"]


class TestSingleHopGraphAndConsumers(unittest.TestCase):
    def test_full_house_validates_and_renders_cleanly(self):
        data = full_house()
        self.assertEqual(oh_render.validate(data), [])
        rendered = oh_render.render(House(data))
        self.assertIn(routing.RULE, rendered)
        # Source item stateDescription offers partial=Partially on, while commandDescription does not.
        source_meta = routing.source_item(House(data), CINEMA)["metadata"]
        state_opts = source_meta["stateDescription"]["config"]["options"]
        cmd_opts = source_meta["commandDescription"]["config"]["options"]
        self.assertIn("partial=Partially on", state_opts)
        self.assertNotIn("partial", cmd_opts)

    def test_hop_graph_types_and_relations(self):
        house = House(full_house())
        graph = hops.compile_house(house)
        # Scaled room on DISC: 2 matrix hops (video + split audio w/ scaler).
        scaled_disc = graph.path(SCALED, DISC)
        self.assertEqual(len(scaled_disc.matrix_hops()), 2)
        audio_hop = next(h for h in scaled_disc.matrix_hops() if h.role == "audio")
        self.assertEqual(audio_hop.scaler_item, "example_matrix_output7_scaler")
        self.assertEqual(audio_hop.scaler_value, 2)  # to-1080p

        # Cinema on DISC: 1 matrix hop, 3 stages (receiver, soundbar, display),
        # 1 USB hop, 1 action hop, EDID relation.
        cinema_disc = graph.path(CINEMA, DISC)
        self.assertEqual(len(cinema_disc.matrix_hops()), 1)
        self.assertEqual([s.role for s in cinema_disc.stage_hops()], ["receiver", "soundbar", "display"])
        self.assertTrue(all(s.active for s in cinema_disc.stage_hops()))
        self.assertIsNotNone(cinema_disc.edid)
        self.assertEqual(cinema_disc.action_hops()[0].value, "close")

        # Cinema on LOCAL_BAR: soundbar + display active, receiver bypassed, matrix parked.
        cinema_bar = graph.path(CINEMA, LOCAL_BAR)
        self.assertTrue(cinema_bar.is_local)
        self.assertEqual(cinema_bar.local_origin, "soundbar")
        self.assertFalse(cinema_bar.matrix_hops()[0].active)
        self.assertEqual([(s.role, s.active) for s in cinema_bar.stage_hops()],
                         [("receiver", False), ("soundbar", True), ("display", True)])

        # Dual room on LOCAL_UP: upstream switch active (input 2), downstream switch feeds matrix_input (0), matrix parked.
        dual_up = graph.path(DUAL, LOCAL_UP)
        self.assertTrue(dual_up.is_local)
        self.assertEqual(dual_up.local_origin, "switch:0")
        self.assertFalse(dual_up.matrix_hops()[0].active)
        sw_up, sw_down = dual_up.switch_hops()
        self.assertEqual((sw_up.device, sw_up.active, sw_up.input_value), ("example_avpro", True, 2))
        self.assertEqual((sw_down.device, sw_down.active, sw_down.input_value), ("example_vrroom", True, 0))
        self.assertEqual([(s.role, s.device, s.active) for s in dual_up.stage_hops()],
                         [("display", "example_tv_left", True), ("display", "example_tv_right", True)])

        # Dual room on LOCAL_DOWN: downstream switch active (1), upstream switch parked (1), matrix parked.
        dual_down = graph.path(DUAL, LOCAL_DOWN)
        sw_up, sw_down = dual_down.switch_hops()
        self.assertEqual((sw_up.active, sw_up.input_value), (False, 1))
        self.assertEqual((sw_down.active, sw_down.input_value), (True, 1))

        # Dual room on LOCAL_DISP: only example_tv_left is active; example_tv_right is bypassed.
        dual_disp = graph.path(DUAL, LOCAL_DISP)
        self.assertEqual([(s.device, s.active, s.input_value) for s in dual_disp.stage_hops()],
                         [("example_tv_left", True, "hdmi2"), ("example_tv_right", False, None)])

        # Lounge on COMPUTER: synced relation with Cinema.
        lounge_comp = graph.path(LOUNGE, COMPUTER)
        self.assertIsNotNone(lounge_comp.sync)
        assert lounge_comp.sync is not None
        self.assertEqual((lounge_comp.sync.leader, lounge_comp.sync.leader_hdmi_device, lounge_comp.sync.leader_input_value),
                         (CINEMA, "example_matrix", "BD"))

    def test_all_six_consumers_derive_consistently_from_hops(self):
        data = full_house()
        house = House(data)
        table = routing.table(house)

        # 1. routing.table
        self.assertEqual(table["partial"], "partial")
        self.assertIn("lifecycle", table)
        dual_tbl = table["rooms"][DUAL]
        self.assertEqual([sw["item"] for sw in dual_tbl["switches"]],
                         ["example_vrroom_input", "example_avpro_output4_input"])
        self.assertEqual([st["role"] for st in table["rooms"][CINEMA]["stages"]],
                         ["display", "soundbar", "receiver"])
        self.assertEqual([st["role"] for st in dual_tbl["stages"]], ["display", "display"])

        # 2. lifecycle descriptor
        desc = table["lifecycle"]
        self.assertIn("example_bar_main_power", desc["resources"])
        self.assertIn("example_bar_main_input", desc["resources"])
        self.assertIn("example_tv_left_power", desc["resources"])
        self.assertIn("example_tv_right_power", desc["resources"])
        self.assertIn("example_avpro_output4_input", desc["resources"])
        self.assertIn("example_vrroom_input", desc["resources"])
        # Active requirements for LOCAL_DISP only activate left TV, never right TV.
        self.assertEqual(lifecycle.active_requirements(house, DUAL, LOCAL_DISP),
                         {"example_tv_left_power": "ON", "example_tv_left_input": "hdmi2"})
        left_reqs = desc["endpoints"][DUAL]["choices"][LOCAL_DISP]["requirements"]
        self.assertIn("example_tv_left_power", left_reqs)
        self.assertNotIn("example_tv_right_power", left_reqs)

        # 3. room policy expected_plan
        plan_bar = expected_plan(house, CINEMA, LOCAL_BAR)
        verified_items = {f.item for f in plan_bar.fields}
        self.assertIn("example_bar_main_power", verified_items)
        self.assertIn("example_bar_main_input", verified_items)
        self.assertIn("example_screen_power", verified_items)
        self.assertNotIn("example_avr_main_power", verified_items)
        plan_dual_up = expected_plan(house, DUAL, LOCAL_UP)
        dual_items = {f.item: f.value for f in plan_dual_up.fields}
        self.assertEqual(dual_items["example_avpro_output4_input"], "2")
        self.assertEqual(dual_items["example_vrroom_input"], "0")
        self.assertEqual(dual_items["example_tv_left_power"], "ON")
        self.assertEqual(dual_items["example_tv_right_power"], "ON")

        # 4. remote_diagnostics & remote_topology
        conf = oh_render.build(house)
        diag = remote_diagnostics.manifest(house, conf)
        cinema_diag = diag["rooms"][CINEMA]["sources"][COMPUTER]
        self.assertEqual([n["device"] for n in cinema_diag["nodes"]],
                         ["example_computer", "example_matrix", "example_avr", "example_bar", "example_screen"])
        dual_diag = diag["rooms"][DUAL]["sources"][COMPUTER]
        self.assertEqual([n["device"] for n in dual_diag["nodes"]],
                         ["example_computer", "example_matrix", "example_avpro", "example_vrroom", "example_tv_left"])
        self.assertEqual([[n["device"] for n in branch] for branch in dual_diag["displayBranches"]],
                         [["example_computer", "example_matrix", "example_avpro", "example_vrroom", "example_tv_right"]])
        # Topology emits parallel edges from example_vrroom -> both displays, NEVER left -> right!
        topo = remote_topology.manifest(house, conf)
        route = topo["routes"][DUAL][COMPUTER]
        edge_pairs = {(e["from"], e["to"]) for e in topo["edges"]}
        self.assertIn(("example_vrroom", "example_tv_left"), edge_pairs)
        self.assertIn(("example_vrroom", "example_tv_right"), edge_pairs)
        self.assertNotIn(("example_tv_left", "example_tv_right"), edge_pairs)
        self.assertEqual([n["device"] for n in route["displayBranches"][0]][-1], "example_tv_right")

        # 5. reboots.rooms_off covers primary matrix, BOTH cascaded switches, USB matrix, soundbar, dual displays!
        def off_items(dev_id: str, spec: dict) -> list[str]:
            return [r["item"] for r in reboots.rooms_off(data, {"id": dev_id}, spec)]
        self.assertEqual(off_items("example_avpro", {"rooms": True}), [routing.item_name(DUAL)])
        self.assertEqual(off_items("example_vrroom", {"rooms": True}), [routing.item_name(DUAL)])
        self.assertEqual(off_items("example_matrix", {"rooms": True}),
                         [routing.item_name(r) for r in (CINEMA, DUAL, LOUNGE, FAST, SCALED)])
        self.assertEqual(off_items("example_usb", {}), [routing.item_name(r) for r in (CINEMA, DUAL)])
        self.assertEqual(off_items("example_bar", {"rooms": True}), [routing.item_name(CINEMA)])
        self.assertEqual(off_items("example_tv_right", {"rooms": True}), [routing.item_name(DUAL)])

        # 6. oh_testing.routing.Check derives modules, ports, scalers, inputs from hops (no vrroom_telnet import!).
        self.assertFalse(hasattr(routing_check, "vrroom_telnet"))
        with mock.patch.object(routing_check.sim, "serve", return_value=None):
            check = routing_check.Check(house, log=lambda *_: None)
        self.assertIn("example_avpro", check.switches)
        self.assertIn("example_vrroom", check.switches)
        self.assertEqual(check.ports["example_avpro_output4_input"], ("example_avpro", 4))
        self.assertEqual(check.ports["example_vrroom_input"], ("example_vrroom", 0))
        self.assertIn("example_bar_main_input", check.inputs)
        self.assertIn("example_tv_left_input", check.inputs)
        self.assertIn("example_tv_right_input", check.inputs)
        self.assertEqual(check.scalers, {"example_matrix_output7_scaler": ("example_matrix", 7)})

    def test_remote_model_recognizes_soundbar_for_volume(self):
        # A room with soundbar + display (no AVR) selects the soundbar as its volume target.
        def bar_only(d):
            d["routing"]["rooms"][CINEMA].pop("receiver")
            d["routing"]["rooms"][LOUNGE]["receiver"].pop("sync_with")
        house = House(full_house(bar_only))
        model = remote_model.build(house)
        cinema_row = next(r for r in model["rooms"] if r["id"] == CINEMA)
        self.assertEqual(cinema_row["audio"], "example_bar")


class TestSchemaValidation(unittest.TestCase):
    def test_mutual_exclusion_for_display_and_displays(self):
        def add_both(d):
            d["routing"]["rooms"][DUAL]["display"] = {"device": "example_tv_left", "input": "hdmi1"}
        problems = routing.validate(full_house(add_both))
        self.assertTrue(any("cannot specify both display and displays" in p for p in problems), problems)

    def test_mutual_exclusion_for_hdmi_switch_and_hdmi_switches(self):
        def add_both(d):
            d["routing"]["rooms"][DUAL]["hdmi_switch"] = {"device": "example_vrroom", "matrix_input": 0}
        problems = routing.validate(full_house(add_both))
        self.assertTrue(any("cannot specify both hdmi_switch and hdmi_switches" in p for p in problems), problems)

    def test_empty_displays_and_empty_hdmi_switches_rejected(self):
        def make_empty(d):
            d["routing"]["rooms"][DUAL]["displays"] = []
            d["routing"]["rooms"][DUAL]["hdmi_switches"] = []
        problems = routing.validate(full_house(make_empty))
        self.assertTrue(any("displays: must list one or more displays" in p for p in problems), problems)
        self.assertTrue(any("hdmi_switches: must list one or more HDMI switches" in p for p in problems), problems)

    def test_downstream_cascaded_switch_must_declare_matrix_input(self):
        def drop_link(d):
            d["routing"]["rooms"][DUAL]["hdmi_switches"][1].pop("matrix_input")
        problems = routing.validate(full_house(drop_link))
        self.assertTrue(any("hdmi_switches[1].matrix_input" in p for p in problems), problems)

    def test_duplicate_display_in_displays_rejected(self):
        def dup_disp(d):
            d["routing"]["rooms"][DUAL]["displays"][1] = {"device": "example_tv_left", "input": "hdmi1"}
        problems = routing.validate(full_house(dup_disp))
        self.assertTrue(any("already used" in p for p in problems), problems)

    def test_soundbar_stage_validation(self):
        def bad_bar(d):
            d["routing"]["rooms"][CINEMA]["soundbar"]["input"] = "bogus_input"
        problems = routing.validate(full_house(bad_bar))
        self.assertTrue(any("soundbar.input" in p for p in problems), problems)


class TestZeroHdmiMatrixHouse(unittest.TestCase):
    def test_zero_matrix_house_works_across_all_consumers_and_routing_check(self):
        data = zero_matrix_house()
        self.assertEqual(oh_render.validate(data), [])
        house = House(data)
        graph = hops.compile_house(house)
        self.assertEqual(graph.path(FAST, LOCAL_DOWN).matrix_hops(), ())
        self.assertEqual(len(graph.path(FAST, LOCAL_DOWN).switch_hops()), 1)
        table = routing.table(house)
        self.assertIsNone(table["edid"])
        self.assertIsNone(table["rooms"][FAST]["hdmi"])
        self.assertIn("lifecycle", table)
        # oh_testing.routing.Check works with zero HDMI matrix!
        with mock.patch.object(routing_check.sim, "serve", return_value=None):
            check = routing_check.Check(house, log=lambda *_: None)
        self.assertIsNone(check.hdmi_matrix)
        check.seed()
        self.assertEqual(check.edid_problems(), {})
        check.sims["example_vrroom"].set(0, 1)
        states = {"example_fast_tv_power": "ON", "example_fast_tv_input": "hdmi1"}
        self.assertEqual(check.readback_choice(FAST, states=states), LOCAL_DOWN)
        self.assertEqual(check.readback_choice(FAST, silent=frozenset({"example_vrroom"}), states=states), "UNDEF")
        self.assertEqual(check.readback_choice(FAST, states={"example_fast_tv_power": "OFF", "example_fast_tv_input": "hdmi1"}), "off")


class TestPerFieldLifecycleAndPerRoomHoldMs(unittest.TestCase):
    def test_per_room_hold_ms_separates_fast_tv_from_slow_avr(self):
        house = House(full_house())
        # Sony TV READ_BACK=10 -> max(15, 10+2)*1000 = 15000 ms
        # Denon/Marantz AVR READ_BACK=18 -> max(15, 18+2)*1000 = 20000 ms
        self.assertEqual(routing.room_hold_ms(house, FAST), 15000)
        self.assertEqual(routing.room_hold_ms(house, DUAL), 15000)
        self.assertEqual(routing.room_hold_ms(house, CINEMA), 20000)
        self.assertEqual(routing.room_hold_ms(house, LOUNGE), 20000)
        self.assertEqual(routing.hold_ms(house), 20000)
        table = routing.table(house)
        self.assertEqual(table["holdMs"], 20000)
        self.assertEqual(table["rooms"][FAST]["holdMs"], 15000)
        self.assertEqual(table["rooms"][CINEMA]["holdMs"], 20000)

    def test_per_field_lifecycle_adoption_without_house_flags(self):
        # Remove usb_unused & idle: standby — lifecycle still compiles for capable fields!
        data = full_house()
        house = House(data)
        self.assertTrue(routing.lifecycle_enabled(house))
        table = routing.table(house)
        self.assertIn("lifecycle", table)
        # Mixed capability: drop control from TV while keeping matrix capable -> lifecycle manages
        # matrix outputs while leaving passive/uncontrolled display outside resources.
        def passive_fast(d):
            for dev in d["devices"]:
                if dev["id"] == "example_fast_tv":
                    dev.pop("controls", None)
            d["routing"]["sources"] = {COMPUTER: d["routing"]["sources"][COMPUTER]}
            d["routing"]["rooms"] = {
                FAST: {
                    "hdmi_output": 5,
                    "sources": [COMPUTER],
                    "display": {"device": "example_fast_tv", "passive": True},
                },
            }
            d["routing"].pop("edid", None)
        mixed_house = House(full_house(passive_fast))
        mixed_table = routing.table(mixed_house)
        self.assertIn("lifecycle", mixed_table)
        self.assertIn("example_matrix_output5_input", mixed_table["lifecycle"]["resources"])
        self.assertNotIn("example_fast_tv_power", mixed_table["lifecycle"]["resources"])
        # Zero capable fields (when capabilities() returns empty) -> no lifecycle descriptor.
        with mock.patch.object(lifecycle_io, "capabilities", return_value={}):
            plain_table = routing.table(mixed_house)
            self.assertNotIn("lifecycle", plain_table)
            self.assertFalse(routing.lifecycle_enabled(mixed_house))


class TestSleepSourcesOrdering(unittest.TestCase):
    def test_sleep_sources_ordered_strictly_before_stage_power_off(self):
        house = House(full_house())
        off_path = hops.compile_room_path(house, CINEMA, routing.OFF)
        after = off_path.after_steps()
        self.assertEqual(off_path.sleep_items(), ["example_sleeper_power"])
        self.assertEqual(routing.after(house, CINEMA, routing.OFF), after)
        items = [step[0] for step in after]
        self.assertEqual(items[0], "example_sleeper_power")
        self.assertLess(items.index("example_sleeper_power"), items.index("example_screen_power"))
        self.assertLess(items.index("example_sleeper_power"), items.index("example_bar_main_power"))
        self.assertLess(items.index("example_sleeper_power"), items.index("example_avr_main_power"))
        self.assertLess(items.index("example_avr_main_power"), items.index("example_curtain_action"))
        table = routing.table(house)
        self.assertEqual(table["rooms"][CINEMA]["sleep"], ["example_sleeper_power"])
        self.assertEqual(table["rooms"][CINEMA]["after"][routing.OFF], after)


class TestPartialReadbackCrossEngineParity(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required to verify routing.js parity")
    def test_hops_python_and_routing_js_agree_on_every_state(self):
        house = House(full_house())
        table = routing.table(house)
        cases = [
            # Cinema: 3 stages (screen, bar, avr) + matrix output 1
            (CINEMA, {
                "example_screen_power": "ON", "example_screen_input": "hdmi1",
                "example_bar_main_power": "ON", "example_bar_main_input": "BD",
                "example_avr_main_power": "ON", "example_avr_main_input": "BD",
                "example_matrix_output1_input": "1",
            }, COMPUTER),
            (CINEMA, {
                "example_screen_power": "ON", "example_screen_input": "hdmi1",
                "example_bar_main_power": "OFF", "example_bar_main_input": "BD",
                "example_avr_main_power": "ON", "example_avr_main_input": "BD",
                "example_matrix_output1_input": "1",
            }, "partial"),
            (CINEMA, {
                "example_screen_power": "OFF", "example_screen_input": "hdmi1",
                "example_bar_main_power": "OFF", "example_bar_main_input": "BD",
                "example_avr_main_power": "OFF", "example_avr_main_input": "BD",
                "example_matrix_output1_input": "8",
            }, "off"),
            (CINEMA, {
                "example_screen_power": "ON", "example_screen_input": "hdmi1",
                "example_bar_main_power": "ON", "example_bar_main_input": "MPLAY",
                "example_avr_main_power": "OFF", "example_avr_main_input": "BD",
                "example_matrix_output1_input": "8",
            }, LOCAL_BAR),
            # Scaled room: split video (output 6) + audio (output 7)
            (SCALED, {
                "example_scaled_tv_power": "ON", "example_scaled_tv_input": "hdmi1",
                "example_scaled_avr_main_power": "ON", "example_scaled_avr_main_input": "BD",
                "example_matrix_output6_input": "2", "example_matrix_output7_input": "2",
            }, DISC),
            (SCALED, {
                "example_scaled_tv_power": "ON", "example_scaled_tv_input": "hdmi1",
                "example_scaled_avr_main_power": "OFF", "example_scaled_avr_main_input": "BD",
                "example_matrix_output6_input": "2", "example_matrix_output7_input": "2",
            }, "partial"),
            # Dual displays + cascaded switches
            (DUAL, {
                "example_tv_left_power": "ON", "example_tv_left_input": "hdmi1",
                "example_tv_right_power": "ON", "example_tv_right_input": "hdmi1",
                "example_vrroom_input": "0", "example_avpro_output4_input": "1",
                "example_matrix_output3_input": "1",
            }, COMPUTER),
            (DUAL, {
                "example_tv_left_power": "ON", "example_tv_left_input": "hdmi1",
                "example_tv_right_power": "OFF", "example_tv_right_input": "hdmi1",
                "example_vrroom_input": "0", "example_avpro_output4_input": "1",
                "example_matrix_output3_input": "1",
            }, "partial"),
            (DUAL, {
                "example_tv_left_power": "ON", "example_tv_left_input": "hdmi1",
                "example_tv_right_power": "ON", "example_tv_right_input": "hdmi1",
                "example_vrroom_input": "0", "example_avpro_output4_input": "2",
                "example_matrix_output3_input": "8",
            }, LOCAL_UP),
            (DUAL, {
                "example_tv_left_power": "ON", "example_tv_left_input": "hdmi1",
                "example_tv_right_power": "ON", "example_tv_right_input": "hdmi1",
                "example_vrroom_input": "1", "example_avpro_output4_input": "1",
                "example_matrix_output3_input": "8",
            }, LOCAL_DOWN),
            (DUAL, {
                "example_tv_left_power": "ON", "example_tv_left_input": "hdmi2",
                "example_tv_right_power": "OFF", "example_tv_right_input": "hdmi1",
                "example_vrroom_input": "0", "example_avpro_output4_input": "1",
                "example_matrix_output3_input": "8",
            }, LOCAL_DISP),
            (DUAL, {
                "example_tv_left_power": "ON", "example_tv_left_input": "hdmi2",
                "example_tv_right_power": "ON", "example_tv_right_input": "hdmi1",
                "example_vrroom_input": "0", "example_avpro_output4_input": "1",
                "example_matrix_output3_input": "8",
            }, "partial"),
            # Fast single-stage room
            (FAST, {
                "example_fast_tv_power": "OFF", "example_fast_tv_input": "hdmi1",
                "example_matrix_output5_input": "8",
            }, "off"),
            (FAST, {
                "example_fast_tv_power": "ON", "example_fast_tv_input": "hdmi1",
                "example_matrix_output5_input": "8",
            }, "partial"),
        ]
        for room_id, states, expected in cases:
            with self.subTest(room=room_id, expected=expected):
                py_val = hops.evaluate_readback(table["rooms"][room_id], lambda item, s=states: s.get(item))
                js_val = run_routing_js_shown(table, room_id, states)
                self.assertEqual(py_val, expected)
                self.assertEqual(js_val, expected)


if __name__ == "__main__":
    unittest.main()
