"""Optional, disabled-until-composed Sonos UPnP event ingress.

Pinned jUPnP 3.0.4 Namespace generates /upnpcallback/dev/<UDN>/svc/
<serviceIdNamespace>/<serviceId>/event/cb. These are unauthenticated UPnP
servlets, not REST resources. The owner supplies *exact verified* path->peer
entries and listener Host values; this module discovers no device, creates no
listener, and starts no service on import. It never forwards a bearer token.

HTTP200 means the native callback endpoint accepted the notification. It is
not proof of a device command, state change, or healthy physical device.
UPnP events remain plaintext trusted-LAN reports, not cryptographic identity.
"""

import asyncio
from collections.abc import Mapping
import html
from http.cookiejar import CookieJar, DefaultCookiePolicy
import ipaddress
import re
from types import MappingProxyType
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from . import AccessError

MAX_BODY = 256 * 1024
MAX_HEADERS = 64
MAX_HEADER_BYTES = 16384
EVENT_NAMESPACE = "urn:schemas-upnp-org:event-1-0"
SERVICES = frozenset({"DeviceProperties", "AVTransport", "RenderingControl", "ZoneGroupTopology",
                      "GroupManagement", "AudioIn", "HTControl", "ContentDirectory"})
PATH = re.compile(r"/upnpcallback/dev/RINCON_[0-9A-F]{12}01400(?:_MR|_MS)?/svc/(?:upnp-org|sonos-com)/([A-Za-z]+)/event/cb\Z")
SID = re.compile(r"uuid:[A-Za-z0-9_-]{1,128}\Z")
HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
CONTENT_TYPE = re.compile(r'(?:text|application)/xml(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*\Z', re.I)


class NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class NotificationError(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code


def _peer(value):
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not isinstance(address, ipaddress.IPv4Address) or address.is_unspecified or address.is_multicast:
            raise ValueError()
        return str(address)
    except ValueError:
        raise AccessError("Callback peers must be exact unicast IPv4 addresses") from None


def _loopback(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not parsed.port:
            raise ValueError()
    except (TypeError, ValueError):
        raise AccessError("Callback upstream must be a fixed explicit loopback HTTP origin") from None
    return value.rstrip("/")


def _host(value):
    try:
        parsed = urlsplit("http://" + value)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.port or _peer(parsed.hostname) != parsed.hostname:
            raise ValueError()
        if value != parsed.hostname + ":" + str(parsed.port):
            raise ValueError()
    except (ValueError, TypeError):
        raise AccessError("Callback Hosts must be exact IPv4:port authorities") from None
    return value


def xml_notification(body):
    """Parse bounded UTF-8 propertysets with no DTD/entities, including nested XML."""
    if not body or len(body) > MAX_BODY:
        raise NotificationError(413, "invalid_size")
    try:
        text = body.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError()
        for _ in range(4):
            if XML_DECLARATION.search(text):
                raise ValueError()
            unescaped = html.unescape(text)
            if unescaped == text:
                break
            text = unescaped
        parser = ET.XMLPullParser(events=("start", "end"))
        depth = nodes = properties = 0
        for offset in range(0, len(body), 2048):
            parser.feed(body[offset:offset + 2048])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    nodes += 1
                    if nodes > 4096 or depth > 24:
                        raise ValueError()
                    if depth == 1 and element.tag != "{" + EVENT_NAMESPACE + "}propertyset":
                        raise ValueError()
                    if depth == 2:
                        if element.tag != "{" + EVENT_NAMESPACE + "}property":
                            raise ValueError()
                        properties += 1
                        if properties > 128:
                            raise ValueError()
                else:
                    depth -= 1
                    element.clear()
        parser.close()
        if depth != 0 or properties == 0:
            raise ValueError()
    except (UnicodeError, ValueError, ET.ParseError, RecursionError):
        raise NotificationError(400, "invalid_xml") from None


def notification(scope, routes, hosts):
    """Validate routing and framing before reading or buffering a request body."""
    if scope.get("method") != "NOTIFY":
        raise NotificationError(405, "method_not_allowed")
    path = scope.get("path")
    raw = scope.get("raw_path", b"")
    if not isinstance(path, str) or path not in routes or raw != path.encode("ascii") or scope.get("query_string"):
        raise NotificationError(404, "not_found")
    try:
        remote = _peer(scope.get("client", ("", 0))[0])
    except (AccessError, TypeError, IndexError):
        raise NotificationError(403, "peer_not_allowed") from None
    if remote != routes[path]:
        raise NotificationError(403, "peer_not_allowed")
    raw_headers = scope.get("headers", ())
    if len(raw_headers) > MAX_HEADERS or sum(len(name) + len(value) for name, value in raw_headers) > MAX_HEADER_BYTES:
        raise NotificationError(431, "invalid_headers")
    headers = {}
    for name, value in raw_headers:
        if not HEADER_NAME.fullmatch(name) or any(char < 32 or char > 126 for char in value):
            raise NotificationError(400, "invalid_headers")
        key = name.decode("ascii").lower()
        if key in headers:
            raise NotificationError(400, "duplicate_header")
        headers[key] = value.decode("ascii").strip()
    if headers.get("host") not in hosts:
        raise NotificationError(403, "host_not_allowed")
    if any(name in headers for name in ("transfer-encoding", "content-encoding", "trailer", "upgrade", "expect", "authorization", "cookie", "origin")):
        raise NotificationError(400, "invalid_headers")
    length = headers.get("content-length", "")
    if not re.fullmatch(r"[0-9]{1,6}", length):
        raise NotificationError(411, "length_required")
    length = int(length)
    if not 0 < length <= MAX_BODY:
        raise NotificationError(413, "invalid_size")
    sequence = headers.get("seq", "")
    if headers.get("nt") != "upnp:event" or headers.get("nts") != "upnp:propchange" or not SID.fullmatch(headers.get("sid", "")) or not re.fullmatch(r"[0-9]{1,10}", sequence) or int(sequence) > 4294967295:
        raise NotificationError(412, "invalid_notification")
    if not CONTENT_TYPE.fullmatch(headers.get("content-type", "")):
        raise NotificationError(415, "xml_required")
    # Every forwarded header is reconstructed; none carries client credentials,
    # forwarding claims, arbitrary Host, or transfer/content encoding authority.
    forwarded = {"SID": headers["sid"], "SEQ": str(int(sequence)), "NT": "upnp:event", "NTS": "upnp:propchange",
                 "Content-Type": 'text/xml; charset="utf-8"', "Content-Length": str(length)}
    return path, length, forwarded


class CallbackRelay:
    """Standalone ASGI application. Configuration alone does not enable a listener.

    routes is an exact verified callback path -> publishing Sonos peer IPv4 map.
    Do not synthesize paths from MR/MS suffix patterns or trust forwarded IPs.
    Native jUPnP retains authority over active subscription IDs and sequences.
    The caller must run its server with proxy-header trust DISABLED.
    """

    def __init__(self, routes, upstream, hosts, *, client=None, concurrency=16, timeout=3, receive_timeout=3, receipt=None):
        if not isinstance(routes, Mapping) or not routes or len(routes) > 512:
            raise AccessError("Provide a finite verified callback path map")
        self.routes = {}
        for path, peer in routes.items():
            match = PATH.fullmatch(path) if isinstance(path, str) else None
            if not match or match.group(1) not in SERVICES:
                raise AccessError("Callback map contains an unsupported event path")
            self.routes[path] = _peer(peer)
        self.routes = MappingProxyType(self.routes)
        self.upstream = _loopback(upstream)
        self.hosts = frozenset(_host(value) for value in hosts)
        if not self.hosts or len(self.hosts) > 8 or type(concurrency) is not int or not 1 <= concurrency <= 32 or not 0 < timeout <= 5 or not 0 < receive_timeout <= 5:
            raise AccessError("Callback bounds or listener Hosts are invalid")
        self.client = client
        if client is not None:
            # Dependency injection is for a dedicated transport, never a site's
            # authenticated HTTP client. Even native Set-Cookie must not persist.
            headers = getattr(client, "headers", {})
            cookies = getattr(client, "cookies", None)
            if getattr(client, "auth", None) is not None or getattr(client, "_trust_env", False) or getattr(client, "follow_redirects", False) or any(name.lower() in {"authorization", "cookie", "proxy-authorization"} for name in headers) or cookies:
                raise AccessError("Callback transport must be dedicated and credential-free")
            if cookies is not None:
                cookies.jar.set_policy(NoCookies())
        self.owns_client = client is None
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout, self.receive_timeout = timeout, receive_timeout
        self.receipt = receipt or (lambda value: None)
        self.closing = False

    async def _reply(self, send, status):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

    def _record(self, status, outcome):
        # Fixed fields only. No SID, body, XML values, credentials, or upstream text.
        try:
            self.receipt({"status": status, "outcome": outcome})
        except Exception:
            pass  # A receipt sink cannot expose an exception or change native ACKs.

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            await self.lifespan(receive, send)
            return
        if scope.get("type") != "http":
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        acquired = False
        try:
            if self.closing or self.client is None:
                raise NotificationError(503, "relay_not_started")
            path, length, headers = notification(scope, self.routes, self.hosts)
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=0.05)
                acquired = True
            except asyncio.TimeoutError:
                raise NotificationError(503, "relay_busy") from None
            body = bytearray()
            async def read_body():
                while True:
                    event = await receive()
                    if event.get("type") == "http.disconnect":
                        raise NotificationError(400, "client_disconnected")
                    if event.get("type") != "http.request" or not isinstance(event.get("body", b""), bytes):
                        raise NotificationError(400, "invalid_body")
                    chunk = event.get("body", b"")
                    if len(body) + len(chunk) > length:
                        raise NotificationError(413, "invalid_size")
                    body.extend(chunk)
                    if not event.get("more_body"):
                        break
            try:
                await asyncio.wait_for(read_body(), timeout=self.receive_timeout)
            except asyncio.TimeoutError:
                raise NotificationError(408, "body_timeout") from None
            if len(body) != length:
                raise NotificationError(400, "length_mismatch")
            xml_notification(body)
            try:
                async def forward():
                    async with self.client.stream("NOTIFY", self.upstream + path, content=bytes(body), headers=headers) as response:
                        # Do not consume, copy or expose native response bodies/headers.
                        return response.status_code
                status = await asyncio.wait_for(forward(), timeout=self.timeout)
            except Exception:
                raise NotificationError(502, "native_unavailable") from None
            if status not in {200, 400, 404, 412, 500}:
                raise NotificationError(502, "native_unavailable")
            self._record(status, "native_response")
            await self._reply(send, status)
        except NotificationError as error:
            self._record(error.status, error.code)
            await self._reply(send, error.status)
        finally:
            if acquired:
                self.semaphore.release()

    async def lifespan(self, receive, send):
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                if self.owns_client:
                    import httpx
                    self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                                  timeout=httpx.Timeout(self.timeout),
                                                  limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                                                  cookies=CookieJar(policy=NoCookies()))
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                self.closing = True
                if self.owns_client and self.client is not None:
                    await self.client.aclose()
                    self.client = None
                await send({"type": "lifespan.shutdown.complete"})
                return
