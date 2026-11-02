"""The listener allocation, generated consumers and configurable collision checks."""
import json
import os
from pathlib import Path
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
from oh_access.service import load_config
from oh_cli import docs
from oh_install import steps
from oh_metrics import exporter


class Allocation(unittest.TestCase):
    def test_one_contiguous_run_with_distinct_application_anchor(self):
        self.assertEqual(oh_ports.ACCESS_PORT, 13600)
        self.assertEqual(list(oh_ports.DEFAULTS.values()), list(range(13600, 13623)))
        self.assertEqual(len(set(oh_ports.DEFAULTS.values())), len(oh_ports.DEFAULTS))

    def test_overrides_cannot_take_other_production_or_development_listeners(self):
        for host in ({"http_port": oh_ports.HTTPS_PORT},
                     {"https_port": oh_ports.DOCS_PORT},
                     {"metrics": {"grafana_port": oh_ports.HELPER_PORT}},
                     {"metrics": {"prometheus_port": 14040, "node_exporter_port": 14040}},
                     {"http_port": 14040, "metrics": {"exporter_port": 14040}}):
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

    def test_stopped_console_configuration_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            userdata = Path(temporary)
            path = userdata / oh_ports.SHELL_CFG
            path.parent.mkdir()
            path.write_text("# console\nsshPort = 8101\nsshHost = 0.0.0.0\nsshIdleTimeout = 123\n")
            oh_ports.configure_native(userdata)
            first = path.read_bytes()
            self.assertTrue(oh_ports.native_configured_bytes(first))
            self.assertIn(b"sshIdleTimeout = 123\n", first)
            self.assertNotIn(b"0.0.0.0", first)
            oh_ports.configure_native(userdata)
            self.assertEqual(path.read_bytes(), first)

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
            self.assertTrue(oh_logging.initialize(userdata, template, os.getuid(), os.getgid()))
            oh_ports.configure_native(userdata)
            self.assertTrue(oh_logging.ready(userdata))
            self.assertTrue(oh_ports.native_configured_bytes((userdata / oh_ports.SHELL_CFG).read_bytes()))

    def test_installer_sets_console_before_start_or_restart(self):
        host = oh_public.demo_host()
        context = steps.Context(AREA, {}, host, "c" * 40, "d" * 64, "example", "192.0.2.5")
        sequence = [type(step) for step in steps.install(context)]
        self.assertLess(sequence.index(steps.PairingLogging), sequence.index(steps.NativeListeners))
        self.assertLess(sequence.index(steps.NativeListeners), sequence.index(steps.Services))
        configured = (f"sshPort = {oh_ports.KARAF_PORT}\nsshHost = 127.0.0.1\n").encode()
        system = mock.Mock()
        system.read_bytes.return_value = configured
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
            config = {"version": 1, "state": str(root), "contract_file": str(root / "contract"),
                      "runtime_url": oh_ports.NATIVE_URL, "runtime_token_file": str(root / "token"),
                      "allowed_hosts": ["example.test"], "bind_host": "127.0.0.1", "port": oh_ports.HTTP_PORT,
                      "tls_cert": str(root / "cert"), "tls_key": str(root / "key"), "timezone": "UTC"}
            path = root / "config.json"
            path.write_text(json.dumps(config)); path.chmod(0o600)
            with self.assertRaises(AccessError):
                load_config(path, tls=False)
            config["port"] = oh_ports.ACCESS_PORT
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(path, tls=False)["port"], oh_ports.ACCESS_PORT)
