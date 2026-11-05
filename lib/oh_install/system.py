"""The host as the install sees it: read-only probes, the owner at the
terminal, and running the commands the owner agreed to. The tests replace
System with a fake host."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
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
import urllib.request
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path

import oh_cli
import oh_deploy as deploy
import oh_http
from oh_install import progress

#: Whether the owner approved this run up front (thermite install --yes),
#: as a system unit with no terminal does: the run's one approval is given
#: (runner.confirm_run) and covers its root commands, nothing reads standard
#: input, and any other question is answered no.
UNATTENDED = ContextVar("thermite_unattended", default=False)


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
    size: int | None = None


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
class UnitOrigin:
    source: str = ""
    fragment: str = ""
    drop_ins: str = ""
    known: bool = True
    load_state: str = "loaded"
    active: str = "inactive"
    enabled: str = ""
    transient: bool | None = False


@dataclasses.dataclass(frozen=True)
class UnitFile:
    content: bytes
    uid: int
    mode: int
    ancestors_trusted: bool = True
    gid: int = 0


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

    def capture(self, argv: Sequence[str], *, timeout: float | None = None,
                diagnostics: bool = False) -> tuple[int, str]:
        """Exit status and stdout; 127 if missing, 124 on timeout.

        Opt in only when the command's stderr is safe to display. Failed
        diagnostic captures report bounded stderr, never stdout or arguments.
        """
        executable = Path(str(argv[0])).name if argv else "command"
        executable = "".join(char if char.isascii() and (char.isalnum() or char in "._+-") else "?"
                             for char in executable[:64]) or "command"
        oh_cli.trace_run((executable,) if diagnostics else argv)
        try:
            out = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
        except FileNotFoundError:
            if diagnostics:
                self._capture_diagnostic(executable, 127, reason="program not found")
            return 127, ""
        except subprocess.TimeoutExpired as exc:
            if diagnostics:
                self._capture_diagnostic(executable, 124, exc.stderr, reason="timed out")
            return 124, ""
        if diagnostics and out.returncode:
            self._capture_diagnostic(executable, out.returncode, out.stderr)
        return out.returncode, out.stdout

    def _capture_diagnostic(self, executable: str, status: int, stderr: str | bytes | None = None, *,
                            reason: str | None = None) -> None:
        from oh_install import output

        heading = f"   {executable}: exit status {status}" + (f" ({reason})" if reason else "")
        data = stderr.encode("utf-8", errors="replace") if isinstance(stderr, str) else stderr or b""
        text = output._safe_text(data[-output.TAIL_BYTES:]).strip()
        omitted = "[earlier stderr omitted]\n" if len(data) > output.TAIL_BYTES else ""
        self.say(heading + ("\n" + omitted + text if omitted or text else ""))

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
        return Stat(found.st_uid, found.st_gid, found.st_mode & 0o7777, os.path.isdir(path), found.st_mtime, found.st_size)

    def listdir(self, path: Path) -> list[str]:
        """The names in a directory; none when it is missing or unreadable."""
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def directory_entries(self, path: Path) -> list[str] | None:
        """Unlike listdir, do not mistake an unreadable directory for empty."""
        try:
            return sorted(os.listdir(path))
        except OSError:
            return None

    def read_bytes(self, path: Path) -> bytes | None:
        """None when path is missing or this user cannot read it."""
        try:
            return Path(path).read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
            return None

    def readlink(self, path: Path) -> str | None:
        """A symlink's target; None when path is not a symlink."""
        try:
            return os.readlink(path)
        except OSError:
            return None

    def lexists(self, path: Path) -> bool:
        try:
            os.lstat(path)
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            # An inaccessible lookup is never permission to overwrite it.
            return True

    def unit_file(self, path: Path) -> UnitFile | None:
        """Read a regular unit through non-symlink ancestors, without root.

        Ownership decisions must not follow a replacement symlink, including
        one in an ancestor. The administrative install/remove follows only
        after this conservative, read-only check.
        """
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            return None
        try:
            with contextlib.ExitStack() as stack:
                flags = os.O_RDONLY | os.O_NOFOLLOW
                directory_flags = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY
                parent = os.open("/", directory_flags)
                stack.callback(os.close, parent)
                info = os.fstat(parent)
                trusted = info.st_uid == 0 and not info.st_mode & 0o022
                for part in path.parts[1:-1]:
                    parent = os.open(part, directory_flags, dir_fd=parent)
                    stack.callback(os.close, parent)
                    info = os.fstat(parent)
                    trusted = trusted and info.st_uid == 0 and not info.st_mode & 0o022
                descriptor = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent)
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
                    return None
                with os.fdopen(os.dup(descriptor), "rb") as source:
                    content = source.read(256 * 1024 + 1)
                return UnitFile(content, info.st_uid, stat.S_IMODE(info.st_mode), bool(trusted), info.st_gid) if len(content) <= 256 * 1024 else None
        except OSError:
            return None

    def safe_directory(self, path: Path) -> bool:
        """Whether an existing directory has only real directory ancestors."""
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            return False
        try:
            with contextlib.ExitStack() as stack:
                flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
                parent = os.open("/", flags)
                stack.callback(os.close, parent)
                for part in path.parts[1:]:
                    parent = os.open(part, flags, dir_fd=parent)
                    stack.callback(os.close, parent)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def safe_unit_parent(self, path: Path) -> bool:
        """Existing unit ancestors must be root-owned real, unwritable dirs."""
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            return False
        try:
            with contextlib.ExitStack() as stack:
                flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
                parent = os.open("/", flags)
                stack.callback(os.close, parent)
                for part in (None, *path.parts[1:]):
                    if part is not None:
                        parent = os.open(part, flags, dir_fd=parent)
                        stack.callback(os.close, parent)
                    info = os.fstat(parent)
                    if info.st_uid != 0 or info.st_mode & 0o022:
                        return False
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def unit_origin(self, name: str) -> UnitOrigin:
        return self.unit_origins((name,))[name]

    def unit_origins(self, names: Sequence[str]) -> dict[str, UnitOrigin]:
        """One bounded, nonsecret systemd snapshot; missing/error is not inactive."""
        result = {name: UnitOrigin(known=False, load_state="", active="unknown") for name in names}
        if not names:
            return result
        code, out = self.capture(["systemctl", "show", *names,
                                  "--property=Id,SourcePath,FragmentPath,DropInPaths,LoadState,ActiveState,UnitFileState,Transient"], timeout=10)
        if code:
            return result
        for block in out.strip().split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            name = values.get("Id")
            if name not in result:
                continue
            known = (values.get("LoadState") in ("loaded", "not-found", "masked")
                     and values.get("ActiveState") in ("active", "inactive", "failed", "activating", "reloading", "deactivating")
                     and values.get('Transient') in ('yes', 'no'))
            result[name] = UnitOrigin(values.get("SourcePath", ""), values.get("FragmentPath", ""), values.get("DropInPaths", ""),
                                      known, values.get("LoadState", ""), values.get("ActiveState", "unknown"), values.get("UnitFileState", ""),
                                      {'yes': True, 'no': False}.get(values.get('Transient')))
        return result

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
        if not isinstance(token, str) or any(ord(c) < 33 or ord(c) > 126 for c in token):
            return NativeProbe(0, problem="invalid runtime credential")
        def remaining():
            value = deadline - self.clock()
            if value <= 0:
                raise oh_http.DeadlineExpired("startup deadline expired")
            return value
        try:
            timeout = min(5, remaining())
            request = urllib.request.Request(url.rstrip("/") + path, method="GET", headers={
                "Accept": "application/json" if json_body else "text/plain", "Authorization": f"Bearer {token}"})
            oh_cli.trace_http("GET", url.rstrip("/") + path)
            status, body = oh_http.request(request, timeout=timeout, remaining=remaining)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, b""
            exc.close()
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            return NativeProbe(0)
        except oh_http.DeadlineExpired:
            return NativeProbe(0, problem="startup deadline expired")
        if self.clock() >= deadline:
            return NativeProbe(0, problem="startup deadline expired")
        if status != 200 or not json_body:
            return NativeProbe(status)
        try:
            value = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return NativeProbe(status, problem="malformed JSON")
        if self.clock() >= deadline:
            return NativeProbe(0, problem="startup deadline expired")
        return NativeProbe(status, value)

    def metrics_health(self, host: dict, address: str | None) -> deploy.MetricsHealth:
        return deploy.metrics_health(host, address)

    def interactive(self) -> bool:
        return not UNATTENDED.get() and sys.stdin.isatty()

    def ask(self, prompt: str) -> str:
        if UNATTENDED.get():
            self.say(f"{prompt.strip()} no: --yes answers only the run's approval")
            return ""
        try:
            with progress.paused():
                if oh_cli.switches.quiet:
                    active = progress.current.get()
                    if active:
                        active.prompt_context()
                    else:
                        print(getattr(self, "said", ""), file=sys.stderr)
                    sys.stderr.write(prompt)
                    sys.stderr.flush()
                    return input()
                return input(prompt)
        except EOFError:
            return ""

    def secret(self, prompt: str) -> str:
        if UNATTENDED.get():
            self.say(f"{prompt.strip()} not asked: --yes reads nothing")
            return ""
        try:
            with progress.paused():
                if oh_cli.switches.quiet and (active := progress.current.get()):
                    active.prompt_context()
                return getpass.getpass(prompt)
        except EOFError:
            return ""

    def say(self, text: str) -> None:
        self.said = text
        with progress.paused():
            if oh_cli.switches.quiet and (active := progress.current.get()):
                active.notes.append(text)
                return
            print(text, flush=True)

    def run(self, argv: Sequence[str], cwd: Path, *, timeout: float | None = None) -> int:
        oh_cli.trace_run(argv, cwd)
        try:
            if progress.capture_command.get():
                from oh_install import output
                active = progress.current.get()
                privileged = bool(argv and argv[0] == "sudo")
                # A command already under sudo -n (--yes) never asks for a
                # password, so it is not authenticated first: that would.
                if privileged and tuple(argv[1:2]) != ("-n",):
                    # Authentication owns the terminal only while it may ask
                    # for a password. The actual command cannot prompt or be
                    # retried after a possibly partial mutation.
                    deadline = self.clock() + timeout if timeout is not None else None
                    with progress.command(terminal=True):
                        code = self.run(("sudo", "-v"), cwd, timeout=timeout)
                    if code:
                        self.say("   sudo authentication failed; no command was run")
                        return code
                    if deadline is not None:
                        timeout = deadline - self.clock()
                        if timeout <= 0:
                            self.say("   startup allowance expired during sudo authentication; no command was run")
                            return 124
                    argv = ("sudo", "-n", *argv[1:])
                return output.run(argv, cwd, timeout=timeout, keep_terminal=privileged,
                                  show_output=progress.show_command_output.get() and not oh_cli.switches.quiet,
                                  report=lambda text: active.message(text, error=True))
            with contextlib.ExitStack() as stack:
                options = {}
                if oh_cli.switches.quiet and progress.terminal_command.get():
                    # CLI quiet mode discards fd 1 process-wide. A package
                    # manager may prompt on stdout: give interactive children
                    # their terminal back instead of hiding that question.
                    try:
                        options["stdout"] = stack.enter_context(open("/dev/tty", "w"))
                    except OSError:
                        options["stdout"] = sys.stderr
                return subprocess.run(list(argv), cwd=cwd, check=False, timeout=timeout, **options).returncode
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

    @staticmethod
    def _read_private_at(parent: int, name: str) -> str:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                raise ValueError("credential must be owned, private, bounded and regular")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                value = stream.read(65537)
            if len(value) > 65536:
                raise ValueError("credential exceeds the supported size")
            return value.decode("utf-8")
        finally:
            os.close(descriptor)

    def read_private(self, path: Path) -> str:
        """Read an explicit operator credential without creating or repairing paths."""
        path = Path(path).absolute()
        if ".." in path.parts:
            raise ValueError("invalid private credential path")
        with contextlib.ExitStack() as stack:
            flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open("/", flags)
            stack.callback(os.close, parent)
            for part in path.parts[1:-1]:
                parent = os.open(part, flags, dir_fd=parent)
                stack.callback(os.close, parent)
            return self._read_private_at(parent, path.name)

    def configure_runtime_access(self, url: str, credentials: Path, token: Path, deadline: float) -> str:
        """Serialize resumable setup; credential bytes never enter command output."""
        from oh_install import native_auth
        with self._runtime_access_store(credentials, token) as (load, save, publish):
            return native_auth.provision(url, load=load, save=save, publish=publish,
                                         deadline=deadline, clock=self.clock)

    def import_runtime_access(self, credentials: Path, token: Path, value: str) -> None:
        """An explicit import supersedes automatic recovery of a different token."""
        with self._runtime_access_store(credentials, token) as (load, save, publish):
            record = load()
            if record is not None:
                if not isinstance(record, dict):
                    raise ValueError("invalid managed credential record")
                if record.get("token") != value:
                    save({**record, "phase": "retired"})
            publish(value)

    @contextlib.contextmanager
    def _runtime_access_store(self, credentials: Path, token: Path):
        credentials, token = Path(credentials), Path(token)
        if credentials.parent != token.parent or credentials.name != "runtime-credentials.json" or token.name != "api-token":
            raise ValueError("runtime credentials must use the operator's fixed private paths")
        with self._private_directory(credentials.parent) as parent:
            lock = os.open(".runtime-access.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=parent)
            try:
                info = os.fstat(lock)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError("runtime setup lock has unsafe custody")
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def read(name):
                    try:
                        return self._read_private_at(parent, name)
                    except FileNotFoundError:
                        return None
                before = read(token.name)
                def load():
                    raw = read(credentials.name)
                    def unique(pairs):
                        value = {}
                        for key, item in pairs:
                            if key in value:
                                raise ValueError("duplicate credential record field")
                            value[key] = item
                        return value
                    return json.loads(raw, object_pairs_hook=unique) if raw is not None else None
                def save(value):
                    self._write_private_at(parent, credentials.name, json.dumps(value, sort_keys=True) + "\n")
                def publish(value):
                    if read(token.name) != before:
                        raise ValueError("runtime access changed during setup; rerun to verify it")
                    self._write_private_at(parent, token.name, value + "\n")
                yield load, save, publish
            finally:
                os.close(lock)

    def write_private(self, path: Path, text: str) -> None:
        path = Path(path)
        if not path.name or ".." in path.parts:
            raise ValueError("invalid private credential path")
        with self._private_directory(path.parent) as parent:
            self._write_private_at(parent, path.name, text)

    @staticmethod
    def _write_private_at(parent: int, name: str, text: str) -> None:
        with contextlib.ExitStack() as stack:
            previous = None
            try:
                existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                stack.callback(os.close, existing)
                previous = os.fstat(existing)
                if (not stat.S_ISREG(previous.st_mode) or previous.st_uid != os.geteuid()
                        or previous.st_nlink != 1 or stat.S_IMODE(previous.st_mode) != 0o600):
                    raise ValueError("existing credential must be owned, private, regular and without hard links")
            temporary = "." + name + "." + os.urandom(12).hex()
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                if previous is None:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                else:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not os.path.samestat(previous, current):
                        raise ValueError("credential changed during private write")
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
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
