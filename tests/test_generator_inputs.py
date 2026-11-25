"""The generator works from what it is given: the house, the host facts,
the versions, and the files the house names, which are beside its house
file. Its modules import nothing that knows a site, a deploy or the
program, at their top or inside a function, which their syntax trees show;
and a build in a process whose selected site is empty shows that none of
them looks for one.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_brand  # noqa: E402
import oh_conf  # noqa: E402
import oh_inventory  # noqa: E402
import oh_render  # noqa: E402
from oh_features import remote, remote_system  # noqa: E402
from oh_integrations import globalcache_ir  # noqa: E402

#: The generator, below lib/: what builds conf/, metrics/ and the gateway's
#: contract from a house (oh_render, oh_metrics, oh_site_contract and the
#: packages they run), the house's own model, shape and reader (oh_conf,
#: oh_inventory, oh_shape, oh_semantic, oh_control4, oh_room_policy), and
#: what those import to draw or place a part of the pages (oh_brand,
#: oh_entry, oh_access_mode, oh_routes).
GENERATOR = ("oh_render.py", "oh_conf.py", "oh_inventory.py", "oh_shape.py", "oh_site_contract.py", "oh_integrations",
             "oh_features", "oh_metrics", "oh_room_policy", "oh_semantic.py", "oh_control4.py", "oh_brand", "oh_entry",
             "oh_access_mode.py", "oh_routes.py")
#: What it stands below. oh_paths is the selected site's path (and the
#: program's), which is the caller's to know.
ABOVE = ("oh_cli", "oh_deploy", "oh_install", "oh_paths", "oh_sandbox", "oh_site")

HOST = {"timezone": "Etc/UTC", "http_port": 14001, "https_port": 14002, "uid": 9001, "gid": 9001,
        "metrics": {"grafana_port": 14003}}
VERSIONS = {"openhab": {"version": "0.0.0"}}
SYSTEM = "html/remote/system.json"


def sources() -> list[Path]:
    found = []
    for name in GENERATOR:
        path = AREA / "lib" / name
        found += [path] if path.is_file() else [each for each in sorted(path.rglob("*.py")) if "tests" not in each.parts]
    return found


def imported(path: Path) -> set[str]:
    """The top-level name of every module a source imports, at its top or
    inside a function."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found


def mapping() -> dict:
    """The example house as its file holds it: read, and not yet a House."""
    return oh_inventory.load(AREA / "instance/devices.yaml")


def example() -> oh_conf.House:
    return oh_conf.House.load(AREA / "instance/devices.yaml")


def digests(files: dict) -> dict[str, str]:
    return {path: hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()
            for path, data in files.items()}


class Layering(unittest.TestCase):
    def test_every_generator_source_is_read(self):
        self.assertTrue(all((AREA / "lib" / name).exists() for name in GENERATOR))
        self.assertGreater(len(sources()), 50)

    def test_no_generator_module_imports_the_site_the_deploy_or_the_program(self):
        found = {str(path.relative_to(AREA)): sorted(imported(path) & set(ABOVE)) for path in sources()}
        self.assertEqual({name: above for name, above in found.items() if above}, {})

    def test_loading_the_generator_loads_none_of_them(self):
        code = ("import sys\n"
                "import oh_render, oh_metrics, oh_site_contract, oh_room_policy\n"
                f"print(sorted(set(sys.modules) & set({ABOVE!r})))\n")
        done = subprocess.run([sys.executable, "-B", "-c", code], cwd=AREA / "lib", capture_output=True, text=True,
                              timeout=120, env={"PATH": os.environ["PATH"]})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "[]\n", ""))


class Inputs(unittest.TestCase):
    def test_the_build_describes_the_host_and_the_versions_it_is_given(self):
        files = oh_render.render(example(), host=HOST, versions=VERSIONS)
        self.assertEqual(json.loads(files[SYSTEM]), remote_system.manifest(HOST, "0.0.0"))
        self.assertEqual([tool["port"] for tool in json.loads(files[SYSTEM])["tools"][:2]], [14001, 14003])

    def test_given_neither_it_uses_the_products_ports_and_the_applications_pin(self):
        self.assertEqual(json.loads(oh_render.render(example())[SYSTEM]), remote_system.described())

    def test_a_build_in_a_process_whose_site_is_empty_is_the_same_build(self):
        # The selected site has no house, no host file and no versions: a
        # generator that looked for one would fail, or build something else.
        code = ("import hashlib, json, sys\n"
                "import oh_conf, oh_render\n"
                "files = oh_render.render(oh_conf.House.load(sys.argv[1]))\n"
                "print(json.dumps({path: hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()\n"
                "                  for path, data in files.items()}))\n")
        with tempfile.TemporaryDirectory() as temporary:
            done = subprocess.run([sys.executable, "-B", "-c", code, str(AREA / "instance/devices.yaml")], cwd=temporary,
                                  capture_output=True, text=True, timeout=120,
                                  env={"PATH": os.environ["PATH"], "PYTHONPATH": str(AREA / "lib"),
                                       "THERMITE_SITE": temporary})
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual(os.listdir(temporary), [])
        self.assertEqual(json.loads(done.stdout), digests(oh_render.render(example())))


class OwnFiles(unittest.TestCase):
    """The files a house names are read from beside its house file."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.instance = Path(temporary.name) / "instance"
        (self.instance / "ir").mkdir(parents=True)
        (self.instance / "mine.json").write_text(json.dumps({"name": "mine", "extends": "thermite"}), encoding="utf-8")
        profile = {"version": 1, "make": "Sony", "model": "UBP-X800M2", "source": "fictional fixture",
                   "commands": {"play": {"code": "38000,1,1,24,24", "verified": True}}}
        (self.instance / "ir/disc.json").write_text(json.dumps(profile), encoding="utf-8")
        self.player = {"kind": "disc_player", "model": "UBP-X800M2",
                       "config": {"ir": {"model": "iTach IP2IR", "ip": "192.0.2.25", "connector": 1, "profile": "ir/disc.json"}}}
        # Whatever a test binds, the next one starts as this one did.
        bound = oh_conf.instance_at(None)
        bound.__enter__()
        self.addCleanup(bound.__exit__, None, None, None)

    def house(self, **more) -> Path:
        path = self.instance / "devices.yaml"
        path.write_text(json.dumps({**mapping(), **more}), encoding="utf-8")
        return path

    def test_reading_a_house_file_says_where_its_files_are(self):
        self.assertIsNone(oh_conf.instance())
        house = oh_conf.House.load(self.house(theme="mine.json"))
        self.assertEqual(house.instance_dir, self.instance)
        self.assertEqual(house.directory, self.instance)
        self.assertIsNone(oh_conf.instance())
        with oh_conf.instance_at(house.instance_dir):
            self.assertEqual(remote.palette(house.data).name, "mine")
            self.assertEqual(globalcache_ir.commissioned(self.player)["commands"], {"play": "38000,1,1,24,24"})
        self.assertIsNone(oh_conf.instance())
        self.assertEqual(oh_render.render(house)["html/remote/theme.css"],
                         oh_brand.css(oh_brand.load(self.instance / "mine.json")).encode("utf-8"))
        # A failed House.load() leaves no process-global directory behind either.
        bad = self.instance / "bad.yaml"
        bad.write_text(json.dumps({**mapping(), "version": 1}), encoding="utf-8")
        with self.assertRaises(oh_inventory.ConfigError):
            oh_conf.House.load(bad)
        self.assertIsNone(oh_conf.instance())

    def test_a_build_reads_its_own_houses_files_whichever_house_was_read_last(self):
        house = oh_conf.House.load(self.house(theme="mine.json"))
        with tempfile.TemporaryDirectory() as other:
            elsewhere = Path(other) / "instance"
            elsewhere.mkdir()
            (elsewhere / "devices.yaml").write_text(json.dumps(mapping()), encoding="utf-8")
            other_house = oh_conf.House.load(elsewhere / "devices.yaml")
            self.assertEqual((oh_conf.instance(), house.instance_dir, other_house.instance_dir),
                             (None, self.instance, elsewhere))
            self.assertEqual(oh_render.render(house)["html/remote/theme.css"],
                             oh_brand.css(oh_brand.load(self.instance / "mine.json")).encode("utf-8"))
            self.assertIsNone(oh_conf.instance())

    def test_a_block_binds_them_for_a_house_that_is_a_mapping_and_gives_them_back(self):
        with oh_conf.instance_at(self.instance):
            self.assertEqual(oh_render.validate({**mapping(), "theme": "mine.json"}), [])
            self.assertEqual(oh_conf.House({**mapping(), "theme": "mine.json"}).data["theme"], "mine.json")
            self.assertEqual(globalcache_ir.commissioned(self.player)["commands"], {"play": "38000,1,1,24,24"})
        self.assertIsNone(oh_conf.instance())

    def test_a_house_read_from_no_file_that_names_one_is_refused(self):
        problems = oh_render.validate({**mapping(), "theme": "mine.json"})
        self.assertEqual(problems, ["theme: mine.json: a theme file is read from beside the house file, and this "
                                    "house was read from none"])
        with self.assertRaises(oh_inventory.ConfigError) as refused:
            oh_conf.House({**mapping(), "theme": "mine.json"})
        self.assertEqual(refused.exception.problems, problems)
        with self.assertRaisesRegex(globalcache_ir.ProfileError, "read from beside the house file"):
            globalcache_ir.commissioned(self.player)

    def test_a_built_in_theme_and_a_house_that_names_no_file_need_no_directory(self):
        self.assertEqual(remote.palette({"theme": "thermite"}).name, "thermite")
        self.assertEqual(oh_render.validate(mapping()), [])
        self.assertIsNone(globalcache_ir.commissioned({"kind": "disc_player", "model": "UBP-X800M2"}))

    def test_a_file_outside_that_directory_is_still_refused(self):
        (self.instance.parent / "outside.json").write_text(json.dumps({"name": "outside", "extends": "thermite"}))
        with oh_conf.instance_at(self.instance), self.assertRaises(oh_brand.PaletteError):
            remote.palette({"theme": "../outside.json"})


if __name__ == "__main__":
    unittest.main()
