"""What a version installs, as the install engine records it.

Each step that installs something declares it (Step.desired) as items: a
path and the class that says how the engine sets it aside and puts it back.
desired() unites them into the set a transaction installs. The installed
manifest, /etc/thermite/installed.json, is the engine's record of the set a
committed transaction installed; installed() reads it, and only the engine
writes it.
"""
from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import secrets
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import oh_deploy as deploy
import oh_apparmor
import oh_metrics as metrics
import oh_units
from oh_install import engine

if TYPE_CHECKING:
    from oh_install.model import Step
    from oh_install.steps import Context
    from oh_install.system import System

#: The installed manifest.
INSTALLED = Path("/") / engine.MANIFEST
#: Where systemd keeps what `systemctl enable` links into thermite.target.
UNITS = Path("/etc/systemd/system")
WANTS = UNITS / f"{oh_units.TARGET}.wants"
#: Root's tool directories, searched before the operator's PATH: nft and
#: apparmor_parser live in sbin on some distributions.
SEARCH = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
#: The version's container images, by their versions.json names.
IMAGES = ("openhab", "atv_helper")


class ManifestError(ValueError):
    """The installed manifest is missing or damaged while Thermite is installed."""


@dataclasses.dataclass(frozen=True)
class Item:
    """One path a version owns and its engine class (engine.CLASSES)."""

    path: Path
    cls: str
    unit: str | None = None
    apply_unit: str | None = None
    target: Path | None = None
    #: For shared-pinned: the image whose pin decides whether it is copied.
    image: str | None = None
    value: str | None = None

    def to_json(self) -> dict:
        data = {"path": str(self.path), "class": self.cls}
        for key, field in (("unit", "unit"), ("apply_unit", "apply_unit"), ("pin", "image"), ("value", "value")):
            if getattr(self, field) is not None:
                data[key] = getattr(self, field)
        if self.target is not None:
            data["target"] = str(self.target)
        return data


def unit(path: Path, name: str | None = None) -> Item:
    """A file systemd loads; a drop-in names the unit it extends."""
    if name is None:
        name = path.parent.name.removesuffix(".d") if path.parent.name.endswith(".d") else (
            path.stem + ".service" if path.suffix == ".container" else path.name)
    return Item(Path(path), "unit", unit=name)


def link(name: str, wanted_by: str = oh_units.TARGET) -> Item:
    """The enablement link `systemctl enable` makes for a unit in UNITS."""
    return Item(UNITS / f"{wanted_by}.wants" / name, "link", target=UNITS / name)


def wants_links(text: str, name: str) -> list[Item]:
    """The links into thermite.target that enabling a unit makes, read from
    its own rendered [Install] section; a unit wanted by anything else is
    the boot target's business (the guard), not an item."""
    section, wanted = None, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Install]" and line.startswith("WantedBy="):
            wanted += line.removeprefix("WantedBy=").split()
    return [link(name) for target in wanted if target == oh_units.TARGET]


def desired(steps: Sequence[Step], system: System, root: Path) -> list[Item]:
    """Every item the steps declare, once each and sorted, checked as the
    engine checks a desired set: allowed paths, and no item inside another."""
    found: dict[Path, Item] = {}
    for step in steps:
        for item in step.desired(system):
            if found.get(item.path, item) != item:
                raise ManifestError(f"{item.path} is declared as both {found[item.path].cls} and {item.cls}")
            found[item.path] = item
    items = [found[path] for path in sorted(found)]
    try:
        engine.check_items([item.to_json() for item in items], str(root), recorded=False)
    except engine.EngineError as exc:
        raise ManifestError(str(exc)) from None
    return items


def installed(system: System, root: Path) -> dict | None:
    """The installed manifest, checked in full; None when there is none.
    A damaged one is never read past: ManifestError says what is wrong."""
    raw = system.read_bytes(INSTALLED)
    if raw is None:
        if system.lexists(INSTALLED):
            raise ManifestError(f"{INSTALLED} cannot be read")
        return None
    try:
        return engine.parse_manifest(raw, root=str(root))
    except engine.EngineError as exc:
        raise ManifestError(f"{INSTALLED} is damaged: {exc}") from None


def thermite_present(context: Context, system: System) -> list[Path]:
    """The fixed paths an installation of Thermite leaves, found here. Only
    these are probed: nothing is scanned or guessed."""
    from oh_install import ingress, ownership
    paths = [*deploy.INSTALLED.values(), ingress.UNIT_FILE, ingress.RULE_FILE, oh_apparmor.CONFIG,
             oh_apparmor.POLICY, *(UNITS / name for name in oh_units.ACCESS_UNITS), ownership.MANIFEST]
    found = [path for path in paths if system.lexists(path)]
    root = context.installation.root
    if system.directory_entries(root):
        found.append(root)
    return found


def missing_recovery(context: Context) -> str:
    """What to do when Thermite's files are here but its manifest is not."""
    from oh_install import ingress
    root = context.installation.root
    site = shlex.quote(str(context.area))
    return (f"Thermite's files are installed here, but {INSTALLED} is missing or damaged, so nothing can "
            "prove which of them this installation owns. Nothing was changed. To recover, as root: "
            f"stop Thermite (systemctl stop {oh_units.TARGET} {oh_units.SLICE}); move its units "
            f"({UNITS}/thermite*, /etc/containers/systemd/thermite*, {WANTS}), {ingress.RULE_FILE}, "
            f"{oh_apparmor.CONFIG} and {oh_apparmor.POLICY} aside, out of those directories; run "
            f"systemctl daemon-reload; then run thermite install --config {site}. The state under {root} "
            "stays where it is; see docs/installation.md#a-missing-manifest.")


def tools(system: System) -> dict:
    """The programs the engine may run, by absolute path: found in root's
    tool directories first. The engine checks that root owns each."""
    found = {}
    for name in engine.TOOLS:
        path = next((str(Path(directory) / name) for directory in SEARCH
                     if (info := system.stat(Path(directory) / name)) is not None and not info.is_dir), None)
        if path is None:
            path = system.which(name)
            path = path if path and Path(path).is_absolute() else None
        if path is None and name not in engine.OPTIONAL_TOOLS:
            raise ManifestError(f"no {name} program here; the install engine needs it")
        found[name] = path
    return found


def image_names(context: Context) -> list[str]:
    """The versions.json names of every image this version runs."""
    return list(dict.fromkeys([*IMAGES, *(service.image for service in metrics.services(context.host))]))


def pins(context: Context) -> dict[str, str]:
    """Each image's pinned digest, by versions.json name."""
    return {name: context.versions[name]["image"]["digest"] for name in image_names(context)}


def images(context: Context) -> list[str]:
    """Each pinned image as the units name it: repository@digest."""
    return [deploy.image(context.versions, name) for name in image_names(context)]


def transaction_id(clock=time.time) -> str:
    """A new transaction id: the UTC second it began, then 6 random hex."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock())) + "-" + secrets.token_hex(3)


def relative(path, root) -> str:
    """A path below the installation root, as the owner reads it."""
    path = PurePosixPath(path)
    return str(path.relative_to(root)) if engine.below(path, root) else str(path)
