"""Owner-only bounded files and atomic replacement, shared by CLI and service."""

import json
import os
from pathlib import Path
import secrets
import stat

from . import AccessError


def private_directory(value):
    path = Path(value).expanduser().absolute()
    # Refuse a symlink anywhere in the path, including intermediate directories.
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise AccessError("Private paths must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AccessError("Private directory must be owned by this user and mode 0700")
    return path


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
