"""Real parser/fan-in with fake HTTP streams, never sockets or a native JVM."""

import asyncio
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
from oh_identity.transport import CommandRefused, LoopbackTransport, native_query


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


WILDCARDS = ["openhab/items/*/stateupdated", "openhab/items/*/statechanged", "openhab/items/*/command"]


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

    def test_commands_name_their_trail_source_and_keep_only_a_refusal_status(self):
        sent = []
        original = httpx.Client

        def reply(request):
            sent.append(request)
            return httpx.Response(202 if request.content == b"ON" else 404, text="private upstream body")

        with patch("oh_identity.transport.httpx.Client",
                   side_effect=lambda **options: original(transport=httpx.MockTransport(reply), **options)):
            transport = self.transport()
            self.addCleanup(transport.close)
            transport.command("power", "ON", source="thermite.web:1a2b3c4d")
            transport.command("power", "ON")
            for source in ("thermite.web:1A2B3C4D", "thermite.web:1a2b3c4d=>x", "other:1a2b3c4d", 7):
                with self.assertRaises(ValueError):
                    transport.command("power", "ON", source=source)
            with self.assertRaises(CommandRefused) as refused:
                transport.command("power", "OFF", source="thermite.repair:0a1b2c3d")
        self.assertEqual(refused.exception.status, 404)
        self.assertNotIn("private", str(refused.exception))
        self.assertEqual([request.headers.get("X-OpenHAB-Source") for request in sent],
                         ["thermite.web:1a2b3c4d", None, "thermite.repair:0a1b2c3d"])
        self.assertEqual({request.headers["Content-Type"] for request in sent}, {"text/plain"})

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

    def test_a_query_is_wildcards_or_exact_public_topics_within_the_url_budget(self):
        self.assertEqual(unquote(native_query(WILDCARDS)), ",".join(WILDCARDS))
        self.assertEqual(unquote(native_query(["openhab/items/tv_pairing_status/stateupdated", "openhab/items/public_volume/command"])),
                         "openhab/items/tv_pairing_status/stateupdated,openhab/items/public_volume/command")
        for refused in ([], ["openhab/items/tv_pairing/command"], ["openhab/items/*"], ["openhab/items/public_*/command"],
                        ["openhab/things/*/status"], ["openhab/items/*/stateupdated,openhab/items/*/command"]):
            with self.subTest(refused=refused), self.assertRaises(ValueError):
                native_query(refused)
        with self.assertRaises(ValueError):
            native_query(["openhab/items/public_" + str(number).zfill(4) + "_long_label_volume/statechanged" for number in range(200)])

    def test_one_bulk_state_read_keeps_only_the_named_items(self):
        original = httpx.Client
        requests = []

        def reply(request):
            requests.append(str(request.url))
            return httpx.Response(200, json=[{"name": "public_power", "state": "ON"}, {"name": "private_token", "state": "private-sentinel"},
                                             {"name": "public_volume", "state": None}, "junk"])
        with patch("oh_identity.transport.httpx.Client", side_effect=lambda **options: original(transport=httpx.MockTransport(reply), **options)):
            states = self.transport().states(["public_power", "public_volume"])
        self.assertEqual(states, {"public_power": "ON"})
        self.assertEqual(requests, ["http://127.0.0.1:8080/rest/items?fields=name%2Cstate"])

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

    def test_one_connection_reports_open_and_projects_before_yielding(self):
        async def check():
            client = HTTPClient()
            opened = []
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client) as factory:
                iterator = self.transport().subscribe(WILDCARDS, project=lambda event: {"safe": event["value"]} if event["value"] != "drop" else None,
                                                      opened=lambda: opened.append(True))
                pending = asyncio.create_task(anext(iterator))
                await client.queue.put(b'data: {"value":"drop","raw":"private-sentinel"}\n\n')
                await client.queue.put(b'data: {"value":"public","raw":"private-sentinel"}\n\n')
                self.assertEqual(await pending, {"safe": "public"})
                self.assertEqual((factory.call_count, len(client.opened), opened), (1, 1, [True]))
                self.assertEqual(unquote(client.opened[0].split("?topics=")[1]), ",".join(WILDCARDS))
                await client.queue.put("fail")
                with self.assertRaises(ValueError):
                    await anext(iterator)
                self.assertCountEqual(client.closed, client.opened)
                self.assertTrue(client.exited)
        async_to_sync(check)()

    def test_quiet_cancel_and_oversize_frame_close_the_connection(self):
        async def check(oversize):
            client = HTTPClient()
            with patch("oh_identity.transport.httpx.AsyncClient", return_value=client):
                iterator = self.transport().subscribe(WILDCARDS, project=lambda event: event)
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
