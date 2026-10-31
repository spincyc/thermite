"""Explicit private composition for legacy plaintext UPnP NOTIFY ingress.

No listener, discovery, credential lookup, state write or service starts on
import. The owner must verify every native callback path and publishing peer.
"""

import ipaddress
import json

from . import AccessError
from .callbacks import CallbackRelay, MAX_HEADER_BYTES
from .files import read_private

FIELDS = {"version", "routes", "upstream", "hosts", "bind_host", "port"}
CONNECTIONS = 32
HEADER_SECONDS = 3


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def load_config(path, *, runtime_url=None):
    """Validate an owned0600 finite map; do not initialize an HTTP client."""
    try:
        data = json.loads(read_private(path, 256 * 1024), object_pairs_hook=_unique)
        if not isinstance(data, dict) or set(data) != FIELDS or type(data["version"]) is not int or data["version"] != 1:
            raise ValueError()
        address = ipaddress.ip_address(data["bind_host"])
        if not isinstance(address, ipaddress.IPv4Address) or address.is_unspecified or address.is_multicast or address.is_loopback or str(address) != data["bind_host"]:
            raise ValueError()
        if type(data["port"]) is not int or not 1024 <= data["port"] <= 65535:
            raise ValueError()
        if not isinstance(data["hosts"], list) or data["hosts"] != [str(address) + ":" + str(data["port"])]:
            raise ValueError()
        if not isinstance(data["upstream"], str):
            raise ValueError()
        relay = CallbackRelay(data["routes"], data["upstream"], data["hosts"])
        if runtime_url is not None and relay.upstream != runtime_url.rstrip("/"):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise AccessError("Invalid private callback configuration, exact routes, listener address or native upstream") from None
    return data


def header_protocol():
    """Pinned h11 with a deadline even before ASGI receives complete headers."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class BoundedHeaders(H11Protocol):
        def connection_made(self, transport):
            self.header_deadline = None
            super().connection_made(transport)
            if len(self.connections) > CONNECTIONS:
                transport.close()
                return
            self.header_deadline = self.loop.call_later(HEADER_SECONDS, transport.close)

        def cancel_header_deadline(self):
            if self.header_deadline is not None:
                self.header_deadline.cancel()
                self.header_deadline = None

        def data_received(self, data):
            super().data_received(data)
            if self.cycle is not None:
                self.cancel_header_deadline()

        def connection_lost(self, exc):
            self.cancel_header_deadline()
            super().connection_lost(exc)

    return BoundedHeaders


def serve(config_path):
    """Owner invokes separately; no TLS/auth proxy or general HTTP forwarding."""
    config = load_config(config_path)
    application = CallbackRelay(config["routes"], config["upstream"], config["hosts"])
    import uvicorn
    uvicorn.run(application, host=config["bind_host"], port=config["port"], http=header_protocol(), ws="none",
                lifespan="on", proxy_headers=False, forwarded_allow_ips="", access_log=False,
                server_header=False, date_header=False, headers=[("Connection", "close")],
                h11_max_incomplete_event_size=MAX_HEADER_BYTES, limit_concurrency=CONNECTIONS,
                backlog=64, timeout_keep_alive=1, timeout_graceful_shutdown=5,
                log_config=None, log_level="critical", workers=1)
