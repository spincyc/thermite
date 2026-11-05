"""What the ISO installer leaves on the installed system: the archinstall plugin
and the first boot, on fake targets; no chroot, sudo, systemd or network."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, AREA / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load("iso_arch_plugin", "iso/arch-plugin.py")
first_boot = load("iso_first_boot", "iso/first-boot.py")

ANSWERS = {"hostname": "thermite", "owner": "casey", "timezone": "Europe/Berlin", "locale": "en_US.UTF-8",
           "keymap": "us", "disk": "/dev/sda", "filesystem": "btrfs", "kernel": "linux-lts", "wifi_ssid": "Example Home"}
HIDDEN = "wifi-secret-1"  # the Wi-Fi passphrase, kept off every record
WIFI = {"ssid": "Example Home", "security": "psk", "hidden": False, "passphrase": HIDDEN}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f casey@example\n"
ACCOUNTS = "etc/passwd"
NSSWITCH = ("passwd: files systemd\n"
            "hosts: mymachines resolve [!UNAVAIL=return] files myhostname dns\n"
            "networks: files\n")


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/iso-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="target-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def tree(self, base: Path, files: dict) -> Path:
        for name, text in files.items():
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_text(text)
        return base


class Plugin(Scratch):
    def setUp(self):
        super().setUp()
        self.target = self.tree(self.root / "target", {
            "etc/os-release": "NAME=\"Arch Linux\"\nID=arch\n",
            ACCOUNTS: "root:x:0:0::/root:/bin/bash\ncasey:x:1000:1000::/home/casey:/bin/bash\n",
            "etc/shadow": "root:$y$fixture:1::::::\ncasey:$y$fixture:1::::::\n",
            "etc/group": "wheel:x:998:casey\n",
            "etc/nsswitch.conf": NSSWITCH,
            "etc/ssh/sshd_config": "#Port 22\nAuthorizedKeysFile .ssh/authorized_keys\n",
            "home/casey/.bashrc": ""})
        wants = self.target / "etc/systemd/system/multi-user.target.wants"
        wants.mkdir(parents=True)
        for unit in ("sshd.service", "nftables.service", "avahi-daemon.service"):
            (wants / unit).symlink_to(f"/usr/lib/systemd/system/{unit}")
        self.media = self.tree(self.root / "media", {
            "payload.json": json.dumps({"application": "0.1.0.dev0", "release": "f" * 64,
                                        "wheels": {"application": {"name": "thermite_app-0.1.0-py3-none-any.whl"}},
                                        "installer": {"node": {"name": "node.tar.xz"}}}),
            "bootstrap.py": "", "FIRST-BOOT.txt": "guide\n", "thermite_app-0.1.0-py3-none-any.whl": "",
            "node.tar.xz": "", "authorized_keys": KEY,
            "packages.json": (AREA / "iso/packages.json").read_text(),
            "first-boot.py": (AREA / "iso/first-boot.py").read_text()})
        self.state = self.tree(self.root / "state", {"answers.json": json.dumps(ANSWERS),
                                                     "wifi.json": json.dumps(WIFI)})

    def finalize(self):
        out = io.StringIO()
        with patch.object(Path, "is_mount", return_value=True), \
                patch.object(plugin.subprocess, "run") as run, patch.object(plugin, "own") as own, redirect_stdout(out):
            plugin.finalize(self.target, self.media, self.state)
        return [call.args[0] for call in run.call_args_list], own, out.getvalue()

    def test_target_gets_the_application_settings_records_and_first_boot(self):
        (self.target / "etc/default").mkdir()
        (self.target / "etc/default/grub").write_text('GRUB_DEFAULT=0\nGRUB_TOP_LEVEL="/boot/vmlinuz-linux"\n')
        commands, own, said = self.finalize()
        target = self.target
        # The application from the medium, without the installer's runtime.
        self.assertEqual(sorted(path.name for path in (target / "opt/thermite-media").iterdir()),
                         ["FIRST-BOOT.txt", "bootstrap.py", "payload.json", "thermite_app-0.1.0-py3-none-any.whl"])
        chroot = ["arch-chroot", str(target)]
        self.assertEqual(commands, [[*chroot, "/usr/bin/python3", "/opt/thermite-media/bootstrap.py"],
                                    [*chroot, "visudo", "-cf", "/etc/sudoers.d/10-thermite-wheel"],
                                    [*chroot, "grub-mkconfig", "-o", "/boot/grub/grub.cfg"]])
        sudoers = target / "etc/sudoers.d/10-thermite-wheel"
        self.assertIn("%wheel ALL=(ALL:ALL) ALL", sudoers.read_text().splitlines())
        self.assertEqual(sudoers.stat().st_mode & 0o777, 0o440)
        sshd = (target / "etc/ssh/sshd_config.d/20-thermite.conf").read_text().splitlines()
        for setting in ("PermitRootLogin no", "PasswordAuthentication no", "KbdInteractiveAuthentication no"):
            self.assertIn(setting, sshd)
        self.assertEqual((target / "etc/ssh/sshd_config").read_text().splitlines()[0],
                         "Include /etc/ssh/sshd_config.d/*.conf")
        keys = target / "home/casey/.ssh/authorized_keys"
        self.assertEqual((keys.read_text(), keys.stat().st_mode & 0o777), (KEY, 0o600))
        self.assertEqual({call.args for call in own.call_args_list},
                         {(keys.parent, 1000, 1000), (keys, 1000, 1000)})
        self.assertIn("hosts: mymachines mdns_minimal [NOTFOUND=return] resolve [!UNAVAIL=return] files myhostname dns",
                      (target / "etc/nsswitch.conf").read_text().splitlines())
        self.assertIn("SystemMaxUse=2G", (target / "etc/systemd/journald.conf.d/50-thermite.conf").read_text())
        grub = (target / "etc/default/grub").read_text().splitlines()
        self.assertEqual([line for line in grub if line.startswith("GRUB_TOP_LEVEL")],
                         ['GRUB_TOP_LEVEL="/boot/vmlinuz-linux-lts"'])
        profile = target / "etc/NetworkManager/system-connections/Example_Home.nmconnection"
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertIn("ssid=Example Home", profile.read_text().splitlines())
        # Units the packages file keeps disabled are disabled again.
        wants = sorted(path.name for path in (target / "etc/systemd/system/multi-user.target.wants").iterdir())
        self.assertEqual(wants, ["avahi-daemon.service", "sshd.service", "thermite-first-boot.service"])
        # Records without a secret; first boot enabled and announced.
        receipt = json.loads((target / "var/log/thermite-install/receipt.json").read_text())
        self.assertEqual((receipt["answers"], receipt["kept_disabled"]), (ANSWERS, ["nftables.service"]))
        self.assertEqual(receipt["logs"]["archinstall"], "/var/log/archinstall/install.log")
        for path in (target / "var/log/thermite-install").iterdir():
            self.assertNotIn(WIFI["passphrase"], path.read_text())
        unit = (target / "etc/systemd/system/thermite-first-boot.service").read_text()
        self.assertIn("User=casey", unit.splitlines())
        self.assertIn("ExecStopPost=+/usr/bin/python3 -I -B /usr/local/lib/thermite-install/first-boot.py finish",
                      unit.splitlines())
        self.assertEqual(json.loads((target / first_boot.PENDING).read_text()),
                         {"owner": "casey", "hostname": "thermite", "timezone": "Europe/Berlin"})
        self.assertIn("journalctl -fu thermite-first-boot.service", (target / first_boot.STATUS).read_text())
        self.assertIn("sudo runs any command as root for casey without a password",
                      " ".join((target / first_boot.STATUS).read_text().split()))
        # The first boot's sudo rule never survives a boot, power loss included.
        tmpfiles = target / first_boot.TMPFILES
        self.assertIn("r /etc/sudoers.d/90-thermite-first-boot", tmpfiles.read_text().splitlines())
        if shutil.which("systemd-tmpfiles"):
            (target / first_boot.SUDOERS).write_text("casey ALL=(ALL:ALL) NOPASSWD: ALL\n")
            subprocess.run(["systemd-tmpfiles", f"--root={target}", "--remove", "--boot", str(tmpfiles)],
                           check=True, capture_output=True)
            self.assertFalse((target / first_boot.SUDOERS).exists())
            self.assertTrue((target / "etc/sudoers.d/10-thermite-wheel").is_file())
        self.assertTrue(all(line.startswith("Thermite: ") for line in said.splitlines()))

    def test_without_a_key_passwords_stay_and_a_missing_owner_stops_it(self):
        (self.media / "authorized_keys").unlink()
        (self.target / "etc/ssh/sshd_config").write_text("Include /etc/ssh/sshd_config.d/*.conf\n")
        self.finalize()
        self.assertEqual((self.target / "etc/ssh/sshd_config.d/20-thermite.conf").read_text().splitlines()[1:],
                         ["PermitRootLogin no"])
        self.assertEqual((self.target / "etc/ssh/sshd_config").read_text().count("Include"), 1)
        self.assertFalse((self.target / "home/casey/.ssh").exists())
        for group in ("wheel:x:998:\n", "wheel:x:998:casey\n"):
            other = self.root / f"other-{len(group)}"
            shutil.copytree(self.target, other, symlinks=True)
            shutil.rmtree(other / "opt")
            (other / "etc/group").write_text(group)
            if group.endswith("casey\n"): (other / "etc/shadow").write_text("casey:!:1::::::\n")
            with patch.object(Path, "is_mount", return_value=True), self.assertRaisesRegex(ValueError, "owner casey"):
                plugin.finalize(other, self.media, self.state)
            self.assertFalse((other / "opt").exists())
        with self.assertRaisesRegex(ValueError, "mounted target"):
            plugin.finalize(self.target, self.media, self.state)

    def test_wifi_profile_is_read_back_as_written_and_follows_the_joined_security(self):
        def parsed(text):
            sections, section = {}, None
            for line in text.splitlines():
                if line.startswith("["): section = sections.setdefault(line.strip("[]"), {})
                elif "=" in line: key, _, value = line.partition("="); section[key] = value
            return sections
        profile = parsed(plugin.wifi_profile(WIFI))
        self.assertEqual((profile["wifi"]["ssid"], profile["connection"]["id"]), ("Example Home", "Example Home"))
        self.assertEqual(profile["wifi-security"], {"key-mgmt": "wpa-psk", "psk": HIDDEN})
        self.assertNotIn("hidden", profile["wifi"])
        # GKeyFile trims the ends and reads backslashes as escapes.
        tricky = {"ssid": " Café;1 ", "security": "sae", "hidden": True, "passphrase": " " + HIDDEN + "\\ "}
        profile = parsed(plugin.wifi_profile(tricky))
        self.assertEqual(profile["wifi-security"]["key-mgmt"], "sae")
        self.assertEqual(profile["wifi-security"]["psk"], "\\s" + HIDDEN + "\\\\\\s")
        self.assertEqual(profile["connection"]["id"], "\\sCafé;1\\s")
        self.assertEqual(profile["wifi"]["ssid"], "".join(f"{byte};" for byte in " Café;1 ".encode()))
        self.assertEqual(profile["wifi"]["hidden"], "true")
        # A digits-only name could read as bytes; it is written as bytes.
        self.assertEqual(plugin.keyfile_ssid("123"), "49;50;51;")
        key = parsed(plugin.wifi_profile({"ssid": "Example Home", "security": "psk", "hidden": False, "key": "ab" * 32}))
        self.assertEqual(key["wifi-security"]["psk"], "ab" * 32)
        open_network = parsed(plugin.wifi_profile({"ssid": "Example Guest", "security": "open", "hidden": False}))
        self.assertNotIn("wifi-security", open_network)

    def test_hosts_line_and_terminal_fallback(self):
        self.assertEqual(plugin.hosts_line("hosts: files dns"), "hosts: mdns_minimal [NOTFOUND=return] files dns")
        self.assertEqual(plugin.hosts_line("hosts: files mdns4_minimal dns"), "hosts: files mdns4_minimal dns")
        if not shutil.which("infocmp"): self.skipTest("needs ncurses' infocmp")
        script = self.root / "term.sh"; script.write_text(plugin.TERM_FALLBACK)
        for given, wanted in (("xterm-thermite-unknown", "xterm-256color"), ("xterm", "xterm"), ("", "")):
            shown = subprocess.run(["sh", "-c", '. "$1"; printf %s "$TERM"', "term", str(script)],
                                   env={"PATH": os.environ["PATH"], "TERM": given}, capture_output=True, text=True)
            self.assertEqual(shown.stdout, wanted, given)


class FirstBoot(Scratch):
    def setUp(self):
        super().setUp()
        self.system = self.tree(self.root / "system", {
            ACCOUNTS: f"casey:x:{os.getuid()}:{os.getgid()}::{self.root}/home:/bin/bash\n",
            "etc/sudoers.d/10-thermite-wheel": "%wheel ALL=(ALL:ALL) ALL\n",
            first_boot.PENDING: json.dumps({"owner": "casey", "hostname": "thermite", "timezone": "Europe/Berlin"}),
            "var/log/thermite-install/receipt.json": "{}"})
        self.home = self.root / "home"; self.home.mkdir()

    def test_prepare_adds_a_checked_rule_and_finish_always_removes_it(self):
        rule = self.system / first_boot.SUDOERS
        ok = lambda argv, **options: subprocess.CompletedProcess(argv, 0)
        self.assertEqual(first_boot.prepare(self.system, run=ok), 0)
        self.assertEqual(rule.read_text().splitlines()[1:],
                         ["casey ALL=(ALL:ALL) NOPASSWD: ALL", "Defaults:casey verifypw=any"])
        self.assertEqual(rule.stat().st_mode & 0o777, 0o440)
        self.assertTrue((self.system / first_boot.LOG).is_file())
        for status, code, kept, says in (("75", "exited", True, "the Internet did not answer in 15 minutes"),
                                         ("1", "exited", True, "status 1; the log says why"),
                                         ("124", "exited", True, "longer than 3 hours"),
                                         ("TERM", "killed", True, "stopped by signal TERM, as a shutdown"),
                                         ("0", "exited", False, "Thermite is installed on thermite.local")):
            first_boot.prepare(self.system, run=ok)
            disabled = []
            first_boot.finish(self.system, status=status, code=code,
                              run=lambda argv, **options: disabled.append(argv))
            self.assertFalse(rule.exists())
            self.assertEqual(disabled, [["systemctl", "disable", first_boot.UNIT]])
            self.assertEqual((self.system / first_boot.PENDING).exists(), kept)
            message = (self.system / first_boot.STATUS).read_text()
            self.assertIn(says, message)
            if kept:
                # The steps by hand include the identity the commit needs.
                self.assertIn("git -C ~/thermite-site config user.email casey@thermite.local", message)
                self.assertIn("finishes or rolls back that install, then installs again", message)
        # A rule visudo refuses is never left behind.
        (self.system / first_boot.PENDING).write_text(json.dumps({"owner": "casey"}))
        refused = lambda argv, **options: subprocess.CompletedProcess(argv, 1)
        self.assertEqual(first_boot.prepare(self.system, run=refused), 1)
        self.assertFalse(rule.exists())

    def thermite(self, install: str) -> Path:
        """A stand-in thermite with install --yes's contract: init writes a
        site; status --json reports the open transaction ($HOME/open); install
        must get --yes, a closed stdin, TERM=dumb and LC_ALL=C, and resolves
        an open transaction and stops, as thermite install does; otherwise
        it runs the given shell."""
        command = self.root / "thermite"
        command.write_text(f"""#!/bin/sh
set -eu
case "$1" in
init) mkdir -p "$3/instance"; echo '{{}}' > "$3/thermite-site.json"
      printf '{{\\n  "timezone": "Etc/UTC",\\n  "uid": 9001\\n}}\\n' > "$3/instance/host.yaml" ;;
status) if [ -f "$HOME/open" ]; then printf '{{"open_transaction": "%s"}}\\n' "$(cat "$HOME/open")"
        else echo '{{"open_transaction": null}}'; fi ;;
install) [ "$2" = --yes ] && [ "$3" = --config ] || exit 7
         [ ! -t 0 ] && [ "$(wc -c)" -eq 0 ] || exit 8
         echo "install sees TERM=$TERM LC_ALL=$LC_ALL" >> "$HOME/installs"
         if [ -f "$HOME/open" ]; then rm "$HOME/open"; echo "resolved, stopped"; exit 2; fi
         {install} ;;
esac
""")
        command.chmod(0o755)
        return command

    def run_first_boot(self, install: str, online=lambda: True, **options) -> tuple[int, str]:
        command = self.thermite(install)
        out = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home), "GIT_CONFIG_GLOBAL": os.devnull,
                                     "GIT_CONFIG_NOSYSTEM": "1"}):
            status = first_boot.run(self.home, root=self.system, command=str(command), online=online,
                                    sleep=lambda seconds: None, out=out, **options)
        return status, out.getvalue()

    def test_run_initializes_commits_and_installs_with_yes_into_the_log_and_the_journal(self):
        if not shutil.which("git"): self.skipTest("needs git")
        status, journal = self.run_first_boot("echo installed")
        self.assertEqual(status, 0)
        site = self.home / first_boot.SITE
        self.assertEqual(json.loads((site / "instance/host.yaml").read_text()),
                         {"timezone": "Europe/Berlin", "uid": 9001})
        git = subprocess.run(["git", "-C", str(site), "log", "--format=%an <%ae> %s"], capture_output=True,
                             text=True).stdout
        self.assertEqual(git.strip(), "casey <casey@thermite.local> Thermite site from the installer")
        self.assertEqual((self.home / "installs").read_text(), "install sees TERM=dumb LC_ALL=C\n")
        # Everything goes to the log and to stdout, which is the journal.
        log = (self.system / first_boot.LOG).read_text()
        self.assertEqual(log, journal)
        self.assertIn("$ " + str(self.root / "thermite") + " install --yes --config " + str(site), log)
        self.assertIn("installed", log)
        # Again: the site and its commit are kept, install runs once more.
        self.assertEqual(self.run_first_boot("echo again")[0], 0)
        self.assertEqual(len(subprocess.run(["git", "-C", str(site), "log", "--oneline"], capture_output=True,
                                            text=True).stdout.splitlines()), 1)

    def test_an_install_a_stopped_run_left_open_is_resolved_then_installed(self):
        if not shutil.which("git"): self.skipTest("needs git")
        (self.home / "open").write_text("20261105T120000-example")
        status, journal = self.run_first_boot("echo installed")
        self.assertEqual(status, 0)
        self.assertEqual(len((self.home / "installs").read_text().splitlines()), 2)
        self.assertIn("Install transaction 20261105T120000-example of a stopped run is still open", journal)
        self.assertLess(journal.index("resolved, stopped"), journal.index("installed"))
        # Nothing open: one run, and its failure is the run's.
        (self.home / "installs").unlink()
        self.assertEqual(self.run_first_boot("exit 5")[0], 5)
        self.assertEqual(len((self.home / "installs").read_text().splitlines()), 1)

    def test_a_slow_install_is_stopped_and_one_that_left_a_process_behind_still_ends(self):
        if not shutil.which("git"): self.skipTest("needs git")
        with patch.object(first_boot, "POLL", 0.1):
            started = time.monotonic()
            status, journal = self.run_first_boot("exec sleep 30", timeout=2)
            self.assertEqual(status, first_boot.TOO_LONG)
            self.assertIn("took too long", journal)
            # A child that keeps the output open does not keep run waiting.
            status, journal = self.run_first_boot("(sleep 30 &); echo installed")
            self.assertEqual(status, 0)
            self.assertLess(time.monotonic() - started, 20)

    def test_no_internet_stops_the_run(self):
        status, journal = self.run_first_boot("exit 0", online=lambda: False)
        self.assertEqual(status, first_boot.OFFLINE)
        self.assertIn("Waiting for the Internet", journal)
        self.assertFalse((self.home / "installs").exists())

    def test_first_boot_drives_no_terminal(self):
        source = (AREA / "iso/first-boot.py").read_text()
        for gone in ("import pty", "pty.fork", "[y/N]", "APPROVAL", "QUESTION", "PASSWORD"):
            self.assertFalse(gone in source, gone)


if __name__ == "__main__": unittest.main()
