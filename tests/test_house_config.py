"""The house file is closed and typed, its problems exit 2, and the house
verbs report what they find the same way."""
import contextlib
import copy
import io
import ipaddress
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_discover
import oh_docs
import oh_inventory
import oh_paths
import oh_render
import yaml
from oh_cli import dhcp_reservations, discover, generate, ir_profile, status, validate

EXAMPLE = json.loads((AREA / "instance/devices.yaml").read_text())
DOCUMENTS = [AREA / "README.md", AREA / "FAQ.md", *sorted((AREA / "docs").glob("*.md")),
             *sorted((AREA / "site/pages").glob("*.md"))]
FENCED = re.compile(r"^```ya?ml\n(.*?)^```", re.MULTILINE | re.DOTALL)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(oh_paths, "require_compatible"):
        code = oh_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/test-house-config"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def house(self, data, name="devices.yaml") -> Path:
        path = self.root / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path


class Loading(Scratch):
    def test_missing_and_unparsable_files_are_config_errors_with_their_position(self):
        with self.assertRaisesRegex(oh_inventory.ConfigError, r"nothing\.yaml: cannot be read"):
            oh_inventory.load(self.root / "nothing.yaml")
        broken = self.house("version: 1\ndevices: [\n  - a\n")
        with self.assertRaises(oh_inventory.ConfigError) as caught:
            oh_inventory.load(broken)
        self.assertEqual((caught.exception.line, caught.exception.column), (3, 3))
        self.assertTrue(str(caught.exception).startswith(f"{broken}:3:3: is not YAML"))

    def test_every_house_verb_exits_2_naming_the_file(self):
        broken = self.house("version: 1\nrooms: {\n")
        invalid = self.house({**EXAMPLE, "devices": [{**EXAMPLE["devices"][0], "model": 900}]}, "invalid.yaml")
        for argv in (["generate", "--check", "--house"], ["validate", "--house"], ["dhcp-reservations", "--house"]):
            for path in (self.root / "missing.yaml", broken, invalid):
                with self.subTest(argv=argv, path=path.name):
                    code, _, err = run(*argv, str(path))
                    self.assertEqual(code, 2)
                    self.assertTrue(err.startswith(f"thermite {argv[0]}: {path}"), err)
                    self.assertNotIn("Traceback", err)


class Closed(unittest.TestCase):
    def problems(self, change):
        data = copy.deepcopy(EXAMPLE)
        change(data)
        return oh_render.validate(data)

    def test_the_example_is_valid(self):
        self.assertEqual(oh_render.validate(copy.deepcopy(EXAMPLE)), [])

    def test_unknown_keys_and_names_are_refused_with_a_hint(self):
        cases = (
            (lambda d: d.update(rooom={}), "unknown top-level key 'rooom'; did you mean rooms?"),
            (lambda d: d["devices"][0].update(modle="X"), "example_tv: unknown key 'modle'; did you mean model?"),
            (lambda d: d["devices"][0]["controls"].update(observe=["sony_rst"]),
             "unknown observe integration 'sony_rst'; did you mean sony_rest?"),
            (lambda d: d["devices"][0]["controls"].update(observe=["sony_ip_control"]),
             "unknown observe integration 'sony_ip_control'; it is a control integration"),
            (lambda d: d["devices"][0]["controls"].update(obsrve=[]), "did you mean observe?"),
            (lambda d: d["devices"][0].update(room="example_wst"), "did you mean example_west?"),
        )
        for change, expected in cases:
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in problem for problem in self.problems(change)), self.problems(change))

    def test_owner_documentation_keys_are_text(self):
        # Real sites annotate devices with notes and the driver `thermite control4 summary` suggests.
        def documented(data):
            data["devices"][0].update(notes="Wall-mounted; IR blaster behind the bar.", control4="tv_ip_sony.c4z")
        self.assertEqual(self.problems(documented), [])
        self.assertTrue(any("notes 7 must be text" in problem for problem in self.problems(lambda d: d["devices"][0].update(notes=7))))

    def test_wrong_types_are_problems_not_crashes(self):
        cases = (
            (lambda d: d.update(devices={"example_tv": {}}), "devices must be a list"),
            (lambda d: d["devices"].append("example_tv"), "devices[5]: must be a mapping"),
            (lambda d: d["devices"][0].update(model=900), "example_tv: model 900 must be text"),
            (lambda d: d["devices"][0].update(id=["x"]), "must be text"),
            (lambda d: d["devices"][0]["controls"].update(observe="sony_rest"), "controls.observe must be a list"),
            (lambda d: d["devices"][0]["controls"].update(observe=[{"x": 1}]), "unknown observe integration"),
            (lambda d: d["devices"][0].update(controls=["sony_rest"]), "controls must be a mapping"),
            (lambda d: d.update(later=["x"]), "later[0]: must be a mapping"),
            (lambda d: d.update(services="network"), "services must be a list"),
            (lambda d: d["rooms"]["example_west"].update({"in": ["x"]}), "is not another room id"),
            (lambda d: d["rooms"]["example_west"].update(tag={"x": 1}), "is not an openHAB Location tag"),
            (lambda d: d.update(control4_rooms={"Den": ["x"]}), "which is not in rooms"),
        )
        for change, expected in cases:
            with self.subTest(expected=expected):
                found = self.problems(change)
                self.assertTrue(any(expected in problem for problem in found), found)

    def test_mac_is_optional_except_for_an_integration_that_needs_it(self):
        data = copy.deepcopy(EXAMPLE)
        del data["devices"][0]["mac"]
        self.assertEqual(oh_render.validate(data), [])
        self.assertEqual(oh_inventory.without_mac(data), ["example_tv"])
        self.assertNotIn("example-tv", [row[0] for row in oh_inventory.reservations(data)])
        self.assertEqual(oh_discover.compare(data, {})["no_mac"], ["example_tv"])
        sonos = {"id": "den_speaker", "room": "example_west", "kind": "speaker", "ip": "192.0.2.30",
                 "model": "Arc", "controls": {"observe": ["sonos"]}}
        data["devices"].append(sonos)
        self.assertIn("den_speaker: sonos needs the device's mac", oh_inventory.validate(data))

    def test_every_device_example_in_the_docs_validates(self):
        examples = [(document, block) for document in DOCUMENTS for block in FENCED.findall(document.read_text())
                    if re.search(r"^devices:", block, re.MULTILINE)]
        self.assertTrue(examples)
        for document, block in examples:
            with self.subTest(document=document.name):
                data = yaml.safe_load(block)
                data.setdefault("version", 1)
                self.assertEqual(oh_render.validate(data), [])


class Validate(Scratch):
    def test_it_runs_generates_checks_and_reports_a_missing_or_empty_conf(self):
        house = self.house(EXAMPLE)
        code, out, _ = run("validate", "--house", str(house), "--conf", str(self.root / "conf"), "--json")
        self.assertEqual(code, 1)
        self.assertIn("does not exist; run thermite generate", json.loads(out)["problems"][0])
        (self.root / "conf").mkdir()
        code, out, _ = run("validate", "--house", str(house), "--conf", str(self.root / "conf"), "--json")
        self.assertEqual((code, json.loads(out)["status"]), (1, "problems"))
        self.assertIn("defines no Things or Items", json.loads(out)["problems"][0])
        themed = self.house({**EXAMPLE, "theme": "no-such-theme"}, "themed.yaml")
        code, out, _ = run("validate", "--house", str(themed), "--json")
        self.assertEqual((code, json.loads(out)["status"]), (2, "invalid"))
        self.assertTrue(json.loads(out)["problems"][0].startswith("theme:"))


class Generate(Scratch):
    def test_prune_removes_only_stray_files_with_the_generated_header(self):
        house, conf = self.house(EXAMPLE), self.root / "conf"
        argv = ["generate", "--house", str(house), "--conf", str(conf), "--root", str(self.root)]
        self.assertEqual(run(*argv)[0], 0)
        (conf / "old.yaml").write_text("# Generated by bin/thermite generate; do not edit.\n")
        (conf / "mine.txt").write_text("the owner's notes\n")
        code, _, err = run(*argv)
        self.assertEqual(code, 1)
        self.assertIn("conf/old.yaml: nothing generates it now; an earlier generate wrote it; "
                      "thermite generate --prune removes it", err)
        self.assertIn("conf/mine.txt: nothing generates it now; thermite generate writes conf/ whole", err)
        code, out, _ = run(*argv, "--prune")
        self.assertEqual(code, 1)
        self.assertIn("removed conf/old.yaml", out)
        self.assertFalse((conf / "old.yaml").exists())
        self.assertTrue((conf / "mine.txt").exists())
        (conf / "mine.txt").unlink()
        self.assertEqual(run(*argv, "--check")[0], 0)


class Reservations(Scratch):
    def test_no_reservation_is_no_crash_and_a_missing_mac_is_named(self):
        data = copy.deepcopy(EXAMPLE)
        for device in data["devices"]:
            del device["mac"]
        code, out, err = run("dhcp-reservations", "--house", str(self.house(data)))
        self.assertEqual(code, 1)
        self.assertIn("0 reservations, 5 devices without a mac", out)
        self.assertIn("example_tv: no mac in the house file", err)
        code, out, _ = run("dhcp-reservations", "--house", str(self.house(EXAMPLE, "full.yaml")), "--csv")
        self.assertEqual((code, out.splitlines()[0]), (0, "alias,mac,ip"))


class Dispatch(unittest.TestCase):
    def test_the_site_check_follows_parsing_and_each_verbs_own_flag(self):
        refused = mock.patch.object(oh_paths, "require_compatible", side_effect=oh_paths.SiteError("wrong site"))
        err = io.StringIO()
        with refused as check, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            for argv in (["validate", "-h"], ["help", "validate"], ["validate", "--house", "x", "--help"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as exit_:
                    oh_cli.main(argv)
                self.assertEqual(exit_.exception.code, 0)
            with self.assertRaises(SystemExit) as exit_:
                oh_cli.main(["validate", "--no-such-switch"])
            self.assertEqual(exit_.exception.code, 2)
            check.assert_not_called()
            with self.assertRaises(SystemExit) as exit_:
                oh_cli.main(["validate"])
            self.assertEqual(exit_.exception.code, 2)
            self.assertIn("thermite: wrong site", err.getvalue())
            check.assert_called_once()
        self.assertFalse(oh_cli.VERBS["theme"].site)
        self.assertTrue(oh_cli.VERBS["generate"].site)
        self.assertFalse(oh_cli._site_check)

    def test_the_overview_names_no_deprecated_command_or_literal_port(self):
        text = oh_cli.overview()
        self.assertNotIn("deploy --dry-run", text)
        self.assertIn("metrics/", oh_cli.VERBS["generate"].summary)


class Lint(Scratch):
    def test_every_make_target_cited_in_code_must_exist(self):
        makefile = self.root / "Makefile"
        makefile.write_text("check:\n\ttrue\nupgrade:\n\ttrue\nsandbox-start:\n\ttrue\nX := 1\n")
        doc = self.root / "README.md"
        doc.write_text("Run `make check` or `make upgrade CONFIG=/x`, and make sure it works.\n"
                       "```sh\nsudo make -C ./src nothing\nmake sandbox-*\n```\n"
                       "`make X` is a variable, not a target.\n")
        problems = oh_docs.stale_targets(makefile, [doc])
        self.assertEqual([p.split(": ", 1)[1] for p in problems],
                         ["make target nothing is not defined", "make target X is not defined"])
        documents = [d for d in DOCUMENTS if d.exists()]
        self.assertEqual(oh_docs.stale_targets(AREA / "Makefile", documents), [])


class Reports(Scratch):
    def test_ir_profile_prints_text_and_json_only_with_json(self):
        from oh_integrations import globalcache_ir
        profile = {"model": "UBP-X800M2", "commands": {"play": "x"}, "pending": ["eject"], "missing": ["home"]}
        with mock.patch.object(globalcache_ir, "load_profile", return_value=profile):
            code, out, _ = run("ir-profile", "profile.json")
            self.assertEqual(code, 0)
            self.assertIn("verified play", out)
            self.assertIn("transmitted nothing", out)
            code, out, _ = run("ir-profile", "profile.json", "--json")
        self.assertEqual(json.loads(out)["verified"], ["play"])
        self.assertEqual(len(out.splitlines()), 1)

    def test_status_exits_1_on_drift_or_runtime_down_and_has_json(self):
        args = SimpleNamespace(state=self.root, revision=None, json=True)
        record = {"commit": "a" * 40, "subject": "s", "conf": {}}
        patches = (mock.patch.object(status.deploy, "read_record", return_value=record),
                   mock.patch.object(status.deploy, "resolve", return_value="a" * 40),
                   mock.patch.object(status.deploy, "drift", return_value={"modified": [], "missing": [], "foreign": []}),
                   mock.patch.object(status.deploy, "installed_units", return_value=[]),
                   mock.patch.object(status.deploy, "service_states", return_value={"thermite.service": "active"}),
                   mock.patch.object(status.deploy, "production_url", return_value="http://127.0.0.1:1"),
                   mock.patch.object(status.deploy, "lan_address", return_value=None),
                   mock.patch.object(status.deploy, "hostname", return_value="fixture"),
                   mock.patch.object(status.deploy, "metrics_installed", return_value=False),
                   mock.patch.object(status.deploy, "deploys", return_value=[]))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(status.deploy, "State", return_value=SimpleNamespace(
                record=self.root / "record", conf=self.root / "conf", atv_app=self.root / "a",
                metrics=self.root / "m", exporter_app=self.root / "e")))
            version = stack.enter_context(mock.patch.object(status.deploy, "openhab_version", return_value="5.0"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(status.show(args, {}), 0)
            report = json.loads(out.getvalue())
            self.assertEqual((report["problems"], report["runtime"]["version"]), ([], "5.0"))
            version.return_value = None
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(status.show(args, {}), 1)
            self.assertEqual(json.loads(out.getvalue())["problems"], ["runtime http://127.0.0.1:1: not answering"])

    def test_discover_refuses_a_wide_sweep_and_saves_only_when_asked(self):
        self.assertIsNone(oh_discover.sweep_refusal(ipaddress.IPv4Network("10.0.0.0/22"), False))
        self.assertIn("--network", oh_discover.sweep_refusal(ipaddress.IPv4Network("10.0.0.0/16"), False))
        self.assertIsNone(oh_discover.sweep_refusal(ipaddress.IPv4Network("10.0.0.0/16"), True))
        house = self.house(EXAMPLE)
        quiet = {"neighbours": {"192.0.2.10": "02:00:00:00:00:0a"}, "load_oui": {}, "probe_ports": {},
                 "ssdp_search": {}, "sddp_search": {}, "ddp_search": {}, "avahi_browse": [], "parse_avahi": {}}
        with contextlib.ExitStack() as stack:
            for name, value in quiet.items():
                stack.enter_context(mock.patch.object(discover.discover, name, return_value=value))
            stack.enter_context(mock.patch.object(discover.discover, "local_interface",
                                                  return_value=("192.0.2.2", ipaddress.IPv4Network("10.0.0.0/16"))))
            sweep = stack.enter_context(mock.patch.object(discover.discover, "ping_sweep"))
            stack.enter_context(mock.patch.object(discover, "AREA", self.root))
            code, _, err = run("discover", "--house", str(house))
            self.assertEqual(code, 2)
            self.assertIn("more than a /22", err)
            sweep.assert_not_called()
            code, out, _ = run("discover", "--house", str(house), "--network", "192.0.2.0/24", "--json")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["missing"], ["example_computer", "example_display", "example_matrix",
                                                          "example_player"])
            self.assertFalse((self.root / "var").exists())
            code, out, _ = run("discover", "--house", str(house), "--no-ping", "--save")
            self.assertEqual(len(list((self.root / "var/discovery").glob("*/scan.json"))), 1)


if __name__ == "__main__":
    unittest.main()
