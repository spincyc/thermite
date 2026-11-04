"""The gateway's own hypercorn server on an ephemeral loopback port, with a
throwaway certificate and the identity tests' fake runtime. No household
address, account or certificate is used."""

import asyncio
from contextlib import AsyncExitStack
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import h2.config
import h2.connection
import h2.events
import httpx
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from oh_access import service
from oh_identity import streams
from oh_identity.asgi import application
from oh_identity.models import SecurityState
from oh_identity.runtime import RuntimeAdapter, install_adapter
from oh_identity.tests.test_core import FakeRuntime, Policy, adapter

OPENSSL = shutil.which("openssl")
HOST = "127.0.0.1"
TOPIC = "openhab/items/loc_den_source/stateupdated"
LIB = Path(__file__).resolve().parents[2]


def certificate(directory):
    """A throwaway self-signed P-256 leaf for the loopback address."""
    cert, key = Path(directory) / "tls-cert.pem", Path(directory) / "tls-key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-sha256",
                    "-nodes", "-days", "1", "-subj", "/CN=" + HOST, "-addext", "subjectAltName=IP:" + HOST,
                    "-addext", "basicConstraints=critical,CA:FALSE", "-keyout", key, "-out", cert],
                   check=True, capture_output=True)
    return cert, key


def trusting(cert, alpn=None):
    context = ssl.create_default_context(cafile=cert)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    if alpn:
        context.set_alpn_protocols(alpn)
    return context


def wait_listening(port, seconds=10):
    deadline = time.monotonic() + seconds
    while True:
        try:
            socket.create_connection((HOST, port), timeout=1).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class Seen:
    """Records every scope that reaches the wrapped application."""

    def __init__(self, application):
        self.application, self.scopes = application, []

    async def __call__(self, scope, receive, send):
        self.scopes.append((scope["type"], scope.get("http_version"), scope.get("client")))
        await self.application(scope, receive, send)


class EventRuntime(FakeRuntime):
    """The fake runtime with native events: one update, then quiet until the
    client goes away."""

    async def events(self, topics):
        yield {"topic": topics[0], "type": "ItemStateUpdatedEvent",
               "payload": json.dumps({"type": "String", "value": "player"})}
        await asyncio.Event().wait()


class Running:
    """service._serve on its own thread and loop; leaving stops it as SIGTERM
    does in the unit."""

    def __init__(self, application, cert=None, key=None, *, graceful=2):
        config = service.listener(HOST, 0, service.tls_context(cert, key) if cert else None)
        config.graceful_timeout = graceful
        self.config, self.sockets = config, config.create_sockets()
        self.port = (self.sockets.secure_sockets or self.sockets.insecure_sockets)[0].getsockname()[1]
        self.bound = [(application, config, self.sockets)]
        self.drained = threading.Event()
        self.started = threading.Event()

    def __enter__(self):
        self.thread = threading.Thread(target=asyncio.run, args=(self._main(),), daemon=True)
        self.thread.start()
        self.started.wait(5)
        wait_listening(self.port)
        return self

    async def _main(self):
        self.loop, self.stop = asyncio.get_running_loop(), asyncio.Event()
        self.started.set()
        await service._serve(self.bound, stop=self.stop, on_shutdown=self.drained.set)

    def __exit__(self, *_):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(30)
        assert not self.thread.is_alive() and self.drained.is_set()


async def first_event(response):
    lines = response.aiter_lines()
    while not (line := await anext(lines)).startswith("data:"):
        pass
    return json.loads(line[5:])


@unittest.skipUnless(OPENSSL, "needs openssl for a throwaway certificate")
@override_settings(ALLOWED_HOSTS=[HOST])
class Http2Gateway(TransactionTestCase):
    """The real Django site behind Admission, over one HTTP/2 connection."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cert, self.key = certificate(directory.name)
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        install_adapter(RuntimeAdapter(adapter().contract, EventRuntime(), Policy()))

    def http2(self, port):
        return httpx.AsyncClient(http2=True, verify=trusting(self.cert), trust_env=False, timeout=10,
                                 base_url=f"https://{HOST}:{port}")

    def test_alpn_offers_http2_then_http11(self):
        admission = service.Admission(application)
        with Running(admission, self.cert, self.key) as server:
            for offered, chosen in ((["h2", "http/1.1"], "h2"), (["http/1.1"], "http/1.1")):
                with trusting(self.cert, offered).wrap_socket(socket.create_connection((HOST, server.port)),
                                                              server_hostname=HOST) as tls:
                    self.assertEqual(tls.selected_alpn_protocol(), chosen)
                    self.assertIn(tls.version(), {"TLSv1.2", "TLSv1.3"})
        self.assertEqual(admission.active, 0)

    def test_one_connection_carries_eight_event_streams_and_a_prompt_read(self):
        seen = Seen(application)

        async def exercise(port):
            async with self.http2(port) as client, AsyncExitStack() as stack:
                opened = await asyncio.gather(*(stack.enter_async_context(
                    client.stream("GET", "/rest/events", params={"topics": TOPIC})) for _ in range(8)))
                self.assertEqual([(each.status_code, each.http_version) for each in opened], [(200, "HTTP/2")] * 8)
                alpn = opened[0].extensions["network_stream"].get_extra_info("ssl_object").selected_alpn_protocol()
                events = await asyncio.gather(*(first_event(each) for each in opened))
                self.assertEqual({json.loads(each["payload"])["value"] for each in events}, {"player"})
                self.assertEqual(streams.slots.count(), 8)
                # Under HTTP/1.1 a browser's six connections would all be held
                # by now and this read would queue behind them.
                started = time.monotonic()
                # Forwarding claims are ignored: the peer stays the socket's.
                read = await client.get("/api/v1/rooms/den/policy", headers={
                    "X-Forwarded-For": "198.51.100.7", "Forwarded": "for=198.51.100.7", "X-Real-IP": "198.51.100.7"})
                elapsed = time.monotonic() - started
                self.assertEqual(streams.slots.count(), 8)
                return alpn, read, elapsed

        with Running(service.Admission(seen), self.cert, self.key) as server:
            alpn, read, elapsed = asyncio.run(exercise(server.port))
        self.assertEqual(alpn, "h2")
        self.assertEqual((read.status_code, read.http_version), (200, "HTTP/2"))
        self.assertIn("effective", read.json())
        self.assertLess(elapsed, 2)
        # Nine requests, one HTTP/2 connection, one real socket peer.
        self.assertEqual(len(seen.scopes), 9)
        self.assertEqual({(kind, version, client[0]) for kind, version, client in seen.scopes}, {("http", "2", HOST)})
        self.assertEqual(len({client for _, _, client in seen.scopes}), 1)
        self.assertEqual(streams.slots.count(), 0)

    def test_requests_over_the_limit_are_refused_per_stream(self):
        admission = service.Admission(application, limit=2)

        async def exercise(port):
            # Two streams of one connection fill the budget; a third is refused.
            async with self.http2(port) as client, AsyncExitStack() as stack:
                held = [await stack.enter_async_context(client.stream("GET", "/rest/events", params={"topics": TOPIC}))
                        for _ in range(2)]
                await asyncio.gather(*(first_event(each) for each in held))
                self.assertEqual(admission.active, 2)
                refused = await client.get("/api/v1/rooms/den/policy")
            # Closing the connection ends its streams and returns their budget.
            for _ in range(100):
                if not admission.active:
                    break
                await asyncio.sleep(0.02)
            async with self.http2(port) as client:
                return refused, await client.get("/api/v1/rooms/den/policy")

        with Running(admission, self.cert, self.key) as server:
            refused, admitted = asyncio.run(exercise(server.port))
        self.assertEqual((refused.status_code, refused.http_version, refused.text), (503, "HTTP/2", "Service Unavailable"))
        self.assertEqual(refused.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(admitted.status_code, 200)
        self.assertEqual(admission.active, 0)

    def test_shutdown_ends_a_held_event_stream_and_releases_its_slot(self):
        # Hypercorn cancels the connection after graceful_timeout; Django's
        # own response tasks outlive that, and _serve cancels them.
        server = Running(service.Admission(application), self.cert, self.key, graceful=1).__enter__()

        async def exercise():
            async with self.http2(server.port) as client, \
                    client.stream("GET", "/rest/events", params={"topics": TOPIC}) as held:
                lines = held.aiter_lines()
                while not (await anext(lines)).startswith("data:"):
                    pass
                self.assertEqual(streams.slots.count(), 1)
                stopping = asyncio.create_task(asyncio.to_thread(server.__exit__, None, None, None))
                try:
                    async for _ in lines:
                        pass
                except httpx.TransportError:
                    pass
                await stopping

        started = time.monotonic()
        asyncio.run(exercise())
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(server.drained.is_set())
        self.assertEqual(streams.slots.count(), 0)

    def test_websocket_handshakes_are_refused_before_django(self):
        seen = Seen(application)
        with Running(service.Admission(seen), self.cert, self.key) as server, \
                self.assertNoLogs("hypercorn.error", logging.ERROR), self.assertNoLogs("django.request", logging.ERROR):
            with trusting(self.cert, ["http/1.1"]).wrap_socket(socket.create_connection((HOST, server.port)),
                                                               server_hostname=HOST) as tls:
                tls.sendall(f"GET /rest/events HTTP/1.1\r\nHost: {HOST}:{server.port}\r\nUpgrade: websocket\r\n"
                            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                            "Sec-WebSocket-Version: 13\r\n\r\n".encode())
                self.assertTrue(tls.recv(4096).startswith(b"HTTP/1.1 403 "))
            # RFC 8441 extended CONNECT, which hypercorn advertises over HTTP/2.
            headers = [(":method", "CONNECT"), (":protocol", "websocket"), (":scheme", "https"),
                       (":path", "/rest/events"), (":authority", f"{HOST}:{server.port}"), ("sec-websocket-version", "13")]
            with H2(self.cert, server.port) as connection:
                connection.send(1, headers, end_stream=False)
                self.assertEqual(connection.status(1), b"403")
        self.assertEqual(seen.scopes, [])


class H2:
    """A raw HTTP/2 client connection, for frames httpx will not send."""

    def __init__(self, cert, port):
        self.tls = trusting(cert, ["h2"]).wrap_socket(socket.create_connection((HOST, port), timeout=10), server_hostname=HOST)
        self.connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
        self.connection.initiate_connection()
        self.tls.sendall(self.connection.data_to_send())
        self.statuses, self.settings = {}, False
        while not self.settings:
            self.receive()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.tls.close()

    def receive(self):
        data = self.tls.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection")
        for event in self.connection.receive_data(data):
            if isinstance(event, h2.events.RemoteSettingsChanged):
                self.settings = True
            elif isinstance(event, h2.events.ResponseReceived):
                self.statuses[event.stream_id] = dict(event.headers)[":status"].encode()
        self.tls.sendall(self.connection.data_to_send())

    def send(self, stream_id, headers, *, end_stream=True, reset=False):
        self.connection.send_headers(stream_id, headers, end_stream=end_stream)
        if reset:
            self.connection.reset_stream(stream_id)

    def flush(self):
        self.tls.sendall(self.connection.data_to_send())

    def status(self, stream_id):
        self.flush()
        while stream_id not in self.statuses:
            self.receive()
        return self.statuses[stream_id]


@unittest.skipUnless(OPENSSL, "needs openssl for a throwaway certificate")
class GatewayServer(SimpleTestCase):
    """The listener settings, the request limit under rapid reset, and the
    unit's process lifecycle."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.cert, self.key = certificate(self.directory)

    def test_listener_keeps_the_gateway_settings(self):
        config = service.listener("192.0.2.10", 13600, service.tls_context(self.cert, self.key))
        self.assertEqual(config.bind, ["192.0.2.10:13600"])
        self.assertEqual(service.listener("2001:db8::1", 13600).bind, ["[2001:db8::1]:13600"])
        self.assertTrue(config.ssl_enabled)
        self.assertFalse(service.listener(HOST, 13600).ssl_enabled)
        context = config.create_ssl_context()
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(context.options & ssl.OP_NO_COMPRESSION and context.options & ssl.OP_NO_RENEGOTIATION)
        self.assertTrue(all(cipher["aead"] and cipher["kea"] in {"kx-ecdhe", "kx-any"} for cipher in context.get_ciphers()))
        self.assertIsNone(config.accesslog)
        self.assertIs(config.errorlog, logging.getLogger("hypercorn.error"))
        self.assertIsNone(config.logconfig)
        self.assertIsNone(config.logconfig_dict)
        self.assertEqual(sys.modules["oh_identity.settings"].LOGGING["loggers"]["hypercorn.error"]["handlers"], ["journal"])
        self.assertFalse(config.include_server_header)
        self.assertEqual(config.response_headers("h2")[0][0], b"date")
        self.assertEqual((config.keep_alive_timeout, config.graceful_timeout), (5, 15))
        self.assertEqual((config.h2_max_concurrent_streams, config.h2_max_header_list_size,
                          config.h2_max_inbound_frame_size), (32, 16384, 16384))
        self.assertGreater(config.keep_alive_max_requests, 10 ** 9)
        self.assertEqual(service.Admission(None).limit, 100)
        # Event streams can never take the whole request budget.
        self.assertLess(streams.TOTAL_STREAMS, service.MAX_REQUESTS)

    def test_rapidly_reset_streams_stay_within_the_request_limit(self):
        # CVE-2023-44487: a reset stream leaves HTTP/2's concurrent-stream
        # count at once, but its handler keeps running. Admission counts
        # handlers, so a burst of resets cannot run more than the limit.
        busy = {"now": 0, "most": 0, "calls": 0}

        async def slow(scope, receive, send):
            busy["calls"] += 1
            busy["now"] += 1
            busy["most"] = max(busy["most"], busy["now"])
            try:
                if scope["path"] != "/ordinary":
                    await asyncio.sleep(2)
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
                await send({"type": "http.response.body", "body": b"ok"})
            finally:
                busy["now"] -= 1

        admission = service.Admission(slow, limit=4)
        with Running(admission, self.cert, self.key) as server:
            with H2(self.cert, server.port) as attacker:
                head = [(":method", "GET"), (":scheme", "https"), (":path", "/"), (":authority", f"{HOST}:{server.port}")]
                for stream_id in range(1, 1000, 2):
                    attacker.send(stream_id, head, reset=True)
                attacker.flush()
                with H2(self.cert, server.port) as ordinary:
                    for _ in range(100):
                        if busy["calls"] and not admission.active:
                            break
                        time.sleep(0.05)
                    ordinary.send(1, [*head[:2], (":path", "/ordinary"), head[3]])
                    self.assertEqual(ordinary.status(1), b"200")
        self.assertLessEqual(busy["most"], 4)
        self.assertLessEqual(busy["calls"], 5)
        self.assertEqual(admission.active, 0)

    def run_unit(self, port, mode="quiet"):
        script = self.directory / "unit.py"
        script.write_text(UNIT)
        return subprocess.Popen([sys.executable, str(script), str(self.cert), str(self.key), str(port), mode],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "PYTHONPATH": str(LIB)})

    def free_port(self):
        with socket.socket() as probe:
            probe.bind((HOST, 0))
            return probe.getsockname()[1]

    def test_sigterm_and_sigint_drain_held_streams_then_exit_cleanly(self):
        for signum, mode in ((signal.SIGTERM, "stubborn"), (signal.SIGINT, "quiet")):
            with self.subTest(signal=signum.name):
                port = self.free_port()
                unit = self.run_unit(port, mode)
                self.addCleanup(unit.kill)
                wait_listening(port)

                async def hold():
                    async with httpx.AsyncClient(http2=True, verify=trusting(self.cert), trust_env=False,
                                                 timeout=30) as client, \
                            client.stream("GET", f"https://{HOST}:{port}/held") as response:
                        chunks = response.aiter_bytes()
                        await anext(chunks)
                        unit.send_signal(signum)
                        async for _ in chunks:
                            pass

                started = time.monotonic()
                try:
                    asyncio.run(hold())
                except httpx.TransportError:
                    pass  # The held stream is cut once graceful_timeout ends.
                stdout, stderr = unit.communicate(timeout=20)
                self.assertEqual(unit.returncode, 0, stderr)
                self.assertLess(time.monotonic() - started, 10)
                # The held stream was cancelled after graceful_timeout, leftover
                # tasks were cancelled, and on_shutdown ran before the loop ended.
                self.assertEqual(stdout.split(), ["drained", "exited"])
                incomplete = "Access shutdown cleanup incomplete: 1 unfinished task(s), 0 failed task(s)."
                self.assertEqual(incomplete in stderr, mode == "stubborn", stderr)

    def test_a_listener_that_cannot_bind_exits_with_startup_failure(self):
        with socket.socket() as taken:
            taken.bind((HOST, 0))
            taken.listen()
            unit = self.run_unit(taken.getsockname()[1])
            stdout, stderr = unit.communicate(timeout=20)
        self.assertEqual(unit.returncode, service.STARTUP_FAILURE)
        self.assertIn("Access server could not bind", stderr)
        self.assertEqual(stdout, "")


# The unit's lifecycle in its own process: real signals, real exit status.
# Every request is held open and leaves behind an orphaned task, as Django's
# response and stream tasks are; "stubborn" orphans outlive cancellation.
UNIT = """
import asyncio
import sys

from oh_access import service

cert, key, port, mode = sys.argv[1:]
service.CANCEL_SECONDS = 1
orphans = set()


async def orphan():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if mode == "stubborn":
            await asyncio.sleep(30)
        raise


async def held(scope, receive, send):
    orphans.add(asyncio.get_running_loop().create_task(orphan()))
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
    await send({"type": "http.response.body", "body": b": open\\n\\n", "more_body": True})
    await asyncio.Event().wait()


config = service.listener("127.0.0.1", int(port), service.tls_context(cert, key))
config.graceful_timeout = 1
service._run_server([(service.Admission(held), config)], on_shutdown=lambda: print("drained", flush=True))
print("exited", flush=True)
"""
