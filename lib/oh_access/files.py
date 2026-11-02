"""Owner-only bounded files and atomic replacement, shared by CLI and service."""

import json
import grp
import os
from pathlib import Path
import pwd
import secrets
import stat

from oh_service import ACCOUNT, GROUP, UID, GID

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


def require_service_account():
    uid, gid = service_identity(required=True)
    if os.geteuid() != uid or os.getegid() != gid:
        raise AccessError("Run this access service as user thermite and group thermite after the ownership handoff")
    return uid, gid


def private_directory(value):
    path = Path(value).expanduser().absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
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
                    os.fchmod(child, 0o700)
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


def read_private(value, maximum=2 * 1024 * 1024):
    path = Path(value).expanduser().absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > maximum:
            raise AccessError("Private file must be owned, regular, mode 0600 and bounded")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise AccessError("Private file exceeds its limit")
        return content
    finally:
        os.close(fd)


def atomic_private(value, content):
    path = Path(value).expanduser().absolute()
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path)
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def json_private(value, data):
    atomic_private(value, (json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n").encode())
