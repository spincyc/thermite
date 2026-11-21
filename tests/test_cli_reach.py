"""Where a verb runs: --url ssh://... on a server, an https gateway, or here.

No ssh runs: subprocess.call and oh_cli.over_ssh are replaced, and every
host is fictional (thermite.example, 192.0.2.x).
"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_cli  # noqa: E402


def quietly(function, *args):
    """(function's result, what it printed on stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = function(*args)
    return result, err.getvalue()


class SshTarget(unittest.TestCase):
    def test_a_server_its_login_port_and_site(self):
        self.assertEqual(oh_cli.ssh_target("ssh://thermite.example"), (["thermite.example"], None))
        self.assertEqual(oh_cli.ssh_target("ssh://owner@thermite.example:2222/srv/site"),
                         (["-p", "2222", "owner@thermite.example"], "/srv/site"))
        self.assertEqual(oh_cli.ssh_target("ssh://192.0.2.50/~/thermite-site"), (["192.0.2.50"], "thermite-site"))
        self.assertEqual(oh_cli.ssh_target("ssh://thermite.example/"), (["thermite.example"], None))

    def test_what_is_no_server_or_could_pass_ssh_a_switch_is_refused(self):
        for url in ("https://thermite.example", "ssh://", "ssh://-oProxyCommand=x", "ssh://-x@thermite.example",
                    "ssh://owner:secret@thermite.example", "ssh://thermite.example?x=1", "ssh://thermite.example#x",
                    "ssh://thermite.example:port"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                oh_cli.ssh_target(url)


class OverSsh(unittest.TestCase):
    def run_it(self, *args, status=0, **kwargs):
        with mock.patch.object(oh_cli.subprocess, "call", return_value=status) as call:
            result, err = quietly(lambda: oh_cli.over_ssh(*args, **kwargs))
        return result, err, call

    def test_it_runs_the_verb_there_with_the_owner_command_on_path(self):
        status, _, call = self.run_it("status", ["--json"], "ssh://owner@thermite.example:2222/srv/site", status=1)
        self.assertEqual(status, 1)
        argv = call.call_args.args[0]
        self.assertEqual(argv[:6], ["ssh", "-T", "-p", "2222", "--", "owner@thermite.example"])
        self.assertIn('PATH="$HOME/.local/bin:$PATH"', argv[6])
        self.assertIn(f"unset {oh_cli.URL_VARIABLE};", argv[6])
        self.assertTrue(argv[6].endswith("thermite status --config /srv/site --json"), argv[6])

    def test_the_words_reach_the_server_quoted(self):
        _, _, call = self.run_it("logs", ["--grep", "a b; rm -rf ~"], "ssh://thermite.example")
        self.assertIn("--grep 'a b; rm -rf ~'", call.call_args.args[0][-1])

    def test_ssh_failing_is_exit_2_and_said(self):
        status, err, _ = self.run_it("status", [], "ssh://thermite.example", status=255)
        self.assertEqual(status, 2)
        self.assertIn("ssh could not run it on thermite.example", err)

    def test_no_ssh_is_exit_2(self):
        with mock.patch.object(oh_cli.subprocess, "call", side_effect=FileNotFoundError):
            status, err = quietly(lambda: oh_cli.over_ssh("status", [], "ssh://thermite.example"))
        self.assertEqual(status, 2)
        self.assertIn("needs ssh", err)

    def test_a_given_standard_output_gets_no_terminal(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sys.stdout, "isatty", return_value=True):
            _, _, call = self.run_it("bugreport", ["--out", "-"], "ssh://thermite.example", stdout=io.BytesIO())
        self.assertEqual(call.call_args.args[0][1], "-T")


class Reached(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(oh_cli.URL_VARIABLE, None)
        self.over = mock.patch.object(oh_cli, "over_ssh", return_value=0)
        self.ssh = self.over.start()
        self.addCleanup(self.over.stop)

    def test_an_ssh_url_runs_a_running_installation_verb_there(self):
        for words in (["--url", "ssh://thermite.example", "--json"], ["--json", "--url=ssh://thermite.example"]):
            with self.subTest(words=words):
                self.assertEqual(oh_cli.reached("status", words), (0, words))
                self.ssh.assert_called_with("status", ["--json"], "ssh://thermite.example")

    def test_thermite_url_names_the_server_unless_a_target_switch_names_one(self):
        os.environ[oh_cli.URL_VARIABLE] = "ssh://thermite.example"
        self.assertEqual(oh_cli.reached("history", ["--last", "3"])[0], 0)
        self.ssh.assert_called_once_with("history", ["--last", "3"], "ssh://thermite.example")
        self.ssh.reset_mock()
        for words in (["--production"], ["--sandbox"], ["--runtime-url", "http://127.0.0.1:13616"],
                      ["--runtime-url=http://127.0.0.1:13616"]):
            with self.subTest(words=words):
                self.assertEqual(oh_cli.reached("status", words), (None, words))
        self.assertEqual(oh_cli.reached("generate", []), (None, []))
        self.ssh.assert_not_called()

    def test_an_https_thermite_url_leaves_a_running_installation_verb_here(self):
        os.environ[oh_cli.URL_VARIABLE] = "https://thermite.example:13601"
        self.assertEqual(oh_cli.reached("status", ["--json"]), (None, ["--json"]))
        self.ssh.assert_not_called()

    def test_help_is_this_thermite_s(self):
        self.assertEqual(oh_cli.reached("status", ["--url", "ssh://thermite.example", "-h"])[0], None)
        self.ssh.assert_not_called()

    def test_a_url_a_verb_cannot_use_is_refused_naming_what_it_takes(self):
        for verb, url, said in (("generate", "ssh://thermite.example", "takes no --url"),
                                ("status", "https://thermite.example", "ssh://[USER@]HOST"),
                                ("tui", "ssh://thermite.example", "https://HOST")):
            with self.subTest(verb=verb):
                (status, _), err = quietly(oh_cli.reached, verb, ["--url", url])
                self.assertEqual(status, 2)
                self.assertIn(said, err)
        self.ssh.assert_not_called()

    def test_a_gateway_verb_gets_its_url_itself(self):
        words = ["--url", "https://thermite.example:13601"]
        self.assertEqual(oh_cli.reached("tui", words), (None, words))

    def test_the_old_runtime_url_of_cameras_becomes_runtime_url_where_it_stood(self):
        (status, words), err = quietly(oh_cli.reached, "cameras", ["list", "--url", "http://127.0.0.1:13616", "--json"])
        self.assertEqual((status, words), (None, ["list", "--runtime-url", "http://127.0.0.1:13616", "--json"]))
        self.assertIn("--runtime-url", err)
        (_, words), _ = quietly(oh_cli.reached, "cameras", ["list", "--url=http://127.0.0.1:13616"])
        self.assertEqual(words, ["list", "--runtime-url=http://127.0.0.1:13616"])

    def test_a_retired_verb_keeps_its_own_url_and_takes_only_ssh_there(self):
        words = ["--url", "http://127.0.0.1:13616", "--json"]
        self.assertEqual(oh_cli.reached("health", words), (None, words))
        self.assertEqual(oh_cli.reached("health", ["--url", "ssh://thermite.example", "--production"])[0], 0)
        self.ssh.assert_called_once_with("health", ["--production"], "ssh://thermite.example")

    def test_bugreport_brings_its_archive_back(self):
        from oh_cli import bugreport
        with mock.patch.object(bugreport, "fetch", return_value=0) as fetch:
            self.assertEqual(oh_cli.reached("bugreport", ["--url", "ssh://thermite.example", "--since", "2h"])[0], 0)
        fetch.assert_called_once_with(["--since", "2h"], "ssh://thermite.example")


class NamedServer(unittest.TestCase):
    def named(self, value):
        with mock.patch.dict(os.environ, {oh_cli.URL_VARIABLE: value}):
            return oh_cli.named_server()

    def test_the_gateway_thermite_url_names(self):
        self.assertEqual(self.named("ssh://owner@thermite.example:2222/srv/site"), "thermite.example")
        self.assertEqual(self.named("https://thermite.example:13601"), "https://thermite.example:13601")
        self.assertIsNone(self.named(""))
        self.assertIsNone(self.named("ssh://-oProxyCommand=x"))


class Retired(unittest.TestCase):
    def test_retired_verbs_are_listed_nowhere_and_each_still_has_its_module(self):
        self.assertFalse(set(oh_cli.RETIRED) & set(oh_cli.VERBS))
        overview = oh_cli.overview()
        for verb, spec in oh_cli.RETIRED.items():
            with self.subTest(verb=verb):
                self.assertNotIn(f"\n  {verb} ", overview)
                self.assertTrue(spec.instead.split()[1] in oh_cli.VERBS)
                self.assertTrue(callable(__import__(oh_cli.module_name(verb), fromlist=["main"]).main))

    def test_a_retired_verb_says_what_replaces_it_then_runs_as_before(self):
        from oh_cli import validate
        with mock.patch.object(validate, "main", return_value=1) as run:
            status, err = quietly(oh_cli.main, ["validate", "--json"])
        self.assertEqual(status, 1)
        run.assert_called_once_with(["--json"])
        self.assertIn("thermite check house replaces it", err)


class Renamed(unittest.TestCase):
    def parser(self):
        parser = oh_cli.command("history", "x", ())
        oh_cli.add_state_dir(parser)
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--status", action="store_true")
        oh_cli.add_renamed(parser, "--check", "--status")
        return parser

    def test_the_old_spelling_warns_and_does_what_the_new_one_does(self):
        parser = self.parser()
        (old, err) = quietly(parser.parse_args, ["--state", "/tmp/x", "--check"])
        self.assertEqual((old.state_dir, old.status), (Path("/tmp/x"), True))
        self.assertIn("--state is now --state-dir", err)
        self.assertIn("--check is now --status", err)
        new, err = quietly(parser.parse_args, ["--state-dir", "/tmp/x"])
        self.assertEqual((new.state_dir, new.status, err), (Path("/tmp/x"), False, ""))

    def test_the_old_spelling_is_not_in_the_help(self):
        shown = self.parser().format_help()
        self.assertIn("--state-dir", shown)
        self.assertNotRegex(shown, r"--state(?![-\w])|--check")


class BrokenPipe(unittest.TestCase):
    def test_a_reader_that_stops_early_is_no_error(self):
        with mock.patch.object(oh_cli, "main", side_effect=BrokenPipeError), \
                mock.patch.object(oh_cli.os, "dup2"):
            self.assertEqual(oh_cli.run("thermite", ["logs"]), 0)
        oh_cli._whole_process = False


if __name__ == "__main__":
    unittest.main()
