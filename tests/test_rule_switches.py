"""Every install switches thermite's own rules back on: the runtime keeps a
rule's disabled flag through restarts and installs, and a rule switched off
there leaves its device quiet with no error anywhere."""
from __future__ import annotations

import contextlib
import http.server
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_sandbox as sandbox  # noqa: E402
from oh_install import changeover, steps  # noqa: E402
from oh_install.system import NativeProbe, System  # noqa: E402

TOKEN = "oh." + "fixture.rule-switch-token"
URL = "http://127.0.0.1:13602"


def rule(uid, detail="NONE", *, editable=False):
    return {"uid": uid, "name": f"{uid} rule", "editable": editable,
            "status": {"status": "UNINITIALIZED" if detail == "DISABLED" else "IDLE", "statusDetail": detail}}


def context(**changes):
    values = dict(area=AREA, versions={"openhab": {"version": "5.2.3"}},
                  host={"root": "/srv/fictional", "uid": 9001, "gid": 9001, "http_port": 13602},
                  head="a" * 40, conf_digest="b" * 64, hostname="fixture", address=None,
                  prepared={"conf/services/addons.cfg": deploy.Blob(b"binding=http\n")},
                  application={"version": "0.1.0.dev0", "digest": "c" * 64})
    values.update(changes)
    return steps.Context(**values)


class DisabledRules(unittest.TestCase):
    def test_only_thermites_disabled_rules_count(self):
        rules = [rule("fixture_b_poll", "DISABLED"), rule("fixture_a_listen", "DISABLED"),
                 rule("fixture_owner_scene", "DISABLED", editable=True), rule("fixture_command"),
                 rule("fixture_broken", "HANDLER_INITIALIZING_ERROR")]
        self.assertTrue(sandbox.valid_rules(rules))
        self.assertEqual(sandbox.disabled_rules(rules), [{"uid": "fixture_a_listen", "name": "fixture_a_listen rule"},
                                                         {"uid": "fixture_b_poll", "name": "fixture_b_poll rule"}])

    def test_a_listing_without_the_fields_it_reads_is_not_valid(self):
        for rules in (None, {}, [None], [{**rule("fixture"), "editable": None}], [{**rule("fixture"), "uid": ""}],
                      [{**rule("fixture"), "status": {"statusDetail": "DISABLED"}}]):
            with self.subTest(rules=rules):
                self.assertFalse(sandbox.valid_rules(rules))


class RuleSwitches(unittest.TestCase):
    def setUp(self):
        self.system = mock.Mock(spec=System)
        self.now = [0.0]
        self.system.clock.side_effect = lambda: self.now[0]
        self.system.sleep.side_effect = lambda seconds: self.now.__setitem__(0, self.now[0] + seconds)
        self.system.read_private.return_value = TOKEN + "\n"
        self.rules = [rule("fixture_switch_poll", "DISABLED"), rule("fixture_switch_command"),
                      rule("fixture_owner_scene", "DISABLED", editable=True)]
        self.listings = []

        def probe(url, token, path, deadline, **kwargs):
            self.assertEqual((url, token, path), (URL, TOKEN, sandbox.RULES_PATH))
            self.listings.append(self.now[0])
            return NativeProbe(200, [dict(row) for row in self.rules])

        def post(url, token, path, body, deadline):
            self.assertEqual((url, token, body), (URL, TOKEN, "true"))
            uid = path.removeprefix("/rest/rules/").removesuffix("/enable")
            for index, row in enumerate(self.rules):
                if row["uid"] == uid:
                    self.rules[index] = rule(uid, editable=row["editable"])
                    return NativeProbe(200)
            return NativeProbe(404)

        self.system.native_probe.side_effect = probe
        self.system.native_post.side_effect = post
        self.step = steps.RuleSwitches(context())

    def run_step(self) -> int:
        found = self.step.check(self.system)
        self.assertIsNone(found.problem)
        if found.done:
            return 0
        for action in self.step.actions(self.system):
            code = action.perform(self.system)
            if code:
                return code
        after = self.step.after_actions(self.system)
        self.assertIsNone(after.problem)
        self.assertTrue(after.done, after.detail)
        return 0

    def test_thermites_disabled_rules_are_switched_back_on_and_others_left_alone(self):
        self.assertEqual(self.step.check(self.system).detail, "1 disabled in the runtime: fixture_switch_poll")
        self.assertEqual(self.run_step(), 0)
        self.system.native_post.assert_called_once_with(URL, TOKEN, "/rest/rules/fixture_switch_poll/enable",
                                                        "true", mock.ANY)
        self.assertEqual(self.rules[2]["status"]["statusDetail"], "DISABLED", "the owner's own rule stays off")
        self.system.say.assert_any_call("   switched fixture_switch_poll back on")
        self.assertEqual(self.step.check(self.system).detail,
                         "none of the runtime's 3 rules that thermite defines is disabled")

    def test_nothing_is_sent_when_none_is_disabled(self):
        self.rules[0] = rule("fixture_switch_poll")
        self.assertEqual(self.run_step(), 0)
        self.system.native_post.assert_not_called()

    def test_a_rule_gone_since_the_listing_is_skipped(self):
        self.system.native_post.side_effect = lambda *args: NativeProbe(404)
        self.assertEqual(self.step.enable(self.system), 0)

    def test_a_refused_switch_fails_the_step_and_says_why(self):
        self.system.native_post.side_effect = lambda *args: NativeProbe(403)
        self.assertEqual(self.step.enable(self.system), 1)
        self.system.say.assert_called_with("   switching fixture_switch_poll on was refused (HTTP 403)")

    def test_a_listing_not_ready_yet_is_asked_again_within_the_timeout(self):
        answers = iter([NativeProbe(503), NativeProbe(0), NativeProbe(200, self.rules)])
        self.system.native_probe.side_effect = lambda *args, **kwargs: next(answers)
        self.assertEqual(self.step.check(self.system).detail, "1 disabled in the runtime: fixture_switch_poll")
        self.assertEqual(self.now[0], 4)

    def test_a_listing_that_never_answers_is_a_problem_by_the_timeout(self):
        self.system.native_probe.side_effect = lambda *args, **kwargs: NativeProbe(503)
        found = self.step.check(self.system)
        self.assertEqual(found.problem, f"the runtime's rules could not be read ({sandbox.RULES_PATH}: HTTP 503)")
        self.assertEqual(self.now[0], self.step.timeout)
        self.system.native_probe.side_effect = lambda *args, **kwargs: NativeProbe(401)
        self.now[0] = 0
        self.assertIn("HTTP 401", self.step.check(self.system).problem)
        self.assertEqual(self.now[0], 0, "a refusal is not asked again")

    def test_a_malformed_listing_or_a_missing_token_is_a_problem(self):
        self.system.native_probe.side_effect = lambda *args, **kwargs: NativeProbe(200, [{"uid": "fixture"}])
        self.assertEqual(self.step.check(self.system).problem, f"{sandbox.RULES_PATH} returned malformed rule data")
        self.system.read_private.side_effect = FileNotFoundError
        self.assertIn("API token", self.step.check(self.system).problem)

    def test_every_install_switches_them_on_just_before_its_health_gate(self):
        verified = [type(step) for step in changeover.verification(context())]
        self.assertLess(verified.index(changeover.Authorize), verified.index(steps.RuleSwitches))
        self.assertLess(verified.index(steps.RuleSwitches), verified.index(steps.Health))
        self.assertFalse(steps.RuleSwitches.always, "it reads back what it switched")


class NativePost(unittest.TestCase):
    """The system's one write to the runtime: an authenticated text/plain POST."""

    def serve(self, status):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"),
                             self.rfile.read(length)))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    def test_it_posts_the_body_with_the_token_and_keeps_the_status(self):
        system = System()
        for status in (200, 404):
            with self.subTest(status=status):
                url, seen = self.serve(status)
                found = system.native_post(url, TOKEN, "/rest/rules/fixture/enable", "true", system.clock() + 10)
                self.assertEqual(found, NativeProbe(status))
                self.assertEqual(seen, [("/rest/rules/fixture/enable", f"Bearer {TOKEN}", "text/plain", b"true")])

    def test_no_connection_a_spent_deadline_or_a_bad_token_sends_nothing(self):
        system = System()
        url, seen = self.serve(200)
        self.assertEqual(system.native_post(url, "bad token", "/rest/rules/fixture/enable", "true",
                                            system.clock() + 10).problem, "invalid runtime credential")
        self.assertEqual(system.native_post(url, TOKEN, "/rest/rules/fixture/enable", "true",
                                            system.clock() - 1).problem, "deadline expired")
        self.assertEqual(seen, [])
        with contextlib.closing(socket.socket()) as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        self.assertEqual(system.native_post(f"http://127.0.0.1:{port}", TOKEN, "/rest/rules/fixture/enable", "true",
                                            system.clock() + 10), NativeProbe(0))


if __name__ == "__main__":
    unittest.main()
