"""Generate conf/ from instance/devices.yaml (bin/thermite generate).

conf/ is output only. Every file in it comes from the inventory through the
integration modules (lib/oh_integrations/, one per integration, each
rendering the devices that list it) and then the house-wide features
(lib/oh_features/). Nothing else may be in conf/.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

import oh_features as features
import oh_integrations as integrations
import oh_inventory as inventory
import oh_ports
from oh_conf import (ADDONS_CFG, COMMAND_SCRIPTS, GUARD_ADDON_WHY, GUARD_PROFILE, RULE_SENDS, RULE_SILENT, Addon, Conf,
                     House, dump, guard, instance, instance_at, linked, uses)
from oh_features import remote_system

#: openHAB's own metrics, which Prometheus scrapes (lib/oh_metrics); every
#: house has them, whatever its devices.
METRICS_ADDON = Addon("misc", "metrics", "openHAB's own metrics at /rest/metrics/prometheus, which Prometheus "
                                         "scrapes (lib/oh_metrics)")

#: jUPnP's settings. openHAB's ConfigDispatcher applies each `pid:key=value`
#: line as a string as it starts, and updates a PID only when a value differs:
#: https://github.com/openhab/openhab-core/blob/5.2.3/bundles/org.openhab.core.config.dispatch/src/main/java/org/openhab/core/config/dispatch/internal/ConfigDispatcher.java
UPNP_CFG = "services/upnp.cfg"


def validate(data: object) -> list[str]:
    """Every problem that stops a render: inventory, integrations, features."""
    problems = inventory.validate(data)
    if problems:
        # The per-module checks below assume a well-formed inventory.
        return problems
    assert isinstance(data, dict)
    for device in data.get("devices") or []:
        for module in integrations.REGISTRY:
            if uses(device, module.PHASE, module.NAME) and module.NEEDS_IP and not device.get("ip"):
                problems.append(f"{device['id']}: {module.NAME} needs the device's own ip")
            if (module.CONTROL and uses(device, "control", module.NAME)
                    and not uses(device, module.PHASE, module.NAME)):
                problems.append(f"{device['id']}: {module.NAME} under control needs it under {module.PHASE} too")
            problems += module.validate(device)
    if problems:
        # The features below ask the integrations about their devices (a
        # remote's inputs, say), which assumes each device is one they know.
        return problems
    for feature in features.REGISTRY:
        problems += feature.validate(data)
    if problems:
        # An integration's house-wide check reads the sections the features
        # own (routing, say), which assumes each has the shape its feature
        # checked.
        return problems
    for module in integrations.defining("validate_house"):
        problems += module.validate_house(data)
    return problems


class Disagreement(inventory.ConfigError):
    """The conf/ built from a house does not agree with it: a Thing of no
    device or at another's address, an Item named after nothing
    (inventory.cross_check). build() raises it, so no such conf is written,
    staged or deployed. `conf` is what was built."""

    def __init__(self, problems: list[str], conf: Conf) -> None:
        super().__init__(None, f"what the house file generates disagrees with it, in {len(problems)} "
                               f"place{'s' if len(problems) != 1 else ''}; nothing done", problems=problems)
        self.conf = conf


def objects(conf: Conf) -> tuple[dict, dict, list[str]]:
    """The Things and Items a Conf defines, by UID and by name, each with the
    file that defines it and its body (as inventory.cross_check takes them),
    and the problems of the definitions themselves: one that two files
    define, and a file under yaml/, things/ or items/ that is not one of the
    Conf's own (Conf.yaml_file), so that none escapes the check by being
    written as text or in the other syntax."""
    things: dict[str, dict] = {}
    items: dict[str, dict] = {}
    problems: list[str] = []
    for path, file in sorted(conf.yaml.items()):
        for table, kind, defined in ((things, "Thing", file.things), (items, "Item", file.items)):
            for key, body in defined.items():
                if str(key) in table:
                    problems.append(f"conf/{path}: {kind} {key} also defined in {table[str(key)]['file']}")
                table[str(key)] = {"file": f"conf/{path}", "body": body or {}}
    problems += [f"conf/{path}: a file that can define Things or Items must be one of Conf.yaml_file's, "
                 "which the house is checked against"
                 for path in sorted(conf.files) if path.startswith(("yaml/", "things/", "items/"))]
    return things, items, problems


def build(house: House, *, host: dict | None = None, versions: dict | None = None) -> Conf:
    """The whole conf/ of a house, from what it is given and the
    application's own files: it looks for no site. `host` is
    instance/host.yaml as oh_host read it, which allocates the listener
    ports (the product's default ones without it), and `versions` is
    versions.json, which names the runtime (the application's own pins
    without it). The files the house names are read from beside its house
    file: the house's own directory (House.directory) for as long as the
    build runs, whichever house file the process read last. What it
    returns agrees with the house (inventory.cross_check), or it raises
    Disagreement."""
    with instance_at(house.directory if house.directory is not None else instance()):
        return _build(house, host, versions)


def _build(house: House, host: dict | None, versions: dict | None) -> Conf:
    conf = Conf(system_metadata=remote_system.described(host, versions))
    for module in integrations.REGISTRY:
        devices = [d for d in house.devices if uses(d, module.PHASE, module.NAME)]
        if not devices:
            continue
        out = conf.yaml_file(module.FILE, module.HEADER)
        for device in devices:
            module.render(device, house, out)
        if module.files:
            conf.files.update(module.files(devices, house))
        for addon in module.addons(devices) if module.addons else module.ADDONS:
            conf.add_addon(addon)
        for path, source in module.ASSETS.items():
            conf.add_asset(path, Path(module.__file__).parent / source)
    for feature in features.REGISTRY:
        feature.render(house, conf)
    if conf.guarded:
        conf.add_addon(Addon("automation", "jsscripting", GUARD_ADDON_WHY))
    conf.add_addon(METRICS_ADDON)
    conf.files["services/runtime.cfg"] = (
        "# Generated by bin/thermite generate; do not edit. See docs/ports.md.\n"
        f"org.openhab.lsp:port={oh_ports.LSP_PORT}\n"
    )
    if any(module.relayed(device) for module in integrations.defining("relayed") for device in house.devices):
        # The gateway's callback relay (oh_access.native_boundary) receives
        # these devices' UPnP events, so jUPnP advertises its port. openHAB
        # takes it as it starts: changed while it runs, jUPnP restarts and
        # openHAB answers every NOTIFY 501 until it restarts.
        conf.files[UPNP_CFG] = (
            "# Generated by bin/thermite generate; do not edit. See docs/ports.md.\n"
            f"org.jupnp:httpProxyPort={oh_ports.effective(host)['callbacks']}\n"
        )
    things, items, problems = objects(conf)
    problems += inventory.cross_check(house, things, items)
    if problems:
        raise Disagreement(problems, conf)
    return conf


def render(house: House, *, host: dict | None = None, versions: dict | None = None) -> dict[str, str | bytes]:
    """Path under conf/ -> content, for every file conf/ must hold (bytes for
    a file copied byte for byte); build()'s inputs."""
    return build(house, host=host, versions=versions).render()


READ_ONLY_HEADER = (
    "Read-only copy for the sandbox or the boot check (oh_render.read_only):",
    "every HTTP binding channel is READONLY and every Item linked to any other",
    "channel is display-only, so it reads its devices and sends nothing.",
)
#: Rules under automation/js/ that hold no RULE_SENDS because they open no
#: connection of their own: they command Items only, which read_only()
#: guards, or watch the runtime. Any other rule without a SEND declaration
#: is refused. The house-wide ones here, and those integrations and
#: features declare (ITEM_ONLY_RULES).
ITEM_ONLY_RULES = frozenset({
    "automation/js/all_off.js", "automation/js/routing.js", "automation/js/thermite_entry.js",
    "automation/js/node_modules/thermite-routing-lifecycle/index.js",
    *integrations.declared("ITEM_ONLY_RULES", (*integrations.REGISTRY, *features.REGISTRY)),
})
#: RULE_SENDS, however it is spaced.
SEND_DECLARATION = re.compile(r"^(\s*)(?:const|let|var)\s+SEND\s*=\s*true\s*;?\s*$")


def safe_link(uid: str, link: object, readonly: set[str]) -> bool:
    """Whether a link carries no command: its channel is READONLY, or it is a
    guard (oh_conf.GUARD_NOTE) that passes no command on."""
    return uid in readonly or (isinstance(link, dict) and link.get("profile") == GUARD_PROFILE
                               and not COMMAND_SCRIPTS & set(link))


def scripted(files: dict[str, str | bytes], why: str) -> str:
    """The files' addons.cfg, asking for the JS add-on too: a copy of conf
    that adds something the add-on runs (the read-only copy's guard, the
    sandbox's page entry) to a house whose own rules need none."""
    addons = str(files.get(ADDONS_CFG, ""))
    asked = re.search(r"^automation\s*=(.*)$", addons, re.MULTILINE)
    names = [name.strip() for name in asked[1].split(",") if name.strip()] if asked else []
    if "jsscripting" in names:
        return addons
    line = "automation = " + ",".join(sorted([*names, "jsscripting"]))
    if asked:
        return addons[:asked.start()] + line + addons[asked.end():]
    return addons + f"\n# jsscripting: {why}.\n{line}\n"


def read_only(files: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """conf files that read every device and command none. The sandbox and
    the boot check run on this. It fails closed: every HTTP binding channel
    is made READONLY; every Item with a link that is neither to a READONLY
    channel (one that was WRITEONLY does not count) nor a guard is guarded
    (oh_conf.GUARD_NOTE), whatever its binding; a rule has its SEND
    declaration turned into RULE_SILENT (and
    avpro_telnet's, vrroom_telnet's and sony_adcp's then read nothing
    either: the matrix and the switch serve few sessions, the projector one
    request at a time, and the live runtime holds theirs; the reboots rule
    opens no USB matrix console); and a rule with none (but ITEM_ONLY_RULES),
    or Things or Items in a format it cannot read, is a ValueError. A copy
    that guards an Item asks for the add-on the guard runs on (scripted)."""
    out = dict(files)
    for path, text in files.items():
        if path.startswith(("things/", "items/")):
            raise ValueError(f"{path}: the read-only copy reads only conf/yaml/")
        if path.startswith("automation/") and not (path.startswith("automation/js/") and path.endswith(".js")):
            if path.endswith(".jar") and path.startswith("automation/js/node_modules/"):
                continue
            raise ValueError(f"{path}: the read-only copy runs only JavaScript rules")
        if path.startswith("automation/js/"):
            if not isinstance(text, str):
                raise ValueError(f"{path}: a rule that is not text cannot be made read-only")
            out[path] = silenced(path, text)
    documents = {path: yaml.safe_load(text) or {} for path, text in files.items()
                 if path.startswith("yaml/") and path.endswith(".yaml")}
    readonly: set[str] = set()
    changed: set[str] = set()
    for path, document in documents.items():
        for uid, thing in (document.get("things") or {}).items():
            for channel_id, channel in (thing.get("channels") or {}).items():
                # A command still refreshes the poll a channel shares, READONLY
                # or not (HttpThingHandler 5.2.3), so the Items of one that
                # only takes commands are guarded too.
                commanded = ((channel or {}).get("config") or {}).get("mode") == "WRITEONLY"
                if uid.startswith("http:"):
                    channel.setdefault("config", {})["mode"] = "READONLY"
                    changed.add(path)
                if not commanded and ((channel or {}).get("config") or {}).get("mode") == "READONLY":
                    readonly.add(f"{uid}:{channel_id}")
    guarded = False
    for path, document in documents.items():
        items = document.get("items") or {}
        for name, item in items.items():
            links = item.get("channels") if isinstance(item.get("channels"), dict) else {}
            if not all(safe_link(uid, links.get(uid), readonly) for uid in linked(item)):
                items[name] = guard(item)
                changed.add(path)
                guarded = True
    if guarded:
        out[ADDONS_CFG] = scripted(files, "the transform:JS profile that keeps the read-only copy's Items "
                                          "display-only")
    for path in changed:
        document = documents[path]
        out[path] = dump(READ_ONLY_HEADER, {key: document.get(key) or {} for key in ("things", "items", "pages")})
    return out


def silenced(path: str, text: str) -> str:
    """A rule with its SEND declaration (RULE_SENDS, however spaced) made
    RULE_SILENT; a rule with none must be one of ITEM_ONLY_RULES."""
    lines = text.split("\n")
    sends = [index for index, line in enumerate(lines) if SEND_DECLARATION.match(line)]
    if not sends:
        if path in ITEM_ONLY_RULES:
            return text
        raise ValueError(f"{path}: a rule with no {RULE_SENDS!r} line cannot be made read-only")
    if len(sends) > 1:
        raise ValueError(f"{path}: {RULE_SENDS!r} appears {len(sends)} times")
    lines[sends[0]] = SEND_DECLARATION.match(lines[sends[0]]).group(1) + RULE_SILENT
    return "\n".join(lines)


#: Where a test copy addresses what it must not reach. A TCP connection to the
#: limited broadcast address fails in the kernel (ENETUNREACH), and a datagram
#: or ping to it is refused without SO_BROADCAST (EACCES), so nothing leaves
#: the host; a documentation or reserved address would still go to the router.
NOWHERE = "255.255.255.255"
#: A Sonos UDN no player has (an all-zero MAC): the binding finds its players
#: by UDN.
NO_SONOS = "RINCON_00000000000001400"
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
SONOS_UDN = re.compile(r"RINCON_[0-9A-F]{12}01400")
#: jupnp never uses a loopback interface, so naming only lo leaves it no
#: network and its router off: it neither hears devices announce themselves
#: nor fetches their descriptions. It replaces the whole UPNP_CFG: a test
#: copy has no callback relay.
NO_UPNP = "org.jupnp:interfaces=lo\n"
#: The files of the integrations that keep a session open with each device
#: (SESSIONS), which the sandbox leaves to the runtime.
SESSION_FILES = frozenset(module.FILE for module in integrations.REGISTRY if module.SESSIONS)


def unaddressed(files: dict[str, str | bytes], paths: frozenset[str] | None = None) -> dict[str, str | bytes]:
    """conf files that reach no device: every IPv4 address in the files
    openHAB reads (all but html/, which a browser runs), or in `paths` only,
    is NOWHERE, every Sonos UDN NO_SONOS, and jupnp has no network. Loopback
    goes too: the Apple TV helper there talks to Apple TVs. The boot check
    runs on this over read_only(); the sandbox, which still reads the other
    devices, over read_only() with paths SESSION_FILES."""
    out = dict(files)
    for path, text in files.items():
        if isinstance(text, str) and not path.startswith("html/") and (paths is None or path in paths):
            out[path] = SONOS_UDN.sub(NO_SONOS, IPV4.sub(NOWHERE, text))
    out[UPNP_CFG] = NO_UPNP
    return out


def unrelayed(files: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """conf files for a runtime without the gateway's callback relay (the
    live sandbox): no UPNP_CFG, so jUPnP advertises the runtime's own port."""
    return {path: text for path, text in files.items() if path != UPNP_CFG}


def direct(files: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """conf files for a runtime with no gateway in front of it (the
    sandbox): the runtime serves the pages at their own paths (oh_entry)
    and they talk to it directly, with no login (oh_access_mode). What
    thermite generate writes has neither: an install's pages go through
    the gateway."""
    import oh_access_mode
    import oh_entry
    return {**files, **oh_entry.files(), ADDONS_CFG: scripted(files, "the runtime's own page entry (oh_entry)"),
            "html/remote/access-config.js": oh_access_mode.script("native")}


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
