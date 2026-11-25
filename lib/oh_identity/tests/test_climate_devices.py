"""Climate devices on the gateway contract: validated mode and setpoint
commands via POST /api/v1/devices/<slug:device>/commands, SSE remote view
items, state projection, and plain-HTTP Guest listener asset/route coverage."""

from __future__ import annotations

from pathlib import Path

from django.test import Client, TestCase

import oh_room_policy
from oh_identity.auth import Guest
from oh_identity.guest import ASSETS, ROUTES
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import Problem, bootstrap
from oh_identity.tests.test_core import PASSPHRASE
from oh_identity.tests.test_trail import Requests, lines

FLOOR = "example_west_floor"
BATH_FLOOR = "example_bath_floor"


def climate_house() -> dict:
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[3] / "instance/devices.yaml").read_text())
    data["rooms"]["example_bath"] = {"label": "Example Bath", "tag": "Bathroom"}
    data["devices"] += [
        {
            "id": FLOOR,
            "room": "example_west",
            "kind": "thermostat",
            "make": "NuHeat",
            "model": "Signature",
            "name": "West Floor",
            "controls": {"control": ["nuheat_http"]},
            "config": {"nuheat_http": {"scale": "F", "step": 100}},
        },
        {
            "id": BATH_FLOOR,
            "room": "example_bath",
            "kind": "thermostat",
            "make": "NuHeat",
            "model": "Signature",
            "name": "Bath Floor",
            "controls": {"control": ["nuheat_http"]},
            "config": {"nuheat_http": {"scale": "C", "step": 50}},
        },
    ]
    return data


def compiled(data: dict) -> dict:
    import oh_render
    import oh_site_contract
    from oh_conf import House

    return oh_site_contract.compile(House(data), oh_render.build(House(data)))


class ClimateContract(TestCase):
    def test_climate_devices_compile_actions_states_and_remote_view(self):
        authority = compiled(climate_house())
        self.assertIn(FLOOR, authority["devices"])
        self.assertIn(BATH_FLOOR, authority["devices"])

        floor = authority["devices"][FLOOR]
        self.assertEqual(floor["rooms"], {})
        self.assertEqual(
            floor["actions"]["climate_mode"],
            {
                "kind": "enum",
                "values": ["Auto", "Hold", "Manual"],
                "item": f"{FLOOR}_climate_mode",
                "resources": [f"device:{FLOOR}"],
            },
        )
        self.assertEqual(
            floor["actions"]["climate_setpoint"],
            {
                "kind": "number",
                "min": -5000,
                "max": 10000,
                "step": 1,
                "item": f"{FLOOR}_climate_setpoint",
                "resources": [f"device:{FLOOR}"],
            },
        )

        # Read-only status items are NOT actions, but ARE in states and remote view.
        for suffix in ("temperature", "heating_call", "hold_until", "online",
                       "has_error", "stale", "write_state", "climate_status"):
            item = f"{FLOOR}_{suffix}"
            self.assertNotIn(item, floor["actions"])
            self.assertIn(item, authority["states"])
            self.assertIn(item, authority["views"]["remote"]["statechanged"])
        self.assertIn(f"{FLOOR}_climate_mode", authority["views"]["remote"]["statechanged"])
        self.assertIn(f"{FLOOR}_climate_setpoint", authority["views"]["remote"]["statechanged"])
        self.assertIn(f"{FLOOR}_problem", authority["views"]["remote"]["statechanged"])

        # Guest listener serves climate assets and device commands route.
        self.assertIn("climate.js", ASSETS)
        self.assertIn("climate.css", ASSETS)
        self.assertEqual(ROUTES["api/v1/devices/<slug:device>/commands"], ("POST",))


class ClimateDispatch(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)

    def setUp(self):
        self.commands: list[tuple[str, str]] = []
        self.states = {
            f"{FLOOR}_temperature": "70.5",
            f"{FLOOR}_climate_setpoint": "7200",
            f"{FLOOR}_climate_mode": "Auto",
            f"{FLOOR}_heating_call": "ON",
            f"{FLOOR}_online": "ON",
            f"{FLOOR}_has_error": "OFF",
            f"{FLOOR}_stale": "OFF",
            f"{FLOOR}_write_state": "indeterminate",
            f"{FLOOR}_climate_status": "ok",
        }
        transport = type("Transport", (), {
            "command": lambda _, item, value, source=None: self.commands.append((item, value)),
            "read": lambda _, item: self.states.get(item, "NULL"),
        })()
        self.adapter = RuntimeAdapter(RuntimeContract(**compiled(climate_house())), transport, policy=oh_room_policy)
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)
        self.request("get", "/api/v1/session")

    def device_command(self, device: str, action: str, value):
        return self.request("post", f"/api/v1/devices/{device}/commands", {"action": action, "value": value})

    def test_valid_mode_and_setpoint_accepted_and_invalid_rejected(self):
        self.signin("owner")
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            resp = self.device_command(FLOOR, "climate_mode", "Manual")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": True, "device": FLOOR, "action": "climate_mode"})
        [line] = lines(logs, "cmd")
        self.assertEqual((line["device"], line["item"], line["value"]), (FLOOR, f"{FLOOR}_climate_mode", "Manual"))

        resp2 = self.device_command(FLOOR, "climate_setpoint", 2250)
        self.assertEqual(resp2.status_code, 202)
        self.assertEqual(self.commands, [(f"{FLOOR}_climate_mode", "Manual"), (f"{FLOOR}_climate_setpoint", "2250")])

        # Invalid mode, non-integer float, boolean, and out-of-range setpoint all rejected (400).
        for action, bad_val in (
            ("climate_mode", "Turbo"),
            ("climate_mode", 1),
            ("climate_setpoint", 22.5),
            ("climate_setpoint", True),
            ("climate_setpoint", 99999),
            ("climate_setpoint", -9999),
        ):
            with self.subTest(action=action, bad_val=bad_val):
                bad = self.device_command(FLOOR, action, bad_val)
                self.assertEqual(bad.status_code, 400)

        # Read-only item cannot be commanded.
        ro = self.device_command(FLOOR, f"{FLOOR}_temperature", 2100)
        self.assertEqual(ro.status_code, 404)

    def test_guest_and_read_projection_for_climate_items(self):
        # Guest can command climate devices directly.
        self.adapter.dispatch_device(Guest(False), BATH_FLOOR, "climate_mode", "Hold")
        self.assertEqual(self.commands, [(f"{BATH_FLOOR}_climate_mode", "Hold")])

        # Reading climate items projects each typed value cleanly.
        self.assertEqual(self.adapter.read(f"{FLOOR}_temperature"), "70.5")
        self.assertEqual(self.adapter.read(f"{FLOOR}_write_state"), "indeterminate")
        self.assertEqual(self.adapter.read(f"{FLOOR}_heating_call"), "ON")
        with self.assertRaises(Problem):
            self.adapter.dispatch_device(Guest(False), FLOOR, "climate_mode", "Bogus")
