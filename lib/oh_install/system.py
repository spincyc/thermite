"""The host as the install sees it: read-only probes, the owner at the
terminal, and running the commands the owner agreed to. The tests replace
System with a fake host."""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import grp
import http.client
import json
import os
import platform
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
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
    home: str | None = None
    shell: str | None = None


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
    #: Stable systemd active-enter monotonic timestamp, in microseconds.
    generation: int | None = None


@dataclasses.dataclass(frozen=True)
class NativeProbe:
    status: int
    value: object = None
    problem: str | None = None


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

    def capture(self, argv: Sequence[str], *, timeout: float | None = None) -> tuple[int, str]:
        """Exit status and output of a read-only command; 127 if it is missing."""
        oh_cli.trace_run(argv)
        try:
            out = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
        except FileNotFoundError:
            return 127, ""
        except subprocess.TimeoutExpired:
            return 124, ""
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
        return User(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir, entry.pw_shell)

    def stat(self, path: Path) -> Stat | None:
        """None when path is missing, or below a directory this user cannot
        search (one of the thermite group's, before a login puts them in it)."""
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
                # Match the servers' restart semantics: old closed connections
                # in TIME_WAIT are reusable, while a live listener still blocks.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
            except OSError:
                return False
        return True

    def unit(self, name: str, *, timeout: float | None = None) -> Unit:
        argv = ["systemctl", "show", name, "--property=ActiveState,UnitFileState,ActiveEnterTimestampMonotonic"]
        code, out = self.capture(argv, timeout=timeout) if timeout is not None else self.capture(argv)
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if code == 0 else {}
        monotonic = int(values.get("ActiveEnterTimestampMonotonic") or 0)
        # systemd counts on CLOCK_MONOTONIC, as time.monotonic() does.
        since = time.time() - time.monotonic() + monotonic / 1e6 if monotonic else None
        return Unit(values.get("ActiveState", "unknown"), values.get("UnitFileState", ""), since, monotonic or None)

    def openhab_version(self, url: str) -> str | None:
        return deploy.openhab_version(url)

    def token_status(self, url: str, token: str) -> int | None:
        return deploy.token_status(url, token)

    def native_probe(self, url: str, token: str, path: str, deadline: float, *, json_body: bool = True) -> NativeProbe:
        """One authenticated read, bounded through headers/body by the startup deadline."""
        from oh_testing.boot import ReportError, ReportHttp

        remaining = deadline - self.clock()
        if remaining <= 0:
            return NativeProbe(0, problem="startup deadline expired")
        # Reuse the report reader's per-receive deadline enforcement, while
        # retaining status codes needed to distinguish auth and metrics readiness.
        client = ReportHttp(url, token, deadline)
        client.timeout = min(5, remaining)
        try:
            status, body = client._request("GET", path,
                                           headers={"Accept": "application/json" if json_body else "text/plain"})
            client.remaining(path)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, b""
            exc.close()
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            return NativeProbe(0)
        except ReportError:
            return NativeProbe(0, problem="startup deadline expired")
        if self.clock() >= deadline:
            return NativeProbe(0, problem="startup deadline expired")
        if status != 200 or not json_body:
            return NativeProbe(status)
        try:
            return NativeProbe(status, json.loads(body.decode("utf-8")))
        except (ValueError, UnicodeError, RecursionError):
            return NativeProbe(status, problem="malformed JSON")

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

    def run(self, argv: Sequence[str], cwd: Path, *, timeout: float | None = None) -> int:
        oh_cli.trace_run(argv, cwd)
        try:
            return subprocess.run(list(argv), cwd=cwd, check=False, timeout=timeout).returncode
        except FileNotFoundError:
            self.say(f"   {argv[0]}: no such program on this host")
            return 127
        except subprocess.TimeoutExpired:
            self.say("   command exceeded the remaining startup allowance; inspect the native service before retrying")
            return 124

    @contextlib.contextmanager
    def _private_directory(self, path: Path):
        """Anchor an owned credential directory; narrow only that final inode."""
        path = Path(path).expanduser()
        if ".." in path.parts:
            raise ValueError("private credential paths must not contain parent traversal")
        path = Path(os.path.abspath(path))
        if path == Path("/"):
            raise ValueError("the filesystem root is not a private credential directory")
        flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        try:
            for part in path.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise ValueError("private credential directory must belong to this operator")
            child = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid():
                raise ValueError("private credential directory custody changed")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.fchmod(descriptor, 0o700)
            yield descriptor
        finally:
            os.close(descriptor)

    def private_directory(self, path: Path) -> None:
        with self._private_directory(path):
            pass

    def write_private(self, path: Path, text: str) -> None:
        path = Path(path)
        if not path.name or ".." in path.parts:
            raise ValueError("invalid private credential path")
        with self._private_directory(path.parent) as parent, contextlib.ExitStack() as stack:
            previous = None
            try:
                existing = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                stack.callback(os.close, existing)
                previous = os.fstat(existing)
                if (not stat.S_ISREG(previous.st_mode) or previous.st_uid != os.geteuid()
                        or previous.st_nlink != 1 or stat.S_IMODE(previous.st_mode) != 0o600):
                    raise ValueError("existing credential must be owned, private, regular and without hard links")
            temporary = "." + path.name + "." + os.urandom(12).hex()
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                if previous is None:
                    os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                else:
                    current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if not os.path.samestat(previous, current):
                        raise ValueError("credential changed during private write")
                    os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.fsync(parent)

    def clock(self) -> float:
        return time.monotonic()

    def wall_time(self) -> float:
        """Unix time, for comparison with a unit's active-enter timestamp."""
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
