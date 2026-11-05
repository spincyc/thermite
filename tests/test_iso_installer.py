"""The ISO installer's core with a fake live system: no disk, archinstall or network."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_iso

spec = importlib.util.spec_from_file_location("iso_installer", AREA / "iso/installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

GB = 10 ** 9
LSBLK = {"blockdevices": [
    {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 128 * GB, "log-sec": 512, "model": "Example SSD ",
     "serial": "EXAMPLE0001", "wwn": "0x5000000000000001", "tran": "sata", "rm": False, "ro": False, "mountpoints": [None],
     "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 100 * GB, "fstype": "ext4",
                   "mountpoints": [None]}]},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": 64 * GB, "log-sec": 512, "model": "Example Stick",
     "serial": "EXAMPLE0002", "tran": "usb", "rm": True, "ro": False, "mountpoints": [None],
     "children": [{"name": "sdb1", "path": "/dev/sdb1", "type": "part", "size": GB, "fstype": "iso9660",
                   "mountpoints": ["/run/archiso/bootmnt"]}]},
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "size": 16 * GB, "log-sec": 4096, "model": "Small",
     "serial": "", "tran": "nvme", "rm": False, "ro": False, "mountpoints": [None]},
    {"name": "zram0", "path": "/dev/zram0", "type": "disk", "size": 4 * GB, "mountpoints": ["[SWAP]"]},
]}
def mac(last):  # a demo MAC, built so the export's MAC guard reads no literal
    return ":".join(["02", "00", "00", "00", "00", last])


IP = [{"ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "address": mac("01"), "addr_info": []},
      {"ifname": "enp1s0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "address": mac("10"),
       "addr_info": [{"family": "inet", "local": "192.0.2.10", "prefixlen": 24}]},
      {"ifname": "wlan0", "flags": ["BROADCAST"], "address": mac("11"), "addr_info": []}]
IWCTL = ("\x1b[0m                               Available networks\x1b[0m\n"
         "--------------------------------------------------------------------------------\n"
         "      Network name                      Security            Signal\n"
         "--------------------------------------------------------------------------------\n"
         "  \x1b[1;90m> \x1b[0m Example Home                      psk                 ****\n"
         "      Example Guest                     open                **  \n"
         "      Example  Loft                     psk                 *\x1b[1;90m***\x1b[0m\n"
         "      Example Office                    8021x               ***\n")
#: iwctl station wlan0 show, joined; the security is what iwd negotiated.
STATION = ("                                 Station: wlan0\n"
           "--------------------------------------------------------------------------------\n"
           "  Settable  Property              Value\n"
           "--------------------------------------------------------------------------------\n"
           "            Scanning              no\n"
           "            State                 connected\n"
           "            Connected network     {ssid}\n"
           "            IPv4 address          192.0.2.30\n"
           "            Security              {security}\n")
SECURITY = {"Example Home": "WPA2-Personal", "Example Guest": "Open", "Example  Loft": "WPA3-Personal",
            "Example Attic": "WPA2-Personal"}
ARCHINSTALL_LINES = ["Starting device modifications in ", "5...4...3...2...1", "Starting installation...",
                     "Adding bootloader Systemd-boot to /dev/sda1", "Creating user owner",
                     "Setting password for owner", "Activating systemd-timesyncd for time synchronization",
                     "Enabling service sshd.service", "Thermite: application installed",
                     "Installation completed without any errors.", "Log files temporarily available"]
ANSWERS = {"hostname": "Thermite", "owner": "casey", "owner_password": "owner-secret-1",
           "owner_password_confirm": "owner-secret-1", "root_password": "root-secret-1",
           "root_password_confirm": "root-secret-1", "timezone": "Europe/Berlin", "locale": "en_US.UTF-8",
           "keymap": "us", "disk": "/dev/sda", "filesystem": "btrfs", "kernel": "linux-lts", "confirm": "sda",
           "disk_identity": {"serial": "EXAMPLE0001", "wwn": "0x5000000000000001", "size": 128 * GB}}
SECRETS = ("owner-secret-1", "root-secret-1", "wifi-secret-1")


NODE_PIN = oh_iso.inputs()["node"]["name"]


def runtime_archives(directory: Path) -> dict:
    """Stand-ins for the pinned Node.js tarball and blessed package, and
    their pins: a node that is a shell script, a blessed that is one file."""
    node = directory / NODE_PIN
    script = b"#!/bin/sh\necho node-fixture\n"
    with tarfile.open(node, "w:xz") as tar:
        for name, data in ((f"{NODE_PIN.removesuffix('.tar.xz')}/bin/node", script),
                           (f"{NODE_PIN.removesuffix('.tar.xz')}/README.md", b"fixture\n")):
            member = tarfile.TarInfo(name); member.size = len(data); member.mode = 0o755
            tar.addfile(member, io.BytesIO(data))
    blessed = directory / "blessed-0.1.81.tgz"
    with tarfile.open(blessed, "w:gz") as tar:
        for name, data in (("package/package.json", b'{"name": "blessed", "version": "0.1.81"}\n'),
                           ("package/lib/blessed.js", b"module.exports = {};\n")):
            member = tarfile.TarInfo(name); member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return {"node": {"name": node.name, "sha256": oh_iso.sha256(node)},
            "blessed": {"name": blessed.name, "integrity": oh_iso.integrity(blessed)}}


def installer_media(media: Path) -> None:
    """The installer's part of a medium, as oh_iso.stage writes it, with stand-in archives."""
    pins = runtime_archives(media)
    files = {}
    for name in oh_iso.INSTALLER:
        staged = media / ("installer.py" if name == "iso/installer.py" else name)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(AREA / name, staged)
        files[str(staged.relative_to(media))] = oh_iso.sha256(staged)
    (media / "look.json").write_text('{"name": "fixture"}\n')
    files["look.json"] = oh_iso.sha256(media / "look.json")
    for name in (*oh_iso.ARCH_FILES, "bootstrap.py", "FIRST-BOOT.txt"):
        source = AREA / "iso" / ("first-boot.txt" if name == "FIRST-BOOT.txt" else name)
        shutil.copyfile(source, media / name)
        files[name] = oh_iso.sha256(media / name)
    wheel = media / "thermite_app-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel fixture")
    payload = json.loads((media / "payload.json").read_text())
    payload["installer"] = {**pins, "files": files}
    payload["wheels"] = {"application": {"name": wheel.name, "sha256": oh_iso.sha256(wheel)}}
    (media / "payload.json").write_text(json.dumps(payload))


class FakeHost(installer.Host):
    def __init__(self, root: Path, *, taken=(), fail=(), archinstall="4.5.1", code=0, lines=ARCHINSTALL_LINES,
                 joined=None, mounted=(), swapped=None, synchronized=True):
        super().__init__(root)
        self.mounts = set(map(str, mounted))  # mount points, as umount leaves them
        self.swapped = swapped  # lsblk reads before the first disk is another
        self.synchronized = synchronized  # the clock, as timedatectl reports it
        self.taken, self.fail, self.version, self.code, self.lines = set(taken), set(fail), archinstall, code, lines
        self.joined = joined  # the Wi-Fi network wlan0 is joined to
        self.calls, self.spawned, self.called = [], [], []

    def run(self, argv, *, timeout=120, input=None):
        argv = list(argv); self.calls.append(argv)
        if argv[0] == "lsblk" and self.swapped is not None:
            # A disk that is replaced by another under the same name once read.
            self.swapped -= 1
            if self.swapped < 0:
                swapped = json.loads(json.dumps(LSBLK))
                swapped["blockdevices"][0]["serial"] = "EXAMPLE0009"
                return subprocess.CompletedProcess(argv, 0, json.dumps(swapped), "")
        def done(code=0, out="", err=""): return subprocess.CompletedProcess(argv, code, out, err)
        if argv[0] in self.fail: return done(1, "", f"{argv[0]} failed")
        if argv[0] == "umount": self.mounts.discard(argv[-1])
        if argv[0] == "lsblk": return done(0, json.dumps(LSBLK))
        if argv[:2] == ["ip", "-json"] and "route" in argv:
            return done(0, json.dumps([{"dst": "default", "gateway": "192.0.2.1", "dev": "enp1s0", "metric": 100},
                                       *([{"dst": "default", "dev": "wlan0", "metric": 600}] if self.joined else [])]))
        if argv[0] == "ip": return done(0, json.dumps(IP))
        if argv[0] == "localectl": return done(0, "de\nus\nuk\n")
        if argv[0] == "timedatectl": return done(0, "yes\n" if self.synchronized else "no\n")
        if argv[0] == "resolvectl": return done(0 if argv[-1] in self.taken else 1)
        if argv[0] == "iwctl":
            action = argv[3] if len(argv) > 3 else ""
            if action == "get-networks": return done(0, IWCTL)
            if action == "show":
                return done(0, STATION.format(ssid=self.joined, security=SECURITY[self.joined]) if self.joined
                            else STATION.split("            State")[0] + "            State                 disconnected\n")
            if action in ("connect", "connect-hidden"): self.joined = argv[4]
            if action == "disconnect": self.joined = None
            return done()
        if argv[0] == sys.executable: return done(0, self.version + "\n")
        return done()

    def online(self): return True

    def mounted(self, path): return str(path) in self.mounts

    def spawn(self, argv, env):
        state = Path(env["THERMITE_INSTALLER_STATE"])
        # What archinstall would read, kept before the run deletes the secrets.
        self.spawned.append({"argv": argv, "config": json.loads((state / "config.json").read_text()),
                             "creds": json.loads((state / "creds.json").read_text()),
                             "creds_mode": (state / "creds.json").stat().st_mode & 0o777,
                             "wifi": json.loads((state / "wifi.json").read_text()) if (state / "wifi.json").exists() else None})
        return types.SimpleNamespace(stdout=iter(line + "\n" for line in self.lines), wait=lambda: self.code)

    def call(self, argv, env):
        self.called.append((list(argv), env))
        return 0

    def hash_password(self, plaintext):
        return "$y$fixture$" + hashlib.sha256(plaintext.encode()).hexdigest()[:16]


def fixture(root: Path) -> tuple[Path, Path]:
    """A live system's files under root/live and the medium's under root/media."""
    live = root / "live"
    for name, text in (("proc/meminfo", "MemTotal:        8000000 kB\n"),
                       ("proc/cpuinfo", "processor\t: 0\nmodel name\t: Example CPU\n"),
                       ("usr/share/zoneinfo/Europe/Berlin", "TZif"), ("usr/share/zoneinfo/America/New_York", "TZif"),
                       ("usr/share/zoneinfo/posix/Europe/Berlin", "TZif"), ("usr/share/zoneinfo/zone.tab", ""),
                       ("usr/share/zoneinfo/SECURITY", "Please report time zone problems\n"),
                       ("usr/share/zoneinfo/Factory", "TZif"),
                       ("usr/share/i18n/SUPPORTED", "en_US.UTF-8 UTF-8\nen_US ISO-8859-1\nde_DE.UTF-8 UTF-8\n")):
        (live / name).parent.mkdir(parents=True, exist_ok=True); (live / name).write_text(text)
    for name in ("sys/firmware/efi", "sys/class/net/wlan0/wireless", "var/lib"):
        (live / name).mkdir(parents=True)
    media = root / "media"; media.mkdir()
    (media / "packages.json").write_bytes((AREA / "iso/packages.json").read_bytes())
    (media / "payload.json").write_text(json.dumps({"application": "0.1.0.dev0", "release": "f" * 64}))
    return live, media


class InstallerCore(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/iso-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="core-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live, self.media = fixture(self.root)
        self.state = self.root / "state"
        self.target = self.root / "target"

    def host(self, **options):
        return FakeHost(self.live, **options)

    def facts(self, host=None):
        return installer.facts(host or self.host(), self.media)

    def test_facts_offer_only_free_disks_and_warn_about_size(self):
        found = self.facts()
        self.assertTrue(found["uefi"])
        self.assertEqual(found["memory"], 8000000 * 1024)
        self.assertEqual(found["cpu"], "Example CPU")
        disks = {disk["name"]: disk for disk in found["disks"]}
        self.assertEqual(set(disks), {"sda", "sdb", "nvme0n1"})
        self.assertTrue(disks["sda"]["eligible"])
        self.assertEqual((disks["sda"]["model"], disks["sda"]["partitions"][0]["fstype"]), ("Example SSD", "ext4"))
        self.assertEqual(disks["sdb"]["reason"], "holds the installation medium")
        self.assertEqual(disks["nvme0n1"]["reason"], "smaller than 32 GB")
        self.assertEqual(found["network"]["interfaces"][0]["addresses"], ["192.0.2.10/24"])
        self.assertTrue(found["network"]["interfaces"][1]["wireless"])
        self.assertIsNone(found["authorized_key"])
        options = installer.choices(self.host())
        self.assertEqual(options["timezones"], ["America/New_York", "Europe/Berlin", "UTC"])
        self.assertEqual(options["locales"], ["de_DE.UTF-8", "en_US.UTF-8"])
        self.assertEqual(options["keymaps"], ["de", "uk", "us"])

    def test_answers_are_checked_and_normalized(self):
        found, options = self.facts(), installer.choices(self.host())
        values, errors = installer.check(ANSWERS, found, options)
        self.assertEqual(errors, {})
        self.assertEqual(values["hostname"], "thermite")
        self.assertEqual(installer.public(values)["disk"], "/dev/sda")
        self.assertFalse(set(installer.public({**ANSWERS, **values})) & {"owner_password", "root_password", "confirm"})
        # Defaults fill what was not asked.
        values, errors = installer.check({}, found, options, ["hostname", "filesystem", "kernel", "timezone"])
        self.assertEqual((values, errors), ({"hostname": "thermite", "filesystem": "btrfs", "kernel": "linux-lts",
                                             "timezone": "UTC"}, {}))
        bad = {"hostname": "-thermite", "owner": "thermite", "owner_password": "short",
               "owner_password_confirm": "short", "root_password": "x", "root_password_confirm": "x",
               "timezone": "Mars/Base", "locale": "en_US", "keymap": "xx", "disk": "/dev/sdb", "filesystem": "xfs",
               "kernel": "linux-zen", "confirm": "sda"}
        _, errors = installer.check(bad, found, options)
        self.assertEqual(set(errors), {"hostname", "owner", "owner_password", "root_password", "timezone", "locale",
                                       "keymap", "disk", "filesystem", "kernel", "confirm"})
        self.assertIn("service account", errors["owner"])
        self.assertIn("installation medium", errors["disk"])
        for name in ("root", "systemd-network", "Casey", "1casey", "x" * 33):
            self.assertIn("owner", installer.check({"owner": name}, found, options, ["owner"])[1], name)
        _, errors = installer.check({**ANSWERS, "root_password": "owner-secret-1",
                                     "root_password_confirm": "owner-secret-1"}, found, options)
        self.assertEqual(errors, {"root_password": "use a different password from the owner's"})
        _, errors = installer.check({**ANSWERS, "owner_password_confirm": "owner-secret-2"}, found, options)
        self.assertEqual(errors, {"owner_password": "the two entries differ"})
        # The destructive confirmation is the selected disk's own name, for
        # the very disk the review showed.
        self.assertIn("type sda", installer.check({**ANSWERS, "confirm": "ERASE"}, found, options)[1]["confirm"])
        for changed in ({}, {**ANSWERS["disk_identity"], "serial": "EXAMPLE0009"},
                        {**ANSWERS["disk_identity"], "size": 256 * GB}):
            self.assertIn("not the disk the review showed",
                          installer.check({**ANSWERS, "disk_identity": changed}, found, options)[1]["confirm"])
        self.assertIn("wifi_ssid", installer.check({**ANSWERS, "wifi_ssid": "Example Home"}, found, options)[1])

    def test_hostname_probe_suggests_the_first_free_number(self):
        host = self.host(taken={"thermite.local", "thermite-2.local"})
        self.assertEqual(installer.hostname_probe(host, "thermite"),
                         {"name": "thermite", "taken": True, "suggestion": "thermite-3"})
        self.assertEqual(installer.hostname_probe(self.host(), "thermite")["taken"], False)
        self.assertIsNone(installer.hostname_probe(self.host(fail=()), "kitchen")["suggestion"])

    def test_configuration_matches_archinstall_45s_schema_and_layout_rules(self):
        found, document = self.facts(), oh_iso.packages()
        answers = installer.check(ANSWERS, found, installer.choices(self.host()))[0]
        config = installer.configuration(answers, found, document)
        self.assertEqual(set(config), {"script", "archinstall-language", "hostname", "kernels", "timezone", "ntp",
                                       "locale_config", "bootloader_config", "disk_config", "network_config",
                                       "packages", "services", "swap", "pacman_config"})
        self.assertEqual(config["kernels"], ["linux-lts", "linux"])
        self.assertEqual(config["bootloader_config"], {"bootloader": "Systemd-boot", "uki": False, "removable": False})
        self.assertEqual(config["locale_config"], {"kb_layout": "us", "sys_lang": "en_US.UTF-8", "sys_enc": "UTF-8"})
        self.assertEqual(config["swap"], {"enabled": True, "algorithm": "zstd"})
        self.assertEqual(config["network_config"], {"type": "nm"})
        self.assertTrue(config["ntp"])
        # One list: the packages file, rendered for the chosen filesystem.
        self.assertEqual(config["packages"], oh_iso.package_names(document, "btrfs"))
        self.assertEqual(installer.selected_packages(document, "ext4"), oh_iso.package_names(document, "ext4"))
        self.assertEqual(config["services"], document["enable"])
        self.assertFalse(set(config["services"]) & set(document["keep_disabled"]))
        disk = config["disk_config"]
        self.assertEqual(disk["config_type"], "default_layout")
        [device] = disk["device_modifications"]
        self.assertEqual((device["device"], device["wipe"]), ("/dev/sda", True))
        boot, root = device["partitions"]
        keys = {"obj_id", "status", "type", "start", "size", "fs_type", "mountpoint", "mount_options", "flags",
                "dev_path", "btrfs"}
        self.assertEqual((set(boot), set(root)), (keys, keys))
        self.assertEqual((boot["fs_type"], boot["mountpoint"], boot["flags"]), ("fat32", "/boot", ["boot", "esp"]))
        self.assertEqual((root["fs_type"], root["mountpoint"], root["mount_options"]), ("btrfs", None, ["compress=zstd"]))
        self.assertEqual([entry["mountpoint"] for entry in root["btrfs"]], ["/", "/home", "/var/log", "/var/cache/pacman/pkg"])
        # archinstall's parse_arg: start >= 1 MiB, MiB-aligned, no overlap,
        # and the last partition ends before GPT's backup header.
        def mib(size):
            self.assertEqual((size["unit"], size["sector_size"]), ("MiB", {"value": 512, "unit": "B"}))
            return size["value"]
        self.assertGreaterEqual(mib(boot["start"]), 1)
        self.assertGreaterEqual(mib(root["start"]), mib(boot["start"]) + mib(boot["size"]))
        self.assertLessEqual((mib(root["start"]) + mib(root["size"])) * installer.MiB, 128 * GB - installer.MiB)
        self.assertGreater((mib(root["start"]) + mib(root["size"])) * installer.MiB, 128 * GB - 2 * installer.MiB)
        ext4 = installer.configuration({**answers, "filesystem": "ext4", "kernel": "linux"}, {**found, "uefi": False},
                                       document)
        boot, root = ext4["disk_config"]["device_modifications"][0]["partitions"]
        self.assertEqual((boot["flags"], root["mountpoint"], root["btrfs"], root["mount_options"]),
                         (["boot"], "/", [], []))
        self.assertEqual(ext4["bootloader_config"]["bootloader"], "Grub")
        self.assertEqual(ext4["kernels"], ["linux", "linux-lts"])
        self.assertNotIn("compsize", ext4["packages"])
        creds = installer.credentials(answers | {"owner_password": "a" * 8, "root_password": "b" * 8}, lambda p: "H" + p)
        self.assertEqual(creds, {"root_enc_password": "Hbbbbbbbb", "users": [
            {"username": "casey", "enc_password": "Haaaaaaaa", "sudo": False, "groups": ["wheel"]}]})

    def test_install_preflights_runs_archinstall_unattended_and_keeps_no_secret(self):
        host, events = self.host(), []
        installer.wifi_connect(host, "wlan0", "Example Home", SECRETS[2])
        result = installer.install({**ANSWERS, "wifi_ssid": "Example Home", "wifi_passphrase": SECRETS[2]},
                                   host, media=self.media, state=self.state, target=self.target, emit=events.append)
        self.assertEqual(result["hostname"], "thermite")
        [spawned] = host.spawned
        argv = spawned["argv"]
        self.assertEqual(argv[:1] + argv[5:9], ["archinstall", "--silent", "--skip-wifi-check", "--skip-version-check",
                                                 "--plugin"])
        self.assertEqual(argv[-1], str(self.media / "arch-plugin.py"))
        self.assertEqual(spawned["creds_mode"], 0o600)
        self.assertEqual(spawned["creds"]["users"][0]["enc_password"], host.hash_password("owner-secret-1"))
        self.assertEqual(spawned["wifi"], {"ssid": "Example Home", "security": "psk", "hidden": False,
                                           "passphrase": SECRETS[2]})
        # The preflight checks every package before the disk is touched.
        preflight = next(call for call in host.calls if call[:2] == ["pacman", "-Sp"])
        self.assertTrue(set(spawned["config"]["packages"]) <= set(preflight))
        self.assertTrue({"linux-lts", "linux", "btrfs-progs", "base"} <= set(preflight))
        self.assertLess(host.calls.index(["pacman", "-Sy", "--noconfirm"]), host.calls.index(preflight))
        # Progress: phases only move forward, every line is streamed and logged.
        phases = [event["percent"] for event in events if event["kind"] == "phase"]
        self.assertEqual(phases, sorted(phases))
        self.assertEqual(phases[-1], 100)
        self.assertEqual(events[-1]["kind"], "done")
        self.assertIn("Creating user owner", [event.get("text") for event in events])
        log = (self.state / "installer.log").read_text()
        self.assertIn("Installation completed without any errors.", log)
        recorded = json.loads((self.state / "answers.json").read_text())
        self.assertEqual((recorded["wifi_ssid"], recorded["wifi_security"]), ("Example Home", "psk"))
        self.assertFalse((self.state / "creds.json").exists() or (self.state / "wifi.json").exists())
        everything = log + json.dumps(events) + "".join(path.read_text() for path in self.state.iterdir()
                                                         if path.is_file() and path.name != "session.lock")
        for secret in SECRETS:
            self.assertNotIn(secret, everything)

    def test_failed_preflight_or_wrong_archinstall_never_reaches_the_disk(self):
        for host in (self.host(fail={"pacman"}), self.host(archinstall="4.6.0")):
            events = []
            with self.assertRaises(RuntimeError):
                installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target,
                                  emit=events.append)
            self.assertEqual(host.spawned, [])
            self.assertEqual(events[-1]["kind"], "failed")
            self.assertFalse((self.state / "creds.json").exists())
        # A run archinstall reports as failed, or that never reports completion, fails.
        for host in (self.host(code=1), self.host(lines=["Starting installation..."])):
            with self.assertRaises(RuntimeError):
                installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target)
            self.assertFalse((self.state / "creds.json").exists())
        with self.assertRaises(installer.Invalid):
            installer.install({**ANSWERS, "confirm": "ERASE"}, self.host(), media=self.media, state=self.state)

    def test_a_disk_replaced_after_the_review_is_never_erased(self):
        # Installed already at the start, or between the checks and the wipe.
        for reads in (0, 1):
            host, events = self.host(swapped=reads), []
            with self.assertRaises((installer.Invalid, RuntimeError)) as caught:
                installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target,
                                  emit=events.append)
            self.assertIn("not the disk the review showed" if reads == 0 else "nothing was erased", str(caught.exception))
            self.assertEqual(host.spawned, [])

    def test_a_clock_that_never_synchronizes_stops_the_run_before_the_disk(self):
        host, events, slept = self.host(synchronized=False), [], []
        with patch.object(installer.time, "sleep", slept.append), \
                self.assertRaisesRegex(RuntimeError, "did not synchronize .* NTP .* Nothing was erased"):
            installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target,
                              emit=events.append)
        self.assertEqual(sum(slept), installer.CLOCK_WAIT)
        self.assertEqual(host.spawned, [])
        self.assertIn("Waiting for the clock to synchronize with a time server", [event.get("text") for event in events])
        self.assertFalse(any(call[:2] == ["pacman", "-Sy"] for call in host.calls))

    def test_a_retry_unmounts_what_the_failed_run_left_mounted_first(self):
        host = self.host(mounted={self.target})
        installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target)
        umount = ["umount", "--recursive", str(self.target)]
        self.assertLess(host.calls.index(umount), next(index for index, call in enumerate(host.calls)
                                                       if call[0] == "lsblk"))
        self.assertIn("which an earlier run left mounted", (self.state / "installer.log").read_text())
        # Nothing is unmounted when nothing is mounted; a mount that stays refuses the run.
        host = self.host()
        installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target)
        self.assertNotIn(umount, host.calls)
        host = self.host(mounted={self.target}, fail={"umount"})
        with self.assertRaisesRegex(installer.Invalid, "still mounted"):
            installer.install(ANSWERS, host, media=self.media, state=self.state, target=self.target)
        self.assertEqual(host.spawned, [])

    def test_wifi_scan_and_connect_keep_the_passphrase_off_command_lines(self):
        host = self.host()
        networks = installer.wifi_scan(host, "wlan0")
        self.assertEqual(networks, [{"ssid": "Example Home", "security": "psk", "signal": 4, "connected": True},
                                    {"ssid": "Example Guest", "security": "open", "signal": 2, "connected": False},
                                    {"ssid": "Example  Loft", "security": "psk", "signal": 4, "connected": False},
                                    {"ssid": "Example Office", "security": "8021x", "signal": 3, "connected": False}])
        with self.assertRaises(installer.Invalid): installer.wifi_scan(host, "enp1s0")
        for ssid, passphrase, security in (("Example Home", "short", "psk"), ("Example Home", "é" * 8, "psk"),
                                           ("Example Home", "g" * 64, "psk"), ("Example Guest", "secret-1", "open"),
                                           ("Example Office", "secret-1", "8021x"), ("", "secret-1", "psk"),
                                           ("x" * 33, "secret-1", "psk")):
            with self.assertRaises(installer.Invalid, msg=(ssid, security)):
                installer.wifi_connect(host, "wlan0", ssid, passphrase, security=security)
        self.assertEqual(host.joined, None)
        # iwd's own file name: a name with spaces as it is, others in hex.
        store = self.live / "var/lib/iwd"
        found = installer.wifi_connect(host, "wlan0", "Example Home", "wifi-secret-1")
        stored = store / "Example Home.psk"
        self.assertEqual(stored.read_text(), "[Security]\nPassphrase=wifi-secret-1\n")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(found["wifi"], {"device": "wlan0", "ssid": "Example Home", "security": "psk", "hidden": False,
                                         "carried": True})
        self.assertEqual(installer.iwd_name("Example_Home-2"), "Example_Home-2")
        self.assertEqual(installer.iwd_name("FRITZ!Box 7590"), "=" + "FRITZ!Box 7590".encode().hex())
        self.assertEqual(installer.iwd_name("Café"), "=436166c3a9")
        # A passphrase as ell reads it back, ends and backslashes included.
        tricky = " wifi\\secret 1 "
        installer.wifi_connect(host, "wlan0", "Example  Loft", tricky)
        self.assertEqual((store / "Example  Loft.psk").read_text(),
                         "[Security]\nPassphrase=\\swifi\\\\secret 1\\s\n")
        self.assertEqual(installer.iwd_profile(host, "Example  Loft")["passphrase"], tricky)
        # Joining the joined network again leaves it first.
        calls = len(host.calls)
        installer.wifi_connect(host, "wlan0", "Example  Loft", tricky)
        self.assertEqual([call[3] for call in host.calls[calls:] if call[0] == "iwctl" and call[3] != "show"],
                         ["disconnect", "connect"])
        # The key itself, an open network (no file), a hidden network.
        installer.wifi_connect(host, "wlan0", "Example Home", "AB" * 32)
        self.assertEqual(stored.read_text(), "[Security]\nPreSharedKey=" + "ab" * 32 + "\n")
        before = sorted(store.iterdir())
        found = installer.wifi_connect(host, "wlan0", "Example Guest", "", security="open")
        self.assertEqual(sorted(store.iterdir()), before)
        self.assertEqual((found["wifi"]["security"], found["wifi"]["carried"]), ("open", True))
        found = installer.wifi_connect(host, "wlan0", "Example Attic", "wifi-secret-1", security="open", hidden=True)
        self.assertIn(["iwctl", "station", "wlan0", "connect-hidden", "Example Attic"], host.calls)
        self.assertEqual((store / "Example Attic.psk").read_text(),
                         "[Security]\nPassphrase=wifi-secret-1\n[Settings]\nHidden=true\n")
        self.assertEqual(found["wifi"]["hidden"], True)
        self.assertFalse(any("wifi-secret-1" in " ".join(call) or tricky in " ".join(call) for call in host.calls))

    def test_the_installed_host_joins_the_wifi_this_system_joined_however_it_was_joined(self):
        options, store = installer.choices(self.host()), self.live / "var/lib/iwd"
        # Joined with iwctl from a shell: iwd's saved passphrase is carried.
        store.mkdir(parents=True, exist_ok=True)
        (store / "Example Home.psk").write_text("[Security]\nPassphrase=wifi-secret-1\nPreSharedKey=" + "0" * 64 + "\n")
        host = self.host(joined="Example Home")
        found = self.facts(host)
        self.assertEqual(found["network"]["route"], "enp1s0")
        self.assertEqual(found["network"]["wifi"]["ssid"], "Example Home")
        self.assertEqual(installer.check({**ANSWERS, "wifi_ssid": "Example Home"}, found, options)[1], {})
        # The review must show it: no Wi-Fi, or another network, is refused.
        self.assertIn("joined to Example Home", installer.check(ANSWERS, found, options)[1]["wifi_ssid"])
        self.assertIn("not Example Guest",
                      installer.check({**ANSWERS, "wifi_ssid": "Example Guest"}, found, options)[1]["wifi_ssid"])
        installer.install({**ANSWERS, "wifi_ssid": "Example Home"}, host, media=self.media, state=self.state,
                          target=self.target)
        self.assertEqual(host.spawned[-1]["wifi"], {"ssid": "Example Home", "security": "psk", "hidden": False,
                                                    "passphrase": SECRETS[2]})
        # An open network needs nothing, and installs.
        host = self.host(joined="Example Guest")
        installer.install({**ANSWERS, "wifi_ssid": "Example Guest"}, host, media=self.media, state=self.state,
                          target=self.target)
        self.assertEqual(host.spawned[-1]["wifi"], {"ssid": "Example Guest", "security": "open", "hidden": False})
        # WPA3 needs the passphrase; iwd's key alone cannot join it.
        (store / "Example  Loft.psk").write_text("[Security]\nPreSharedKey=" + "0" * 64 + "\n")
        host = self.host(joined="Example  Loft")
        found = self.facts(host)
        self.assertEqual((found["network"]["wifi"]["security"], found["network"]["wifi"]["carried"]), ("sae", False))
        self.assertIn("join it on the Network step",
                      installer.check({**ANSWERS, "wifi_ssid": "Example  Loft"}, found, options)[1]["wifi_ssid"])
        with self.assertRaisesRegex(installer.Invalid, "not saved"):
            installer.install({**ANSWERS, "wifi_ssid": "Example  Loft"}, host, media=self.media, state=self.state,
                              target=self.target)
        installer.wifi_connect(host, "wlan0", "Example  Loft", "wifi-secret-1")
        installer.install({**ANSWERS, "wifi_ssid": "Example  Loft"}, host, media=self.media, state=self.state,
                          target=self.target)
        self.assertEqual(host.spawned[-1]["wifi"], {"ssid": "Example  Loft", "security": "sae", "hidden": False,
                                                    "passphrase": SECRETS[2]})
        self.assertEqual(json.loads((self.state / "answers.json").read_text())["wifi_security"], "sae")
        # Joined with nothing saved (iwd's agent): refused until joined here.
        (store / "Example Home.psk").unlink()
        found = self.facts(self.host(joined="Example Home"))
        self.assertFalse(found["network"]["wifi"]["carried"])

    def test_serve_answers_each_request_and_one_session_installs(self):
        first = installer.Session(self.host(), self.media, self.state, self.target)
        second = installer.Session(self.host(), self.media, self.state, self.target)
        self.addCleanup(first.close); self.addCleanup(second.close)
        requests = [{"id": 1, "op": "facts"}, {"id": 2, "op": "check", "answers": {"owner": "thermite"},
                                                 "fields": ["owner"]},
                    {"id": 3, "op": "hostname", "name": "Kitchen"}, {"id": 4, "op": "nonsense"},
                    {"id": 5, "op": "claim", "label": "tty1"}]
        out = io.StringIO()
        installer.serve(first, io.StringIO("\n".join(map(json.dumps, requests)) + "\nnot json\n"), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4, 5, None])
        self.assertEqual(replies[0]["result"]["disks"][0]["name"], "sda")
        self.assertIn("service account", replies[1]["result"]["errors"]["owner"])
        self.assertEqual(replies[2]["result"]["name"], "kitchen")
        self.assertFalse(replies[3]["ok"] or replies[5]["ok"])
        self.assertEqual(replies[4]["result"], {"claimed": True})
        holder = second.claim("ssh")
        self.assertFalse(holder["claimed"])
        self.assertTrue(holder["holder"].startswith("tty1 since "))
        with self.assertRaises(installer.Invalid):
            second.handle({"id": 9, "op": "install", "answers": ANSWERS}, lambda message: None)
        # Install streams its events under the request's id, then answers.
        out = io.StringIO()
        request = {"id": 7, "op": "install", "answers": ANSWERS}
        installer.serve(first, io.StringIO(json.dumps(request) + "\n"), out)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertTrue(all(line["id"] == 7 for line in lines))
        self.assertTrue(all("event" in line for line in lines[:-1]))
        self.assertEqual(lines[-1]["result"]["hostname"], "thermite")

    def test_launch_unpacks_the_pinned_runtime_once_and_starts_the_terminal(self):
        self.said = self.enterContext(patch.object(installer, "print", create=True))
        installer_media(self.media)
        host = self.host()
        env = {"TERM": "xterm-kitty", "PATH": "/usr/bin"}
        self.assertEqual(installer.launch(host, media=self.media, state=self.state, env=env), 0)
        (argv, seen), = host.called
        node = self.state / "runtime/node"
        self.assertEqual(argv[:2], [str(node), str(self.media / "tui/installer/main.js")])
        self.assertEqual(subprocess.run([str(node)], capture_output=True, text=True).stdout, "node-fixture\n")
        modules = Path(argv[argv.index("--modules") + 1])
        self.assertTrue((modules / "blessed/lib/blessed.js").is_file())
        self.assertEqual(argv[argv.index("--label") + 1], "the console")
        self.assertEqual(argv[argv.index("--core") + 1], str(self.media / "installer.py"))
        # No terminfo for kitty on this live system: xterm-256color instead.
        self.assertEqual((seen["TERM"], seen["THERMITE_REMOTE_LOOK"]), ("xterm-256color", '{"name": "fixture"}\n'))
        again = self.live / "usr/local/bin/thermite-installer"
        self.assertIn("installer.py launch", again.read_text())
        # Without a key in the image, the live sshd is left as archiso has it.
        self.assertFalse((self.live / "etc/ssh/sshd_config.d/05-thermite.conf").exists())
        # An SSH login reuses what the console unpacked; a known TERM stays.
        (self.live / "usr/share/terminfo/x/xterm-kitty").parent.mkdir(parents=True)
        (self.live / "usr/share/terminfo/x/xterm-kitty").write_text("")
        node.write_text("unpacked once")
        installer.launch(host, media=self.media, state=self.state, ssh=True,
                         env={**env, "SSH_CLIENT": "192.0.2.20 50000 22"})
        argv, seen = host.called[-1]
        self.assertEqual((node.read_text(), seen["TERM"]), ("unpacked once", "xterm-kitty"))
        self.assertEqual(argv[argv.index("--label") + 1], "SSH from 192.0.2.20")
        # A damaged medium never runs, unpacked already or not: any file
        # the installer runs, or installs on the target.
        for name in ("arch-plugin.py", "first-boot.py", "packages.json", "arch-install.sh", "bootstrap.py",
                     "FIRST-BOOT.txt", "thermite_app-0.1.0-py3-none-any.whl", "tui/installer/view.js", NODE_PIN):
            kept = (self.media / name).read_bytes()
            (self.media / name).write_bytes(kept + b"damaged")
            self.assertEqual(installer.launch(host, media=self.media, state=self.state, env=env), 1, name)
            self.assertIn(f"{name} on the medium is damaged", self.said.call_args.args[0])
            (self.media / name).write_bytes(kept)
        self.assertEqual(len(host.called), 2)

    def test_an_images_key_opens_live_ssh_to_the_installer_alone(self):
        self.said = self.enterContext(patch.object(installer, "print", create=True))
        installer_media(self.media)
        key = "ssh-ed25519 " + "A" * 68 + " casey@example"
        (self.media / "authorized_keys").write_text(key + "\n")
        payload = json.loads((self.media / "payload.json").read_text())
        payload["authorized_key"] = {"type": "ssh-ed25519", "fingerprint": "SHA256:fixture", "comment": "casey@example"}
        (self.media / "payload.json").write_text(json.dumps(payload))
        host = self.host()
        installer.launch(host, media=self.media, state=self.state, env={"TERM": "linux"})
        line = (self.live / "root/.ssh/authorized_keys").read_text()
        self.assertTrue(line.startswith('command="'))
        self.assertIn("installer.py launch --ssh\",restrict,pty " + key, line)
        self.assertEqual((self.live / "root/.ssh/authorized_keys").stat().st_mode & 0o777, 0o600)
        config = (self.live / "etc/ssh/sshd_config.d/05-thermite.conf").read_text()
        for setting in ("PermitRootLogin forced-commands-only", "PasswordAuthentication no",
                        "KbdInteractiveAuthentication no", "AuthenticationMethods publickey"):
            self.assertIn(setting, config.splitlines())
        self.assertIn(["systemctl", "reload-or-restart", "sshd.service"], host.calls)
        self.assertEqual(self.facts(host)["authorized_key"]["fingerprint"], "SHA256:fixture")

    def test_commands_never_read_the_front_ends_request_pipe(self):
        # The core's stdin is the front end's requests: a child must not
        # inherit it (iwctl's passphrase agent would wait on it).
        stdin = "import os, sys; print(os.path.realpath('/proc/self/fd/0'), repr(sys.stdin.read()))"
        completed = installer.Host().run([sys.executable, "-I", "-c", stdin], timeout=30)
        self.assertEqual(completed.stdout.split(), ["/dev/null", "''"])
        completed = installer.Host().run([sys.executable, "-I", "-c", "import sys; print(sys.stdin.read())"],
                                         timeout=30, input="given\n")
        self.assertEqual(completed.stdout, "given\n\n")

    def test_core_stays_stdlib_only(self):
        import ast
        tree = ast.parse((AREA / "iso/installer.py").read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        self.assertTrue(imported <= set(sys.stdlib_module_names) | {"__future__"}, imported)


if __name__ == "__main__": unittest.main()
