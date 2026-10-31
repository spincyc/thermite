"""Owner-only state, deliberately outside generated runtime configuration."""

import os
import secrets
import stat
import uuid
from pathlib import Path


def private_state(value):
    if not value:
        raise ValueError("Set THERMITE_IDENTITY_STATE to a private state directory")
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Identity state must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Identity state must be owned by this service and mode 0700")
    os.umask(0o077)
    for name in ("identity.sqlite3", "identity.sqlite3-wal", "identity.sqlite3-shm", "identity.sqlite3-journal", "secret-key", "site-id"):
        target = path / name
        if target.is_symlink():
            raise ValueError("Identity files must not be symlinks")
        if target.exists() and (target.stat().st_uid != os.geteuid() or stat.S_IMODE(target.stat().st_mode) != 0o600):
            raise ValueError("Identity files must be owned by this service and mode 0600")
    key = path / "secret-key"
    try:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(64))
    identity = path / "site-id"
    try:
        fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(uuid.uuid4()))
    uuid.UUID(identity.read_text().strip())
    return path
