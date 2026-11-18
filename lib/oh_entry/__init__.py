"""Small, pinned native HTTP entry helper for a runtime with no gateway in
front of it: the sandbox's copy of conf/ (oh_render.direct). No compiler or
new daemon is needed. What thermite generate writes does not have it: an
install's pages go through the gateway.

The generated JS owns registration/disposal, while every request runs in Java.
The checked-in JAR is a reproducible build artifact of EntryFilter.java. Its
exact bytes and source/compiler/API inputs are pinned in inputs.json.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

import oh_routes

ROOT = Path(__file__).resolve().parent
JAR = "filter.jar"
JAR_PATH = "automation/js/node_modules/thermite-entry/filter.jar"
SCRIPT_PATH = "automation/js/thermite_entry.js"
CLASS = "org/thermite/entry/EntryFilter.class"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pins() -> dict:
    return json.loads((ROOT / "inputs.json").read_text(encoding="utf-8"))


def approved_artifacts() -> dict[str, str]:
    """Exact source-controlled binary exception for the public export guard."""
    return {str((ROOT / JAR).relative_to(ROOT.parent.parent)): pins()["artifact_sha256"]}


def checked_artifact() -> bytes:
    lock = pins()
    source = (ROOT / "EntryFilter.java").read_bytes()
    artifact = (ROOT / JAR).read_bytes()
    if sha(source) != lock["source_sha256"] or sha(artifact) != lock["artifact_sha256"]:
        raise ValueError("Thermite entry source or artifact differs from its reviewed pin")
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        if archive.namelist() != ["META-INF/MANIFEST.MF", CLASS]:
            raise ValueError("Thermite entry artifact has unexpected entries")
    return artifact


def files() -> dict[str, str | bytes]:
    """Generated paths sort the helper before its activating script during deploy."""
    artifact = checked_artifact()
    script = (ROOT / "loader.js").read_text(encoding="utf-8")
    script = script.replace("@JAR_PATH@", JAR_PATH).replace("@JAR_SHA256@", sha(artifact))
    script = script.replace("@ROUTES_JSON@", json.dumps(oh_routes.ROUTES, separators=(",", ":")))
    script = script.replace("@PATHS_JSON@", json.dumps([*oh_routes.ROUTES, *oh_routes.ALIASES], separators=(",", ":")))
    return {JAR_PATH: artifact, SCRIPT_PATH: script}


def package(class_bytes: bytes) -> bytes:
    """Stable ZIP metadata and stored entries avoid timestamp/zlib build drift."""
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in (("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\n\r\n"),
                              (CLASS, class_bytes)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return result.getvalue()


def rebuild(*, java: Path, compiler: Path, servlet: Path, work: Path) -> bytes:
    """Explicit developer gate; verifies pinned inputs, never downloads or installs."""
    lock = pins()
    for path, expected in ((ROOT / "EntryFilter.java", lock["source_sha256"]),
                           (compiler, lock["compiler"]["sha256"]),
                           (servlet, lock["servlet"]["sha256"])):
        if sha(Path(path).read_bytes()) != expected:
            raise ValueError("Thermite entry build input differs from its reviewed pin")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="entry-", dir=work) as temporary:
        classes = Path(temporary)
        subprocess.run([str(java), "-jar", str(compiler), "-17", "-g:none", "-proc:none", "-nowarn",
                        "-classpath", str(servlet), "-d", str(classes), str(ROOT / "EntryFilter.java")],
                       check=True, capture_output=True, text=True, timeout=30)
        compiled = {str(path.relative_to(classes)): path.read_bytes() for path in classes.rglob("*.class")}
        if set(compiled) != {CLASS}:
            raise ValueError("Thermite entry compiler produced unexpected classes")
        return package(compiled[CLASS])
