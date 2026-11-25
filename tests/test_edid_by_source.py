"""Per-room source EDIDs in validation, routing tables, policy and simulated hardware.

The production JavaScript uses this synthetic house in test_zone_sync.Runtime.
"""
import unittest
from unittest import mock

from tests.test_zone_sync import CINEMA, COMPUTER, DEN, DISC, LOUNGE, OUT2, PLAYER, capped, house, rendered_table

import oh_render
from oh_conf import House
from oh_room_policy import expected_plan
from oh_testing import routing as routing_check

CAP = "4k60-444-hdr-7.1ch"
EDID1 = "example_matrix_input1_edid"


class Validation(unittest.TestCase):
    def test_agreeing_caps_for_one_source_are_valid_and_render_only_on_configured_outputs(self):
        data = house(capped)
        self.assertEqual(oh_render.validate(data), [])
        self.assertEqual(rendered_table(data)["edid"]["overrides"], {"1": {OUT2: 24}})
        data["routing"]["rooms"][DEN]["edid_by_source"] = {COMPUTER: CAP}
        self.assertEqual(oh_render.validate(data), [])
        self.assertEqual(rendered_table(data)["edid"]["overrides"],
                         {"1": {OUT2: 24, "example_matrix_output3_input": 24}})
        self.assertNotIn("overrides", rendered_table(house())["edid"])

    def test_conflicting_caps_for_one_source_name_the_rooms(self):
        data = house(capped)
        data["routing"]["rooms"][DEN]["edid_by_source"] = {COMPUTER: "4k60-444-hdr-2ch"}
        problems = oh_render.validate(data)
        self.assertTrue(any("conflicts with" in problem and LOUNGE in problem and DEN in problem
                            and COMPUTER in problem for problem in problems), problems)

    def test_invalid_configurations_give_the_location_and_cause(self):
        where = f"routing.rooms.{LOUNGE}.edid_by_source"
        cases = [
            (lambda entry, r: r.pop("edid"), where, "needs routing.edid"),
            (lambda entry, r: entry.pop("hdmi_output"), where, "needs hdmi_output"),
            (lambda entry, r: entry.update(edid_by_source=[]), where, "must map sources to EDID names"),
            (lambda entry, r: entry.update(edid_by_source={}), where, "must map sources to EDID names"),
            (lambda entry, r: entry.update(edid_by_source={COMPUTER: "unknown"}), f"{where}.{COMPUTER}", "EDID name"),
            (lambda entry, r: entry.update(edid_by_source={COMPUTER: []}), f"{where}.{COMPUTER}", "EDID name"),
            (lambda entry, r: entry["sources"].remove(COMPUTER), f"{where}.{COMPUTER}", "offered HDMI matrix source"),
        ]
        for change, path, reason in cases:
            with self.subTest(path=path, reason=reason):
                data = house(capped)
                route = data["routing"]
                change(route["rooms"][LOUNGE], route)
                problems = oh_render.validate(data)
                self.assertTrue(any(path in problem and reason in problem for problem in problems), problems)

        data = house()
        data["routing"]["rooms"][CINEMA]["edid_by_source"] = {PLAYER: CAP}
        problems = oh_render.validate(data)
        self.assertTrue(any(f"routing.rooms.{CINEMA}.edid_by_source.{PLAYER}" in problem
                            and "offered HDMI matrix source" in problem for problem in problems), problems)


class Consumers(unittest.TestCase):
    def test_policy_declares_the_capped_code_only_for_the_room_source_pair(self):
        data = house(capped)
        rooms = data["routing"]["rooms"]
        plan = expected_plan(data, LOUNGE, COMPUTER)
        self.assertIn((EDID1, "24"), plan.unsynced_effects)
        self.assertNotIn(EDID1, dict(plan.boundary_effects))
        del rooms[LOUNGE]["receiver"]["sync_with"]
        self.assertEqual(dict(expected_plan(data, LOUNGE, COMPUTER).boundary_effects)[EDID1], "24")
        self.assertEqual(dict(expected_plan(data, DEN, COMPUTER).boundary_effects)[EDID1], "42")
        self.assertEqual(dict(expected_plan(data, LOUNGE, DISC).boundary_effects)["example_matrix_input2_edid"], "41")

    def test_routing_check_requires_the_cap_while_the_room_watches_then_accepts_the_remaining_viewer(self):
        with mock.patch.object(routing_check.sim, "serve", return_value=None):
            check = routing_check.Check(House(house(capped)), log=lambda *_: None)
        matrix = check.sims["example_matrix"]
        for output in (1, 2, 3):
            matrix.set(output, 8)
        for number in (1, 2, 3):
            matrix.set_edid(number, 36)
        matrix.set(2, 1)
        matrix.set(3, 1)
        matrix.set_edid(1, 42)
        self.assertEqual(check.edid_problems(), {EDID1: ([24], 42)})
        matrix.set_edid(1, 24)
        self.assertEqual(check.edid_problems(), {})
        matrix.set(2, 8)
        self.assertEqual(check.edid_problems(), {EDID1: ([42], 24)})
        matrix.set_edid(1, 42)
        self.assertEqual(check.edid_problems(), {})
        matrix.set(3, 8)
        self.assertEqual(check.edid_problems(), {EDID1: ([36], 42)})

    def test_bk808_settings_edid_accepts_string_digit_keys_and_rejects_duplicates(self):
        from oh_integrations import bk808_http
        matrix = {
            "id": "example_matrix", "room": "example_west", "kind": "hdmi_matrix", "ip": "192.0.2.20",
            "controls": {"control": ["bk808_http"]},
            "config": {"bk808_http": {"edid": {"1": "4k60-444-hdr-2ch", "8": "copy-output-1"}}},
        }
        self.assertEqual(bk808_http.validate(matrix), [])
        desired = bk808_http.desired(matrix)["edid"]
        self.assertEqual(desired[1], bk808_http.EDID["4k60-444-hdr-2ch"])
        self.assertEqual(desired[8], bk808_http.EDID["copy-output-1"])
        dup = {**matrix, "config": {"bk808_http": {"edid": {1: "4k60-444-hdr-2ch", "1": "copy-output-1"}}}}
        self.assertTrue(any("more than once" in problem for problem in bk808_http.validate(dup)))
        for bad in ("01", "0", "9", "abc"):
            wrong = {**matrix, "config": {"bk808_http": {"edid": {bad: "4k60-444-hdr-2ch"}}}}
            self.assertTrue(bk808_http.validate(wrong), bad)
        data = house()
        next(d for d in data["devices"] if d["id"] == "example_matrix")["config"] = {
            "bk808_http": {"edid": {"1": "4k60-444-hdr-2ch"}}}
        problems = oh_render.validate(data)
        self.assertTrue(any("config.bk808_http.edid.1" in problem and "routing.edid sets the EDID" in problem
                            for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
