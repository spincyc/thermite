"""Install bundled wheels offline after the native OS installation.

Run inside the new target with arch-chroot or curtin in-target. No partitioning, service
activation, accounts, credentials, site inventory or network operations occur.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import venv
import zipfile


#: Sourced by every login shell (POSIX sh). `thermite install` switches
#: ~/.local/bin/thermite to the application it verified; putting that
#: directory first lets it win over this image's frozen /usr/local/bin
#: launcher. Prepending only when it is not already first keeps re-sourcing
#: idempotent.
PROFILE = """\
if [ -n "${HOME:-}" ]; then
    case ":${PATH:-}:" in
        ":$HOME/.local/bin:"*) ;;
        *) PATH="$HOME/.local/bin${PATH:+:$PATH}"; export PATH ;;
    esac
fi
case "$-" in *i*) cat /opt/thermite/FIRST-BOOT.txt ;; esac
"""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not path.parts:
        raise ValueError("unsafe wheel member")
    return str(path)


def unpack(wheel: Path, library: Path) -> None:
    """Our two wheel inputs contain only ordinary purelib payloads."""
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.infolist():
            name = safe_name(entry.filename)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or ".data" in PurePosixPath(name).parts:
                raise ValueError("unsupported wheel member")
            target = library / name
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                if target.exists(): raise ValueError("wheel payloads overlap")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
                target.chmod(0o644)


def install(media: Path, *, root: Path = Path("/")) -> dict:
    """The root override supports an offline synthetic filesystem test."""
    media, root = Path(media), Path(root)
    receipt = json.loads((media / "payload.json").read_text())
    system_yaml = receipt.get("python_dependency") == {"provider": "system", "package": "python-yaml", "module": "yaml"}
    expected = {"application"} if system_yaml else {"application", "pyyaml"}
    if receipt.get("version") != 1 or set(receipt["wheels"]) != expected:
        raise ValueError("invalid ISO payload manifest")
    wheels = []
    for description in receipt["wheels"].values():
        name = safe_name(description["name"])
        if "/" in name: raise ValueError("wheel name must be a basename")
        path = media / name
        if path.is_symlink() or digest(path) != description["sha256"]:
            raise ValueError("ISO wheel checksum mismatch")
        wheels.append(path)
    destination = root / "opt/thermite"
    launcher = root / "usr/local/bin/thermite"
    notice = root / "etc/update-motd.d/90-thermite"
    for path in (destination, launcher, notice):
        for parent in (path, *path.parents):
            if parent.is_symlink(): raise ValueError("installation path contains a symlink")
            if parent == root: break
        if path.exists(): raise ValueError(f"installation destination exists: {path}")
    destination.mkdir(parents=True)
    environment = destination / "venv"
    # Arch's signed python-yaml package follows its system Python ABI.
    # Ubuntu instead carries the ABI-pinned dependency wheel on the image.
    # Neither route uses pip, ensurepip or network during this hook.
    venv.EnvBuilder(with_pip=False, system_site_packages=system_yaml).create(environment)
    interpreter = environment / "bin/python3"
    libraries = list(environment.glob("lib/python*/site-packages"))
    if len(libraries) != 1: raise ValueError("unexpected Python environment layout")
    for wheel in wheels: unpack(wheel, libraries[0])
    if system_yaml:
        subprocess.run([str(interpreter), "-I", "-B", "-c",
                        "import yaml; v=tuple(map(int,yaml.__version__.split('.')[:2])); "
                        "assert (6,0) <= v < (7,0), 'Thermite requires PyYAML>=6,<7'"], check=True)
    entry = environment / "bin/thermite"
    entry.write_text(f"#!{interpreter}\nimport sys\nfrom thermite_app import main\nraise SystemExit(main())\n")
    entry.chmod(0o755)
    subprocess.run([str(interpreter), "-I", "-B", str(entry), "--version"], check=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(entry)
    guide = media / "FIRST-BOOT.txt"
    shutil.copyfile(guide, destination / "FIRST-BOOT.txt")
    notice.parent.mkdir(parents=True, exist_ok=True)
    notice.write_text("#!/bin/sh\ncat /opt/thermite/FIRST-BOOT.txt\n")
    notice.chmod(0o755)
    # Arch does not use Ubuntu's dynamic MOTD directory, and its default PATH
    # lacks ~/.local/bin, which Ubuntu's default ~/.profile puts first.
    if system_yaml:
        profile = root / "etc/profile.d/thermite.sh"
        if profile.exists() or profile.is_symlink(): raise ValueError("Thermite login notice already exists")
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(PROFILE)
        profile.chmod(0o644)
    (destination / "installation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("Thermite application installed. Follow /opt/thermite/FIRST-BOOT.txt after reboot.")
    return receipt


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("ISO bootstrap must run inside the installed target as root")
    if sys.version_info < (3, 11):
        raise SystemExit("Thermite requires Python 3.11 or newer")
    payload = json.loads((Path(__file__).resolve().parent / "payload.json").read_text())
    if "pyyaml" in payload["wheels"] and sys.version_info[:2] != (3, 12):
        raise SystemExit("the Ubuntu ISO dependency wheel requires Python 3.12")
    install(Path(__file__).resolve().parent)
