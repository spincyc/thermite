"""Configured administrator tools, without credentials or runtime health claims."""
from __future__ import annotations

import json
import re

import oh_ports
from oh_conf import LIB

#: The application's own pins: its versions.json, beside lib/.
PINS = LIB.parent / "versions.json"
ADMIN_ASSETS = frozenset({"system.html", "system.json"})
_TOOLS = (
    ("openhab", "openHAB", "Advanced runtime settings and add-ons.", "http", "/settings/", True),
    ("grafana", "Grafana", "Dashboards and history.", "grafana", "/", False),
    ("prometheus", "Prometheus", "Metrics queries.", "prometheus", "/query", True),
)


def manifest(host: dict, runtime_version: str) -> dict:
    """Pure, finite endpoint metadata; the client supplies its current hostname."""
    if not isinstance(host, dict):
        raise ValueError("System metadata requires a host mapping")
    if not isinstance(runtime_version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,63}", runtime_version):
        raise ValueError("System metadata requires a runtime version")
    ports = oh_ports.effective(host)
    return {"version": 1, "runtime": {"version": runtime_version}, "tools": [
        {"id": ident, "label": label, "description": description, "scheme": "http",
         "port": ports[port], "path": path, "localOnly": local}
        for ident, label, description, port, path, local in _TOOLS]}


def described(host: dict | None = None, versions: dict | None = None) -> dict:
    """manifest() of a build's inputs. `host` is instance/host.yaml as
    oh_host read it; without one the tools are at the product's default
    ports. `versions` is versions.json; without it the application's own
    pins (PINS) name the runtime. No site is looked for."""
    if versions is None:
        versions = json.loads(PINS.read_text(encoding="utf-8"))
    try:
        runtime = versions["openhab"]["version"]
    except (KeyError, TypeError):
        raise ValueError("System metadata requires the pinned runtime version") from None
    return manifest({} if host is None else host, runtime)
