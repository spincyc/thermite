"""Owner-only bounded files and atomic replacement, shared by CLI and service."""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import errno
import json
import grp
import os
from pathlib import Path
import pwd
import re
import secrets
import stat

from oh_service import ACCOUNT, GROUP, UID, GID, _open_root

from . import AccessError


def service_identity(*, required=False):
    """Resolve the fixed service account, while permitting an offline stage plan."""
    try:
        account = pwd.getpwnam(ACCOUNT)
    except KeyError:
        account = None
    try:
        group = grp.getgrnam(GROUP)
    except KeyError:
        group = None
    if account is None and group is None and not required:
        return UID, GID
    if account is None or group is None or account.pw_uid == 0 or group.gr_gid == 0 or account.pw_gid != group.gr_gid:
        raise AccessError("Create the thermite service account and matching primary group before commissioning")
    return account.pw_uid, group.gr_gid


#: Set only by a unit that runs an access service in a container: the
#: account's numbers as UID:GID, where no account database names them.
STATED_IDENTITY = "THERMITE_SERVICE_IDENTITY"


def stated_identity():
    """The service account's numbers as the unit states them, or None."""
    value = os.environ.get(STATED_IDENTITY)
    if value is None:
        return None
    match = re.fullmatch(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9})", value)
    if not match:
        raise AccessError(f"{STATED_IDENTITY} must be the service account's UID:GID, neither of them 0")
    return int(match[1]), int(match[2])


def require_service_account():
    """Refuse to run an access service as any account but the service's.

    This is a guard against a mistake (a service verb typed as the operator
    or as root would otherwise create state the service cannot use), not a
    boundary: what keeps another account out of the secrets is their owner
    and modes, which read_private and private_directory check on every use
    against the account this process really runs as.

    The account is the host's `thermite`, looked up by name. A container
    has no such name, so its unit states the numbers instead
    (STATED_IDENTITY), and they are honoured only when this process runs
    as exactly them: the variable can say which account is meant, never
    make another one pass."""
    uid, gid = stated_identity() or service_identity(required=True)
    if os.geteuid() != uid or os.getegid() != gid:
        raise AccessError("Run this access service as user thermite and group thermite after the ownership handoff")
    return uid, gid


def private_directory(value):
    path = Path(value).expanduser().absolute()
    # Ancestors need search permission, not permission to list their names.
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            created = None
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    created = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                child = os.open(part, flags, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                if created is not None:
                    if info.st_uid != os.geteuid() or not os.path.samestat(created, info):
                        raise AccessError("Private directory changed during creation")
                    # mkdir inherits SGID from a shared parent even with mode
                    # 0700. Normalize only the directory this call created.
                    writable = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=child)
                    try:
                        os.fchmod(writable, 0o700)
                    finally:
                        os.close(writable)
                current = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not os.path.samestat(current, os.fstat(child)):
                    raise AccessError("Private directory changed during creation")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AccessError("Private directory must be owned by this user and mode 0700")
        return path
    finally:
        os.close(descriptor)


class Oversized(AccessError):
    """A private file that is the service's own but past its bound: what it
    holds is wrong, not who may read it."""


def read_private(value, maximum=2 * 1024 * 1024):
    path = Path(value).expanduser().absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise AccessError("Private file must be owned, regular and mode 0600")
        if info.st_size > maximum:
            raise Oversized("Private file exceeds its limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise Oversized("Private file exceeds its limit")
        return content
    finally:
        os.close(fd)


class Unsynced(OSError):
    """atomic_private put the new contents in place, but its directory could
    not be synced: a crash may yet undo the write."""


#: Random bytes in the name of atomic_private's temporary file, written
#: beside its target as "." + the target's name + "." + their hex.
TEMPORARY_BYTES = 12


def atomic_private(value, content):
    path = Path(value).expanduser().absolute()
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path)
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(TEMPORARY_BYTES))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # The rename outlives a crash only once its directory is on disk too.
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            # Some file systems (vboxsf, some CIFS) cannot sync a directory; the file is in place.
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise Unsynced(error.errno, error.strerror) from error
    finally:
        temporary.unlink(missing_ok=True)


def remove_temporaries(value):
    """Remove the temporary files that atomic_private writes of value left
    when their process was killed midway: this user's regular files by
    those names, nothing else. For the one writer of value, before it
    writes."""
    path = Path(value).expanduser().absolute()
    left = re.compile(re.escape("." + path.name + ".") + "[0-9a-f]{%d}" % (2 * TEMPORARY_BYTES))
    try:
        entries = list(os.scandir(path.parent))
    except FileNotFoundError:
        return
    for entry in entries:
        if left.fullmatch(entry.name):
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                Path(entry.path).unlink(missing_ok=True)


def json_private(value, data):
    atomic_private(value, (json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n").encode())


def _single_name(name: str) -> str:
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"invalid entry name: {name!r}")
    return name


def open_tree_dir(root_fd: int, parts: Sequence[str], *, create: bool = False, mode: int = 0o750) -> int:
    """Walk `parts` from an open directory fd without following symlinks.

    Returns a new open directory fd (caller closes it). When `create` is True,
    missing subdirectories are created via mkdirat with `mode` and normalized
    with fchmod on the opened descriptor.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.dup(root_fd)
    try:
        for part in parts:
            _single_name(part)
            created = False
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode, dir_fd=current)
                except FileExistsError:
                    pass
                else:
                    created = True
                child = os.open(part, flags, dir_fd=current)
                if created:
                    try:
                        os.fchmod(child, mode)
                    except BaseException:
                        os.close(child)
                        raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def open_directory(root: Path | str, relative: Path | str = ".", *, create: bool = False, mode: int = 0o750) -> int:
    """Open `root / relative` anchored from `/` with O_DIRECTORY | O_NOFOLLOW."""
    root_path = Path(root)
    rel_path = Path(relative)
    if rel_path.is_absolute():
        raise ValueError(f"relative path must not be absolute: {relative!r}")
    parts = [part for part in rel_path.parts if part != "."]
    root_fd = _open_root(root_path)
    try:
        return open_tree_dir(root_fd, parts, create=create, mode=mode)
    finally:
        os.close(root_fd)


def replace_at(dir_fd: int, name: str, data: bytes, mode: int, *, sync: bool = True,
               prefix: str = ".", suffix: str = ".tmp") -> None:
    """Atomically write `name` inside `dir_fd` using a random temporary name."""
    _single_name(name)
    for _ in range(16):
        temp = f"{prefix}{name}.{secrets.token_hex(TEMPORARY_BYTES)}{suffix}"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"could not allocate temporary file for {name}")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if sync:
                os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=dir_fd)
        raise
    if sync:
        os.fsync(dir_fd)


def unlink_at(dir_fd: int, name: str) -> bool:
    """Unlink `name` inside `dir_fd` without following symlinks."""
    _single_name(name)
    try:
        os.unlink(name, dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        return False


def prune_at(root_fd: int, parts: Sequence[str]) -> None:
    """Remove empty directories along `parts` below `root_fd`, deepest first."""
    if not parts:
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.dup(root_fd)]
    names: list[str] = []
    try:
        for part in parts:
            _single_name(part)
            try:
                child = os.open(part, flags, dir_fd=fds[-1])
            except OSError:
                break
            fds.append(child)
            names.append(part)
        for index in range(len(names) - 1, -1, -1):
            os.close(fds.pop())
            try:
                os.rmdir(names[index], dir_fd=fds[-1])
            except OSError:
                break
    finally:
        for fd in fds:
            os.close(fd)
