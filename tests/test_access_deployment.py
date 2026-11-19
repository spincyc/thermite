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
import oh_units
from oh_access import AccessError, deployment, staging
from oh_access.files import json_private, private_directory, atomic_private
from oh_install import access
from oh_install.access import (Gateway, GatewayInputs, installed_release, openssl_config, served_names, site_inputs,
                               subject_names)
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
        for side in (talking, listening):
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
            body = json.dumps({"bootstrap_required": True, "setup_available": False, "guest_text": True,
                               "guest_cameras": False}).encode()
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


class PrivateFiles(PrivateFixture):
    def test_a_private_file_and_the_directory_its_rename_is_in_reach_the_disk(self):
        import stat
        synced, real = [], os.fsync

        def fsync(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            real(descriptor)
        with patch.object(os, "fsync", fsync):
            path = self.private("record.json", "{}")
        self.assertEqual(synced, [False, True])
        self.assertEqual(path.read_text(), "{}")

    def test_a_directory_its_file_system_cannot_sync_leaves_the_file_in_place(self):
        import errno
        import stat
        real = os.fsync

        def fsync(descriptor, code=errno.EINVAL):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(code, os.strerror(code))
            real(descriptor)
        for code in (errno.EINVAL, errno.ENOTSUP):
            with patch.object(os, "fsync", lambda descriptor: fsync(descriptor, code)):  # noqa: B023 (called in this iteration)
                path = self.private("record.json", str(code))
            self.assertEqual(path.read_text(), str(code))
        # Any other failure: the file is in place, but a crash may undo it.
        from oh_access.files import Unsynced
        with patch.object(os, "fsync", lambda descriptor: fsync(descriptor, errno.EIO)), \
                self.assertRaises(Unsynced) as unsynced:
            self.private("record.json", "unsynced")
        self.assertEqual(unsynced.exception.errno, errno.EIO)
        self.assertEqual((self.root / "record.json").read_text(), "unsynced")


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

                # ruff: disable[B023] (called in this iteration)
                def factory(*args, **kwargs):
                    status = next(remaining)
                    response = SimpleNamespace(status=200 if status == "malformed" else status,
                                               read=lambda maximum: b"{}" if status == "malformed" else b"[]")
                    return SimpleNamespace(request=lambda *args, **kwargs: requests.append(kwargs["headers"]),
                                           getresponse=lambda: response, close=lambda: closes.append(True))
                # ruff: enable[B023]

                kwargs = dict(service=(os.geteuid(), os.getegid()), connection_factory=factory,
                              clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))  # noqa: B023 (called in this iteration)
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
#: The install transaction a gateway fixture runs in, and the next one.
TRANSACTION, NEXT = "20261105T101500Z-0a1b2c", "20261105T111500Z-4d5e6f"
#: A site's pin of the Python image, the ref it makes, and the service
#: account's numbers on the fixture host.
VERSIONS = {"atv_helper": {"image": {"ref": "docker.io/library/python:3.14-slim", "digest": "sha256:" + "5" * 64}}}
IMAGE = "docker.io/library/python@sha256:" + "5" * 64
IDENTITY = (9001, 9001)


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
        #: The first process of each container, as root's podman reports it.
        self.container_pids = {container: "456" for container in oh_units.ACCESS_CONTAINERS.values()}

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
        if ("install" in argv and "-d" not in argv and Path(argv[-2]).is_file()
                and argv[-1].startswith(("/etc/systemd/system/", "/etc/containers/systemd/"))):
            self.unit_bytes[argv[-1]] = Path(argv[-2]).read_bytes()
        if "stop" in argv:
            self.active = False
        if "--now" in argv:
            self.active = True
        return 0

    def unit_origin(self, name):
        if name in self.overrides:
            return self.overrides[name]
        quadlet, *dropins = map(str, staging.destinations(name))
        if dropins and quadlet in self.unit_bytes:
            # As systemd's generator makes it from the quadlet this run installed.
            return UnitOrigin(source=quadlet, fragment="/run/systemd/generator/" + name, drop_ins=" ".join(dropins),
                              active="active" if self.active else "inactive")
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
        if tuple(argv[:5]) == ("sudo", "-n", "podman", "container", "inspect"):
            return 0, self.container_pids[argv[-1]] + "\n"
        state = "generated" if argv[2] in oh_units.ACCESS_CONTAINED else "enabled"
        active = "activating" if argv[2] == getattr(self, "inactive", None) else "active"
        return 0, f"ActiveState={active}\nUnitFileState={state}\nNeedDaemonReload=no\n"

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

    def unit_plan(self, gateway, release):
        """The units' protected copies, and the commands that install them."""
        files, commands = {}, []
        units = staging.units(release, state=self.root / "state", image=IMAGE, identity=IDENTITY)
        for name, copy, destination, source in staging.unit_files(units):
            path = gateway.directory / "test-units" / copy
            atomic_private(path, source.encode())
            files.setdefault(name, []).append([str(path), str(destination)])
            if destination.name == staging.LIFECYCLE:
                commands.append(["sudo", "install", "-d", str(destination.parent)])
            commands.append(["sudo", "install", str(path), str(destination)])
        return files, commands

    def stage(self, release, **kwargs):
        """staging.stage's receipt for `release`, as the gateway runs it."""
        self.staged.append({"release": release, **kwargs})
        command = ["sudo", "-u", "thermite", "-g", "thermite", str(release / "env/bin/python"),
                   str(release / "program/bin/thermite"), "access"]
        files, commands = self.unit_plan(self.current, release)
        return {"destination": str(release), "persistent_state": kwargs["state"], "config": str(release / "config.json"),
                "units": ["thermite-access.service", "thermite-access-worker.service"],
                "unit_files": files, "containers": {"thermite-access-worker.service": "thermite-access-worker"},
                "ownership_commands": [["sudo", "/usr/bin/python3", "-I", "-S", str(self.root / "custody.py"), "custody.json"]],
                "verification_command": [*command, "verify", "--descriptor", str(release / "config.json")],
                "unit_install_commands": commands}

    def gateway(self, *, inputs=True, transaction=TRANSACTION, context=None):
        """A gateway for a fictional site, inside install transaction
        `transaction`; with `inputs`, the private authority and a served
        leaf already in access/inputs."""
        if context is None:
            area = private_directory(self.root / "site")
            context = SimpleNamespace(area=area, var=area / "var", head="a" * 40, conf_digest="c" * 64,
                                      versions=VERSIONS,
                                      application={"version": "1.2.3", "digest": "b" * 64},
                                      installation=SimpleNamespace(
                                          access=self.root / "production" / "access",
                                          state=SimpleNamespace(root=self.root / "production" / "runtime",
                                                                textfile=self.root / "production" / "runtime" / "textfile")),
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
        units = staging.units(self.root / "release", state=self.root / "state", callbacks=True, image=IMAGE,
                              identity=IDENTITY)
        self.assertEqual(set(units), set(staging.ACCESS_UNITS))
        for name, text in units.items():
            with self.subTest(unit=name):
                service = text.split("[Service]\n", 1)[1].split("\n\n", 1)[0].splitlines()
                # A host service is started through env; a container's output is podman's.
                self.assertEqual(any(line.startswith("ExecStart=:/usr/bin/env -- ") for line in service),
                                 name not in oh_units.ACCESS_CONTAINED)
                self.assertEqual([line for line in service if line.startswith("SyslogIdentifier=")],
                                 ["SyslogIdentifier=" + name.removesuffix(".service")])

    def test_failed_migration_never_activates_and_the_next_transaction_stages_a_new_release(self):
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
            self.assertIn("   Gateway deployment is incomplete; the install transaction puts the previous gateway "
                          "back.", system.messages)
            system.fail_migration = False
            again = self.gateway(context=gateway.context, transaction=NEXT)
            self.assertEqual(again.perform(system), 0, system.messages)
        journal = self.journal(again)
        self.assertEqual((journal["phase"], journal["previous_release"]), ("complete", "/srv/old-release"))
        self.assertEqual([entry["release"] for entry in self.staged], [gateway.release, again.release])
        self.assertNotEqual(gateway.release, again.release)

    def test_a_transaction_names_the_release_and_its_record_after_itself(self):
        gateway = self.gateway()
        self.assertEqual(gateway.release, gateway.root / f"releases/{TRANSACTION}")
        self.assertEqual(gateway.directory, gateway.context.var / f"transaction/{TRANSACTION}/gateway")

    def test_upgrade_serves_https_on_the_host_files_port_and_checks_readiness_there(self):
        gateway, system = self.gateway(), real_openssl(Host())
        # The installed release served HTTPS on another port; the leaf and
        # key it served are the ones in access/inputs.
        old = private_directory(self.root / "old-release")
        cert, key = gateway.inputs / "tls-cert.pem", gateway.inputs / "tls-key.pem"
        served = key.read_bytes()
        system.installed, system.active = old, True
        with https_security(cert, key) as (port, requests):
            gateway.context.host = {"timezone": "UTC", "gateway": {"port": port, "listen": ["192.0.2.10"]}}
            self.assertEqual(gateway.perform(system), 0, system.messages)
        [staged] = self.staged
        self.assertEqual((staged["port"], staged["host_ports"]["access"]), (port, port))
        # host.yaml's chosen addresses, and loopback, where readiness was just checked.
        self.assertEqual(staged["listen"], ["127.0.0.1", "192.0.2.10"])
        self.assertEqual((staged["tls_cert"], staged["tls_key"]),
                         (str(gateway.inputs / "tls-cert.pem"), str(gateway.inputs / "tls-key.pem")))
        self.assertEqual(key.read_bytes(), served, "the served key is kept")
        journal = self.journal(gateway)
        self.assertEqual((journal["metadata"]["port"], journal["previous_release"]), (port, str(old)))
        # The old listener's process stops before the new release starts, and
        # readiness is checked, and the terminal profile saved, on the new port.
        stops = [index for index, command in enumerate(system.commands) if "stop" in command]
        start = next(index for index, command in enumerate(system.commands) if "--now" in command)
        self.assertTrue(stops)
        self.assertLess(max(stops), start)
        self.assertEqual(requests, [("/api/v1/security", f"house.example.test:{port}")])
        self.assertFalse(any("Gateway verified" in message for message in system.messages), "an upgrade says what changed")
        profile = json.loads((gateway.context.var / "gateway/current.json").read_bytes())
        self.assertEqual(profile["url"], f"https://house.example.test:{port}")

    def test_a_kept_callback_map_moves_to_this_allocations_port_and_runtime(self):
        gateway, system = self.gateway(), real_openssl(Host())
        # Written when the native runtime had 13601 and the relay 13614.
        routes = {"/upnpcallback/dev/RINCON_02000000000101400_MR/svc/upnp-org/RenderingControl/event/cb": "192.0.2.40"}
        json_private(gateway.inputs / "callbacks.json", {
            "version": 1, "routes": routes, "upstream": "http://127.0.0.1:13601", "hosts": ["192.0.2.10:13614"],
            "bind_host": "192.0.2.10", "port": 13614})
        metadata = gateway._metadata(system)
        self.assertEqual(gateway._metadata(system)["callback_file"], metadata["callback_file"])
        self.assertEqual((metadata["port"], metadata["callbacks"]), (oh_ports.ACCESS_PORT, True))
        self.assertEqual((metadata["allowed_hosts"], metadata["listen"], metadata["timezone"]),
                         (HOSTS, ["0.0.0.0"], "UTC"), "derived from this host")
        self.assertEqual(metadata["state"], str(gateway.root / "state"))
        self.assertEqual(metadata["callback_file"], str(gateway.inputs / "callbacks.json"))
        self.assertEqual(json.loads(Path(metadata["callback_file"]).read_bytes()), {
            "version": 1, "routes": routes, "upstream": "http://127.0.0.1:13602",
            "hosts": [f"192.0.2.10:{oh_ports.CALLBACK_PORT}"], "bind_host": "192.0.2.10", "port": oh_ports.CALLBACK_PORT})
        self.assertIn(f"   Sonos event callbacks move from port 13614 to {oh_ports.CALLBACK_PORT}", system.messages)

    def test_a_relay_without_its_exact_map_is_refused_before_anything_stops(self):
        relay = UnitOrigin(fragment="/etc/systemd/system/" + staging.ACCESS_UNITS[2], active="active")
        for inputs in (False, True):
            with self.subTest(fixed_inputs=inputs):
                gateway, system = self.gateway(inputs=inputs), Host()
                system.installed = private_directory(self.root / "old-release")
                system.overrides[staging.ACCESS_UNITS[2]] = relay
                self.assertEqual(GatewayInputs(gateway).perform(system), 1)
                self.assertIn("   installed callback service has no recoverable exact callback map; refusing to "
                              "retire it", system.messages)

    def test_callbacks_found_new_wait_for_the_next_transaction(self):
        # Commissioning finds Sonos callbacks no installed relay serves: the
        # map waits in access/inputs, and changeover.relay runs one more
        # install transaction for its unit.
        gateway, system = self.gateway(), Host()
        found = gateway.inputs / "callbacks.json"
        gateway.boundary = lambda system, metadata, callback: json_private(found, {"version": 1}) or found
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0, system.messages)
        self.assertTrue(gateway.deferred)
        self.assertIn("   Sonos event callbacks were found; one more install transaction activates their relay",
                      system.messages)
        [staged] = self.staged
        self.assertFalse(staged["callbacks"])
        self.assertTrue(found.exists(), "the map waits in access/inputs")

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
        unit = staging.units(release, state=Path("/srv/thermite/access/state"), image=IMAGE, identity=IDENTITY,
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
        from oh_features.remote import FILES, built
        gateway = self.gateway(inputs=False)
        assets = private_directory(self.root / "legacy-assets")
        for name in FILES:
            value = b"unchanged source"
            if name.endswith(".html"):
                value = (b'<html data-thermite-hosted="true" data-thermite-build="fedcba9876543210" lang="en">'
                         b'<script src="/static/remote/access-config.js?v=0123456789abcdef"></script>')
            elif name == "access-config.js":
                value = b'globalThis.THERMITE_ACCESS_CONFIG = {"mode":"native"};'
            elif name == "system.json":
                value = b'{"version":1,"tools":[{"id":"openhab","localOnly":false}]}'
            atomic_private(assets / name, value)
        staged = Gateway._assets(gateway, assets)
        script = (staged / "access-config.js").read_bytes()
        self.assertIn(b'"mode":"authenticated"', script)
        self.assertIn(hashlib.sha256(script).hexdigest()[:16], (staged / "index.html").read_text())
        self.assertTrue(json.loads((staged / "system.json").read_bytes())["tools"][0]["localOnly"])
        # The pages keep the build generate gave them, which the gateway's contract reads from them.
        self.assertEqual({built((staged / name).read_bytes()) for name in FILES if name.endswith(".html")}, {"fedcba9876543210"})
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
        quadlet, lifecycle = map(str, staging.destinations(name))
        generated = dict(source=quadlet, fragment="/run/systemd/generator/" + name)
        for case, origin in (
                ("a drop-in beside the lifecycle's", UnitOrigin(**generated, drop_ins=lifecycle + " " + lifecycle.replace("50-container-lifecycle", "custom"))),
                ("no lifecycle drop-in", UnitOrigin(**generated)),
                ("a unit file in place of the generated service", UnitOrigin(fragment="/etc/systemd/system/" + name, drop_ins=lifecycle)),
                ("another quadlet", UnitOrigin(**{**generated, "source": quadlet.replace("worker", "other")}, drop_ins=lifecycle)),
                ("a transient unit", UnitOrigin(**generated, drop_ins=lifecycle, transient=True))):
            with self.subTest(case=case):
                gateway, system = self.gateway(transaction=TRANSACTION[:-1] + str(len(self.staged))), Host()
                system.installed = Path("/srv/old-release")
                system.overrides[name] = origin
                with patch.object(gateway, "_https", return_value="https://house.example.test:13601"):
                    self.assertEqual(gateway.perform(system), 1)
                self.assertTrue(any("unknown unit provenance or overrides" in message for message in system.messages), system.messages)
                self.assertFalse((gateway.context.var / "gateway/current.json").exists())

    def test_the_worker_and_the_relay_are_installed_as_quadlets_started_and_seen_running_this_release(self):
        gateway, system = self.gateway(), Host()
        system.installed, system.active = Path("/srv/old-release"), True
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0, system.messages)
        worker = "thermite-access-worker.service"
        quadlet, lifecycle = map(str, staging.destinations(worker))
        self.assertEqual((quadlet, lifecycle), ("/etc/containers/systemd/thermite-access-worker.container",
                                                "/etc/systemd/system/thermite-access-worker.service.d/50-container-lifecycle.conf"))
        self.assertEqual(set(system.unit_bytes), {"/etc/systemd/system/thermite-access.service", quadlet, lifecycle})
        commands = [command for command in system.commands if command[:2] == ("sudo", "systemctl")]
        # Reloaded once the files are there; the host service enabled, the generated one only started.
        self.assertEqual(commands[-3:], [("sudo", "systemctl", "daemon-reload"),
                                         ("sudo", "systemctl", "enable", "--now", "thermite-access.service"),
                                         ("sudo", "systemctl", "start", worker)])
        self.assertLess(max(index for index, command in enumerate(system.commands) if "install" in command),
                        system.commands.index(("sudo", "systemctl", "daemon-reload")))
        # Each look hands root's helper the gateway's main process and the
        # container's own first process, never podman's monitor.
        looks = [command for command in system.commands if "live" in command]
        self.assertEqual(len(looks), 2, "once when it answers, and again after the settle")
        for look in looks:
            self.assertEqual(look[-4:], ("live", str(gateway.release), "123", "456"))

    def test_a_contained_service_that_restarts_while_it_is_verified_fails_the_deployment(self):
        gateway, system = self.gateway(), Host()
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if seconds == access.STEADY:
                system.container_pids["thermite-access-worker"] = "789"

        system.sleep = sleep
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 1)
        self.assertEqual(slept, [access.STEADY])
        self.assertIn("   a gateway service restarted while it was verified; its journal says why (journalctl -u "
                      "thermite-access.service -u thermite-access-worker.service)", system.messages)
        self.assertEqual(self.journal(gateway)["phase"], "activated")
        self.assertFalse((gateway.context.var / "gateway/current.json").exists())

    def test_a_contained_service_that_never_runs_fails_the_deployment_within_its_bound(self):
        for case, change in (("no container", lambda system: system.container_pids.update({"thermite-access-worker": "0"})),
                             ("an inactive unit", lambda system: setattr(system, "inactive", "thermite-access-worker.service"))):
            with self.subTest(case=case):
                gateway, system = self.gateway(transaction=TRANSACTION[:-1] + str(len(self.staged))), Host()
                change(system)
                slept = []
                system.sleep = slept.append
                with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
                    self.assertEqual(gateway.perform(system), 1)
                self.assertEqual(slept, [1] * 29, "thirty looks, a second apart")
                self.assertTrue(any(message.startswith("   gateway did not verify after activation: thermite-access-worker.service ")
                                    for message in system.messages), system.messages)
                self.assertFalse((gateway.context.var / "gateway/current.json").exists())

    def test_a_rotated_runtime_token_is_staged_with_the_same_identity_and_tls(self):
        gateway, system = self.gateway(), Host()
        with patch.object(Gateway, "_https", return_value="https://house.example.test:13601"):
            self.assertEqual(gateway.perform(system), 0)
            system.installed, system.active = gateway.release, True
            atomic_private(gateway.context.area / gateway.context.token_file, b"new-runtime-credential")
            again = self.gateway(context=gateway.context, transaction=NEXT)
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
        gateway.context.host = {**gateway.context.host, "guest_http": guest_http,
                                "music": {"libraries": {"flac": "/srv/example/flac"}}}
        with patch.object(gateway, "_https", return_value="https://house.example.test:13601"), \
                patch.object(gateway, "_guest_http", return_value=["   Guest HTTP verified: http://192.0.2.10:13600/"]):
            self.assertEqual(gateway.perform(system), 0, system.messages)
        [staged] = self.staged
        self.assertEqual(staged["guest_http"], guest_http)
        self.assertEqual(staged["music"], {"libraries": {"flac": "/srv/example/flac"}})
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
        import oh_conf
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
        with oh_conf.instance_at(dirty / "instance"):
            before = oh_paths.SITE
            with site_inputs(frozen):
                self.assertEqual(remote.palette({"theme": "theme.json"}).name, "committed")
                self.assertEqual(globalcache_ir.commissioned(device)["commands"], {"play": "38000,1,1,24,24"})
                self.assertEqual(oh_paths.SITE, frozen)
            self.assertEqual(oh_paths.SITE, before)
            self.assertEqual(oh_conf.instance(), dirty / "instance")


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
        return gateway, gateway._metadata(system)

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
