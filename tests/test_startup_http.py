"""Startup HTTP deadlines against bounded loopback fixtures; no JVM runs."""

import contextlib
import json
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_http
import oh_sandbox as lib
from oh_install.system import NativeProbe, System


class StartupHttp(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inst = lib.Instance("startup-test", self.root, self.root / "conf", self.root / "userdata",
                                 13619, 13620, 13621, shutdown_port=13626)
        self.inst.conf.mkdir()
        self.inst.userdata.mkdir()
        self.rt = lib.Runtime("5.2.3", self.root / "distribution", self.root / "jre")
        lib.write_secret(self.inst.token_file, "fixture-existing-token")
        self.jvm = mock.patch.object(lib.subprocess, "Popen", side_effect=AssertionError("no JVM permitted"))
        self.jvm.start()
        self.addCleanup(self.jvm.stop)

    @contextlib.contextmanager
    def server(self, respond):
        stopping = threading.Event()
        requests = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                try:
                    route = self.rfile.readline().split()[1].decode()
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    requests.append(route)
                    respond(route, self.connection, stopping)
                except OSError:
                    # Clients deliberately close unfinished responses at expiry.
                    pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        worker.start()
        try:
            self.inst.http_port = server.server_address[1]
            with mock.patch.object(oh_http.urllib.request, "getproxies", return_value={}):
                yield requests
        finally:
            stopping.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "loopback HTTP fixture did not stop")

    @staticmethod
    def trickle(connection, stopping, phase):
        prefix = (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nX-Slow: " if phase == "headers" else
                  b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        connection.sendall(prefix)
        for _ in range(100):
            connection.sendall(b" ")
            if stopping.wait(.03):
                return
        connection.sendall(b"\r\n\r\n" if phase == "headers" else b"")

    @staticmethod
    def reply(connection, value):
        body = json.dumps(value).encode()
        connection.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)

    def bounded_failure(self, operation):
        started = time.monotonic()
        with self.assertRaisesRegex(lib.SandboxError, "deadline"):
            operation(started + .2)
        self.assertLess(time.monotonic() - started, .8, "startup HTTP read outlived its deadline")

    def test_slow_token_headers_and_body_preserve_existing_token_without_karaf_commands(self):
        before = self.inst.token_file.stat()
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), self.server(
                    lambda route, connection, stopping: self.trickle(connection, stopping, phase)) as requests, \
                    mock.patch.object(lib, "_wait_karaf"), mock.patch.object(lib, "karaf_command") as command:
                self.bounded_failure(lambda deadline: lib.provision_token(self.rt, self.inst, deadline=deadline))
                self.assertEqual(requests, ["/rest/addons?serviceId=karaf"])
                command.assert_not_called()
                self.assertEqual(self.inst.token_file.read_text(), "fixture-existing-token\n")
                after = self.inst.token_file.stat()
                self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_rest_registration_uses_the_original_deadline_for_headers_and_body(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), self.server(
                    lambda route, connection, stopping: self.trickle(connection, stopping, phase)) as requests, \
                    mock.patch.object(lib, "group_members", return_value=[123]):
                self.bounded_failure(lambda deadline: lib.wait_rest(self.inst, 123, 60, deadline=deadline))
                self.assertEqual(requests, ["/rest/"])

    def test_addon_wait_bounds_a_client_that_has_no_deadline(self):
        with self.server(lambda route, connection, stopping: self.trickle(connection, stopping, "body")):
            client = lib.Http(self.inst.url)
            self.bounded_failure(lambda deadline: lib.wait_addons(client, {"binding-http"}, deadline))

    def test_boot_addons_and_handler_recovery_share_the_startup_deadline(self):
        addons = "/rest/addons?serviceId=all"
        things = "/rest/things?summary=true"
        for slow_route in (addons, things):
            for phase in ("headers", "body"):
                def respond(route, connection, stopping):
                    if route == slow_route:
                        self.trickle(connection, stopping, phase)
                    else:
                        self.reply(connection, [{"uid": "binding-http", "installed": True}])

                with self.subTest(route=slow_route, phase=phase), self.server(respond) as requests, \
                        mock.patch.object(lib, "warm_template", return_value=(self.root / "template", False)), \
                        mock.patch.object(lib, "restore_template"), \
                        mock.patch.object(lib, "start_instance", return_value=123) as start, \
                        mock.patch.object(lib, "wait_rest"), \
                        mock.patch.object(lib, "provision_token", return_value=self.inst.token_file), \
                        mock.patch.object(lib, "stop_instance") as stop:
                    self.bounded_failure(lambda deadline: lib.boot(
                        self.rt, self.inst, {"binding": ["http"]}, deadline - .2, deadline, log=lambda _: None))
                    self.assertEqual(requests, [addons] if slow_route == addons else [addons, things])
                    self.assertEqual(start.call_count, 1, "an expired readiness probe restarted the runtime")
                    stop.assert_not_called()

    def test_native_probe_uses_shared_transport_for_slow_headers_and_body(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), self.server(
                    lambda route, connection, stopping: self.trickle(connection, stopping, phase)):
                started = time.monotonic()
                result = System().native_probe(self.inst.url, "fixture-token", "/rest/", started + .2)
                self.assertEqual(result, NativeProbe(0, problem="startup deadline expired"))
                self.assertLess(time.monotonic() - started, .8)

    def test_template_build_stops_after_its_addon_read_deadline(self):
        services = self.inst.conf / "services"
        services.mkdir()
        (services / "addons.cfg").write_text("binding=http\n")
        wipe = lib.wipe_dir
        def provision(*args, **kwargs):
            lib.write_secret(self.inst.token_file, "fixture-template-token")
            return self.inst.token_file
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), self.server(
                    lambda route, connection, stopping: self.trickle(connection, stopping, phase)) as requests, \
                    mock.patch.object(lib, "wipe_dir", side_effect=lambda path, **kwargs: wipe(path, root=self.root)), \
                    mock.patch.object(lib, "start_instance", return_value=123), mock.patch.object(lib, "wait_rest"), \
                    mock.patch.object(lib, "provision_token", side_effect=provision), \
                    mock.patch.object(lib, "stop_instance") as stop:
                self.bounded_failure(lambda deadline: lib.warm_template(self.rt, self.inst, deadline, log=lambda _: None))
                self.assertEqual(requests, ["/rest/addons?serviceId=all"])
                stop.assert_called_once()
                self.assertFalse((self.root / "template").exists(), "expired startup published a warmed template")

    def test_first_boot_recovery_does_not_restart_after_the_budget_expires(self):
        now = [0.0]
        client = lib.Http(self.inst.url, deadline=.25)
        pending = [{"UID": "binding:example", "statusInfo": {"status": "UNINITIALIZED", "statusDetail": "NOT_YET_READY"}}]
        def sleep(seconds):
            now[0] += seconds
        with mock.patch.object(lib.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(lib.time, "sleep", side_effect=sleep), \
                mock.patch.object(client, "try_get_json", return_value=(200, pending)), \
                mock.patch.object(lib, "stop_instance") as stop, mock.patch.object(lib, "start_instance") as start:
            with self.assertRaisesRegex(lib.SandboxError, "deadline"):
                lib.unstick_first_boot(self.rt, self.inst, 123, client, .25, grace=60, log=lambda _: None)
        self.assertEqual(now[0], .25)
        stop.assert_not_called()
        start.assert_not_called()

    def test_recovery_shutdown_uses_remaining_budget_and_cannot_launch_late(self):
        now = [0.0]
        client = lib.Http(self.inst.url, deadline=.25)
        def stopped(inst, *, timeout, log):
            self.assertEqual(timeout, .25)
            now[0] += timeout
        with mock.patch.object(lib.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(lib, "stop_instance", side_effect=stopped) as stop, \
                mock.patch.object(lib, "start_instance") as start:
            with self.assertRaisesRegex(lib.SandboxError, "deadline"):
                lib.unstick_first_boot(self.rt, self.inst, 123, client, .25, grace=0, log=lambda _: None)
        stop.assert_called_once()
        start.assert_not_called()

    def test_late_success_does_not_recreate_an_existing_token(self):
        now = [0]
        def late(*args, **kwargs):
            now[0] = 2
            return True
        with mock.patch.object(lib.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(lib, "_wait_karaf"), mock.patch.object(lib, "token_works", side_effect=late), \
                mock.patch.object(lib, "karaf_command") as command:
            with self.assertRaisesRegex(lib.SandboxError, "deadline"):
                lib.provision_token(self.rt, self.inst, deadline=1)
        command.assert_not_called()
        self.assertEqual(self.inst.token_file.read_text(), "fixture-existing-token\n")

    def test_expired_request_never_opens_a_connection(self):
        client = lib.Http(self.inst.url, deadline=time.monotonic() - 1)
        with mock.patch.object(oh_http.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(lib.SandboxError, "deadline"):
                client.try_get_json("/rest/")
        opener.return_value.open.assert_not_called()

    def test_ordinary_http_and_deadline_http_accept_complete_responses(self):
        def respond(route, connection, stopping):
            self.reply(connection, {"ready": True})
        with self.server(respond):
            for deadline in (None, time.monotonic() + 2):
                with self.subTest(deadline=deadline):
                    self.assertEqual(lib.Http(self.inst.url, deadline=deadline).try_get_json("/rest/"),
                                     (200, {"ready": True}))


if __name__ == "__main__":
    unittest.main()
