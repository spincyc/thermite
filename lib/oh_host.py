"""instance/host.yaml: the facts of the host a site runs on.

load() parses the file and validate() checks every setting of it, before a
unit, a generated file or a deploy uses one. Each section is checked by the
module that reads it (oh_ports, oh_service, oh_metrics, oh_music, oh_shares,
oh_access.guest); nothing here knows a site, a deploy or the program, so
the generator and the deploy read the same file through the same reader.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import oh_metrics as metrics
import oh_music as music
import oh_ports
import oh_service
import oh_shares as shares
from oh_access import guest


class HostError(ValueError):
    """instance/host.yaml is unusable; generated units are never written from it."""


#: Everything an installation keeps on the host's state filesystem lives in
#: this one root-owned directory unless instance/host.yaml names another root
#: (oh_deploy.Installation).
DEFAULT_ROOT = "/srv/thermite"
REQUIRED = ("timezone", "http_port", "https_port", "uid", "gid")
KEYS = frozenset((*REQUIRED, "root", "gateway", "metrics", "guest_http", "music", "shares"))
METRICS_KEYS = frozenset(("units", "retention", "retention_size", "yoke_targets", "node_exporter_uid",
                          *(name + "_port" for name in oh_ports.METRICS)))
#: Host values are pasted into unit files, container arguments and shell
#: words: no whitespace, quotes, systemd specifiers or volume separators.
_ROOT = re.compile(r"/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*")
_TIMEZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")
_UNIT = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_DURATION = re.compile(r"0|(?=\d)(?:\d+y)?(?:\d+w)?(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?:\d+ms)?")
_SIZE = re.compile(r"\d+(?:B|KB|MB|GB|TB|PB|EB)")


def validate(host: object, where: str = "instance/host.yaml") -> dict:
    """The site's host facts, checked completely before anything uses them."""
    def fail(problem: str):
        raise HostError(f"{where}: {problem}")

    if not isinstance(host, dict):
        fail("must be a mapping")
    missing = [key for key in REQUIRED if key not in host]
    unknown = sorted(str(key) for key in host if key not in KEYS)
    if missing:
        fail("missing " + ", ".join(missing))
    if unknown:
        fail("unknown setting " + ", ".join(unknown) + " (allowed: " + ", ".join(sorted(KEYS)) + ")")
    root = host.get("root", DEFAULT_ROOT)
    if (not isinstance(root, str) or not _ROOT.fullmatch(root) or os.path.normpath(root) != root):
        fail("root must be a normalized absolute path below / of letters, digits and ._@+-")
    zone = host["timezone"]
    if not isinstance(zone, str) or not _TIMEZONE.fullmatch(zone) or ".." in zone.split("/"):
        fail("timezone must be an IANA time zone name such as Etc/UTC")
    import zoneinfo
    try:
        zoneinfo.ZoneInfo(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        fail(f"timezone {zone!r} is not a known IANA time zone")
    overrides = host.get("metrics")
    if overrides is not None:
        if not isinstance(overrides, dict):
            fail("metrics must be a mapping")
        unknown = sorted(str(key) for key in overrides if key not in METRICS_KEYS)
        if unknown:
            fail("unknown metrics setting " + ", ".join(unknown))
        units = overrides.get("units", [])
        if not isinstance(units, list) or not all(isinstance(unit, str) and _UNIT.fullmatch(unit) for unit in units):
            fail("metrics.units must be a list of systemd unit names")
        if "retention" in overrides and not (isinstance(overrides["retention"], str)
                                             and _DURATION.fullmatch(overrides["retention"])):
            fail("metrics.retention must be a Prometheus duration such as 395d")
        if "retention_size" in overrides and not (isinstance(overrides["retention_size"], str)
                                                  and _SIZE.fullmatch(overrides["retention_size"])):
            fail("metrics.retention_size must be a Prometheus size such as 12GB")
    try:
        guest.settings(host.get("guest_http"))
        music.settings(host)
        for name, share in shares.settings(host).items():
            if share.where == root or share.where.startswith(root + "/") or root.startswith(share.where + "/"):
                raise ValueError(f"shares.{name}.where and the installation root ({root}) overlap")
        oh_service.validate_identity(host["uid"], host["gid"])
        own = metrics.node_exporter_id(host)
        if type(own) is not int or own <= 0 or own in (host["uid"], host["gid"]):
            raise ValueError("metrics.node_exporter_uid must be a positive number that is neither uid nor gid "
                             "(by default one below uid): node_exporter runs as an account of its own")
        oh_ports.effective(host)
        metrics.yoke_targets(host)
    except ValueError as exc:
        fail(str(exc))
    return host


def load(source: Path | bytes, where: str | None = None) -> dict:
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
        raise HostError(f"{where}: not valid YAML ({str(exc).splitlines()[0]})") from None
    if refused:
        raise HostError(f"{where}: " + "; ".join(f"line {line}: {sentence}" for line, _, _, sentence in refused))
    return validate(host, where)
