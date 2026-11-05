"""Unified gateway deployment preserves authority through failure and retry."""

import json
import hashlib
from contextlib import contextmanager
import http.server
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_ports
from oh_access import AccessError, deployment, staging
from oh_access.files import json_private, private_directory, atomic_private
from oh_install.access import Gateway, installed_release, openssl_config, served_names, site_inputs, subject_names
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


@contextmanager
def https_security(chain, key):
    """A loopback HTTPS server answering the gateway's security endpoint, as
    the installer's readiness check reads it: (its port, the requests seen)."""
    requests = []

    class Security(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers["Host"]))
            body = json.dumps({"enabled": False, "revision": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, key)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Security)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], requests
    finally:
        server.shutdown()
        server.server_close()


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


class Adoption(PrivateFixture):
    def config(self):
        release = private_directory(self.root / "old-release")
        state = private_directory(self.root / "existing-state")
        cert = self.private("old-release/cert", "existing public certificate")
        key = self.private("old-release/key", "existing private key")
        self.private("old-release/callbacks.json", '{"exact":"route and peer"}')
        config = {"version": 1, "state": str(state), "contract_file": str(release / "contract.json"),
                  "runtime_url": "http://127.0.0.1:13602", "runtime_token_file": str(release / "bearer"),
                  "allowed_hosts": ["house.example.test"], "bind_host": "192.0.2.5", "port": 13601,
                  "timezone": "UTC", "tls_cert": str(cert), "tls_key": str(key)}
        json_private(release / "config.json", config)
        return release, state

    def test_adopt_retains_private_state_and_exact_tls_callback_material(self):
        release, state = self.config()
        self.private("existing-state/site-id", "permanent-site")
        result = deployment.adopt(release / "config.json", self.root / "adopted", uid=os.geteuid(), gid=os.getegid(), service_uid=os.geteuid())
        self.assertEqual(result["state"], str(state))
        self.assertFalse({"allowed_hosts", "bind_host", "timezone"} & set(result),
                         "the installer derives them from the host each run")
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
        config = self.private("release/config.json", json.dumps({"runtime_url": "http://127.0.0.1:13602", "runtime_token_file": str(token)}))
        expected = self.private("operator/expected.json", json.dumps({"runtime_url": "http://127.0.0.1:13602",
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


#: The names the fixture host's gateway derives: hostname, address, loopback.
HOSTS = ["house.example.test", "192.0.2.10", "localhost", "127.0.0.1"]


class Host:
    def __init__(self):
        self.commands, self.messages = [], []
        self.active = False
        self.fail_migration = False
        self.fail_upstream = False
        self.installed = None
        self.unit_bytes = {}
        # No callback relay is installed unless a test installs one.
        self.overrides = {staging.ACCESS_UNITS[2]: UnitOrigin(load_state="not-found", active="inactive")}

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
        if argv[0] == "openssl":
            # Certificate checks read the fixture's real certificates.
            result = subprocess.run(list(argv), capture_output=True, text=True, check=False)
            return result.returncode, result.stdout
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


class GatewayFixture(PrivateFixture):
    def setUp(self):
        super().setUp()
        from oh_install.runner import _TASK_APPROVAL
        token = _TASK_APPROVAL.set(True)
        self.addCleanup(_TASK_APPROVAL.reset, token)
        self.staged = []
        staged = patch.object(staging, "stage", side_effect=self.stage)
        staged.start()
        self.addCleanup(staged.stop)

    def unit_commands(self, gateway, release):
        commands = []
        for name, source in staging.units(release, state=self.root / "state").items():
            path = gateway.directory / "test-units" / name
            atomic_private(path, source.encode())
            commands.append(["sudo", "install", str(path), "/etc/systemd/system/" + name])
        return commands

    def stage(self, release, **kwargs):
        """staging.stage's receipt for `release`, as the gateway runs it."""
        self.staged.append({"release": release, **kwargs})
        command = ["sudo", "-u", "thermite", "-g", "thermite", str(release / "env/bin/python"),
                   str(release / "program/bin/thermite"), "access"]
        return {"destination": str(release), "persistent_state": kwargs["state"], "config": str(release / "config.json"),
                "units": ["thermite-access.service", "thermite-access-worker.service"],
                "ownership_commands": [["sudo", "/usr/bin/python3", "-I", "-S", str(self.root / "custody.py"), "custody.json"]],
                "verification_command": [*command, "verify", "--config", str(release / "config.json")],
                "unit_install_commands": self.unit_commands(self.current, release)}

    def gateway(self, *, inputs=True, transaction=None, context=None):
        """A gateway for a fictional site; with `inputs`, the private
        authority and a served leaf already in access/inputs."""
        if context is None:
            area = private_directory(self.root / "site")
            context = SimpleNamespace(area=area, var=area / "var", head="a" * 40, conf_digest="c" * 64,
                                      application={"version": "1.2.3", "digest": "b" * 64},
                                      installation=SimpleNamespace(
                                          access=self.root / "production" / "access",
                                          state=SimpleNamespace(textfile=self.root / "production" / "runtime" / "textfile")),
                                      hostname="house.example.test",
                                      address="192.0.2.10", host={"timezone": "UTC"}, configuration=area,
                                      url="http://127.0.0.1:13602", token_file=Path("var/token"))
            atomic_private(context.area / context.token_file, os.urandom(32).hex().encode())
        gateway = Gateway(context, boundary=lambda system, metadata, callback: callback, transaction=transaction)
        self.current = gateway
        for name, value in (("_verify_boundary", None), ("_assets", self.root / "managed-assets")):
            replaced = patch.object(gateway, name, return_value=value)
            replaced.start()
            self.addCleanup(replaced.stop)
        if inputs and not (gateway.inputs / "tls-key.pem").exists():
            system = real_openssl(Host())
            gateway._issue(system, gateway.inputs, HOSTS, gateway._authority(system, "house.example.test"))
        return gateway

    def journal(self, gateway):
        return json.loads(gateway.journal_path.read_bytes())


class Lifecycle(GatewayFixture):
    def test_partial_unit_set_is_never_treated_as_a_new_identity(self):
        for name in staging.ACCESS_UNITS[1:]:
            system = Host()
            system.overrides[name] = UnitOrigin(fragment="/etc/systemd/system/" + name, active="active")
            with self.subTest(unit=name), self.assertRaisesRegex(AccessError, "related access units remain"):
                installed_release(system)
            self.assertEqual(system.commands, [])

    def test_every_access_unit_logs_under_its_own_name_not_env(self):
        units = staging.units(self.root / "release", state=self.root / "state", callbacks=True)
        self.assertEqual(set(units), set(staging.ACCESS_UNITS))
        for name, text in units.items():
            with self.subTest(unit=name):
                service = text.split("[Service]\n", 1)[1].split("\n\n", 1)[0].splitlines()
                self.assertTrue(any(line.startswith("ExecStart=:/usr/bin/env -- ") for line in service))
                self.assertEqual([line for line in service if line.startswith("SyslogIdentifier=")],
                                 ["SyslogIdentifier=" + name.removesuffix(".service")])

    def test_failed_migration_never_activates_and_a_rerun_stages_a_new_release(self):
        gateway, system = self.gateway(), Host()
        system.installed = Path("/srv/old-release")
        system.active = True
        system.fail_migration = True
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 1)
            self.assertFalse(any("--now" in command for command in system.commands))
            self.assertEqual(self.journal(gateway)["phase"], "staged")
            migration = next(index for index, command in enumerate(system.commands) if "migrate" in command)
            stops = [index for index, command in enumerate(system.commands) if "stop" in command]
            self.assertEqual(len(stops), 2)
            self.assertLess(max(stops), migration)
            self.assertFalse(any("backup" in " ".join(command) for command in system.commands),
                             "the install transaction sets the identity state aside")
            self.assertIn("   Gateway deployment is incomplete; rerun the same install command to stage it again.",
                          system.messages)
            self.assertIn("   Previous gateway release retained: /srv/old-release", system.messages)
            system.fail_migration = False
            again = self.gateway(context=gateway.context)
            self.assertEqual(again.perform(system), 0, system.messages)
        journal = self.journal(again)
        self.assertEqual((journal["phase"], journal["previous_release"]), ("complete", "/srv/old-release"))
        self.assertEqual([entry["release"] for entry in self.staged], [gateway.release, again.release])
        self.assertNotEqual(gateway.release, again.release)

    def test_a_transaction_names_the_release_and_its_record_after_itself(self):
        gateway, system = self.gateway(transaction="20261105T101500Z-0a1b2c"), Host()
        self.assertEqual(gateway.release, gateway.root / "releases/20261105T101500Z-0a1b2c")
        self.assertEqual(gateway.directory, gateway.context.var / "transaction/20261105T101500Z-0a1b2c/gateway")
        system.fail_migration = True
        self.assertEqual(gateway.perform(system), 1)
        self.assertIn("   Gateway deployment is incomplete; the install transaction puts the previous gateway back.",
                      system.messages)
        self.assertFalse(any("rerun" in message for message in system.messages))

    def test_upgrade_moves_https_to_the_host_files_port_and_checks_readiness_there(self):
        gateway, system = self.gateway(inputs=False), real_openssl(Host())
        # The installed version 2 release serves HTTPS on 13601, with TLS
        # material in its own release: the inputs had no fixed paths then.
        old = private_directory(self.root / "old-release")
        authority = gateway._authority(system, "house.example.test")
        cert, key = gateway._issue(system, old / "tls", HOSTS, authority)
        json_private(old / "config.json", {
            "version": 2, "state": str(gateway.root / "state"), "contract_file": str(old / "contract.json"),
            "runtime_url": "http://127.0.0.1:13602", "runtime_token_file": str(old / "runtime-bearer"),
            "allowed_hosts": HOSTS, "bind_host": "0.0.0.0", "port": 13601,
            "tls_cert": str(cert), "tls_key": str(key), "timezone": "UTC", "guest_http": None})
        system.installed, system.active = old, True
        helper = gateway._helper

        def adopt_or_helper(system, *argv):
            if argv[0] != "adopt":
                return helper(system, *argv)
            deployment.adopt(argv[1], argv[2], uid=os.geteuid(), gid=os.getegid(), service_uid=os.geteuid())

        with https_security(cert, key) as (port, requests), \
                patch.object(gateway, "_helper", side_effect=adopt_or_helper), \
                patch("oh_install.access.service_identity", return_value=(os.geteuid(), os.getegid())):
            gateway.context.host = {"timezone": "UTC", "gateway": {"port": port}}
            self.assertEqual(gateway.perform(system), 0, system.messages)
        [staged] = self.staged
        self.assertEqual((staged["port"], staged["host_ports"]["access"]), (port, port))
        self.assertEqual((staged["tls_cert"], staged["tls_key"]),
                         (str(gateway.inputs / "tls-cert.pem"), str(gateway.inputs / "tls-key.pem")))
        self.assertEqual((gateway.inputs / "tls-key.pem").read_bytes(), key.read_bytes(), "the served key is kept")
        journal = self.journal(gateway)
        self.assertEqual((journal["metadata"]["port"], journal["previous_release"]), (port, str(old)))
        self.assertIn(f"   Gateway HTTPS moves from port 13601 to {port}: update bookmarks, home-screen apps "
                      "and terminal --url", system.messages)
        # The old listener's process stops before the new release starts, and
        # readiness is checked, and the terminal profile saved, on the new port.
        stops = [index for index, command in enumerate(system.commands) if "stop" in command]
        start = next(index for index, command in enumerate(system.commands) if "--now" in command)
        self.assertTrue(stops)
        self.assertLess(max(stops), start)
        self.assertEqual(requests, [("/api/v1/security", f"house.example.test:{port}")])
        self.assertIn(f"   Gateway verified: https://house.example.test:{port}", system.messages)
        profile = json.loads((gateway.context.var / "gateway/current.json").read_bytes())
        self.assertEqual(profile["url"], f"https://house.example.test:{port}")

    def test_an_adopted_callback_map_moves_to_this_allocations_port_and_runtime(self):
        gateway, system = self.gateway(inputs=False), real_openssl(Host())
        authority = gateway._authority(system, "house.example.test")
        # Installed when the native runtime had 13601 and the relay 13614.
        old = private_directory(self.root / "old-release")
        cert, key = gateway._issue(system, old / "tls", HOSTS, authority)
        # Its hosts, bind address and timezone are another host's.
        json_private(old / "config.json", {
            "version": 2, "state": str(gateway.root / "state"), "contract_file": str(old / "contract.json"),
            "runtime_url": "http://127.0.0.1:13601", "runtime_token_file": str(old / "runtime-bearer"),
            "allowed_hosts": ["old-name.example.test", "192.0.2.5"], "bind_host": "192.0.2.5", "port": 13600,
            "tls_cert": str(cert), "tls_key": str(key), "timezone": "America/Chicago",
            "guest_http": None})
        routes = {"/upnpcallback/dev/RINCON_02000000000101400_MR/svc/upnp-org/RenderingControl/event/cb": "192.0.2.40"}
        json_private(old / "callbacks.json", {"version": 1, "routes": routes, "upstream": "http://127.0.0.1:13601",
                                              "hosts": ["192.0.2.10:13614"], "bind_host": "192.0.2.10", "port": 13614})
        system.installed = old
        adopted = []

        def adopt(system, *argv):
            self.assertEqual(argv[0], "adopt")
            adopted.append(argv)
            deployment.adopt(argv[1], argv[2], uid=os.geteuid(), gid=os.getegid(), service_uid=os.geteuid())

        private_directory(gateway.directory)
        with patch.object(gateway, "_helper", side_effect=adopt), \
                patch("oh_install.access.service_identity", return_value=(os.geteuid(), os.getegid())):
            metadata = gateway._metadata(system, old)
            self.assertEqual(gateway._metadata(system, old)["callback_file"], metadata["callback_file"])
        self.assertEqual(len(adopted), 1, "inputs at their fixed paths are adopted once")
        self.assertEqual((metadata["port"], metadata["callbacks"]), (oh_ports.ACCESS_PORT, True))
        self.assertEqual((metadata["allowed_hosts"], metadata["bind_host"], metadata["timezone"]),
                         (HOSTS, "0.0.0.0", "UTC"), "derived from this host, never adopted")
        self.assertEqual(metadata["state"], str(gateway.root / "state"), "identity state is adopted")
        self.assertEqual(metadata["callback_file"], str(gateway.inputs / "callbacks.json"))
        self.assertEqual(json.loads(Path(metadata["callback_file"]).read_bytes()), {
            "version": 1, "routes": routes, "upstream": "http://127.0.0.1:13602",
            "hosts": [f"192.0.2.10:{oh_ports.CALLBACK_PORT}"], "bind_host": "192.0.2.10", "port": oh_ports.CALLBACK_PORT})
        self.assertIn(f"   Sonos event callbacks move from port 13614 to {oh_ports.CALLBACK_PORT}", system.messages)
        self.assertIn(f"   Gateway HTTPS moves from port 13600 to {oh_ports.ACCESS_PORT}: update bookmarks, "
                      "home-screen apps and terminal --url", system.messages)

    def test_identity_state_elsewhere_is_refused_before_anything_stops(self):
        gateway, system = self.gateway(inputs=False), real_openssl(Host())
        old = private_directory(self.root / "old-release")
        system.installed = old

        def adopt(system, *argv):
            destination = private_directory(argv[2])
            for name in ("tls-cert.pem", "tls-key.pem"):
                atomic_private(destination / name, b"fixture")
            json_private(destination / "adopted.json", {
                "state": str(self.root / "elsewhere"), "runtime_url": "http://127.0.0.1:13602",
                "port": oh_ports.ACCESS_PORT, "previous_release": str(old), "callbacks": False,
                "tls_cert": str(destination / "tls-cert.pem"), "tls_key": str(destination / "tls-key.pem")})

        with patch.object(gateway, "_helper", side_effect=adopt), \
                patch("oh_install.access.service_identity", return_value=(os.geteuid(), os.getegid())):
            self.assertEqual(gateway.perform(system), 1)
        self.assertTrue(any("keeps its identity state in" in message for message in system.messages), system.messages)
        self.assertFalse(any("stop" in command or "migrate" in command for command in system.commands))

    def test_not_found_units_do_not_block_fresh_deployment(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0)
        self.assertTrue(any("migrate" in command for command in system.commands))
        self.assertFalse(any("stop" in command for command in system.commands))

    def test_commissioning_failure_stops_before_staging_or_migration(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_boundary", side_effect=ValueError("native commissioning failed")):
            self.assertEqual(gateway.perform(system), 1)
        self.assertIn("   native commissioning failed", system.messages)
        self.assertEqual(self.staged, [])
        self.assertFalse(any("migrate" in command or "--now" in command for command in system.commands))

    def test_installed_private_release_is_discovered_from_unit_not_readability(self):
        system = Host()
        system.installed = Path("/srv/private-release")
        self.assertEqual(installed_release(system), system.installed)
        with patch.object(system, "unit_file", return_value=UnitFile(b"", 1000, 0o644)):
            with self.assertRaisesRegex(ValueError, "root custody"):
                installed_release(system)

    def test_the_unit_staging_writes_identifies_its_release_and_repeats_are_refused(self):
        release = Path("/srv/thermite/access/releases/fixture")
        unit = staging.units(release, state=Path("/srv/thermite/access/state"),
                             textfile=Path("/srv/thermite/runtime/textfile"))["thermite-access.service"]
        # Its list directives repeat: each writable path is its own line.
        self.assertEqual(unit.count("\nReadWritePaths="), 2)
        system = Host()
        system.installed = release
        with patch.object(system, "unit_file", return_value=UnitFile(unit.encode(), 0, 0o644)):
            self.assertEqual(installed_release(system), release)
        for directive in ("User=thermite", "WorkingDirectory=/srv/elsewhere"):
            repeated = unit.replace("[Service]\n", "[Service]\n" + directive + "\n")
            with self.subTest(directive=directive), \
                    patch.object(system, "unit_file", return_value=UnitFile(repeated.encode(), 0, 0o644)), \
                    self.assertRaisesRegex(AccessError, "ambiguous"):
                installed_release(system)

    def test_failed_https_never_marks_deployment_complete(self):
        gateway, system = self.gateway(), Host()
        with patch.object(gateway, "_https", side_effect=ValueError("certificate validation failed")):
            self.assertEqual(gateway.perform(system), 1)
        self.assertEqual(self.journal(gateway)["phase"], "activated")
        self.assertFalse((gateway.context.var / "gateway/current.json").exists())
        self.assertFalse(any("Gateway verified" in message for message in system.messages))

    def test_legacy_native_assets_use_gateway_transport_with_current_hashes(self):
        from oh_features.remote import FILES
        gateway = self.gateway(inputs=False)
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
        built = Gateway._assets(gateway, assets)
        script = (built / "access-config.js").read_bytes()
        self.assertIn(b'"mode":"authenticated"', script)
        self.assertIn(hashlib.sha256(script).hexdigest()[:16], (built / "index.html").read_text())
        self.assertTrue(json.loads((built / "system.json").read_bytes())["tools"][0]["localOnly"])
        self.assertIn('"mode":"native"', (assets / "access-config.js").read_text())

    def test_each_activation_is_offered_to_the_deploy_history(self):
        gateway, system = self.gateway(), Host()
        events = []

        def record(seen, event):
            self.assertIs(seen, system)
            events.append(event)
            return False

        gateway.record = record
        with patch.object(gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0, "an unrecorded activation still succeeds")
        self.assertEqual(events, [{"commit": "a" * 40, "application": {"version": "1.2.3", "digest": "b" * 64},
                                   "release": str(gateway.release), "previous_release": None}])
        self.assertIn("   The gateway release was not added to thermite history; its record stays in "
                      + str(gateway.journal_path), system.messages)

    def test_an_unknown_worker_override_fails_verification_without_a_profile(self):
        gateway, system = self.gateway(), Host()
        system.installed = Path("/srv/old-release")
        name = "thermite-access-worker.service"
        system.overrides[name] = UnitOrigin(fragment="/etc/systemd/system/" + name,
                                           drop_ins="/etc/systemd/system/thermite-access-worker.service.d/custom.conf")
        with patch.object(gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 1)
        self.assertTrue(any("unknown unit provenance or overrides" in message for message in system.messages), system.messages)
        self.assertFalse((gateway.context.var / "gateway/current.json").exists())

    def test_a_rotated_runtime_token_is_staged_with_the_same_identity_and_tls(self):
        gateway, system = self.gateway(), Host()
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0)
            system.installed, system.active = gateway.release, True
            atomic_private(gateway.context.area / gateway.context.token_file, b"new-runtime-credential")
            again = self.gateway(context=gateway.context)
            self.assertEqual(again.perform(system), 0, system.messages)
        first, second = self.staged
        self.assertEqual(Path(second["runtime_token_file"]).read_bytes(), b"new-runtime-credential")
        for key in ("state", "tls_cert", "tls_key"):
            self.assertEqual(second[key], first[key])
        journal = self.journal(again)
        self.assertEqual(journal["previous_release"], str(gateway.release))
        self.assertEqual(journal["runtime_credential_sha256"], hashlib.sha256(b"new-runtime-credential").hexdigest())
        self.assertFalse(any("new-runtime-credential" in message or journal["runtime_credential_sha256"] in message
                             for message in system.messages))

    def test_staged_auth_failure_stops_before_migration_or_activation(self):
        gateway, system = self.gateway(), Host()
        system.fail_upstream = True
        with patch.object(gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 1)
        self.assertFalse(any("migrate" in command or "--now" in command or "stop" in command for command in system.commands))
        self.assertNotEqual(self.journal(gateway)["phase"], "complete")
        self.assertFalse((gateway.context.var / "gateway/current.json").exists())

    def test_fresh_install_prepares_tls_and_release_without_manual_staging(self):
        gateway, system = self.gateway(inputs=False), real_openssl(Host())
        guest_http = {"listen": ["192.0.2.10"], "clients": ["192.0.2.0/24"]}
        gateway.context.host = {**gateway.context.host, "guest_http": guest_http}
        with patch.object(gateway, "_https", return_value="https://house.example.test:13601"), \
                patch.object(gateway, "_guest_http", return_value=["   Guest HTTP verified: http://192.0.2.10:13600/"]):
            self.assertEqual(gateway.perform(system), 0, system.messages)
        [staged] = self.staged
        self.assertEqual(staged["guest_http"], guest_http)
        self.assertEqual(staged["host_ports"]["guest_http"], 13600)
        self.assertEqual(staged["state"], str(gateway.root / "state"))
        self.assertEqual(staged["house"], gateway.context.configuration / "instance/devices.yaml")
        self.assertEqual(staged["assets"], self.root / "managed-assets")
        self.assertEqual(staged["runtime_url"], gateway.context.url)
        self.assertIn("127.0.0.1", staged["allowed_hosts"])
        self.assertIn("   Guest HTTP verified: http://192.0.2.10:13600/", system.messages)
        self.assertTrue(any(command[0] == "openssl" for command in system.commands))
        metadata = self.journal(gateway)["metadata"]
        authority = gateway.context.var / "gateway/authority/ca-cert.pem"
        self.assertEqual(metadata["tls_ca"], str(authority))
        self.assertEqual(metadata["tls_cert"], str(gateway.inputs / "tls-cert.pem"))
        self.assertIn(("sudo", "install", "-o", "root", "-g", "root", "-m", "0644", str(authority),
                       str(gateway.root / "tls-cert.pem")), system.commands)
        handshake(Path(metadata["tls_cert"]), Path(metadata["tls_key"]), authority, "house.example.test")
        self.assertTrue(any("migrate" in command for command in system.commands))
        self.assertTrue(any("--now" in command for command in system.commands))
        self.assertEqual(self.journal(gateway)["phase"], "complete")
        profile = json.loads((gateway.context.var / "gateway/current.json").read_bytes())
        self.assertEqual(profile["url"], "https://house.example.test:13601")
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


class CertificateProfile(GatewayFixture):
    """New private authority + leaf; renewal; replacing the former self-signed profile."""

    def text(self, path):
        return subprocess.run(["openssl", "x509", "-noout", "-text", "-in", str(path)], capture_output=True,
                              text=True, check=True).stdout

    def days(self, path):
        dates = subprocess.run(["openssl", "x509", "-noout", "-startdate", "-enddate", "-in", str(path)],
                               capture_output=True, text=True, check=True).stdout
        values = dict(line.split("=", 1) for line in dates.splitlines())
        return (ssl.cert_time_to_seconds(values["notAfter"]) - ssl.cert_time_to_seconds(values["notBefore"])) / 86400

    def served(self, system):
        """The gateway and the inputs it serves now."""
        gateway = self.gateway()
        return gateway, gateway._metadata(system, None)

    def test_authority_signs_only_a_short_lived_server_leaf(self):
        system = real_openssl(Host())
        gateway, metadata = self.served(system)
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
        system = real_openssl(Host())
        gateway, metadata = self.served(system)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-sha256", "-nodes", "-days", "3650", "-subj", "/CN=house.example.test",
                        "-addext", "subjectAltName=DNS:house.example.test",
                        "-keyout", metadata["tls_key"], "-out", metadata["tls_cert"]],
                       check=True, capture_output=True)
        legacy = Path(metadata["tls_cert"]).read_bytes()
        metadata = gateway._certificate(system, metadata)
        self.assertNotEqual(Path(metadata["tls_cert"]).read_bytes(), legacy)
        self.assertEqual(metadata["tls_cert"], str(gateway.inputs / "tls-cert.pem"), "served from the fixed inputs")
        authority = gateway.context.var / "gateway/authority/ca-cert.pem"
        self.assertEqual(metadata["tls_ca"], str(authority))
        handshake(Path(metadata["tls_cert"]), Path(metadata["tls_key"]), authority, "house.example.test")
        notices = [message for message in system.messages if "Gateway TLS" in message]
        self.assertEqual(notices, ["   Gateway TLS now uses a new private certificate authority: trust "
                                   + str(gateway.root / "tls-cert.pem") + " once on every browser and terminal client"])
        system.messages.clear()
        self.assertEqual(gateway._certificate(system, metadata), metadata)
        self.assertEqual(system.messages, [])

    def test_leaf_near_expiry_is_renewed_by_the_same_authority(self):
        system = real_openssl(Host())
        gateway, metadata = self.served(system)
        authority = Path(metadata["tls_ca"])
        gateway._issue(system, gateway.inputs, ["house.example.test"], (authority, authority.parent / "ca-key.pem"), days=30)
        short, before = Path(metadata["tls_cert"]).read_bytes(), authority.read_bytes()
        metadata = gateway._certificate(system, metadata)
        self.assertNotEqual(Path(metadata["tls_cert"]).read_bytes(), short)
        self.assertEqual(authority.read_bytes(), before)
        self.assertGreater(self.days(metadata["tls_cert"]), 800)
        self.assertTrue(any("renewed by its private authority" in message and "need no change" in message
                            for message in system.messages))

    def test_a_name_change_reissues_the_leaf_from_the_same_authority(self):
        system = real_openssl(Host())
        gateway, metadata = self.served(system)
        authority = Path(metadata["tls_ca"])
        before = authority.read_bytes()
        self.assertEqual(gateway._certificate(system, metadata), metadata, "the same names need no new certificate")
        self.assertEqual(system.messages, [])
        gateway.context.hostname, gateway.context.address = "kitchen.example.test", "192.0.2.11"
        hosts = ["kitchen.example.test", "192.0.2.11", "localhost", "127.0.0.1"]
        served = Path(metadata["tls_cert"]).read_bytes()
        metadata = gateway._certificate(system, metadata)
        self.assertEqual(metadata["allowed_hosts"], hosts)
        self.assertNotEqual(Path(metadata["tls_cert"]).read_bytes(), served)
        self.assertEqual((authority.read_bytes(), metadata["tls_ca"]), (before, str(authority)),
                         "clients keep trusting the same authority")
        listed = subprocess.run(["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", metadata["tls_cert"]],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(served_names(listed), subject_names(hosts), "exactly this host's names, none of the old")
        handshake(Path(metadata["tls_cert"]), Path(metadata["tls_key"]), authority, "kitchen.example.test")
        self.assertEqual([message for message in system.messages if "Gateway TLS" in message], [
            "   Gateway TLS certificate reissued by its private authority for " + ", ".join(hosts)
            + "; clients trusting " + str(gateway.root / "tls-cert.pem") + " need no change"])
        system.messages.clear()
        self.assertEqual(gateway._certificate(system, metadata), metadata)
        self.assertEqual(system.messages, [])

    def test_served_names_compare_addresses_canonically_and_names_without_case(self):
        listed = ("X509v3 Subject Alternative Name: \n"
                  "    DNS:House.example.test, IP Address:192.0.2.10, IP Address:2001:DB8:0:0:0:0:0:1, DNS:localhost\n")
        self.assertEqual(served_names(listed), subject_names(["house.example.test", "192.0.2.10", "2001:db8::1", "localhost"]))
        self.assertEqual(served_names("No extensions in certificate\n"), set())
        self.assertNotEqual(served_names(listed), subject_names(["house.example.test", "192.0.2.10", "localhost"]))

    def test_names_cannot_inject_openssl_configuration(self):
        for host in ("house.example.test\nbasicConstraints = CA:TRUE", "house example", "house/x", ""):
            with self.subTest(host=host), self.assertRaisesRegex(AccessError, "literal hostname"):
                openssl_config([host])
        self.assertIn("subjectAltName = DNS:house.example.test,IP:192.0.2.10\n",
                      openssl_config(["house.example.test", "192.0.2.10"]))


if __name__ == "__main__":
    unittest.main()
