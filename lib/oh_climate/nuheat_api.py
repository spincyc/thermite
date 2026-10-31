"""Nuheat's documented OpenAPI v2, without OAuth or device commissioning.

Contract: https://api.mynuheat.com/swagger/v2/swagger.json (reviewed 2026-10-31).
The schema does not define temperature scaling or mode integer meanings. Keep
both raw until a per-device contract has been independently verified. A 204 is
an acknowledgement, and a subsequent cloud read is not equipment telemetry.
Native weekly schedule CRUD is absent from v2; v1 Schedule returns 405.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

ORIGIN = "https://api.mynuheat.com"
MAX_BODY = 256 * 1024
INT_MIN, INT_MAX = -(2 ** 31), 2 ** 31 - 1
MODES = frozenset(("Auto", "Hold", "Manual"))


class NuheatError(Exception):
    """Fixed classification only; response bodies, identities and tokens stay out."""

    def __init__(self, code: str, *, status: int | None = None,
                 retry_after: int | None = None, indeterminate: bool = False):
        self.code, self.status = code, status
        self.retry_after, self.indeterminate = retry_after, indeterminate
        super().__init__(code)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class Transport(Protocol):
    def __call__(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes | None) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPS:
    """Fixed-origin HTTPS with verified TLS, no redirects/proxy inheritance.

    Constructing this transport does not open a connection. No retries are
    performed, especially after an ambiguous write. OAuth tokens are supplied
    by a separate approved-client credential service, never through a URL.
    """

    def __init__(self, timeout: float = 10):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("invalid timeout")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, method, path, headers, body):
        valid_path = path in ("/api/v2/Thermostat", "/api/v2/Account") or bool(
            re.fullmatch(r"/api/v2/Thermostat/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", path))
        valid_write = path in {"/api/v2/Mode/" + mode for mode in MODES}
        if not ((method == "GET" and valid_path and body is None) or (method == "PUT" and valid_write)):
            raise ValueError("unapproved endpoint")
        request = urllib.request.Request(ORIGIN + path, data=body, headers=dict(headers), method=method)
        try:
            answer = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            answer = error
        with answer:
            payload = answer.read(MAX_BODY + 1)
            if len(payload) > MAX_BODY:
                raise NuheatError("response_too_large", indeterminate=method == "PUT")
            return Response(answer.code, payload, dict(answer.headers))


def _serial(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("invalid serial number")
    return value


def _integer(value):
    return value if type(value) is int and INT_MIN <= value <= INT_MAX else None


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware time is required")
    return value.astimezone(timezone.utc)


def _date(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return _instant(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _label(value):
    if not isinstance(value, str):
        return None
    return "".join(c for c in value if ord(c) >= 32 and not 127 <= ord(c) <= 159)[:128]


@dataclass(frozen=True)
class Snapshot:
    serial: str = field(repr=False)
    name: str | None
    online: bool | None
    heating_call: bool | None
    temperature_raw: int | None
    setpoint_raw: int | None
    mode_raw: int | None
    hold_until: datetime | None
    has_error: bool | None
    received_at: datetime
    time_basis: str = field(default="cloud_receipt", init=False)

    def stale(self, now: datetime, max_age_seconds: float = 120):
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("invalid maximum age")
        return not 0 <= (_instant(now) - self.received_at).total_seconds() <= max_age_seconds


@dataclass(frozen=True)
class AccountPreferences:
    """Reported display preferences; not proof of API raw-temperature units."""

    temperature_scale: str | None
    language: str | None
    received_at: datetime


@dataclass(frozen=True)
class DeviceContract:
    """Private commissioning facts; no default scale, limits, or mode mapping.

    temperature_encoding identifies an independently verified raw encoding;
    min/max are verified device/installation limits in that SAME raw encoding.
    Merely reading Account.temperatureScale does not establish the encoding.
    mode_codes are optional, independently verified meanings for readback only.
    """

    serial: str = field(repr=False)
    writable_modes: tuple[str, ...] = ()
    temperature_encoding: str | None = None
    minimum_raw: int | None = None
    maximum_raw: int | None = None
    mode_codes: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        _serial(self.serial)
        if any(mode not in MODES for mode in self.writable_modes) or len(set(self.writable_modes)) != len(self.writable_modes):
            raise ValueError("invalid writable modes")
        configured = (self.temperature_encoding is not None, self.minimum_raw is not None, self.maximum_raw is not None)
        if any(configured) and not all(configured):
            raise ValueError("incomplete temperature contract")
        if all(configured) and (not isinstance(self.temperature_encoding, str) or not self.temperature_encoding.strip()
                                or _integer(self.minimum_raw) is None or _integer(self.maximum_raw) is None
                                or self.minimum_raw > self.maximum_raw):
            raise ValueError("invalid temperature contract")
        if any(mode not in MODES or _integer(code) is None for mode, code in self.mode_codes.items()):
            raise ValueError("invalid mode mapping")
        if len(set(self.mode_codes.values())) != len(self.mode_codes):
            raise ValueError("ambiguous mode mapping")
        object.__setattr__(self, "writable_modes", tuple(self.writable_modes))
        object.__setattr__(self, "mode_codes", MappingProxyType(dict(self.mode_codes)))


@dataclass(frozen=True)
class Acknowledgement:
    serial: str = field(repr=False)
    mode: str
    temperature_raw: int | None
    hold_until: datetime | None
    acknowledged_at: datetime
    confirmed: bool = field(default=False, init=False)


def compare_readback(ack: Acknowledgement, snapshot: Snapshot, contract: DeviceContract,
                     now: datetime, max_age_seconds: float = 120):
    """matches/different/unknown for CLOUD readback, never physical confirmation.

    A later fetch receipt is required, but the API has no native acquisition
    timestamp. Even 'matches' cannot establish equipment operation or runtime.
    """
    if (ack.serial != contract.serial or snapshot.serial != contract.serial or snapshot.online is not True
            or snapshot.stale(now, max_age_seconds) or snapshot.received_at <= ack.acknowledged_at
            or ack.mode not in contract.mode_codes or snapshot.mode_raw is None):
        return "unknown"
    if snapshot.mode_raw != contract.mode_codes[ack.mode]:
        return "different"
    if ack.temperature_raw is not None:
        if snapshot.setpoint_raw is None:
            return "unknown"
        if snapshot.setpoint_raw != ack.temperature_raw:
            return "different"
    if ack.hold_until is not None:
        if snapshot.hold_until is None:
            return "unknown"
        if snapshot.hold_until != ack.hold_until:
            return "different"
    return "matches"


class Client:
    """Injected transport/token/clock; writes disabled unless explicitly enabled.

    No login, discovery at construction, poll loop, retry, scheduler or cache.
    Only registered DeviceContracts can be written. OAuth, grants and current
    actor authorization must be enforced by the calling service before a call.
    """

    def __init__(self, transport: Transport, access_token: Callable[[], str], *,
                 contracts=(), writes_enabled: bool = False, now=None):
        if type(writes_enabled) is not bool:
            raise ValueError("invalid write enable")
        self.transport, self.access_token = transport, access_token
        self.now = now or (lambda: datetime.now(timezone.utc))
        contracts = tuple(contracts)
        if any(not isinstance(item, DeviceContract) for item in contracts):
            raise ValueError("invalid device contract")
        self.contracts = {item.serial: item for item in contracts}
        if len(self.contracts) != len(contracts):
            raise ValueError("duplicate device contract")
        self.writes_enabled = writes_enabled

    def _request(self, method, path, payload=None):
        writing = method == "PUT"
        try:
            token = self.access_token()
        except Exception:
            raise NuheatError("authorization_unavailable") from None
        if not isinstance(token, str) or not token or len(token) > 16384 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise NuheatError("authorization_unavailable")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}  # lint: allow (runtime token, never a literal credential)
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        try:
            response = self.transport(method, path, headers, body)
        except Exception as error:
            if isinstance(error, NuheatError) and error.code == "response_too_large":
                raise NuheatError("response_too_large", indeterminate=writing) from None
            raise NuheatError("transport_unavailable", indeterminate=writing) from None
        if not isinstance(response, Response) or type(response.status) is not int or not isinstance(response.body, bytes):
            raise NuheatError("invalid_response", indeterminate=writing)
        if len(response.body) > MAX_BODY:
            raise NuheatError("response_too_large", indeterminate=writing)
        status = response.status
        if status != (204 if writing else 200):
            codes = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                     405: "unsupported", 429: "rate_limited"}
            retry_after = None
            if status == 429:
                value = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
                try:
                    retry_after = int(value) if str(value).isdigit() else math.ceil((parsedate_to_datetime(value) - _instant(self.now())).total_seconds())
                    retry_after = max(0, min(retry_after, 7 * 86400))
                except (TypeError, ValueError, OverflowError):
                    pass
            raise NuheatError(codes.get(status, "remote_failure"), status=status, retry_after=retry_after,
                              indeterminate=writing and (status >= 500 or status not in codes))
        if writing:
            return None
        try:
            return json.loads(response.body)
        except (UnicodeError, ValueError, RecursionError):
            raise NuheatError("invalid_response") from None

    def _snapshot(self, value, at, expected=None):
        try:
            if not isinstance(value, dict):
                raise ValueError()
            serial = _serial(value.get("serialNumber"))
            if expected is not None and serial != expected:
                raise ValueError()
        except ValueError:
            raise NuheatError("invalid_response") from None
        online = value.get("online") if type(value.get("online")) is bool else None
        live = online is True
        error = value.get("errorState")
        return Snapshot(serial, _label(value.get("name")), online,
                        value.get("isHeating") if live and type(value.get("isHeating")) is bool else None,
                        _integer(value.get("currentTemperature")) if live else None,
                        _integer(value.get("setPointTemperature")) if live else None,
                        _integer(value.get("mode")) if live else None,
                        _date(value.get("holdUntil")) if live else None,
                        bool(error) if isinstance(error, str) else False if error is None and "errorState" in value else None,
                        at)

    def thermostats(self):
        """Cloud-account enumeration only when explicitly called; no IP mapping."""
        try:
            value = self._request("GET", "/api/v2/Thermostat")
        except NuheatError as error:
            if error.code == "not_found":  # Documented empty-list response.
                return ()
            raise
        if not isinstance(value, list) or len(value) > 256:
            raise NuheatError("invalid_response")
        at = _instant(self.now())
        snapshots = tuple(self._snapshot(row, at) for row in value)
        if len({row.serial for row in snapshots}) != len(snapshots):
            raise NuheatError("invalid_response")
        return snapshots

    def thermostat(self, serial):
        _serial(serial)
        value = self._request("GET", "/api/v2/Thermostat/" + serial)
        return self._snapshot(value, _instant(self.now()), serial)

    def account_preferences(self):
        value = self._request("GET", "/api/v2/Account")
        if not isinstance(value, dict):
            raise NuheatError("invalid_response")
        return AccountPreferences(_label(value.get("temperatureScale")), _label(value.get("language")),
                                  _instant(self.now()))

    def _write(self, serial, mode, temperature=None, until=None):
        _serial(serial)
        contract = self.contracts.get(serial)
        if not self.writes_enabled or contract is None or mode not in contract.writable_modes:
            raise NuheatError("write_not_configured")
        payload = {"serialNumber": serial}
        if mode != "Auto":
            if contract.temperature_encoding is None:
                raise NuheatError("temperature_not_verified")
            if _integer(temperature) is None or not contract.minimum_raw <= temperature <= contract.maximum_raw:
                raise ValueError("temperature outside verified limits")
            payload.update(temperature=temperature, temperatureType=0)  # Absolute only, never relative replay.
        if mode == "Hold":
            until = _instant(until)
            # Explicit local safety bound, not a claim about v2's undocumented limit.
            if not 0 < (until - _instant(self.now())).total_seconds() <= 23 * 3600:
                raise ValueError("hold must end within 23 hours")
            payload["holdUntil"] = until.isoformat().replace("+00:00", "Z")
        self._request("PUT", "/api/v2/Mode/" + mode, payload)
        return Acknowledgement(serial, mode, temperature, until, _instant(self.now()))

    def auto(self, serial):
        return self._write(serial, "Auto")

    def manual(self, serial, temperature_raw):
        return self._write(serial, "Manual", temperature_raw)

    def hold(self, serial, temperature_raw, until: datetime):
        return self._write(serial, "Hold", temperature_raw, until)
