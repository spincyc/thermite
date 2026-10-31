"""A stand-in for pyatv and the Apple TVs it drives, for the tests of
lib/oh_atv.py and for end-to-end checks of the openHAB side.

FakePyatv is a module-like object offering what oh_atv.Bridge uses of pyatv
0.18.0: scan, connect, pair and const.Protocol / const.PowerState. It maps
each host to an AppleTV (or answers for every host with one). Like pyatv it
holds listeners by weak reference and calls them on the event loop the
connection was made on, also when the change comes from another thread (a
test calling drop() or set_power()).

AppleTV records every key press, app launch and power command; it can be
offline (scan finds nothing, connect and commands fail), refuse to show a
code, drop its connections as the real one does after a while, and turn
itself on or off, reported to every connection as tvOS pushes it. Pairing
shows a 4-digit code (code_shown) after begin; the right code makes it trust
a new random credential, and connect refuses any credential it does not
trust.

Server runs the real oh_atv.Bridge and oh_atv.serve on that fake, on its own
event loop in a background thread, on 127.0.0.1 and an ephemeral port. Its
`paired` argument pre-seeds the secrets directory with device id ->
credential, trusted by every stand-in, so an Apple TV can start out paired.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import random
import tempfile
import threading
import types
import weakref
from pathlib import Path
from typing import Any

import oh_atv as atv


class Protocol(enum.Enum):
    """pyatv.const.Protocol."""

    DMAP = 1
    MRP = 2
    AirPlay = 3
    Companion = 4
    RAOP = 5


class PowerState(enum.Enum):
    """pyatv.const.PowerState."""

    Unknown = 0
    Off = 1
    On = 2


class AppleTV:
    """State and records of one stand-in Apple TV; safe to read and change
    from any thread."""

    def __init__(self, *, power: bool = True, online: bool = True, accepts_pairing: bool = True,
                 code: str | None = None, name: str = "Apple TV",
                 apps: dict[str, str] | None = None) -> None:
        self.lock = threading.Lock()
        self.name = name
        self.power = power
        #: False: scan finds nothing, connect and commands fail.
        self.online = online
        #: False: begin fails as when tvOS stops showing codes after failed attempts.
        self.accepts_pairing = accepts_pairing
        #: The code the next pairing shows; None for a random one.
        self.code = code
        #: The code on screen now, or None.
        self.code_shown: str | None = None
        #: Credentials it accepts on connect.
        self.credentials: set[str] = set()
        #: How many coming commands fail, as on a connection the TV dropped silently.
        self.fail_next = 0
        self.apps = dict(apps or {"com.apple.TVWatchList": "TV", "com.netflix.Netflix": "Netflix"})
        self._presses: list[str] = []
        self._apps_launched: list[str] = []
        self._power_commands: list[str] = []
        self._commands: list[str] = []
        self._connections: list[_Connection] = []
        self._pairing: _Pairing | None = None

    @property
    def presses(self) -> list[str]:
        """Every key sent, by its pyatv name, in order."""
        with self.lock:
            return list(self._presses)

    @property
    def apps_launched(self) -> list[str]:
        with self.lock:
            return list(self._apps_launched)

    @property
    def power_commands(self) -> list[str]:
        """"on" or "off" for every turn_on/turn_off, in order."""
        with self.lock:
            return list(self._power_commands)

    @property
    def commands(self) -> list[str]:
        """"key <name>", "app <bundle id>" or "power on|off" for every command, in order."""
        with self.lock:
            return list(self._commands)

    @property
    def connections(self) -> int:
        """How many connections are open."""
        with self.lock:
            return len(self._connections)

    def drop(self) -> None:
        """Close every connection from the Apple TV's side."""
        with self.lock:
            dropped, self._connections = self._connections, []
        for connection in dropped:
            connection.lose(ConnectionResetError("the Apple TV closed the connection"))

    def set_power(self, on: bool) -> None:
        """Turn it on or off, as its own remote would, and report it."""
        with self.lock:
            old, self.power = self.power, on
            connections = list(self._connections)
        for connection in connections:
            connection.notify_power(old, on)

    def _command(self, connection: _Connection) -> None:
        with self.lock:
            if connection.closed or connection not in self._connections or not self.online:
                raise ConnectionResetError("not connected")
            if self.fail_next > 0:
                self.fail_next -= 1
                raise ConnectionResetError("the command went unanswered")


def _state(on: bool) -> PowerState:
    return PowerState.On if on else PowerState.Off


class _Listened:
    """pyatv's StateProducer: one listener, held by weak reference."""

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self._listener: weakref.ReferenceType | None = None

    @property
    def listener(self) -> Any:
        return self._listener() if self._listener is not None else None

    @listener.setter
    def listener(self, target: Any) -> None:
        self._listener = weakref.ref(target) if target is not None else None

    def deliver(self, name: str, *args: Any) -> None:
        def call() -> None:
            listener = self.listener
            if listener is not None:
                getattr(listener, name)(*args)

        with contextlib.suppress(RuntimeError):
            self._connection.loop.call_soon_threadsafe(call)


class _Remote:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        if name not in atv.KEYS:
            raise AttributeError(name)

        async def press(*args: Any, **kwargs: Any) -> None:
            tv = self._connection.tv
            tv._command(self._connection)
            with tv.lock:
                tv._presses.append(name)
                tv._commands.append(f"key {name}")

        return press


class _Power(_Listened):
    @property
    def power_state(self) -> PowerState:
        return _state(self._connection.tv.power)

    async def turn_on(self, await_new_state: bool = False) -> None:
        await self._turn(True)

    async def turn_off(self, await_new_state: bool = False) -> None:
        await self._turn(False)

    async def _turn(self, on: bool) -> None:
        tv = self._connection.tv
        tv._command(self._connection)
        with tv.lock:
            tv._power_commands.append("on" if on else "off")
            tv._commands.append("power " + ("on" if on else "off"))
        tv.set_power(on)


class _Apps:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def app_list(self) -> list[Any]:
        tv = self._connection.tv
        tv._command(self._connection)
        return [types.SimpleNamespace(name=name, identifier=ident) for ident, name in tv.apps.items()]

    async def launch_app(self, bundle_id_or_url: str) -> None:
        tv = self._connection.tv
        tv._command(self._connection)
        with tv.lock:
            tv._apps_launched.append(bundle_id_or_url)
            tv._commands.append(f"app {bundle_id_or_url}")


class _Connection(_Listened):
    """pyatv's AppleTV interface object for one connection."""

    def __init__(self, tv: AppleTV, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(self)
        self.tv = tv
        self.loop = loop
        self.closed = False
        self.remote_control = _Remote(self)
        self.power = _Power(self)
        self.apps = _Apps(self)

    def lose(self, error: Exception) -> None:
        self.closed = True
        self.deliver("connection_lost", error)

    def notify_power(self, old: bool, new: bool) -> None:
        self.power.deliver("powerstate_update", _state(old), _state(new))

    def close(self) -> set[asyncio.Task]:
        if not self.closed:
            self.closed = True
            with self.tv.lock:
                if self in self.tv._connections:
                    self.tv._connections.remove(self)
            self.deliver("connection_closed")
        return set()


class _Config:
    """pyatv's BaseConfig, as scan returns it."""

    def __init__(self, host: str, tv: AppleTV) -> None:
        self.address = host
        self.name = tv.name
        self.identifier = f"stand-in-{host}"
        self.tv = tv
        self.credentials: dict[Protocol, str] = {}

    def set_credentials(self, protocol: Protocol, credentials: str) -> bool:
        self.credentials[protocol] = credentials
        return True


class _Pairing:
    """pyatv's PairingHandler for the Companion protocol."""

    def __init__(self, tv: AppleTV) -> None:
        self.tv = tv
        self.service = types.SimpleNamespace(credentials=None)
        self.has_paired = False
        self._pin: int | None = None
        self._closed = False

    async def begin(self) -> None:
        tv = self.tv
        with tv.lock:
            if not tv.online:
                raise ConnectionRefusedError("the Apple TV did not answer")
            if not tv.accepts_pairing:
                raise RuntimeError("pairing was refused")
            tv.code_shown = tv.code if tv.code is not None else f"{random.randrange(10000):04d}"
            tv._pairing = self

    def pin(self, pin: int) -> None:
        self._pin = pin

    async def finish(self) -> None:
        tv = self.tv
        with tv.lock:
            shown = tv.code_shown if tv._pairing is self else None
            if tv._pairing is self:
                tv.code_shown, tv._pairing = None, None
            if shown is None or self._pin is None or f"{self._pin:04d}" != shown:
                raise RuntimeError("pairing failed")
            issued = os.urandom(32).hex()
            tv.credentials.add(issued)
        self.service.credentials = issued
        self.has_paired = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self.tv.lock:
            if self.tv._pairing is self:
                self.tv.code_shown, self.tv._pairing = None, None


class FakePyatv:
    """The pyatv module, as far as oh_atv uses it, over stand-in Apple TVs."""

    const = types.SimpleNamespace(Protocol=Protocol, PowerState=PowerState)
    Protocol = Protocol
    PowerState = PowerState

    def __init__(self, tvs: dict[str, AppleTV] | AppleTV, *, offline_scan_s: float = 0.0) -> None:
        self.tvs = tvs
        #: How long a scan that finds nothing takes (pyatv waits its timeout).
        self.offline_scan_s = offline_scan_s
        self.scans = 0

    def tv_for(self, host: str) -> AppleTV | None:
        return self.tvs if isinstance(self.tvs, AppleTV) else self.tvs.get(host)

    async def scan(self, loop: asyncio.AbstractEventLoop, timeout: float = 5, hosts: list[str] | None = None,
                   **_: Any) -> list[_Config]:
        self.scans += 1
        found = []
        for host in hosts or []:
            tv = self.tv_for(host)
            if tv is not None and tv.online:
                found.append(_Config(host, tv))
        if not found and self.offline_scan_s:
            await asyncio.sleep(min(timeout, self.offline_scan_s))
        return found

    async def connect(self, config: _Config, loop: asyncio.AbstractEventLoop, **_: Any) -> _Connection:
        tv = config.tv
        offered = config.credentials.get(Protocol.Companion)
        with tv.lock:
            if not tv.online:
                raise ConnectionRefusedError("the Apple TV did not answer")
            if offered not in tv.credentials:
                raise PermissionError("the Apple TV refused the pairing")
            connection = _Connection(tv, loop)
            tv._connections.append(connection)
        return connection

    async def pair(self, config: _Config, protocol: Protocol, loop: asyncio.AbstractEventLoop,
                   **_: Any) -> _Pairing:
        if protocol is not Protocol.Companion:
            raise NotImplementedError(f"the stand-in pairs only Companion, not {protocol}")
        return _Pairing(config.tv)


class Server:
    """The real helper on 127.0.0.1 over FakePyatv, in a background thread."""

    def __init__(self, tvs: dict[str, AppleTV] | AppleTV, secrets: Path | None = None,
                 paired: dict[str, str] | None = None, *, log: Any = None, offline_scan_s: float = 0.0,
                 **options: Any) -> None:
        self.tvs = tvs
        self.pyatv = FakePyatv(tvs, offline_scan_s=offline_scan_s)
        self._temp = tempfile.TemporaryDirectory(prefix="oh-atv-") if secrets is None else None
        self.secrets = Path(self._temp.name) / "pyatv" if self._temp else Path(secrets)
        for ident, issued in (paired or {}).items():
            atv.write_credentials(self.secrets, ident, issued)
            for tv in [tvs] if isinstance(tvs, AppleTV) else tvs.values():
                with tv.lock:
                    tv.credentials.add(issued)
        self._log = log
        self._lines: list[str] = []
        self._lines_lock = threading.Lock()
        self._closed = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="oh-atv-sim", daemon=True)
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start(options), self.loop).result(10)
        except BaseException:
            self.close()
            raise

    async def _start(self, options: dict[str, Any]) -> None:
        self.bridge = atv.Bridge(self.secrets, self.pyatv, log=self._record, **options)
        self.server = await atv.serve(self.bridge, "127.0.0.1", 0)

    def _record(self, line: str) -> None:
        with self._lines_lock:
            self._lines.append(line)
        if self._log is not None:
            self._log(line)

    def lines(self) -> list[str]:
        """Every line the helper logged, in order."""
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
        bridge = getattr(self, "bridge", None)
        if bridge is not None:
            await bridge.close()

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
        """Nothing: it serves from construction on. For callers that start every stand-in."""
        return self

    def stop(self) -> None:
        self.close()

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
