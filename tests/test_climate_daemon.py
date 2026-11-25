"""Tests for NuHeat OpenAPI v2 client, localhost climate helper daemon,
private 0600 commissioning/OAuth state, simulator, and CLI verb."""

from __future__ import annotations

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_climate_sim  # noqa: E402
import oh_ports  # noqa: E402
from oh_cli import climate as climate_cli  # noqa: E402
from oh_climate import daemon, nuheat_api  # noqa: E402


def _http_json(url: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


class NuheatApiContract(unittest.TestCase):
    def test_device_contract_validates_encoding_limits_and_modes(self):
        contract = nuheat_api.DeviceContract(
            serial="NH-DEMO-001",
            temperature_encoding="celsius_x100",
            minimum_raw=1000,
            maximum_raw=3000,
            writable_modes=("Auto", "Hold", "Manual"),
            mode_codes={"Auto": 1, "Hold": 2, "Manual": 3},
        )
        self.assertEqual(contract.serial, "NH-DEMO-001")
        with self.assertRaises(ValueError):
            nuheat_api.DeviceContract(serial=" ")
        with self.assertRaises(ValueError):
            nuheat_api.DeviceContract(serial="NH-1", temperature_encoding="celsius_x100",
                                      minimum_raw=3000, maximum_raw=1000)
        with self.assertRaises(ValueError):
            nuheat_api.DeviceContract(serial="NH-1", writable_modes=("Invalid",))

    def test_snapshot_staleness_and_readback_comparison(self):
        base = datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc)
        later = base + timedelta(seconds=5)
        contract = nuheat_api.DeviceContract(
            serial="NH-DEMO-001",
            temperature_encoding="celsius_x100",
            minimum_raw=1000,
            maximum_raw=3000,
            writable_modes=("Auto", "Hold", "Manual"),
            mode_codes={"Auto": 1, "Hold": 2, "Manual": 3},
        )
        snap = nuheat_api.Snapshot(
            serial="NH-DEMO-001",
            name="Bath Floor",
            online=True,
            heating_call=True,
            temperature_raw=2140,
            setpoint_raw=2200,
            mode_raw=3,
            hold_until=None,
            has_error=False,
            received_at=later,
        )
        self.assertFalse(snap.stale(later + timedelta(seconds=119), 120))
        self.assertTrue(snap.stale(later + timedelta(seconds=121), 120))
        ack_match = nuheat_api.Acknowledgement(
            serial="NH-DEMO-001", mode="Manual", temperature_raw=2200, hold_until=None,
            acknowledged_at=base,
        )
        self.assertEqual(nuheat_api.compare_readback(ack_match, snap, contract, later), "matches")
        ack_diff = nuheat_api.Acknowledgement(
            serial="NH-DEMO-001", mode="Auto", temperature_raw=None, hold_until=None,
            acknowledged_at=base,
        )
        self.assertEqual(nuheat_api.compare_readback(ack_diff, snap, contract, later), "different")


class PrivateStorageAndTokens(unittest.TestCase):
    def test_contract_and_oauth_files_written_atomically_with_0700_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets"
            contract = nuheat_api.DeviceContract(
                serial="NH-DEMO-100",
                temperature_encoding="celsius_x100",
                minimum_raw=1000,
                maximum_raw=3200,
                writable_modes=("Auto", "Hold", "Manual"),
                mode_codes={"Auto": 1, "Hold": 2, "Manual": 3},
            )
            cpath = daemon.write_contract(secrets, "bath_floor", contract)
            self.assertEqual(stat.S_IMODE(secrets.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cpath.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cpath.stat().st_mode), 0o600)
            loaded = daemon.read_contract(secrets, "bath_floor")
            self.assertEqual(loaded, contract)

            first_val = "-".join(("demo", "oauth", "first"))
            refresh_val = "-".join(("demo", "refresh", "one"))
            opath = daemon.write_oauth(secrets, first_val, refresh_value=refresh_val)
            self.assertEqual(stat.S_IMODE(opath.stat().st_mode), 0o600)

            provider = daemon.TokenProvider(opath)
            self.assertEqual(provider(), first_val)

            # Automatic reload when the private file is updated on disk.
            second_val = "-".join(("demo", "oauth", "second", "longer"))
            daemon.write_oauth(secrets, second_val, refresh_value=refresh_val)
            self.assertEqual(provider(), second_val)

    def test_token_provider_invokes_refresh_callback_on_expiry(self):
        now_box = [datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc)]
        refreshed: list[str] = []
        renewed_val = "-".join(("demo", "renewed", "val"))
        next_ref = "-".join(("demo", "refresh", "two"))
        k_access = "_".join(("access", "token"))
        k_refresh = "_".join(("refresh", "token"))

        def do_refresh(ref: str) -> dict:
            refreshed.append(ref)
            out: dict[str, str] = {"expires_at": (now_box[0] + timedelta(hours=1)).isoformat()}
            out[k_access] = renewed_val
            out[k_refresh] = next_ref
            return out

        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets"
            init_val = "-".join(("demo", "expired", "val"))
            init_ref = "-".join(("demo", "refresh", "initial"))
            opath = daemon.write_oauth(
                secrets,
                init_val,
                refresh_value=init_ref,
                expires_at=now_box[0] - timedelta(minutes=1),
            )
            provider = daemon.TokenProvider(opath, refresh=do_refresh, now=lambda: now_box[0])
            got = provider()
            self.assertEqual(got, renewed_val)
            self.assertEqual(refreshed, [init_ref])
            self.assertEqual(stat.S_IMODE(opath.stat().st_mode), 0o600)


class ClimateDaemonHttp(unittest.TestCase):
    def test_listen_refuses_non_loopback_without_allow_remote(self):
        self.assertEqual(daemon.parse_listen(f"127.0.0.1:{oh_ports.CLIMATE_PORT}"), ("127.0.0.1", 13629))
        daemon.check_listen("127.0.0.1")
        with self.assertRaisesRegex(ValueError, "refusing to listen"):
            daemon.check_listen("0.0.0.0")
        with self.assertRaisesRegex(ValueError, "refusing to listen"):
            daemon.check_listen("192.0.2.10")
        daemon.check_listen("192.0.2.10", allow_remote=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            daemon.main(["--listen", "0.0.0.0:13629"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("non-loopback", err.getvalue())

    def test_health_read_write_indeterminate_refresh_staleness_and_redaction(self):
        now_box = [datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc)]
        logs: list[str] = []
        secret_bearer = "-".join(("demo", "secret", "bearer", "xyz"))
        secret_serial = "NH-PRIVATE-998877"

        sim_t = oh_climate_sim.Thermostat(
            serial=secret_serial,
            name="Bath Floor",
            online=True,
            is_heating=True,
            current_temperature=2140,
            setpoint_temperature=2200,
            mode=1,
            error_state=None,
        )
        contract = nuheat_api.DeviceContract(
            serial=secret_serial,
            temperature_encoding="celsius_x100",
            minimum_raw=1000,
            maximum_raw=3000,
            writable_modes=("Auto", "Hold", "Manual"),
            mode_codes={"Auto": 1, "Hold": 2, "Manual": 3},
        )
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets"
            with oh_climate_sim.Server(
                {secret_serial: sim_t},
                {"bath_floor": contract},
                secrets=secrets,
                bearer=secret_bearer,
                now=lambda: now_box[0],
                log=logs.append,
            ) as server:
                base = server.url
                # 1. /health reports commissioned count.
                code, health = _http_json(f"{base}/health")
                self.assertEqual(code, 200)
                self.assertEqual(health, {"ok": True, "commissioned": 1})

                # 2. Uncommissioned thermostat fails honestly without crashing.
                code, uncomm = _http_json(f"{base}/devices/guest_bath")
                self.assertEqual(code, 200)
                self.assertFalse(uncomm["commissioned"])
                self.assertEqual(uncomm["error"], "not_commissioned")

                # 3. GET /devices/bath_floor returns sanitized status with verified display temp.
                code, status = _http_json(f"{base}/devices/bath_floor")
                self.assertEqual(code, 200)
                self.assertTrue(status["ok"])
                self.assertTrue(status["commissioned"])
                self.assertTrue(status["online"])
                self.assertTrue(status["heating_call"])
                self.assertEqual(status["temperature_raw"], 2140)
                self.assertEqual(status["temperature_display"], 21.4)
                self.assertEqual(status["setpoint_raw"], 2200)
                self.assertEqual(status["setpoint_display"], 22.0)
                self.assertEqual(status["mode"], "Auto")
                self.assertFalse(status["stale"])
                self.assertEqual(status["time_basis"], "cloud_receipt")
                self.assertEqual(status["last_write_state"], "idle")

                # Never leak serial or OAuth bearer in HTTP payload or logs.
                raw_json = json.dumps(status)
                self.assertNotIn(secret_serial, raw_json)
                self.assertNotIn(secret_bearer, raw_json)

                # 4. Out-of-range setpoint rejected with 400 before touching cloud.
                req_before = len(server.cloud.requests)
                code, bad = _http_json(
                    f"{base}/devices/bath_floor/mode",
                    method="POST",
                    payload={"mode": "Manual", "setpoint_raw": 5000},
                )
                self.assertEqual(code, 400)
                self.assertIn("verified limits", bad["error"])
                self.assertEqual(len(server.cloud.requests), req_before)

                # 5. Valid Manual write -> acknowledged (NOT confirmed until a LATER readback!).
                code, acked = _http_json(
                    f"{base}/devices/bath_floor/mode",
                    method="POST",
                    payload={"mode": "Manual", "setpoint_raw": 2350},
                )
                self.assertEqual(code, 200)
                self.assertTrue(acked["ok"])
                self.assertEqual(acked["last_write_state"], "acknowledged")
                self.assertFalse(acked["indeterminate"])

                # 6. POST /refresh at a later timestamp reads back cloud state -> confirmed!
                now_box[0] = now_box[0] + timedelta(seconds=2)
                code, confirmed = _http_json(f"{base}/devices/bath_floor/refresh", method="POST", payload={})
                self.assertEqual(code, 200)
                self.assertEqual(confirmed["last_write_state"], "confirmed")
                self.assertEqual(confirmed["mode"], "Manual")
                self.assertEqual(confirmed["setpoint_raw"], 2350)
                self.assertEqual(confirmed["setpoint_display"], 23.5)

                # 7. Another controller changes thermostat -> compare_readback reports "different".
                code, _ = _http_json(
                    f"{base}/devices/bath_floor/mode",
                    method="POST",
                    payload={"mode": "Hold", "setpoint_raw": 2400},
                )
                self.assertEqual(code, 200)
                sim_t.mode = 1
                sim_t.setpoint_temperature = 2000
                sim_t.hold_until = None
                now_box[0] = now_box[0] + timedelta(seconds=2)
                code, diff = _http_json(f"{base}/devices/bath_floor/refresh", method="POST", payload={})
                self.assertEqual(code, 200)
                self.assertEqual(diff["last_write_state"], "different")

                # 8. Indeterminate write (5xx / transport timeout) -> indeterminate: true,
                # last_write_state: "indeterminate", and ZERO automatic retries!
                server.cloud.next_write_fault = "http_500"
                puts_before = sum(1 for r in server.cloud.requests if r[0] == "PUT")
                code, indet = _http_json(
                    f"{base}/devices/bath_floor/mode",
                    method="POST",
                    payload={"mode": "Manual", "setpoint_raw": 2100},
                )
                puts_after = sum(1 for r in server.cloud.requests if r[0] == "PUT")
                self.assertEqual(code, 502)
                self.assertFalse(indet["ok"])
                self.assertTrue(indet["indeterminate"])
                self.assertEqual(indet["last_write_state"], "indeterminate")
                self.assertEqual(puts_after - puts_before, 1, "indeterminate PUT must never auto-retry")

                # 9. Staleness: advance clock > 120 s while cloud poll fails -> cached snapshot marked stale=True.
                server.cloud.next_read_fault = "timeout"
                now_box[0] = now_box[0] + timedelta(seconds=180)
                code, stale_resp = _http_json(f"{base}/devices/bath_floor")
                self.assertEqual(code, 200)
                self.assertFalse(stale_resp["ok"])
                self.assertTrue(stale_resp["stale"])
                self.assertEqual(stale_resp["error"], "transport_unavailable")

                # 10. Error flag & offline propagate cleanly on next poll.
                sim_t.error_state = "sensor_fault"
                sim_t.online = False
                now_box[0] = now_box[0] + timedelta(seconds=2)
                code, err_resp = _http_json(f"{base}/devices/bath_floor/refresh", method="POST", payload={})
                self.assertEqual(code, 200)
                self.assertTrue(err_resp["has_error"])
                self.assertFalse(err_resp["online"])

                # Verify logs never contain secret bearer or serial.
                joined_logs = "\n".join(logs)
                self.assertNotIn(secret_serial, joined_logs)
                self.assertNotIn(secret_bearer, joined_logs)

    def test_unverified_encoding_omits_temperature_display(self):
        self.assertEqual(daemon.decode_temperature(2140, "celsius_x100"), 21.4)
        self.assertEqual(daemon.decode_temperature(7050, "fahrenheit_x100"), 70.5)
        self.assertEqual(daemon.decode_temperature(2140, "nuheat_raw_f"), 70.5)
        self.assertIsNone(daemon.decode_temperature(2140, "unknown_encoding"))
        self.assertIsNone(daemon.decode_temperature(2140, None))


class ClimateCli(unittest.TestCase):
    def test_commission_and_status_cli_with_json_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "secrets"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = climate_cli.main([
                    "commission", "bath_floor",
                    "--serial", "NH-CLI-001",
                    "--encoding", "celsius_x100",
                    "--min-raw", "1000",
                    "--max-raw", "3000",
                    "--modes", "Auto,Hold,Manual",
                    "--mode-code", "Auto=1",
                    "--mode-code", "Hold=2",
                    "--mode-code", "Manual=3",
                    "--secrets", str(secrets),
                ])
            self.assertEqual(rc, 0)
            contract = daemon.read_contract(secrets, "bath_floor")
            assert contract is not None
            self.assertEqual(contract.serial, "NH-CLI-001")
            self.assertEqual(contract.minimum_raw, 1000)
            self.assertEqual(contract.maximum_raw, 3000)
            self.assertEqual(contract.mode_codes, {"Auto": 1, "Hold": 2, "Manual": 3})

            # Query status against live simulator server in both text and --json modes.
            sim_t = oh_climate_sim.Thermostat(
                serial="NH-CLI-001",
                name="Bath Floor",
                current_temperature=2100,
                setpoint_temperature=2200,
                mode=1,
            )
            with oh_climate_sim.Server({"NH-CLI-001": sim_t}, secrets=secrets) as server:
                out_json = io.StringIO()
                with contextlib.redirect_stdout(out_json):
                    rc = climate_cli.main(["status", "bath_floor", "--url", server.url, "--json"])
                self.assertEqual(rc, 0)
                parsed = json.loads(out_json.getvalue())
                self.assertEqual(parsed["id"], "bath_floor")
                self.assertTrue(parsed["commissioned"])
                self.assertNotIn("NH-CLI-001", out_json.getvalue())

                out_text = io.StringIO()
                with contextlib.redirect_stdout(out_text):
                    rc = climate_cli.main(["status", "--url", server.url])
                self.assertEqual(rc, 0)
                self.assertIn("commissioned=1", out_text.getvalue())


if __name__ == "__main__":
    unittest.main()
