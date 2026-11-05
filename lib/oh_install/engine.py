"""Install transactions: the installed manifest, its classes and the journal.

Root runs a private copy of this file with `/usr/bin/python3 -I -S`, so it
imports the standard library only; operator code and tests import it as a
module. It is the only code that sets an installed version aside, puts it
back, or writes /etc/thermite/installed.json. How each item is collected and
reinstated is fixed here, by the item's class; a release supplies data
(paths and classes), never commands.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shlex
import stat
import subprocess
import sys
import time

#: Everything the engine touches outside the installation root is below
#: this directory. Tests run a copy with it rewritten to a scratch tree.
SYSTEM = Path("/")
#: The journal format this engine writes, and the only one it reads. The
#: engine that begins a transaction is pinned beside its journal and runs
#: every later verb on it, so no engine reads another's journals.
FORMAT = 1
#: The installed manifest's format.
MANIFEST_FORMAT = 2
#: The installed manifest, relative to SYSTEM.
MANIFEST = "etc/thermite/installed.json"
#: Room kept free beyond a full, uncloned copy of what is set aside.
MARGIN = 256 * 1024 * 1024
#: The table the native boundary unit loads from its nft item.
TABLE = ("inet", "thermite_native")
#: The target whose enablement is the boot guard, and the slice every unit
#: runs in.
TARGET = "thermite.target"
SLICE = "thermite.slice"
#: Tool names a manifest may give absolute paths for; the engine runs no other
#: program. Those that may be null are only needed when an item uses them.
TOOLS = ("systemctl", "cp", "sync", "nft", "apparmor_parser", "podman")
OPTIONAL_TOOLS = ("nft", "apparmor_parser", "podman")
#: The directories a tool may be named in, by its own name. Tests run a copy
#: with a scratch directory added.
TOOL_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")
#: install | upgrade | clean | uninstall | purge | revert. Uninstall and
#: purge record installed=false and leave the boot target disabled. An
#: uninstall keeps all state: every installed item below the root stays in
#: place, recorded as shared. A purge removes runtime/ and access/ whole,
#: whatever the desired set lists, then the installed manifest and every
#: transaction, its own backup included: nothing Thermite made is left.
KINDS = ("install", "upgrade", "clean", "revert", "uninstall", "purge")
UNINSTALLING = ("uninstall", "purge")

ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
UNIT = re.compile(r"thermite[A-Za-z0-9@._:-]*")
PIN = re.compile(r"[a-z0-9_]{1,64}")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,511}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class EngineError(ValueError):
    """A refusal or failure with a message for the owner."""


@dataclasses.dataclass(frozen=True)
class Class:
    """How the engine treats one class of item.

    `shape` is what the path must be on disk: a regular file, a symlink or a
    directory. `aside` is the directory under the transaction that holds
    what collect sets aside. `place` says where items of the class may
    live: system paths outside the installation root, or below it.
    `recorded` lists the fields commit records for the class, `declared`
    those a desired item may carry, and `required` the declared ones it
    must."""

    name: str
    shape: str
    aside: str | None
    place: str
    recorded: tuple[str, ...] = ()
    declared: tuple[str, ...] = ()
    required: tuple[str, ...] = ()


FILE_META = ("sha256", "mode", "uid", "gid")
#: The class table. Collect, reinstatement and commit for each are in
#: collect_action() and the Engine; docs/installation.md explains them.
CLASSES = {c.name: c for c in (
    # A root-owned regular file Thermite owns outright.
    Class("file", "file", "files", "any", FILE_META),
    # A file systemd loads: a unit, quadlet or drop-in. Rolling one back
    # ends with one daemon-reload.
    Class("unit", "file", "files", "system", (*FILE_META, "unit"), ("unit",), ("unit",)),
    # An enablement symlink in a .wants directory, never the boot target's
    # own: only the guard and commit touch that one, through systemctl.
    Class("link", "link", "links", "system", ("target",), ("target",)),
    # The rule file whose table apply_unit loads; the live table is put back
    # with the file.
    Class("nft", "file", "files", "system", (*FILE_META, "apply_unit"), ("apply_unit",), ("apply_unit",)),
    # A policy loaded into the kernel while AppArmor is enabled.
    Class("apparmor", "file", "files", "system", FILE_META),
    # State a version modifies or migrates: copied aside, put back whole.
    Class("tree", "dir", "trees", "root"),
    # State replaced wholesale (purge, a clean install): moved aside, and the
    # aside deleted at commit. What the new version creates there is
    # recorded as a tree.
    Class("tree-move", "dir", "trees", "root"),
    # Side-by-side releases; the transaction adds the child named by its id.
    Class("versioned", "dir", "trees", "root", ("current", "previous")),
    # Bulk data and logs, left in place.
    Class("shared", "dir", None, "root"),
    # Shared unless the named pin changes; then copied aside like a tree.
    Class("shared-pinned", "dir", "trees", "root", ("pin",), ("pin", "value"), ("pin", "value")),
)}
#: Classes whose items are directories; nothing lies inside one.
TREES = frozenset(name for name, c in CLASSES.items() if c.shape == "dir")

#: Durable journal phases, in order, then those of a rollback.
PHASES = ("planned", "guarded", "collecting", "switching", "committed", "kept",
          "rolling-back", "rolled-back", "stuck")
#: Phases after which the transaction needs nothing more.
TERMINAL = ("kept", "rolled-back")
#: Written beside the journal, holding the phase, once a transaction reaches
#: a terminal phase. It is the one part of a transaction every engine reads,
#: whatever its journal format: one marked so blocks no later begin, and the
#: next commit prunes it.
FINISHED = "terminal"
#: A purge renames its own transaction to this prefix, then removes it.
PURGED = ".purged-"
#: Each phase and the phases a verb may move it to.
NEXT = {
    "planned": ("guarded", "rolling-back"),
    "guarded": ("collecting", "rolling-back"),
    "collecting": ("switching", "rolling-back"),
    "switching": ("committed", "rolling-back"),
    "committed": ("kept",),
    "kept": (),
    "rolling-back": ("rolled-back", "stuck"),
    "rolled-back": (),
    "stuck": ("rolling-back",),
}
#: What collect does with an entry: copy it aside (the new version keeps and
#: may migrate it), move it aside (removed or replaced wholesale), note it as
#: added (rollback displaces it), or leave it.
ACTIONS = ("copy", "move", "add", "leave")
#: Entry states. Collect: planned, then partial (a copy) or moving, then
#: aside. Rollback: displacing (the live path to failed/), returning (the
#: aside to the live path), restored; or discarded, for a copy taken while
#: the live path was never touched.
STATES = ("planned", "partial", "moving", "aside", "displacing", "returning", "restored", "discarded")


def checked_path(value, what="path") -> PurePosixPath:
    """An absolute, normalised path other than /, or EngineError."""
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise EngineError(f"{what} must be an absolute path: {value!r}")
    path = PurePosixPath(value)
    if str(path) != value or ".." in path.parts or "." in path.parts or path == PurePosixPath("/"):
        raise EngineError(f"{what} must be normalised, without . or ..: {value!r}")
    return path


def below(path, parent) -> bool:
    """Whether `path` is strictly inside `parent`; both are normalised."""
    parent = str(parent)
    return str(path).startswith(parent if parent.endswith("/") else parent + "/")


def managed(root, path) -> bool:
    """Whether the item at `path` is a managed root file rather than state.

    Root files outside the installation root are written only by an install,
    so one that drifted from the manifest refuses the transaction. Items
    below the root are state that Thermite's own commands change between
    transactions (a deploy rewrites runtime/deploy/deployed.json): they are
    set aside as found."""
    return not (PurePosixPath(path) == PurePosixPath(root) or below(path, root))


def allowed(root, path, cls, system=SYSTEM) -> bool:
    """Whether the fixed allowlist lets an item of class `cls` live at `path`.

    System paths are Thermite-named entries in the directories its units,
    rules, policies and documentation go to. Below the installation root
    only runtime/ and access/ hold items."""
    path, root, system = PurePosixPath(path), PurePosixPath(root), PurePosixPath(system)
    place = CLASSES[cls].place
    if path == root or below(path, root):
        if place == "system":
            return False
        parts = path.relative_to(root).parts
        return bool(parts) and parts[0] in ("runtime", "access")
    if place == "root" or not (path == system or below(path, system)):
        return False
    parts = path.relative_to(system).parts
    if parts == ("etc", "thermite-native.nft"):
        return cls in ("file", "nft")
    if parts[:3] == ("etc", "systemd", "system") and len(parts) > 3:
        if parts[3].endswith(".wants") and len(parts) == 5:
            return (parts[3] in ("thermite.target.wants", "multi-user.target.wants")
                    and parts[4].startswith("thermite") and cls == "link")
        return parts[3].startswith("thermite") and cls in ("unit", "file")
    if parts[:3] == ("etc", "containers", "systemd") and len(parts) == 4:
        return parts[3].startswith("thermite") and cls in ("unit", "file")
    if parts[:2] == ("etc", "thermite") and len(parts) > 2:
        # The manifest is the engine's own record, never an item.
        return not parts[2].startswith(("installed.json", ".installed.json")) and cls == "file"
    if parts[:2] == ("etc", "apparmor.d") and len(parts) == 3:
        return parts[2].startswith("thermite-") and cls in ("apparmor", "file")
    if parts[:4] == ("usr", "share", "doc", "thermite") and len(parts) > 4:
        return cls == "file"
    return False


def _keys(value, what, required, optional=()) -> dict:
    if not isinstance(value, dict):
        raise EngineError(f"{what} must be a JSON object")
    missing = [key for key in required if key not in value]
    unknown = sorted(set(value) - set(required) - set(optional))
    if missing or unknown:
        problems = [*(["missing " + ", ".join(missing)] if missing else []),
                    *(["unknown " + ", ".join(unknown)] if unknown else [])]
        raise EngineError(f"{what}: {'; '.join(problems)}")
    return value


def _string(value, what, pattern=None) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or pattern and not pattern.fullmatch(value):
        raise EngineError(f"{what} is not valid: {value!r}")
    return value


def _number(value, what, maximum=2 ** 32) -> int:
    if type(value) is not int or not 0 <= value < maximum:
        raise EngineError(f"{what} is not valid: {value!r}")
    return value


def check_item(item, root, system=SYSTEM, *, recorded) -> dict:
    """One item, desired (as a step declares it) or recorded (as commit
    wrote it), normalised; EngineError names what is wrong."""
    if not isinstance(item, dict) or item.get("class") not in CLASSES:
        raise EngineError(f"unknown item class: {item.get('class') if isinstance(item, dict) else item!r}")
    cls = CLASSES[item["class"]]
    path = str(checked_path(item.get("path"), "item path"))
    what = f"{cls.name} item {path}"
    if recorded:
        if cls.name == "tree-move":
            raise EngineError(f"{what}: a tree-move item is recorded as a tree")
        _keys(item, what, ("path", "class", *cls.recorded))
    else:
        _keys(item, what, ("path", "class", *cls.required), cls.declared)
    if not allowed(root, path, cls.name, system):
        raise EngineError(f"{what} is outside what Thermite may own")
    if "sha256" in item:
        _string(item["sha256"], f"{what} sha256", SHA256)
    for key, maximum in (("mode", 0o10000), ("uid", 2 ** 32), ("gid", 2 ** 32)):
        if key in item:
            _number(item[key], f"{what} {key}", maximum)
    for key in ("unit", "apply_unit"):
        if key in item:
            _string(item[key], f"{what} {key}", UNIT)
    if "apply_unit" in item and not item["apply_unit"].endswith(".service"):
        # Restarting it reloads the table; the target and slice are never
        # restarted, only the guard and commit touch the target.
        raise EngineError(f"{what} apply_unit must be a Thermite service: {item['apply_unit']!r}")
    if "target" in item:
        checked_path(item["target"], f"{what} target")
    if "pin" in item:
        _string(item["pin"], f"{what} pin", PIN)
    if "value" in item:
        _string(item["value"], f"{what} pin value")
    for key in ("current", "previous"):
        if key in item and not (key == "previous" and item[key] is None):
            _string(item[key], f"{what} {key}", ID)
    return dict(item)


def check_items(items, root, system=SYSTEM, *, recorded) -> list[dict]:
    """A set of items, sorted by path: no path twice, and nothing inside a
    tree-shaped item, so each byte belongs to one item."""
    if not isinstance(items, list):
        raise EngineError("items must be a list")
    checked = sorted((check_item(item, root, system, recorded=recorded) for item in items),
                     key=lambda item: item["path"])
    for first, second in zip(checked, checked[1:]):
        if first["path"] == second["path"]:
            raise EngineError(f"{first['path']} is declared twice")
    trees = [item["path"] for item in checked if item["class"] in TREES]
    for item in checked:
        inside = [tree for tree in trees if below(item["path"], tree)]
        if inside:
            raise EngineError(f"{item['path']} lies inside the {inside[0]} item; items never nest")
    return checked


def _images(value, what) -> list[str]:
    if not isinstance(value, list):
        raise EngineError(f"{what} must be a list")
    return [_string(ref, what, IMAGE) for ref in value]


def _pins(value) -> dict:
    if not isinstance(value, dict):
        raise EngineError("pins must be a JSON object")
    for name, digest in value.items():
        _string(name, "pin name", PIN)
        _string(digest, f"pin {name}")
    return dict(value)


def check_tools(value) -> dict:
    """The tool table: absolute paths for the required tools, null allowed
    for the optional ones. Custody is checked where they run."""
    _keys(value, "tools", TOOLS)
    for name in TOOLS:
        if value[name] is None and name in OPTIONAL_TOOLS:
            continue
        if checked_path(value[name], f"tool {name}").name != name:
            raise EngineError(f"tool {name} must be a program named {name}: {value[name]!r}")
    return dict(value)


def validate_manifest(data, *, root=None, system=SYSTEM) -> dict:
    """The installed manifest, checked in full. `root`, when given, must be
    its installation."""
    _keys(data, "installed manifest", ("format", "installation", "transaction", "committed_at", "installed",
                                       "release", "pins", "tools", "items", "images", "application", "kept"))
    if data["format"] != MANIFEST_FORMAT:
        raise EngineError(f"installed manifest format {data['format']!r} is not {MANIFEST_FORMAT}")
    installation = str(checked_path(data["installation"], "installation"))
    if root is not None and installation != str(root):
        raise EngineError(f"the installed manifest is for {installation}, not {root}")
    _string(data["transaction"], "manifest transaction", ID)
    _string(data["kept"], "manifest kept transaction", ID)
    _string(data["committed_at"], "manifest committed_at")
    if type(data["installed"]) is not bool:
        raise EngineError("manifest installed must be true or false")
    if not isinstance(data["release"], dict):
        raise EngineError("manifest release must be a JSON object")
    _keys(data["images"], "manifest images", ("current", "previous"))
    _keys(data["application"], "manifest application", ("current", "previous"))
    for key in ("current", "previous"):
        _images(data["images"][key], f"manifest images {key}")
        if data["application"][key] is not None:
            _string(data["application"][key], f"manifest application {key}")
    return {**data, "pins": _pins(data["pins"]), "tools": check_tools(data["tools"]),
            "items": check_items(data["items"], installation, system, recorded=True)}


def parse_manifest(raw: bytes, *, root=None, system=SYSTEM) -> dict:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise EngineError(f"the installed manifest is not JSON: {error}") from None
    return validate_manifest(data, root=root, system=system)


def render_manifest(manifest) -> bytes:
    """The manifest's bytes: stable key order, one trailing newline."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def validate_desired(data, root, system=SYSTEM) -> dict:
    """What the operator asks a transaction to install: its kind, the tools
    the engine may run, the desired items, image refs this attempt pulled
    (pruned if it rolls back and a later one commits) and any extra room it
    needs beyond the copies (the new gateway release)."""
    _keys(data, "desired set", ("kind", "tools", "items"), ("pulled", "extra"))
    if data["kind"] not in KINDS:
        raise EngineError(f"unknown transaction kind {data['kind']!r}")
    return {"kind": data["kind"], "tools": check_tools(data["tools"]),
            "items": check_items(data["items"], root, system, recorded=False),
            "pulled": _images(data.get("pulled", []), "pulled images"),
            "extra": _number(data.get("extra", 0), "extra room", 2 ** 63)}


def validate_release(data) -> dict:
    """What commit records about the version just installed."""
    _keys(data, "release", ("release", "pins", "images"), ("application",))
    if not isinstance(data["release"], dict):
        raise EngineError("release must be a JSON object")
    application = data.get("application")
    if application is not None:
        _string(application, "application digest")
    return {"release": data["release"], "pins": _pins(data["pins"]),
            "images": _images(data["images"], "release images"), "application": application}


def validate_operator(data) -> dict:
    """The operator's side of the transaction, kept for status and recovery."""
    _keys(data, "operator", ("uid", "site", "record"), ("head_before", "head_after"))
    _number(data["uid"], "operator uid")
    checked_path(data["site"], "operator site")
    checked_path(data["record"], "operator record")
    for key in ("head_before", "head_after"):
        if data.get(key) is not None:
            _string(data[key], f"operator {key}", re.compile(r"[0-9a-f]{40,64}"))
    return {"head_before": None, "head_after": None, **data}


def needed_tools(entries, apparmor) -> set[str]:
    """The tools a transaction's entries need: an nft item's live table is
    snapshotted and reloaded, and a policy is loaded or removed while
    AppArmor is enabled."""
    needed = {"systemctl", "cp", "sync"}
    if any(entry.cls == "nft" and entry.action != "leave" for entry in entries):
        needed.add("nft")
    if apparmor and any(entry.cls == "apparmor" and entry.action != "leave" for entry in entries):
        needed.add("apparmor_parser")
    return needed


def collect_action(current, desired, exists, pin_changed=False) -> str:
    """What collect does with one path: `current` and `desired` are its class
    in the installed manifest and the desired set (None when absent there),
    `exists` whether it is on disk now.

    Move aside what the new version removes, copy aside what it keeps and
    may modify or migrate, note what it adds, and leave what is shared.
    """
    if desired is None:
        if current in (None, "shared"):
            return "leave"
        return "move" if exists else "leave"
    if desired == "shared":
        return "leave"
    if desired == "shared-pinned":
        return "copy" if exists and current is not None and pin_changed else "leave"
    if desired == "versioned":
        # The child named by the transaction id is the entry, unless the
        # whole directory is new.
        return "leave" if exists else "add"
    if not exists:
        return "add"
    return "move" if desired == "tree-move" else "copy"


@dataclasses.dataclass
class Entry:
    """One path the transaction collects or adds, and how far it got."""

    n: int
    path: str
    cls: str
    action: str
    existed: bool
    # Set aside because the new version drops it: its removal effects
    # (unloading a policy, deleting a table) apply at collect.
    removed: bool = False
    aside: str | None = None
    state: str = "planned"
    # What was live when collected: verified after reinstatement.
    meta: dict | None = None
    apply_unit: str | None = None

    def to_json(self) -> dict:
        return {"n": self.n, "path": self.path, "class": self.cls, "action": self.action, "existed": self.existed,
                "removed": self.removed, "aside": self.aside, "state": self.state, "meta": self.meta,
                "apply_unit": self.apply_unit}

    @classmethod
    def from_json(cls, data) -> Entry:
        _keys(data, "journal entry", ("n", "path", "class", "action", "existed", "removed", "aside", "state",
                                       "meta", "apply_unit"))
        if data["class"] not in CLASSES or data["action"] not in ACTIONS or data["state"] not in STATES:
            raise EngineError(f"journal entry {data.get('n')!r} is not valid")
        checked_path(data["path"], "journal entry path")
        if data["aside"] is not None and not re.fullmatch(r"(files|links|trees)/\d+", data["aside"]):
            raise EngineError(f"journal entry {data['n']!r} aside is not valid")
        fields = {**data}
        fields["cls"] = fields.pop("class")
        return cls(**fields)


@dataclasses.dataclass
class Journal:
    """The write-ahead record of one transaction, saved before every step it
    announces; recovery confirms each step from disk, never from this alone."""

    id: str
    kind: str
    installation: str
    engine_sha256: str
    engine_path: str
    tools: dict
    before: dict
    current: dict | None
    desired: list
    operator: dict
    space: dict
    entries: list[Entry]
    pulled: list[str]
    phase: str = "planned"
    phases: list = dataclasses.field(default_factory=list)
    live: dict = dataclasses.field(default_factory=lambda: {"nft_checked": False, "nft": None})
    rollback_from: str | None = None
    manifest: dict | None = None
    outcome: dict | None = None
    format: int = FORMAT

    def advance(self, phase, at) -> None:
        if phase != self.phase and phase not in NEXT[self.phase]:
            raise EngineError(f"transaction {self.id} is {self.phase}; it cannot become {phase}")
        if phase != self.phase:
            self.phase = phase
            self.phases.append([phase, at])

    @property
    def open(self) -> bool:
        return self.phase not in TERMINAL

    def to_json(self) -> dict:
        data = {field.name: getattr(self, field.name) for field in dataclasses.fields(self)}
        data["entries"] = [entry.to_json() for entry in self.entries]
        return data

    def render(self) -> bytes:
        # Saved tens of times per transaction; status and rollback --print
        # are what people read.
        return (json.dumps(self.to_json(), sort_keys=True, separators=(",", ":")) + "\n").encode()

    @classmethod
    def parse(cls, raw: bytes) -> Journal:
        """A journal this engine wrote. A foreign format is refused, naming
        the engine that wrote it: no engine reads another's journals."""
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise EngineError(f"the journal is not JSON: {error}") from None
        if not isinstance(data, dict):
            raise EngineError("the journal is not a JSON object")
        if data.get("format") != FORMAT:
            raise EngineError(f"the journal has format {data.get('format')!r}, not {FORMAT}; "
                              f"run the engine that wrote it: {data.get('engine_path')}")
        names = [field.name for field in dataclasses.fields(cls)]
        _keys(data, "journal", names)
        _string(data["id"], "journal id", ID)
        if data["kind"] not in KINDS or data["phase"] not in PHASES:
            raise EngineError("the journal's kind or phase is not valid")
        _string(data["engine_sha256"], "journal engine_sha256", SHA256)
        fields = {name: data[name] for name in names}
        fields["entries"] = [Entry.from_json(entry) for entry in data["entries"]]
        if [entry.n for entry in fields["entries"]] != list(range(len(fields["entries"]))):
            raise EngineError("journal entries are not numbered in order")
        return cls(**fields)


@dataclasses.dataclass(frozen=True)
class Stat:
    """What the engine reads of a path, without following a symlink: its
    shape (file, link, dir or other), permissions, owner, size and device."""

    kind: str
    mode: int
    uid: int
    gid: int
    size: int
    dev: int


class Disk:
    """The filesystem as the engine uses it. Paths are absolute and nothing
    follows a symlink. `owner` is the uid of what the engine creates: root,
    when root runs it."""

    owner = 0

    def lstat(self, path) -> Stat | None:
        raise NotImplementedError

    def read_bytes(self, path, limit) -> bytes:
        raise NotImplementedError

    def readlink(self, path) -> str:
        raise NotImplementedError

    def listdir(self, path) -> list[str]:
        raise NotImplementedError

    def allocated(self, path) -> int:
        """Bytes a full, uncloned copy of the path takes."""
        raise NotImplementedError

    def available(self, path) -> int:
        """Bytes free for root on the path's filesystem."""
        raise NotImplementedError

    def write_atomic(self, path, data, mode) -> None:
        """Replace the file: a temporary file, fsync, rename, directory fsync."""
        raise NotImplementedError

    def mkdir(self, path, mode) -> None:
        raise NotImplementedError

    def rename(self, source, destination) -> None:
        """rename(2): OSError(EXDEV) across filesystems; never replaces."""
        raise NotImplementedError

    def copy(self, source, destination) -> None:
        """`cp -a --reflink=auto`: owners, modes, links and sparseness kept."""
        raise NotImplementedError

    def remove_tree(self, path) -> None:
        """Remove the path and anything below it, never crossing a mount."""
        raise NotImplementedError

    def sync_fs(self, path) -> None:
        """`sync -f`: everything written to the path's filesystem is durable."""
        raise NotImplementedError

    def fsync_dir(self, path) -> None:
        raise NotImplementedError

    def lock(self, path):
        """A context holding the exclusive lock on a directory."""
        raise NotImplementedError


class Host:
    """The programs the engine runs, named by their tools key: systemctl,
    nft, apparmor_parser and podman. `run` is for changes; `capture` only
    reads."""

    def run(self, argv) -> int:
        raise NotImplementedError

    def capture(self, argv) -> tuple[int, str]:
        raise NotImplementedError


class SpaceShort(EngineError):
    """Too little room to set the current version aside; nothing changed."""

    def __init__(self, space, root):
        super().__init__(describe_space(space, root))
        self.space = space


def partial(path) -> Path:
    """Where a copy is built before its rename: a hidden sibling, so that no
    loader (systemd, AppArmor) picks up a half-written file."""
    path = Path(path)
    return path.with_name("." + path.name + ".partial")


def _size(count) -> str:
    for unit, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}"
    return "<1 MiB" if count else "0 MiB"


def describe_space(space, root) -> str:
    """The room table the owner sees before anything stops, and on refusal."""
    lines = [f"Room to set aside the current version, on {root} (free {_size(space['available'])}):"]
    lines += [f"  {label:<36}{_size(count):>10}" for label, count in space["items"]]
    if space["extra"]:
        lines.append(f"  {'the new release':<36}{_size(space['extra']):>10}")
    lines.append(f"  {'margin':<36}{_size(space['margin']):>10}")
    lines.append(f"  {'needed':<36}{_size(space['required']):>10}  (a full copy; cloning may use less)")
    if space["required"] > space["available"]:
        lines.append("Not enough space: nothing was stopped or changed. "
                     f"Free {_size(space['required'] - space['available'])}, then rerun.")
    return "\n".join(lines)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Engine:
    """The transaction verbs over a Disk and a Host.

    `source` is this engine's own bytes: begin pins them beside the journal,
    and every later verb refuses a journal another engine began. `system` is
    the directory system paths are below (/ outside tests)."""

    def __init__(self, disk: Disk, host: Host, root, *, source=b"", system=SYSTEM, clock=_now, say=None):
        self.disk, self.host = disk, host
        self.root = Path(str(checked_path(str(root), "installation root")))
        self.system = Path(system)
        self.source = source
        self.clock = clock
        self.say = say or (lambda text: None)

    @property
    def transactions(self) -> Path:
        return self.root / ".transaction"

    @property
    def manifest_path(self) -> Path:
        return self.system / MANIFEST

    def directory(self, id) -> Path:
        return self.transactions / _string(id, "transaction id", ID)

    # Reading -------------------------------------------------------------

    def _safe(self, path) -> None:
        """Every existing ancestor is a real directory, never a symlink."""
        for ancestor in reversed(Path(path).parents):
            found = self.disk.lstat(ancestor)
            if found is None:
                return
            if found.kind != "dir":
                raise EngineError(f"{path}: {ancestor} is not a directory (a symlink is never followed)")

    def _private(self, path, kind) -> Stat:
        found = self.disk.lstat(path)
        if found is None or found.kind != kind or found.uid != self.disk.owner or found.mode & 0o077:
            raise EngineError(f"{path} is not private to root")
        return found

    def check_root(self) -> None:
        """The installation root is root's own directory, reached without
        symlinks, that no one else can write."""
        self._safe(self.root)
        found = self.disk.lstat(self.root)
        if found is None or found.kind != "dir" or found.uid != self.disk.owner or found.mode & 0o022:
            raise EngineError(f"{self.root} must be a directory owned by root that only root can write")

    def installed(self) -> tuple[dict | None, bytes | None]:
        """The installed manifest and its bytes, or (None, None)."""
        self._safe(self.manifest_path)
        if self.disk.lstat(self.manifest_path) is None:
            return None, None
        raw = self.disk.read_bytes(self.manifest_path, 1024 * 1024)
        return parse_manifest(raw, root=self.root, system=self.system), raw

    def ids(self) -> list[str]:
        if self.disk.lstat(self.transactions) is None:
            return []
        self._private(self.transactions, "dir")
        return sorted(name for name in self.disk.listdir(self.transactions) if ID.fullmatch(name))

    def read(self, id) -> Journal | None:
        """The transaction's journal; None when begin never wrote one."""
        directory = self.directory(id)
        if self.disk.lstat(directory / "journal.json") is None:
            return None
        self._private(directory, "dir")
        self._private(directory / "journal.json", "file")
        journal = Journal.parse(self.disk.read_bytes(directory / "journal.json", 16 * 1024 * 1024))
        if journal.id != id or journal.installation != str(self.root):
            raise EngineError(f"{directory}/journal.json is not transaction {id} of {self.root}")
        return journal

    def _open(self, id) -> Journal:
        journal = self.read(id)
        if journal is None:
            raise EngineError(f"there is no transaction {id} under {self.transactions}")
        if hashlib.sha256(self.source).hexdigest() != journal.engine_sha256:
            raise EngineError(f"transaction {id} was begun by another engine; run {journal.engine_path}")
        return journal

    def save(self, journal) -> None:
        self.disk.write_atomic(self.directory(journal.id) / "journal.json", journal.render(), 0o600)

    def _advance(self, journal, phase) -> None:
        journal.advance(phase, self.clock())
        self.save(journal)
        if phase in TERMINAL:
            self._mark(journal)

    def _mark(self, journal) -> None:
        """The format-independent record that the transaction is finished."""
        path = self.directory(journal.id) / FINISHED
        if self.disk.lstat(path) is None:
            self.disk.write_atomic(path, f"{journal.phase}\n".encode(), 0o600)

    def finished(self, id) -> bool:
        """Whether the transaction is marked finished, by any engine."""
        found = self.disk.lstat(self.directory(id) / FINISHED)
        return found is not None and found.kind == "file"

    def _meta(self, path, cls) -> dict:
        found = self.disk.lstat(path)
        shape = CLASSES[cls].shape
        if found is None or found.kind != shape:
            raise EngineError(f"{path} is not a {shape} as its {cls} class needs")
        if shape == "link":
            return {"target": self.disk.readlink(path)}
        meta = {"mode": found.mode, "uid": found.uid, "gid": found.gid}
        if shape == "file":
            meta["sha256"] = hashlib.sha256(self.disk.read_bytes(path, 64 * 1024 * 1024)).hexdigest()
        return meta

    def _drift(self, item) -> None:
        """Refuse a managed root file that is not as the manifest recorded."""
        found = self._meta(item["path"], item["class"])
        recorded = {key: item[key] for key in found if key in item}
        if found != recorded:
            changed = ", ".join(sorted(key for key in found if found[key] != recorded.get(key)))
            raise EngineError(f"{item['path']} changed since it was installed ({changed}); "
                              "restore it or move it aside by hand, then rerun")

    def _apparmor(self) -> bool:
        flag = self.system / "sys/module/apparmor/parameters/enabled"
        found = self.disk.lstat(flag)
        return found is not None and found.kind == "file" and self.disk.read_bytes(flag, 16).strip() == b"Y"

    # Planning ------------------------------------------------------------

    def desired(self, data) -> dict:
        """The desired set, checked, as the engine enforces it for its kind:
        a purge's is runtime/ and access/ as tree-move items, nothing else."""
        desired = validate_desired(data, self.root, self.system)
        if desired["kind"] == "purge":
            whole = [{"path": str(self.root / name), "class": "tree-move"} for name in ("access", "runtime")]
            other = [item["path"] for item in desired["items"] if item not in whole]
            if other:
                raise EngineError(f"a purge installs nothing and removes everything, not {other[0]}")
            desired["items"] = whole
        return desired

    def entries(self, current, desired, id=None, kind=None) -> list[Entry]:
        """The collect rule over the installed and desired items, checked
        against the disk. Refuses a managed root file that drifted, is
        missing, or is present without being installed (9-Q12), and items
        that would nest across the two versions. An uninstall leaves every
        installed item below the root in place."""
        installed = {item["path"]: item for item in (current or {}).get("items", [])}
        wanted = {item["path"]: item for item in desired}
        replaced = [path for path, item in wanted.items() if item["class"] == "tree-move"]
        pins = (current or {}).get("pins", {})
        entries = []
        for path in sorted(set(installed) | set(wanted)):
            old, new = installed.get(path), wanted.get(path)
            if not new and any(below(path, tree) for tree in replaced):
                continue  # It moves with the tree that replaces it.
            self._safe(path)
            exists = self.disk.lstat(path) is not None
            if managed(self.root, path):
                if old and not exists:
                    raise EngineError(f"{path} is installed but missing; reinstall it or rerun after repair")
                if old:
                    self._drift(old)
                elif exists:
                    raise EngineError(f"{path} exists but the installed manifest does not list it; "
                                      "move it aside by hand if it is not wanted, then rerun")
            changed = bool(new and old and new["class"] == "shared-pinned" and pins.get(new["pin"]) != new["value"])
            action = collect_action(old and old["class"], new and new["class"], exists, changed)
            if kind == "uninstall" and old and not new and not managed(self.root, path):
                action = "leave"
            cls = (new or old)["class"]
            if action == "move" and not new:
                cls = old["class"]
            if exists and action in ("copy", "move"):
                self._meta(path, cls)  # The shape the class needs.
            # Rollback reloads the old rules with the old version's unit.
            entries.append(Entry(len(entries), path, cls, action, exists, removed=action == "move" and not new,
                                 aside=f"{CLASSES[cls].aside}/{len(entries)}" if action in ("copy", "move") else None,
                                 apply_unit=(old or new).get("apply_unit")))
            if new and new["class"] == "versioned" and exists and id is not None:
                child = f"{path}/{id}"
                if self.disk.lstat(child) is not None:
                    raise EngineError(f"{child} already exists")
                entries.append(Entry(len(entries), child, "versioned", "add", False))
        active = [entry.path for entry in entries if entry.action != "leave"]
        for path in active:
            inside = [other for other in active if below(path, other)]
            if inside:
                raise EngineError(f"{path} lies inside {inside[0]} across the two versions; "
                                  "items never nest")
        return entries

    def space(self, entries, extra=0) -> dict:
        """Room for a full, uncloned copy of everything copied aside, and of
        anything moved across filesystems; the previous kept aside already
        counts as used (9-Q11)."""
        device = self.disk.lstat(self.root).dev
        items, files = [], [0, 0]
        for entry in entries:
            if entry.action not in ("copy", "move") or not entry.existed:
                continue
            found = self.disk.lstat(entry.path)
            if entry.action == "move" and found.dev == device:
                continue
            count = self.disk.allocated(entry.path)
            if managed(self.root, entry.path):
                files[0] += 1
                files[1] += count
            else:
                items.append([str(Path(entry.path).relative_to(self.root)), count])
        if files[0]:
            items.append([f"{files[0]} root files and links", files[1]])
        required = sum(count for _, count in items) + MARGIN + extra
        return {"required": required, "available": self.disk.available(self.root), "margin": MARGIN,
                "extra": extra, "items": items}

    def plan(self, desired, current=None) -> dict:
        """What begin would set aside, and whether it fits. Changes nothing."""
        self.check_root()
        desired = self.desired(desired)
        if current is None:
            current, _ = self.installed()
        else:
            current = validate_manifest(current, root=self.root, system=self.system)
        return self.space(self.entries(current, desired["items"], kind=desired["kind"]), desired["extra"])

    # Verbs ---------------------------------------------------------------

    def begin(self, id, desired, operator, current=None) -> Journal:
        """Record a transaction before anything changes: what each path's
        collect action is, the boot target's state, and this engine's own
        bytes, pinned beside the journal to run every later verb.

        `current` describes the running version when no manifest is
        installed yet (the one-time bridge)."""
        if not self.source:
            raise EngineError("begin needs the engine's own source to pin")
        directory = self.directory(id)
        self.check_root()
        with self.disk.lock(self.root):
            self.sweep()
            for other in self.ids():
                if self.finished(other):
                    continue
                try:
                    journal = self.read(other)
                except EngineError as error:
                    raise EngineError(f"transaction {other} cannot be read ({error}); resolve it first") from None
                if journal is not None and journal.open:
                    raise EngineError(f"transaction {other} is {journal.phase}; finish or roll it back first")
            if self.disk.lstat(directory) is not None:
                raise EngineError(f"{directory} already exists")
            installed, raw = self.installed()
            source = "installed" if installed else "none"
            if current is not None:
                if installed is not None:
                    raise EngineError(f"{self.manifest_path} exists; only an installation without one is described")
                installed, source = validate_manifest(current, root=self.root, system=self.system), "bridge"
            desired = self.desired(desired)
            operator = validate_operator(operator)
            entries = self.entries(installed, desired["items"], id, desired["kind"])
            apparmor = self._apparmor()
            for tool in sorted(needed_tools(entries, apparmor)):
                if not desired["tools"][tool]:
                    path = next(entry.path for entry in entries if entry.action != "leave" and entry.cls == (
                        "nft" if tool == "nft" else "apparmor"))
                    raise EngineError(f"{path} needs the {tool} tool, which the desired set leaves null")
            space = self.space(entries, desired["extra"])
            if space["required"] > space["available"]:
                raise SpaceShort(space, self.root)
            target = {"enabled": self.host.capture(("systemctl", "is-enabled", TARGET))[0] == 0,
                      "active": self.host.capture(("systemctl", "is-active", TARGET))[0] == 0}
            if self.disk.lstat(self.transactions) is None:
                self.disk.mkdir(self.transactions, 0o700)
                self.disk.fsync_dir(self.root)
            self.disk.mkdir(directory, 0o700)
            for name in ("files", "links", "trees", "failed", "live"):
                self.disk.mkdir(directory / name, 0o700)
            self.disk.fsync_dir(directory)
            self.disk.fsync_dir(self.transactions)
            self.disk.write_atomic(directory / "engine.py", self.source, 0o600)
            if raw is not None:
                self.disk.write_atomic(directory / "manifest", raw, 0o600)
            journal = Journal(
                id=id, kind=desired["kind"], installation=str(self.root),
                engine_sha256=hashlib.sha256(self.source).hexdigest(), engine_path=str(directory / "engine.py"),
                tools=desired["tools"], operator=operator, space=space, entries=entries, pulled=desired["pulled"],
                before={"manifest": "manifest" if raw is not None else None, "source": source, "target": target,
                        "apparmor": apparmor},
                current=installed, desired=desired["items"], phases=[["planned", self.clock()]])
            self.save(journal)
            return journal

    def _run(self, *argv) -> None:
        if self.host.run(argv):
            raise EngineError(f"{' '.join(argv)} failed")

    def _systemctl(self, *argv) -> None:
        self._run("systemctl", *argv)

    def _sync_units(self) -> None:
        """Make an enable or disable durable: systemctl does not sync its links."""
        units = self.system / "etc/systemd/system"
        if self.disk.lstat(units) is not None:
            self.disk.sync_fs(units)

    def _stopped(self) -> bool:
        target = self.host.capture(("systemctl", "is-active", TARGET))[0] == 0
        code, state = self.host.capture(("systemctl", "show", "-P", "ActiveState", SLICE))
        return not target and code == 0 and state.strip() == "inactive"

    def _target_installed(self) -> bool:
        """Whether the boot target's unit file is installed: systemctl
        refuses to disable (or enable) a unit it cannot find."""
        if self.disk.lstat(self.system / "etc/systemd/system" / TARGET) is None:
            return False
        return self.host.capture(("systemctl", "is-enabled", TARGET))[1].strip() != "not-found"

    def guard(self, id) -> Journal:
        """Disable the boot target, so that a power loss from here until the
        commit boots no Thermite service, and never a mixed set. A host
        without the target (a fresh install, or one after an uninstall) has
        nothing to boot, so there is nothing to disable."""
        self.check_root()
        with self.disk.lock(self.root):
            journal = self._open(id)
            if journal.phase == "guarded":
                return journal
            if journal.phase != "planned":
                raise EngineError(f"transaction {id} is {journal.phase}, not planned")
            if self._target_installed():
                self._systemctl("disable", TARGET)
                self._sync_units()
            self._advance(journal, "guarded")
            return journal

    def collect(self, id) -> Journal:
        """Set the stopped version aside: all copies first, so that running
        out of room moves nothing live, then the moves with their removal
        effects. Each step is saved in the journal before it is taken."""
        self.check_root()
        with self.disk.lock(self.root):
            journal = self._open(id)
            if journal.phase == "switching":
                return journal
            if journal.phase != "guarded":
                raise EngineError(f"transaction {id} is {journal.phase}, not guarded")
            if self.host.capture(("systemctl", "is-enabled", TARGET))[0] == 0 or not self._stopped():
                raise EngineError(f"{TARGET} must be disabled and {SLICE} stopped before collecting")
            installed = {item["path"]: item for item in (journal.current or {}).get("items", [])}
            for entry in journal.entries:
                self._safe(entry.path)
                if entry.existed and entry.action in ("copy", "move") and managed(self.root, entry.path):
                    self._drift(installed[entry.path])
            self._advance(journal, "collecting")
            directory = self.directory(id)
            if any(entry.cls == "nft" and entry.action != "leave" for entry in journal.entries):
                code, table = self.host.capture(("nft", "list", "table", *TABLE))
                if code == 0:
                    self.disk.write_atomic(directory / "live/thermite_native.nft", table.encode(), 0o600)
                journal.live = {"nft_checked": True, "nft": "live/thermite_native.nft" if code == 0 else None}
                self.save(journal)
            for entry in journal.entries:
                if entry.action == "copy":
                    entry.meta, entry.state = self._meta(entry.path, entry.cls), "partial"
                    self.save(journal)
                    self._copy(Path(entry.path), directory / entry.aside)
                    entry.state = "aside"
                    self.save(journal)
            reload = False
            for entry in journal.entries:
                if entry.action != "move":
                    continue
                entry.meta, entry.state = self._meta(entry.path, entry.cls), "moving"
                self.save(journal)
                if entry.removed and entry.cls == "apparmor" and journal.before["apparmor"]:
                    self._unload(entry.path)
                self._move(Path(entry.path), directory / entry.aside)
                entry.state = "aside"
                self.save(journal)
                if entry.removed and entry.cls == "nft" and journal.live["nft"]:
                    self._run("nft", "delete", "table", *TABLE)
                reload = reload or entry.cls in ("unit", "link")
            if reload:
                self._systemctl("daemon-reload")
            self._advance(journal, "switching")
            return journal

    def _unload(self, path) -> None:
        # Unloading needs the policy file, so it comes before the file goes.
        if self.host.run(("apparmor_parser", "-R", str(path))):
            self.say(f"warning: apparmor_parser -R {path} failed; the profile may not have been loaded")

    def _discard(self, path) -> None:
        if self.disk.lstat(path) is not None:
            self.disk.remove_tree(path)
            self.disk.fsync_dir(Path(path).parent)

    def _copy(self, source, destination) -> None:
        """A durable copy that appears whole or not at all."""
        building = partial(destination)
        self._discard(building)
        self.disk.copy(source, building)
        self.disk.sync_fs(building)
        self.disk.rename(building, destination)
        self.disk.fsync_dir(destination.parent)

    def _move(self, source, destination) -> None:
        """Move by rename, or across filesystems by a copy and then removing
        the source. The journal announced the move, and the destination
        appears only when complete, so a destination already present means
        only the source's removal is left. Rerunning it finishes it."""
        self._safe(source)
        self._safe(destination)
        if self.disk.lstat(destination) is not None:
            self._discard(partial(destination))
            self._discard(source)
            return
        self._discard(partial(destination))
        if self.disk.lstat(source) is None:
            raise EngineError(f"{source} is missing, and so is {destination}")
        try:
            self.disk.rename(source, destination)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            self._copy(source, destination)
            self.disk.remove_tree(source)
        self.disk.fsync_dir(destination.parent)
        self.disk.fsync_dir(source.parent)

    def commit(self, id, release=None) -> Journal:
        """Record what the new version installed, then commit: the durable
        `committed` phase is the commit point (9-F1). After it, recovery
        finishes the commit instead of undoing it.

        Without `release`, the installed release, pins and images carry over
        (an uninstall)."""
        self.check_root()
        with self.disk.lock(self.root):
            journal = self._open(id)
            if journal.phase in ("committed", "kept"):
                return self._finish(journal)
            if journal.phase != "switching":
                raise EngineError(f"transaction {id} is {journal.phase}, not switching")
            current = journal.current or {}
            if release is None:
                release = {"release": current.get("release", {}), "pins": current.get("pins", {}),
                           "images": current.get("images", {}).get("current", []),
                           "application": current.get("application", {}).get("current")}
            release = validate_release(release)
            items, devices = [], {}
            installed = {item["path"]: item for item in current.get("items", [])}
            for item in journal.desired:
                path, cls = item["path"], item["class"]
                if cls in ("shared", "shared-pinned"):
                    items.append({"path": path, "class": cls, **({"pin": item["pin"]} if "pin" in item else {})})
                    continue
                if cls == "versioned":
                    self._meta(f"{path}/{id}", "versioned")
                    old = installed.get(path, {})
                    previous = old.get("current") if old.get("current") not in (None, id) else old.get("previous")
                    items.append({"path": path, "class": cls, "current": id, "previous": previous})
                    devices.setdefault(self.disk.lstat(path).dev, path)
                    continue
                found = self.disk.lstat(path)
                if found is None:
                    if managed(self.root, path):
                        raise EngineError(f"{path} was not installed")
                    continue  # State the new version did not create is not recorded.
                cls = "tree" if cls == "tree-move" else cls
                record = {"path": path, "class": cls, **self._meta(path, cls)}
                record = {key: record[key] for key in ("path", "class", *CLASSES[cls].recorded) if key in record}
                record.update({key: item[key] for key in ("unit", "apply_unit") if key in item})
                if cls == "link" and "target" in item and record["target"] != item["target"]:
                    raise EngineError(f"{path} points to {record['target']}, not {item['target']}")
                items.append(record)
                devices.setdefault(found.dev, path)
            if journal.kind == "uninstall":
                # All state stays, recorded as shared so a reinstall finds it.
                listed = [item["path"] for item in journal.desired]
                for path in installed:
                    if not managed(self.root, path) and not any(
                            path == other or below(path, other) or below(other, path) for other in listed):
                        items.append({"path": path, "class": "shared"})
            old_images = current.get("images", {"current": [], "previous": []})
            old_application = current.get("application", {"current": None, "previous": None})
            manifest = {
                "format": MANIFEST_FORMAT, "installation": str(self.root), "transaction": id, "kept": id,
                "committed_at": self.clock(), "installed": journal.kind not in UNINSTALLING,
                "release": release["release"], "pins": release["pins"], "tools": journal.tools, "items": items,
                "images": {"current": release["images"],
                           "previous": old_images["previous"] if set(old_images["current"]) == set(release["images"])
                           else old_images["current"]},
                "application": {"current": release["application"],
                                "previous": old_application["previous"]
                                if old_application["current"] in (None, release["application"])
                                else old_application["current"]},
            }
            journal.manifest = validate_manifest(manifest, root=self.root, system=self.system)
            # What the new version wrote went through the page cache: make it
            # durable before the commit point says it is installed.
            for path in devices.values():
                self.disk.sync_fs(path)
            self._advance(journal, "committed")
            return self._finish(journal)

    def _finish(self, journal) -> Journal:
        """Everything after the commit point; rerunning it is harmless."""
        if journal.phase == "kept":
            self._mark(journal)
            if journal.kind == "purge":
                self._purge(journal)
            return journal
        self._safe(self.manifest_path)
        if journal.kind == "purge":
            self._discard(self.manifest_path)
        else:
            raw = render_manifest(journal.manifest)
            if self.disk.lstat(self.manifest_path.parent) is None:
                self.disk.mkdir(self.manifest_path.parent, 0o755)
                self.disk.fsync_dir(self.manifest_path.parent.parent)
            if self.disk.lstat(self.manifest_path) is None or self.disk.read_bytes(
                    self.manifest_path, 1024 * 1024) != raw:
                self.disk.write_atomic(self.manifest_path, raw, 0o644)
        if journal.kind not in UNINSTALLING:
            self._systemctl("enable", TARGET)
            self._sync_units()
            # Already running unless a power loss came after the commit point.
            if self.host.run(("systemctl", "start", TARGET)):
                self.say(f"warning: systemctl start {TARGET} failed")
        self._advance(journal, "kept")
        self.prune(journal)
        if journal.kind == "purge":
            self._purge(journal)
        return journal

    def _purge(self, journal) -> None:
        """The last of a purge: its own transaction, backup and all. The
        directory is renamed out of the transactions first, so that a crash
        while removing it leaves no half transaction, only what sweep()
        removes."""
        directory = self.directory(journal.id)
        if self.disk.lstat(directory) is not None:
            # The manifest's directory goes first, while the transaction
            # still says what is left to do.
            parent = self.manifest_path.parent
            found = self.disk.lstat(parent)
            if found is not None and found.kind == "dir" and not self.disk.listdir(parent):
                self._discard(parent)
            gone = self.transactions / f"{PURGED}{journal.id}"
            self._discard(gone)
            self.disk.rename(directory, gone)
            self.disk.fsync_dir(self.transactions)
        self.sweep()

    def sweep(self) -> None:
        """Finish removing what a purge set to go: its renamed transaction,
        and the transaction directory once nothing else is in it (begin
        makes it again)."""
        if self.disk.lstat(self.transactions) is None:
            return
        self._private(self.transactions, "dir")
        names = self.disk.listdir(self.transactions)
        purged = [name for name in names if name.startswith(PURGED)]
        for name in purged:
            self._discard(self.transactions / name)
        if len(purged) == len(names):
            self._discard(self.transactions)

    def prune(self, journal) -> None:
        """After a commit, best effort: earlier finished transactions (their
        failed/ too), this one's tree-move asides, versioned children beyond
        the previous one, and images neither current nor previous."""
        def attempt(what, step):
            try:
                step()
            except (OSError, EngineError) as error:
                self.say(f"warning: could not remove {what}: {error}")

        pulled = []
        for other in self.ids():
            if other == journal.id:
                continue
            try:
                found = self.read(other)
            except EngineError:
                if not self.finished(other):
                    continue  # Never remove what cannot be read, unless it is finished.
                found = None
            if found is not None and found.phase not in TERMINAL:
                continue
            if found is not None and found.phase == "rolled-back":
                pulled += found.pulled
            attempt(self.directory(other), lambda other=other: self._discard(self.directory(other)))
        directory = self.directory(journal.id)
        for entry in journal.entries:
            if entry.cls == "tree-move" and entry.action == "move":
                attempt(entry.path, lambda entry=entry: self._discard(directory / entry.aside))
        current = journal.current or {}
        kept = {item["path"]: item for item in journal.manifest["items"] if item["class"] == "versioned"}
        for item in current.get("items", []):
            new = kept.get(item["path"])
            if item["class"] == "versioned" and new and item["previous"] not in (None, new["current"], new["previous"]):
                attempt(item["path"], lambda path=f"{item['path']}/{item['previous']}": self._discard(path))
        images = journal.manifest["images"]
        keep = set() if journal.kind == "purge" else set(images["current"]) | set(images["previous"])
        old = current.get("images", {}).get("previous", [])
        if journal.kind == "purge":
            old = [*old, *current.get("images", {}).get("current", [])]
        if journal.tools.get("podman"):
            for ref in sorted((set(old) | set(pulled)) - keep):
                attempt(f"image {ref}", lambda ref=ref: self._run("podman", "image", "rm", "--ignore", "--", ref))

    def rollback(self, id) -> Journal:
        """Put the version that ran before the transaction back, from what is
        on disk, or finish a committed transaction. Every step is saved
        before it is taken and rerunning continues it; a reinstatement that
        cannot complete leaves the transaction stuck, with Thermite stopped
        and the boot guard on."""
        self.check_root()
        with self.disk.lock(self.root):
            journal = self._open(id)
            if journal.phase in ("committed", "kept"):
                return self._finish(journal)
            if journal.phase == "rolled-back":
                self._mark(journal)
                return journal
            if journal.phase not in ("rolling-back", "stuck"):
                journal.rollback_from = journal.phase
            self._advance(journal, "rolling-back")
            try:
                self._reinstate(journal)
                if journal.before["target"]["enabled"]:
                    self._systemctl("enable", TARGET)
                    self._sync_units()
            except (OSError, EngineError) as error:
                journal.outcome = {"at": self.clock(), "result": "stuck", "started": False, "error": str(error)}
                self._advance(journal, "stuck")
                return journal
            started, error = False, None
            if journal.before["target"]["active"]:
                started = self.host.run(("systemctl", "start", TARGET)) == 0
                error = None if started else f"systemctl start {TARGET} failed"
            journal.outcome = {"at": self.clock(), "result": "rolled-back", "started": started, "error": error}
            self._advance(journal, "rolled-back")
            return journal

    def _reinstate(self, journal) -> None:
        source = journal.rollback_from
        if source in ("collecting", "switching"):
            self.host.run(("systemctl", "stop", TARGET, SLICE))
            if not self._stopped():
                raise EngineError(f"{TARGET} or {SLICE} is still active after systemctl stop")
            for entry in reversed(journal.entries):
                self._safe(entry.path)
                self._entry(journal, entry, source)
            self._systemctl("daemon-reload")
            # Live state is put back from the files whether or not their entry
            # was touched: a power loss may have booted a half-collected set.
            nft = next((entry for entry in journal.entries if entry.cls == "nft" and entry.action != "leave"), None)
            if nft is not None and journal.live["nft_checked"]:
                self._table(journal, nft)
            if journal.before["apparmor"]:
                for entry in journal.entries:
                    if entry.cls == "apparmor" and entry.existed and entry.action in ("copy", "move"):
                        self._run("apparmor_parser", "-r", entry.path)
            self._verify(journal)
        expected = None
        if journal.before["manifest"]:
            expected = self.disk.read_bytes(self.directory(journal.id) / "manifest", 1024 * 1024)
        found = None
        if self.disk.lstat(self.manifest_path) is not None:
            found = self.disk.read_bytes(self.manifest_path, 1024 * 1024)
        if found != expected:
            raise EngineError(f"{self.manifest_path} is not as it was before the transaction")
        self.host.run(("systemctl", "reset-failed", "thermite*"))

    def _table(self, journal, entry) -> None:
        """The live table as it was: loaded from the restored rule file by
        the old version's unit, from the text collect saved when no rule
        file of the old version loaded it, or deleted if there was none."""
        if journal.live["nft"] and entry.existed:
            self._systemctl("restart", entry.apply_unit)
            if self.host.capture(("nft", "list", "table", *TABLE))[0]:
                raise EngineError(f"{entry.apply_unit} did not load table {' '.join(TABLE)}")
            return
        if self.host.capture(("nft", "list", "table", *TABLE))[0] == 0:
            self._run("nft", "delete", "table", *TABLE)
        if journal.live["nft"]:
            self._run("nft", "-f", str(self.directory(journal.id) / journal.live["nft"]))

    def _entry(self, journal, entry, source) -> None:
        """Reinstate one entry. Before its aside is complete the live path is
        the authority; after, the aside is."""
        if entry.action == "leave" or entry.state in ("restored", "discarded"):
            return
        directory = self.directory(journal.id)
        live = Path(entry.path)
        failed = directory / "failed" / str(entry.n)
        aside = directory / entry.aside if entry.aside else None
        if entry.action == "add":
            if source != "switching":
                entry.state = "discarded"
                self.save(journal)
                return
            if entry.state != "displacing":
                entry.state = "displacing"
                self.save(journal)
            if entry.cls == "apparmor" and journal.before["apparmor"] and self.disk.lstat(live) is not None:
                self._unload(live)
            if self.disk.lstat(live) is not None:
                self._move(live, failed)
            entry.state = "restored"
            self.save(journal)
            return
        if entry.state in ("planned", "partial") or entry.action == "copy" and source == "collecting":
            # The live path was never touched: what was set aside is surplus.
            self._discard(partial(aside))
            self._discard(aside)
            entry.state = "discarded"
            self.save(journal)
            return
        if self.disk.lstat(aside) is None:
            # Moving: the move never completed, so the live path is still
            # whole (its removal effect may have run). Later: the aside went
            # back already, by this engine or by hand from rollback --print.
            # Either way what is live is the old version, and _verify checks.
            self._discard(partial(aside))
            entry.state = "restored"
            self.save(journal)
            return
        if entry.state in ("moving", "aside"):
            entry.state = "displacing"
            self.save(journal)
        if entry.state == "displacing":
            if self.disk.lstat(live) is not None:
                self._move(live, failed)
            entry.state = "returning"
            self.save(journal)
        self._move(aside, live)
        entry.state = "restored"
        self.save(journal)

    def _verify(self, journal) -> None:
        """Every reinstated or untouched path is what was collected; every
        added one is gone."""
        for entry in journal.entries:
            if entry.state not in ("restored", "discarded") or entry.meta is None and entry.action != "add":
                continue
            if entry.action == "add":
                if entry.state == "discarded":
                    continue
                if self.disk.lstat(entry.path) is not None:
                    raise EngineError(f"{entry.path} is still present after the rollback")
                continue
            found = self._meta(entry.path, entry.cls)
            if found != entry.meta:
                raise EngineError(f"{entry.path} is not as it was collected after the rollback")

    def status(self) -> list[dict]:
        """Every transaction under the root: its kind and phase, or why it
        cannot be read."""
        self.check_root()
        found = []
        for id in self.ids():
            try:
                journal = self.read(id)
            except EngineError as error:
                finished = self.finished(id)
                found.append({"id": id, "open": not finished, "error": str(error),
                              **({"phase": self.disk.read_bytes(self.directory(id) / FINISHED, 64).decode().strip()}
                                 if finished else {})})
                continue
            if journal is None:
                found.append({"id": id, "phase": None, "open": False, "error": "begin did not finish"})
                continue
            found.append({"id": id, "kind": journal.kind, "phase": journal.phase, "open": journal.open,
                          "engine": journal.engine_path, "outcome": journal.outcome,
                          **({"bytes": self.disk.allocated(self.directory(id))} if journal.phase == "kept" else {})})
        return found

    def _manual_entry(self, journal, entry, source) -> list[str]:
        """One entry's commands, derived from its state as _entry derives
        its steps. Each move is guarded by what is on disk, so running the
        commands again, or after rollback got partway, loses nothing."""
        if entry.action == "leave" or entry.state in ("restored", "discarded") or entry.state == "planned" and (
                entry.action != "add"):
            return []
        if entry.action == "add" and source != "switching":
            return []
        directory = self.directory(journal.id)
        live, failed = entry.path, str(directory / "failed" / str(entry.n))
        lines = [f"# {entry.n} {entry.path} ({entry.cls}, {entry.action}, {entry.state})"]
        if entry.action == "add":
            if entry.cls == "apparmor" and journal.before["apparmor"]:
                lines.append(f"if {_present(live)}; then {shlex.join(['apparmor_parser', '-R', live])}; fi")
            lines.append(f"if {_present(live)}; then {shlex.join(['mv', '-T', live, failed])}; fi")
            return lines
        aside = str(directory / entry.aside)
        if entry.state == "partial" or entry.action == "copy" and source == "collecting":
            # The live path was never touched.
            return [*lines, shlex.join(["rm", "-rf", "--", str(partial(aside)), aside])]
        if entry.state == "moving":
            lines.append(shlex.join(["rm", "-rf", "--", str(partial(aside))]))
        # Only while the aside exists does it go back; before that, whatever
        # is live moves out of its way.
        lines.append(f"if {_present(aside)}; then {{ ! {_present(live)} || {shlex.join(['mv', '-T', live, failed])}; }}"
                     f" && {shlex.join(['mv', '-T', aside, live])}; fi")
        return lines

    def manual(self, journal) -> list[str]:
        """The shell commands that put the old version back by hand, for when
        the engine itself cannot run (rollback --print)."""
        directory = self.directory(journal.id)
        source = journal.rollback_from or journal.phase
        lines = [f"# Roll back transaction {journal.id} ({journal.kind}, {journal.phase}) by hand, as root."]
        if source in ("collecting", "switching"):
            lines.append(shlex.join(["systemctl", "stop", TARGET, SLICE]))
            for entry in reversed(journal.entries):
                lines += self._manual_entry(journal, entry, source)
            lines.append(shlex.join(["systemctl", "daemon-reload"]))
            nft = next((entry for entry in journal.entries if entry.cls == "nft" and entry.action != "leave"), None)
            if nft is not None and journal.live["nft_checked"]:
                if journal.live["nft"] and nft.existed:
                    lines.append(shlex.join(["systemctl", "restart", nft.apply_unit]))
                else:
                    lines.append(shlex.join(["nft", "delete", "table", *TABLE]) + " 2>/dev/null || true")
                    if journal.live["nft"]:
                        lines.append(shlex.join(["nft", "-f", str(directory / journal.live["nft"])]))
            if journal.before["apparmor"]:
                lines += [shlex.join(["apparmor_parser", "-r", entry.path]) for entry in journal.entries
                          if entry.cls == "apparmor" and entry.action in ("copy", "move")]
        lines.append(shlex.join(["systemctl", "reset-failed", "thermite*"]))
        if journal.before["target"]["enabled"]:
            lines.append(shlex.join(["systemctl", "enable", TARGET]))
        if journal.before["target"]["active"]:
            lines.append(shlex.join(["systemctl", "start", TARGET]))
        return lines


def _present(path) -> str:
    """A shell test that the path exists, a dangling symlink included."""
    quoted = shlex.quote(str(path))
    return f"{{ [ -e {quoted} ] || [ -L {quoted} ]; }}"


# The real host ----------------------------------------------------------------

def trusted_tool(name, value, owner=None) -> str:
    """The resolved path of tool `name`, if it is the program of that name
    in one of the system's program directories, and root (or, in tests, the
    caller) owns it and every directory on the way, and no one else can
    change any of them; a sticky world-writable directory such as /tmp is
    allowed on the way."""
    owners = {0, os.geteuid() if owner is None else owner}
    path = Path(str(checked_path(value, f"tool {name}")))
    if name not in TOOLS or path.name != name or str(path.parent) not in TOOL_DIRS:
        raise EngineError(f"tool {name} must be the {name} program in {', '.join(TOOL_DIRS)}, not {path}")
    real = Path(os.path.realpath(path))
    for each in (path, real):
        for ancestor in each.parents:
            info = os.stat(ancestor)
            if info.st_uid not in owners or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise EngineError(f"tool {path}: {ancestor} could be changed by someone other than root")
    info = os.stat(real)
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022 or not info.st_mode & 0o100:
        raise EngineError(f"tool {path} must be an executable file only root can change")
    return str(real)


def trusted_tools(tools) -> dict:
    return {name: trusted_tool(name, path) for name, path in check_tools(tools).items() if path is not None}


class OsDisk(Disk):
    """The filesystem, as root (`owner`, the effective uid) sees it. Copies
    and filesystem syncs run the cp and sync tools."""

    def __init__(self, tools=None):
        self.tools = dict(tools or {})
        self.owner = os.geteuid()

    def lstat(self, path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        mode = info.st_mode
        kind = ("file" if stat.S_ISREG(mode) else "link" if stat.S_ISLNK(mode) else
                "dir" if stat.S_ISDIR(mode) else "other")
        return Stat(kind, stat.S_IMODE(mode), info.st_uid, info.st_gid, info.st_size, info.st_dev)

    def read_bytes(self, path, limit):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise EngineError(f"{path} is not a regular file")
            data = source.read(limit + 1)
        if len(data) > limit:
            raise EngineError(f"{path} is larger than {limit} bytes")
        return data

    def readlink(self, path):
        return os.readlink(path)

    def listdir(self, path):
        return sorted(os.listdir(path))

    def allocated(self, path):
        """Allocated blocks, as `cp -a` keeps sparse files sparse. Walked
        with a list, not recursion, however deep the tree."""
        info = os.lstat(path)
        total = info.st_blocks * 512
        pending = [path] if stat.S_ISDIR(info.st_mode) else []
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    total += entry.stat(follow_symlinks=False).st_blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
        return total

    def available(self, path):
        info = os.statvfs(path)
        return info.f_bavail * info.f_frsize

    def write_atomic(self, path, data, mode):
        path = Path(path)
        temporary = path.with_name("." + path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
        try:
            os.fchmod(descriptor, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        self.fsync_dir(path.parent)

    # Every change below goes through descriptors for the directories it
    # changes, each reached from / without following a symlink: runtime/
    # and access/ belong to other users, who could otherwise swap a
    # directory on the way for a symlink between a check and the change.

    def mkdir(self, path, mode):
        path = Path(path)
        with _directory(path.parent) as parent:
            os.mkdir(path.name, mode, dir_fd=parent)
            made = os.open(path.name, _DIRECTORY, dir_fd=parent)
            try:
                os.fchmod(made, mode)  # Whatever the umask.
            finally:
                os.close(made)

    def rename(self, source, destination):
        source, destination = Path(source), Path(destination)
        with _directory(source.parent) as old, _directory(destination.parent) as new:
            if _exists(new, destination.name):
                raise FileExistsError(errno.EEXIST, "the engine never renames onto a path", str(destination))
            os.rename(source.name, destination.name, src_dir_fd=old, dst_dir_fd=new)

    def _tool(self, name, *argv, shown=None, descriptors=()):
        if not self.tools.get(name):
            raise EngineError(f"no {name} tool is configured")
        result = subprocess.run([self.tools[name], *map(str, argv)], stdin=subprocess.DEVNULL, cwd="/",
                                capture_output=True, text=True, env=ENVIRONMENT, pass_fds=descriptors, check=False)
        if result.returncode:
            raise EngineError(f"{name} {shlex.join(map(str, shown or argv))} failed: {result.stderr.strip()}")

    def copy(self, source, destination):
        """cp into a directory root has just made, private to root, in the
        destination's directory, then a rename out of it: cp never writes
        through a path anyone else could have planted."""
        source, destination = Path(source), Path(destination)
        flags = ("-a", "--reflink=auto", "--no-target-directory", "--")
        with _directory(source.parent) as old, _directory(destination.parent) as new:
            if _exists(new, destination.name):
                raise FileExistsError(errno.EEXIST, "the engine never copies onto a path", str(destination))
            work = f".{destination.name}.cp"
            if _exists(new, work):
                _remove(new, work, _device(new))  # What a crash left.
            os.mkdir(work, 0o700, dir_fd=new)
            made = os.open(work, _DIRECTORY, dir_fd=new)
            try:
                found = os.fstat(made)
                if found.st_uid != os.geteuid() or found.st_mode & 0o077:
                    raise EngineError(f"{destination.parent / work} is not the directory the engine just made")
                self._tool("cp", *flags, f"/proc/self/fd/{old}/{source.name}",
                           f"/proc/self/fd/{made}/{destination.name}", descriptors=(old, made),
                           shown=(*flags, source, destination))
                if _exists(new, destination.name):
                    raise FileExistsError(errno.EEXIST, "the engine never copies onto a path", str(destination))
                os.rename(destination.name, destination.name, src_dir_fd=made, dst_dir_fd=new)
            finally:
                os.close(made)
                with contextlib.suppress(OSError, EngineError):
                    _remove(new, work, _device(new))

    def remove_tree(self, path):
        path = Path(path)
        with _directory(path.parent) as parent:
            if _exists(parent, path.name):
                _remove(parent, path.name, _device(parent))

    def sync_fs(self, path):
        self._tool("sync", "--file-system", "--", path)

    def fsync_dir(self, path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def lock(self, path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EngineError(f"another engine verb is running on {path}") from None
            yield
        finally:
            os.close(descriptor)


#: What root's tools run with: nothing inherited from the caller.
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextlib.contextmanager
def _directory(path):
    """A descriptor for a directory, reached from / without following a
    symlink at any step."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _exists(directory, name) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _device(directory) -> int:
    return os.fstat(directory).st_dev


def _remove(parent, name, device):
    """rm -rf --one-file-system, by descriptor: a symlink is removed, never
    followed, and anything on another filesystem than `device` (the
    parent's), the path itself included when it is a mount point, is
    refused. Walked with a stack of open directories, not recursion."""
    def checked(directory, entry):
        info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
        if info.st_dev != device:
            raise EngineError(f"{entry} is another filesystem, mounted inside what is removed")
        return info

    if not stat.S_ISDIR(checked(parent, name).st_mode):
        os.unlink(name, dir_fd=parent)
        return
    top = os.open(name, _DIRECTORY, dir_fd=parent)
    stack = [(top, parent, name, os.listdir(top))]
    try:
        while stack:
            directory, above, here, entries = stack[-1]
            if not entries:
                stack.pop()
                os.close(directory)
                os.rmdir(here, dir_fd=above)
                continue
            entry = entries.pop()
            if stat.S_ISDIR(checked(directory, entry).st_mode):
                child = os.open(entry, _DIRECTORY, dir_fd=directory)
                stack.append((child, directory, entry, os.listdir(child)))
            else:
                os.unlink(entry, dir_fd=directory)
    finally:
        for directory, *_ in stack:
            os.close(directory)


class SubprocessHost(Host):
    """The tools, by their resolved paths, with a fixed environment. What
    they print goes to stderr, so that the engine's own output stays clean."""

    def __init__(self, tools=None):
        self.tools = dict(tools or {})

    def _argv(self, argv):
        if not self.tools.get(argv[0]):
            raise EngineError(f"no {argv[0]} tool is configured")
        return [self.tools[argv[0]], *map(str, argv[1:])]

    def run(self, argv):
        return subprocess.run(self._argv(argv), stdin=subprocess.DEVNULL, stdout=sys.stderr, env=ENVIRONMENT, cwd="/",
                              check=False).returncode

    def capture(self, argv):
        result = subprocess.run(self._argv(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd="/",
                                env=ENVIRONMENT, check=False)
        return result.returncode, result.stdout


# The command line ---------------------------------------------------------------

#: Exit statuses: refused or failed; not enough room; rolled back; stuck.
FAILED, SHORT, ROLLED_BACK, STUCK = 1, 2, 3, 5


def _input(disk, value) -> dict:
    """An operator-written JSON input, read once; root reads no operator file
    after begin."""
    path = Path(str(checked_path(value, "input file")))
    try:
        return json.loads(disk.read_bytes(path, 4 * 1024 * 1024))
    except (UnicodeDecodeError, ValueError) as error:
        raise EngineError(f"{path} is not JSON: {error}") from None


def _pinned(engine, id) -> Path:
    """The engine copy begin pinned for a transaction, if root's custody of
    it holds."""
    directory = engine.directory(id)
    engine._safe(directory)
    engine._private(engine.transactions, "dir")
    engine._private(directory, "dir")
    engine._private(directory / "engine.py", "file")
    return directory / "engine.py"


def _delegate(pinned, argv) -> None:
    """Run the verb with the engine that began the transaction instead."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-I", "-S", str(pinned), *argv])


def _open_one(engine) -> str | None:
    """The one transaction that is not finished, by id; None if none is."""
    open_ids = []
    for id in engine.ids():
        try:
            journal = engine.read(id)
        except EngineError:
            if not engine.finished(id):
                open_ids.append(id)  # Only its own engine can read it.
            continue
        if journal is not None and journal.open:
            open_ids.append(id)
    if len(open_ids) > 1:
        raise EngineError(f"more than one transaction is open: {', '.join(open_ids)}; name one with --id")
    return open_ids[0] if open_ids else None


def parser() -> argparse.ArgumentParser:
    command_line = argparse.ArgumentParser(prog="engine.py", description="Thermite's install transactions, as root.")
    verbs = command_line.add_subparsers(dest="verb", required=True)
    for verb, help in (("plan", "the room needed to set the current version aside, as JSON"),
                       ("begin", "record a transaction and pin this engine for it"),
                       ("guard", "disable thermite.target until the commit"),
                       ("collect", "set the stopped version aside"),
                       ("commit", "record the new version and commit it"),
                       ("rollback", "put the previous version back, or finish a committed transaction"),
                       ("status", "list the transactions")):
        command = verbs.add_parser(verb, help=help)
        command.add_argument("--root", required=True, help="the installation root")
        if verb in ("begin", "guard", "collect", "commit", "rollback"):
            command.add_argument("--id", required=verb != "rollback", help="the transaction")
        if verb in ("plan", "begin"):
            command.add_argument("--desired", required=True, help="the desired set, JSON")
            command.add_argument("--current", help="a description of an installation without a manifest")
        if verb == "begin":
            command.add_argument("--operator", required=True, help="the operator's side, JSON")
        if verb == "commit":
            command.add_argument("--release", help="the release installed, JSON (none for an uninstall)")
        if verb == "rollback":
            shown = command.add_mutually_exclusive_group()
            shown.add_argument("--dry-run", action="store_true", help="say what a rollback would do")
            shown.add_argument("--print", action="store_true", help="print the commands to roll back by hand")
        if verb == "status":
            command.add_argument("--json", action="store_true")
    return command_line


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    here = Path(__file__).resolve()
    # Nothing root runs may find a program or file in the caller's directory.
    os.chdir("/")
    # Removing a tree holds a descriptor for each level of it.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and (hard == resource.RLIM_INFINITY or soft < hard):
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    try:
        return _main(args, argv, here)
    except SpaceShort as error:
        print(error, file=sys.stderr)
        return SHORT
    except (OSError, EngineError) as error:
        print(f"thermite install engine: {error}", file=sys.stderr)
        return FAILED


def _main(args, argv, here) -> int:
    engine = Engine(OsDisk(), SubprocessHost(), args.root, source=here.read_bytes(), say=lambda text: print(
        text, file=sys.stderr))
    engine.check_root()
    if args.verb in ("plan", "begin"):
        desired = _input(engine.disk, args.desired)
        current = _input(engine.disk, args.current) if args.current else None
        tools = trusted_tools(validate_desired(desired, engine.root, engine.system)["tools"])
        engine.disk.tools = engine.host.tools = tools
        if args.verb == "plan":
            space = engine.plan(desired, current)
            print(json.dumps(space, sort_keys=True))
            if space["required"] > space["available"]:
                print(describe_space(space, engine.root), file=sys.stderr)
                return SHORT
            return 0
        journal = engine.begin(args.id, desired, _input(engine.disk, args.operator), current)
        print(json.dumps({"id": journal.id, "phase": journal.phase, "engine": journal.engine_path}))
        return 0
    if args.verb == "status":
        found = engine.status()
        if args.json:
            print(json.dumps(found, sort_keys=True))
            return 0
        for each in found:
            if "kind" not in each:
                print(f"{each['id']}: {each['error']}")
                continue
            print(f"{each['id']}: {each['kind']} {each['phase']}" + (", open" if each["open"] else "")
                  + (f", the kept backup ({_size(each['bytes'])})" if "bytes" in each else ""))
        return 0
    id = args.id
    if id is None:
        with engine.disk.lock(engine.root):
            engine.sweep()  # What a purge killed partway left.
        id = _open_one(engine)
        if id is None:
            print("No transaction is open.")
            return 0
        argv = [*argv, "--id", id]
    pinned = _pinned(engine, id)
    if here != pinned:
        _delegate(pinned, argv)
    journal = engine.read(id)
    if journal is None:
        raise EngineError(f"transaction {id} has no journal; begin did not finish")
    if args.verb == "rollback" and (args.print or args.dry_run):
        # Printing runs nothing, so no tool is checked: this is the way out
        # when one can no longer be trusted.
        engine._open(id)
        if args.dry_run:
            print(f"Rolling back transaction {id} ({journal.kind}, {journal.phase}) would run:")
        print("\n".join(engine.manual(journal)))
        return 0
    # Only the tools the verb runs are checked; pruning images is best
    # effort, so podman going missing never blocks a commit or rollback.
    needed = ({"systemctl", "sync"} if args.verb in ("guard", "commit")
              else needed_tools(journal.entries, journal.before["apparmor"]))
    tools = {name: trusted_tool(name, journal.tools[name]) for name in sorted(needed)}
    if args.verb in ("commit", "rollback") and journal.tools["podman"]:
        try:
            tools["podman"] = trusted_tool("podman", journal.tools["podman"])
        except EngineError as error:
            engine.say(f"warning: images will not be pruned: {error}")
    engine.disk.tools = engine.host.tools = tools
    if args.verb == "rollback":
        if journal.phase in TERMINAL:
            engine.rollback(id)  # Marks it finished, if a crash came first.
            return 0
        journal = engine.rollback(id)
        if journal.phase == "stuck":
            print(f"Stuck: {journal.outcome['error']}. Thermite stays stopped with the boot guard on; "
                  f"rerun, or roll back by hand: {sys.executable} -I -S {pinned} rollback --root {engine.root} "
                  f"--id {id} --print", file=sys.stderr)
            return STUCK
        return 0 if journal.phase == "kept" else ROLLED_BACK
    if args.verb == "commit":
        journal = engine.commit(id, _input(engine.disk, args.release) if args.release else None)
    else:
        journal = getattr(engine, args.verb)(id)
    print(json.dumps({"id": journal.id, "phase": journal.phase}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
