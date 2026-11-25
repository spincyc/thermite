"""The house's alerts as Home shows them (web/remote/alerts.js).

One model reads the gateway's feed (GET /api/v1/alerts, oh_identity.alerts)
and says what to draw. tests/home_alerts_harness.js runs it beside a fake feed
and a fake clock; what Home draws from it is tests/test_listen_page.py's.
The invariant: an empty list is never good news by itself. Only an answer
that says monitoring is evaluating its rules, counts nothing firing and
lists nothing may be shown as "Nothing is firing".
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_listen_session import house, model
from oh_metrics import alerts as catalogue

AREA = Path(__file__).resolve().parents[1]
REMOTE = AREA / "web/remote"
NODE = shutil.which("node")
ADMIN = "An Admin sees those about the house as a whole."


@unittest.skipUnless(NODE, "needs node")
class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A capped heap and a deadline: every wait in the harness is a finite tick of a fake clock.
        run = subprocess.run([NODE, "--max-old-space-size=256", str(AREA / "tests/home_alerts_harness.js")],
                             input=json.dumps(model(house())), capture_output=True, text=True, timeout=60)
        if run.returncode or not run.stdout:
            raise AssertionError(run.stderr[-4000:] or "the harness never finished")
        cls.out = json.loads(run.stdout)

    def test_nothing_is_claimed_before_the_house_is_read(self):
        self.assertEqual(self.out["unread"], {"state": "reading", "tone": "", "summary": "", "lines": [], "more": 0,
                                              "moreText": "", "marks": {}})

    def test_nothing_is_firing_is_said_only_of_an_evaluating_monitoring_with_nothing_firing(self):
        clear = self.out["clear"]
        self.assertEqual((clear["state"], clear["tone"], clear["summary"]), ("clear", "ok", "Nothing is firing."))
        # Every monitoring state, count, list and version the feed could give or get wrong, and a refusal:
        # the harness fails on any but the one healthy answer that is shown as clear.
        self.assertEqual(self.out["matrix"], {"cases": 284, "clear": 1})

    def test_an_alert_says_what_and_where_by_labels_in_the_feeds_order(self):
        self.assertEqual(self.out["lines"], [
            ["critical", "Control path", "Example West · Example Tv · Not responding to control", "Control path text.", "3 h"],
            ["critical", "Lock", "Example West", "Lock text.", "10 min"],
            # Ids this page's house does not have are left out, never shown.
            ["warning", "Unknown ids", "Control port closed", "Unknown ids text.", "under a minute"],
            ["warning", "Disk", "node", "Disk text.", "5 d"],
            # A severity this page has no word for is a warning, not a refusal of the whole answer.
            ["warning", "A severity of later", "", "A severity of later text.", ""]])
        self.assertEqual(self.out["firing"], ["firing", "critical", "2 critical alerts and 3 warnings", 0, "",
                                              {"example_west": "critical"}])

    def test_a_rooms_mark_is_the_worst_of_its_alerts(self):
        worst, mild, tone, summary = self.out["marks"]
        self.assertEqual((worst, mild), ({"example_west": "critical"}, {"example_west": "warning"}))
        self.assertEqual((tone, summary), ("warning", "1 warning"))

    def test_alerts_the_viewer_is_not_given_are_counted_not_hidden(self):
        some, none, one, untitled = self.out["more"]
        self.assertEqual(some, ["firing", "critical", "1 critical alert", 1, 2, "and 2 more that are not shown here. " + ADMIN])
        self.assertEqual(none, ["firing", "warning", "2 alerts firing", 0, 2, "They are not shown here. " + ADMIN])
        self.assertEqual(one, ["firing", "warning", "1 alert firing", 0, 1, "It is not shown here. " + ADMIN])
        # An alert the page cannot show is one of them, whatever the count says.
        self.assertEqual(untitled, one)

    def test_monitoring_that_cannot_be_read_says_so_and_never_that_nothing_fires(self):
        left, silent, unreachable, refused = self.out["unreadable"]
        self.assertEqual(left, ["stalled", "critical", "Monitoring is not evaluating its rules. It last left:", 1, 1, 1])
        self.assertEqual(silent, ["stalled", "warning", "Monitoring is not evaluating its rules, so alerts cannot be read.", 0, 0, 0])
        # What an unreachable answer lists all the same is not shown.
        self.assertEqual(unreachable, ["unreachable", "warning", "Monitoring does not answer, so alerts cannot be read.", 0, 0, 0])
        self.assertEqual(refused, ["unanswered", "warning", "thermite does not answer, so alerts cannot be read.", 0, 0, 0])

    def test_one_doubtful_reading_is_held_back_and_the_second_is_shown(self):
        self.assertEqual(self.out["doubt"], ["clear", "clear", "unreachable", "stalled", "clear", "clear", "clear"])
        # A page just opened claims nothing while its first doubtful reading is confirmed.
        self.assertEqual(self.out["opened"], ["reading", 0, "stalled", 1])
        self.assertEqual(self.out["preview"], "unreachable")

    def test_it_reads_every_half_minute_while_shown_and_at_once_when_shown_again(self):
        polling = self.out["polling"]
        self.assertEqual((polling["polled"], polling["hidden"], polling["stopped"]), (5, 0, 0))
        # Shown again after longer than a reading speaks for: no claim until the house is read anew.
        self.assertEqual(polling["recent"], "clear")
        self.assertEqual(polling["away"], ["reading", 1, True])
        # One read at a time, and an answer nobody waits for any more does not hold up the next.
        self.assertEqual((polling["hung"], polling["back"]), (1, "clear"))
        self.assertEqual(polling["stepped"], ["clear", "reading"])

    def test_how_long_an_alert_has_fired_is_said_in_its_largest_unit(self):
        self.assertEqual(self.out["ago"], ["under a minute", "1 min", "59 min", "1 h", "47 h", "2 d", "", ""])

    def test_the_preview_can_show_each_state(self):
        samples = self.out["samples"]
        self.assertEqual({name: shown[0] for name, shown in samples.items()},
                         {"": "clear", "firing": "firing", "admin": "firing", "hidden": "firing", "house": "firing",
                          "stalled": "stalled", "silent": "stalled", "unreachable": "unreachable", "unanswered": "unanswered"})
        self.assertEqual(samples["admin"][3], ["Example West · Example Tv · Not responding to control",
                                               "Example West · Source not reported", "node"])
        self.assertEqual((samples["hidden"][4], samples["house"][4]), (2, 2))

    def test_the_previews_alerts_say_the_catalogues_words(self):
        shown = self.out["catalogue"]
        self.assertEqual(sorted(shown), ["ThermiteCheckFailing", "ThermiteControlUnknown", "ThermiteHostDiskLow"])
        for name, (severity, title, text, check, check_text, service) in shown.items():
            with self.subTest(alert=name):
                self.assertEqual((title, text), catalogue.CATALOG[name])
                self.assertEqual(severity, catalogue.severity(name))
                if check:
                    self.assertEqual(check_text, catalogue.CHECK_WORDS[check])
                    self.assertIn("check", catalogue.ABOUT[name])
                if service:
                    self.assertIn(service, {*catalogue.CORE_JOBS, *catalogue.SERVICES})
                    self.assertNotIn(name, catalogue.ABOUT)


class Metadata(unittest.TestCase):
    def test_house_wide_alerts_in_identity_about_expose_validated_service_labels(self):
        try:
            import oh_units
            from oh_identity import alerts as feed
        except ImportError as exc:  # httpx is a site requirement
            self.skipTest(str(exc))
        saved = set(feed._logged)
        self.addCleanup(lambda: (feed._logged.clear(), feed._logged.update(saved)))
        ids = {"room": frozenset({"example_west"}), "zone": frozenset({"example_west"}),
               "device": frozenset({"example_matrix"}), "check": frozenset(catalogue.CHECK_WORDS)}
        expected = {
            "ThermiteServiceFailed": ({"name": oh_units.RUNTIME}, "critical", "service", oh_units.RUNTIME),
            "ThermiteServiceDown": ({"name": oh_units.ACCESS}, "critical", "service", oh_units.ACCESS),
            "ThermiteServiceFlapping": ({"name": oh_units.ATV}, "warning", "service", oh_units.ATV),
            "ThermiteScrapeFailed": ({"job": "openhab"}, "warning", "service", "openhab"),
            "ThermiteRoutingQuarantined": ({}, "critical", "service", "routing"),
            "ThermiteGatewayCertExpiring": ({"service": "gateway"}, "warning", "service", "gateway"),
        }
        for name, (labels, severity, field, value) in expected.items():
            with self.subTest(alert=name):
                self.assertIn(name, feed.ABOUT)
                self.assertIn(name, catalogue.CATALOG)
                entry = feed.record(name, labels, 1000, ids)
                self.assertEqual(entry["severity"], severity)
                self.assertEqual(entry[field], value)
                self.assertEqual((entry["title"], entry["text"]), catalogue.CATALOG[name])
        # Unrecognized service/unit labels are dropped rather than passed through.
        feed._logged.discard(("ThermiteServiceFailed", "name"))
        with self.assertLogs("oh_identity.alerts", "WARNING"):
            bogus = feed.record("ThermiteServiceFailed", {"name": "evil.service"}, 1000, ids)
        self.assertNotIn("service", bogus)


class Page(unittest.TestCase):
    def test_home_has_the_panel_above_its_rooms_and_loads_the_model_before_its_script(self):
        page = (REMOTE / "home.html").read_text()
        self.assertLess(page.index("data-home-alerts "), page.index('class="home__layout"'))
        self.assertLess(page.index('src="alerts.js"'), page.index('src="home.js"'))
        # Hidden until there is a feed to read: a page openHAB serves by itself has none.
        self.assertRegex(page, r'<section class="home__panel home__alerts"[^>]* data-home-alerts hidden>')
        self.assertIn('<p data-home-alerts-summary role="status">', page)

    def test_an_alert_is_a_target_a_thumb_can_open(self):
        sheet = re.sub(r"/\*.*?\*/", "", (REMOTE / "home.css").read_text(), flags=re.S)
        summary = re.search(r"\.home \.home__alerts summary \{([^}]*)\}", sheet).group(1)
        self.assertIn("min-height: 44px", summary)

    def test_no_script_plays_a_sound_or_raises_a_toast_for_an_alert(self):
        for name in ("alerts.js", "home.js"):
            script = (REMOTE / name).read_text()
            self.assertEqual([word for word in ("Audio(", "Notification", "toast", "vibrate") if word in script], [], name)


if __name__ == "__main__":
    unittest.main()
