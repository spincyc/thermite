"""A stand-in for NuHeat OpenAPI v2 and the localhost climate helper daemon,
for tests of lib/oh_climate/daemon.py and lib/oh_integrations/nuheat_http.

FakeNuheat implements the oh_climate.nuheat_api.Transport protocol over
in-memory Thermostat objects, supporting mode/setpoint writes, indeterminate
5xx or timeout write faults, device error flags, offline states, and read
failures. Server runs the real oh_climate.daemon.ClimateService on 127.0.0.1
with an ephemeral port in a background thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oh_climate import daemon, nuheat_api


@dataclass
class Thermostat:
    """In-memory NuHeat thermostat state; mutate under FakeNuheat.lock or from tests."""

    serial: str
    name: str = "Floor Thermostat"
    online: bool = True
    is_heating: bool = False
    current_temperature: int = 2150
    setpoint_temperature: int = 2200
    mode: int = 1
    hold_until: str | None = None
    error_state: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "serialNumber": self.serial,
            "name": self.name,
            "online": self.online,
            "isHeating": self.is_heating,
            "currentTemperature": self.current_temperature,
            "setPointTemperature": self.setpoint_temperature,
            "mode": self.mode,
            "holdUntil": self.hold_until,
            "errorState": self.error_state,
        }


class FakeNuheat:
    """In-memory NuHeat OpenAPI v2 Transport for deterministic tests."""

    def __init__(
        self,
        thermostats: Mapping[str, Thermostat],
        *,
        expected_bearer: str | None = None,
        mode_codes: Mapping[str, int] | None = None,
    ) -> None:
        self.lock = threading.Lock()
        self.thermostats: dict[str, Thermostat] = dict(thermostats)
        self.expected_bearer = expected_bearer
        self.mode_codes: dict[str, int] = dict(mode_codes or {"Auto": 1, "Hold": 2, "Manual": 3})
        #: Fault injection: None, "timeout", "http_500", "http_401", "http_429", "http_400".
        self.next_write_fault: str | None = None
        #: Optional fault for next GET: None, "timeout", "http_503", "http_401".
        self.next_read_fault: str | None = None
        #: Whether a 500/timeout write still mutates state (simulating ambiguous cloud apply).
        self.apply_on_indeterminate: bool = False
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> nuheat_api.Response:
        parsed = json.loads(body.decode("utf-8")) if body else None
        with self.lock:
            self.requests.append((method, path, parsed))
            if self.expected_bearer is not None:
                auth = headers.get("Authorization")
                if auth != "Bearer " + self.expected_bearer:
                    return nuheat_api.Response(401, b'{"error":"unauthorized"}')

            if method == "GET":
                fault = self.next_read_fault
                self.next_read_fault = None
                if fault == "timeout":
                    raise TimeoutError("simulated cloud read timeout")
                if fault == "http_503":
                    return nuheat_api.Response(503, b"service unavailable")
                if fault == "http_401":
                    return nuheat_api.Response(401, b"unauthorized")
                if path == "/api/v2/Thermostat":
                    rows = [row.payload() for row in self.thermostats.values()]
                    return nuheat_api.Response(200, json.dumps(rows).encode("utf-8"))
                if path.startswith("/api/v2/Thermostat/"):
                    serial = path.removeprefix("/api/v2/Thermostat/")
                    unit = self.thermostats.get(serial)
                    if unit is None:
                        return nuheat_api.Response(404, b"not found")
                    return nuheat_api.Response(200, json.dumps(unit.payload()).encode("utf-8"))
                if path == "/api/v2/Account":
                    return nuheat_api.Response(200, json.dumps({"temperatureScale": "F", "language": "en"}).encode("utf-8"))
                return nuheat_api.Response(404, b"not found")

            if method == "PUT" and path.startswith("/api/v2/Mode/"):
                mode = path.removeprefix("/api/v2/Mode/")
                if not isinstance(parsed, dict):
                    return nuheat_api.Response(400, b"bad payload")
                serial_raw = parsed.get("serialNumber")
                unit = self.thermostats.get(str(serial_raw)) if isinstance(serial_raw, str) else None
                if unit is None:
                    return nuheat_api.Response(404, b"not found")

                def _apply() -> None:
                    if mode in self.mode_codes:
                        unit.mode = self.mode_codes[mode]
                    if "temperature" in parsed and type(parsed["temperature"]) is int:
                        unit.setpoint_temperature = parsed["temperature"]
                    unit.hold_until = parsed.get("holdUntil") if mode == "Hold" else None

                fault = self.next_write_fault
                self.next_write_fault = None
                if fault == "timeout":
                    if self.apply_on_indeterminate:
                        _apply()
                    raise TimeoutError("simulated cloud write timeout")
                if fault == "http_500":
                    if self.apply_on_indeterminate:
                        _apply()
                    return nuheat_api.Response(500, b"internal error")
                if fault == "http_401":
                    return nuheat_api.Response(401, b"unauthorized")
                if fault == "http_429":
                    return nuheat_api.Response(429, b"rate limited", {"Retry-After": "30"})
                if fault == "http_400":
                    return nuheat_api.Response(400, b"invalid request")
                _apply()
                return nuheat_api.Response(204, b"")

            return nuheat_api.Response(404, b"not found")


class Server:
    """Real oh_climate.daemon.ClimateService on 127.0.0.1 over FakeNuheat."""

    def __init__(
        self,
        thermostats: Mapping[str, Thermostat],
        contracts: Mapping[str, nuheat_api.DeviceContract] | None = None,
        *,
        secrets: Path | None = None,
        bearer: str | None = None,
        refresh_bearer: str | None = None,
        refresh: Callable[[str], Mapping[str, Any] | str | None] | None = None,
        now: Callable[[], datetime] | None = None,
        mode_codes: Mapping[str, int] | None = None,
        log: Callable[[str], None] | None = None,
        **options: Any,
    ) -> None:
        self._default_bearer = bearer if bearer is not None else ("sim-" + "nuheat-" + "access")
        self.cloud = FakeNuheat(thermostats, expected_bearer=self._default_bearer, mode_codes=mode_codes)
        self._temp = tempfile.TemporaryDirectory(prefix="oh-climate-") if secrets is None else None
        if self._temp is not None:
            self.secrets = Path(self._temp.name) / "climate"
        else:
            assert secrets is not None
            self.secrets = Path(secrets)
        daemon.write_oauth(self.secrets, self._default_bearer, refresh_value=refresh_bearer)
        for ident, contract in (contracts or {}).items():
            daemon.write_contract(self.secrets, ident, contract)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._refresh = refresh
        self._log = log
        self._lines: list[str] = []
        self._lines_lock = threading.Lock()
        self._closed = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="oh-climate-sim", daemon=True)
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start(options), self.loop).result(10)
        except BaseException:
            self.close()
            raise

    async def _start(self, options: dict[str, Any]) -> None:
        self.service = daemon.ClimateService(
            self.secrets,
            transport=self.cloud,
            refresh=self._refresh,
            now=self._now,
            log=self._record,
            **options,
        )
        self.server = await daemon.serve(self.service, "127.0.0.1", 0)

    def _record(self, line: str) -> None:
        with self._lines_lock:
            self._lines.append(line)
        if self._log is not None:
            self._log(line)

    def lines(self) -> list[str]:
        with self._lines_lock:
            return list(self._lines)

    @property
    def address(self) -> str:
        return "127.0.0.1:%d" % self.server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://{self.address}"

    async def _stop(self) -> None:
        server = getattr(self, "server", None)
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)
        service = getattr(self, "service", None)
        if service is not None:
            await service.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(10)
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()
        if self._temp is not None:
            self._temp.cleanup()

    def start(self) -> Server:
        return self

    def stop(self) -> None:
        self.close()

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
