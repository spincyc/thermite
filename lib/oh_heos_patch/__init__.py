"""Rebuild the pinned HEOS binding with a small, auditable reconnect repair.

Build artifacts and owner activation inputs are staged only. This module never
connects to a device, reads or edits native userdata, updates OSGi, or starts a
service. It targets exactly the openHAB release in versions.json; when that
moves, the build and `make check` refuse until the patch is retired or rebased.
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
AREA = ROOT.parents[1]
LOCK = ROOT / "inputs.json"
VERSIONS = AREA / "versions.json"
PACKAGE = "org/openhab/binding/heos/internal/"
ARTIFACT = "org.openhab.binding.heos-5.2.3.thermite1.jar"
COORDINATES = "org.openhab.addons.bundles/org.openhab.binding.heos/5.2.3.thermite1"
MAVEN_PATH = Path("org/openhab/addons/bundles/org.openhab.binding.heos/5.2.3.thermite1") / ARTIFACT
OVERRIDE = f'mvn:{COORDINATES};range="[5.2.3,5.2.4)"'
REPOSITORY = Path("/openhab/userdata/thermite/maven")
REPOSITORY_ID = "thermite.heos"
DEFAULT_REPOSITORIES = "org.ops4j.pax.url.mvn.defaultRepositories"
PATCHES = {"system": "HeosSystem.patch", "events": "HeosEventController.patch", "command": "HeosSendCommand.patch"}
EXPECTED_CLASSES = frozenset({
    PACKAGE + "api/HeosSystem.class",
    PACKAGE + "api/HeosSystem$KeepAliveRunnable.class",
    PACKAGE + "api/HeosEventController.class",
    PACKAGE + "resources/HeosSendCommand.class",
})
TEST_MAIN = "org.openhab.binding.heos.internal.api.ReconnectTest"
PROBE = "thermite_heos_patch_probe.js"


class PatchError(ValueError):
    """An unverified input, a moved version pin or a failed build; do not stage or activate it."""


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pins() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def pinned_openhab(versions: Path = VERSIONS) -> str:
    return json.loads(Path(versions).read_text(encoding="utf-8"))["openhab"]["version"]


def require_pinned(versions: Path = VERSIONS) -> str:
    """Refuse every openHAB release but the one this patch was written and tested against."""
    lock = pins()
    pinned = pinned_openhab(versions)
    if pinned != lock["upstream"]:
        retire = "; ".join(f"{key}: {value}" for key, value in lock["retire_when"].items())
        raise PatchError(
            f"{Path(versions).name} pins openHAB {pinned}, but lib/oh_heos_patch patches exactly the "
            f"{lock['upstream']} HEOS binding. If openHAB {pinned}'s HEOS binding contains both upstream fixes "
            f"({retire}), retire the patch: roll back its override on every runtime that applied it, then delete "
            "lib/oh_heos_patch, tests/heos_patch, tests/test_heos_patch.py and their .thermite-export.json entries. "
            f"Otherwise rebase it onto {pinned}: update inputs.json (bundle, sources, runtime hash), regenerate the "
            "three patches with zero fuzz, and rebuild with --test-sources so the Java gate passes on the new "
            "classes and still fails on the unmodified ones. See lib/oh_heos_patch/README.md.")
    return pinned


def verified(path: Path, expected: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or sha(path) != expected:
        raise PatchError(f"unverified build input: {path.name}")
    return path


def _execute(args: list[str], *, cwd: Path | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PatchError(f"build tool failed: {Path(args[0]).name}") from exc


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = 90) -> str:
    result = _execute(args, cwd=cwd, timeout=timeout)
    if result.returncode:
        raise PatchError(f"build tool failed ({result.returncode}): {result.stdout[-3000:]}{result.stderr[-3000:]}")
    return result.stdout + result.stderr


def _signature(name: str) -> bool:
    upper = name.upper()
    return upper.startswith("META-INF/") and upper.endswith((".SF", ".RSA", ".DSA", ".EC"))


def manifest(data: bytes, *, patched: bool) -> bytes:
    # Keep the complete main OSGi contract: imports, exports and service components.
    lines = data.replace(b"\r\n", b"\n").split(b"\n\n", 1)[0].rstrip(b"\n").split(b"\n")
    if patched:
        if [line.startswith(b"Bundle-Version:") for line in lines].count(True) != 1 \
                or lines.count(b"Bundle-Version: 5.2.3") != 1:
            raise PatchError("unexpected upstream bundle version")
        lines[lines.index(b"Bundle-Version: 5.2.3")] = b"Bundle-Version: 5.2.3.thermite1"
        lines.append(b"Thermite-Patch: heos-reconnect-v1")
    return b"\r\n".join(lines) + b"\r\n\r\n"


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


def class_hashes(bundle: Path) -> dict[str, str]:
    with zipfile.ZipFile(bundle) as archive:
        return {name: hashlib.sha256(archive.read(name)).hexdigest() for name in sorted(EXPECTED_CLASSES)}


def override_text(previous: str) -> str:
    """Pure merge of userdata/etc/overrides.properties; refuses a competing HEOS override.

    Unrelated lines, including another Thermite binding override, stay byte-for-byte.
    """
    for line in previous.splitlines():
        value = line.strip()
        if value and not value.startswith("#") and "org.openhab.binding.heos" in value and value != OVERRIDE:
            raise PatchError("a HEOS override already exists; review it explicitly")
    if OVERRIDE in [line.strip() for line in previous.splitlines()]:
        return previous
    separator = "" if not previous or previous.endswith("\n") else "\n"
    return previous + separator + "# thermite pinned HEOS reconnect repair\n" + OVERRIDE + "\n"


def repository_entry(repository: Path = REPOSITORY) -> str:
    repository = Path(repository)
    if (not repository.is_absolute() or ".." in repository.parts
            or any(ord(char) < 32 for char in str(repository))):
        raise PatchError("Maven repository requires an absolute local path")
    return repository.as_uri().rstrip("/") + "/@id=" + REPOSITORY_ID


def maven_config_text(previous: str, *, repository: Path = REPOSITORY) -> str:
    """Prepend ONE private read-only default repository; preserve every other setting.

    Karaf's override processing accepts only mvn: URIs, so the derivative needs a
    durable repository outside tmp/mvn. The entry is HEOS's own, even when another
    Thermite derivative already shares the directory, so each rolls back alone.
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
    owned = [part for part in entries if part.endswith("@id=" + REPOSITORY_ID)]
    if owned:
        if owned != [entry]:
            raise PatchError("a competing thermite HEOS Maven repository already exists")
        return previous
    replacement = DEFAULT_REPOSITORIES + "=" + entry + ("," + value if value else "") + "\n"
    return "".join([*lines[:start], replacement, *lines[end:]])


def _publish(path: Path, data: bytes) -> None:
    """Publish a completed staged output without following an existing link."""
    if path.is_symlink():
        raise PatchError("staged output must not be a symlink")
    handle, temporary = tempfile.mkstemp(prefix=".heos-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build(inputs: dict[str, Path], *, java: Path, runtime: Path, work: Path, output: Path,
          test_sources: Path | None = None, versions: Path = VERSIONS) -> dict:
    """Compile exact upstream + reviewed patch; output only into an owned build directory.

    With test_sources, the reconnect test must pass on the rebuilt artifact and,
    as a negative control, fail on the unmodified upstream bundle.
    """
    require_pinned(versions)
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
        raise PatchError(f"runtime dependency tree differs from the pinned {lock['upstream']} distribution")
    work, output = Path(work), Path(output)
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if work.is_symlink() or output.is_symlink():
        raise PatchError("build directories must not be symlinks")
    with tempfile.TemporaryDirectory(prefix="heos-", dir=work) as name:
        folder = Path(name)
        sources = []
        for key, patch in PATCHES.items():
            source = folder / lock["assets"][key]["name"]
            source.write_bytes(paths[key].read_bytes())
            _run(["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", str(source), str(ROOT / patch)], cwd=folder)
            sources.append(source)
        libraries = ":".join(map(str, [paths["annotations"], *dependencies]))
        classes = folder / "classes"
        compiler = [str(java), "-jar", str(paths["compiler"]), "-21", "-proc:none", "-nowarn"]
        compile_log = _run([*compiler, "-classpath", f"{paths['bundle']}:{libraries}", "-d", str(classes),
                            *map(str, sources)])
        replacements = {str(path.relative_to(classes)): path.read_bytes() for path in classes.rglob("*.class")}
        artifact = folder / ARTIFACT
        report = {"version": 1, "upstream": lock["upstream"], "patched": lock["patched"],
                  "inputs": {key: sha(path) for key, path in paths.items()},
                  "patches": {name: sha(ROOT / name) for name in PATCHES.values()},
                  "dependencies": dependency_hashes,
                  "runtime_system_sha256": system_hash,
                  "compiler_log": compile_log,
                  "artifact": rebuild(paths["bundle"], replacements, artifact),
                  "classes": class_hashes(artifact),
                  "upstream_classes": class_hashes(paths["bundle"]),
                  "activation": "staged_only; owner stop-first restart, feature refresh and loaded-class verification required"}
        if test_sources is not None:
            fixtures = sorted(Path(test_sources).glob("*.java"))
            if not fixtures:
                raise PatchError("offline Java reconnect test fixtures are missing")
            tests = folder / "tests"
            _run([*compiler, "-classpath", f"{classes}:{paths['bundle']}:{libraries}", "-d", str(tests),
                  *map(str, fixtures)])
            command = [str(java), "-Djava.security.manager=allow", "-ea", "-cp"]
            report["java_tests"] = _run([*command, f"{tests}:{artifact}:{libraries}", TEST_MAIN], timeout=180)
            control = _execute([*command, f"{tests}:{paths['bundle']}:{libraries}", TEST_MAIN], timeout=180)
            failed = [line for line in control.stdout.splitlines() if line.startswith("FAIL ")]
            if control.returncode == 0 or not failed:
                raise PatchError("negative control passed: the reconnect test no longer detects the upstream defect")
            report["negative_control"] = {"exit": control.returncode, "failures": failed}
        _publish(output / ARTIFACT, artifact.read_bytes())
        _publish(output / "overrides.properties.fragment", override_text("").encode())
        _publish(output / "maven-layout.json", (json.dumps({"version": 1, "coordinates": "mvn:" + COORDINATES,
            "relative_path": str(MAVEN_PATH), "default_repository_entry": repository_entry()}, indent=2) + "\n").encode())
        _publish(output / "build.json", (json.dumps(report, indent=2) + "\n").encode())
        return report


def probe(expected_version: str, expected_classes: dict[str, str]) -> str:
    config = json.dumps({"version": expected_version, "classes": expected_classes}, sort_keys=True)
    return (ROOT / "activation_probe.js").read_text(encoding="utf-8").replace("__THERMITE_CONFIG__", config)


def prepare(staged: Path, *, maven: Path, output: Path, overrides: Path | None = None,
            versions: Path = VERSIONS) -> dict:
    """Owner activation data from COPIES of the runtime's two Karaf files.

    Reads no live userdata: the owner copies the files out and installs the
    results. Requires a staged build that passed the Java gate and its control.
    """
    require_pinned(versions)
    lock = pins()
    staged, output = Path(staged), Path(output)
    receipt = json.loads((staged / "build.json").read_text(encoding="utf-8"))
    artifact = verified(staged / ARTIFACT, str(receipt.get("artifact", {}).get("sha256")))
    if (receipt.get("upstream") != lock["upstream"] or receipt.get("patched") != lock["patched"]
            or receipt.get("inputs", {}).get("bundle") != lock["assets"]["bundle"]["sha256"]
            or receipt.get("classes") != class_hashes(artifact)
            or "java_tests" not in receipt or not receipt.get("negative_control", {}).get("failures")):
        raise PatchError("the staged build is not a complete, gated build of this patch")
    before = {"overrides.properties": Path(overrides).read_bytes() if overrides is not None else None,
              "org.ops4j.pax.url.mvn.cfg": Path(maven).read_bytes()}
    try:
        desired = {"overrides.properties": override_text((before["overrides.properties"] or b"").decode()).encode(),
                   "org.ops4j.pax.url.mvn.cfg": maven_config_text(before["org.ops4j.pax.url.mvn.cfg"].decode()).encode()}
    except UnicodeError as exc:
        raise PatchError("Karaf configuration is not UTF-8 text") from exc
    if output.exists() or output.is_symlink():
        raise PatchError("a fresh output directory is required")
    output.mkdir(mode=0o700, parents=False)
    files = {ARTIFACT: artifact.read_bytes(),
             "apply-" + PROBE: probe(lock["patched"], receipt["classes"]).encode(),
             "rollback-" + PROBE: probe(lock["upstream"], receipt["upstream_classes"]).encode(),
             **desired}
    files.update({name + ".before": data for name, data in before.items() if data is not None})
    for name, data in sorted(files.items()):
        _publish(output / name, data)
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items()))
    _publish(output / "SHA256SUMS", sums.encode())
    return {"output": str(output), "artifact_sha256": sha(artifact), "maven_path": str(MAVEN_PATH),
            "overrides_changed": desired["overrides.properties"] != before["overrides.properties"],
            "maven_changed": desired["org.ops4j.pax.url.mvn.cfg"] != before["org.ops4j.pax.url.mvn.cfg"],
            "activation": "not_started"}
