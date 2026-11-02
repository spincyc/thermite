"""Rebuild the pinned Android TV binding with a small, auditable lifecycle patch.

Build artifacts and activation instructions are staged only. This module never
connects to a device, edits native userdata, updates OSGi, or starts a service.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "inputs.json"
PACKAGE = "org/openhab/binding/androidtv/internal/"
ARTIFACT = "org.openhab.binding.androidtv-5.2.3.thermite1.jar"
COORDINATES = "org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1"
MAVEN_PATH = Path("org/openhab/addons/bundles/org.openhab.binding.androidtv/5.2.3.thermite1") / ARTIFACT
OVERRIDE = f'mvn:{COORDINATES};range="[5.2.3,5.2.4)"'
REPOSITORY = Path("/openhab/userdata/thermite/maven")
DEFAULT_REPOSITORIES = "org.ops4j.pax.url.mvn.defaultRepositories"
PATCHES = {"manager": "googletv-lifecycle.patch", "handler": "handler-ownership.patch"}
EXPECTED_CLASSES = frozenset({
    PACKAGE + "AndroidTVHandler.class",
    PACKAGE + "protocol/googletv/GoogleTVConnectionManager.class",
    PACKAGE + "protocol/googletv/GoogleTVConnectionManager$1.class",
    PACKAGE + "protocol/googletv/GoogleTVSocketLifecycle.class",
    PACKAGE + "protocol/googletv/GoogleTVSocketLifecycle$Attempt.class",
})


class PatchError(ValueError):
    """An unverified input or a failed build; do not stage or activate it."""


def cached_jar_report(staged: Path, cached: Path, expected_sha256: str) -> dict:
    from .integrity import compare_cached_jar
    return compare_cached_jar(staged, cached, expected_sha256)


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pins() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def verified(path: Path, expected: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or sha(path) != expected:
        raise PatchError(f"unverified build input: {path.name}")
    return path


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = 90) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PatchError(f"build tool failed: {Path(args[0]).name}") from exc
    if result.returncode:
        raise PatchError(f"build tool failed ({result.returncode}): {result.stdout[-3000:]}{result.stderr[-3000:]}")
    return result.stdout + result.stderr


def _signature(name: str) -> bool:
    upper = name.upper()
    return upper.startswith("META-INF/") and upper.endswith((".SF", ".RSA", ".DSA", ".EC"))


def manifest(data: bytes, *, patched: bool) -> bytes:
    # Discard obsolete per-entry signature digests. Preserve the complete main
    # OSGi contract, including all imports/exports and service components.
    main = data.replace(b"\r\n", b"\n").split(b"\n\n", 1)[0]
    if patched:
        if main.count(b"Bundle-Version: 5.2.3\n") != 1:
            raise PatchError("unexpected upstream bundle version")
        main = main.replace(b"Bundle-Version: 5.2.3\n", b"Bundle-Version: 5.2.3.thermite1\n")
        main += b"\nThermite-Patch: googletv-lifecycle-v1"
    return main.replace(b"\n", b"\r\n") + b"\r\n\r\n"


def rebuild(bundle: Path, replacements: dict[str, bytes], output: Path, *, patched: bool = True) -> dict:
    if patched and set(replacements) != EXPECTED_CLASSES:
        raise PatchError("compiled class set differs from the reviewed patch")
    entries = {}
    with zipfile.ZipFile(bundle) as source:
        names = source.namelist()
        if len(names) != len(set(names)):
            raise PatchError("duplicate upstream JAR entry")
        for name in names:
            if _signature(name):
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise PatchError("unsafe upstream JAR entry")
            entries[name] = source.read(name)
    entries["META-INF/MANIFEST.MF"] = manifest(entries["META-INF/MANIFEST.MF"], patched=patched)
    entries.update(replacements)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as dest:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            dest.writestr(info, content)
    return {"sha256": sha(output), "size": output.stat().st_size,
            "replaced": sorted(replacements), "vendor_signature_preserved": False}


def override_text(previous: str) -> str:
    """Pure merge for a future owner deployment adapter; never edits userdata.

    Refuse an already-existing Android TV override rather than silently taking
    ownership of a different vendor patch. Unrelated lines stay byte-for-byte.
    """
    for line in previous.splitlines():
        value = line.strip()
        if value and not value.startswith("#") and "androidtv" in value.lower() and value != OVERRIDE:
            raise PatchError("an Android TV override already exists; review it explicitly")
    if OVERRIDE in [line.strip() for line in previous.splitlines()]:
        return previous
    separator = "" if not previous or previous.endswith("\n") else "\n"
    return previous + separator + "# thermite pinned Android TV lifecycle repair\n" + OVERRIDE + "\n"


def repository_entry(repository: Path = REPOSITORY) -> str:
    repository = Path(repository)
    if (not repository.is_absolute() or ".." in repository.parts
            or any(ord(char) < 32 for char in str(repository))):
        raise PatchError("Maven repository requires an absolute local path")
    return repository.as_uri().rstrip("/") + "/@id=thermite.androidtv"


def maven_config_text(previous: str, *, repository: Path = REPOSITORY) -> str:
    """Prepend ONE private read-only default repository; preserve every other setting.

    Legacy Karaf override processing accepts only mvn: URIs. An artifact in
    tmp/mvn alone is not durable. Keep its authoritative copy outside caches
    and keep the existing localRepository/default repository entries intact.
    """
    entry = repository_entry(repository)
    lines = previous.splitlines(keepends=True)
    blocks = []
    index = 0
    while index < len(lines):
        start = index
        logical = ""
        while True:
            part = lines[index].rstrip("\r\n")
            logical += part if index == start else part.lstrip()
            slashes = len(part) - len(part.rstrip("\\"))
            index += 1
            if not slashes % 2:
                break
            logical = logical[:-1]
            if index == len(lines):
                raise PatchError("unfinished Maven configuration continuation")
        match = re.match(r"^\s*" + re.escape(DEFAULT_REPOSITORIES) + r"\s*[=:]\s*(.*)$", logical)
        if match:
            blocks.append((start, index, match.group(1)))
    if len(blocks) != 1:
        raise PatchError("exactly one explicit existing defaultRepositories property is required")
    start, end, value = blocks[0]
    entries = [part.strip() for part in value.split(",") if part.strip()]
    owned = [part for part in entries if "@id=thermite.androidtv" in part]
    if owned:
        if owned != [entry]:
            raise PatchError("a competing thermite Maven repository already exists")
        return previous
    replacement = DEFAULT_REPOSITORIES + "=" + entry + ("," + value if value else "") + "\n"
    return "".join([*lines[:start], replacement, *lines[end:]])


def _publish(path: Path, data: bytes) -> None:
    """Publish a completed staged output without following an existing link."""
    if path.is_symlink():
        raise PatchError("staged output must not be a symlink")
    handle, temporary = tempfile.mkstemp(prefix=".androidtv-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build(inputs: dict[str, Path], *, java: Path, runtime: Path, work: Path, output: Path,
          test_sources: Path | None = None) -> dict:
    """Compile exact upstream + reviewed patch; output only into an owned build directory."""
    lock = pins()
    paths = {key: verified(inputs[key], entry["sha256"]) for key, entry in lock["assets"].items()}
    runtime = Path(runtime)
    dependencies = sorted((runtime / "system").rglob("*.jar"))
    if not dependencies:
        raise PatchError("the pinned runtime/system dependency tree is required")
    if any(path.is_symlink() for path in dependencies):
        raise PatchError("runtime build dependencies must be regular files")
    dependency_hashes = {str(path.relative_to(runtime)): sha(path) for path in dependencies}
    system_hash = hashlib.sha256(json.dumps(dependency_hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if system_hash != lock["runtime_system_sha256"] or len(dependencies) != lock["runtime_system_jars"]:
        raise PatchError("runtime dependency tree differs from the pinned5.2.3 distribution")
    work, output = Path(work), Path(output)
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if work.is_symlink() or output.is_symlink():
        raise PatchError("build directories must not be symlinks")
    with tempfile.TemporaryDirectory(prefix="androidtv-", dir=work) as name:
        folder = Path(name)
        unsigned = folder / "upstream-unsigned.jar"
        rebuild(paths["bundle"], {}, unsigned, patched=False)
        sources = []
        for key, patch in PATCHES.items():
            source = folder / lock["assets"][key]["name"]
            source.write_bytes(paths[key].read_bytes())
            _run(["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", str(source), str(ROOT / patch)], cwd=folder)
            sources.append(source)
        sources.append(ROOT / "GoogleTVSocketLifecycle.java")
        classpath = ":".join(map(str, [unsigned, paths["annotations"], *dependencies]))
        classes = folder / "classes"
        compiler = [str(java), "-jar", str(paths["compiler"]), "-21", "-proc:none", "-nowarn"]
        compile_log = _run([*compiler, "-classpath", classpath, "-d", str(classes), *map(str, sources)])
        replacements = {str(path.relative_to(classes)): path.read_bytes() for path in classes.rglob("*.class")}
        report = {"version": 1, "upstream": lock["upstream"], "patched": lock["patched"],
                  "inputs": {key: sha(path) for key, path in paths.items()},
                  "patches": {name: sha(ROOT / name) for name in [*PATCHES.values(), "GoogleTVSocketLifecycle.java"]},
                  "dependencies": dependency_hashes,
                  "runtime_system_sha256": system_hash,
                  "compiler_log": compile_log,
                  "artifact": rebuild(paths["bundle"], replacements, folder / ARTIFACT),
                  "activation": "staged_only; owner native restart and loaded-bundle verification required"}
        if test_sources is not None:
            test_sources = Path(test_sources)
            fixtures = sorted(test_sources.glob("*.java"))
            if not fixtures:
                raise PatchError("offline Java lifecycle test fixtures are missing")
            tests = folder / "tests"
            _run([*compiler, "-classpath", str(classes) + ":" + classpath, "-d", str(tests), *map(str, fixtures)])
            report["java_tests"] = _run([str(java), "-Djava.security.manager=allow", "-ea", "-cp",
                str(tests) + ":" + str(classes) + ":" + classpath,
                "org.openhab.binding.androidtv.internal.protocol.googletv.LifecycleTest"], timeout=45)
            if (test_sources / "ActivationTest.java").is_file():
                hosts = folder / "private-hosts"
                hosts.write_text("127.0.0.1 localhost " + os.uname().nodename + "\n")
                report["activation_tests"] = _run([str(java), "-Djava.security.manager=allow",
                    "-Djdk.net.hosts.file=" + str(hosts), "-Duser.home=" + str(folder / "maven-home"), "-cp",
                    str(tests) + ":" + ":".join(map(str, dependencies)), "ActivationTest",
                    str(folder / "maven-test"), str(folder / ARTIFACT)], timeout=45)
        _publish(output / ARTIFACT, (folder / ARTIFACT).read_bytes())
        _publish(output / "overrides.properties.fragment", override_text("").encode())
        _publish(output / "maven-layout.json", (json.dumps({"version": 1, "coordinates": "mvn:" + COORDINATES,
            "relative_path": str(MAVEN_PATH), "default_repository_entry": repository_entry()}, indent=2) + "\n").encode())
        _publish(output / "build.json", (json.dumps(report, indent=2) + "\n").encode())
        return report
