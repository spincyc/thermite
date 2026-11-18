"""Config selection happens before module imports, and means the site in every verb."""
import argparse
import os
from pathlib import Path
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

    def test_config_before_or_after_verb_and_site_alias_select_the_same_directory(self):
        for args in (["--config", "config with spaces", "install", "--dry-run"],
                     ["install", "--config=config with spaces", "--dry-run"],
                     ["install", "--site", "config with spaces", "--dry-run"]):
            with self.subTest(args=args):
                self.assertEqual(oh_paths.select_args(args), ["install", "--dry-run"])
                self.assertEqual(oh_paths.SITE, Path("config with spaces").absolute())

    def test_duplicate_or_missing_config_is_rejected_without_selecting_a_site(self):
        for args in (["install", "--config"], ["install", "--config", "--dry-run"],
                     ["install", "--config="], ["--config", "first", "install", "--site", "second"]):
            with self.subTest(args=args), self.assertRaises(oh_paths.SiteError):
                oh_paths.select_args(args)
            self.assertNotIn("THERMITE_SITE", os.environ)

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

    def test_check_stops_after_a_failed_validation(self):
        from oh_cli import check, generate, validate, lint
        with patch.object(generate, "main", return_value=0) as generated, \
                patch.object(validate, "main", return_value=1) as validated, \
                patch.object(lint, "main", return_value=0) as scanned:
            self.assertEqual(check.main([]), 1)
        # An upgrade's check removes what an earlier version generated.
        generated.assert_called_once_with(["--prune"])
        validated.assert_called_once_with([])
        scanned.assert_not_called()


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
        # the selector in its help (oh_cli.command); nothing below it may.
        import oh_cli
        selectors, walked = set(oh_paths.SELECTORS), 0
        for verb in oh_cli.VERBS:
            for path, parser in parsers(oh_cli.parser_of(verb), (verb,)):
                walked += 1
                declared = [action.option_strings for action in parser._actions
                            if selectors & set(action.option_strings)]
                with self.subTest(command=" ".join(path)):
                    self.assertEqual(declared, [list(oh_paths.SELECTORS)] if len(path) == 1 else [],
                                     f"thermite {' '.join(path)} declares a site selector for itself: the launcher "
                                     "would take it as the site; give the switch another name")
        self.assertGreater(walked, len(oh_cli.VERBS), "the walk reached no action's parser")

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
