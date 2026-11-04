"""Installed outside-checkout and two-release rollback contracts, fully offline."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import argparse
import importlib.util
import marshal
import py_compile
import struct

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_deploy
import oh_init
import oh_package
import oh_paths
import oh_release


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class Packaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/split_export_audit"))
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="package-tests-", dir=scratch)
        cls.root = Path(cls.temp.name)
        cls.public = oh_package.public_files()
        cls.wheel, cls.release = oh_package.wheel(cls.public, cls.root / "wheels")
        cls.program = cls.root / "program"
        cls.program.mkdir()
        for name, value in oh_package.runtime_files(cls.public).items():
            path = cls.program / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_reviewed_sources_and_identity_suite_are_packaged_without_rewriting(self):
        manifest = AREA / ".thermite-export.json"
        reviewed = (json.loads(manifest.read_text())["paths"] if manifest.is_file()
                    else oh_release.manifest(AREA)["files"])
        self.assertEqual(set(self.public), set(reviewed))
        for name in reviewed:
            self.assertEqual(self.public[name], (AREA / name).read_bytes(), name)
        expected = {"lib/oh_identity/test_settings.py", "tests/test_packaging.py", "tests/test_deploy.py",
                    "tests/test_deploy_auth.py"}
        expected.update(path.relative_to(AREA).as_posix()
                        for path in (AREA / "lib/oh_identity/tests").glob("*.py"))
        self.assertTrue(expected <= self.release["files"].keys())
        with zipfile.ZipFile(self.wheel) as archive:
            for name in expected | {"Makefile", "lib/oh_identity/README.md", "lib/oh_cli/__init__.py"}:
                self.assertEqual(archive.read("thermite_app/runtime/" + name), self.public[name], name)

    def test_new_site_tracks_inputs_and_ignores_derived_configuration(self):
        site = self.root / "new-schema-site"
        oh_init.initialize(site, program=self.program)
        self.assertFalse((site / "bin").exists())
        self.assertFalse((site / "Makefile").exists())
        descriptor = json.loads((site / oh_paths.SITE_FILE).read_text())
        self.assertEqual(descriptor, {"version": 2, "application": self.release["application"],
                                      "release": self.release["digest"]})
        derived = ("conf/services/addons.cfg", "metrics/prometheus/prometheus.yml",
                   "deploy/thermite-openhab.container", "var/runtime-state")
        for name in derived:
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("derived fixture\n")
        subprocess.run(["git", "-C", str(site), "init", "--initial-branch=fixture"],
                       check=True, capture_output=True)
        ignored = subprocess.run(["git", "-C", str(site), "check-ignore", "--", *derived],
                                 check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(set(ignored), set(derived))
        status = subprocess.run(["git", "-C", str(site), "status", "--porcelain", "--untracked-files=all"],
                                check=True, capture_output=True, text=True).stdout
        self.assertIn("instance/devices.yaml", status)
        self.assertIn("instance/host.yaml", status)
        self.assertIn("versions.json", status)
        self.assertIn(oh_paths.SITE_FILE, status)
        for name in derived:
            self.assertNotIn(name, status)

    def test_fresh_clone_renders_with_exact_installed_release_without_creating_var(self):
        source = self.root / "fresh-clone-source"
        oh_init.initialize(source, program=self.program)
        def git(directory, *args):
            return subprocess.run(["git", "-C", str(directory), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git(source, "init", "--initial-branch=fixture")
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "config", "commit.gpgsign", "false")
        git(source, "add", ".")
        git(source, "commit", "-m", "private site")
        clone = self.root / "fresh-clone"
        subprocess.run(["git", "clone", "--no-local", str(source), str(clone)],
                       check=True, capture_output=True)
        self.assertFalse((clone / "var").exists())
        descriptor = (clone / oh_paths.SITE_FILE).read_bytes()
        program_before = hashes(self.program)
        with patch.object(oh_paths, "PROGRAM", self.program):
            files, application = oh_deploy.prepared_files(clone, "HEAD", clone / "var")
        self.assertIn("conf/services/addons.cfg", files)
        self.assertIn("deploy/thermite-openhab.container", files)
        self.assertEqual(application, {"version": self.release["application"], "digest": self.release["digest"]})
        self.assertFalse((clone / "var").exists(), "planning must not materialize release or build caches")
        self.assertFalse(any(clone.glob(".site-render-*")))
        self.assertEqual((clone / oh_paths.SITE_FILE).read_bytes(), descriptor)
        self.assertEqual(git(clone, "status", "--porcelain"), "")
        self.assertEqual(hashes(self.program), program_before)

    def test_release_resolution_requires_exact_installed_identity(self):
        store = self.root / "not-created-release-store"
        selected = oh_release.resolve(store, self.release["digest"], self.release["application"],
                                      program=self.program)
        self.assertEqual(selected, self.program)
        for digest, version in (("0" * 64, self.release["application"]),
                                (self.release["digest"], "another-version"),
                                ("../outside", self.release["application"])):
            with self.subTest(digest=digest, version=version), self.assertRaises(oh_release.ReleaseError):
                oh_release.resolve(store, digest, version, program=self.program)
        self.assertFalse(store.exists())
        damaged = self.root / "damaged-installed-program"
        damaged.mkdir()
        source = damaged / "reviewed.py"
        source.write_text("reviewed\n")
        release = oh_release.create({source.name: source.read_bytes()})
        (damaged / oh_release.FILE).write_bytes(oh_release.encoded(release))
        source.write_text("changed\n")
        with self.assertRaises(oh_release.ReleaseError):
            oh_release.resolve(store, release["digest"], release["application"], program=damaged)
        self.assertFalse(store.exists())

    def test_existing_release_cache_is_authoritative_even_when_installed_release_matches(self):
        store = self.root / "authoritative-release-store"
        cached = oh_release.cache(self.program, store)
        # An unavailable installed application does not prevent exact rollback.
        self.assertEqual(oh_release.resolve(store, self.release["digest"], self.release["application"],
                                            program=self.root / "uninstalled"), cached)
        changed = cached / "lib/oh_atv.py"
        changed.chmod(0o600)
        changed.write_text("damaged cache\n")
        with self.assertRaises(oh_release.ReleaseError):
            oh_release.resolve(store, self.release["digest"], self.release["application"], program=self.program)
        self.assertEqual(changed.read_text(), "damaged cache\n")
        shutil.rmtree(cached)
        cached.mkdir()
        with self.assertRaises(oh_release.ReleaseError):
            oh_release.resolve(store, self.release["digest"], self.release["application"], program=self.program)
        self.assertEqual(list(cached.iterdir()), [])
        cached.rmdir()
        for target in (self.program, self.root / "missing-release"):
            with self.subTest(target=target):
                cached.symlink_to(target, target_is_directory=True)
                with self.assertRaises(oh_release.ReleaseError):
                    oh_release.resolve(store, self.release["digest"], self.release["application"], program=self.program)
                with self.assertRaises(oh_release.ReleaseError):
                    oh_release.cache(self.program, store)
                self.assertTrue(cached.is_symlink())
                cached.unlink()
        store.rmdir()
        store.symlink_to(self.root / "missing-store", target_is_directory=True)
        with self.assertRaises(oh_release.ReleaseError):
            oh_release.resolve(store, self.release["digest"], self.release["application"], program=self.program)
        store.unlink()
        store.write_text("not a directory\n")
        with self.assertRaises(oh_release.ReleaseError):
            oh_release.resolve(store, self.release["digest"], self.release["application"], program=self.program)
        if os.geteuid() != 0:
            store.unlink()
            store.mkdir(mode=0o000)
            try:
                with self.assertRaises(PermissionError):
                    oh_release.resolve(store, self.release["digest"], self.release["application"], program=self.program)
            finally:
                store.chmod(0o700)

    def test_wheel_is_repeatable_explicit_public_payload_and_real_metadata(self):
        other, release = oh_package.wheel(self.public, self.root / "repeat")
        self.assertEqual(self.wheel.read_bytes(), other.read_bytes())
        self.assertEqual(release, self.release)
        self.assertEqual(oh_release.manifest(self.program), release)
        with zipfile.ZipFile(self.wheel) as archive:
            names = archive.namelist()
            self.assertFalse(any("/conf/" in name or "/var/" in name or "/metrics/" in name for name in names))
            metadata = archive.read(f"thermite_app-{oh_release.VERSION}.dist-info/METADATA").decode()
            self.assertIn("Requires-Dist: PyYAML>=6,<7", metadata)
            self.assertIn("All rights reserved", metadata)
            demo = json.loads(archive.read("thermite_app/runtime/instance/devices.yaml"))
            self.assertEqual(demo.get("access", {}).get("mode", "authenticated"), "authenticated")

    def test_pyatv_environment_reentry_uses_program_launcher_and_same_private_site(self):
        from oh_cli import apple_tv
        environment = self.root / "pyatv-only"
        (environment / "bin").mkdir(parents=True)
        (environment / "bin/python").touch()
        selected = self.root / "selected-site"
        args = argparse.Namespace(setup=False, listen="127.0.0.1:8099", secrets=selected / "private-pairing", allow_remote=False)
        class Reentered(Exception): pass
        with patch.object(apple_tv, "VENV", environment), patch.object(apple_tv, "AREA", selected), \
                patch.object(apple_tv, "in_venv", return_value=False), \
                patch.object(apple_tv.sys, "argv", ["/installed-environment/bin/thermite", "atv", "serve"]), \
                patch.object(apple_tv.os, "execv", side_effect=Reentered) as execute:
            with self.assertRaises(Reentered): apple_tv.serve(args)
        self.assertEqual(execute.call_args.args, (str(environment / "bin/python"),
            [str(environment / "bin/python"), str(oh_paths.PROGRAM / "bin/thermite"), "--site", str(selected),
             "apple-tv", "serve", "--listen", args.listen, "--secrets", str(args.secrets)]))

    def test_init_upgrade_keeps_configuration_state_and_old_immutable_release(self):
        site = self.root / "site-upgrade"
        first = oh_init.initialize(site, program=self.program)
        (site / "instance/devices.yaml").write_text("owner configuration\n")
        (site / "var/owner-state").write_text("keep state\n")
        (site / "Makefile").write_text("# Owner's custom tooling\n")
        second_program = self.root / "program-next"
        shutil.copytree(self.program, second_program)
        filename = "lib/oh_atv.py"
        (second_program / filename).write_bytes((second_program / filename).read_bytes() + b"\n# next reviewed release\n")
        files = {name: (second_program / name).read_bytes() for name in self.release["files"]}
        newer = oh_release.create(files, "0.1.1.dev0")
        (second_program / oh_release.FILE).write_bytes(oh_release.encoded(newer))
        second = oh_init.initialize(site, program=second_program, upgrade=True)
        self.assertNotEqual(first["release"], second["release"])
        self.assertEqual((site / "instance/devices.yaml").read_text(), "owner configuration\n")
        self.assertEqual((site / "var/owner-state").read_text(), "keep state\n")
        self.assertEqual((site / "Makefile").read_text(), "# Owner's custom tooling\n")
        for identity in (first["release"], second["release"]):
            self.assertEqual(oh_release.manifest(site / "var/application-releases" / identity)["digest"], identity)
        self.assertEqual((site / oh_paths.SITE_FILE).stat().st_mode & 0o777, 0o600)

    def test_init_refuses_existing_files_symlinks_and_program_overlap(self):
        target = self.root / "not-empty"; target.mkdir(); (target / "keep").write_text("owner")
        before = hashes(target)
        with self.assertRaises(ValueError): oh_init.initialize(target, program=self.program)
        self.assertEqual(hashes(target), before)
        link = self.root / "site-link"; link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError): oh_init.initialize(link, program=self.program)
        with self.assertRaises(ValueError): oh_init.initialize(self.program, program=self.program)

    def test_upgrade_and_cache_refuse_internal_symlinks_before_any_write(self):
        for component in ("var",):
            with self.subTest(component=component):
                site = self.root / ("linked-" + component)
                oh_init.initialize(site, program=self.program)
                outside = self.program
                if (site / component).exists(): shutil.rmtree(site / component)
                (site / component).symlink_to(outside, target_is_directory=True)
                program_before, outside_before, site_before = hashes(self.program), hashes(outside), hashes(site)
                with self.assertRaises(ValueError): oh_init.initialize(site, program=self.program, upgrade=True)
                self.assertEqual(hashes(self.program), program_before)
                self.assertEqual(hashes(outside), outside_before)
                self.assertEqual(hashes(site), site_before)
                self.assertEqual(oh_release.manifest(self.program), self.release)
        alias = self.root / "cache-parent-alias"
        alias.symlink_to(self.program, target_is_directory=True)
        before = hashes(self.program)
        with self.assertRaises(ValueError): oh_release.cache(self.program, alias / "application-releases")
        with self.assertRaises(ValueError): oh_release.cache(self.program, self.program / "application-releases")
        self.assertEqual(hashes(self.program), before)

    def test_manifest_accepts_only_regular_mapped_bytecode_and_cache_copy_excludes_it(self):
        root = self.root / "cache-validation"
        source = root / "lib/reviewed.py"
        source.parent.mkdir(parents=True)
        source.write_text('VALUE = "reviewed"\n')
        release = oh_release.create({"lib/reviewed.py": source.read_bytes()})
        (root / oh_release.FILE).write_bytes(oh_release.encoded(release))
        with patch.object(sys, "pycache_prefix", None):
            compiled = Path(py_compile.compile(str(source), doraise=True))
        self.assertEqual(oh_release.manifest(root), release)
        cached = oh_release.cache(root, self.root / "mapped-release-cache")
        self.assertFalse(any(path.name == "__pycache__" for path in cached.rglob("*")))
        for name in ("unknown.txt", "unlisted." + sys.implementation.cache_tag + ".pyc"):
            extra = compiled.parent / name
            extra.write_bytes(compiled.read_bytes())
            with self.assertRaises(ValueError): oh_release.manifest(root)
            extra.unlink()
        nested = compiled.parent / "nested"
        nested.mkdir()
        with self.assertRaises(ValueError): oh_release.manifest(root)
        nested.rmdir()
        compiled.unlink(); compiled.symlink_to(source)
        with self.assertRaises(ValueError): oh_release.manifest(root)
        compiled.unlink(); compiled.parent.rmdir()
        compiled.parent.symlink_to(source.parent, target_is_directory=True)
        with self.assertRaises(ValueError): oh_release.manifest(root)

    def test_two_private_git_revisions_use_exact_release_and_rollback_old_helpers(self):
        site = self.root / "git-site"
        old = oh_init.initialize(site, program=self.program)
        # Version-one private sites historically committed their generated trees.
        # Keep this rollback fixture explicit even though new sites use schema two.
        descriptor = json.loads((site / oh_paths.SITE_FILE).read_text())
        descriptor["version"] = 1
        (site / oh_paths.SITE_FILE).write_text(json.dumps(descriptor))
        (site / "conf/services").mkdir(parents=True)
        (site / "conf/services/addons.cfg").write_text("package=minimal\n")
        def git(*args):
            return subprocess.run(["git", "-C", str(site), *args], check=True, text=True, capture_output=True).stdout.strip()
        git("init", "--initial-branch=fixture")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "commit.gpgsign", "false")
        git("add", "."); git("add", "-f", "conf"); git("commit", "-m", "first private site")
        first_commit = git("rev-parse", "HEAD")
        newer_program = self.root / "rollback-next"
        shutil.copytree(self.program, newer_program)
        changed = "lib/oh_atv.py"
        (newer_program / changed).write_bytes((newer_program / changed).read_bytes() + b"\n# v2 exact helper\n")
        newer = oh_release.create({name: (newer_program / name).read_bytes() for name in self.release["files"]}, "0.1.1.dev0")
        (newer_program / oh_release.FILE).write_bytes(oh_release.encoded(newer))
        oh_init.initialize(site, program=newer_program, upgrade=True)
        descriptor = json.loads((site / oh_paths.SITE_FILE).read_text())
        descriptor["version"] = 1
        (site / oh_paths.SITE_FILE).write_text(json.dumps(descriptor))
        git("add", "."); git("commit", "-m", "second private site")
        second_commit = git("rev-parse", "HEAD")
        var = site / "var"
        old_files, old_app = oh_deploy.deployment_files(site, first_commit, var)
        new_files, new_app = oh_deploy.deployment_files(site, second_commit, var)
        self.assertEqual(old_app["digest"], old["release"])
        self.assertEqual(new_app["digest"], newer["digest"])
        self.assertNotEqual(old_files[changed].data, new_files[changed].data)
        helper = self.root / "finite-helper"
        for name in oh_deploy.ATV_APP:
            path = helper / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old_files[name].data)
        # The deployed closure remains usable with only the standard library,
        # without the full application, a site, PyYAML or a Git checkout.
        isolated = {key: value for key, value in os.environ.items() if key not in {"THERMITE_SITE", "PYTHONPATH"}}
        for args in (("apple-tv", "--help"), ("apple-tv", "serve", "--help")):
            result = subprocess.run([sys.executable, "-S", str(helper / "bin/thermite"), *args],
                                    capture_output=True, text=True, env=isolated, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        layout = oh_deploy.State(self.root / "synthetic-state")
        for _, directories in layout.directories():
            for path in directories: path.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(oh_deploy.DeployError):
            oh_deploy.deploy(site, layout, var, second_commit)  # Boot gate remains mandatory.
        oh_deploy.deploy(site, layout, var, second_commit, force=True)
        self.assertEqual((layout.atv_app / changed).read_bytes(), new_files[changed].data)
        oh_deploy.deploy(site, layout, var, first_commit, force=True, how="rollback")
        self.assertEqual((layout.atv_app / changed).read_bytes(), old_files[changed].data)
        self.assertEqual(oh_deploy.read_record(layout)["application"], old_app)
        cache_file = var / "application-releases" / old["release"] / changed
        cache_file.chmod(0o600); cache_file.write_text("tampered")
        before = hashes(layout.root)
        with self.assertRaises(oh_deploy.DeployError):
            oh_deploy.deploy(site, layout, var, first_commit, force=True)
        self.assertEqual(hashes(layout.root), before, "bad rollback release refuses before any deployment write")

    def test_installed_commands_outside_checkout_write_only_private_site(self):
        envroot = self.root / "environment"
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(envroot)], check=True, capture_output=True)
        python = envroot / "bin/python"
        clean = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "THERMITE_SITE"}}
        clean.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
        install_environment = {key: value for key, value in clean.items() if key != "PYTHONDONTWRITEBYTECODE"}
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", str(self.wheel)],
                       check=True, capture_output=True, env=install_environment)
        entry = envroot / "bin/thermite"
        program = next(envroot.glob("lib/python*/site-packages/thermite_app/runtime"))
        ordinary = [p for p in program.rglob("*.pyc") if p.parent.name == "__pycache__"]
        self.assertTrue(ordinary, "ordinary pip-compiled caches remain supported")
        self.assertEqual(oh_release.manifest(program), self.release)
        # Forge matching timestamp/size caches for both earliest program imports.
        # dont_write_bytecode alone would still execute these unhashed bodies.
        for relative in ("lib/oh_paths.py", "lib/oh_cli/__init__.py"):
            source = program / relative
            compiled = source.parent / "__pycache__" / (source.stem + "." + sys.implementation.cache_tag + ".pyc")
            info = source.stat()
            code = compile("raise RuntimeError('UNHASHED_BYTECODE_EXECUTED')\n", str(source), "exec")
            compiled.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(info.st_mtime) & 0xffffffff,
                info.st_size & 0xffffffff) + marshal.dumps(code))
        self.assertEqual(oh_release.manifest(program), self.release)
        before = hashes(program)
        # Exercise the actual public PEP517 entry point too, without build
        # isolation/downloads. This is distinct from calling our wheel API.
        public_source = self.root / "public-source"
        for name, value in self.public.items():
            path = public_source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
        built = subprocess.run([str(python), "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
                                "--disable-pip-version-check", "--wheel-dir", str(self.root / "pep517-wheel"), str(public_source)],
                               env=clean, capture_output=True, text=True, timeout=45)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        cwd = self.root / "unrelated"; cwd.mkdir()
        hooks = self.root / "hooks"; hooks.mkdir()
        (hooks / "sitecustomize.py").write_text("import socket\ndef refuse(*a,**k): raise RuntimeError('offline proof forbids network')\nsocket.socket.connect=refuse\nsocket.socket.connect_ex=refuse\nsocket.socket.sendto=refuse\nsocket.create_connection=refuse\n")
        clean["PYTHONPATH"] = str(hooks)
        def command(*args, expected=0):
            run = subprocess.run([str(entry), *map(str, args)], cwd=cwd, env=clean, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
            return run.stdout
        command("--help"); command("--version"); command("theme", "check", "thermite")
        source_entry = subprocess.run([str(python), str(program / "bin/thermite"), "--help"],
                                      cwd=cwd, env=clean, capture_output=True, text=True, timeout=30)
        self.assertEqual(source_entry.returncode, 0, source_entry.stdout + source_entry.stderr)
        self.assertNotIn("UNHASHED_BYTECODE_EXECUTED", source_entry.stdout + source_entry.stderr)
        report = json.loads(command("version", "--json"))
        self.assertIsNone(report["openhab"]["url"])
        command("docs", "build", "--out", cwd / "docs")
        (cwd / "portable").mkdir()
        command("docs", "export", "--with-code", "--to", cwd / "portable")
        command("package", "--out", cwd / "repackaged")
        self.assertEqual((cwd / "repackaged" / self.wheel.name).read_bytes(), self.wheel.read_bytes(),
                         "rebuilding the exported/installed application must retain its exact release bytes")
        command("generate", expected=2)
        site = self.root / "installed site with spaces"
        command("init", site)
        self.assertFalse((site / "bin").exists())
        self.assertFalse((site / "Makefile").exists())
        command("--config", site, "check")
        command("check", "--config=" + str(site))
        command("--site", site, "generate", "--check")
        self.assertEqual(json.loads(command("--site", site, "validate", "--json"))["status"], "ok")
        # A config argument selects the site before module-level paths are imported.
        subprocess.run(["git", "-C", str(site), "init", "--initial-branch=fixture"],
                       check=True, capture_output=True)
        refused = subprocess.run([str(entry), "install", "--config", str(site), "--dry-run"],
                                 cwd=cwd, env=clean, capture_output=True, text=True, timeout=30)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("git rev-parse", refused.stderr)
        self.assertNotIn("unrecognized arguments", refused.stderr)
        self.assertFalse((site / "bin").exists())
        for options in (("--config",), ("--config", "--dry-run"), ("--config=" ,),
                        ("--config", site, "--site", site)):
            command("install", *options, expected=2)
        description = json.loads((site / oh_paths.SITE_FILE).read_text()); description["application"] = "9.9.9"
        (site / oh_paths.SITE_FILE).write_text(json.dumps(description))
        current = hashes(site)
        command("--site", site, "generate", expected=2)
        self.assertEqual(hashes(site), current)
        self.assertEqual(hashes(program), before, "installed program files must not receive configuration, cache or state")
