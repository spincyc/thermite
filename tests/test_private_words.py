"""Private publication filters remain site data across the application split."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_public
import oh_site


class PrivateWords(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch/split_input_review"))
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="private-words-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site, self.program = self.root / "selected-site", self.root / "application"
        self.site.mkdir()
        self.program.mkdir()
        self.enterContext(mock.patch.object(oh_site, "AREA", self.site))
        self.enterContext(mock.patch.object(oh_site, "SOURCE", self.program))

    def write(self, relative, content):
        path = self.site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def assert_blocked_by_both_guards(self, marker):
        payload = {"fixture.md": ("Synthetic disclosure: " + marker.upper()).encode()}
        self.assertTrue(oh_public.guard(payload, self.site))
        self.assertTrue(oh_site.guard(payload, set()))

    def test_instance_filter_blocks_public_source_and_docs(self):
        self.write("instance/private-words.txt", "# Synthetic fixture only\n\n Fixture-Instance-Marker \n")
        self.assertEqual(oh_site.private_words(), ["fixture-instance-marker"])
        self.assert_blocked_by_both_guards("fixture-instance-marker")

    def test_legacy_filter_still_blocks_public_source_and_docs(self):
        self.write("site/private-words.txt", "fixture-legacy-marker\n")
        self.assertEqual(oh_site.private_words(), ["fixture-legacy-marker"])
        self.assert_blocked_by_both_guards("fixture-legacy-marker")

    def test_both_locations_apply_and_packaged_filters_are_not_selected(self):
        self.write("instance/private-words.txt", "fixture-instance-marker\n")
        self.write("site/private-words.txt", "fixture-legacy-marker\n")
        (self.program / "private-words.txt").write_text("fixture-application-marker\n")
        self.assertEqual(oh_site.private_words(), ["fixture-instance-marker", "fixture-legacy-marker"])
        for marker in ("fixture-instance-marker", "fixture-legacy-marker"):
            self.assert_blocked_by_both_guards(marker)
        payload = {"fixture.md": b"fixture-application-marker"}
        self.assertEqual(oh_site.guard(payload, set()), [])
        self.assertEqual(oh_public.guard(payload, self.site), [])

    def test_explicit_file_preserves_override_and_missing_file_behavior(self):
        self.write("instance/private-words.txt", "fixture-instance-marker\n")
        chosen = self.write("custom-filter.txt", "# Comment\n Fixture-Explicit-Marker \n")
        self.assertEqual(oh_site.private_words(chosen), ["fixture-explicit-marker"])
        self.assertEqual(oh_site.private_words(self.root / "absent.txt"), [])

    def test_sites_without_optional_filters_still_pass(self):
        self.assertEqual(oh_site.private_words(), [])
        payload = {"fixture.md": b"Ordinary public application documentation."}
        self.assertEqual(oh_site.guard(payload, set()), [])
        self.assertEqual(oh_public.guard(payload, self.site), [])

    def test_build_host_name_is_not_a_fact_unless_listed(self):
        house = self.write("instance/devices.yaml", "version: 1\nrooms: {}\ndevices: []\n")
        payload = {"fixture.md": b"Run thermite on a machine called fixture-buildhost."}
        with mock.patch("socket.gethostname", return_value="fixture-buildhost.example"):
            self.assertNotIn("fixture-buildhost", oh_site.house_facts(house, self.site / "instance/host.yaml"))
            self.assertEqual(oh_site.guard(payload, oh_site.house_facts(house, self.site / "absent.yaml")), [])
            self.assertEqual(oh_public.guard(payload, self.site), [])
            self.write("instance/private-words.txt", "fixture-buildhost\n")
            self.assert_blocked_by_both_guards("fixture-buildhost")


if __name__ == "__main__":
    unittest.main()
