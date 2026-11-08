"""The room-policy enforcement engine with fake stores, devices and clocks.

Pure: no Django, network, device or background thread. The house is the
public fictional example; synthetic plans stand in where a test needs
specific shared wiring.
"""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_room_policy  # noqa: E402
from oh_room_policy import (Field, Observation, Plan, PolicyError, canonical, conflicts, desired_at,  # noqa: E402
                            due_boundaries, expected_plan, local_instant, repair_plan, verify)
from oh_room_policy.worker import BACKOFF, SETTLED, PolicyRecord, Worker  # noqa: E402
from oh_access import AccessError  # noqa: E402
from oh_access.worker import OBSERVED, NativeCache, PolicyIO, Warnings, observe  # noqa: E402

HOUSE = json.loads((AREA / "instance/devices.yaml").read_text())
WEST, EAST = "example_west", "example_east"
PLAYER, COMPUTER = "example_player", "example_computer"
MONDAY = datetime(2026, 11, 2, tzinfo=timezone.utc)  # weekday 0


def at(hour, minute=0, second=0, day=MONDAY):
    return day.replace(hour=hour, minute=minute, second=second)


def policy(room, revision=1, *, hold=None, enabled=True, locks=(), daily_off=(), zone="UTC"):
    config = {"version": 1, "room": room, "timezone": zone, "revision": revision, "enabled": enabled,
              "daily_off": list(daily_off), "locks": list(locks)}
    if hold is not None:
        config["manual_lock"] = {"desired": hold, "started_at": at(8).isoformat()}
    return config


def window(start, end, desired, ident="window", days=(0,), unlock="leave"):
    return {"id": ident, "start": start, "end": end, "days": list(days), "desired": desired, "unlock": unlock}


class Store:
    """Fake durable store: claims are once-only, and every call is logged."""

    def __init__(self, *records, log=None):
        self.records = list(records)
        self.log = log if log is not None else []
        self.claims, self.saved = {}, {}

    def set(self, *records):
        self.records = list(records)

    def policies(self):
        return [PolicyRecord(copy.deepcopy(config), changed) for config, changed in self.records]

    def current_revisions(self, rooms):
        revisions = dict.fromkeys(rooms)
        for config, _ in self.records:
            if isinstance(config, dict) and isinstance(config.get("room"), str) and config["room"] in revisions:
                revisions[config["room"]] = config.get("revision")
        return revisions

    def claim_boundary(self, room, revision, key, when):
        self.log.append(("claim", key))
        if key in self.claims:
            return False
        self.claims[key] = None
        return True

    def finish_boundary(self, room, revision, key, outcome):
        self.claims[key] = (outcome["status"], outcome["reason"])
        self.log.append(("finish", key, outcome["status"], outcome["reason"]))

    def save_reconcile(self, room, revision, state):
        self.saved[room] = (revision, copy.deepcopy(state))
        return True


class Devices:
    """Fake native I/O: fresh observations and recorded writes."""

    def __init__(self, clock, log, observed=None, sources=None):
        self.clock, self.log = clock, log
        self.observed = dict(observed or {})
        self.sources = dict(sources or {})
        self.native_power = {"example_player_power"}
        self.writes, self.fail, self.origins = [], False, []

    def read(self, items):
        return {item: Observation(self.observed[item], self.clock(),
                                  "native_power" if item in self.native_power else "native")
                for item in items if item in self.observed}

    def write(self, room, item, value, revision, origin="repair"):
        self.log.append(("write", room, item, value))
        self.origins.append(origin)
        if self.fail:
            error = TimeoutError("transport may have sent it")
            error.trail_id = "0badc0de"
            raise error
        self.writes.append((room, item, value))
        return "c0ffee00"

    def source(self, room, item):
        return self.sources[room]


class Harness:
    def make(self, *records, house=HOUSE, now=at(10, 5), observed=None, sources=None):
        self.wall, self.mono, self.log = [now], [1000.0], []
        self.store = Store(*records, log=self.log)
        self.devices = Devices(lambda: self.wall[0], self.log, observed, sources)
        self.worker = Worker(house, self.store, self.devices, wall_clock=lambda: self.wall[0], monotonic=lambda: self.mono[0])
        return self.worker

    def step(self):
        return self.worker.step()

    def advance(self, seconds):
        self.wall[0] += timedelta(seconds=seconds)
        self.mono[0] += seconds


PLAYER_ROUTE = {"example_tv_power": "ON", "example_player_power": "ON", "example_tv_input": "hdmi1"}
COMPUTER_ROUTE = {"example_tv_power": "ON", "example_matrix_output1_input": "1", "example_tv_input": "hdmi2"}


class ManualHolds(Harness, unittest.TestCase):
    def test_hold_repairs_one_known_mismatch_with_backoff_and_never_sends_source(self):
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), observed={**PLAYER_ROUTE, "example_tv_power": "OFF"})
        status = self.step()[WEST]
        self.assertEqual(self.devices.writes, [(WEST, "example_tv_power", "ON")])
        self.assertEqual((status["phase"], status["state"], status["repair"]), ("enforcing", "degraded", "example_tv_power"))
        self.assertEqual(self.store.claims, {}, "a manual hold has no boundaries to claim")
        self.step()
        self.assertEqual(len(self.devices.writes), 1, "a repair waits for its backoff")
        self.devices.observed["example_tv_power"] = "ON"
        self.advance(2)
        status = self.step()[WEST]
        self.assertEqual((status["phase"], status["state"]), ("verified", "verified"))
        self.assertEqual(self.store.saved[WEST][1]["phase"], "verified")
        self.assertFalse(any(item == "loc_example_west_source" for _, item, _ in self.devices.writes))

    def test_unknown_or_stale_observations_are_never_verified_or_repaired(self):
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), observed={"example_tv_power": "OFF"})
        self.devices.read = lambda items: {"example_tv_power": Observation("OFF", self.wall[0] - timedelta(seconds=31))}
        status = self.step()[WEST]
        self.assertEqual(status["state"], "unknown")
        self.assertEqual(self.devices.writes, [])

    def test_hold_overrides_schedule_and_disabled_schedule(self):
        hold = policy(WEST, hold=COMPUTER, locks=[window("10:00", "11:00", PLAYER)])
        self.make((hold, at(8)), observed=COMPUTER_ROUTE)
        self.assertEqual(self.step()[WEST]["phase"], "verified")
        self.assertEqual(self.store.claims, {}, "the schedule's start is not sent under a hold")


class PlanCompilation(Harness, unittest.TestCase):
    """Compiling a route costs milliseconds; the worker does it once per route."""

    def counting(self):
        calls = []

        def compile_once(house, room, choice):
            calls.append((room, choice))
            return expected_plan(house, room, choice)
        patcher = patch("oh_room_policy.worker.expected_plan", compile_once)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_each_route_is_compiled_once_across_steps_and_boundaries(self):
        calls = self.counting()
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)),
                  (policy(EAST, locks=[window("10:00", "11:00", COMPUTER)]), at(9)),
                  now=at(10, 0, 30), observed=PLAYER_ROUTE)
        for _ in range(5):
            self.step()
            self.advance(6)
        self.assertIn((EAST, "loc_example_east_source", COMPUTER), self.devices.writes, "the boundary was sent")
        self.assertEqual(self.store.saved[WEST][1]["phase"], "verified")
        self.assertIn((WEST, PLAYER), calls)
        self.assertIn((EAST, COMPUTER), calls)
        self.assertEqual(len(calls), len(set(calls)), calls)

    def test_an_io_with_compiled_plans_lends_them(self):
        calls = self.counting()
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), observed=PLAYER_ROUTE)
        self.devices.plans = {room: {choice: expected_plan(HOUSE, room, choice)
                                     for choice in ("off", *spec["sources"])}
                              for room, spec in HOUSE["routing"]["rooms"].items()}
        calls.clear()
        for _ in range(3):
            self.step()
            self.advance(6)
        self.assertEqual(self.store.saved[WEST][1]["phase"], "verified")
        self.assertEqual(calls, [])


def trail(logs):
    return [record.getMessage() for record in logs.records]


class Trail(Harness, unittest.TestCase):
    """One INFO line per boundary outcome, repair write and phase transition;
    nothing on the status heartbeat."""

    def test_repairs_and_phase_transitions_are_logged_once_each(self):
        self.make((policy(WEST, revision=4, hold=PLAYER, enabled=False), at(8)),
                  observed={**PLAYER_ROUTE, "example_tv_power": "OFF"})
        with self.assertLogs("oh_room_policy.worker", "INFO") as logs:
            self.step()
        self.assertEqual(trail(logs), [
            "repair room=example_west item=example_tv_power value=ON revision=4 cmd=c0ffee00 outcome=dispatched attempt=1",
            "phase room=example_west revision=4 previous=- phase=enforcing state=degraded reason=- desired=example_player"])
        self.assertEqual(self.devices.origins, ["repair"])
        self.devices.observed["example_tv_power"] = "ON"
        self.advance(2)
        with self.assertLogs("oh_room_policy.worker", "INFO") as logs:
            self.step()
        self.assertEqual(trail(logs), ["phase room=example_west revision=4 previous=enforcing phase=verified "
                                       "state=verified reason=- desired=example_player"])
        # The 10 s heartbeat republishes the same status without a line.
        with self.assertNoLogs("oh_room_policy.worker", "INFO"):
            for _ in range(4):
                self.advance(6)
                self.step()
        self.assertEqual(self.store.saved[WEST][1]["phase"], "verified")

    def test_boundary_outcomes_name_their_key_status_reason_and_command(self):
        config = policy(WEST, locks=[window("10:00", "11:00", COMPUTER)])
        self.make((config, at(9)), now=at(10, 0, 30))
        self.devices.fail = True
        with self.assertLogs("oh_room_policy.worker", "INFO") as logs:
            self.step()
        key = f"{WEST}:1:window:2026-11-02:start"
        self.assertEqual(trail(logs)[0], f"boundary key={key} room=example_west revision=1 event=start "
                                         "target=example_computer status=uncertain reason=dispatch_unconfirmed cmd=0badc0de")
        self.assertEqual(self.devices.origins, ["schedule"])
        self.make((config, at(9)), now=at(12))
        with self.assertLogs("oh_room_policy.worker", "INFO") as logs:
            self.step()
        self.assertIn("status=skipped reason=missed cmd=-", trail(logs)[0])

    def test_policy_io_writes_carry_their_trail_id_to_openhab(self):
        adapter = Adapter()
        io = PolicyIO(adapter, Revisions(3), None, HOUSE)
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            repair = io.write(WEST, "example_tv_power", "ON", 3)
            scheduled = io.write(WEST, "loc_example_west_source", COMPUTER, 3, origin="schedule")
        self.assertEqual(adapter.transport.sources, ["thermite.repair:" + repair, "thermite.schedule:" + scheduled])
        self.assertEqual(trail(logs)[0].split(" ms=")[0],
                         f"cmd id={repair} origin=repair client=- user=- principal=worker token=- peer=- req=- "
                         "room=example_west action=field item=example_tv_power value=ON outcome=accepted")
        self.assertIn(" action=source item=loc_example_west_source value=example_computer outcome=accepted", trail(logs)[1])

        class Refused(Exception):
            status = 503
        adapter.transport.command = lambda item, value, source=None: (_ for _ in ()).throw(Refused("private body"))
        with self.assertLogs("oh_identity.trail", "INFO") as logs, self.assertRaises(Refused) as raised:
            io.write(WEST, "example_tv_power", "ON", 3)
        self.assertIn(f"id={raised.exception.trail_id} origin=repair", trail(logs)[0])
        self.assertIn("outcome=runtime_unavailable(http=503)", trail(logs)[0])
        self.assertNotIn("private body", trail(logs)[0])


class Boundaries(Harness, unittest.TestCase):
    def test_unlock_never_replays_an_earlier_daily_off(self):
        daily = [{"id": "night", "time": "10:00", "days": [0]}]
        self.make((policy(WEST, revision=3, daily_off=daily), at(10, 4)), now=at(10, 5))
        self.assertEqual(self.step()[WEST]["phase"], "unlocked")
        self.assertEqual((self.store.claims, self.devices.writes), ({}, []))
        # The same rule before the revision changed was due and is sent once.
        self.make((policy(WEST, revision=3, daily_off=daily), at(9)), now=at(10, 5))
        self.step()
        self.assertEqual(self.devices.writes, [(WEST, "loc_example_west_source", "off")])
        self.step()
        self.assertEqual(len(self.devices.writes), 1)

    def test_claim_precedes_send_and_an_existing_claim_sends_nothing(self):
        config = policy(WEST, locks=[window("10:00", "11:00", COMPUTER)])
        self.make((config, at(9)), now=at(10, 0, 30))
        self.step()
        key = f"{WEST}:1:window:2026-11-02:start"
        self.assertEqual(self.log[:2], [("claim", key), ("write", WEST, "loc_example_west_source", COMPUTER)])
        self.assertEqual(self.store.claims[key], ("applied", "dispatched"))
        # A restarted worker sharing the durable claims sends nothing again.
        claims = self.store.claims
        self.make((config, at(9)), now=at(10, 1))
        self.store.claims = claims
        self.step()
        self.assertEqual(self.devices.writes, [])

    def test_uncertain_dispatch_is_recorded_and_never_replayed(self):
        config = policy(WEST, locks=[window("10:00", "11:00", COMPUTER)])
        self.make((config, at(9)), now=at(10, 0, 30))
        self.devices.fail = True
        self.step()
        key = f"{WEST}:1:window:2026-11-02:start"
        self.assertEqual(self.store.claims[key], ("uncertain", "dispatch_unconfirmed"))
        self.devices.fail = False
        self.advance(60)
        self.step()
        self.assertFalse(any(entry[0] == "write" and entry[2] == "loc_example_west_source" for entry in self.log[2:]))

    def test_unlock_into_an_active_window_catches_up_once_unless_the_source_is_current(self):
        config = policy(WEST, revision=4, locks=[window("09:00", "11:00", COMPUTER)])
        self.make((config, at(10, 4)), now=at(10, 5), sources={WEST: PLAYER})
        self.step()
        key = f"{WEST}:4:window:2026-11-02:start"
        self.assertIn((WEST, "loc_example_west_source", COMPUTER), self.devices.writes)
        self.assertEqual(self.store.claims[key], ("applied", "dispatched"))
        before = len(self.devices.writes)
        self.advance(30)
        self.step()
        self.assertEqual(self.log.count(("claim", key)), 1, "one catch-up per revision")
        self.assertFalse(any(item == "loc_example_west_source" for _, item, _ in self.devices.writes[before:]))
        # An edit mid-window catches up again, but a current source is not re-sent.
        self.devices.sources[WEST] = COMPUTER
        self.store.set((policy(WEST, revision=5, locks=[window("09:00", "11:00", COMPUTER)]), self.wall[0]))
        self.advance(1)
        self.step()
        key = f"{WEST}:5:window:2026-11-02:start"
        self.assertEqual(self.store.claims[key], ("skipped", "already_current"))
        self.assertFalse(any(item == "loc_example_west_source" for _, item, _ in self.devices.writes[before:]))

    def test_catch_up_is_sent_when_current_source_is_unreadable(self):
        config = policy(WEST, revision=2, locks=[window("09:00", "11:00", COMPUTER)])
        self.make((config, at(10, 4)), now=at(10, 5))  # Devices.source raises KeyError.
        self.step()
        self.assertIn((WEST, "loc_example_west_source", COMPUTER), self.devices.writes)

    def test_scheduled_start_on_time_is_sent_even_when_already_current(self):
        config = policy(WEST, locks=[window("10:00", "11:00", COMPUTER)])
        self.make((config, at(9)), now=at(10, 0, 30), sources={WEST: COMPUTER})
        self.step()
        self.assertIn((WEST, "loc_example_west_source", COMPUTER), self.devices.writes)


class DueBoundaries(unittest.TestCase):
    def test_catch_up_targets_only_the_active_occurrence_at_the_revision_start(self):
        config = oh_room_policy.validate(policy(WEST, revision=7, locks=[window("09:00", "11:00", COMPUTER)],
                                                daily_off=[{"id": "early", "time": "09:30", "days": [0]}]),
                                         WEST, {"rooms": {WEST: {"sources": [PLAYER, COMPUTER]}}})
        events = due_boundaries(at(10, 5), config, not_before=at(10, 4))
        self.assertEqual([(event.kind, event.at, event.catch_up, event.target) for event in events],
                         [("start", at(10, 4), True, COMPUTER)])
        self.assertEqual(due_boundaries(at(10, 5), config, {events[0].key}, not_before=at(10, 4)), [])
        events = due_boundaries(at(9, 0, 10), config, not_before=at(8))
        self.assertEqual([(event.kind, event.at, event.catch_up) for event in events], [("start", at(9), False)])
        self.assertEqual(due_boundaries(at(10, 5), {**config, "enabled": False}, not_before=at(10, 4)), [])

    def test_missed_and_coalesced_actions_are_reported_not_sent(self):
        contract = {"rooms": {WEST: {"sources": [PLAYER, COMPUTER]}}}
        config = oh_room_policy.validate(policy(WEST, daily_off=[{"id": "night", "time": "09:00", "days": [0]}],
                                                locks=[window("09:30", "11:00", PLAYER, unlock="off")]), WEST, contract)
        outcomes = {event.kind: event.outcome for event in due_boundaries(at(10), config, not_before=at(8))}
        self.assertEqual(outcomes, {"daily_off": "missed", "start": "due"})
        late = {event.kind: event.outcome for event in due_boundaries(at(11, 5), config, not_before=at(8))}
        self.assertEqual(late["unlock"], "due")
        self.assertEqual({event.kind: event.outcome for event in due_boundaries(at(11, 30), config, not_before=at(8))}["unlock"], "missed")


class DaylightSaving(unittest.TestCase):
    ZONE = "America/Chicago"

    def test_gap_advances_and_fold_uses_the_first_occurrence(self):
        self.assertEqual(local_instant(datetime(2026, 3, 8).date(), "02:30", self.ZONE), datetime(2026, 3, 8, 8, tzinfo=timezone.utc))
        self.assertEqual(local_instant(datetime(2026, 11, 1).date(), "01:30", self.ZONE), datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))

    def test_windows_span_a_fold_and_its_daily_off_fires_once(self):
        contract = {"rooms": {WEST: {"sources": [PLAYER]}}}
        config = oh_room_policy.validate(policy(WEST, zone=self.ZONE, locks=[window("01:00", "02:00", PLAYER, days=[6])],
                                                daily_off=[{"id": "night", "time": "01:30", "days": [6]}]), WEST, contract)
        fold = datetime(2026, 11, 1, tzinfo=timezone.utc)
        # 01:00 CDT is 06:00Z; 02:00 CST is 08:00Z: the window lasts two hours.
        self.assertTrue(desired_at(fold.replace(hour=7, minute=30), config)["locked"])
        self.assertFalse(desired_at(fold.replace(hour=8), config)["locked"])
        first = [event for event in due_boundaries(fold.replace(hour=6, minute=31), config, not_before=fold) if event.kind == "daily_off"]
        self.assertEqual(len(first), 1)
        second = due_boundaries(fold.replace(hour=7, minute=31), config, {first[0].key}, not_before=fold)
        self.assertFalse([event for event in second if event.kind == "daily_off"])

    def test_daily_off_in_a_gap_fires_at_the_first_valid_minute(self):
        contract = {"rooms": {WEST: {"sources": [PLAYER]}}}
        config = oh_room_policy.validate(policy(WEST, zone=self.ZONE, daily_off=[{"id": "night", "time": "02:30", "days": [6]}]),
                                         WEST, contract)
        spring = datetime(2026, 3, 8, tzinfo=timezone.utc)
        self.assertEqual(due_boundaries(spring.replace(hour=7, minute=59), config, not_before=spring), [])
        events = due_boundaries(spring.replace(hour=8, minute=1), config, not_before=spring)
        self.assertEqual([(event.kind, event.at) for event in events], [("daily_off", spring.replace(hour=8))])


def synthetic(table):
    """expected_plan replacement: room -> choice -> {item: (value, device)}."""
    def plan(house, room, choice):
        if room not in table or choice not in table[room]:
            raise PolicyError("invalid_room_source")
        fields = tuple(Field(item, value, "device:" + device) for item, (value, device) in table[room][choice].items())
        return Plan(room, choice, room + "_source", fields, tuple(sorted({field.resource for field in fields})),
                    tuple((field.item, field.value) for field in fields))
    return plan


SYNTHETIC_HOUSE = {"routing": {"rooms": {"west": {"sources": ["a", "b"]}, "east": {"sources": ["a", "b"]},
                                         "north": {"sources": ["a"]}}}}
SHARED = {"west": {"off": {"amp_input": ("0", "amp")}, "a": {"amp_input": ("1", "amp")}, "b": {"amp_input": ("2", "amp")}},
          "east": {"off": {"amp_input": ("0", "amp")}, "a": {"amp_input": ("2", "amp")}, "b": {"amp_input": ("1", "amp")}},
          "north": {"off": {"north_power": ("OFF", "north_tv")}, "a": {"north_power": ("ON", "north_tv")}}}


class SharedResources(Harness, unittest.TestCase):
    def setUp(self):
        patcher = patch("oh_room_policy.worker.expected_plan", synthetic(SHARED))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_conflicting_holds_are_degraded_and_never_repaired(self):
        self.make((policy("west", hold="a", enabled=False), at(8)), (policy("east", hold="a", enabled=False), at(8)),
                  house=SYNTHETIC_HOUSE, observed={"amp_input": "0"})
        result = self.step()
        for room in ("west", "east"):
            self.assertEqual((result[room]["state"], result[room]["reason"]), ("degraded", "shared_resource_conflict"))
        self.assertEqual(self.devices.writes, [])

    def test_a_start_whose_effects_conflict_with_a_protected_room_is_skipped(self):
        self.make((policy("west", hold="a", enabled=False), at(8)),
                  (policy("east", locks=[window("10:00", "11:00", "a")]), at(9)), house=SYNTHETIC_HOUSE, now=at(10, 0, 30))
        self.step()
        self.assertEqual(self.store.claims["east:1:window:2026-11-02:start"], ("skipped", "shared_resource_conflict"))
        self.assertNotIn(("east", "east_source", "a"), self.devices.writes)

    def test_pure_conflicts_compare_fields_or_boundary_effects(self):
        plan = synthetic(SHARED)
        west, east, north = plan(None, "west", "a"), plan(None, "east", "a"), plan(None, "north", "a")
        self.assertEqual(conflicts(west, [east, north]), ["east"])
        self.assertEqual(conflicts(west, [plan(None, "east", "b"), north], boundary=True), [])
        self.assertEqual(conflicts(west, [west]), [])


AVR, ZONE2, PROJECTOR = "avr_power", "avr_zone2_power", "projector_power"
CINEMA = {"cinema": {"off": {AVR: ("OFF", "avr"), ZONE2: ("OFF", "avr"), PROJECTOR: ("OFF", "projector")},
                     "tv": {AVR: ("ON", "avr"), ZONE2: ("OFF", "avr"), PROJECTOR: ("ON", "projector")}}}
CINEMA_HOUSE = {"routing": {"rooms": {"cinema": {"sources": ["tv"]}}}}
FRIDAY = MONDAY + timedelta(days=4)


class Reported:
    """Native I/O through the real NativeCache, fed only when a device
    changes, as the denonmarantz binding and the sony_adcp rule report it, so
    each reading ages from its device's last change. A report without a
    value is a poll: the device reports its state, changed or not. A device
    obeys a repair two seconds later unless it is deaf; one in `remote` is
    turned back on that many seconds after it goes off. Every write asserts
    that it acts on a fresh, mismatched reading."""

    def __init__(self, test, clock, notify):
        self.test, self.clock = test, clock
        self.cache = NativeCache([Field(item, "OFF", "device:" + device) for item, (_, device) in CINEMA["cinema"]["off"].items()],
                                 clock=clock, on_change=lambda item: notify("cinema"))
        self.state, self.due, self.writes = {}, [], []
        self.deaf, self.remote, self.broken = set(), {}, False

    def report(self, item, value=None):
        if value is None or self.state.get(item) != value:
            self.state[item] = self.state.get(item) if value is None else value
            self.cache.accept((item, self.state[item]))
            if self.state[item] == "OFF" and item in self.remote:
                self.due.append((self.clock() + timedelta(seconds=self.remote[item]), item, "ON"))

    def read(self, items):
        if self.broken:
            raise OSError("native feed unavailable")
        return self.cache.read(items)

    def write(self, room, item, value, revision, origin="repair"):
        reading = self.cache.read((item,))[item]
        self.test.assertLessEqual((self.clock() - reading.observed_at).total_seconds(), 30, "repaired on a stale reading")
        self.test.assertNotEqual(reading.value, value)
        self.writes.append((self.clock(), item))
        if item not in self.deaf:
            self.due.append((self.clock() + timedelta(seconds=2), item, value))
        return "c0ffee00"


class QuietDevices(unittest.TestCase):
    """A room locked Off whose devices report only when they change: it is
    Unknown 30 s after each report, and each fresh mismatch is repaired."""

    def setUp(self):
        patcher = patch("oh_room_policy.worker.expected_plan", synthetic(CINEMA))
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, now):
        self.wall, self.mono = [now], [1000.0]
        self.store = Store((policy("cinema", hold="off", enabled=False), now))
        self.io = Reported(self, lambda: self.wall[0], lambda room: self.worker.notify(room))
        self.worker = Worker(CINEMA_HOUSE, self.store, self.io, wall_clock=lambda: self.wall[0],
                             monotonic=lambda: self.mono[0])
        self.statuses = []

    def run_until(self, until, *script):
        """The supervisor: a pass, then a sleep until the worker is due or the
        next device report, which wakes it as the native feed does."""
        self.io.due.extend(script)
        while self.wall[0] < until:
            result = self.worker.step()
            if "cinema" in result:  # Else the room was not due for a read.
                self.statuses.append(result["cinema"])
            wake = min(until, self.wall[0] + timedelta(seconds=self.worker.next_delay(5.0)))
            self.io.due.sort(key=lambda report: report[0])
            when = self.io.due[0][0] if self.io.due else None
            seconds = ((when if when is not None and when <= wake else wake) - self.wall[0]).total_seconds()
            self.wall[0] += timedelta(seconds=max(0.0, seconds))
            self.mono[0] += max(0.0, seconds)
            if when is not None and when <= wake:
                self.io.report(*self.io.due.pop(0)[1:])

    def writes(self, item=None):
        return [(when, written) for when, written in self.io.writes if item in (None, written)]

    def test_every_wait_ends_while_the_mismatch_it_waits_on_is_fresh(self):
        # The longest step, after another Item's repair in the room.
        self.assertLess(max(BACKOFF) + BACKOFF[0], Field(AVR, "OFF", "device:avr").max_age)
        self.assertGreater(SETTLED, max(BACKOFF))

    def test_a_day_of_devices_turning_themselves_on_is_repaired_each_time(self):
        """A day as one house lived it: each device turned itself on again,
        and only a change reports. Unknown passes used to push the next write
        out to 60 s, by which time the mismatch was stale too, so nothing was
        sent: the receiver stayed on from 11:35:36, and both from 16:12."""
        def friday(hour, minute, second=0):
            return at(hour, minute, second, day=FRIDAY)
        turned_on = [(friday(11, 29, 21), AVR), (friday(11, 29, 45), PROJECTOR), (friday(11, 31, 46), PROJECTOR),
                     (friday(11, 32, 33), PROJECTOR), (friday(11, 33, 21), AVR), (friday(11, 33, 47), PROJECTOR),
                     (friday(11, 33, 51), AVR), (friday(11, 34, 51), AVR), (friday(11, 35, 36), AVR),
                     (friday(16, 12, 22), AVR), (friday(16, 12, 45), PROJECTOR)]
        # Changes the devices made themselves; a repair's OFF follows it.
        turned_off = [(friday(11, 32, 45), PROJECTOR), (friday(11, 33, 6), AVR), (friday(12, 12, 54), AVR),
                      (friday(12, 13), PROJECTOR)]
        self.start(friday(8, 0))
        for item in (AVR, ZONE2, PROJECTOR):
            self.io.report(item, "OFF")
        self.run_until(friday(16, 30), *[(when, item, "ON") for when, item in turned_on],
                       *[(when, item, "OFF") for when, item in turned_off])
        self.assertEqual(self.writes(), turned_on)
        self.assertEqual(self.io.state, {AVR: "OFF", ZONE2: "OFF", PROJECTOR: "OFF"})
        status = self.statuses[-1]
        self.assertEqual((status["phase"], status["state"], status["unknown"]),
                         ("enforcing", "unknown", [AVR, ZONE2, PROJECTOR]))

    def test_a_remote_that_undoes_every_repair_is_backed_off_per_item(self):
        self.start(at(10))
        self.io.report(ZONE2, "OFF")
        self.io.report(PROJECTOR, "OFF")
        self.io.remote[AVR] = 1
        # Zone 2 and the projector are polled, so the room verifies between repairs.
        polls = [(at(10) + timedelta(seconds=10 * n), item, None) for n in range(1, 64) for item in (ZONE2, PROJECTOR)]
        with self.assertLogs("oh_room_policy.worker", "INFO") as logs:
            self.run_until(at(10, 5), (at(10, 0, 1), AVR, "ON"), (at(10, 1, 35), ZONE2, "ON"),
                           (at(10, 2, 15), PROJECTOR, "ON"), *polls)
            self.io.remote.clear()
            self.run_until(at(10, 10, 30), (at(10, 10), AVR, "ON"))
        lines = trail(logs)
        receiver = [line for line in lines if line.startswith("repair room=cinema item=avr_power")]
        times = [when for when, _ in self.writes(AVR) if when < at(10, 5)]
        gaps = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:])]
        # The receiver is back on 3 s after each repair: the backoff, not
        # the remote, sets the pace, and it stays at its longest step.
        self.assertEqual(gaps, [max(3, BACKOFF[min(n, len(BACKOFF) - 1)]) for n in range(len(gaps))])
        self.assertLessEqual(len(times), 20)
        self.assertTrue(any("phase=verified" in line for line in lines), "the room verified between repairs")
        # Neither the receiver's other zone nor the projector waits for the
        # contested Item's backoff: each first repair is sent at once.
        self.assertIn((at(10, 1, 35), ZONE2), self.writes())
        self.assertIn((at(10, 2, 15), PROJECTOR), self.writes())
        for item in (ZONE2, PROJECTOR):
            self.assertIn(f"repair room=cinema item={item} value=OFF revision=1 cmd=c0ffee00 "
                          "outcome=dispatched attempt=1", lines)
        everything = [when for when, _ in self.writes()]
        self.assertGreaterEqual(min((b - a).total_seconds() for a, b in zip(everything, everything[1:])), BACKOFF[0])
        # Once the receiver has needed no repair for SETTLED, it starts over.
        self.assertEqual(self.writes(AVR)[-1], (at(10, 10), AVR))
        self.assertEqual(receiver[len(times) - 1].rsplit(" ", 1)[1], f"attempt={len(times)}")
        self.assertEqual(receiver[-1].rsplit(" ", 1)[1], "attempt=1")

    def test_a_mismatch_after_a_quiet_spell_is_repaired_at_once(self):
        self.start(at(8))
        self.io.report(AVR, "OFF")
        self.io.report(PROJECTOR, "OFF")
        self.run_until(at(9))
        self.io.cache.clear()  # A reconnected feed: no reading at all.
        self.run_until(at(10))
        self.io.broken = True
        self.run_until(at(11))
        self.assertEqual(self.statuses[-1]["reason"], "native_read_unavailable")
        self.io.broken = False
        with self.assertLogs("oh_room_policy.worker", "INFO") as logs:
            self.run_until(at(13, 40), (at(12), AVR, "ON"), (at(13), PROJECTOR, "ON"), (at(13, 30), AVR, "ON"))
        self.assertEqual(self.writes(), [(at(12), AVR), (at(13), PROJECTOR), (at(13, 30), AVR)])
        # Hours of Unknown passes, wake-ups and failed reads added no backoff.
        self.assertEqual([line.rsplit(" ", 1)[1] for line in trail(logs) if line.startswith("repair")], ["attempt=1"] * 3)
        self.assertEqual(self.statuses[-1]["retry_seconds"], 0)

    def test_a_stale_reading_is_never_verified_or_repaired(self):
        self.start(at(10))
        self.io.deaf.add(AVR)  # It ignores repairs and reports nothing.
        self.io.report(AVR, "ON")
        self.io.report(PROJECTOR, "OFF")
        self.run_until(at(14))
        # Repairs stop once the reading is older than Field.max_age.
        self.assertEqual(self.writes(), [(at(10) + timedelta(seconds=second), AVR) for second in (0, 2, 7, 17)])
        self.assertNotIn("verified", {status["phase"] for status in self.statuses})
        self.assertEqual((self.statuses[-1]["state"], self.statuses[-1]["unknown"]), ("unknown", [AVR, ZONE2, PROJECTOR]))
        # A worker started while the receiver is on, with no report since,
        # sends nothing until a device reports.
        self.start(at(14))
        self.io.state = {AVR: "ON", PROJECTOR: "ON"}
        self.run_until(at(15), (at(14, 30), PROJECTOR, None))
        self.assertEqual(self.writes(), [(at(14, 30), PROJECTOR)])
        self.assertNotIn("verified", {status["phase"] for status in self.statuses})


class UnreadablePolicies(Harness, unittest.TestCase):
    def test_a_row_for_a_room_removed_from_the_house_blocks_nothing(self):
        orphan = policy("attic", hold="anything")
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), (orphan, at(8)),
                  observed={**PLAYER_ROUTE, "example_tv_power": "OFF"})
        result = self.step()
        self.assertEqual(result["attic"], {"phase": "ignored", "reason": "unknown_room"})
        self.assertNotIn("attic", self.store.saved)
        self.assertEqual(self.devices.writes, [(WEST, "example_tv_power", "ON")])

    def test_an_invalid_row_blocks_only_rooms_sharing_its_hardware(self):
        retired = policy(EAST, hold="retired_source")
        # The example rooms share one HDMI matrix, so west cannot be enforced.
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), (retired, at(8)),
                  observed={**PLAYER_ROUTE, "example_tv_power": "OFF"})
        result = self.step()
        self.assertEqual(result[EAST], {"phase": "invalid", "reason": "invalid_policy"})
        self.assertEqual((result[WEST]["state"], result[WEST]["reason"]), ("degraded", "invalid_policy"))
        self.assertEqual(self.devices.writes, [])
        with patch("oh_room_policy.worker.expected_plan", synthetic(SHARED)):
            self.make((policy("north", hold="a", enabled=False), at(8)), (policy("east", hold="retired"), at(8)),
                      (policy("west", hold="a", enabled=False), at(8)), house=SYNTHETIC_HOUSE,
                      observed={"north_power": "OFF", "amp_input": "0"})
            result = self.step()
        self.assertEqual(result["west"]["reason"], "invalid_policy")
        self.assertEqual(self.devices.writes, [("north", "north_power", "ON")])

    def test_a_disabled_schedule_without_a_hold_is_inert_even_when_invalid(self):
        stale = policy(EAST, enabled=False, locks=[window("10:00", "11:00", "retired_source")])
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), (stale, at(8)),
                  observed={**PLAYER_ROUTE, "example_tv_power": "OFF"})
        result = self.step()
        self.assertEqual(result[EAST], {"phase": "unlocked", "desired": None})
        self.assertEqual(self.devices.writes, [(WEST, "example_tv_power", "ON")])

    def test_malformed_rows_are_reported_not_raised(self):
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)), (["not", "a", "policy"], at(8)),
                  ({"room": ["unhashable"]}, at(8)), observed=PLAYER_ROUTE)
        self.assertEqual(self.step()[WEST]["phase"], "verified")


class RouteVerification(unittest.TestCase):
    def test_native_power_needs_provider_time_and_repairs_exclude_source_and_edid(self):
        plan = expected_plan(HOUSE, WEST, PLAYER)
        now = at(10)
        observations = {"example_tv_power": Observation("ON", now), "example_tv_input": Observation("hdmi2", now),
                        "example_player_power": Observation("ON", now, basis="native")}
        result = verify(plan, observations, now)
        self.assertEqual((result["state"], result["mismatched"], result["unknown"]),
                         ("degraded", ["example_tv_input"], ["example_player_power"]))
        self.assertEqual(repair_plan(plan, result), [("example_tv_input", "hdmi1")])
        self.assertNotIn(plan.source_item, {item for item, _ in repair_plan(plan, {"mismatched": [plan.source_item]})})
        observations["example_player_power"] = Observation("ON", now, basis="native_power")
        observations["example_tv_input"] = Observation("hdmi1", now - timedelta(seconds=10))
        self.assertEqual(verify(plan, observations, now)["state"], "verified")
        self.assertEqual(verify(plan, observations, now + timedelta(seconds=31))["state"], "unknown")

    def test_a_value_no_route_uses_is_a_mismatch_and_only_the_target_is_written(self):
        plan = expected_plan(HOUSE, WEST, PLAYER)
        now = at(10)
        observations = {item: Observation(value, now, basis="native_power" if item == "example_player_power" else "native")
                        for item, value in PLAYER_ROUTE.items()}
        # A Sony TV on its home screen or an app reports `tv`.
        for value in ("tv", "hdmi9", "HDMI1", " hdmi1"):
            with self.subTest(value=value):
                result = verify(plan, {**observations, "example_tv_input": Observation(value, now)}, now)
                self.assertEqual((result["state"], result["mismatched"]), ("degraded", ["example_tv_input"]))
                self.assertEqual(repair_plan(plan, result), [("example_tv_input", "hdmi1")])
        for value in ("NULL", "UNDEF", "", "x" * 129, None, 1, ["hdmi1"]):
            with self.subTest(value=value):
                result = verify(plan, {**observations, "example_tv_input": Observation(value, now)}, now)
                self.assertEqual((result["state"], result["unknown"], result["mismatched"]),
                                 ("unknown", ["example_tv_input"], []))
                self.assertEqual(repair_plan(plan, result), [])

    def test_a_number_item_matches_by_value(self):
        plan = expected_plan(HOUSE, WEST, COMPUTER)
        now = at(10)
        observed = {item: Observation(value, now) for item, value in COMPUTER_ROUTE.items()}
        for value, state in (("1", "verified"), ("1.0", "verified"), ("01.00", "verified"), ("1.5", "degraded"),
                             ("2", "degraded"), ("-1", "degraded"), ("0", "degraded")):
            with self.subTest(value=value):
                result = verify(plan, {**observed, "example_matrix_output1_input": Observation(value, now)}, now)
                self.assertEqual(result["state"], state)
        self.assertEqual([canonical(value) for value in ("3.0", "3", "03", "-0.0", "-2.50", "10", "3e0", "3.")],
                         ["3", "3", "3", "0", "-2.5", "10", "3e0", "3."])


class Cache(unittest.TestCase):
    def fields(self):
        return [field for choice in ("off", PLAYER, COMPUTER) for field in expected_plan(HOUSE, WEST, choice).fields]

    def event(self, item, value, kind="ItemStateUpdatedEvent"):
        return {"topic": f"openhab/items/{item}/stateupdated", "type": kind, "payload": json.dumps({"value": value})}

    def test_projection_keeps_every_field_value_as_verify_reads_it_and_valid_timestamps(self):
        cache = NativeCache(self.fields(), native_power={"example_player_power": "example_player_reported_at"})
        self.assertIn("openhab/items/example_player_reported_at/stateupdated", cache.topics)
        for value, kept in (("hdmi1", "hdmi1"), ("tv", "tv"), ("hdmi9", "hdmi9"), ("NULL", None), ("UNDEF", None),
                            ("", None), ("x" * 129, None), (3, None), (None, None)):
            with self.subTest(value=value):
                self.assertEqual(cache.project(self.event("example_tv_input", value)), ("example_tv_input", kept))
        self.assertEqual(cache.project(self.event("example_matrix_output1_input", "1.0")), ("example_matrix_output1_input", "1"))
        for event in (self.event("example_tv_input", "hdmi1", "ItemStateChangedEvent"), self.event("unrelated_item", "ON"),
                      self.event("example_player_reported_at", "yesterday"), {**self.event("example_tv_power", "ON"), "payload": "{"},
                      {**self.event("example_tv_power", "ON"), "payload": "[]"}):
            self.assertIsNone(cache.project(event))
        self.assertIsNotNone(cache.project(self.event("example_player_reported_at", "2026-11-02T10:00:00Z")))

    def test_native_power_consumes_its_provider_timestamp_once(self):
        changed = []
        clock = [at(10)]
        cache = NativeCache(self.fields(), native_power={"example_player_power": "example_player_reported_at"},
                            clock=lambda: clock[0], on_change=changed.append)
        cache.accept(("example_player_power", "ON"))
        self.assertEqual(cache.read(("example_player_power",)), {})
        cache.accept(("example_player_reported_at", "2026-11-02T09:59:58Z"))
        cache.accept(("example_player_power", "ON"))
        reading = cache.read(("example_player_power",))["example_player_power"]
        self.assertEqual((reading.observed_at, reading.basis), (at(9, 59, 58), "native_power"))
        cache.accept(("example_player_power", "ON"))
        self.assertEqual(cache.read(("example_player_power",)), {}, "a timestamp is never reused")
        cache.accept(("example_tv_power", "ON"))
        cache.accept(("example_tv_power", "ON"))
        self.assertEqual(changed.count("example_tv_power"), 1, "same-value reports do not wake the loop")
        cache.clear()
        self.assertEqual(cache.read(("example_tv_power",)), {})
        self.assertEqual(changed.count("example_tv_power"), 2)


class OutsideTheRoute(Harness, unittest.TestCase):
    """A room held on the player: its TV goes to its home screen, which no
    route uses, and the lock switches it back. Through the native cache, as
    the worker service reads it."""

    def report(self, item, value):
        frame = self.cache.project({"topic": f"openhab/items/{item}/stateupdated", "type": "ItemStateUpdatedEvent",
                                    "payload": json.dumps({"type": "String", "value": value})})
        self.cache.accept(frame)

    def poll(self, tv_input):
        """One report from each of the room's devices, as their providers poll."""
        self.report("example_tv_power", "ON")
        self.report("example_tv_input", tv_input)
        self.report("example_player_reported_at", self.wall[0].isoformat())
        self.report("example_player_power", "ON")

    def test_a_tv_on_its_home_screen_is_switched_back_and_an_unknown_input_is_not(self):
        self.make((policy(WEST, hold=PLAYER, enabled=False), at(8)))
        fields = [field for choice in ("off", PLAYER, COMPUTER) for field in expected_plan(HOUSE, WEST, choice).fields]
        self.cache = NativeCache(fields, native_power={"example_player_power": "example_player_reported_at"},
                                 clock=lambda: self.wall[0])
        self.devices.read = self.cache.read
        self.poll("hdmi1")
        self.assertEqual(self.step()[WEST]["phase"], "verified")
        self.advance(5)
        self.poll("tv")
        status = self.step()[WEST]
        self.assertEqual((status["state"], status["mismatched"], status["repair"]),
                         ("degraded", ["example_tv_input"], "example_tv_input"))
        self.assertEqual(self.devices.writes, [(WEST, "example_tv_input", "hdmi1")])
        self.advance(5)
        self.poll("hdmi1")
        self.assertEqual(self.step()[WEST]["phase"], "verified")
        self.advance(5)
        self.poll("UNDEF")
        status = self.step()[WEST]
        self.assertEqual((status["state"], status["unknown"]), ("unknown", ["example_tv_input"]))
        self.assertEqual(len(self.devices.writes), 1, "an Unknown input is never repaired")


class Transport:
    def __init__(self):
        self.commands, self.sources, self.reads = [], [], {"loc_example_west_source": " example_player\n"}

    def command(self, item, value, source=None):
        self.commands.append((item, value))
        self.sources.append(source)

    def read(self, item):
        return self.reads[item]


class Adapter:
    def __init__(self):
        self.transport = Transport()
        self.contract = type("Contract", (), {"rooms": {room: {"sources": spec["sources"]}
                                                        for room, spec in HOUSE["routing"]["rooms"].items()}})()


class Revisions:
    def __init__(self, revision):
        self.revision = revision

    def current_revision(self, room):
        return self.revision


class Writes(unittest.TestCase):
    def test_only_current_revision_compiled_fields_and_sources_are_written(self):
        adapter = Adapter()
        io = PolicyIO(adapter, Revisions(3), None, HOUSE)
        io.write(WEST, "example_tv_power", "ON", 3)
        io.write(WEST, "loc_example_west_source", COMPUTER, 3)
        for args in ((WEST, "example_tv_power", "ON", 2), (WEST, "example_tv_power", "BLINK", 3),
                     (WEST, "unrelated_item", "ON", 3), (EAST, "example_tv_power", "ON", 3)):
            with self.subTest(args=args), self.assertRaises(AccessError):
                io.write(*args)
        self.assertEqual(adapter.transport.commands, [("example_tv_power", "ON"), ("loc_example_west_source", COMPUTER)])
        self.assertEqual(io.source(WEST, "loc_example_west_source"), PLAYER)
        with self.assertRaises(AccessError):
            io.source(WEST, "example_tv_power")


class Feed:
    """Two connections: the first fails after one frame, the second ends."""

    def __init__(self, stop):
        self.stop, self.connections, self.filters = stop, 0, []

    def subscribe(self, filters, *, project, opened=None):
        self.filters.append(filters)
        self.connections += 1
        connection = self.connections

        async def frames():
            yield project({"topic": "openhab/items/example_tv_power/stateupdated", "type": "ItemStateUpdatedEvent",
                           "payload": json.dumps({"value": "ON"})})
            if connection == 1:
                raise ConnectionError("private upstream detail")
            self.stop.set()
        return frames()


class Observer(unittest.TestCase):
    def test_disconnect_clears_observations_warns_with_class_only_and_backs_off(self):
        import threading
        stop = threading.Event()
        changed = []
        cache = NativeCache([Field("example_tv_power", "ON", "device:example_tv")], on_change=changed.append)
        delays = []

        async def sleep(delay):
            delays.append(delay)
        feed = Feed(stop)
        with patch("oh_access.worker.asyncio.sleep", sleep), self.assertLogs("oh_access.worker", logging.WARNING) as logs:
            asyncio.run(observe(feed, cache, stop))
        self.assertEqual(delays, [1])
        # One wildcard subscription per connection, whatever the house observes.
        self.assertEqual(feed.filters, [OBSERVED, OBSERVED])
        self.assertEqual(changed, ["example_tv_power", "example_tv_power", "example_tv_power", "example_tv_power"])
        self.assertEqual(cache.read(("example_tv_power",)), {})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ConnectionError", logs.output[0])
        self.assertNotIn("private", logs.output[0])

    def test_warnings_are_rate_limited_and_count_suppressed_failures(self):
        clock = [0.0]
        warnings = Warnings(every=300, clock=lambda: clock[0])
        with self.assertLogs("oh_access.worker", logging.WARNING) as logs:
            for moment in (0, 10, 20, 301):
                clock[0] = moment
                warnings.failed(OSError("detail"))
        self.assertEqual(len(logs.output), 2)
        self.assertIn("2 earlier failure(s)", logs.output[1])


class Wildcard(unittest.TestCase):
    def test_only_observed_topics_are_kept_before_their_payload_is_read(self):
        cache = NativeCache([Field("tv_power", "ON", "device:tv", native_power=True)], native_power={"tv_power": "tv_reported_at"})
        self.assertEqual(OBSERVED, ["openhab/items/*/stateupdated"])
        self.assertIsNone(cache.project({"topic": "openhab/items/private_token/stateupdated", "type": "ItemStateUpdatedEvent",
                                         "payload": "not json"}))
        self.assertEqual(cache.project({"topic": "openhab/items/tv_power/stateupdated", "type": "ItemStateUpdatedEvent",
                                        "payload": json.dumps({"value": "ON"})}), ("tv_power", "ON"))


if __name__ == "__main__":
    unittest.main()
