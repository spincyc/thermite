"""The one-time bridge from an installation without an installed manifest.

DELETE THIS MODULE, its tests (tests/test_txn_bridge.py) and every caller
marked "bridge" in the release after the one that introduces the install
engine: by then every installation has committed a transaction and has
/etc/thermite/installed.json. Gateway._adopt goes with it.

Installations made before the engine record their units in
/etc/thermite/units.json (version 1) and keep everything else at fixed
paths. The first upgrade describes that installation as the engine's
"current" manifest (engine.begin --current), so the transaction can set it
aside and put it back exactly like any later one. It describes only what it
can prove is Thermite's: the units the version 1 manifest records, exact
installer files at the other fixed paths, and the links systemctl made for
them. Anything else refuses the upgrade before anything changes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

import oh_deploy as deploy
from oh_install import engine, manifest, ownership

if TYPE_CHECKING:
    from oh_install.steps import Context
    from oh_install.system import System

#: The transaction id the description carries: it names no transaction.
DESCRIBED = "19700101T000000Z-000000"
#: The site commit the version 1 upgrade wrapper makes before the host phase.
PIN_SUBJECT = "Upgrade Thermite application"


class BridgeError(manifest.ManifestError):
    """The installation cannot be described: nothing was changed."""


def needed(system: System) -> bool:
    """Whether this is an installation from before the engine: units.json
    and no installed manifest."""
    return not system.lexists(manifest.INSTALLED) and system.lexists(ownership.MANIFEST)


def _refuse(problem: str) -> BridgeError:
    return BridgeError(f"cannot describe the installed version for its first install transaction: {problem}. "
                       "Nothing was changed.")


def _file(system: System, item: manifest.Item) -> dict:
    """A managed file as the engine records it, read without root: only a
    root-owned file not writable by others, through root-owned ancestors."""
    found = system.unit_file(item.path)
    if found is None or found.uid != 0 or found.mode & 0o022 or not found.ancestors_trusted:
        raise _refuse(f"{item.path} is not a root-owned file only root can change")
    record = {"path": str(item.path), "class": item.cls, "sha256": hashlib.sha256(found.content).hexdigest(),
              "mode": found.mode, "uid": found.uid, "gid": found.gid}
    for key in ("unit", "apply_unit"):
        if getattr(item, key) is not None:
            record[key] = getattr(item, key)
    return record


def _link(system: System, item: manifest.Item) -> dict:
    target = system.readlink(item.path)
    if target != str(item.target):
        raise _refuse(f"{item.path} does not link to {item.target}")
    return {"path": str(item.path), "class": "link", "target": target}


def _prove(context: Context, system: System) -> None:
    """Refuse unless the fixed paths hold what this installer wrote."""
    from oh_access import AccessError
    from oh_install import access, ingress, steps
    owned = ownership.inspect(context, system)
    if owned.problems:
        raise _refuse("; ".join(owned.problems))
    if not owned.recorded:
        raise _refuse(f"{ownership.MANIFEST} does not record exactly the unit files installed")
    if steps.boundary_owned(system) is False:
        raise _refuse(f"{ingress.UNIT_FILE} is not the installer's")
    if system.lexists(ingress.RULE_FILE):
        rules = system.unit_file(ingress.RULE_FILE)
        try:
            ingress.rule_ports(rules.content if rules else b"")
        except (ValueError, UnicodeError) as exc:
            raise _refuse(f"{ingress.RULE_FILE}: {exc}") from None
    try:
        access.installed_release(system)
    except (OSError, AccessError) as exc:
        raise _refuse(str(exc)) from None
    if not steps.installation_owned(context, system) or not steps.state_owned(context, system):
        raise _refuse(f"{context.installation.root} holds more than this installer created")


def _pins(context: Context, system: System) -> tuple[dict, list[str]]:
    """The installed images, read from the installed quadlets, and the
    pins of those this version also names."""
    refs = []
    for target in sorted(deploy.INSTALLED.values()):
        if target.suffix == ".container":
            found = system.unit_file(target)
            ref = deploy.unit_image(found.content.decode(errors="replace") if found else None)
            if ref and engine.IMAGE.fullmatch(ref):
                refs.append(ref)
    repositories = {deploy.image(context.versions, name).split("@")[0]: name for name in manifest.image_names(context)}
    pins = {repositories[ref.split("@")[0]]: ref.split("@", 1)[1] for ref in refs
            if "@" in ref and ref.split("@")[0] in repositories}
    return pins, sorted(set(refs))


def describe(context: Context, system: System, items: list[manifest.Item], tools: dict, at: str) -> dict:
    """The installed version as an installed manifest, over the items this
    version declares: managed files and links as they are now, the state it
    keeps as trees and shared directories, and Prometheus' samples with the
    pin of the image that wrote them. Versioned releases are not described:
    the transaction adds its own beside them, and older ones stay."""
    _prove(context, system)
    root = context.installation.root
    described = []
    for item in items:
        if not system.lexists(item.path):
            continue
        if engine.managed(root, item.path):
            described.append(_link(system, item) if item.cls == "link" else _file(system, item))
        elif item.cls in ("tree", "shared"):
            described.append({"path": str(item.path), "class": item.cls})
        elif item.cls == "shared-pinned":
            described.append({"path": str(item.path), "class": item.cls, "pin": item.image})
    pins, images = _pins(context, system)
    units = json.loads(system.unit_file(ownership.MANIFEST).content)
    record = {}
    raw = system.read_bytes(context.layout.record)
    if raw is not None:
        try:
            record = json.loads(raw)
        except ValueError:
            record = {}
    current = {
        "format": engine.MANIFEST_FORMAT, "installation": str(root), "transaction": DESCRIBED, "kept": DESCRIBED,
        "committed_at": at, "installed": True,
        "release": {"described": str(ownership.MANIFEST), "site": record.get("commit") if isinstance(record, dict) else None},
        "pins": pins, "tools": tools, "items": described, "images": {"current": images, "previous": []},
        "application": {"current": units.get("release") if isinstance(units.get("release"), str) else None,
                        "previous": None},
    }
    try:
        return engine.validate_manifest(current, root=str(root))
    except engine.EngineError as exc:
        raise _refuse(str(exc)) from None


def site_before(root: Path, site: Path, digest: str | None) -> tuple[str | None, str | None]:
    """The site revisions around the version 1 wrapper's pin commit, which
    it made before running this host phase without saying so: (before,
    after), or (None, None) when HEAD is not that commit. It is when HEAD
    has one parent, the subject the wrapper writes, changes only the site
    files the wrapper edits, and its site requirement pins release `digest`
    where its parent's did not."""
    from oh_source_upgrade import SITE_FILES, git
    if not digest:
        return None, None
    head = git(root, "rev-parse", "HEAD")
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if len(parents) != 1 or git(root, "log", "-1", "--format=%s", "HEAD") != PIN_SUBJECT:
        return None, None
    prefix = site.relative_to(root).as_posix()
    files = {(prefix + "/" if prefix != "." else "") + name for name in SITE_FILES}
    changed = set(filter(None, git(root, "diff", "--name-only", "-z", parents[0], head).split("\0")))
    if not changed or not changed <= files:
        return None, None
    pinned = (prefix + "/" if prefix != "." else "") + SITE_FILES[0]

    def pins(revision):
        try:
            requirement = json.loads(git(root, "show", f"{revision}:{pinned}"))
        except (subprocess.CalledProcessError, ValueError):
            return False
        return isinstance(requirement, dict) and requirement.get("release") == digest
    if not pins(head) or pins(parents[0]):
        return None, None
    return parents[0], head
