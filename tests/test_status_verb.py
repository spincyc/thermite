"""thermite status: what is deployed here, the runtime's own checks that
thermite health made, and --runtime and --installation, which run those
checks as health ran them. Every answer comes from a stand-in: no test here
reaches a runtime, Prometheus, systemd or a device."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli  # noqa: E402
import oh_deploy  # noqa: E402
import oh_host  # noqa: E402
import oh_paths  # noqa: E402
import oh_sandbox  # noqa: E402
from oh_cli import cameras, health, history, status  # noqa: E402

HOST = oh_host.load(AREA / "instance/host.yaml")
TOKEN = "oh." + "fixture.status-token-sentinel"
URL = oh_deploy.production_url(HOST)
THINGS = "/rest/things?summary=false"
ITEMS = "/rest/items?recursive=false&fields=name,type,state,groupType"
ADDONS = "/rest/addons?serviceId=all"
RULES = "/rest/rules?summary=true"
REST = ["/rest/", THINGS, ITEMS, RULES, ADDONS]
GATEWAY = {"thermite-access.service": ("loaded", "active"), "thermite-access-worker.service": ("loaded", "active"),
           "thermite-access-callbacks.service": ("not-found", "inactive")}


def thing(uid, state="ONLINE", detail="NONE"):
    return {"UID": uid, "thingTypeUID": "http:url", "label": uid, "configuration": {},
            "statusInfo": {"status": state, "statusDetail": detail, "description": None}}


def rule(uid, detail="NONE"):
    return {"uid": uid, "templateUID": None, "name": f"{uid} rule", "visibility": "VISIBLE", "description": None,
            "tags": [], "configuration": {}, "editable": False,
            "status": {"status": "UNINITIALIZED" if detail == "DISABLED" else "IDLE", "statusDetail": detail}}


class Fixture(unittest.TestCase):
    """A host whose runtime, systemd and deploy record are stand-ins."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.token = self.root / "var/fixture/api-token"
        self.token.parent.mkdir(parents=True)
        self.token.write_text(TOKEN + "\n")
        self.addons = self.root / "conf/services/addons.cfg"
        self.addons.parent.mkdir(parents=True)
        self.addons.write_text("binding = http\n")
        self.responses = {
            "/rest/": (200, {"runtimeInfo": {"version": "5.2.3"}}),
            THINGS: (200, [thing("http:url:fixture")]),
            ITEMS: (200, [{"name": "fixture_power", "type": "Switch", "state": "ON"}]),
            RULES: (200, [rule("fixture_poll")]),
            ADDONS: (200, [{"uid": "binding-http", "installed": True}]),
        }
        self.version = "5.2.3"
        self.units = dict(GATEWAY)
        self.metrics = {"installed": True, "ok": True, "summary": "Prometheus ready, targets 3/3; Grafana answers",
                        "targets_up": 3, "targets": 3, "down": [], "systemd_collector": True}
        self.http = mock.Mock()
        self.http.try_get_json.side_effect = lambda path: copy.deepcopy(self.responses[path])
        scope = contextlib.ExitStack()
        self.addCleanup(scope.close)
        patches = [
            (status, "AREA", self.root), (health, "AREA", self.root),
            (health, "SANDBOX_TOKEN", self.root / "var/sandbox/api-token"),
            (oh_paths, "require_compatible", mock.Mock()),
            (oh_deploy, "load", mock.Mock(return_value=({}, HOST))),
            (oh_deploy, "hostname", mock.Mock(return_value="fixture")),
            (oh_deploy, "lan_address", mock.Mock(return_value="192.0.2.10")),
            (oh_deploy, "installed_units", mock.Mock(return_value=[])),
            (oh_deploy, "service_states", mock.Mock(return_value={})),
            (oh_deploy, "openhab_version", mock.Mock(side_effect=lambda url: self.version)),
            (oh_deploy, "metrics_installed", mock.Mock(return_value=False)),
            (oh_deploy, "music_installed", mock.Mock(return_value=False)),
            (status, "show_transaction", mock.Mock()),
            (health, "unit_states", mock.Mock(side_effect=lambda names: dict(self.units))),
            (health, "metrics_report", mock.Mock(side_effect=lambda host: copy.deepcopy(self.metrics))),
            (oh_sandbox, "Http", mock.Mock(return_value=self.http)),
        ]
        self.client = patches[-1][2]
        for target, name, value in patches:
            scope.enter_context(mock.patch.object(target, name, value))
        scope.enter_context(mock.patch("oh_access.client.deployment", return_value=None))
        scope.enter_context(mock.patch.dict(os.environ))
        os.environ.pop(oh_cli.URL_VARIABLE, None)

    def run_cli(self, *argv):
        """(exit status, stdout, stderr) of `thermite ARGV`."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = oh_cli.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def report(self, *argv):
        code, out, err = self.run_cli("status", "--state-dir", str(self.state), "--json", *argv)
        self.assertEqual(out.count("\n"), 1, "one line of JSON")
        return code, json.loads(out), err


class PlainStatus(Fixture):
    def test_the_runtimes_checks_follow_with_this_hosts_token_and_nothing_twice(self):
        code, report, _ = self.report()
        self.assertEqual(code, 1, "nothing is deployed in the stand-in state")
        self.assertEqual(report["problems"], ["nothing is deployed"])
        checks = report["runtime_checks"]
        self.assertIsNone(report["runtime_checks_reason"])
        self.assertEqual((checks["url"], checks["openhab_version"], checks["things"]["count"]), (URL, "5.2.3", 1))
        self.assertEqual(checks["failures"], [])
        for key in ("metrics", "music", "gateway"):
            self.assertNotIn(key, checks, f"status reads the {key} itself")
        self.client.assert_called_once_with(URL, token=TOKEN, timeout=15)
        self.assertEqual([call.args[0] for call in self.http.try_get_json.call_args_list], REST)
        self.assertEqual(report["gateway"]["services"]["summary"], "2 services active")
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_its_human_section_names_each_check(self):
        self.responses[THINGS] = 200, [thing("http:url:fixture"), thing("http:url:gone", "OFFLINE", "COMMUNICATION_ERROR")]
        self.responses[ITEMS] = 200, [{"name": "fixture_level", "type": "Dimmer", "state": "NULL"}]
        code, out, _ = self.run_cli("status", "--state-dir", str(self.state))
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertIn(f"checks   the runtime's own, with the API token in {self.token}", lines)
        self.assertIn("things   2: OFFLINE/COMMUNICATION_ERROR 1, ONLINE 1", lines)
        self.assertIn("           warn    http:url:gone  OFFLINE/COMMUNICATION_ERROR", lines)
        self.assertIn("items    1, NULL/UNDEF: 1", lines)
        self.assertIn("           NULL  fixture_level (Dimmer)", lines)
        self.assertIn("rules    1, thermite's disabled: 0", lines)
        self.assertIn("add-ons  1 installed: binding-http", lines)
        self.assertIn("         2 services active", lines)
        self.assertLess(lines.index(f"runtime  {URL}: runtime 5.2.3 answers; from the LAN: "
                                    f"{oh_deploy.production_url(HOST, '192.0.2.10')}"),
                        lines.index("things   2: OFFLINE/COMMUNICATION_ERROR 1, ONLINE 1"))

    def test_a_failed_check_is_a_problem(self):
        self.responses[THINGS] = 200, [thing("http:url:bad", "OFFLINE", "CONFIGURATION_ERROR")]
        self.responses[RULES] = 200, [rule("fixture_poll", "DISABLED")]
        code, report, _ = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["problems"][1:], [
            "runtime: 1 Thing(s) have configuration errors",
            "runtime: 1 of thermite's rules disabled in the runtime: fixture_poll; "
            "the next thermite install or upgrade switches them back on"])
        self.assertEqual(report["warnings"], [])

    def test_offline_things_are_warnings_as_installation_readiness_has_them(self):
        """A device unplugged or off the network does not fail plain status;
        --runtime, as health did, still fails it."""
        offline = thing("http:url:gone", "OFFLINE", "COMMUNICATION_ERROR")
        offline["statusInfo"]["description"] = "No route to host"
        self.responses[THINGS] = 200, [thing("http:url:fixture"), offline,
                                       thing("http:url:bridged", "OFFLINE", "BRIDGE_OFFLINE"),
                                       thing("http:url:new", "UNKNOWN")]
        code, report, _ = self.report()
        self.assertEqual(report["problems"], ["nothing is deployed"], "only what fails without the runtime's Things")
        self.assertEqual(report["warnings"], [
            "runtime: Thing http:url:bridged is OFFLINE/BRIDGE_OFFLINE",
            "runtime: Thing http:url:gone is OFFLINE/COMMUNICATION_ERROR (No route to host)",
            "runtime: Thing http:url:new is UNKNOWN/NONE; it has not initialized"])
        self.assertEqual(report["runtime_checks"]["failures"], [])
        verdicts = {row["uid"]: row["verdict"] for row in report["runtime_checks"]["things"]["things"]}
        self.assertEqual(verdicts, {"http:url:bridged": "warn", "http:url:fixture": "ok", "http:url:gone": "warn",
                                    "http:url:new": "pending"})
        code, out, _ = self.run_cli("status", "--state-dir", str(self.state))
        self.assertIn("           waiting http:url:new  UNKNOWN/NONE", out.splitlines())
        code, report, _ = self.report("--allow-offline", "http:url:gone", "http:url:new")
        self.assertEqual(report["warnings"], ["runtime: Thing http:url:bridged is OFFLINE/BRIDGE_OFFLINE"],
                         "a Thing --allow-offline names is not even a warning")
        code, out, _ = self.run_cli("status", "--runtime", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["failures"], ["3 Thing(s) not ONLINE"])
        self.assertNotIn("warnings", json.loads(out), "--runtime's report is health's, as it was")

    def test_the_checks_switches_shape_them(self):
        self.responses[THINGS] = 200, [thing("http:url:gone", "OFFLINE", "COMMUNICATION_ERROR")]
        self.responses[ADDONS] = 200, []
        other = self.root / "other-token"
        other.write_text(TOKEN + "\n")
        code, report, _ = self.report("--allow-offline", "http:url:gone", "--addons", str(self.addons),
                                      "--token-file", str(other), "--timeout", "3")
        self.assertEqual(report["problems"][1:], ["runtime: add-ons not installed: binding-http"])
        self.client.assert_called_once_with(URL, token=TOKEN, timeout=3.0)
        self.assertEqual(report["runtime_checks"]["things"]["things"][0]["verdict"], "allowed")

    def test_a_runtime_that_does_not_answer_is_one_problem_and_no_checks(self):
        self.version = None
        code, report, _ = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["problems"], ["nothing is deployed", f"runtime {URL}: not answering"])
        self.assertIsNone(report["runtime_checks"])
        self.assertEqual(report["runtime_checks_reason"], f"runtime {URL} is not answering")
        self.client.assert_not_called()

    def test_checks_that_cannot_run_are_a_problem_not_a_failure_to_run(self):
        def refused():
            self.responses["/rest/"] = 401, None

        def malformed():
            self.responses[ITEMS] = 200, {"not": "a list"}

        def unreadable():
            self.responses[THINGS] = 503, None

        for name, change, reason in (
                ("no token", lambda: self.token.unlink(), f"no {self.token}: thermite install saves production's API "
                                                          "token there (docs/installation.md)"),
                ("refused", refused, f"{URL} refused the API token in {self.token} (HTTP 401)"),
                ("malformed", malformed, "/rest/items returned malformed Item data"),
                ("unreadable", unreadable, "/rest/things answered HTTP 503")):
            with self.subTest(name):
                saved = copy.deepcopy(self.responses)
                change()
                code, report, _ = self.report()
                self.assertEqual(code, 1)
                self.assertIsNone(report["runtime_checks"])
                self.assertEqual(report["runtime_checks_reason"], reason)
                self.assertEqual(report["problems"][-1], f"runtime checks could not run: {reason}")
                self.responses = saved
                self.token.write_text(TOKEN + "\n")
        self.token.unlink()
        code, out, _ = self.run_cli("status", "--state-dir", str(self.state))
        self.assertIn(f"checks   could not run: no {self.token}", out)

    def test_a_gateway_service_that_is_not_active_is_a_problem(self):
        self.units["thermite-access-worker.service"] = ("loaded", "failed")
        code, report, _ = self.report()
        self.assertIn("gateway: thermite-access-worker.service is failed", report["problems"])
        self.units = {name: ("not-found", "inactive") for name in GATEWAY}
        code, report, _ = self.report()
        self.assertEqual(report["problems"], ["nothing is deployed"], "no gateway installed, nothing of it fails")

    def test_without_runtime_checks_reads_what_is_deployed_alone(self):
        code, report, _ = self.report("--no-runtime-checks")
        self.assertEqual((report["runtime_checks"], report["runtime_checks_reason"]), (None, "not asked for"))
        self.assertEqual(report["warnings"], [])
        self.client.assert_not_called()

    def test_state_dir_and_its_old_spelling_read_the_same_state(self):
        code, new, err = self.run_cli("status", "--state-dir", str(self.state))
        self.assertEqual(err, "")
        self.assertIn(f"deployed nothing yet ({self.state / 'deploy/deployed.json'} does not exist)", new)
        code, old, err = self.run_cli("status", "--state", str(self.state))
        self.assertEqual(old, new)
        self.assertIn("thermite status: --state is now --state-dir; --state goes in the next release", err)


class Runtime(Fixture):
    """status --runtime and --installation against health as it ran."""

    def both(self, health_argv, status_argv):
        """(health's, status's) (exit status, stdout), each from a fresh stand-in."""
        results = []
        for argv in (["health", *health_argv], ["status", *status_argv]):
            self.http.reset_mock()
            self.client.reset_mock()
            code, out, _ = self.run_cli(*argv)
            results.append((code, out, [call.args for call in self.client.call_args_list],
                            [call.kwargs for call in self.client.call_args_list]))
        return results

    def test_each_target_is_checked_as_health_checked_it(self):
        sandbox_token = self.root / "var/sandbox/api-token"
        sandbox_token.parent.mkdir()
        sandbox_token.write_text(TOKEN + "\n")
        for health_argv, status_argv, url, token in (
                (["--production"], ["--runtime"], URL, TOKEN),
                (["--production"], ["--runtime", "--production"], URL, TOKEN),
                (["--sandbox"], ["--sandbox"], oh_cli.SANDBOX_URL, TOKEN),
                (["--sandbox"], ["--runtime", "--sandbox"], oh_cli.SANDBOX_URL, TOKEN),
                (["--url", "http://192.0.2.20:13602"], ["--runtime-url", "http://192.0.2.20:13602"],
                 "http://192.0.2.20:13602", None)):
            for failing in (False, True):
                with self.subTest(status=status_argv, failing=failing):
                    self.responses[THINGS] = 200, [thing("http:url:fixture", "OFFLINE" if failing else "ONLINE")]
                    told, asked = self.both(health_argv, status_argv)
                    self.assertEqual(asked, told)
                    self.assertEqual(asked[0], 1 if failing else 0)
                    self.assertEqual(asked[2], [(url,)])
                    self.assertEqual(asked[3], [{"token": token, "timeout": 15}])
                    told, asked = self.both([*health_argv, "--json"], [*status_argv, "--json"])
                    self.assertEqual(json.loads(asked[1]), json.loads(told[1]))
                    self.assertEqual(asked[1].count("\n"), 1, "one line, as every verb's --json")
                    self.assertEqual(asked[0], told[0])
        production = json.loads(self.both(["--production", "--json"], ["--runtime", "--json"])[1][1])
        self.assertEqual(production["metrics"], self.metrics, "of production, the services beside it too")
        self.assertIn("gateway", production)

    def test_the_runtimes_switches_reach_its_checks(self):
        self.responses[THINGS] = 200, [thing("http:url:gone", "OFFLINE", "COMMUNICATION_ERROR")]
        self.responses[ADDONS] = 200, []
        switches = ["--allow-offline", "http:url:gone", "--addons", str(self.addons), "--timeout", "4",
                    "--token-file", str(self.token)]
        told, asked = self.both(["--url", "http://192.0.2.20:13602", *switches],
                                ["--runtime-url", "http://192.0.2.20:13602", *switches])
        self.assertEqual(asked, told)
        self.assertEqual(asked[0], 1, "the missing add-on")
        self.assertEqual(asked[3], [{"token": TOKEN, "timeout": 4.0}])

    def test_installation_is_healths_installation(self):
        for things, expected in (([thing("http:url:fixture")], 0),
                                 ([thing("http:url:gone", "OFFLINE", "COMMUNICATION_ERROR")], 0),
                                 ([thing("http:url:bad", "OFFLINE", "CONFIGURATION_ERROR")], 1)):
            self.responses[THINGS] = 200, things
            for extra in ([], ["--json"], ["--addons", str(self.addons)]):
                with self.subTest(things=things[0]["UID"], extra=extra):
                    told, asked = self.both(["--production", "--installation", "--settle", "0", *extra],
                                            ["--installation", "--settle", "0", *extra])
                    if "--json" in extra:
                        self.assertEqual(json.loads(asked[1]), json.loads(told[1]))
                        self.assertEqual(json.loads(asked[1])["mode"], "installation")
                        self.assertEqual(asked[0], told[0])
                    else:
                        self.assertEqual(asked, told)
                        self.assertIn("RESULT:", asked[1])
                    self.assertEqual(asked[0], expected)

    def test_installation_takes_what_the_installer_passes(self):
        parser = oh_cli.parser_of("status")
        args = parser.parse_args(["--installation", "--settle", "120", "--addons", "conf/services/addons.cfg",
                                  "--revision", "0" * 40])
        self.assertTrue(args.installation)
        self.assertEqual((args.settle, args.addons, args.revision), (120, Path("conf/services/addons.cfg"), "0" * 40))
        seen = []
        with mock.patch.object(health, "production", side_effect=lambda args: seen.append(vars(args).copy())), \
                mock.patch.object(health, "settled", return_value={"failures": [], "mode": "installation",
                                                                   "things": {"things": []},
                                                                   "rules": {"disabled": []}}):
            code, out, _ = self.run_cli("status", "--installation", "--settle", "120", "--revision", "0" * 40)
        self.assertEqual((code, out), (0, "RESULT: installation ready\n"))
        self.assertTrue(seen[0]["production"] and seen[0]["installation"])
        self.assertEqual((seen[0]["settle"], seen[0]["revision"]), (120, "0" * 40))

    def test_a_runtime_that_cannot_be_queried_exits_2(self):
        self.responses["/rest/"] = 0, None
        code, out, err = self.run_cli("status", "--runtime-url", "http://192.0.2.20:13602")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "thermite status: http://192.0.2.20:13602/rest/ did not answer (HTTP connection failed)\n")
        self.token.unlink()
        code, out, err = self.run_cli("status", "--runtime")
        self.assertEqual(code, 2)
        self.assertIn(f"thermite status: no {self.token}: thermite install saves", err)

    def test_switches_that_do_not_go_together_are_refused(self):
        for argv in (["--installation", "--sandbox"], ["--installation", "--runtime-url", "http://192.0.2.20:1"],
                     ["--installation", "--allow-offline", "http:url:x"], ["--settle", "5"],
                     ["--runtime", "--settle", "5"], ["--installation", "--settle", "3601"],
                     ["--sandbox", "--revision", "0" * 40], ["--runtime", "--state-dir", "/tmp/x"],
                     ["--sandbox", "--production"], ["--runtime", "--no-runtime-checks"], ["--url", "http://x:1"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli("status", *argv)
                self.assertEqual(code, 2)
                self.client.assert_not_called()

    def test_retired_health_warns_and_runs_as_before(self):
        code, out, err = self.run_cli("health", "--url", "http://127.0.0.1:13616", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["url"], "http://127.0.0.1:13616")
        self.assertIn("\n  ", out, "health's own indented JSON, unchanged")
        self.assertIn("thermite health: thermite status --runtime replaces it", err)


class History(Fixture):
    def test_state_dir_and_its_old_spelling_list_the_same_deploys(self):
        layout = oh_deploy.State(self.state)
        layout.history.parent.mkdir(parents=True)
        layout.history.write_text(json.dumps({"deployed_at": "2026-02-01T00:00:00+00:00", "commit": "b" * 40,
                                              "by": "ops", "boot_checked": True, "how": "deploy",
                                              "subject": "A fixture deploy"}) + "\n")
        code, new, err = self.run_cli("history", "--state-dir", str(self.state))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("A fixture deploy", new)
        code, old, err = self.run_cli("history", "--state", str(self.state))
        self.assertEqual((code, old), (0, new))
        self.assertIn("thermite history: --state is now --state-dir; --state goes in the next release", err)
        code, out, _ = self.run_cli("history", "--state-dir", str(self.state), "--json")
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual([entry["subject"] for entry in json.loads(out)], ["A fixture deploy"])
        with contextlib.redirect_stdout(io.StringIO()) as empty:
            self.assertEqual(history.main(["--state-dir", str(self.root / "none")]), 0)
        self.assertEqual(empty.getvalue(), f"no deploys yet ({self.root / 'none/deploy/history'} does not exist or is empty)\n")


class Cameras(Fixture):
    """cameras list's runtime switches: --runtime-url and --token-file, and
    the spellings before them."""

    def run_cameras(self, *argv):
        self.client.reset_mock()
        house = SimpleNamespace(data={}, devices=[{"id": "fixture_camera"}])
        rows = [{"id": "fixture_camera", "label": "Fixture", "states": {"online": "ON", "motion": "OFF",
                                                                          "recording": "ON"}}]
        with mock.patch.object(cameras.House, "load", return_value=house), \
                mock.patch.object(cameras.remote_cameras, "validate", return_value=[]), \
                mock.patch.object(cameras.protect, "validate", return_value=[]), \
                mock.patch.object(cameras, "uses", return_value=True), \
                mock.patch.object(cameras, "status", return_value=rows) as read, \
                mock.patch.object(cameras.pair, "openhab", return_value="production") as production:
            code, out, err = self.run_cli("cameras", "list", *argv)
        return code, out, err, read, production

    def test_runtime_url_and_token_file(self):
        self.token.chmod(0o600)
        code, out, err, read, production = self.run_cameras("--runtime-url", "http://127.0.0.1:13602",
                                                            "--token-file", str(self.token), "--json")
        self.assertEqual((code, err), (0, ""))
        self.client.assert_called_once_with("http://127.0.0.1:13602", token=TOKEN, allow_write=True)
        self.assertIs(read.call_args.args[0], self.http)
        production.assert_not_called()
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out)["cameras"][0]["id"], "fixture_camera")
        code, out, err, read, production = self.run_cameras()
        self.assertEqual(read.call_args.args[0], "production")
        self.client.assert_not_called()

    def test_the_old_spellings_warn_and_do_the_same(self):
        self.token.chmod(0o600)
        for argv in (["--url", "http://127.0.0.1:13602", "--runtime-token-file", str(self.token)],
                     ["--url=http://127.0.0.1:13602", "--runtime-token-file", str(self.token)]):
            with self.subTest(argv=argv):
                code, out, err, read, production = self.run_cameras(*argv)
                self.assertEqual(code, 0)
                self.client.assert_called_once_with("http://127.0.0.1:13602", token=TOKEN, allow_write=True)
                self.assertIn("--url http://127.0.0.1:13602 is taken as --runtime-url until the next release", err)
                self.assertIn("thermite cameras list: --runtime-token-file is now --token-file; "
                              "--runtime-token-file goes in the next release", err)

    def test_one_runtime_only(self):
        for argv in (["--url", "http://127.0.0.1:13602", "--production"],
                     ["--runtime-url", "http://127.0.0.1:13602", "--production"]):
            with self.subTest(argv=argv):
                code, *_ = self.run_cameras(*argv)
                self.assertEqual(code, 2)
                self.client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
