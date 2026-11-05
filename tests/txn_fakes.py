"""An in-memory disk and host for the install engine, and whole installations
on them. Helpers only: the test_txn_*.py modules use them.

MemoryDisk keeps what the engine has written apart from what is durable: a
change becomes durable only once an fsync of its directory, or a sync of its
filesystem, covers it, and crash() keeps only what was covered, replayed in
order. Every mutating call, of the disk or the host, counts; `fail_at` kills
the run (raises Crash) just before that call.

crash(rng) is harsher than any real filesystem: besides what was covered,
each uncovered change survives or not at random, out of order, and a copy
or a tree removal may survive in part.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import errno
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_install import engine

ROOT = "/srv/thermite"
ID = "20261105T101500Z-3f9a2c"
OLD = "20261020T080000Z-77ab01"
CLOCK = "2026-11-05T10:31:12Z"
SOURCE = b"# the engine that began the transaction\n"
TOOLS = {"systemctl": "/usr/bin/systemctl", "cp": "/usr/bin/cp", "sync": "/usr/bin/sync",
         "nft": "/usr/sbin/nft", "apparmor_parser": "/usr/sbin/apparmor_parser", "podman": "/usr/bin/podman"}
OPERATOR = {"uid": 1000, "site": "/home/owner/site", "record": f"/home/owner/site/var/transaction/{ID}/record.json"}
#: A pin name; versions.json calls each service's image digest its pin.
PROMETHEUS = "prometheus"
UNITS = "/etc/systemd/system"
WANTS = f"{UNITS}/multi-user.target.wants"
BOUNDARY = "thermite-native-boundary.service"
RULES = "/etc/thermite-native.nft"
POLICY = "/etc/apparmor.d/thermite-node-exporter"
APPARMOR = "/sys/module/apparmor/parameters/enabled"


class Crash(BaseException):
    """The engine (or the operator's run around it) was killed here."""


@dataclasses.dataclass
class Node:
    kind: str
    data: bytes = b""
    target: str = ""
    mode: int = 0o644
    uid: int = 0
    gid: int = 0

    def __deepcopy__(self, memo):
        # Every field is immutable; forks copy thousands of these.
        return Node(self.kind, self.data, self.target, self.mode, self.uid, self.gid)


def parent(path):
    return str(PurePosixPath(path).parent)


def inside(path, top):
    return path == top or top == "/" or path.startswith(top + "/")


class MemoryDisk(engine.Disk):
    """A filesystem of path → Node, with a durable layer behind it.

    `mounts` maps a directory to a filesystem name: a rename between two
    raises EXDEV. `budget` caps the bytes of file data on a filesystem:
    past it, writes and copies raise ENOSPC, a copy after writing what fit."""

    owner = 0

    def __init__(self, *, mounts=None, budget=None):
        self.nodes = {"/": Node("dir", mode=0o755)}
        self.base = copy.deepcopy(self.nodes)
        self.pending = []  # [op, covered]
        self.mounts = dict(mounts or {})
        self.budget = dict(budget or {})
        self.fail_at = None
        self.count = 0
        self.calls = []
        # Called with each mutating call before it happens.
        self.on_tick = None

    # Faults --------------------------------------------------------------

    def tick(self, *call):
        if self.on_tick is not None:
            self.on_tick(call)
        self.count += 1
        self.calls.append(call)
        if self.fail_at is not None and self.count == self.fail_at:
            raise Crash(call)

    def fs(self, path):
        matches = [top for top in self.mounts if inside(path, top)]
        return self.mounts[max(matches, key=len)] if matches else "root"

    def used(self, fs):
        return sum(len(node.data) for path, node in self.nodes.items() if node.kind == "file" and self.fs(path) == fs)

    def _room(self, path, more):
        fs = self.fs(path)
        if fs in self.budget and self.used(fs) + more > self.budget[fs]:
            raise OSError(errno.ENOSPC, "No space left on device", path)

    # Durability ----------------------------------------------------------

    def _record(self, op, covered=False):
        self.pending.append([op, covered])
        self._fold()

    def _fold(self):
        while self.pending and self.pending[0][1]:
            apply(self.base, self.pending.pop(0)[0])

    def _cover(self, test):
        for item in self.pending:
            if not item[1] and test(item[0]):
                item[1] = True
        self._fold()

    def settle(self):
        """Everything so far is durable (a fixture's starting state)."""
        self._cover(lambda op: True)
        for item in self.pending:
            item[1] = True
        self._fold()

    def crash(self, rng=None):
        """Power loss: keep what a sync covered, replayed in order. With
        `rng`, each uncovered change also survives at random, and a copy or
        removal may survive only in part."""
        nodes = copy.deepcopy(self.base)
        for op, covered in self.pending:
            if covered:
                apply(nodes, op)
            elif rng is not None and rng.random() < 0.5:
                apply(nodes, torn(op, nodes, rng))
        self.nodes, self.base, self.pending = nodes, copy.deepcopy(nodes), []

    # Disk ----------------------------------------------------------------

    def _reached(self, *paths):
        """OsDisk reaches every directory it changes from / without
        following a symlink: a symlink on the way fails as ELOOP does."""
        for path in paths:
            for ancestor in PurePosixPath(path).parents:
                node = self.nodes.get(str(ancestor))
                if node is not None and node.kind == "link":
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(path))

    def _need_dir(self, path):
        node = self.nodes.get(path)
        if node is None or node.kind != "dir":
            raise FileNotFoundError(errno.ENOENT, "No such directory", path)

    def lstat(self, path):
        node = self.nodes.get(str(path))
        if node is None:
            return None
        size = len(node.data) if node.kind == "file" else len(node.target)
        return engine.Stat(node.kind, node.mode, node.uid, node.gid, size, hash(self.fs(str(path))) & 0xffff)

    def read_bytes(self, path, limit):
        node = self.nodes.get(str(path))
        if node is None or node.kind != "file":
            raise FileNotFoundError(errno.ENOENT, "No such file", str(path))
        if len(node.data) > limit:
            raise OSError(errno.EFBIG, "File too large", str(path))
        return node.data

    def readlink(self, path):
        node = self.nodes[str(path)]
        assert node.kind == "link", path
        return node.target

    def listdir(self, path):
        self._need_dir(str(path))
        prefix = str(path).rstrip("/") + "/"
        return sorted(name[len(prefix):] for name in self.nodes
                      if name.startswith(prefix) and "/" not in name[len(prefix):])

    def allocated(self, path):
        return sum(len(node.data) for name, node in self.nodes.items() if inside(name, str(path)))

    def available(self, path):
        fs = self.fs(str(path))
        return self.budget[fs] - self.used(fs) if fs in self.budget else 1 << 40

    def write_atomic(self, path, data, mode):
        path = str(path)
        self.tick("write_atomic", path)
        self._reached(path)
        self._need_dir(parent(path))
        old = self.nodes.get(path)
        self._room(path, len(data) - (len(old.data) if old and old.kind == "file" else 0))
        op = ("put", path, Node("file", bytes(data), mode=mode, uid=self.owner, gid=self.owner))
        apply(self.nodes, op)
        self._record(op, covered=True)

    def mkdir(self, path, mode):
        path = str(path)
        self.tick("mkdir", path)
        self._reached(path)
        self._need_dir(parent(path))
        if path in self.nodes:
            raise FileExistsError(errno.EEXIST, "File exists", path)
        op = ("put", path, Node("dir", mode=mode, uid=self.owner, gid=self.owner))
        apply(self.nodes, op)
        self._record(op)

    def rename(self, source, destination):
        source, destination = str(source), str(destination)
        self.tick("rename", source, destination)
        self._reached(source, destination)
        if source not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "No such file", source)
        if self.fs(source) != self.fs(destination):
            raise OSError(errno.EXDEV, "Invalid cross-device link", source)
        self._need_dir(parent(destination))
        if destination in self.nodes:
            raise FileExistsError(errno.EEXIST, "the engine never renames onto a path", destination)
        op = ("rename", source, destination)
        apply(self.nodes, op)
        self._record(op)

    def copy(self, source, destination):
        source, destination = str(source), str(destination)
        self.tick("copy", source, destination)
        self._reached(source, destination)
        if source not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, "No such file", source)
        self._need_dir(parent(destination))
        if destination in self.nodes:
            raise FileExistsError(errno.EEXIST, "File exists", destination)
        made = {}
        try:
            for name in sorted(name for name in self.nodes if inside(name, source)):
                node = copy.deepcopy(self.nodes[name])
                self._room(destination, len(node.data) if node.kind == "file" else 0)
                target = destination + name[len(source):]
                self.nodes[target] = made[target] = node
        finally:
            self._record(("copy", made))

    def remove_tree(self, path):
        path = str(path)
        self.tick("remove_tree", path)
        self._reached(path)
        op = ("remove", path)
        apply(self.nodes, op)
        self._record(op)

    def sync_fs(self, path):
        self.tick("sync_fs", str(path))
        fs = self.fs(str(path))
        self._cover(lambda op: self.fs(op_path(op)) == fs)

    def fsync_dir(self, path):
        path = str(path)
        self.tick("fsync_dir", path)
        self._need_dir(path)
        self._cover(lambda op: op[0] != "copy" and parent(op_path(op)) == path)

    @contextlib.contextmanager
    def lock(self, path):
        yield

    # Fixtures and the host's own changes (not counted: the host call is) --

    def put(self, path, kind="file", data=b"", target="", mode=None, uid=0, gid=0, *, durable=False):
        path = str(path)
        for ancestor in reversed(PurePosixPath(path).parents):
            if str(ancestor) not in self.nodes:
                op = ("put", str(ancestor), Node("dir", mode=0o755))
                apply(self.nodes, op)
                self._record(op, covered=durable)
        mode = mode if mode is not None else (0o755 if kind == "dir" else 0o777 if kind == "link" else 0o644)
        op = ("put", path, Node(kind, data, target, mode, uid, gid))
        apply(self.nodes, op)
        self._record(op, covered=durable)

    def drop(self, path):
        op = ("remove", str(path))
        apply(self.nodes, op)
        self._record(op)

    def walk(self, top):
        """Every node at or below `top`, as comparable tuples."""
        return {name: (node.kind, node.data, node.target, node.mode, node.uid, node.gid)
                for name, node in self.nodes.items() if inside(name, top)}


def fork(disk, host):
    """An independent copy of a disk and its host, as they are now, without
    the call log."""
    calls, disk.calls = disk.calls, []
    try:
        copied, copied_host = copy.deepcopy((disk, host))
    finally:
        disk.calls = calls
    copied.on_tick = None
    return copied, copied_host


def kill_points(disk, host, run):
    """Run `run(disk, host)` and return, for each mutating call it made, the
    call and a copy of the disk and host just before it: the state a kill
    there leaves."""
    points = []
    disk.on_tick = lambda call: points.append((call, *fork(disk, host)))
    try:
        run(disk, host)
    finally:
        disk.on_tick = None
    return points


def torn(op, nodes, rng):
    """Part of a copy (some of its nodes) or of a removal (some leaves,
    and a directory only once all below it went); other changes whole."""
    if op[0] == "copy":
        return ("copy", {name: node for name, node in op[1].items() if rng.random() < 0.5})
    if op[0] == "remove":
        gone = set()
        for name in sorted((name for name in nodes if inside(name, op[1])), key=len, reverse=True):
            below = [other for other in nodes if other != name and inside(other, name)]
            if all(other in gone for other in below) and rng.random() < 0.5:
                gone.add(name)
        return ("removed", sorted(gone))
    return op


def op_path(op):
    if op[0] == "rename":
        return op[2]
    if op[0] == "copy":
        return min(op[1]) if op[1] else "/"
    return op[1]


def apply(nodes, op):
    """Apply one change; one whose precondition a crash lost is skipped."""
    kind = op[0]
    if kind == "put":
        if parent(op[1]) in nodes or op[1] == "/":
            nodes[op[1]] = copy.deepcopy(op[2])
    elif kind == "remove":
        for name in [name for name in nodes if inside(name, op[1])]:
            del nodes[name]
    elif kind == "rename":
        _, source, destination = op
        if source not in nodes or destination in nodes or parent(destination) not in nodes:
            return
        for name in sorted(name for name in nodes if inside(name, source)):
            nodes[destination + name[len(source):]] = nodes.pop(name)
    elif kind == "copy":
        for name in sorted(op[1]):
            if parent(name) in nodes:
                nodes[name] = copy.deepcopy(op[1][name])
    elif kind == "removed":
        for name in op[1]:
            nodes.pop(name, None)


def boundary(unit):
    """Whether a unit is a native boundary: outside the target, and loading
    the native table from the rule file when it starts."""
    return unit.endswith("boundary.service")


class FakeHost(engine.Host):
    """systemctl, nft, apparmor_parser and podman over a MemoryDisk: unit
    activity, the live table, the loaded policies and the image store.
    Enabling and disabling the target add and remove its link on the disk,
    as systemctl does, without syncing it. `failures` maps an argv to the
    exit status it returns instead of acting. Like systemctl, it refuses to
    enable, disable or restart a unit whose file is not installed. Like
    SubprocessHost, it runs only the tools `tools` names."""

    def __init__(self, disk, *, system="/", tools=None):
        self.disk = disk
        self.tools = dict(TOOLS if tools is None else tools)
        self.system = system.rstrip("/")
        self.active = set()
        self.table = None
        self.loaded = {}
        self.images = set()
        self.failures = {}
        self.log = []

    def path(self, value):
        return self.system + value

    @property
    def target_link(self):
        return self.path(f"{WANTS}/{engine.TARGET}")

    def enabled(self):
        return self.target_link in self.disk.nodes

    def installed(self, unit):
        return self.path(f"{UNITS}/{unit}") in self.disk.nodes

    def _start(self, unit):
        if unit == engine.TARGET:
            if self.path(f"{UNITS}/{engine.TARGET}") not in self.disk.nodes:
                return 5
            self.active |= {engine.TARGET, "thermite-openhab.service"}
        else:
            self.active.add(unit)
        return 0

    def _configured(self, argv):
        if not self.tools.get(argv[0]):
            raise engine.EngineError(f"no {argv[0]} tool is configured")

    def run(self, argv):
        argv = tuple(str(arg) for arg in argv)
        self._configured(argv)
        self.disk.tick("host", *argv)
        self.log.append(argv)
        if argv in self.failures:
            return self.failures[argv]
        command, *rest = argv
        if command == "systemctl":
            verb, *names = rest
            if verb == "disable" and names == [engine.TARGET]:
                if not self.installed(engine.TARGET):
                    return 1  # Failed to disable unit: Unit file thermite.target does not exist.
                if self.enabled():
                    self.disk.drop(self.target_link)
                return 0
            if verb == "enable" and names == [engine.TARGET]:
                if not self.installed(engine.TARGET):
                    return 1
                if not self.enabled():
                    self.disk.put(self.target_link, "link", target=self.path(f"{UNITS}/{engine.TARGET}"))
                return 0
            if verb == "stop":
                if engine.TARGET in names or engine.SLICE in names:
                    # The boundary is outside the target and its slice.
                    self.active = {unit for unit in self.active if not unit.startswith("thermite") or boundary(unit)}
                self.active -= set(names)
                return 0
            if verb == "start":
                return max((self._start(name) for name in names), default=0)
            if verb == "restart" and len(names) == 1:
                # A boundary unit loads the native table from the rule file.
                rules = self.disk.nodes.get(self.path(RULES))
                if not self.installed(names[0]) or boundary(names[0]) and rules is None:
                    return 5  # Unit not found, or its ExecStart failed.
                if boundary(names[0]):
                    self.table = rules.data.decode()
                self.active.add(names[0])
                return 0
            if verb in ("daemon-reload", "reset-failed"):
                return 0
        if argv[:2] == ("nft", "-f"):
            node = self.disk.nodes.get(argv[2])
            if node is None or self.table is not None:
                return 1  # The fake only loads into an empty table.
            self.table = node.data.decode()
            return 0
        if argv == ("nft", "delete", "table", *engine.TABLE):
            if self.table is None:
                return 1
            self.table = None
            return 0
        if command == "apparmor_parser":
            flag, path = rest
            if flag == "-r":
                node = self.disk.nodes.get(path)
                if node is None:
                    return 1
                self.loaded[path] = node.data
                return 0
            if flag == "-R":
                return 0 if self.loaded.pop(path, None) is not None else 1
        if argv[:5] == ("podman", "image", "rm", "--ignore", "--"):
            self.images.discard(argv[5])
            return 0
        raise AssertionError(f"FakeHost does not know {argv}")

    def capture(self, argv):
        argv = tuple(str(arg) for arg in argv)
        self._configured(argv)
        if argv == ("systemctl", "is-enabled", engine.TARGET):
            if not self.installed(engine.TARGET):
                return 4, "not-found\n"
            return (0, "enabled\n") if self.enabled() else (1, "disabled\n")
        if argv == ("systemctl", "is-active", engine.TARGET):
            return (0, "active\n") if engine.TARGET in self.active else (3, "inactive\n")
        if argv == ("systemctl", "show", "-P", "ActiveState", engine.SLICE):
            return 0, "active\n" if self.members() else "inactive\n"
        if argv == ("nft", "list", "table", *engine.TABLE):
            return (0, self.table) if self.table is not None else (1, "")
        raise AssertionError(f"FakeHost does not know {argv}")

    def members(self):
        return {unit for unit in self.active if unit.startswith("thermite") and unit != engine.TARGET
                and not boundary(unit)}

    def boot(self):
        """Power on after a loss: only what is enabled on disk starts, and
        live state comes back from files."""
        self.active, self.table, self.loaded = set(), None, {}
        nodes = self.disk.nodes
        for unit in [name.rsplit("/", 1)[1] for name in nodes if name.startswith(self.path(f"{WANTS}/"))]:
            if boundary(unit) and self.installed(unit) and self.path(RULES) in nodes:
                self.table = nodes[self.path(RULES)].data.decode()
                self.active.add(unit)
        flag = nodes.get(self.path(APPARMOR))
        if flag is not None and flag.data.strip() == b"Y":
            self.loaded = {name: node.data for name, node in nodes.items()
                           if name.startswith(self.path("/etc/apparmor.d/thermite-"))}
        if self.enabled():
            self._start(engine.TARGET)

    def state(self):
        return {"enabled": self.enabled(), "target": engine.TARGET in self.active, "members": sorted(self.members()),
                "table": self.table, "loaded": dict(sorted(self.loaded.items()))}


# Installations ---------------------------------------------------------------

def unit_text(name, version):
    return f"# {name}, fixture version {version}\n[Unit]\nDescription=Fictional {name}\n".encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


@dataclasses.dataclass
class Version:
    """One installed or desired version: its items and the bytes it writes."""

    files: dict          # path -> (class, bytes, extra fields)
    links: dict          # path -> target
    trees: dict          # path -> (class, {relative: bytes}, extra fields)
    pins: dict
    images: list
    label: str


def version(label, *, units=("thermite-openhab.container",), extra_units=(), apparmor=True, prometheus="one",
            tree_class="tree", shared=False, dropins=()):
    files = {
        f"{UNITS}/{engine.TARGET}": ("unit", unit_text(engine.TARGET, label), {"unit": engine.TARGET}),
        f"{UNITS}/{engine.SLICE}": ("unit", unit_text(engine.SLICE, label), {"unit": engine.SLICE}),
        f"{UNITS}/{BOUNDARY}": ("unit", unit_text(BOUNDARY, label), {"unit": BOUNDARY}),
        RULES: ("nft", f"table inet thermite_native {{ # {label} }}\n".encode(), {"apply_unit": BOUNDARY}),
        "/etc/thermite/node-exporter-containers.conf": ("file", f"containers {label}\n".encode(), {}),
        f"{ROOT}/runtime/deploy/deployed.json": ("file", json.dumps({"version": label}).encode(), {}),
    }
    for name in units:
        files[f"/etc/containers/systemd/{name}"] = ("unit", unit_text(name, label),
                                                    {"unit": name.replace(".container", ".service")})
    for name in extra_units:
        files[f"{UNITS}/{name}"] = ("unit", unit_text(name, label), {"unit": name})
    for name in dropins:  # In a directory the version creates: thermite-x.service.d/50-y.conf
        files[f"{UNITS}/{name}"] = ("unit", unit_text(name, label), {"unit": name.split("/")[0][:-2]})
    if apparmor:
        files[POLICY] = ("apparmor", f"profile thermite-node-exporter {{ # {label} }}\n".encode(), {})
    links = {f"{WANTS}/{BOUNDARY}": f"{UNITS}/{BOUNDARY}"}
    for name in extra_units:
        if name.endswith(".path"):
            links[f"{UNITS}/thermite.target.wants/{name}"] = f"{UNITS}/{name}"
    trees = {
        f"{ROOT}/runtime/userdata": (tree_class, {"jsondb/things.json": f"things {label}".encode(),
                                                  "etc/version.properties": f"openhab {label}".encode()}, {}),
        f"{ROOT}/access/state": (tree_class, {"identity.sqlite3": f"identity {label}".encode()}, {}),
        f"{ROOT}/runtime/logs": ("shared", {"openhab.log": b"log line\n"}, {}),
        f"{ROOT}/runtime/monitoring/prometheus": ("shared-pinned", {"chunks/000001": f"samples {prometheus}".encode()},
                                                  {"pin": PROMETHEUS, "value": f"sha256:{prometheus}"}),
        f"{ROOT}/access/releases": ("versioned", {}, {}),
    }
    if shared:
        trees = {path: ("shared", content, {}) for path, (_, content, _) in trees.items()}
    return Version(files, links, trees, {PROMETHEUS: f"sha256:{prometheus}"},
                   [f"quay.io/prometheus/prometheus@sha256:{prometheus}"], label)


def desired_items(new: Version, *, purge=False):
    if purge:
        return [{"path": f"{ROOT}/runtime", "class": "tree-move"}, {"path": f"{ROOT}/access", "class": "tree-move"}]
    items = [{"path": path, "class": cls, **fields} for path, (cls, _, fields) in new.files.items()]
    items += [{"path": path, "class": "link", "target": target} for path, target in new.links.items()]
    items += [{"path": path, "class": cls, **fields} for path, (cls, _, fields) in new.trees.items()]
    return items


def empty(disk, *, apparmor=True):
    """A host Thermite was never installed on, its directories present."""
    for directory in (UNITS, WANTS, f"{UNITS}/thermite.target.wants", "/etc/containers/systemd", "/etc/thermite",
                      "/etc/apparmor.d", ROOT, f"{ROOT}/runtime/deploy", f"{ROOT}/runtime/monitoring", f"{ROOT}/access"):
        disk.put(directory, "dir", durable=True)
    if apparmor:
        disk.put(APPARMOR, data=b"Y\n", durable=True)
    disk.settle()


def install(disk, host, old: Version, *, release=OLD, apparmor=True, manifest=True):
    """A host running `old`, installed by an earlier transaction, all durable."""
    empty(disk, apparmor=apparmor)
    items = []
    for path, (cls, data, fields) in old.files.items():
        disk.put(path, data=data, durable=True)
        items.append({"path": path, "class": cls, "sha256": digest(data), "mode": 0o644, "uid": 0, "gid": 0,
                      **fields})
    for path, target in old.links.items():
        disk.put(path, "link", target=target, durable=True)
        items.append({"path": path, "class": "link", "target": target})
    for path, (cls, content, fields) in old.trees.items():
        disk.put(path, "dir", mode=0o750, uid=980, gid=980, durable=True)
        for name, data in content.items():
            disk.put(f"{path}/{name}", data=data, mode=0o640, uid=980, gid=980, durable=True)
        if cls == "versioned":
            disk.put(f"{path}/{release}/config.json", data=b"{}", durable=True)
            items.append({"path": path, "class": cls, "current": release, "previous": None})
        elif cls == "shared-pinned":
            items.append({"path": path, "class": cls, "pin": fields["pin"]})
        else:
            items.append({"path": path, "class": cls})
    disk.put(f"{WANTS}/{engine.TARGET}", "link", target=f"{UNITS}/{engine.TARGET}", durable=True)
    data = {"format": 2, "installation": ROOT, "transaction": release, "kept": release,
            "committed_at": "2026-10-20T08:10:00Z", "installed": True,
            "release": {"label": old.label}, "pins": old.pins, "tools": TOOLS, "items": items,
            "images": {"current": old.images, "previous": []},
            "application": {"current": f"sha256:{old.label}", "previous": None}}
    data = engine.validate_manifest(data, root=ROOT)
    if manifest:
        disk.put("/etc/thermite/installed.json", data=engine.render_manifest(data), durable=True)
    host.active = {engine.TARGET, "thermite-openhab.service", BOUNDARY}
    host.table = old.files[RULES][1].decode()
    if apparmor and POLICY in old.files:
        host.loaded = {POLICY: old.files[POLICY][1]}
    host.images = set(old.images)
    disk.settle()
    return data


def switch(disk, host, new: Version, id=ID):
    """The new version's install while Thermite is stopped: plain writes that
    nothing syncs, its live state, then the start of the target."""
    for path, (cls, data, fields) in new.files.items():
        disk.tick("switch", path)
        disk.put(path, data=data)
    for path, target in new.links.items():
        disk.tick("switch", path)
        if path not in disk.nodes:
            disk.put(path, "link", target=target)
    for path, (cls, content, fields) in new.trees.items():
        if cls == "shared":
            continue
        disk.tick("switch", path)
        if path not in disk.nodes:
            disk.put(path, "dir", mode=0o750, uid=980, gid=980)
        for name, data in content.items():
            disk.put(f"{path}/{name}", data=data, mode=0o640, uid=980, gid=980)
        if cls == "versioned":
            disk.put(f"{path}/{id}/config.json", data=b'{"new": true}')
    apply = new.files[RULES][2]["apply_unit"] if RULES in new.files else BOUNDARY
    for argv in (("systemctl", "daemon-reload"), ("systemctl", "restart", apply),
                 *((("apparmor_parser", "-r", POLICY),) if POLICY in new.files and disk.nodes.get(APPARMOR) else ()),
                 ("systemctl", "start", engine.TARGET)):
        assert host.run(argv) == 0, argv


def release(new: Version):
    return {"release": {"label": new.label}, "pins": new.pins, "images": new.images,
            "application": f"sha256:{new.label}"}


def managed_paths(*versions, id=ID, pinned=False):
    """Every path a transaction must leave all-old or all-new. Shared data
    (logs, samples) is outside, unless its pin changes and it is copied."""
    paths, shared = {"/etc/thermite/installed.json"}, set()
    for each in versions:
        paths |= set(each.files) | set(each.links) | set(each.trees)
        paths |= {f"{path}/{id}" for path, (cls, _, _) in each.trees.items() if cls == "versioned"}
        shared |= {path for path, (cls, _, _) in each.trees.items()
                   if cls == "shared" or cls == "shared-pinned" and not pinned}
    return sorted(paths - shared)


#: Where Thermite writes: anything appearing or vanishing here shows.
TOPS = (UNITS, "/etc/containers/systemd", "/etc/thermite", "/etc/apparmor.d", "/usr/share/doc/thermite", ROOT)


def world(disk, host, paths):
    """The managed set, byte for byte; what exists where Thermite writes
    (outside its transactions), so that a directory left behind shows; and
    the live state Thermite runs on."""
    found = {}
    for path in paths:
        found.update(disk.walk(path))
    layout = {name: node.kind for name, node in disk.nodes.items()
              if any(inside(name, top) for top in TOPS) and not inside(name, f"{ROOT}/.transaction")}
    return {"files": found, "layout": layout, "host": host.state()}
