"""Real parser/fan-in with fake HTTP streams, never sockets or a native JVM."""

import asyncio
import fnmatch
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import httpx

from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from oh_identity.runtime import project
from oh_identity.transport import LoopbackTransport, native_batches


class HTTPStream:
    def __init__(self, client, url):
        self.client, self.url = client, url
        self.headers = {"content-type": "text/event-stream"}

    async def __aenter__(self):
        self.client.opened.append(self.url)
        return self

    async def __aexit__(self, *args):
        self.client.closed.append(self.url)

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size=None):
        while True:
            value = await self.client.queue.get()
            if value == "fail":
                raise ValueError("private response sentinel")
            yield value


class HTTPClient:
    def __init__(self):
        self.opened, self.closed = [], []
        self.queue = asyncio.Queue()
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.exited = True

    def stream(self, method, url, headers):
        if method != "GET":
            raise AssertionError("Write prohibited")
        return HTTPStream(self, url)


def many_topics(count=140):
    return ["openhab/items/public_" + str(number).zfill(4) + "_long_label_volume/" + verb
            for number in range(count) for verb in ("stateupdated", "statechanged", "command")]


class NativeTransport(SimpleTestCase):
    def transport(self):
        temporary = tempfile.TemporaryDirectory(dir=settings.STATE)
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "bearer"
        path.write_text("synthetic-not-used")
        path.chmod(0o600)
        transport = LoopbackTransport("http://127.0.0.1:8080", path)
        self.addCleanup(transport.close)
        return transport

    def test_pools_are_reused_bounded_and_closed_explicitly(self):
        clients, capacities = [], []
        original = httpx.Client

        def reply(request):
            if request.method == "POST":
                return httpx.Response(202)
            if request.url.path.endswith("/state"):
                return httpx.Response(200, text="ON")
            if request.url.path.startswith("/media"):
                return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"image")
            return httpx.Response(200, json={"members": []})

        def client(**options):
            limits = options["limits"]
            capacities.append((limits.max_connections, limits.max_keepalive_connections))
            self.assertFalse(options["trust_env"])
            self.assertFalse(options["follow_redirects"])
            instance = original(transport=httpx.MockTransport(reply), **options)
            clients.append(instance)
            return instance

        with patch("oh_identity.transport.httpx.Client", side_effect=client) as create:
            transport = self.transport()
            self.assertEqual(transport.read("power"), "ON")
            self.assertEqual(transport.group("status"), {"members": []})
            self.assertEqual(transport.read("power"), "ON")
            self.assertEqual(create.call_count, 1)
            transport.command("power", "OFF")
            transport.command("power", "ON")
            self.assertEqual(transport.media("/media/image", "GET", None, 20), ("image/jpeg", b"image"))
            self.assertEqual(create.call_count, 3)
            self.assertEqual(capacities, [(8, 8), (4, 4), (2, 2)])
            transport.close()
            transport.close()
            self.assertTrue(all(instance.is_closed for instance in clients))
            with self.assertRaises(RuntimeError):
                transport.command("power", "ON")

    def test_a_slow_snapshot_does_not_hold_command_pool_or_client_creation_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = httpx.Client

        def reply(request):
            if request.method == "GET":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("read fixture was not released")
            return httpx.Response(202 if request.method == "POST" else 200, text="ON")

        def client(**options):
            return original(transport=httpx.MockTransport(reply), **options)

        with patch("oh_identity.transport.httpx.Client", side_effect=client) as create:
            transport = self.transport()
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(transport.read, "power")
                try:
                    self.assertTrue(entered.wait(1))
                    transport.command("power", "OFF")
                    self.assertFalse(reading.done())
                    self.assertEqual(create.call_count, 2)
                finally:
                    release.set()
                self.assertEqual(reading.result(timeout=1), "ON")

    def test_small_frame_arrives_without_waiting_for_more_native_bytes(self):
        async def check():
            class QuietStream(httpx.AsyncByteStream):
                closed = False

                async def __aiter__(self):
                    yield b'data: {"value":"ON"}\n\n'
                    await asyncio.Event().wait()

                async def aclose(self):
                    self.closed = True

            stream = QuietStream()
            native = httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream))
            async with httpx.AsyncClient(transport=native) as client:
                iterator = self.transport()._events(client, "openhab/items/public_power/stateupdated")
                try:
                    self.assertEqual(await asyncio.wait_for(anext(iterator), .5), {"value": "ON"})
                finally:
                    await iterator.aclose()
            self.assertTrue(stream.closed)
        async_to_sync(check)()

    def test_exact_batches_budget_union_and_no_private_expansion(self):
        topics = many_topics()
        topics += ["openhab/items/tv_pairing_status/stateupdated"]
        batches = native_batches(topics)
        self.assertGreater(len(batches), 1)
        self.assertLessEqual(len(batches), 4)
        patterns = [pattern for batch in batches for pattern in unquote(batch).split(",")]
        self.assertTrue(all(len(("http://127.0.0.1:8080/rest/events?topics=" + batch).encode()) <= 6000 for batch in batches))
        for topic in topics:
            self.assertEqual(sum(fnmatch.fnmatchcase(topic, pattern) for pattern in patterns), 1)
        self.assertFalse(any(fnmatch.fnmatchcase("openhab/items/private_pairing/command", pattern) for pattern in patterns))
        self.assertFalse(any(fnmatch.fnmatchcase("openhab/items/unlisted_volume/command", pattern) for pattern in patterns))
        with self.assertRaises(ValueError):
            native_batches(many_topics(1000))
        with self.assertRaises(ValueError):
            native_batches(["openhab/items/private_pairing/command"])

    def test_public_redaction_is_keyed_item_scoped_stable_and_bounded(self):
        spec = {"kind": "redacted"}
        one = project("public_state", "private sentinel", spec)
        self.assertTrue(one.startswith("hidden:"))
        self.assertNotIn("sentinel", one)
        self.assertEqual(one, project("public_state", "private sentinel", spec))
        self.assertNotEqual(one, project("other_state", "private sentinel", spec))
        self.assertNotEqual(one, project("public_state", "different", spec))
        with override_settings(SECRET_KEY="different-synthetic-site-key"):
            self.assertNotEqual(one, project("public_state", "private sentinel", spec))
        self.assertEqual(project("public_state", "x" * 65537, spec), "UNDEF")
        self.assertEqual(json.loads(project("public_problem", '{"reported":null,"raw":"sentinel"}',
                         {"kind": "json", "fields": {"reported": {"kind": "enum", "nullable": True, "values": [None, "tv"]}}})), {"reported": None})

    def test_shared_client_failure_cancels_all_streams(self):
        async def check():
            client = HTTPClient()
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client) as factory:
                transport = self.transport()
                iterator = transport.events(many_topics(), project=lambda event: {"safe": True})
                pending = asyncio.create_task(anext(iterator))
                for _ in range(100):
                    if len(client.opened) == len(native_batches(many_topics())):
                        break
                    await asyncio.sleep(.001)
                await client.queue.put("fail")
                with self.assertRaises(ValueError):
                    await pending
                self.assertEqual(factory.call_count, 1)
                self.assertGreater(len(client.opened), 1)
                self.assertCountEqual(client.closed, client.opened)
                self.assertTrue(client.exited)
        async_to_sync(check)()

    def test_cancel_projects_before_bounded_queue_and_closes_every_shard(self):
        async def check():
            client = HTTPClient()
            from asyncio import Queue
            recorded = []

            class SafeQueue(Queue):
                async def put(self, value):
                    recorded.append(value)
                    return await super().put(value)
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client), patch("oh_identity.transport.asyncio.Queue", SafeQueue):
                iterator = self.transport().events(many_topics(), project=lambda event: {"safe": event["value"]})
                pending = asyncio.create_task(anext(iterator))
                await client.queue.put(b'data: {"value":"public","raw":"private-sentinel"}\n\n')
                self.assertEqual(await pending, {"safe": "public"})
                for _ in range(200):
                    await client.queue.put(b'data: {"value":"public","raw":"private-sentinel"}\n\n')
                await asyncio.sleep(.02)
                # Producers block at 64 queued canonical frames, with at most
                # one additional pending put per native shard.
                self.assertLessEqual(len(recorded), 65 + len(client.opened))
                self.assertNotIn("sentinel", json.dumps(recorded))
                await iterator.aclose()
                self.assertCountEqual(client.closed, client.opened)
                self.assertTrue(client.exited)
        async_to_sync(check)()

    def test_quiet_cancel_and_oversize_frame_close_all(self):
        async def check(oversize):
            client = HTTPClient()
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client):
                iterator = self.transport().events(many_topics(), project=lambda event: event)
                pending = asyncio.create_task(anext(iterator))
                await asyncio.sleep(.01)
                if oversize:
                    await client.queue.put(b"x" * 65537)
                    with self.assertRaises(ValueError):
                        await pending
                else:
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                self.assertCountEqual(client.closed, client.opened)
                self.assertTrue(client.exited)
        async_to_sync(check)(False)
        async_to_sync(check)(True)
