"""Thermite's contiguous TCP listener defaults and host override validation.

The allocation is a local convention, not an IANA registration. See docs/ports.md.
Device protocol destinations and discovery multicast ports do not belong here.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

if __name__ == "__main__":
    # Release validation accepts installer-produced bytecode as inert.
    # Never execute or write adjacent .pyc files when run directly as root.
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(Path(__file__).resolve().parent / (".uncached-" + os.urandom(16).hex()))

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import ipaddress
import re
import socket
import stat
from types import MappingProxyType

BASE = 13600
#: In port order: the plain-HTTP Guest listener, the HTTPS application, then
#: every other listener. None is a port whose listener was removed (13612 to
#: 13614 were Alertmanager's, ntfy's and the notification adapter's): it
#: stays unused, so that no listener after it moves.
NAMES = (
    "guest_http", "access", "http", "https", "karaf", "lsp", "helper", "prometheus", "node_exporter",
    "blackbox", "exporter", "grafana", None, None, None, "callbacks",
    "sandbox_http", "sandbox_https", "sandbox_karaf", "boot_http", "boot_https", "boot_karaf", "docs", "preview",
    "native_shutdown", "sandbox_shutdown", "boot_shutdown", "music", "music_stream",
)
DEFAULTS = MappingProxyType({name: BASE + offset for offset, name in enumerate(NAMES) if name})
ACCESS_PORT = DEFAULTS["access"]
HTTP_PORT = DEFAULTS["http"]
HTTPS_PORT = DEFAULTS["https"]
KARAF_PORT = DEFAULTS["karaf"]
SHUTDOWN_PORT = DEFAULTS["native_shutdown"]
LSP_PORT = DEFAULTS["lsp"]
HELPER_PORT = DEFAULTS["helper"]
CALLBACK_PORT = DEFAULTS["callbacks"]
DOCS_PORT = DEFAULTS["docs"]
GUEST_HTTP_PORT = DEFAULTS["guest_http"]
SANDBOX_URL = f"http://127.0.0.1:{DEFAULTS['sandbox_http']}"
NATIVE_URL = f"http://127.0.0.1:{HTTP_PORT}"
METRICS = ("prometheus", "node_exporter", "blackbox", "exporter", "grafana")
#: Music Assistant's listeners (oh_music), by host.yaml's music: key that moves each.
MUSIC = {"music": "port", "music_stream": "stream_port"}
SHELL_CFG = Path("etc/org.apache.karaf.shell.cfg")
CUSTOM_PROPERTIES = Path("etc/custom.properties")
SHUTDOWN_KEYS = frozenset(("karaf.shutdown.port", "karaf.shutdown.host"))
USERS = Path("etc/users.properties")
#: The console account the distribution ships in USERS, with a password its
#: documentation prints. A production install replaces that password
#: (configure_native(console_password=)); the sandbox and the boot check,
#: whose userdata is a developer's own, keep it (oh_sandbox.KARAF_PASSWORD).
CONSOLE_USER = "openhab"
#: The production console's password, made by the first install or upgrade
#: that finds none: root's alone (0600), beside the share logins. No service
#: reads it, and userdata holds only its digest; an administrator who wants
#: the console reads it with sudo.
CONSOLE_PASSWORD = Path("/etc/thermite/karaf-password")
_CONSOLE_SECRET = re.compile(r"[!-~]{16,256}")
_CONSOLE_LINE = re.compile(r"(\s*" + CONSOLE_USER + r"\s*[=:]\s*)[^,\r\n]*(.*)")


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


#: host.yaml's `gateway:` mapping. Its port is the HTTPS application,
#: allocated as "access"; top-level http_port/https_port are native runtime's.
GATEWAY_FIELDS = ("port", "listen")
#: What the HTTPS application binds unless gateway.listen chooses: every IPv4
#: interface, so also a guest network this host has an address on.
GATEWAY_LISTEN = ("0.0.0.0",)
#: Always bound: the installer's readiness check and the monitoring probe
#: reach the gateway here.
LOOPBACK = "127.0.0.1"


def own_address(setting: str, value) -> str:
    """One of a host's own IP addresses as a listener may bind it, in its
    canonical spelling. Whether this host has it is known only when it binds."""
    try:
        address = ipaddress.ip_address(value) if isinstance(value, str) and "%" not in value else None
    except ValueError:
        address = None
    if (address is None or address.is_unspecified or address.is_multicast or address.version == 6
            and (address.ipv4_mapped is not None or address.is_link_local)):
        raise ValueError(f"{setting}: {value!r} is not one of this host's own IP addresses "
                         "(never a wildcard, multicast, link-local IPv6 or IPv4-mapped address)")
    return str(address)


def gateway_listen(host: dict | None = None) -> list[str]:
    """The addresses the HTTPS application binds: GATEWAY_LISTEN, or
    host.yaml's gateway.listen with LOOPBACK first."""
    gateway = (host or {}).get("gateway") or {}
    if not isinstance(gateway, dict) or "listen" not in gateway:
        return list(GATEWAY_LISTEN)
    chosen = gateway["listen"]
    if not isinstance(chosen, list) or not chosen:
        raise ValueError("gateway.listen must be a non-empty list of this host's own IP addresses; leave it out to "
                         "listen on every IPv4 interface")
    return list(dict.fromkeys([LOOPBACK, *(own_address("gateway.listen", value) for value in chosen)]))


def effective(host: dict | None = None) -> dict[str, int]:
    """Validate production overrides against each other and the remaining listeners.

    Every listener keeps its default unless host.yaml moves it, so a default
    a moved listener frees (such as 13601 after `gateway: {port: ...}`) may
    be taken by another override, such as the Guest port.
    """
    host = host or {}
    result = dict(DEFAULTS)
    gateway = host.get("gateway") or {}
    if not isinstance(gateway, dict):
        raise ValueError("gateway: must be a mapping")
    unknown = sorted(str(key) for key in gateway if key not in GATEWAY_FIELDS)
    if unknown:
        raise ValueError("unknown gateway setting " + ", ".join(unknown) + " (allowed: " + ", ".join(GATEWAY_FIELDS) + ")")
    result["access"] = checked("gateway.port", gateway.get("port", result["access"]))
    gateway_listen(host)
    for name in ("http", "https"):
        result[name] = host.get(name + "_port", result[name])
    metrics = host.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("metrics: must be a mapping")
    for name in METRICS:
        result[name] = metrics.get(name + "_port", result[name])
    music = host.get("music") or {}
    if not isinstance(music, dict):
        raise ValueError("music: must be a mapping")
    for name, key in MUSIC.items():
        result[name] = music.get(key, result[name])
    guest = host.get("guest_http") or {}
    if not isinstance(guest, dict):
        raise ValueError("guest_http: must be a mapping")
    result["guest_http"] = guest.get("port", result["guest_http"])
    return unique(result)


def gateway_port(host: dict | None = None) -> int:
    """The HTTPS application's port: host.yaml's gateway.port, else ACCESS_PORT."""
    return effective(host)["access"]


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


def console_digest(password: str) -> str:
    """A password as Karaf itself keeps it in USERS (the distribution's
    etc/org.apache.karaf.jaas.cfg: SHA-256, hexadecimal, between {CRYPT}
    marks), so that Karaf need not rewrite the file and userdata never holds
    the password."""
    return "{CRYPT}" + hashlib.sha256(password.encode("ascii")).hexdigest().upper() + "{CRYPT}"


def console_users(text: str, password: str) -> str:
    """USERS with the console account's password replaced; its groups and
    every other line stay as they are."""
    stored = console_digest(password)
    return "\n".join(f"{match[1]}{stored}{match[2]}" if (match := _CONSOLE_LINE.fullmatch(line)) else line
                     for line in text.split("\n"))


#: Native configuration files are small; anything larger is refused unread.
MAXIMUM_CONFIG = 1024 * 1024
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_directory(path: Path, *, anchored: bool = True) -> int:
    """Open a directory without following a symbolic link.

    Anchored opening walks every component from the filesystem root, as root
    helpers must: the service account can rename or replace entries below its
    state. Unanchored opening trusts the caller's own ancestors (a developer
    sandbox may live below a symlinked home) and refuses only a final link.
    """
    path = Path(os.path.abspath(path))
    if not anchored:
        return os.open(path, _DIRECTORY)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def native_etc(userdata: Path, *, anchored: bool = True):
    """A descriptor for userdata/etc, never reached through a symbolic link."""
    parent = open_directory(userdata, anchored=anchored)
    try:
        directory = os.open(SHELL_CFG.parent.name, _DIRECTORY, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        yield directory
    finally:
        os.close(directory)


def read_regular(directory: int, name: str, limit: int = MAXIMUM_CONFIG) -> tuple[bytes, os.stat_result]:
    """Read one single-link regular file of an anchored directory.

    O_NOFOLLOW refuses a planted symbolic link; the link count refuses a hard
    link to another file, such as one outside this service's state.
    """
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"{name} must be a single-link regular file")
        chunks, size = [], 0
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError(f"{name} exceeds {limit} bytes")
        return b"".join(chunks), info
    finally:
        os.close(descriptor)


def unchanged(previous: os.stat_result, current: os.stat_result) -> bool:
    """Whether a name still holds the inode read earlier. The descriptor is
    closed by then, and ext4 hands a freed inode number straight to the next
    file, so type, link count and change time must match as well."""
    return (os.path.samestat(previous, current) and stat.S_IFMT(previous.st_mode) == stat.S_IFMT(current.st_mode)
            and previous.st_nlink == current.st_nlink and previous.st_ctime_ns == current.st_ctime_ns)


def replace_regular(directory: int, name: str, data: bytes, previous: os.stat_result, *,
                    uid: int | None = None, gid: int | None = None) -> None:
    """Atomically replace a file read by read_regular() in the same directory.

    The new inode is created exclusively beside it and renamed over the name,
    so no link planted there is written through. Ownership is applied to that
    inode by descriptor: the previous owner unless uid/gid are given. Its
    permission bits are kept, without set-ID bits.
    """
    if not unchanged(previous, os.stat(name, dir_fd=directory, follow_symlinks=False)):
        raise ValueError(f"{name} changed while it was being configured")
    temporary = f".{name}.{os.urandom(8).hex()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
    try:
        try:
            owner = (previous.st_uid if uid is None else uid, previous.st_gid if gid is None else gid)
            created = os.fstat(descriptor)
            if (created.st_uid, created.st_gid) != owner:
                os.fchown(descriptor, *owner)
            os.fchmod(descriptor, stat.S_IMODE(previous.st_mode) & 0o777)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise
    os.fsync(directory)


def console_secret(path: Path) -> str:
    """The console password kept in `path`, made there when there is none.

    The file is the caller's alone, root's in production, in a directory no
    one else writes. One that exists is never replaced: a rerun, a later
    upgrade and a rollback to another commit of this release keep it.
    """
    path = Path(os.path.abspath(path))
    parent = open_directory(path.parent.parent)
    try:
        try:
            directory = os.open(path.parent.name, _DIRECTORY, dir_fd=parent)
        except FileNotFoundError:
            os.mkdir(path.parent.name, 0o755, dir_fd=parent)
            directory = os.open(path.parent.name, _DIRECTORY, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        held = os.fstat(directory)
        if held.st_uid != os.geteuid() or held.st_mode & 0o022:
            raise ValueError(f"{path.parent} must be the administrator's and writable by no one else")
        try:
            data, info = read_regular(directory, path.name, 1024)
        except FileNotFoundError:
            data = os.urandom(24).hex().encode("ascii")
            temporary = f".{path.name}.{os.urandom(8).hex()}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=directory)
            try:
                try:
                    os.fchmod(descriptor, 0o600)
                    os.write(descriptor, data + b"\n")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.rename(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                raise
            os.fsync(directory)
        else:
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError(f"{path} must be the administrator's alone (0600)")
    finally:
        os.close(directory)
    text = data.decode("ascii", "replace").strip()
    if not _CONSOLE_SECRET.fullmatch(text):
        raise ValueError(f"{path} holds no console password; remove it and rerun for a new one")
    return text


def configure_shutdown(userdata: Path, port: int, *, anchored: bool = False) -> None:
    with native_etc(userdata, anchored=anchored) as etc:
        data, info = read_regular(etc, CUSTOM_PROPERTIES.name)
        replace_regular(etc, CUSTOM_PROPERTIES.name,
                        shutdown_properties(data.decode("utf-8"), port).encode("utf-8"), info)


def configure_native(userdata: Path, *, anchored: bool = False, uid: int | None = None,
                     gid: int | None = None, console_password: Path | None = None) -> None:
    """Pin native console/shutdown sockets without changing other Karaf settings.

    With console_password, the file a production install keeps it in
    (CONSOLE_PASSWORD), the console account gets that password in place of
    the distribution's, on every run: a userdata that was initialized again,
    or restored from before the password existed, gets it again.

    Every file is read and validated before any is replaced. Root callers
    pass anchored=True and the service identity that should own the files.
    """
    with native_etc(userdata, anchored=anchored) as etc:
        console, console_info = read_regular(etc, SHELL_CFG.name)
        custom, custom_info = read_regular(etc, CUSTOM_PROPERTIES.name)
        users = accounts = users_info = None
        if console_password is not None:
            # Made even when no account takes it: the installed manifest names the file.
            secret = console_secret(console_password)
            try:
                users, users_info = read_regular(etc, USERS.name)
            except FileNotFoundError:
                pass  # No account file: nothing holds the distribution's password.
            else:
                accounts = console_users(users.decode("utf-8"), secret).encode("utf-8")
        shutdown = shutdown_properties(custom.decode("utf-8"), SHUTDOWN_PORT)
        lines = [line for line in console.decode("utf-8").splitlines()
                 if not re.match(r"\s*(sshPort|sshHost)\s*=", line)]
        text = "\n".join([*lines, f"sshPort = {KARAF_PORT}", "sshHost = 127.0.0.1", ""])
        replace_regular(etc, SHELL_CFG.name, text.encode("utf-8"), console_info, uid=uid, gid=gid)
        replace_regular(etc, CUSTOM_PROPERTIES.name, shutdown.encode("utf-8"), custom_info, uid=uid, gid=gid)
        if accounts != users:
            replace_regular(etc, USERS.name, accounts, users_info, uid=uid, gid=gid)


@contextmanager
def _native_stopped(userdata: Path):
    """Hold this pinned Karaf instance's existing lock while changing its files.

    The distribution sets karaf.lock.dir=${karaf.data}/tmp. Karaf's
    SimpleFileLock uses a POSIX record lock on its 'lock' file on Linux.
    Missing first-install lock paths are not created here; the caller still
    stops the instance's service and checks its destination listeners.
    """
    directory = open_directory(userdata)
    descriptor = None
    try:
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
    parser.add_argument("--console-password", type=Path, metavar="FILE",
                        help="also give the console account the password kept in FILE, the administrator's "
                             "alone, made there when missing")
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
    owner = {"uid": args.uid, "gid": args.gid} if args.uid is not None and args.gid is not None else {}
    if args.console_password is not None:
        owner["console_password"] = args.console_password
    try:
        # Root runs this inside service-writable state: never follow a link.
        with _native_stopped(args.userdata):
            configure_native(args.userdata, anchored=True, **owner)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"could not configure the initialized native listeners and console: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
