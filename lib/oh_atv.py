"""The Apple TV helper: a small HTTP service on localhost that drives the
house's Apple TVs for openHAB, which has no Apple TV binding.

openHAB rules call it on the same host; it talks to each Apple TV with pyatv
over the Companion protocol, the one tvOS keeps for remotes, and keeps one
connection per Apple TV open, because a press then takes milliseconds where
a new connection takes about two seconds.

    GET  /devices/<id>?host=<ipv4>          status, from what is cached
    POST /devices/<id>/key    {"host", "key"}     a remote key (KEYS)
    POST /devices/<id>/power  {"host", "on"}      on (true) or standby (false)
    POST /devices/<id>/app    {"host", "app"}     launch an app by bundle id
    POST /devices/<id>/pair   {"host"}            the TV shows a 4-digit code
    POST /devices/<id>/pair   {"host", "code"}    finish pairing with it

Every device answer is the status object with "ok" and "error" added:

    {"id", "host", "paired", "connected", "power": "on"|"off"|"unknown",
     "commanded_power": null|"on"|"off", "power_command_ack_at": null|<epoch ms>,
     "power_reported_at": null|<epoch ms>,
     "pairing": "none"|"code_shown", "ok": true|false, "error": null|"text"}

Power is the Apple TV's native snapshot or pushed report, never inferred
from a command acknowledgement. commanded_power and power_command_ack_at
describe the last acknowledged command separately; power_reported_at is
when this helper received its current native observation, not device time.

On a GET, "ok" is true and "error" is the last problem met for that Apple
TV (null once something later worked); on a POST they describe that
request. A request the helper cannot even attribute to a device (a bad id,
host or body, an unknown path) gets only {"ok": false, "error": ...}. The
error texts are meant to be shown to whoever stands at the TV.

A GET never waits on the network: it answers from memory and, when a paired
Apple TV has no connection, starts connecting in the background (at most
once every reconnect_after seconds), so the next GET shows it connected.

Pairing stores the Companion credential pyatv returns in <secrets>/<id>
(default var/secrets/pyatv/ of this area, directory 0700, file 0600, written
atomically). That credential lets anyone on the LAN control the Apple TV, so
it is never logged or returned, and neither is the pairing code.

The service binds 127.0.0.1 by default and main() refuses any non-loopback
address without --allow-remote: it has no authentication, openHAB runs on
the same host, and an anonymous client on the LAN must not be able to pair
with or drive the Apple TVs through it.

Only the standard library is used here; pyatv is imported in main(), and
Bridge takes the module as an argument so the tests can hand it the fake of
lib/oh_atv_sim.py instead.
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
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from oh_paths import SITE as AREA
SECRETS = AREA / "var/secrets/pyatv"
LISTEN = "127.0.0.1:8099"
#: pyatv RemoteControl methods a key name may call.
KEYS = frozenset({
    "up", "down", "left", "right", "select", "menu", "home", "top_menu", "play_pause", "play", "pause",
    "next", "previous", "skip_forward", "skip_backward", "volume_up", "volume_down"})
DEVICE_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}")
CODE = re.compile(r"[0-9]{4}")
MAX_BODY = 4096
#: The request line and headers together.
MAX_HEAD = 8192
#: How long a client may take to send its request.
READ_TIMEOUT = 10.0

NOT_PAIRED = "Not paired yet. Pair it on the Pairing page."
NO_ANSWER = "The Apple TV did not answer. Is it on and on the network?"
EXPIRED = "The code expired. Ask for a new one."
NO_CODE = "No code was asked for. Ask for a code first."
REJECTED = "The Apple TV did not accept the code. Ask for a new one and try again."


class HttpError(Exception):
    """A request answered with `status` and the plain-English `message`."""

    def __init__(self, status: int, message: str, allow: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.allow = allow


def describe(error: BaseException) -> str:
    """One short line about an exception from pyatv, for errors and the log."""
    if isinstance(error, TimeoutError):
        return "no answer in time"
    return (" ".join(str(error).split()) or type(error).__name__)[:200]


def write_credentials(secrets: Path, ident: str, credentials: str) -> Path:
    """Store one Apple TV's credential: a reader never sees half a file, and
    nobody but this user reads it at all."""
    secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(secrets, 0o700)
    # mkstemp creates the file 0600.
    fd, temp = tempfile.mkstemp(prefix=f".{ident}.", suffix=".tmp", dir=secrets)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credentials + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, secrets / ident)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return secrets / ident


def read_credentials(secrets: Path, ident: str) -> str | None:
    """The stored credential, or None if this Apple TV was never paired."""
    try:
        text = (secrets / ident).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return text or None


def check_host(value: object) -> str:
    if not isinstance(value, str):
        raise HttpError(400, "The host is missing: give the Apple TV's IPv4 address.")
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise HttpError(400, f"The host {value[:64]!r} is not an IPv4 address.") from None


class Device:
    """What the helper knows about one Apple TV, by its device id."""

    def __init__(self, ident: str, host: str) -> None:
        self.id = ident
        self.host = host
        self.atv: Any = None
        #: pyatv holds listeners by weak reference only.
        self.listener: _Listener | None = None
        self.power = "unknown"
        self.commanded_power: str | None = None
        self.power_command_ack_at: int | None = None
        self.power_reported_at: int | None = None
        self.pairing: Any = None
        self.pairing_since = 0.0
        self.error: str | None = None
        #: Serializes connecting, commands and pairing for this Apple TV.
        self.lock = asyncio.Lock()
        self.last_attempt = -math.inf


class _Listener:
    """pyatv's DeviceListener and PowerListener for one connection; callbacks
    from a connection that is no longer the device's are ignored."""

    def __init__(self, bridge: Bridge, device: Device, atv: Any) -> None:
        self.bridge, self.device, self.atv = bridge, device, atv

    def connection_lost(self, exception: Exception) -> None:
        self.bridge._lost(self.device, self.atv, f"connection lost ({describe(exception)})")

    def connection_closed(self) -> None:
        self.bridge._lost(self.device, self.atv, "connection closed")

    def powerstate_update(self, old_state: Any, new_state: Any) -> None:
        self.bridge._pushed_power(self.device, self.atv, new_state)


class Bridge:
    """The Apple TVs behind the HTTP API: their state, connections and pairings.

    Everything runs on one event loop; per-device locks keep one Apple TV's
    connect, commands and pairing in order while different ones go on side by
    side.
    """

    def __init__(self, secrets: Path, pyatv: Any, *, pair_timeout: float = 180.0, connect_timeout: float = 10.0,
                 reconnect_after: float = 15.0, scan_timeout: float = 5.0,
                 log: Callable[[str], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.secrets = Path(secrets)
        self.pyatv = pyatv
        self.pair_timeout = pair_timeout
        self.connect_timeout = connect_timeout
        self.reconnect_after = reconnect_after
        self.scan_timeout = scan_timeout
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.clock = clock
        self.wall_clock = wall_clock
        self.devices: dict[str, Device] = {}
        self._tasks: set[asyncio.Future] = set()

    # -- state ---------------------------------------------------------------

    def _note(self, device: Device, message: str) -> None:
        self.log(f"{device.id}: {message}")

    def paired(self, ident: str) -> bool:
        return read_credentials(self.secrets, ident) is not None

    def status(self, device: Device) -> dict[str, Any]:
        return {"id": device.id, "host": device.host, "paired": self.paired(device.id),
                "connected": device.atv is not None, "power": device.power,
                "commanded_power": device.commanded_power, "power_command_ack_at": device.power_command_ack_at,
                "power_reported_at": device.power_reported_at,
                "pairing": "code_shown" if device.pairing is not None else "none"}

    def _device(self, ident: str, host: str) -> Device:
        device = self.devices.get(ident)
        if device is None:
            device = self.devices[ident] = Device(ident, host)
        elif device.host != host:
            self._note(device, f"address changed from {device.host} to {host}")
            device.host = host
            self._drop(device)
            device.commanded_power = None
            device.power_command_ack_at = None
            device.last_attempt = -math.inf
            handler = self._take_pairing(device)
            if handler is not None:
                self._spawn(self._close_pairing(handler))
        return device

    def _power_word(self, state: Any) -> str:
        states = self.pyatv.const.PowerState
        return "on" if state == states.On else "off" if state == states.Off else "unknown"

    def _read_power(self, atv: Any) -> str:
        try:
            return self._power_word(atv.power.power_state)
        except Exception:
            return "unknown"

    def _spawn(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _drop(self, device: Device) -> None:
        """Forget and close the device's connection, if it has one."""
        atv, device.atv, device.listener = device.atv, None, None
        if atv is None:
            return
        device.power = "unknown"
        device.power_reported_at = None
        try:
            pending = atv.close()
        except Exception:
            return
        # The loop keeps only weak references to tasks.
        for task in pending or ():
            if isinstance(task, asyncio.Future):
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def _lost(self, device: Device, atv: Any, why: str) -> None:
        if device.atv is atv:
            self._note(device, why)
            self._drop(device)

    def _pushed_power(self, device: Device, atv: Any, state: Any) -> None:
        if device.atv is atv:
            word = self._power_word(state)
            device.power = word
            device.power_reported_at = int(self.wall_clock() * 1000)
            self._note(device, f"power {word} (reported by the Apple TV); at_ms={device.power_reported_at}")

    def _take_pairing(self, device: Device) -> Any:
        handler, device.pairing = device.pairing, None
        return handler

    def _pairing_expired(self, device: Device) -> bool:
        return device.pairing is not None and self.clock() - device.pairing_since > self.pair_timeout

    async def _close_pairing(self, handler: Any) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler.close(), self.connect_timeout)

    # -- connecting ----------------------------------------------------------

    async def _scan(self, host: str) -> Any:
        found = await self.pyatv.scan(asyncio.get_running_loop(), hosts=[host], timeout=self.scan_timeout)
        if not found:
            raise HttpError(502, NO_ANSWER)
        return found[0]

    async def _open(self, host: str, credentials: str) -> Any:
        config = await self._scan(host)
        config.set_credentials(self.pyatv.const.Protocol.Companion, credentials)
        return await self.pyatv.connect(config, asyncio.get_running_loop())

    async def _connect(self, device: Device) -> None:
        """Open the device's connection; the caller holds its lock."""
        device.last_attempt = self.clock()
        credentials = read_credentials(self.secrets, device.id)
        if credentials is None:
            raise HttpError(409, NOT_PAIRED)
        try:
            atv = await asyncio.wait_for(self._open(device.host, credentials), self.connect_timeout)
        except HttpError as error:
            self._note(device, f"could not connect to {device.host}: {error.message}")
            raise
        except TimeoutError:
            self._note(device, f"could not connect to {device.host}: no answer in time")
            raise HttpError(502, NO_ANSWER) from None
        except Exception as error:
            reason = describe(error).replace(credentials, "[hidden]")
            self._note(device, f"could not connect to {device.host}: {reason}")
            raise HttpError(502, f"Could not connect to the Apple TV: {reason}") from None
        listener = _Listener(self, device, atv)
        atv.listener = listener
        with contextlib.suppress(Exception):
            atv.power.listener = listener
        device.atv, device.listener = atv, listener
        device.power = self._read_power(atv)
        device.power_reported_at = int(self.wall_clock() * 1000) if device.power != "unknown" else None
        device.error = None
        self._note(device, f"connected to {device.host}, power {device.power}")

    async def _connect_in_background(self, device: Device) -> None:
        async with device.lock:
            if device.atv is not None or not self.paired(device.id):
                return
            try:
                await self._connect(device)
            except HttpError as error:
                device.error = error.message

    async def _run(self, device: Device, what: str, action: Callable[[Any], Awaitable[Any]]) -> None:
        """Connect before dispatch, then attempt the action once.

        A timeout or lost reply can follow an accepted action. Replaying a
        toggle or Next would undo or repeat it, so only a new explicit request
        may reconnect and send again after an ambiguous command failure.
        """
        if not self.paired(device.id):
            raise HttpError(409, NOT_PAIRED)
        if device.atv is None:
            await self._connect(device)
        atv = device.atv
        try:
            await asyncio.wait_for(action(atv), self.connect_timeout)
        except Exception as error:
            if device.atv is atv:
                self._drop(device)
            # Protocol exception text can quote private response fields. The
            # bounded class/time classification is enough for this outcome.
            reason = "no answer in time" if isinstance(error, TimeoutError) else type(error).__name__
            self._note(device, f"{what} not confirmed ({reason}); not replaying")
            raise HttpError(502, "The Apple TV did not confirm the command; it may have been applied. "
                                "Check the device before trying again.") from None

    # -- the API's actions ---------------------------------------------------

    def look(self, ident: str, host: str) -> dict[str, Any]:
        """GET: the cached status; may start a background connect, never waits."""
        device = self._device(ident, host)
        if not device.lock.locked() and self._pairing_expired(device):
            self._spawn(self._close_pairing(self._take_pairing(device)))
            device.error = EXPIRED
            self._note(device, "the pairing code expired")
        if (device.atv is None and not device.lock.locked()
                and self.clock() - device.last_attempt > self.reconnect_after and self.paired(ident)):
            device.last_attempt = self.clock()
            self._spawn(self._connect_in_background(device))
        return self.status(device) | {"ok": True, "error": device.error}

    async def press(self, device: Device, key: str) -> None:
        async with device.lock:
            await self._run(device, f"key {key}", lambda atv: getattr(atv.remote_control, key)())
        self._note(device, f"key {key}")

    async def set_power(self, device: Device, on: bool) -> None:
        word = "on" if on else "off"
        # Includes time queued for this device's lock and any reconnect.
        started = self.clock()
        self._note(device, f"power {word} command start; at_ms={int(self.wall_clock() * 1000)}")
        async with device.lock:
            await self._run(device, f"power {word}",
                            lambda atv: atv.power.turn_on() if on else atv.power.turn_off())
            # An acknowledged Sleep/Wake request is not a standby/wake report.
            # A native callback may already have arrived while awaiting it.
            device.commanded_power = word
            device.power_command_ack_at = int(self.wall_clock() * 1000)
            elapsed = max(0, round((self.clock() - started) * 1000))
            self._note(device, f"power {word} command acknowledged; at_ms={device.power_command_ack_at}; "
                               f"elapsed_ms={elapsed}")

    async def launch(self, device: Device, app: str) -> None:
        async with device.lock:
            await self._run(device, f"app {app}", lambda atv: atv.apps.launch_app(app))
        self._note(device, f"app {app}")

    async def pair_begin(self, device: Device) -> None:
        async with device.lock:
            old = self._take_pairing(device)
            if old is not None:
                await self._close_pairing(old)
            handler = None
            try:
                config = await asyncio.wait_for(self._scan(device.host), self.connect_timeout)
                handler = await asyncio.wait_for(
                    self.pyatv.pair(config, self.pyatv.const.Protocol.Companion, asyncio.get_running_loop(), name="thermite"),
                    self.connect_timeout)
                await asyncio.wait_for(handler.begin(), self.connect_timeout)
            except Exception as error:
                if handler is not None:
                    await self._close_pairing(handler)
                if isinstance(error, HttpError):
                    message = error.message
                elif isinstance(error, TimeoutError):
                    message = NO_ANSWER
                else:
                    message = f"The Apple TV did not start pairing: {describe(error)}"
                self._note(device, f"pairing could not begin: {message}")
                raise HttpError(502, message) from None
            device.pairing = handler
            device.pairing_since = self.clock()
            self._note(device, "pairing begun; the Apple TV shows a code")

    async def pair_finish(self, device: Device, code: str) -> None:
        async with device.lock:
            if self._pairing_expired(device):
                await self._close_pairing(self._take_pairing(device))
                self._note(device, "the pairing code expired")
                raise HttpError(409, EXPIRED)
            handler = self._take_pairing(device)
            if handler is None:
                raise HttpError(409, NO_CODE)
            credentials = None
            reason = "code not accepted"
            try:
                handler.pin(int(code))
                await asyncio.wait_for(handler.finish(), self.connect_timeout)
                if handler.has_paired:
                    credentials = handler.service.credentials
            except Exception as error:
                # The type only: pyatv's text could quote what was sent.
                reason = f"code not accepted ({type(error).__name__})"
            finally:
                await self._close_pairing(handler)
            if not isinstance(credentials, str) or not credentials.strip():
                self._note(device, f"pairing failed: {reason}")
                raise HttpError(422, REJECTED)
            try:
                write_credentials(self.secrets, device.id, credentials.strip())
            except OSError as error:
                self._note(device, f"pairing succeeded but could not be stored: {error.strerror}")
                raise HttpError(500, f"Paired, but the helper could not store the pairing: {error.strerror}. "
                                     "Fix that and pair again.") from None
            # The open connection, if any, was made with the old credential.
            self._drop(device)
            device.last_attempt = self.clock()
            self._note(device, "pairing succeeded; stored")
        self._spawn(self._connect_in_background(device))

    async def close(self) -> None:
        """Close every connection and pairing and stop background work."""
        for device in self.devices.values():
            handler = self._take_pairing(device)
            if handler is not None:
                await self._close_pairing(handler)
            self._drop(device)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- HTTP ----------------------------------------------------------------

    async def request(self, method: str, target: str, body: bytes) -> tuple[int, dict[str, Any]]:
        """(HTTP status, JSON answer) for one request."""
        split = urllib.parse.urlsplit(target)
        segments = split.path.split("/")
        if len(segments) not in (3, 4) or segments[:2] != ["", "devices"]:
            raise HttpError(404, f"No such path: {split.path[:100]}")
        ident = urllib.parse.unquote(segments[2])
        if not DEVICE_ID.fullmatch(ident):
            raise HttpError(400, f"The device id {ident[:70]!r} is not lowercase letters, digits and _ "
                                 "starting with a letter (at most 64).")
        if len(segments) == 3:
            if method != "GET":
                raise HttpError(405, f"{method} is not allowed here; use GET.", allow="GET")
            query = urllib.parse.parse_qs(split.query)
            return 200, self.look(ident, check_host((query.get("host") or [None])[-1]))
        action = urllib.parse.unquote(segments[3])
        if action not in ("key", "power", "app", "pair"):
            raise HttpError(404, f"No such path: {split.path[:100]}")
        if method != "POST":
            raise HttpError(405, f"{method} is not allowed here; use POST.", allow="POST")
        data = parse_body(body)
        host = check_host(data.get("host"))
        run = self._action(action, data)
        device = self._device(ident, host)
        try:
            await run(device)
        except HttpError as error:
            device.error = error.message
            return error.status, self.status(device) | {"ok": False, "error": error.message}
        device.error = None
        return 200, self.status(device) | {"ok": True, "error": None}

    def _action(self, action: str, data: dict[str, Any]) -> Callable[[Device], Awaitable[None]]:
        """The validated call one POST makes; HttpError 400 on bad fields."""
        if action == "key":
            key = data.get("key")
            if not isinstance(key, str) or key not in KEYS:
                raise HttpError(400, f"Unknown key {str(key)[:40]!r}; known keys: {', '.join(sorted(KEYS))}.")
            return lambda device: self.press(device, key)
        if action == "power":
            on = data.get("on")
            if not isinstance(on, bool):
                raise HttpError(400, "Give \"on\": true or false.")
            return lambda device: self.set_power(device, on)
        if action == "app":
            app = data.get("app")
            if not isinstance(app, str) or not BUNDLE_ID.fullmatch(app):
                raise HttpError(400, "Give \"app\" as a bundle id such as com.netflix.Netflix.")
            return lambda device: self.launch(device, app)
        if "code" not in data:
            return self.pair_begin
        code = data["code"]
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise HttpError(400, "The code is the 4 digits the Apple TV shows.")
        return lambda device: self.pair_finish(device, code)


def parse_body(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HttpError(400, "The body is not JSON.") from None
    if not isinstance(data, dict):
        raise HttpError(400, "The body must be a JSON object.")
    return data


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, bytes]:
    """(method, target, body) of one HTTP/1.x request: Content-Length bodies only."""
    line = await reader.readline()
    if not line:
        raise ConnectionError("closed before a request")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HttpError(400, "That is not an HTTP/1.1 request.")
    method, target = parts[0], parts[1]
    size, length = len(line), 0
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
    if length > MAX_BODY:
        raise HttpError(413, f"The body is larger than {MAX_BODY} bytes.")
    return method, target, await reader.readexactly(length) if length else b""


def response(status: int, body: dict[str, Any], allow: str | None = None) -> bytes:
    payload = json.dumps(body).encode("utf-8")
    head = [f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}", "Content-Type: application/json",
            f"Content-Length: {len(payload)}", "Connection: close"]
    if allow:
        head.append(f"Allow: {allow}")
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload


async def _exchange(bridge: Bridge, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    allow = None
    try:
        try:
            method, target, body = await asyncio.wait_for(read_request(reader), READ_TIMEOUT)
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError):
            return
        except HttpError as error:
            status, answer = error.status, {"ok": False, "error": error.message}
        except ValueError:
            # StreamReader.readline past its limit.
            status, answer = 431, {"ok": False, "error": "The request line or a header is too long."}
        else:
            try:
                status, answer = await bridge.request(method, target, body)
            except HttpError as error:
                status, answer, allow = error.status, {"ok": False, "error": error.message}, error.allow
            except Exception as error:
                bridge.log(f"internal error: {type(error).__name__}: {describe(error)}")
                status, answer = 500, {"ok": False, "error": f"The helper failed: {type(error).__name__}"}
        writer.write(response(status, answer, allow))
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def serve(bridge: Bridge, host: str, port: int) -> asyncio.Server:
    """Start the HTTP API on host:port (port 0: any free one)."""
    return await asyncio.start_server(lambda r, w: _exchange(bridge, r, w), host, port, limit=MAX_HEAD)


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
    return host, number


async def _run(arguments: argparse.Namespace, pyatv: Any) -> None:
    host, port = arguments.listen
    bridge = Bridge(arguments.secrets, pyatv)
    server = await serve(bridge, host, port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)
    bridge.log(f"listening on {host}:{port}, pairings in {arguments.secrets}")
    try:
        await stop.wait()
    finally:
        bridge.log("stopping")
        server.close()
        await bridge.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oh_atv", description=__doc__.splitlines()[0])
    parser.add_argument("--listen", type=parse_listen, default=parse_listen(LISTEN), metavar="HOST:PORT",
                        help=f"address to serve on (default {LISTEN})")
    parser.add_argument("--secrets", type=Path, default=SECRETS, metavar="DIR",
                        help=f"where pairings are stored (default {SECRETS})")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a non-loopback --listen address: anyone who reaches it can pair with and "
                             "drive the Apple TVs, as the API has no authentication")
    arguments = parser.parse_args(argv)
    if not ipaddress.ip_address(arguments.listen[0]).is_loopback and not arguments.allow_remote:
        parser.error(f"{arguments.listen[0]} is not a loopback address; the API has no authentication, so "
                     "it serves only this host unless --allow-remote is given")
    import pyatv
    import pyatv.const  # noqa: F401

    asyncio.run(_run(arguments, pyatv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
