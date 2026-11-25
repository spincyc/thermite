"""Recognize Thermite's installed unit files before changing or removing them.

A root-owned regular unit file is Thermite's when its exact bytes are those
the installed manifest records for its path (/etc/thermite/installed.json,
which only the install engine writes), or the unit the current committed
inputs render: what a transaction has just installed and not yet committed.
An effective unit must also resolve to that file without unowned drop-ins.
Anything else in the namespace belongs to someone else and fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import oh_deploy as deploy
import oh_units
from oh_install import engine

if TYPE_CHECKING:
    from oh_install.steps import Context
    from oh_install.system import System

#: The installed manifest: the record of what a committed transaction installed.
INSTALLED = Path("/") / engine.MANIFEST


def unit_name(path: Path) -> str:
    if path.suffix == ".conf" and path.parent.name.endswith(".service.d"):
        return path.parent.name.removesuffix(".d")
    return path.stem + ".service" if path.suffix == ".container" else path.name


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recorded(context: Context, system: System) -> dict[str, str]:
    """path -> sha256 of the files the installed manifest records, when root
    custody protects it; nothing when there is none or it proves nothing."""
    file = system.unit_file(INSTALLED)
    if file is None or file.uid != 0 or file.mode & 0o022 or not file.ancestors_trusted:
        return {}
    try:
        installed = engine.parse_manifest(file.content, root=str(context.installation.root))
    except engine.EngineError:
        return {}
    return {item["path"]: item["sha256"] for item in installed["items"] if "sha256" in item}


def _template(name: str) -> bool:
    return "@." in name


@dataclass(frozen=True)
class Owned:
    # canonical role, effective unit name, source file
    entries: tuple[tuple[str, str, Path], ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def runtime_bound(self) -> bool:
        return any(role == oh_units.RUNTIME and path.suffix == '.container' for role, _, path in self.entries)

    def units(self, roles=None) -> list[str]:
        return list(dict.fromkeys([name for _, name, _ in self.entries] if roles is None else
                                  [name for selected in roles for role, name, _ in self.entries if role == selected]))


def inspect(context: Context, system: System) -> Owned:
    entries, problems = [], []
    try:
        templates = deploy.render_units(context.versions, context.host, getattr(context, "house", None))
    except (ValueError, KeyError, TypeError) as exc:
        return Owned(problems=(f"cannot validate generated service ownership: {exc}",))
    manifest = recorded(context, system)
    candidates = []
    # A share's mount unit is named after its mount point: only this host's
    # are known here. One a host no longer has is the install engine's to
    # set aside, by the installed manifest.
    units = deploy.every_unit(context.host)
    for source, target in units.items():
        role = unit_name(target)
        if not system.safe_unit_parent(target.parent):
            problems.append(f"unsafe unit directory {target.parent}; refusing to install or remove files through it")
        candidates.append((source, target, role))
    present = {target for _, target, _ in candidates if system.lexists(target)}
    matched = set()
    for source, path, _ in candidates:
        if path not in present:
            continue
        file = system.unit_file(path)
        if file is None or file.uid != 0 or file.mode & 0o022 or not file.ancestors_trusted:
            problems.append(f"cannot prove Thermite ownership of {path}; preserve it and resolve the collision")
            continue
        # A unit this host no longer renders (music) is proven by the record
        # alone.
        template = templates[source].encode() if source in templates else None
        if manifest.get(str(path)) != sha256(file.content) and file.content != template:
            problems.append(f"cannot prove Thermite ownership of {path}; it is neither what {INSTALLED} records "
                            "nor this site's generated unit; preserve it and resolve the collision")
            continue
        matched.add(path)
    # A template (the share gate) is never a unit systemd shows; its file's
    # proof above is all there is to it.
    names = list(dict.fromkeys(role for _, _, role in candidates if not _template(role)))
    origins = system.unit_origins(names)
    dropins = {role: units[source] for role, source in deploy.CONTAINER_DROPINS.items()}
    primaries = {unit_name(path): path for path in units.values() if path.suffix != '.conf'}
    for _, path, name in candidates:
        if path in present and path not in matched:
            continue
        if _template(name):
            if path in matched:
                entries.append((name, name, path))
            continue
        role = name
        origin = origins[name]
        if not origin.known:
            problems.append(f"cannot inspect {name}; systemd did not return a complete unit state")
            continue
        if origin.transient is not False:
            problems.append(f"{name} is transient or has unknown unit custody; refusing to change it")
            continue
        declared = {str(dropins[role])} if role in dropins else set()
        proven = {str(dropins[role])} if declared and dropins[role] in matched else set()
        actual = set(origin.drop_ins.split())
        active = origin.active in ('active', 'activating', 'reloading', 'deactivating')
        if not actual <= proven or (active and role in dropins and actual != declared):
            problems.append(f"{name} has missing or unowned lifecycle drop-ins; refusing to change it")
            continue
        if path not in matched:
            # Missing support files can be repaired on an inactive owned unit.
            # A missing primary may have only our orphaned drop-in, never a
            # different effective service occupying its name.
            implicit_slice = (role == oh_units.SLICE and origin.load_state == 'loaded' and origin.active == 'inactive'
                              and not origin.source and not origin.fragment and not actual)
            if path.suffix != '.conf' and not implicit_slice and (origin.load_state != 'not-found' or origin.source or origin.fragment):
                problems.append(f"{name} already resolves to another unit; preserve it and resolve the collision")
            continue
        primary = primaries[role] if path.suffix == '.conf' else path
        bound = (origin.source == str(primary) if primary.suffix == '.container' else origin.fragment == str(primary))
        loaded = bool(origin.source or origin.fragment)
        if (loaded or active or origin.enabled == 'enabled') and not bound:
            problems.append(f"{name} does not resolve exclusively to {primary}; refusing to stop or replace it")
            continue
        if path.suffix == '.conf' and loaded and primary not in matched:
            problems.append(f"{name} has no proven primary unit for its lifecycle drop-in")
            continue
        entries.append((role, name, path))
    return Owned(tuple(entries), tuple(dict.fromkeys(problems)))
