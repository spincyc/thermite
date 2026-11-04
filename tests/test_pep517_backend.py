"""The PEP 517 backend: metadata from [project], reproducible sdist and wheel.

The installed application builds a newer release with `python -m venv` and
`pip install --isolated <checkout>`, then imports thermite_app and verifies
its release manifest. These tests repeat that offline, without build
isolation (which would download PyYAML).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_application_update  # noqa: E402
import oh_package  # noqa: E402
import oh_release  # noqa: E402


def environment():
    clean = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "THERMITE_SITE"}}
    clean.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
    return clean


class Backend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = Path(os.environ.get("TMPDIR") or AREA / ".scratch/pep517-backend")
        scratch.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="pep517-", dir=scratch)
        cls.root = Path(cls.temporary.name)
        cls.public = oh_package.public_files()
        cls.wheel, cls.release = oh_package.wheel(cls.public, cls.root / "wheel")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def project(self, text):
        return oh_package.project({**self.public, "pyproject.toml": text.encode()})

    def test_metadata_follows_the_project_table(self):
        table = oh_package.project(self.public)
        with zipfile.ZipFile(self.wheel) as archive:
            info = f"{oh_package.distribution(table)}-{oh_release.VERSION}.dist-info"
            self.assertEqual(archive.read(info + "/METADATA"), oh_package.metadata(table))
            self.assertEqual(archive.read(info + "/entry_points.txt"), b"[console_scripts]\nthermite = thermite_app:main\n")
        changed = self.project('[project]\nname = "thermite-app"\ndynamic = ["version"]\n'
                               'description = "Another summary"\nrequires-python = ">=3.12"\n'
                               'dependencies = ["PyYAML>=6,<7", "Django>=5"]\nlicense = "LicenseRef-Proprietary"\n'
                               'optional-dependencies = {site = ["uvicorn; python_version < \'3.15\'"]}\n')
        text = oh_package.metadata(changed).decode()
        self.assertTrue(text.startswith("Metadata-Version: 2.4\nName: thermite-app\n"))
        for line in ("Summary: Another summary", "Requires-Python: >=3.12", "Requires-Dist: Django>=5",
                     "License-Expression: LicenseRef-Proprietary", "Provides-Extra: site",
                     "Requires-Dist: uvicorn; (python_version < '3.15') and extra == \"site\""):
            self.assertIn(line + "\n", text)
        self.assertEqual(oh_package.entry_points(changed), b"")

    def test_unsupported_or_unsafe_project_values_fail_closed(self):
        base = '[project]\nname = "thermite-app"\ndynamic = ["version"]\n'
        for extra, problem in (('readme = "README.md"\n', "unsupported key readme"),
                               ('description = "two\\nlines"\n', "description must be one line"),
                               ('scripts = {thermite = "os:system"}\n', "must run thermite_app:main"),
                               ('license = {file = "LICENSE"}\n', "license must be"),
                               ('dependencies = "PyYAML"\n', "dependencies must be a list")):
            with self.subTest(problem=problem), self.assertRaisesRegex(ValueError, problem):
                self.project(base + extra)
        with self.assertRaisesRegex(ValueError, "unsupported key version"):
            self.project('[project]\nname = "thermite-app"\nversion = "1"\n')
        with self.assertRaisesRegex(ValueError, "dynamic"):
            self.project('[project]\nname = "thermite-app"\n')
        with self.assertRaisesRegex(ValueError, "readable"):
            self.project("[build-system]\n")

    def test_sdist_is_exactly_the_reviewed_files_and_reproducible(self):
        modes = oh_package._modes(self.public)
        first = oh_package.sdist(self.public, self.root / "sdist-a", modes)
        second = oh_package.sdist(self.public, self.root / "sdist-b", modes)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.name, f"thermite_app-{oh_release.VERSION}.tar.gz")
        base = f"thermite_app-{oh_release.VERSION}/"
        with tarfile.open(first) as archive:
            members = {member.name: member for member in archive.getmembers()}
            self.assertEqual(set(members), {base + name for name in self.public} | {base + "PKG-INFO"})
            self.assertTrue(all(member.isfile() and member.uid == 0 and member.mtime == oh_package.EPOCH
                                for member in members.values()))
            self.assertEqual(members[base + "bin/thermite"].mode, 0o755)
            self.assertEqual(members[base + "pyproject.toml"].mode, 0o644)
            self.assertEqual(archive.extractfile(base + "PKG-INFO").read(),
                             oh_package.metadata(oh_package.project(self.public)))
            for name in ("lib/oh_package.py", ".thermite-export.json"):
                self.assertEqual(archive.extractfile(base + name).read(), self.public[name])
            archive.extractall(self.root / "unpacked", filter="data")
        # A wheel built from the unpacked sdist is the very same release.
        source = self.root / "unpacked" / base.rstrip("/")
        out = self.root / "from-sdist"
        built = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, 'lib'); import oh_package; "
                                "print(oh_package.build_wheel(sys.argv[1]))", str(out)],
                               cwd=source, env=environment(), capture_output=True, text=True, timeout=120)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual((out / built.stdout.strip()).read_bytes(), self.wheel.read_bytes())

    def test_pip_installs_a_checkout_that_verifies_like_the_update_workflow(self):
        checkout = self.root / "checkout"
        for name, value in self.public.items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        venv = self.root / "candidate"
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], check=True,
                       capture_output=True, timeout=120)
        python = venv / "bin/python"
        installed = subprocess.run([str(python), "-I", "-m", "pip", "install", "--isolated", "--no-index", "--no-deps",
                                    "--no-build-isolation", str(checkout)],
                                   env={key: value for key, value in environment().items()
                                        if key != "PYTHONDONTWRITEBYTECODE"},
                                   capture_output=True, text=True, timeout=300)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertTrue((venv / "bin/thermite").is_file())
        verified = subprocess.run([str(python), "-I", "-B", "-c", oh_application_update.VERIFY], env=environment(),
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        result = json.loads(verified.stdout)
        self.assertEqual((result["digest"], result["application"]), (self.release["digest"], oh_release.VERSION))


if __name__ == "__main__":
    unittest.main()
