"""Portable offline installer contracts; no disk, daemon or network action."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_iso
import oh_package

spec = importlib.util.spec_from_file_location("iso_bootstrap", AREA / "iso/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class InstallerISO(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/iso-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-", dir=scratch)
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_seed_keeps_storage_and_identity_interactive_without_defaults(self):
        import yaml
        document = yaml.safe_load(oh_iso.configuration())
        config = document["autoinstall"]
        self.assertEqual(config["interactive-sections"], ["*"])
        self.assertNotIn("storage", config)
        self.assertNotIn("identity", config)
        self.assertNotIn("early-commands", config)
        self.assertEqual(config["refresh-installer"], {"update": False})
        commands = config["late-commands"]
        self.assertEqual(commands[-1], ["curtin", "in-target", "--target=/target", "--", "python3",
                                       "/opt/thermite-media/bootstrap.py"])
        # The first install's `python3 -m venv` needs Ubuntu's ensurepip.
        prerequisites = commands[-2]
        self.assertEqual(prerequisites[:6], ["curtin", "in-target", "--target=/target", "--", "sh", "-c"])
        self.assertIn("apt-get install -y python3-venv git ||", prerequisites[6])
        guide = (AREA / "iso/first-boot.txt").read_text()
        self.assertIn("sudo apt-get install python3-venv git", guide)
        self.assertIn('thermite install --config "$HOME/thermite-site"', guide)
        self.assertNotIn("thermite access", guide)
        # host.yaml has no state setting since the fixed /srv/thermite layout.
        self.assertNotIn("state directory", guide)
        self.assertFalse(any("wipefs" in str(item) or "mkfs" in str(item) for item in commands))

    def test_boot_seed_patch_preserves_args_and_explicit_confirmation(self):
        original = 'menuentry "Try or Install Ubuntu Server" {\n linux /casper/vmlinuz quiet ---\n initrd /casper/initrd\n}\n'
        result = oh_iso.grub_config(original)
        self.assertIn("quiet ", result)
        self.assertIn(r"ds=nocloud\;s=/cdrom/nocloud/", result)
        self.assertIn("---", result)
        self.assertNotIn("autoinstall", result)
        self.assertIn("initrd /casper/initrd", result)
        with self.assertRaises(ValueError): oh_iso.grub_config("linux /different/kernel ---\n")
        with self.assertRaises(ValueError): oh_iso.grub_config("linux /casper/vmlinuz autoinstall ---\n")

    def test_arch_boot_starts_thermites_installer_not_archinstalls_menus(self):
        self.assertTrue(oh_iso.inputs()["base"]["name"].startswith("archlinux-"))
        self.assertTrue(oh_iso.inputs("ubuntu")["base"]["name"].startswith("ubuntu-"))
        script = (AREA / "iso/arch-install.sh").read_text()
        self.assertIn('python3 -I -B "$media/installer.py" launch', script)
        self.assertNotIn("archinstall", script.replace("archinstall's menus", ""))
        self.assertNotIn("read ", script)

    def test_node_and_blessed_are_pinned_like_the_base_image(self):
        pins = oh_iso.inputs()
        node = pins["node"]
        version = re.fullmatch(r"node-(v\d+\.\d+\.\d+)-linux-x64\.tar\.xz", node["name"])[1]
        self.assertEqual(node["url"], f"https://nodejs.org/dist/{version}/{node['name']}")
        self.assertEqual(node["provenance"], f"https://nodejs.org/dist/{version}/SHASUMS256.txt")
        self.assertRegex(node["sha256"], r"^[0-9a-f]{64}$")
        # blessed is the one thermite tui locks; the registry publishes its
        # sha512 integrity, not a SHA-256.
        lock = json.loads((AREA / "tui/remote/package-lock.json").read_text())["packages"]["node_modules/blessed"]
        blessed = pins["blessed"]
        self.assertEqual((blessed["url"], blessed["integrity"]), (lock["resolved"], lock["integrity"]))
        self.assertEqual(blessed["name"], f"blessed-{lock['version']}.tgz")
        self.assertNotIn("sha256", blessed)
        self.assertNotIn("node", oh_iso.inputs("ubuntu"))

    def test_authorized_key_is_one_plain_public_key(self):
        def key(kind, comment=" casey@example", body=None):
            import base64
            blob = body or (len(kind).to_bytes(4, "big") + kind.encode() + (32).to_bytes(4, "big") + bytes(range(32)))
            return kind + " " + base64.b64encode(blob).decode() + comment
        good = self.root / "good.pub"; good.write_text("# the owner's laptop\n" + key("ssh-ed25519") + "\n")
        found = oh_iso.public_key(good)
        self.assertEqual((found["type"], found["comment"]), ("ssh-ed25519", "casey@example"))
        self.assertEqual(found["line"], key("ssh-ed25519"))
        if shutil.which("ssh-keygen"):
            printed = subprocess.run(["ssh-keygen", "-l", "-f", str(good)], capture_output=True, text=True).stdout
            self.assertEqual(printed.split()[1], found["fingerprint"])
        for index, text in enumerate((key("ssh-ed25519") + "\n" + key("ssh-ed25519"), 'command="sh" ' + key("ssh-ed25519"),
                                      key("ssh-rsa", body=b"\0\0\0\x0bssh-ed25519"), "ssh-ed25519 !!!", "",
                                      key("ssh-ed25519").replace("ssh-ed25519 ", "ssh-dss ", 1))):
            bad = self.root / f"bad-{index}.pub"; bad.write_text(text)
            with self.assertRaises(ValueError): oh_iso.public_key(bad)

    def test_package_tiers_cover_thermites_packages_step(self):
        from oh_install import steps
        document = oh_iso.packages()
        names = oh_iso.package_names(document, "btrfs")
        # thermite install's Packages step finds nothing to do on an image host.
        self.assertTrue(set(steps.PACKAGES["pacman"]) <= set(names))
        self.assertEqual(document["tiers"][0]["packages"], list(steps.PACKAGES["pacman"]))
        self.assertTrue({"openssh", "rsync", "avahi", "nss-mdns", "sudo", "btop", "emacs-nox", "jq", "miller",
                         "kitty-terminfo", "ghostty-terminfo", "rxvt-unicode-terminfo", "foot-terminfo"} <= set(names))
        # Excluded by the review: heavy, a GUI stack, or a broker unit.
        self.assertFalse({"wireshark-cli", "gupnp-tools", "mosquitto"} & set(names))
        self.assertNotIn("compsize", oh_iso.package_names(document, "ext4"))
        self.assertIn("compsize", names)
        self.assertFalse(set(document["enable"]) & set(document["keep_disabled"]))
        self.assertTrue({"named.service", "nftables.service", "rsyncd.service"} <= set(document["keep_disabled"]))
        self.assertIn("sshd.service", document["enable"])
        for broken in ({"version": 2}, {**document, "enable": ["sshd.service", "named.service"]},
                       {**document, "tiers": [*document["tiers"], {"name": "x", "title": "x", "description": "x",
                                                                   "packages": ["git"]}]},
                       {**document, "tiers": [{"name": "x", "title": "x", "description": "x", "packages": ["git *"]}]}):
            with patch.object(oh_iso.json, "loads", return_value=broken), self.assertRaises(ValueError):
                oh_iso.packages()

    def test_docs_list_exactly_the_configured_packages(self):
        # The core's configuration and preflight read packages.json itself
        # (tests/test_iso_installer.py); the guide's table must agree with it.
        document = oh_iso.packages()
        guide = (AREA / "docs/iso.md").read_text()
        section = guide[guide.index("## Packages"):guide.index("\n## ", guide.index("## Packages") + 1)]
        rows = re.findall(r"^\| ([^|]+) \| (`.*`) \|$", section, re.M)
        self.assertEqual([(title, re.findall(r"`([^`]+)`", listed)) for title, listed in rows],
                         [(tier["title"], tier["packages"]) for tier in document["tiers"]])

    def test_arch_boot_configs_keep_source_uuid_and_local_script(self):
        options = "archisobasedir=arch archisosearchuuid=2026-11-01-08-42-25-00"
        for kind, original in (("syslinux", "APPEND " + options + "\n"), ("systemd", "options  " + options + "\n")):
            patched = oh_iso.arch_boot_config(original, kind)
            self.assertIn(options, patched)
            self.assertIn("script=/run/archiso/bootmnt/thermite/arch-install.sh", patched)
            with self.assertRaises(ValueError): oh_iso.arch_boot_config(patched, kind)
        with self.assertRaises(ValueError): oh_iso.arch_boot_config("options root=/dev/sda\n", "systemd")

    def test_arch_efi_copy_checks_partition_bounds(self):
        import struct
        image = self.root / "base.iso"
        mbr = bytearray(512); mbr[510:] = b"\x55\xaa"; mbr[466] = 0xef
        struct.pack_into("<II", mbr, 470, 1, 1)
        image.write_bytes(mbr + b"E" * 512)
        output = self.root / "efi.img"
        oh_iso.arch_efi_image(image, output)
        self.assertEqual(output.read_bytes(), b"E" * 512)
        struct.pack_into("<II", mbr, 470, 1, 3)
        image.write_bytes(mbr + b"E" * 512)
        with self.assertRaises(ValueError): oh_iso.arch_efi_image(image, self.root / "bad.img")
        self.assertFalse((self.root / "bad.img").exists())

    def test_input_mismatch_and_output_symlinks_refuse_without_building(self):
        file = self.root / "input.iso"; file.write_bytes(b"untrusted")
        with self.assertRaises(ValueError): oh_iso.verified(file, {"sha256": "0" * 64})
        with self.assertRaises(ValueError): oh_iso.new_path(file)
        link = self.root / "link"; link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): oh_iso.new_path(link / "out.iso")
        with self.assertRaises(ValueError): oh_iso.regular(link)

    def test_fetch_verifies_cached_inputs_without_network_and_refuses_corruption(self):
        pin = {key: {"name": key, "sha256": hashlib.sha256(key.encode()).hexdigest(),
                     "url": "https://example.invalid/" + key} for key in ("base", "node")}
        pin["blessed"] = {"name": "blessed", "url": "https://example.invalid/blessed",
                          "integrity": "sha512-" + base64.b64encode(hashlib.sha512(b"blessed").digest()).decode()}
        cache = self.root / "cache"; cache.mkdir()
        for key in pin: (cache / key).write_text(key)
        with patch.object(oh_iso, "inputs", return_value=pin), \
                patch.object(oh_iso.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            self.assertEqual(set(oh_iso.fetch(cache)), {"base", "node", "blessed"})
            for key in ("node", "blessed"):
                (cache / key).write_text("changed")
                with self.assertRaises(ValueError): oh_iso.fetch(cache)
                self.assertEqual((cache / key).read_text(), "changed")
                (cache / key).write_text(key)
        # An integrity that is not sha512 is never accepted.
        weak = {**pin["blessed"], "integrity": "sha1-" + base64.b64encode(hashlib.sha1(b"blessed").digest()).decode()}
        with self.assertRaises(ValueError): oh_iso.verified(cache / "blessed", weak)

    def test_fetch_downloads_a_missing_input_and_keeps_only_a_verified_one(self):
        import io as streams
        body = b"node tarball"
        pin = {"node": {"name": "node.tar.xz", "sha256": hashlib.sha256(body).hexdigest(),
                        "url": "https://example.invalid/node.tar.xz"}}
        class Response(streams.BytesIO):
            url = pin["node"]["url"]
        cache = self.root / "cache"
        with patch.object(oh_iso, "inputs", return_value=pin), \
                patch.object(oh_iso.urllib.request, "urlopen", return_value=Response(body)) as opened:
            self.assertEqual(oh_iso.fetch(cache), {"node": str(cache / "node.tar.xz")})
        opened.assert_called_once_with(pin["node"]["url"], timeout=60)
        self.assertEqual((cache / "node.tar.xz").read_bytes(), body)
        (cache / "node.tar.xz").unlink()
        with patch.object(oh_iso, "inputs", return_value=pin), \
                patch.object(oh_iso.urllib.request, "urlopen", return_value=Response(b"tampered")):
            with self.assertRaises(ValueError): oh_iso.fetch(cache)
        self.assertEqual(list(cache.iterdir()), [])

    def test_arch_media_carries_the_installer_its_runtime_and_the_key(self):
        from tests.test_iso_installer import runtime_archives
        cache = self.root / "cache"; cache.mkdir()
        pins = {"base": oh_iso.inputs()["base"], **runtime_archives(cache)}
        key = self.root / "owner.pub"
        key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f casey@example\n")
        media = self.root / "media"; media.mkdir()
        with patch.object(oh_iso, "inputs", return_value=pins):
            payload = oh_iso.stage(media, cache, authorized_key=oh_iso.public_key(key))
        installer = payload["installer"]
        self.assertEqual(installer["node"], pins["node"])
        self.assertEqual(installer["blessed"], pins["blessed"])
        self.assertEqual(set(installer["files"]), {"installer.py", "look.json", *(name for name in oh_iso.INSTALLER
                                                                                   if name.startswith("tui/"))})
        for name, digest in installer["files"].items():
            self.assertEqual(oh_iso.sha256(media / name), digest, name)
        self.assertEqual(json.loads((media / "payload.json").read_text()), payload)
        self.assertEqual(json.loads((media / "packages.json").read_text()), oh_iso.packages())
        self.assertIn("torch", json.loads((media / "look.json").read_text()))
        self.assertEqual((media / "authorized_keys").read_text(), key.read_text())
        self.assertEqual(payload["authorized_key"]["comment"], "casey@example")
        self.assertFalse((media / "arch-config.json").exists())
        self.assertEqual((media / "first-boot.py").read_bytes(), (AREA / "iso/first-boot.py").read_bytes())
        # What tty1 and SSH run: the core unpacks the runtime the medium carries.
        spec = importlib.util.spec_from_file_location("staged_installer", media / "installer.py")
        staged = importlib.util.module_from_spec(spec); spec.loader.exec_module(staged)
        node, modules = staged.runtime(media, self.root / "state")
        self.assertTrue(os.access(node, os.X_OK) and (modules / "blessed/package.json").is_file())
        if shutil.which("node"):
            # The staged terminal loads from its medium paths alone.
            completed = subprocess.run([shutil.which("node"), "-e", "const m = require(process.argv[1]); "
                                        "console.log(typeof m.parse, require(process.argv[2]).STEPS.length)",
                                        str(media / "tui/installer/main.js"), str(media / "tui/installer/controller.js")],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.stdout.strip(), "function 11", completed.stderr)
        with patch.object(oh_iso, "inputs", return_value={"base": pins["base"]}), \
                self.assertRaisesRegex(ValueError, "needs the Arch image"):
            other = self.root / "ubuntu-media"; other.mkdir()
            oh_iso.stage(other, cache, base="ubuntu", authorized_key=oh_iso.public_key(key))

    def test_build_records_its_inputs_and_key_in_the_receipt(self):
        from tests.test_iso_installer import runtime_archives
        cache = self.root / "cache"; cache.mkdir()
        (cache / "base.iso").write_bytes(b"base image")
        pins = {"base": {"name": "base.iso", "sha256": hashlib.sha256(b"base image").hexdigest(),
                         "url": "https://example.invalid/base.iso"}, **runtime_archives(cache)}
        key = self.root / "owner.pub"
        key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f casey@example\n")
        mapped = {}
        def xorriso(argv, **options):
            mapped["media"] = sorted(path.name for path in Path(argv[argv.index("-map") + 1]).iterdir())
            Path(argv[argv.index("-outdev") + 1]).write_bytes(b"remastered")
            return subprocess.CompletedProcess(argv, 0)
        output = self.root / "out/thermite.iso"
        with patch.object(oh_iso, "inputs", return_value=pins), patch.object(oh_iso, "executable", return_value="xorriso"), \
                patch.object(oh_iso, "arch_mapping", return_value=([], [])), \
                patch.object(oh_iso.subprocess, "run", side_effect=xorriso):
            result = oh_iso.build(cache, output, self.root / "work", authorized_key=key)
        self.assertIn(pins["node"]["name"], mapped["media"])
        self.assertIn("authorized_keys", mapped["media"])
        self.assertEqual(result["sha256"], hashlib.sha256(b"remastered").hexdigest())
        self.assertEqual(result["inputs"]["blessed"], {"name": pins["blessed"]["name"],
                                                       "integrity": pins["blessed"]["integrity"]})
        fingerprint = oh_iso.public_key(key)["fingerprint"]
        self.assertEqual(result["payload"]["authorized_key"]["fingerprint"], fingerprint)
        self.assertIn(fingerprint, result["ssh"])
        self.assertEqual(json.loads(Path(str(output) + ".json").read_text()), result)

    def test_bootstrap_wheel_traversal_and_links_are_rejected(self):
        target = self.root / "library"; target.mkdir()
        for index, name in enumerate(("../escape", "/absolute", "back\\slash")):
            wheel = self.root / f"bad-{index}.whl"
            with zipfile.ZipFile(wheel, "w") as archive: archive.writestr(name, b"bad")
            with self.assertRaises(ValueError): bootstrap.unpack(wheel, target)
        wheel = self.root / "linked.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            member = zipfile.ZipInfo("symlink"); member.external_attr = 0o120777 << 16
            archive.writestr(member, b"elsewhere")
        with self.assertRaises(ValueError): bootstrap.unpack(wheel, target)
        self.assertEqual(list(target.iterdir()), [])

    def test_arch_bootstrap_uses_system_yaml_and_offline_bundled_application(self):
        media = self.root / "arch-media"; media.mkdir()
        wheel, release = oh_package.wheel(oh_package.public_files(), media)
        payload = {"version": 1, "application": release["application"], "release": release["digest"],
                   "python_dependency": {"provider": "system", "package": "python-yaml", "module": "yaml"},
                   "wheels": {"application": {"name": wheel.name, "sha256": bootstrap.digest(wheel)}}}
        (media / "payload.json").write_text(json.dumps(payload))
        (media / "FIRST-BOOT.txt").write_text("Owner commissioning follows first boot.\n")
        target = self.root / "arch-target"; target.mkdir()
        self.assertEqual(bootstrap.install(media, root=target), payload)
        environment = target / "opt/thermite/venv"
        self.assertIn("include-system-site-packages = true", (environment / "pyvenv.cfg").read_text())
        profile = target / "etc/profile.d/thermite.sh"
        self.assertTrue(profile.is_file())
        # Login shells source profile.d as POSIX sh: the owner's command, which
        # thermite install switches, must win over this image's /usr/local/bin.
        home = self.root / "owner home"
        shells = [shell for shell in (("sh",), ("dash",), ("bash", "--posix"), ("zsh", "--emulate", "sh"))
                  if shutil.which(shell[0])]
        self.assertIn(("sh",), shells)
        for shell in shells:
            for before, after in (("/usr/local/bin:/usr/bin", f"{home}/.local/bin:/usr/local/bin:/usr/bin"),
                                  (f"{home}/.local/bin:/usr/bin", f"{home}/.local/bin:/usr/bin"),
                                  (f"/usr/bin:{home}/.local/bin", f"{home}/.local/bin:/usr/bin:{home}/.local/bin")):
                completed = subprocess.run([shutil.which(shell[0]), *shell[1:], "-c",
                                            '. "$1"; . "$1"; printf %s "$PATH"', "profile", str(profile)],
                                           env={"HOME": str(home), "PATH": before}, stdin=subprocess.DEVNULL,
                                           capture_output=True, text=True, timeout=30)
                self.assertEqual((completed.returncode, completed.stderr), (0, ""), shell)
                self.assertEqual(completed.stdout, after, (shell, before))
        self.assertFalse((target / "etc/passwd").exists())
        result = subprocess.run([str(target / "usr/local/bin/thermite"), "--version"],
                                cwd=self.root, env={key: value for key, value in os.environ.items()
                                                   if key not in {"PYTHONPATH", "THERMITE_SITE"}},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_bootstrap_installed_command_and_private_site_outside_checkout(self):
        media = self.root / "media"; media.mkdir()
        public = oh_package.public_files()
        wheel, release = oh_package.wheel(public, media)
        # The bootstrap only needs a second checksum-verified wheel. Its
        # real PyYAML pin is separately verified by the build/fetch path;
        # this tiny offline fixture keeps this test independent of the host.
        dependency = media / "pyyaml-fixture.whl"
        with zipfile.ZipFile(dependency, "w") as archive:
            archive.writestr("yaml/__init__.py", "__version__ = '6.0.3'\n")
        payload = {"version": 1, "application": release["application"], "release": release["digest"],
                   "wheels": {"application": {"name": wheel.name, "sha256": bootstrap.digest(wheel)},
                              "pyyaml": {"name": dependency.name, "sha256": bootstrap.digest(dependency)}}}
        (media / "payload.json").write_text(json.dumps(payload))
        (media / "FIRST-BOOT.txt").write_text("Owner commissioning follows first boot.\n")
        system = self.root / "system"; system.mkdir()
        with patch.object(bootstrap, "subprocess", wraps=subprocess) as processes:
            self.assertEqual(bootstrap.install(media, root=system), payload)
        self.assertEqual(len(processes.run.call_args_list), 1)
        installed = system / "opt/thermite/venv"
        entry = system / "usr/local/bin/thermite"
        outside = self.root / "outside"; outside.mkdir()
        environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "THERMITE_SITE"}}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for args in (("--help",), ("--version",), ("init", str(self.root / "private-site"))):
            completed = subprocess.run([str(entry), *args], cwd=outside, env=environment, capture_output=True,
                                       text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        # ISO creation is an application operation and must not demand a
        # selected private site. Missing cached media should be its first error.
        completed = subprocess.run([str(entry), "iso", "build", "--cache", str(self.root / "missing-cache"),
                                    "--work", str(self.root / "work"), "--out", str(self.root / "out.iso")],
                                   cwd=outside, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("expected a regular file", completed.stderr)
        site = self.root / "private-site"
        self.assertTrue((site / "thermite-site.json").is_file())
        # The site README describes the unified install the first-boot guide uses.
        readme = (site / "README.md").read_text()
        self.assertIn("`thermite install` deploys the native services and the HTTPS gateway", readme)
        self.assertNotIn("separate commissioning", readme)
        self.assertFalse((site / "bin").exists())
        self.assertFalse((site / "Makefile").exists())
        result = subprocess.run([str(entry), "--config", str(site), "--help"], cwd=outside, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((installed / "instance").exists())
        with self.assertRaises(ValueError): bootstrap.install(media, root=system)
        empty = self.root / "checksum-refusal"; empty.mkdir()
        dependency.write_bytes(b"tampered")
        with self.assertRaises(ValueError): bootstrap.install(media, root=empty)
        self.assertFalse((empty / "opt").exists())


if __name__ == "__main__": unittest.main()
