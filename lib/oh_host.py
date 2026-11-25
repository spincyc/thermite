"""instance/host.yaml: the facts of the host a site runs on.

SHAPE declares every top-level and nested key (oh_shape.Key), from which key
checks, JSON Schema (schema()) and documentation tables derive. load() parses
the file, problems() collects every structural and semantic error in one pass,
and validate() raises HostError with every collected problem or returns the
host mapping with optional defaults applied. Each section is checked by the
module that reads it (oh_ports, oh_service, oh_metrics, oh_music, oh_shares,
oh_access.guest); nothing here knows a site, a deploy or the program, so the
generator and the deploy read the same file through the same reader.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import oh_metrics as metrics
import oh_music as music
import oh_ports
import oh_service
import oh_shares as shares
import oh_shape
from oh_access import guest
from oh_shape import LIST, MAPPING, NUMBER, TEXT, TRUTH, Key
from oh_types import HostFile


class HostError(ValueError):
    """instance/host.yaml is unusable; generated units are never written from it."""

    def __init__(self, message: str, problems: list[str] | tuple[str, ...] = ()) -> None:
        self.problems = list(problems)
        super().__init__(message)


#: Everything an installation keeps on the host's state filesystem lives in
#: this one root-owned directory unless instance/host.yaml names another root
#: (oh_deploy.Installation).
DEFAULT_ROOT = "/srv/thermite"
#: Optional internal host defaults applied when omitted from instance/host.yaml.
DEFAULTS: dict[str, int] = {
    "http_port": oh_ports.HTTP_PORT,
    "https_port": oh_ports.HTTPS_PORT,
    "uid": oh_service.UID,
    "gid": oh_service.GID,
}

_METRICS_PORT_DOCS: dict[str, str] = {
    "prometheus": f"Prometheus loopback listener port (default {oh_ports.DEFAULTS['prometheus']})",
    "node_exporter": f"node_exporter loopback listener port (default {oh_ports.DEFAULTS['node_exporter']})",
    "blackbox": f"blackbox_exporter loopback listener port (default {oh_ports.DEFAULTS['blackbox']})",
    "exporter": f"thermite metrics exporter loopback listener port (default {oh_ports.DEFAULTS['exporter']})",
    "grafana": f"Grafana LAN listener port (default {oh_ports.DEFAULTS['grafana']})",
}

_GATEWAY_KEYS: dict[str, Key] = {
    "port": Key(NUMBER, f"the HTTPS gateway listener port (default {oh_ports.ACCESS_PORT})"),
    "listen": Key(LIST, "this host's own IP addresses the HTTPS gateway binds besides loopback",
                  each=Key(TEXT, "an IPv4 or IPv6 address of this host")),
}

_METRICS_KEYS: dict[str, Key] = {
    "units": Key(LIST, "additional systemd unit names for node_exporter to report on",
                 each=Key(TEXT, "a systemd unit name")),
    "retention": Key(TEXT, "how long Prometheus keeps samples, a duration such as 395d"),
    "retention_size": Key(TEXT, "maximum disk Prometheus uses, a size such as 12GB"),
    "yoke_targets": Key(LIST, "optional external Yoke observer host:port endpoints for Prometheus to scrape",
                        each=Key(TEXT, "a host:port endpoint")),
    "node_exporter_uid": Key(NUMBER, "positive numeric uid and gid for node_exporter's own account, distinct from "
                                     "uid and gid"),
    **{f"{name}_port": Key(NUMBER, _METRICS_PORT_DOCS[name]) for name in oh_ports.METRICS},
}

_GUEST_HTTP_KEYS: dict[str, Key] = {
    "port": Key(NUMBER, f"the plain-HTTP Guest listener port (default {oh_ports.GUEST_HTTP_PORT})"),
    "listen": Key(LIST, "this host's own IP addresses the Guest listener binds", required=True,
                  each=Key(TEXT, "an IPv4 or IPv6 address of this host")),
    "clients": Key(LIST, "client CIDR networks admitted by the Guest listener", required=True,
                   each=Key(TEXT, "an IPv4 or IPv6 CIDR network")),
    "names": Key(LIST, "lowercase DNS hostnames guests may type to reach the Guest listener",
                 each=Key(TEXT, "a lowercase DNS hostname")),
}

_MUSIC_KEYS: dict[str, Key] = {
    "libraries": Key(MAPPING, "music libraries mounted read-only into Music Assistant", by="library name",
                     each=Key(TEXT, "an absolute host directory path"), empty=True),
    "port": Key(NUMBER, f"Music Assistant loopback API listener port (default {oh_ports.DEFAULTS['music']})"),
    "stream_port": Key(NUMBER, f"Music Assistant LAN audio stream listener port (default "
                               f"{oh_ports.DEFAULTS['music_stream']})"),
}

_SHARE_KEYS: dict[str, Key] = {
    "type": Key(TEXT, "the network filesystem type", required=True, choices=tuple(shares.TYPES)),
    "server": Key(TEXT, "the server hostname or IPv4 address", required=True),
    "share": Key(TEXT, "the SMB share name (and optional subdirectory), for cifs shares"),
    "export": Key(TEXT, "the exported absolute directory path, for nfs shares"),
    "where": Key(TEXT, "the local mount point below /mnt or /media", required=True),
    "writable": Key(TRUTH, "whether the share is mounted read-write instead of read-only"),
    "version": Key(TEXT, "optional quoted SMB or NFS protocol version such as 3.1.1 or 4.2"),
}

#: The complete shape of instance/host.yaml, declared once (oh_shape.Key).
#: Validation, JSON Schema (schema()) and documentation reference tables
#: derive from this declaration.
SHAPE: Key = Key(MAPPING, "the host file", keys={
    "version": Key(NUMBER, "the format of the file: 1", choices=(1,)),
    "timezone": Key(TEXT, "the host's IANA time zone name such as Etc/UTC", required=True),
    "root": Key(TEXT, f"the root-owned installation directory (default {DEFAULT_ROOT})"),
    "http_port": Key(NUMBER, f"the native openHAB HTTP listener port on loopback (default {oh_ports.HTTP_PORT})"),
    "https_port": Key(NUMBER, f"the native openHAB HTTPS listener port on loopback (default {oh_ports.HTTPS_PORT})"),
    "uid": Key(NUMBER, f"the positive non-root numeric user id of the thermite service account (default {oh_service.UID})"),
    "gid": Key(NUMBER, f"the positive non-root numeric group id of the thermite service account (default {oh_service.GID})"),
    "gateway": Key(MAPPING, "HTTPS gateway listener port and bind addresses", keys=_GATEWAY_KEYS, empty=True),
    "metrics": Key(MAPPING, "Prometheus, Grafana and node_exporter host overrides", keys=_METRICS_KEYS, empty=True),
    "guest_http": Key(MAPPING, "opt-in plain-HTTP Guest listener restricted to listed client networks",
                      keys=_GUEST_HTTP_KEYS, empty=True),
    "music": Key(MAPPING, "opt-in Music Assistant libraries and listener ports", keys=_MUSIC_KEYS, empty=True),
    "shares": Key(MAPPING, "network shares mounted by Thermite", by="share name",
                  each=Key(MAPPING, "a network share", keys=_SHARE_KEYS), empty=True),
})

assert SHAPE.keys is not None
REQUIRED = tuple(name for name, declaration in SHAPE.keys.items() if declaration.required)
KEYS = frozenset(SHAPE.keys)
METRICS_KEYS = frozenset(_METRICS_KEYS)

#: Host values are pasted into unit files, container arguments and shell
#: words: no whitespace, quotes, systemd specifiers or volume separators.
_ROOT = re.compile(r"/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*")
_TIMEZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")
_UNIT = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_DURATION = re.compile(r"0|(?=\d)(?:\d+y)?(?:\d+w)?(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?:\d+ms)?")
_SIZE = re.compile(r"\d+(?:B|KB|MB|GB|TB|PB|EB)")


def schema(*, schema_id: str | None = None) -> dict[str, Any]:
    """instance/host.yaml as JSON Schema (draft 2020-12), derived from SHAPE."""
    out: dict[str, Any] = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if schema_id is not None:
        out["$id"] = schema_id
    out["title"] = "thermite host file"
    out.update(oh_shape.schema(SHAPE))
    return out


def problems(host: object) -> list[str]:
    """Collect every structural and semantic problem in a parsed host mapping."""
    if not oh_shape.of_kind(SHAPE, host):
        return ["must be a mapping"]
    assert isinstance(host, dict)
    assert SHAPE.keys is not None

    found: list[str] = []
    # Track top-level keys that already have a domain-specific message so the
    # oh_shape.mismatches() safety pass never duplicates an existing error.
    reported: set[str] = set()

    def record(message: str, key: str | None = None) -> None:
        found.append(message)
        if key is not None:
            reported.add(key)

    # Required keys (timezone) first so an empty mapping names what is missing.
    for key in REQUIRED:
        if key not in host or host[key] is None:
            record(f"missing {key}", key)

    unknown = sorted(str(key) for key in oh_shape.unknown(SHAPE.keys, host))
    if unknown:
        record("unknown setting " + ", ".join(unknown) + " (allowed: " + ", ".join(sorted(KEYS)) + ")")

    if "version" in host:
        version = host["version"]
        if type(version) is not int or not oh_shape.fits(SHAPE.keys["version"], version):
            record("version must be 1", "version")

    if "timezone" in host and host["timezone"] is not None:
        zone = host["timezone"]
        if not isinstance(zone, str) or not _TIMEZONE.fullmatch(zone) or ".." in zone.split("/"):
            record("timezone must be an IANA time zone name such as Etc/UTC", "timezone")
        else:
            import zoneinfo
            try:
                zoneinfo.ZoneInfo(zone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                record(f"timezone {zone!r} is not a known IANA time zone", "timezone")

    root = host.get("root", DEFAULT_ROOT)
    root_ok = isinstance(root, str) and bool(_ROOT.fullmatch(root)) and os.path.normpath(root) == root
    if not root_ok:
        record("root must be a normalized absolute path below / of letters, digits and ._@+-", "root")

    uid = host["uid"] if "uid" in host else DEFAULTS["uid"]
    gid = host["gid"] if "gid" in host else DEFAULTS["gid"]
    identity_ok = True
    try:
        oh_service.validate_identity(uid, gid)
    except ValueError as exc:
        identity_ok = False
        record(str(exc), "uid")
        reported.add("gid")

    overrides = host.get("metrics")
    metrics_is_dict = isinstance(overrides, dict)
    if overrides is not None:
        if not metrics_is_dict:
            record("metrics must be a mapping", "metrics")
        else:
            unknown_metrics = sorted(str(key) for key in oh_shape.unknown(_METRICS_KEYS, overrides))
            if unknown_metrics:
                record("unknown metrics setting " + ", ".join(unknown_metrics), "metrics")
            units = overrides.get("units", [])
            if not isinstance(units, list) or not all(isinstance(unit, str) and _UNIT.fullmatch(unit) for unit in units):
                record("metrics.units must be a list of systemd unit names", "metrics")
            if "retention" in overrides and not (isinstance(overrides["retention"], str)
                                                 and _DURATION.fullmatch(overrides["retention"])):
                record("metrics.retention must be a Prometheus duration such as 395d", "metrics")
            if "retention_size" in overrides and not (isinstance(overrides["retention_size"], str)
                                                      and _SIZE.fullmatch(overrides["retention_size"])):
                record("metrics.retention_size must be a Prometheus size such as 12GB", "metrics")
            try:
                metrics.yoke_targets(host)
            except ValueError as exc:
                record(str(exc), "metrics")

    if identity_ok and (overrides is None or isinstance(overrides, dict)):
        own = overrides["node_exporter_uid"] if (isinstance(overrides, dict) and "node_exporter_uid" in overrides) else uid - 1
        if type(own) is not int or own <= 0 or own in (uid, gid):
            record("metrics.node_exporter_uid must be a positive number that is neither uid nor gid "
                   "(by default one below uid): node_exporter runs as an account of its own", "metrics")

    guest_ok = True
    try:
        guest.settings(host.get("guest_http"))
    except ValueError as exc:
        guest_ok = False
        record(str(exc), "guest_http")

    music_ok = True
    try:
        music.settings(host)
    except ValueError as exc:
        music_ok = False
        record(str(exc), "music")

    try:
        declared_shares = shares.settings(host)
    except ValueError as exc:
        record(str(exc), "shares")
    else:
        if root_ok:
            for name, share in declared_shares.items():
                if share.where == root or share.where.startswith(root + "/") or root.startswith(share.where + "/"):
                    record(f"shares.{name}.where and the installation root ({root}) overlap", "shares")

    # Gateway settings and listener ports (collected independently, then
    # checked together for collisions).
    gateway_value = host.get("gateway")
    gateway_ok = True
    if gateway_value is not None:
        if not isinstance(gateway_value, dict):
            gateway_ok = False
            record("gateway: must be a mapping", "gateway")
        else:
            unknown_gw = sorted(str(key) for key in oh_shape.unknown(_GATEWAY_KEYS, gateway_value))
            if unknown_gw:
                gateway_ok = False
                record("unknown gateway setting " + ", ".join(unknown_gw) + " (allowed: "
                       + ", ".join(oh_ports.GATEWAY_FIELDS) + ")", "gateway")
            if "port" in gateway_value:
                try:
                    oh_ports.checked("gateway.port", gateway_value["port"])
                except ValueError as exc:
                    gateway_ok = False
                    record(str(exc), "gateway")
            if "listen" in gateway_value:
                try:
                    oh_ports.gateway_listen(host)
                except ValueError as exc:
                    gateway_ok = False
                    record(str(exc), "gateway")

    ports_ok = True
    for name in ("http", "https"):
        field = name + "_port"
        if field in host:
            try:
                oh_ports.checked(name, host[field])
            except ValueError as exc:
                ports_ok = False
                record(str(exc), field)

    metrics_ports_ok = True
    if metrics_is_dict:
        assert isinstance(overrides, dict)
        for name in oh_ports.METRICS:
            field = name + "_port"
            if field in overrides:
                try:
                    oh_ports.checked(name, overrides[field])
                except ValueError as exc:
                    metrics_ports_ok = False
                    record(str(exc), "metrics")

    music_ports_ok = True
    music_value = host.get("music")
    if music_ok and isinstance(music_value, dict):
        for name, key in oh_ports.MUSIC.items():
            if key in music_value:
                try:
                    oh_ports.checked(name, music_value[key])
                except ValueError as exc:
                    music_ports_ok = False
                    record(str(exc), "music")

    if ports_ok:
        effective_host: dict[str, Any] = {}
        for field in ("http_port", "https_port"):
            if field in host:
                effective_host[field] = host[field]
        if gateway_ok and isinstance(gateway_value, dict):
            effective_host["gateway"] = gateway_value
        if metrics_is_dict and metrics_ports_ok:
            effective_host["metrics"] = overrides
        if music_ok and music_ports_ok and isinstance(music_value, dict):
            effective_host["music"] = music_value
        if guest_ok and host.get("guest_http") is not None:
            effective_host["guest_http"] = host["guest_http"]
        try:
            oh_ports.effective(effective_host)
        except ValueError as exc:
            record(str(exc))

    # Structural safety pass via oh_shape.mismatches() for any declared key
    # whose semantic checks did not already report an error.
    for key, declaration in SHAPE.keys.items():
        if key in reported or host.get(key) is None:
            continue
        found.extend(oh_shape.mismatches(declaration, host[key], key))

    return found


def validate(host: object, where: str = "instance/host.yaml") -> dict[str, Any]:
    """The site's host facts, checked completely before anything uses them.

    Raises HostError carrying all collected problems when invalid. When valid,
    returns `host` directly if all optional defaults are already present, or a
    new mapping with DEFAULTS populated for omitted http_port/https_port/uid/gid.
    """
    found = problems(host)
    if found:
        raise HostError(f"{where}: " + "; ".join(found), problems=found)
    assert isinstance(host, dict)
    if all(key in host for key in DEFAULTS):
        return host
    return {**DEFAULTS, **host}


def load(source: Path | bytes, where: str | None = None) -> dict[str, Any]:
    """Parse and validate host facts from a file or committed bytes. The
    file is read as the house file is (oh_inventory.StrictLoader): what
    the versions of YAML read differently, and a key written twice, are
    refused with the line and what to write."""
    # Here, not at the top: the install imports this module (through
    # oh_deploy) before it has installed PyYAML.
    import yaml
    import oh_inventory

    if isinstance(source, bytes):
        where, data = where or "instance/host.yaml", source
    else:
        where, data = where or str(source), Path(source).read_bytes()
    try:
        host, refused = oh_inventory.read(data)
    except yaml.YAMLError as exc:
        problem = f"not valid YAML ({str(exc).splitlines()[0]})"
        raise HostError(f"{where}: {problem}", problems=[problem]) from None
    if refused:
        items = [f"line {line}: {sentence}" for line, _, _, sentence in refused]
        raise HostError(f"{where}: " + "; ".join(items), problems=items)
    return validate(host, where)


__all__ = [
    "DEFAULTS",
    "DEFAULT_ROOT",
    "HostError",
    "HostFile",
    "KEYS",
    "METRICS_KEYS",
    "REQUIRED",
    "SHAPE",
    "load",
    "problems",
    "schema",
    "validate",
]
