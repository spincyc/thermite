"""A stand-in for a Google TV's remote service (oh_integrations.androidtv_googletv),
for bin/thermite test controls and the tests.

It speaks the Android TV Remote protocol as a Sony BRAVIA does on TLS port
6466, as far as the androidtv binding uses it: every message, each way, is
one length byte and a protobuf message. It serves a throwaway self-signed
certificate (made with the openssl command) and asks for no client
certificate, so it is a TV the binding is already paired with; pairing
(port 6467) is not played. On a connection it sends its device info; to the
client's answer, its go-ahead; to the client's acknowledgement, its power,
volume and mute, and the app on screen. Then it takes key presses
(RemoteKeyInject, field 10) and app links (RemoteAppLinkLaunchRequest,
field 90): POWER toggles the power, MUTE the mute, VOLUME_UP and
VOLUME_DOWN step the volume one unit within 0-max_volume (leaving mute as it
is), each reported at once to every connection, as the TV does; any other
key is only recorded. It records every key and link, and can be told to
close every connection unanswered (silent).
"""

from __future__ import annotations

import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

PORT = 6466
POWER = 26
MUTE = 91
VOLUME_UP = 24
VOLUME_DOWN = 25
#: RemoteMessage fields, as their protobuf keys.
CONFIGURE = b"\x0a"
SET_ACTIVE = b"\x12"
KEY_INJECT = b"\x52"
PING_RESPONSE = b"\x4a"
APP_LINK = b"\xd2\x05"
#: How often a connection looks for reports from another one, in seconds.
POLL_S = 0.02


def _field(key: bytes, payload: bytes) -> bytes:
    if len(payload) > 127:
        raise ValueError(f"{len(payload)} bytes is too long for a one-byte length")
    return key + bytes([len(payload)]) + payload


def varint(data: bytes, at: int) -> tuple[int, int]:
    """(value, index after it) of the protobuf varint at data[at]."""
    value, shift = 0, 0
    while True:
        byte = data[at]
        value |= (byte & 0x7F) << shift
        at += 1
        if byte < 0x80:
            return value, at
        shift += 7


def device_info(model: str, manufacturer: str) -> bytes:
    """RemoteConfigure, as a Sony BRAVIA sends it first."""
    info = (_field(b"\x0a", model.encode()) + _field(b"\x12", manufacturer.encode()) + b"\x18\x01"
            + _field(b"\x22", b"9") + _field(b"\x2a", b"com.google.android.tv.remote.service")
            + _field(b"\x32", b"5.2.473254133"))
    return _field(CONFIGURE, b"\x08\xff\x04" + _field(b"\x12", info))


def volume_report(model: str, max_volume: int, volume: int, muted: bool) -> bytes:
    """RemoteSetVolumeLevel: the TV's own speakers (02), its scale, level and mute."""
    body = (b"\x08\x03\x10\x06" + _field(b"\x1a", model.encode()) + b"\x20\x02\x28\x00"
            + bytes([0x30, max_volume, 0x38, volume, 0x40, int(muted)]))
    return _field(b"\x92\x03", body)


def power_report(on: bool) -> bytes:
    """RemoteStart: whether the screen is on."""
    return _field(b"\xc2\x02", b"\x08" + bytes([int(on)]))


def app_report(app: str) -> bytes:
    """RemoteImeKeyInject: the app on screen."""
    return _field(b"\xa2\x01", _field(b"\x0a", _field(b"\x62", app.encode())))


def key_message(code: int) -> bytes:
    """RemoteKeyInject of a short press, as a client sends it."""
    out = bytearray()
    value = code
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return _field(KEY_INJECT, b"\x08" + bytes(out) + b"\x10\x03")


def app_link_message(link: str) -> bytes:
    """RemoteAppLinkLaunchRequest, as a client sends it."""
    return _field(APP_LINK, _field(b"\x0a", link.encode()))


class TV:
    """State, reports and the press log of one stand-in TV."""

    def __init__(self, *, power: bool = True, volume: int = 12, muted: bool = False, max_volume: int = 100,
                 app: str = "com.google.android.tvlauncher", model: str = "BRAVIA 4K GB",
                 manufacturer: str = "Sony") -> None:
        self.power = power
        self.volume = volume
        self.muted = muted
        self.max_volume = max_volume
        self.app = app
        self.model = model
        self.manufacturer = manufacturer
        #: When set, every connection is closed unanswered.
        self.silent = False
        self.lock = threading.Lock()
        self._presses: list[tuple[str, int | str]] = []
        #: Messages it does not know, as hex.
        self.unknown: list[str] = []
        #: Open connection -> the reports waiting to be sent on it.
        self._outboxes: dict[object, list[bytes]] = {}

    def presses(self) -> list[tuple[str, int | str]]:
        """("key", key code) or ("link", app link) of every press, in order."""
        with self.lock:
            return list(self._presses)

    def attach(self, connection: object) -> None:
        with self.lock:
            self._outboxes[connection] = [device_info(self.model, self.manufacturer)]

    def detach(self, connection: object) -> None:
        with self.lock:
            self._outboxes.pop(connection, None)

    def outbox(self, connection: object) -> list[bytes]:
        """The reports waiting for this connection, taken."""
        with self.lock:
            waiting = self._outboxes.get(connection) or []
            if connection in self._outboxes:
                self._outboxes[connection] = []
            return waiting

    def _volume(self) -> bytes:
        return volume_report(self.model, self.max_volume, self.volume, self.muted)

    def receive(self, connection: object, message: bytes) -> None:
        """One message from a client: its answer goes to that connection, a
        change to every connection."""
        with self.lock:
            if message.startswith(CONFIGURE):
                self._outboxes[connection].append(SET_ACTIVE + b"\x00")
            elif message.startswith(SET_ACTIVE):
                self._outboxes[connection] += [power_report(self.power), self._volume(), app_report(self.app)]
            elif message.startswith(KEY_INJECT) and len(message) > 3 and message[2] == 0x08:
                code, _ = varint(message, 3)
                self._presses.append(("key", code))
                self._press(code)
            elif message.startswith(APP_LINK) and len(message) > 4 and message[3:4] == b"\x0a":
                link = message[5:5 + message[4]].decode("utf-8", "replace")
                self._presses.append(("link", link))
            elif not message.startswith(PING_RESPONSE):
                self.unknown.append(message.hex())

    def _press(self, code: int) -> None:
        if code == POWER:
            self.power = not self.power
            report = power_report(self.power)
        elif code == MUTE:
            self.muted = not self.muted
            report = self._volume()
        elif code in (VOLUME_UP, VOLUME_DOWN):
            self.volume = min(max(self.volume + (1 if code == VOLUME_UP else -1), 0), self.max_volume)
            report = self._volume()
        else:
            return
        for outbox in self._outboxes.values():
            outbox.append(report)

    def set_power(self, on: bool) -> None:
        """Turn it on or off, as its own remote would, and report it."""
        with self.lock:
            self.power = on
            for outbox in self._outboxes.values():
                outbox.append(power_report(on))


#: The stand-in for each integration, by its NAME.
SIMS = {"androidtv_googletv": TV}


def _context() -> ssl.SSLContext:
    """A server context with a fresh self-signed certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as tmp:
        key, cert = Path(tmp) / "key.pem", Path(tmp) / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
                        "-subj", "/CN=Google TV stand-in"], check=True, capture_output=True)
        context.load_cert_chain(cert, key)
    return context


class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        tv, connection = self.server.tv, self.request
        connection.settimeout(5)
        try:
            connection.do_handshake()
        except (OSError, ssl.SSLError):
            return
        if tv.silent:
            return
        tv.attach(connection)
        try:
            self._serve(tv, connection)
        except (OSError, ssl.SSLError):
            pass
        finally:
            tv.detach(connection)

    def _serve(self, tv: TV, connection: ssl.SSLSocket) -> None:
        connection.settimeout(POLL_S)
        buffer = bytearray()
        while not (tv.silent or self.server.closing):
            for report in tv.outbox(connection):
                connection.sendall(bytes([len(report)]) + report)
            try:
                chunk = connection.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                return
            buffer += chunk
            while buffer and len(buffer) > buffer[0]:
                size = buffer[0]
                message = bytes(buffer[1:1 + size])
                del buffer[:1 + size]
                tv.receive(connection, message)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    tv: TV
    context: ssl.SSLContext
    #: Set by Server.stop(): every open connection ends.
    closing = False

    def get_request(self):
        sock, address = super().get_request()
        # The handshake runs in the connection's own thread (handle()).
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address


class Server:
    """One stand-in TV served on 127.0.0.1."""

    def __init__(self, tv: TV, port: int = 0) -> None:
        self.tv = tv
        self.tcp = _Server(("127.0.0.1", port), _Handler)
        self.tcp.tv = tv
        self.tcp.context = _context()
        # stop() waits up to one poll_interval for the loop to notice.
        self.thread = threading.Thread(target=self.tcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def address(self) -> str:
        host, port = self.tcp.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.tcp.shutdown()
            self.thread.join(timeout=5)
        self.tcp.closing = True
        self.tcp.server_close()

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class Client:
    """A TLS client speaking the binding's side of the protocol, for the tests."""

    def __init__(self, address: str, timeout: float = 3.0) -> None:
        host, _, port = address.rpartition(":")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.connection = context.wrap_socket(socket.create_connection((host, int(port)), timeout=timeout))
        self.reader = self.connection.makefile("rb")

    def read(self) -> bytes | None:
        """The next message, or None if none came in time or the TV closed."""
        try:
            size = self.reader.read(1)
            return self.reader.read(size[0]) if size else None
        except (TimeoutError, OSError):
            return None

    def send(self, message: bytes) -> None:
        self.connection.sendall(bytes([len(message)]) + message)

    def close(self) -> None:
        self.reader.close()
        self.connection.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
