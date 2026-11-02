"""Optional standard Prometheus targets for independently operated Yoke observers.

This module only validates destination addresses and builds scrape data. It
does not import Yoke, discover endpoints, or start a monitoring process.
"""

from __future__ import annotations

import ipaddress
import re

_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
_PORT = re.compile(r"[0-9]{1,5}\Z")


def _address(value: object, where: str) -> tuple[str, int]:
    """Validate an explicit host:port and return its duplicate-detection key."""
    message = f"{where}: must be a hostname or IP address with an explicit port (IPv6 in brackets)"
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ValueError(message)
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:([0-9]{1,5})", value)
        if match is None or "%" in match[1]:
            raise ValueError(message)
        try:
            hostname = str(ipaddress.IPv6Address(match[1]))
        except ValueError as error:
            raise ValueError(message) from error
        port = match[2]
    else:
        if value.count(":") != 1:
            raise ValueError(message)
        hostname, port = value.split(":")
        hostname = hostname.removesuffix(".").lower()
        if (not hostname or len(hostname) > 253
                or any(_LABEL.fullmatch(label) is None for label in hostname.split("."))):
            raise ValueError(message)
        # A dotted numeric address must be IPv4, not a misspelled hostname.
        if re.fullmatch(r"[0-9.]+", hostname):
            try:
                hostname = str(ipaddress.IPv4Address(hostname))
            except ValueError as error:
                raise ValueError(message) from error
    if _PORT.fullmatch(port) is None or not 1 <= int(port) <= 65535:
        raise ValueError(f"{where}: port must be an integer from 1 to 65535")
    return hostname, int(port)


def targets(host: dict) -> tuple[str, ...]:
    """Sorted configured HTTP destinations; absent or empty means disabled."""
    metrics = host.get("metrics")
    if metrics is None:
        metrics = {}
    if not isinstance(metrics, dict):
        raise ValueError("metrics: must be a mapping")
    configured = metrics.get("yoke_targets", [])
    if not isinstance(configured, list):
        raise ValueError("metrics.yoke_targets: must be a list of host:port strings")
    seen: set[tuple[str, int]] = set()
    for index, value in enumerate(configured):
        where = f"metrics.yoke_targets[{index}]"
        key = _address(value, where)
        if key in seen:
            raise ValueError(f"{where}: duplicate Yoke target {value!r}")
        seen.add(key)
    return tuple(sorted(configured))


def scrape_job(host: dict) -> dict | None:
    """One optional job, using Prometheus' HTTP /metrics and timing defaults."""
    configured = targets(host)
    if not configured:
        return None
    return {"job_name": "yoke", "static_configs": [{"targets": list(configured)}]}
