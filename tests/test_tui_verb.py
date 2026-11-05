"""thermite tui: the terminal remote on a gateway from any computer, no site needed.

No network and no gateway: the TLS fetch and Node's launch are mocked.
"""
from pathlib import Path
import contextlib
import io
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import build_opener

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_paths
import oh_ports
from oh_access import client
from oh_cli import remote, tui

PORT = oh_ports.ACCESS_PORT
MODEL = {"version": 1, "rooms": [{"id": "synthetic_room", "label": "Synthetic room", "sources": []}],
         "sources": {}, "devices": {}, "initial": {}}
#: The real shared launch, before Scratch replaces it with a mock.
REAL_LAUNCH = remote.launch
CONTENT = ("globalThis.REMOTE_HOUSE = " + json.dumps(MODEL) + ";\n").encode()


def unverified(code=20):
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_code = code
    return URLError(error)


class PlainHTTP(socketserver.BaseRequestHandler):
    """Answers whatever arrives, a TLS handshake included, as a plain-HTTP
    server such as a gateway's Guest port does: with an HTTP error."""

    def handle(self):
        self.request.recv(4096)
        self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


@contextlib.contextmanager
def plain_http():
    """A loopback plain-HTTP server's port."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), PlainHTTP)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


#: A site's instance/host.yaml that moves HTTPS to 16301 and gives Guests 13601.
MOVED = {"timezone": "Etc/UTC", "http_port": 13602, "https_port": 13603, "uid": 9001, "gid": 9001,
         "gateway": {"port": 16301},
         "guest_http": {"port": 13601, "listen": ["192.0.2.10"], "clients": ["192.0.2.0/24"]}}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Normalize(unittest.TestCase):
    def test_default_is_the_server_named_thermite_on_the_access_port(self):
        self.assertEqual(tui.gateway(None), f"https://thermite:{PORT}")
        self.assertEqual(tui.DEFAULT_URL, f"https://thermite:{PORT}")

    def test_bare_host_and_host_port_mean_https(self):
        cases = {
            "thermite": f"https://thermite:{PORT}",
            "192.0.2.50": f"https://192.0.2.50:{PORT}",
            "192.0.2.50:13700": "https://192.0.2.50:13700",
            "Thermite.Example": f"https://thermite.example:{PORT}",
            f"https://192.0.2.50:{PORT}/": f"https://192.0.2.50:{PORT}",
            "https://thermite.example": "https://thermite.example",
            "[2001:db8::10]": f"https://[2001:db8::10]:{PORT}",
            "http://127.0.0.1:13616": "http://127.0.0.1:13616",
            "http://localhost:13616": "http://localhost:13616",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(tui.gateway(given), expected)

    def test_refuses_plain_http_off_this_computer_and_anything_but_an_origin(self):
        for given in ("http://192.0.2.50", "http://thermite:13601", "ftp://thermite",
                      f"https://thermite:{PORT}/remote", "https://user@thermite", "thermite:port", "https://"):
            with self.subTest(given=given), self.assertRaises(ValueError):
                tui.gateway(given)

    def test_a_refused_url_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            tui.main(["--url", "http://192.0.2.50"])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("needs https", err.getvalue())


class Scratch(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR") or AREA / ".scratch")
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="tui-verb-", dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root / "no-site"
        self.site.mkdir()
        environment = {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_CACHE_HOME": str(self.root / "cache"),
                       "XDG_STATE_HOME": str(self.root / "state")}
        self.enterContext(mock.patch.dict(os.environ, environment))
        os.environ.pop("NODE_EXTRA_CA_CERTS", None)
        os.environ.pop("OPENHAB_REMOTE_TUI_BUILD", None)
        # No site, no installed gateway on this computer.
        self.enterContext(mock.patch.object(oh_cli, "AREA", self.site))
        self.enterContext(mock.patch.object(tui, "UNIT", self.root / "no-unit.service"))
        self.opened = []
        self.answer = lambda request: Response(CONTENT)
        self.enterContext(mock.patch.object(tui, "build_opener", side_effect=self.opener))
        self.launch = self.enterContext(mock.patch.object(remote, "launch", return_value=0))

    def opener(self, *handlers):
        contexts = [handler._context for handler in handlers if hasattr(handler, "_context")]

        def open_(request, timeout):
            self.opened.append((request.full_url, contexts))
            answer = self.answer(request)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return mock.Mock(open=open_)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = oh_cli.main(["tui", *argv])
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def authority(self, name="ca.pem"):
        if not shutil.which("openssl"):
            self.skipTest("openssl makes the fixture authority")
        path = self.root / name
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
                        str(self.root / (name + ".key")), "-out", str(path), "-days", "1", "-subj", "/CN=Example CA"],
                       check=True, capture_output=True)
        return path


class NoSite(Scratch):
    def test_registered_without_a_site_and_never_checks_one(self):
        self.assertFalse(oh_cli.VERBS["tui"].site)
        with mock.patch.object(oh_paths, "require_compatible", side_effect=oh_paths.SiteError("no site")) as check:
            status, _, err = self.run_main()
        self.assertEqual(status, 0, err)
        check.assert_not_called()

    def test_default_run_reads_the_model_from_thermite_as_a_guest(self):
        status, _, err = self.run_main()
        self.assertEqual(status, 0, err)
        self.assertEqual(self.opened[0][0], f"https://thermite:{PORT}{tui.MODEL_PATH}")
        self.launch.assert_called_once()


class SiteDefault(Scratch):
    """With a site selected, the default follows its HTTPS port (gateway.port)."""

    def host_file(self, host):
        (self.site / "instance").mkdir(exist_ok=True)
        (self.site / "instance/host.yaml").write_text(json.dumps(host))

    def test_without_a_host_file_the_product_default_stays(self):
        self.assertEqual(tui.default_url(), f"https://thermite:{oh_ports.ACCESS_PORT}")
        self.assertEqual(tui.gateway("192.0.2.50"), f"https://192.0.2.50:{oh_ports.ACCESS_PORT}")

    def test_default_and_bare_hosts_use_the_sites_gateway_port_never_its_guest_port(self):
        self.host_file(MOVED)
        cases = {None: "https://thermite:16301", "thermite": "https://thermite:16301",
                 "192.0.2.50": "https://192.0.2.50:16301", "[2001:db8::10]": "https://[2001:db8::10]:16301",
                 "192.0.2.50:13700": "https://192.0.2.50:13700", "https://thermite.example": "https://thermite.example"}
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(tui.gateway(given), expected)
        status, _, err = self.run_main()
        self.assertEqual(status, 0, err)
        self.assertEqual(self.opened[0][0], f"https://thermite:16301{tui.MODEL_PATH}")

    def test_an_unusable_host_file_is_named_unless_the_url_gives_a_port(self):
        self.host_file({**MOVED, "gateway": {"port": "16301"}})
        status, _, err = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("gateway.port: listener port must be an integer", err)
        self.assertIn("--url HOST:PORT", err)
        self.assertEqual(self.opened, [])
        status, _, err = self.run_main("--url", "192.0.2.50:16301")
        self.assertEqual(status, 0, err)
        self.assertEqual(self.opened[0][0], f"https://192.0.2.50:16301{tui.MODEL_PATH}")


class PlainPort(Scratch):
    """A plain-HTTP port, such as a Guest port, is named as one."""

    def test_a_port_answering_plain_http_is_named_with_the_fix(self):
        self.answer = lambda request: URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))
        with plain_http() as port:
            status, _, err = self.run_main("--url", f"127.0.0.1:{port}")
        self.assertEqual(status, 2)
        self.assertIn(f"https://127.0.0.1:{port} is not HTTPS: 127.0.0.1:{port} answers plain HTTP", err)
        self.assertIn("Guest port", err)
        self.assertIn("thermite tui --url 127.0.0.1:PORT", err)
        self.assertNotIn("Traceback", err)
        self.launch.assert_not_called()

    def test_a_failed_handshake_without_plain_http_keeps_the_general_hint(self):
        self.answer = lambda request: URLError(ssl.SSLError(1, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"))
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
            status, _, err = self.run_main("--url", f"127.0.0.1:{port}")
        self.assertEqual(status, 2)
        self.assertIn(f"cannot make a secure connection to https://127.0.0.1:{port}", err)
        self.assertIn("gateway.port", err)

    def test_a_real_handshake_with_a_plain_http_server_is_recognized(self):
        with mock.patch.object(tui, "build_opener", side_effect=build_opener), \
                plain_http() as port, self.assertRaises(tui.Problem) as raised:
            tui.fetch(f"https://127.0.0.1:{port}", None, "the system's")
        self.assertIn("answers plain HTTP", raised.exception.lines[0])


class Delegation(Scratch):
    def prepared(self, *argv):
        status, _, err = self.run_main(*argv)
        self.assertEqual(status, 0, err)
        verb, prepare, build = self.launch.call_args.args
        self.assertEqual(verb, "tui")
        return build, prepare("/usr/bin/node")

    def test_delegates_to_the_shared_launch_with_the_gateway_and_view_switches(self):
        build, (command, environment) = self.prepared("--url", "192.0.2.50", "--room", "synthetic_room",
                                                       "--light", "--no-log")
        self.assertEqual(command[:2], ["/usr/bin/node", str(remote.TUI / "main.js")])
        self.assertEqual(command[command.index("--modules") + 1], str(remote.modules(build)))
        self.assertEqual(command[command.index("--url") + 1], f"https://192.0.2.50:{PORT}")
        self.assertEqual(command[command.index("--room") + 1], "synthetic_room")
        self.assertEqual(command[command.index("--mode") + 1], "light")
        self.assertIn("--no-log", command)
        self.assertNotIn("--native", command)
        self.assertNotIn("--mock", command)
        self.assertEqual(Path(command[command.index("--state") + 1]),
                         self.root / "state/thermite/tui-remote/state.json")
        self.assertEqual(Path(environment["THERMITE_REMOTE_HOUSE_FILE"]).read_bytes(), CONTENT)
        self.assertTrue(Path(environment["THERMITE_REMOTE_HOUSE_FILE"]).is_relative_to(self.root / "cache"))
        self.assertIn(remote.LOOK, environment)
        self.assertNotIn("THERMITE_REMOTE_TOKEN_FILE", environment)
        self.assertNotIn("NODE_EXTRA_CA_CERTS", environment)

    def test_blessed_goes_in_the_user_cache_unless_remote_has_it(self):
        with mock.patch.object(remote, "ready", return_value=False):
            self.assertEqual(tui.build(), self.root / "cache/thermite/tui-remote/build")
        with mock.patch.object(remote, "ready", return_value=True):
            self.assertEqual(tui.build(), remote.BUILD)

    def test_an_access_login_credential_for_the_origin_is_used(self):
        url = f"https://thermite:{PORT}"
        credential = client.credential_path(url)
        credential.write_text("validated by the shared client")
        _, (_, environment) = self.prepared()
        self.assertEqual(environment["THERMITE_REMOTE_TOKEN_FILE"], str(credential))

    def test_the_shared_launch_execs_node_with_what_prepare_returns(self):
        prepare = mock.Mock(return_value=(["/usr/bin/node", "main.js"], {"A": "1"}))
        with mock.patch.object(remote.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(remote, "ready", return_value=True), \
                mock.patch.object(remote.os, "execve") as execute:
            status = REAL_LAUNCH("tui", prepare, self.root / "build")
        self.assertEqual(status, 0)
        prepare.assert_called_once_with("/usr/bin/node")
        execute.assert_called_once_with("/usr/bin/node", ["/usr/bin/node", "main.js"], {"A": "1"})

    def test_remote_and_tui_share_main_js_argv(self):
        import argparse
        arguments = argparse.Namespace(pair=True, still=False, log_visible=None, room="r", choose=None,
                                       mode=None, volume="buttons", latency=None, matrix_down=False,
                                       state=Path("/state.json"), mock=True, url=None, sandbox=False)
        self.assertEqual(remote.node_argv(arguments, "node", Path("/b")),
                         remote.main_js("node", Path("/b"), Path("/state.json"), ["--mock"], arguments))


class Trust(Scratch):
    def test_untrusted_certificate_names_the_file_to_copy_and_exits_2(self):
        self.answer = lambda request: unverified()
        status, _, err = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("does not trust", err)
        self.assertIn("tls-cert.pem", err)
        self.assertIn("/srv/thermite/access/tls-cert.pem", err)
        self.assertIn(f"thermite tui --url https://thermite:{PORT} --ca-file FILE", err)
        self.assertNotIn("Traceback", err)
        self.launch.assert_not_called()

    def test_a_wrong_ca_file_is_named_and_a_misnamed_certificate_explained(self):
        ca = self.authority()
        self.answer = lambda request: unverified()
        status, _, err = self.run_main("--url", "192.0.2.50", "--ca-file", str(ca))
        self.assertEqual(status, 2)
        self.assertIn(f"authority in {ca.resolve()} (--ca-file) did not sign", err)
        self.answer = lambda request: unverified(tui.MISNAMED)
        status, _, err = self.run_main("--url", "192.0.2.50", "--ca-file", str(ca))
        self.assertEqual(status, 2)
        self.assertIn("does not name 192.0.2.50", err)
        self.assertFalse(client.ca_path(client.credential_path(f"https://192.0.2.50:{PORT}")).exists())

    def test_ca_file_is_trusted_by_python_and_node_and_remembered_for_the_origin(self):
        ca = str(self.authority().resolve())
        status, _, err = self.run_main("--url", "192.0.2.50", "--ca-file", ca)
        self.assertEqual(status, 0, err)
        self.assertIn("trusting", err)
        _, prepare, _ = self.launch.call_args.args
        self.assertEqual(prepare("/usr/bin/node")[1]["NODE_EXTRA_CA_CERTS"], ca)
        url = f"https://192.0.2.50:{PORT}"
        self.assertEqual(client.saved_ca(client.credential_path(url)), ca)
        self.assertEqual(tui.trust(url, None), (ca, "saved for " + url))

    def test_missing_or_invalid_ca_file_exits_2(self):
        status, _, err = self.run_main("--ca-file", str(self.root / "missing.pem"))
        self.assertEqual((status, "no such file" in err), (2, True))
        bad = self.root / "bad.pem"
        bad.write_text("not a certificate")
        status, _, err = self.run_main("--ca-file", str(bad))
        self.assertEqual((status, "not a readable PEM certificate" in err), (2, True))
        self.assertEqual(self.opened, [])

    def test_on_the_server_the_installed_gateways_authority_is_used(self):
        root = self.root / "srv/thermite/access"
        (root / "releases/abc").mkdir(parents=True)
        shutil.copyfile(self.authority(), root / "tls-cert.pem")
        unit = self.root / "thermite-access.service"
        unit.write_text("[Unit]\nDescription=x\n[Service]\nUser=thermite\nWorkingDirectory="
                        + str(root / "releases/abc") + "\n")
        with mock.patch.object(tui, "UNIT", unit):
            self.assertEqual(tui.trust(f"https://thermite:{PORT}", None),
                             (str(root / "tls-cert.pem"), "this computer's installation"))

    def test_a_selected_sites_gateway_profile_wins_for_its_own_address(self):
        ca = str(self.authority())
        url = f"https://thermite:{PORT}"
        with mock.patch.object(client, "deployment", return_value={"url": url, "ca_file": ca}):
            self.assertEqual(tui.trust(url, None), (ca, "this computer's installation"))

    def test_plain_http_on_loopback_needs_no_trust(self):
        self.assertEqual(tui.trust("http://127.0.0.1:13616", None), (None, "plain http"))


class Reachability(Scratch):
    def test_unresolvable_thermite_suggests_url(self):
        self.answer = lambda request: URLError(socket.gaierror(-2, "Name or service not known"))
        status, _, err = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("cannot find a computer named thermite", err)
        self.assertIn("--url", err)
        self.launch.assert_not_called()

    def test_refused_timeout_and_http_errors_exit_2(self):
        for answer, text in ((URLError(ConnectionRefusedError(111, "refused")), "nothing answers"),
                             (URLError(TimeoutError("timed out")), "no answer"),
                             (HTTPError("u", 404, "Not Found", {}, io.BytesIO()), "answered HTTP 404")):
            with self.subTest(text=text):
                self.answer = lambda request, answer=answer: answer
                status, _, err = self.run_main("--url", "192.0.2.50")
                self.assertEqual(status, 2)
                self.assertIn(text, err)
                self.assertNotIn("Traceback", err)

    def test_a_model_that_is_not_data_is_refused(self):
        self.answer = lambda request: Response(b"fetch('https://example.invalid')")
        status, _, err = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("cannot read", err)
        self.launch.assert_not_called()


class Help(unittest.TestCase):
    def test_help_states_default_trust_switches_exit_status_and_examples(self):
        text = oh_cli.parser_of("tui").format_help()
        for wanted in (f"https://thermite:{PORT}", "--ca-file", "tls-cert.pem", "exit status:",
                       f"thermite tui --url https://192.0.2.50:{PORT}", "Guests"):
            self.assertIn(wanted, text)
        self.assertNotIn(str(Path.home()), text)

    def test_completion_offers_the_verb_and_its_switches(self):
        from oh_cli import completion
        _, command = completion.verbs()["tui"]
        names = {name for option in command.options for name in option.names}
        self.assertTrue({"--url", "--ca-file", "--room", "--theme"} <= names)


if __name__ == "__main__":
    unittest.main()
