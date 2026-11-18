"""openHAB's add-on files: pinned, kept on the runtime's own disk, never fetched at a start.

openHAB installs the add-ons conf/services/addons.cfg lists by resolving
Maven coordinates. Left alone it downloads each from its project's
repository when it starts, trusting TLS and nothing else. Here every file
an add-on needs is pinned in versions.json (`addons`): which files each
add-on needs beyond the distribution's own system repository, and each
file's SHA-256. The files are fetched once into the operator's cache and
verified there (ensure), then copied, verified again, into a repository
inside userdata that Karaf reads before any other (provision). The
generated addons.cfg says `remote = false`, so openHAB keeps its online
repository out of the resolver, and provision empties the remote list too:
a start resolves from local files or fails.

Root runs this file with the system Python while openHAB is stopped
(main), inside state the service account writes: it imports the standard
library and oh_ports only, and never follows a link there.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import xml.etree.ElementTree as ElementTree

import oh_ports

#: The repository, below userdata: openHAB's update tool leaves it alone, and
#: it goes wherever userdata goes when an install transaction sets it aside.
REPOSITORY = PurePosixPath("thermite/maven")
#: Where the runtime's container sees userdata.
MOUNTED = PurePosixPath("/openhab/userdata")
MAVEN_CFG = "org.ops4j.pax.url.mvn.cfg"
DEFAULT_REPOSITORIES = "org.ops4j.pax.url.mvn.defaultRepositories"
REMOTE_REPOSITORIES = "org.ops4j.pax.url.mvn.repositories"
#: The repository's id in Karaf's list. Entries named thermite.<binding> are
#: the ones the binding repairs were once installed with by hand.
ID = "thermite"
#: No add-on file is larger; one that claims to be is refused unread.
MAXIMUM_FILE = 256 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
UID = re.compile(r"[a-z]+-[a-z0-9]+")
_FILE = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.+-]*/)+[A-Za-z0-9_][A-Za-z0-9_.+-]*\.(?:jar|xml|kar)")
_COORDINATES = re.compile(r"(?:wrap:)?mvn:([^/$]+)/([^/$]+)/([^/$]+)(?:/([^/$]*))?(?:/([^/$]*))?(?:\$.*)?")


class AddonError(ValueError):
    """An add-on file that is not pinned, not here or not what its pin says."""


# What an add-on needs ---------------------------------------------------------

def artifact_path(uri: str) -> str:
    """Where a feature's `mvn:` bundle lies in a Maven repository."""
    match = _COORDINATES.fullmatch(uri.strip())
    if not match:
        raise AddonError(f"{uri!r} is not a Maven bundle location")
    group, artifact, version, kind, classifier = match.groups()
    name = f"{artifact}-{version}{'-' + classifier if classifier else ''}.{kind or 'jar'}"
    return f"{group.replace('.', '/')}/{artifact}/{version}/{name}"


def features(system: Path) -> dict[str, tuple[list[str], list[str]]]:
    """Every feature the distribution's system repository defines: the
    features it depends on and the bundles it installs itself."""
    found: dict[str, tuple[list[str], list[str]]] = {}
    for path in sorted(Path(system).rglob("*-features.xml")):
        for element in ElementTree.parse(path).getroot().iter():
            if element.tag.endswith("}feature") and element.get("name"):
                found.setdefault(element.get("name"), (
                    [child.text.strip() for child in element if child.tag.endswith("}feature") and child.text],
                    [child.text.strip() for child in element if child.tag.endswith("}bundle") and child.text]))
    return found


def needs(runtime: Path, uid: str, known: dict | None = None) -> list[str]:
    """The files add-on `uid` (binding-http) needs that the distribution does
    not carry: its feature's bundles and those of every feature it depends
    on, less what lies in runtime/system. This is what openHAB downloads
    for it; tests/test_addons.py holds the example house's list against what
    a real runtime fetched."""
    system = Path(runtime) / "system"
    known = features(system) if known is None else known
    if "openhab-" + uid not in known:
        raise AddonError(f"openHAB's distribution in {runtime} defines no add-on {uid}")
    seen: list[str] = []
    pending = ["openhab-" + uid]
    while pending:
        name = pending.pop()
        if name in seen or name not in known:
            continue
        seen.append(name)
        pending.extend(known[name][0])
    paths = {artifact_path(uri) for name in seen for uri in known[name][1]}
    return sorted(path for path in paths if not (system / path).is_file())


# The pins ---------------------------------------------------------------------

def entry(versions: dict) -> dict:
    """versions.json's `addons`, checked: {"repository": URL, "requires":
    {add-on: [file, ...]}, "files": {file: {"sha256", "size"}}}."""
    value = versions.get("addons")
    if not isinstance(value, dict):
        raise AddonError("versions.json pins no add-on files (addons): copy the entry from Thermite's versions.json")
    repository, requires, files = value.get("repository"), value.get("requires"), value.get("files")
    if not (isinstance(repository, str) and repository.startswith("https://") and isinstance(requires, dict)
            and isinstance(files, dict)):
        raise AddonError("versions.json addons needs an https repository, requires and files")
    for path, pin in files.items():
        if not (isinstance(path, str) and _FILE.fullmatch(path) and isinstance(pin, dict)
                and isinstance(pin.get("sha256"), str) and SHA256.fullmatch(pin["sha256"])
                and type(pin.get("size")) is int and 0 < pin["size"] <= MAXIMUM_FILE):
            raise AddonError(f"versions.json addons.files {path!r} needs a repository path, a lowercase sha256 and a size")
    for uid, paths in requires.items():
        if not (isinstance(uid, str) and UID.fullmatch(uid) and isinstance(paths, list)
                and all(path in files for path in paths)):
            raise AddonError(f"versions.json addons.requires {uid!r} must list files that addons.files pins")
    return value


def pinned(versions: dict, uids) -> dict[str, dict]:
    """{file: its pin} for everything the add-ons `uids` need."""
    if not uids:
        return {}
    addons = entry(versions)
    missing = sorted(uid for uid in set(uids) if uid not in addons["requires"])
    if missing:
        raise AddonError(f"versions.json pins no files for {', '.join(missing)} (addons.requires). Run "
                         f"thermite sandbox fetch --pin-addons {' '.join(missing)} where the network is reachable, "
                         "review the change to versions.json and commit it")
    return {path: addons["files"][path] for uid in sorted(set(uids)) for path in addons["requires"][uid]}


def digest(files: dict[str, dict]) -> str:
    """One name for a set of pinned files, as the installed manifest records it."""
    material = json.dumps({path: pin["sha256"] for path, pin in files.items()}, sort_keys=True)
    return "sha256:" + hashlib.sha256(material.encode()).hexdigest()


# The operator's cache ---------------------------------------------------------

def sha256_file(path: Path) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stored(store: Path, path: str, pin: dict) -> bool:
    """Whether the cache holds `path` as pinned."""
    file = Path(store) / path
    try:
        info = file.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == pin["size"] and sha256_file(file) == pin["sha256"]


def ensure(versions: dict, uids, store: Path, download, log=print) -> dict[str, dict]:
    """Every file the add-ons need, in the cache and verified; `download`
    (oh_sandbox.download) fetches one that is not, and refuses one whose
    digest is not the pinned one. Returns pinned(versions, uids)."""
    files = pinned(versions, uids)
    repository = entry(versions)["repository"].rstrip("/")
    for path, pin in files.items():
        if not stored(store, path, pin):
            download(f"{repository}/{path}", pin["sha256"], (Path(store) / path).parent, log=log)
    return files


def pin(versions: dict, runtime: Path, uids, store: Path, fetch, log=print) -> dict:
    """versions.json's `addons` with `uids` pinned anew, beside what it
    already pins: each add-on's files as this distribution's feature files
    name them, and for a file not pinned yet the digest of what the
    repository serves now (`fetch(url, destination)`), which must be the
    file the repository's own checksum beside it names (.sha256, or .sha1
    where it publishes no other). A file already pinned keeps its pin, and
    the cache's copy must match it."""
    current = versions.get("addons") if isinstance(versions.get("addons"), dict) else {}
    repository = str(current.get("repository") or "https://openhab.jfrog.io/openhab/libs-release").rstrip("/")
    requires, files = dict(current.get("requires") or {}), dict(current.get("files") or {})
    known = features(Path(runtime) / "system")
    for uid in sorted(set(uids)):
        requires[uid] = needs(runtime, uid, known)
        for path in requires[uid]:
            target = Path(store) / path
            if path in files:
                if not stored(store, path, files[path]):
                    fetch(f"{repository}/{path}", target)
                if not stored(store, path, files[path]):
                    raise AddonError(f"{target} is not the file versions.json pins as {path}")
                continue
            fetch(f"{repository}/{path}", target)
            for algorithm in ("sha256", "sha1"):
                published = Path(f"{target}.{algorithm}")
                try:
                    fetch(f"{repository}/{path}.{algorithm}", published)
                except OSError:
                    continue
                with open(target, "rb") as stream:
                    served = hashlib.file_digest(stream, algorithm).hexdigest()
                if published.read_text(encoding="ascii", errors="replace").split()[:1] != [served]:
                    raise AddonError(f"{repository}/{path} is not the file its published .{algorithm} names")
                break
            else:
                raise AddonError(f"{repository} publishes no checksum for {path}: it is not pinned unchecked")
            found = sha256_file(target)
            files[path] = {"sha256": found, "size": target.stat().st_size}
            log(f"pinned  {path} sha256 {found}")
    used = {path for paths in requires.values() for path in paths}
    return {"repository": repository, "requires": dict(sorted(requires.items())),
            "files": {path: files[path] for path in sorted(used)}}


# Karaf's resolver settings ----------------------------------------------------

def repository_entry(directory: PurePosixPath | Path) -> str:
    """The repository as an entry of Karaf's default repositories."""
    directory = PurePosixPath(directory)
    if not directory.is_absolute() or ".." in directory.parts or any(ord(char) < 32 for char in str(directory)):
        raise AddonError("the add-on repository needs an absolute local path")
    return Path(directory).as_uri().rstrip("/") + "/@id=" + ID


def _properties(text: str) -> list[tuple[int, int, str, str]]:
    """(first line, line after, key, value) of each property, continuation
    lines joined as Java reads them."""
    lines = text.splitlines()
    found, index = [], 0
    while index < len(lines):
        start, logical = index, ""
        while True:
            part = lines[index].rstrip("\r\n")
            logical += part if index == start else part.lstrip()
            index += 1
            if (len(part) - len(part.rstrip("\\"))) % 2 == 0:
                break
            logical = logical[:-1]
            if index == len(lines):
                raise AddonError("the Maven settings end inside a continued line")
        match = re.match(r"\s*([^\s#!=:][^\s=:]*)\s*[=:]\s*(.*)$", logical)
        if match:
            found.append((start, index, match.group(1), match.group(2)))
    return found


def maven_config(text: str, repository: str) -> str:
    """userdata/etc/org.ops4j.pax.url.mvn.cfg with `repository` first among
    the default repositories, in place of any entry of Thermite's own, and
    no remote repository. Every other setting stays as it is; settled
    settings come back unchanged."""
    lines = text.splitlines()
    properties = _properties(text)
    defaults = [found for found in properties if found[2] == DEFAULT_REPOSITORIES]
    if len(defaults) != 1:
        raise AddonError(f"{MAVEN_CFG} must set {DEFAULT_REPOSITORIES} exactly once: it is not the "
                         "distribution's file")
    replaced: dict[int, tuple[int, list[str]]] = {}
    start, end, _, value = defaults[0]
    others = [part.strip() for part in value.split(",") if part.strip()
              and not re.search(r"@id=" + re.escape(ID) + r"(?:\.[A-Za-z0-9_-]+)?(?:@|$)", part.strip())]
    wanted = ", \\\n    ".join([repository, *others])
    if [part.strip() for part in value.split(",") if part.strip()] != [repository, *others]:
        replaced[start] = (end, [f"{DEFAULT_REPOSITORIES}=\\", "    " + wanted])
    for start, end, key, value in properties:
        if key == REMOTE_REPOSITORIES and value.strip():
            replaced[start] = (end, [f"{REMOTE_REPOSITORIES}="])
    absent = not any(key == REMOTE_REPOSITORIES for _, _, key, _ in properties)
    if not replaced and not absent:
        return text
    out, index = [], 0
    while index < len(lines):
        if index in replaced:
            end, new = replaced[index]
            out += new
            index = end
        else:
            out.append(lines[index])
            index += 1
    if absent:
        out.append(f"{REMOTE_REPOSITORIES}=")
    return "\n".join(out) + "\n"


# The repository inside userdata -----------------------------------------------

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _child(parent: int, name: str, owner: tuple[int, int] | None) -> int:
    """A directory of `parent`, made private to the service when missing."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    if owner is not None:
        os.fchown(descriptor, *owner)
    os.fchmod(descriptor, 0o700)
    return descriptor


def _put(directory: int, name: str, data: bytes, owner: tuple[int, int] | None) -> None:
    """A new inode with `data`, renamed over `name`: nothing planted there is written through."""
    temporary = f".{name}.{os.urandom(8).hex()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=directory)
    try:
        try:
            if owner is not None:
                os.fchown(descriptor, *owner)
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def _same(directory: int, name: str, sha256: str) -> bool:
    try:
        data, _ = oh_ports.read_regular(directory, name, MAXIMUM_FILE)
    except (OSError, ValueError):
        return False
    return hashlib.sha256(data).hexdigest() == sha256


def _prune(top: int, keep: set[str]) -> None:
    """Take away every file of the repository that is not in `keep`, and
    the directories that leaves empty. Links are removed, never followed."""
    for directory, names, files, descriptor in os.fwalk(".", topdown=False, dir_fd=top):
        relative = PurePosixPath(directory)
        for name in files:
            if str(relative / name) not in keep:
                os.unlink(name, dir_fd=descriptor)
        for name in names:
            # Walked already, bottom up: empty now unless it holds a kept file.
            try:
                if stat.S_ISLNK(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode):
                    os.unlink(name, dir_fd=descriptor)
                else:
                    os.rmdir(name, dir_fd=descriptor)
            except OSError:
                pass


def provision(userdata: Path, files: dict[str, dict], store: Path, *, mounted: PurePosixPath | Path | None = None,
              owner: tuple[int, int] | None = None, anchored: bool = False) -> list[str]:
    """Make userdata's repository hold exactly `files`, each copied from the
    cache `store` and refused unless it is what its pin says, and point
    Karaf's resolver at it alone. `mounted` is where the runtime sees
    userdata when that is not `userdata` itself (a container). With
    `owner` (root, in production) what is written becomes the service
    account's. Returns what changed, for the log."""
    userdata = Path(os.path.abspath(userdata))
    changed = []
    top = oh_ports.open_directory(userdata, anchored=anchored)
    try:
        repository = top
        for part in REPOSITORY.parts:
            child = _child(repository, part, owner)
            if repository != top:
                os.close(repository)
            repository = child
        try:
            for path, pin in sorted(files.items()):
                parts = PurePosixPath(path).parts
                directory = os.dup(repository)
                try:
                    for part in parts[:-1]:
                        child = _child(directory, part, owner)
                        os.close(directory)
                        directory = child
                    if _same(directory, parts[-1], pin["sha256"]):
                        continue
                    source = os.open(Path(store) / path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    try:
                        if os.fstat(source).st_size != pin["size"]:
                            raise AddonError(f"{Path(store) / path} is not the size versions.json pins")
                        with os.fdopen(os.dup(source), "rb") as stream:
                            data = stream.read()
                    finally:
                        os.close(source)
                    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
                        raise AddonError(f"{Path(store) / path} is not the file versions.json pins (sha256 differs): "
                                         "remove it and run the command again to fetch it anew")
                    _put(directory, parts[-1], data, owner)
                    changed.append(path)
                finally:
                    os.close(directory)
            _prune(repository, set(files))
        finally:
            os.close(repository)
        etc = os.open("etc", _DIRECTORY, dir_fd=top)
        try:
            data, info = oh_ports.read_regular(etc, MAVEN_CFG)
            where = (PurePosixPath(mounted) if mounted is not None else PurePosixPath(userdata)) / REPOSITORY
            new = maven_config(data.decode("utf-8"), repository_entry(where)).encode("utf-8")
            if new != data:
                oh_ports.replace_regular(etc, MAVEN_CFG, new, info, **({"uid": owner[0], "gid": owner[1]} if owner else {}))
                changed.append("etc/" + MAVEN_CFG)
        finally:
            os.close(etc)
    finally:
        os.close(top)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the pinned add-on files into stopped native userdata and point Karaf at them")
    parser.add_argument("userdata", type=Path)
    parser.add_argument("--versions", type=Path, required=True, help="the versions.json whose addons entry pins the files")
    parser.add_argument("--store", type=Path, required=True, help="the cache that holds the fetched files")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("addons", nargs="*", metavar="ADDON", help="the add-ons conf's addons.cfg lists (binding-http)")
    args = parser.parse_args(argv)
    try:
        versions = json.loads(args.versions.read_text(encoding="utf-8"))
        changed = provision(args.userdata, pinned(versions, args.addons), args.store, mounted=MOUNTED,
                            owner=(args.uid, args.gid), anchored=True)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"could not install the pinned add-on files: {exc}\n")
    print(f"add-on repository: {len(changed)} changed" if changed else "add-on repository: as pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
