"""Portable offline installer contracts; no disk, daemon or network action."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
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
plugin_spec = importlib.util.spec_from_file_location("iso_arch_plugin", AREA / "iso/arch-plugin.py")
arch_plugin = importlib.util.module_from_spec(plugin_spec)
plugin_spec.loader.exec_module(arch_plugin)


class InstallerISO(unittest.TestCase):
    def setUp(self):
        scratch = AREA.parents[1] / ".scratch/arch-iso/tests"
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

    def test_arch_defaults_preserve_native_storage_and_identity_choices(self):
        self.assertTrue(oh_iso.inputs()["base"]["name"].startswith("archlinux-"))
        self.assertTrue(oh_iso.inputs("ubuntu")["base"]["name"].startswith("ubuntu-"))
        config = json.loads((AREA / "iso/arch-config.json").read_text())
        self.assertEqual(set(config), {"packages", "network_config"})
        self.assertTrue({"python", "python-yaml", "git", "make", "podman", "runc", "util-linux", "nodejs", "npm", "sudo"} <= set(config["packages"]))
        script = (AREA / "iso/arch-install.sh").read_text()
        self.assertNotIn("--silent", script)
        self.assertLess(script.index("pacman -Sp"), script.index("exec archinstall"))
        self.assertLess(script.index("plugin.name not in plugins"), script.index("exec archinstall"))

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

    def test_arch_plugin_refuses_unmounted_or_root_only_targets_and_propagates_failure(self):
        target = self.root / "target"; (target / "etc").mkdir(parents=True)
        (target / "etc/os-release").write_text("ID=arch\n")
        (target / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
        (target / "etc/shadow").write_text("root:!:1:0:99999:7:::\n")
        (target / "etc/group").write_text("wheel:x:998:\n")
        media = self.root / "media"; media.mkdir(); (media / "payload.json").write_text("{}")
        with self.assertRaises(ValueError): arch_plugin.finalize(target, media)
        with patch.object(Path, "is_mount", return_value=True):
            with self.assertRaises(ValueError): arch_plugin.finalize(target, media)
        self.assertFalse((target / "opt").exists())
        with (target / "etc/passwd").open("a") as out: out.write("owner:x:1000:1000:Owner:/home/owner:/bin/bash\n")
        with (target / "etc/shadow").open("a") as out: out.write("owner:$y$fixture:1:0:99999:7:::\n")
        (target / "etc/group").write_text("wheel:x:998:owner\n")
        failure = subprocess.CalledProcessError(2, ["arch-chroot"])
        with patch.object(Path, "is_mount", return_value=True), patch.object(arch_plugin.subprocess, "run", side_effect=failure) as run:
            with self.assertRaises(subprocess.CalledProcessError): arch_plugin.finalize(target, media)
        run.assert_called_once_with(["arch-chroot", str(target), "/usr/bin/python3", "/opt/thermite-media/bootstrap.py"], check=True)

    def test_input_mismatch_and_output_symlinks_refuse_without_building(self):
        file = self.root / "input.iso"; file.write_bytes(b"untrusted")
        with self.assertRaises(ValueError): oh_iso.verified(file, {"sha256": "0" * 64})
        with self.assertRaises(ValueError): oh_iso.new_path(file)
        link = self.root / "link"; link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): oh_iso.new_path(link / "out.iso")
        with self.assertRaises(ValueError): oh_iso.regular(link)

    def test_fetch_verifies_cached_inputs_without_network_and_refuses_corruption(self):
        pin = {key: {"name": key, "sha256": hashlib.sha256(key.encode()).hexdigest(),
                     "url": "https://example.invalid/" + key} for key in ("base", "pyyaml")}
        cache = self.root / "cache"; cache.mkdir()
        for key in pin: (cache / key).write_text(key)
        with patch.object(oh_iso, "inputs", return_value=pin), \
                patch.object(oh_iso.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            self.assertEqual(set(oh_iso.fetch(cache)), {"base", "pyyaml"})
            (cache / "pyyaml").write_text("changed")
            with self.assertRaises(ValueError): oh_iso.fetch(cache)
        self.assertEqual((cache / "pyyaml").read_text(), "changed")

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
        self.assertTrue((target / "etc/profile.d/thermite.sh").is_file())
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
