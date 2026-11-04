"""The listener allocation, generated consumers and configurable collision checks."""
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_atv
import oh_deploy
import oh_metrics
import oh_logging
import oh_ports
import oh_public
import oh_render
import oh_sandbox
from oh_access import AccessError
from oh_access.service import CONFIG_VERSION, load_config
from oh_cli import docs
from oh_install import steps
from oh_metrics import exporter


class Allocation(unittest.TestCase):
    def test_one_contiguous_run_with_distinct_application_anchor(self):
        self.assertEqual(oh_ports.ACCESS_PORT, 13600)
        self.assertEqual(list(oh_ports.DEFAULTS.values()), list(range(13600, 13627)))
        self.assertEqual(len(set(oh_ports.DEFAULTS.values())), len(oh_ports.DEFAULTS))
        self.assertEqual(tuple(oh_ports.DEFAULTS)[-4:], ("native_shutdown", "sandbox_shutdown", "boot_shutdown", "guest_http"))
        self.assertEqual([oh_ports.DEFAULTS[name] for name in ("native_shutdown", "sandbox_shutdown", "boot_shutdown")],
                         [13623, 13624, 13625])
        self.assertEqual(oh_ports.GUEST_HTTP_PORT, 13626)

    def test_overrides_cannot_take_other_production_or_development_listeners(self):
        for host in ({"http_port": oh_ports.HTTPS_PORT},
                     {"https_port": oh_ports.DOCS_PORT},
                     {"metrics": {"grafana_port": oh_ports.HELPER_PORT}},
                     {"metrics": {"prometheus_port": 14040, "node_exporter_port": 14040}},
                     {"http_port": 14040, "metrics": {"exporter_port": 14040}},
                     *({"http_port": oh_ports.DEFAULTS[name]} for name in ("native_shutdown", "sandbox_shutdown", "boot_shutdown"))):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "collides"):
                oh_ports.effective(host)

    def test_custom_site_ports_are_allowed_and_strictly_typed(self):
        host = {"http_port": 14040, "https_port": 14041, "metrics": {"grafana_port": 14042}}
        self.assertEqual(oh_ports.effective(host)["http"], 14040)
        self.assertEqual(oh_metrics.ports(host)["grafana"], 14042)
        for value in (True, "13601", 0, 1023, 65536):
            with self.subTest(value=value), self.assertRaises(ValueError):
                oh_ports.effective({"http_port": value})
        for name in ("access", "callbacks"):
            with self.assertRaisesRegex(ValueError, "collides"):
                oh_ports.listener(name, 14040, upstream=14040)

    def test_explicit_upstream_cannot_free_the_selected_sites_configured_http_port(self):
        allocated = oh_ports.effective()
        with self.assertRaisesRegex(ValueError, "collides"):
            oh_ports.listeners({"access": oh_ports.HTTP_PORT}, upstream=14041, allocated=allocated)
        ports = oh_ports.listeners({"access": oh_ports.ACCESS_PORT}, upstream=14041, allocated=allocated)
        self.assertEqual(ports["http"], oh_ports.HTTP_PORT)
        self.assertIn(14041, ports.values())
        # Without a selected host, the explicit origin retains the direct API's
        # established override of the native HTTP default.
        self.assertEqual(oh_ports.listeners({"access": oh_ports.HTTP_PORT}, upstream=14041)["access"], oh_ports.HTTP_PORT)

    def test_stopped_console_configuration_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            userdata = Path(temporary)
            path = userdata / oh_ports.SHELL_CFG
            path.parent.mkdir()
            path.write_text("# console\nsshPort = 8101\nsshHost = 0.0.0.0\nsshIdleTimeout = 123\n")
            custom = userdata / oh_ports.CUSTOM_PROPERTIES
            preserved = "# custom\nkaraf.pid.file=${karaf.data}/tmp/karaf.pid\nkaraf.handle.sigterm=true\n"
            custom.write_text(preserved + "karaf.shutdown.port: 0\nkaraf.shutdown.host 0.0.0.0\nkaraf.shutdown.port = 9999\n")
            oh_ports.configure_native(userdata)
            first = path.read_bytes()
            configured = custom.read_bytes()
            self.assertTrue(oh_ports.native_configured_bytes(first))
            self.assertIn(b"sshIdleTimeout = 123\n", first)
            self.assertNotIn(b"0.0.0.0", first)
            self.assertTrue(oh_ports.shutdown_configured_bytes(configured))
            self.assertTrue(configured.startswith(preserved.encode()))
            self.assertEqual(configured.count(b"karaf.shutdown.port"), 1)
            self.assertNotIn(b"0.0.0.0", configured)
            oh_ports.configure_native(userdata)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(custom.read_bytes(), configured)

    def test_listener_setup_ignores_independent_default_console_and_unused_http_default(self):
        def unrelated_listener(address, **kwargs):
            if address[1] in (8101, oh_ports.HTTP_PORT):
                return mock.MagicMock()
            raise ConnectionRefusedError()

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(oh_ports.socket, "create_connection", side_effect=unrelated_listener) as connect, \
                mock.patch.object(oh_ports, "configure_native") as configure:
            userdata = Path(temporary)
            self.assertEqual(oh_ports.main([str(userdata), "--http-port", "14040"]), 0)
        self.assertEqual([call.args[0][1] for call in connect.call_args_list],
                         [14040, oh_ports.KARAF_PORT, oh_ports.SHUTDOWN_PORT])
        configure.assert_called_once_with(userdata, anchored=True)

    def test_exact_userdata_record_lock_blocks_running_legacy_instance_but_not_foreign_instance(self):
        script = """
import sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
import oh_ports
with mock.patch.object(oh_ports.socket, 'create_connection', side_effect=ConnectionRefusedError):
    raise SystemExit(oh_ports.main([sys.argv[2]]))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "thermite"
            (target / "etc").mkdir(parents=True)
            console, custom = target / oh_ports.SHELL_CFG, target / oh_ports.CUSTOM_PROPERTIES
            console.write_text("sshPort=8101\nsshHost=0.0.0.0\n")
            custom.write_text("karaf.lock.dir=${karaf.data}/tmp\n")
            before = console.read_bytes(), custom.read_bytes()
            (target / "tmp").mkdir()
            lock = target / "tmp/lock"
            lock.touch()
            foreign = root / "independent-openhab/tmp/lock"
            foreign.parent.mkdir(parents=True)
            foreign.touch()
            for active, blocked in ((lock, True), (foreign, False)):
                with self.subTest(active=active), active.open("r+b") as descriptor:
                    fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    child = subprocess.run([sys.executable, "-c", script, str(AREA / "lib"), str(target)],
                                           capture_output=True, text=True, timeout=5)
                    self.assertEqual(child.returncode, 2 if blocked else 0, child.stderr)
                    if blocked:
                        self.assertIn("userdata lock is held", child.stderr)
                        self.assertEqual((console.read_bytes(), custom.read_bytes()), before)
                    else:
                        self.assertTrue(oh_ports.native_configured_bytes(console.read_bytes()))

    def test_native_guard_holds_existing_lock_through_writes_without_creating_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (root, root / "tmp"):
                path.mkdir(exist_ok=True)
                before = list(path.iterdir())
                with oh_ports._native_stopped(root):
                    self.assertEqual(list(path.iterdir()), before)
            lock = root / "tmp/lock"
            lock.touch()
            check = "import fcntl,sys; f=open(sys.argv[1], 'r+b'); fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"
            with oh_ports._native_stopped(root):
                child = subprocess.run([sys.executable, "-c", check, str(lock)], capture_output=True, timeout=5)
                self.assertNotEqual(child.returncode, 0)
            child = subprocess.run([sys.executable, "-c", check, str(lock)], capture_output=True, timeout=5)
            self.assertEqual(child.returncode, 0, child.stderr)

    def test_native_guard_rejects_symlinks_and_nonregular_or_linked_lock_files(self):
        for invalid in ("userdata-link", "tmp-link", "lock-link", "directory", "fifo", "hardlink"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                userdata = root / "userdata"
                userdata.mkdir()
                outside = root / "outside"
                outside.mkdir()
                (outside / "lock").touch()
                if invalid == "userdata-link":
                    (root / "linked").symlink_to(userdata, target_is_directory=True)
                    userdata = root / "linked"
                elif invalid == "tmp-link":
                    (userdata / "tmp").symlink_to(outside, target_is_directory=True)
                else:
                    (userdata / "tmp").mkdir()
                    lock = userdata / "tmp/lock"
                    if invalid == "lock-link":
                        lock.symlink_to(outside / "lock")
                    elif invalid == "directory":
                        lock.mkdir()
                    elif invalid == "fifo":
                        os.mkfifo(lock)
                    else:
                        os.link(outside / "lock", lock)
                with self.assertRaises((OSError, ValueError)):
                    with oh_ports._native_stopped(userdata):
                        self.fail("invalid native lock path admitted")

    def test_listener_setup_refuses_each_actual_listener_before_writing(self):
        ports = (14040, oh_ports.KARAF_PORT, oh_ports.SHUTDOWN_PORT)
        for occupied in ports:
            def listener(address, **kwargs):
                if address[1] == occupied:
                    return mock.MagicMock()
                raise ConnectionRefusedError()

            with self.subTest(port=occupied), \
                    mock.patch.object(oh_ports.socket, "create_connection", side_effect=listener), \
                    mock.patch.object(oh_ports, "configure_native") as configure, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    oh_ports.main(["/configured/thermite/userdata", "--http-port", "14040"])
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn(f"port {occupied} answers", errors.getvalue())
                configure.assert_not_called()

    def test_shutdown_config_requires_both_positive_fixed_port_and_loopback(self):
        for content in (None, b"", b"karaf.shutdown.port=0\nkaraf.shutdown.host=127.0.0.1\n",
                        b"karaf.shutdown.port=-1\nkaraf.shutdown.host=127.0.0.1\n",
                        f"karaf.shutdown.port={oh_ports.SHUTDOWN_PORT}\nkaraf.shutdown.host=0.0.0.0\n".encode(),
                        f"karaf.shutdown.port={oh_ports.SHUTDOWN_PORT}\n".encode()):
            with self.subTest(content=content):
                self.assertFalse(oh_ports.shutdown_configured_bytes(content))
        for port in (0, -1, True, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                oh_ports.shutdown_properties("", port)

    def test_shutdown_writer_preserves_unrelated_continued_values_and_physical_newlines(self):
        preserved = ("# comment ending in a backslash\\\r\n"
                     "other=keep\\\r\n\tkaraf.shutdown.port=100\\\r\n  !still the same value\r\n"
                     "karaf.pid.file=${karaf.data}/tmp/karaf.pid\r\n")
        managed = "karaf.shutdown.port=12\\\n 345\nkaraf.shutdown.host=0.0.\\\n 0.0\n"
        expected = oh_ports.shutdown_properties("", oh_ports.SHUTDOWN_PORT)
        result = oh_ports.shutdown_properties(preserved + managed, oh_ports.SHUTDOWN_PORT)
        self.assertEqual(result, preserved + expected)
        self.assertTrue(oh_ports.shutdown_configured_bytes(result.encode()))
        self.assertEqual(oh_ports.shutdown_properties(result, oh_ports.SHUTDOWN_PORT), result)
        with tempfile.TemporaryDirectory() as temporary:
            userdata = Path(temporary)
            path = userdata / oh_ports.CUSTOM_PROPERTIES
            path.parent.mkdir()
            path.write_bytes((preserved + managed).encode())
            oh_ports.configure_shutdown(userdata, oh_ports.SHUTDOWN_PORT)
            self.assertEqual(path.read_bytes(), result.encode())

    def test_shutdown_reader_counts_logical_entries_and_managed_continuations(self):
        port = str(oh_ports.SHUTDOWN_PORT)
        host = "karaf.shutdown.host=127.0.0.1\n"
        for count in (1, 2, 3, 4):
            for ending in ("\n", "\r", "\r\n"):
                with self.subTest(backslashes=count, ending=repr(ending)):
                    first = "other=keep" + "\\" * count + ending
                    managed = "karaf.shutdown.port=" + port + ending
                    text = first + managed + host
                    self.assertEqual(oh_ports.shutdown_configured_bytes(text.encode()), count % 2 == 0)
                    result = oh_ports.shutdown_properties(text, oh_ports.SHUTDOWN_PORT)
                    self.assertTrue(result.startswith(first + (managed if count % 2 else "")))
                    self.assertTrue(oh_ports.shutdown_configured_bytes(result.encode()))
                    self.assertEqual(oh_ports.shutdown_properties(result, oh_ports.SHUTDOWN_PORT), result)
        continued = "karaf.shutdown.po\\\n rt=" + port[:2] + "\\\r\n\t" + port[2:] + "\n" + host
        self.assertTrue(oh_ports.shutdown_configured_bytes(continued.encode()))
        self.assertEqual(oh_ports.shutdown_properties(continued, oh_ports.SHUTDOWN_PORT),
                         oh_ports.shutdown_properties("", oh_ports.SHUTDOWN_PORT))
        for tail in ("karaf.shutdown.port\n", "karaf.shutdown.port=" + port + " \n"):
            self.assertFalse(oh_ports.shutdown_configured_bytes(("karaf.shutdown.port=" + port + "\n" + tail + host).encode()))

    def test_shutdown_overrides_have_an_idempotent_standalone_eof_boundary(self):
        configured = oh_ports.shutdown_properties("", oh_ports.SHUTDOWN_PORT)
        for original, separator in (("other=keep\\", "\n\n"), ("other=keep\\\n", "\n"),
                                    ("other=keep\\\r", "\n\n"), ("other=keep\\\r\n", "\n"),
                                    ("other=keep\\\\", "\n"), ("# comment\\", "\n"),
                                    ("! comment\\\n", ""), ("other=keep\n", "")):
            with self.subTest(original=repr(original)):
                result = oh_ports.shutdown_properties(original, oh_ports.SHUTDOWN_PORT)
                self.assertEqual(result, original + separator + configured)
                self.assertTrue(oh_ports.shutdown_configured_bytes(result.encode()))
                self.assertEqual(oh_ports.shutdown_properties(result, oh_ports.SHUTDOWN_PORT), result)

    def test_ambiguous_escaped_keys_fail_closed_without_modifying_native_files(self):
        configured = oh_ports.shutdown_properties("", oh_ports.SHUTDOWN_PORT)
        for text in ("karaf\\.shutdown.port=100\n", r"karaf.shutdown.po\u0072t=100" + "\n",
                     r"other\:key=value" + "\n", "\\\n"):
            with self.subTest(text=text):
                self.assertFalse(oh_ports.shutdown_configured_bytes((text + configured).encode()))
                with self.assertRaisesRegex(ValueError, "manual review"):
                    oh_ports.shutdown_properties(text + configured, oh_ports.SHUTDOWN_PORT)
        with tempfile.TemporaryDirectory() as temporary:
            userdata = Path(temporary)
            console = userdata / oh_ports.SHELL_CFG
            console.parent.mkdir()
            console.write_bytes(b"sshPort=8101\n")
            custom = userdata / oh_ports.CUSTOM_PROPERTIES
            custom.write_bytes(b"other\\:key=value\n")
            before = console.read_bytes(), custom.read_bytes()
            with self.assertRaisesRegex(ValueError, "manual review"):
                oh_ports.configure_native(userdata)
            self.assertEqual((console.read_bytes(), custom.read_bytes()), before)

    def test_native_configuration_reads_both_existing_files_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            userdata = Path(temporary)
            path = userdata / oh_ports.SHELL_CFG
            path.parent.mkdir()
            path.write_text("sshPort=8101\n")
            before = path.read_bytes()
            with self.assertRaises(FileNotFoundError):
                oh_ports.configure_native(userdata)
            self.assertEqual(path.read_bytes(), before)

    def test_console_writer_cannot_create_partial_first_install_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            userdata = root / "userdata"
            userdata.mkdir()
            with self.assertRaises(FileNotFoundError):
                oh_ports.configure_native(userdata)
            self.assertEqual(list(userdata.iterdir()), [])
            template = root / "template"
            (template / "etc").mkdir(parents=True)
            (template / oh_logging.RELATIVE).write_text("<Configuration><Loggers><Root/></Loggers></Configuration>")
            (template / oh_logging.SELECTOR).write_bytes(oh_logging.DEFAULT_SELECTOR)
            (template / oh_ports.SHELL_CFG).write_text("sshPort = 8101\nsshHost = 127.0.0.1\n")
            (template / oh_ports.CUSTOM_PROPERTIES).write_text("karaf.pid.file=${karaf.data}/tmp/karaf.pid\n")
            self.assertTrue(oh_logging.initialize(userdata, template, os.getuid(), os.getgid()))
            oh_ports.configure_native(userdata)
            self.assertTrue(oh_logging.ready(userdata))
            self.assertTrue(oh_ports.native_configured_bytes((userdata / oh_ports.SHELL_CFG).read_bytes()))
            self.assertTrue(oh_ports.shutdown_configured_bytes((userdata / oh_ports.CUSTOM_PROPERTIES).read_bytes()))

    def test_installer_sets_console_before_start_or_restart(self):
        host = oh_public.demo_host()
        versions = json.loads((AREA / "versions.json").read_text())
        context = steps.Context(AREA, versions, host, "c" * 40, "d" * 64, "example", "192.0.2.5")
        sequence = [type(step) for step in steps.install(context)]
        self.assertLess(sequence.index(steps.NativePreparation), sequence.index(steps.Deploy))
        self.assertLess(sequence.index(steps.NativePreparation), sequence.index(steps.Services))
        system = mock.Mock()
        system.unit.return_value.active = "inactive"
        with mock.patch.object(steps.ownership, "inspect", return_value=steps.ownership.Owned()):
            commands = [action.argv for action in steps.NativePreparation(context).actions(system)]
        logging = next(i for i, argv in enumerate(commands) if any(word.endswith("/oh_logging.py") for word in argv))
        listeners = next(i for i, argv in enumerate(commands) if any(word.endswith("/oh_ports.py") for word in argv))
        receipt = next(i for i, argv in enumerate(commands) if "--receipt" in argv)
        self.assertLess(logging, listeners)
        self.assertLess(listeners, receipt)
        configured = (f"sshPort = {oh_ports.KARAF_PORT}\nsshHost = 127.0.0.1\n").encode()
        system.read_bytes.side_effect = lambda path: configured if path.name == oh_ports.SHELL_CFG.name else oh_ports.shutdown_properties("", oh_ports.SHUTDOWN_PORT).encode()
        self.assertTrue(steps.NativeListeners(context).check(system).done)


class Consumers(unittest.TestCase):
    def test_generated_services_use_the_allocation_and_preserve_addresses(self):
        host = oh_public.demo_host()
        services = oh_metrics.services(host)
        for service, name in zip(services, ("ntfy", "notifications", "alertmanager", "prometheus", "node_exporter", "blackbox", "exporter", "grafana")):
            expected_host = "0.0.0.0" if name == "grafana" else "127.0.0.1"
            self.assertEqual(service.listen, f"{expected_host}:{oh_ports.DEFAULTS[name]}")
        alertmanager = next(service for service in services if service.image == "alertmanager")
        self.assertIn("--cluster.listen-address=", alertmanager.args)
        generated = oh_render.render(oh_public.demo())
        self.assertIn(f"org.openhab.lsp:port={oh_ports.LSP_PORT}", generated["services/runtime.cfg"])
        self.assertEqual(oh_atv.LISTEN, "127.0.0.1:13605")
        self.assertEqual(exporter.OPENHAB, oh_ports.NATIVE_URL)
        self.assertIn("lib/oh_ports.py", oh_metrics.EXPORTER_APP)
        self.assertIn("lib/oh_ports.py", oh_deploy.ATV_APP)

    def test_sandbox_and_boot_check_remain_loopback_and_separate(self):
        sandbox = oh_sandbox.sandbox_instance()
        boot = oh_sandbox.boot_check_instance()
        self.assertEqual((sandbox.http_port, sandbox.https_port, sandbox.ssh_port), (13615, 13616, 13617))
        self.assertEqual((boot.http_port, boot.https_port, boot.ssh_port), (13618, 13619, 13620))
        self.assertEqual((sandbox.shutdown_port, boot.shutdown_port), (13624, 13625))
        self.assertEqual(sandbox.http_address, "127.0.0.1")
        self.assertEqual(boot.http_address, "127.0.0.1")
        self.assertIn("org.openhab.core.model.lsp", oh_sandbox.FEATURES_PROCESSING)

    def test_docs_preview_uses_the_allocated_default(self):
        with mock.patch.object(docs, "cmd_serve", return_value=0) as serve:
            self.assertEqual(docs.main(["serve"]), 0)
        self.assertEqual(serve.call_args.args[0].port, oh_ports.DOCS_PORT)

    def test_access_loader_rejects_collision_before_composition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            for name in ("token", "contract"):
                path = root / name
                path.write_text("{}")
                path.chmod(0o600)
            config = {"version": CONFIG_VERSION, "state": str(root), "contract_file": str(root / "contract"),
                      "runtime_url": oh_ports.NATIVE_URL, "runtime_token_file": str(root / "token"),
                      "allowed_hosts": ["example.test"], "bind_host": "127.0.0.1", "port": oh_ports.HTTP_PORT,
                      "tls_cert": str(root / "cert"), "tls_key": str(root / "key"), "timezone": "UTC", "guest_http": None}
            path = root / "config.json"
            path.write_text(json.dumps(config)); path.chmod(0o600)
            with self.assertRaises(AccessError):
                load_config(path, tls=False)
            config["port"] = oh_ports.ACCESS_PORT
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(path, tls=False)["port"], oh_ports.ACCESS_PORT)
