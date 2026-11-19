"""thermite check reads the house file, the host file and versions.json,
builds in memory everything they generate, and writes nothing: whatever a
site's ignored conf/ holds, it is neither read nor touched. That a conf
agrees with its house is no longer read back from disk; it is the build's
own check (oh_render.build), taken from the Conf in memory.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA))
sys.path.insert(0, str(AREA / "lib"))

import oh_inventory as inventory  # noqa: E402
import oh_render as render  # noqa: E402
from oh_conf import Conf, House  # noqa: E402
from oh_integrations import sony_ip_control as sony  # noqa: E402
from tests import test_generated_output  # noqa: E402

EXAMPLE = json.loads((AREA / "instance/devices.yaml").read_text(encoding="utf-8"))
STRANGER = "http:url:stranger"


def stranger():
    """The Sony integration, rendering one Thing more: of no device, at no
    device's address."""
    rendered = sony.render

    def with_a_stranger(device, house, out):
        rendered(device, house, out)
        out.things[STRANGER] = {"configuration": {"baseURL": "http://192.0.2.250/"}}
    return mock.patch.object(sony, "render", with_a_stranger)


class Agreement(unittest.TestCase):
    def test_the_things_and_items_in_memory_are_the_ones_the_written_files_define(self):
        # The check reads the Conf, not the text generate writes: for every
        # fixture house, each names the same Things and Items in the same
        # files, with the same addresses.
        houses = test_generated_output.houses()
        self.assertGreater(len(houses), 20)
        for name, house in houses.items():
            conf = render.build(House.load(house) if isinstance(house, Path) else House(copy.deepcopy(house)))
            things, items, problems = render.objects(conf)
            written = {"things": {}, "items": {}}
            for path, text in conf.render().items():
                if path.startswith("yaml/"):
                    document = yaml.safe_load(text) or {}
                    for section, table in written.items():
                        table.update({str(key): (f"conf/{path}", inventory.addresses(body))
                                      for key, body in (document.get(section) or {}).items()})
            with self.subTest(house=name):
                self.assertEqual(problems, [])
                self.assertEqual({uid: (thing["file"], inventory.addresses(thing["body"])) for uid, thing in things.items()},
                                 written["things"])
                self.assertEqual({name: item["file"] for name, item in items.items()},
                                 {name: file for name, (file, _) in written["items"].items()})

    def test_what_disagrees_is_named_with_the_file_that_defines_it(self):
        house = House(copy.deepcopy(EXAMPLE))
        things, items, _ = render.objects(render.build(house))
        where = things["http:url:example_tv"]["file"]
        things[STRANGER] = {"file": "conf/yaml/more.yaml", "body": {"configuration": {"baseURL": "http://192.0.2.250/"}}}
        things["http:url:example_tv"] = {"file": where, "body": {"configuration": {"baseURL": "http://192.0.2.11/"}}}
        items["floating"] = {"file": "conf/yaml/more.yaml", "body": {"type": "Switch"}}
        del items["example_matrix"]
        self.assertEqual(inventory.cross_check(house, things, items), [
            f"{where}: Thing http:url:example_tv uses 192.0.2.11, but example_tv is at 192.0.2.10",
            "conf/yaml/more.yaml: Thing http:url:stranger: 'stranger' is not a device id in instance/devices.yaml",
            "conf/yaml/more.yaml: Item floating is neither loc_<room>[_<point>], named after a device id, nor one of "
            "HOUSE_ITEMS",
            "example_matrix: has Things but no equipment group Item named example_matrix",
        ])

    def test_one_thing_or_item_in_two_files_is_a_problem(self):
        conf = Conf()
        for name in ("yaml/a.yaml", "yaml/b.yaml"):
            file = conf.yaml_file(name, ())
            file.things["http:url:example_tv"] = {}
            file.items["example_tv"] = {"type": "Group"}
        things, items, problems = render.objects(conf)
        self.assertEqual(problems, ["conf/yaml/b.yaml: Thing http:url:example_tv also defined in conf/yaml/a.yaml",
                                    "conf/yaml/b.yaml: Item example_tv also defined in conf/yaml/a.yaml"])
        self.assertEqual((things["http:url:example_tv"]["file"], items["example_tv"]["file"]),
                         ("conf/yaml/b.yaml", "conf/yaml/b.yaml"))

    def test_a_file_written_as_text_where_things_or_items_are_read_is_a_problem(self):
        # What the reader of conf/ on disk found by parsing every syntax, the
        # build refuses to hold: a definition outside its own YAML files.
        conf = Conf()
        conf.files.update({"things/more.things": "Thing http:url:stranger \"x\"\n", "items/more.items": "Switch x\n",
                           "yaml/more.yaml": "things: {}\n", "automation/js/rule.js": "// a rule\n"})
        self.assertEqual(render.objects(conf)[2], [
            f"conf/{path}: a file that can define Things or Items must be one of Conf.yaml_file's, which the "
            "house is checked against" for path in ("items/more.items", "things/more.things", "yaml/more.yaml")])

    def test_a_build_that_disagrees_with_its_house_raises_and_carries_what_it_built(self):
        with stranger(), self.assertRaises(render.Disagreement) as caught:
            render.build(House(copy.deepcopy(EXAMPLE)))
        error = caught.exception
        self.assertIsInstance(error, inventory.ConfigError)
        self.assertEqual(error.lines(), [
            "conf/yaml/sony_ip_control.yaml: Thing http:url:stranger: 'stranger' is not a device id in "
            "instance/devices.yaml",
            "what the house file generates disagrees with it, in 1 place; nothing done"])
        self.assertIn(STRANGER, render.objects(error.conf)[0])
        # So render(), which every writer calls, hands out no such conf either.
        with stranger(), self.assertRaises(render.Disagreement):
            render.render(House(copy.deepcopy(EXAMPLE)))


class Site(unittest.TestCase):
    """The verb itself, on a checkout without a descriptor whose ignored
    conf/ holds what an earlier release and its owner left there."""

    LEFT = {"conf/automation/js/thermite_entry.js": b"// Generated by bin/thermite generate; do not edit.\n",
            "conf/automation/js/node_modules/thermite-entry/filter.jar": b"PK\x03\x04",
            "conf/yaml/sony.yaml": b"# Generated by bin/thermite generate from instance/devices.yaml; do not edit.\n"
                                   b"things:\n  http:url:someone_else:\n    configuration:\n"
                                   b"      baseURL: http://192.0.2.99/\n",
            "conf/yaml/broken.yaml": b"things: [\n",
            "conf/notes.txt": b"the owner's notes\n"}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name) / "site"
        (self.site / "instance").mkdir(parents=True)
        for name in ("instance/devices.yaml", "instance/host.yaml", "versions.json"):
            (self.site / name).write_bytes((AREA / name).read_bytes())
        (self.site / "Makefile").write_text("check:\n\ttrue\n", encoding="utf-8")
        for name, data in self.LEFT.items():
            (self.site / name).parent.mkdir(parents=True, exist_ok=True)
            (self.site / name).write_bytes(data)

    def thermite(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(AREA / "bin/thermite"), *argv, "--config", str(self.site)],
                              cwd=self.site.parent, capture_output=True, text=True, timeout=120,
                              env={"PATH": os.environ["PATH"], "HOME": str(self.site.parent)})

    def files(self) -> dict[str, tuple[bytes, int]]:
        return {str(path.relative_to(self.site)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in sorted(self.site.rglob("*")) if path.is_file()}

    def test_check_passes_whatever_conf_holds_and_leaves_every_file_as_it_was(self):
        before = self.files()
        done = self.thermite("check")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.site / "metrics").exists() or (self.site / "deploy").exists())
        lines = done.stdout.splitlines()
        self.assertTrue(lines[0].startswith("house file: 5 devices, conf: "), lines)
        self.assertRegex(lines[1], r"^build: \d+ files of conf/, \d+ of metrics/, \d+ unit files; none written$")
        self.assertTrue(lines[2].startswith("secret scan: "), lines)
        # What it left is generate's to name, and --prune's to remove.
        named = self.thermite("generate", "--check")
        self.assertEqual(named.returncode, 1)
        for name in ("conf/automation/js/thermite_entry.js", "conf/automation/js/node_modules/thermite-entry/filter.jar"):
            self.assertIn(f"thermite generate: {name}: nothing generates it now; an earlier generate wrote it; "
                          "thermite generate --prune removes it", named.stderr.splitlines())
        self.assertEqual(self.files(), before)

    def test_a_host_file_it_cannot_use_stops_it_before_the_lint(self):
        host = json.loads((self.site / "instance/host.yaml").read_text(encoding="utf-8"))
        (self.site / "instance/host.yaml").write_text(json.dumps({**host, "state_root": "/mnt/example"}), encoding="utf-8")
        before = self.files()
        done = self.thermite("check")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stderr.splitlines()[-1][:len("thermite check: ")], "thermite check: ")
        self.assertIn("instance/host.yaml: unknown setting state_root", done.stderr)
        self.assertTrue(done.stderr.rstrip().endswith("; nothing can be generated"), done.stderr)
        self.assertNotIn("secret scan", done.stdout)
        self.assertEqual(self.files(), before)

    def test_a_house_file_with_a_problem_stops_it_first(self):
        (self.site / "instance/devices.yaml").write_text(json.dumps({**EXAMPLE, "housing": {}}), encoding="utf-8")
        before = self.files()
        done = self.thermite("check")
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("thermite check: ", done.stderr)
        self.assertIn("housing", done.stderr)
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
