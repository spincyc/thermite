"""The command record (oh_integrations._commands): one vocabulary, one rule
that takes a script device's commands, and the Items that carry it.

What the record does with a command runs in the Node harnesses
(integration_scripts_harness.js, lifecycle_io_harness.js and each
integration's own). Here are the invariants no harness sees: where command
rules are built, what can reach a problem Item, and what is rendered for
which device.
"""
import json
import re
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_integrations as integrations  # noqa: E402
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_render  # noqa: E402
from oh_conf import YamlFile, parse_options, unfilled  # noqa: E402
from oh_features import remote_model  # noqa: E402
from oh_features.routing import lifecycle_io  # noqa: E402
from oh_integrations import _commands, _io  # noqa: E402
from oh_metrics import exporter  # noqa: E402
from tests import test_zone_sync as zones  # noqa: E402

SCRIPTS = AREA / "lib/oh_integrations"
EXAMPLE = oh_inventory.load(AREA / "instance/devices.yaml")
#: How many command rules each script integration builds, all of them through
#: thermiteDevice.commandRule: a receiver's reboot holds its rule for seconds,
#: so HEOS keeps its keys in a second one.
COMMAND_RULES = {
    "androidtv_googletv/androidtv_googletv.js": 1, "avpro_telnet/avpro_telnet.js": 1, "bk808_cec.js": 1,
    "globalcache_ir.js": 1, "heos.js": 2, "pyatv_http/pyatv_http.js": 1, "sonos_control.js": 1,
    "sony_adcp/sony_adcp.js": 1, "sony_ip_control/sony_ip_control.js": 1, "vrroom_telnet/vrroom_telnet.js": 1}


def scripts():
    """{path below lib/oh_integrations: its code, without comment lines}."""
    return {str(path.relative_to(SCRIPTS)): "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("//"))
        for path in sorted(SCRIPTS.rglob("*.js"))}


class Seam(unittest.TestCase):
    def test_the_shared_command_rule_is_the_only_place_a_script_integration_builds_one(self):
        """A rule a script built itself would send its commands unrecorded,
        as six scripts did: every one now goes through commandRule."""
        code = scripts()
        triggers = {name: text.count("ItemCommandTrigger") for name, text in code.items() if "ItemCommandTrigger" in text}
        self.assertEqual(triggers, {"_device.js": 1})
        rule = code["_device.js"].split("function commandRule(", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("ItemCommandTrigger", rule)
        self.assertIn("record(", rule)
        calls = {name: text.count("thermiteDevice.commandRule(") for name, text in code.items()
                 if "thermiteDevice.commandRule(" in text}
        self.assertEqual(calls, COMMAND_RULES)

    def test_no_script_but_the_shared_record_writes_a_problem_item(self):
        writers = {name: [line.strip() for line in text.splitlines() if "_problem" in line]
                   for name, text in {**scripts(), "lifecycle_io.js": "\n".join(
                       line for line in lifecycle_io.SOURCE.read_text(encoding="utf-8").splitlines()
                       if not line.lstrip().startswith("//"))}.items()}
        self.assertEqual({name: lines for name, lines in writers.items() if lines},
                         {"_device.js": ["status(`${id}_problem`, now.read || now.command || 'UNDEF');"]})

    def test_every_actor_a_script_creates_is_handed_the_scripts_record(self):
        """The actor takes the rule's commands in its place: without the
        record it would conclude them unrecorded."""
        made = 0
        for name, text in scripts().items():
            actors = len(re.findall(r"\.(?:actor|httpActor)\(", text)) if "require('thermite-routing-io')" in text else 0
            self.assertEqual(text.count("record: thermiteDevice.recorder("), actors, name)
            made += actors
        self.assertEqual(made, 6)
        conf = oh_render.build(zones.house())
        generic = {path: text for path, text in conf.files.items() if "/thermite_routing_io_" in path}
        self.assertEqual(set(generic), {"automation/js/thermite_routing_io_example_matrix.js",
                                        "automation/js/thermite_routing_io_example_usb.js"})
        for path, text in generic.items():
            self.assertEqual(text.count("const thermiteDevice = "), 1, path)
            self.assertIn("httpActor(CONFIG, {readonly: !SEND, record: thermiteDevice.recorder(CONFIG.key)});", text)
            self.assertNotIn(unfilled(_commands.TABLE), text)
        self.assertIn('"tally":"bk808_http_commands"', generic["automation/js/thermite_routing_io_example_matrix.js"])
        self.assertIn('"tally":"ukm404_http_commands"', generic["automation/js/thermite_routing_io_example_usb.js"])

    def test_an_integration_has_a_problem_hook_exactly_when_its_script_takes_commands(self):
        ruled = {module.NAME for module in integrations.REGISTRY if "SOURCE" in vars(module)
                 and "thermiteDevice.commandRule(" in Path(module.SOURCE).read_text(encoding="utf-8")}
        self.assertEqual(len(ruled), len(COMMAND_RULES))
        self.assertEqual({module.NAME for module in integrations.defining("problem")}, ruled)


class Vocabulary(unittest.TestCase):
    def test_the_scripts_table_is_this_one_with_the_integrations_tally(self):
        source = _io.device("example")
        self.assertNotIn(unfilled(_commands.TABLE), source)
        line = next(line.strip() for line in source.splitlines() if line.strip().startswith("const COMMANDS = "))
        self.assertEqual(json.loads(line[len("const COMMANDS = "):-1]),
                         {"problems": list(_commands.CODES), "drops": list(_commands.DROPS),
                          "outcomes": list(_commands.OUTCOMES), "tally": "example_commands"})
        self.assertEqual(_commands.tally_item("example_tv", "sony_ip_control"), "example_tv_sony_ip_control_commands")

    def test_codes_are_plain_apart_from_the_drops_and_each_has_words_an_option_can_carry(self):
        names = [*_commands.CODES, *_commands.DROPS, *_commands.OUTCOMES]
        self.assertTrue(all(re.fullmatch(r"[a-z]+(_[a-z]+)*", name) for name in names), names)
        self.assertFalse(set(_commands.CODES) & set(_commands.DROPS))
        self.assertNotIn(_commands.CLEAR, _commands.CODES)
        self.assertIn("other", _commands.CODES)
        self.assertIn("unknown", _commands.OUTCOMES)
        for code, words in _commands.CODES.items():
            self.assertRegex(words, r"^[a-z][A-Za-z ]+$", code)

    def test_every_code_a_script_names_is_of_the_table(self):
        """Wherever a script writes a code as a literal: a result's `code`,
        the last argument of a failure(), the default of a codeOf() and what
        a drop() returns. The shared record turns any other into `other`
        (integration_scripts_harness.js), so this catches a misspelling."""
        allowed = set(_commands.CODES) | set(_commands.DROPS)
        found = set()
        sources = {**scripts(), "lifecycle_io.js": lifecycle_io.SOURCE.read_text(encoding="utf-8")}
        for name, text in sources.items():
            literals = []
            for match in re.finditer(r"\bcode:\s*([^,}\n]*)", text):
                # What a code is compared with is not a code it is given.
                literals += re.findall(r"'([^']*)'", re.sub(r"[!=]==\s*'[^']*'", "", match[1]))
            literals += re.findall(r"\bfailure\([^\n]*,\s*'([a-z_]+)'\)", text)
            literals += re.findall(r"\bcodeOf\(\w+,\s*'([a-z_]+)'\)", text)
            literals += re.findall(r"\bdrop\([^\n]*,\s*'([a-z_]+)'\)", text)
            self.assertLessEqual(set(literals), allowed, name)
            found |= set(literals)
        self.assertEqual(found, allowed, "a code no script gives is not one a script can tell apart")

    def test_the_exporter_counts_the_same_outcomes(self):
        self.assertEqual(exporter.COMMAND_OUTCOMES, _commands.OUTCOMES)


class Items(unittest.TestCase):
    def test_a_problem_item_takes_the_codes_alone_and_a_tally_is_in_no_group(self):
        made = _commands.items({"id": "example_tv"}, "sony_ip_control")
        self.assertEqual(list(made), ["example_tv_problem", "example_tv_sony_ip_control_commands"])
        problem, tally = made.values()
        self.assertEqual((problem["type"], problem["groups"], problem["tags"]), ("String", ["example_tv"], ["Status"]))
        described = problem["metadata"]["stateDescription"]["config"]
        self.assertIs(described["readOnly"], True)
        self.assertEqual(dict(parse_options(described["options"])), _commands.CODES)
        self.assertEqual((tally["type"], tally["autoupdate"]), ("String", False))
        self.assertNotIn("groups", tally)
        self.assertNotIn("tags", tally)

    def test_the_example_house_has_them_for_each_device_a_script_commands_and_no_other(self):
        conf = oh_render.build(EXAMPLE)
        items = {name: item for file in conf.yaml.values() for name, item in file.items.items()}
        problems = {name for name in items if name.endswith("_problem") and not name.startswith("loc_")}
        self.assertEqual(problems, {"example_tv_problem", "example_display_problem", "example_player_problem"})
        self.assertEqual({name for name in items if name.endswith("_commands")},
                         {"example_tv_sony_ip_control_commands", "example_display_sony_ip_control_commands",
                          "example_player_pyatv_http_commands"})
        for name in problems:
            options = items[name]["metadata"]["stateDescription"]["config"]["options"]
            self.assertEqual([code for code, _ in parse_options(options)], list(_commands.CODES), name)
        # Each script has its table, and none the unfilled line.
        for path, text in conf.files.items():
            if isinstance(text, str) and path.endswith(".js") and "const thermiteDevice = " in text:
                self.assertNotIn(unfilled(_commands.TABLE), text, path)
                self.assertEqual(text.count('"tally":"'), 1, path)

    def test_two_scripts_of_one_device_share_its_problem_item_and_count_apart(self):
        device = {"id": "example_tv", "controls": {"control": ["sony_ip_control", "androidtv_googletv"]}}
        self.assertEqual(_commands.problem(device), "example_tv_problem")
        files = {name: YamlFile(()) for name in ("sony_ip_control", "androidtv_googletv")}
        for name, out in files.items():
            _commands.render(device, out, name)
        self.assertEqual(sorted(name for out in files.values() for name in out.items),
                         ["example_tv_androidtv_googletv_commands", "example_tv_problem",
                          "example_tv_sony_ip_control_commands"])
        self.assertIsNone(_commands.problem({"id": "example_matrix", "controls": {"control": ["bk808_http"]}}))

    def test_a_device_only_its_routing_actor_commands_gets_them_from_the_routing_renderer(self):
        conf = oh_render.build(zones.house())
        made = conf.yaml[lifecycle_io.ITEMS].items
        self.assertEqual({name for name in made if name.endswith(("_problem", "_commands"))},
                         {"example_avr_problem", "example_avr_denonmarantz_http_commands",
                          "example_matrix_problem", "example_matrix_bk808_http_commands",
                          "example_usb_problem", "example_usb_ukm404_http_commands"})
        self.assertEqual(made["example_avr_problem"], _commands.items({"id": "example_avr"}, "x")["example_avr_problem"])
        # A device whose own script has an actor keeps the ones its integration renders.
        self.assertIn("example_tv_problem", conf.yaml["yaml/sony_ip_control.yaml"].items)
        # Without the routing lifecycle its commands go to its binding, which reports nothing.
        plain = {name for file in oh_render.build(EXAMPLE).yaml.values() for name in file.items}
        self.assertNotIn("example_matrix_problem", plain)

    def test_the_remote_model_names_the_problem_item_of_whichever_script_commands_the_device(self):
        devices = {device["id"]: device for device in EXAMPLE["devices"]}
        self.assertEqual(remote_model.capabilities(devices["example_player"])[2], "example_player_problem")
        self.assertEqual(remote_model.capabilities(devices["example_tv"])[2], "example_tv_problem")
        self.assertIsNone(remote_model.capabilities(devices["example_matrix"])[2])


class Tally(unittest.TestCase):
    MAPPING = {"devices": {"example_tv": {"room": "example_west", "kind": "tv"}},
               "commands": {"example_tv_sony_ip_control_commands": {"device": "example_tv",
                                                                     "integration": "sony_ip_control"}}}
    LABELS = {"device": "example_tv", "room": "example_west", "kind": "tv", "integration": "sony_ip_control"}

    def samples(self, state):
        return exporter._commands(self.MAPPING, [{"name": "example_tv_sony_ip_control_commands", "state": state}])

    def test_every_outcome_is_a_counter_from_zero_with_the_seconds_its_commands_took(self):
        zeros = {outcome: {"count": 0, "ms": 0} for outcome in _commands.OUTCOMES}
        samples = self.samples(json.dumps({"version": 1, "outcomes": zeros}))
        self.assertEqual(samples, [sample for outcome in _commands.OUTCOMES for sample in (
            ("thermite_device_commands_total", {**self.LABELS, "outcome": outcome}, 0.0),
            ("thermite_device_command_seconds_total", {**self.LABELS, "outcome": outcome}, 0.0))])
        counted = {**zeros, "applied": {"count": 12, "ms": 3400}, "refused": {"count": 2, "ms": 90}}
        found = {(name, labels["outcome"]): value for name, labels, value
                 in self.samples(json.dumps({"version": 1, "outcomes": counted}))}
        self.assertEqual(found[("thermite_device_commands_total", "applied")], 12)
        self.assertEqual(found[("thermite_device_command_seconds_total", "applied")], 3.4)
        self.assertEqual(found[("thermite_device_commands_total", "refused")], 2)
        self.assertEqual(found[("thermite_device_commands_total", "unknown")], 0)
        for family in ("thermite_device_commands_total", "thermite_device_command_seconds_total"):
            self.assertEqual(exporter.FAMILIES[family][0], "counter")

    def test_a_tally_that_cannot_be_read_has_no_series_and_only_the_tables_outcomes_are_labels(self):
        for state in ("NULL", "UNDEF", "", "[]", "not JSON", json.dumps({"version": 2, "outcomes": {}}),
                      json.dumps({"version": 1, "outcomes": []}), "x" * (exporter.ROUTING_BYTES + 1)):
            with self.subTest(state=state[:40]):
                self.assertEqual(self.samples(state), [])
        odd = {"applied": {"count": -1, "ms": "soon"}, "<b>": {"count": 5, "ms": 5}, "sent": 3}
        samples = self.samples(json.dumps({"version": 1, "outcomes": odd}))
        # A count that is no count is left out; one not counted yet is zero.
        self.assertEqual({labels["outcome"] for _, labels, _ in samples}, set(_commands.OUTCOMES) - {"applied"})
        self.assertIn(("thermite_device_commands_total", {**self.LABELS, "outcome": "sent"}, 0.0), samples)
        self.assertEqual(exporter._commands({"devices": {}}, []), [])

    def test_what_a_scripts_reads_meet_is_a_gauge_of_its_own_and_never_a_label(self):
        zeros = {outcome: {"count": 0, "ms": 0} for outcome in _commands.OUTCOMES}

        def read_ok(reads):
            tally = {"version": 1, "outcomes": zeros, **({} if reads is None else {"reads": reads})}
            return [(labels, value) for name, labels, value in self.samples(json.dumps(tally))
                    if name == "thermite_device_read_ok"]

        self.assertEqual(read_ok("ok"), [(self.LABELS, 1.0)])
        # A read the device declined was answered: only a read that got
        # nothing it can use says the device cannot be read.
        self.assertEqual(exporter.READ_ANSWERS, ("ok", *_commands.ANSWERED))
        self.assertLess(set(_commands.ANSWERED), set(_commands.CODES))
        for code in (*_commands.CODES, "something new"):
            self.assertEqual(read_ok(code), [(self.LABELS, 1.0 if code in _commands.ANSWERED else 0.0)], code)
        self.assertEqual({code for code in _commands.CODES if read_ok(code)[0][1] == 0},
                         {"unreachable", "no_answer", "auth", "protocol", "other"})
        # A script that reads nothing of the device says nothing of it.
        for nothing in (None, "", 1, True, ["ok"], {"ok": 1}):
            self.assertEqual(read_ok(nothing), [], nothing)
        self.assertEqual(exporter.FAMILIES["thermite_device_read_ok"][0], "gauge")
        self.assertIn(f"reads: now.read || '{exporter.READ_ANSWERS[0]}'", scripts()["_device.js"])

    def test_an_integration_says_its_rule_reads_its_devices_exactly_when_the_rule_concludes_reads(self):
        """READS is what gives a device its `reads` check: a rule that polled
        and did not say so would leave its devices with no reading of whether
        they answer, and one that said so without polling a check that is
        unknown for good."""
        concludes = re.compile(r"thermiteDevice\.(?:health\(|problem\.read\()")
        polling = {module.NAME for module in integrations.REGISTRY if "SOURCE" in vars(module)
                   and concludes.search(Path(module.SOURCE).read_text(encoding="utf-8"))}
        self.assertEqual(polling, {"avpro_telnet", "pyatv_http", "sony_adcp", "sony_ip_control", "vrroom_telnet"})
        self.assertEqual({module.NAME for module in integrations.REGISTRY if module.READS}, polling)
        for device, check in ((check["device"], check) for check in oh_metrics.exporter_map(EXAMPLE)["health_checks"]):
            if check["check"] == "reads":
                self.assertEqual(check["metric"], "thermite_device_read_ok")
                self.assertIn(check["labels"]["integration"], polling)
                self.assertIn(_commands.tally_item(device, check["labels"]["integration"]),
                              oh_metrics.exporter_map(EXAMPLE)["commands"])

    def test_the_map_names_each_tally_with_its_device_and_integration(self):
        mapping = oh_metrics.exporter_map(EXAMPLE)
        self.assertEqual(mapping["commands"], {
            "example_display_sony_ip_control_commands": {"device": "example_display", "integration": "sony_ip_control"},
            "example_player_pyatv_http_commands": {"device": "example_player", "integration": "pyatv_http"},
            "example_tv_sony_ip_control_commands": {"device": "example_tv", "integration": "sony_ip_control"}})
        # A tally is read as a tally alone, and every problem Item's clear state is the record's.
        self.assertFalse(set(mapping["commands"]) & set(mapping["items"]))
        problems = {name: spec for name, spec in mapping["items"].items() if spec["point"] == "problem"}
        self.assertEqual(set(problems), {"example_tv_problem", "example_display_problem", "example_player_problem"})
        self.assertEqual({spec.get("clear_state") for spec in problems.values()}, {_commands.CLEAR})
        actors = oh_metrics.exporter_map(zones.house())["commands"]
        self.assertEqual(actors["example_avr_denonmarantz_http_commands"],
                         {"device": "example_avr", "integration": "denonmarantz_http"})
        self.assertEqual(actors["example_matrix_bk808_http_commands"],
                         {"device": "example_matrix", "integration": "bk808_http"})

    def test_a_round_reads_the_tallies_with_the_items(self):
        counted = {outcome: {"count": 1, "ms": 250} for outcome in _commands.OUTCOMES}
        answers = {"things": [], "items": [{"name": "example_tv_sony_ip_control_commands",
                                            "state": json.dumps({"version": 1, "outcomes": counted})}]}
        samples = exporter.collect({**self.MAPPING, "version": oh_metrics.MAP_VERSION},
                                   lambda path, timeout: answers["things" if path == exporter.THINGS_PATH else "items"])
        self.assertIn(("thermite_device_command_seconds_total", {**self.LABELS, "outcome": "busy"}, 0.25), samples)


if __name__ == "__main__":
    unittest.main()
