"""Thermite's contiguous TCP listener defaults and host override validation.

The allocation is a local convention, not an IANA registration. See docs/ports.md.
Device protocol destinations and discovery multicast ports do not belong here.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import socket
from types import MappingProxyType

BASE = 13600
NAMES = (
    "access", "http", "https", "karaf", "lsp", "helper", "prometheus", "node_exporter",
    "blackbox", "exporter", "grafana", "alertmanager", "ntfy", "notifications", "callbacks",
    "sandbox_http", "sandbox_https", "sandbox_karaf", "boot_http", "boot_https", "boot_karaf", "docs", "preview",
)
DEFAULTS = MappingProxyType({name: BASE + offset for offset, name in enumerate(NAMES)})
ACCESS_PORT = DEFAULTS["access"]
HTTP_PORT = DEFAULTS["http"]
HTTPS_PORT = DEFAULTS["https"]
KARAF_PORT = DEFAULTS["karaf"]
LSP_PORT = DEFAULTS["lsp"]
HELPER_PORT = DEFAULTS["helper"]
CALLBACK_PORT = DEFAULTS["callbacks"]
DOCS_PORT = DEFAULTS["docs"]
SANDBOX_URL = f"http://127.0.0.1:{DEFAULTS['sandbox_http']}"
NATIVE_URL = f"http://127.0.0.1:{HTTP_PORT}"
METRICS = ("prometheus", "node_exporter", "blackbox", "exporter", "grafana", "alertmanager", "ntfy", "notifications")
SHELL_CFG = Path("etc/org.apache.karaf.shell.cfg")


def checked(name: str, value: int) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError(f"{name}: listener port must be an integer from 1024 to 65535")
    return value


def unique(ports: dict[str, int]) -> dict[str, int]:
    seen = {}
    for name, value in ports.items():
        checked(name, value)
        if value in seen:
            raise ValueError(f"listener port {value} collides: {seen[value]} and {name}")
        seen[value] = name
    return ports


def effective(host: dict | None = None) -> dict[str, int]:
    """Validate production overrides against each other and the remaining listeners."""
    host = host or {}
    result = dict(DEFAULTS)
    for name in ("http", "https"):
        result[name] = host.get(name + "_port", result[name])
    metrics = host.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("metrics: must be a mapping")
    for name in METRICS:
        result[name] = metrics.get(name + "_port", result[name])
    return unique(result)


def listener(name: str, value: int, *, upstream: int | None = None) -> int:
    """Validate a separately staged listener against defaults and its actual upstream."""
    ports = dict(DEFAULTS)
    ports[name] = value
    if upstream is not None:
        ports["http"] = checked("native HTTP", upstream)
    unique(ports)
    return value


def native_configured_bytes(data: bytes | None) -> bool:
    if data is None:
        return False
    values = {}
    for line in data.decode("utf-8").splitlines():
        if match := re.fullmatch(r"\s*(sshPort|sshHost)\s*=\s*(\S+)\s*", line):
            values[match[1]] = match[2]
    return values == {"sshPort": str(KARAF_PORT), "sshHost": "127.0.0.1"}


def configure_native(userdata: Path) -> None:
    """Change only the native console's port and keep it restricted to loopback."""
    path = Path(userdata) / SHELL_CFG
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not re.match(r"\s*(sshPort|sshHost)\s*=", line)]
    path.write_text("\n".join([*lines, f"sshPort = {KARAF_PORT}", "sshHost = 127.0.0.1", ""]), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set the stopped native console's Thermite listener and loopback address")
    parser.add_argument("userdata", type=Path)
    parser.add_argument("--http-port", type=int, default=HTTP_PORT)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args(argv)
    for port in (args.http_port, HTTP_PORT, KARAF_PORT, 8101):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                parser.error(f"stop native openHAB before changing listener configuration (port {port} answers)")
        except OSError:
            pass
    try:
        configure_native(args.userdata)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"could not configure the initialized native console: {exc}\n")
    if args.uid is not None and args.gid is not None:
        os.chown(args.userdata / SHELL_CFG, args.uid, args.gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
