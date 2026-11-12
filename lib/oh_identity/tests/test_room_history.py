"""Each room's source through the day, read from a fake Prometheus the way the
House board's Rooms through the day reads the real one."""
import copy
from unittest.mock import patch

import httpx
from asgiref.sync import async_to_sync
from django.test import Client, TestCase

from oh_identity import history
from oh_identity.models import SecurityState, User
from oh_identity.runtime import RuntimeContract, install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, adapter
from oh_identity.tests.test_guest_listener import GuestClient

#: 2026-11-02 10:30:15 UTC: the day ends at the start of its minute.
NOW = 1793615415
END = NOW - NOW % 60
START = END - 24 * 3600
ROOMS = {"den": {"source_item": "loc_den_source", "sources": ["player"]},
         "desk": {"source_item": "loc_desk_source", "sources": ["player"]}}
HISTORY = {"port": 15607, "values": {"-1": None, "0": "off", "1": "player"},
           "labels": {"off": "Off", "player": "Player"},
           "zones": [{"room": "den", "item": "loc_den_source", "label": "Den", "query": "den_query"},
                     {"room": "desk", "item": "loc_desk_source", "label": "Desk", "query": "desk_query"}]}


def sample(at, value):
    return [at, str(value)]


class Prometheus:
    """A query_range that answers with the given series, and records each ask."""

    def __init__(self, result):
        self.result, self.asked = result, []

    def __call__(self, port, expr, start, end, step):
        self.asked.append((port, expr, start, end, step))
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


def series(item, *samples):
    return {"metric": {"item": item, "room": "fixture", "zone": "fixture"}, "values": list(samples)}


class RoomHistory(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE)

    def setUp(self):
        history._cache.clear()
        self.addCleanup(history._cache.clear)
        clock = patch.object(history, "clock", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.adapter = adapter()
        self.adapter.contract = RuntimeContract(ROOMS, history=HISTORY)
        install_adapter(self.adapter)
        _, self.admin = issue_token(self.owner, "history test")
        _, self.member = issue_token(self.operator, "history test")

    def tearDown(self):
        self.assertEqual(self.adapter.transport.commands, [], "reading history never routes")

    def read(self, prometheus, token=None, status=200):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token} if token else {}
        with patch.object(history, "query", prometheus):
            response = Client().get("/api/v1/history/rooms", secure=True, **headers)
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.json()

    def test_runs_of_one_value_unknown_and_no_data(self):
        prometheus = Prometheus([
            series("loc_den_source", sample(START, 0), sample(START + 60, 0), sample(START + 120, -1),
                   sample(START + 180, 1), sample(START + 300, 1), sample(END - 60, 1)),
            series("loc_desk_source", sample(START, 7), sample(START + 60, "NaN"), sample(START + 120, 0.5))])
        answer = self.read(prometheus)
        self.assertEqual({key: answer[key] for key in ("version", "start", "end", "step", "labels")},
                         {"version": 1, "start": START, "end": END, "step": 60,
                          "labels": {"off": "Off", "player": "Player"}})
        den, desk = answer["rooms"]
        self.assertEqual((den["room"], den["label"], desk["room"], desk["label"]), ("den", "Den", "desk", "Desk"))
        # Off, Unknown (no readback), the player; a missing sample is no run (no data).
        self.assertEqual(den["segments"], [[START, START + 120, "off"], [START + 120, START + 180, None],
                                           [START + 180, START + 240, "player"], [START + 300, START + 360, "player"],
                                           [END - 60, END, "player"]])
        # A number the contract does not name is Unknown, as the board greys an unmapped value.
        self.assertEqual(desk["segments"], [[START, START + 180, None]])
        port, expr, start, end, step = prometheus.asked[0]
        self.assertEqual((port, expr, start, end, step), (15607, "(den_query) or (desk_query)", START, END - 60, 60))

    def test_a_zone_without_series_has_no_runs(self):
        answer = self.read(Prometheus([series("loc_den_source", sample(START, 0))]))
        self.assertEqual([room["segments"] for room in answer["rooms"]], [[[START, START + 60, "off"]], []])

    def test_guests_members_and_admins_read_the_same_answer_once_a_step(self):
        prometheus = Prometheus([series("loc_den_source", sample(START, 0))])
        answers = [self.read(prometheus, token) for token in (None, self.member, self.admin)]
        self.assertEqual(answers[1:], answers[:1] * 2)
        self.assertEqual(len(prometheus.asked), 1, "clients within a step share one query")
        with patch.object(history, "clock", return_value=NOW + 60):
            later = self.read(prometheus)
        self.assertEqual(len(prometheus.asked), 2)
        self.assertEqual(later["end"], END + 60)

    def test_authentication_on_still_lets_guests_read(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True})
        self.read(Prometheus([]))

    def test_prometheus_failures_are_unavailable_and_not_cached(self):
        for result in (httpx.ConnectError("refused"), ValueError("not a range"),
                       [series("loc_den_source", sample(START, 0)), series("loc_den_source", sample(START, 1))],
                       [{"metric": {"item": "loc_den_source"}}], [series("loc_den_source", ["broken"])]):
            self.assertEqual(self.read(Prometheus(result), status=503), {"error": "history_unavailable"}, result)
        self.assertEqual(history._cache, {})

    def test_a_contract_without_history_is_unavailable(self):
        self.adapter.contract = RuntimeContract(ROOMS)
        prometheus = Prometheus([])
        self.assertEqual(self.read(prometheus, status=503), {"error": "history_unavailable"})
        self.assertEqual(prometheus.asked, [])

    def test_only_get(self):
        response = Client().post("/api/v1/history/rooms", secure=True)
        self.assertEqual(response.status_code, 405)

    def test_the_guest_listener_does_not_serve_it(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": False, "revision": 1})
        prometheus = Prometheus([])
        with patch.object(history, "query", prometheus):
            response = async_to_sync(GuestClient().get)("/api/v1/history/rooms")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(prometheus.asked, [])


class Contract(TestCase):
    def test_history_must_name_the_contracts_rooms_and_values(self):
        for broken in ({**HISTORY, "port": 80},
                       {**HISTORY, "extra": True},
                       {**HISTORY, "zones": [{**HISTORY["zones"][0], "room": "attic"}]},
                       {**HISTORY, "zones": [{**HISTORY["zones"][0], "item": "loc_desk_source"}]},
                       {**HISTORY, "zones": [HISTORY["zones"][0], HISTORY["zones"][0]]},
                       {**HISTORY, "zones": [{**HISTORY["zones"][0], "label": "Den‮"}]},
                       {**HISTORY, "values": {**HISTORY["values"], "2": "attic"}},
                       {**HISTORY, "values": {"one": "off"}},
                       {**HISTORY, "labels": {**HISTORY["labels"], "bad value": "Bad"}},
                       {key: value for key, value in HISTORY.items() if key != "labels"}):
            with self.assertRaises(ValueError, msg=broken):
                RuntimeContract(ROOMS, history=broken)
        self.assertEqual(RuntimeContract(ROOMS, history=HISTORY).history["port"], 15607)
        self.assertIsNone(RuntimeContract(ROOMS).history)
