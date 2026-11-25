"""Explicit private composition for legacy plaintext UPnP NOTIFY ingress.

No listener, discovery, credential lookup, state write or service starts on
import. The owner must verify every native callback path and publishing peer.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import logging
import socket
import weakref
from urllib.parse import urlsplit

import oh_ports

from . import AccessError
from .callbacks import CallbackRelay, MAX_HEADER_BYTES
from .files import read_private, require_service_account

FIELDS = {"version", "routes", "upstream", "hosts", "bind_host", "port"}
CONNECTIONS = 32
HEADER_SECONDS = 3
GRACEFUL_SECONDS = 5
BACKLOG = 64
MAX_REFUSALS = 64


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
        # The stage checks the site's complete allocation. Here only the
        # configured listener/upstream are authoritative; defaults may be freed.
        if not isinstance(data["upstream"], str):
            raise ValueError()
        upstream_port = urlsplit(data["upstream"]).port
        if upstream_port is None:
            raise ValueError()
        oh_ports.unique({"callbacks": data["port"], "native HTTP": upstream_port})
        if not isinstance(data["hosts"], list) or data["hosts"] != [str(address) + ":" + str(data["port"])]:
            raise ValueError()
        relay = CallbackRelay(data["routes"], data["upstream"], data["hosts"])
        if runtime_url is not None and relay.upstream != runtime_url.rstrip("/"):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise AccessError("Invalid private callback configuration, exact routes, listener address or native upstream") from None
    return data


class ConnectionLimit:
    """Bounds concurrent TCP connections at accept() before Hypercorn reads a
    single byte. Connections over `limit` are closed immediately; active slots
    are released when the accepted socket closes."""

    def __init__(self, limit: int = CONNECTIONS, *, max_refusals: int = MAX_REFUSALS) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("connection limit must be a positive integer")
        if type(max_refusals) is not int or max_refusals < 1:
            raise ValueError("max_refusals must be a positive integer")
        self.limit = limit
        self.max_refusals = max_refusals
        self.active = 0

    def release(self) -> None:
        if self.active > 0:
            self.active -= 1

    def only(self, sock: socket.socket) -> socket.socket:
        bounded = _BoundedListener(sock.family, sock.type, sock.proto, fileno=sock.detach())
        bounded.limiter = self
        return bounded


class _TrackedConnection(socket.socket):
    _finalizer: weakref.finalize | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._finalizer is not None:
                self._finalizer()


class _BoundedListener(socket.socket):
    limiter: ConnectionLimit | None = None

    def accept(self):
        limiter = self.limiter
        assert limiter is not None
        for _ in range(limiter.max_refusals):
            connection, peer = super().accept()
            if limiter.active < limiter.limit:
                limiter.active += 1
                tracked = _TrackedConnection(connection.family, connection.type, connection.proto,
                                             fileno=connection.detach())
                tracked._finalizer = weakref.finalize(tracked, limiter.release)
                return tracked, peer
            connection.close()
        raise BlockingIOError(errno.EAGAIN, "callback connection limit reached")


def listener(bind_host: str, port: int, *, connections: int = CONNECTIONS, header_seconds: float = HEADER_SECONDS,
             graceful_seconds: float = GRACEFUL_SECONDS, backlog: int = BACKLOG):
    """Plaintext HTTP/1.1 Hypercorn listener with fixed header deadline,
    header-size limit, single-request Connection: close, and bounded
    concurrent connections before ASGI runs."""
    from hypercorn.config import Config
    from . import service as access_service

    limiter = ConnectionLimit(connections)

    class CallbackListener(Config):
        ssl_enabled = False

        def create_sockets(self):
            sockets = super().create_sockets()
            for listening in sockets.insecure_sockets:
                listening.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT,
                                     access_service.UNANSWERED_SECONDS * 1000)
            sockets.insecure_sockets[:] = [limiter.only(sock) for sock in sockets.insecure_sockets]
            return sockets

    config = CallbackListener()
    config.bind = [f"[{bind_host}]:{port}" if ipaddress.ip_address(bind_host).version == 6 else f"{bind_host}:{port}"]
    config.backlog = backlog
    config.workers = 1
    # No access log, no Server header, no Date header; Hypercorn trusts no
    # proxy/forwarded headers and configures no logging of its own.
    config.accesslog = None
    config.errorlog = logging.getLogger("hypercorn.error")
    config.include_server_header = False
    config.include_date_header = False
    # Hypercorn starts its idle timer at connection start and only cancels it
    # once complete headers arrive, so dripping bytes never extend it.
    config.keep_alive_timeout = header_seconds
    # Close every HTTP/1.1 connection after one response (appends Connection: close).
    config.keep_alive_max_requests = 1
    config.graceful_timeout = graceful_seconds
    config.h11_max_incomplete_size = MAX_HEADER_BYTES
    config.h2_max_concurrent_streams = 1
    config.h2_max_header_list_size = MAX_HEADER_BYTES
    config.h2_max_inbound_frame_size = 2 ** 14
    config.connection_limit = limiter
    return config


def serve(config_path):
    """Owner invokes separately; no TLS/auth proxy or general HTTP forwarding."""
    require_service_account()
    config = load_config(config_path)
    from . import metrics, service as access_service

    application = CallbackRelay(config["routes"], config["upstream"], config["hosts"], receipt=metrics.callback_receipt)
    server_config = listener(config["bind_host"], config["port"])
    textfile = metrics.start(metrics.CALLBACKS, metrics.CALLBACKS_FILE)
    try:
        access_service._run_server([(application, server_config)])
    finally:
        if textfile:
            textfile.stop()
