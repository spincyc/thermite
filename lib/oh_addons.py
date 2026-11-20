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

Two bindings run repaired (REPAIRS: lib/oh_androidtv_patch,
lib/oh_heos_patch). A house whose conf lists one of them gets, in the same
repository, the binding's bundle rebuilt from the pinned upstream file and
the repair's committed classes, and the line in Karaf's overrides that makes
openHAB install it in place of the upstream one. Karaf applies an override
only when it provisions a feature, so an OSGi cache that holds another build
of a repaired binding is cleared: the next start provisions afresh, as it
does after openHAB's own update. verify reads the cache after that start
and says which binding is not the repaired one.

Root runs this file with the system Python while openHAB is stopped
(main), inside state the service account writes: it imports the standard
library, oh_ports and the repairs' own packages, and never follows a link
there.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import xml.etree.ElementTree as ElementTree
import zipfile

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
#: The packages beside this file that each repair one binding.
REPAIRS = ("oh_androidtv_patch", "oh_heos_patch")
OVERRIDES = "overrides.properties"
MANIFEST = "META-INF/MANIFEST.MF"
#: Where Equinox keeps each installed bundle: <id>/<generation>/bundleFile.
BUNDLES = ("cache", "org.eclipse.osgi")
#: No bundle has more entries; one that claims to is not read.
MAXIMUM_ENTRIES = 50_000
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


def digest(files: dict[str, dict], wanted=()) -> str:
    """One name for a set of pinned files and the repairs installed beside
    them, as the installed manifest records it."""
    material = {path: pin["sha256"] for path, pin in files.items()}
    material.update({repair.path: repair.content for repair in wanted})
    return "sha256:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


# The binding repairs ----------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Repair:
    """One binding that runs repaired, as its package declares it."""

    package: str
    #: The add-on whose houses need it (binding-heos) and its bundle's name.
    addon: str
    bundle: str
    title: str
    #: The openHAB release it repairs, and the version the repaired bundle carries.
    upstream: str
    patched: str
    #: The repaired bundle's Thermite-Patch header.
    marker: str
    #: The upstream bundle: a pinned add-on file, and the digest it must have.
    source: str
    source_sha256: str
    #: The repaired bundle's place in the repository, and Karaf's override line.
    path: str
    override: str
    comment: str
    #: The classes the repair replaces, by entry name and SHA-256, and the
    #: content of the whole repaired bundle (content_digest).
    classes: dict
    content: str

    @property
    def pin(self) -> str:
        return "repair_" + self.addon.replace("-", "_")

    def assemble(self, upstream: bytes) -> bytes:
        """The repaired bundle: the upstream one with the committed classes
        in place of its own, and refused unless it is the reviewed one."""
        module = importlib.import_module(self.package)
        classes = {}
        for name, sha256 in self.classes.items():
            data = (Path(module.ROOT) / "classes" / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise AddonError(f"lib/{self.package}/classes/{name} is not the class its inputs.json pins")
            classes[name] = data
        if hashlib.sha256(upstream).hexdigest() != self.source_sha256:
            raise AddonError(f"{self.source} is not the bundle the {self.title} was built from")
        data = module.assemble(io.BytesIO(upstream), classes)
        if identity(io.BytesIO(data)) != (self.bundle, self.patched, self.marker, self.content):
            raise AddonError(f"the rebuilt {self.bundle} is not the reviewed {self.title}")
        return data


def known_repairs() -> list[Repair]:
    found = []
    for package in REPAIRS:
        module = importlib.import_module(package)
        lock = module.pins()
        found.append(Repair(package, module.ADDON, module.BUNDLE, module.TITLE, lock["upstream"], lock["patched"],
                            module.MARKER, artifact_path("mvn:" + module.UPSTREAM_COORDINATES),
                            lock["assets"]["bundle"]["sha256"], str(module.MAVEN_PATH), module.OVERRIDE,
                            module.COMMENT, dict(lock["classes"]), lock["content_sha256"]))
    return found


def approved_artifacts() -> dict[str, str]:
    """The repairs' committed classes by repository path and SHA-256: the
    binary files a public package may carry for them (oh_public.guard)."""
    return {f"lib/{repair.package}/classes/{name}": sha256
            for repair in known_repairs() for name, sha256 in repair.classes.items()}


def repairs(versions: dict, uids) -> list[Repair]:
    """The repairs the add-ons `uids` run with. One written for another
    openHAB release than versions.json pins is refused, with what to do: it
    is retired or rebased, never skipped."""
    wanted = [repair for repair in known_repairs() if repair.addon in set(uids)]
    pinned_release = (versions.get("openhab") or {}).get("version")
    for repair in wanted:
        if pinned_release != repair.upstream:
            raise AddonError(importlib.import_module(repair.package).refusal(str(pinned_release)))
    return wanted


def manifest_headers(data: bytes) -> dict[str, str]:
    """A JAR manifest's main section."""
    headers: dict[str, str] = {}
    name = None
    for line in data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n", 1)[0].split("\n"):
        if line.startswith(" ") and name is not None:
            headers[name] += line[1:]
        elif ": " in line:
            name, value = line.split(": ", 1)
            headers[name] = value
    return headers


def identity(jar, content: bool = True) -> tuple[str, str, str | None, str | None]:
    """A bundle's symbolic name, version and Thermite-Patch header, and one
    name for its content however it is packed: every entry but the manifest,
    by name and SHA-256. Pax adds a header to the manifest of the copy it
    hands the framework, and may pack it differently; nothing else of it
    may differ. `jar` is a path or an open file."""
    with zipfile.ZipFile(jar) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = {info.filename for info in infos}
        if (len(infos) > MAXIMUM_ENTRIES or len(names) != len(infos) or MANIFEST not in names
                or sum(info.file_size for info in infos) > 2 * MAXIMUM_FILE):
            raise AddonError("not a bundle this reads")
        headers = manifest_headers(archive.read(MANIFEST))
        digest = None
        if content:
            entries = {info.filename: hashlib.sha256(archive.read(info)).hexdigest()
                       for info in infos if info.filename != MANIFEST}
            digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return (headers.get("Bundle-SymbolicName", "").split(";")[0].strip(), headers.get("Bundle-Version", ""),
            headers.get("Thermite-Patch"), digest)


def overrides(text: str, wanted: list[Repair], known: list[Repair]) -> str:
    """userdata/etc/overrides.properties with the wanted repairs' lines and
    no other of Thermite's (one a retired repair left, say). Every other
    line stays; settled text comes back unchanged. Another override of a
    repaired binding is refused: two cannot both apply."""
    kept = []
    for line in text.splitlines():
        value = line.strip()
        if value.startswith("# thermite pinned ") or re.search(r"/[0-9.]+\.thermite[0-9]+;", value):
            continue
        for repair in known:
            if value and not value.startswith("#") and repair.bundle in value:
                raise AddonError(f"{OVERRIDES} overrides {repair.bundle} with a bundle that is not Thermite's "
                                 f"{repair.title}: remove that line from userdata/etc/{OVERRIDES} while Thermite is "
                                 "stopped, or keep it and take the binding out of the house")
        kept.append(line)
    lines = [*kept, *(line for repair in sorted(wanted, key=lambda repair: repair.addon)
                      for line in ("# " + repair.comment, repair.override))]
    return text if text.splitlines() == lines else "".join(line + "\n" for line in lines)


def _installed(top: int, wanted: set[str]) -> list[tuple[str, str, str | None, str | None]]:
    """What the OSGi cache below userdata holds of the bundles `wanted` and of
    every bundle that carries Thermite's header: identity() of the newest
    generation of each. A file that is no readable bundle is none of them."""
    found = []
    try:
        cache = os.open(BUNDLES[0], _DIRECTORY, dir_fd=top)
    except FileNotFoundError:
        return found
    try:
        try:
            bundles = os.open(BUNDLES[1], _DIRECTORY, dir_fd=cache)
        except FileNotFoundError:
            return found
        try:
            for name in sorted(os.listdir(bundles)):
                if not name.isdigit():
                    continue
                try:
                    holder = os.open(name, _DIRECTORY, dir_fd=bundles)
                except OSError:
                    continue
                try:
                    for generation in sorted((entry for entry in os.listdir(holder) if entry.isdigit()),
                                             key=int, reverse=True):
                        try:
                            directory = os.open(generation, _DIRECTORY, dir_fd=holder)
                        except OSError:
                            continue
                        try:
                            descriptor = os.open("bundleFile", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                                 dir_fd=directory)
                        except OSError:
                            continue
                        finally:
                            os.close(directory)
                        with os.fdopen(descriptor, "rb") as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_size > MAXIMUM_FILE:
                                break
                            try:
                                bundle, version, marker, _ = identity(stream, content=False)
                                if bundle in wanted or marker is not None:
                                    stream.seek(0)
                                    found.append(identity(stream))
                            except (OSError, ValueError, KeyError, RuntimeError, EOFError, zipfile.BadZipFile):
                                pass
                        break
                finally:
                    os.close(holder)
        finally:
            os.close(bundles)
    finally:
        os.close(cache)
    return found


def _stale(found, wanted: list[Repair]) -> str | None:
    """Why the OSGi cache cannot stay: each build of a binding it holds that
    is not the one this version installs."""
    by_bundle = {repair.bundle: repair for repair in wanted}
    reasons = []
    for bundle, version, marker, content in sorted(found):
        repair = by_bundle.get(bundle)
        if repair is None:
            reasons.append(f"it holds a repaired {bundle} {version} that this version does not install")
        elif (version, marker, content) != (repair.patched, repair.marker, repair.content):
            reasons.append(f"it holds {bundle} {version}, not the {repair.title} ({repair.patched})")
    return "; ".join(reasons) or None


def verify(userdata: Path, wanted: list[Repair], *, anchored: bool = False) -> list[str]:
    """What is wrong with the bundles a started runtime installed: one line
    for each repaired binding that is missing or is not the repaired build."""
    problems = []
    top = oh_ports.open_directory(Path(os.path.abspath(userdata)), anchored=anchored)
    try:
        found = _installed(top, {repair.bundle for repair in wanted})
    finally:
        os.close(top)
    for repair in wanted:
        mine = [entry for entry in found if entry[0] == repair.bundle]
        if not mine:
            problems.append(f"openHAB has not installed {repair.bundle}, which {repair.addon} needs")
        for _, version, marker, content in mine:
            if (version, marker) != (repair.patched, repair.marker):
                problems.append(f"openHAB runs {repair.bundle} {version}, not the {repair.title} ({repair.patched})")
            elif content != repair.content:
                problems.append(f"openHAB runs a {repair.bundle} {version} whose content is not the reviewed "
                                f"{repair.title}")
    return problems


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


def _cached(store: Path, path: str, pin: dict) -> bytes:
    """A file of the operator's cache, refused unless it is what its pin says."""
    source = os.open(Path(store) / path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(source, "rb") as stream:
        if os.fstat(stream.fileno()).st_size != pin["size"]:
            raise AddonError(f"{Path(store) / path} is not the size versions.json pins")
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise AddonError(f"{Path(store) / path} is not the file versions.json pins (sha256 differs): "
                         "remove it and run the command again to fetch it anew")
    return data


def _hold_cache(top: int, owner: tuple[int, int] | None) -> bool:
    """Give userdata an empty OSGi cache directory if it has none; whether
    it had none. Where the directory is missing, the distribution's start
    script first builds the cache with a server of its own, on the house's
    conf, and stops that server while it installs the add-ons. On 5.2.3
    that left add-ons installed and never started in eleven of twenty
    starts of userdata that had run, and the restart tried did not recover
    it; with the directory there one server installs everything, and twenty
    of twenty came up whole (lib/oh_androidtv_patch/README.md has the
    counts). A link in its place is refused."""
    cache = _child(top, BUNDLES[0], owner)
    try:
        try:
            os.stat(BUNDLES[1], dir_fd=cache, follow_symlinks=False)
        except FileNotFoundError:
            os.close(_child(cache, BUNDLES[1], owner))
            return True
        os.close(os.open(BUNDLES[1], _DIRECTORY, dir_fd=cache))
        return False
    finally:
        os.close(cache)


def provision(userdata: Path, files: dict[str, dict], store: Path, *, wanted=(),
              mounted: PurePosixPath | Path | None = None, owner: tuple[int, int] | None = None,
              anchored: bool = False, hold_cache: bool = False) -> list[str]:
    """Make userdata's repository hold exactly `files`, each copied from the
    cache `store` and refused unless it is what its pin says, and the
    rebuilt bundle of each repair in `wanted`; point Karaf's resolver at the
    repository alone and its overrides at those bundles; and empty an OSGi
    cache that holds another build of a repaired binding. `mounted` is
    where the runtime sees userdata when that is not `userdata` itself (a
    container). With `owner` (root, in production) what is written becomes
    the service account's. With `hold_cache`, userdata that has no OSGi
    cache at all gets the empty directory too (_hold_cache): an installation
    asks for it, whose runtime the distribution's start script starts; the
    test runtime builds its own cache first and does not. Returns what
    changed, for the log."""
    userdata = Path(os.path.abspath(userdata))
    changed = []
    for repair in wanted:
        if files.get(repair.source, {}).get("sha256") != repair.source_sha256:
            raise AddonError(f"versions.json does not pin {repair.source} as the bundle the {repair.title} was "
                             f"built from (sha256 {repair.source_sha256})")
    top = oh_ports.open_directory(userdata, anchored=anchored)
    try:
        repository = top
        for part in REPOSITORY.parts:
            child = _child(repository, part, owner)
            if repository != top:
                os.close(repository)
            repository = child
        try:
            built = {repair.path: repair for repair in wanted}
            for path in sorted({*files, *built}):
                parts = PurePosixPath(path).parts
                directory = os.dup(repository)
                try:
                    for part in parts[:-1]:
                        child = _child(directory, part, owner)
                        os.close(directory)
                        directory = child
                    if path in built:
                        source = built[path].source
                        data = built[path].assemble(_cached(store, source, files[source]))
                        if _same(directory, parts[-1], hashlib.sha256(data).hexdigest()):
                            continue
                    elif _same(directory, parts[-1], files[path]["sha256"]):
                        continue
                    else:
                        data = _cached(store, path, files[path])
                    _put(directory, parts[-1], data, owner)
                    changed.append(path)
                finally:
                    os.close(directory)
            _prune(repository, {*files, *built})
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
            try:
                data, _ = oh_ports.read_regular(etc, OVERRIDES)
            except FileNotFoundError:
                data = b""
            new = overrides(data.decode("utf-8"), list(wanted), known_repairs()).encode("utf-8")
            if new != data:
                _put(etc, OVERRIDES, new, owner)
                changed.append("etc/" + OVERRIDES)
        finally:
            os.close(etc)
        stale = _stale(_installed(top, {repair.bundle for repair in wanted}), list(wanted))
        if stale:
            # Karaf applies an override only when it provisions a feature.
            # The emptied cache keeps its directory (_hold_cache).
            shutil.rmtree(BUNDLES[0], dir_fd=top)
            _hold_cache(top, owner)
            changed.append(f"cache (cleared, so that the next start installs every bundle anew: {stale})")
        elif hold_cache and _hold_cache(top, owner):
            changed.append("cache (made empty: userdata had none, and openHAB's start script must not build it "
                           "with a server of its own)")
    finally:
        os.close(top)
    return changed


#: What a failed verification means and what to do, after its findings.
UNREPAIRED = ("This version does not run without that repair, so it is not committed: what ran before is put "
              "back, with its state. Nothing in the site is wrong and nothing there fixes it: stay on the version "
              "that ran before and report these lines, with openHAB's log of this start, as a fault in Thermite's "
              "repair (lib/oh_androidtv_patch/README.md, lib/oh_heos_patch/README.md).")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the pinned add-on files into stopped native userdata and point Karaf at them")
    parser.add_argument("userdata", type=Path)
    parser.add_argument("--versions", type=Path, required=True, help="the versions.json whose addons entry pins the files")
    parser.add_argument("--store", type=Path, required=True, help="the cache that holds the fetched files")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--verify", action="store_true",
                        help="change nothing: check that the started runtime installed each repaired binding")
    parser.add_argument("addons", nargs="*", metavar="ADDON", help="the add-ons conf's addons.cfg lists (binding-http)")
    # The add-ons follow the options that follow userdata, and Python 3.11's
    # parse_args has already matched ADDON, empty, with userdata by then.
    args = parser.parse_intermixed_args(argv)
    try:
        versions = json.loads(args.versions.read_text(encoding="utf-8"))
        wanted = repairs(versions, args.addons)
        if args.verify:
            problems = verify(args.userdata, wanted, anchored=True)
            if problems:
                parser.exit(1, "".join(line + "\n" for line in problems) + UNREPAIRED + "\n")
            print("binding repairs: " + (", ".join(f"{repair.bundle} {repair.patched}" for repair in wanted)
                                         or "none needed"))
            return 0
        changed = provision(args.userdata, pinned(versions, args.addons), args.store, wanted=wanted, mounted=MOUNTED,
                            owner=(args.uid, args.gid), anchored=True, hold_cache=True)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"could not install the pinned add-on files: {exc}\n")
    for line in changed:
        print(f"add-on repository: {line}")
    if not changed:
        print("add-on repository: as pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
