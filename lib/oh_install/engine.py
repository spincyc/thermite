"""Install transactions: the installed manifest, its classes and the journal.

Root runs a private copy of this file with `/usr/bin/python3 -I -S`, so it
imports the standard library only; operator code and tests import it as a
module. It is the only code that sets an installed version aside, puts it
back, or writes /etc/thermite/installed.json. How each item is collected and
reinstated is fixed here, by the item's class; a release supplies data
(paths and classes), never commands.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path, PurePosixPath
import re

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
#: install | upgrade | clean | uninstall | purge | revert. Only the last
#: three differ: uninstall and purge record installed=false and leave the
#: boot target disabled.
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
    # State replaced wholesale (purge, a clean install): moved aside and
    # deleted at commit. Never recorded.
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
    """Whether `path` is strictly inside `parent`."""
    return PurePosixPath(parent) in PurePosixPath(path).parents


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
            raise EngineError(f"{what}: a tree-move item is deleted at commit, never recorded")
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
        checked_path(value[name], f"tool {name}")
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
        return "leave"  # The child named by the transaction id is the entry.
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
        data = dataclasses.asdict(self)
        data["class"] = data.pop("cls")
        return data

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
        return (json.dumps(self.to_json(), indent=1, sort_keys=True) + "\n").encode()

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
