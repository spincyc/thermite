"""The gateway's one upstream subscription, its cache and the client streams
it serves. A fake upstream feeds native events; no socket or JVM is used."""

import asyncio
import json
import threading
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.test import Client, TransactionTestCase, override_settings

from oh_identity import hub as hubs
from oh_identity import streams, topics
from oh_identity.models import PersonalToken, SecurityState
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.security import get_state
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE

WILDCARDS = ["openhab/items/*/stateupdated", "openhab/items/*/statechanged", "openhab/items/*/command"]
#: A pairing code the runtime holds, which no client frame may carry.
CODE = "1234"
#: A quiet stream's heartbeat: a data frame, which an EventSource delivers
#: (a comment it would hide), so a client can tell a quiet stream from a dead one.
HEARTBEAT = {"type": "heartbeat"}


def contract():
    return RuntimeContract({
        "den": {"source_item": "loc_den_source", "sources": ["player"]},
        "desk": {"source_item": "loc_desk_source", "sources": ["player"]}},
        states={"loc_den_source": {"kind": "enum", "values": ["player", "off"]},
                "amp_volume": {"kind": "number", "max": 98, "commands": True},
                "amp_problem": {"kind": "problem"},
                "matrix_label": {"kind": "redacted"},
                "tv_pairing": {"kind": "enum", "commands": True},
                "tv_pairing_status": {"kind": "pairing_status"},
                "tv_pairing_result": {"kind": "pairing_result"},
                "amp_token": {"kind": "enum", "values": ["x"]}},
        views={"remote": {"stateupdated": ["loc_den_source", "loc_desk_source"],
                          "statechanged": ["loc_den_source", "amp_volume", "amp_problem", "matrix_label",
                                           "tv_pairing_status", "tv_pairing", "amp_token", "unknown_item"],
                          "command": ["amp_volume", "loc_den_source", "tv_pairing", "tv_pairing_status"]},
               "home": {"statechanged": ["loc_den_source", "loc_desk_source"]},
               "pair": {"stateupdated": ["tv_pairing_status", "tv_pairing_result"]}})


def event(item, value, verb="stateupdated", old=None, **extra):
    payload = {"type": "String", "value": value, **extra}
    if old is not None:
        payload["oldValue"] = old
    return {"topic": f"openhab/items/{item}/{verb}", "type": topics.TYPES[verb], "payload": json.dumps(payload)}


def decode(data):
    return json.loads(data.decode()[len("data: "):]) if data.startswith(b"data: ") else data


class Connection:
    def __init__(self, filters):
        self.filters, self.queue, self.closed = list(filters), asyncio.Queue(), False


class Upstream:
    """A native runtime whose subscribe() is one upstream connection fed by
    the test, and whose states() is the one bulk read of every Item."""

    def __init__(self, values=None):
        self.values = dict(values or {"loc_den_source": "player", "loc_desk_source": "off", "amp_volume": "40",
                                      "amp_problem": "UNDEF", "matrix_label": "private label", "amp_token": "x",
                                      "tv_pairing": CODE, "tv_pairing_status": "paired",
                                      "tv_pairing_result": json.dumps({"state": "paired", "pin": CODE})})
        self.connections, self.reads, self.commands = [], [], []
        self.hold = None  # A threading.Event a read waits for.

    def command(self, item, value):
        self.commands.append((item, value))

    def read(self, item):
        return self.values[item]

    def states(self, names):
        self.reads.append(sorted(names))
        values = dict(self.values)
        if self.hold:
            self.hold.wait(2)
        return {name: values[name] for name in names if name in values}

    async def subscribe(self, filters, *, project, opened=None):
        connection = Connection(filters)
        self.connections.append(connection)
        try:
            if opened:
                opened()
            while True:
                value = await connection.queue.get()
                if isinstance(value, BaseException):
                    raise value
                projected = project(value)
                if projected is not None:
                    yield projected
        finally:
            connection.closed = True

    def send(self, value, connection=-1):
        self.connections[connection].queue.put_nowait(value)


async def frame(frames, timeout=1):
    return decode(await asyncio.wait_for(anext(frames), timeout))


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@override_settings(ALLOWED_HOSTS=["testserver"])
class EventHub(TransactionTestCase):
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        self.guest = {"guest": True, "security_revision": get_state().revision}

    def check(self, body):
        async def run():
            upstream = Upstream()
            adapter = RuntimeAdapter(contract(), upstream)
            await body(upstream, adapter)
        async_to_sync(run)()

    def test_views_keep_only_public_topics_and_one_state_verb_per_item(self):
        views = topics.views(contract())
        self.assertEqual(views["remote"], {
            "openhab/items/loc_den_source/stateupdated", "openhab/items/loc_desk_source/stateupdated",
            "openhab/items/amp_volume/statechanged", "openhab/items/amp_problem/statechanged",
            "openhab/items/matrix_label/statechanged", "openhab/items/tv_pairing_status/statechanged",
            "openhab/items/amp_volume/command"})
        self.assertEqual(views["home"], {"openhab/items/loc_den_source/statechanged", "openhab/items/loc_desk_source/statechanged"})
        self.assertEqual(views["pair"], {"openhab/items/tv_pairing_status/stateupdated", "openhab/items/tv_pairing_result/stateupdated"})
        self.assertFalse(any("token" in topic or "/tv_pairing/" in topic for view in views.values() for topic in view))
        # A contract compiled without views still has all three.
        derived = topics.views(RuntimeContract({"den": {"source_item": "loc_den_source", "sources": ["player"]}},
                                               states={"tv_pairing_status": {"kind": "pairing_status"}}))
        self.assertEqual(derived["home"], {"openhab/items/loc_den_source/statechanged"})
        self.assertIn("openhab/items/loc_den_source/stateupdated", derived["remote"])
        self.assertEqual(derived["pair"], {"openhab/items/tv_pairing_status/stateupdated"})
        self.assertEqual(hubs.upstream_filters(frozenset().union(*views.values())), WILDCARDS)

    def test_one_upstream_serves_every_stream_and_each_view_its_topics(self):
        async def body(upstream, adapter):
            opened = {view: [streams.stream(adapter, view, self.guest) for _ in range(count)]
                      for view, count in (("remote", 3), ("home", 2), ("pair", 1))}
            first = {view: [await frame(each) for each in frames] for view, frames in opened.items()}
            self.assertEqual(len(upstream.connections), 1)
            self.assertEqual(upstream.connections[0].filters, WILDCARDS)
            self.assertEqual(len(upstream.reads), 1)
            # Snapshot first: the view's Items, projected.
            remote = first["remote"][0]
            self.assertEqual((remote["type"], remote["view"]), ("snapshot", "remote"))
            self.assertEqual(remote["states"]["loc_den_source"], "player")
            self.assertEqual(remote["states"]["amp_volume"], "40")
            self.assertTrue(remote["states"]["matrix_label"].startswith("hidden:"))
            self.assertEqual(set(remote["states"]), {"loc_den_source", "loc_desk_source", "amp_volume", "amp_problem",
                                                     "matrix_label", "tv_pairing_status"})
            self.assertEqual(first["home"][0]["states"], {"loc_den_source": "player", "loc_desk_source": "off"})
            self.assertEqual(first["pair"][0]["states"]["tv_pairing_result"], json.dumps({"state": "paired"}, separators=(",", ":")))
            self.assertTrue(all(each == first[view][0] for view in first for each in first[view]))
            hub = hubs.hub_for(adapter)
            self.assertEqual(hub.status()["streams"], {"remote": 3, "home": 2, "pair": 1})

            upstream.send(event("loc_den_source", "off"))
            upstream.send(event("loc_den_source", "off", "statechanged", old="player"))
            upstream.send(event("tv_pairing_status", "code_shown"))
            upstream.send(event("tv_pairing_status", "code_shown", "statechanged", old="paired"))
            for each in opened["remote"]:
                self.assertEqual([(await frame(each))["topic"] for _ in range(2)],
                                 ["openhab/items/loc_den_source/stateupdated", "openhab/items/tv_pairing_status/statechanged"])
            for each in opened["home"]:
                self.assertEqual((await frame(each))["topic"], "openhab/items/loc_den_source/statechanged")
            self.assertEqual((await frame(opened["pair"][0]))["topic"], "openhab/items/tv_pairing_status/stateupdated")
            with patch("oh_identity.streams.HEARTBEAT", .02):
                for frames in opened.values():
                    for each in frames:
                        self.assertEqual(await frame(each), HEARTBEAT)
                        await each.aclose()
            self.assertEqual(hub.status()["streams"], {"remote": 0, "home": 0, "pair": 0})
            self.assertEqual(len(upstream.connections), 1)
        self.check(body)

    def test_pairing_commands_and_private_items_never_reach_a_client(self):
        async def body(upstream, adapter):
            frames = streams.stream(adapter, "remote", self.guest)
            snapshot = await frame(frames)
            self.assertNotIn("amp_token", snapshot["states"])
            hub = hubs.hub_for(adapter)
            sentinel = "synthetic-private-sentinel"
            for private in (event("tv_pairing", "4321", "command"), event("tv_pairing", "4321", "statechanged", old="1"),
                            event("amp_token", "x", "statechanged", old=sentinel), event("unlisted_secret", sentinel),
                            {"topic": "openhab/items/amp_volume/command", "type": "ItemStateEvent", "payload": "{}"},
                            {"topic": "openhab/items/amp_volume/command", "payload": object()}):
                self.assertIsNone(hub.projection(private))
                upstream.send(private)
            upstream.send(event("amp_volume", "47", "command", cookie=sentinel))
            upstream.send(event("amp_volume", "470", "statechanged", old="40"))
            upstream.send(event("matrix_label", "other private label", "statechanged", old="private label"))
            command = await frame(frames)
            self.assertEqual(command["topic"], "openhab/items/amp_volume/command")
            self.assertEqual(json.loads(command["payload"]), {"type": "String", "value": "47"})
            self.assertEqual(json.loads((await frame(frames))["payload"]), {"type": "String", "value": "UNDEF", "oldValue": "40"})
            label = json.loads((await frame(frames))["payload"])
            self.assertTrue(label["value"].startswith("hidden:") and label["oldValue"].startswith("hidden:"))
            self.assertNotIn("private", json.dumps(label))
            self.assertNotIn("tv_pairing", hub.values)
            self.assertNotIn("amp_token", hub.values)
            await frames.aclose()
        self.check(body)

    @patch("oh_identity.streams.REVALIDATE", 0)
    def test_revalidation_still_ends_a_revoked_stream(self):
        admin = bootstrap("owner", PASSPHRASE)
        token, _ = issue_token(admin, "stream")
        snapshot = {"user": admin.pk, "revision": admin.auth_revision, "token": str(token.pk),
                    "security_revision": get_state().revision}

        async def body(upstream, adapter):
            frames = streams.stream(adapter, "remote", snapshot)
            self.assertEqual((await frame(frames))["type"], "snapshot")
            upstream.send(event("loc_den_source", "off"))
            self.assertEqual(json.loads((await frame(frames))["payload"])["value"], "off")
            await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=True)
            upstream.send(event("loc_den_source", "player"))
            with self.assertRaises(StopAsyncIteration):
                await frame(frames)
            self.assertEqual(hubs.hub_for(adapter).status()["streams"]["remote"], 0)
            # A quiet stream is fenced at its next heartbeat.
            await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=False)
            frames = streams.stream(adapter, "remote", snapshot)
            await frame(frames)
            await sync_to_async(PersonalToken.objects.filter(pk=token.pk).update)(revoked=True)
            with patch("oh_identity.streams.HEARTBEAT", .02), self.assertRaises(StopAsyncIteration):
                await frame(frames)
        self.check(body)

    def test_a_client_that_stops_reading_is_resynced_within_a_bounded_queue(self):
        async def body(upstream, adapter):
            with patch("oh_identity.hub.QUEUE", 4):
                frames = streams.stream(adapter, "remote", self.guest)
                await frame(frames)
                hub = hubs.hub_for(adapter)
                subscriber = next(iter(hub.subscribers["remote"]))
                most = 0
                for index in range(50):
                    upstream.send(event("amp_volume", str(index), "statechanged", old=str(index - 1)))
                    await settle()
                    most = max(most, len(subscriber.frames))
                self.assertLessEqual(most, 4)
                self.assertGreater(hub.counters["overflows"], 0)
                received = [await frame(frames) for _ in range(len(subscriber.frames))]
                snapshots = [each for each in received if each.get("type") == "snapshot"]
                self.assertEqual(len(snapshots), 1)
                values = [snapshots[0]["states"]["amp_volume"]] + [json.loads(each["payload"])["value"]
                                                                    for each in received[received.index(snapshots[0]) + 1:]]
                self.assertEqual(values[-1], "49")
                await frames.aclose()
        self.check(body)

    def test_upstream_reconnects_and_resyncs_with_newer_events_winning(self):
        async def body(upstream, adapter):
            with patch("oh_identity.hub.BACKOFF", (0,)):
                frames = streams.stream(adapter, "remote", self.guest)
                self.assertEqual((await frame(frames))["states"]["loc_den_source"], "player")
                hub = hubs.hub_for(adapter)
                upstream.values.update(loc_den_source="off", amp_volume="30")
                upstream.hold = threading.Event()
                upstream.send(ConnectionError("synthetic upstream loss"))
                self.assertEqual(await frame(frames), {"type": "unavailable"})
                self.assertEqual(hub.status()["upstream"], "connecting")
                for _ in range(100):
                    if len(upstream.connections) == 2 and len(upstream.reads) == 2:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(upstream.connections[0].closed)
                # The read began before this event; the event is newer.
                upstream.send(event("amp_volume", "35", "statechanged", old="30"))
                await settle()
                upstream.hold.set()
                snapshot = await frame(frames)
                self.assertEqual(snapshot["type"], "snapshot")
                self.assertEqual((snapshot["states"]["loc_den_source"], snapshot["states"]["amp_volume"]), ("off", "35"))
                self.assertEqual((hub.counters["upstream_connects"], hub.counters["upstream_failures"], hub.counters["resyncs"]), (2, 1, 2))
                upstream.send(event("loc_den_source", "player"))
                self.assertEqual(json.loads((await frame(frames))["payload"])["value"], "player")
                await frames.aclose()
        self.check(body)

    def test_upstream_closes_after_the_last_stream_lingers_and_observers_see_lifecycle(self):
        seen = []

        async def body(upstream, adapter):
            with patch("oh_identity.hub.LINGER", .05), patch.object(hubs, "observers", [lambda kind, fields: seen.append((kind, fields))]):
                meta = {"view": "home", "origin": "https", "principal": "guest", "http_version": "2", "peer": "digest"}
                frames = streams.stream(adapter, "home", self.guest, meta=meta)
                await frame(frames)
                hub = hubs.hub_for(adapter)
                self.assertEqual(hub.status()["upstream"], "serving")
                await frames.aclose()
                for _ in range(100):
                    if hub.status()["upstream"] == "closed":
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(upstream.connections[0].closed)
                self.assertEqual(hub.status()["upstream"], "closed")
                frames = streams.stream(adapter, "home", self.guest)
                self.assertEqual((await frame(frames))["type"], "snapshot")
                self.assertEqual((len(upstream.connections), len(upstream.reads)), (2, 2))
                await frames.aclose()
        self.check(body)
        kinds = [kind for kind, _ in seen]
        self.assertEqual(kinds[:4], ["stream_open", "upstream_open", "resync", "stream_close"])
        self.assertEqual(seen[0][1], {"view": "home", "origin": "https", "principal": "guest", "http_version": "2", "peer": "digest"})
        self.assertEqual(seen[3][1]["reason"], "client")
        self.assertNotIn("player", json.dumps(seen))


@override_settings(ALLOWED_HOSTS=["testserver"])
class EventEndpoints(TransactionTestCase):
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        install_adapter(RuntimeAdapter(contract(), Upstream()))
        self.admin = bootstrap("owner", PASSPHRASE)
        _, self.token = issue_token(self.admin, "stream")

    def get(self, path, **extra):
        response = Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True, **extra)
        self.addCleanup(response.close)
        return response

    def test_one_endpoint_per_view_and_pairing_needs_an_admin(self):
        for view in ("remote", "home"):
            response = self.get("/api/v1/events/" + view)
            self.assertEqual((response.status_code, response["Content-Type"], response["Cache-Control"]),
                             (200, "text/event-stream", "no-store"))
        self.assertEqual(self.get("/api/v1/events/pair").status_code, 401)
        member = __import__("oh_identity.models", fromlist=["User"]).User.objects.create_user("member", password=PASSPHRASE)
        _, raw = issue_token(member, "member")
        self.assertEqual(self.get("/api/v1/events/pair", HTTP_AUTHORIZATION="Bearer " + raw).status_code, 403)
        self.assertEqual(self.get("/api/v1/events/pair", HTTP_AUTHORIZATION="Bearer " + self.token).status_code, 200)
        for path in ("/api/v1/events/remote?topics=openhab/items/loc_den_source/stateupdated", "/api/v1/events/other",
                     "/rest/events?topics=openhab/items/loc_den_source/stateupdated"):
            self.assertEqual(self.get(path).status_code, 404, path)
        refused = Client().post("/api/v1/events/remote", secure=True)
        self.assertEqual((refused.status_code, refused["Allow"]), (405, "GET"))

    def test_streams_are_admitted_per_peer_and_in_total(self):
        path = "/api/v1/events/remote"
        with patch("oh_identity.streams.slots", streams.Slots(per_peer=2, total=3)) as slots:
            first = [Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True) for _ in range(2)]
            self.assertEqual([response.status_code for response in first], [200, 200])
            refused = Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True)
            self.assertEqual((refused.status_code, refused.json()["error"]), (429, "too_many_streams"))
            self.assertIn("Retry-After", refused)
            other = Client(REMOTE_ADDR="2001:db8::1").get(path, secure=True)
            self.assertEqual(other.status_code, 200)
            self.assertEqual(Client(REMOTE_ADDR="2001:db8:1::1").get(path, secure=True).status_code, 429)
            for response in (*first, other):
                response.close()
            self.assertEqual(slots.count(), 0)
            release = slots.acquire("peer")
            release()
            release()
            self.assertEqual(slots.count(), 0)
