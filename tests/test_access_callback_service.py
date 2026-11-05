"""Offline callback composition, private staging and pinned HTTP parser bounds."""

import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_ports
from oh_access import AccessError, callback_service as service, staging
from oh_access.service import load_config as load_access
from oh_access.files import atomic_private, json_private, private_directory

CALLBACK = "/upnpcallback/dev/RINCON_02000000000101400_MR/svc/upnp-org/RenderingControl/event/cb"
PEER = "192.0.2.40"
CONFIG = {"version": 1, "routes": {CALLBACK: PEER}, "upstream": "http://127.0.0.1:8080",
          "hosts": ["192.0.2.2:8088"], "bind_host": "192.0.2.2", "port": 8088}


class Configuration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = private_directory(temporary.name)
        self.path = self.root / "callbacks.json"
        json_private(self.path, CONFIG)

    def test_exact_private_configuration_loads_without_client_or_listener_or_state(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), patch.object(service, "header_protocol", side_effect=AssertionError("listener forbidden")):
            self.assertEqual(service.load_config(self.path, runtime_url="http://127.0.0.1:8080/"), CONFIG)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_runtime_loader_accepts_freed_default_and_retains_actual_upstream_collision_guard(self):
        callback_port = oh_ports.DEFAULTS["grafana"]
        config = CONFIG | {"upstream": oh_ports.NATIVE_URL, "port": callback_port,
                           "hosts": [CONFIG["bind_host"] + ":" + str(callback_port)]}
        json_private(self.path, config)
        self.assertEqual(service.load_config(self.path), config)
        json_private(self.path, config | {"port": oh_ports.HTTP_PORT,
                                         "hosts": [CONFIG["bind_host"] + ":" + str(oh_ports.HTTP_PORT)]})
        with self.assertRaises(AccessError):
            service.load_config(self.path)

    def test_strict_schema_exact_bind_host_port_routes_and_no_credentials(self):
        changes = ({"version": True}, {"version": 2}, {"extra": "synthetic private value"},
                   {"routes": {}}, {"routes": {CALLBACK + "*": PEER}}, {"routes": {CALLBACK: "peer.example"}},
                   {"bind_host": "0.0.0.0"}, {"bind_host": "127.0.0.1"}, {"bind_host": "::1"},
                   {"bind_host": "192.0.2.3"}, {"port": True}, {"port": 80}, {"port": 65536},
                   {"hosts": ["*:8088"]}, {"hosts": ["192.0.2.2:8089"]},
                   {"hosts": ["192.0.2.2:8088", "192.0.2.3:8088"]},
                   {"upstream": "http://192.0.2.3:8080"}, {"upstream": "http://127.0.0.1:8080/rest"},
                   {"upstream": "http://synthetic:private@127.0.0.1:8080"}, {"upstream": "http://127.0.0.1:8080?token=private"},
                   {"state": str(self.root)}, {"token_file": "private"})
        for change in changes:
            with self.subTest(change=change):
                json_private(self.path, CONFIG | change)
                with self.assertRaises(AccessError) as caught:
                    service.load_config(self.path)
                self.assertNotIn("synthetic", str(caught.exception))
                self.assertNotIn("token=", str(caught.exception))
        atomic_private(self.path, b'{"version":1,"version":1}')
        with self.assertRaises(AccessError):
            service.load_config(self.path)

    def test_private_permissions_symlink_and_mismatched_native_origin_fail(self):
        with self.assertRaises(AccessError):
            service.load_config(self.path, runtime_url="http://127.0.0.1:8181")
        self.path.chmod(0o644)
        with self.assertRaises(AccessError):
            service.load_config(self.path)
        self.path.chmod(0o600)
        linked = self.root / "link.json"
        linked.symlink_to(self.path)
        with self.assertRaises(AccessError):
            service.load_config(linked)

    def test_explicit_cli_callbacks_does_not_dispatch_site_or_worker(self):
        from oh_cli.access import main
        with patch.object(service, "serve") as serve, patch("sys.stdout", new_callable=io.StringIO), patch("oh_access.service.serve", side_effect=AssertionError("wrong service")):
            self.assertEqual(main(["callbacks", "--config", str(self.path)]), 0)
        serve.assert_called_once_with(self.path)

    def test_server_has_actual_header_deadline_and_no_forwarded_trust_logging_or_credentials(self):
        fake_uvicorn = type(sys)("uvicorn")
        fake_uvicorn.run = Mock()
        protocol = object()
        with patch.object(service, "require_service_account"), patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), patch.object(service, "header_protocol", return_value=protocol):
            service.serve(self.path)
        app = fake_uvicorn.run.call_args.args[0]
        options = fake_uvicorn.run.call_args.kwargs
        self.assertEqual(app.routes, CONFIG["routes"])
        self.assertIsNone(app.client)
        self.assertEqual((options["host"], options["port"]), (CONFIG["bind_host"], CONFIG["port"]))
        self.assertIs(options["http"], protocol)
        self.assertEqual(options["lifespan"], "on")
        for key in ("proxy_headers", "access_log", "server_header", "date_header"):
            self.assertFalse(options[key])
        self.assertEqual(options["forwarded_allow_ips"], "")
        self.assertEqual(options["ws"], "none")
        self.assertIn(("Connection", "close"), options["headers"])
        self.assertEqual((options["h11_max_incomplete_event_size"], options["limit_concurrency"]), (16384, 32))
        self.assertEqual((options["timeout_keep_alive"], options["timeout_graceful_shutdown"]), (1, 5))
        self.assertEqual((app.timeout, app.receive_timeout), (3, 3))

    def test_composed_relay_lifespan_starts_and_closes_dedicated_transport(self):
        app = service.CallbackRelay(CONFIG["routes"], CONFIG["upstream"], CONFIG["hosts"])
        client = Mock()
        client.aclose = AsyncMock()
        fake_httpx = type(sys)("httpx")
        fake_httpx.AsyncClient, fake_httpx.Timeout, fake_httpx.Limits = Mock(return_value=client), Mock(), Mock()
        events = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
        sent = []
        async def receive(): return next(events)
        async def send(value): sent.append(value)
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            asyncio.run(app({"type": "lifespan"}, receive, send))
        self.assertEqual(sent, [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}])
        client.aclose.assert_awaited_once()
        self.assertIsNone(app.client)
        options = fake_httpx.AsyncClient.call_args.kwargs
        self.assertFalse(options["trust_env"])
        self.assertFalse(options["follow_redirects"])
        self.assertNotIn("auth", options)
        self.assertNotIn("headers", options)

    def fixture_stage(self, name, **overrides):
        source, assets = self.root / "source", self.root / "assets"
        if not source.exists():
            (source / "lib").mkdir(parents=True)
            (source / "bin").mkdir()
            (source / "lib/requirements-site.txt").write_text("synthetic fixture\n")
            (source / "bin/thermite").write_text("synthetic fixture\n")
            assets.mkdir()
            from oh_features.remote import FILES
            for filename in FILES:
                (assets / filename).write_text("synthetic private asset\n")
            atomic_private(self.root / "bearer", b"synthetic native bearer")
            atomic_private(self.root / "key", b"synthetic key")
            (self.root / "cert").write_text("synthetic cert")
        def compile_fixture(house, asset_root, destination):
            json_private(destination, {"rooms": {}})
        inputs = dict(state=self.root / "site-state", plan_directory=self.root / (name + "-plan"), house="synthetic.yaml", assets=assets,
                      runtime_url=CONFIG["upstream"], runtime_token_file=self.root / "bearer",
                      tls_key=self.root / "key", tls_cert=self.root / "cert", allowed_hosts=["site.example"],
                      bind_host="127.0.0.1", port=9443, timezone="UTC", area=source, install_environment=False)
        # Isolate permanent-location policy for disposable temporary fixtures;
        # real private file custody and callback validation remain unpatched.
        with patch.object(staging, "_disposable", return_value=False), patch.object(staging, "_state_location"), patch.object(staging, "_service_parents"), patch("oh_access.service.compile_contract", side_effect=compile_fixture), patch("oh_access.service.ssl.SSLContext.load_cert_chain"):
            return staging.stage(self.root / name, **(inputs | overrides))

    def test_freed_site_ports_survive_full_staging_and_both_runtime_loaders(self):
        allocated = oh_ports.effective({"metrics": {"grafana_port": 14040}})
        for number, (access_port, callback_port) in enumerate(((13611, None), (13601, 13611), (13611, 13601))):
            with self.subTest(access=access_port, callbacks=callback_port):
                if callback_port is not None:
                    config = CONFIG | {"upstream": oh_ports.NATIVE_URL, "port": callback_port,
                                       "hosts": [CONFIG["bind_host"] + ":" + str(callback_port)]}
                    json_private(self.path, config)
                receipt = self.fixture_stage("freed-" + str(number), runtime_url=oh_ports.NATIVE_URL,
                                             port=access_port, callbacks=self.path if callback_port is not None else None,
                                             host_ports=allocated)
                self.assertTrue(receipt["staged"])
                self.assertFalse(receipt["activated"])
                self.assertEqual(load_access(receipt["config"], tls=False)["port"], access_port)
                if callback_port is not None:
                    callback_path = Path(receipt["destination"]) / "callbacks.json"
                    self.assertEqual(service.load_config(callback_path), config)

    def test_optional_private_stage_adds_only_third_unit_and_no_state_write_allowance(self):
        plain = self.fixture_stage("release-default")
        self.assertEqual(len(plain["units"]), 2)
        self.assertFalse(plain["callbacks_staged"])
        self.assertFalse((self.root / "release-default/callbacks.json").exists())
        receipt = self.fixture_stage("release-opt-in", callbacks=self.path)
        self.assertEqual(len(receipt["units"]), 3)
        self.assertTrue(receipt["callbacks_staged"])
        self.assertFalse(receipt["callbacks_activated"])
        self.assertFalse(receipt["activated"])
        config = self.root / "release-opt-in/callbacks.json"
        self.assertEqual(service.load_config(config), CONFIG)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        unit = (self.root / "release-opt-in/units/thermite-access-callbacks.service").read_text()
        self.assertIn('"callbacks" "--config"', unit)
        for field in ("User=thermite", "Group=thermite", "Slice=thermite.slice", "UMask=0077"):
            self.assertIn("\n" + field + "\n", unit)
        self.assertIn("callbacks.json", unit)
        self.assertNotIn("ReadWritePaths", unit)
        self.assertNotIn("runtime-bearer", unit)
        self.assertNotIn("tls-key", unit)
        self.assertEqual(receipt["persistent_state"], plain["persistent_state"])
        self.assertEqual(list((self.root / "site-state").iterdir()), [])
        self.assertNotIn(CALLBACK, json.dumps(receipt))
        self.assertIn("SUBSCRIBE", " ".join(receipt["activation_requirements"]))

    def test_staging_refuses_mismatched_upstream_before_creating_release_or_state(self):
        with self.assertRaisesRegex(AccessError, "callback configuration"):
            self.fixture_stage("bad", callbacks=self.path, runtime_url="http://127.0.0.1:8181")
        self.assertFalse((self.root / "bad").exists())
        self.assertFalse((self.root / "bad-plan").exists())
        self.assertFalse((self.root / "site-state").exists())

    def test_stage_cli_forwards_optional_config_without_enabling_a_listener(self):
        from oh_cli import access as cli
        # Staging itself is replaced, so these paths are never created or read.
        args = ["stage", "--destination", str(self.root / "release"), "--state", str(self.root / "site-state"),
                "--plan-directory", str(self.root / "operator-plan"),
                "--assets", str(self.root / "assets"), "--runtime-url", CONFIG["upstream"],
                "--runtime-token-file", str(self.root / "bearer"), "--tls-cert", str(self.root / "cert"),
                "--tls-key", str(self.root / "key"), "--allowed-host", "site.example", "--callbacks", str(self.path)]
        allocated = oh_ports.effective()
        # The selected site's instance/host.yaml is not an input of this test.
        with patch.object(cli, "_site_ports", return_value=allocated), patch.object(staging, "stage", return_value={"activated": False}) as stage, patch("sys.stdout", new_callable=io.StringIO), patch.object(service, "serve", side_effect=AssertionError("listener forbidden")):
            self.assertEqual(cli.main(args), 0)
        self.assertEqual(stage.call_args.kwargs["callbacks"], self.path)
        self.assertEqual(stage.call_args.kwargs["state"], self.root / "site-state")
        self.assertEqual(stage.call_args.kwargs["host_ports"], allocated)
        self.assertEqual(list(self.root.iterdir()), [self.path])


@unittest.skipUnless(importlib.util.find_spec("uvicorn"), "optional pinned service environment required")
class Protocol(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from uvicorn import Config
        from uvicorn.server import ServerState
        self.loop = asyncio.get_running_loop()
        self.timers = []
        class Clock:
            def call_later(clock, delay, callback):
                timer = Mock()
                timer.delay, timer.callback = delay, callback
                self.timers.append(timer)
                return timer
            def create_task(clock, coro): return self.loop.create_task(coro)
        self.clock = Clock()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
        self.config = Config(app, http=service.header_protocol(), ws="none", lifespan="off", proxy_headers=False,
                             access_log=False, server_header=False, log_config=None,
                             h11_max_incomplete_event_size=16384, headers=[("Connection", "close")])
        self.config.load()
        self.state = ServerState()
        self.state.default_headers = self.config.encoded_headers
        self.connections = []

    def connection(self):
        class Transport:
            closing, data = False, b""
            def get_extra_info(transport, name):
                return {"peername": (PEER, 1400), "sockname": ("192.0.2.2", 8088)}.get(name)
            def is_closing(transport): return transport.closing
            def close(transport): transport.closing = True
            def write(transport, data): transport.data += data
            def pause_reading(transport): pass
            def resume_reading(transport): pass
        protocol = self.config.http_protocol_class(self.config, self.state, {}, _loop=self.clock)
        transport = Transport()
        protocol.connection_made(transport)
        self.connections.append((protocol, transport))
        return protocol, transport

    async def asyncTearDown(self):
        if self.state.tasks:
            await asyncio.gather(*self.state.tasks)
        for protocol, transport in self.connections:
            protocol.connection_lost(None)
        self.assertFalse(self.state.connections)
        self.assertFalse(self.state.tasks)

    async def test_partial_initial_headers_keep_fixed_deadline_and_close_on_expiry(self):
        protocol, transport = self.connection()
        timer = self.timers[0]
        self.assertEqual(timer.delay, 3)
        protocol.data_received(b"NOTIFY /upnpcallback HTTP/1.1\r\nHost: ")
        self.assertIsNone(protocol.cycle)
        timer.cancel.assert_not_called()
        self.assertEqual(len(self.timers), 1)  # Dripping bytes do not extend it.
        timer.callback()
        self.assertTrue(transport.closing)

    async def test_complete_headers_cancel_timer_and_each_response_closes_connection(self):
        protocol, transport = self.connection()
        protocol.data_received(b"NOTIFY /upnpcallback HTTP/1.1\r\nHost: 192.0.2.2:8088\r\nContent-Length: 0\r\n\r\n")
        self.timers[0].cancel.assert_called_once()
        self.assertIsNone(protocol.header_deadline)
        await asyncio.gather(*self.state.tasks)
        self.assertIn(b"200 OK", transport.data)
        self.assertTrue(transport.closing)

    async def test_header_size_and_connection_count_are_bounded_before_asgi(self):
        protocol, transport = self.connection()
        with patch.object(protocol.logger, "warning"):
            protocol.data_received(b"NOTIFY / HTTP/1.1\r\nX-Long: " + b"x" * 17000)
        self.assertIn(b"400 Bad Request", transport.data)
        self.assertTrue(transport.closing)
        for _ in range(service.CONNECTIONS):
            last, last_transport = self.connection()
        self.assertTrue(last_transport.closing)
        self.assertIsNone(last.header_deadline)


if __name__ == "__main__":
    unittest.main()
