"""Client telemetry (POST /api/v1/telemetry), the gateway's metering
wrapper, its event-stream metrics and its finite problem catalogue."""

import json
import re
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings

from oh_access import metrics
from oh_identity import auth, streams, telemetry
from oh_identity import hub as hubs
from oh_identity.models import SecurityState
from oh_identity.runtime import RuntimeAdapter, install_adapter
from oh_identity.security import get_state
from oh_identity.tests.test_core import adapter
from oh_identity.tests.test_event_hub import Upstream, contract, frame

REPORT = {"client": "web", "protocol": "h2", "events": {"policy_timeout": {"count": 3, "waits": [8.0, 8.5]},
                                                       "stream_error": {"count": 1}}}


def post(client, data, *, content_type="application/json", **headers):
    body = data if isinstance(data, (str, bytes)) else json.dumps(data)
    return client.post("/api/v1/telemetry", body, content_type=content_type, secure=True, headers=headers)


class Schema(SimpleTestCase):
    def test_a_full_report_parses(self):
        client, protocol, events = telemetry.parse(json.dumps(REPORT).encode())
        self.assertEqual((client, protocol), ("web", "h2"))
        self.assertEqual(events, {"policy_timeout": (3, [8.0, 8.5]), "stream_error": (1, [])})
        self.assertEqual(telemetry.parse(b'{"client":"tui","events":{"locks_timeout":{"count":1}}}')[1], "unknown")

    def test_the_largest_valid_report_fits(self):
        events = {kind: {"count": telemetry.MAX_COUNT, "waits": [float(telemetry.MAX_WAIT)] * telemetry.MAX_WAITS}
                  for kind in metrics.CLIENT_KINDS}
        raw = json.dumps({"client": "web", "protocol": "http/1.1", "events": events}).encode()
        self.assertLessEqual(len(raw), telemetry.REPORT_BYTES)
        telemetry.parse(raw)

    def test_anything_outside_the_schema_is_refused(self):
        bad = [
            b"", b"[]", b"not json", b"{" * 5000, b"x" * (telemetry.REPORT_BYTES + 1),
            {"client": "web"}, {"client": "phone", "events": {"stream_error": {"count": 1}}},
            {"client": "web", "protocol": "spdy", "events": {"stream_error": {"count": 1}}},
            {"client": "web", "events": {}}, {"client": "web", "events": {"free text": {"count": 1}}},
            {"client": "web", "events": {"stream_error": {"count": 0}}},
            {"client": "web", "events": {"stream_error": {"count": telemetry.MAX_COUNT + 1}}},
            {"client": "web", "events": {"stream_error": {"count": True}}},
            {"client": "web", "events": {"stream_error": {"count": 1.5}}},
            {"client": "web", "events": {"stream_error": {"count": 1, "waits": [1, 2]}}},
            {"client": "web", "events": {"stream_error": {"count": 1, "waits": [-1]}}},
            {"client": "web", "events": {"stream_error": {"count": 1, "waits": [telemetry.MAX_WAIT + 1]}}},
            {"client": "web", "events": {"stream_error": {"count": 1, "waits": ["1"]}}},
            {"client": "web", "events": {"stream_error": {"count": 1, "note": "hello"}}},
            {"client": "web", "events": {"stream_error": {"count": 1}}, "user": "example"},
        ]
        for value in bad:
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            with self.subTest(raw=raw[:60]), self.assertRaises(telemetry.Invalid):
                telemetry.parse(raw)
        with self.assertRaises(telemetry.Invalid):
            telemetry.parse(b'{"client":"web","events":{"stream_error":{"count":1,"waits":[NaN]}}}')


class Limits(SimpleTestCase):
    def test_each_peer_and_everyone_spend_tokens(self):
        now = [0.0]
        limits = telemetry.Limits(per_peer=2, window=60, total=3, clock=lambda: now[0])
        self.assertEqual([limits.take("a"), limits.take("a")], [0, 0])
        self.assertAlmostEqual(limits.take("a"), 30)
        self.assertEqual(limits.take("b"), 0)
        self.assertGreater(limits.take("c"), 0, "the shared bucket is empty")
        now[0] = 30.0
        self.assertEqual(limits.take("a"), 0)

    def test_peers_are_forgotten_oldest_first(self):
        limits = telemetry.Limits(per_peer=1, total=100, peers=2, clock=lambda: 0.0)
        for peer in ("a", "b", "c"):
            limits.take(peer)
        self.assertEqual(list(limits._buckets), ["b", "c"])
        self.assertEqual(limits.take("a"), 0, "a forgotten peer starts full")


class Endpoint(TestCase):
    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True, "revision": 1})
        install_adapter(adapter())
        self.addCleanup(setattr, telemetry, "limits", telemetry.limits)
        telemetry.limits = telemetry.Limits(per_peer=2, total=100)

    def test_an_anonymous_page_reports_without_a_csrf_token(self):
        events = metrics.CLIENT_EVENTS.labels("web", "policy_timeout")
        waits = metrics.CLIENT_WAIT.labels("web", "policy_timeout")
        reports = metrics.CLIENT_REPORTS.labels("web", "h2")
        before = (events._value.get(), waits._sum.get(), reports._value.get())
        client = Client(enforce_csrf_checks=True)
        with self.assertLogs("oh_identity.telemetry", "WARNING") as logged:
            response = post(client, REPORT, Origin="https://testserver")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertFalse(response.cookies)
        self.assertEqual(events._value.get(), before[0] + 3)
        self.assertEqual(waits._sum.get(), before[1] + 16.5)
        self.assertEqual(reports._value.get(), before[2] + 1)
        line = logged.output[0]
        self.assertRegex(line, r"client=web protocol=h2 peer=[0-9a-f]{8} policy_timeout=3\(max 8\.5s\) stream_error=1")
        self.assertNotIn("127.0.0.1", line)

    def test_refusals(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get("/api/v1/telemetry", secure=True).status_code, 405)
        cases = [
            (post(client, REPORT, content_type="text/plain"), 415, "json_required"),
            (post(client, REPORT, Origin="https://attacker.example"), 403, "invalid_origin"),
            (post(client, REPORT, Origin="null"), 403, "invalid_origin"),
            (post(client, {"client": "web", "events": {"bogus": {"count": 1}}}), 400, "invalid_telemetry"),
        ]
        for response, status, code in cases:
            with self.subTest(code=code):
                self.assertEqual((response.status_code, json.loads(response.content)["error"]), (status, code))
        self.assertEqual(post(client, REPORT).status_code, 204, "the second of two reports")
        throttled = post(client, REPORT)
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(json.loads(throttled.content)["error"], "telemetry_throttled")
        self.assertIn("Retry-After", throttled)

    def test_oversized_bodies_are_refused_before_they_are_read(self):
        response = post(Client(), "x" * (telemetry.REPORT_BYTES + 1))
        self.assertEqual((response.status_code, json.loads(response.content)["error"]), (400, "invalid_telemetry"))


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


class Metering(SimpleTestCase):
    def run_app(self, path, status=None, scope=None):
        sent = []

        async def application(scope, receive, send):
            if status is not None:
                await send({"type": "http.response.start", "status": status, "headers": []})
                await send({"type": "http.response.body", "body": b""})

        async def send(message):
            sent.append(message)

        async_to_sync(metrics.Metered(application))({"type": "http", "path": path, **(scope or {})}, _receive, send)
        return sent

    def test_requests_are_counted_by_listener_route_and_status_class(self):
        refused = metrics.REQUESTS.labels("https", "command", "5xx")
        aborted = metrics.REQUESTS.labels("https", "events", "aborted")
        timed = metrics.REQUEST_SECONDS.labels("command")
        before = (refused._value.get(), aborted._value.get(), timed._sum.get())
        self.run_app("/api/v1/rooms/example_west/commands", 503)
        self.run_app("/api/v1/events/remote")
        self.assertEqual(refused._value.get(), before[0] + 1)
        self.assertEqual(aborted._value.get(), before[1] + 1)
        self.assertGreaterEqual(timed._sum.get(), before[2])
        self.assertEqual(metrics.IN_FLIGHT.labels("command")._value.get(), 0)

    def test_other_scopes_pass_through(self):
        seen = []

        async def application(scope, receive, send):
            seen.append(scope["type"])

        async_to_sync(metrics.Metered(application))({"type": "lifespan"}, _receive, None)
        self.assertEqual(seen, ["lifespan"])


def hub_samples():
    return {(sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in metrics.Hub().collect() for sample in family.samples}


@override_settings(ALLOWED_HOSTS=["testserver"])
class StreamMetering(TransactionTestCase):
    """Event-stream metrics come from oh_identity.hub's observers and
    EventHub.status(), never from hooks of their own."""

    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        observing = patch.object(hubs, "observers", [])
        observing.start()
        self.addCleanup(observing.stop)
        metrics.watch_streams()
        metrics.watch_streams()

    def test_streams_are_counted_by_view_version_and_peer_and_the_hub_reports_its_state(self):
        self.assertEqual(hubs.observers, [metrics.observe])
        open_streams = metrics.SSE_STREAMS.labels("home", "2")
        ended = metrics.SSE_SECONDS.labels("client")
        guest = {"guest": True, "security_revision": get_state().revision}
        meta = {"view": "home", "origin": "https", "principal": "guest", "http_version": "2", "peer": "digest"}

        async def body():
            adapter = RuntimeAdapter(contract(), Upstream())
            before = (open_streams._value.get(), ended._sum.get(), hub_samples())
            pages = [streams.stream(adapter, "home", guest, meta=meta) for _ in range(2)]
            for page in pages:
                self.assertEqual((await frame(page))["type"], "snapshot")
            metrics.refresh_streams()
            self.assertEqual(open_streams._value.get(), before[0] + 2)
            self.assertGreaterEqual(metrics.SSE_PEER_MAX.labels("2")._value.get(), 2)
            during = hub_samples()
            self.assertEqual(during[("thermite_gateway_upstream_state", (("state", "serving"),))], 1)
            self.assertEqual(during[("thermite_gateway_upstream_state", (("state", "closed"),))], 0)
            self.assertEqual(during[("thermite_gateway_upstream_connects_total", ())],
                             before[2][("thermite_gateway_upstream_connects_total", ())] + 1)
            self.assertGreater(during[("thermite_gateway_sse_frames_total", ())],
                               before[2][("thermite_gateway_sse_frames_total", ())])
            for page in pages:
                await page.aclose()
            metrics.refresh_streams()
            self.assertEqual(open_streams._value.get(), before[0])
            self.assertGreaterEqual(ended._sum.get(), before[1])
        async_to_sync(body)()
        # The lifetime histogram names only how a stream ended.
        self.assertEqual(set(metrics.SSE_ENDS), {"client", "revoked", "error"})

    def test_refusals_name_the_full_limit(self):
        install_adapter(RuntimeAdapter(contract(), Upstream()))
        path = "/api/v1/events/home"
        peer, total = metrics.SSE_REFUSED.labels("peer"), metrics.SSE_REFUSED.labels("total")
        before = (peer._value.get(), total._value.get())
        with patch("oh_identity.streams.slots", streams.Slots(per_peer=1, total=2)):
            first = Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True)
            self.assertEqual(Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True).status_code, 429)
            second = Client(REMOTE_ADDR="2001:db8::1").get(path, secure=True)
            self.assertEqual(Client(REMOTE_ADDR="2001:db8:1::1").get(path, secure=True).status_code, 429)
            for response in (first, second):
                response.close()
        self.assertEqual((peer._value.get(), total._value.get()), (before[0] + 1, before[1] + 1))

    def test_without_a_hub_the_subscription_is_closed(self):
        with patch.object(hubs, "_hubs", {}):
            samples = hub_samples()
        self.assertEqual(samples[("thermite_gateway_upstream_state", (("state", "closed"),))], 1)
        self.assertEqual(samples[("thermite_gateway_upstream_failures_total", ())], 0)


class ProblemCatalogue(SimpleTestCase):
    def test_every_problem_code_in_the_identity_app_is_catalogued(self):
        root = Path(auth.__file__).parent
        found = set()
        for source in root.glob("*.py"):
            text = source.read_text()
            found |= set(re.findall(r'(?:Problem|failure)\(\s*"([a-z_]+)"\s*[,)]', text))
        # services.py builds "invalid_" + field for its two boolean fields.
        found |= {"invalid_active", "invalid_admin"}
        self.assertGreater(len(found), 40)
        self.assertLessEqual(found, auth.PROBLEMS, sorted(found - auth.PROBLEMS))

    def test_an_unknown_code_is_counted_as_other(self):
        other = metrics.PROBLEMS.labels("other")
        before = other._value.get()
        auth.failure("not_a_catalogued_code", 400)
        self.assertEqual(other._value.get(), before + 1)
