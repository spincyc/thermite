"""Keep pairing credentials out of openHAB's native logs, and send its own log to the journal.

Item command events and REST DEBUG messages include command values. Filters on
each native logger drop messages naming a pairing command Item.
Pax Logging bypasses Log4j2's global filter on its direct backend path, so a
global filter alone is insufficient. Status/result Items and ordinary commands
remain visible. Install it while openHAB is stopped; editing the XML alone does
not prove that a running logger has reloaded it.

What openhab.log gets (the Root logger's routes: bindings, rule engines, the
JavaScript rules' console lines) also goes to the JOURNAL Console appender,
the container's stdout, which Podman's journald driver files under
thermite-openhab.service with SYSLOG_IDENTIFIER and CONTAINER_NAME
thermite-openhab. Any journal reader (root, adm, systemd-journal, and wheel
where the distribution grants it) reads it without sudo:

    journalctl -u thermite-openhab.service

This is safer than opening userdata/logs to the thermite group. That would
need group traversal into the service-private userdata, whose jsondb holds
users, API tokens and Thing credentials and whose siblings hold pairing
material; group-readable log files against the runtime's 0077 umask through
every rotation; and weaker custody checks (oh_service). The journal changes
no file mode, and journald bounds, rotates and rate-limits what it keeps.
openhab.log stays as it was. events.log stays file-only: the openhab.event
logger is not additive, and its item traffic is voluminous and records the
household's activity. Every logger scope keeps the pairing guard first, and
the journal appender carries it as well, so the new route sees no pairing
payload: protected() and journaled() check both.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

from oh_ports import HTTP_PORT, native_etc, open_directory, read_regular, replace_regular

RELATIVE = Path("etc/log4j2.xml")
SELECTOR = Path("etc/org.ops4j.pax.logging.cfg")
DEFAULT_SELECTOR = b"org.ops4j.pax.logging.log4j2.config.file=${karaf.etc}/log4j2.xml\n"
# Both "Item 'example_pairing' received command ..." and REST's
# "items/example_pairing ... value ...", including multiline payloads.
PATTERN = r"(?s).*\b[A-Za-z_][A-Za-z0-9_]*_pairing\b.*"
ATTRIBUTES = {"regex": PATTERN, "onMatch": "DENY", "onMismatch": "NEUTRAL", "useRawMsg": "false"}
PAIRING_RULES = frozenset(f"automation/js/{name}.js" for name in
                         ("sony_ip_control", "androidtv_googletv", "pyatv_http"))
#: The Root logger's journal route: a Console appender on the JVM's stdout.
#: journald stamps each line, so it carries no time, and the logger's full
#: name, so rules' lines can be found (org.openhab.automation.jsscripting.file.<script>).
JOURNAL = "THERMITE_JOURNAL"
JOURNAL_PATTERN = "[%-5.5p] [%c] - %m%n"


def pairing_conf(files: dict) -> bool:
    """Every pairing integration uses Item commands, including Android/Apple-only houses."""
    return any(path in PAIRING_RULES or (path.endswith((".yaml", ".yml"))
               and re.search(rb"(?m)^\s+pairing:\s*$", blob.data)) for path, blob in files.items())


def selected_default(data: bytes) -> bool:
    """Accept the pinned native selector; unsupported selectors fail closed."""
    key = "org.ops4j.pax.logging.log4j2.config.file"
    try:
        lines = [line.strip() for line in data.decode("utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", "!"))]
    except UnicodeError:
        return False
    values = [match[1].strip() for line in lines
              if (match := re.fullmatch(re.escape(key) + r"\s*[=:]\s*(.*)", line))]
    return len(lines) == 1 and values == ["${karaf.etc}/log4j2.xml"]


def _selected(etc: int, userdata: Path) -> None:
    if not selected_default(read_regular(etc, SELECTOR.name)[0]):
        raise ValueError(f"{Path(userdata) / SELECTOR} selects an unsupported native logging configuration; "
                         "expected org.ops4j.pax.logging.log4j2.config.file=${karaf.etc}/log4j2.xml")


def document(data: bytes) -> ET.Element:
    root = ET.fromstring(data, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    if root.tag != "Configuration" or root.get("strict", "false").lower() == "true":
        raise ValueError("expected a non-strict Log4j2 Configuration XML document")
    return root


def filters(root: ET.Element) -> list[ET.Element]:
    return [child for child in root if isinstance(child.tag, str)
            and (child.tag.lower() == "filters" or child.tag.lower().endswith("filter"))]


def is_guard(element: ET.Element) -> bool:
    return element.tag == "RegexFilter" and element.attrib == ATTRIBUTES


def scopes(root: ET.Element) -> list[ET.Element]:
    """Protect Pax's backend path, including custom non-additive loggers."""
    result = [root]
    for container in root:
        if isinstance(container.tag, str) and container.tag.lower() == "loggers":
            result.extend(child for child in container if isinstance(child.tag, str)
                          and child.tag.lower() in ("logger", "root", "asynclogger", "asyncroot"))
    if sum(scope.tag.lower() in ("root", "asyncroot") for scope in result) != 1:
        # Otherwise Log4j2 creates an implicit, unfiltered ERROR root route.
        raise ValueError("native logging must contain exactly one explicit Root or AsyncRoot logger")
    return result


def scope_protected(root: ET.Element) -> bool:
    groups = filters(root)
    if len(groups) != 1:
        return False
    first = groups[0]
    if first.tag.lower() == "filters":
        children = [child for child in first if isinstance(child.tag, str)]
        return bool(children) and is_guard(children[0])
    return is_guard(first)


def protected(data: bytes) -> bool:
    """Every logger must reject pairing messages before a custom ACCEPT."""
    try:
        return all(scope_protected(scope) for scope in scopes(document(data)))
    except (ValueError, ET.ParseError):
        return False


def guard_scope(root: ET.Element) -> None:
    existing = filters(root)
    position = list(root).index(existing[0]) if existing else 0
    group = ET.Element("Filters")
    group.append(ET.Element("RegexFilter", ATTRIBUTES))
    for old in existing:
        root.remove(old)
        children = list(old) if old.tag.lower() == "filters" else [old]
        for child in children:
            if not is_guard(child):
                group.append(child)
    # ScriptFilter's ScriptRef must be created after its <Scripts> declaration.
    # Keep the existing filter's position, and any declarations before it.
    declarations = [index + 1 for index, child in enumerate(root)
                    if isinstance(child.tag, str) and child.tag.lower() in ("properties", "scripts")]
    root.insert(max([position, *declarations]), group)


def journal_appender() -> ET.Element:
    """The journal route, guarded itself: a pairing payload that reached it
    any other way than through a guarded logger would still be dropped."""
    appender = ET.Element("Console", {"name": JOURNAL, "target": "SYSTEM_OUT"})
    appender.append(ET.Element("RegexFilter", ATTRIBUTES))
    appender.append(ET.Element("PatternLayout", {"pattern": JOURNAL_PATTERN}))
    return appender


def _shape(element: ET.Element) -> tuple:
    return (element.tag, sorted(element.attrib.items()),
            [_shape(child) for child in element if isinstance(child.tag, str)])


def _containers(root: ET.Element) -> list[ET.Element]:
    return [child for child in root if isinstance(child.tag, str) and child.tag.lower() == "appenders"]


def _root_logger(root: ET.Element) -> ET.Element:
    return next(scope for scope in scopes(root)[1:] if scope.tag.lower() in ("root", "asyncroot"))


def _journal_refs(logger: ET.Element) -> list[ET.Element]:
    return [child for child in logger if isinstance(child.tag, str)
            and child.tag.lower() == "appenderref" and child.get("ref") == JOURNAL]


def journaled(data: bytes) -> bool:
    """openhab.log's routes also reach the journal: exactly one appender
    named JOURNAL, exactly journal_appender(), and one plain reference to it
    on the Root logger."""
    try:
        root = document(data)
        named = [appender for container in _containers(root) for appender in container
                 if isinstance(appender.tag, str) and appender.get("name") == JOURNAL]
        refs = _journal_refs(_root_logger(root))
    except (ValueError, ET.ParseError):
        return False
    return (len(named) == 1 and _shape(named[0]) == _shape(journal_appender())
            and len(refs) == 1 and _shape(refs[0]) == ("AppenderRef", [("ref", JOURNAL)], []))


def configured(data: bytes) -> bool:
    """What installation leaves: every route guarded, and openhab.log's journaled."""
    return protected(data) and journaled(data)


def _append(parent: ET.Element, child: ET.Element) -> None:
    """Append in the parent's existing indentation, for whoever reads the file."""
    if len(parent):
        child.tail = parent[-1].tail
        parent[-1].tail = parent.text
    parent.append(child)


def journal_route(root: ET.Element) -> None:
    """Replace any appender named JOURNAL, and any Root reference to it, with exactly ours."""
    containers = _containers(root)
    if not containers:
        loggers = next((index for index, child in enumerate(root)
                        if isinstance(child.tag, str) and child.tag.lower() == "loggers"), len(root))
        root.insert(loggers, ET.Element("Appenders"))
        containers = _containers(root)
    for container in containers:
        for appender in [child for child in container if isinstance(child.tag, str) and child.get("name") == JOURNAL]:
            container.remove(appender)
    _append(containers[0], journal_appender())
    logger = _root_logger(root)
    for ref in _journal_refs(logger):
        logger.remove(ref)
    _append(logger, ET.Element("AppenderRef", {"ref": JOURNAL}))


def guarded(data: bytes, *, journal: bool = True) -> bytes:
    """Prepend DENY at every native route without replacing existing
    configuration; with `journal`, also route openhab.log to the journal."""
    if protected(data) and (not journal or journaled(data)):
        return data
    root = document(data)
    for scope in scopes(root):
        if not scope_protected(scope):
            guard_scope(scope)
    if journal and not journaled(ET.tostring(root)):
        journal_route(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _install_at(etc: int, userdata: Path, *, journal: bool = True) -> bool:
    _selected(etc, userdata)
    before, info = read_regular(etc, RELATIVE.name)
    after = guarded(before, journal=journal)
    if before == after:
        return False
    replace_regular(etc, RELATIVE.name, after, info)
    return True


def install(userdata: Path, *, anchored: bool = False, journal: bool = True) -> bool:
    """Atomically protect an initialized, stopped instance, preserving ownership/mode.

    No symbolic link below userdata is followed and hard-linked files are
    refused. The root helper also anchors userdata itself (anchored=True).
    A development sandbox keeps its own console (journal=False).
    """
    with native_etc(userdata, anchored=anchored) as etc:
        return _install_at(etc, userdata, journal=journal)


def ready(userdata: Path) -> bool:
    try:
        with native_etc(userdata, anchored=False) as etc:
            _selected(etc, userdata)
            return configured(read_regular(etc, RELATIVE.name)[0])
    except (OSError, ValueError):
        return False


def _seed(source: Path, directory: int, uid: int, gid: int) -> None:
    """Copy the trusted template's entries into a new, anchored directory.

    Every inode is created exclusively through its parent's descriptor and
    owned by descriptor, so a concurrently planted link is never followed.
    """
    with os.scandir(source) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            info = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode) & 0o777
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), entry.name, dir_fd=directory)
                os.chown(entry.name, uid, gid, dir_fd=directory, follow_symlinks=False)
            elif stat.S_ISDIR(info.st_mode):
                os.mkdir(entry.name, 0o700, dir_fd=directory)
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    os.fchown(child, uid, gid)
                    _seed(Path(entry.path), child, uid, gid)
                    os.fchmod(child, mode)
                    os.utime(child, ns=(info.st_atime_ns, info.st_mtime_ns))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                data = Path(entry.path).read_bytes()
                child = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                dir_fd=directory)
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(child, view):]
                    os.fchown(child, uid, gid)
                    os.fchmod(child, mode)
                    os.utime(child, ns=(info.st_atime_ns, info.st_mtime_ns))
                finally:
                    os.close(child)
            else:
                raise ValueError(f"{entry.path} is not a regular file, directory or symbolic link")


def initialize(userdata: Path, template: Path, uid: int, gid: int, *, anchored: bool = False) -> bool:
    """Seed the complete pinned userdata only into a truly empty first install.

    Seeding just etc/log4j2.xml could make the container skip its remaining
    initialization. Existing registrations, caches and configuration are
    never copied over or re-owned. Nothing below userdata is followed.
    """
    userdata, template = Path(userdata), Path(template)
    try:
        directory = open_directory(userdata, anchored=anchored)
        mode = None
    except FileNotFoundError:
        parent = open_directory(userdata.parent, anchored=anchored)
        try:
            os.mkdir(userdata.name, 0o700, dir_fd=parent)
            directory = os.open(userdata.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        mode = 0o755
    try:
        names = os.listdir(directory)
        if names:
            try:
                os.stat(str(RELATIVE), dir_fd=directory, follow_symlinks=False)
                return False
            except (FileNotFoundError, NotADirectoryError):
                raise ValueError(f"{userdata} is not empty; refusing to seed userdata over existing state") from None
        if not (template / RELATIVE).is_file() or not selected_default((template / SELECTOR).read_bytes()):
            raise ValueError(f"{template} is not a complete openHAB userdata template")
        if mode is None:
            mode = stat.S_IMODE(os.fstat(directory).st_mode)
        _seed(template, directory, uid, gid)
        etc = os.open(RELATIVE.parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            _install_at(etc, userdata)
        finally:
            os.close(etc)
        os.fchown(directory, uid, gid)
        os.fchmod(directory, mode)
    finally:
        os.close(directory)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("userdata", type=Path, help="the initialized instance's userdata directory")
    parser.add_argument("--http-port", type=int, default=HTTP_PORT, help=f"its HTTP port (default: {HTTP_PORT})")
    parser.add_argument("--template", type=Path, help="seed complete pinned userdata on an empty first install")
    parser.add_argument("--uid", type=int, help="owner of newly seeded userdata")
    parser.add_argument("--gid", type=int, help="group of newly seeded userdata")
    args = parser.parse_args(argv)
    if args.template is not None and (args.uid is None or args.gid is None):
        parser.error("--template requires --uid and --gid")
    try:
        with socket.create_connection(("127.0.0.1", args.http_port), timeout=1):
            parser.exit(2, "openHAB still answers on its HTTP port; stop it before changing its logging configuration\n")
    except OSError:
        pass
    try:
        # Root runs this inside service-writable state: never follow a link.
        created = (initialize(args.userdata, args.template, args.uid, args.gid, anchored=True)
                   if args.template else False)
        changed = install(args.userdata, anchored=True) or created
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(2, f"could not protect {args.userdata / RELATIVE}: {exc}\n")
    print("pairing credentials are filtered; start openHAB to load the filter" if changed
          else "pairing logging filter is already installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
