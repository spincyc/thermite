"""Finite, hash-verified application releases and their persistent rollback cache."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

VERSION = "0.1.0.dev0"
FILE = "thermite-release.json"
HEX = re.compile(r"[0-9a-f]{64}")


class ReleaseError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(data: dict) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ReleaseError("invalid application path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ReleaseError("application paths must stay inside their release")
    return name


def create(files: dict[str, bytes], version: str = VERSION) -> dict:
    body = {"version": 1, "application": version,
            "files": {safe(name): digest(value) for name, value in sorted(files.items()) if name != FILE}}
    return {**body, "digest": digest(encoded(body))}


def manifest(root: Path, expected: str | None = None) -> dict:
    root = Path(root)
    path = root / FILE
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise ReleaseError("application release must contain a regular manifest")
    try: data = json.loads(path.read_text())
    except (ValueError, OSError) as exc: raise ReleaseError("invalid application release manifest") from exc
    if not isinstance(data, dict) or set(data) != {"version", "application", "files", "digest"} or data.get("version") != 1:
        raise ReleaseError("unsupported application release manifest")
    if not isinstance(data["application"], str) or not isinstance(data["files"], dict) or not HEX.fullmatch(str(data["digest"])):
        raise ReleaseError("invalid application release identity")
    body = {key: data[key] for key in ("version", "application", "files")}
    if digest(encoded(body)) != data["digest"] or expected is not None and data["digest"] != expected:
        raise ReleaseError("application release identity does not match")
    for name, sha in data["files"].items():
        safe(name)
        target = root / name
        ancestors = [root.joinpath(*PurePosixPath(name).parts[:count]) for count in range(1, len(PurePosixPath(name).parts))]
        if not HEX.fullmatch(str(sha)) or target.is_symlink() or any(p.is_symlink() for p in ancestors):
            raise ReleaseError("application files must be regular and remain inside their release")
        if not target.is_file() or digest(target.read_bytes()) != sha:
            raise ReleaseError(f"application file differs from its release: {name}")
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink(): raise ReleaseError("application release contains a symlink")
        if "__pycache__" in relative.parts:
            # pip may compile listed Python sources during installation. Those
            # caches are accepted as inert installer output, never authority:
            # both launchers select a fresh nonexistent cache prefix first.
            if path.is_dir() and relative.name == "__pycache__" and relative.parts.count("__pycache__") == 1:
                if any(PurePosixPath(name).parent == relative.parent and name.endswith(".py") for name in data["files"]):
                    continue
            if path.is_file() and path.parent.name == "__pycache__":
                try:
                    source = Path(importlib.util.source_from_cache(str(path))).relative_to(root)
                except ValueError:
                    source = None
                tag = re.escape(sys.implementation.cache_tag)
                if source is not None and str(source) in data["files"] and re.fullmatch(r".+\." + tag + r"(?:\.opt-[0-9]+)?\.pyc", path.name):
                    with path.open("rb") as handle: header = handle.read(16)
                    if (len(header) == 16 and path.stat().st_size > 16 and header[:4] == importlib.util.MAGIC_NUMBER
                            and int.from_bytes(header[4:8], "little") in {0, 1, 3}):
                        continue
            raise ReleaseError("application release contains an unrecognized bytecode cache")
        if path.is_file() and str(relative) not in {*data["files"], FILE}:
            raise ReleaseError("application release contains an unlisted file")
    return data


def cache(program: Path, store: Path) -> Path:
    """Copy a complete verified release once; never overwrite a prior release."""
    data = manifest(program)
    program = Path(program).resolve()
    store = Path(store).expanduser().absolute()
    if any(parent.is_symlink() for parent in (store, *store.parents)):
        raise ReleaseError("application cache ancestors must not be symlinks")
    store = store.resolve()
    if store == program or program in store.parents or store in program.parents:
        raise ReleaseError("application cache and installed program must be separate")
    store.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = store / data["digest"]
    if target.exists():
        manifest(target, data["digest"])
        return target
    with tempfile.TemporaryDirectory(prefix=".release-", dir=store) as folder:
        temporary = Path(folder) / "complete"
        temporary.mkdir(mode=0o700)
        for name in [*data["files"], FILE]:
            out = temporary / name; out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(program) / name, out)
            out.chmod(0o555 if name.startswith("bin/") else 0o444)
        manifest(temporary, data["digest"])
        temporary.rename(target)
    return target


def blobs(root: Path, names: tuple[str, ...], expected: str) -> dict[str, bytes]:
    data = manifest(root, expected)
    missing = set(names) - set(data["files"])
    if missing: raise ReleaseError("application release lacks helper dependencies: " + ", ".join(sorted(missing)))
    return {name: (Path(root) / name).read_bytes() for name in names}
