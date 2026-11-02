"""Standalone, stdlib-only custody helper copied into an operator-owned plan.

Run the saved copy with the trusted system Python, never a service-owned
interpreter or module. Only the finite inodes recorded during staging may be
changed. Both walks open paths without following links, and the complete first
walk must succeed before the second can change a mode or owner.
"""

import contextlib
import ctypes
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import sys


class CustodyError(ValueError):
    pass


def _mount_id(descriptor):
    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise CustodyError("Cannot verify custody mount identity")


def manifest(inventories, uid, gid):
    roots = []
    for inventory in inventories:
        if not inventory:
            continue
        root = inventory[0][0]
        entries = []
        for path, mode in inventory:
            info = path.lstat()
            entries.append({"path": str(path.relative_to(root)), "device": info.st_dev,
                            "inode": info.st_ino, "type": stat.S_IFMT(info.st_mode), "mode": mode})
        roots.append({"path": str(root), "entries": entries})
    return {"version": 1, "operator_uid": os.geteuid(), "uid": uid, "gid": gid, "roots": roots}


@contextlib.contextmanager
def _root_descriptor(path):
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        # Reopen the anchored inode, never its replaceable pathname.
        child = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _trees(plan):
    if plan.get("version") != 1 or any(type(plan.get(key)) is not int or plan[key] <= 0
                                      for key in ("operator_uid", "uid", "gid")):
        raise CustodyError("Invalid custody identity")
    result = []
    for root in plan["roots"]:
        path = Path(root["path"])
        if not path.is_absolute() or path == Path("/") or str(path) != os.path.abspath(path):
            raise CustodyError("Invalid custody root")
        if any(path == other or path in other.parents or other in path.parents for other, _ in result):
            raise CustodyError("Custody roots must be separate")
        tree = {}
        entries = root["entries"]
        seen = set()
        for entry in entries:
            relative = entry["path"]
            parts = () if relative == "." else tuple(relative.split("/"))
            if any(part in {"", ".", ".."} for part in parts) or parts in seen:
                raise CustodyError("Invalid custody entry")
            if any(type(entry.get(key)) is not int or entry[key] < 0 for key in ("device", "inode", "type")):
                raise CustodyError("Invalid custody inode")
            kind, mode = entry["type"], entry["mode"]
            if not (kind == stat.S_IFDIR and mode == 0o700 or kind == stat.S_IFREG and mode in (0o600, 0o700)
                    or kind == stat.S_IFLNK and mode is None):
                raise CustodyError("Invalid custody mode or file type")
            seen.add(parts)
            node = tree
            for part in parts:
                node = node.setdefault(part, {})
            node[None] = entry
        if () not in seen or any(parts[:-1] not in seen for parts in seen if parts):
            raise CustodyError("Incomplete custody ancestry")
        result.append((path, tree))
    if not result:
        raise CustodyError("Empty custody plan")
    return result


def _walk(tree, descriptor, mount, owners):
    entry = tree[None]
    info = os.fstat(descriptor)
    if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) != (entry["device"], entry["inode"], entry["type"]):
        raise CustodyError("Custody inode changed since staging")
    if info.st_uid not in owners or _mount_id(descriptor) != mount:
        raise CustodyError("Custody owner or mount changed")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise CustodyError("Custody files must not have hard links")
    yield descriptor, entry, info
    for name, child_tree in tree.items():
        if name is None:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise CustodyError("Custody ancestor is not a directory")
        kind = child_tree[None]["type"]
        flags = os.O_PATH if kind == stat.S_IFLNK else os.O_RDONLY | os.O_NONBLOCK
        if kind == stat.S_IFDIR:
            flags |= os.O_DIRECTORY
        child = os.open(name, flags | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            yield from _walk(child_tree, child, mount, owners)
        finally:
            os.close(child)


def _chown_link(descriptor, uid, gid):
    # fchownat(AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW), as in oh_service.
    function = ctypes.CDLL(None, use_errno=True).fchownat
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)
    function.restype = ctypes.c_int
    if function(descriptor, b"", uid, gid, 0x1000 | 0x100):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def apply(plan):
    trees = _trees(plan)
    uid, gid = plan["uid"], plan["gid"]
    owners = {plan["operator_uid"], uid}
    # Keep root descriptors across both walks, so a rename cannot redirect the
    # repair into a different release or persistent-state directory.
    with contextlib.ExitStack() as stack:
        opened = [(tree, stack.enter_context(_root_descriptor(path))) for path, tree in trees]
        mounts = [_mount_id(descriptor) for _, descriptor in opened]
        for (tree, descriptor), mount in zip(opened, mounts):
            for _ in _walk(tree, descriptor, mount, owners):
                pass
        for (tree, descriptor), mount in zip(opened, mounts):
            for child, entry, info in _walk(tree, descriptor, mount, owners):
                if entry["mode"] is not None and stat.S_IMODE(info.st_mode) != entry["mode"]:
                    os.fchmod(child, entry["mode"])
                if (info.st_uid, info.st_gid) != (uid, gid):
                    if stat.S_ISLNK(info.st_mode):
                        _chown_link(child, uid, gid)
                    else:
                        os.fchown(child, uid, gid)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 1:
            raise CustodyError("Supply the operator-owned custody manifest")
        descriptor = os.open(arguments[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 64 * 1024 * 1024:
                raise CustodyError("Custody manifest must be private, regular and bounded")
            plan = json.load(source)
        if info.st_uid != plan["operator_uid"]:
            raise CustodyError("Custody manifest is not operator-owned")
        account, group = pwd.getpwnam("thermite"), grp.getgrnam("thermite")
        if (account.pw_uid, account.pw_gid, group.gr_gid) != (plan["uid"], plan["gid"], plan["gid"]):
            raise CustodyError("Thermite service identity changed since staging")
        apply(plan)
        print("Finite thermite custody plan applied.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print("thermite custody: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
