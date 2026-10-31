"""The host as the install sees it: read-only probes, the owner at the
terminal, and running the commands the owner agreed to. The tests replace
System with a fake host."""

from __future__ import annotations

import dataclasses
import getpass
import grp
import os
import platform
import pwd
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import oh_cli
import oh_deploy as deploy


@dataclasses.dataclass(frozen=True)
class Group:
    name: str
    gid: int
    members: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class User:
    name: str
    uid: int
    gid: int


@dataclasses.dataclass(frozen=True)
class Stat:
    uid: int
    gid: int
    mode: int
    is_dir: bool
    mtime: float


@dataclasses.dataclass(frozen=True)
class Unit:
    """A systemd unit's ActiveState and UnitFileState, and when it last
    became active (Unix time), if it ever did."""

    active: str
    enabled: str
    since: float | None = None


class System:
    def hostname(self) -> str:
        return deploy.hostname()

    def address(self) -> str | None:
        return deploy.lan_address()

    def machine(self) -> str:
        return platform.machine()

    def kernel(self) -> str:
        return platform.release()

    def overlayfs(self) -> bool:
        """Whether the running kernel has overlayfs or can still load it.
        A kernel upgraded since the last boot has lost its modules (pacman
        deletes them), and then Podman cannot set up its storage."""
        try:
            if "overlay" in Path("/proc/filesystems").read_text(encoding="utf-8").split():
                return True
        except OSError:
            pass
        return any(Path(root, self.kernel()).is_dir() for root in ("/usr/lib/modules", "/lib/modules"))

    def login(self) -> str:
        return pwd.getpwuid(os.getuid()).pw_name

    def process_groups(self) -> set[int]:
        return {os.getegid(), *os.getgroups()}

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def capture(self, argv: Sequence[str]) -> tuple[int, str]:
        """Exit status and output of a read-only command; 127 if it is missing."""
        oh_cli.trace_run(argv)
        try:
            out = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return 127, ""
        return out.returncode, out.stdout

    def group(self, key: str | int) -> Group | None:
        try:
            entry = grp.getgrgid(key) if isinstance(key, int) else grp.getgrnam(key)
        except KeyError:
            return None
        return Group(entry.gr_name, entry.gr_gid, tuple(entry.gr_mem))

    def user(self, key: str | int) -> User | None:
        try:
            entry = pwd.getpwuid(key) if isinstance(key, int) else pwd.getpwnam(key)
        except KeyError:
            return None
        return User(entry.pw_name, entry.pw_uid, entry.pw_gid)

    def stat(self, path: Path) -> Stat | None:
        """None when path is missing, or below a directory this user cannot
        search (one of the openhab group's, before a login puts them in it)."""
        try:
            found = os.stat(path)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return None
        return Stat(found.st_uid, found.st_gid, found.st_mode & 0o7777, os.path.isdir(path), found.st_mtime)

    def listdir(self, path: Path) -> list[str]:
        """The names in a directory; none when it is missing or unreadable."""
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def read_bytes(self, path: Path) -> bytes | None:
        """None when path is missing or this user cannot read it."""
        try:
            return Path(path).read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
            return None

    def fstype(self, path: Path) -> str | None:
        """The file system type of path, or of its nearest existing parent."""
        path = Path(path)
        while not path.exists():
            path = path.parent
        code, out = self.capture(["findmnt", "-no", "FSTYPE", "--target", str(path)])
        return (out.strip() or None) if code == 0 else None

    def port_free(self, port: int) -> bool:
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                return False
        return True

    def unit(self, name: str) -> Unit:
        code, out = self.capture(["systemctl", "show", name,
                                  "--property=ActiveState,UnitFileState,ActiveEnterTimestampMonotonic"])
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if code == 0 else {}
        monotonic = int(values.get("ActiveEnterTimestampMonotonic") or 0)
        # systemd counts on CLOCK_MONOTONIC, as time.monotonic() does.
        since = time.time() - time.monotonic() + monotonic / 1e6 if monotonic else None
        return Unit(values.get("ActiveState", "unknown"), values.get("UnitFileState", ""), since)

    def openhab_version(self, url: str) -> str | None:
        return deploy.openhab_version(url)

    def token_status(self, url: str, token: str) -> int | None:
        return deploy.token_status(url, token)

    def metrics_health(self, host: dict, address: str | None) -> deploy.MetricsHealth:
        return deploy.metrics_health(host, address)

    def interactive(self) -> bool:
        return sys.stdin.isatty()

    def ask(self, prompt: str) -> str:
        try:
            if oh_cli.switches.quiet:
                # Under -q what say() printed goes nowhere, yet the owner must
                # see what a question is about.
                sys.stderr.write(f"{getattr(self, 'said', '')}\n{prompt}")
                sys.stderr.flush()
                return input()
            return input(prompt)
        except EOFError:
            return ""

    def secret(self, prompt: str) -> str:
        try:
            return getpass.getpass(prompt)
        except EOFError:
            return ""

    def say(self, text: str) -> None:
        self.said = text
        print(text, flush=True)

    def run(self, argv: Sequence[str], cwd: Path) -> int:
        oh_cli.trace_run(argv, cwd)
        try:
            return subprocess.run(list(argv), cwd=cwd, check=False).returncode
        except FileNotFoundError:
            self.say(f"   {argv[0]}: no such program on this host")
            return 127

    def write_private(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    def clock(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
