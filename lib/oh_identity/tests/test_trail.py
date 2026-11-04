"""The command trail: one key=value journal line per command, refusal,
security event, gateway request and event stream, never with a credential,
pairing code, body or address. Fictional accounts and documentation
addresses only."""

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase

import oh_room_policy
from oh_access import audit as audit_reader
from oh_identity import streams, topics, trail
from oh_identity.batches import commands
from oh_identity.models import Audit, PersonalToken, RoomPolicy, SecurityState, User
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import PASSPHRASE, FakeRuntime, Policy, adapter
from oh_identity.tests.test_guest_listener import call as guest_call
from oh_identity.transport import CommandRefused

HEX8 = re.compile(r"[0-9a-f]{8}")
TAB = "web/5e6f7a8b"
#: An empty field, as the trail prints it.
NONE = "-"
CODE = "4826"


FIELD = re.compile(r' ([a-z_]+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse(message):
    """(kind, fields) of one trail line; a quoted value keeps its quotes."""
    first = FIELD.search(message)
    kind = message[:first.start()] if first else message
    return kind, dict(FIELD.findall(message[len(kind):]))


def lines(logs, kind):
    return [parse(record.getMessage())[1] for record in logs.records if parse(record.getMessage())[0] == kind]


def without(fields, *names):
    return {key: value for key, value in fields.items() if key not in names}


class Requests:
    def request(self, method, path, data=None, *, client=None, **extra):
        client = client or self.client
        csrf = client.cookies.get(settings.CSRF_COOKIE_NAME)
        extra.setdefault("HTTP_ORIGIN", "https://testserver")
        return getattr(client, method)(path, data=json.dumps(data) if data is not None else None,
                                       content_type="application/json", secure=True,
                                       HTTP_X_CSRFTOKEN=csrf.value if csrf else "", **extra)

    def signin(self, name):
        self.request("get", "/api/v1/session")
        response = self.request("post", "/api/v1/session", {"username": name, "password": PASSPHRASE})
        self.assertEqual(response.status_code, 200)

    def command(self, action="volume", value=56.5, room="den", **extra):
        return self.request("post", f"/api/v1/rooms/{room}/commands", {"action": action, "value": value}, **extra)


class Commands(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE)

    def setUp(self):
        self.adapter = adapter()
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)
        self.request("get", "/api/v1/session")
        warnings = patch.object(trail, "warnings", trail.Limited())
        warnings.start()
        self.addCleanup(warnings.stop)

    def test_a_session_command_names_its_client_user_peer_and_openhab_source(self):
        self.signin("operator")
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.assertEqual(self.command(HTTP_X_THERMITE_CLIENT=TAB).status_code, 202)
        [line] = lines(logs, "cmd")
        self.assertEqual(without(line, "id", "peer", "ms"), {
            "origin": "web", "client": TAB, "user": "operator", "principal": "session", "token": NONE, "req": "-",
            "room": "den", "action": "volume", "item": "amp_volume", "value": "56.5", "outcome": "accepted"})
        self.assertRegex(line["id"], HEX8)
        self.assertRegex(line["ms"], r"^\d+$")
        # The audit's keyed peer digest, never the address.
        self.assertTrue(trail.peer_digest("127.0.0.1").startswith(line["peer"]))
        self.assertEqual(self.adapter.transport.sources, ["thermite.web:" + line["id"]])
        self.assertNotIn("127.0.0.1", "\n".join(logs.output))

    def test_guests_tokens_and_unlabelled_or_forged_clients(self):
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.assertEqual(self.command().status_code, 202)
        [line] = lines(logs, "cmd")
        self.assertEqual((line["origin"], line["client"], line["user"], line["principal"], line["token"]),
                         ("api", "-", "guest", "guest", "-"))
        token, raw = issue_token(self.operator, "laptop")
        terminal = Client()
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            for label in ("tui/laptop", "web/../escape", "admin/x", "web/a b", "tui/" + "x" * 64):
                response = terminal.post("/api/v1/rooms/den/commands", json.dumps({"action": "source", "value": "off"}),
                                         content_type="application/json", secure=True, HTTP_AUTHORIZATION="Bearer " + raw,
                                         HTTP_X_THERMITE_CLIENT=label)
                self.assertEqual(response.status_code, 202)
        found = lines(logs, "cmd")
        self.assertEqual((found[0]["origin"], found[0]["client"], found[0]["principal"], found[0]["token"], found[0]["user"]),
                         ("tui", "tui/laptop", "token", str(token.pk)[:8], "operator"))
        self.assertEqual([(line["origin"], line["client"]) for line in found[1:]], [("api", "-")] * 4)
        self.assertNotIn(raw, "\n".join(logs.output))
        self.assertNotIn("escape", "\n".join(logs.output))

    def test_invalid_commands_are_not_logged(self):
        with self.assertNoLogs("oh_identity.trail", "INFO"):
            self.assertEqual(self.command("volume", 70.5).status_code, 400)
            self.assertEqual(self.command("no_such_action", "x").status_code, 404)

    def test_refusals_name_the_lock_kind_and_room(self):
        RoomPolicy.objects.create(room_id="desk", config={"locked": True})
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.assertEqual(self.command(room="desk", action="source", value="off").status_code, 423)
            self.assertEqual(self.command("shared_power", "OFF").status_code, 423)
        room, device = lines(logs, "cmd")
        self.assertEqual((room["room"], room["outcome"], room["lock"], room["lock_room"]),
                         ("desk", "room_locked", "schedule", "desk"))
        self.assertEqual((device["room"], device["outcome"], device["lock"], device["lock_room"]),
                         ("den", "device_locked", "schedule", "desk"))
        # A 423 response is unchanged: the lock kind is the trail's alone.
        self.assertEqual(self.command(room="desk", action="source", value="off").json(), {"error": "room_locked"})
        self.assertEqual(self.adapter.transport.commands, [])

    def test_a_manual_lock_is_named_and_its_audit_line_says_who_set_it(self):
        self.adapter.policy = oh_room_policy
        self.signin("owner")
        with self.assertLogs("oh_identity.trail", "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.request("put", "/api/v1/rooms/den/lock", {"revision": 0, "locked": True}).status_code, 200)
        [audit] = lines(logs, "audit")
        self.assertEqual(without(audit, "peer", "at"), {"action": "room.lock", "outcome": "ok", "principal": "session",
                                                        "actor": str(self.owner.pk), "user": "owner", "target": "den"})
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.assertEqual(self.command("source", "off").status_code, 423)
        [line] = lines(logs, "cmd")
        self.assertEqual((line["outcome"], line["lock"], line["lock_room"], line["user"]), ("room_locked", "manual", "den", "owner"))

    def test_runtime_failures_keep_the_status_or_exception_class(self):
        for error, outcome in ((CommandRefused(503), "runtime_unavailable(http=503)"),
                               (ConnectionResetError("private upstream text"), "runtime_unavailable(exc=ConnectionResetError)")):
            with self.subTest(outcome=outcome), patch.object(self.adapter.transport, "command", side_effect=error), \
                    self.assertLogs("oh_identity.trail", "INFO") as logs:
                self.assertEqual(self.command().status_code, 502)
            [line] = lines(logs, "cmd")
            self.assertEqual(line["outcome"], outcome)
            self.assertNotIn("private upstream text", "\n".join(logs.output))

    def test_an_unreadable_policy_warns_with_its_class_at_most_once_per_interval(self):
        RoomPolicy.objects.create(room_id="den", config={"unknown": True})
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            for _ in range(3):
                self.assertEqual(self.command().status_code, 503)
            self.assertEqual(self.request("get", "/api/v1/locks").status_code, 200)
        warned = [record.getMessage() for record in logs.records if record.levelname == "WARNING"]
        self.assertEqual(warned, ["policy_unavailable where=command room=den exc=ValueError suppressed=0",
                                  "policy_unavailable where=status room=den exc=ValueError suppressed=0"])
        self.assertEqual([line["outcome"] for line in lines(logs, "cmd")], ["policy_unavailable"] * 3)
        self.adapter.policy = None
        with self.assertLogs("oh_identity.trail", "WARNING") as logs:
            self.command()
        self.assertEqual(logs.records[0].getMessage(), "policy_unavailable where=command room=den exc=no_engine suppressed=0")

    def test_pairing_codes_never_reach_the_trail(self):
        pairings = {"tv_pairing": {"value": "sony_ip_control", "config": {"length": 4, "chars": "digits"}}}
        self.adapter = RuntimeAdapter(RuntimeContract({"den": {"source_item": "loc_den_source", "sources": ["player"]}},
                                                      pairings=pairings), FakeRuntime(), Policy())
        install_adapter(self.adapter)
        _, raw = issue_token(self.owner, "pairing")
        with self.assertLogs("oh_identity.trail", "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            for value in ("REQUEST", CODE):
                response = Client().post("/rest/items/tv_pairing", value, content_type="text/plain", secure=True,
                                         HTTP_AUTHORIZATION="Bearer " + raw, HTTP_X_THERMITE_CLIENT=TAB)
                self.assertEqual(response.status_code, 202)
        found = lines(logs, "cmd")
        self.assertEqual([(line["action"], line["item"], line["value"], line["room"], line["outcome"]) for line in found],
                         [("pair", "tv_pairing", "<private>", "-", "accepted")] * 2)
        self.assertEqual(self.adapter.transport.sources, ["thermite.web:" + line["id"] for line in found])
        self.assertEqual([line["action"] for line in lines(logs, "audit")], ["pairing.command"] * 2)
        self.assertNotIn(CODE, "\n".join(logs.output))
        self.assertNotIn("REQUEST", "\n".join(logs.output))

    def test_origin_reads_the_guest_mark_and_the_request_id_from_the_scope(self):
        guest = SimpleNamespace(scope={trail.GUEST_SCOPE: object(), trail.REQUEST_ID: "0a1b2c3d"},
                                headers={"X-Thermite-Client": TAB})
        self.assertEqual(trail.origin(guest), trail.Origin("guest", TAB, "0a1b2c3d"))
        forged = SimpleNamespace(scope={trail.REQUEST_ID: "not-an-id"}, headers={"X-Thermite-Client": "guest/x"})
        self.assertEqual(trail.origin(forged), trail.Origin("api", None, None))
        with self.assertRaises(ValueError):
            trail.Origin("admin")


class GuestListener(TestCase):
    """Commands through the plain-HTTP Guest listener: origin guest from the
    listener's scope mark, principal guest, a peer digest and no address."""

    def setUp(self):
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": True, "revision": 1})
        self.adapter = adapter()
        install_adapter(self.adapter)

    def test_listener_commands_are_guests_whatever_the_client_claims(self):
        command = {"action": "source", "value": "off"}
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.assertEqual(guest_call("post", "/api/v1/rooms/den/commands", command,
                                        **{"X-Thermite-Client": TAB}).status_code, 202)
            self.assertEqual(guest_call("post", "/api/v1/rooms/den/commands", command,
                                        **{"X-Thermite-Client": "tui/laptop"}).status_code, 202)
            RoomPolicy.objects.create(room_id="den", config={"locked": True})
            self.assertEqual(guest_call("post", "/api/v1/rooms/den/commands", command).status_code, 423)
        web, terminal, refused = lines(logs, "cmd")
        self.assertEqual(without(web, "id", "peer", "ms"), {
            "origin": "guest", "client": TAB, "user": "guest", "principal": "guest", "token": NONE, "req": NONE,
            "room": "den", "action": "source", "item": "loc_den_source", "value": "off", "outcome": "accepted"})
        self.assertEqual((terminal["origin"], terminal["client"], terminal["principal"]), ("guest", "tui/laptop", "guest"))
        self.assertEqual((refused["origin"], refused["principal"], refused["outcome"], refused["lock_room"]),
                         ("guest", "guest", "room_locked", "den"))
        self.assertIn(refused["lock"], ("manual", "schedule"))
        self.assertTrue(trail.peer_digest("192.0.2.50").startswith(web["peer"]))
        self.assertNotIn("192.0.2.50", "\n".join(logs.output))
        self.assertEqual(self.adapter.transport.sources, ["thermite.guest:" + web["id"], "thermite.guest:" + terminal["id"]])


class AllOff(Requests, TransactionTestCase):
    def setUp(self):
        self.owner = bootstrap("owner", PASSPHRASE)
        self.adapter = adapter()
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)
        self.signin("owner")

    def test_a_batch_line_and_one_child_line_per_room(self):
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            self.assertEqual(self.request("post", "/api/v1/all-off", {}, HTTP_X_THERMITE_CLIENT=TAB).status_code, 202)
        [batch] = lines(logs, "all_off")
        self.assertEqual(without(batch, "id", "peer", "ms"), {
            "origin": "web", "client": TAB, "user": "owner", "principal": "session", "token": NONE, "req": "-",
            "rooms": "2", "accepted": "2", "skipped": "0", "outcome": "accepted"})
        children = sorted(lines(logs, "cmd"), key=lambda line: line["room"])
        self.assertEqual([(line["batch"], line["origin"], line["client"], line["user"], line["room"], line["value"],
                           line["outcome"]) for line in children],
                         [(batch["id"], "all_off", TAB, "owner", room, "off", "accepted") for room in ("den", "desk")])
        self.assertEqual(len({line["id"] for line in children} | {batch["id"]}), 3)
        self.assertEqual(sorted(self.adapter.transport.sources), sorted("thermite.all_off:" + line["id"] for line in children))

    def test_rooms_the_batch_could_not_admit_are_logged_busy(self):
        release = threading.Event()
        futures = [commands.submit(lambda _: release.wait(3), item) for item in range(8)]
        try:
            with self.assertLogs("oh_identity.trail", "INFO") as logs:
                self.assertEqual(self.request("post", "/api/v1/all-off", {}).status_code, 202)
        finally:
            release.set()
            for future in futures:
                future.result(timeout=2)
        self.assertEqual([line["outcome"] for line in lines(logs, "cmd")], ["runtime_busy"] * 2)
        [batch] = lines(logs, "all_off")
        self.assertEqual((batch["accepted"], batch["skipped"], batch["origin"]), ("0", "2", "api"))


class Auditing(Requests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = bootstrap("owner", PASSPHRASE)
        cls.operator = User.objects.create_user("operator", password=PASSPHRASE)

    def setUp(self):
        install_adapter(adapter())
        self.client = Client(enforce_csrf_checks=True)

    def test_every_row_is_mirrored_once_committed(self):
        with self.assertLogs("oh_identity.trail", "INFO") as logs, self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.signin("owner")
        self.assertEqual(len(callbacks), 1)
        [line] = lines(logs, "audit")
        self.assertEqual(without(line, "peer", "at"), {"action": "login", "outcome": "ok", "principal": "password",
                                                       "actor": str(self.owner.pk), "user": "owner", "target": "-"})
        row = Audit.objects.get(action="login")
        self.assertEqual(line["peer"], row.peer[:8])
        self.assertEqual(datetime.fromisoformat(line["at"]), row.at)

    def test_denied_logins_name_an_existing_account_by_id_only(self):
        typed = "ghost-sentinel-name"
        with self.assertLogs("oh_identity.trail", "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            self.request("get", "/api/v1/session")
            for name in ("operator", typed):
                guess = "wrong-" + name
                self.assertEqual(self.request("post", "/api/v1/session", {"username": name, "password": guess}).status_code, 401)
        self.assertEqual([(line["target"], line["actor"], line["principal"], line["outcome"]) for line in lines(logs, "audit")],
                         [(str(self.operator.pk), "-", "anonymous", "denied"), ("-", "-", "anonymous", "denied")])
        self.assertNotIn("sentinel", "\n".join(logs.output) + str(list(Audit.objects.values())))
        self.assertNotIn("wrong-", "\n".join(logs.output))

    def test_token_login_is_attributed_to_the_password_and_peer_and_keeps_the_name(self):
        with self.assertLogs("oh_identity.trail", "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            response = Client().post("/api/v1/token-login", json.dumps({"username": "operator", "password": PASSPHRASE,
                                                                        "name": "laptop"}),
                                     content_type="application/json", secure=True, REMOTE_ADDR="192.0.2.61")
        self.assertEqual(response.status_code, 201)
        created = Audit.objects.get(action="token.create")
        ident = response.json()["id"]
        self.assertEqual((created.principal, created.actor_id, created.target), ("password", self.operator.pk, ident[:8] + " laptop"))
        self.assertEqual(created.peer, trail.peer_digest("192.0.2.61"))
        self.assertEqual(lines(logs, "audit")[-1]["target"], '"' + ident[:8] + ' laptop"')
        self.assertNotIn(response.json()["token"], "\n".join(logs.output))
        self.assertNotIn("192.0.2.61", "\n".join(logs.output))
        # The name outlives the token row.
        self.signin("operator")
        self.assertEqual(self.request("delete", "/api/v1/tokens/" + ident).status_code, 200)
        PersonalToken.objects.all().delete()
        self.assertEqual(Audit.objects.get(action="token.revoke").target, ident[:8] + " laptop")

    def test_the_security_switch_names_the_credential_and_peer_that_changed_it(self):
        from oh_identity.security import get_state
        self.signin("owner")
        with self.captureOnCommitCallbacks(execute=True):
            off = self.request("post", "/api/v1/security", {"enabled": False, "revision": get_state().revision,
                                                             "password": PASSPHRASE}, REMOTE_ADDR="192.0.2.70")
            self.assertEqual(off.status_code, 200)
            self.request("get", "/api/v1/session")
            on = self.request("post", "/api/v1/security", {"enabled": True, "revision": get_state().revision,
                                                            "username": "owner", "password": PASSPHRASE}, REMOTE_ADDR="192.0.2.71")
            self.assertEqual(on.status_code, 200)
        rows = list(Audit.objects.filter(action__in=("security.disable", "security.enable")).order_by("id")
                    .values_list("action", "principal", "peer", "actor_id"))
        self.assertEqual([row[:2] for row in rows[-2:]], [("security.disable", "session"), ("security.enable", "password")])
        self.assertEqual(rows[-1][2], trail.peer_digest("192.0.2.71"))
        self.assertTrue(rows[-2][2] and rows[-1][3] == self.owner.pk)

    def test_the_audit_reader_filters_by_time_and_action_family(self):
        self.signin("owner")
        token, _ = issue_token(self.owner, "laptop")
        self.request("delete", f"/api/v1/tokens/{token.pk}")
        guess = "wrong-guess"
        self.request("post", "/api/v1/session", {"username": "operator", "password": guess})
        Audit.objects.filter(action__in=("bootstrap", "security.enable")).update(at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        actions = [entry["action"] for entry in audit_reader.select()]
        self.assertEqual(actions[:2], ["security.enable", "bootstrap"])
        self.assertEqual([entry["action"] for entry in audit_reader.select(actions=["token"])], ["token.create", "token.revoke"])
        recent = [entry["action"] for entry in audit_reader.select(after="1d")]
        self.assertEqual(recent[0], "login")
        self.assertNotIn("bootstrap", recent)
        denied = audit_reader.select(actions=["login"])[-1]
        self.assertEqual((denied["outcome"], denied["target"], denied["account"], denied["actor"]),
                         ("denied", str(self.operator.pk), "operator", None))
        self.assertRegex(denied["peer"], HEX8)
        text = audit_reader.text(denied)
        self.assertRegex(text, r"^\S+ audit action=login outcome=denied principal=anonymous actor=- user=- target=\d+ "
                               r"peer=[0-9a-f]{8} account=operator$")
        now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
        self.assertEqual(audit_reader.since("90m", now, timezone.utc), now - timedelta(minutes=90))
        self.assertEqual(audit_reader.since("2026-11-04T10:00", now, timezone.utc), datetime(2026, 11, 4, 10, tzinfo=timezone.utc))
        with self.assertRaises(audit_reader.AccessError):
            audit_reader.since("yesterday", now, timezone.utc)


class Streams(TransactionTestCase):
    def setUp(self):
        bootstrap("owner", PASSPHRASE)
        self.adapter = adapter()
        install_adapter(self.adapter)

    def test_a_stream_logs_its_opening_and_its_end_once_with_counts(self):
        path = "/api/v1/events/remote"
        # topics= is the number of topics the stream's view carries.
        carried = str(len(topics.views(self.adapter.contract)["remote"]))
        with patch("oh_identity.streams.slots", streams.Slots()), self.assertLogs("oh_identity.trail", "INFO") as logs:
            first = Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True)
            second = Client(REMOTE_ADDR="192.0.2.20").get(path, secure=True)
            first.close()
            first.close()
            second.close()
        opened, closed = lines(logs, "stream open"), lines(logs, "stream close")
        self.assertEqual([(line["streams_peer"], line["streams_total"], line["principal"], line["topics"]) for line in opened],
                         [("1", "1", "guest", carried), ("2", "2", "guest", carried)])
        self.assertEqual([(line["streams_peer"], line["streams_total"]) for line in closed], [("1", "1"), ("0", "0")])
        self.assertTrue(all(re.fullmatch(r"\d+", line["ms"]) for line in closed))
        self.assertNotIn("192.0.2.20", "\n".join(logs.output))

    def test_the_hook_releases_once(self):
        released = []
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            close = trail.stream_opened(lambda: released.append(1), SimpleNamespace(scope={trail.REQUEST_ID: "0a1b2c3d"}),
                                        None, topics=3, counts=lambda: (len(released), len(released)))
            close()
            close()
        self.assertEqual(released, [1])
        self.assertEqual([record.getMessage().split(" ")[:3] for record in logs.records],
                         [["stream", "open", "id=0a1b2c3d"], ["stream", "close", "id=0a1b2c3d"]])


class RequestLog(SimpleTestCase):
    """The pure ASGI request line and X-Request-Id."""

    def run_app(self, status, *, method="POST", path="/api/v1/rooms/den/commands", raises=None, **scope):
        seen, sent = [], []

        async def app(scope, receive, send):
            seen.append(scope)
            if raises:
                raise raises
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"private body"})

        async def send(message):
            sent.append(message)

        request = {"type": "http", "method": method, "path": path, "client": ("192.0.2.30", 50000), **scope}
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            trail.LOG.info("marker")
            try:
                asyncio.run(trail.RequestLog(app)(request, None, send))
            except type(raises) if raises else ():
                pass
        return seen, sent, [record.getMessage() for record in logs.records][1:]

    def test_changes_and_refusals_are_logged_with_the_id_the_response_carries(self):
        seen, sent, logged = self.run_app(202)
        [line] = logged
        kind, fields = parse(line)
        self.assertEqual((kind, fields["method"], fields["path"], fields["status"], fields["listener"]),
                         ("req", "POST", "/api/v1/rooms/den/commands", "202", "-"))
        self.assertEqual(dict(sent[0]["headers"])[b"x-request-id"].decode(), fields["id"])
        self.assertEqual(seen[0][trail.REQUEST_ID], fields["id"])
        self.assertEqual(fields["peer"], trail.peer_digest("192.0.2.30")[:8])
        self.assertNotIn("192.0.2.30", line)
        self.assertNotIn("private body", line)

    def test_successful_reads_are_not_logged_but_carry_an_id(self):
        for path in ("/api/v1/locks", "/api/v1/rooms/den/policy", "/static/remote/home.js"):
            _, sent, logged = self.run_app(200, method="GET", path=path)
            self.assertEqual(logged, [])
            self.assertRegex(dict(sent[0]["headers"])[b"x-request-id"].decode(), HEX8)
        _, _, logged = self.run_app(404, method="GET", path="/api/v1/rooms/den/policy")
        self.assertEqual(parse(logged[0])[1]["status"], "404")

    def test_guest_listener_failures_and_hostile_paths(self):
        _, _, logged = self.run_app(403, **{trail.GUEST_SCOPE: object()})
        self.assertEqual(parse(logged[0])[1]["listener"], "guest")
        _, _, logged = self.run_app(None, raises=RuntimeError("private failure"))
        self.assertEqual(parse(logged[0])[1]["status"], "-")
        self.assertNotIn("private failure", logged[0])
        _, _, logged = self.run_app(404, method="GET", path="/x y\nreq id=forged")
        self.assertEqual(len(logged), 1)
        self.assertNotIn("\n", logged[0])
        self.assertIn('path="/x y\\nreq id=forged"', logged[0])

    def test_other_scopes_pass_through_untouched(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope)
        for kind in ("lifespan", "websocket"):
            asyncio.run(trail.RequestLog(app)({"type": kind}, None, None))
        self.assertEqual(seen, [{"type": "lifespan"}, {"type": "websocket"}])


class Lines(SimpleTestCase):
    def test_values_cannot_break_a_line_or_forge_a_field(self):
        self.assertEqual(trail.line("x", a="plain-1.0", b="two words", c=None, d=True, e=3, f="a\nb=c"),
                         'x a=plain-1.0 b="two words" c=- d=true e=3 f="a\\nb=c"')
        self.assertEqual(trail.cause(CommandRefused(503)), "http=503")
        self.assertEqual(trail.cause(TimeoutError("private")), "exc=TimeoutError")

    def test_warnings_are_limited_per_key_and_count_what_they_suppress(self):
        now = [0.0]
        limited = trail.Limited(every=300, clock=lambda: now[0])
        with self.assertLogs("oh_identity.trail", "WARNING") as logs:
            self.assertTrue(limited.warning("a", "w", room="den"))
            self.assertFalse(limited.warning("a", "w", room="den"))
            self.assertTrue(limited.warning("b", "w", room="desk"))
            now[0] = 301
            self.assertTrue(limited.warning("a", "w", room="den"))
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["w room=den suppressed=0", "w room=desk suppressed=0", "w room=den suppressed=1"])
