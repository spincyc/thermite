"""Installer lifecycle flow: one transient rule, one native deadline, one Thing
predicate, the native preparation's scope and recorded unit ownership."""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli  # noqa: E402
import oh_deploy as deploy  # noqa: E402
import oh_http  # noqa: E402
import oh_sandbox as sandbox  # noqa: E402
from oh_access import deployment, native_boundary as boundary  # noqa: E402
from oh_cli import health, uninstall  # noqa: E402
from oh_install import native_auth, ownership, progress, steps  # noqa: E402
from oh_install.access import Gateway  # noqa: E402
from oh_install.model import Check, Task  # noqa: E402
from oh_install.system import NativeProbe, System, Unit, UnitFile, UnitOrigin  # noqa: E402
from tests.test_access_boundary import Native, ORIGIN  # noqa: E402


def scratch() -> Path:
    root = Path(os.environ.get("TMPDIR") or AREA / ".scratch/lifecycle-flow-tests")
    root.mkdir(parents=True, exist_ok=True)
    return root


def clocked(system):
    now = [0.0]
    system.clock.side_effect = lambda: now[0]
    system.sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + seconds)
    return now


def thing(uid, status, detail="NONE"):
    return {"UID": uid, "statusInfo": {"status": status, "statusDetail": detail}}


class Redirector(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        Redirector.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class DirectHttp(unittest.TestCase):
    """S9: runtime bearer credentials never reach a proxy or a redirect target."""

    def setUp(self):
        Redirector.seen = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def test_requests_ignore_environment_proxies_and_return_redirects(self):
        proxy = {"http_proxy": "http://192.0.2.1:9", "HTTP_PROXY": "http://192.0.2.1:9", "no_proxy": "", "NO_PROXY": ""}
        for remaining in (None, lambda: 5):
            with self.subTest(deadline=remaining is not None), mock.patch.dict(os.environ, proxy):
                Redirector.seen = []
                token = secrets.token_urlsafe(16)
                bearer = {}
                bearer["Authorization"] = "Bearer " + token
                request = urllib.request.Request(self.origin + "/direct", headers=bearer)
                self.assertEqual(oh_http.request(request, timeout=5, remaining=remaining), (200, b"{}"))
                request = urllib.request.Request(self.origin + "/start", headers=bearer)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    oh_http.request(request, timeout=5, remaining=remaining)
                error.exception.close()
                self.assertEqual(error.exception.code, 302)
                self.assertEqual([path for path, _ in Redirector.seen], ["/direct", "/start"],
                                 "the proxy is unused and the redirect is not followed")


class TransientRule(unittest.TestCase):
    def test_one_rule_for_native_reads_and_none_for_writes(self):
        for status in (None, 0, 404, 429, 500, 502, 503, 504):
            self.assertTrue(oh_http.retryable("GET", status), status)
            self.assertFalse(oh_http.retryable("PUT", status), status)
        for status in (200, 301, 400, 401, 403, 501):
            self.assertFalse(oh_http.retryable("GET", status), status)
        self.assertEqual(deployment.TRANSIENT_STATUSES, oh_http.TRANSIENT_STATUSES,
                         "the standalone gateway helper mirrors the shared rule")
        self.assertEqual(native_auth.TRANSIENT_STATUSES, oh_http.TRANSIENT_STATUSES,
                         "the standalone onboarding helper mirrors the shared rule")
        self.assertTrue(steps.Token.transient(503))
        self.assertFalse(steps.Token.transient(501))


class CommissioningTransport(unittest.TestCase):
    """L1 and L15: GET transport failures retry within the deadline; PUT never repeats."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=scratch())
        self.addCleanup(temporary.cleanup)
        self.token = Path(temporary.name) / "token"
        self.token.write_bytes(b"private-test-token\n")
        self.token.chmod(0o600)
        self.native = Native()

    def configure(self, request, **kwargs):
        return boundary.configure(ORIGIN, self.token, {"devices": []}, "192.0.2.10", request=request,
                                  clock=lambda: self.native.now, sleep=self.native.sleep, **kwargs)

    def test_refused_reads_are_retried_until_rest_returns(self):
        failures = [3]

        def request(method, url, **options):
            if method == "GET" and failures[0]:
                failures[0] -= 1
                self.native.now += 0.01
                raise boundary.TransportError("Native commissioning request failed; rerun the deployment to resume")
            return self.native.request(method, url, **options)

        recorded = []
        result = self.configure(request, timeout=30, record=lambda path, original: recorded.append((path, original)))
        self.assertTrue(result["native_security"])
        self.assertEqual(failures, [0])
        self.assertEqual(recorded, [(boundary.AUTH_CONFIG, {"implicitUserRole": True, "trustedNetworks": "192.0.2.0/24",
                                                            "allowBasicAuth": False, "cacheExpiration": 321})],
                         "the original mapping is recorded once, before the replacing PUT")

    def test_refused_reads_until_the_deadline_name_the_phase(self):
        def request(method, url, **options):
            self.native.now += 0.5
            raise boundary.TransportError("Native commissioning request failed; rerun the deployment to resume")

        with self.assertRaises(boundary.BoundaryError) as error:
            self.configure(request, timeout=5)
        self.assertIn("exceeded its deadline during REST authentication configuration read (connection failed)",
                      str(error.exception))

    def test_a_failed_put_is_not_repeated_and_unrecorded_original_blocks_the_put(self):
        def request(method, url, **options):
            if method == "PUT":
                self.native.calls.append((method, url))
                raise boundary.TransportError("Native commissioning request failed; rerun the deployment to resume")
            return self.native.request(method, url, **options)

        with self.assertRaises(boundary.TransportError):
            self.configure(request, timeout=30)
        self.assertEqual([call[0] for call in self.native.calls].count("PUT"), 1)
        self.native.calls.clear()

        def broken(path, original):
            raise OSError("read-only")

        with self.assertRaises(boundary.BoundaryError) as error:
            self.configure(self.native.request, timeout=30, record=broken)
        self.assertIn("nothing was replaced", str(error.exception))
        self.assertNotIn("PUT", [call[0] for call in self.native.calls])

    def test_shared_installer_deadlines_beyond_two_minutes_are_accepted(self):
        self.assertTrue(self.configure(self.native.request, timeout=900)["native_security"])
        with self.assertRaises(boundary.BoundaryError):
            self.configure(self.native.request, timeout=boundary.MAX_DEADLINE + 1)


def context(**changes):
    values = dict(area=AREA, versions={"openhab": {"version": "5.2.3"}},
                  host={"root": "/srv/fictional", "uid": 9001, "gid": 9001, "http_port": 13602},
                  head="a" * 40, conf_digest="b" * 64, hostname="fixture", address=None,
                  prepared={"conf/services/addons.cfg": deploy.Blob(b"binding=sonos\n")},
                  application={"version": "0.1.0.dev0", "digest": "c" * 64})
    values.update(changes)
    return steps.Context(**values)


class NativeDeadlinePolicy(unittest.TestCase):
    def test_operator_override_bounds(self):
        self.assertEqual(steps.native_timeout_argument("1800"), 1800)
        for text in ("299", "7201", "ten"):
            with self.subTest(text=text), self.assertRaises(Exception):
                steps.native_timeout_argument(text)
        self.assertEqual(steps.NativeDeadline.of(context(native_timeout=1800)).budget, 1800)
        self.assertEqual(steps.NativeDeadline.of(SimpleNamespace()).budget, steps.WAIT)

    def test_only_native_phases_spend_the_one_budget(self):
        system = mock.Mock(spec=System)
        now = clocked(system)
        native = steps.NativeDeadline(100)
        self.assertEqual(native.start(system), 100)
        now[0] = 30
        native.stop(system)
        now[0] = 500  # monitoring image downloads between native phases
        self.assertEqual(native.start(system), 570)
        self.assertEqual(native.remaining(system), 70)
        now[0] = 900
        native.stop(system)
        self.assertEqual(native.remaining(system), 0, "an exhausted budget stays exhausted")

    def test_one_policy_object_spans_startup_commissioning_and_final_readiness(self):
        from oh_install import changeover
        for sequence in (changeover.switching(context(), "20261105T101500Z-3f9a2c"),):
            types = [type(step) for step in sequence]
            settle, gateway = sequence[types.index(steps.NativeSettle)], sequence[types.index(Gateway)]
            self.assertEqual(types.index(steps.Token) + 1, types.index(steps.NativeSettle))
            self.assertEqual(types.index(steps.NativeSettle) + 1, types.index(Gateway))
            self.assertIsInstance(gateway.boundary, steps.Commissioning)
            self.assertIs(gateway.boundary.gateway, gateway)
            natives = {id(step.native) for step in sequence
                       if isinstance(step, (steps.Answers, steps.Token, steps.NativeReadiness))}
            natives.add(id(gateway.boundary.native))
            self.assertEqual(len(natives), 1)

    def test_commissioning_uses_the_remaining_allowance_and_keeps_callbacks_in_the_fixed_inputs(self):
        temporary = tempfile.TemporaryDirectory(dir=scratch())
        self.addCleanup(temporary.cleanup)
        system = mock.Mock(spec=System)
        now = clocked(system)
        native = steps.NativeDeadline(600)
        native.start(system)
        now[0] = 450
        native.stop(system)
        hook = steps.Commissioning(context(), native)
        hook.gateway = SimpleNamespace(inputs=Path(temporary.name), options={"house": "/fictional/devices.yaml"})
        calls, callbacks = [], {"version": 1, "routes": {}}

        def configure(*args, **kwargs):
            calls.append(kwargs)
            return {"native_security": True, "callbacks": callbacks if len(calls) > 1 else None}

        with mock.patch.object(boundary, "configure", side_effect=configure), \
                mock.patch("oh_inventory.load", return_value={"devices": []}):
            self.assertIsNone(hook(system, {}, None))
            self.assertNotIn("record", calls[0], "userdata, set aside by the transaction, keeps the originals")
            self.assertEqual(calls[0]["timeout"], 150)
            self.assertEqual(native.deadline, None)
            native.start(system)
            selected = hook(system, {}, None)
        self.assertEqual(selected, Path(temporary.name) / "callbacks.json")
        self.assertEqual(json.loads(selected.read_text()), callbacks)
        native.spent = 600
        with mock.patch.object(boundary, "configure") as never, self.assertRaises(Exception) as error:
            hook(system, {}, None)
        never.assert_not_called()
        self.assertIn("--native-timeout", str(error.exception))


class ThingReadiness(unittest.TestCase):
    """L5: only configuration errors fail fast; bridged players wait for their bridge."""

    def fixture(self, rows, wait=60):
        step = steps.NativeReady(context(), steps.NativeDeadline(wait))
        step.settle = 0
        system = mock.Mock(spec=System)
        now = clocked(system)
        system.read_private.return_value = "fictional-runtime-token"
        system.unit.return_value = Unit("active", "enabled", 0, 1)
        sequence = iter(rows)
        current = [None]

        def probe(url, token, path, deadline, **kwargs):
            if path == step.addons_path:
                current[0] = next(sequence, current[0])
                return NativeProbe(200, [{"uid": "binding-sonos", "installed": True}])
            if path == step.things_path:
                return NativeProbe(200, current[0])
            return NativeProbe(200)

        system.native_probe.side_effect = probe
        return step, system, now

    def test_bridge_uninitialized_children_are_pending_until_their_bridge_is_online(self):
        player = "heos:player:fixture"
        rows = [[thing("heos:bridge:fixture", "INITIALIZING"), thing(player, "UNINITIALIZED", "BRIDGE_UNINITIALIZED")],
                [thing("heos:bridge:fixture", "ONLINE"), thing(player, "UNINITIALIZED", "BRIDGE_UNINITIALIZED")],
                [thing("heos:bridge:fixture", "ONLINE"), thing(player, "ONLINE")]]
        step, system, now = self.fixture(rows)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(step.wait(system), 0)
        self.assertEqual(now[0], 10)
        system.run.assert_not_called()

    def test_unfinished_things_at_the_deadline_are_named(self):
        step, system, now = self.fixture([[thing("sonos:PLAY1:fixture", "OFFLINE", "HANDLER_INITIALIZING_ERROR")]], wait=20)
        self.assertEqual(step.wait(system), 1)
        self.assertEqual(now[0], 20)
        message = " ".join(call.args[0] for call in system.say.call_args_list)
        self.assertIn("sonos:PLAY1:fixture OFFLINE/HANDLER_INITIALIZING_ERROR", message)
        self.assertIn("--native-timeout", message)

    def test_configuration_errors_fail_at_once_with_their_uid(self):
        step, system, now = self.fixture([[thing("http:url:fixture", "OFFLINE", "CONFIGURATION_ERROR")]])
        self.assertEqual(step.wait(system), 1)
        self.assertEqual(now[0], 0)
        self.assertIn("http:url:fixture OFFLINE/CONFIGURATION_ERROR", system.say.call_args.args[0])

    def test_settle_before_commissioning_needs_add_ons_not_thing_initialization(self):
        step = steps.NativeSettle(context(), steps.NativeDeadline(60))
        system = mock.Mock(spec=System)
        now = clocked(system)
        system.read_private.return_value = "fictional-runtime-token"
        system.unit.return_value = Unit("active", "enabled", 0, 1)
        addons = iter([[], [{"uid": "binding-sonos", "installed": True}]])
        last = [[]]

        def probe(url, token, path, deadline, **kwargs):
            self.assertNotEqual(path, step.metrics_path)
            if path == step.addons_path:
                last[0] = next(addons, last[0])
                return NativeProbe(200, last[0])
            return NativeProbe(200, [thing("sonos:PLAY1:fixture", "UNINITIALIZED", "HANDLER_MISSING_ERROR")])

        system.native_probe.side_effect = probe
        self.assertEqual(step.wait(system), 0)
        self.assertEqual(now[0], 15, "required add-ons, then ten seconds of stable REST")
        system.run.assert_not_called()


class UnitFailures(unittest.TestCase):
    """L17: a failed or crash-looping runtime ends the wait with its cause."""

    def test_failed_unit_and_crash_loop_end_runtime_wait(self):
        for units, expected in (([Unit("failed", "enabled", None, None)], "failed"),
                                ([Unit("activating", "enabled", 0, generation) for generation in (1, 2, 3, 4)], "crash loop")):
            with self.subTest(expected=expected):
                step = steps.Answers(SimpleNamespace(url="http://127.0.0.1:13602"), steps.NativeDeadline(600))
                system = mock.Mock(spec=System)
                now = clocked(system)
                system.openhab_version.return_value = None
                system.unit.side_effect = [*units, *([units[-1]] * 200)]
                self.assertEqual(step.wait(system), 1)
                self.assertLess(now[0], 600)
                self.assertIn(expected, system.say.call_args.args[0])

    def test_long_waits_show_their_current_state(self):
        system = mock.Mock(spec=System)
        with mock.patch.object(oh_cli, "switches", oh_cli.Switches(verbose=True)), \
                contextlib.redirect_stdout(io.StringIO()):
            with progress.Progress(system, "1. Runtime readiness") as display:
                progress.update("REST at http://127.0.0.1:13602 is not ready")
                progress.update("REST at http://127.0.0.1:13602 is not ready")
                display.finish("OK")
        shown = [call.args[0] for call in system.say.call_args_list if "waiting:" in call.args[0]]
        self.assertEqual(len(shown), 1)
        self.assertIn("is not ready", shown[0])


class NativePreparationScope(unittest.TestCase):
    """What the native preparation repairs, once Halt has stopped Thermite."""

    def test_the_preparation_stops_and_starts_nothing(self):
        system = mock.Mock(spec=System)
        system.unit.return_value = Unit("active", "enabled", 0, 1)
        system.stat.return_value = SimpleNamespace(uid=9001, gid=9001, mode=0o750, is_dir=True)
        with self.owned_everything(), mock.patch.object(deploy, "prepare_openhab_argv", return_value=("fixture-prepare",)):
            for receipt in (True, False):
                with mock.patch.object(steps, "service_receipt", return_value=receipt):
                    for step in (steps.NativePreparation(context()),):
                        with self.subTest(step=type(step).__name__, receipt=receipt):
                            self.assertFalse([action for action in step.actions(system) if action.argv[0] == "systemctl"],
                                             "Halt stopped all of Thermite before this step")

    def owned_everything(self):
        names = ("thermite-openhab.service", "thermite-atv.service", "thermite-grafana.service",
                 "thermite-prometheus.service", "thermite.target", "thermite-atv-restart.path")
        return mock.patch.object(ownership, "inspect", return_value=ownership.Owned(
            entries=tuple((name, name, Path("/fictional")) for name in names)))

    def test_an_openhab_only_change_repairs_only_its_trees(self):
        system = mock.Mock(spec=System)
        system.unit.return_value = Unit("active", "enabled", 0, 1)
        with self.owned_everything(), mock.patch.object(steps, "service_receipt", return_value=True), \
                mock.patch.object(steps.StateDirectories, "wrong", return_value=[]), \
                mock.patch.object(steps.Account, "missing", return_value=[]), \
                mock.patch.object(deploy, "prepare_openhab_argv", return_value=("fixture-prepare",)):
            preparation = steps.NativePreparation(context()).actions(system)
        repairs = [action.argv for action in preparation if action.argv[1:2] and action.argv[1].endswith("lib/oh_service.py")]
        self.assertEqual([argv[-1] for argv in repairs], ["--runtime", "--check-pairing-logging", "--runtime"])
        self.assertFalse(any("--receipt" in argv for argv in repairs), "only a full repair records the receipt")

    def test_custody_repair_records_the_receipt(self):
        system = mock.Mock(spec=System)
        system.unit.return_value = Unit("active", "enabled", 0, 1)
        with self.owned_everything(), mock.patch.object(steps, "service_receipt", return_value=False), \
                mock.patch.object(deploy, "prepare_openhab_argv", return_value=("fixture-prepare",)):
            preparation = steps.NativePreparation(context()).actions(system)
        self.assertEqual(preparation[0].argv[-4:], ("--uid", "9001", "--gid", "9001"), "the full repair")
        self.assertEqual(preparation[-1].argv[-1], "--receipt")

    def test_a_receipt_of_the_previous_policy_reruns_the_full_preparation(self):
        # The upgrade that moves the logs: a v4 receipt no longer stands for a full repair.
        root = context().layout.root
        system = mock.Mock(spec=System)
        receipt = {"version": 1, "uid": 9001, "gid": 9001, "policy": "thermite-service-v4"}
        system.unit_file.side_effect = lambda path: (UnitFile(json.dumps(receipt).encode(), 9001, 0o660, gid=9001)
                                                     if Path(path) == root / steps.oh_service.RECEIPT else None)
        self.assertFalse(steps.service_receipt(context(), system))
        receipt["policy"] = steps.oh_service.POLICY
        self.assertTrue(steps.service_receipt(context(), system))
        receipt["policy"] = "thermite-service-v4"
        with mock.patch.object(steps.StateDirectories, "wrong", return_value=[]), \
                mock.patch.object(steps.MetricsDirectories, "wrong", return_value=[]), \
                mock.patch.object(steps.Account, "missing", return_value=[]):
            preparation = steps.NativePreparation(context())
            self.assertTrue(preparation.custody(system))
            self.assertFalse(preparation.check(system).done)

    def test_preparation_ignores_other_units_files(self):
        system = mock.Mock(spec=System)
        system.unit.return_value = Unit("active", "enabled", 10, 1)
        system.stat.return_value = SimpleNamespace(mtime=0)
        stale = lambda units, system, names=None: [name for name in (deploy.ATV_QUADLET,)  # noqa: E731
                                                   if names is None or name in names]
        with mock.patch.object(steps, "service_receipt", return_value=True), \
                mock.patch.object(steps.StateDirectories, "wrong", return_value=[]), \
                mock.patch.object(steps.Account, "missing", return_value=[]), \
                mock.patch.object(steps.Units, "stale", stale), \
                mock.patch.object(steps.PairingLogging, "check", return_value=Check(True, "fixture")), \
                mock.patch.object(steps.NativeListeners, "check", return_value=Check(True, "fixture")):
            self.assertTrue(steps.NativePreparation(context()).check(system).done,
                            "a changed Apple TV helper unit restarts the helper, not openHAB")


class Health(unittest.TestCase):
    """L2: final installation health settles pending Things and only fails on errors."""

    def args(self, settle=30):
        return SimpleNamespace(installation=True, settle=settle)

    def report(self, rows, failures=()):
        summary = sandbox.summarize_things(rows, installation=True)
        return {"things": summary, "failures": list(failures)}

    def test_pending_things_are_polled_then_reported_as_warnings(self):
        clock = [0.0]
        reports = iter([self.report([thing("sonos:PLAY1:a", "UNINITIALIZED", "HANDLER_MISSING_ERROR")]),
                        self.report([thing("sonos:PLAY1:a", "ONLINE")])])
        with mock.patch.object(health, "run", side_effect=lambda args: next(reports)):
            result = health.settled(self.args(), clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertEqual(result["things"]["pending"], [])
        self.assertEqual(clock[0], 5)
        pending = self.report([thing("sonos:PLAY1:a", "UNINITIALIZED", "HANDLER_MISSING_ERROR")])
        clock[0] = 0
        with mock.patch.object(health, "run", return_value=pending):
            result = health.settled(self.args(), clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertEqual(clock[0], 30)
        self.assertEqual(result["failures"], [])
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                mock.patch.object(oh_cli, "switches", oh_cli.Switches()):
            health.print_human({**result, "mode": "installation"})
        self.assertIn("WAIT", output.getvalue())
        self.assertIn("installation ready", output.getvalue())
        self.assertIn("still initializing", output.getvalue())

    def test_configuration_errors_and_refusals_end_the_settle_window(self):
        clock = [0.0]
        failing = self.report([thing("http:url:a", "OFFLINE", "CONFIGURATION_ERROR"),
                               thing("sonos:PLAY1:a", "INITIALIZING")], failures=["1 Thing(s) have configuration errors"])
        with mock.patch.object(health, "run", return_value=failing):
            self.assertIs(health.settled(self.args(), clock=lambda: clock[0], sleep=mock.Mock()), failing)
        with mock.patch.object(health, "run", side_effect=sandbox.SandboxError("refused (HTTP 401)")), \
                self.assertRaises(sandbox.SandboxError):
            health.settled(self.args(), clock=lambda: clock[0], sleep=mock.Mock())

    def test_transient_rest_outages_are_retried_within_the_window(self):
        clock = [0.0]
        outcomes = [health.Unavailable("/rest/things answered HTTP 503"), self.report([thing("http:url:a", "ONLINE")])]

        def run(args):
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(health, "run", side_effect=run):
            result = health.settled(self.args(), clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertEqual(result["failures"], [])
        self.assertEqual(health.unavailable(503), health.Unavailable)
        self.assertEqual(health.unavailable(401), sandbox.SandboxError)
        self.assertEqual(health.unavailable(200), sandbox.SandboxError)


class SandboxMirror(unittest.TestCase):
    def test_renderer_refusal_is_a_reported_error_not_a_traceback(self):
        temporary = tempfile.TemporaryDirectory(dir=scratch())
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "conf"
        (source / "rules").mkdir(parents=True)
        (source / "rules/fixture.yaml").write_text("rule: fixture\n")

        def refuse(files):
            raise ValueError("rule fixture has no SEND declaration")

        with mock.patch.object(sandbox, "is_disposable", return_value=True), \
                self.assertRaises(sandbox.SandboxError) as error:
            sandbox.mirror_conf(source, Path(temporary.name) / "mirror", refuse)
        self.assertIn("no SEND declaration", str(error.exception))
        self.assertFalse((Path(temporary.name) / "mirror").exists())


class UnitOwnership(unittest.TestCase):
    """D1: recorded hashes, not re-rendered templates, prove unit ownership."""

    SOURCE, TARGET = "deploy/thermite-fixture.service", Path("/etc/systemd/system/thermite-fixture.service")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=scratch())
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name)
        for target, value in ((deploy, "INSTALLED"), (deploy, "CONTAINER_DROPINS")):
            patcher = mock.patch.object(target, value, {self.SOURCE: self.TARGET} if value == "INSTALLED" else {})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.installed = b"[Unit]\nDescription=fixture installed by an earlier release\n"
        self.files = {self.TARGET: UnitFile(self.installed, 0, 0o644)}

    def system(self):
        system = mock.Mock(spec=System)
        system.safe_unit_parent.return_value = True
        system.lexists.side_effect = lambda path: Path(path) in self.files
        system.unit_file.side_effect = lambda path: self.files.get(Path(path))
        system.unit_origins.side_effect = lambda names: {name: UnitOrigin(fragment=str(self.TARGET), enabled="enabled")
                                                         for name in names}
        return system

    def inspect(self, template=b"[Unit]\nDescription=fixture as rendered now\nTimezone=changed\n"):
        context = SimpleNamespace(versions={}, host={}, var=self.area / "var", application={"digest": "d" * 64},
                                  unit=lambda name: template)
        with mock.patch.object(deploy, "render_units", return_value={self.SOURCE: template.decode()}):
            return ownership.inspect(context, self.system()), context

    def test_changed_templates_do_not_strand_recorded_units(self):
        owned, _ = self.inspect()
        self.assertTrue(owned.problems, "neither recorded nor rendered: refuse")
        self.files[ownership.MANIFEST] = UnitFile(json.dumps({"version": 1, "release": None, "units": {
            str(self.TARGET): ownership.sha256(self.installed)}}).encode(), 0, 0o644)
        owned, _ = self.inspect()
        self.assertEqual(owned.problems, ())
        self.assertTrue(owned.recorded)
        self.assertEqual(owned.units(), ["thermite-fixture.service"])
        self.files[ownership.MANIFEST] = UnitFile(self.files[ownership.MANIFEST].content, 1000, 0o644)
        self.assertTrue(self.inspect()[0].problems, "an owner-writable manifest proves nothing")

    def test_units_installed_before_the_manifest_are_taken_over_from_an_earlier_build(self):
        build = self.area / "var/site-builds" / ("e" * 40 + "-" + "f" * 64) / self.SOURCE
        build.parent.mkdir(parents=True)
        build.write_bytes(self.installed)
        owned, context = self.inspect()
        self.assertEqual(owned.problems, ())
        self.assertFalse(owned.recorded, "the Units step records the takeover")
        manifest = json.loads(ownership.manifest_bytes(context))
        self.assertEqual(manifest["release"], "d" * 64)
        self.assertEqual(manifest["units"], {str(self.TARGET): ownership.sha256(context.unit(self.SOURCE))})

    def test_units_step_records_the_manifest_after_installing(self):
        system = mock.Mock(spec=System)
        system.read_bytes.return_value = None
        system.stat.return_value = object()
        step = steps.Units(context(prepared={self.SOURCE: deploy.Blob(b"unit")}))
        actions = step.actions(system)
        self.assertEqual(actions[-1].argv[-2:], (str(step.manifest), str(ownership.MANIFEST)))
        self.assertTrue(actions[-1].root)
        self.assertIsInstance(actions[-2], Task)


class Uninstall(unittest.TestCase):
    """D8: uninstall names what it keeps."""

    def test_kept_names_the_state_an_uninstall_keeps_and_a_purge_removes(self):
        text = uninstall.kept(context(), purge=False)
        for expected in ("Thermite's state under /srv/fictional", "/etc/thermite/installed.json", "the images",
                         "thermite uninstall --purge removes the state and the images too"):
            self.assertIn(expected, text)
        purged = uninstall.kept(context(), purge=True)
        self.assertNotIn("/srv/fictional", purged)
        self.assertIn("var/gateway/authority", purged)


if __name__ == "__main__":
    unittest.main()
