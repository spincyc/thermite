"""Unified gateway deployment preserves authority through failure and retry."""

import json
import hashlib
from contextlib import closing
import os
from pathlib import Path
import shutil
import ssl
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_access import AccessError, deployment, staging
from oh_access.files import json_private, private_directory, atomic_private
from oh_install.access import Gateway, installed_release, openssl_config, site_inputs
from oh_install.system import UnitFile, UnitOrigin


def handshake(chain, key, authority, name):
    """An in-memory TLS handshake: a client trusting only the authority."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(chain, key)
    client = ssl.create_default_context(cafile=str(authority))
    buffers = [ssl.MemoryBIO() for _ in range(4)]
    talking = client.wrap_bio(buffers[0], buffers[1], server_hostname=name)
    listening = server.wrap_bio(buffers[2], buffers[3], server_side=True)
    for _ in range(10):
        done = 0
        for side, incoming, outgoing in ((talking, buffers[0], buffers[1]), (listening, buffers[2], buffers[3])):
            try:
                side.do_handshake()
                done += 1
            except ssl.SSLWantReadError:
                pass
        buffers[2].write(buffers[1].read())
        buffers[0].write(buffers[3].read())
        if done == 2:
            return talking.getpeercert()
    raise AssertionError("TLS handshake did not complete")


class PrivateFixture(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/unified-gateway"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def private(self, name, content):
        path = self.root / name
        atomic_private(path, content.encode() if isinstance(content, str) else content)
        return path


class Backup(PrivateFixture):
    def test_whole_state_includes_identity_keys_sqlite_and_worker_state(self):
        state = private_directory(self.root / "state")
        originals = {"site-id": "original-site", "secret-key": "original-key", "identity.sqlite3-wal": "journal",
                     "locks/room-bedroom": "", "worker/reconcile.json": '{"revision":7}'}
        connection = sqlite3.connect(state / "identity.sqlite3")
        connection.execute("CREATE TABLE preserved(value TEXT)")
        connection.execute("INSERT INTO preserved VALUES('original administrator')")
        connection.commit()
        connection.close()
        for name, content in originals.items():
            self.private("state/" + name, content)
        parent = private_directory(self.root / "backups")
        archive = parent / "before.tar.gz"
        witness = deployment.backup(state, archive)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as saved:
            for name, content in originals.items():
                self.assertEqual(saved.extractfile("state/" + name).read(), content.encode())
            database = self.root / "restored.sqlite3"
            database.write_bytes(saved.extractfile("state/identity.sqlite3").read())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM preserved").fetchone(), ("original administrator",))
        self.private("state/site-id", "new state must not replace the rollback copy")
        self.assertEqual(deployment.backup(state, archive), witness)
        with tarfile.open(archive) as saved:
            self.assertEqual(saved.extractfile("state/site-id").read(), b"original-site")

    def test_incomplete_publication_is_retained_and_retried(self):
        state = private_directory(self.root / "state")
        self.private("state/site-id", "original")
        archive = self.private("backups/before.tar.gz", "interrupted")
        deployment.backup(state, archive)
        self.assertEqual(len(list(archive.parent.glob("*.unverified"))), 1)
        with tarfile.open(archive) as saved:
            self.assertEqual(saved.extractfile("state/site-id").read(), b"original")

    def test_corrupted_completed_backup_and_links_fail_closed(self):
        state = private_directory(self.root / "state")
        self.private("state/site-id", "original")
        parent = private_directory(self.root / "backups")
        archive = parent / "before.tar.gz"
        deployment.backup(state, archive)
        archive.write_bytes(b"corrupted")
        with self.assertRaisesRegex(deployment.DeploymentError, "receipt"):
            deployment.backup(state, archive)
        (state / "escape").symlink_to(self.root / "outside")
        with self.assertRaises(OSError):
            deployment.backup(state, parent / "another.tar.gz")
        self.assertFalse((parent / "another.tar.gz.complete.json").exists())

    def test_sqlite_schema_upgrade_retains_original_snapshot(self):
        try:
            import django  # noqa: F401
        except ImportError:
            self.skipTest("identity dependencies are required for real migration")
        state = private_directory(self.root / "state")
        program = "from oh_identity.composition import setup; setup(%r); from django.core.management import call_command; call_command('migrate', 'oh_identity', %r, verbosity=0)"
        environment = {**os.environ, "PYTHONPATH": str(AREA / "lib"), "DJANGO_SETTINGS_MODULE": "oh_identity.settings"}
        environment.pop("THERMITE_IDENTITY_STATE", None)
        subprocess.run([sys.executable, "-c", program % (str(state), "0001")], env=environment, check=True, capture_output=True)
        keys = {name: (state / name).read_bytes() for name in ("site-id", "secret-key")}
        backup = private_directory(self.root / "backups") / "before.tar.gz"
        deployment.backup(state, backup)
        subprocess.run([sys.executable, "-c", program % (str(state), "0002")], env=environment, check=True, capture_output=True)
        self.assertEqual({name: (state / name).read_bytes() for name in keys}, keys)
        with closing(sqlite3.connect(state / "identity.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT enabled FROM oh_identity_securitystate").fetchone(), (0,))
        with tarfile.open(backup) as saved:
            before = self.root / "before.sqlite3"
            before.write_bytes(saved.extractfile("state/identity.sqlite3").read())
        with closing(sqlite3.connect(before)) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='oh_identity_securitystate'").fetchone())


class Adoption(PrivateFixture):
    def config(self):
        release = private_directory(self.root / "old-release")
        state = private_directory(self.root / "existing-state")
        cert = self.private("old-release/cert", "existing public certificate")
        key = self.private("old-release/key", "existing private key")
        self.private("old-release/callbacks.json", '{"exact":"route and peer"}')
        config = {"version": 1, "state": str(state), "contract_file": str(release / "contract.json"),
                  "runtime_url": "http://127.0.0.1:13601", "runtime_token_file": str(release / "bearer"),
                  "allowed_hosts": ["house.example.test"], "bind_host": "192.0.2.5", "port": 13600,
                  "timezone": "UTC", "tls_cert": str(cert), "tls_key": str(key)}
        json_private(release / "config.json", config)
        return release, state

    def test_adopt_retains_private_state_and_exact_tls_callback_material(self):
        release, state = self.config()
        self.private("existing-state/site-id", "permanent-site")
        result = deployment.adopt(release / "config.json", self.root / "adopted", uid=os.geteuid(), gid=os.getegid(), service_uid=os.geteuid())
        self.assertEqual(result["state"], str(state))
        self.assertEqual(result["allowed_hosts"], ["house.example.test"])
        self.assertEqual((self.root / "adopted/tls-key.pem").read_text(), "existing private key")
        self.assertEqual((self.root / "adopted/callbacks.json").read_bytes(), (release / "callbacks.json").read_bytes())
        self.assertEqual((state / "site-id").read_text(), "permanent-site")
        self.assertFalse((state / "secret-key").exists(), "adoption must not initialize Django")

    def test_adoption_rejects_symlinked_inputs(self):
        release, _ = self.config()
        (release / "key").unlink()
        (release / "key").symlink_to(self.private("outside", "secret"))
        with self.assertRaises(OSError):
            deployment.adopt(release / "config.json", self.root / "adopted", uid=os.geteuid(), gid=os.getegid(), service_uid=os.geteuid())


class StagedUpstream(PrivateFixture):
    def material(self):
        token = self.private("release/bearer", "staged-credential")
        config = self.private("release/config.json", json.dumps({"runtime_url": "http://127.0.0.1:13601", "runtime_token_file": str(token)}))
        expected = self.private("operator/expected.json", json.dumps({"runtime_url": "http://127.0.0.1:13601",
                             "runtime_credential_sha256": hashlib.sha256(token.read_bytes()).hexdigest()}))
        return token, config, expected

    def test_actual_staged_credential_is_used_and_auth_failure_is_not_readiness(self):
        token, config, expected = self.material()
        requests = []
        response = SimpleNamespace(status=200, read=lambda maximum: b'[{"name":"LivingRoom_Power"}]')
        connection = SimpleNamespace(request=lambda *args, **kwargs: requests.append((args, kwargs)),
                                     getresponse=lambda: response, close=lambda: None)
        factory = lambda *args, **kwargs: connection
        deployment.upstream(config, expected, service=(os.geteuid(), os.getegid()), connection_factory=factory)
        self.assertEqual(requests[0][1]["headers"]["Authorization"], "Bearer staged-credential")
        response.status = 401
        with self.assertRaisesRegex(deployment.DeploymentError, "readiness failed"):
            deployment.upstream(config, expected, service=(os.geteuid(), os.getegid()), connection_factory=factory)
        token.write_text("obsolete-staged-credential")
        calls = len(requests)
        with self.assertRaisesRegex(deployment.DeploymentError, "credential differs"):
            deployment.upstream(config, expected, service=(os.geteuid(), os.getegid()), connection_factory=factory)
        self.assertEqual(len(requests), calls, "wrong staged credentials must not reach the native listener")

    def test_staged_upstream_waits_for_reload_but_refuses_authentication_and_malformed_data(self):
        token, config, expected = self.material()
        for replies, succeeds in (((404, 503, 200), True), ((401,), False), ((302,), False), (("malformed",), False)):
            with self.subTest(replies=replies):
                now, requests, closes = [0.0], [], []
                remaining = iter(replies)

                def factory(*args, **kwargs):
                    status = next(remaining)
                    response = SimpleNamespace(status=200 if status == "malformed" else status,
                                               read=lambda maximum: b"{}" if status == "malformed" else b"[]")
                    return SimpleNamespace(request=lambda *args, **kwargs: requests.append(kwargs["headers"]),
                                           getresponse=lambda: response, close=lambda: closes.append(True))

                kwargs = dict(service=(os.geteuid(), os.getegid()), connection_factory=factory,
                              clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
                if succeeds:
                    deployment.upstream(config, expected, **kwargs)
                    self.assertEqual(now[0], 2)
                else:
                    with self.assertRaisesRegex(deployment.DeploymentError, "readiness failed"):
                        deployment.upstream(config, expected, **kwargs)
                    self.assertEqual(now[0], 0)
                self.assertEqual(len(requests), len(replies))
                self.assertEqual(len(closes), len(requests))
                self.assertTrue(all(headers["Authorization"] == "Bearer staged-credential" for headers in requests))
                self.assertEqual(token.read_text(), "staged-credential")

    def test_staged_upstream_transient_failure_has_one_bounded_allowance(self):
        _, config, expected = self.material()
        now = [0.0]
        connection = SimpleNamespace(request=lambda *args, **kwargs: None,
                                     getresponse=lambda: SimpleNamespace(status=503, read=lambda maximum: b"[]"),
                                     close=lambda: None)
        with self.assertRaisesRegex(deployment.DeploymentError, "after 30 seconds"):
            deployment.upstream(config, expected, service=(os.geteuid(), os.getegid()),
                                connection_factory=lambda *args, **kwargs: connection,
                                clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        self.assertEqual(now[0], 30)

    def test_privileges_are_dropped_before_service_data_and_network(self):
        _, config, expected = self.material()
        uid, gid = os.geteuid(), os.getegid()
        dropped = []
        def factory(*args, **kwargs):
            self.assertEqual(dropped, [("groups", []), ("gid", gid), ("uid", uid)])
            return SimpleNamespace(request=lambda *args, **kwargs: None,
                                   getresponse=lambda: SimpleNamespace(status=200, read=lambda maximum: b"[]"), close=lambda: None)
        with patch.object(deployment.os, "geteuid", return_value=0), \
                patch.object(deployment.os, "setgroups", side_effect=lambda value: dropped.append(("groups", value))), \
                patch.object(deployment.os, "setgid", side_effect=lambda value: dropped.append(("gid", value))), \
                patch.object(deployment.os, "setuid", side_effect=lambda value: dropped.append(("uid", value))):
            deployment.upstream(config, expected, service=(uid, gid), connection_factory=factory)


class Host:
    def __init__(self):
        self.commands, self.messages = [], []
        self.active = False
        self.fail_migration = False
        self.fail_upstream = False
        self.installed = None
        self.unit_bytes = {}
        self.overrides = {}

    def say(self, text):
        self.messages.append(text)

    def interactive(self):
        return True

    def ask(self, text):
        raise AssertionError("gateway must inherit the workflow's single approval")

    def run(self, argv, cwd):
        self.commands.append(tuple(argv))
        if "migrate" in argv and self.fail_migration:
            return 1
        if "upstream" in argv and self.fail_upstream:
            return 1
        if "install" in argv and "-d" not in argv and Path(argv[-2]).is_file() and argv[-1].startswith("/etc/systemd/system/"):
            self.unit_bytes[argv[-1]] = Path(argv[-2]).read_bytes()
        if "stop" in argv:
            self.active = False
        if "--now" in argv:
            self.active = True
        return 0

    def unit_origin(self, name):
        if name in self.overrides:
            return self.overrides[name]
        if self.installed is None and "/etc/systemd/system/" + name not in self.unit_bytes:
            return UnitOrigin(load_state="not-found", active="inactive")
        return UnitOrigin(fragment="/etc/systemd/system/" + name, active="active" if self.active else "inactive")

    def unit_origins(self, names):
        return {name: self.unit_origin(name) for name in names}

    def unit_file(self, path):
        if str(path) in self.unit_bytes:
            return UnitFile(self.unit_bytes[str(path)], 0, 0o644)
        return UnitFile(("[Service]\nUser=thermite\nGroup=thermite\nWorkingDirectory=" + str(self.installed) + "\n").encode(), 0, 0o644)

    def capture(self, argv, *, diagnostics=False):
        if "--value" in argv:
            return 0, "123\n"
        return 0, "ActiveState=active\nMainPID=123\nUnitFileState=enabled\nNeedDaemonReload=no\n"

    def sleep(self, seconds):
        pass


def real_openssl(system):
    """Run real openssl commands for a fake host; record and fake the rest."""
    run, capture = system.run, system.capture

    def run_openssl(argv, cwd):
        if argv[0] != "openssl":
            return run(argv, cwd)
        system.commands.append(tuple(argv))
        return subprocess.run(list(argv), capture_output=True, check=False).returncode

    def capture_openssl(argv, *, diagnostics=False):
        if argv[0] != "openssl":
            return capture(argv, diagnostics=diagnostics)
        result = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        return result.returncode, result.stdout

    system.run, system.capture = run_openssl, capture_openssl
    return system


class Lifecycle(PrivateFixture):
    def test_partial_unit_set_is_never_treated_as_a_new_identity(self):
        for name in staging.ACCESS_UNITS[1:]:
            system = Host()
            system.overrides[name] = UnitOrigin(fragment="/etc/systemd/system/" + name, active="active")
            with self.subTest(unit=name), self.assertRaisesRegex(AccessError, "related access units remain"):
                installed_release(system)
            self.assertEqual(system.commands, [])

    def setUp(self):
        super().setUp()
        from oh_install.runner import _TASK_APPROVAL
        token = _TASK_APPROVAL.set(True)
        self.addCleanup(_TASK_APPROVAL.reset, token)

    def unit_commands(self, gateway, release):
        commands = []
        for name, source in staging.units(release, state=self.root / "state").items():
            path = gateway.directory / "test-units" / name
            atomic_private(path, source.encode())
            commands.append(["sudo", "install", str(path), "/etc/systemd/system/" + name])
        return commands

    def gateway(self):
        area = private_directory(self.root / "site")
        context = SimpleNamespace(area=area, var=area / "var", head="a" * 40, conf_digest="c" * 64,
                                  application={"version": "1.2.3", "digest": "b" * 64},
                                  installation=SimpleNamespace(access=self.root / "production" / "access"), hostname="house.example.test",
                                  address="192.0.2.10", host={"timezone": "UTC"}, configuration=area,
                                  url="http://127.0.0.1:13601", token_file=Path("var/token"))
        gateway = Gateway(context, boundary=lambda system, metadata, callback: callback)
        verification = patch.object(gateway, "_verify_boundary")
        verification.start()
        self.addCleanup(verification.stop)
        private_directory(gateway.directory)
        token = os.urandom(32).hex().encode()
        atomic_private(context.area / context.token_file, token)
        snapshot = gateway.directory / "test-runtime-token"
        atomic_private(snapshot, token)
        system = real_openssl(Host())
        authority = gateway._authority(system, "house.example.test")
        cert, key = gateway._issue(system, self.root / "tls", ["house.example.test"], authority)
        metadata = {"state": str(self.root / "state"), "tls_cert": str(cert), "tls_key": str(key),
                    "tls_ca": str(authority[0]), "allowed_hosts": ["house.example.test"], "bind_host": "0.0.0.0",
                    "port": 13600, "timezone": "UTC", "callback_file": None, "callbacks": False}
        release = str(self.root / "release")
        command = ["sudo", "-u", "thermite", "-g", "thermite", release + "/env/bin/python", release + "/program/bin/thermite", "access"]
        receipt = {"destination": release, "persistent_state": metadata["state"], "config": release + "/config.json",
                   "units": ["thermite-access.service", "thermite-access-worker.service"],
                   "ownership_commands": [["sudo", "/usr/bin/python3", "-I", "-S", str(gateway.directory / "custody.py"), "custody.json"]],
                   "verification_command": [*command, "verify", "--config", release + "/config.json"],
                   "unit_install_commands": self.unit_commands(gateway, release)}
        journal = {"version": 1, "versions": gateway.versions, "previous_release": "/srv/old-release", "metadata": metadata, "stage": receipt, "phase": "staged",
                   "runtime_credential_sha256": hashlib.sha256(token).hexdigest(), "runtime_token_file": str(snapshot)}
        json_private(gateway.journal_path, journal)
        return gateway

    def test_failed_migration_never_activates_and_retry_reuses_backup_and_release(self):
        gateway, system = self.gateway(), Host()
        system.installed = Path("/srv/old-release")
        system.active = True
        system.fail_migration = True
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 1)
            self.assertFalse(any("--now" in command for command in system.commands))
            journal = json.loads(gateway.journal_path.read_bytes())
            self.assertEqual(journal["phase"], "backed-up")
            self.assertIn("   Complete identity backup retained: " + journal["backup"], system.messages)
            backup = next(command for command in system.commands if "backup" in command)
            migration_index = next(index for index, command in enumerate(system.commands) if "migrate" in command)
            backup_index = system.commands.index(backup)
            stops = [index for index, command in enumerate(system.commands) if "stop" in command]
            self.assertEqual(len(stops), 3)
            self.assertLess(max(stops), backup_index)
            self.assertLess(backup_index, migration_index)
            system.fail_migration = False
            self.assertEqual(gateway.perform(system), 0)
            journal = json.loads(gateway.journal_path.read_bytes())
            self.assertEqual(journal["phase"], "complete")
            self.assertEqual(journal["previous_release"], "/srv/old-release")
            self.assertEqual(journal["versions"], gateway.versions)
            self.assertEqual([command for command in system.commands if "backup" in command], [backup, backup])
            before = len(system.commands)
            self.assertEqual(gateway.perform(system), 0)
            self.assertFalse(any("stop" in command or "migrate" in command or "--now" in command for command in system.commands[before:]))

    def test_not_found_units_do_not_block_fresh_deployment(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0)
        self.assertTrue(any("migrate" in command for command in system.commands))
        self.assertFalse(any("stop" in command for command in system.commands))

    def test_commissioning_failure_does_not_claim_an_identity_backup_exists(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_boundary", side_effect=ValueError("native commissioning failed")):
            self.assertEqual(gateway.perform(system), 1)
        self.assertFalse(any("identity backup" in message.lower() for message in system.messages))
        self.assertTrue(any("same install or upgrade command" in message for message in system.messages))
        self.assertFalse(any("backup" in command or "migrate" in command or "--now" in command for command in system.commands))

    def test_installed_private_release_is_discovered_from_unit_not_readability(self):
        system = Host()
        system.installed = Path("/srv/private-release")
        self.assertEqual(installed_release(system), system.installed)
        with patch.object(system, "unit_file", return_value=UnitFile(b"", 1000, 0o644)):
            with self.assertRaisesRegex(ValueError, "root custody"):
                installed_release(system)

    def test_failed_https_never_marks_deployment_complete(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", side_effect=ValueError("certificate validation failed")):
            self.assertEqual(gateway.perform(system), 1)
        self.assertEqual(json.loads(gateway.journal_path.read_bytes())["phase"], "activated")
        self.assertFalse((gateway.context.var / "gateway/current.json").exists())
        self.assertFalse(any("Gateway verified" in message for message in system.messages))

    def test_legacy_native_assets_use_gateway_transport_with_current_hashes(self):
        from oh_features.remote import FILES
        import hashlib
        gateway = self.gateway()
        assets = private_directory(self.root / "legacy-assets")
        for name in FILES:
            value = b"unchanged source"
            if name.endswith(".html"):
                value = b'<script src="/static/remote/access-config.js?v=0123456789abcdef"></script>'
            elif name == "access-config.js":
                value = b'globalThis.THERMITE_ACCESS_CONFIG = {"mode":"native"};'
            elif name == "system.json":
                value = b'{"version":1,"tools":[{"id":"openhab","localOnly":false}]}'
            atomic_private(assets / name, value)
        built = gateway._assets(assets)
        script = (built / "access-config.js").read_bytes()
        self.assertIn(b'"mode":"authenticated"', script)
        self.assertIn(hashlib.sha256(script).hexdigest()[:16], (built / "index.html").read_text())
        self.assertTrue(json.loads((built / "system.json").read_bytes())["tools"][0]["localOnly"])
        self.assertIn('"mode":"native"', (assets / "access-config.js").read_text())

    def test_completed_but_unhealthy_service_repairs_with_an_additional_backup(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0)
        original = json.loads(gateway.journal_path.read_bytes())["backup"]
        with patch.object(gateway, "_https", side_effect=[ValueError("stopped"), "https://house.example.test:13600"]):
            self.assertEqual(gateway.perform(system), 0)
        self.assertNotEqual(json.loads(gateway.journal_path.read_bytes())["backup"], original)

    def test_completed_release_repairs_changed_protected_unit_before_success(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0)
            destination = "/etc/systemd/system/thermite-access.service"
            expected = system.unit_bytes[destination]
            system.unit_bytes[destination] = expected + b"\nExecStart=/changed-command\n"
            before = len(system.commands)
            self.assertEqual(gateway.perform(system), 0)
        self.assertEqual(system.unit_bytes[destination], expected)
        self.assertTrue(any("install" in command and command[-1] == destination for command in system.commands[before:]))
        self.assertTrue(any("--now" in command for command in system.commands[before:]))

    def test_completed_release_rejects_unknown_worker_override_without_repair(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0)
            profile = (gateway.context.var / "gateway/current.json").read_bytes()
            name = "thermite-access-worker.service"
            system.overrides[name] = UnitOrigin(fragment="/etc/systemd/system/" + name,
                                               drop_ins="/etc/systemd/system/thermite-access-worker.service.d/custom.conf")
            before = len(system.commands)
            self.assertEqual(gateway.perform(system), 1)
        self.assertFalse(any("stop" in command or "migrate" in command or "--now" in command for command in system.commands[before:]))
        self.assertEqual((gateway.context.var / "gateway/current.json").read_bytes(), profile)

    def test_non_mapping_journal_is_a_controlled_failure(self):
        gateway, system = self.gateway(), Host()
        json_private(gateway.journal_path, [])
        self.assertEqual(gateway.perform(system), 1)
        self.assertTrue(any("journal does not match" in message for message in system.messages))

    def test_rotated_runtime_token_restages_same_site_without_changing_identity_or_tls(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0)
        original = json.loads(gateway.journal_path.read_bytes())
        system.installed = Path(original["stage"]["destination"])
        system.active = True
        atomic_private(gateway.context.area / gateway.context.token_file, b"new-runtime-credential")
        def stage(release, **kwargs):
            self.assertEqual(Path(kwargs["runtime_token_file"]).read_bytes(), b"new-runtime-credential")
            self.assertEqual(kwargs["state"], original["metadata"]["state"])
            self.assertEqual(kwargs["tls_key"], original["metadata"]["tls_key"])
            self.assertEqual(kwargs["tls_cert"], original["metadata"]["tls_cert"])
            receipt = {**original["stage"], "destination": str(release), "config": str(release / "config.json")}
            receipt["verification_command"] = [part.replace(original["stage"]["destination"], str(release)) for part in receipt["verification_command"]]
            return receipt
        with patch.object(staging, "stage", side_effect=stage) as staged, \
                patch.object(gateway, "_assets", return_value=self.root / "assets"), \
                patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0, system.messages)
            self.assertEqual(gateway.perform(system), 0, system.messages)
            staged.assert_called_once()
        current = json.loads(gateway.journal_path.read_bytes())
        self.assertNotEqual(current["stage"]["destination"], original["stage"]["destination"])
        self.assertEqual(current["previous_release"], original["stage"]["destination"])
        self.assertNotEqual(current["backup"], original["backup"])
        self.assertEqual(current["runtime_credential_sha256"], hashlib.sha256(b"new-runtime-credential").hexdigest())
        self.assertTrue(list(gateway.directory.glob("history-*.json")))
        self.assertFalse(any("new-runtime-credential" in message or current["runtime_credential_sha256"] in message for message in system.messages))

    def test_staged_auth_failure_stops_before_migration_or_activation(self):
        gateway, system = self.gateway(), Host()
        system.fail_upstream = True
        with patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 1)
        self.assertFalse(any("migrate" in command or "--now" in command or "stop" in command for command in system.commands))
        self.assertNotEqual(json.loads(gateway.journal_path.read_bytes())["phase"], "complete")
        self.assertFalse((gateway.context.var / "gateway/current.json").exists())

    def test_fresh_install_prepares_tls_and_release_without_manual_staging(self):
        gateway, system = self.gateway(), real_openssl(Host())
        gateway.journal_path.unlink()
        shutil.rmtree(gateway.context.var / "gateway/authority")
        def stage(release, **kwargs):
            self.assertEqual(kwargs["state"], str(gateway.root / "state"))
            self.assertEqual(kwargs["house"], gateway.context.configuration / "instance/devices.yaml")
            self.assertEqual(kwargs["assets"], self.root / "managed-assets")
            self.assertEqual(kwargs["runtime_url"], gateway.context.url)
            self.assertIn("127.0.0.1", kwargs["allowed_hosts"])
            self.assertFalse((Path(kwargs["state"]) / "site-id").exists())
            command = ["sudo", "-u", "thermite", "-g", "thermite", str(release / "env/bin/python"), str(release / "program/bin/thermite"), "access"]
            return {"destination": str(release), "persistent_state": kwargs["state"], "config": str(release / "config.json"),
                    "units": ["thermite-access.service", "thermite-access-worker.service"],
                    "ownership_commands": [], "verification_command": [*command, "verify", "--config", str(release / "config.json")],
                    "unit_install_commands": self.unit_commands(gateway, release)}
        with patch.object(staging, "stage", side_effect=stage) as staged, \
                patch.object(gateway, "_assets", return_value=self.root / "managed-assets"), \
                patch.object(gateway, "_https", return_value="https://house.example.test:13600"):
            self.assertEqual(gateway.perform(system), 0, system.messages)
            staged.assert_called_once()
        self.assertTrue(any(command[0] == "openssl" for command in system.commands))
        metadata = json.loads(gateway.journal_path.read_bytes())["metadata"]
        authority = gateway.context.var / "gateway/authority/ca-cert.pem"
        self.assertEqual(metadata["tls_ca"], str(authority))
        self.assertIn(("sudo", "install", "-o", "root", "-g", "root", "-m", "0644", str(authority),
                       str(gateway.root / "tls-cert.pem")), system.commands)
        handshake(Path(metadata["tls_cert"]), Path(metadata["tls_key"]), authority, "house.example.test")
        self.assertTrue(any("migrate" in command for command in system.commands))
        self.assertTrue(any("--now" in command for command in system.commands))
        self.assertEqual(json.loads(gateway.journal_path.read_bytes())["phase"], "complete")
        profile = json.loads((gateway.context.var / "gateway/current.json").read_bytes())
        self.assertEqual(profile["url"], "https://house.example.test:13600")
        self.assertEqual(profile["ca_file"], str(gateway.root / "tls-cert.pem"))
        self.assertEqual(set(profile), {"version", "url", "ca_file", "release", "state", "versions", "journal"})

    def test_contract_inputs_ignore_poisoned_working_provider_and_theme(self):
        import oh_paths
        from oh_features import remote
        from oh_integrations import globalcache_ir
        frozen = private_directory(self.root / "committed")
        dirty = private_directory(self.root / "working")
        for root in (frozen, dirty):
            private_directory(root / "instance/ir")
        (frozen / "instance/theme.json").write_text('{"name":"committed","extends":"thermite"}')
        (dirty / "instance/theme.json").write_text("invalid dirty theme")
        profile = {"version": 1, "make": "Sony", "model": "UBP-X800M2", "source": "fictional fixture",
                   "commands": {"play": {"code": "38000,1,1,24,24", "verified": True}}}
        (frozen / "instance/ir/disc.json").write_text(json.dumps(profile))
        (dirty / "instance/ir/disc.json").write_text("invalid dirty provider")
        device = {"kind": "disc_player", "model": "UBP-X800M2",
                  "ir": {"model": "iTach IP2IR", "ip": "192.0.2.25", "connector": 1, "profile": "ir/disc.json"}}
        with patch.object(remote, "INSTANCE", dirty / "instance"), patch.object(globalcache_ir, "INSTANCE", dirty / "instance"):
            before = oh_paths.SITE
            with site_inputs(frozen):
                self.assertEqual(remote.palette({"theme": "theme.json"}).name, "committed")
                self.assertEqual(globalcache_ir.commissioned(device)["commands"], {"play": "38000,1,1,24,24"})
                self.assertEqual(oh_paths.SITE, frozen)
            self.assertEqual(oh_paths.SITE, before)
            self.assertEqual(remote.INSTANCE, dirty / "instance")
            self.assertEqual(globalcache_ir.INSTANCE, dirty / "instance")


class CertificateProfile(PrivateFixture):
    """New private authority + leaf; renewal; replacing the former self-signed profile."""

    gateway, unit_commands = Lifecycle.gateway, Lifecycle.unit_commands

    def setUp(self):
        super().setUp()
        from oh_install.runner import _TASK_APPROVAL
        token = _TASK_APPROVAL.set(True)
        self.addCleanup(_TASK_APPROVAL.reset, token)

    def text(self, path):
        return subprocess.run(["openssl", "x509", "-noout", "-text", "-in", str(path)], capture_output=True,
                              text=True, check=True).stdout

    def days(self, path):
        dates = subprocess.run(["openssl", "x509", "-noout", "-startdate", "-enddate", "-in", str(path)],
                               capture_output=True, text=True, check=True).stdout
        values = dict(line.split("=", 1) for line in dates.splitlines())
        return (ssl.cert_time_to_seconds(values["notAfter"]) - ssl.cert_time_to_seconds(values["notBefore"])) / 86400

    def adopted(self, gateway, system, **changes):
        """A completed journal whose metadata came from an existing gateway."""
        journal = json.loads(gateway.journal_path.read_bytes())
        journal["metadata"].update(changes)
        journal.update(phase="complete", plan=str(gateway.directory / "plan-old"))
        json_private(gateway.journal_path, journal)
        system.installed = Path(journal["stage"]["destination"])
        return journal

    def test_authority_signs_only_a_short_lived_server_leaf(self):
        gateway, system = self.gateway(), real_openssl(Host())
        metadata = json.loads(gateway.journal_path.read_bytes())["metadata"]
        authority = Path(metadata["tls_ca"])
        self.assertEqual(oct(authority.parent.stat().st_mode & 0o777), oct(0o700))
        self.assertEqual(oct((authority.parent / "ca-key.pem").stat().st_mode & 0o777), oct(0o600))
        ca = self.text(authority)
        self.assertIn("CA:TRUE, pathlen:0", ca)
        self.assertIn("Certificate Sign, CRL Sign", ca)
        leaf = self.text(metadata["tls_cert"])
        for expected in ("CA:FALSE", "TLS Web Server Authentication", "Digital Signature", "DNS:house.example.test",
                         "Issuer: CN=Thermite gateway authority house.example.test"):
            self.assertIn(expected, leaf)
        self.assertLessEqual(self.days(metadata["tls_cert"]), 825)
        self.assertEqual(len(Path(metadata["tls_cert"]).read_bytes().split(b"BEGIN CERTIFICATE")), 3)
        handshake(Path(metadata["tls_cert"]), Path(metadata["tls_key"]), authority, "house.example.test")
        cert, key = gateway._issue(system, self.root / "multi", ["house.example.test", "192.0.2.10", "127.0.0.1"],
                                   (authority, authority.parent / "ca-key.pem"))
        self.assertIn("IP Address:192.0.2.10", self.text(cert))
        handshake(cert, key, authority, "127.0.0.1")
        self.assertEqual(sorted(path.name for path in cert.parent.iterdir()), ["tls-cert.pem", "tls-key.pem"])

    def test_former_self_signed_profile_is_replaced_once_with_a_trust_notice(self):
        gateway, system = self.gateway(), real_openssl(Host())
        legacy = private_directory(self.root / "adopted")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-sha256", "-nodes", "-days", "3650", "-subj", "/CN=house.example.test",
                        "-addext", "subjectAltName=DNS:house.example.test",
                        "-keyout", str(legacy / "tls-key.pem"), "-out", str(legacy / "tls-cert.pem")],
                       check=True, capture_output=True)
        journal = self.adopted(gateway, system, tls_cert=str(legacy / "tls-cert.pem"),
                               tls_key=str(legacy / "tls-key.pem"))
        journal["metadata"].pop("tls_ca")
        metadata = gateway._certificate(system, journal, journal["metadata"])
        self.assertNotEqual(metadata["tls_cert"], str(legacy / "tls-cert.pem"))
        authority = gateway.context.var / "gateway/authority/ca-cert.pem"
        self.assertEqual(metadata["tls_ca"], str(authority))
        handshake(Path(metadata["tls_cert"]), Path(metadata["tls_key"]), authority, "house.example.test")
        notices = [message for message in system.messages if "Gateway TLS" in message]
        self.assertEqual(notices, ["   Gateway TLS now uses a new private certificate authority: trust "
                                   + str(gateway.root / "tls-cert.pem") + " once on every browser and terminal client"])
        # A completed deployment restages with its rollback data retained.
        saved = json.loads(gateway.journal_path.read_bytes())
        self.assertEqual((saved["phase"], saved["metadata"]), ("inputs", metadata))
        self.assertNotIn("stage", saved)
        self.assertEqual(saved["previous_release"], str(system.installed))
        self.assertEqual(saved["runtime_token_file"], journal["runtime_token_file"])
        self.assertTrue(list(gateway.directory.glob("history-*.json")))
        system.messages.clear()
        self.assertEqual(gateway._certificate(system, saved, saved["metadata"]), metadata)
        self.assertEqual(system.messages, [])

    def test_leaf_near_expiry_is_renewed_by_the_same_authority(self):
        gateway, system = self.gateway(), real_openssl(Host())
        journal = json.loads(gateway.journal_path.read_bytes())
        authority = Path(journal["metadata"]["tls_ca"])
        cert, key = gateway._issue(system, self.root / "short", ["house.example.test"],
                                   (authority, authority.parent / "ca-key.pem"), days=30)
        journal = self.adopted(gateway, system, tls_cert=str(cert), tls_key=str(key))
        before = authority.read_bytes()
        metadata = gateway._certificate(system, journal, journal["metadata"])
        self.assertNotEqual(metadata["tls_cert"], str(cert))
        self.assertEqual(authority.read_bytes(), before)
        self.assertGreater(self.days(metadata["tls_cert"]), 800)
        self.assertTrue(any("renewed by its private authority" in message and "need no change" in message
                            for message in system.messages))

    def test_staging_under_way_keeps_its_certificate_until_the_next_run(self):
        gateway, system = self.gateway(), real_openssl(Host())
        journal = json.loads(gateway.journal_path.read_bytes())
        journal["metadata"]["tls_cert"] = str(self.root / "missing.pem")
        before = dict(journal["metadata"])
        self.assertEqual(gateway._certificate(system, journal, journal["metadata"]), before)
        self.assertFalse(any(command[0] == "openssl" for command in system.commands))

    def test_names_cannot_inject_openssl_configuration(self):
        for host in ("house.example.test\nbasicConstraints = CA:TRUE", "house example", "house/x", ""):
            with self.subTest(host=host), self.assertRaisesRegex(AccessError, "literal hostname"):
                openssl_config([host])
        self.assertIn("subjectAltName = DNS:house.example.test,IP:192.0.2.10\n",
                      openssl_config(["house.example.test", "192.0.2.10"]))


if __name__ == "__main__":
    unittest.main()
