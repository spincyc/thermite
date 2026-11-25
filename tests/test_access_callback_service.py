"""Offline callback composition, private staging and pinned HTTP parser bounds."""

import asyncio
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_ports
from oh_access import AccessError, callback_service as service, files, staging
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
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), patch.object(service, "listener", side_effect=AssertionError("listener forbidden")):
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
            self.assertEqual(main(["callbacks", "--descriptor", str(self.path)]), 0)
        serve.assert_called_once_with(self.path)

    def test_server_has_actual_header_deadline_and_no_forwarded_trust_logging_or_credentials(self):
        from oh_access import service as access_service
        import logging

        with patch.object(service, "require_service_account"), \
                patch.object(access_service, "_run_server") as run_server:
            service.serve(self.path)
        run_server.assert_called_once()
        [(app, config)] = run_server.call_args.args[0]
        self.assertEqual(app.routes, CONFIG["routes"])
        self.assertIsNone(app.client)
        self.assertEqual(config.bind, [f"{CONFIG['bind_host']}:{CONFIG['port']}"])
        self.assertFalse(config.ssl_enabled)
        self.assertIsNone(config.accesslog)
        self.assertIs(config.errorlog, logging.getLogger("hypercorn.error"))
        self.assertIsNone(config.logconfig)
        self.assertIsNone(config.logconfig_dict)
        self.assertFalse(config.include_server_header)
        self.assertFalse(config.include_date_header)
        self.assertEqual(config.response_headers("h11"), [])
        self.assertEqual((config.keep_alive_timeout, config.keep_alive_max_requests, config.graceful_timeout), (3, 1, 5))
        self.assertEqual((config.h11_max_incomplete_size, config.h2_max_header_list_size), (16384, 16384))
        self.assertEqual((config.h2_max_concurrent_streams, config.backlog, config.workers), (1, 64, 1))
        self.assertEqual(config.connection_limit.limit, 32)
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
        def compile_fixture(house, asset_root, destination, **_):
            json_private(destination, {"rooms": {}})
        inputs = dict(state=self.root / "site-state", plan_directory=self.root / (name + "-plan"), house="synthetic.yaml", assets=assets,
                      runtime_url=CONFIG["upstream"], runtime_token_file=self.root / "bearer",
                      tls_key=self.root / "key", tls_cert=self.root / "cert", allowed_hosts=["site.example"],
                      listen=["127.0.0.1"], port=9443, timezone="UTC", area=source, application=source,
                      install_environment=False,
                      image="registry.example/python@sha256:" + "5" * 64)
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
        unit = (self.root / "release-opt-in/units/thermite-access-callbacks.container").read_text()
        self.assertIn(" access callbacks --descriptor ", unit)
        # The command the receipt hands the installer is one this release's
        # own access verb accepts, and the receipt addresses nobody else.
        import oh_cli
        for name, verb in (("verification_command", "verify"), ("migration_command", "migrate")):
            command = receipt[name]
            parsed = oh_cli.parser_of("access").parse_args(command[command.index("access") + 1:])
            self.assertEqual((parsed.command, parsed.descriptor), (verb, self.root / "release-opt-in/config.json"))
        self.assertFalse({"activation_commands", "owner_commands", "bootstrap_command",
                          "activation_requirements"} & set(receipt))
        account = files.service_identity()
        for field in (f"User={account[0]}", f"Group={account[1]}", "Slice=thermite.slice", "UMask=0077",
                      f"Environment=THERMITE_SERVICE_IDENTITY={account[0]}:{account[1]}"):
            self.assertIn("\n" + field + "\n", unit)
        self.assertIn("callbacks.json", unit)
        # The relay is shown the program, its environment and its own map:
        # no state, no secret, and nothing of the release that holds one.
        release = self.root / "release-opt-in"
        self.assertEqual([line for line in unit.splitlines() if line.startswith("Volume=")],
                         [f"Volume={release / part}:{release / part}:ro,z"
                          for part in ("program", "env", "callbacks.json")])
        self.assertNotIn("site-state", unit)
        self.assertNotIn("runtime-bearer", unit)
        self.assertNotIn("tls-key", unit)
        self.assertNotIn("secrets", unit)
        # The plan holds the protected copy of every file the host installs,
        # and the receipt the commands that install exactly those.
        plan = self.root / "release-opt-in-plan/units"
        self.assertEqual({path.name for path in plan.iterdir()},
                         {"thermite-access.container", "thermite-access.service.d.conf", "thermite-access-worker.container",
                          "thermite-access-worker.service.d.conf", "thermite-access-callbacks.container",
                          "thermite-access-callbacks.service.d.conf"})
        self.assertEqual([command[-2:] for command in receipt["unit_install_commands"] if "-d" not in command],
                         [[source, destination] for name in receipt["units"]
                          for source, destination in receipt["unit_files"][name]])
        self.assertEqual(
            [command[-1] for command in receipt["unit_install_commands"]],
            ["/etc/containers/systemd/thermite-access.container",
             "/etc/systemd/system/thermite-access.service.d",
             "/etc/systemd/system/thermite-access.service.d/50-container-lifecycle.conf",
             "/etc/containers/systemd/thermite-access-worker.container",
             "/etc/systemd/system/thermite-access-worker.service.d",
             "/etc/systemd/system/thermite-access-worker.service.d/50-container-lifecycle.conf",
             "/etc/containers/systemd/thermite-access-callbacks.container",
             "/etc/systemd/system/thermite-access-callbacks.service.d",
             "/etc/systemd/system/thermite-access-callbacks.service.d/50-container-lifecycle.conf"])
        self.assertEqual(receipt["containers"], {"thermite-access.service": "thermite-access",
                                                 "thermite-access-worker.service": "thermite-access-worker",
                                                 "thermite-access-callbacks.service": "thermite-access-callbacks"})
        self.assertEqual(receipt["image"], "registry.example/python@sha256:" + "5" * 64)
        self.assertEqual(receipt["persistent_state"], plain["persistent_state"])
        self.assertEqual(list((self.root / "site-state").iterdir()), [])
        self.assertNotIn(CALLBACK, json.dumps(receipt))

    def test_staging_refuses_mismatched_upstream_before_creating_release_or_state(self):
        with self.assertRaisesRegex(AccessError, "callback configuration"):
            self.fixture_stage("bad", callbacks=self.path, runtime_url="http://127.0.0.1:8181")
        self.assertFalse((self.root / "bad").exists())
        self.assertFalse((self.root / "bad-plan").exists())
        self.assertFalse((self.root / "site-state").exists())


@unittest.skipUnless(importlib.util.find_spec("hypercorn"), "optional pinned service environment required")
class Protocol(unittest.IsolatedAsyncioTestCase):
    class _Running:
        """Run oh_access.service._serve in its own thread & event loop, just
        as the real service process does, so its task-cleanup teardown stays
        isolated from the test loop."""

        def __init__(self, app, config):
            self.app, self.config = app, config
            self.sockets = config.create_sockets()
            self.port = self.sockets.insecure_sockets[0].getsockname()[1]
            self.started = asyncio.Event()
            self._ready = __import__("threading").Event()
            self._thread = None
            self._loop = None
            self._stop = None

        def start(self):
            import threading
            self._thread = threading.Thread(target=asyncio.run, args=(self._main(),), daemon=True)
            self._thread.start()
            assert self._ready.wait(5), "callback test server failed to start"
            return self

        async def _main(self):
            from oh_access import service as access_service
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._ready.set()
            await access_service._serve([(self.app, self.config, self.sockets)], stop=self._stop)

        def stop(self):
            if self._loop is not None and self._stop is not None:
                self._loop.call_soon_threadsafe(self._stop.set)
            if self._thread is not None:
                self._thread.join(10)
                assert not self._thread.is_alive(), "callback test server did not stop cleanly"

    async def start_server(self, *, connections=service.CONNECTIONS, header_seconds=0.35):
        self.seen = []
        self.lifespan_events = []

        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    event = await receive()
                    self.lifespan_events.append(event["type"])
                    if event["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif event["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            self.seen.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})

        config = service.listener("127.0.0.1", 0, connections=connections, header_seconds=header_seconds,
                                  graceful_seconds=1)
        running = self._Running(app, config).start()
        for _ in range(100):
            if "lifespan.startup" in self.lifespan_events:
                break
            await asyncio.sleep(0.01)
        return running.port, config, running

    async def stop_server(self, running):
        await asyncio.to_thread(running.stop)

    async def test_partial_initial_headers_keep_fixed_deadline_and_close_on_expiry(self):
        port, config, running = await self.start_server(header_seconds=0.25)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"NOTIFY /upnpcallback HTTP/1.1\r\nHost: ")
            await writer.drain()
            # Dripping bytes before the deadline must not extend it.
            await asyncio.sleep(0.12)
            writer.write(b"127.0.0.1")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), timeout=1.5)
            self.assertEqual(data, b"")
            self.assertEqual(self.seen, [])
            writer.close()
            await writer.wait_closed()
        finally:
            await self.stop_server(running)
        self.assertEqual(self.lifespan_events, ["lifespan.startup", "lifespan.shutdown"])
        self.assertEqual(config.connection_limit.active, 0)

    async def test_complete_headers_answer_without_server_or_date_and_close_connection(self):
        port, config, running = await self.start_server(header_seconds=2)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"NOTIFY /upnpcallback HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                         "X-Forwarded-For: 198.51.100.7\r\nContent-Length: 0\r\n\r\n".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=2)
            writer.close()
            await writer.wait_closed()
        finally:
            await self.stop_server(running)
        self.assertTrue(response.startswith(b"HTTP/1.1 200 "), response)
        lowered = response.lower()
        self.assertIn(b"\r\nconnection: close\r\n", lowered)
        self.assertNotIn(b"\r\nserver:", lowered)
        self.assertNotIn(b"\r\ndate:", lowered)
        # Hypercorn trusts no forwarded header: client stays the socket peer.
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0]["client"][0], "127.0.0.1")
        self.assertEqual(config.connection_limit.active, 0)

    async def test_header_size_and_connection_count_are_bounded_before_asgi(self):
        port, config, running = await self.start_server(connections=2, header_seconds=0.5)
        try:
            # Oversized incomplete header is rejected before ASGI runs.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"NOTIFY / HTTP/1.1\r\nX-Long: " + b"x" * 17000)
            await writer.drain()
            oversized = await asyncio.wait_for(reader.read(), timeout=2)
            self.assertRegex(oversized, rb"^HTTP/1\.1 (?:400|431) ")
            self.assertIn(b"\r\nconnection: close\r\n", oversized.lower())
            self.assertEqual(self.seen, [])
            writer.close()
            await writer.wait_closed()
            for _ in range(100):
                if config.connection_limit.active == 0:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(config.connection_limit.active, 0)

            # Two open connections fill the limit; a third is closed immediately.
            held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
            for _ in range(50):
                if config.connection_limit.active == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(config.connection_limit.active, 2)
            extra_reader, extra_writer = await asyncio.open_connection("127.0.0.1", port)
            self.assertEqual(await asyncio.wait_for(extra_reader.read(), timeout=1.5), b"")
            extra_writer.close()
            await extra_writer.wait_closed()
            self.assertEqual(config.connection_limit.active, 2)
            for _, w in held:
                w.close()
                await w.wait_closed()
            # Hypercorn's idle task finishes at header_seconds and closes the
            # accepted sockets, releasing both slots for subsequent requests.
            for _ in range(100):
                if config.connection_limit.active == 0:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(config.connection_limit.active, 0)
            self.assertEqual(self.seen, [])

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"NOTIFY /upnpcallback HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                         "Content-Length: 0\r\n\r\n".encode())
            await writer.drain()
            after = await asyncio.wait_for(reader.read(), timeout=2)
            self.assertTrue(after.startswith(b"HTTP/1.1 200 "), after)
            writer.close()
            await writer.wait_closed()
        finally:
            await self.stop_server(running)
        self.assertEqual(config.connection_limit.active, 0)


if __name__ == "__main__":
    unittest.main()
