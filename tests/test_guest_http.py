"""The opt-in plain-HTTP Guest listener outside Django: its host.yaml schema,
port, gateway configuration, sockets, installer check and status line.
Requests through it are tested with Django in lib/oh_identity/tests."""

import asyncio
import contextlib
import http.server
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy
import oh_ports
from oh_access import AccessError, deployment, guest
from oh_access.files import json_private, private_directory
from oh_access.service import CONFIG_VERSION, load_config, validate_listeners
from oh_cli import status
from oh_install.access import Gateway

HOST = {"timezone": "Etc/UTC", "http_port": 13601, "https_port": 13602, "uid": 9001, "gid": 9001}
#: A single-interface host: bind its LAN address, admit its LAN.
SINGLE = {"listen": ["192.0.2.10"], "clients": ["192.0.2.0/24"]}


class Schema(unittest.TestCase):
    def test_host_yaml_opens_a_restricted_listener_only_when_asked(self):
        self.assertEqual(oh_deploy.validate_host({**HOST, "guest_http": SINGLE})["guest_http"], SINGLE)
        self.assertEqual(guest.settings(SINGLE), {"port": 13626, **SINGLE, "names": []})
        self.assertIsNone(guest.settings(None))
        full = {"port": 14626, "listen": ["198.51.100.1", "2001:DB8::1"], "clients": ["198.51.100.0/24", "2001:db8::/64"],
                "names": ["remote.guest.example"]}
        normal = guest.settings(full)
        self.assertEqual(normal["listen"], ["198.51.100.1", "2001:db8::1"])
        self.assertEqual(guest.settings(normal), normal)
        self.assertEqual(guest.origins(normal), ["http://198.51.100.1:14626", "http://[2001:db8::1]:14626",
                                                 "http://remote.guest.example:14626"])

    def test_every_setting_is_checked_with_a_named_reason(self):
        cases = {
            "must be a mapping": ["192.0.2.10"],
            "unknown guest_http setting bind": {**SINGLE, "bind": "192.0.2.10"},
            "listen must be a non-empty list": {"clients": SINGLE["clients"]},
            "clients must be a non-empty list": {"listen": SINGLE["listen"], "clients": []},
            "'0.0.0.0' is not one of this host's own": {**SINGLE, "listen": ["0.0.0.0"]},
            "'::' is not one": {**SINGLE, "listen": ["::"]},
            "'::ffff:192.0.2.10' is not one": {**SINGLE, "listen": ["::ffff:192.0.2.10"]},
            "'fe80::1%eth0' is not one": {**SINGLE, "listen": ["fe80::1%eth0"]},
            "'239.255.255.250' is not one": {**SINGLE, "listen": ["239.255.255.250"]},
            "'192.0.2.10/24' is not a network": {**SINGLE, "clients": ["192.0.2.10/24"]},
            "not every address": {**SINGLE, "clients": ["0.0.0.0/0"]},
            "as an IPv4 network": {**SINGLE, "clients": ["::ffff:192.0.2.0/120"]},
            "is not a lowercase DNS name": {**SINGLE, "names": ["Remote_Guest"]},
            "under guest_http.listen": {**SINGLE, "names": ["192.0.2.11"]},
            "guest_http.port: listener port": {**SINGLE, "port": "13626"},
        }
        for reason, value in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(oh_deploy.HostError, reason):
                oh_deploy.validate_host({**HOST, "guest_http": value})

    def test_port_is_allocated_and_cannot_collide(self):
        self.assertEqual(oh_ports.GUEST_HTTP_PORT, 13626)
        self.assertEqual(oh_ports.DEFAULTS["guest_http"], 13626)
        self.assertEqual(oh_ports.effective({"guest_http": {**SINGLE, "port": 14626}})["guest_http"], 14626)
        for port in (oh_ports.ACCESS_PORT, 13601, oh_ports.DOCS_PORT):
            with self.subTest(port=port), self.assertRaisesRegex(oh_deploy.HostError, "collides"):
                oh_deploy.validate_host({**HOST, "guest_http": {**SINGLE, "port": port}})
        with self.assertRaisesRegex(ValueError, "collides"):
            validate_listeners(oh_ports.NATIVE_URL, 14626, guest_port=14626)


class Peers(unittest.TestCase):
    def test_peers_match_client_networks_including_mapped_ipv4(self):
        networks = [ipaddress.ip_network("192.0.2.0/24"), ipaddress.ip_network("2001:db8::/64")]
        for peer, expected in ((("192.0.2.77", 5000), True), (("::ffff:192.0.2.77", 5000, 0, 0), True),
                               (("2001:db8::9", 5000, 0, 0), True), (("198.51.100.7", 5000), False),
                               (("::ffff:198.51.100.7", 5000, 0, 0), False), (("2001:db8:1::9", 5000, 0, 0), False),
                               (("fe80::1%eth0", 5000, 0, 2), False), (None, False), (("not an address", 1), False)):
            with self.subTest(peer=peer):
                self.assertIs(guest.admitted(peer, networks), expected)

    def test_refusals_are_counted_and_logged_at_most_once_an_interval(self):
        now = [0.0]
        refused = guest.Refusals(interval=60, clock=lambda: now[0])
        with self.assertLogs("oh_access", "WARNING") as logs:
            for moment in (0, 1, 2, 59, 61, 62):
                now[0] = moment
                refused()
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Guest HTTP listener closed 1 connection(s) from outside its client networks",
                          "Guest HTTP listener closed 4 connection(s) from outside its client networks"])
        self.assertFalse(any("127.0.0" in record.getMessage() for record in logs.records))

    def test_admitting_sockets_close_other_peers_before_any_server_reads(self):
        received = []
        clients = guest.Clients(["127.0.0.2/32"])

        async def answer(reader, writer):
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"answered")
            await writer.drain()
            writer.close()

        async def exchange(port, source):
            reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(source, 0))
            writer.write(b"GET / HTTP/1.1\r\n\r\n")
            try:
                return await asyncio.wait_for(reader.read(), 5)
            except ConnectionResetError:
                return b""
            finally:
                writer.close()

        async def scenario():
            bound = socket.socket()
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
            admitting = clients.only(bound)
            # The same bound socket, now owned by the admitting one.
            self.assertEqual((bound.fileno(), admitting.getsockname()), (-1, ("127.0.0.1", port)))
            server = await asyncio.start_server(answer, sock=admitting)
            try:
                return [await exchange(port, source) for source in ("127.0.0.2", "127.0.0.3", "127.0.0.1", "127.0.0.2")]
            finally:
                server.close()
                await server.wait_closed()

        with self.assertLogs("oh_access", "WARNING") as logs:
            self.assertEqual(asyncio.run(scenario()), [b"answered", b"", b"", b"answered"])
        self.assertEqual(received, [b"GET / HTTP/1.1\r\n\r\n"] * 2)
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Guest HTTP listener closed 1 connection(s) from outside its client networks"])
        self.assertEqual(clients.refused.count, 1)

    def test_marking_is_set_by_the_listener_in_a_copy_of_the_scope(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope)
        listener = guest.Listener(frozenset({"http://192.0.2.10:13626"}), "https://house.example.test:13600")
        original = {"type": "http", "headers": [(b"thermite.guest_http", b"forged")]}
        asyncio.run(guest.mark(app, listener)(original, None, None))
        asyncio.run(app(original, None, None))
        self.assertIs(seen[0][guest.SCOPE], listener)
        self.assertNotIn(guest.SCOPE, seen[1])
        self.assertEqual(listener.hosts, {"192.0.2.10:13626"})


class GatewayConfiguration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        for name in ("token", "contract"):
            path = self.root / name
            path.write_text("{}")
            path.chmod(0o600)
        self.config = {"version": CONFIG_VERSION, "state": str(self.root), "contract_file": str(self.root / "contract"),
                       "runtime_url": oh_ports.NATIVE_URL, "runtime_token_file": str(self.root / "token"),
                       "allowed_hosts": ["house.example.test", "192.0.2.10"], "bind_host": "0.0.0.0",
                       "port": oh_ports.ACCESS_PORT, "tls_cert": str(self.root / "cert"), "tls_key": str(self.root / "key"),
                       "timezone": "UTC", "guest_http": None}

    def load(self, **changes):
        json_private(self.root / "config.json", {**self.config, **changes})
        return load_config(self.root / "config.json", tls=False)

    def test_version_two_names_the_listener_or_null(self):
        self.assertIsNone(self.load()["guest_http"])
        settings = guest.settings(SINGLE)
        self.assertEqual(self.load(guest_http=settings)["guest_http"], settings)
        rejected = ({"version": 1}, {"guest_http": SINGLE}, {"guest_http": {**settings, "listen": ["0.0.0.0"]}},
                    {"guest_http": {**settings, "port": oh_ports.ACCESS_PORT}})
        for changes in rejected:
            with self.subTest(changes=changes), self.assertRaises(AccessError):
                self.load(**changes)
        configuration = dict(self.config)
        del configuration["guest_http"]
        json_private(self.root / "config.json", {**configuration, "version": 1})
        with self.assertRaises(AccessError):
            load_config(self.root / "config.json", tls=False)

    def test_upgrade_adopts_version_one_and_two_configurations(self):
        release = private_directory(self.root / "release")
        for name in ("cert", "key"):
            json_private(release / name, {})
        base = {**self.config, "tls_cert": str(release / "cert"), "tls_key": str(release / "key"),
                "state": str(private_directory(self.root / "state"))}
        old = dict(base)
        del old["guest_http"]
        accepted = ({**old, "version": 1}, {**base, "version": 2}, {**base, "guest_http": guest.settings(SINGLE)})
        refused = ({**base, "version": 1}, {**old, "version": 2}, {**base, "version": 3}, {**base, "version": "2"})
        for index, config in enumerate((*accepted, *refused)):
            json_private(release / "config.json", config)
            destination = self.root / f"adopted-{index}"
            adopt = lambda: deployment.adopt(release / "config.json", destination, uid=os.geteuid(), gid=os.getegid(),  # noqa: E731
                                             service_uid=os.geteuid())
            with self.subTest(version=config["version"], guest_http="guest_http" in config):
                if config in accepted:
                    self.assertNotIn("guest_http", adopt())
                else:
                    with self.assertRaises(deployment.DeploymentError):
                        adopt()


class Answer(http.server.BaseHTTPRequestHandler):
    body = json.dumps({"id": 0, "is_guest": True}).encode()
    peers = []

    def do_GET(self):
        Answer.peers.append((self.client_address[0], self.path, self.headers["Host"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class InstallerAndStatus(unittest.TestCase):
    def gateway(self, settings):
        gateway = Gateway.__new__(Gateway)
        gateway.context = SimpleNamespace(host={**HOST, **({"guest_http": settings} if settings else {})})
        return gateway

    @contextlib.contextmanager
    def server(self, body=None):
        Answer.peers.clear()
        handler = type("Handler", (Answer,), {"body": body or Answer.body})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()

    def test_installer_checks_each_admitted_address_as_a_guest(self):
        self.assertEqual(self.gateway(None)._guest_http(), [])
        with self.server() as port:
            settings = {"port": port, "listen": ["127.0.0.1"], "clients": ["127.0.0.0/8"]}
            self.assertEqual(self.gateway(settings)._guest_http(), [f"   Guest HTTP verified: http://127.0.0.1:{port}/"])
            self.assertEqual(Answer.peers, [("127.0.0.1", "/api/v1/me", f"127.0.0.1:{port}")])
            outside = {**settings, "clients": ["127.0.0.2/32"]}
            self.assertEqual(self.gateway(outside)._guest_http(),
                             [f"   Guest HTTP http://127.0.0.1:{port}/ not checked: this host's address is outside guest_http.clients"])
        with self.server(json.dumps({"id": 1, "is_guest": False}).encode()) as port:
            with self.assertRaisesRegex(AccessError, "Guest HTTP listener returned an unexpected response"):
                self.gateway({**settings, "port": port})._guest_http()

    def test_status_lists_the_guest_addresses(self):
        for settings, expected in ((None, []), ({**SINGLE, "names": ["remote.guest.example"]},
                                                ["http://192.0.2.10:13626/", "http://remote.guest.example:13626/"])):
            report, out = {}, io.StringIO()
            with contextlib.redirect_stdout(out):
                status.show_guest({**HOST, **({"guest_http": settings} if settings else {})}, report, print)
            self.assertEqual(report["guest_http"], expected)
            self.assertEqual(out.getvalue().splitlines(),
                             [f"guest    {url}: Guest controls over plain HTTP, for 192.0.2.0/24" for url in expected])


if __name__ == "__main__":
    unittest.main()
