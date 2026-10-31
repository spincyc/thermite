"""Keep pairing credentials out of openHAB's native logs.

Item command events and REST DEBUG messages include command values. Filters on
each native logger drop messages naming a pairing command Item.
Pax Logging bypasses Log4j2's global filter on its direct backend path, so a
global filter alone is insufficient. Status/result Items and ordinary commands
remain visible. Install it while openHAB is stopped; editing the XML alone does
not prove that a running logger has reloaded it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import stat
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

RELATIVE = Path("etc/log4j2.xml")
SELECTOR = Path("etc/org.ops4j.pax.logging.cfg")
DEFAULT_SELECTOR = b"org.ops4j.pax.logging.log4j2.config.file=${karaf.etc}/log4j2.xml\n"
# Both "Item 'example_pairing' received command ..." and REST's
# "items/example_pairing ... value ...", including multiline payloads.
PATTERN = r"(?s).*\b[A-Za-z_][A-Za-z0-9_]*_pairing\b.*"
ATTRIBUTES = {"regex": PATTERN, "onMatch": "DENY", "onMismatch": "NEUTRAL", "useRawMsg": "false"}
PAIRING_RULES = frozenset(f"automation/js/{name}.js" for name in
                         ("sony_ip_control", "androidtv_googletv", "pyatv_http"))


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


def configuration(userdata: Path) -> Path:
    if not selected_default((userdata / SELECTOR).read_bytes()):
        raise ValueError(f"{userdata / SELECTOR} selects an unsupported native logging configuration; "
                         "expected org.ops4j.pax.logging.log4j2.config.file=${karaf.etc}/log4j2.xml")
    return userdata / RELATIVE


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


def guarded(data: bytes) -> bytes:
    """Prepend DENY at every native route without replacing existing configuration."""
    if protected(data):
        return data
    root = document(data)
    for scope in scopes(root):
        if not scope_protected(scope):
            guard_scope(scope)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def install(userdata: Path) -> bool:
    """Atomically protect an initialized, stopped instance, preserving ownership/mode."""
    path = configuration(userdata)
    before = path.read_bytes()
    after = guarded(before)
    if before == after:
        return False
    original = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".pairing-logging-", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, stat.S_IMODE(original.st_mode))
        current = temp.stat()
        if (current.st_uid, current.st_gid) != (original.st_uid, original.st_gid):
            os.chown(temp, original.st_uid, original.st_gid)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def ready(userdata: Path) -> bool:
    try:
        return protected(configuration(userdata).read_bytes())
    except (OSError, ValueError):
        return False


def initialize(userdata: Path, template: Path, uid: int, gid: int) -> bool:
    """Seed the complete pinned userdata only into a truly empty first install.

    Seeding just etc/log4j2.xml could make the container skip its remaining
    initialization. Existing registrations, caches and configuration are
    never copied over or re-owned.
    """
    if (userdata / RELATIVE).exists():
        return False
    if userdata.exists() and any(userdata.iterdir()):
        raise ValueError(f"{userdata} is not empty; refusing to seed userdata over existing state")
    if not (template / RELATIVE).is_file() or not selected_default((template / SELECTOR).read_bytes()):
        raise ValueError(f"{template} is not a complete openHAB userdata template")
    mode = stat.S_IMODE(userdata.stat().st_mode) if userdata.exists() else 0o755
    shutil.copytree(template, userdata, symlinks=True, dirs_exist_ok=True)
    os.chmod(userdata, mode)
    install(userdata)
    for path in [*userdata.rglob("*"), userdata]:
        os.chown(path, uid, gid, follow_symlinks=False)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("userdata", type=Path, help="the initialized instance's userdata directory")
    parser.add_argument("--http-port", type=int, default=8080, help="its HTTP port (default: 8080)")
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
        created = initialize(args.userdata, args.template, args.uid, args.gid) if args.template else False
        changed = install(args.userdata) or created
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(2, f"could not protect {args.userdata / RELATIVE}: {exc}\n")
    print("pairing credentials are filtered; start openHAB to load the filter" if changed
          else "pairing logging filter is already installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
