"""Initialize or explicitly upgrade a private site; never activate a service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

import oh_paths
import oh_release

IGNORED = ("/var/", "/build/", "/.scratch/", "/.venv/", "__pycache__/", "*.pyc", "/conf/", "/metrics/", "/deploy/")


def initialize(target: Path, *, program: Path = oh_paths.PROGRAM, upgrade: bool = False) -> dict:
    target, program = Path(target).expanduser().absolute(), Path(program).resolve()
    release = oh_release.manifest(program)
    if target == program or program in target.parents or target in program.parents:
        raise ValueError("private site and installed application must be separate")
    for parent in (target, *target.parents):
        if parent.is_symlink(): raise ValueError("site path must not contain symlinks")
    # Check every mutable destination before caching or creating anything. An
    # owned site's internal var symlink must not redirect an upgrade into the
    # immutable application.
    for relative in ("var/application-releases", oh_paths.SITE_FILE, ".gitignore"):
        path = target / relative
        for parent in (path, *path.parents):
            if parent == target: break
            if parent.is_symlink(): raise ValueError("mutable site paths must not contain symlinks")
        resolved = path.resolve()
        if target.resolve() not in resolved.parents:
            raise ValueError("mutable site paths must stay inside the private site")
    existed = target.exists()
    if existed and (not target.is_dir() or target.stat().st_uid != os.getuid()):
        raise ValueError("site must be an owned directory")
    if upgrade:
        previous = oh_paths.descriptor(target)
        if previous is None: raise ValueError("--upgrade needs an existing thermite site")
    elif existed and any(target.iterdir()):
        raise ValueError("init requires a new or empty directory; existing files are never overwritten")
    else:
        previous = None
    ignore = target / ".gitignore"
    if ignore.exists() and not ignore.is_file():
        raise ValueError("site ignore rules must be a regular file")
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not upgrade: target.chmod(0o700)
    # Preserve exact old releases before changing the site's requirement.
    cached = oh_release.cache(program, target / "var/application-releases")
    descriptor = {"version": 2, "application": release["application"], "release": release["digest"]}
    if not upgrade:
        for name in ("instance/devices.yaml", "instance/host.yaml", "versions.json"):
            out = target / name; out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(program / name, out); out.chmod(0o600)
        (target / "README.md").write_text("# Private thermite configuration\n\nThe initial devices are fictional. Edit instance/ before deployment. Run `thermite check --config .`, then commit instance/, versions.json and thermite-site.json in private Git. Install from any directory with `thermite install --config /path/to/this/site`. The application is installed separately; this directory needs no launcher or Makefile. conf/, metrics/ and deploy/ are ignored build output. Never publish site facts upstream. init starts no service; accounts and TLS require separate commissioning.\n")
    original = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    missing = [line for line in IGNORED if line not in original.splitlines()]
    if missing:
        ignore.write_text(original + ("\n" if original and not original.endswith("\n") else "") + "\n".join(missing) + "\n",
                          encoding="utf-8")
    path = target / oh_paths.SITE_FILE
    handle, temporary = tempfile.mkstemp(prefix=".site-", dir=target)
    try:
        with os.fdopen(handle, "w") as out: json.dump(descriptor, out, indent=2); out.write("\n")
        os.replace(temporary, path)
    finally: Path(temporary).unlink(missing_ok=True)
    return {"site": str(target), "application": release["application"], "release": release["digest"],
            "cached": str(cached), "upgraded": upgrade, "previous": previous, "activated": False}
