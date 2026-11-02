"""Strict content equivalence for Pax's known cached-JAR container transformation."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import zipfile

UPDATE_LOCATION = "mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1"
MANIFEST = "META-INF/MANIFEST.MF"


class IntegrityError(ValueError):
    """Fixed reason only; never copy untrusted manifest values into diagnostics."""


def manifest_headers(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise IntegrityError("manifest_encoding") from exc
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise IntegrityError("manifest_encoding")
    headers: dict[str, str] = {}
    seen: set[str] = set()
    current = None
    finished = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line:
            finished = True
            continue
        if finished:
            raise IntegrityError("manifest_extra_section")
        if line.startswith(" "):
            if current is None:
                raise IntegrityError("manifest_orphan_continuation")
            headers[current] += line[1:]
            continue
        if ": " not in line:
            raise IntegrityError("manifest_invalid_header")
        name, value = line.split(": ", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise IntegrityError("manifest_invalid_header")
        if name.lower() in seen:
            raise IntegrityError("manifest_duplicate_header")
        seen.add(name.lower())
        current = name
        headers[name] = value
    if headers.get("Manifest-Version") != "1.0":
        raise IntegrityError("manifest_version")
    return headers


def compare_cached_jar(staged: Path, cached: Path, expected_staged_sha256: str) -> dict:
    """Verify all10913 content entries, allowing only Pax's exact added header.

    ZIP compression/order/attributes can change when Pax repacks the JAR. The
    authenticated source JAR must still match its external receipt hash; no
    non-manifest byte or entry-name difference is permitted in the cache.
    """
    staged, cached = Path(staged), Path(cached)
    if staged.is_symlink() or cached.is_symlink() or not staged.is_file() or not cached.is_file():
        raise IntegrityError("jar_regular_file_required")
    if staged.stat().st_size > 32 * 1024 * 1024 or cached.stat().st_size > 128 * 1024 * 1024:
        raise IntegrityError("jar_size")
    source_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    if source_hash != expected_staged_sha256:
        raise IntegrityError("staged_sha_mismatch")
    try:
        with zipfile.ZipFile(staged) as source, zipfile.ZipFile(cached) as native:
            source_names, native_names = source.namelist(), native.namelist()
            if len(source_names) != len(set(source_names)) or len(native_names) != len(set(native_names)):
                raise IntegrityError("jar_duplicate_entry")
            if set(source_names) != set(native_names) or MANIFEST not in source_names:
                raise IntegrityError("jar_entry_set_changed")
            source_manifest = source.read(MANIFEST)
            if native.getinfo(MANIFEST).file_size > len(source_manifest) + 512:
                raise IntegrityError("manifest_size")
            before = manifest_headers(source_manifest)
            after = manifest_headers(native.read(MANIFEST))
            allowed = dict(before)
            if "Bundle-UpdateLocation" not in before:
                allowed["Bundle-UpdateLocation"] = UPDATE_LOCATION
            if after != before and after != allowed:
                raise IntegrityError("manifest_changed")
            for name in source_names:
                if name == MANIFEST:
                    continue
                if (source.getinfo(name).file_size != native.getinfo(name).file_size
                        or source.read(name) != native.read(name)):
                    raise IntegrityError("jar_entry_bytes_changed")
    except IntegrityError:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
        raise IntegrityError("jar_invalid") from exc
    return {"staged_sha256": source_hash, "cached_sha256": hashlib.sha256(cached.read_bytes()).hexdigest(),
            "entries": len(source_names), "all_nonmanifest_entry_bytes_equal": True,
            "only_manifest_change": "exact_Bundle-UpdateLocation_added" if after != before else "none"}
