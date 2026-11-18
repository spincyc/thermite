"""Rebuild the pinned Android TV binding with a small, auditable lifecycle patch.

An install puts the repaired bundle into the runtime of every house whose
conf lists the Android TV binding (lib/oh_addons.py): the pinned upstream
bundle with the compiled classes this package carries (classes/, assemble).
The build here is what makes and proves those classes; it never connects to
a device or touches a runtime. The repair targets exactly the openHAB
release in versions.json; when that moves, an install and `make check`
refuse until it is retired or rebased.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "inputs.json"
PACKAGE = "org/openhab/binding/androidtv/internal/"
AREA = ROOT.parents[1]
VERSIONS = AREA / "versions.json"
#: The add-on whose houses run this repair, and its bundle.
ADDON = "binding-androidtv"
BUNDLE = "org.openhab.binding.androidtv"
TITLE = "Android TV connection repair"
MARKER = "googletv-lifecycle-v1"
ARTIFACT = "org.openhab.binding.androidtv-5.2.3.thermite1.jar"
UPSTREAM_COORDINATES = "org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3"
COORDINATES = "org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1"
MAVEN_PATH = Path("org/openhab/addons/bundles/org.openhab.binding.androidtv/5.2.3.thermite1") / ARTIFACT
#: Karaf's override: this bundle in place of any 5.2.3 one of the same name.
OVERRIDE = f'mvn:{COORDINATES};range="[5.2.3,5.2.4)"'
COMMENT = "thermite pinned Android TV lifecycle repair"
PATCHES = {"manager": "googletv-lifecycle.patch", "handler": "handler-ownership.patch"}
EXPECTED_CLASSES = frozenset({
    PACKAGE + "AndroidTVHandler.class",
    PACKAGE + "protocol/googletv/GoogleTVConnectionManager.class",
    PACKAGE + "protocol/googletv/GoogleTVConnectionManager$1.class",
    PACKAGE + "protocol/googletv/GoogleTVSocketLifecycle.class",
    PACKAGE + "protocol/googletv/GoogleTVSocketLifecycle$Attempt.class",
})


class PatchError(ValueError):
    """An unverified input, a moved version pin or a failed build; nothing of it is installed."""


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pins() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def refusal(pinned: str) -> str:
    """What to do when versions.json pins another openHAB release than the repair's."""
    lock = pins()
    retire = "; ".join(f"{key}: {value}" for key, value in lock["retire_when"].items())
    return (f"versions.json pins openHAB {pinned}, but lib/oh_androidtv_patch repairs exactly the "
            f"{lock['upstream']} Android TV binding, and a house with Android TV or Google TV devices is not "
            f"installed without it. If openHAB {pinned}'s binding contains the upstream fix ({retire}), "
            "retire the repair: delete lib/oh_androidtv_patch, tests/androidtv_patch, "
            "tests/test_androidtv_patch.py, their .thermite-export.json entries and its name in lib/oh_addons.py "
            "REPAIRS; the next install takes its bundle and override out of each runtime. Otherwise rebase it onto "
            f"{pinned}: update inputs.json (bundle, sources, runtime hash), regenerate the two patches with zero "
            "fuzz, and rebuild with --test-sources and --write-classes so the Java gate passes on the new classes. "
            "See lib/oh_androidtv_patch/README.md.")


def require_pinned(versions: Path = VERSIONS) -> str:
    """Refuse every openHAB release but the one this repair was written and tested against."""
    pinned = json.loads(Path(versions).read_text(encoding="utf-8"))["openhab"]["version"]
    if pinned != pins()["upstream"]:
        raise PatchError(refusal(pinned))
    return pinned


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
        main += b"\nThermite-Patch: " + MARKER.encode()
    return main.replace(b"\n", b"\r\n") + b"\r\n\r\n"


def assemble(bundle, replacements: dict[str, bytes], *, patched: bool = True) -> bytes:
    """The bundle (a path or an open file) rebuilt with `replacements` in
    place of its own classes: the same bytes on one host for the same
    input, and the same content on every host."""
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
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as dest:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            dest.writestr(info, content)
    return buffer.getvalue()


def rebuild(bundle: Path, replacements: dict[str, bytes], output: Path, *, patched: bool = True) -> dict:
    Path(output).write_bytes(assemble(bundle, replacements, patched=patched))
    return {"sha256": sha(output), "size": output.stat().st_size,
            "replaced": sorted(replacements), "vendor_signature_preserved": False}


def committed() -> dict[str, bytes]:
    """The compiled classes of the repair, as this checkout carries them
    (classes/): what an install puts into the bundle, with no compiler."""
    return {name: (ROOT / "classes" / name).read_bytes() for name in sorted(EXPECTED_CLASSES)}


def reviewed(replacements: dict[str, bytes], artifact: Path, *, write: bool = False) -> None:
    """Hold a build against what inputs.json pins and classes/ carries: the
    classes an install uses are the ones this source compiles to. `write`
    records a new build instead (after the patch or its inputs changed)."""
    import oh_addons
    classes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(replacements.items())}
    content = oh_addons.identity(artifact)[3]
    lock = pins()
    if write:
        shutil.rmtree(ROOT / "classes", ignore_errors=True)
        for name, data in replacements.items():
            (ROOT / "classes" / name).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / "classes" / name).write_bytes(data)
        lock["classes"], lock["content_sha256"] = classes, content
        LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        return
    if classes != lock.get("classes") or content != lock.get("content_sha256"):
        raise PatchError("this build is not the one inputs.json pins (classes, content_sha256): after a reviewed "
                         "change to the patch or its inputs, build with --write-classes and commit classes/ and "
                         "inputs.json with it")
    if {name: hashlib.sha256(data).hexdigest() for name, data in committed().items()} != classes:
        raise PatchError("classes/ does not hold the classes inputs.json pins")


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
          test_sources: Path | None = None, versions: Path = VERSIONS, write_classes: bool = False) -> dict:
    """Compile exact upstream + reviewed patch; output only into an owned build directory.

    The build must be the one inputs.json pins and classes/ carries;
    write_classes records it as that instead, and needs the Java gate."""
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
                  "activation": "by thermite install, for a house whose conf lists " + ADDON}
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
        if write_classes and test_sources is None:
            raise PatchError("--write-classes needs --test-sources: only a gated build is recorded")
        reviewed(replacements, folder / ARTIFACT, write=write_classes)
        _publish(output / ARTIFACT, (folder / ARTIFACT).read_bytes())
        _publish(output / "build.json", (json.dumps(report, indent=2) + "\n").encode())
        return report
