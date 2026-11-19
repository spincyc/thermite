"""Network shares: SMB/CIFS or NFS directories a host mounts with Thermite,
for Music Assistant's libraries or anything else that reads them.

instance/host.yaml declares each by name:

    shares:
      music:
        type: cifs
        server: nas.example
        share: music
        where: /mnt/nas/music

Each becomes a native mount unit (oh_deploy.render_share_mount), part of
thermite.target and installed, rolled back and removed with Thermite's other
units. A share on a named server is mounted only once that name resolves:
thermite-share-gate@<server>.service waits for it, a bounded GATE_TIMEOUT
after network-online.target, since a LAN's DNS may answer later than that.
A server given as an IPv4 address needs no gate. Every mount is nofail with
a bounded MOUNT_TIMEOUT, read-only unless the share is `writable`, nosuid
and nodev. An SMB share mounts with the login in the owner's private copy
(var/<host>/share-<name>.credentials), which the install copies, root's
alone, to CREDENTIALS/share-<name>.credentials; NFS needs none.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
from pathlib import PurePosixPath

#: A share's keys in host.yaml.
KEYS = frozenset(("type", "server", "share", "export", "where", "writable", "version"))
#: The file systems a share may be: its type, and the key naming what the
#: server exports.
TYPES = {"cifs": "share", "nfs": "export"}
#: A share's name: its unit's description and its credentials file's name.
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
#: Pasted into unit files: no whitespace, quotes, commas, colons or specifiers.
_PATH = re.compile(r"/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*")
_SHARE = re.compile(r"[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*")
_VERSION = re.compile(r"[0-9]{1,2}(?:\.[0-9]{1,2}){0,2}")
#: Mount points: below these, so that a share never covers the system's own
#: directories (the install engine owns mount units of these names alone).
TOPS = ("mnt", "media")
#: Where the install puts each SMB share's login, root's alone (0600).
CREDENTIALS = PurePosixPath("/etc/thermite")
#: Seconds a gate waits for its server's name after network-online.target,
#: and a mount for its server.
GATE_TIMEOUT = 60
MOUNT_TIMEOUT = 30
#: The packages that mount each type, by package manager (oh_install.steps.PACKAGES).
PACKAGES = {"pacman": {"cifs": "cifs-utils", "nfs": "nfs-utils"},
            "apt-get": {"cifs": "cifs-utils", "nfs": "nfs-common"}}
#: Each SMB share's files and directories, as the thermite account sees them.
FILE_MODE, DIR_MODE = "0640", "0750"


def escape_path(path: str) -> str:
    """`systemd-escape --path`: the name systemd requires of the mount unit
    for this mount point."""
    text = path.strip("/")
    out = []
    for index, char in enumerate(text):
        if char == "/":
            out.append("-")
        elif char.isascii() and (char.isalnum() or char in ":_.") and not (index == 0 and char == "."):
            out.append(char)
        else:
            out.append("".join(f"\\x{byte:02x}" for byte in char.encode()))
    return "".join(out) or "-"


@dataclasses.dataclass(frozen=True)
class Share:
    name: str
    type: str
    server: str
    #: The SMB share (with any directory inside it), or the NFS export.
    source: str
    where: str
    writable: bool = False
    version: str | None = None

    @property
    def what(self) -> str:
        return f"//{self.server}/{self.source}" if self.type == "cifs" else f"{self.server}:{self.source}"

    @property
    def unit(self) -> str:
        """Its mount unit's name, which systemd derives from its mount point."""
        return escape_path(self.where) + ".mount"

    @property
    def gate(self) -> str | None:
        """The unit that waits for its server's name; None for an address."""
        return None if address(self.server) else f"thermite-share-gate@{self.server}.service"

    @property
    def credentials(self) -> PurePosixPath | None:
        """Where an SMB share's login is installed; NFS has none."""
        return CREDENTIALS / f"share-{self.name}.credentials" if self.type == "cifs" else None

    def options(self, uid: int, gid: int) -> str:
        """Its mount options: read-only unless writable; a missing server
        never holds up the boot."""
        words = ["rw" if self.writable else "ro", "nosuid", "nodev", "_netdev", "nofail"]
        if self.type == "cifs":
            # The files are the thermite account's, whoever owns them on the server.
            words += [f"credentials={self.credentials}", f"uid={uid}", f"gid={gid}",
                      f"file_mode={FILE_MODE}", f"dir_mode={DIR_MODE}"]
        if self.version:
            words.append(("vers=" if self.type == "cifs" else "nfsvers=") + self.version)
        return ",".join(words)


def address(server: str) -> bool:
    """Whether a share's server is an IPv4 address, which needs no lookup."""
    try:
        return ipaddress.ip_address(server).version == 4
    except ValueError:
        return False


def settings(host: dict | None) -> dict[str, Share]:
    """host.yaml's shares:, checked, by name; {} for a host without."""
    shares = (host or {}).get("shares") or {}
    if not isinstance(shares, dict):
        raise ValueError("shares must map a name to a share")
    found: dict[str, Share] = {}
    for name, share in shares.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"shares: {name!r} is not a name of lowercase letters, digits and _ starting with a letter")
        where = f"shares.{name}"
        if not isinstance(share, dict):
            raise ValueError(f"{where} must be a mapping")
        unknown = sorted(str(key) for key in share if key not in KEYS)
        if unknown:
            raise ValueError(f"unknown {where} setting " + ", ".join(unknown) + " (allowed: " + ", ".join(sorted(KEYS)) + ")")
        kind = share.get("type")
        if kind not in TYPES:
            raise ValueError(f"{where}.type must be one of " + ", ".join(TYPES) + f", not {kind!r}")
        key, other = TYPES[kind], next(value for value in TYPES.values() if value != TYPES[kind])
        if other in share:
            raise ValueError(f"{where} is {kind}: name what the server exports as {key}, not {other}")
        server = share.get("server")
        if not isinstance(server, str) or not (address(server) or _HOST.fullmatch(server) and not _numeric(server)):
            raise ValueError(f"{where}.server must be a host name such as nas.example or an IPv4 address")
        source = share.get(key)
        if kind == "cifs" and not (isinstance(source, str) and _SHARE.fullmatch(source)
                                   and os.path.normpath("/" + source) == "/" + source):
            raise ValueError(f"{where}.share must be the share's name (and any directory inside it) of letters, "
                             "digits and ._@+-")
        if kind == "nfs" and not (isinstance(source, str) and _PATH.fullmatch(source) and os.path.normpath(source) == source):
            raise ValueError(f"{where}.export must be the exported directory, a normalized absolute path of letters, "
                             "digits and ._@+-")
        point = share.get("where")
        if (not isinstance(point, str) or not _PATH.fullmatch(point) or os.path.normpath(point) != point
                or point.split("/")[1] not in TOPS or point.count("/") < 2):
            raise ValueError(f"{where}.where must be a normalized absolute path of letters, digits and ._@+- below "
                             + " or ".join(f"/{top}" for top in TOPS))
        writable = share.get("writable", False)
        if type(writable) is not bool:
            raise ValueError(f"{where}.writable must be true or false")
        version = share.get("version")
        if version is not None and not (isinstance(version, str) and _VERSION.fullmatch(version)):
            raise ValueError(f"{where}.version must be a quoted protocol version such as "
                             + ('"3.1.1"' if kind == "cifs" else '"4.2"'))
        found[name] = Share(name, kind, server, source, point, writable, version)
    points = sorted((share.where, name) for name, share in found.items())
    for (outer, first), (inner, second) in zip(points, points[1:], strict=False):
        if inner == outer or inner.startswith(outer + "/"):
            raise ValueError(f"shares.{second}.where is {'shares.' + first + '.where' if inner == outer else 'inside shares.' + first}: "
                             "each share needs a mount point of its own")
    return found


def _numeric(server: str) -> bool:
    """A name of digits and dots alone is a malformed address, never a host."""
    return all(part.isdigit() for part in server.split("."))


def gated(host: dict | None) -> bool:
    """Whether a share waits for its server's name: the gate is installed."""
    return any(share.gate for share in settings(host).values())


def packages(host: dict | None, manager: str) -> tuple[str, ...]:
    """What mounting this host's shares needs, by `manager`'s names."""
    return tuple(sorted({PACKAGES[manager][share.type] for share in settings(host).values()}))
