"""Offline ASGI probes for the optional finite Sonos callback ingress."""

import asyncio
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_access import AccessError
from oh_access.callbacks import CallbackRelay, MAX_BODY, notification, xml_notification, NotificationError

CALLBACK = "/upnpcallback/dev/RINCON_02000000000101400_MR/svc/upnp-org/RenderingControl/event/cb"
PEER = "192.0.2.40"
HOST = "192.0.2.2:8088"
BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
        b'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
        b'<e:property><LastChange>&lt;Event&gt;private fixture title&lt;/Event&gt;</LastChange>'
        b'</e:property></e:propertyset>')


def scope(body=BODY):
    return {"type": "http", "method": "NOTIFY", "path": CALLBACK, "raw_path": CALLBACK.encode(),
            "query_string": b"", "client": (PEER, 1400),
            "headers": [(b"host", HOST.encode()), (b"nt", b"upnp:event"), (b"nts", b"upnp:propchange"),
                        (b"sid", b"uuid:RINCON_02000000000101400_sub0000001234"), (b"seq", b"0"),
                        (b"content-type", b'text/xml; charset="utf-8"'), (b"content-length", str(len(body)).encode())]}


def replace_header(value, name, data):
    value["headers"] = [(key, val) for key, val in value["headers"] if key.lower() != name.lower()]
    value["headers"].append((name, data))


class FakeClient:
    def __init__(self, status=200, block=None):
        self.status, self.block = status, block
        self.calls, self.closed = [], 0
        self.entered = asyncio.Event()

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        self.entered.set()
        try:
            if self.block:
                await self.block.wait()
            response = type("Response", (), {"status_code": self.status})()
            yield response
        finally:
            self.closed += 1


async def request(app, value=None, body=BODY, events=None):
    sent = []
    pending = iter(events or [{"type": "http.request", "body": body, "more_body": False}])
    async def receive():
        return next(pending)
    async def send(event):
        sent.append(event)
    await app(value or scope(body), receive, send)
    return sent


class Validation(unittest.TestCase):
    def test_exact_route_map_no_synthesized_udn_or_service_permission(self):
        app = CallbackRelay({CALLBACK: PEER}, "http://127.0.0.1:8080", [HOST], client=FakeClient())
        self.assertEqual(app.routes, {CALLBACK: PEER})
        for path in (CALLBACK.replace("event/cb", "action"), CALLBACK.replace("RenderingControl", "SetVolume"),
                     CALLBACK.replace("upnp-org", "schemas-upnp-org"), CALLBACK + ".xml"):
            with self.assertRaises(AccessError):
                CallbackRelay({path: PEER}, "http://127.0.0.1:8080", [HOST], client=FakeClient())
        other = scope()
        other["path"] = CALLBACK.replace("_MR", "_MS")
        other["raw_path"] = other["path"].encode()
        with self.assertRaises(NotificationError):
            notification(other, app.routes, app.hosts)

    def test_fixed_loopback_no_dns_credentials_redirect_target_or_path(self):
        for upstream in ("http://example.test:8080", "https://127.0.0.1:8080", "http://localhost:8080", "http://127.0.0.1:8080/rest", "http://x:y@127.0.0.1:8080", "http://127.0.0.1:8080?next=http://evil", "http://127.0.0.1"):
            with self.assertRaises(AccessError):
                CallbackRelay({CALLBACK: PEER}, upstream, [HOST], client=FakeClient())

    def test_injected_transports_cannot_add_ambient_credentials_proxies_or_redirects(self):
        # The header's name is what the relay refuses; its value is a placeholder
        # so the public package's literal-credential scan accepts this file.
        for key,value in (("auth",object()),("_trust_env",True),("follow_redirects",True),
                          ("headers",{"Authorization":"<synthetic>"})):
            client=FakeClient();setattr(client,key,value)
            with self.assertRaises(AccessError):
                CallbackRelay({CALLBACK:PEER},'http://127.0.0.1:8080',[HOST],client=client)

    def test_finite_gena_headers_include_sonos_sid_and_uint32_boundaries(self):
        for sequence in (b"0", b"1", b"4294967295"):
            value = scope()
            replace_header(value, b"seq", sequence)
            _, _, forwarded = notification(value, {CALLBACK: PEER}, {HOST})
            self.assertEqual(forwarded["SEQ"], sequence.decode())
            self.assertEqual(set(forwarded), {"SID", "SEQ", "NT", "NTS", "Content-Type", "Content-Length"})
        for name, invalid in ((b"seq", b"4294967296"), (b"seq", b"-1"), (b"sid", b"not-a-sid"),
                              (b"sid", b"uuid:injected\r\nX-Test: x"), (b"nt", b"upnp:rootdevice"), (b"nts", b"upnp:alive")):
            value = scope()
            replace_header(value, name, invalid)
            with self.assertRaises(NotificationError):
                notification(value, {CALLBACK: PEER}, {HOST})

    def test_xml_rejects_entities_external_access_and_wrong_envelope(self):
        xml_notification(BODY)
        attacks = [b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>',
                   BODY.replace(b"private fixture title", b"&lt;!DOCTYPE x&gt;"),
                   BODY.replace(b"private fixture title", b"&amp;lt;!ENTITY x&amp;gt;"),
                   b'<propertyset><property><Volume>20</Volume></property></propertyset>',
                   b'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"/>',
                   BODY[:-20], b'\xff\xfe'+BODY, b'x'*(MAX_BODY+1)]
        for body in attacks:
            with self.subTest(body=body[:30]), self.assertRaises(NotificationError):
                xml_notification(body)


class Relay(unittest.IsolatedAsyncioTestCase):
    def app(self, client=None, **kwargs):
        self.client = client or FakeClient()
        self.receipts = []
        return CallbackRelay({CALLBACK: PEER}, "http://127.0.0.1:8080", [HOST], client=self.client, receipt=self.receipts.append, **kwargs)

    async def test_one_native_notify_without_credentials_and_no_body_or_sid_echo(self):
        app = self.app()
        value = scope()
        value["headers"].extend([(b"x-forwarded-for", b"203.0.113.99"), (b"user-agent", b"Sonos fixture")])
        sent = await request(app, value)
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(self.client.calls[0]["method"], "NOTIFY")
        self.assertEqual(self.client.calls[0]["url"], "http://127.0.0.1:8080" + CALLBACK)
        self.assertEqual(self.client.calls[0]["content"], BODY)
        headers = self.client.calls[0]["headers"]
        self.assertFalse(set(headers) & {"Authorization", "Cookie", "Host", "Forwarded", "X-Forwarded-For"})
        self.assertEqual(self.client.closed, 1)
        self.assertNotIn("private fixture title", json.dumps([sent, self.receipts], default=str))
        self.assertNotIn("sub0000001234", json.dumps([sent, self.receipts], default=str))

    async def test_other_methods_paths_peers_and_host_never_read_body_or_forward(self):
        app = self.app()
        values = []
        for method in ("GET", "POST", "SUBSCRIBE", "UNSUBSCRIBE", "PUT", "OPTIONS"):
            value = scope();value["method"] = method;values.append(value)
        for field, data in (("path", "/rest/items/proof_power"), ("raw_path", CALLBACK.replace("/dev/", "/%64ev/").encode()),
                            ("query_string", b"url=http://evil"), ("client", ("203.0.113.8", 1234))):
            value = scope();value[field] = data;values.append(value)
        value = scope();replace_header(value, b"host", b"evil.example");values.append(value)
        for value in values:
            async def unread():
                raise AssertionError("Rejected request must not read its body")
            sent = []
            async def send(event):sent.append(event)
            await app(value, unread, send)
            self.assertIn(sent[0]["status"], {403, 404, 405})
        self.assertEqual(self.client.calls, [])

    async def test_duplicate_headers_chunked_and_browser_credentials_never_forward(self):
        app = self.app()
        variants = []
        for name in (b"sid", b"content-length", b"host", b"seq"):
            value = scope();existing=next(val for key,val in value["headers"] if key==name)
            value["headers"].append((name.upper(),existing));variants.append(value)
        for name,data in ((b"transfer-encoding",b"chunked"),(b"content-encoding",b"gzip"),(b"authorization",b"Bearer synthetic"),(b"cookie",b"session=synthetic"),(b"origin",b"https://browser.invalid")):
            value=scope();value["headers"].append((name,data));variants.append(value)
        for value in variants:
            self.assertEqual((await request(app,value))[0]["status"],400)
        self.assertEqual(self.client.calls,[])

    async def test_truncated_oversized_disconnected_and_xml_body_fail_closed(self):
        app=self.app()
        cases=[(scope(),BODY[:-1],None,400),(scope(),BODY+b'x',None,413),
               (scope(),BODY,[{"type":"http.disconnect"}],400)]
        for value,body,events,status in cases:
            self.assertEqual((await request(app,value,body,events))[0]["status"],status)
        value=scope();replace_header(value,b'content-length',str(MAX_BODY+1).encode())
        self.assertEqual((await request(app,value))[0]["status"],413)
        self.assertEqual(self.client.calls,[])

    async def test_body_and_upstream_timeouts_are_bounded(self):
        app=self.app(timeout=.02,receive_timeout=.02)
        async def never():await asyncio.Event().wait()
        sent=[]
        async def send(value):sent.append(value)
        await app(scope(),never,send)
        self.assertEqual(sent[0]['status'],408)
        self.assertEqual(self.client.calls,[])
        self.client.block=asyncio.Event()
        self.assertEqual((await request(app))[0]['status'],502)
        self.assertEqual(self.client.closed,1)

    async def test_redirect_and_arbitrary_native_bodies_are_not_relayed(self):
        app=self.app(FakeClient(status=302))
        sent=await request(app)
        self.assertEqual(sent[0]['status'],502)
        self.assertEqual(sent[-1]['body'],b'')
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.client.closed,1)
        self.client.status=412
        self.assertEqual((await request(app))[0]['status'],412)

    async def test_cancel_closes_native_context_and_releases_capacity(self):
        block=asyncio.Event();client=FakeClient(block=block);app=self.app(client,concurrency=1)
        task=asyncio.create_task(request(app))
        await asyncio.wait_for(client.entered.wait(),1)
        self.assertEqual((await request(app))[0]['status'],503)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(client.closed,1)
        client.block=None
        self.assertEqual((await request(app))[0]['status'],200)

    async def test_not_started_and_shutdown_do_not_accept_notifications(self):
        app=CallbackRelay({CALLBACK:PEER},'http://127.0.0.1:8080',[HOST])
        self.assertEqual((await request(app))[0]['status'],503)
        client=FakeClient();app=self.app(client);sent=[]
        events=iter([{'type':'lifespan.shutdown'}])
        async def receive():return next(events)
        async def send(value):sent.append(value)
        await app({'type':'lifespan'},receive,send)
        self.assertEqual(sent,[{'type':'lifespan.shutdown.complete'}])
        self.assertEqual((await request(app))[0]['status'],503)
        self.assertEqual(client.calls,[])

    async def test_owned_client_lifespan_has_no_environment_proxy_or_redirect_trust(self):
        app=CallbackRelay({CALLBACK:PEER},'http://127.0.0.1:8080',[HOST])
        client=FakeClient();created=[];closed=[]
        async def close():closed.append(True)
        client.aclose=close
        def create(**kwargs):created.append(kwargs);return client
        fake=type(sys)('httpx');fake.AsyncClient=create
        fake.Timeout=lambda seconds:seconds
        fake.Limits=lambda **kwargs:kwargs
        pending=asyncio.Queue();started=asyncio.Event();sent=[]
        async def send(value):
            sent.append(value)
            if value['type']=='lifespan.startup.complete':started.set()
        with patch.dict(sys.modules,{'httpx':fake}):
            task=asyncio.create_task(app({'type':'lifespan'},pending.get,send))
            await pending.put({'type':'lifespan.startup'})
            await asyncio.wait_for(started.wait(),1)
            self.assertEqual((await request(app))[0]['status'],200)
            await pending.put({'type':'lifespan.shutdown'})
            await asyncio.wait_for(task,1)
        self.assertEqual(len(created),1)
        self.assertFalse(created[0]['trust_env'])
        self.assertFalse(created[0]['follow_redirects'])
        self.assertNotIn('headers',created[0])
        self.assertFalse(created[0]['cookies']._policy.set_ok(None,None))
        self.assertEqual(closed,[True])
        self.assertIsNone(app.client)
        self.assertEqual((await request(app))[0]['status'],503)


if __name__ == '__main__':
    unittest.main()
