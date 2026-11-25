"""Config selection happens before module imports, and means the site in every verb."""
import argparse
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_paths


class ConfigParameter(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.selected = patch.object(oh_paths, "SITE", oh_paths.PROGRAM)
        self.selected.start()
        self.addCleanup(self.selected.stop)
        self.explicit = patch.object(oh_paths, "CONFIG_EXPLICIT", None)
        self.explicit.start()
        self.addCleanup(self.explicit.stop)

    def test_config_before_or_after_verb_selects_the_same_directory(self):
        for args in (["--config", "config with spaces", "install", "--dry-run"],
                     ["install", "--config=config with spaces", "--dry-run"],
                     ["install", "--dry-run", "--config", "config with spaces"]):
            with self.subTest(args=args):
                self.assertEqual(oh_paths.select_args(args), ["install", "--dry-run"])
                self.assertEqual(oh_paths.SITE, Path("config with spaces").absolute())

    def test_the_retired_spelling_an_older_release_still_sends_selects_the_site(self):
        # A downgrade that rolled back: the older release's changeover.health
        # asks the owner command, still the newer release, with --site.
        import oh_cli
        health = ["health", "--production", "--installation", "--settle", "120", "--revision", "0" * 40]
        for args in (["--site", "config with spaces", *health], [health[0], "--site", "config with spaces", *health[1:]],
                     ["--site=config with spaces", *health]):
            with self.subTest(args=args):
                os.environ.pop("THERMITE_SITE", None)
                self.assertEqual(oh_paths.select_args(args), health)
                self.assertEqual(oh_paths.SITE, Path("config with spaces").absolute())
        asked = oh_cli.parser_of("health").parse_args(health[1:])
        self.assertTrue(asked.production and asked.installation)

    def test_duplicate_or_missing_config_is_rejected_without_selecting_a_site(self):
        for args in (["install", "--config"], ["install", "--config", "--dry-run"],
                     ["install", "--config="], ["--config", "first", "install", "--config=second"],
                     ["--config", "first", "install", "--site", "second"], ["--site", "first", "--site", "second"]):
            with self.subTest(args=args), self.assertRaises(oh_paths.SiteError) as refused:
                oh_paths.select_args(args)
            self.assertNotIn("THERMITE_SITE", os.environ)
        self.assertEqual(str(refused.exception), "pass --config exactly once")

    def test_a_selector_is_the_site_wherever_it_stands_and_whatever_the_verb(self):
        # The scanner once kept --config for some access actions, found by
        # a table of them and only behind switches it listed: -qv was not.
        for leading in ([], ["-v"], ["-qv"], ["--no-color", "-q"]):
            args = [*leading, "access", "audit", "--descriptor", "service.json", "--since", "7d"]
            for selector in (["--config", "house"], ["--config=house"], ["--site", "house"]):
                for place in range(len(args) + 1):
                    # Not between a switch and its value.
                    if place and args[place - 1] in ("--descriptor", "--since"):
                        continue
                    given = [*args[:place], *selector, *args[place:]]
                    with self.subTest(given=given):
                        os.environ.pop("THERMITE_SITE", None)
                        self.assertEqual(oh_paths.select_args(given), args)
                        self.assertEqual(oh_paths.SITE, Path("house").absolute())

    def test_other_switches_pass_through_and_select_no_site(self):
        for args in (["access", "serve", "--descriptor", "service.json"],
                     ["-qv", "access", "callbacks", "--descriptor=callbacks.json"],
                     ["sandbox", "start", "--conf", "conf"], ["status", "--configuration", "x", "--sites", "y"]):
            with self.subTest(args=args):
                self.assertEqual(oh_paths.select_args(args), args)
                self.assertNotIn("THERMITE_SITE", os.environ)

    def test_option_separator_leaves_remaining_arguments_untouched(self):
        args = ["install", "--", "--config", "not-a-selector"]
        self.assertEqual(oh_paths.select_args(args), args)
        self.assertNotIn("THERMITE_SITE", os.environ)

    def test_check_runs_every_step_whatever_one_found_and_never_generates(self):
        from oh_cli import check, generate, lint, validate
        house = {"devices": 0, "items": 0, "notes": [], "problems": [], "status": "ok", "things": 0}
        for codes, expected in (((0, 0, 0), 0), ((1, 0, 0), 1), ((0, 1, 0), 1), ((0, 2, 0), 2), ((0, 0, 1), 1)):
            with self.subTest(codes=codes), patch.object(generate, "main") as generated, \
                    patch.object(validate, "checked", return_value={**house, "status": ("ok", "problems")[codes[0]]}) \
                    as validated, \
                    patch.object(check, "built", return_value=({"conf": 0, "metrics": 0, "units": 0}, codes[1])) as built, \
                    patch.object(lint, "checked", return_value={"secret_scan": {"files": 0, "findings": ["x"] * codes[2]},
                                                                "doc_targets": None}) as scanned, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(check.main([]), expected)
                self.assertEqual((validated.call_count, built.call_count, scanned.call_count), (1, 1, 1))
                # The verb that writes conf/, metrics/ and deploy/ is not run, with --prune or without.
                generated.assert_not_called()


def parsers(parser, path):
    """(path, parser) for a verb's parser and every action's below it."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from parsers(child, (*path, name))


class SelectorSpelling(unittest.TestCase):
    def test_no_verb_or_action_spells_a_switch_of_its_own_like_the_site_selector(self):
        # select_args takes --config and --site out of every command line
        # before any parser sees it, so a parser that declared either for
        # something else could never be given it. Each verb's parser shows
        # --config in its help (oh_cli.command); nothing below it may, and
        # none declares --site, the retired spelling.
        import oh_cli
        selectors, walked = set(oh_paths.SELECTORS), 0
        for verb in oh_cli.VERBS:
            for path, parser in parsers(oh_cli.parser_of(verb), (verb,)):
                walked += 1
                declared = [action.option_strings for action in parser._actions
                            if selectors & set(action.option_strings)]
                with self.subTest(command=" ".join(path)):
                    self.assertEqual(declared, [["--config"]] if len(path) == 1 else [],
                                     f"thermite {' '.join(path)} declares a site selector for itself: the launcher "
                                     "would take it as the site; give the switch another name")
        self.assertGreater(walked, len(oh_cli.VERBS), "the walk reached no action's parser")

    def test_no_help_shows_the_retired_spelling(self):
        import oh_cli
        retired = re.compile(r"--site(?![\w-])")
        self.assertIn("--config DIR", oh_cli.overview())
        self.assertIsNone(retired.search(oh_cli.overview()))
        for verb in oh_cli.VERBS:
            for path, parser in parsers(oh_cli.parser_of(verb), (verb,)):
                with self.subTest(command=" ".join(path)):
                    self.assertIsNone(retired.search(parser.format_help()))

    def test_no_parser_takes_part_of_a_switch_for_the_whole(self):
        # Each verb's parser declares the selector for its help and is never
        # given it. With argparse's abbreviations on, --confi DIR passed the
        # launcher, and the parser took it for --config and selected nothing.
        import oh_cli
        for verb in oh_cli.VERBS:
            for path, parser in parsers(oh_cli.parser_of(verb), (verb,)):
                with self.subTest(command=" ".join(path)):
                    self.assertFalse(parser.allow_abbrev, f"thermite {' '.join(path)} accepts abbreviated switches")

    def test_an_abbreviated_selector_is_refused_through_the_launcher(self):
        for argv, refused in ((["lint", "--confi", "house"], "--confi house"),
                              (["docs", "check", "--conf=house"], "--conf=house")):
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, str(AREA / "bin/thermite"), *argv], capture_output=True,
                                        text=True, timeout=120, env={name: value for name, value in os.environ.items()
                                                                     if name != "THERMITE_SITE"})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("unrecognized arguments: " + refused, result.stderr)
