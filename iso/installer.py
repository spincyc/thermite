"""The Thermite installer's core, on the Arch live medium; stdlib only.

The terminal front end (tui/installer) asks the questions. This checks the
answers, writes archinstall 4.5's configuration and credentials, runs it
without its menus, streams progress as JSON lines and keeps a log. It never
chooses a disk itself: install needs the disk's name typed back.

    python3 -I -B installer.py launch [--ssh]   the terminal (archiso runs it on tty1)
    python3 -I -B installer.py serve            JSON-lines requests on stdin

Every request is one line, {"id": N, "op": NAME, ...}; every answer is one
line with the same id, {"ok": true, "result": ...} or {"ok": false,
"error": TEXT}. install also sends {"id": N, "event": {...}} lines before
its answer. Passwords arrive only on stdin; they are hashed for the
credentials file, which is deleted after the run, and are never logged.
"""
from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid

MEDIA = Path("/run/archiso/bootmnt/thermite")
STATE = Path("/run/thermite-installer")
TARGET = Path("/mnt")
MiB = 1024 ** 2
#: Disks smaller than this are not offered; smaller than WARN_DISK warns.
MIN_DISK = 32 * 10 ** 9
WARN_DISK = 64 * 10 ** 9
WARN_MEMORY = 4 * 1024 ** 3
BOOT_MIB = 1024
FILESYSTEMS = ("btrfs", "ext4")
KERNELS = ("linux-lts", "linux")
#: Btrfs subvolumes, as archinstall's default layout makes them. Thermite
#: makes /srv/thermite its own subvolume when it installs.
SUBVOLUMES = (("@", "/"), ("@home", "/home"), ("@log", "/var/log"), ("@pkg", "/var/cache/pacman/pkg"))
DEFAULTS = {"hostname": "thermite", "timezone": "UTC", "locale": "en_US.UTF-8", "keymap": "us",
            "filesystem": "btrfs", "kernel": "linux-lts"}
#: Answers kept on the target, in the receipt and in the log. Everything else
#: (the passwords, the Wi-Fi passphrase, the typed confirmation) is secret.
PUBLIC = ("hostname", "owner", "timezone", "locale", "keymap", "disk", "filesystem", "kernel", "wifi_ssid",
          "wifi_security")
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
#: Names the target's packages or Thermite itself use; thermite is the
#: service account (UID 9001) that thermite install creates.
RESERVED = frozenset({"thermite", "root", "bin", "daemon", "mail", "ftp", "http", "nobody", "dbus", "uuidd",
                      "dhcpcd", "git", "avahi", "polkitd", "rtkit", "tss", "alpm", "named", "ntp", "rpc",
                      "openvpn", "nm-openconnect", "tcpdump", "wheel", "users", "admin", "owner"})
ARCHINSTALL = (4, 5)
#: What archinstall straps besides the packages file; the preflight checks
#: them too, so a missing package fails before the disk is touched.
BASE = ("base", "linux-firmware", "mkinitcpio")
#: Lines of archinstall's output that start a phase, and its share of the run.
PHASES = (("Starting device modifications", "disk", 5, "Partitioning and formatting the disk"),
          ("Starting installation", "base", 10, "Installing the base system"),
          ("Setting up swap on zram", "swap", 40, "Setting up zram swap"),
          ("Adding bootloader", "bootloader", 45, "Installing the boot loader"),
          ("Creating user", "accounts", 55, "Creating the owner"),
          ("Setting password for", "packages", 60, "Installing Thermite's packages"),
          ("Activating systemd-timesyncd", "services", 80, "Enabling services"),
          ("Thermite:", "thermite", 85, "Installing Thermite"),
          ("Installation completed without any errors", "finished", 100, "Installed"))


class Invalid(ValueError):
    """An answer the installer refuses, with the message to show."""


class Host:
    """The live system as the core reads and changes it; tests pass a fake."""

    def __init__(self, root: Path = Path("/")):
        self.root = Path(root)

    def path(self, name: str) -> Path:
        return self.root / name.lstrip("/")

    def run(self, argv, *, timeout: float = 120, input: str | None = None) -> subprocess.CompletedProcess:
        """argv to its end, its output captured. Its stdin is input, else
        /dev/null: never the front end's request pipe, which is the core's."""
        try:
            return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                                  **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))

    def mounted(self, path: Path) -> bool:
        return os.path.ismount(path)

    def online(self) -> bool:
        try:
            with socket.create_connection(("archlinux.org", 443), timeout=5):
                return True
        except OSError:
            return False

    def spawn(self, argv, env: dict) -> subprocess.Popen:
        # Its own session: a closed SSH login's hangup must not reach it.
        return subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", env=env, bufsize=1, start_new_session=True)

    def call(self, argv, env: dict) -> int:
        """A program on this terminal, until it ends."""
        return subprocess.run(list(argv), env=env).returncode

    def hash_password(self, plaintext: str) -> str:
        """yescrypt, as archinstall and Arch's PAM hash passwords."""
        libcrypt = ctypes.CDLL("libcrypt.so.2")
        libcrypt.crypt_gensalt.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int]
        libcrypt.crypt_gensalt.restype = ctypes.c_char_p
        libcrypt.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        libcrypt.crypt.restype = ctypes.c_char_p
        setting = libcrypt.crypt_gensalt(b"$y$", 5, None, 0)
        hashed = libcrypt.crypt(plaintext.encode(), setting) if setting else None
        if not hashed or not hashed.startswith(b"$y$"): raise RuntimeError("could not hash the password")
        return hashed.decode()


# ---- facts ------------------------------------------------------------------

def disks(host: Host) -> list[dict]:
    """Every whole disk, with why it is not offered when it is not."""
    completed = host.run(["lsblk", "--json", "--bytes", "--tree", "--output",
                          "NAME,PATH,TYPE,SIZE,LOG-SEC,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,FSTYPE"], timeout=30)
    if completed.returncode: raise RuntimeError("lsblk failed: " + completed.stderr.strip())
    found = []
    for device in json.loads(completed.stdout).get("blockdevices", []):
        if device.get("type") != "disk" or str(device.get("name", "")).startswith(("zram", "loop")): continue
        tree = [device, *(device.get("children") or [])]
        mounts = [point for node in tree for point in (node.get("mountpoints") or []) if point]
        size = int(device.get("size") or 0)
        reason = ("holds the installation medium" if any(point.startswith("/run/archiso") for point in mounts)
                  else "read-only" if device.get("ro") else "in use" if mounts
                  else f"smaller than {MIN_DISK // 10 ** 9} GB" if size < MIN_DISK else None)
        found.append({"name": device["name"], "path": device["path"], "size": size,
                      "sector_size": int(device.get("log-sec") or 512),
                      "model": (device.get("model") or "").strip(), "serial": (device.get("serial") or "").strip(),
                      "transport": device.get("tran") or "", "removable": bool(device.get("rm")),
                      "partitions": [{"name": child["name"], "size": int(child.get("size") or 0),
                                      "fstype": child.get("fstype") or ""} for child in device.get("children") or []],
                      "eligible": reason is None, "reason": reason})
    return found


def interfaces(host: Host) -> list[dict]:
    completed = host.run(["ip", "-json", "address", "show"], timeout=10)
    result = []
    for link in json.loads(completed.stdout or "[]") if not completed.returncode else []:
        if link.get("ifname") == "lo" or "LOOPBACK" in link.get("flags", []): continue
        name = link["ifname"]
        result.append({"name": name, "mac": link.get("address", ""), "up": "LOWER_UP" in link.get("flags", []),
                       "wireless": host.path(f"/sys/class/net/{name}/wireless").is_dir(),
                       "addresses": [f"{entry['local']}/{entry['prefixlen']}" for entry in link.get("addr_info", [])
                                     if entry.get("family") == "inet"]})
    return result


def default_route(host: Host) -> str | None:
    """The interface of the preferred default route: the one this live
    system reaches the Internet through."""
    completed = host.run(["ip", "-json", "route", "show", "default"], timeout=10)
    try:
        routes = json.loads(completed.stdout or "[]") if not completed.returncode else []
    except ValueError:
        return None
    routes = [route for route in routes if isinstance(route, dict) and route.get("dev")]
    return min(routes, key=lambda route: route.get("metric", 0))["dev"] if routes else None


def network(host: Host) -> dict:
    """The interfaces, whether the Internet answers, the route's interface,
    and the Wi-Fi network this live system is joined to ({ssid, security,
    hidden, carried}, never its passphrase), which the installed host joins too."""
    found = interfaces(host)
    wifi = None
    for entry in found:
        if entry["wireless"] and (wifi := wifi_joined(host, entry["name"])): break
    return {"interfaces": found, "online": host.online(), "route": default_route(host), "wifi": wifi}


def facts(host: Host, media: Path = MEDIA) -> dict:
    memory = 0
    for line in host.path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"): memory = int(line.split()[1]) * 1024
    cpu = next((line.split(":", 1)[1].strip() for line in host.path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")), "")
    found = disks(host)
    machine = platform.machine()
    warnings = []
    if machine != "x86_64": warnings.append(f"Thermite needs an x86_64 host; this is {machine}.")
    if memory < WARN_MEMORY: warnings.append("Less than 4 GiB of memory: openHAB and monitoring may not fit.")
    if not any(disk["eligible"] for disk in found): warnings.append("No disk of at least 32 GB is free to install on.")
    elif not any(disk["eligible"] and disk["size"] >= WARN_DISK for disk in found):
        warnings.append("Thermite's history wants a disk of at least 64 GB.")
    payload = media / "payload.json"
    release = json.loads(payload.read_text()) if payload.is_file() else {}
    return {"machine": machine, "uefi": host.path("/sys/firmware/efi").is_dir(), "memory": memory, "cpu": cpu,
            "disks": found, "network": network(host), "warnings": warnings,
            "application": release.get("application", ""), "release": release.get("release", ""),
            "authorized_key": authorized_key(host, media)}


def authorized_key(host: Host, media: Path) -> dict | None:
    """The public key the image was built with ({type, fingerprint, comment})."""
    payload = media / "payload.json"
    return json.loads(payload.read_text()).get("authorized_key") if payload.is_file() else None


def choices(host: Host) -> dict:
    """Timezones, UTF-8 locales and console keymaps the live system knows."""
    zones = host.path("/usr/share/zoneinfo")
    timezones = sorted(str(path.relative_to(zones)) for path in zones.rglob("*")
                       if path.is_file() and path.relative_to(zones).parts[0] not in ("posix", "right", "Etc")
                       and path.name[:1].isupper() and "." not in path.name) + ["UTC"]
    supported = host.path("/usr/share/i18n/SUPPORTED")
    locales = sorted({line.split()[0] for line in supported.read_text().splitlines()
                      if line.strip() and line.split()[-1] == "UTF-8"}) if supported.is_file() else ["en_US.UTF-8"]
    completed = host.run(["localectl", "list-keymaps"], timeout=10)
    keymaps = sorted(set(completed.stdout.split())) if not completed.returncode else ["us"]
    return {"timezones": sorted(set(timezones)), "locales": locales, "keymaps": keymaps}


def taken(host: Host, name: str) -> bool | None:
    """Whether NAME.local answers on this network: true, false, or unknown."""
    completed = host.run(["resolvectl", "query", "--legend=no", "--protocol=mdns", f"{name}.local"], timeout=8)
    if completed.returncode == 0: return True
    if completed.returncode == 127: return None
    return False


def hostname_probe(host: Host, name: str) -> dict:
    """The name's state on the LAN, and the first free thermite-N if taken."""
    found = taken(host, name)
    suggestion = None
    if found:
        stem = re.sub(r"-[0-9]+$", "", name)
        for number in range(2, 10):
            candidate = f"{stem}-{number}"
            if taken(host, candidate) is False:
                suggestion = candidate
                break
    return {"name": name, "taken": found, "suggestion": suggestion}


# ---- Wi-Fi (iwd on the live system) -----------------------------------------
#
# The installed host joins the network this live system is joined to, with
# the credentials iwd saved for it: joined here, or with iwctl from a shell.

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: A WPA passphrase: 8 to 63 printable ASCII characters (IEEE 802.11i).
PASSPHRASE = re.compile(r"[\x20-\x7e]{8,63}")
#: Or the key itself, 64 hex digits; WPA2 only, as WPA3 (SAE) needs the passphrase.
HEX_KEY = re.compile(r"[0-9A-Fa-f]{64}")
#: iwd's network types the installer joins: open, and WPA/WPA2/WPA3 Personal.
JOINABLE = ("open", "psk")


def wifi_scan(host: Host, device: str) -> list[dict]:
    device = wireless(host, device)
    host.run(["iwctl", "station", device, "scan"], timeout=15)
    time.sleep(2)
    completed = host.run(["iwctl", "station", device, "get-networks"], timeout=15)
    if completed.returncode: raise RuntimeError(completed.stderr.strip() or "Wi-Fi scan failed")
    networks = []
    for line in ANSI.sub("", completed.stdout).splitlines():
        # From the right: the signal's stars and the security are single
        # words, so a name keeps its own runs of spaces.
        columns = line.rstrip().rsplit(None, 2)
        if len(columns) != 3 or not columns[2] or set(columns[2]) - {"*"}: continue
        name = columns[0].strip()
        connected = name.startswith(">")
        name = name.removeprefix(">").strip() if connected else name
        if not name or name == "Network name": continue
        networks.append({"ssid": name, "security": columns[1], "signal": len(columns[2]), "connected": connected})
    return networks


def wireless(host: Host, device: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", device or "") or not host.path(f"/sys/class/net/{device}/wireless").is_dir():
        raise Invalid("not a Wi-Fi device")
    return device


def iwd_name(ssid: str) -> str:
    """iwd's file name for a network (iwd.network(5)): an SSID of ASCII
    letters, digits, spaces, - and _ as it is, any other hex-encoded after =."""
    return ssid if re.fullmatch(r"[A-Za-z0-9 _-]+", ssid) else "=" + ssid.encode().hex()


def escape(value: str) -> str:
    """A value as iwd's settings files (ell) read it back: backslashes
    escaped, and the spaces at either end, which it would trim."""
    return re.sub(r"^ +| +$", lambda spaces: "\\s" * len(spaces[0]), value.replace("\\", "\\\\"))


def unescape(value: str) -> str:
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    return re.sub(r"\\(.)", lambda found: escapes.get(found[1], found[0]), value)


def check_ssid(ssid) -> str:
    if not isinstance(ssid, str) or not ssid or len(ssid.encode()) > 32 or any(ord(ch) < 32 for ch in ssid):
        raise Invalid("invalid network name")
    return ssid


def check_key(security: str, passphrase: str) -> str:
    """The credential an iwd network type takes: none for an open network,
    a WPA passphrase or a 64-hex-digit key for the rest."""
    if security == "open":
        if passphrase: raise Invalid("an open network takes no passphrase")
        return ""
    if not (PASSPHRASE.fullmatch(passphrase) or HEX_KEY.fullmatch(passphrase)):
        raise Invalid("a WPA passphrase is 8 to 63 printable ASCII characters, or the key as 64 hex digits")
    return passphrase


def iwd_profile(host: Host, ssid: str) -> dict | None:
    """What iwd saved for a network on this live system: {kind, passphrase,
    key, hidden}; None when it saved nothing."""
    store = host.path("/var/lib/iwd")
    for kind in ("psk", "open"):
        path = store / f"{iwd_name(ssid)}.{kind}"
        if not path.is_file(): continue
        values = {}
        for line in path.read_text(errors="replace").splitlines():
            key, equals, value = line.partition("=")
            if equals and not line.startswith(("#", "[")): values[key.strip()] = unescape(value.lstrip(" \t"))
        return {"kind": kind, "passphrase": values.get("Passphrase", ""), "key": values.get("PreSharedKey", ""),
                "hidden": values.get("Hidden", "").lower() == "true"}
    return None


def station(host: Host, device: str) -> dict:
    """iwctl station DEVICE show, as {property: value}."""
    completed = host.run(["iwctl", "station", device, "show"], timeout=15)
    shown = {}
    for line in ANSI.sub("", completed.stdout if not completed.returncode else "").splitlines():
        found = re.match(r"\s*(?:\*\s+)?(State|Connected network|Security)\s{2,}(.*?)\s*$", line)
        if found: shown[found[1]] = found[2]
    return shown


def wifi_joined(host: Host, device: str) -> dict | None:
    """The network DEVICE is joined to: {device, ssid, security, hidden,
    carried}. security is what the installed host's NetworkManager uses:
    open, psk (WPA/WPA2) or sae (iwd negotiated WPA3); carried says whether
    iwd saved what the installed host needs to join it."""
    shown = station(host, device)
    ssid = shown.get("Connected network")
    if shown.get("State") != "connected" or not ssid: return None
    profile = iwd_profile(host, ssid) or {"kind": "", "passphrase": "", "key": "", "hidden": False}
    negotiated = shown.get("Security", "")
    security = ("sae" if "WPA3" in negotiated else "open" if negotiated.lower().startswith("open")
                else "psk" if "Personal" in negotiated or "PSK" in negotiated
                else None if negotiated or profile["kind"] not in JOINABLE else profile["kind"])
    carried = (security == "open" or security == "sae" and bool(profile["passphrase"])
               or security == "psk" and bool(profile["passphrase"] or profile["key"]))
    return {"device": device, "ssid": ssid, "security": security, "hidden": profile["hidden"], "carried": carried}


def wifi_credentials(host: Host, wifi: dict) -> dict:
    """What the installed host joins with (wifi.json): the joined network
    and the passphrase or key iwd saved for it."""
    if not wifi.get("carried"):
        raise Invalid(f"{wifi['ssid']}'s passphrase is not saved on this live system; join it on the Network step")
    profile = iwd_profile(host, wifi["ssid"]) or {"passphrase": "", "key": ""}
    credentials = {"ssid": wifi["ssid"], "security": wifi["security"], "hidden": wifi["hidden"]}
    if wifi["security"] == "sae": credentials["passphrase"] = profile["passphrase"]
    elif wifi["security"] == "psk":
        credentials.update({"passphrase": profile["passphrase"]} if profile["passphrase"] else {"key": profile["key"]})
    return credentials


def wifi_connect(host: Host, device: str, ssid, passphrase: str, *, security: str = "psk",
                 hidden: bool = False) -> dict:
    """Join a network. Its passphrase goes in iwd's own root-only file,
    never on a command line another process could read. A network this
    device is already joined to is left and joined again, so the
    passphrase given is the one that works."""
    device, ssid = wireless(host, device), check_ssid(ssid)
    if hidden: security = "psk" if passphrase else "open"
    if security not in JOINABLE:
        raise Invalid(f"the installer joins open and WPA Personal networks, not {security}; use a cable")
    passphrase = check_key(security, passphrase or "")
    store = host.path("/var/lib/iwd"); store.mkdir(mode=0o700, exist_ok=True)
    path = store / f"{iwd_name(ssid)}.{security}"
    lines = (["[Security]", "PreSharedKey=" + passphrase.lower() if HEX_KEY.fullmatch(passphrase)
              else "Passphrase=" + escape(passphrase)] if passphrase else [])
    lines += ["[Settings]", "Hidden=true"] if hidden else []
    if lines:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(handle, "w") as out: out.write("\n".join(lines) + "\n")
    if station(host, device).get("Connected network") == ssid:
        host.run(["iwctl", "station", device, "disconnect"], timeout=15)
    completed = host.run(["iwctl", "station", device, "connect-hidden" if hidden else "connect", ssid], timeout=40)
    if completed.returncode: raise RuntimeError(ANSI.sub("", completed.stdout + completed.stderr).strip() or "could not connect")
    for _ in range(15):
        if host.online(): return network(host)
        time.sleep(2)
    raise RuntimeError(f"joined {ssid} but the Internet is not reachable")


# ---- answers ------------------------------------------------------------------

def check_hostname(value, answers, facts, choices):
    value = str(value or "").strip().lower()
    if not HOSTNAME.fullmatch(value):
        raise Invalid("1-63 lowercase letters, digits and hyphens, not starting or ending with a hyphen")
    return value


def check_owner(value, answers, facts, choices):
    value = str(value or "").strip()
    if value == "thermite": raise Invalid("thermite is the service account Thermite creates; choose your own name")
    if not USERNAME.fullmatch(value): raise Invalid("lowercase letters, digits, - and _, starting with a letter")
    if value in RESERVED or value.startswith("systemd-"): raise Invalid(f"{value} is a system account name")
    return value


def password(name: str, confirm: str):
    def check(value, answers, facts, choices):
        value = value if isinstance(value, str) else ""
        if len(value) < 8: raise Invalid("at least 8 characters")
        if any(ch in value for ch in "\0\n\r"): raise Invalid("no line breaks")
        if answers.get(confirm) != value: raise Invalid("the two entries differ")
        if name == "root_password" and value == answers.get("owner_password"):
            raise Invalid("use a different password from the owner's")
        return value
    return check


def listed(name: str, key: str):
    def check(value, answers, facts, choices):
        value = str(value or "").strip()
        if value not in choices[key]: raise Invalid(f"not a known {name}")
        return value
    return check


def one_of(options):
    def check(value, answers, facts, choices):
        if value not in options: raise Invalid("choose one of " + ", ".join(options))
        return value
    return check


def check_disk(value, answers, facts, choices):
    disk = next((disk for disk in facts["disks"] if disk["path"] == value), None)
    if disk is None: raise Invalid("choose one of the disks shown")
    if not disk["eligible"]: raise Invalid(f"{disk['path']} {disk['reason']}")
    return disk["path"]


def check_confirm(value, answers, facts, choices):
    disk = next((disk for disk in facts["disks"] if disk["path"] == answers.get("disk")), None)
    if disk is None or str(value or "").strip() != disk["name"]:
        raise Invalid(f"type {disk['name'] if disk else 'the disk name'} to erase it")
    return True


def check_wifi(value, answers, facts, choices):
    """The Wi-Fi network the installed host joins: the one this live system
    is joined to, as the review showed it, or none."""
    joined = facts.get("network", {}).get("wifi")
    if not joined:
        if value: raise Invalid(f"this live system is no longer joined to {value}; join it on the Network step")
        return None
    if value != joined["ssid"]:
        raise Invalid(f"this live system is joined to {joined['ssid']}, not {value or 'no Wi-Fi'}")
    if joined["security"] is None: raise Invalid(f"{joined['ssid']} is not an open or WPA Personal network")
    if not joined["carried"]:
        raise Invalid(f"{joined['ssid']}'s passphrase is not saved on this live system; join it on the Network step")
    return value


CHECKS = {"hostname": check_hostname, "owner": check_owner,
          "owner_password": password("owner_password", "owner_password_confirm"),
          "root_password": password("root_password", "root_password_confirm"),
          "timezone": listed("timezone", "timezones"), "locale": listed("locale", "locales"),
          "keymap": listed("keymap", "keymaps"), "disk": check_disk, "filesystem": one_of(FILESYSTEMS),
          "kernel": one_of(KERNELS), "wifi_ssid": check_wifi, "confirm": check_confirm}


def check(answers: dict, facts: dict, choices: dict, fields=None) -> tuple[dict, dict]:
    """The named fields (all by default), normalized, and an error per bad field."""
    values, errors = {}, {}
    for field in fields or CHECKS:
        if field not in CHECKS: raise Invalid(f"unknown answer {field}")
        try: values[field] = CHECKS[field](answers.get(field, DEFAULTS.get(field)), answers, facts, choices)
        except Invalid as exc: errors[field] = str(exc)
    return values, errors


def public(answers: dict) -> dict:
    return {key: answers[key] for key in PUBLIC if answers.get(key) is not None}


# ---- archinstall's files ------------------------------------------------------

def size(mib: int, sector: int) -> dict:
    return {"value": mib, "unit": "MiB", "sector_size": {"value": sector, "unit": "B"}}


def layout(disk: dict, filesystem: str, uefi: bool) -> dict:
    """archinstall's default layout for one disk: a 1 GiB FAT32 /boot, then
    the root filesystem to the end, less the last MiB GPT keeps for its
    backup header. Btrfs gets the default subvolumes and zstd compression."""
    sector, total = disk["sector_size"], disk["size"] // MiB
    root_start = 1 + BOOT_MIB
    root_size = total - 1 - root_start
    if root_size * MiB < MIN_DISK // 2: raise Invalid("the disk is too small")
    def identity(index): return str(uuid.uuid5(uuid.NAMESPACE_URL, f"thermite-installer:{disk['path']}:{index}"))
    boot = {"obj_id": identity(0), "status": "create", "type": "primary", "start": size(1, sector),
            "size": size(BOOT_MIB, sector), "fs_type": "fat32", "mountpoint": "/boot", "mount_options": [],
            "flags": ["boot", "esp"] if uefi else ["boot"], "dev_path": None, "btrfs": []}
    btrfs = filesystem == "btrfs"
    root = {"obj_id": identity(1), "status": "create", "type": "primary", "start": size(root_start, sector),
            "size": size(root_size, sector), "fs_type": filesystem, "mountpoint": None if btrfs else "/",
            "mount_options": ["compress=zstd"] if btrfs else [], "flags": [], "dev_path": None,
            "btrfs": [{"name": name, "mountpoint": point} for name, point in SUBVOLUMES] if btrfs else []}
    return {"config_type": "default_layout",
            "device_modifications": [{"device": disk["path"], "wipe": True, "partitions": [boot, root]}]}


def selected_packages(document: dict, filesystem: str) -> list[str]:
    """packages.json's tiers in order; a filesystem's own tier only with it."""
    return [name for tier in document["tiers"] if tier.get("filesystem") in (None, filesystem)
            for name in tier["packages"]]


def kernels(answers: dict) -> list[str]:
    """The chosen kernel first (the boot default), the other as the fallback entry."""
    return [answers["kernel"], *(kernel for kernel in KERNELS if kernel != answers["kernel"])]


def configuration(answers: dict, facts: dict, document: dict) -> dict:
    """archinstall 4.5's --config: everything but the credentials."""
    disk = next(disk for disk in facts["disks"] if disk["path"] == answers["disk"])
    return {
        "script": "guided",
        "archinstall-language": "English",
        "hostname": answers["hostname"],
        "kernels": kernels(answers),
        "timezone": answers["timezone"],
        "ntp": True,
        "locale_config": {"kb_layout": answers["keymap"], "sys_lang": answers["locale"], "sys_enc": "UTF-8"},
        # systemd-boot's default entry is the first kernel; BIOS needs GRUB,
        # whose default the Thermite plugin sets.
        "bootloader_config": {"bootloader": "Systemd-boot" if facts["uefi"] else "Grub", "uki": False,
                              "removable": False},
        "disk_config": layout(disk, answers["filesystem"], facts["uefi"]),
        "network_config": {"type": "nm"},
        "packages": selected_packages(document, answers["filesystem"]),
        "services": list(document["enable"]),
        "swap": {"enabled": True, "algorithm": "zstd"},
        "pacman_config": {"parallel_downloads": 5, "color": False},
    }


def credentials(answers: dict, hasher) -> dict:
    """archinstall 4.5's --creds: crypt hashes only. The owner joins wheel,
    whose sudo rule the Thermite plugin enables."""
    return {"root_enc_password": hasher(answers["root_password"]),
            "users": [{"username": answers["owner"], "enc_password": hasher(answers["owner_password"]),
                       "sudo": False, "groups": ["wheel"]}]}


def preflight_packages(answers: dict, facts: dict, document: dict) -> list[str]:
    boot = () if facts["uefi"] else ("grub",)
    btrfs = ("btrfs-progs",) if answers["filesystem"] == "btrfs" else ()
    return [*BASE, *kernels(answers), *boot, *btrfs, *selected_packages(document, answers["filesystem"])]


# ---- the run ------------------------------------------------------------------

def private(path: Path, text: str) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "w") as out: out.write(text)


class Run:
    """One installation: its log, its progress and its files in STATE."""

    def __init__(self, host: Host, state: Path, emit):
        self.host, self.state, self.emit = host, Path(state), emit
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log = (self.state / "installer.log").open("a", encoding="utf-8")
        self.percent = 0

    def say(self, kind: str, **fields) -> None:
        event = {"kind": kind, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), **fields}
        self.log.write(json.dumps(event) + "\n"); self.log.flush()
        self.emit(event)

    def phase(self, phase: str, percent: int, label: str) -> None:
        if percent < self.percent: return
        self.percent = percent
        self.say("phase", phase=phase, percent=percent, label=label)

    def command(self, argv, label: str, timeout: float = 600) -> None:
        self.say("line", text="$ " + " ".join(argv[:3]) + (" …" if len(argv) > 3 else ""))
        completed = self.host.run(argv, timeout=timeout)
        for line in (completed.stdout + completed.stderr).splitlines()[-20:]:
            self.say("line", text=line)
        if completed.returncode: raise RuntimeError(f"{label} failed; see the log")


def archinstall_version(host: Host) -> tuple[int, int] | None:
    completed = host.run([sys.executable, "-I", "-c",
                          "from importlib.metadata import version; print(version('archinstall'))"], timeout=30)
    found = re.match(r"(\d+)\.(\d+)", completed.stdout.strip()) if not completed.returncode else None
    return (int(found[1]), int(found[2])) if found else None


def install(answers: dict, host: Host, *, media: Path = MEDIA, state: Path = STATE, target: Path = TARGET,
            emit=lambda event: None) -> dict:
    """Check everything again, preflight, then run archinstall unattended."""
    # A failed run can leave the target mounted, and its disk then reads as
    # in use: unmount it first, so the review can try again.
    left = host.mounted(target)
    if left and (host.run(["umount", "--recursive", str(target)], timeout=120).returncode or host.mounted(target)):
        raise Invalid(f"{target} is still mounted from an earlier run, and could not be unmounted; "
                      f"from a shell, umount -R {target}, then try again")
    found, options = facts(host, media), choices(host)
    values, errors = check(answers, found, options)
    if errors: raise Invalid("; ".join(f"{field}: {message}" for field, message in errors.items()))
    answers = {**values, "owner_password": answers["owner_password"], "root_password": answers["root_password"]}
    joined = found["network"]["wifi"]
    if joined: answers["wifi_security"] = joined["security"]
    document = json.loads((media / "packages.json").read_text())
    run = Run(host, state, emit)
    if left: run.say("line", text=f"Unmounted {target}, which an earlier run left mounted")
    run.say("answers", answers=public(answers))
    secrets = [state / "creds.json", state / "wifi.json"]
    try:
        run.phase("preflight", 1, "Checking the installer, keyring, mirrors and packages")
        if archinstall_version(host) != ARCHINSTALL:
            raise RuntimeError("this medium needs its bundled archinstall 4.5; do not upgrade it")
        run.command(["systemctl", "start", "pacman-init.service"], "the package keyring")
        run.command(["pacman", "-Sy", "--noconfirm"], "the package database refresh")
        # Every package the run will strap, checked before the disk is wiped.
        run.command(["pacman", "-Sp", "--noconfirm", *preflight_packages(answers, found, document)],
                    "the package check")
        config = configuration(answers, found, document)
        (state / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        (state / "answers.json").write_text(json.dumps(public(answers), indent=2) + "\n")
        private(state / "creds.json", json.dumps(credentials(answers, host.hash_password)) + "\n")
        if joined: private(state / "wifi.json", json.dumps(wifi_credentials(host, joined)) + "\n")
        run.phase("disk", 3, f"Erasing {answers['disk']}")
        argv = ["archinstall", "--config", str(state / "config.json"), "--creds", str(state / "creds.json"),
                "--silent", "--skip-wifi-check", "--skip-version-check", "--plugin", str(media / "arch-plugin.py")]
        run.say("line", text="$ " + " ".join(argv))
        process = host.spawn(argv, {**os.environ, "THERMITE_INSTALLER_STATE": str(state), "TERM": "dumb"})
        for line in process.stdout:
            line = ANSI.sub("", line.rstrip("\n"))
            run.say("line", text=line)
            for marker, phase, percent, label in PHASES:
                if marker in line: run.phase(phase, percent, label)
        code = process.wait()
        if code: raise RuntimeError(f"archinstall stopped with status {code}; see the log")
        if run.percent < 100: raise RuntimeError("archinstall ended without reporting a completed installation")
        run.say("done", hostname=answers["hostname"])
    except Exception as exc:
        run.say("failed", error=str(exc))
        raise
    finally:
        for path in secrets: path.unlink(missing_ok=True)
        keep(run, target)
        run.log.close()
    return {"hostname": answers["hostname"], "log": str(state / "installer.log")}


def keep(run: Run, target: Path) -> None:
    """The run's own log, on the target beside archinstall's, while it is mounted."""
    destination = target / "var/log/thermite-install"
    if not target.is_mount() or not destination.is_dir(): return
    run.log.flush()
    (destination / "installer.log").write_bytes((run.state / "installer.log").read_bytes())


# ---- launch: the terminal on the live system -----------------------------------

#: The live sshd with the image's key: that key alone, as root, and only
#: into the installer (its authorized_keys line forces the command).
#: sshd_config.d is read in order and the first value wins, so this
#: precedes archiso's own 10-archiso.conf.
LIVE_SSHD = """\
# Thermite installer: SSH with the image's key only, straight into the installer.
PermitRootLogin forced-commands-only
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
"""
#: The command a login runs again on this live system.
COMMAND = "thermite-installer"


def digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def sri(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha512-" + base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()


def runtime(media: Path, state: Path) -> tuple[Path, Path]:
    """Node.js and blessed from the medium, checked against payload.json
    again and unpacked once into STATE: (node, node_modules)."""
    pinned = json.loads((media / "payload.json").read_text())["installer"]
    unpacked = state / "runtime"
    marker = unpacked / "pinned.json"
    node, modules = unpacked / "node", unpacked / "node_modules"
    if marker.is_file() and json.loads(marker.read_text()) == pinned: return node, modules
    for name, expected in pinned["files"].items():
        if digest(media / name) != expected: raise ValueError(f"{name} on the medium is damaged")
    archive = media / pinned["node"]["name"]
    if digest(archive) != pinned["node"]["sha256"]: raise ValueError(f"{archive.name} on the medium is damaged")
    package = media / pinned["blessed"]["name"]
    if sri(package) != pinned["blessed"]["integrity"]: raise ValueError(f"{package.name} on the medium is damaged")
    shutil.rmtree(unpacked, ignore_errors=True)
    unpacked.mkdir(mode=0o700, parents=True)
    # Only the node binary; the tarball's npm and headers stay packed.
    with tarfile.open(archive, "r:xz") as tar:
        member = tar.getmember(pinned["node"]["name"].removesuffix(".tar.xz") + "/bin/node")
        if not member.isfile(): raise ValueError("the Node.js archive has no node program")
        with tar.extractfile(member) as source, node.open("xb") as out: shutil.copyfileobj(source, out)
    node.chmod(0o755)
    # An npm tarball keeps its files under package/.
    with tarfile.open(package, "r:gz") as tar:
        members = []
        for member in tar.getmembers():
            if not member.name.startswith("package/"): raise ValueError("unexpected blessed archive layout")
            member.name = "blessed/" + member.name.removeprefix("package/")
            members.append(member)
        tar.extractall(modules, members=members, filter="data")
    marker.write_text(json.dumps(pinned) + "\n")
    return node, modules


def live_ssh(host: Host, media: Path) -> bool:
    """With the image's key: sshd takes that key alone, into the installer."""
    key = media / "authorized_keys"
    if not key.is_file(): return False
    forced = (f'command="{sys.executable} -I -B {media / "installer.py"} launch --ssh",restrict,pty '
              + key.read_text().strip() + "\n")
    directory = host.path("/root/.ssh")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(directory / "authorized_keys", forced)
    config = host.path("/etc/ssh/sshd_config.d/05-thermite.conf")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(LIVE_SSHD)
    host.run(["systemctl", "reload-or-restart", "sshd.service"], timeout=30)
    return True


def terminal(env: dict, host: Host) -> str:
    """TERM if this system describes it, else xterm-256color: SSH from a
    terminal the live system has no terminfo for still gets the installer."""
    name = env.get("TERM", "")
    known = name and name != "dumb" and any(host.path(f"{root}/{name[0]}/{name}").exists()
                                           for root in ("/usr/share/terminfo", "/usr/lib/terminfo"))
    return name if known else "xterm-256color"


def launch(host: Host, *, media: Path = MEDIA, state: Path = STATE, ssh: bool = False, env=None) -> int:
    """The installer's terminal on this console or SSH login; its status."""
    env = dict(os.environ if env is None else env)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not ssh:
        live_ssh(host, media)
        command = host.path(f"/usr/local/bin/{COMMAND}")
        command.parent.mkdir(parents=True, exist_ok=True)
        command.write_text(f"#!/bin/sh\nexec {sys.executable} -I -B {media / 'installer.py'} launch \"$@\"\n")
        command.chmod(0o755)
        # Terminus draws the installer's box corners and marks on the console.
        if env.get("TERM") == "linux" and host.path("/usr/share/kbd/consolefonts/ter-v16n.psf.gz").exists():
            host.run(["setfont", "ter-v16n"], timeout=10)
    try:
        node, modules = runtime(media, state)
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"The Thermite installer cannot start: {exc}", file=sys.stderr)
        return 1
    client = env.get("SSH_CLIENT", "").split()
    label = f"SSH from {client[0]}" if ssh and client else "SSH" if ssh else "the console"
    env.update({"TERM": terminal(env, host), "THERMITE_REMOTE_LOOK": (media / "look.json").read_text()})
    status = host.call([str(node), str(media / "tui/installer/main.js"), "--modules", str(modules),
                        "--core", str(media / "installer.py"), "--python", sys.executable, "--label", label,
                        "--core-log", str(state / "core.log")], env)
    print(f"Left the Thermite installer. {COMMAND} starts it again; "
          f"tail -f {state / 'installer.log'} follows an installation.")
    return status


# ---- serve ----------------------------------------------------------------------

class Session:
    """One front end's requests. Only one session may install at a time:
    claim takes a lock the console and an SSH login share."""

    def __init__(self, host: Host, media: Path = MEDIA, state: Path = STATE, target: Path = TARGET):
        self.host, self.media, self.state, self.target = host, Path(media), Path(state), Path(target)
        self.facts = self.options = self.lock = None

    def handle(self, request: dict, write) -> object:
        op = request.get("op")
        if op == "facts":
            if self.facts is None or request.get("refresh"): self.facts = facts(self.host, self.media)
            return self.facts
        if op == "choices":
            if self.options is None: self.options = choices(self.host)
            return self.options
        if op == "network":
            found = network(self.host)
            if self.facts is not None: self.facts["network"] = found
            return found
        if op == "hostname": return hostname_probe(self.host, check_hostname(request.get("name"), {}, {}, {}))
        if op == "wifi-scan": return wifi_scan(self.host, request.get("device"))
        if op == "wifi-connect":
            found = wifi_connect(self.host, request.get("device"), request.get("ssid"), request.get("passphrase") or "",
                                 security=str(request.get("security") or "psk"), hidden=bool(request.get("hidden")))
            if self.facts is not None: self.facts["network"] = found
            return found
        if op == "check":
            if self.facts is None: self.facts = facts(self.host, self.media)
            if self.options is None: self.options = choices(self.host)
            values, errors = check(request.get("answers") or {}, self.facts, self.options, request.get("fields"))
            return {"values": public(values), "errors": errors}
        if op == "claim": return self.claim(str(request.get("label") or "console"))
        if op == "install":
            if self.lock is None: raise Invalid("another session is installing")
            return install(request.get("answers") or {}, self.host, media=self.media, state=self.state,
                           target=self.target, emit=lambda event: write({"id": request.get("id"), "event": event}))
        if op == "reboot":
            self.host.run(["systemctl", "reboot"], timeout=30)
            return True
        raise Invalid(f"unknown request {op!r}")

    def close(self) -> None:
        if self.lock is not None: self.lock.close()
        self.lock = None

    def claim(self, label: str) -> dict:
        if self.lock is not None: return {"claimed": True}
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open(self.state / "session.lock", "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            holder = handle.read().strip()
            handle.close()
            return {"claimed": False, "holder": holder or "another session"}
        handle.truncate(0); handle.write(f"{label} since {time.strftime('%H:%M')}\n"); handle.flush()
        self.lock = handle
        return {"claimed": True}


def serve(session: Session, stdin=sys.stdin, stdout=sys.stdout) -> int:
    """A front end that goes away (a dropped SSH login) stops getting
    answers; a running install carries on into its log."""
    gone = []
    def write(message: dict) -> None:
        if gone: return
        try:
            stdout.write(json.dumps(message) + "\n"); stdout.flush()
        except OSError:
            gone.append(True)
    for line in stdin:
        if not line.strip(): continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict): raise Invalid("a request is a JSON object")
        except ValueError as exc:
            write({"id": None, "ok": False, "error": f"unreadable request: {exc}"})
            continue
        try:
            write({"id": request.get("id"), "ok": True, "result": session.handle(request, write)})
        except (Invalid, RuntimeError, OSError, ValueError, KeyError) as exc:
            write({"id": request.get("id"), "ok": False, "error": str(exc) or type(exc).__name__})
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["launch"] and set(argv[1:]) <= {"--ssh"}:
        if os.geteuid() != 0: raise SystemExit("the installer runs as root on the live medium")
        ssh = "--ssh" in argv
        status = launch(Host(), ssh=ssh)
        if ssh:
            # The forced command is the whole login: a shell follows the installer.
            shell = pwd.getpwuid(0).pw_shell or "/bin/sh"
            os.execv(shell, ["-" + Path(shell).name])
        return status
    if argv[:1] == ["serve"]:
        if os.geteuid() != 0: raise SystemExit("the installer core runs as root on the live medium")
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        session = Session(Host())
        try: return serve(session)
        finally: session.close()
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
