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
from oh_room_policy import (Field, Observation, Plan, PolicyError, conflicts, desired_at, due_boundaries,  # noqa: E402
                            expected_plan, local_instant, repair_plan, verify)
from oh_room_policy.worker import PolicyRecord, Worker  # noqa: E402
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
        fields = tuple(Field(item, value, "device:" + device, ("0", "1", "2", "OFF", "ON"))
                       for item, (value, device) in table[room][choice].items())
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


class Cache(unittest.TestCase):
    def fields(self):
        return [field for choice in ("off", PLAYER, COMPUTER) for field in expected_plan(HOUSE, WEST, choice).fields]

    def event(self, item, value, kind="ItemStateUpdatedEvent"):
        return {"topic": f"openhab/items/{item}/stateupdated", "type": kind, "payload": json.dumps({"value": value})}

    def test_projection_keeps_only_compiled_values_and_valid_timestamps(self):
        cache = NativeCache(self.fields(), native_power={"example_player_power": "example_player_reported_at"})
        self.assertIn("openhab/items/example_player_reported_at/stateupdated", cache.topics)
        self.assertEqual(cache.project(self.event("example_tv_input", "hdmi1")), ("example_tv_input", "hdmi1"))
        for event in (self.event("example_tv_input", "hdmi9"), self.event("example_tv_input", "hdmi1", "ItemStateChangedEvent"),
                      self.event("unrelated_item", "ON"), self.event("example_tv_input", "x" * 129),
                      self.event("example_player_reported_at", "yesterday"), {**self.event("example_tv_power", "ON"), "payload": "{"}):
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
        cache = NativeCache([Field("example_tv_power", "ON", "device:example_tv", ("ON", "OFF"))], on_change=changed.append)
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
        cache = NativeCache([Field("tv_power", "ON", "device:tv", ("ON", "OFF"), True)], native_power={"tv_power": "tv_reported_at"})
        self.assertEqual(OBSERVED, ["openhab/items/*/stateupdated"])
        self.assertIsNone(cache.project({"topic": "openhab/items/private_token/stateupdated", "type": "ItemStateUpdatedEvent",
                                         "payload": "not json"}))
        self.assertEqual(cache.project({"topic": "openhab/items/tv_power/stateupdated", "type": "ItemStateUpdatedEvent",
                                        "payload": json.dumps({"value": "ON"})}), ("tv_power", "ON"))


if __name__ == "__main__":
    unittest.main()
