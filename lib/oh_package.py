"""Build a wheel from the reviewed public artifact, never a private tree walk.

Also a minimal PEP 517 backend (wheel and sdist) for the public export. Core
metadata comes from pyproject.toml's [project] table in those reviewed bytes.
No installer, native runtime, daemon or network operation is invoked here.
"""
from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile

from oh_release import VERSION, FILE, create, encoded, safe

BOOTSTRAP = '''"""Installed thermite entry point; program resources stay read-only."""
def main():
    import os
    import sys
    from pathlib import Path
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parent / "runtime"
    sys.pycache_prefix = str(root / (".uncached-" + os.urandom(16).hex()))
    sys.path.insert(0, str(root / "lib"))
    import oh_paths
    try:
        argv = oh_paths.select_args(sys.argv[1:])
        from oh_cli import run
        return run("thermite", argv)
    except (oh_paths.SiteError, ValueError) as exc:
        print("thermite: " + str(exc), file=sys.stderr)
        return 2
'''


def runtime_files(public: dict[str, bytes]) -> dict[str, bytes]:
    """Retain only public program resources and fictional init templates."""
    prefixes = ("bin/", "lib/", "web/", "tui/", "site/", "instance/", "docs/", "tests/", "iso/")
    names = {"versions.json", "README.md", "FAQ.md", "Makefile", "CHANGELOG.md", "pyproject.toml", "requirements.txt"}
    files = {safe(name): value for name, value in public.items() if name.startswith(prefixes) or name in names}
    required = {"bin/thermite", "lib/oh_paths.py", "lib/oh_release.py", "instance/devices.yaml", "instance/host.yaml", "versions.json"}
    if not required <= files.keys(): raise ValueError("public artifact lacks required application files")
    files[FILE] = encoded(create(files))
    return files


#: The one import package the wheel provides; console scripts must name it.
PACKAGE = "thermite_app"
_PROJECT_KEYS = frozenset(("name", "dynamic", "description", "requires-python", "dependencies",
                           "optional-dependencies", "license", "scripts", "keywords", "classifiers", "urls"))
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_SCRIPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: Fixed archive timestamp: reproducible, and representable in ZIP files.
EPOCH = 315532800


def project(public: dict[str, bytes]) -> dict:
    """The validated [project] table of the reviewed pyproject.toml.

    Unsupported keys fail rather than silently vanishing from the metadata.
    Values become RFC 822 header lines, so line breaks are refused.
    """
    def fail(problem: str):
        raise ValueError(f"pyproject.toml [project]: {problem}")

    def text(value, key: str) -> str:
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n"):
            fail(f"{key} must be one line of text")
        return value

    def strings(value, key: str) -> list[str]:
        if not isinstance(value, list):
            fail(f"{key} must be a list of strings")
        return [text(item, key) for item in value]

    try:
        table = tomllib.loads(public["pyproject.toml"].decode("utf-8"))["project"]
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("pyproject.toml must contain a readable [project] table") from exc
    if not isinstance(table, dict):
        fail("must be a table")
    unknown = sorted(set(table) - _PROJECT_KEYS)
    if unknown:
        fail("unsupported key " + ", ".join(unknown))
    if table.get("dynamic") != ["version"]:
        fail('dynamic must be ["version"]; the version comes from oh_release.VERSION')
    if not _NAME.fullmatch(text(table.get("name"), "name")):
        fail("name is not a valid distribution name")
    for key in ("description", "requires-python"):
        if key in table:
            text(table[key], key)
    for key in ("dependencies", "keywords", "classifiers"):
        strings(table.get(key, []), key)
    extras = table.get("optional-dependencies", {})
    if not isinstance(extras, dict) or not all(_NAME.fullmatch(name) for name in extras):
        fail("optional-dependencies must map extra names to requirement lists")
    for name, requirements in extras.items():
        strings(requirements, "optional-dependencies." + name)
    license = table.get("license")
    if license is not None and not isinstance(license, str) and (not isinstance(license, dict) or set(license) != {"text"}):
        fail("license must be an SPDX expression or {text = ...}")
    if license is not None:
        text(license if isinstance(license, str) else license["text"], "license")
    urls = table.get("urls", {})
    if not isinstance(urls, dict):
        fail("urls must be a table")
    for label, url in urls.items():
        text(label, "urls")
        text(url, "urls." + label)
    scripts = table.get("scripts", {})
    if not isinstance(scripts, dict):
        fail("scripts must be a table")
    for name, target in scripts.items():
        if not _SCRIPT.fullmatch(name) or text(target, "scripts." + name) != PACKAGE + ":main":
            fail(f"script {name} must run {PACKAGE}:main")
    return table


def distribution(table: dict) -> str:
    """The normalized distribution name used in wheel and sdist filenames."""
    return re.sub(r"[-_.]+", "_", table["name"]).lower()


def metadata(table: dict) -> bytes:
    """Core metadata, in the same field order for every unchanged input."""
    license = table.get("license")
    expression = isinstance(license, str)
    lines = [f"Metadata-Version: {'2.4' if expression else '2.3'}", f"Name: {table['name']}", f"Version: {VERSION}"]
    if "description" in table:
        lines.append(f"Summary: {table['description']}")
    if "requires-python" in table:
        lines.append(f"Requires-Python: {table['requires-python']}")
    lines += [f"Requires-Dist: {requirement}" for requirement in table.get("dependencies", [])]
    for extra, requirements in sorted(table.get("optional-dependencies", {}).items()):
        lines.append(f"Provides-Extra: {extra}")
        for requirement in requirements:
            specifier, _, marker = (part.strip() for part in requirement.partition(";"))
            condition = f'({marker}) and extra == "{extra}"' if marker else f'extra == "{extra}"'
            lines.append(f"Requires-Dist: {specifier}; {condition}")
    if expression:
        lines.append(f"License-Expression: {license}")
    elif license is not None:
        lines.append(f"License: {license['text']}")
    if table.get("keywords"):
        lines.append("Keywords: " + ",".join(table["keywords"]))
    lines += [f"Classifier: {classifier}" for classifier in table.get("classifiers", [])]
    lines += [f"Project-URL: {label}, {url}" for label, url in table.get("urls", {}).items()]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def entry_points(table: dict) -> bytes:
    scripts = table.get("scripts", {})
    if not scripts:
        return b""
    return ("[console_scripts]\n" + "".join(f"{name} = {target}\n" for name, target in sorted(scripts.items()))).encode()


def wheel(public: dict[str, bytes], output: Path) -> tuple[Path, dict]:
    files = runtime_files(public)
    release = json.loads(files[FILE])
    table = project(public)
    dist = distribution(table)
    info = f"{dist}-{VERSION}.dist-info"
    entries = {f"{PACKAGE}/__init__.py": BOOTSTRAP.encode(),
               **{f"{PACKAGE}/runtime/" + name: value for name, value in files.items()},
               info + "/METADATA": metadata(table),
               info + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: thermite\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    if points := entry_points(table):
        entries[info + "/entry_points.txt"] = points
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(entries.items()):
        token = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + token, len(data)])
    writer.writerow([info + "/RECORD", "", ""])
    entries[info + "/RECORD"] = record.getvalue().encode()
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    target = output / f"{dist}-{VERSION}-py3-none-any.whl"
    if target.is_symlink(): raise ValueError("wheel output must not be a symlink")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name, data in sorted(entries.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (0o755 if "/bin/" in name else 0o644) << 16
            out.writestr(entry, data)
    return target, release


def public_files(area: Path | None = None) -> dict[str, bytes]:
    import oh_paths
    import oh_public
    root = Path(area or oh_paths.PROGRAM)
    manifest = root / oh_public.MANIFEST
    if not manifest.exists() and (root / FILE).is_file():
        from oh_release import manifest as verify_release
        release = verify_release(root)
        files = {name: (root / name).read_bytes() for name in release["files"]}
        problems = oh_public.guard(files, root)
        if problems:
            raise ValueError("public package privacy checks failed:\n" + "\n".join(problems))
        return files
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError("package from the Thermite application repository with its reviewed file manifest")
    data = json.loads(manifest.read_text())
    if (not isinstance(data, dict) or set(data) != {"version", "paths"} or data["version"] != 1
            or not isinstance(data["paths"], list) or not all(isinstance(name, str) for name in data["paths"])
            or len(set(data["paths"])) != len(data["paths"])):
        raise ValueError("invalid application source manifest")
    files = {}
    for name in data["paths"]:
        relative = safe(name)
        path = root / relative
        if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
            raise ValueError(f"application source must be a regular file: {relative}")
        files[relative] = path.read_bytes()
    problems = oh_public.guard(files, root)
    if problems:
        raise ValueError("public package privacy checks failed:\n" + "\n".join(problems))
    return files


def sdist(public: dict[str, bytes], output: Path, modes: dict[str, int] | None = None) -> Path:
    """A reproducible source archive of exactly the reviewed files plus PKG-INFO.

    Building a wheel from it reads the same export manifest, which it contains.
    """
    table = project(public)
    base = f"{distribution(table)}-{VERSION}"
    members = {safe(name): value for name, value in public.items()}
    members["PKG-INFO"] = metadata(table)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    target = output / f"{base}.tar.gz"
    if target.is_symlink(): raise ValueError("sdist output must not be a symlink")
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed, \
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(members.items()):
            entry = tarfile.TarInfo(f"{base}/{name}")
            entry.size, entry.mtime = len(data), EPOCH
            entry.mode = 0o755 if (modes or {}).get(name, 0) & 0o111 else 0o644
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            archive.addfile(entry, io.BytesIO(data))
    target.write_bytes(raw.getvalue())
    return target


def _modes(public: dict[str, bytes]) -> dict[str, int]:
    import oh_paths
    return {name: (oh_paths.PROGRAM / name).stat().st_mode for name in public}


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    """Both entry points package the exact reviewed, tested application bytes."""
    return wheel(public_files(), Path(wheel_directory))[0].name


def build_sdist(sdist_directory, config_settings=None):
    public = public_files()
    return sdist(public, Path(sdist_directory), _modes(public)).name


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []
