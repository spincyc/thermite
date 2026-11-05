"""Rootless remaster of integrity-pinned Arch and Ubuntu installer ISOs.

No host disk is opened for writing. The Arch image boots into Thermite's own
installer (iso/installer.py and tui/installer, on a pinned Node.js and
blessed); the Ubuntu image keeps its interactive native installer. Both
carry a reviewed, fictional-site application payload.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.request

import oh_paths
import oh_package


def inputs(base: str = "arch") -> dict:
    if base not in ("arch", "ubuntu"): raise ValueError("base must be arch or ubuntu")
    document = json.loads((oh_paths.PROGRAM / "iso/inputs.json").read_text())
    return document["bases"][base]


#: Arch package and systemd unit names the image may name.
PACKAGE = re.compile(r"[a-z0-9][a-z0-9@._+-]*")
UNIT = re.compile(r"[A-Za-z0-9@._-]+\.(?:service|socket|timer|path)")
FILESYSTEMS = ("btrfs", "ext4")


def packages() -> dict:
    """iso/packages.json, checked: the one tiered list the Arch image installs.

    The build copies it onto the medium, where the live installer renders
    archinstall's package list and its preflight from it, and docs/iso.md and
    the tests read it here.
    """
    document = json.loads((oh_paths.PROGRAM / "iso/packages.json").read_text())
    if document.get("version") != 1 or set(document) != {"version", "tiers", "enable", "keep_disabled"}:
        raise ValueError("iso/packages.json: unsupported document")
    seen = set()
    for tier in document["tiers"]:
        if set(tier) - {"name", "title", "description", "packages", "filesystem"} or not tier.get("packages"):
            raise ValueError(f"iso/packages.json: malformed tier {tier.get('name')!r}")
        if tier.get("filesystem", FILESYSTEMS[0]) not in FILESYSTEMS:
            raise ValueError(f"iso/packages.json: unknown filesystem in tier {tier['name']!r}")
        for name in tier["packages"]:
            if not isinstance(name, str) or not PACKAGE.fullmatch(name) or name in seen:
                raise ValueError(f"iso/packages.json: invalid or repeated package {name!r}")
            seen.add(name)
    units = [*document["enable"], *document["keep_disabled"]]
    if not all(isinstance(unit, str) and UNIT.fullmatch(unit) for unit in units) or len(set(units)) != len(units):
        raise ValueError("iso/packages.json: invalid, repeated or both enabled and disabled units")
    return document


def package_names(document: dict, filesystem: str | None = None) -> list[str]:
    """Every tier's packages in order; a filesystem's tier only with that filesystem."""
    return [name for tier in document["tiers"] if tier.get("filesystem") in (None, filesystem)
            for name in tier["packages"]]


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


def integrity(path: Path, algorithm: str = "sha512") -> str:
    """npm's Subresource Integrity form of a file's digest."""
    with path.open("rb") as stream:
        return algorithm + "-" + base64.b64encode(hashlib.file_digest(stream, algorithm).digest()).decode()


def verified(path: Path, description: dict) -> Path:
    """The file, if it has the pinned digest: a SHA-256, or the npm registry's
    sha512 integrity for a package it publishes no SHA-256 of."""
    path = regular(path)
    if "sha256" in description:
        if sha256(path) != description["sha256"]: raise ValueError(f"SHA256 mismatch: {path}")
    elif not description.get("integrity", "").startswith("sha512-") or integrity(path) != description["integrity"]:
        raise ValueError(f"integrity mismatch: {path}")
    return path


#: What the live medium's installer runs, at the same paths under the
#: medium's thermite directory as in the program, so main.js finds app.js.
INSTALLER = ("iso/installer.py", "tui/installer/controller.js", "tui/installer/core.js", "tui/installer/main.js",
             "tui/installer/view.js", "tui/remote/app.js", "tui/remote/grid.js", "tui/remote/paint.js",
             "tui/remote/styles.js", "tui/remote/theme.js", "tui/remote/torch.js")
#: OpenSSH public key types --authorized-key accepts.
KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
             "ecdsa-sha2-nistp521", "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa")


def public_key(path: Path) -> dict:
    """The one OpenSSH public key in a file, without options, and its
    fingerprint as ssh-keygen -l prints it."""
    lines = [line.strip() for line in regular(path).read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1: raise ValueError("--authorized-key needs a file with exactly one public key")
    fields = lines[0].split(None, 2)
    if len(fields) < 2 or fields[0] not in KEY_TYPES:
        raise ValueError("--authorized-key needs an OpenSSH public key line (ssh-ed25519 AAAA... comment)")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except binascii.Error:
        raise ValueError("--authorized-key: the key is not valid base64") from None
    size = int.from_bytes(blob[:4], "big") if len(blob) >= 4 else 0
    if blob[4:4 + size].decode("ascii", "replace") != fields[0]:
        raise ValueError("--authorized-key: the key's data does not match its type")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return {"type": fields[0], "fingerprint": fingerprint, "comment": fields[2] if len(fields) > 2 else "",
            "line": " ".join(fields[:2]) + (" " + fields[2] if len(fields) > 2 else "")}


def look() -> str:
    """The installer's colors and torch, as thermite tui gets them (THERMITE_REMOTE_LOOK)."""
    import oh_brand

    return json.dumps({**json.loads(oh_brand.as_json(oh_brand.load())), "torch": oh_brand.spec()}) + "\n"


def fetch(cache: Path, *, base: str = "arch") -> dict:
    """Download only the fixed HTTPS inputs; never replace a mismatched cache."""
    cache = Path(cache).expanduser().absolute()
    for parent in (cache, *cache.parents):
        if parent.is_symlink(): raise ValueError("cache path must not contain symlinks")
    cache.mkdir(parents=True, exist_ok=True)
    result = {}
    for key, description in inputs(base).items():
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


#: The first `thermite install` prepares its application environment with
#: `python3 -m venv`, which needs Ubuntu's ensurepip (python3-venv) before the
#: installer's own package step runs. Install it with the OS when the media or
#: mirror provides it; an offline image that lacks it still installs, and
#: FIRST-BOOT.txt gives the one apt command to run first.
UBUNTU_PREREQUISITES = ("DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv git || "
                        "echo 'Thermite: python3-venv or git is unavailable offline; "
                        "see /opt/thermite/FIRST-BOOT.txt before thermite install' >&2")


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
            ["curtin", "in-target", "--target=/target", "--", "sh", "-c", UBUNTU_PREREQUISITES],
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


def arch_grub_config(original: str) -> str:
    """Use Arch's local startup-script hook without changing root discovery."""
    lines, count = [], 0
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith(("linux ", "linux\t")) and "/vmlinuz-linux" in line:
            if "script=" in line: raise ValueError("unexpected preconfigured Arch boot entry")
            line = line.rstrip("\n") + " script=/run/archiso/bootmnt/thermite/arch-install.sh\n"
            count += 1
        if 'menuentry "Arch Linux install medium' in line:
            line = line.replace("Arch Linux install medium", "Thermite installation (Arch Linux)", 1)
        lines.append(line)
    if not count: raise ValueError("base ISO has no recognized Arch live boot entry")
    return "".join(lines)


def arch_boot_config(original: str, kind: str) -> str:
    """Patch native Syslinux/systemd-boot options, retaining Arch's UUID."""
    prefix = {"syslinux": "APPEND ", "systemd": "options "}.get(kind)
    if prefix is None: raise ValueError("unknown Arch boot configuration format")
    lines, count = [], 0
    for line in original.splitlines(keepends=True):
        if line.startswith(prefix):
            if "archisobasedir=arch" not in line or "script=" in line:
                raise ValueError("unexpected Arch live boot options")
            line = line.rstrip("\n") + " script=/run/archiso/bootmnt/thermite/arch-install.sh\n"
            count += 1
        if line.startswith(("MENU LABEL ", "title ")):
            line = line.replace("Arch Linux install medium", "Thermite installation (Arch Linux)", 1)
        lines.append(line)
    if not count: raise ValueError("base ISO has no recognized Arch live boot options")
    return "".join(lines)


def arch_efi_image(image: Path, output: Path) -> None:
    """Copy the pinned ISO's EFI MBR partition without mounts or loop devices."""
    with image.open("rb") as source:
        mbr = source.read(512)
        if len(mbr) != 512 or mbr[510:] != b"\x55\xaa": raise ValueError("invalid base MBR")
        partitions = [(i + 1, *struct.unpack_from("<II", mbr, 446 + 16 * i + 8))
                      for i in range(4) if mbr[446 + 16 * i + 4] == 0xef]
        if len(partitions) != 1 or partitions[0][0] != 2: raise ValueError("unexpected Arch EFI partition layout")
        _, start, sectors = partitions[0]
        if not start or not sectors or (start + sectors) * 512 > image.stat().st_size:
            raise ValueError("invalid Arch EFI partition bounds")
        source.seek(start * 512)
        with output.open("xb") as destination:
            remaining = sectors * 512
            while remaining:
                data = source.read(min(1024 * 1024, remaining))
                if not data: raise ValueError("truncated Arch EFI partition")
                destination.write(data); remaining -= len(data)


def arch_mapping(staging: Path, image: Path, tool: str) -> tuple[list[str], list[str]]:
    copier = executable("mcopy")
    esp = staging / "efi.img"
    arch_efi_image(image, esp)
    mapping = []
    files = {"boot/syslinux/archiso_sys-linux.cfg": "syslinux",
             "loader/entries/01-archiso-linux.conf": "systemd",
             "loader/entries/02-archiso-speech-linux.conf": "systemd",
             "boot/grub/loopback.cfg": "grub"}
    for name, kind in files.items():
        target = staging / name.replace("/", "-")
        subprocess.run([tool, "-osirrox", "on", "-indev", str(image), "-extract", "/" + name,
                        str(target)], check=True, capture_output=True)
        target.chmod(0o644)
        original = target.read_text()
        if kind == "systemd":
            # systemd-boot reads its own FAT partition, not the ISO tree.
            embedded = staging / (target.name + ".embedded")
            subprocess.run([copier, "-i", str(esp), "::/" + name, str(embedded)], check=True, capture_output=True)
            if embedded.read_text() != original: raise ValueError("Arch ISO and EFI boot configurations disagree")
        target.write_text(arch_grub_config(original) if kind == "grub" else arch_boot_config(original, kind))
        mapping.extend(["-map", str(target), "/" + name])
        if kind == "systemd":
            subprocess.run([copier, "-o", "-i", str(esp), str(target), "::/" + name], check=True, capture_output=True)
    # Replay discovers all upstream boot details first. Replacing its copied
    # appended partition afterward updates both USB and El Torito UEFI boots.
    return mapping, ["-append_partition", "2", "0xef", str(esp)]


def stage(media: Path, cache: Path, *, base: str = "arch", authorized_key: dict | None = None) -> dict:
    """Write the medium's thermite directory from the reviewed public files
    and the verified cache; returns its payload.json."""
    pin = inputs(base)
    public = oh_package.public_files()
    wheel, release = oh_package.wheel(public, media)
    payload = {"version": 1, "application": release["application"], "release": release["digest"], "base": base,
               "wheels": {"application": {"name": wheel.name, "sha256": sha256(wheel)}}}
    if "pyyaml" in pin:
        dependency = verified(Path(cache) / pin["pyyaml"]["name"], pin["pyyaml"])
        shutil.copyfile(dependency, media / dependency.name)
        payload["wheels"]["pyyaml"] = {"name": dependency.name, "sha256": pin["pyyaml"]["sha256"]}
    else:
        payload["python_dependency"] = {"provider": "system", "package": "python-yaml", "module": "yaml"}
    for source, name in (("bootstrap.py", "bootstrap.py"), ("first-boot.txt", "FIRST-BOOT.txt")):
        (media / name).write_bytes(public["iso/" + source])
    if base == "arch":
        for name in ("arch-install.sh", "arch-plugin.py", "packages.json"):
            (media / name).write_bytes(public["iso/" + name])
        # The installer and what it runs on: Node.js and blessed as pinned,
        # unpacked on the live system only after their digests match again.
        files = {}
        for name in INSTALLER:
            staged = media / ("installer.py" if name == "iso/installer.py" else name)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(public[name])
            files[str(staged.relative_to(media))] = sha256(staged)
        (media / "look.json").write_text(look())
        files["look.json"] = sha256(media / "look.json")
        runtime = {}
        for key in ("node", "blessed"):
            archive = verified(Path(cache) / pin[key]["name"], pin[key])
            shutil.copyfile(archive, media / archive.name)
            runtime[key] = {name: pin[key][name] for name in ("name", "sha256", "integrity") if name in pin[key]}
        payload["installer"] = {**runtime, "files": files}
        if authorized_key:
            (media / "authorized_keys").write_text(authorized_key["line"] + "\n")
            payload["authorized_key"] = {name: authorized_key[name] for name in ("type", "fingerprint", "comment")}
    elif authorized_key:
        raise ValueError("--authorized-key needs the Arch image; Ubuntu's installer asks for keys itself")
    (media / "payload.json").write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def build(cache: Path, output: Path, work: Path, *, xorriso: str | None = None, base: str = "arch",
          authorized_key: Path | None = None) -> dict:
    """Build a real hybrid ISO using the base's BIOS/UEFI boot image replay.

    The caller chooses a disposable work parent (normally .scratch/iso).
    A completed image has a SHA256 sidecar and a provenance receipt.
    authorized_key: a public key file; the live system then accepts SSH
    with that key alone, into the installer, and the owner gets it.
    """
    pin = inputs(base)
    image = verified(Path(cache) / pin["base"]["name"], pin["base"])
    for key in pin:
        verified(Path(cache) / pin[key]["name"], pin[key])
    key = public_key(authorized_key) if authorized_key else None
    output = new_path(output)
    checksum, receipt_path = new_path(Path(str(output) + ".sha256")), new_path(Path(str(output) + ".json"))
    tool = str(regular(Path(xorriso))) if xorriso else executable("xorriso")
    work = Path(work).expanduser().absolute()
    for parent in (work, *work.parents):
        if parent.is_symlink(): raise ValueError("work path must not contain symlinks")
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="remaster-", dir=work) as temporary:
        staging = Path(temporary)
        media = staging / "thermite"; media.mkdir()
        # Build only the privacy-reviewed public payload, never copy a
        # checkout or selected private site directly into installation media.
        payload = stage(media, cache, base=base, authorized_key=key)
        if base == "arch":
            mapping, appended = arch_mapping(staging, image, tool)
        else:
            mapping = ubuntu_mapping(staging, media, image, tool)
            appended = []
        # Native xorriso manipulation preserves El Torito and appended EFI
        # partition details; a generic mkisofs filesystem rebuild loses them.
        partial = staging / "thermite.iso"
        subprocess.run([tool, "-indev", str(image), "-outdev", str(partial),
                        "-map", str(media), "/thermite", *mapping,
                        "-boot_image", "any", "replay", *appended, "-commit", "-end"], check=True)
        # Publish without overwriting a concurrently created destination.
        with partial.open("rb") as source, output.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    installation = ("Thermite's installer: checked answers, the disk's name typed back before it is erased"
                    if base == "arch" else "interactive storage and identity; no unattended disk selection")
    result = {"version": 1, "image": output.name, "sha256": sha256(output), "distribution": base,
              "inputs": {name: {field: value for field, value in description.items() if field != "url"}
                         for name, description in pin.items()},
              "base": pin["base"], "payload": payload, "boot": "upstream BIOS and UEFI images replayed",
              "installation": installation,
              "ssh": (f"live installer and owner accept {key['fingerprint']} only" if key else "no key")}
    with checksum.open("x") as out: out.write(result["sha256"] + "  " + output.name + "\n")
    with receipt_path.open("x") as out: json.dump(result, out, indent=2); out.write("\n")
    return result


def ubuntu_mapping(staging: Path, media: Path, image: Path, tool: str) -> list[str]:
    """Retain Ubuntu's offline installer and refresh its media checksums."""
    seed = staging / "nocloud"; seed.mkdir()
    (seed / "user-data").write_bytes(configuration())
    (seed / "meta-data").write_text("instance-id: thermite-installer\n")
    mapping, changed = [], {}
    for name in ("grub.cfg", "loopback.cfg"):
        target = staging / name
        subprocess.run([tool, "-osirrox", "on", "-indev", str(image), "-extract", "/boot/grub/" + name,
                        str(target)], check=True, capture_output=True)
        target.chmod(0o644)
        target.write_text(grub_config(target.read_text()))
        mapping.extend(["-map", str(target), "/boot/grub/" + name])
        changed["boot/grub/" + name] = target.read_bytes()
    # Ubuntu's initial media check uses this list. Refresh every replaced
    # file and include the new payload rather than retain stale hashes.
    md5sums = staging / "md5sum.txt"
    subprocess.run([tool, "-osirrox", "on", "-indev", str(image), "-extract", "/md5sum.txt",
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
    return ["-map", str(seed), "/nocloud", *mapping]
