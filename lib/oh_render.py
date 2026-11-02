"""Generate conf/ from instance/devices.yaml (bin/thermite generate).

conf/ is output only. Every file in it comes from the inventory through the
integration modules (lib/oh_integrations/, one per integration, each
rendering the devices that list it) and then the house-wide features
(lib/oh_features/). Nothing else may be in conf/.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import oh_features as features
import oh_integrations as integrations
import oh_inventory as inventory
from oh_conf import GUARD_ADDON_WHY, RULE_SENDS, RULE_SILENT, Addon, Conf, House, dump, guard, linked, uses

#: openHAB's own metrics, which Prometheus scrapes (lib/oh_metrics); every
#: house has them, whatever its devices.
METRICS_ADDON = Addon("misc", "metrics", "openHAB's own metrics at /rest/metrics/prometheus, which Prometheus "
                                         "scrapes (lib/oh_metrics)")

from oh_paths import SITE as AREA
INVENTORY = AREA / "instance/devices.yaml"
CONF = AREA / "conf"


def validate(data: object) -> list[str]:
    """Every problem that stops a render: inventory, integrations, features."""
    problems = inventory.validate(data)
    if problems:
        # The per-module checks below assume a well-formed inventory.
        return problems
    assert isinstance(data, dict)
    for device in data.get("devices") or []:
        for module in integrations.REGISTRY:
            if uses(device, module.PHASE, module.NAME) and getattr(module, "NEEDS_IP", True) and not device.get("ip"):
                problems.append(f"{device['id']}: {module.NAME} needs the device's own ip")
            if (getattr(module, "CONTROL", False) and uses(device, "control", module.NAME)
                    and not uses(device, module.PHASE, module.NAME)):
                problems.append(f"{device['id']}: {module.NAME} under control needs it under {module.PHASE} too")
            problems += module.validate(device)
    for module in integrations.REGISTRY:
        if hasattr(module, "validate_house"):
            problems += module.validate_house(data)
    for feature in features.REGISTRY:
        problems += feature.validate(data)
    return problems


def build(data: dict) -> Conf:
    """The whole conf/ of a validated inventory."""
    house = House(data)
    conf = Conf()
    for module in integrations.REGISTRY:
        devices = [d for d in house.devices if uses(d, module.PHASE, module.NAME)]
        if not devices:
            continue
        out = conf.yaml_file(module.FILE, module.HEADER)
        for device in devices:
            module.render(device, house, out)
        if hasattr(module, "files"):
            conf.files.update(module.files(devices, house))
        for addon in module.addons(devices) if hasattr(module, "addons") else module.ADDONS:
            conf.add_addon(addon)
        for path, source in module.ASSETS.items():
            conf.add_asset(path, Path(module.__file__).parent / source)
    for feature in features.REGISTRY:
        feature.render(house, conf)
    if conf.guarded:
        conf.add_addon(Addon("automation", "jsscripting", GUARD_ADDON_WHY))
    conf.add_addon(METRICS_ADDON)
    return conf


def render(data: dict) -> dict[str, str | bytes]:
    """Path under conf/ -> content, for every file conf/ must hold (bytes for
    a file copied byte for byte)."""
    return build(data).render()


READ_ONLY_HEADER = (
    "Read-only copy for the sandbox or the boot check (oh_render.read_only):",
    "every HTTP binding channel is READONLY and every Item of another binding",
    "that could send a command is display-only, so it reads its devices and",
    "sends nothing.",
)
#: Bindings other than HTTP whose Items an integration's control phase links
#: read-write (oh_integrations CONTROL); read_only() makes those Items
#: display-only.
COMMANDING_BINDINGS = frozenset({"denonmarantz", "androidtv", "sonos"})


def read_only(files: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """conf files that read every device and command none. The sandbox and
    the boot check run on this: every HTTP binding channel is READONLY, every
    Item linked to a COMMANDING_BINDINGS channel is guarded
    (oh_conf.GUARD_NOTE), and a rule that talks to its devices itself has
    RULE_SENDS turned into RULE_SILENT. Every other read-write channel is
    already guarded."""
    out = dict(files)
    for path, text in files.items():
        if path.startswith("automation/js/") and isinstance(text, str):
            out[path] = silenced(path, text)
            continue
        if not (path.startswith("yaml/") and path.endswith(".yaml")):
            continue
        document = yaml.safe_load(text) or {}
        changed = False
        for uid, thing in (document.get("things") or {}).items():
            if uid.startswith("http:"):
                for channel in (thing.get("channels") or {}).values():
                    channel.setdefault("config", {})["mode"] = "READONLY"
                changed = True
        items = document.get("items") or {}
        for name, item in items.items():
            if any(uid.split(":")[0] in COMMANDING_BINDINGS for uid in linked(item)):
                items[name] = guard(item)
                changed = True
        if changed:
            out[path] = dump(READ_ONLY_HEADER, {key: document.get(key) or {} for key in ("things", "items", "pages")})
    return out


def silenced(path: str, text: str) -> str:
    """A rule with its RULE_SENDS line, if it has one, made RULE_SILENT."""
    lines = text.split("\n")
    sends = [index for index, line in enumerate(lines) if line == RULE_SENDS]
    if not sends:
        return text
    if len(sends) > 1:
        raise ValueError(f"{path}: {RULE_SENDS!r} appears {len(sends)} times")
    lines[sends[0]] = RULE_SILENT
    return "\n".join(lines)


def on_disk(conf: Path) -> set[str]:
    conf = Path(conf)
    return {p.relative_to(conf).as_posix() for p in conf.rglob("*") if p.is_file()} if conf.is_dir() else set()


def content(path: Path, like: str | bytes) -> str | bytes:
    """A file as bytes when `like` is bytes, else as text."""
    if isinstance(like, bytes):
        return Path(path).read_bytes()
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Path(path).read_bytes()


def put(path: Path, data: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def compare(conf: Path, files: dict[str, str | bytes]) -> tuple[list[str], list[str], list[str]]:
    """(stale, missing, stray) paths under conf/ against a render."""
    conf = Path(conf)
    present = on_disk(conf)
    stale = [p for p in files if p in present and content(conf / p, files[p]) != files[p]]
    missing = [p for p in files if p not in present]
    stray = sorted(present - set(files))
    return stale, missing, stray


def write(conf: Path, files: dict[str, str | bytes]) -> list[str]:
    """Write every file that differs; never delete. Returns the paths written."""
    conf = Path(conf)
    stale, missing, _ = compare(conf, files)
    for path in sorted(stale + missing):
        put(conf / path, files[path])
    return sorted(stale + missing)
