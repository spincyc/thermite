"""Initialize or explicitly upgrade a private site; never activate a service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import oh_paths
import oh_release

WRAPPER = '''
"""This private site's installed thermite application."""
import os
import sys
from pathlib import Path
sys.dont_write_bytecode = True
os.environ["THERMITE_SITE"] = str(Path(__file__).resolve().parents[1])
from thermite_app import main
raise SystemExit(main())
'''
MAKEFILE = '''PYTHON ?= python3
.PHONY: help check openhab-generate openhab-test openhab-deploy
help:
\t@bin/thermite --help
check:
\t@bin/thermite generate --check
\t@bin/thermite validate
openhab-generate:
\t@bin/thermite generate
openhab-test:
\t@bin/thermite test $(TEST)
openhab-deploy:
\t@bin/thermite deploy
'''


def initialize(target: Path, *, program: Path = oh_paths.PROGRAM, upgrade: bool = False) -> dict:
    target, program = Path(target).expanduser().absolute(), Path(program).resolve()
    release = oh_release.manifest(program)
    if target == program or program in target.parents or target in program.parents:
        raise ValueError("private site and installed application must be separate")
    for parent in (target, *target.parents):
        if parent.is_symlink(): raise ValueError("site path must not contain symlinks")
    # Check every mutable destination before caching or creating anything. An
    # owned site's internal var/bin symlink must not redirect an upgrade into
    # the immutable application or another site's launcher.
    for relative in ("bin/thermite", "var/application-releases", oh_paths.SITE_FILE):
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
        launcher = target / "bin/thermite"
        if launcher.is_symlink() or not launcher.is_file() or launcher.read_text().partition("\n")[2] != WRAPPER:
            raise ValueError("site launcher was customized; review it before upgrading")
    elif existed and any(target.iterdir()):
        raise ValueError("init requires a new or empty directory; existing files are never overwritten")
    else:
        previous = None
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not upgrade: target.chmod(0o700)
    # Preserve exact old releases before changing the site's requirement.
    cached = oh_release.cache(program, target / "var/application-releases")
    descriptor = {"version": 1, "application": release["application"], "release": release["digest"]}
    if not upgrade:
        for name in ("instance/devices.yaml", "instance/host.yaml", "versions.json"):
            out = target / name; out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(program / name, out); out.chmod(0o600)
        (target / "bin").mkdir()
        (target / "Makefile").write_text(MAKEFILE)
        (target / ".gitignore").write_text("/var/\n/build/\n/.scratch/\n/.venv/\n__pycache__/\n*.pyc\n")
        (target / "README.md").write_text("# Private thermite site\n\nThe initial devices are fictional. Edit instance/ before deployment. Keep this site and its generated conf/, metrics/ and deploy/ committed only in private Git. Never publish these files upstream. init starts no service; accounts and TLS require separate explicit commissioning.\n")
    (target / "bin/thermite").write_text("#!" + sys.executable + "\n" + WRAPPER)
    (target / "bin/thermite").chmod(0o700)
    path = target / oh_paths.SITE_FILE
    handle, temporary = tempfile.mkstemp(prefix=".site-", dir=target)
    try:
        with os.fdopen(handle, "w") as out: json.dump(descriptor, out, indent=2); out.write("\n")
        os.replace(temporary, path)
    finally: Path(temporary).unlink(missing_ok=True)
    return {"site": str(target), "application": release["application"], "release": release["digest"],
            "cached": str(cached), "upgraded": upgrade, "previous": previous, "activated": False}
