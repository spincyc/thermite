"""Owner-invoked, finite installation of the two monitoring credentials.

This standalone stdlib helper is invoked by an explicit approved administrative
command. It never discovers credentials or rotates an existing Grafana password.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time

NAMES = ("openhab-token", "grafana-admin-password")
MAXIMUM = 65536
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class SecretError(ValueError):
    pass


def _path(value):
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise SecretError("credential paths must not contain parent traversal")
    return Path(os.path.abspath(path))


def _keep(stack, fd):
    stack.callback(os.close, fd)
    return fd


def _directory(path, stack):
    fd = _keep(stack, os.open("/", DIRECTORY))
    for part in path.parts[1:]:
        fd = _keep(stack, os.open(part, DIRECTORY, dir_fd=fd))
    return fd


def _child(parent, name, stack, *, create=False):
    try:
        return _keep(stack, os.open(name, DIRECTORY, dir_fd=parent))
    except FileNotFoundError:
        if not create:
            return None
    os.mkdir(name, mode=0o700, dir_fd=parent)
    return _keep(stack, os.open(name, DIRECTORY, dir_fd=parent))


def _regular(fd, *, source_uid=None):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAXIMUM:
        raise SecretError("credentials must be bounded nonempty regular files without hard links")
    if source_uid is not None and (info.st_uid != source_uid or stat.S_IMODE(info.st_mode) != 0o600):
        raise SecretError("the operator credential copy must be owned by the invoking administrator and mode 0600")
    return info


def _read(fd):
    with os.fdopen(fd, "rb", closefd=False) as stream:
        content = stream.read(MAXIMUM + 1)
    if not 0 < len(content) <= MAXIMUM:
        raise SecretError("credential size changed while reading")
    return content


def _unchanged(parent, name, info):
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise SecretError("credential custody changed during installation")


def _owner_copy(parent, name, content, uid, gid):
    temporary = "." + name + "." + os.urandom(12).hex()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish without ever replacing another operator copy.
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=parent)
    os.fsync(parent)


def install(state_root, *, name, source=None, uid, gid, preserve_existing=False, generate_if_missing=False):
    """Install exact bytes, or preserve an existing password without opening source."""
    if name not in NAMES or type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0:
        raise SecretError("choose a supported credential and non-root service uid/gid")
    if preserve_existing and name != "grafana-admin-password":
        raise SecretError("only the generated Grafana password may preserve an existing credential")
    if generate_if_missing and not preserve_existing:
        raise SecretError("generation requires preserving the existing Grafana password")
    root = _path(state_root)
    if root == Path("/"):
        raise SecretError("the filesystem root cannot be a service state root")
    if os.geteuid() != 0 and (uid != os.geteuid() or gid != os.getegid()):
        raise SecretError("an administrative command is required to install another service identity's credential")
    with ExitStack() as stack:
        root_fd = _directory(root, stack)
        monitoring = _child(root_fd, "monitoring", stack)
        private = _child(monitoring, "secrets", stack) if monitoring is not None else None
        existing, info = None, None
        if private is not None:
            try:
                existing = _keep(stack, os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=private))
            except FileNotFoundError:
                pass
            if existing is not None:
                info = _regular(existing)

        content = None
        preserved = existing is not None and preserve_existing
        changed = existing is None
        if not preserved:
            try:
                source_uid = int(os.environ.get("SUDO_UID", str(os.getuid())))
            except ValueError:
                raise SecretError("invalid invoking administrator identity") from None
            if source_uid < 0:
                raise SecretError("invalid invoking administrator identity")
            if source is None:
                raise SecretError("an operator-owned credential source is required for a missing or synchronized credential")
            source_path = _path(source)
            parent = _directory(source_path.parent, stack)
            try:
                source_fd = _keep(stack, os.open(source_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent))
            except FileNotFoundError:
                if not generate_if_missing:
                    raise
                info_parent = os.fstat(parent)
                if info_parent.st_uid != source_uid or info_parent.st_mode & 0o022:
                    raise SecretError("new credential copies require a non-writable administrator-owned parent")
                try:
                    source_gid = int(os.environ.get("SUDO_GID", str(os.getgid())))
                except ValueError:
                    raise SecretError("invalid invoking administrator group") from None
                if source_gid < 0:
                    raise SecretError("invalid invoking administrator group")
                content = os.urandom(24).hex().encode()
                _owner_copy(parent, source_path.name, content, source_uid, source_gid)
            else:
                _regular(source_fd, source_uid=source_uid)
                content = _read(source_fd)
            changed = existing is None or _read(existing) != content

        # Every root, ancestor, existing target and required source has now been
        # validated. Only these finite private parents may be created/repaired.
        if monitoring is None:
            monitoring = _child(root_fd, "monitoring", stack, create=True)
        if private is None:
            private = _child(monitoring, "secrets", stack, create=True)
        for fd, mode in ((monitoring, 0o750), (private, 0o700)):
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
        if existing is not None:
            _unchanged(private, name, info)
        if not changed:
            os.fchown(existing, uid, gid)
            os.fchmod(existing, 0o600)
        else:
            temporary = "." + name + "." + os.urandom(12).hex()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=private)
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchown(stream.fileno(), uid, gid)
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=private, dst_dir_fd=private)
                os.fsync(private)
            finally:
                try:
                    os.unlink(temporary, dir_fd=private)
                except FileNotFoundError:
                    pass
        return {"installed": True, "name": name, "changed": changed, "preserved": preserved}


def _validate_runtime_token(url, content):
    """Verify a service credential with one bounded, read-only loopback request."""
    # Both modules are stdlib-only and ship together in every pinned release;
    # the administrative helper also runs directly, without package imports.
    if __package__:
        from . import native_auth
    else:
        # Release validation accepts installer-produced bytecode as inert.
        # Match the application's launcher: never execute adjacent .pyc files
        # when this finite helper runs with administrative privileges.
        sys.dont_write_bytecode = True
        sys.pycache_prefix = str(Path(__file__).resolve().parent / (".uncached-" + os.urandom(16).hex()))
        import native_auth
    try:
        origin = native_auth._origin(url)
        token = content.decode("ascii").strip()
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("invalid token")
        client = native_auth._Client(origin, time.monotonic() + 10, time.monotonic)
        native_auth._metadata(client, token)
    except (OSError, ValueError, RecursionError, native_auth.NativeAuthError):
        raise SecretError("installed runtime credential could not be verified") from None


def recover_runtime_token(state_root, *, destination, uid, gid, url):
    """Restore only a missing operator token from the installed service copy.

    No account or service secret is changed. Existing local credentials and
    managed journals require explicit recovery and are never replaced here.
    """
    if type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0:
        raise SecretError("choose a non-root service identity")
    root, destination = _path(state_root), _path(destination)
    if root == Path("/") or destination.name != "api-token":
        raise SecretError("choose the installed state root and operator api-token path")
    try:
        owner = int(os.environ.get("SUDO_UID", str(os.getuid())))
        group = int(os.environ.get("SUDO_GID", str(os.getgid())))
    except ValueError:
        raise SecretError("invalid invoking operator identity") from None
    if owner < 0 or group < 0 or os.geteuid() != 0 and (owner, group) != (os.getuid(), os.getgid()):
        raise SecretError("invalid invoking operator identity")
    with ExitStack() as stack:
        root_fd = _directory(root, stack)
        monitoring = _child(root_fd, "monitoring", stack)
        private = _child(monitoring, "secrets", stack) if monitoring is not None else None
        if private is None:
            raise SecretError("the installation has no saved runtime credential")
        directory = os.fstat(private)
        if (directory.st_uid, directory.st_gid, stat.S_IMODE(directory.st_mode)) != (uid, gid, 0o700):
            raise SecretError("installed credential directory has unsafe custody")
        credential = _keep(stack, os.open("openhab-token", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=private))
        info = _regular(credential, source_uid=uid)
        if info.st_gid != gid:
            raise SecretError("installed credential has the wrong group")
        content = _read(credential)
        parent = _directory(destination.parent, stack)
        directory = os.fstat(parent)
        if directory.st_uid != owner or stat.S_IMODE(directory.st_mode) != 0o700:
            raise SecretError("operator credential directory must be private and owned")
        lock = _keep(stack, os.open(".runtime-access.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   0o600, dir_fd=parent))
        lock_info = os.fstat(lock)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_uid not in {owner, os.geteuid()}):
            raise SecretError("runtime access lock has unsafe custody")
        os.fchown(lock, owner, group)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in (destination.name, "runtime-credentials.json"):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise SecretError("existing operator credentials require explicit recovery")
        _validate_runtime_token(url, content)
        _unchanged(private, "openhab-token", info)
        _owner_copy(parent, destination.name, content, owner, group)
    return {"recovered": True, "name": "openhab-token"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_root", type=Path)
    parser.add_argument("--name", required=True, choices=NAMES)
    parser.add_argument("--source", type=Path, help="invoking administrator's private mode-0600 copy")
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--preserve-existing", action="store_true")
    parser.add_argument("--generate-if-missing", action="store_true", help="generate only a genuinely missing Grafana password and save the invoking administrator's private copy")
    parser.add_argument("--recover-runtime-token", action="store_true", help="restore a missing operator token from the installed service credential")
    parser.add_argument("--runtime-url", help="literal loopback runtime origin for credential recovery")
    args = parser.parse_args(argv)
    try:
        if args.recover_runtime_token:
            if args.name != "openhab-token" or args.source is None or args.preserve_existing or args.generate_if_missing:
                raise SecretError("invalid runtime recovery arguments")
            result = recover_runtime_token(args.state_root, destination=args.source, uid=args.uid, gid=args.gid, url=args.runtime_url)
        else:
            if args.runtime_url is not None:
                raise SecretError("runtime origin is only used for credential recovery")
            values = vars(args).copy()
            values.pop("recover_runtime_token")
            values.pop("runtime_url")
            result = install(**values)
    except (OSError, SecretError):
        print("thermite credential: operation refused; check finite paths, file custody and service identity", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
