"""Zone sync: a receiver zone that takes another room's zone input while both
rooms show the same matrix source (routing `sync_with`).

A synthetic two-room house with fictional ids and documentation addresses:
the Example Cinema's receiver is the main zone of an AVR, the Example
Lounge's is its zone 2 and follows the Cinema. The JavaScript runs the
production routing.js and lifecycle planner (tests/zone_sync_harness.js).

The player is cabled to the receiver itself (MPLAY); in the `shared` house
the Lounge offers it too, on the same input of its zone, so the two rooms
share a source no matrix carries and that is never synced.
"""
import copy
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_render  # noqa: E402
from oh_features import routing  # noqa: E402
from oh_room_policy import Field, Observation, PolicyError, conflicts, expected_plan, repair_plan, verify  # noqa: E402
from oh_access.worker import NativeCache, observed_fields  # noqa: E402
from tests.test_room_policy_worker import Harness, at, policy  # noqa: E402

CINEMA, LOUNGE = "example_cinema", "example_lounge"
COMPUTER, DISC, GAME, PLAYER = "example_computer", "example_disc", "example_game", "example_player"
OUT1, OUT2, ZONE2 = "example_matrix_output1_input", "example_matrix_output2_input", "example_avr_zone2_input"
MAIN, PLAYER_POWER = "example_avr_main_input", "example_player_power"
WHERE = f"routing.rooms.{LOUNGE}.receiver.sync_with"


def device(ident, room, kind, number, **extra):
    return {"id": ident, "room": room, "kind": kind, "name": ident.replace("_", " ").title(),
            "ip": f"192.0.2.{number}", **extra}


TV = {"make": "Sony", "model": "Example TV", "controls": {"observe": ["sony_rest"], "control": ["sony_ip_control"]}}
HOUSE = {
    "version": 1,
    "rooms": {CINEMA: {"label": "Example Cinema", "tag": "Room"}, LOUNGE: {"label": "Example Lounge", "tag": "Room"}},
    "devices": [
        device("example_matrix", CINEMA, "hdmi_matrix", 20, model="HDMI matrix", controls={"control": ["bk808_http"]}),
        device("example_usb", CINEMA, "usb_matrix", 21, model="UKM-404", controls={"control": ["ukm404_http"]}),
        device("example_avr", CINEMA, "avr", 22, make="Marantz", model="CINEMA 30", zones=2,
               controls={"observe": ["denonmarantz_http"], "control": ["denonmarantz_http"]}),
        device("example_screen", CINEMA, "tv", 23, **copy.deepcopy(TV)),
        device("example_tv", LOUNGE, "tv", 24, **copy.deepcopy(TV)),
        device(PLAYER, CINEMA, "streamer", 25, make="Apple", model="Example Player"),
        device(COMPUTER, CINEMA, "computer", 26, model="Computer"),
        device(DISC, CINEMA, "disc_player", 27, make="Sony", model="Example Disc"),
        device(GAME, CINEMA, "game_console", 28, make="Sony", model="Example Game"),
    ],
    "routing": {
        "hdmi_matrix": "example_matrix", "hdmi_off_input": 8, "usb_off_host": 4, "edid": {},
        "sources": {PLAYER: {"label": "Player"}, COMPUTER: {"label": "Computer", "hdmi_input": 1, "usb_host": 1},
                    DISC: {"label": "Disc", "hdmi_input": 2}, GAME: {"label": "Game", "hdmi_input": 3}},
        "rooms": {
            # The player is cabled to the receiver alone, so it is never synced;
            # the game reaches the Cinema's receiver on GAME1 (input_by_source).
            CINEMA: {"hdmi_output": 1, "usb": {"example_usb": [1]}, "sources": [PLAYER, COMPUTER, DISC, GAME],
                     "receiver": {"device": "example_avr", "input": "BD", "sources": {PLAYER: "MPLAY"},
                                  "input_by_source": {GAME: "GAME1"}},
                     "display": {"device": "example_screen", "input": "hdmi1"}},
            # usb_unused: park turns the lifecycle planner on.
            LOUNGE: {"hdmi_output": 2, "usb": {"example_usb": [2]}, "usb_unused": "park",
                     "sources": [COMPUTER, DISC, GAME],
                     "receiver": {"device": "example_avr", "zone": 2, "input": "AUX2", "sync_with": CINEMA},
                     "display": {"device": "example_tv", "input": "hdmi2"}},
        },
    },
}


def house(change=None):
    data = copy.deepcopy(HOUSE)
    if change:
        change(data["routing"]["rooms"], data)
    return data


def shared(rooms, data, idle="standby"):
    """The Lounge offers the player too: its zone 2 selects the player's input
    (MPLAY) as the Cinema's main zone does. The player wakes over its own
    integration and, under `idle: standby`, sleeps once no room uses it."""
    rooms[LOUNGE]["sources"].insert(1, PLAYER)
    rooms[LOUNGE]["receiver"]["sources"] = {PLAYER: "MPLAY"}
    next(d for d in data["devices"] if d["id"] == PLAYER)["controls"] = {"control": ["pyatv_http"]}
    data["routing"]["sources"][PLAYER]["idle"] = idle


def rendered_table(data):
    """The TABLE routing.js is rendered with, as the runtime receives it."""
    text = oh_render.render(data)[routing.RULE]
    start = text.index("const TABLE = ") + len("const TABLE = ")
    return json.JSONDecoder().raw_decode(text, start)[0]


class Validation(unittest.TestCase):
    def problems(self, change):
        return routing.validate(house(change))

    def test_the_synthetic_house_and_the_example_are_valid(self):
        self.assertEqual(oh_render.validate(house()), [])
        self.assertEqual(oh_render.validate(json.loads((AREA / "instance/devices.yaml").read_text())), [])

    def test_each_rule_has_a_clear_problem(self):
        def swap_zones(rooms, _):
            rooms[CINEMA]["receiver"] = {"device": "example_avr", "zone": 2, "input": "SOURCE"}
            rooms[LOUNGE]["receiver"] = {"device": "example_avr", "input": "AUX2", "sync_with": CINEMA}

        def switch(rooms, data):
            data["devices"].append(device("example_switch", CINEMA, "hdmi_switch", 29, make="HDFury", model="VRROOM",
                                          controls={"control": ["vrroom_http"]}))
            rooms[CINEMA]["hdmi_switch"] = {"device": "example_switch", "matrix_input": 1, "inputs": {DISC: 2}}

        cases = {
            "leader is no room": (lambda rooms, _: rooms[LOUNGE]["receiver"].update(sync_with="example_attic"),
                                  f"{WHERE}: 'example_attic' is not another room under routing.rooms"),
            "leader is itself": (lambda rooms, _: rooms[LOUNGE]["receiver"].update(sync_with=LOUNGE),
                                 f"{WHERE}: '{LOUNGE}' is not another room under routing.rooms"),
            "no hdmi_output": (lambda rooms, _: rooms[CINEMA].pop("hdmi_output"),
                               f"{WHERE}: {LOUNGE} and {CINEMA} both need an hdmi_output"),
            "no leader receiver": (lambda rooms, _: rooms[CINEMA].pop("receiver"),
                                   f"{WHERE}: {CINEMA} needs a receiver on another zone of example_avr"),
            "same zone": (lambda rooms, _: rooms[LOUNGE]["receiver"].pop("zone"),
                          f"{WHERE}: {CINEMA} needs a receiver on another zone of example_avr"),
            "leader switch": (switch, f"{WHERE}: {CINEMA} has an hdmi_switch, so its matrix output does not decide what "
                                      "its receiver shows"),
            "split audio": (lambda rooms, _: rooms[CINEMA].update(hdmi_audio_output=3),
                            f"{WHERE}: {CINEMA} splits its audio (hdmi_audio_output), so its receiver does not show "
                            "its picture"),
            "chain": (lambda rooms, _: rooms[CINEMA]["receiver"].update(sync_with=LOUNGE),
                      f"{WHERE}: {CINEMA} syncs with '{LOUNGE}' itself; a leader cannot follow another room"),
            "display": (lambda rooms, _: rooms[LOUNGE]["display"].update(sync_with=CINEMA),
                        f"routing.rooms.{LOUNGE}.display.sync_with: only a receiver can sync with another room's"),
            "not selectable": (swap_zones, f"{WHERE}: example_avr's main zone cannot select 'SOURCE', {CINEMA}'s input "
                                           f"for {COMPUTER}"),
            "own input": (lambda rooms, _: rooms[LOUNGE]["receiver"].update(input="BD"),
                          f"{WHERE}: 'BD', {CINEMA}'s input for {COMPUTER}, is already an input of example_avr zone 2 "
                          "in this room"),
            "nothing shared": (lambda rooms, _: rooms[CINEMA].update(sources=[PLAYER]),
                               f"{WHERE}: {LOUNGE} and {CINEMA} share no source from the HDMI matrix"),
        }
        for name, (change, problem) in cases.items():
            with self.subTest(name):
                self.assertIn(problem, self.problems(change))


class Table(unittest.TestCase):
    def test_follower_names_its_synced_routes_and_leader_lists_it(self):
        table = routing.table(house())
        lounge, cinema = table["rooms"][LOUNGE], table["rooms"][CINEMA]
        synced = lambda value: {"plan": None, "value": value,  # noqa: E731
                                "inputs": [["example_tv_input", "hdmi2"], [ZONE2, value]]}
        expected = {COMPUTER: synced("BD"), DISC: synced("BD"), GAME: synced("GAME1")}
        for source in expected:
            expected[source]["plan"] = f"{source}@{CINEMA}"
        self.assertEqual(lounge["sync"], {"leader": CINEMA, "hdmi": OUT1, "input": ZONE2, "choices": expected})
        self.assertEqual(cinema["followers"], [LOUNGE])
        self.assertNotIn("sync", cinema)
        self.assertNotIn("followers", lounge)
        self.assertIn(OUT1, lounge["diagnosis"]["checks"])
        # Source choices, and every per-choice step list, keep the plain ids.
        for raw in (lounge, cinema):
            for field in ("choices", "power", "inputs", "after"):
                self.assertFalse(any("@" in choice for choice in raw[field]))
        self.assertEqual(lounge["inputs"][COMPUTER], [["example_tv_input", "hdmi2"], [ZONE2, "AUX2"]])
        # Without sync_with nothing changes: the example's output stays byte-identical.
        example = routing.table(json.loads((AREA / "instance/devices.yaml").read_text()))
        plain = routing.table(house(lambda rooms, _: rooms[LOUNGE]["receiver"].pop("sync_with")))
        for rooms in (example["rooms"], plain["rooms"]):
            self.assertFalse(any({"sync", "followers"} & set(raw) for raw in rooms.values()))


class Descriptor(unittest.TestCase):
    def setUp(self):
        self.descriptor = routing.table(house())["lifecycle"]
        self.choices = self.descriptor["endpoints"][LOUNGE]["choices"]

    def test_a_synced_choice_is_the_own_route_on_the_leader_input_with_a_lease(self):
        for source, value, lease in ((COMPUTER, "BD", "1"), (DISC, "BD", "2"), (GAME, "GAME1", "3")):
            with self.subTest(source):
                own, synced = self.choices[source], self.choices[f"{source}@{CINEMA}"]
                self.assertEqual(synced["requirements"], {**own["requirements"], ZONE2: value})
                self.assertEqual(synced["leases"], {OUT1: lease})
                self.assertEqual({key: item for key, item in synced.items() if key not in ("requirements", "leases")},
                                 {key: item for key, item in own.items() if key != "requirements"})
        self.assertNotIn(f"off@{CINEMA}", self.choices)
        self.assertNotIn(f"{PLAYER}@{CINEMA}", self.choices)
        self.assertFalse(any("leases" in route for route in self.descriptor["endpoints"][CINEMA]["choices"].values()))

    def test_the_zone_on_the_leader_input_needs_its_power_and_protects_the_source(self):
        resources = self.descriptor["resources"]
        self.assertEqual(resources[ZONE2]["requires"]["BD"], {"example_avr_zone2_power": "ON"})
        self.assertIn({"source": COMPUTER, "requirements": {OUT1: "1", "example_avr_zone2_power": "ON", ZONE2: "BD"}},
                      self.descriptor["consumers"])


class Policy(unittest.TestCase):
    def observations(self, now, zone="AUX2", leader="1", leader_age=0):
        plan = expected_plan(house(), LOUNGE, COMPUTER)
        values = {field.item: field.value for field in plan.fields}
        values[ZONE2] = zone
        out = {item: Observation(value, now) for item, value in values.items()}
        if leader is not None:
            out[OUT1] = Observation(leader, now - timedelta(seconds=leader_age))
        return out

    def test_the_follower_zone_field_accepts_the_leader_input_while_the_leader_shows_the_source(self):
        plan = expected_plan(house(), LOUNGE, COMPUTER)
        zone = next(field for field in plan.fields if field.item == ZONE2)
        self.assertEqual((zone.value, zone.alternatives), ("AUX2", (("BD", OUT1, "1"),)))
        self.assertEqual([(field.item, field.value, field.resource) for field in plan.conditions],
                         [(OUT1, "1", "device:example_matrix")])
        self.assertEqual(plan.observed()[-1], OUT1)
        self.assertNotIn(OUT1, {field.item for field in plan.fields})
        self.assertEqual(expected_plan(house(), LOUNGE, "off").conditions, ())
        self.assertFalse(any(field.alternatives for field in expected_plan(house(), CINEMA, COMPUTER).fields))
        # Conditions are observed, never compared: the leader stays free to move.
        self.assertEqual(conflicts(expected_plan(house(), CINEMA, DISC), [plan]), [])
        self.assertEqual(conflicts(plan, [expected_plan(house(), CINEMA, DISC)]), [])

    def test_verify_matches_the_alternative_only_on_a_fresh_condition(self):
        plan = expected_plan(house(), LOUNGE, COMPUTER)
        now = at(10)
        cases = [({"zone": "AUX2", "leader": None}, "verified", []),
                 ({"zone": "BD", "leader": "1"}, "verified", []),
                 ({"zone": "BD", "leader": "2"}, "degraded", [ZONE2]),
                 ({"zone": "BD", "leader": "1", "leader_age": 31}, "unknown", []),
                 ({"zone": "BD", "leader": None}, "unknown", []),
                 ({"zone": "TV", "leader": "1"}, "degraded", [ZONE2])]
        for arguments, state, mismatched in cases:
            with self.subTest(**arguments):
                result = verify(plan, self.observations(now, **arguments), now)
                self.assertEqual((result["state"], result["mismatched"]), (state, mismatched))
                self.assertEqual(repair_plan(plan, result), [(ZONE2, "AUX2")] if mismatched else [])

    def test_a_synthetic_condition_field_round_trips_through_the_native_cache(self):
        rooms = {room: {"sources": spec["sources"]} for room, spec in HOUSE["routing"]["rooms"].items()}
        fields = observed_fields(house(), rooms)
        cache = NativeCache(fields)
        self.assertIn(f"openhab/items/{OUT1}/stateupdated", cache.topics)
        event = {"topic": f"openhab/items/{OUT1}/stateupdated", "type": "ItemStateUpdatedEvent",
                 "payload": json.dumps({"value": "1"})}
        cache.accept(cache.project(event))
        self.assertEqual(cache.read((OUT1,))[OUT1].value, "1")
        self.assertIsInstance(fields[0], Field)


class LockedFollower(Harness, unittest.TestCase):
    def worker(self, zone, leader):
        plan = expected_plan(house(), LOUNGE, COMPUTER)
        observed = {field.item: field.value for field in plan.fields}
        observed.update({ZONE2: zone, OUT1: leader})
        self.make((policy(LOUNGE, hold=COMPUTER, enabled=False), at(8)), house=house(), observed=observed)
        reads = []
        read = self.devices.read
        self.devices.read = lambda items: reads.append(tuple(items)) or read(items)
        return reads

    def test_the_worker_reads_the_condition_and_repairs_the_zone_only_once_the_leader_moves(self):
        reads = self.worker("BD", "1")
        status = self.step()[LOUNGE]
        self.assertIn(OUT1, reads[0])
        self.assertEqual((status["phase"], status["state"]), ("verified", "verified"))
        self.assertEqual(self.devices.writes, [])
        self.devices.observed[OUT1] = "2"
        self.advance(10)
        status = self.step()[LOUNGE]
        self.assertEqual(self.devices.writes, [(LOUNGE, ZONE2, "AUX2")], "never the leader output itself")
        self.assertEqual(status["repair"], ZONE2)


class SharedSource(unittest.TestCase):
    """The player, cabled to the receiver, offered on both of its zones."""

    def test_the_zones_offer_it_on_the_input_it_is_cabled_to(self):
        self.assertEqual(oh_render.validate(house(shared)), [])
        where = f"routing.rooms.{LOUNGE}.receiver"
        cabled = f"(routing.rooms.{CINEMA}.receiver.sources.{PLAYER})"

        def lounge(**spec):
            return lambda rooms, data: (shared(rooms, data), rooms[LOUNGE]["receiver"].update(spec))

        def sleeps(rooms, data):
            shared(rooms, data, idle="preserve")
            rooms[CINEMA]["sleep_sources"] = [PLAYER]

        cases = {
            "another input": (lounge(sources={PLAYER: "SAT/CBL"}),
                              f"{where}.sources.{PLAYER}: example_avr has {PLAYER} on 'MPLAY' {cabled}"),
            "another source on its input": (lambda rooms, _: rooms[LOUNGE]["receiver"].update(sources={DISC: "MPLAY"}),
                                            f"{where}.sources.{DISC}: example_avr has {PLAYER} on 'MPLAY' {cabled}"),
            "the leader's signal": (lounge(sources={PLAYER: "BD"}),
                                    f"{where}.sources.{PLAYER}: example_avr has {CINEMA}'s signal on 'BD' "
                                    f"(routing.rooms.{CINEMA}.receiver.input)"),
            "its own signal": (lounge(input="MPLAY", sources={}),
                               f"{where}.input: example_avr has {PLAYER} on 'MPLAY' {cabled}"),
            "one room's sleep": (sleeps, f"routing.rooms.{CINEMA}.sleep_sources: {PLAYER!r} is shared with {LOUNGE}"),
        }
        for name, (change, problem) in cases.items():
            with self.subTest(name):
                self.assertIn(problem, routing.validate(house(change)))
        with self.assertRaises(PolicyError) as unsafe:
            expected_plan(house(sleeps), CINEMA, "off")
        self.assertEqual(unsafe.exception.code, "unsafe_shared_source_sleep")

    def test_each_room_switches_and_powers_only_its_own_zone(self):
        table = routing.table(house(shared))
        lounge = table["rooms"][LOUNGE]
        self.assertEqual(lounge["inputs"][PLAYER], [["example_tv_input", "hdmi2"], [ZONE2, "MPLAY"]])
        self.assertEqual(lounge["choices"][PLAYER], [[OUT2, 8], ["example_usb_device2_host", 4]])
        self.assertEqual(lounge["stages"][1]["readback"], {"MPLAY": PLAYER})
        self.assertNotIn(PLAYER, lounge["sync"]["choices"], "a source no matrix carries is never synced")
        descriptor = table["lifecycle"]
        zones = {CINEMA: {"example_avr_main_power": "ON", MAIN: "MPLAY"},
                 LOUNGE: {"example_avr_zone2_power": "ON", ZONE2: "MPLAY"}}
        for room, other in ((CINEMA, LOUNGE), (LOUNGE, CINEMA)):
            for choice, route in descriptor["endpoints"][room]["choices"].items():
                touched = {item for key in ("requirements", "release", "deactivate", "leases")
                           for item in route.get(key, {})}
                self.assertFalse(touched & set(zones[other]), f"{room} {choice} reaches {other}'s zone")
            requirements = descriptor["endpoints"][room]["choices"][PLAYER]["requirements"]
            self.assertEqual({item: value for item, value in requirements.items()
                              if item.startswith(("example_avr", PLAYER))}, {**zones[room], PLAYER_POWER: "ON"})
        self.assertIn({"source": PLAYER, "requirements": {"example_avr_zone2_power": "ON", ZONE2: "MPLAY"}},
                      descriptor["consumers"])
        self.assertEqual(descriptor["sources"][PLAYER], {"power": PLAYER_POWER, "idle": "standby", "capable": True})
        self.assertEqual(descriptor["resources"][PLAYER_POWER]["release"], "OFF")

    def test_a_room_held_on_it_lets_the_other_choose_it(self):
        data = house(shared)
        lounge = expected_plan(data, LOUNGE, PLAYER)
        self.assertEqual({field.item: field.value for field in lounge.fields},
                         {"example_tv_power": "ON", "example_avr_zone2_power": "ON", PLAYER_POWER: "ON",
                          "example_tv_input": "hdmi2", ZONE2: "MPLAY"})
        self.assertEqual(lounge.conditions, ())
        for held_room, other in ((CINEMA, LOUNGE), (LOUNGE, CINEMA)):
            held = expected_plan(data, held_room, PLAYER)
            # The player's own controls from the other room are refused (the
            # gateway compares resources); its routes are not.
            self.assertIn(f"device:{PLAYER}", held.resources)
            for choice in ["off", *data["routing"]["rooms"][other]["sources"]]:
                with self.subTest(held=held_room, choice=choice):
                    self.assertEqual(conflicts(expected_plan(data, other, choice), [held]), [])


@unittest.skipUnless(shutil.which("node"), "node is required for the production routing script")
class Runtime(unittest.TestCase):
    def test_break_before_make_after_the_lease_and_a_shared_source_end_to_end(self):
        tables = {"lifecycle": rendered_table(house()),
                  "plain": rendered_table(house(lambda rooms, _: rooms[LOUNGE].pop("usb_unused"))),
                  "shared": rendered_table(house(shared)),
                  "preserve": rendered_table(house(lambda rooms, data: shared(rooms, data, idle="preserve")))}
        self.assertIn("lifecycle", tables["lifecycle"])
        self.assertNotIn("lifecycle", tables["plain"])
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/zone_sync_harness.js"),
                                 str(routing.SOURCE), str(routing.SOURCE.with_name("lifecycle.js"))],
                                input=json.dumps(tables), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["passed"], report["failed"]), (19, 0))


if __name__ == "__main__":
    unittest.main()
