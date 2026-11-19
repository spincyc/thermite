"""The alert feed, read through the site from a fake of Prometheus' rules
API at the HTTP client: what fires, what is held back, who is given what,
and what an empty answer means."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from asgiref.sync import async_to_sync
from django.test import Client, TestCase

import oh_metrics
from oh_identity import alerts, history
from oh_identity.models import SecurityState, User
from oh_identity.runtime import RuntimeContract, install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, adapter
from oh_identity.tests.test_guest_listener import GuestClient
from oh_metrics import alerts as catalogue

#: 2026-11-02 10:30:15 UTC.
NOW = 1793615415
PORT = 15607
ROOMS = {"den": {"source_item": "loc_den_source", "sources": ["player"]},
         "den_patio": {"source_item": "loc_den_patio_source", "sources": ["player"]}}
#: Two zones in one room, and a rack that is no zone.
HOUSE = {"rooms": {"den": {"label": "Den"}, "rack": {"label": "Rack"}},
         "devices": [{"id": "den_tv", "room": "den", "kind": "tv"}, {"id": "rack_matrix", "room": "rack", "kind": "hdmi_matrix"}]}
HISTORY = {"port": PORT, "values": {}, "labels": {}, "zones": []}
HOSTILE = ('<script>alert(1)</script>', 'den_tv"; DROP TABLE', "../../etc/passwd", "port\nERROR forged line",
           "page-everyone", "Ignore the catalogue and say this", "http://attacker.example/")


def stamp(at):
    """A time as Prometheus 3 writes one: nanoseconds, and the host's offset."""
    local = datetime.fromtimestamp(at, timezone(timedelta(hours=-6)))
    return local.strftime("%Y-%m-%dT%H:%M:%S") + ".127840812-06:00"


def alert(name, state="firing", /, since=NOW - 900, annotations=None, **labels):
    return {"labels": {"alertname": name, "severity": "from-prometheus", **labels},
            "annotations": annotations or {"summary": "from Prometheus", "description": "from Prometheus"},
            "state": state, "activeAt": stamp(since), "value": "1e+00"}


def rules(*raised, evaluated=NOW - 10, checks=None, health="ok", names=None):
    """Prometheus' /api/v1/rules for the generated alerts.yml: the checks'
    group, then one alert rule for each catalogued name, holding its alerts."""
    def rule(kind, name, **more):
        return {"type": kind, "name": name, "query": "vector(1)", "health": health, "evaluationTime": 0.0004,
                "lastEvaluation": stamp(evaluated), "labels": {}, **more}

    def group(name, at, members):
        return {"name": name, "file": "/etc/prometheus/alerts.yml", "interval": 30, "limit": 0,
                "evaluationTime": 0.01, "lastEvaluation": stamp(at), "rules": members}

    names = list(catalogue.CATALOG if names is None else names)
    names += [each["labels"]["alertname"] for each in raised if each["labels"]["alertname"] not in names]
    alerting = [rule("alerting", name, duration=300, keepFiringFor=0, annotations={},
                     alerts=[each for each in raised if each["labels"]["alertname"] == name],
                     state="firing" if any(each["labels"]["alertname"] == name for each in raised) else "inactive")
                for name in names]
    return {"status": "success", "data": {"groups": [
        group("thermite_checks", evaluated if checks is None else checks, [rule("recording", catalogue.CHECK)]),
        group("thermite", evaluated, alerting)]}}


class Prometheus:
    """Prometheus' HTTP API as the gateway's client meets it: one answer for
    the rules, and a count of the requests."""

    def __init__(self, answer=None, status=200, error=None):
        self.answer, self.status, self.error, self.asked = rules() if answer is None else answer, status, error, 0

    def __call__(self, request):
        self.asked += 1
        assert (request.method, request.url.host, request.url.port, request.url.path, request.url.query) == (
            "GET", "127.0.0.1", PORT, "/api/v1/rules", b""), request.url
        if self.error:
            raise self.error
        if isinstance(self.answer, bytes):
            return httpx.Response(self.status, content=self.answer)
        return httpx.Response(self.status, json=self.answer)


class Feed(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE)

    def setUp(self):
        for store in (alerts._cache, alerts._logged):
            store.clear()
            self.addCleanup(store.clear)
        clock = patch.object(alerts, "clock", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.adapter = adapter()
        self.adapter.contract = RuntimeContract(ROOMS, house=HOUSE, history=HISTORY)
        install_adapter(self.adapter)
        _, self.admin = issue_token(self.owner, "alerts test")
        _, self.member = issue_token(self.operator, "alerts test")

    def tearDown(self):
        self.assertEqual(self.adapter.transport.commands, [], "reading alerts never routes")

    def read(self, prometheus, token=None, status=200):
        """GET /api/v1/alerts, with the gateway's HTTP client answered by `prometheus`."""
        if not isinstance(prometheus, Prometheus):
            prometheus = Prometheus(prometheus)
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token} if token else {}
        original = httpx.Client
        with patch.object(history.httpx, "Client",
                          side_effect=lambda **options: original(transport=httpx.MockTransport(prometheus), **options)):
            response = Client().get("/api/v1/alerts", secure=True, **headers)
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.json()

    def fresh(self, prometheus, token=None):
        alerts._cache.clear()
        return self.read(prometheus, token)

    def test_a_firing_alert_names_its_room_zone_device_and_check_in_the_catalogues_words(self):
        answer = self.read(rules(alert("ThermiteCheckFailing", room="den", zone="den_patio", device="den_tv",
                                       check="port", required="true", scope="control")))
        title, text = catalogue.CATALOG["ThermiteCheckFailing"]
        self.assertEqual(answer, {"version": 1, "monitoring": "ok", "evaluated": NOW - 10, "firing": 1, "alerts": [{
            "name": "ThermiteCheckFailing", "severity": "critical", "title": title, "text": text, "since": NOW - 900,
            "room": "den", "zone": "den_patio", "device": "den_tv", "check": "port",
            "check_text": catalogue.CHECK_WORDS["port"]}]})

    def test_nothing_firing_while_the_rules_are_evaluated_is_ok_and_empty(self):
        self.assertEqual(self.read(rules()),
                         {"version": 1, "monitoring": "ok", "evaluated": NOW - 10, "firing": 0, "alerts": []})

    def test_a_pending_alert_is_not_shown_or_counted(self):
        answer = self.read(rules(alert("ThermiteCheckFailing", "pending", room="den", check="port"),
                                 alert("ThermiteControlUnknown", room="den", zone="den", check="source")))
        self.assertEqual((answer["firing"], [each["name"] for each in answer["alerts"]]), (1, ["ThermiteControlUnknown"]))

    def test_every_pair_of_both_inhibition_tables_is_applied(self):
        def names(*raised):
            return sorted(each["name"] for each in self.fresh(rules(*raised), self.admin)["alerts"])

        for source, targets in catalogue.INHIBITS:
            self.assertNotIn(source, targets)
            for target in targets:
                with self.subTest(source=source, target=target):
                    # Whatever the room: these pairs have no `equal`.
                    self.assertEqual(names(alert(source), alert(target, room="den")), [source])
                    self.assertEqual(names(alert(target, room="den")), [target])
                    self.assertEqual(names(alert(source, "pending"), alert(target)), [target],
                                     "a source that is only pending holds nothing back")
        for source, targets in catalogue.ROOM_INHIBITS:
            self.assertNotIn(source, targets)
            for target in targets:
                with self.subTest(source=source, target=target):
                    self.assertEqual(names(alert(source, room="den", check="port"),
                                           alert(target, room="den", zone="den_patio", check="source")), [source])
                    self.assertEqual(names(alert(source, room="rack", check="port"),
                                           alert(target, room="den", check="source")), sorted([source, target]))
                    # No room label and an empty one are equal.
                    self.assertEqual(names(alert(source, check="port"), alert(target, room="", check="source")), [source])

    def test_an_inhibited_alert_is_not_counted_either(self):
        answer = self.read(rules(alert("ThermiteRuntimeUnavailable"),
                                 alert("ThermiteCheckFailing", room="den", device="den_tv", check="thing")), self.admin)
        self.assertEqual((answer["firing"], [each["name"] for each in answer["alerts"]]),
                         (1, ["ThermiteRuntimeUnavailable"]))

    def test_no_label_value_or_annotation_of_prometheus_reaches_a_client_or_the_log(self):
        script, device, path, check, severity, words, link = HOSTILE
        raised = [alert("ThermiteCheckFailing", room=script, zone=path, device=device, check=check, severity=severity,
                        annotations={"summary": words, "description": link}),
                  alert("ThermiteAttackersOwn", room="den", annotations={"summary": words, "description": link}),
                  # A label of another alert's vocabulary is not read at all.
                  alert("ThermiteGatewayDown", room=script, device=device, instance=link)]
        with self.assertLogs("oh_identity.alerts", "WARNING") as logged:
            answer = self.read(rules(*raised), self.admin)
            self.fresh(rules(*raised), self.admin)
        sent = json.dumps(answer)
        for text in (*HOSTILE, "from-prometheus", "ThermiteAttackersOwn"):
            self.assertNotIn(text, sent)
            self.assertNotIn(text, json.dumps(answer, ensure_ascii=False))
            self.assertNotIn(text, "\n".join(logged.output))
        # Counted, so the page is not silent about it; named only from the catalogue.
        self.assertEqual(answer["firing"], 3)
        title, text = catalogue.CATALOG["ThermiteCheckFailing"]
        self.assertEqual(answer["alerts"], [
            {"name": "ThermiteCheckFailing", "severity": "critical", "title": title, "text": text, "since": NOW - 900},
            {"name": "ThermiteGatewayDown", "severity": "critical", "since": NOW - 900,
             **dict(zip(("title", "text"), catalogue.CATALOG["ThermiteGatewayDown"], strict=True))}])
        # Once each, by alert and label name, however often it is read.
        self.assertEqual(sorted(line.rsplit(": ", 1)[1] for line in logged.output), sorted(
            [f"alert=ThermiteCheckFailing label={label}" for label in ("room", "zone", "device", "check")]
            + ["alert=uncatalogued label=alertname"]))
        # With nothing of the house to its name it is the house's, not a Guest's.
        self.assertEqual(self.fresh(rules(*raised))["alerts"], [])

    def test_a_label_the_alert_is_not_about_is_never_read(self):
        raised = [alert("ThermiteHostDiskLow", device="den_tv", room="den", mountpoint="/srv"),
                  alert("ThermiteThingStuck", room="rack", device="rack_matrix", zone="den", check="port", thing="x:y:z")]
        with self.assertNoLogs("oh_identity.alerts", "WARNING"):
            disk, thing = sorted(self.read(rules(*raised), self.admin)["alerts"], key=lambda each: each["name"])
        self.assertEqual(set(disk), {"name", "severity", "title", "text", "since"})
        self.assertEqual({key: thing[key] for key in thing if key not in ("title", "text", "since")},
                         {"name": "ThermiteThingStuck", "severity": "critical", "room": "rack", "device": "rack_matrix"})

    def test_a_house_wide_check_names_its_service_and_no_room(self):
        [failing] = self.read(rules(alert("ThermiteCheckFailing", room="house", device="apple_tv_helper",
                                          check="service")), self.admin)["alerts"]
        with self.assertLogs("oh_identity.alerts", "WARNING") as logged:
            [unknown] = self.fresh(rules(alert("ThermiteControlUnknown", room="house", device="den_tv",
                                               check="runtime")), self.admin)["alerts"]
        self.assertEqual({key: failing[key] for key in failing if key not in ("title", "text", "since")},
                         {"name": "ThermiteCheckFailing", "severity": "critical", "service": "apple_tv_helper",
                          "check": "service", "check_text": catalogue.CHECK_WORDS["service"]})
        # A house device is no service of the house.
        self.assertNotIn("service", unknown)
        self.assertNotIn("device", unknown)
        self.assertEqual(len(logged.output), 1)

    def test_a_policy_alert_names_its_zone(self):
        answer = self.read(rules(alert("ThermitePolicyUnenforced", zone="den_patio", state="unavailable")))
        self.assertEqual([(each["name"], each["zone"]) for each in answer["alerts"]],
                         [("ThermitePolicyUnenforced", "den_patio")])

    def test_everyone_reads_the_rooms_alerts_and_only_an_admin_the_houses(self):
        raised = [alert("ThermiteHostDiskLow"), alert("ThermiteServiceDown", name="thermite-openhab.service"),
                  alert("ThermiteCheckFailing", room="house", device="openhab", check="runtime"),
                  alert("ThermiteControlUnknown", room="den", zone="den", check="source"),
                  alert("ThermiteThingStuck", room="rack", device="rack_matrix")]
        prometheus = Prometheus(rules(*raised))
        guest, member, admin = (self.read(prometheus, token) for token in (None, self.member, self.admin))
        self.assertEqual(prometheus.asked, 1, "one read of Prometheus serves them all")
        self.assertEqual(guest, member)
        self.assertEqual([each["name"] for each in guest["alerts"]], ["ThermiteThingStuck", "ThermiteControlUnknown"])
        self.assertEqual(sorted(each["name"] for each in admin["alerts"]), sorted(each["labels"]["alertname"] for each in raised))
        # Whoever asks is told how many fire, so fewer shown is not fewer firing.
        self.assertEqual((guest["firing"], member["firing"], admin["firing"]), (5, 5, 5))
        for each in guest["alerts"]:
            self.assertNotIn("service", each)
            self.assertNotEqual(each.get("check"), "runtime")

    def test_with_authentication_off_nobody_is_an_admin_as_on_the_system_page(self):
        SecurityState.objects.update_or_create(pk=1)
        answer = self.read(rules(alert("ThermiteHostDiskLow"), alert("ThermiteControlUnknown", room="den", check="source")))
        self.assertEqual((answer["firing"], [each["name"] for each in answer["alerts"]]), (2, ["ThermiteControlUnknown"]))

    def test_prometheus_not_answering_is_unreachable_never_an_empty_ok(self):
        silent = {"version": 1, "monitoring": "unreachable", "evaluated": None, "firing": None, "alerts": []}
        firing = rules(alert("ThermiteControlUnknown", room="den", check="source"))
        for prometheus in (Prometheus(error=httpx.ConnectError("refused")), Prometheus(error=httpx.ReadTimeout("slow")),
                           Prometheus(firing, status=503), Prometheus(b"<html>not prometheus</html>"),
                           Prometheus({"status": "error", "errorType": "unavailable", "error": "starting"}),
                           Prometheus({"status": "success", "data": {}}),
                           Prometheus({"status": "success", "data": {"groups": [{"name": "thermite"}]}}),
                           Prometheus(rules(alert("ThermiteControlUnknown", room=7))),
                           Prometheus(b"[" * (history.MAX_BYTES + 1))):
            with self.subTest(prometheus=(prometheus.status, type(prometheus.error).__name__)):
                self.assertEqual(self.fresh(prometheus), silent)
                # The silence is shared like an answer: pages do not each wait for it.
                self.assertEqual(self.read(prometheus, self.admin), silent)
                self.assertEqual(prometheus.asked, 1)

    def test_a_contract_that_names_no_prometheus_is_unreachable(self):
        self.adapter.contract = RuntimeContract(ROOMS, house=HOUSE)
        prometheus = Prometheus()
        self.assertEqual(self.read(prometheus)["monitoring"], "unreachable")
        self.assertEqual(prometheus.asked, 0)

    def test_rules_no_longer_evaluated_are_stalled_though_their_alerts_are_still_listed(self):
        # Prometheus lists what its last evaluation left: ten minutes ago.
        left = alert("ThermiteControlUnknown", room="den", check="source")
        answer = self.read(rules(left, evaluated=NOW - 600))
        self.assertEqual((answer["monitoring"], answer["evaluated"], answer["firing"]), ("stalled", NOW - 600, 1))
        self.assertEqual([each["name"] for each in answer["alerts"]], ["ThermiteControlUnknown"])
        for name, stalled in (
                ("the checks' group alone is old", rules(checks=NOW - alerts.STALE_SECONDS - 1)),
                ("no rule is loaded", {"status": "success", "data": {"groups": []}}),
                ("no alert rule is loaded", rules(names=())),
                ("never evaluated", json.loads(json.dumps(rules()).replace(stamp(NOW - 10), "0001-01-01T00:00:00Z"))),
                ("a time without an offset", json.loads(json.dumps(rules()).replace("-06:00", ""))),
                ("a rule fails", rules(health="err"))):
            with self.subTest(name):
                self.assertEqual(self.fresh(stalled)["monitoring"], "stalled")
        for name, current in (("just inside", rules(evaluated=NOW - alerts.STALE_SECONDS)),
                              ("rules loaded since the last evaluation", rules(health="unknown"))):
            with self.subTest(name):
                self.assertEqual(self.fresh(current)["monitoring"], "ok")
        # Four evaluations may be missed, and no fewer.
        interval = int(oh_metrics.SCRAPE_INTERVAL.removesuffix("s"))
        self.assertEqual(alerts.STALE_SECONDS, 4 * interval)
        self.assertLessEqual(alerts.CACHE_SECONDS, interval)

    def test_clients_share_one_read_for_the_cache_time(self):
        prometheus = Prometheus(rules())
        for _ in range(20):
            self.read(prometheus)
        self.assertEqual(prometheus.asked, 1)
        with patch.object(alerts, "clock", return_value=NOW + alerts.CACHE_SECONDS):
            self.read(prometheus)
            self.read(prometheus)
        self.assertEqual(prometheus.asked, 2)

    def test_critical_alerts_come_first_then_the_oldest_and_a_long_list_is_cut_but_counted(self):
        raised = [alert("ThermiteControlUnknown", since=NOW - 3000 + n, room="den", zone="den", check="source")
                  for n in range(alerts.MAX_RECORDS + 10)]
        raised += [alert("ThermiteCheckFailing", since=NOW - 60, room="rack", device="rack_matrix", check="port"),
                   alert("ThermiteCheckFailing", since=NOW - 120, room="rack", device="rack_matrix", check="thing")]
        answer = self.read(rules(*raised))
        self.assertEqual((answer["firing"], len(answer["alerts"])), (alerts.MAX_RECORDS + 12, alerts.MAX_RECORDS))
        self.assertEqual([(each["name"], each["since"]) for each in answer["alerts"][:3]],
                         [("ThermiteCheckFailing", NOW - 120), ("ThermiteCheckFailing", NOW - 60),
                          ("ThermiteControlUnknown", NOW - 3000)])

    def test_only_get(self):
        self.assertEqual(Client().post("/api/v1/alerts", secure=True).status_code, 405)

    def test_the_guest_listener_serves_a_guests_answer(self):
        prometheus = Prometheus(rules(alert("ThermiteHostDiskLow"), alert("ThermiteControlUnknown", room="den", check="source")))
        original = httpx.Client
        with patch.object(history.httpx, "Client",
                          side_effect=lambda **options: original(transport=httpx.MockTransport(prometheus), **options)):
            response = async_to_sync(GuestClient().get)("/api/v1/alerts")
        self.assertEqual(response.status_code, 200)
        answer = json.loads(response.content)
        self.assertEqual((answer["firing"], [each["name"] for each in answer["alerts"]]), (2, ["ThermiteControlUnknown"]))


class Vocabulary(TestCase):
    def test_every_alert_the_feed_reads_labels_of_is_catalogued_and_reads_only_known_labels(self):
        self.assertLessEqual(set(catalogue.ABOUT), set(catalogue.CATALOG))
        for name, labels in catalogue.ABOUT.items():
            self.assertLessEqual(set(labels), {"room", "zone", "device", "check"}, name)
        # The two per-check alerts carry what a check's recording rule does.
        recorded = catalogue.rules({})["groups"][0]["rules"][0]["labels"]
        self.assertLessEqual(set(recorded) - {"required", "scope"}, set(catalogue.ABOUT["ThermiteCheckFailing"]))

    def test_a_house_wide_checks_device_is_one_of_the_feeds_services(self):
        for rule in catalogue.rules({})["groups"][0]["rules"]:
            if rule.get("record") == catalogue.CHECK and rule["labels"]["room"] == alerts.HOUSE_ROOM:
                self.assertIn(rule["labels"]["device"], alerts.SERVICES)
                self.assertIn(rule["labels"]["check"], catalogue.CHECK_WORDS)

    def test_severity_is_the_catalogues(self):
        self.assertEqual({catalogue.severity(name) for name in catalogue.CATALOG}, {"critical", "warning"})
        self.assertEqual({name for name in catalogue.CATALOG if catalogue.severity(name) == "critical"}, catalogue.CRITICAL)
