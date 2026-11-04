"""Thermite's contiguous TCP listener defaults and host override validation.

The allocation is a local convention, not an IANA registration. See docs/ports.md.
Device protocol destinations and discovery multicast ports do not belong here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import re
import socket
import stat
from types import MappingProxyType

BASE = 13600
NAMES = (
    "access", "http", "https", "karaf", "lsp", "helper", "prometheus", "node_exporter",
    "blackbox", "exporter", "grafana", "alertmanager", "ntfy", "notifications", "callbacks",
    "sandbox_http", "sandbox_https", "sandbox_karaf", "boot_http", "boot_https", "boot_karaf", "docs", "preview",
    "native_shutdown", "sandbox_shutdown", "boot_shutdown",
)
DEFAULTS = MappingProxyType({name: BASE + offset for offset, name in enumerate(NAMES)})
ACCESS_PORT = DEFAULTS["access"]
HTTP_PORT = DEFAULTS["http"]
HTTPS_PORT = DEFAULTS["https"]
KARAF_PORT = DEFAULTS["karaf"]
SHUTDOWN_PORT = DEFAULTS["native_shutdown"]
LSP_PORT = DEFAULTS["lsp"]
HELPER_PORT = DEFAULTS["helper"]
CALLBACK_PORT = DEFAULTS["callbacks"]
DOCS_PORT = DEFAULTS["docs"]
SANDBOX_URL = f"http://127.0.0.1:{DEFAULTS['sandbox_http']}"
NATIVE_URL = f"http://127.0.0.1:{HTTP_PORT}"
METRICS = ("prometheus", "node_exporter", "blackbox", "exporter", "grafana", "alertmanager", "ntfy", "notifications")
SHELL_CFG = Path("etc/org.apache.karaf.shell.cfg")
CUSTOM_PROPERTIES = Path("etc/custom.properties")
SHUTDOWN_KEYS = frozenset(("karaf.shutdown.port", "karaf.shutdown.host"))


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


def listeners(selected: dict[str, int], *, upstream: int | None = None,
              allocated: dict[str, int] | None = None) -> dict[str, int]:
    """Validate staged listeners against the selected site's complete allocation."""
    if allocated is not None and set(allocated) != set(DEFAULTS):
        raise ValueError("host listeners must contain the complete Thermite allocation")
    ports = unique(dict(DEFAULTS if allocated is None else allocated))
    ports.update(selected)
    if upstream is not None:
        upstream = checked("native HTTP", upstream)
        if allocated is not None and upstream != ports["http"]:
            # An explicit upstream does not relocate this site's configured
            # native listener. Both origins must remain reserved.
            ports["native HTTP upstream"] = upstream
        else:
            ports["http"] = upstream
    return unique(ports)


def listener(name: str, value: int, *, upstream: int | None = None) -> int:
    """Validate a separately staged listener against defaults and its actual upstream."""
    listeners({name: value}, upstream=upstream)
    return value


def native_configured_bytes(data: bytes | None) -> bool:
    if data is None:
        return False
    values = {}
    for line in data.decode("utf-8").splitlines():
        if match := re.fullmatch(r"\s*(sshPort|sshHost)\s*=\s*(\S+)\s*", line):
            values[match[1]] = match[2]
    return values == {"sshPort": str(KARAF_PORT), "sshHost": "127.0.0.1"}


def _property_entries(text: str):
    """Group Java Properties logical entries while retaining their physical text.

    Only CR/LF terminate natural lines; comments cannot continue. Escaped keys
    and empty continuation-only entries require manual review rather than
    guessing which property they name. Values of unrelated entries are opaque.
    """
    raw, logical, continued = "", "", False
    for natural in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text):
        if not natural:
            continue
        body = natural.rstrip("\r\n").lstrip(" \t\f")
        if not continued and (not body or body.startswith(("#", "!"))):
            yield natural, None, None, False
            continue
        raw += natural
        logical += body
        continued = (len(body) - len(body.rstrip("\\"))) % 2 == 1
        if continued:
            logical = logical[:-1]
            if not logical:
                raise ValueError("empty property continuations require manual review")
            continue
        key, value = _property_pair(logical)
        yield raw, key, value, False
        raw, logical = "", ""
    if raw:
        key, value = _property_pair(logical)
        yield raw, key, value, continued


def _property_pair(logical: str) -> tuple[str, str]:
    key = re.split(r"[ \t\f:=]", logical, maxsplit=1)[0]
    if "\\" in key:
        raise ValueError("escaped property keys require manual review")
    value = logical[len(key):].lstrip(" \t\f")
    if value.startswith(("=", ":")):
        value = value[1:].lstrip(" \t\f")
    return key, value


def shutdown_configured_bytes(data: bytes | None, port: int = SHUTDOWN_PORT) -> bool:
    if data is None:
        return False
    try:
        values = {key: value for _, key, value, _ in _property_entries(data.decode("utf-8")) if key in SHUTDOWN_KEYS}
    except ValueError:
        return False
    return values == {"karaf.shutdown.port": str(port), "karaf.shutdown.host": "127.0.0.1"}


def shutdown_properties(text: str, port: int) -> str:
    """Persist a fixed loopback socket for both Karaf Main and its Stop client.

    Port zero creates an unallocated ephemeral listener. A negative port uses
    Stop's PID fallback, which force-kills after roughly eleven seconds; keep
    the socket request for unhurried cold-cache shutdown instead.
    """
    checked("Karaf shutdown", port)
    kept = [(raw, continued) for raw, key, _, continued in _property_entries(text) if key not in SHUTDOWN_KEYS]
    prefix = "".join(raw for raw, _ in kept)
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    if kept and kept[-1][1]:
        # Finish an EOF continuation with a blank natural line. A lone CR
        # needs two LFs because the first one completes its CRLF terminator.
        prefix += "\n\n" if prefix.endswith("\r") else "\n"
    return prefix + f"karaf.shutdown.port = {port}\nkaraf.shutdown.host = 127.0.0.1\n"


def configure_shutdown(userdata: Path, port: int) -> None:
    path = Path(userdata) / CUSTOM_PROPERTIES
    path.write_bytes(shutdown_properties(path.read_bytes().decode("utf-8"), port).encode("utf-8"))


def configure_native(userdata: Path) -> None:
    """Pin native console/shutdown sockets without changing other Karaf settings."""
    path = Path(userdata) / SHELL_CFG
    text = path.read_text(encoding="utf-8")
    custom = Path(userdata) / CUSTOM_PROPERTIES
    shutdown = shutdown_properties(custom.read_bytes().decode("utf-8"), SHUTDOWN_PORT)
    lines = [line for line in text.splitlines() if not re.match(r"\s*(sshPort|sshHost)\s*=", line)]
    path.write_text("\n".join([*lines, f"sshPort = {KARAF_PORT}", "sshHost = 127.0.0.1", ""]), encoding="utf-8")
    custom.write_bytes(shutdown.encode("utf-8"))


@contextmanager
def _native_stopped(userdata: Path):
    """Hold this pinned Karaf instance's existing lock while changing its files.

    The distribution sets karaf.lock.dir=${karaf.data}/tmp. Karaf's
    SimpleFileLock uses a POSIX record lock on its 'lock' file on Linux.
    Missing first-install lock paths are not created here; the caller still
    stops the instance's service and checks its destination listeners.
    """
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = None
    try:
        for part in Path(os.path.abspath(userdata)).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        try:
            temporary = os.open("tmp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
        except FileNotFoundError:
            yield
            return
        os.close(directory)
        directory = temporary
        try:
            descriptor = os.open("lock", os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=directory)
        except FileNotFoundError:
            yield
            return
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("native Karaf lock must be a single-link regular file")
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise ValueError("stop the target native Thermite openHAB before changing listeners: its userdata lock is held") from None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set stopped native console/shutdown listeners to Thermite's loopback ports")
    parser.add_argument("userdata", type=Path)
    parser.add_argument("--http-port", type=int, default=HTTP_PORT)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args(argv)
    # The caller stops this installation's service before reconfiguration.
    # These are availability checks for its selected listeners: another
    # openHAB's default console (8101) does not identify a Thermite process.
    for port in dict.fromkeys((args.http_port, KARAF_PORT, SHUTDOWN_PORT)):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                parser.error(f"stop native Thermite openHAB before changing listener configuration (port {port} answers)")
        except OSError:
            pass
    try:
        with _native_stopped(args.userdata):
            configure_native(args.userdata)
            if args.uid is not None and args.gid is not None:
                for relative in (SHELL_CFG, CUSTOM_PROPERTIES):
                    os.chown(args.userdata / relative, args.uid, args.gid)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"could not configure the initialized native listeners: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
