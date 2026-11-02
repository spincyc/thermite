"""Finite ownership repair for Thermite's mutable service state.

Run explicitly as an administrative command, or as the configured service
identity. Paths are opened relative to directory descriptors without following
symlinks. Hard links and mount crossings are refused before any repair.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import json
import os
from pathlib import Path
import secrets
import stat

ACCOUNT = GROUP = "thermite"
UID = GID = 9001
RECEIPT_VERSION = 1
POLICY = "thermite-service-v1"
RECEIPT = "deploy/service-account.json"
SHARED_DIR = 0o2770
SHARED_FILE = 0o660
SHARED_PROGRAM = 0o770
RUNTIME_DIR = 0o750
RUNTIME_FILE = 0o640
PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600

SHARED = ("conf", "metrics", "atv/app", "exporter/app", "deploy")
PRIVATE = ("secrets", "atv/secrets", "monitoring/secrets", "userdata")
NATIVE_ROOTS = ("conf", "userdata", "addons", "atv", "deploy", "secrets", "metrics", "monitoring", "exporter", "backups")
RUNTIME_ROOTS = ("", "addons", "atv", "atv/venv", "monitoring", "monitoring/prometheus", "monitoring/grafana",
                 "monitoring/alertmanager", "monitoring/ntfy", "exporter")
DEPLOY_RECORDS = ("deploy/history", "deploy/deployed.json", "deploy/atv-restart", "deploy/metrics-restart", RECEIPT)


class ServiceError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Issue:
    path: Path
    uid: int
    gid: int
    mode: int
    wanted_uid: int
    wanted_gid: int
    wanted_mode: int


def _under(relative: str, roots: tuple[str, ...]) -> bool:
    return any(relative == root or relative.startswith(root + "/") for root in roots)


def desired_mode(relative: str, mode: int, directory: bool) -> int:
    """Narrow access; an existing private file in shared config stays private.

    Shared directories themselves are the operator's deployment boundary.
    Descendant directories that were deliberately private remain private.
    """
    if _under(relative, PRIVATE):
        return PRIVATE_DIR if directory else PRIVATE_FILE
    if _under(relative, ("backups",)):
        # Recovery can enumerate archive names without reading credentials.
        return RUNTIME_DIR if directory else PRIVATE_FILE
    if _under(relative, SHARED):
        if directory:
            return 0o2700 if relative not in SHARED and not mode & 0o070 else SHARED_DIR
        shared = SHARED_PROGRAM if mode & 0o111 else SHARED_FILE
        return shared if relative in DEPLOY_RECORDS or mode & 0o070 else shared & 0o700
    if directory:
        return RUNTIME_DIR if relative in RUNTIME_ROOTS else RUNTIME_DIR & (mode | 0o700)
    return (0o750 if mode & 0o111 else RUNTIME_FILE) & (mode | 0o600)


def validate_identity(uid: int, gid: int) -> None:
    if type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0:
        raise ServiceError("service uid and gid must be positive non-root integers")


def _open_root(root: Path) -> int:
    root = Path(os.path.abspath(root))
    if root == Path("/"):
        raise ServiceError("the filesystem root is not service state")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


@contextlib.contextmanager
def _entries(root: Path, device: int | None = None):
    """Stream checked inodes with at most one open descriptor per depth."""
    first = _open_root(root)
    device = os.fstat(first).st_dev if device is None else device

    def visit(relative: str, descriptor: int):
        info = os.fstat(descriptor)
        if info.st_dev != device:
            raise ServiceError(f"refusing mounted state entry {root / relative}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ServiceError(f"refusing hard-linked state file {root / relative}")
        yield relative, descriptor, info
        if not stat.S_ISDIR(info.st_mode):
            return
        for name in sorted(os.listdir(descriptor)):
            child_relative = f"{relative}/{name}" if relative else name
            found = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(found.st_mode):
                flags = os.O_PATH | os.O_NOFOLLOW
            elif stat.S_ISDIR(found.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            elif stat.S_ISREG(found.st_mode):
                flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
            else:
                raise ServiceError(f"refusing special state entry {root / child_relative}")
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                actual = os.fstat(child)
                if (actual.st_dev, actual.st_ino, stat.S_IFMT(actual.st_mode)) != (
                        found.st_dev, found.st_ino, stat.S_IFMT(found.st_mode)):
                    raise ServiceError(f"state changed during inspection: {root / child_relative}")
                yield from visit(child_relative, child)
            finally:
                os.close(child)

    walk = visit("", first)
    try:
        yield walk
    finally:
        walk.close()
        os.close(first)


@contextlib.contextmanager
def _selected_entries(root: Path, deployment: bool):
    first = _open_root(root)
    device = os.fstat(first).st_dev
    if not deployment:
        try:
            os.stat("access", dir_fd=first, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            os.close(first)
            raise ServiceError("native state contains access custody; relocate/repair access separately before normalization")

    def selected():
        if not deployment:
            yield "", first, os.fstat(first)
        for relative in SHARED if deployment else NATIVE_ROOTS:
            try:
                with _entries(root / relative, device) as entries:
                    for child, descriptor, info in entries:
                        yield relative + ("/" + child if child else ""), descriptor, info
            except FileNotFoundError:
                # Older installs do not yet have metrics/exporter directories.
                continue

    walk = selected()
    try:
        yield walk
    finally:
        walk.close()
        os.close(first)


def _issues(root: Path, entries, uid: int, gid: int) -> list[Issue]:
    issues = []
    for relative, _, info in entries:
        mode = stat.S_IMODE(info.st_mode)
        wanted = mode if stat.S_ISLNK(info.st_mode) else desired_mode(relative, mode, stat.S_ISDIR(info.st_mode))
        if (info.st_uid, info.st_gid, mode) != (uid, gid, wanted):
            issues.append(Issue(root / relative, info.st_uid, info.st_gid, mode, uid, gid, wanted))
    return issues


def audit_state(root: Path, uid: int = UID, gid: int = GID, *, deployment: bool = False) -> list[Issue]:
    validate_identity(uid, gid)
    root = Path(os.path.abspath(root))
    with _selected_entries(root, deployment) as entries:
        return _issues(root, entries, uid, gid)


def _chown_link(descriptor: int, uid: int, gid: int) -> None:
    # Linux fchownat(AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW) acts on the opened
    # symlink inode even if its name is concurrently replaced. No target is read.
    function = ctypes.CDLL(None, use_errno=True).fchownat
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    function.restype = ctypes.c_int
    if function(descriptor, b"", uid, gid, 0x1000 | 0x100):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _remove_receipt(root: Path) -> None:
    try:
        directory = _open_root(root / "deploy")
    except FileNotFoundError:
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(Path(RECEIPT).name, dir_fd=directory)
    finally:
        os.close(directory)


def _write_receipt(root: Path, uid: int, gid: int) -> None:
    directory = _open_root(root / "deploy")
    name = f".service-account.{secrets.token_hex(12)}.tmp"
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"version": RECEIPT_VERSION, "uid": uid, "gid": gid, "policy": POLICY}, output, sort_keys=True)
            output.write("\n")
            output.flush()
            if (os.fstat(output.fileno()).st_uid, os.fstat(output.fileno()).st_gid) != (uid, gid):
                os.fchown(output.fileno(), uid, gid)
            os.fchmod(output.fileno(), SHARED_FILE)
            os.fsync(output.fileno())
        os.replace(name, Path(RECEIPT).name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)
        os.close(directory)


def normalize_state(root: Path, uid: int = UID, gid: int = GID, *, deployment: bool = False,
                    receipt: bool = False) -> list[Issue]:
    """Repair only this state tree; return its pre-repair differences."""
    validate_identity(uid, gid)
    if deployment and receipt:
        raise ServiceError("a full service-account receipt cannot be written by a deployment-only repair")
    root = Path(os.path.abspath(root))
    # Validate the whole tree before any mutation. The repair walk repeats
    # inode checks as it opens entries; descriptors stay bounded by depth.
    try:
        issues = audit_state(root, uid, gid, deployment=deployment)
        if not deployment:
            _remove_receipt(root)
        with _selected_entries(root, deployment) as entries:
            for relative, descriptor, info in entries:
                if (info.st_uid, info.st_gid) != (uid, gid):
                    if stat.S_ISLNK(info.st_mode):
                        _chown_link(descriptor, uid, gid)
                    else:
                        os.fchown(descriptor, uid, gid)
                if not stat.S_ISLNK(info.st_mode):
                    wanted = desired_mode(relative, stat.S_IMODE(info.st_mode), stat.S_ISDIR(info.st_mode))
                    if stat.S_IMODE(os.fstat(descriptor).st_mode) != wanted:
                        os.fchmod(descriptor, wanted)
        if receipt:
            _write_receipt(root, uid, gid)
    except BaseException:
        if not deployment:
            # Invalidating the fixed administrative receipt is the only
            # permitted mutation when validation itself has failed.
            with contextlib.suppress(OSError):
                _remove_receipt(root)
        raise
    return issues


def pairing_logging(root: Path) -> bool:
    import oh_logging
    # Read only the two fixed files; native userdata can be large and active.
    directory = _open_root(Path(root) / "userdata/etc")
    try:
        contents = {}
        for path in (oh_logging.RELATIVE, oh_logging.SELECTOR):
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                    return False
                contents[path] = os.read(descriptor, info.st_size + 1)
            finally:
                os.close(descriptor)
        return oh_logging.selected_default(contents[oh_logging.SELECTOR]) and oh_logging.protected(contents[oh_logging.RELATIVE])
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--uid", type=int, default=UID)
    parser.add_argument("--gid", type=int, default=GID)
    parser.add_argument("--deployment", action="store_true", help="repair only shared deployment outputs; leave active stores alone")
    parser.add_argument("--receipt", action="store_true", help="record successful full repair at deploy/service-account.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--check-pairing-logging", action="store_true")
    args = parser.parse_args(argv)
    if args.receipt and (args.deployment or args.check or args.check_pairing_logging):
        parser.error("--receipt requires a full repair")
    try:
        if args.check_pairing_logging:
            if not pairing_logging(args.root):
                raise ServiceError("pairing logging protection is missing; run thermite install before deploying")
            return 0
        issues = (audit_state(args.root, args.uid, args.gid, deployment=args.deployment) if args.check else
                  normalize_state(args.root, args.uid, args.gid, deployment=args.deployment, receipt=args.receipt))
        if args.check:
            for issue in issues:
                print(f"{issue.path}: expected {issue.wanted_uid}:{issue.wanted_gid} {issue.wanted_mode:04o}")
            return int(bool(issues))
        print(f"{args.root}: {len(issues)} ownership/permission entries repaired")
        return 0
    except (OSError, ServiceError) as exc:
        parser.exit(2, f"service state: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
