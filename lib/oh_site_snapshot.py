"""Render committed private data with its exact, verified application release.

Only data enters the temporary site. The child imports program code from the
cached release in an isolated interpreter; the caller's modules, environment,
working inventory and generated cache never become application authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

import oh_release

GENERATED = ("conf", "metrics", "deploy")
CODE_SUFFIXES = frozenset({".py", ".pyc", ".js", ".sh", ".so", ".class", ".jar", ".whl"})


class SnapshotError(ValueError):
    pass


def render(inputs: dict[str, bytes], release: Path, work: Path, expected: str) -> dict[str, bytes]:
    """Return derived trees without leaving a cache or changing the input site."""
    release, work = Path(release).absolute(), Path(work).absolute()
    oh_release.manifest(release, expected)
    required = {"thermite-site.json", "versions.json", "instance/devices.yaml", "instance/host.yaml"}
    if not required <= inputs.keys():
        raise SnapshotError("committed site lacks required configuration inputs")
    for name in inputs:
        oh_release.safe(name)
        if name not in {"thermite-site.json", "versions.json"} and not name.startswith("instance/"):
            raise SnapshotError("snapshot accepts only committed site data")
        if name.startswith("instance/") and Path(name).suffix.lower() in CODE_SUFFIXES:
            raise SnapshotError("site instance inputs must be data, not application code")
    if not work.is_dir() or any(path.is_symlink() for path in (work, *work.parents)):
        raise SnapshotError("snapshot workspace must be an existing directory without symlinks")
    with tempfile.TemporaryDirectory(prefix=".site-render-", dir=work) as temporary:
        site = Path(temporary)
        for name, data in inputs.items():
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        home, temp = site / ".home", site / ".tmp"
        home.mkdir(mode=0o700)
        temp.mkdir(mode=0o700)
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "HOME": str(home), "TMPDIR": str(temp), "THERMITE_SITE": str(site),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            result = subprocess.run([sys.executable, "-I", "-B", str(release / "bin/thermite"),
                                     "--site", str(site), "generate"],
                                    cwd=release, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SnapshotError("the pinned application could not render the committed site") from exc
        if result.returncode:
            # Config parser errors can include input lines. Keep private data
            # out of installer reports and exception messages: the child's
            # output goes to a file only the owner reads, named instead.
            descriptor, log = tempfile.mkstemp(prefix="site-render-error-", suffix=".log", dir=work)
            with os.fdopen(descriptor, "wb") as file:
                file.write((result.stderr or b"") + (result.stdout or b""))
            raise SnapshotError("the pinned application refused the committed site configuration; "
                                f"its output is in {log}")
        files = {}
        for prefix in GENERATED:
            tree = site / prefix
            if not tree.is_dir() or tree.is_symlink():
                raise SnapshotError("the pinned application omitted a generated tree")
            for path in sorted(tree.rglob("*")):
                if path.is_symlink():
                    raise SnapshotError("generated snapshot contains a symlink")
                if path.is_file():
                    files[path.relative_to(site).as_posix()] = path.read_bytes()
        if not any(name.startswith("conf/") for name in files):
            raise SnapshotError("the pinned application generated no runtime configuration")
        return files
