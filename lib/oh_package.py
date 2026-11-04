"""Build a wheel from the reviewed public artifact, never a private tree walk.

Also a minimal PEP517 backend for the public export. No installer, native
runtime, daemon or network operation is invoked by this module.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
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


def wheel(public: dict[str, bytes], output: Path) -> tuple[Path, dict]:
    files = runtime_files(public)
    release = json.loads(files[FILE])
    info = f"thermite_app-{VERSION}.dist-info"
    entries = {"thermite_app/__init__.py": BOOTSTRAP.encode(),
               **{"thermite_app/runtime/" + name: value for name, value in files.items()},
               info + "/METADATA": ("Metadata-Version: 2.3\nName: thermite-app\nVersion: " + VERSION
                   + "\nSummary: Thermite house configuration and control application\nRequires-Python: >=3.11\n"
                   "Requires-Dist: PyYAML>=6,<7\nLicense: All rights reserved\n\n").encode(),
               info + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: thermite\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
               info + "/entry_points.txt": b"[console_scripts]\nthermite = thermite_app:main\n"}
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(entries.items()):
        token = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + token, len(data)])
    writer.writerow([info + "/RECORD", "", ""])
    entries[info + "/RECORD"] = record.getvalue().encode()
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    target = output / f"thermite_app-{VERSION}-py3-none-any.whl"
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


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    """Both entry points package the exact reviewed, tested application bytes."""
    return wheel(public_files(), Path(wheel_directory))[0].name


def get_requires_for_build_wheel(config_settings=None):
    return []
