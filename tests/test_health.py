"""Installation readiness reports device setup without weakening ordinary health."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_cli import health  # noqa: E402
import oh_sandbox as sandbox  # noqa: E402

TOKEN = "oh." + "fixture.private-token-sentinel"
PRIVATE = "private-configuration-sentinel"
HOST = {"state_root": "/srv/fixture", "http_port": 13601, "https_port": 13602,
        "uid": 9001, "gid": 9001, "timezone": "UTC"}
THINGS = "/rest/things?summary=false"
ITEMS = "/rest/items?recursive=false&fields=name,type,state,groupType"
ADDONS = "/rest/addons?serviceId=all"


def thing(uid, status="ONLINE", detail="NONE", description=None, *, kind="http:url", configuration=None):
    return {"UID": uid, "thingTypeUID": kind, "label": uid,
            "configuration": {"fixture": PRIVATE} if configuration is None else configuration,
            "statusInfo": {"status": status, "statusDetail": detail, "description": description}}


def heos(uid):
    return thing(uid, "ONLINE", "CONFIGURATION_ERROR", "Can't log in. Username or password not set.",
                 kind="heos:bridge", configuration={"ipAddress": "192.0.2.13", "fixture": PRIVATE})


def observed_devices():
    return [*(thing(f"http:url:fixture_online_{number}") for number in range(85)),
            *(thing(f"androidtv:googletv:fixture_shield_{number}", "OFFLINE", "NONE",
                    "GoogleTV: PIN Process Incomplete", kind="androidtv:googletv") for number in range(5)),
            thing("http:url:fixture_edid", "OFFLINE", "COMMUNICATION_ERROR", "No route to host"),
            thing("http:url:fixture_usb", "OFFLINE", "COMMUNICATION_ERROR", "Request timed out after 3000 ms"),
            heos("heos:bridge:fixture_receiver_a"), heos("heos:bridge:fixture_receiver_b")]


class InstallationThingVerdicts(unittest.TestCase):
    def test_complete_initializing_and_failed_states_use_the_strict_installation_policy(self):
        cases = [("ONLINE", None, "ok"), ("ONLINE", "NONE", "ok"),
                 *(('OFFLINE', detail, 'warn') for detail in (None, "NONE", "COMMUNICATION_ERROR", "BRIDGE_OFFLINE")),
                 *(('OFFLINE', detail, 'fail') for detail in ("CONFIGURATION_ERROR", "HANDLER_CONFIGURATION_PENDING",
                                                            "HANDLER_MISSING_ERROR", "UNEXPECTED_DETAIL")),
                 ("ONLINE", "CONFIGURATION_ERROR", "fail"),
                 *((status, detail, "fail") for status in ("INITIALIZING", "UNKNOWN", "UNINITIALIZED", "REMOVED")
                   for detail in (None, "NONE", "HANDLER_MISSING_ERROR", "NOT_YET_READY"))]
        for status, detail, expected in cases:
            with self.subTest(status=status, detail=detail):
                self.assertEqual(sandbox.installation_thing_verdict(thing("http:url:fixture", status, detail)), expected)

    def test_optional_heos_warning_requires_the_complete_exact_account_free_bridge(self):
        original = heos("heos:bridge:fixture")
        self.assertEqual(sandbox.installation_thing_verdict(original), "warn")
        for updates in ({"configuration": None}, {"configuration": {"username": "fixture-user"}},
                        {"configuration": {"password": PRIVATE}}, {"thingTypeUID": "http:url"},
                        {"UID": "heos:bridge:fixture:unexpected"},
                        {"statusInfo": {**original["statusInfo"], "status": "OFFLINE"}},
                        {"statusInfo": {**original["statusInfo"], "description": "Another configuration error"}}):
            with self.subTest(fields=tuple(updates)):
                self.assertEqual(sandbox.installation_thing_verdict({**original, **updates}), "fail")
        summary_dto = {key: value for key, value in original.items() if key != "configuration"}
        self.assertEqual(sandbox.installation_thing_verdict(summary_dto), "fail")

    def test_installation_summary_preserves_rows_and_keeps_normal_health_strict(self):
        rows = [thing("http:url:z_fixture", "INITIALIZING"), heos("heos:bridge:fixture"),
                thing("http:url:a_fixture", "OFFLINE", "COMMUNICATION_ERROR"),
                thing("http:url:a_fixture", "UNINITIALIZED", "HANDLER_MISSING_ERROR")]
        before = copy.deepcopy(rows)
        installation = sandbox.summarize_things(rows, installation=True)
        strict = sandbox.summarize_things(rows, require_online=True)
        self.assertEqual([row["verdict"] for row in installation["things"]], ["warn", "warn", "fail", "fail"])
        self.assertEqual([row["verdict"] for row in strict["things"]], ["warn", "fail", "fail", "fail"])
        self.assertEqual(len(installation["failed"]), 2)
        self.assertEqual(installation["count"], 4)
        self.assertEqual(rows, before)
        self.assertNotIn(PRIVATE, json.dumps(installation))


class Health(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA.parents[1] / ".scratch/install_health_tests"))
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="health-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addons = self.root / "conf/services/addons.cfg"
        self.addons.parent.mkdir(parents=True)
        self.addons.write_text("binding = http, androidtv\nmisc = metrics\n")
        self.token_file = self.root / "var/fixture/api-token"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(TOKEN + "\n")
        self.token_file.chmod(0o600)
        self.responses = {
            "/rest/": (200, {"runtimeInfo": {"version": "5.2.3"}}),
            THINGS: (200, [thing("http:url:fixture")]),
            ITEMS: (200, [{"name": "fixture_power", "type": "Switch", "state": "ON"}]),
            ADDONS: (200, [{"uid": uid, "installed": True}
                           for uid in ("binding-http", "binding-androidtv", "misc-metrics")]),
        }
        self.metrics = {"installed": True, "ok": True, "summary": "Prometheus ready, targets 53/53; Grafana answers",
                        "targets_up": 53, "targets": 53, "down": [], "systemd_collector": True}
        entry = mock.patch.object(health.oh_entry, "ready", return_value=True)
        self.entry = entry.start()
        self.addCleanup(entry.stop)

    def args(self, *, installation=True):
        return SimpleNamespace(url="http://127.0.0.1:13601", token_file=self.token_file, timeout=1,
                               production=True, installation=installation, allow_offline=None,
                               addons=self.addons, host=HOST)

    @contextlib.contextmanager
    def backend(self):
        http = mock.Mock()
        http.try_get_json.side_effect = lambda path: copy.deepcopy(self.responses[path])
        with mock.patch.object(health.lib, "Http", return_value=http) as client, \
                mock.patch.object(health.lib, "read_token", return_value=TOKEN), \
                mock.patch.object(health, "metrics_report", return_value=copy.deepcopy(self.metrics)):
            yield http, client

    def report(self, *, installation=True):
        with self.backend() as (http, client):
            result = health.run(self.args(installation=installation))
        client.assert_called_once_with("http://127.0.0.1:13601", token=TOKEN, timeout=1)
        self.assertEqual([call.args[0] for call in http.try_get_json.call_args_list],
                         ["/rest/", THINGS, ITEMS, ADDONS])
        return result

    def test_seven_device_problems_are_visible_warnings_only_for_installation(self):
        self.responses[THINGS] = 200, observed_devices()
        installation = self.report()
        strict = self.report(installation=False)
        self.assertEqual(installation["mode"], "installation")
        self.assertNotIn("mode", strict)
        self.assertEqual(installation["things"]["count"], 94)
        self.assertEqual(installation["failures"], [])
        self.assertEqual(installation["things"]["failed"], [])
        self.assertEqual(sum(row["verdict"] == "warn" for row in installation["things"]["things"]), 9)
        self.assertFalse(any(row["verdict"] == "allowed" for row in installation["things"]["things"]))
        self.assertEqual(len(strict["things"]["failed"]), 7)
        self.assertTrue(strict["failures"])
        self.assertEqual(sum(row["verdict"] == "warn" for row in strict["things"]["things"]), 2)

    def test_only_known_offline_setup_and_communication_details_are_nonfatal(self):
        for detail in (None, "NONE", "COMMUNICATION_ERROR", "BRIDGE_OFFLINE"):
            with self.subTest(detail=detail):
                self.responses[THINGS] = 200, [thing("http:url:fixture", "OFFLINE", detail)]
                result = self.report()
                self.assertEqual(result["failures"], [])
                self.assertEqual(result["things"]["things"][0]["verdict"], "warn")

    def test_handler_configuration_and_unfinished_states_remain_fatal(self):
        for status, detail in (("OFFLINE", "CONFIGURATION_ERROR"), ("ONLINE", "CONFIGURATION_ERROR"),
                               ("OFFLINE", "HANDLER_CONFIGURATION_PENDING"),
                               ("OFFLINE", "HANDLER_MISSING_ERROR"), ("OFFLINE", "UNEXPECTED_DETAIL"),
                               ("UNINITIALIZED", "NONE"), ("UNINITIALIZED", "HANDLER_MISSING_ERROR"),
                               ("UNINITIALIZED", "NOT_YET_READY"), ("UNINITIALIZED", "DISABLED"),
                               ("UNKNOWN", "NONE"), ("INITIALIZING", "NONE"), ("REMOVED", "NONE")):
            with self.subTest(status=status, detail=detail):
                self.responses[THINGS] = 200, [thing("http:url:fixture", status, detail)]
                result = self.report()
                self.assertTrue(result["failures"])
                self.assertEqual(result["things"]["things"][0]["verdict"], "fail")
                self.assertNotIn("not ONLINE", " ".join(result["failures"]))

    def test_only_the_existing_exact_optional_heos_account_case_is_nonfatal(self):
        base = heos("heos:bridge:fixture")
        self.responses[THINGS] = 200, [base]
        self.assertEqual(self.report()["failures"], [])
        variants = [{**base, "configuration": {"password": PRIVATE}},
                    {**base, "configuration": None}, {**base, "thingTypeUID": "http:url"},
                    {**base, "statusInfo": {**base["statusInfo"], "description": "Another configuration error"}}]
        for row in variants:
            with self.subTest(row=row):
                self.responses[THINGS] = 200, [row]
                self.assertTrue(self.report()["failures"])

    def test_http_authentication_and_service_failures_are_never_device_warnings(self):
        for path in ("/rest/", THINGS, ITEMS, ADDONS):
            original = self.responses[path]
            for status in (0, 401, 403, 500, 503):
                with self.subTest(path=path, status=status):
                    self.responses[path] = status, {"private": PRIVATE}
                    with self.backend(), self.assertRaises(sandbox.SandboxError) as error:
                        health.run(self.args())
                    self.assertNotIn(PRIVATE, str(error.exception))
            self.responses[path] = original

    def test_malformed_rest_dtos_fail_closed_without_configuration_disclosure(self):
        malformed = {
            "/rest/": (None, [], {}, {"runtimeInfo": []}, {"runtimeInfo": {"version": ""}},
                       {"runtimeInfo": {"version": 5}}),
            THINGS: (None, {}, [None], [PRIVATE], [{"UID": "http:url:fixture", "statusInfo": {}}],
                     [thing("", "ONLINE")], [thing("http:url:fixture", True)],
                     [thing("http:url:fixture", "ONLINE", True)],
                     [{**thing("http:url:fixture"), "label": {"private": PRIVATE}}],
                     [{**thing("http:url:fixture"), "label": [PRIVATE]}],
                     [thing("http:url:fixture", description={"private": PRIVATE})],
                     [thing("http:url:fixture", description=[PRIVATE])]),
            ITEMS: (None, {}, [None], [{}], [{"name": None, "type": "Switch", "state": "ON"}],
                    [{"name": "", "type": "Switch", "state": "ON"}],
                    [{"name": "fixture", "type": None, "state": "ON"}],
                    [{"name": "fixture", "type": "", "state": "ON"}],
                    [{"name": "fixture", "type": "Switch", "state": []}],
                    [{"name": "fixture", "type": "Group", "state": "NULL", "groupType": []}]),
            ADDONS: (None, {}, [None], [PRIVATE], [{"uid": "binding-http", "installed": "yes"}],
                     [{"installed": True}]),
        }
        for path, values in malformed.items():
            original = self.responses[path]
            for value in values:
                with self.subTest(path=path, value=value):
                    self.responses[path] = 200, value
                    with self.backend(), self.assertRaises(sandbox.SandboxError) as error:
                        health.run(self.args())
                    self.assertNotIn(PRIVATE, str(error.exception))
            self.responses[path] = original

    def test_missing_required_addons_remain_fatal(self):
        self.responses[ADDONS] = 200, [{"uid": "binding-http", "installed": True}]
        result = self.report()
        self.assertEqual(result["addons"]["missing"], ["binding-androidtv", "misc-metrics"])
        self.assertTrue(result["failures"])

    def test_addons_may_use_the_native_type_and_id_identity(self):
        self.responses[ADDONS] = 200, [{"type": kind, "id": name, "installed": True}
                                     for kind, name in (("binding", "http"), ("binding", "androidtv"),
                                                        ("misc", "metrics"))]
        self.assertEqual(self.report()["failures"], [])

    def test_native_string_item_states_may_be_empty(self):
        self.responses[ITEMS] = 200, [{"name": "fixture_text", "type": "String", "state": ""}]
        self.assertEqual(self.report()["failures"], [])

    def test_installation_requires_monitoring_to_be_installed_and_healthy(self):
        for metrics in ({"installed": False, "ok": True, "summary": "not installed"},
                        {**self.metrics, "ok": False, "summary": "systemd collector failed"},
                        {**self.metrics, "systemd_collector": None},
                        {**self.metrics, "ok": False, "summary": "Prometheus unavailable"},
                        {**self.metrics, "ok": False, "summary": "Grafana unavailable"}):
            with self.subTest(metrics=metrics):
                self.metrics = metrics
                self.assertTrue(self.report()["failures"])

    def test_installation_requires_the_thermite_entry_but_ordinary_health_is_unchanged(self):
        self.entry.return_value = False
        report = self.report()
        self.assertEqual(report["web_entry"], {"ok": False})
        self.assertIn("Thermite web entry is not ready", report["failures"])
        self.entry.assert_called_once_with("http://127.0.0.1:13601", timeout=1)
        self.entry.reset_mock()
        strict = self.report(installation=False)
        self.entry.assert_not_called()
        self.assertNotIn("web_entry", strict)
        self.assertEqual(strict["failures"], [])

    def test_installation_human_output_is_compact_actionable_and_has_no_secret_fields(self):
        self.responses[THINGS] = 200, observed_devices()
        self.responses[ITEMS] = 200, [{"name": f"fixture_undefined_{number}", "type": "Switch", "state": "NULL"}
                                    for number in range(251)]
        report = self.report()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            health.print_human(report)
        text = output.getvalue()
        self.assertIn("warn", text.lower())
        self.assertIn("pair", text.lower())
        self.assertIn("http:url:fixture_edid", text)
        self.assertIn("http:url:fixture_usb", text)
        self.assertIn("RESULT:", text)
        self.assertNotIn("fixture_undefined_0", text)
        self.assertLess(len(text.splitlines()), 35)
        for rendered in (text, json.dumps(report)):
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn(TOKEN, rendered)
        strict = self.report(installation=False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            health.print_human(strict)
        self.assertIn("fixture_undefined_0", output.getvalue())
        self.assertIn("NULL/UNDEF: 251", output.getvalue())

    def test_installation_prints_actual_runtime_failure_rows(self):
        self.responses[THINGS] = 200, [thing("http:url:fixture_bad", "OFFLINE", "CONFIGURATION_ERROR")]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            health.print_human(self.report())
        self.assertIn("FAIL", output.getvalue())
        self.assertIn("http:url:fixture_bad", output.getvalue())
        self.assertIn("CONFIGURATION_ERROR", output.getvalue())

    def test_production_defaults_installation_addons_to_the_selected_site_only(self):
        for installation in (False, True):
            args = self.args(installation=installation)
            args.token_file = args.addons = None
            with self.subTest(installation=installation), mock.patch.object(health, "AREA", self.root), \
                    mock.patch.object(health.deploy, "load", return_value=({}, HOST)), \
                    mock.patch.object(health.deploy, "hostname", return_value="fixture"):
                health.production(args)
            self.assertEqual(args.token_file, self.token_file)
            self.assertEqual(args.addons, self.addons if installation else None)
        args = self.args()
        explicit = self.root / "explicit-addons.cfg"
        args.addons = explicit
        with mock.patch.object(health, "AREA", self.root), mock.patch.object(health.deploy, "load", return_value=({}, HOST)):
            health.production(args)
        self.assertEqual(args.addons, explicit)

    def test_installation_cli_requires_production_and_rejects_allow_offline(self):
        for arguments in (["--installation"], ["--sandbox", "--installation"],
                          ["--url", "http://127.0.0.1:13615", "--installation"],
                          ["--production", "--installation", "--allow-offline", "http:url:fixture"]):
            with self.subTest(arguments=arguments), mock.patch.object(health, "production") as production, \
                    mock.patch.object(health, "run") as run, contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                health.main(arguments)
            self.assertEqual(error.exception.code, 2)
            production.assert_not_called()
            run.assert_not_called()

    def test_installation_cli_json_reports_warnings_and_success_without_secret_configuration(self):
        self.responses[THINGS] = 200, observed_devices()
        def production(args):
            args.url, args.token_file, args.host, args.addons = self.args().url, self.token_file, HOST, self.addons
        with self.backend(), mock.patch.object(health, "production", side_effect=production), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(health.main(["--production", "--installation", "--json"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["mode"], "installation")
        self.assertEqual(result["failures"], [])
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertNotIn(TOKEN, output.getvalue())

    def test_normal_allow_offline_remains_an_explicit_separate_policy(self):
        self.responses[THINGS] = 200, [thing("http:url:fixture", "OFFLINE", "COMMUNICATION_ERROR")]
        args = self.args(installation=False)
        args.allow_offline = ["http:url:fixture"]
        with self.backend():
            result = health.run(args)
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["things"]["things"][0]["verdict"], "allowed")


if __name__ == "__main__":
    unittest.main()
