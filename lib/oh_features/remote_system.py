"""Configured administrator tools, without credentials or runtime health claims."""
from __future__ import annotations

import json
from pathlib import Path
import re

import oh_ports
import oh_access_mode

ADMIN_ASSETS = frozenset({"system.html", "system.json"})
_TOOLS = (
    ("openhab", "openHAB", "Advanced runtime settings and add-ons.", "http", "/settings/", False),
    ("grafana", "Grafana", "Dashboards and history.", "grafana", "/", False),
    ("prometheus", "Prometheus", "Metrics queries.", "prometheus", "/query", True),
    ("alertmanager", "Alertmanager", "Alert routing and silences.", "alertmanager", "/", True),
    ("ntfy", "ntfy", "Local notification history.", "ntfy", "/", True),
)


def manifest(host: dict, runtime_version: str, *, mode: str = "authenticated") -> dict:
    """Pure, finite endpoint metadata; the client supplies its current hostname."""
    if not isinstance(host, dict):
        raise ValueError("System metadata requires a host mapping")
    if mode not in oh_access_mode.MODES:
        raise ValueError("System metadata requires a valid access mode")
    if not isinstance(runtime_version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,63}", runtime_version):
        raise ValueError("System metadata requires a runtime version")
    ports = oh_ports.effective(host)
    return {"version": 1, "runtime": {"version": runtime_version}, "tools": [
        {"id": ident, "label": label, "description": description, "scheme": "http",
         "port": ports[port], "path": path, "localOnly": local or ident == "openhab" and mode != "native"}
        for ident, label, description, port, path, local in _TOOLS]}


def selected(*, site: Path | None = None, program: Path | None = None, mode: str = "authenticated") -> dict:
    """Read only the selected host settings and the application's runtime pin."""
    import yaml
    from oh_paths import PROGRAM, SITE

    host = yaml.safe_load((Path(site or SITE) / "instance/host.yaml").read_text(encoding="utf-8"))
    versions = json.loads((Path(program or PROGRAM) / "versions.json").read_text(encoding="utf-8"))
    try:
        runtime = versions["openhab"]["version"]
    except (KeyError, TypeError):
        raise ValueError("System metadata requires the pinned runtime version") from None
    return manifest(host, runtime, mode=mode)
