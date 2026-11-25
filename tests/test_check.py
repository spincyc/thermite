"""thermite check reads the house file, the host file and versions.json,
builds in memory everything they generate, and writes nothing: whatever a
site's ignored conf/ holds, it is neither read nor touched. That a conf
agrees with its house is no longer read back from disk; it is the build's
own check (oh_render.build), taken from the Conf in memory.
"""
from __future__ import annotations

import contextlib
import copy
import io
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

import oh_cli  # noqa: E402
import oh_host  # noqa: E402
import oh_inventory as inventory  # noqa: E402
import oh_paths  # noqa: E402
import oh_render as render  # noqa: E402
from oh_cli import check, lint, validate  # noqa: E402
from oh_conf import Conf, House  # noqa: E402
from oh_integrations import sony_ip_control as sony  # noqa: E402
from tests import test_generated_output  # noqa: E402

EXAMPLE = inventory.load(AREA / "instance/devices.yaml")
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


def run(*argv: str) -> tuple[int, str, str]:
    """thermite ARGV in this process: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(oh_paths, "require_compatible"):
        code = oh_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def called(main, *argv: str) -> tuple[int, str, str]:
    """A verb's main(ARGV), called as is: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


HOUSE = str(AREA / "instance/devices.yaml")
OK = {"devices": 1, "items": 2, "notes": [], "problems": [], "status": "ok", "things": 1}
PROBLEMS = {**OK, "problems": ["conf/yaml/more.yaml: Thing http:url:stranger: 'stranger' is not a device id"],
            "status": "problems"}
BUILT = {"conf": 3, "metrics": 2, "units": 1}
UNBUILT = {"error": "instance/host.yaml: unknown setting state_root; nothing can be generated", "problems": []}
UNREAD = {"error": "[Errno 2] No such file or directory: 'versions.json'; nothing can be generated", "problems": []}
CLEAN = {"secret_scan": {"files": 4, "findings": []}, "doc_targets": {"documents": 2, "stale": []}}
FOUND = {"secret_scan": {"files": 4, "findings": ["notes.txt:1: a credential-looking line"]},
         "doc_targets": {"documents": 2, "stale": ["README.md: make no-such-target: the Makefile has no such target"]}}


class Steps(unittest.TestCase):
    """Which steps thermite check runs, in what order, and its exit status:
    each step's own report stands in for the step."""

    def steps(self, house=OK, build=(BUILT, 0), linted=CLEAN):
        ran: list[str] = []

        def step(name, value):
            def stand_in(*_):
                ran.append(name)
                return value
            return stand_in
        scope = contextlib.ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(mock.patch.object(validate, "checked", side_effect=step("house", house)))
        scope.enter_context(mock.patch.object(check, "built", side_effect=step("build", build)))
        scope.enter_context(mock.patch.object(lint, "checked", side_effect=step("lint", linted)))
        return ran

    def test_each_step_named_runs_once_and_always_in_the_same_order(self):
        for argv, expected in (([], ["house", "build", "lint"]), (["lint"], ["lint"]), (["build"], ["build"]),
                               (["lint", "house"], ["house", "lint"]), (["build", "house", "build"], ["house", "build"]),
                               (["house", "build", "lint"], ["house", "build", "lint"])):
            with self.subTest(argv=argv):
                ran = self.steps()
                code, out, err = run("check", *argv)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(ran, expected)

    def test_a_step_that_is_not_one_is_a_wrong_switch(self):
        ran = self.steps()
        with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()) as err:
            check.main(["generate"])
        self.assertEqual(refused.exception.code, 2)
        self.assertIn("invalid choice: 'generate'", err.getvalue())
        self.assertEqual(ran, [])

    def test_every_step_runs_whatever_the_one_before_found_and_the_worst_is_the_exit_status(self):
        for house, build, linted, expected in ((OK, (BUILT, 0), CLEAN, 0), (PROBLEMS, (BUILT, 0), CLEAN, 1),
                                               (OK, (UNBUILT, 1), CLEAN, 1), (OK, (BUILT, 0), FOUND, 1),
                                               (PROBLEMS, (UNBUILT, 1), FOUND, 1), (PROBLEMS, (UNREAD, 2), FOUND, 2)):
            with self.subTest(house=house["status"], build=build[1], lint=linted is FOUND):
                ran = self.steps(house, build, linted)
                code, out, _ = run("check")
                self.assertEqual(code, expected)
                self.assertEqual(ran, ["house", "build", "lint"])
                ran.clear()
                code, out, _ = run("check", "--json")
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(out), {"house": house, "build": build[0], "lint": linted,
                                                   "status": ("ok", "problems", "unreadable")[expected]})

    def test_each_step_says_what_it_said_as_its_own_verb_under_check_s_name(self):
        self.steps(PROBLEMS, (UNBUILT, 1), FOUND)
        code, out, err = run("check")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            "house file: 1 devices, conf: 1 Things, 2 Items, 1 problems",
            "secret scan: 4 files, 1 findings",
            "make targets in the docs: 2 documents, 1 stale"])
        self.assertEqual(err.splitlines(), [
            f"thermite check: {PROBLEMS['problems'][0]}",
            f"thermite check: {UNBUILT['error']}",
            f"thermite check: {FOUND['secret_scan']['findings'][0]}",
            f"thermite check: {FOUND['doc_targets']['stale'][0]}"])

    def test_json_holds_only_the_steps_it_ran_and_prints_nothing_else(self):
        self.steps(PROBLEMS, (BUILT, 0), FOUND)
        for argv, keys in (([], {"house", "build", "lint", "status"}), (["lint"], {"lint", "status"}),
                           (["house", "build"], {"house", "build", "status"})):
            with self.subTest(argv=argv):
                code, out, err = run("check", "--json", *argv)
                self.assertEqual((code, err, out.count("\n")), (1, "", 1))
                self.assertEqual(set(json.loads(out)), keys)

    def test_no_doc_targets_to_check_is_null_and_no_problem(self):
        with mock.patch.object(lint, "secret_scan", return_value={"files": 2, "findings": []}), \
                mock.patch.object(lint, "doc_targets", return_value=None):
            self.assertEqual(run("check", "lint"), (0, "secret scan: 2 files, 0 findings\n", ""))
            code, out, _ = run("check", "lint", "--json")
        self.assertEqual((code, json.loads(out)), (0, {"lint": {"secret_scan": {"files": 2, "findings": []},
                                                                "doc_targets": None}, "status": "ok"}))


class Retired(unittest.TestCase):
    """thermite validate and thermite lint: thermite check house and
    thermite check lint now, and each runs as before after a warning."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def house(self, data: dict) -> str:
        path = self.root / "devices.yaml"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    @staticmethod
    def warning(verb: str) -> str:
        return (f"thermite {verb}: {oh_cli.RETIRED[verb].instead} replaces it; thermite {verb} goes in the next "
                "release\n")

    def test_validate_warns_and_runs_as_before_and_check_house_says_the_same(self):
        broken = self.house({**EXAMPLE, "housing": {}})
        for argv in ((), ("--json",)):
            for house, expected in ((HOUSE, 0), (broken, 2)):
                with self.subTest(argv=argv, house=house):
                    retired = run("validate", "--house", house, *argv)
                    checked = run("check", "house", "--house", house, *argv)
                    # validate keeps its old exit status until it goes; check found a problem.
                    self.assertEqual(retired[0], expected)
                    self.assertEqual(checked[0], min(expected, 1))
                    self.assertTrue(retired[2].startswith(self.warning("validate")), retired[2])
                    said = retired[2][len(self.warning("validate")):]
                    if not argv and expected == 0:
                        self.assertEqual(called(validate.main, "--house", house), (expected, retired[1], said))
                    if argv:
                        self.assertEqual(json.loads(checked[1]),
                                         {"house": json.loads(retired[1]),
                                          "status": "ok" if expected == 0 else "problems"})
                        self.assertEqual((checked[2], said), ("", ""))
                    else:
                        self.assertEqual(checked[1:], (retired[1], said.replace("thermite validate: ",
                                                                                "thermite check: ")))

    def test_what_the_house_file_generates_disagreeing_is_a_problem_of_validate_and_of_check(self):
        with stranger():
            retired = run("validate", "--house", HOUSE)
            checked = run("check", "house", "build", "--house", HOUSE)
            built = run("check", "build", "--json", "--house", HOUSE)
        self.assertEqual(retired[0], 1)
        problem = ("conf/yaml/sony_ip_control.yaml: Thing http:url:stranger: 'stranger' is not a device id in "
                   "instance/devices.yaml")
        self.assertIn(f"thermite validate: {problem}\n", retired[2])
        self.assertEqual(checked[0], 1)
        self.assertEqual(checked[1], retired[1])
        self.assertEqual(checked[2].splitlines()[-2:], [
            f"thermite check: {problem}",
            "thermite check: what the house file generates disagrees with it, in 1 place; nothing done"])
        self.assertEqual((built[0], json.loads(built[1])), (1, {
            "build": {"error": "what the house file generates disagrees with it, in 1 place; nothing done",
                      "problems": [problem]}, "status": "problems"}))

    def test_lint_warns_and_runs_as_before_and_check_lint_says_the_same(self):
        for linted, expected in ((CLEAN, 0), (FOUND, 1)):
            with self.subTest(expected=expected), \
                    mock.patch.object(lint, "secret_scan", return_value=linted["secret_scan"]), \
                    mock.patch.object(lint, "doc_targets", return_value=linted["doc_targets"]):
                retired = run("lint")
                direct = called(lint.main)
                checked = run("check", "lint")
                self.assertEqual(retired[0], expected)
                self.assertEqual(retired, (direct[0], direct[1], self.warning("lint") + direct[2]))
                self.assertEqual(checked, (direct[0], direct[1], direct[2].replace("thermite lint: ", "thermite check: ")))
                self.assertEqual(direct[1], f"secret scan: 4 files, {expected} findings\n"
                                            f"make targets in the docs: 2 documents, {expected} stale\n")

    def test_check_house_build_reuses_loaded_house_and_built_conf(self):
        with mock.patch.object(House, "load", wraps=House.load) as load_spy, \
                mock.patch.object(render, "build", wraps=render.build) as build_spy:
            code, out, err = run("check", "house", "build", "--house", HOUSE)
        self.assertEqual(code, 0, err)
        self.assertIn("house file:", out)
        self.assertIn("build:", out)
        self.assertEqual(load_spy.call_count, 1, "House.load should run once across house + build")
        self.assertEqual(build_spy.call_count, 1, "oh_render.build should run once across house + build")


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

    def test_a_schema_1_site_is_refused_by_name_and_nothing_of_it_is_read(self):
        (self.site / "thermite-site.json").write_text(
            json.dumps({"version": 1, "application": "0.1.0.dev0", "release": "a" * 64}), encoding="utf-8")
        (self.site / "instance/devices.yaml").write_text("not a house\n", encoding="utf-8")
        before = self.files()
        done = self.thermite("check")
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertEqual(done.stderr, (
            f"thermite: {self.site} is a schema-1 site (thermite-site.json says version 1): the retired layout, whose "
            f"generated conf/ was committed. This thermite reads only version 2. Commit {oh_paths.SCHEMA_1_READER[:12]} "
            "of thermite is the last that reads version 1, and its upgrade rewrites the descriptor as version 2: run "
            f"`thermite upgrade --config {self.site} --revision {oh_paths.SCHEMA_1_READER}`, then upgrade again "
            "(`thermite --help` lists the verbs)\n"))
        self.assertEqual(self.files(), before)

    def test_a_host_file_it_cannot_use_stops_the_build_and_the_lint_still_runs(self):
        host = oh_host.load(self.site / "instance/host.yaml")
        (self.site / "instance/host.yaml").write_text(json.dumps({**host, "state_root": "/mnt/example"}), encoding="utf-8")
        before = self.files()
        done = self.thermite("check")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr.splitlines()[-1][:len("thermite check: ")], "thermite check: ")
        self.assertIn("instance/host.yaml: unknown setting state_root", done.stderr)
        self.assertTrue(done.stderr.rstrip().endswith("; nothing can be generated"), done.stderr)
        self.assertNotIn("build: ", done.stdout)
        self.assertIn("secret scan: ", done.stdout)
        self.assertEqual(self.files(), before)
        linted = self.thermite("check", "lint")
        self.assertEqual(linted.returncode, 0, linted.stderr)
        self.assertEqual(self.files(), before)

    def test_a_house_file_with_a_problem_stops_it_first(self):
        for name, text in (("a problem", json.dumps({**EXAMPLE, "housing": {}})), ("not YAML", "version: 1\nrooms: {\n")):
            with self.subTest(name):
                (self.site / "instance/devices.yaml").write_text(text, encoding="utf-8")
                before = self.files()
                done = self.thermite("check")
                self.assertEqual((done.returncode, done.stdout), (1, ""))
                self.assertIn("thermite check: ", done.stderr)
                self.assertIn("housing" if name == "a problem" else "is not YAML", done.stderr)
                self.assertEqual(self.files(), before)
                done = self.thermite("check", "--json")
                self.assertEqual((done.returncode, json.loads(done.stdout)["status"]), (1, "problems"))
                self.assertEqual(set(json.loads(done.stdout)), {"house", "status"}, "nothing after house ran")

    def test_a_file_it_cannot_read_is_a_check_that_could_not_run(self):
        (self.site / "instance/devices.yaml").unlink()
        done = self.thermite("check", "--json")
        self.assertEqual((done.returncode, json.loads(done.stdout)["status"]), (2, "unreadable"))
        self.assertEqual(set(json.loads(done.stdout)), {"house", "status"})
        (self.site / "instance/devices.yaml").write_bytes((AREA / "instance/devices.yaml").read_bytes())
        (self.site / "versions.json").unlink()
        done = self.thermite("check", "build")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertTrue(done.stderr.rstrip().endswith("; nothing can be generated"), done.stderr)
        (self.site / "versions.json").write_text("{", encoding="utf-8")
        done = self.thermite("check", "build")
        self.assertEqual(done.returncode, 1, done.stderr)


if __name__ == "__main__":
    unittest.main()
