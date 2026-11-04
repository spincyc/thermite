"""Real loopback protocol faults and durable onboarding recovery, without a runtime."""
from __future__ import annotations

import base64
import copy
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_install import native_auth as auth


class Store:
    def __init__(self):
        self.value, self.published = None, None
        self.history = []
        self.fail_save, self.fail_publish = None, False

    def load(self):
        return copy.deepcopy(self.value)

    def save(self, value):
        if value["phase"] == self.fail_save:
            raise OSError("private response detail must never escape")
        self.value = copy.deepcopy(value)
        self.history.append(copy.deepcopy(value))

    def publish(self, token):
        if self.value is None or self.value.get("token") != token or self.value["phase"] not in ("recorded", "published"):
            raise AssertionError("publication preceded durable plaintext recovery")
        self.published = token
        if self.fail_publish:
            raise OSError("private publication detail must never escape")


class StandIn:
    """A small stateful model of pinned handlers, with actual socket failures."""

    def __init__(self, store):
        self.store = store
        self.uuid = "a748ea7b-7390-462e-9f91-7591cd5ca746"
        self.user = None
        self.tokens, self.sessions = {}, set()
        self.calls, self.mutations = [], []
        self.fault = None
        self.csrf = "c" * 32
        self.auth_params = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.run_request()

            def do_POST(self):
                self.run_request()

            def do_DELETE(self):
                self.run_request()

            def respond(self, code, payload=b"", kind="text/plain", headers=None):
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            def run_request(self):
                try:
                    owner.handle(self)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def page(self, action, params=None, *, result=None):
        password_input_type = "password"
        fields = {"csrf_token": "hidden", "username": "text", "password": password_input_type, "new_password": "hidden",
                  "password_repeat": password_input_type if action == "/auth" and self.user is None else "hidden",
                  "token_name": "hidden" if action == "/auth" else "text",
                  "token_scope": "hidden" if action == "/auth" else "text"}
        fields.update({key: "hidden" for key in params or {}})
        values = {"csrf_token": self.csrf} | (params or {})
        if result is not None:
            fields.pop("csrf_token")
        inputs = "".join('<input type="' + kind + '" name="' + key + '" value="' + html.escape(values.get(key, ""), quote=True) + '">' for key, kind in fields.items())
        return (('<div class="message"><code>' + result + "</code></div>") if result is not None else "") + '<form class="' + ("hide" if result is not None else "show") + '" method="POST" action="' + action + '">' + inputs + "</form>"

    def handle(self, request):
        parsed = urlsplit(request.path)
        path = parsed.path
        method = request.command
        self.calls.append((method, path))
        data = {}
        if method == "POST":
            body = request.rfile.read(int(request.headers.get("Content-Length", "0"))).decode("ascii")
            data = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
        if self.fault == "redirect" and path == "/auth" and method == "GET":
            return request.respond(302, headers={"Location": "http://192.0.2.7:1234/foreign"})
        if self.fault == "oversize" and path == "/auth":
            return request.respond(200, b"x" * (auth.MAX_BODY + 1), "text/html")
        if self.fault == "oversize_headers" and path == "/rest/uuid":
            return request.respond(200, self.uuid, headers={"X-Large": "x" * auth.MAX_HEADERS})
        if self.fault == "header_drip" and path == "/rest/uuid":
            request.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nX-Drip: ")
            for _ in range(100):
                request.connection.sendall(b"x")
                time.sleep(0.03)
            return
        if self.fault == "drip" and path == "/rest/uuid":
            request.send_response(200)
            request.send_header("Content-Type", "text/plain")
            request.send_header("Content-Length", str(len(self.uuid)))
            request.end_headers()
            for char in self.uuid:
                request.wfile.write(char.encode("ascii")); request.wfile.flush()
                time.sleep(0.03)
            return
        if self.fault == "unavailable" and path == "/rest/uuid":
            return request.respond(503)
        if path == "/rest/uuid":
            if self.fault == "uuid_auth" and request.headers.get("Authorization") not in {"Bearer " + value for value in self.tokens.values()}:
                return request.respond(401)
            return request.respond(200, self.uuid)
        if path == "/auth" and method == "GET":
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            self.auth_params = params
            page = self.page(path, params)
            if self.fault == "foreign_form":
                page = page.replace('action="/auth"', 'action="http://192.0.2.7:1234/foreign"')
            elif self.fault == "duplicate_csrf":
                page = page.replace("</form>", '<input type="hidden" name="csrf_token" value="' + self.csrf + '"></form>')
            elif self.fault == "null_type":
                page = page.replace('type="hidden"', 'type', 1)
            elif self.fault == "null_value":
                page = page.replace('value="' + self.csrf + '"', 'value', 1)
            elif self.fault == "null_method":
                page = page.replace('method="POST"', 'method')
            return request.respond(200, page, "text/html")
        if path == "/auth" and method == "POST":
            self.mutations.append((path, self.store.load()))
            if self.user is None:
                assert self.store.value is not None and self.store.value["phase"] == "prepared"
                assert data["password_repeat"] == data["password"]
                self.user = (data["username"], data["password"])
            elif self.user != (data["username"], data["password"]):
                return request.respond(200, self.page(path, self.auth_params), "text/html")
            if self.fault == "lost_signup":
                request.connection.shutdown(socket.SHUT_RDWR)
                request.close_connection = True
                return
            state = data["state"] if self.fault != "bad_state" else "wrong"
            redirect = self.origin + "?" + urlencode({"code": "a" * 32, "state": state})
            if self.fault == "foreign_redirect":
                redirect = redirect.replace(self.origin, "http://192.0.2.7:1234")
            return request.respond(302, headers={"Location": redirect})
        if path == "/rest/auth/token":
            if self.fault == "exchange_unavailable":
                return request.respond(503)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode("ascii")).digest()).decode("ascii").rstrip("=")
            assert challenge == self.auth_params["code_challenge"]
            assert data["client_id"] == self.origin == data["redirect_uri"]
            refresh = "f" * 32
            self.sessions.add(refresh)
            result = {"access_token": "header.payload.signature", "refresh_token": refresh, "token_type": "bearer",
                      "expires_in": 3600, "scope": "admin", "user": {"name": self.user[0], "roles": ["administrator"]}}
            if self.fault == "wrong_user":
                result["user"]["name"] = "someoneelse"
            return request.respond(200, json.dumps(result), "application/json")
        if path == "/rest/auth/logout":
            self.sessions.discard(data["refresh_token"])
            return request.respond(200)
        if path == "/rest/auth/apitokens" and method == "GET":
            bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if bearer != "header.payload.signature" and bearer not in self.tokens.values():
                return request.respond(401)
            if self.fault == "duplicate_json":
                return request.respond(200, '[{"name":"x","name":"y","scope":""}]', "application/json")
            result = [{"name": name, "createdTime": 1, "scope": ""} for name in self.tokens]
            return request.respond(200, json.dumps(result), "application/json")
        if path.startswith("/rest/auth/apitokens/") and method == "DELETE":
            name = path.rsplit("/", 1)[1]
            self.mutations.append((path, self.store.load()))
            self.tokens.pop(name, None)
            return request.respond(200)
        if path == "/createApiToken" and method == "GET":
            return request.respond(200, self.page(path), "text/html")
        if path == "/createApiToken" and method == "POST":
            assert self.store.value["phase"] == "issuing" and self.store.value["token"] is None
            assert "token_scope" in data and data["token_scope"] == ""
            assert self.user == (data["username"], data["password"])
            self.mutations.append((path, self.store.load()))
            name = data["token_name"]
            if name in self.tokens:
                return request.respond(200, self.page(path), "text/html")
            token = "oh." + name + "." + "R" * 80
            self.tokens[name] = token
            if self.fault == "lost_token":
                request.connection.shutdown(socket.SHUT_RDWR)
                request.close_connection = True
                return
            result = self.page(path, result=token)
            if self.fault == "multiple_tokens":
                result += "<code>untrusted result</code>"
            return request.respond(200, result, "text/html")
        request.respond(404)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.runtime = StandIn(self.store)
        self.addCleanup(self.runtime.close)

    def provision(self, **overrides):
        values = {"load": self.store.load, "save": self.store.save, "publish": self.store.publish,
                  "deadline": time.monotonic() + 3}
        return auth.provision(self.runtime.origin, **(values | overrides))

    def test_fresh_signup_records_before_mutation_validates_before_publish_and_retires_session(self):
        token = self.provision()
        self.assertEqual(token, self.store.published)
        self.assertEqual([value["phase"] for value in self.store.history], ["prepared", "issuing", "recorded", "published"])
        self.assertEqual(self.store.value["token"], token)
        self.assertEqual(self.runtime.sessions, set())
        self.assertEqual(len(self.runtime.tokens), 1)
        before = list(self.runtime.mutations)
        self.assertEqual(self.provision(), token)
        self.assertEqual(self.runtime.mutations, before)
        self.store.published = None
        self.runtime.fault = "uuid_auth"
        self.assertEqual(self.provision(), token)
        self.assertEqual(self.store.published, token)
        self.assertEqual(self.runtime.mutations, before)

    def test_existing_unmanaged_accounts_are_not_adopted(self):
        self.runtime.user = ("existing", "private existing password")
        with self.assertRaises(auth.NativeAuthError) as refused:
            self.provision()
        self.assertEqual(refused.exception.code, "existing_users")
        self.assertIsNone(self.store.value)
        self.assertEqual(self.runtime.mutations, [])

    def test_lost_signup_response_recovers_same_journal_credentials_without_recreation(self):
        self.runtime.fault = "lost_signup"
        with self.assertRaises(auth.NativeAuthError) as unknown:
            self.provision()
        self.assertFalse(unknown.exception.retryable)
        saved, user = self.store.load(), self.runtime.user
        self.assertEqual(saved["phase"], "prepared")
        self.runtime.fault = None
        self.provision()
        self.assertEqual(self.runtime.user, user)
        self.assertEqual(self.store.value["username"], saved["username"])

    def test_lost_token_response_revokes_only_owned_orphan_then_recreates_once(self):
        self.runtime.fault = "lost_token"
        with self.assertRaises(auth.NativeAuthError):
            self.provision()
        saved = self.store.load()
        self.assertEqual(saved["phase"], "issuing")
        self.assertIsNone(saved["token"])
        self.runtime.tokens["unrelated"] = "untouched private fixture"
        self.runtime.fault = None
        token = self.provision()
        deleted = [path for path, _ in self.runtime.mutations if path.startswith("/rest/auth/apitokens/")]
        self.assertEqual(deleted, ["/rest/auth/apitokens/" + saved["token_name"]])
        self.assertEqual(self.runtime.tokens["unrelated"], "untouched private fixture")
        self.assertEqual(self.runtime.tokens[saved["token_name"]], token)

    def test_publish_failure_preserves_plaintext_and_republishes_without_remote_mutation(self):
        self.store.fail_publish = True
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "storage")
        saved, before = self.store.load(), list(self.runtime.mutations)
        self.assertEqual(saved["phase"], "recorded")
        self.store.fail_publish = False
        self.assertEqual(self.provision(), saved["token"])
        self.assertEqual(self.runtime.mutations, before)

    def test_post_publication_save_failure_recovers_same_token_without_rotation(self):
        self.store.fail_save = "published"
        with self.assertRaises(auth.NativeAuthError):
            self.provision()
        saved, before = self.store.load(), list(self.runtime.mutations)
        self.assertEqual(saved["phase"], "recorded")
        self.assertEqual(saved["token"], self.store.published)
        self.store.fail_save = None
        self.assertEqual(self.provision(), saved["token"])
        self.assertEqual(self.runtime.mutations, before)

    def test_unowned_reserved_name_collision_is_never_promoted_to_issuing_or_deleted(self):
        self.store.fail_save = "issuing"
        with self.assertRaises(auth.NativeAuthError):
            self.provision()
        self.assertEqual(self.store.value["phase"], "prepared")
        name = self.store.value["token_name"]
        self.runtime.tokens[name] = "existing foreign fixture"
        self.store.fail_save = None
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "collision")
        self.assertEqual(self.runtime.tokens[name], "existing foreign fixture")
        self.assertEqual(self.store.value["phase"], "prepared")
        self.assertFalse(any(method == "DELETE" for method, _ in self.runtime.calls))

    def test_owned_password_refusal_is_a_safe_credentials_error_without_reset(self):
        self.store.fail_save = "issuing"
        with self.assertRaises(auth.NativeAuthError):
            self.provision()
        self.store.fail_save = None
        self.runtime.user = (self.runtime.user[0], "changed outside onboarding")
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "credentials")
        self.assertEqual(self.runtime.tokens, {})

    def test_rejected_recorded_token_never_rotates_or_resets_native_credentials(self):
        self.provision()
        self.runtime.tokens.clear()
        before = list(self.runtime.mutations)
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "credentials")
        self.assertEqual(self.runtime.mutations, before)

    def test_identity_change_prevents_credential_posts_and_publication(self):
        self.provision()
        self.runtime.uuid = "89bcfe67-e572-486c-b735-888b54f082f0"
        before = list(self.runtime.mutations)
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "identity")
        self.assertEqual(self.runtime.mutations, before)

    def test_storage_failure_prevents_corresponding_remote_mutation_or_publication(self):
        for phase, forbidden in (("prepared", "/auth"), ("issuing", "/createApiToken"), ("recorded", "publication")):
            with self.subTest(phase=phase):
                store, runtime = Store(), None
                runtime = StandIn(store)
                try:
                    store.fail_save = phase
                    with self.assertRaises(auth.NativeAuthError) as failed:
                        auth.provision(runtime.origin, load=store.load, save=store.save, publish=store.publish,
                                       deadline=time.monotonic() + 3)
                    self.assertEqual(failed.exception.code, "storage")
                    self.assertIsNone(store.published)
                    if forbidden != "publication":
                        self.assertFalse(any(path == forbidden for path, _ in runtime.mutations))
                finally:
                    runtime.close()

    def test_unexpected_forms_and_redirects_are_not_followed(self):
        for fault in ("redirect", "foreign_form", "duplicate_csrf", "null_type", "null_value", "null_method",
                      "bad_state", "foreign_redirect", "wrong_user", "multiple_tokens", "duplicate_json"):
            with self.subTest(fault=fault):
                self.runtime.fault = fault
                with self.assertRaises(auth.NativeAuthError):
                    self.provision()
                if fault in ("redirect", "foreign_form", "duplicate_csrf", "null_type", "null_value", "null_method"):
                    self.assertEqual(self.runtime.mutations, [])
                self.runtime.user = None
                self.runtime.tokens.clear()
                self.runtime.mutations.clear()
                self.store.value = None

    def test_oversized_body_and_drip_deadline_are_bounded(self):
        self.runtime.fault = "oversize"
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "oversize")
        self.runtime.fault = "oversize_headers"
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "oversize")
        for fault in ("drip", "header_drip"):
            with self.subTest(fault=fault):
                self.runtime.fault = fault
                started = time.monotonic()
                with self.assertRaises(auth.NativeAuthError) as failed:
                    self.provision(deadline=started + 0.15)
                self.assertEqual(failed.exception.code, "deadline")
                self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(self.runtime.mutations, [])

    def test_readiness_is_retryable_but_post_failure_is_not(self):
        for fault, retryable in (("unavailable", True), ("exchange_unavailable", False)):
            self.runtime.fault = fault
            with self.assertRaises(auth.NativeAuthError) as failed:
                self.provision()
            self.assertEqual(failed.exception.retryable, retryable)

    def test_deadline_expiring_after_probe_cannot_publish_or_mark_published(self):
        self.provision()
        self.store.published = None
        clock = mock.Mock(return_value=0)
        original = auth._token_valid
        def expire(client, journal):
            original(client, journal)
            clock.return_value = 2
        with mock.patch.object(auth, "_token_valid", side_effect=expire), self.assertRaises(auth.NativeAuthError) as failed:
            self.provision(clock=clock, deadline=1)
        self.assertEqual(failed.exception.code, "deadline")
        self.assertIsNone(self.store.published)
        clock.return_value = 0
        self.store.value["phase"] = "recorded"
        def publish(token):
            self.store.publish(token)
            clock.return_value = 2
        with self.assertRaises(auth.NativeAuthError):
            self.provision(clock=clock, deadline=1, publish=publish)
        self.assertEqual(self.store.value["phase"], "recorded")

    def test_errors_and_outputs_never_include_password_token_or_raw_dependency_details(self):
        self.store.fail_publish = True
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output, mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
            with self.assertRaises(auth.NativeAuthError) as failed:
                self.provision()
        text = str(failed.exception) + output.getvalue() + errors.getvalue()
        self.assertNotIn(self.store.value["password"], text)
        self.assertNotIn(self.store.value["token"], text)
        self.assertNotIn("private publication", text)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_nonloopback_origin_and_invalid_journal_fail_before_remote_mutation(self):
        for origin in ("http://localhost:8080", "http://192.0.2.2:8080", "https://127.0.0.1:8080",
                       "http://user:secret@127.0.0.1:8080", self.runtime.origin + "/path"):
            with self.subTest(origin=origin), self.assertRaises(auth.NativeAuthError):
                auth.provision(origin, load=self.store.load, save=self.store.save, publish=self.store.publish,
                               deadline=time.monotonic() + 1)
        untrusted_detail = "untrusted raw detail"
        self.store.value = {"version": 1, "password": untrusted_detail}
        with self.assertRaises(auth.NativeAuthError) as failed:
            self.provision()
        self.assertEqual(failed.exception.code, "journal")
        self.assertEqual(self.runtime.calls, [])

    def test_explicit_import_retired_journal_cannot_republish_or_contact_native(self):
        self.provision()
        self.store.value["phase"] = "retired"
        for token in (self.store.value["token"], None):
            with self.subTest(recorded_token=token is not None):
                self.store.value["token"] = token
                before = list(self.runtime.calls), list(self.runtime.mutations), self.store.published
                with self.assertRaises(auth.NativeAuthError) as failed:
                    self.provision()
                self.assertEqual(failed.exception.code, "retired")
                self.assertFalse(failed.exception.retryable)
                self.assertEqual((self.runtime.calls, self.runtime.mutations, self.store.published), before)


if __name__ == "__main__":
    unittest.main()
