"""Archinstall 4.5 finalization hook: runs after OS, before the Reboot menu.

The native installer alone owns disk selection, layout and wipe confirmation.
The supported on_genfstab hook provides its actual mounted target, avoiding
any guessed device or a payload transfer that happens too late after reboot.
"""
from pathlib import Path
import shutil
import subprocess

__archinstall__version__ = 4.5


def finalize(target: Path, media: Path) -> None:
    target = Path(target)
    if target == Path("/") or target.is_symlink() or not target.is_mount():
        raise ValueError("Thermite finalization requires the native installer's mounted target")
    if "ID=arch" not in (target / "etc/os-release").read_text().splitlines():
        raise ValueError("Thermite finalization requires an installed Arch system")
    # A root-only or locked installation must not be reported as ready.
    passwords = {line.split(":", 1)[0]: line.split(":")[1]
                 for line in (target / "etc/shadow").read_text().splitlines()}
    wheel = next((line.split(":")[3].split(",") for line in
                  (target / "etc/group").read_text().splitlines() if line.startswith("wheel:")), [])
    owners = []
    for line in (target / "etc/passwd").read_text().splitlines():
        name, _, uid, _, _, home, shell = line.split(":")
        if (name != "thermite" and 1000 <= int(uid) < 60000 and name in wheel
                and shell and Path(shell).name not in ("nologin", "false")
                and home.startswith("/home/") and passwords.get(name, "").startswith("$")):
            owners.append(name)
    if not owners: raise ValueError("Create a normal owner user with a password and sudo access in archinstall")
    destination = target / "opt/thermite-media"
    for path in (target / "opt", destination):
        if path.is_symlink(): raise ValueError("Thermite payload destination contains a symlink")
    if destination.exists(): raise ValueError("Thermite payload destination already exists")
    shutil.copytree(media, destination, symlinks=False)
    subprocess.run(["arch-chroot", str(target), "/usr/bin/python3", "/opt/thermite-media/bootstrap.py"], check=True)
    print("Thermite application ready. Reboot, log in as your owner, and follow /opt/thermite/FIRST-BOOT.txt.")


class Plugin:
    def on_genfstab(self, installation):
        finalize(installation.target, Path(__file__).resolve().parent)
        return False
