"""Music Assistant's websocket, as the gateway reads its events: asyncio
streams and wsproto, always on loopback.

Music Assistant sends its server info first, then waits for an auth command
carrying the token (it ignores a header on the upgrade); once authenticated,
the connection carries every event, `{"event", "object_id", "data"}`, and
the answers to commands sent on it, `{"message_id", "result"}` or
`{"message_id", "error_code"}`. It pings every 25 seconds and drops a client
that does not answer, or that lets 512 messages queue unread.
"""

from __future__ import annotations

import asyncio
import json
import secrets

from wsproto import ConnectionType, WSConnection
from wsproto.events import (AcceptConnection, CloseConnection, Message, Ping, RejectConnection, RejectData,
                            Request, TextMessage)
from wsproto.utilities import LocalProtocolError, RemoteProtocolError

from .client import HOST, Unauthorized, Unavailable

#: Bytes of one message: a large house's players/all or a long queue fits.
MESSAGE_BYTES = 8 * 1024 * 1024
#: Seconds the server may stay silent before the connection is treated as
#: dead: Music Assistant pings every 25.
QUIET = 60
#: Seconds to connect, upgrade and authenticate.
CONNECT = 10


class Closed(Unavailable):
    """The connection ended or went silent."""


class Socket:
    """One authenticated connection. receive() yields each message in order,
    answering pings as they arrive."""

    def __init__(self, reader, writer, *, quiet=QUIET):
        self.reader, self.writer, self.quiet = reader, writer, quiet
        self.ws = WSConnection(ConnectionType.CLIENT)
        self.pending = []
        self.text = []
        self.size = 0

    @classmethod
    async def open(cls, port, token, *, host=HOST, quiet=QUIET, timeout=CONNECT):
        """A connection authenticated with token; Unauthorized when Music
        Assistant refuses it, Unavailable when it cannot be reached."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError):
            raise Unavailable("Music Assistant is unreachable") from None
        socket = cls(reader, writer, quiet=quiet)
        try:
            await asyncio.wait_for(socket._start(port, token, host), timeout)
        except BaseException as error:
            socket.abort()
            if isinstance(error, asyncio.TimeoutError):
                raise Closed("Music Assistant did not answer") from None
            raise
        return socket

    async def _start(self, port, token, host):
        self._write(self.ws.send(Request(host=f"{host}:{port}", target="/ws")))
        while True:
            event = await self._event()
            if isinstance(event, AcceptConnection):
                break
            if isinstance(event, (RejectConnection, RejectData)):
                raise Closed("Music Assistant refused the websocket")
        info = await self.receive()
        if "server_version" not in info:
            raise Closed("Music Assistant did not send its server info")
        await self.send({"message_id": "auth", "command": "auth", "args": {"token": token}})
        answer = await self.receive()
        if answer.get("message_id") == "connection":
            # A server with no user follows its info with this error, and closes.
            raise Closed("Music Assistant is not set up")
        result = answer.get("result")
        if answer.get("message_id") != "auth" or not isinstance(result, dict) or result.get("authenticated") is not True:
            raise Unauthorized("Music Assistant refused the token")

    def _write(self, data):
        if data:
            self.writer.write(data)

    async def _event(self):
        """The next wsproto event, reading as needed."""
        while not self.pending:
            try:
                data = await asyncio.wait_for(self.reader.read(65536), self.quiet)
            except asyncio.TimeoutError:
                raise Closed("Music Assistant went silent") from None
            except OSError:
                raise Closed("Music Assistant connection lost") from None
            try:
                self.ws.receive_data(data or None)
                self.pending.extend(self.ws.events())
            except RemoteProtocolError:
                raise Closed("Music Assistant broke the protocol") from None
            if not data and not self.pending:
                raise Closed("Music Assistant closed the connection")
        return self.pending.pop(0)

    async def send(self, message):
        try:
            self._write(self.ws.send(TextMessage(data=json.dumps(message, separators=(",", ":")))))
            await self.writer.drain()
        except (OSError, LocalProtocolError):
            raise Closed("Music Assistant connection lost") from None

    async def command(self, command, **args):
        """Send one command; its message ID, which its answer carries."""
        message_id = secrets.token_hex(6)
        await self.send({"message_id": message_id, "command": command, "args": args})
        return message_id

    async def receive(self):
        """The next message, a JSON object; Closed when the connection ends."""
        while True:
            event = await self._event()
            if isinstance(event, Ping):
                self._write(self.ws.send(event.response()))
                try:
                    await self.writer.drain()
                except OSError:
                    raise Closed("Music Assistant connection lost") from None
            elif isinstance(event, CloseConnection):
                try:
                    self._write(self.ws.send(event.response()))
                except LocalProtocolError:
                    pass
                raise Closed("Music Assistant closed the connection")
            elif isinstance(event, Message):
                if not isinstance(event, TextMessage):
                    raise Closed("Music Assistant sent binary data")
                self.size += len(event.data)
                if self.size > MESSAGE_BYTES:
                    raise Closed("Music Assistant message too large")
                self.text.append(event.data)
                if event.message_finished:
                    data, self.text, self.size = "".join(self.text), [], 0
                    try:
                        message = json.loads(data)
                    except ValueError:
                        raise Closed("Music Assistant sent invalid JSON") from None
                    if isinstance(message, dict):
                        return message

    def abort(self):
        try:
            self.writer.close()
        except Exception:
            pass

    async def close(self):
        try:
            self._write(self.ws.send(CloseConnection(code=1000)))
            await self.writer.drain()
        except Exception:
            pass
        self.abort()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
