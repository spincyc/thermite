"""Persistent firewall commissioning refuses foreign policy and resumes cutover."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_access.native_boundary import filter_rules, firewall_ports
from oh_install import ingress, runner
from oh_install.model import Command
from oh_install.system import UnitFile, UnitOrigin

OLD = {"http_port": 13602, "https_port": 13603}
NEW = {"http_port": 14601, "https_port": 14602}
#: A file the installer wrote under an earlier default allocation, when the
#: native listeners were 13601-13604 and 13623.
EARLIER = frozenset({13601, 13602, 13603, 13604, 13623})
EARLIER_FILE = ingress.MARKER + "# Ports: 13601,13602\n" + filter_rules(EARLIER)


def live_policy(value):
    """The live table filtering a host's native ports, or a set of ports."""
    ports = firewall_ports(value) if isinstance(value, dict) else value
    return {"nftables": [{"metainfo": {"version": "fixture"}},
        {"table": {"family": "inet", "name": "thermite_native", "handle": 10}},
        {"chain": {"family": "inet", "table": "thermite_native", "name": "input", "type": "filter", "hook": "input", "prio": -10, "policy": "accept"}},
        {"rule": {"family": "inet", "table": "thermite_native", "chain": "input", "expr": [
            {"match": {"op": "!=", "left": {"meta": {"key": "iifname"}}, "right": "lo"}},
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                       "right": {"set": sorted(ports)}}},
            {"reject": {"type": "tcp reset"}}]}}]}


class System:
    def __init__(self, *, installed=None, live=None):
        self.files = {}
        if installed is not None:
            self.files = {ingress.UNIT_FILE: UnitFile(ingress.unit_text().encode(), 0, 0o644),
                          ingress.RULE_FILE: UnitFile(ingress.rule_text(installed).encode(), 0, 0o644)}
        self.live = live_policy(live) if live else None
        self.messages, self.commands, self.captures = [], [], []
        self.list_output = None
        self.list_status = 0
        self.fail = None
        self.failed = False
        self.corrupt_after_restart = False
        self.safe = True
        self.override = None
        self.module_tree = True

    def kernel(self):
        return "fictional-kernel"

    def stat(self, path):
        return object() if self.module_tree and path == Path("/usr/lib/modules/fictional-kernel") else None

    def safe_unit_parent(self, path):
        return self.safe

    def lexists(self, path):
        return path in self.files

    def unit_file(self, path):
        return self.files.get(path)

    def unit_origin(self, name):
        assert name == ingress.UNIT
        return self.override or UnitOrigin(fragment=str(ingress.UNIT_FILE) if ingress.UNIT_FILE in self.files else "",
                                           load_state="loaded" if ingress.UNIT_FILE in self.files else "not-found")

    def say(self, text):
        self.messages.append(text)

    def capture(self, argv, *, diagnostics=False):
        assert diagnostics
        self.captures.append(tuple(argv))
        assert tuple(argv[:4]) == ("sudo", "-n", "nft", "--json")
        if tuple(argv[4:]) == ("list", "tables"):
            entries = [{"table": {"family": "inet", "name": "other_firewall"}}]
            if self.live is not None:
                entries.append({"table": {"family": "inet", "name": "thermite_native"}})
            return self.list_status, self.list_output if self.list_output is not None else json.dumps({"nftables": entries})
        assert tuple(argv[4:]) == ("list", "table", "inet", "thermite_native")
        return (0, json.dumps(self.live)) if self.live is not None else (1, "")

    def perform(self, action, system, area):
        assert system is self
        assert action.root and action.terminal
        argv = action.argv
        self.commands.append(argv)
        if not self.failed and self.fail and argv[:len(self.fail)] == self.fail:
            self.failed = True
            return 7
        if argv[0] == "install":
            source, target = Path(argv[-2]), Path(argv[-1])
            assert target in (ingress.RULE_FILE, ingress.UNIT_FILE)
            self.files[target] = UnitFile(source.read_bytes(), 0, 0o644)
        elif argv[:2] == ("systemctl", "restart"):
            self.live = live_policy(ingress.rule_ports(self.files[ingress.RULE_FILE].content))
            if self.corrupt_after_restart:
                self.live["nftables"].append(copy.deepcopy(self.live["nftables"][-1]))
        else:
            assert argv == ("-v",) or argv[:2] in {("nft", "--check"), ("systemctl", "daemon-reload"), ("systemctl", "enable")}
        return 0


class NativeIngress(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/unified-boundary"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = SimpleNamespace(area=self.root, var=self.root / "var", host=NEW)
        self.step = ingress.NativeIngress(self.context)

    def perform(self, system, *, probe=False):
        with patch("oh_install.runner.perform", side_effect=system.perform):
            return self.step.perform(system, probe=probe)

    def assert_no_install(self, system):
        self.assertEqual(system.commands, [("-v",)])

    def test_first_apply_checks_then_installs_and_verifies_only_owned_table(self):
        system = System()
        self.assertIsNone(self.step.check(system).problem)
        self.assertEqual(self.perform(system), 0)
        self.assertEqual(system.commands[1][0:2], ("nft", "--check"))
        self.assertEqual(system.commands[-1], ("systemctl", "restart", ingress.UNIT))
        self.assertEqual(ingress.rule_ports(system.files[ingress.RULE_FILE].content), firewall_ports(NEW))
        self.assertEqual(system.live, live_policy(NEW))
        self.assertEqual(system.captures[-1][-4:], ("list", "table", "inet", "thermite_native"))

    def test_unrecognized_existing_table_refuses_before_install(self):
        system = System(live=OLD)
        self.assertEqual(self.perform(system), 1)
        self.assert_no_install(system)
        self.assertEqual(system.live, live_policy(OLD))
        self.assertIn("no managed boundary files", " ".join(system.messages))

    def test_extra_live_rule_refuses_without_overwriting_policy(self):
        system = System(installed=OLD, live=OLD)
        system.live["nftables"].append(copy.deepcopy(system.live["nftables"][-1]))
        original = copy.deepcopy(system.live)
        self.assertEqual(self.perform(system), 1)
        self.assert_no_install(system)
        self.assertEqual(system.live, original)

    def test_a_live_table_other_than_the_installed_file_is_refused(self):
        # A transaction's rollback restores the file and reloads the table,
        # so a difference is a rule someone else loaded.
        system = System(installed=NEW, live=OLD)
        self.assertEqual(self.perform(system), 1)
        self.assert_no_install(system)
        self.assertEqual(system.live, live_policy(OLD))

    def test_a_failed_restart_fails_the_step(self):
        system = System(installed=OLD, live=OLD)
        system.fail = ("systemctl", "restart")
        self.assertEqual(self.perform(system), 7)
        self.assertEqual(system.live, live_policy(OLD))

    def test_a_file_from_an_earlier_allocation_is_recognized_and_replaced(self):
        system = System(live=EARLIER)
        system.files = {ingress.UNIT_FILE: UnitFile(ingress.unit_text().encode(), 0, 0o644),
                        ingress.RULE_FILE: UnitFile(EARLIER_FILE.encode(), 0, 0o644)}
        self.assertEqual(ingress.rule_ports(EARLIER_FILE.encode()), EARLIER)
        self.assertIsNone(self.step.check(system).problem)
        self.assertEqual(self.perform(system), 0)
        self.assertEqual(ingress.rule_ports(system.files[ingress.RULE_FILE].content), firewall_ports(NEW))
        self.assertEqual(system.live, live_policy(NEW))
        for edited in (EARLIER_FILE.replace("{ 13601", "{ 22, 13601"), EARLIER_FILE.replace("13623 }", "13623, 22 }"),
                       EARLIER_FILE.replace("# Ports: 13601,13602", "# Ports: 13601,13605")):
            with self.subTest(edited=edited.splitlines()[1]), self.assertRaises(ValueError):
                ingress.rule_ports(edited.encode())

    def test_post_apply_verification_failure_fails_the_step(self):
        system = System(installed=OLD, live=OLD)
        system.corrupt_after_restart = True
        self.assertEqual(self.perform(system), 1)

    def test_malformed_table_listing_fails_before_any_firewall_mutation(self):
        for output in ("", "{", "null", "{}", '{"nftables":null}', '{"nftables":[null]}',
                       '{"nftables":[{"table":null}]}', '{"nftables":[{"unknown":{}}]}',
                       '{"nftables":[],"nftables":[]}'):
            with self.subTest(output=output):
                system = System()
                system.list_output = output
                self.assertEqual(self.perform(system), 1)
                self.assert_no_install(system)

    def test_unavailable_table_listing_fails_before_install(self):
        system = System()
        system.list_status = 1
        self.assertEqual(self.perform(system), 1)
        self.assert_no_install(system)

    def test_prerequisites_preserve_live_policy_and_root_files(self):
        system = System(installed=OLD, live=OLD)
        files, live = copy.deepcopy(system.files), copy.deepcopy(system.live)
        self.assertEqual(self.perform(system, probe=True), 0)
        self.assertEqual(system.commands[0], ("-v",))
        self.assertEqual(system.commands[1][:2], ("nft", "--check"))
        self.assertEqual(len(system.commands), 2)
        self.assertEqual(system.files, files)
        self.assertEqual(system.live, live)

    def test_missing_modules_hint_follows_failed_probe_only(self):
        system = System()
        system.module_tree = False
        self.assertEqual(self.perform(system, probe=True), 0)
        self.assertNotIn("reboot", " ".join(system.messages))
        system.commands.clear()
        system.list_status = 3
        self.assertEqual(self.perform(system, probe=True), 1)
        self.assert_no_install(system)
        self.assertIn("exit 3", " ".join(system.messages))
        self.assertIn("reboot", " ".join(system.messages))
        self.assertIn("A reboot alone does not complete", " ".join(system.messages))

    def test_rule_validation_failure_preserves_active_policy_and_root_files(self):
        system = System(installed=OLD, live=OLD)
        files, live = copy.deepcopy(system.files), copy.deepcopy(system.live)
        system.fail = ("nft", "--check")
        self.assertEqual(self.perform(system, probe=True), 7)
        self.assertEqual(system.files, files)
        self.assertEqual(system.live, live)
        self.assertEqual(len(system.commands), 2)

    def test_interrupted_install_with_missing_boundary_can_resume_after_probe(self):
        system = System()
        self.assertEqual(self.perform(system, probe=True), 0)
        self.assertEqual(system.files, {})
        self.assertIsNone(system.live)
        self.assertEqual(self.perform(system), 0)
        self.assertIn(ingress.UNIT_FILE, system.files)
        self.assertIn(ingress.RULE_FILE, system.files)
        self.assertEqual(system.live, live_policy(NEW))

    def test_dry_run_does_not_probe_or_create_files(self):
        system = System()
        step = ingress.NativeIngressPrerequisites(self.context)
        self.assertEqual(runner.run([step], system, self.root, dry_run=True), 0)
        self.assertEqual(system.commands, [])
        self.assertEqual(system.captures, [])
        self.assertFalse(self.context.var.exists())

    def test_failed_prerequisite_stops_workflow_before_following_service_step(self):
        for failure in ("inspection", "rule_check"):
            with self.subTest(failure=failure):
                system = System()
                if failure == "inspection":
                    system.list_status = 1
                else:
                    system.fail = ("nft", "--check")
                following = Mock()
                perform = runner.perform

                def fake_commands(action, host, area, *args, **kwargs):
                    if isinstance(action, Command):
                        return system.perform(action, host, area)
                    return perform(action, host, area, *args, **kwargs)

                with patch.object(runner, "perform", side_effect=fake_commands):
                    result = runner.run([ingress.NativeIngressPrerequisites(self.context), following],
                                        system, self.root, root_confirmed=True)
                self.assertNotEqual(result, 0)
                following.check.assert_not_called()
                self.assertEqual(system.files, {})
                self.assertIsNone(system.live)

    def test_unit_logs_as_itself_and_replaces_the_exact_unit_before_it(self):
        self.assertIn("\nExecStart=/usr/bin/env nft -f /etc/thermite-native.nft\nSyslogIdentifier=thermite-native-boundary\n",
                      ingress.unit_text())
        system = System(installed=OLD, live=OLD)
        system.files[ingress.UNIT_FILE] = UnitFile(ingress.PREVIOUS_UNIT_TEXT.encode(), 0, 0o644)
        self.assertIsNone(self.step.check(system).problem, "an upgrade adopts the unit an earlier install wrote")
        self.assertEqual(self.perform(system), 0)
        self.assertEqual(system.files[ingress.UNIT_FILE].content, ingress.unit_text().encode())
        from oh_install import steps
        for content, owned in ((ingress.PREVIOUS_UNIT_TEXT.encode(), True), (ingress.unit_text().encode(), True),
                               (ingress.PREVIOUS_UNIT_TEXT.encode() + b"# edited\n", False)):
            system.files[ingress.UNIT_FILE] = UnitFile(content, 0, 0o644)
            with self.subTest(owned=owned):
                self.assertIs(steps.boundary_owned(system), owned)

    def test_preflight_rejects_changed_root_files_or_service_overrides(self):
        for case in ("permissions", "rule_body", "unit_body", "parent", "dropin", "unknown"):
            system = System(installed=OLD, live=OLD)
            if case == "permissions": system.files[ingress.RULE_FILE] = UnitFile(ingress.rule_text(OLD).encode(), 0, 0o664)
            elif case == "rule_body": system.files[ingress.RULE_FILE] = UnitFile(ingress.rule_text(OLD).encode() + b"# edited\n", 0, 0o644)
            elif case == "unit_body": system.files[ingress.UNIT_FILE] = UnitFile(ingress.unit_text().encode() + b"# edited\n", 0, 0o644)
            elif case == "parent": system.safe = False
            elif case == "dropin": system.override = UnitOrigin(fragment=str(ingress.UNIT_FILE), drop_ins="foreign.conf")
            elif case == "unknown": system.override = UnitOrigin(known=False)
            with self.subTest(case=case):
                self.assertIsNotNone(self.step.check(system).problem)
                self.assertEqual(system.commands, [])


if __name__ == "__main__":
    unittest.main()
