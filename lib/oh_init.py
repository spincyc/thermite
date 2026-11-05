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
#: --empty's house: no rooms or devices, in the sample's JSON-as-YAML form.
EMPTY_HOUSE = '{\n  "version": 1,\n  "rooms": {},\n  "devices": []\n}\n'
README = ("# Private thermite configuration\n\n{house} Run `thermite check --config .`, then commit instance/, "
          "versions.json and thermite-site.json in private Git. Install from any directory with `thermite install "
          "--config /path/to/this/site`. The application is installed separately; this directory needs no launcher or "
          "Makefile. conf/, metrics/ and deploy/ are ignored build output. Never publish site facts upstream. init "
          "starts no service. `thermite install` deploys the native services and the HTTPS gateway, with its private "
          "certificate authority, in one approved run; household authentication starts off until you enable it on the "
          "gateway's Security page.\n")


def initialize(target: Path, *, program: Path = oh_paths.PROGRAM, upgrade: bool = False, empty: bool = False) -> dict:
    """empty writes a house with no rooms or devices instead of the fictional example."""
    if upgrade and empty: raise ValueError("--empty creates a new site; --upgrade keeps the existing house")
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
        if empty: (target / "instance/devices.yaml").write_text(EMPTY_HOUSE)
        (target / "README.md").write_text(README.format(
            house="The house is empty. Add rooms and devices to instance/devices.yaml." if empty else
            "The initial devices are fictional. Edit instance/ before deployment."))
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
            "cached": str(cached), "upgraded": upgrade, "previous": previous, "empty": empty, "activated": False}
