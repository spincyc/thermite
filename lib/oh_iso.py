"""Rootless remaster of an integrity-pinned Ubuntu Server installer ISO.

No host disk is opened for writing. The image carries an interactive native
OS installer and a reviewed, fictional-site application payload.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

import oh_paths
import oh_package


def inputs() -> dict:
    return json.loads((oh_paths.PROGRAM / "iso/inputs.json").read_text())


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file(): raise ValueError(f"expected a regular file: {path}")
    return path


def new_path(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink(): raise ValueError(f"output already exists: {path}")
    for parent in path.parents:
        if parent.is_symlink(): raise ValueError("output path must not contain symlinks")
    return path


def verified(path: Path, description: dict) -> Path:
    path = regular(path)
    if sha256(path) != description["sha256"]:
        raise ValueError(f"SHA256 mismatch: {path}")
    return path


def fetch(cache: Path) -> dict:
    """Download only the fixed HTTPS inputs; never replace a mismatched cache."""
    cache = Path(cache).expanduser().absolute()
    for parent in (cache, *cache.parents):
        if parent.is_symlink(): raise ValueError("cache path must not contain symlinks")
    cache.mkdir(parents=True, exist_ok=True)
    result = {}
    for key in ("base", "pyyaml"):
        description = inputs()[key]
        target = cache / description["name"]
        if target.exists() or target.is_symlink():
            verified(target, description)
        else:
            if not description["url"].startswith("https://"): raise ValueError("ISO inputs require HTTPS")
            handle, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
            try:
                with os.fdopen(handle, "wb") as out, urllib.request.urlopen(description["url"], timeout=60) as response:
                    if not response.url.startswith("https://"): raise ValueError("download redirected away from HTTPS")
                    shutil.copyfileobj(response, out)
                verified(Path(temporary), description)
                # A concurrently created path must never be replaced.
                os.link(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        result[key] = str(target)
    return result


def configuration() -> bytes:
    """All ordinary screens remain interactive, especially storage/identity.

    Omitting the autoinstall kernel flag also retains Subiquity's explicit
    autoinstall confirmation. Never add a storage plan or default password.
    """
    import yaml
    value = {"autoinstall": {
        "version": 1,
        "interactive-sections": ["*"],
        "refresh-installer": {"update": False},
        "apt": {"geoip": False, "fallback": "offline-install"},
        "late-commands": [
            ["mkdir", "-p", "/target/opt/thermite-media"],
            ["cp", "-a", "/cdrom/thermite/.", "/target/opt/thermite-media/"],
            ["curtin", "in-target", "--target=/target", "--", "python3", "/opt/thermite-media/bootstrap.py"],
        ],
    }}
    return ("#cloud-config\n" + yaml.safe_dump(value, sort_keys=False)).encode()


def grub_config(original: str) -> str:
    """Add local seed discovery without bypassing disk confirmation."""
    lines, count = [], 0
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith(("linux ", "linux\t")) and "/casper/vmlinuz" in line:
            if "autoinstall" in line or "ds=" in line: raise ValueError("unexpected preconfigured base boot entry")
            suffix = " ds=nocloud\\;s=/cdrom/nocloud/"
            if "---" in line: line = line.replace("---", suffix + " ---", 1)
            else: line = line.rstrip("\n") + suffix + "\n"
            count += 1
        lines.append(line)
    if not count: raise ValueError("base ISO has no recognized Ubuntu live boot entry")
    return "".join(lines)


def executable(name: str) -> str:
    found = shutil.which(name)
    fallback = Path.home() / ".local/bin" / name
    if found: return found
    if fallback.is_file() and os.access(fallback, os.X_OK): return str(fallback)
    raise ValueError(f"{name} is required; install it before building an ISO")


def build(cache: Path, output: Path, work: Path, *, xorriso: str | None = None) -> dict:
    """Build a real hybrid ISO using the base's BIOS/UEFI boot image replay.

    The caller chooses a disposable work parent (normally .scratch/iso).
    A completed image has a SHA256 sidecar and a provenance receipt.
    """
    pin = inputs()
    base = verified(Path(cache) / pin["base"]["name"], pin["base"])
    dependency = verified(Path(cache) / pin["pyyaml"]["name"], pin["pyyaml"])
    output = new_path(output)
    checksum, receipt_path = new_path(Path(str(output) + ".sha256")), new_path(Path(str(output) + ".json"))
    tool = str(regular(Path(xorriso))) if xorriso else executable("xorriso")
    work = Path(work).expanduser().absolute()
    for parent in (work, *work.parents):
        if parent.is_symlink(): raise ValueError("work path must not contain symlinks")
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build only the privacy-reviewed public payload, never copy a checkout
    # or selected private site directly into installation media.
    public = oh_package.public_files()
    with tempfile.TemporaryDirectory(prefix="remaster-", dir=work) as temporary:
        staging = Path(temporary)
        media = staging / "thermite"; media.mkdir()
        wheel, release = oh_package.wheel(public, media)
        shutil.copyfile(dependency, media / dependency.name)
        for source, name in (("bootstrap.py", "bootstrap.py"), ("first-boot.txt", "FIRST-BOOT.txt")):
            shutil.copyfile(oh_paths.PROGRAM / "iso" / source, media / name)
        payload = {"version": 1, "application": release["application"], "release": release["digest"],
                   "wheels": {"application": {"name": wheel.name, "sha256": sha256(wheel)},
                              "pyyaml": {"name": dependency.name, "sha256": pin["pyyaml"]["sha256"]}}}
        (media / "payload.json").write_text(json.dumps(payload, indent=2) + "\n")
        seed = staging / "nocloud"; seed.mkdir()
        (seed / "user-data").write_bytes(configuration())
        (seed / "meta-data").write_text("instance-id: thermite-installer\n")
        mapping, changed = [], {}
        for name in ("grub.cfg", "loopback.cfg"):
            target = staging / name
            subprocess.run([tool, "-osirrox", "on", "-indev", str(base), "-extract", "/boot/grub/" + name,
                            str(target)], check=True, capture_output=True)
            target.chmod(0o644)
            target.write_text(grub_config(target.read_text()))
            mapping.extend(["-map", str(target), "/boot/grub/" + name])
            changed["boot/grub/" + name] = target.read_bytes()
        # Ubuntu's initial media check uses this list. Refresh every replaced
        # file and include the new payload rather than retain stale hashes.
        md5sums = staging / "md5sum.txt"
        subprocess.run([tool, "-osirrox", "on", "-indev", str(base), "-extract", "/md5sum.txt",
                        str(md5sums)], check=True, capture_output=True)
        md5sums.chmod(0o644)
        lines = []
        for line in md5sums.read_text().splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2: raise ValueError("malformed base media checksum list")
            name = parts[1].lstrip("*").removeprefix("./")
            if name not in changed: lines.append(line)
        for directory in (media, seed):
            for file in sorted(directory.iterdir()):
                changed[directory.name + "/" + file.name] = file.read_bytes()
        for name, data in sorted(changed.items()):
            lines.append(hashlib.md5(data, usedforsecurity=False).hexdigest() + "  ./" + name)
        md5sums.write_text("\n".join(lines) + "\n")
        mapping.extend(["-map", str(md5sums), "/md5sum.txt"])
        # Native xorriso manipulation preserves El Torito and appended EFI
        # partition details; a generic mkisofs filesystem rebuild loses them.
        partial = staging / "thermite.iso"
        subprocess.run([tool, "-indev", str(base), "-outdev", str(partial),
                        "-map", str(media), "/thermite", "-map", str(seed), "/nocloud", *mapping,
                        "-boot_image", "any", "replay", "-commit", "-end"], check=True)
        # Publish without overwriting any concurrently created destination.
        # Copy via an exclusive regular file also works across filesystems.
        # A failure can leave a partial image for diagnosis. Its missing
        # receipt marks it incomplete and a rerun refuses to overwrite it.
        with partial.open("rb") as source, output.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    result = {"version": 1, "image": output.name, "sha256": sha256(output), "base": pin["base"],
              "payload": payload, "boot": "upstream BIOS and UEFI images replayed",
              "installation": "interactive storage and identity; no unattended disk selection"}
    with checksum.open("x") as out: out.write(result["sha256"] + "  " + output.name + "\n")
    with receipt_path.open("x") as out: json.dump(result, out, indent=2); out.write("\n")
    return result
