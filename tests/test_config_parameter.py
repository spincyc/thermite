"""Config selection happens before module imports and preserves service options."""
import os
from pathlib import Path
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

    def test_service_config_files_survive_bootstrap_with_an_explicit_site(self):
        for action in ("bootstrap", "serve", "worker", "verify", "callbacks"):
            for flag in (["--config", "service.json"], ["--config=service.json"]):
                args = ["-v", "access", action, *flag]
                self.assertEqual(oh_paths.select_args(["--site", "house", *args]), args)
                self.assertEqual(oh_paths.SITE, Path("house").absolute())

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
