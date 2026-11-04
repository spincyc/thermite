"""The opt-in plain-HTTP Guest listener: its settings, sockets and marking.

instance/host.yaml's optional guest_http mapping opens a second gateway port
without TLS. It binds only the listed addresses, and its sockets close a
connection from outside its client networks as accept() returns it, before the
server sees it. Every request it admits is marked by the listener itself, in
the ASGI scope, as a guest-listener request; the identity application then
answers it only as a Guest (oh_identity.guest).

Importing this module loads no framework or server, so the host schema,
staging and status use it too. Nothing here depends on the HTTP server.
"""

from dataclasses import dataclass
import ipaddress
import logging
import re
import socket
import time

import oh_ports

FIELDS = ("port", "listen", "clients", "names")
#: The ASGI scope key the listener sets. Request data never reaches the scope's
#: top level, so no client can claim it.
SCOPE = "thermite.guest_http"
#: A lowercase DNS name a guest may type, such as remote.guest.example.
_NAME = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
log = logging.getLogger("oh_access")


def _address(value):
    try:
        address = ipaddress.ip_address(value) if isinstance(value, str) and "%" not in value else None
    except ValueError:
        address = None
    if (address is None or address.is_unspecified or address.is_multicast or address.version == 6
            and (address.ipv4_mapped is not None or address.is_link_local)):
        raise ValueError(f"guest_http.listen: {value!r} is not one of this host's own IP addresses "
                         "(never a wildcard, multicast, link-local IPv6 or IPv4-mapped address)")
    return str(address)


def _network(value):
    try:
        network = ipaddress.ip_network(value) if isinstance(value, str) else None
    except ValueError:
        network = None
    if network is None:
        raise ValueError(f"guest_http.clients: {value!r} is not a network such as 192.0.2.0/24 with no host bits set")
    if network.prefixlen == 0:
        raise ValueError("guest_http.clients must name the guest network, not every address")
    if network.version == 6 and network.network_address.ipv4_mapped is not None:
        raise ValueError(f"guest_http.clients: write {value!r} as an IPv4 network")
    return str(network)


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"guest_http.names: {value!r} is not a lowercase DNS name")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError(f"guest_http.names: list the address {value!r} under guest_http.listen")


def _values(data, key, check, required=True):
    values = data.get(key, [])
    if not isinstance(values, list) or required and not values:
        raise ValueError(f"guest_http.{key} must be a {'non-empty ' if required else ''}list")
    return list(dict.fromkeys(check(value) for value in values))


def settings(value):
    """host.yaml's guest_http mapping as the gateway's configuration, or None
    when the listener is not wanted. Raises ValueError naming the problem.
    The result is its own normal form: settings(settings(x)) == settings(x)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("guest_http must be a mapping")
    unknown = sorted(str(key) for key in value if key not in FIELDS)
    if unknown:
        raise ValueError("unknown guest_http setting " + ", ".join(unknown) + " (allowed: " + ", ".join(sorted(FIELDS)) + ")")
    port = oh_ports.checked("guest_http.port", value.get("port", oh_ports.GUEST_HTTP_PORT))
    return {"port": port, "listen": _values(value, "listen", _address), "clients": _values(value, "clients", _network),
            "names": _values(value, "names", _name, required=False)}


def origin(scheme, host, port):
    """scheme://host:port, bracketing an IPv6 address."""
    return f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"


def origins(guest):
    """The listener's http origins: its addresses, then its names."""
    return [origin("http", host, guest["port"]) for host in (*guest["listen"], *guest["names"])]


def admitted(peer, networks):
    """Whether a socket peer, (address, port, ...), lies in one of networks.
    An IPv4-mapped IPv6 peer counts as its IPv4 address; a scope is ignored."""
    try:
        address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
    except (TypeError, IndexError, ValueError):
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


@dataclass(frozen=True)
class Listener:
    """What the identity application needs to know about the listener: the
    origins it answers to and the HTTPS site where accounts sign in."""
    origins: frozenset
    secure: str

    @property
    def hosts(self):
        return frozenset(value.removeprefix("http://") for value in self.origins)


def mark(app, listener):
    """The ASGI application for the guest listener's sockets: each request it
    passes on carries this listener in its scope. Any ASGI server can run it."""
    async def guest(scope, receive, send):
        await app({**scope, SCOPE: listener}, receive, send)
    return guest


class Refusals:
    """Counts connections closed for their address. Logs at most once per
    interval, never an address: a busy guest network cannot flood the journal."""

    def __init__(self, interval=60, clock=time.monotonic):
        self.interval, self.clock = interval, clock
        self.count, self.logged = 0, None

    def __call__(self):
        self.count += 1
        now = self.clock()
        if self.logged is None or now - self.logged >= self.interval:
            log.warning("Guest HTTP listener closed %d connection(s) from outside its client networks", self.count)
            self.count, self.logged = 0, now


class Clients:
    """The client networks a Guest listener admits, and its count of refused
    connections. only() turns a bound listening socket into one whose accept()
    closes each connection from elsewhere, so no server reads a byte of it."""

    def __init__(self, networks):
        self.networks = tuple(ipaddress.ip_network(value) for value in networks)
        self.refused = Refusals()

    def only(self, sock):
        admitting = _Admitting(sock.family, sock.type, sock.proto, fileno=sock.detach())
        admitting.clients = self
        return admitting


class _Admitting(socket.socket):
    clients = None

    def accept(self):
        # The event loop accepts until this raises BlockingIOError: the
        # backlog holds no more connections.
        while True:
            connection, peer = super().accept()
            if admitted(peer, self.clients.networks):
                return connection, peer
            connection.close()
            self.clients.refused()
