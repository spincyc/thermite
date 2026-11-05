"""Commission native authentication and finite Sonos callback ingress.

Protocol sources, pinned with the runtime (no discovery or I/O on import):
https://github.com/openhab/openhab-core/blob/5.2.3/bundles/org.openhab.core.io.rest.auth/src/main/java/org/openhab/core/io/rest/auth/AuthFilter.java
https://github.com/openhab/openhab-core/blob/5.2.3/bundles/org.openhab.core.io.rest.core/src/main/java/org/openhab/core/io/rest/core/internal/service/ConfigurableServiceResource.java
https://github.com/jupnp/jupnp/blob/3.0.4/bundles/org.jupnp/src/main/java/org/jupnp/OSGiUpnpServiceConfiguration.java
https://github.com/jupnp/jupnp/blob/3.0.4/bundles/org.jupnp/src/main/java/org/jupnp/model/Namespace.java

The caller owns service stop/start and native listener isolation. Successful
configuration readback does not prove a physical device delivered a notification.
Device descriptions are plaintext LAN evidence bound to committed IP/UDN/MAC;
they are not cryptographic device identities. No wildcard routes are generated.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import io
import ipaddress
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import oh_http
import oh_ports
from oh_integrations import sonos
from . import AccessError
from .callbacks import CallbackRelay, SERVICES, XML_DECLARATION
from .files import read_private

MAX_BODY = 256 * 1024
MAX_HEADERS = 32 * 1024
DEVICE_NAMESPACE = "urn:schemas-upnp-org:device-1-0"
AUTH_CONFIG = "/rest/services/org.openhab.restauth/config"
UPNP_CONFIG = "/rest/services/org.jupnp/config"
ITEMS = "/rest/items?recursive=false&fields=name"
CALLBACK_URI = "http://localhost/upnpcallback"
FILTER_TABLE = "thermite_native"
#: An installer passes its remaining native readiness allowance, at most two hours.
MAX_DEADLINE = 2 * 60 * 60
FILTER_PRIORITY = -10
_UDN = re.compile(r"RINCON_[0-9A-F]{12}01400\Z")
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}\Z")
_SERVICE = re.compile(r"urn:([A-Za-z0-9.-]+):serviceId:([A-Za-z0-9_.-]+)\Z")


class BoundaryError(AccessError):
    """Fixed diagnostics only: never include native responses or credentials."""


class TransportError(BoundaryError):
    """No HTTP status arrived: refused, reset or timed-out connection."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def _ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
        if not isinstance(value, str) or str(address) != value or address.is_loopback or address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise BoundaryError("Callback discovery requires exact unicast LAN IPv4 addresses") from None


def _runtime(value):
    # No DNS, alternate URL forms, credentials, redirects, queries or fragments.
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})/?", value) if isinstance(value, str) else None
    if not match or int(match[2]) > 65535:
        raise BoundaryError("Native commissioning requires an explicit loopback HTTP origin")
    return value.rstrip("/")


class _Reader(io.RawIOBase):
    def __init__(self, stream, sock, remaining, maximum):
        self.stream, self.sock, self.remaining, self.maximum = stream, sock, remaining, maximum
        self.received = 0

    def readable(self):
        return True

    def readinto(self, target):
        self.sock.settimeout(self.remaining())
        count = self.stream.readinto(memoryview(target)[:self.maximum + 1 - self.received])
        self.received += count or 0
        if self.received > self.maximum:
            raise BoundaryError("Native commissioning response exceeds its size limit")
        return count

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class _Socket:
    def __init__(self, sock, remaining, maximum):
        self.sock, self.remaining, self.maximum = sock, remaining, maximum

    def makefile(self, mode):
        return io.BufferedReader(_Reader(self.sock.makefile(mode, buffering=0), self.sock, self.remaining, self.maximum))


def request(method, url, *, headers, body=None, timeout=5, max_bytes=MAX_BODY):
    """One bounded direct HTTP request, with no proxy, cookie jar or redirects."""
    target = urlsplit(url)
    try:
        ipaddress.ip_address(target.hostname)
        if target.scheme != "http" or target.username or target.password or target.fragment or not target.port:
            raise ValueError()
        if method not in {"GET", "PUT"} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise BoundaryError("Invalid commissioning request") from None
    deadline = time.monotonic() + timeout

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TransportError("Native commissioning request timed out")
        return left

    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=remaining())
    response_type = connection.response_class
    connection.response_class = lambda sock, *args, **kwargs: response_type(
        _Socket(sock, remaining, max_bytes + MAX_HEADERS), *args, **kwargs)
    try:
        connection.request(method, target.path + ("?" + target.query if target.query else ""), body=body,
                           headers={**headers, "Connection": "close"})
        with connection.getresponse() as response:
            fields = response.headers
            if sum(len(k) + len(v) + 4 for k, v in fields.raw_items()) > MAX_HEADERS:
                raise BoundaryError("Native commissioning headers exceed their size limit")
            for name in ("Content-Length", "Transfer-Encoding", "Content-Type", "Content-Encoding", "Location"):
                if len(fields.get_all(name, [])) > 1:
                    raise BoundaryError("Ambiguous commissioning response")
            if fields.get("Content-Encoding") or fields.get("Transfer-Encoding") and fields.get("Content-Length"):
                raise BoundaryError("Encoded or ambiguous commissioning response")
            length = fields.get("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]+", length) or int(length) > max_bytes):
                raise BoundaryError("Native commissioning response exceeds its size limit")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise BoundaryError("Native commissioning response exceeds its size limit")
            return Response(response.status, {k.lower(): v for k, v in fields.items()}, payload)
    except (OSError, http.client.HTTPException):
        raise TransportError("Native commissioning request failed; rerun the deployment to resume") from None
    finally:
        connection.close()


def _json(response):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        if len(response.body) > MAX_BODY or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError()
        return json.loads(response.body.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise BoundaryError("Native commissioning received invalid JSON") from None


def descriptor_routes(body, device):
    """Derive only supported services of actual devices in this exact descriptor."""
    peer = _ipv4(device.get("ip"))
    expected, mac = device.get("sonos_udn"), device.get("mac")
    if not isinstance(expected, str) or not _UDN.fullmatch(expected) or not isinstance(mac, str) or not _MAC.fullmatch(mac) or expected != "RINCON_" + mac.replace(":", "").upper() + "01400":
        raise BoundaryError("Sonos callback discovery requires matching committed UDN and MAC")
    try:
        if not isinstance(body, bytes) or not body or len(body) > MAX_BODY:
            raise ValueError()
        text = body.decode("utf-8-sig")
        if "\x00" in text or XML_DECLARATION.search(text):
            raise ValueError()
        parser = ET.XMLPullParser(events=("start", "end"))
        depth = nodes = 0
        for offset in range(0, len(text), 2048):
            parser.feed(text[offset:offset + 2048])
            for event, _ in parser.read_events():
                depth += 1 if event == "start" else -1
                nodes += event == "start"
                if depth > 24 or nodes > 4096:
                    raise ValueError()
        parser.close()
        root = ET.fromstring(text)
        prefix = "{" + DEVICE_NAMESPACE + "}"
        if root.tag != prefix + "root" or len(root.findall(prefix + "device")) != 1:
            raise ValueError()

        def scalar(element, name):
            found = element.findall(prefix + name)
            if len(found) != 1 or len(found[0]) or not found[0].text or found[0].text != found[0].text.strip():
                raise ValueError()
            return found[0].text

        routes, udns = {}, set()

        def visit(element, root_device=False):
            udn = scalar(element, "UDN")
            if not udn.startswith("uuid:"):
                raise ValueError()
            udn = udn[5:]
            if udn not in {expected, expected + "_MR", expected + "_MS"} or root_device and udn != expected or udn in udns:
                raise ValueError()
            udns.add(udn)
            if len(element.findall(prefix + "serviceList")) > 1 or len(element.findall(prefix + "deviceList")) > 1:
                raise ValueError()
            seen = set()
            for service in element.findall(prefix + "serviceList/" + prefix + "service"):
                service_id = scalar(service, "serviceId")
                match = _SERVICE.fullmatch(service_id)
                if not match or service_id in seen:
                    raise ValueError()
                seen.add(service_id)
                namespace, name = match.groups()
                if name not in SERVICES:
                    continue  # The pinned Sonos binding does not subscribe to these.
                if namespace not in {"upnp-org", "sonos-com"}:
                    raise ValueError()
                event = urlsplit(urljoin(f"http://{peer}:1400/xml/device_description.xml", scalar(service, "eventSubURL")))
                if event.scheme != "http" or event.hostname != peer or event.port != 1400 or event.username or event.password or event.query or event.fragment:
                    raise ValueError()
                path = f"/upnpcallback/dev/{udn}/svc/{namespace}/{name}/event/cb"
                routes[path] = peer
            for child in element.findall(prefix + "deviceList/" + prefix + "device"):
                visit(child)

        visit(root.find(prefix + "device"), True)
        if not routes:
            raise ValueError()
        return routes
    except (ValueError, UnicodeError, ET.ParseError, RecursionError, TypeError, AttributeError):
        raise BoundaryError("Sonos descriptor does not prove a bounded callback map for its committed identity") from None


def validate_callbacks(config, runtime_url):
    """Validate adopted configuration without rewriting its routes or listener."""
    from .callback_service import FIELDS
    try:
        if not isinstance(config, dict) or set(config) != FIELDS or type(config["version"]) is not int or config["version"] != 1:
            raise ValueError()
        address = _ipv4(config["bind_host"])
        oh_ports.unique({"callbacks": config["port"], "native HTTP": urlsplit(runtime_url).port})
        if config["hosts"] != [f"{address}:{config['port']}"] or config["upstream"] != runtime_url:
            raise ValueError()
        CallbackRelay(config["routes"], config["upstream"], config["hosts"])
        return config
    except (ValueError, TypeError, KeyError, AttributeError, AccessError):
        raise BoundaryError("Existing callback configuration cannot be safely adopted") from None


def discover_callbacks(house_data, runtime_url, lan_address, *, callback_port=oh_ports.CALLBACK_PORT, request=request):
    """Read only committed Sonos addresses; never follow descriptor URLs or SSDP."""
    devices = [device for device in house_data.get("devices", []) if sonos.relayed(device)]
    if not devices:
        return None
    if len(devices) > 64:
        raise BoundaryError("Sonos callback inventory exceeds its commissioning limit")
    routes = {}
    for device in devices:
        address = _ipv4(device.get("ip"))
        response = request("GET", f"http://{address}:1400/xml/device_description.xml", headers={"Accept": "text/xml, application/xml"},
                           body=None, timeout=5, max_bytes=MAX_BODY)
        if response.status != 200 or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {"text/xml", "application/xml"}:
            raise BoundaryError("Sonos callback discovery is unavailable; rerun the deployment when its device answers")
        found = descriptor_routes(response.body, device)
        if routes.keys() & found.keys():
            raise BoundaryError("Sonos callback identities overlap in committed inventory")
        routes.update(found)
    config = {"version": 1, "routes": routes, "upstream": _runtime(runtime_url), "hosts": [f"{lan_address}:{callback_port}"],
              "bind_host": lan_address, "port": callback_port}
    return validate_callbacks(config, runtime_url)


def relocated_callbacks(config, runtime_url, port):
    """An adopted map on this site's callback port and native runtime.

    Its verified routes and LAN address are kept; the listener and upstream
    are the allocation's, so a changed allocation moves them, as it moves the
    HTTPS port. openHAB's jUPnP proxy port follows from the same allocation
    in conf/services/upnp.cfg (oh_render.build); configure() checks it.
    """
    runtime_url = _runtime(runtime_url)
    address = config["bind_host"]
    return validate_callbacks({**config, "port": port, "hosts": [f"{address}:{port}"], "upstream": runtime_url},
                              runtime_url)


def native_filter_rules(http_port, https_port, *, karaf_port=oh_ports.KARAF_PORT, lsp_port=oh_ports.LSP_PORT,
                        shutdown_port=oh_ports.SHUTDOWN_PORT):
    """An atomic nft input file affecting only Thermite's reserved table.

    The lifecycle owner must establish ownership of an existing table before
    applying this. `add table` is idempotent; delete/recreate in the same batch
    handles first installation and removes obsolete chains without a gap.
    https://netfilter.org/projects/nftables/manpage.html
    """
    return filter_rules(oh_ports.unique({"HTTP": http_port, "HTTPS": https_port, "Karaf": karaf_port,
                                         "LSP": lsp_port, "shutdown": shutdown_port}).values())


def filter_rules(ports):
    """native_filter_rules() for a set of ports, whichever listeners they are."""
    members = ", ".join(str(value) for value in sorted(ports))
    return (f"add table inet {FILTER_TABLE}\n"
            f"delete table inet {FILTER_TABLE}\n"
            f"add table inet {FILTER_TABLE}\n"
            f"add chain inet {FILTER_TABLE} input {{ type filter hook input priority {FILTER_PRIORITY}; policy accept; }}\n"
            f'add rule inet {FILTER_TABLE} input iifname != "lo" tcp dport {{ {members} }} reject with tcp reset\n')


def verify_native_filter(value, http_port, https_port, *, karaf_port=oh_ports.KARAF_PORT,
                         lsp_port=oh_ports.LSP_PORT, shutdown_port=oh_ports.SHUTDOWN_PORT):
    """Check `nft -j -n list table inet thermite_native` against exact policy.

    Accept no early verdict, extra rule, altered hook, extra port, interval or
    named set. Counter/handle values are not authority. The caller must check
    nft's exit status before passing its parsed JSON here.
    """
    return verify_filter(value, oh_ports.unique({"HTTP": http_port, "HTTPS": https_port, "Karaf": karaf_port,
                                                 "LSP": lsp_port, "shutdown": shutdown_port}).values())


def verify_filter(value, ports):
    """verify_native_filter() for a set of ports, whichever listeners they are."""
    expected = set(ports)
    try:
        if not isinstance(value, dict) or set(value) != {"nftables"} or not isinstance(value["nftables"], list):
            raise ValueError()
        objects = {}
        for entry in value["nftables"]:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise ValueError()
            kind, data = next(iter(entry.items()))
            if kind == "metainfo":
                continue
            if kind not in {"table", "chain", "rule"} or kind in objects or not isinstance(data, dict):
                raise ValueError()
            objects[kind] = data
        table, chain, rule = (objects[kind] for kind in ("table", "chain", "rule"))
        if table.get("family") != "inet" or table.get("name") != FILTER_TABLE or table.get("flags"):
            raise ValueError()
        for item in (chain, rule):
            if item.get("family") != "inet" or item.get("table") != FILTER_TABLE:
                raise ValueError()
        if (chain.get("name"), chain.get("type"), chain.get("hook"), chain.get("prio"), chain.get("policy")) != ("input", "filter", "input", FILTER_PRIORITY, "accept") or rule.get("chain") != "input":
            raise ValueError()
        expressions = rule["expr"]
        if len(expressions) != 3 or expressions[0] != {"match": {"op": "!=", "left": {"meta": {"key": "iifname"}}, "right": "lo"}} or expressions[2] != {"reject": {"type": "tcp reset"}}:
            raise ValueError()
        match = expressions[1]["match"]
        if set(match) != {"op", "left", "right"} or match["op"] not in {"==", "in"} or match["left"] != {"payload": {"protocol": "tcp", "field": "dport"}}:
            raise ValueError()
        if not isinstance(match["right"], dict) or set(match["right"]) != {"set"}:
            raise ValueError()
        members = match["right"]["set"]
        if not isinstance(members, list) or len(members) != len(expected) or any(type(port) is not int for port in members) or set(members) != expected:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise BoundaryError("The active native network filter does not match the required boundary") from None
    return {"native_filter": True}


def firewall_ports(host):
    """The native ports the table filters, from the site's validated complete allocation."""
    ports = oh_ports.effective(host)
    return frozenset(oh_ports.unique({"HTTP": ports["http"], "HTTPS": ports["https"], "Karaf": ports["karaf"],
                                      "LSP": ports["lsp"], "shutdown": ports["native_shutdown"]}).values())


def firewall_rules(host):
    """Installer adapter using the site's validated complete port allocation."""
    return filter_rules(firewall_ports(host))


def verify_firewall(output, ports):
    """Installer adapter for firewall_ports() or a rule file's own ports;
    reject malformed/duplicate JSON and any foreign policy."""
    try:
        raw = output.encode("utf-8") if isinstance(output, str) else output
        value = _json(Response(200, {"content-type": "application/json"}, raw))
    except (AttributeError, TypeError):
        raise BoundaryError("Invalid native network filter inspection") from None
    verify_filter(value, ports)


def verify_native_listeners(snapshot, *, http_port, https_port, lsp_port=oh_ports.LSP_PORT, filtered=False):
    """Validate a successful `ss -H -ltn` snapshot; the caller captures it.

    Both HTTP listeners must exist exclusively on literal loopback addresses.
    The pinned native LSP has no bind-address option; accepting its socket
    requires a separately verified native network filter (`filtered=True`).
    This proves local bindings, not external firewall or device health.
    """
    ports = oh_ports.unique({"HTTP": http_port, "HTTPS": https_port, "LSP": lsp_port})
    found = set()
    for line in snapshot.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "LISTEN":
            raise BoundaryError("Native listener inspection returned an invalid socket snapshot")
        address, separator, port_text = fields[3].rpartition(":")
        if not separator or not port_text.isdecimal():
            raise BoundaryError("Native listener inspection returned an invalid socket address")
        port = int(port_text)
        if port not in ports.values():
            continue
        if port == lsp_port:
            if filtered is not True:
                raise BoundaryError("Native LSP requires a verified network filter")
            continue
        try:
            ip = ipaddress.ip_address(address.strip("[]"))
            if not ip.is_loopback:
                raise ValueError()
        except ValueError:
            raise BoundaryError("Native HTTP or HTTPS is not isolated to loopback") from None
        found.add(port)
    if found != {http_port, https_port}:
        raise BoundaryError("Both native loopback HTTP and HTTPS listeners must be ready")
    return {"native_listeners": True}


def configure(runtime_url, token_file, house_data, lan_address, *, callback_port=oh_ports.CALLBACK_PORT,
              existing_callbacks=None, request=request, timeout=120, clock=time.monotonic, sleep=time.sleep,
              record=None):
    """Preserve settings, configure the boundary, and prove authenticated access.

    jUPnP's callback proxy port is read back, never written: openHAB applies
    it from conf/services/upnp.cfg as it starts (oh_render.build).

    The callback config is returned privately to the lifecycle caller. This
    function neither writes it nor logs native bodies, tokens, paths or peers.
    PUT is never retried after an unknown outcome; a rerun reads actual state.
    Native reads retry transient statuses and failed connections (oh_http's
    one rule) until the caller's deadline, which an installer shares with its
    other native readiness waits. `record(service_path, original)` runs, and
    must persist the complete original mapping, before each replacing PUT.
    """
    runtime_url = _runtime(runtime_url)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= MAX_DEADLINE:
        raise BoundaryError("Invalid native commissioning deadline")
    deadline = clock() + timeout
    try:
        token = read_private(token_file, 16384).decode("utf-8").strip()
    except UnicodeError:
        raise BoundaryError("Invalid private runtime credential") from None
    if not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise BoundaryError("Invalid private runtime credential")

    phase, observation = "Sonos callback discovery", None
    resources = {AUTH_CONFIG: "REST authentication configuration", UPNP_CONFIG: "jUPnP configuration"}

    def stage(value):
        nonlocal phase, observation
        if value != phase:
            phase, observation = value, None

    def deadline_error():
        # Only fixed labels, configuration keys we own and HTTP status codes
        # belong here; native values, descriptions and addresses stay private.
        detail = " (" + observation + ")" if observation else ""
        return BoundaryError("Native commissioning exceeded its deadline during " + phase + detail
                             + "; rerun the deployment")

    def bounded_request(method, url, *, headers, body=None, timeout=5, max_bytes=MAX_BODY):
        left = deadline - clock()
        if left <= 0:
            raise deadline_error()
        try:
            return request(method, url, headers=headers, body=body, timeout=min(timeout, left), max_bytes=max_bytes)
        except BoundaryError:
            if clock() >= deadline:
                raise deadline_error() from None
            raise

    def call(method, path, *, body=None, authenticated=True):
        nonlocal observation
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = bounded_request(method, runtime_url + path, headers=headers, body=body,
                                       timeout=5, max_bytes=MAX_BODY)
        except TransportError:
            # Add-on and configuration reloads restart the native HTTP stack.
            # A read is repeated; a write's outcome is unknown, so it is not.
            if not oh_http.retryable(method):
                raise
            observation = "connection failed"
            return None
        if authenticated and response.status in {401, 403}:
            raise BoundaryError("Native commissioning rejected the internal credential; no replacement was made")
        return response

    def pause():
        sleep(min(1, max(0, deadline - clock())))

    def read(path, *, readback=False):
        nonlocal observation
        stage(resources[path] + (" readback" if readback else " read"))
        # A configuration update can briefly unregister its REST resource.
        while True:
            response = call("GET", path)
            if response is not None and response.status == 200:
                value = _json(response)
                if not isinstance(value, dict):
                    raise BoundaryError("Native configuration is not a mapping")
                return value
            if response is not None:
                if not oh_http.retryable("GET", response.status):
                    raise BoundaryError("Native configuration could not be read")
                observation = "last HTTP status " + str(response.status)
            pause()

    def update(path, desired):
        nonlocal observation
        original = read(path)
        intended = {**original, **desired}
        if original != intended or any(type(original.get(key)) is not type(value) for key, value in desired.items()):
            stage(resources[path] + " update")
            if record is not None:
                try:
                    record(path, original)
                except (OSError, ValueError, TypeError, AccessError):
                    raise BoundaryError("Could not record the original native configuration; nothing was replaced") from None
            response = call("PUT", path, body=json.dumps(intended, allow_nan=False, separators=(",", ":")).encode())
            if response.status not in {200, 204}:
                raise BoundaryError("Native configuration update was not confirmed; rerun the deployment")
        while True:
            actual = read(path, readback=True)
            if actual == intended and all(type(actual.get(key)) is type(value) for key, value in desired.items()):
                return
            if any(actual.get(key) != value for key, value in original.items() if key not in desired):
                raise BoundaryError("Unrelated native settings changed during commissioning; review before retrying")
            mismatches = []
            for key, value in desired.items():
                if key not in actual:
                    mismatches.append(key + " missing")
                elif type(actual[key]) is not type(value):
                    mismatches.append(key + " type mismatch")
                elif actual[key] != value:
                    mismatches.append(key + " value mismatch")
            observation = ", ".join(mismatches) or "configuration keys differ"
            pause()

    if existing_callbacks is not None:
        if isinstance(existing_callbacks, (str, Path)):
            from .callback_service import load_config
            existing_callbacks = load_config(existing_callbacks, runtime_url=runtime_url)
        callbacks = validate_callbacks(existing_callbacks, runtime_url)
    else:
        callbacks = discover_callbacks(house_data, runtime_url, lan_address, callback_port=callback_port, request=bounded_request)

    update(AUTH_CONFIG, {"implicitUserRole": False, "trustedNetworks": ""})
    if callbacks is not None:
        upnp = read(UPNP_CONFIG)
        if upnp.get("callbackURI", CALLBACK_URI) != CALLBACK_URI:
            raise BoundaryError("Existing jUPnP callback namespace differs from the verified relay paths")
        # Changing jUPnP's configuration restarts it, and openHAB then answers
        # every NOTIFY 501 until it restarts. openHAB starts with the port
        # instead: conf/services/upnp.cfg (oh_render) sets the decimal string
        # jUPnP's Integer.parseInt takes, so only a readback belongs here.
        if upnp.get("httpProxyPort") != str(callbacks["port"]):
            raise BoundaryError("jUPnP does not advertise the callback relay's port: openHAB has not applied "
                                "conf/services/upnp.cfg; deploy the generated configuration, restart openHAB and "
                                "rerun the deployment")
    for authenticated in (True, False):
        stage("authenticated Item access" if authenticated else "anonymous Item access")
        failure = ("Internal native Item access did not pass commissioning" if authenticated else
                   "Anonymous native Item access was not rejected; commissioning is incomplete")
        while True:
            response = call("GET", ITEMS, authenticated=authenticated)
            if response is None:
                pass
            elif response.status == 200:
                if not isinstance(_json(response), list):
                    raise BoundaryError(failure)
                if authenticated:
                    break
            elif not authenticated and response.status in {401, 403}:
                break
            elif not oh_http.retryable("GET", response.status):
                raise BoundaryError(failure)
            # Config Admin readback can precede REST resource re-registration
            # and application of the auth filter. Only GET verification repeats.
            if response is not None:
                observation = "last HTTP status " + str(response.status)
            pause()
    return {"native_security": True, "callbacks": callbacks}
