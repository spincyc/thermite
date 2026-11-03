"""Publish a completed, private upgrade backup without replacing an older file.

The installer stops the writers before invoking this administrative helper.
Only a successful tar, durable archive publication and durable completion
receipt authorize reuse. Legacy archives and interrupted publications without
receipts are retained for inspection, never mistaken for completed backups.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

UNSAVED = ("userdata/cache", "userdata/tmp")
RECEIPT_SUFFIX = ".complete.json"


class BackupError(ValueError):
    pass


def archive_argv(root: Path, file: Path | str, grafana: bool = False) -> tuple[str, ...]:
    return ("tar", "--create", "--gzip", "--file", str(file), "--directory", str(root),
            *(f"--exclude={path}" for path in UNSAVED), "conf", "userdata",
            *(("monitoring/grafana",) if grafana else ()))


def receipt_path(archive: Path) -> Path:
    return archive.with_name(archive.name + RECEIPT_SUFFIX)


def completed(raw: bytes | None, name: str, mtime: float, *, size: int | None = None) -> bool:
    """Recognize the helper's completion witness and unchanged archive metadata."""
    try:
        value = json.loads(raw or b"null")
    except (ValueError, UnicodeError):
        return False
    return (isinstance(value, dict) and set(value) == {"version", "archive", "mtime", "size"}
            and type(value["version"]) is int and value["version"] == 1
            and value["archive"] == name and value["mtime"] == mtime
            and type(value["size"]) is int and value["size"] > 0
            and (size is None or value["size"] == size))


def _directory(path: Path, stack: ExitStack) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    stack.callback(os.close, descriptor)
    for part in path.parts[1:]:
        descriptor = os.open(part, flags, dir_fd=descriptor)
        stack.callback(os.close, descriptor)
    return descriptor


def _temporary(parent: int, name: str, mode: int, stack: ExitStack) -> tuple[str, int]:
    temporary = "." + name + "." + os.urandom(12).hex() + ".unfinished"
    descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    stack.callback(os.close, descriptor)
    def remove():
        os.unlink(temporary, dir_fd=parent)
    stack.callback(remove)
    return temporary, descriptor


def create(root: Path, *, name: str, uid: int, gid: int, grafana: bool = False) -> Path:
    root = Path(root).expanduser()
    if ".." in root.parts:
        raise BackupError("backup roots must not contain parent traversal")
    root = Path(os.path.abspath(root))
    if root == Path("/") or Path(name).name != name or not name.endswith(".tar.gz") or len(name) > 180:
        raise BackupError("choose a service root and a plain .tar.gz backup filename")
    if type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0:
        raise BackupError("invalid backup owner")
    if os.geteuid() != 0 and (uid, gid) != (os.geteuid(), os.getegid()):
        raise BackupError("an administrative command is required for another backup owner")
    witness = name + RECEIPT_SUFFIX
    with ExitStack() as stack:
        source = _directory(root, stack)
        parent = os.open("backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source)
        stack.callback(os.close, parent)
        for entry in (name, witness):
            try:
                os.stat(entry, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise BackupError("backup publication is already occupied; rerun to choose a new filename")
        temporary, descriptor = _temporary(parent, name, 0o600, stack)
        # Keep both the input root and output inode anchored if paths change.
        # stdout supplies tar's archive file; no command opens its final name.
        subprocess.run(archive_argv(Path(f"/proc/self/fd/{source}"), "-", grafana),
                       stdout=descriptor, pass_fds=(source,), check=True)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if info.st_size == 0:
            raise BackupError("tar returned an empty backup")
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
        receipt_name, receipt = _temporary(parent, witness, 0o640, stack)
        os.fchown(receipt, uid, gid)
        os.fchmod(receipt, 0o640)
        with os.fdopen(os.dup(receipt), "w", encoding="utf-8") as output:
            json.dump({"version": 1, "archive": name, "mtime": info.st_mtime, "size": info.st_size}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(receipt_name, witness, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    return root / "backups" / name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--grafana", action="store_true")
    args = parser.parse_args(argv)
    try:
        create(args.root, name=args.name, uid=args.uid, gid=args.gid, grafana=args.grafana)
    except (BackupError, OSError, subprocess.CalledProcessError) as exc:
        print(f"thermite backup: {exc}; no completed backup was recorded by this attempt", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
