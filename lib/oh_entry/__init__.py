"""Small, pinned native HTTP entry helper; no compiler or new daemon at install time.

The generated JS owns registration/disposal, while every request runs in Java.
The checked-in JAR is a reproducible build artifact of EntryFilter.java. Its
exact bytes and source/compiler/API inputs are pinned in inputs.json.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request
import zipfile

import oh_http
import oh_routes

ROOT = Path(__file__).resolve().parent
JAR = "filter.jar"
JAR_PATH = "automation/js/node_modules/thermite-entry/filter.jar"
SCRIPT_PATH = "automation/js/thermite_entry.js"
CLASS = "org/thermite/entry/EntryFilter.class"


def ready(url: str, timeout: float) -> bool:
    """Probe both native entry routes once, within one finite total allowance.

    Only the literal loopback HTTP origin used by the native lifecycle is
    accepted. Requests carry no credentials, use no proxy, follow no redirect,
    and close after headers. A 200 at home.html would not prove its identity:
    MainUI's SPA fallback can also answer 200, so that is not a readiness test.
    """
    if not isinstance(url, str):
        raise TypeError("native entry origin must be a string")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("native entry timeout must be finite and positive")
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\])(?::([1-9][0-9]{0,4}))?/?", url)
    if match is None or int(match[2] or 80) > 65535:
        return False
    origin = f"http://{match[1]}:{int(match[2] or 80)}"
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise oh_http.DeadlineExpired("native entry deadline expired")
        return value

    try:
        for path in ("/", "/overview/"):
            status, response = oh_http.headers(urllib.request.Request(origin + path, method="HEAD"),
                                               timeout=timeout, remaining=remaining)
            if ([value.strip().lower() for value in response.get_all("Cache-Control", [])] != ["no-store"]):
                return False
            if path == "/":
                if status != 200 or response.get_all("X-Thermite-Page", []) != ["home"] or response.get_all("Location", []):
                    return False
            elif status != 302 or response.get_all("Location", []) != ["/"]:
                return False
    except (OSError, http.client.HTTPException, oh_http.DeadlineExpired):
        return False
    return True


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
