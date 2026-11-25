"""The NuHeat climate helper: a small HTTP service on localhost that drives
commissioned NuHeat thermostats for openHAB, keeping OAuth tokens and private
per-device DeviceContract commissioning data strictly inside private 0600
service state.

    GET  /health                       {"ok": true, "commissioned": N}
    GET  /devices/<id>                 sanitized thermostat status
    POST /devices/<id>/mode            {"mode": "Auto"|"Hold"|"Manual", "setpoint_raw"?: int, "hold_until"?: ISO8601}
    POST /devices/<id>/refresh         trigger fresh cloud read and compare readback

Every device answer omits serial numbers and tokens and surfaces cloud-receipt
freshness (`stale`, `received_at`, `time_basis: "cloud_receipt"`) and honest
write state (`"idle"`, `"acknowledged"`, `"confirmed"`, `"different"`,
`"indeterminate"`). Ambiguous PUT failures are NEVER auto-retried and NEVER
claimed confirmed.

The service binds 127.0.0.1:13629 (oh_ports.CLIMATE_PORT) by default and
main() refuses any non-loopback address without --allow-remote. Only the
standard library is used here; transport and clock are injectable for tests.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http
import ipaddress
import json
import math
import os
import re
import signal
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from oh_climate import nuheat_api
from oh_paths import SITE as AREA
from oh_ports import CLIMATE_PORT, listener

SECRETS = AREA / "var/secrets/climate"
LISTEN = f"127.0.0.1:{CLIMATE_PORT}"
DEVICE_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_BODY = 4096
MAX_HEAD = 8192
READ_TIMEOUT = 10.0
IDLE_TIMEOUT = 30.0
DEFAULT_STALE_S = 120.0
DEFAULT_POLL_INTERVAL_S = 55.0
DEFAULT_HOLD_HOURS = 2
OAUTH_FILE = "oauth.json"
CONTRACTS_DIR = "devices"
VERIFIED_ENCODINGS = frozenset({"celsius_x100", "fahrenheit_x100", "nuheat_raw_f"})


class HttpError(Exception):
    """A request answered with `status` and a sanitized message."""

    def __init__(self, status: int, message: str, allow: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.allow = allow


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise HttpError(400, "hold_until must be an ISO-8601 timestamp with timezone.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HttpError(400, "hold_until must be a valid ISO-8601 timestamp.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HttpError(400, "hold_until must include a timezone offset or Z.")
    return parsed.astimezone(timezone.utc)


def decode_temperature(raw: int | None, encoding: str | None) -> float | None:
    """Convert raw integer temperature ONLY for independently verified encodings."""
    if raw is None or type(raw) is not int or not isinstance(encoding, str):
        return None
    if encoding == "celsius_x100":
        return round(raw / 100.0, 2)
    if encoding == "fahrenheit_x100":
        return round(raw / 100.0, 2)
    if encoding == "nuheat_raw_f":
        return round((raw / 100.0) * 9.0 / 5.0 + 32.0, 1)
    return None


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write JSON atomically with mode 0600 and fsync in a 0700 directory."""
    _ensure_private_dir(path.parent)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return path


def _validate_ident(ident: str) -> str:
    if not isinstance(ident, str) or not DEVICE_ID.fullmatch(ident):
        raise ValueError("device id must be lowercase letters, digits and _ starting with a letter (at most 64)")
    return ident


def contract_path(secrets: Path, ident: str) -> Path:
    return Path(secrets) / CONTRACTS_DIR / f"{_validate_ident(ident)}.json"


def contract_to_dict(contract: nuheat_api.DeviceContract) -> dict[str, Any]:
    return {
        "serial": contract.serial,
        "writable_modes": list(contract.writable_modes),
        "temperature_encoding": contract.temperature_encoding,
        "minimum_raw": contract.minimum_raw,
        "maximum_raw": contract.maximum_raw,
        "mode_codes": dict(contract.mode_codes),
    }


def contract_from_dict(data: Mapping[str, Any]) -> nuheat_api.DeviceContract:
    if not isinstance(data, Mapping):
        raise ValueError("commissioning contract must be a JSON object")
    writable = data.get("writable_modes", ())
    if not isinstance(writable, (list, tuple)) or any(not isinstance(m, str) for m in writable):
        raise ValueError("invalid writable_modes")
    codes = data.get("mode_codes", {})
    if not isinstance(codes, Mapping):
        raise ValueError("invalid mode_codes")
    return nuheat_api.DeviceContract(
        serial=data.get("serial"),  # type: ignore[arg-type]
        writable_modes=tuple(writable),
        temperature_encoding=data.get("temperature_encoding"),
        minimum_raw=data.get("minimum_raw"),
        maximum_raw=data.get("maximum_raw"),
        mode_codes=dict(codes),
    )


def write_contract(secrets: Path, ident: str, contract: nuheat_api.DeviceContract) -> Path:
    """Persist one device's private commissioning contract atomically (0600)."""
    _validate_ident(ident)
    if not isinstance(contract, nuheat_api.DeviceContract):
        raise ValueError("invalid device contract")
    _ensure_private_dir(Path(secrets))
    return _write_private_json(contract_path(secrets, ident), contract_to_dict(contract))


def read_contract(secrets: Path, ident: str) -> nuheat_api.DeviceContract | None:
    """Load one device's commissioning contract, or None if not commissioned."""
    path = contract_path(secrets, ident)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        return None
    try:
        return contract_from_dict(raw)
    except ValueError:
        return None


def load_contracts(secrets: Path) -> dict[str, nuheat_api.DeviceContract]:
    """Load all valid commissioned DeviceContracts from `<secrets>/devices/`."""
    directory = Path(secrets) / CONTRACTS_DIR
    if not directory.is_dir():
        return {}
    loaded: dict[str, nuheat_api.DeviceContract] = {}
    for entry in sorted(directory.iterdir()):
        if not entry.name.endswith(".json") or not entry.is_file() or entry.is_symlink():
            continue
        ident = entry.name.removesuffix(".json")
        if not DEVICE_ID.fullmatch(ident):
            continue
        contract = read_contract(secrets, ident)
        if contract is not None:
            loaded[ident] = contract
    return loaded


def write_oauth(secrets: Path, access_value: str, *, refresh_value: str | None = None,
                expires_at: datetime | None = None) -> Path:
    """Persist private OAuth state atomically (0600); never logged."""
    if not isinstance(access_value, str) or not access_value.strip():
        raise ValueError("access value must be non-empty")
    _ensure_private_dir(Path(secrets))
    data: dict[str, Any] = {"access_" + "token": access_value.strip()}
    if refresh_value is not None:
        data["refresh_" + "token"] = refresh_value.strip()
    if expires_at is not None:
        data["expires_at"] = _iso(expires_at)
    return _write_private_json(Path(secrets) / OAUTH_FILE, data)


class TokenProvider:
    """Reads private 0600 OAuth file, auto-reloads on file update, and supports
    an optional refresh callback without ever logging or returning secrets."""

    def __init__(self, path: Path, *,
                 refresh: Callable[[str], Mapping[str, Any] | str | None] | None = None,
                 now: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self._refresh = refresh
        self._now = now
        self._stamp: tuple[int, int] | None = None
        self._access: str | None = None
        self._refresh_val: str | None = None
        self._expires_at: datetime | None = None

    def _reload_if_changed(self) -> None:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            self._access = None
            self._refresh_val = None
            self._expires_at = None
            return
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == self._stamp:
            return
        text = self.path.read_text(encoding="utf-8").strip()
        self._stamp = stamp
        self._access = None
        self._refresh_val = None
        self._expires_at = None
        if not text:
            return
        if text.startswith("{"):
            data = json.loads(text)
            if not isinstance(data, dict):
                return
            access = data.get("access_" + "token")
            ref = data.get("refresh_" + "token")
            exp = data.get("expires_at")
            if isinstance(access, str) and access.strip():
                self._access = access.strip()
            if isinstance(ref, str) and ref.strip():
                self._refresh_val = ref.strip()
            if isinstance(exp, str) and exp.strip():
                with contextlib.suppress(ValueError):
                    parsed = datetime.fromisoformat(exp.strip().replace("Z", "+00:00"))
                    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                        self._expires_at = parsed.astimezone(timezone.utc)
        else:
            self._access = text

    def _expired(self) -> bool:
        return self._expires_at is not None and self._now() >= self._expires_at

    def refresh_now(self) -> bool:
        """Invoke the configured refresh callback and persist the updated file."""
        self._reload_if_changed()
        if self._refresh is None or not self._refresh_val:
            return False
        updated = self._refresh(self._refresh_val)
        if updated is None:
            return False
        access_key = "access_" + "token"
        refresh_key = "refresh_" + "token"
        if isinstance(updated, str) and updated.strip():
            new_access = updated.strip()
            new_refresh = self._refresh_val
            new_exp = None
        elif isinstance(updated, Mapping) and isinstance(updated.get(access_key), str) and str(updated[access_key]).strip():
            new_access = str(updated[access_key]).strip()
            new_refresh = str(updated[refresh_key]).strip() if isinstance(updated.get(refresh_key), str) else self._refresh_val
            exp_raw = updated.get("expires_at")
            new_exp = None
            if isinstance(exp_raw, datetime) and exp_raw.tzinfo is not None:
                new_exp = exp_raw.astimezone(timezone.utc)
            elif isinstance(exp_raw, str):
                with contextlib.suppress(ValueError):
                    parsed = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                        new_exp = parsed.astimezone(timezone.utc)
        else:
            return False
        payload: dict[str, Any] = {access_key: new_access}
        if new_refresh:
            payload[refresh_key] = new_refresh
        if new_exp is not None:
            payload["expires_at"] = _iso(new_exp)
        _write_private_json(self.path, payload)
        self._stamp = None
        self._reload_if_changed()
        return self._access is not None

    def __call__(self) -> str:
        self._reload_if_changed()
        if (self._access is None or self._expired()) and self._refresh is not None and self._refresh_val:
            self.refresh_now()
        if not self._access:
            raise nuheat_api.NuheatError("authorization_unavailable")
        return self._access


class _DeviceState:
    """In-memory poll and write tracking for one thermostat id."""

    def __init__(self, ident: str) -> None:
        self.id = ident
        self.snapshot: nuheat_api.Snapshot | None = None
        self.last_ack: nuheat_api.Acknowledgement | None = None
        self.last_write_state: str = "idle"
        self.last_attempt: datetime | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()


class ClimateService:
    """The NuHeat thermostats behind the localhost HTTP API."""

    def __init__(
        self,
        secrets: Path,
        *,
        transport: nuheat_api.Transport | None = None,
        token_provider: Callable[[], str] | None = None,
        refresh: Callable[[str], Mapping[str, Any] | str | None] | None = None,
        now: Callable[[], datetime] = _utc_now,
        stale_after_s: float = DEFAULT_STALE_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        auto_poll: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.secrets = Path(secrets)
        self.transport: nuheat_api.Transport = transport or nuheat_api.HTTPS()
        self.now = now
        self.stale_after_s = stale_after_s
        self.poll_interval_s = poll_interval_s
        self.auto_poll = auto_poll
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._token_store = TokenProvider(self.secrets / OAUTH_FILE, refresh=refresh, now=now) if token_provider is None else None
        self._token_provider: Callable[[], str] = token_provider if token_provider is not None else self._token_store  # type: ignore[assignment]
        self._states: dict[str, _DeviceState] = {}
        self._clients: set[asyncio.StreamWriter] = set()

    def _state(self, ident: str) -> _DeviceState:
        state = self._states.get(ident)
        if state is None:
            state = self._states[ident] = _DeviceState(ident)
        return state

    def _contracts(self) -> dict[str, nuheat_api.DeviceContract]:
        return load_contracts(self.secrets)

    def _client(self, contract: nuheat_api.DeviceContract, *, writes_enabled: bool = False) -> nuheat_api.Client:
        return nuheat_api.Client(
            self.transport,
            self._token_provider,
            contracts=(contract,),
            writes_enabled=writes_enabled,
            now=self.now,
        )

    def _mode_name(self, contract: nuheat_api.DeviceContract | None, mode_raw: int | None) -> str | None:
        if contract is None or mode_raw is None:
            return None
        for name, code in contract.mode_codes.items():
            if code == mode_raw:
                return name
        return None

    def status_payload(
        self,
        ident: str,
        contract: nuheat_api.DeviceContract | None,
        state: _DeviceState,
        *,
        ok: bool = True,
        error: str | None = None,
        indeterminate: bool | None = None,
    ) -> dict[str, Any]:
        snap = state.snapshot
        encoding = contract.temperature_encoding if contract is not None else None
        current_now = self.now()
        stale = True if snap is None else snap.stale(current_now, self.stale_after_s)
        temp_raw = snap.temperature_raw if snap is not None else None
        sp_raw = snap.setpoint_raw if snap is not None else None
        mode_raw = snap.mode_raw if snap is not None else None
        payload: dict[str, Any] = {
            "id": ident,
            "commissioned": contract is not None,
            "online": snap.online if snap is not None else None,
            "heating_call": snap.heating_call if snap is not None else None,
            "temperature_raw": temp_raw,
            "temperature_display": decode_temperature(temp_raw, encoding),
            "setpoint_raw": sp_raw,
            "setpoint_display": decode_temperature(sp_raw, encoding),
            "mode": self._mode_name(contract, mode_raw),
            "mode_raw": mode_raw,
            "hold_until": _iso(snap.hold_until) if snap is not None else None,
            "has_error": snap.has_error if snap is not None else None,
            "stale": stale,
            "received_at": _iso(snap.received_at) if snap is not None else None,
            "time_basis": "cloud_receipt",
            "writable_modes": list(contract.writable_modes) if contract is not None else [],
            "min_raw": contract.minimum_raw if contract is not None else None,
            "max_raw": contract.maximum_raw if contract is not None else None,
            "temperature_encoding": encoding,
            "last_write_state": state.last_write_state,
            "ok": ok,
            "error": error if error is not None else state.error,
        }
        if indeterminate is not None:
            payload["indeterminate"] = indeterminate
        return payload

    def _fetch_sync(self, contract: nuheat_api.DeviceContract) -> nuheat_api.Snapshot:
        client = self._client(contract, writes_enabled=False)
        try:
            return client.thermostat(contract.serial)
        except nuheat_api.NuheatError as err:
            if err.code == "unauthorized" and self._token_store is not None and self._token_store.refresh_now():
                return client.thermostat(contract.serial)
            raise

    async def _poll_locked(self, ident: str, contract: nuheat_api.DeviceContract, state: _DeviceState) -> tuple[bool, str | None]:
        state.last_attempt = self.now()
        try:
            snap = await asyncio.to_thread(self._fetch_sync, contract)
        except nuheat_api.NuheatError as err:
            state.error = err.code
            self.log(f"{ident}: poll failed ({err.code})")
            return False, err.code
        state.snapshot = snap
        state.error = None
        if state.last_ack is not None and state.last_write_state in ("acknowledged", "different"):
            comparison = nuheat_api.compare_readback(state.last_ack, snap, contract, self.now(), self.stale_after_s)
            if comparison == "matches":
                state.last_write_state = "confirmed"
            elif comparison == "different":
                state.last_write_state = "different"
        return True, None

    async def get_device(self, ident: str) -> tuple[int, dict[str, Any]]:
        contract = read_contract(self.secrets, ident)
        state = self._state(ident)
        if contract is None:
            return 200, self.status_payload(ident, None, state, ok=True, error="not_commissioned")
        async with state.lock:
            needs_fetch = self.auto_poll and (
                state.snapshot is None
                or state.last_attempt is None
                or (self.now() - state.last_attempt).total_seconds() >= self.poll_interval_s
            )
            if needs_fetch:
                ok, err = await self._poll_locked(ident, contract, state)
                return 200, self.status_payload(ident, contract, state, ok=ok, error=err)
            return 200, self.status_payload(ident, contract, state, ok=state.error is None)

    async def refresh_device(self, ident: str) -> tuple[int, dict[str, Any]]:
        contract = read_contract(self.secrets, ident)
        state = self._state(ident)
        if contract is None:
            return 409, self.status_payload(ident, None, state, ok=False, error="not_commissioned")
        async with state.lock:
            ok, err = await self._poll_locked(ident, contract, state)
            status = 200 if ok else 502
            return status, self.status_payload(ident, contract, state, ok=ok, error=err)

    def _write_sync(self, contract: nuheat_api.DeviceContract, mode: str,
                    setpoint_raw: int | None, hold_until: datetime | None) -> nuheat_api.Acknowledgement:
        client = self._client(contract, writes_enabled=True)
        if mode == "Auto":
            return client.auto(contract.serial)
        if mode == "Manual":
            assert setpoint_raw is not None
            return client.manual(contract.serial, setpoint_raw)
        assert setpoint_raw is not None and hold_until is not None
        return client.hold(contract.serial, setpoint_raw, hold_until)

    async def set_mode(self, ident: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        contract = read_contract(self.secrets, ident)
        state = self._state(ident)
        if contract is None:
            return 409, self.status_payload(ident, None, state, ok=False, error="not_commissioned")
        mode = data.get("mode")
        if not isinstance(mode, str) or mode not in nuheat_api.MODES:
            raise HttpError(400, "mode must be one of Auto, Hold, or Manual.")
        if mode not in contract.writable_modes:
            return 409, self.status_payload(ident, contract, state, ok=False, error="mode_not_writable")

        async with state.lock:
            setpoint_raw: int | None = None
            hold_until: datetime | None = None
            if mode != "Auto":
                if contract.temperature_encoding is None or contract.minimum_raw is None or contract.maximum_raw is None:
                    return 409, self.status_payload(ident, contract, state, ok=False, error="temperature_not_verified")
                if "setpoint_raw" in data and data["setpoint_raw"] is not None:
                    raw = data["setpoint_raw"]
                    if type(raw) is not int:
                        raise HttpError(400, "setpoint_raw must be an integer.")
                    setpoint_raw = raw
                elif state.snapshot is not None and state.snapshot.setpoint_raw is not None:
                    setpoint_raw = state.snapshot.setpoint_raw
                else:
                    raise HttpError(400, "setpoint_raw is required for Manual or Hold mode.")
                if not contract.minimum_raw <= setpoint_raw <= contract.maximum_raw:
                    raise HttpError(400, f"setpoint_raw must be within verified limits ({contract.minimum_raw}..{contract.maximum_raw}).")
            if mode == "Hold":
                if "hold_until" in data and data["hold_until"] is not None:
                    hold_until = _parse_iso(data["hold_until"])
                else:
                    hold_until = self.now() + timedelta(hours=DEFAULT_HOLD_HOURS)
                delta = (hold_until - self.now()).total_seconds()
                if not math.isfinite(delta) or not 0 < delta <= 23 * 3600:
                    raise HttpError(400, "hold_until must be in the future and within 23 hours.")

            try:
                ack = await asyncio.to_thread(self._write_sync, contract, mode, setpoint_raw, hold_until)
            except ValueError as err:
                raise HttpError(400, str(err)) from None
            except nuheat_api.NuheatError as err:
                state.error = err.code
                if err.indeterminate:
                    state.last_ack = None
                    state.last_write_state = "indeterminate"
                    self.log(f"{ident}: write {mode} indeterminate ({err.code}); not retrying")
                    return 502, self.status_payload(
                        ident, contract, state, ok=False, error=err.code, indeterminate=True,
                    )
                self.log(f"{ident}: write {mode} failed ({err.code})")
                if err.code == "unauthorized" and self._token_store is not None:
                    # Refresh for future calls only; never auto-retry a write.
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self._token_store.refresh_now)
                status = 429 if err.code == "rate_limited" else 409 if err.code in ("write_not_configured", "temperature_not_verified") else 400 if err.code == "invalid_request" else 502
                payload = self.status_payload(ident, contract, state, ok=False, error=err.code, indeterminate=False)
                if err.retry_after is not None:
                    payload["retry_after"] = err.retry_after
                return status, payload

            state.last_ack = ack
            state.last_write_state = "acknowledged"
            state.error = None
            self.log(f"{ident}: write {mode} acknowledged")
            return 200, self.status_payload(ident, contract, state, ok=True, error=None, indeterminate=False)

    async def request(self, method: str, target: str, body: bytes) -> tuple[int, dict[str, Any]]:
        split = urllib.parse.urlsplit(target)
        if split.path == "/health":
            if method != "GET":
                raise HttpError(405, f"{method} is not allowed here; use GET.", allow="GET")
            return 200, {"ok": True, "commissioned": len(self._contracts())}
        segments = split.path.split("/")
        if len(segments) not in (3, 4) or segments[:2] != ["", "devices"]:
            raise HttpError(404, f"No such path: {split.path[:100]}")
        ident = urllib.parse.unquote(segments[2])
        if not DEVICE_ID.fullmatch(ident):
            raise HttpError(400, f"The device id {ident[:70]!r} is not valid.")
        if len(segments) == 3:
            if method != "GET":
                raise HttpError(405, f"{method} is not allowed here; use GET.", allow="GET")
            return await self.get_device(ident)
        action = urllib.parse.unquote(segments[3])
        if action not in ("mode", "refresh"):
            raise HttpError(404, f"No such path: {split.path[:100]}")
        if method != "POST":
            raise HttpError(405, f"{method} is not allowed here; use POST.", allow="POST")
        if action == "refresh":
            return await self.refresh_device(ident)
        data = parse_body(body)
        return await self.set_mode(ident, data)

    async def close(self) -> None:
        for writer in list(self._clients):
            with contextlib.suppress(Exception):
                writer.close()


def parse_body(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HttpError(400, "The body is not JSON.") from None
    if not isinstance(data, dict):
        raise HttpError(400, "The body must be a JSON object.")
    return data


async def read_request(reader: asyncio.StreamReader, *, first_timeout: float = READ_TIMEOUT) -> tuple[str, str, bytes, bool]:
    """(method, target, body, keep_alive) of one HTTP/1.x request: Content-Length bodies only."""
    line = await asyncio.wait_for(reader.readline(), first_timeout)
    if not line:
        raise ConnectionError("closed before a request")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HttpError(400, "That is not an HTTP/1.1 request.")
    method, target, version = parts[0], parts[1], parts[2]
    size, length = len(line), 0
    connection_tokens: set[str] = set()
    async with asyncio.timeout(READ_TIMEOUT):
        while True:
            header = await reader.readline()
            size += len(header)
            if size > MAX_HEAD:
                raise HttpError(431, "The request headers are too large.")
            if not header:
                raise ConnectionError("closed in the headers")
            if header in (b"\r\n", b"\n"):
                break
            name, _, value = header.decode("latin-1").partition(":")
            name = name.strip().lower()
            if name == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    raise HttpError(400, "Content-Length is not a number.") from None
                if length < 0:
                    raise HttpError(400, "Content-Length is negative.")
            elif name == "transfer-encoding":
                raise HttpError(400, "Send the body with Content-Length, not Transfer-Encoding.")
            elif name == "connection":
                connection_tokens.update(token.strip().lower() for token in value.split(",") if token.strip())
        if length > MAX_BODY:
            raise HttpError(413, f"The body is larger than {MAX_BODY} bytes.")
        body = await reader.readexactly(length) if length else b""
    keep_alive = ("close" not in connection_tokens) and (
        version == "HTTP/1.1" or "keep-alive" in connection_tokens
    )
    return method, target, body, keep_alive


def response(status: int, body: dict[str, Any], allow: str | None = None, *, keep_alive: bool = False) -> bytes:
    payload = json.dumps(body, allow_nan=False).encode("utf-8")
    head = [
        f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}",
        "Content-Type: application/json",
        f"Content-Length: {len(payload)}",
        "Connection: keep-alive" if keep_alive else "Connection: close",
    ]
    if allow:
        head.append(f"Allow: {allow}")
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload


async def _exchange(service: ClimateService, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    service._clients.add(writer)
    first = True
    try:
        while True:
            allow = None
            keep_alive = False
            try:
                method, target, body, keep_alive = await read_request(
                    reader, first_timeout=READ_TIMEOUT if first else IDLE_TIMEOUT,
                )
            except (ConnectionError, TimeoutError, asyncio.IncompleteReadError):
                return
            except HttpError as error:
                status, answer, keep_alive = error.status, {"ok": False, "error": error.message}, False
            except ValueError:
                status, answer, keep_alive = 431, {"ok": False, "error": "The request line or a header is too long."}, False
            else:
                try:
                    status, answer = await service.request(method, target, body)
                except HttpError as error:
                    status, answer, allow = error.status, {"ok": False, "error": error.message}, error.allow
                except Exception as error:
                    service.log(f"internal error: {type(error).__name__}")
                    status, answer, keep_alive = 500, {"ok": False, "error": f"The helper failed: {type(error).__name__}"}, False
            first = False
            writer.write(response(status, answer, allow, keep_alive=keep_alive))
            await writer.drain()
            if not keep_alive:
                return
    except (ConnectionError, OSError):
        pass
    finally:
        service._clients.discard(writer)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def serve(service: ClimateService, host: str, port: int) -> asyncio.Server:
    """Start the HTTP API on host:port (port 0: any free one)."""
    return await asyncio.start_server(lambda r, w: _exchange(service, r, w), host, port, limit=MAX_HEAD)


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not IP:PORT") from None
    if not sep or not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"{value!r} is not IP:PORT")
    if number:
        try:
            listener("climate", number)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return host, number


async def _run(arguments: argparse.Namespace) -> None:
    host, port = arguments.listen
    service = ClimateService(arguments.secrets)
    server = await serve(service, host, port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)
    service.log(f"listening on {host}:{port}, secrets in {arguments.secrets}")
    try:
        await stop.wait()
    finally:
        service.log("stopping")
        server.close()
        await service.close()


def check_listen(host: str, *, allow_remote: bool = False) -> None:
    """Refuse non-loopback bind addresses unless allow_remote is explicitly set."""
    if not ipaddress.ip_address(host).is_loopback and not allow_remote:
        raise ValueError(
            f"refusing to listen on non-loopback address {host}; the API has no authentication, "
            "so it serves only this host unless --allow-remote is given"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oh_climate.daemon", description=__doc__.splitlines()[0])
    parser.add_argument("--listen", type=parse_listen, default=parse_listen(LISTEN), metavar="HOST:PORT",
                        help=f"address to serve on (default {LISTEN})")
    parser.add_argument("--secrets", type=Path, default=SECRETS, metavar="DIR",
                        help=f"where commissioning contracts and OAuth state are stored (default {SECRETS})")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a non-loopback --listen address: anyone who reaches it can read and "
                             "command commissioned thermostats, as the localhost API has no authentication")
    arguments = parser.parse_args(argv)
    try:
        check_listen(arguments.listen[0], allow_remote=arguments.allow_remote)
    except ValueError as err:
        parser.error(str(err))
    asyncio.run(_run(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
