"""Dependency-free identifier and address syntax shared by the gateway, the
worker, the callback relay and terminal clients. Importing it loads no
framework or HTTP client, so operator-side staging can use it too."""

import ipaddress
import re
from urllib.parse import urlsplit

# Inventory room IDs are snake_case; the identity tables store at most 100.
ROOM_PATTERN = r"[a-z][a-z0-9_]{0,99}"
ROOM_ID = re.compile(ROOM_PATTERN)


class RoomConverter:
    """URL converter: a route never reaches a view with a malformed room ID."""
    regex = ROOM_PATTERN

    def to_python(self, value):
        return value

    def to_url(self, value):
        if not isinstance(value, str) or not ROOM_ID.fullmatch(value):
            raise ValueError("Invalid room ID")
        return value


def loopback_origin(value):
    """An explicit http://127.0.0.1 or http://[::1] origin with no path,
    credentials, query or fragment. Returns the parsed URL; callers decide
    whether a port is required. Raises ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("Loopback origin must be text")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Not an explicit loopback HTTP origin")
    parsed.port  # Raises ValueError for an out-of-range or malformed port.
    return parsed


def peer_bucket(address):
    """The unit a direct peer is counted in: one IPv4 address, or one IPv6
    /64 (a single host normally controls a whole /64). IPv4-mapped IPv6
    addresses count as their IPv4 address. Unparseable input is one bucket."""
    try:
        parsed = ipaddress.ip_address(str(address or "").split("%", 1)[0])
    except ValueError:
        return "unknown"
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if parsed.version == 4:
        return str(parsed)
    return str(ipaddress.ip_network(str(parsed) + "/64", strict=False))
