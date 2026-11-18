"""No one peer can hold the gateway's requests: real sockets against the
gateway's own server on loopback, with a throwaway certificate. Peers are
loopback addresses; no household address is used."""

import asyncio
import contextlib
import logging
import re
import socket
import ssl
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from django.conf import settings
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from oh_access import guest, service
from oh_identity import streams, trail
from oh_identity.asgi import application
from oh_identity.models import SecurityState
from oh_identity.runtime import RuntimeAdapter, install_adapter
from oh_identity.tests.test_core import Policy, adapter
from oh_identity.tests.test_gateway_server import EVENTS, H2, HOST, OPENSSL, EventRuntime, Running, certificate, trusting

# Another peer on loopback: connections made from this address.
OTHER = "127.0.0.2"
POLICY = "/api/v1/rooms/den/policy"
COMMANDS = "/api/v1/rooms/den/commands"


def connect(cert, port, source=HOST):
    """An HTTP/1.1 connection to the gateway from the peer at source."""
    plain = socket.create_connection((HOST, port), timeout=10, source_address=(source, 0))
    return trusting(cert, ["http/1.1"]).wrap_socket(plain, server_hostname=HOST)


def head(port, method="POST", path=COMMANDS, length=None):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {HOST}:{port}", "Content-Type: application/json"]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def whole(data):
    """Whether data is one whole answer, by its Content-Length or its last chunk."""
    top, _, body = data.partition(b"\r\n\r\n")
    length = re.search(rb"\r\ncontent-length: (\d+)", top, re.I)
    return len(body) >= int(length[1]) if length else body.endswith(b"0\r\n\r\n")


def answer(connection):
    """(status, head and body) of one answer; (None, b"") if the gateway
    closed the connection instead."""
    data = b""
    while not whole(data):
        more = connection.recv(65536)
        if not more:
            break
        data += more
    return (int(data.split(b" ", 2)[1]) if data else None), data


def answered(connections, count, seconds=10):
    """The answers that have come, without waiting on a connection that has
    none, once count of the connections have theirs."""
    answers, until = {}, time.monotonic() + seconds
    for connection in connections:
        connection.setblocking(False)
    while sum(map(whole, answers.values())) < count and time.monotonic() < until:
        for index, connection in enumerate(connections):
            try:
                answers[index] = answers.get(index, b"") + connection.recv(65536)
            except (ssl.SSLWantReadError, BlockingIOError):
                pass
        time.sleep(0.02)
    return [(int(data.split(b" ", 2)[1]), data) for data in answers.values() if data]


def settled(value, expected, seconds=10):
    """Wait for value() to become expected; return what it last was."""
    until = time.monotonic() + seconds
    while value() != expected and time.monotonic() < until:
        time.sleep(0.02)
    return value()


@unittest.skipUnless(OPENSSL, "needs openssl for a throwaway certificate")
@override_settings(ALLOWED_HOSTS=[HOST])
class HeldRequests(TransactionTestCase):
    """The real Django site behind Admission."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cert, self.key = certificate(directory.name)
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        install_adapter(RuntimeAdapter(adapter().contract, EventRuntime(), Policy()))
        # A fresh limiter: an earlier test's refusal must not suppress this one.
        warnings = patch.object(trail, "warnings", trail.Limited())
        warnings.start()
        self.addCleanup(warnings.stop)

    def test_a_peer_that_sends_heads_and_no_bodies_leaves_the_other_peers_their_requests(self):
        # The gateway as it is served: every limit at its default.
        admission = service.Admission(application)
        with self.assertLogs("oh_identity.trail", "WARNING") as logs, Running(admission, self.cert, self.key) as server:
            with contextlib.ExitStack() as held:
                greedy = [held.enter_context(connect(self.cert, server.port, OTHER)) for _ in range(100)]
                for connection in greedy:
                    connection.sendall(head(server.port, length=10))
                # Wait for the last of them to reach the gateway.
                settled(lambda: admission.active, 100, seconds=1)
                started = time.monotonic()
                with connect(self.cert, server.port) as other:
                    other.sendall(head(server.port, "GET", POLICY))
                    status, _ = answer(other)
                elapsed = time.monotonic() - started
                self.assertEqual(status, 200)
                self.assertLess(elapsed, 2)
                # Its 40 requests wait for bodies; the rest were answered at once.
                self.assertEqual((service.MAX_REQUESTS, service.PEER_REQUESTS), (100, 40))
                self.assertEqual(settled(lambda: admission.active, 40), 40)
                refused = answered(greedy, 60)
            self.assertEqual(settled(lambda: admission.active, 0), 0)
        self.assertEqual([status for status, _ in refused], [429] * 60)
        self.assertIn(b"retry-after: 1\r\n", refused[0][1])
        self.assertTrue(refused[0][1].endswith(b"\r\n\r\nToo Many Requests"))
        # One warning for all of them, naming the peer by its digest only.
        peer = trail.peer_digest(OTHER)[:8]
        self.assertEqual([record.getMessage() for record in logs.records][:1],
                         [f"peer_busy status=429 active=40 limit=40 peer={peer} suppressed=0"])
        self.assertNotIn(OTHER, "\n".join(record.getMessage() for record in logs.records))
        self.assertEqual((admission.active, admission.peers), (0, {}))

    def test_a_body_that_does_not_arrive_in_time_is_answered_408_and_gives_its_slot_back(self):
        admission = service.Admission(application, body_seconds=1)
        # The one warning is the trail's: the site and the server log no error.
        with self.assertLogs("oh_identity.trail", "WARNING") as logs, self.assertNoLogs("django.request", logging.ERROR), \
                self.assertNoLogs("hypercorn.error", logging.ERROR), self.assertNoLogs("asyncio", logging.ERROR), \
                Running(admission, self.cert, self.key) as server:
            with connect(self.cert, server.port) as late:
                late.sendall(head(server.port, length=10))
                started = time.monotonic()
                status, text = answer(late)
                waited = time.monotonic() - started
                self.assertEqual(status, 408)
                self.assertTrue(text.endswith(b"\r\n\r\nRequest Timeout"))
                self.assertIn(b"connection: close\r\n", text)
                # The gateway closes the connection: nothing more is read from it.
                self.assertEqual(late.recv(4096), b"")
            self.assertGreaterEqual(waited, 0.9)
            self.assertLess(waited, 5)
            self.assertEqual(settled(lambda: admission.active, 0), 0)
            # A body that comes within the deadline, late or in pieces, is
            # read: the site answers it (here refusing a request with no
            # CSRF token), not the deadline.
            with connect(self.cert, server.port) as slow:
                body = b'{"action":"volume","value":40}'
                slow.sendall(head(server.port, length=len(body)) + body[:5])
                time.sleep(0.2)
                slow.sendall(body[5:])
                status, text = answer(slow)
            self.assertEqual(status, 403)
            self.assertIn(b"csrf_failed", text)
            # Over HTTP/2 the stream is answered and its connection goes on.
            authority = (":authority", f"{HOST}:{server.port}")
            with H2(self.cert, server.port) as connection:
                connection.send(1, [(":method", "POST"), (":scheme", "https"), (":path", COMMANDS), authority,
                                    ("content-type", "application/json")], end_stream=False)
                self.assertEqual(connection.status(1), b"408")
                connection.send(3, [(":method", "GET"), (":scheme", "https"), (":path", POLICY), authority])
                self.assertEqual(connection.status(3), b"200")
            self.assertEqual(settled(lambda: admission.active, 0), 0)
        peer = trail.peer_digest(HOST)[:8]
        self.assertEqual([record.getMessage() for record in logs.records],
                         [f"body_overdue status=408 seconds=1 peer={peer} suppressed=0"])
        self.assertEqual(admission.peers, {})

    def test_an_event_stream_outlives_the_head_and_body_deadlines(self):
        for http2 in (True, False):
            with self.subTest(http2=http2):
                self.held_stream(http2)

    def held_stream(self, http2):
        admission = service.Admission(application, body_seconds=0.5)
        server = Running(admission, self.cert, self.key)
        # The head's deadline, also far shorter than the stream is held.
        server.config.keep_alive_timeout = 1

        async def exercise(port):
            async with httpx.AsyncClient(http2=http2, verify=trusting(self.cert), trust_env=False, timeout=10,
                                         base_url=f"https://{HOST}:{port}") as client, \
                    client.stream("GET", EVENTS) as held:
                started, frames = time.monotonic(), 0
                async for line in held.aiter_lines():
                    frames += line.startswith("data:")
                    if time.monotonic() - started > 2:
                        break
                # Still open, still counted, and other requests still answer.
                self.assertEqual((streams.slots.count(), admission.active), (1, 1))
                self.assertEqual((await client.get(POLICY)).status_code, 200)
                return frames

        with patch.object(streams, "HEARTBEAT", 0.25), self.assertNoLogs("oh_identity.trail", logging.WARNING), server:
            frames = asyncio.run(exercise(server.port))
            self.assertEqual(settled(lambda: admission.active, 0), 0)
        # The snapshot, then a heartbeat each quarter second for two
        # seconds, with room for a slow host.
        self.assertGreaterEqual(frames, 4)


async def held(scope, receive, send):
    """Answers once its client has gone."""
    while (await receive())["type"] != "http.disconnect":
        pass


class Call:
    """One request to an ASGI application from a peer's address, without a server."""

    def __init__(self, application, address):
        self.messages, self.inbox = [], asyncio.Queue()
        self.inbox.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        scope = {"type": "http", "client": (address, 40000), "path": "/", "method": "GET"}
        self.task = asyncio.ensure_future(application(scope, self.inbox.get, self.sent))

    async def sent(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return next((message["status"] for message in self.messages if message["type"] == "http.response.start"), None)

    async def leave(self):
        self.inbox.put_nowait({"type": "http.disconnect"})
        await self.task


@override_settings(ALLOWED_HOSTS=[HOST])
class PeerBudget(SimpleTestCase):
    def test_an_ipv6_64_and_an_ipv4_mapped_address_each_count_as_one_peer(self):
        async def exercise():
            admission = service.Admission(held, peer_limit=2)
            # Documentation addresses: two hosts of one /64, then a third.
            first, second = Call(admission, "2001:db8::1"), Call(admission, "2001:db8::2")
            third = Call(admission, "2001:db8::3")
            await third.task
            neighbour = Call(admission, "2001:db8:0:1::1")
            mapped, plain = Call(admission, "::ffff:192.0.2.1"), Call(admission, "192.0.2.1")
            again = Call(admission, "192.0.2.1")
            await again.task
            await asyncio.sleep(0)
            counted = (admission.active, dict(admission.peers))
            await first.leave()
            # A slot given back is the peer's to use again.
            fourth = Call(admission, "2001:db8::4")
            await asyncio.sleep(0)
            statuses = [call.status for call in (first, second, third, neighbour, mapped, plain, again, fourth)]
            for call in (second, neighbour, mapped, plain, fourth):
                await call.leave()
            return counted, statuses, (admission.active, admission.peers)

        with patch.object(trail, "warnings", trail.Limited()), self.assertLogs("oh_identity.trail", "WARNING"):
            counted, statuses, left = asyncio.run(exercise())
        self.assertEqual(counted, (5, {"2001:db8::/64": 2, "2001:db8:0:1::/64": 1, "192.0.2.1": 2}))
        self.assertEqual(statuses, [None, None, 429, None, None, None, 429, None])
        self.assertEqual(left, (0, {}))

    def test_a_peer_keeps_its_event_streams_and_a_full_connection_beside_them(self):
        config = service.listener(HOST, 13601)
        admission = service.Admission(None)
        self.assertEqual((admission.limit, admission.peer_limit, admission.body_seconds),
                         (service.MAX_REQUESTS, service.PEER_REQUESTS, service.BODY_SECONDS))
        # A browser shares one HTTP/2 connection between its pages, so it can
        # never ask for more than this, whatever it shows.
        self.assertGreaterEqual(service.PEER_REQUESTS, streams.PEER_STREAMS + config.h2_max_concurrent_streams)
        # And however one peer behaves, the others keep half the gateway.
        self.assertLessEqual(service.PEER_REQUESTS, service.MAX_REQUESTS // 2)
        # The head's deadline is the idle timeout; the body's is not shorter.
        self.assertGreaterEqual(service.BODY_SECONDS, config.keep_alive_timeout)
        # A body of the largest size the site reads fits the deadline on a
        # link far slower than any that loads the remote: 64 kbit/s.
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE * 8 / service.BODY_SECONDS, 64000)
        # A connection is not dropped for silence before its own client
        # would have given the stream up: three heartbeats.
        self.assertGreater(service.UNANSWERED_SECONDS, 3 * streams.HEARTBEAT)

    def test_every_listening_socket_hands_its_connections_the_unanswered_timeout(self):
        clients = guest.Clients([HOST + "/32"])
        for config in (service.listener(HOST, 0), service.listener(HOST, 0, clients=clients)):
            sockets = config.create_sockets()
            [listening] = sockets.insecure_sockets
            self.addCleanup(listening.close)
            listening.listen()
            with socket.create_connection(listening.getsockname(), timeout=5):
                connection, _ = listening.accept()
                with connection:
                    self.assertEqual(connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT),
                                     service.UNANSWERED_SECONDS * 1000)

    @unittest.skipUnless(OPENSSL, "needs openssl for a throwaway certificate")
    def test_a_peer_that_asks_and_never_reads_gives_its_slot_back(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cert, key = certificate(directory.name)

        async def flood(scope, receive, send):
            """Far more than the socket buffers hold; ends when its client has gone, as the site does."""
            async def write():
                await send({"type": "http.response.start", "status": 200, "headers": []})
                for _ in range(1024):
                    await send({"type": "http.response.body", "body": bytes(2 ** 16), "more_body": True})
            writing = asyncio.ensure_future(write())
            try:
                await held(scope, receive, send)
            finally:
                writing.cancel()
                await asyncio.gather(writing, return_exceptions=True)

        admission = service.Admission(flood)
        with patch.object(service, "UNANSWERED_SECONDS", 1), self.assertNoLogs("hypercorn.error", logging.ERROR), \
                self.assertNoLogs("asyncio", logging.ERROR), Running(admission, cert, key) as server, \
                connect(cert, server.port) as deaf:
            deaf.sendall(head(server.port, "GET", "/"))
            self.assertEqual(settled(lambda: admission.active, 1), 1)
            # The kernel gives the connection up; nothing else would.
            self.assertEqual(settled(lambda: admission.active, 0, seconds=20), 0)
