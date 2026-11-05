"""Archinstall 4.5 plugin: Thermite's part of the installed system.

archinstall calls on_genfstab last, with the target still mounted: after the
base system, boot loader, owner, packages, services and time sync. This
installs the application from the medium, configures SSH, sudo, mDNS, the
journal and the terminal, writes the installation's records, and enables
the first boot (first-boot.py). Every step prints a "Thermite:" line, which
the installer's progress follows; any failure fails the installation.

The installer core passes its state directory in THERMITE_INSTALLER_STATE:
answers.json (no secrets) and, with Wi-Fi, wifi.json.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

__archinstall__version__ = 4.5

LOG = "var/log/thermite-install"
#: What bootstrap.py reads from /opt/thermite-media; the installer's own
#: runtime (Node.js, blessed, tui/) stays on the medium.
BOOTSTRAP = ("payload.json", "bootstrap.py", "FIRST-BOOT.txt")
#: Root never logs in over SSH; with the image's key, nobody uses a password.
SSHD = "# Written by the Thermite installer.\nPermitRootLogin no\n"
SSHD_KEY_ONLY = "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
SUDOERS = "# Written by the Thermite installer: the owner's sudo.\n%wheel ALL=(ALL:ALL) ALL\n"
JOURNALD = "# Written by the Thermite installer.\n[Journal]\nSystemMaxUse=2G\n"
#: A terminal this host has no terminfo for (a new emulator over SSH)
#: still gets a usable one.
TERM_FALLBACK = """\
# Written by the Thermite installer: a terminal without terminfo here gets xterm-256color.
if [ -n "${TERM:-}" ] && command -v infocmp >/dev/null 2>&1 && ! infocmp "$TERM" >/dev/null 2>&1; then
    TERM=xterm-256color
    export TERM
fi
"""


def say(text: str) -> None:
    print("Thermite: " + text, flush=True)


def chroot(target: Path, *argv: str) -> None:
    subprocess.run(["arch-chroot", str(target), *argv], check=True)


def own(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def owner(target: Path, name: str) -> dict:
    """The owner archinstall created: a normal user in wheel with a password."""
    passwords = {line.split(":", 1)[0]: line.split(":")[1]
                 for line in (target / "etc/shadow").read_text().splitlines()}
    wheel = next((line.split(":")[3].split(",") for line in
                  (target / "etc/group").read_text().splitlines() if line.startswith("wheel:")), [])
    for line in (target / "etc/passwd").read_text().splitlines():
        login, _, uid, gid, _, home, shell = line.split(":")
        if login != name: continue
        if (name == "thermite" or not 1000 <= int(uid) < 60000 or name not in wheel
                or Path(shell).name in ("nologin", "false") or not home.startswith("/home/")
                or not passwords.get(name, "").startswith("$")):
            break
        return {"name": name, "uid": int(uid), "gid": int(gid), "home": home}
    raise ValueError(f"the owner {name} was not created as a normal user in wheel with a password")


def hosts_line(line: str) -> str:
    """nsswitch's hosts with mDNS (.local) before systemd-resolved."""
    if "mdns" in line: return line
    words = line.split()
    at = next((index for index, word in enumerate(words) if word in ("resolve", "files", "dns")), len(words))
    return " ".join([*words[:at], "mdns_minimal", "[NOTFOUND=return]", *words[at:]])


def keyfile_value(value: str) -> str:
    """A string as NetworkManager's keyfile (GKeyFile) reads it back:
    backslashes escaped, and the spaces at either end, which it trims."""
    return re.sub(r"^ +| +$", lambda spaces: "\\s" * len(spaces[0]), value.replace("\\", "\\\\"))


def keyfile_ssid(ssid: str) -> str:
    """ssid= as NetworkManager reads it: a plain name as itself, any other
    as its bytes, keyfile's integer-list form, which nothing can misread."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*", ssid) and re.search("[A-Za-z]", ssid): return ssid
    return "".join(f"{byte};" for byte in ssid.encode())


def wifi_profile(wifi: dict) -> str:
    """The installed host's NetworkManager profile for the network the
    installer joined: WPA2 (wpa-psk) or WPA3 (sae) as iwd negotiated it,
    or open."""
    security = ""
    if wifi["security"] in ("psk", "sae"):
        key = wifi.get("passphrase") or wifi.get("key")
        security = f"\n[wifi-security]\nkey-mgmt={'sae' if wifi['security'] == 'sae' else 'wpa-psk'}\npsk={keyfile_value(key)}\n"
    return (f"[connection]\nid={keyfile_value(wifi['ssid'])}\ntype=wifi\nautoconnect=true\n\n"
            f"[wifi]\nmode=infrastructure\nssid={keyfile_ssid(wifi['ssid'])}\n"
            + ("hidden=true\n" if wifi.get("hidden") else "")
            + f"{security}\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n")


def configure(target: Path, media: Path, answers: dict, account: dict, wifi: dict | None) -> list[str]:
    """The installed system's settings; the units it kept from being enabled."""
    say("sudo for wheel, SSH without root logins")
    write(target / "etc/sudoers.d/10-thermite-wheel", SUDOERS, 0o440)
    chroot(target, "visudo", "-cf", "/etc/sudoers.d/10-thermite-wheel")
    key = media / "authorized_keys"
    write(target / "etc/ssh/sshd_config.d/20-thermite.conf", SSHD + (SSHD_KEY_ONLY if key.is_file() else ""))
    config = target / "etc/ssh/sshd_config"
    if "Include /etc/ssh/sshd_config.d/*.conf" not in config.read_text().splitlines():
        config.write_text("Include /etc/ssh/sshd_config.d/*.conf\n" + config.read_text())
    if key.is_file():
        home = target / account["home"].lstrip("/")
        directory = home / ".ssh"
        directory.mkdir(mode=0o700, exist_ok=True)
        write(directory / "authorized_keys", key.read_text(), 0o600)
        for path in (directory, directory / "authorized_keys"): own(path, account["uid"], account["gid"])
    say("mDNS names, the journal's size, the terminal")
    nsswitch = target / "etc/nsswitch.conf"
    nsswitch.write_text("".join(hosts_line(line.rstrip("\n")) + "\n" if line.startswith("hosts:") else line
                                for line in nsswitch.read_text().splitlines(keepends=True)))
    write(target / "etc/systemd/journald.conf.d/50-thermite.conf", JOURNALD)
    write(target / "etc/profile.d/thermite-term.sh", TERM_FALLBACK)
    # GRUB (BIOS) boots the newest kernel unless told; systemd-boot boots
    # the first of archinstall's kernels, the chosen one.
    grub = target / "etc/default/grub"
    if grub.is_file():
        lines = [line for line in grub.read_text().splitlines() if not line.startswith("GRUB_TOP_LEVEL=")]
        grub.write_text("\n".join([*lines, f'GRUB_TOP_LEVEL="/boot/vmlinuz-{answers["kernel"]}"']) + "\n")
        chroot(target, "grub-mkconfig", "-o", "/boot/grub/grub.cfg")
    if wifi:
        say(f"Wi-Fi {wifi['ssid']}")
        name = "".join(ch if ch.isascii() and ch.isalnum() or ch in "-_" else "_" for ch in wifi["ssid"])
        write(target / f"etc/NetworkManager/system-connections/{name}.nmconnection", wifi_profile(wifi), 0o600)
    # Units packages.json keeps disabled stay so, whatever a package did.
    document = json.loads((media / "packages.json").read_text())
    stopped = []
    for link in sorted((target / "etc/systemd/system").glob("*.wants/*")):
        if link.name in document["keep_disabled"] and link.is_symlink():
            link.unlink(); stopped.append(link.name)
    return stopped


def first_boot(media: Path):
    spec = importlib.util.spec_from_file_location("thermite_first_boot", media / "first-boot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finalize(target: Path, media: Path, state: Path) -> None:
    target, media, state = Path(target), Path(media), Path(state)
    if target == Path("/") or target.is_symlink() or not target.is_mount():
        raise ValueError("Thermite finalization requires the native installer's mounted target")
    if "ID=arch" not in (target / "etc/os-release").read_text().splitlines():
        raise ValueError("Thermite finalization requires an installed Arch system")
    answers = json.loads((state / "answers.json").read_text())
    account = owner(target, answers["owner"])
    say("installing the application")
    destination = target / "opt/thermite-media"
    for path in (target / "opt", destination):
        if path.is_symlink(): raise ValueError("Thermite payload destination contains a symlink")
    if destination.exists(): raise ValueError("Thermite payload destination already exists")
    destination.mkdir(parents=True)
    payload = json.loads((media / "payload.json").read_text())
    for name in (*BOOTSTRAP, *(wheel["name"] for wheel in payload["wheels"].values())):
        shutil.copyfile(media / name, destination / name)
    chroot(target, "/usr/bin/python3", "/opt/thermite-media/bootstrap.py")
    wifi_file = state / "wifi.json"
    stopped = configure(target, media, answers, account, json.loads(wifi_file.read_text()) if wifi_file.is_file() else None)
    say("the installation's records and the first boot")
    records = target / LOG
    records.mkdir(parents=True, exist_ok=True)
    receipt = {"version": 1, "installed": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "answers": answers,
               "application": payload["application"], "release": payload["release"],
               "installer": payload.get("installer", {}), "authorized_key": payload.get("authorized_key"),
               "kept_disabled": stopped,
               "logs": {"installer": f"/{LOG}/installer.log", "archinstall": "/var/log/archinstall/install.log",
                        "first_boot": f"/{LOG}/first-boot.log"}}
    write(records / "receipt.json", json.dumps(receipt, indent=2) + "\n")
    write(records / "answers.json", json.dumps(answers, indent=2) + "\n")
    first_boot(media).install(target, media, answers)
    say("application installed; the first boot sets up the house")


class Plugin:
    def on_genfstab(self, installation):
        finalize(installation.target, Path(__file__).resolve().parent, Path(os.environ["THERMITE_INSTALLER_STATE"]))
        return False
