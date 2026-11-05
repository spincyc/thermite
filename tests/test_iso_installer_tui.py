"""The installer's terminal against the real core on a fake live system (tests/iso_installer_tui_harness.js)."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def serve(root: Path, scenario: str) -> None:
    """One core session on stdin/stdout, as iso/installer.py serve runs on the medium."""
    sys.path.insert(0, str(AREA))
    from tests.test_iso_installer import FakeHost, fixture, installer
    live, media = root / "live", root / "media"
    if not live.exists():
        fixture(root)
    host = FakeHost(live, taken=("thermite.local",), code=1 if scenario == "fails" else 0)
    session = installer.Session(host, media, root / f"state-{scenario}", root / f"target-{scenario}")
    try:
        installer.serve(session, sys.stdin, sys.stdout)
    finally:
        session.close()


@unittest.skipUnless(NODE, "needs node")
class InstallerTerminal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = AREA / ".scratch/iso-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tui-", dir=scratch) as root:
            sys.path.insert(0, str(AREA))
            from tests.test_iso_installer import fixture
            fixture(Path(root))
            result = subprocess.run([NODE, str(AREA / "tests/iso_installer_tui_harness.js"), sys.executable,
                                     __file__, root], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        cls.proof = json.loads(result.stdout)

    def check(self, *keys):
        for key in keys:
            self.assertTrue(self.proof[key], key)

    def test_welcome_reads_the_machine_and_claims_the_installer(self):
        self.check("welcome", "claimed")

    def test_network_lists_interfaces_and_joins_wifi_with_its_passphrase(self):
        self.check("network", "wifiListed", "wifiAsks", "wifiJoined")

    def test_hidden_and_open_wifi_join_and_the_review_checks_the_joined_network(self):
        self.check("spacedName", "hiddenAsksName", "hiddenJoined", "openJoined", "openContinues", "reviewShowsWifi",
                   "reviewChecksWifi", "wifiChangeable", "openInstalled")

    def test_a_taken_host_name_is_replaced_by_a_free_one_or_kept_on_a_second_enter(self):
        self.check("hostnameSuggested", "hostnameSaysWhy", "hostnameRefused", "takenWarns", "takenKept")

    def test_the_cores_refusals_show_under_their_fields_and_secrets_stay_masked(self):
        self.check("ownerRefused", "passwordsDiffer", "secretsMasked", "ownerAccepted", "rootMustDiffer",
                   "rootAccepted")

    def test_lists_search_and_choose_and_only_eligible_disks_are_offered(self):
        self.check("searchFilters", "zoneChosen", "regionAccepted", "diskReasons", "onlyEligible", "systemChoices",
                   "systemAccepted")

    def test_review_needs_the_disks_name_before_installing(self):
        self.check("reviewSummary", "confirmRefused")

    def test_progress_runs_to_done_and_cannot_be_quit(self):
        self.check("quitRefusedWhileInstalling", "progress", "installDrawn", "done", "doneStays", "reboots", "quits")

    def test_a_failed_installation_says_why_and_returns_to_review(self):
        self.check("failureShown", "failureReturnsToReview")

    def test_console_and_ssh_share_one_installer(self):
        self.check("lockShared")

    def test_every_frame_fits_uses_the_themes_styles_and_shows_no_secret(self):
        self.assertGreater(self.proof["frames"], 100)
        self.assertEqual(self.proof["overflow"], [])
        self.assertEqual(sorted(set(self.proof["unknownStyles"])), [])
        self.assertEqual(self.proof["leaked"], [])


if __name__ == "__main__" and sys.argv[1:2] == ["core"]:
    serve(Path(sys.argv[2]), sys.argv[3])
