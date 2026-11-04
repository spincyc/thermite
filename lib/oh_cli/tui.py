"""Run the house remote in a terminal against a Thermite gateway, from any computer.

    thermite tui [--url URL] [--ca-file FILE] [--room ID] [--pair] [--theme NAME|FILE] ...

It needs no private site: the gateway serves the house's model, and
thermite remote's launch path (oh_cli.remote.launch) runs tui/remote/main.js.
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import oh_cli
import oh_ports
from oh_cli import remote

#: Most homes run their Thermite server on a computer of this name.
HOST = "thermite"
DEFAULT_URL = f"https://{HOST}:{oh_ports.ACCESS_PORT}"
#: The only hosts plain http may name, as web/remote/access.js allows them.
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
#: The gateway's copy of the house model; Guests may read it.
MODEL_PATH = "/static/remote/house.js"
#: The installed gateway's unit; its WorkingDirectory is <gateway root>/releases/<release>
#: (oh_install.access.Gateway), and <gateway root>/tls-cert.pem its authority.
UNIT = Path("/etc/systemd/system/thermite-access.service")
AUTHORITY = "tls-cert.pem"
#: Where a server keeps that file in the default installation root,
#: /srv/thermite: the gateway root is its access/ directory.
EXAMPLE_AUTHORITY = "/srv/thermite/access/" + AUTHORITY
TIMEOUT = 10
#: X509_V_ERR_CERT_HAS_EXPIRED and X509_V_ERR_HOSTNAME_MISMATCH.
EXPIRED, MISNAMED = 10, 62

DESCRIPTION = f"""\
The house remote in this terminal, as thermite remote draws it, against a
Thermite gateway: from the server itself or from any other computer with
thermite installed, such as a laptop. It needs no private configuration:
the gateway serves the house's rooms and controls. It needs Node.js; the
first run installs the pinned blessed into ~/.cache/thermite/tui-remote
($XDG_CACHE_HOME) with npm ci.

Without --url it opens {DEFAULT_URL}: most homes run their
Thermite server on a computer named {HOST}. --url names the gateway as
https://HOST:PORT, or as HOST:PORT or HOST, which mean https (on port
{oh_ports.ACCESS_PORT} without one). Plain http reaches only this computer:
localhost, 127.0.0.1 or [::1].

Guests control unlocked rooms without a login. After thermite access login
for the same address, that account's personal credential is used.
"""

TRUST = f"""\
trust:
  The gateway's certificate is signed by the server's private certificate
  authority: {AUTHORITY} in the gateway root, the installation root's
  access/ directory (e.g. {EXAMPLE_AUTHORITY}); thermite
  install prints its path. On the server itself the installation's copy is
  used. On another computer, copy that file once and pass it with
  --ca-file; it is then remembered for that address, as thermite access
  login --ca-file remembers it. Certificates are always verified.
"""

EXIT = """\
exit status:
  0  the remote was quit
  1  the remote stopped on an error (printed once the terminal is restored)
  2  a wrong switch, no Node.js, the gateway not found, not answering or not
     serving a house, or its certificate not trusted
"""

EXAMPLES = (
    ("thermite tui", f"the house on {DEFAULT_URL}, the server named {HOST}"),
    ("thermite tui --url 192.0.2.50", f"a server of another name: https://192.0.2.50:{oh_ports.ACCESS_PORT}"),
    (f"thermite tui --url https://192.0.2.50:{oh_ports.ACCESS_PORT} --ca-file ~/thermite-ca.pem",
     f"from a laptop, trusting a copy of the server's {AUTHORITY}"),
    (f"thermite access login --url {DEFAULT_URL} --user operator",
     "sign in once; thermite tui then uses that account"),
    ("thermite tui --room ROOM --light", "start in that room, in the theme's light mode"),
)


class Problem(Exception):
    """Why the remote cannot open, as the lines to print."""

    def __init__(self, *lines: str):
        super().__init__(lines[0])
        self.lines = lines


def _home(variable: str, default: str) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / default


def cache() -> Path:
    return _home("XDG_CACHE_HOME", ".cache") / "thermite/tui-remote"


def state_file() -> Path:
    return _home("XDG_STATE_HOME", ".local/state") / "thermite/tui-remote/state.json"


def build() -> Path:
    """Where blessed is installed: thermite remote's, when this thermite has
    it (or OPENHAB_REMOTE_TUI_BUILD names it), else this user's cache."""
    if os.environ.get("OPENHAB_REMOTE_TUI_BUILD") or remote.ready(remote.BUILD):
        return remote.BUILD
    return cache() / "build"


def gateway(value: str | None) -> str:
    """The gateway's origin from --url: a bare HOST or HOST:PORT is https,
    on the access port when it names none; raises ValueError."""
    text = (value or DEFAULT_URL).strip()
    bare = "://" not in text
    if bare:
        text = "https://" + text
    wrong = ValueError(f"--url {value}: give the gateway as https://HOST:PORT, HOST:PORT or HOST")
    try:
        parsed = urlsplit(text)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise wrong from None
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise wrong
    scheme = parsed.scheme.lower()
    if scheme == "http":
        if host not in LOOPBACK:
            raise ValueError(f"--url {value}: plain http only reaches this computer (localhost, 127.0.0.1 "
                             f"or [::1]); a gateway on {host} needs https")
        return f"http://{'[' + host + ']' if ':' in host else host}" + (f":{port}" if port not in (None, 80) else "")
    if scheme != "https":
        raise ValueError(f"--url {value}: a gateway is https, not {scheme}")
    if bare and port is None:
        port = oh_ports.ACCESS_PORT
    from oh_access import client

    shown = f"[{host}]" if ":" in host else host
    return client.origin(f"https://{shown}" + (f":{port}" if port is not None else ""))


def installed_profile() -> dict | None:
    """The selected site's verified gateway profile ({url, ca_file}), if any."""
    from oh_access import client

    try:
        return client.deployment(oh_cli.AREA)
    except (ValueError, OSError):
        return None


def installed_authority() -> str | None:
    """This computer's own gateway authority certificate: its site's profile,
    else beside the release the installed gateway unit runs."""
    profile = installed_profile()
    if profile:
        return profile["ca_file"]
    try:
        text = UNIT.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    section, directory = None, None
    for line in map(str.strip, text.splitlines()):
        if line.startswith("["):
            section = line
        elif section == "[Service]" and line.startswith("WorkingDirectory="):
            directory = line.split("=", 1)[1].replace("%%", "%")
    release = Path(directory) if directory else None
    if release is None or not release.is_absolute() or release.parent.name != "releases":
        return None
    trust = release.parent.parent / AUTHORITY
    return str(trust) if trust.is_file() else None


def copy_hint(url: str) -> str:
    return (f"copy {AUTHORITY} from the server's gateway root (e.g. {EXAMPLE_AUTHORITY}; thermite install "
            f"prints its path) and pass it once: thermite tui --url {url} --ca-file FILE")


def trust(url: str, ca_file: Path | None) -> tuple[str | None, str]:
    """The CA file to trust for url, and where it came from: --ca-file, this
    computer's installation (for its own address), the CA saved for url by
    thermite access login or an earlier --ca-file, this computer's
    installation, NODE_EXTRA_CA_CERTS; else (None, the system's)."""
    if not url.startswith("https:"):
        return None, "plain http"
    if ca_file is not None:
        try:
            chosen = str(Path(ca_file).expanduser().resolve(strict=True))
        except OSError:
            raise Problem(f"--ca-file {ca_file}: no such file", copy_hint(url)) from None
        source = "--ca-file"
    else:
        from oh_access import AccessError, client

        profile = installed_profile()
        if profile and profile["url"] == url:
            return _checked(url, profile["ca_file"], "this computer's installation")
        try:
            saved = client.saved_ca(client.credential_path(url))
        except AccessError:
            raise Problem(f"the CA file saved for {url} is missing or unreadable", copy_hint(url)) from None
        if saved:
            chosen, source = saved, "saved for " + url
        elif local := installed_authority():
            chosen, source = local, "this computer's installation"
        elif os.environ.get("NODE_EXTRA_CA_CERTS"):
            chosen, source = os.environ["NODE_EXTRA_CA_CERTS"], "NODE_EXTRA_CA_CERTS"
        else:
            return None, "the system's"
    return _checked(url, chosen, source)


def _checked(url: str, chosen: str, source: str) -> tuple[str, str]:
    try:
        ssl.create_default_context().load_verify_locations(cafile=chosen)
    except (OSError, ssl.SSLError):
        raise Problem(f"{chosen} ({source}) is not a readable PEM certificate file", copy_hint(url)) from None
    return chosen, source


def _untrusted(url: str, ca: str | None, source: str, error: ssl.SSLCertVerificationError) -> Problem:
    host = urlsplit(url).hostname
    if error.verify_code == MISNAMED:
        return Problem(f"{url}'s certificate does not name {host}",
                       "use --url with a name or address the gateway was installed for "
                       "(thermite install prints its HTTPS address)")
    if error.verify_code == EXPIRED:
        return Problem(f"{url}'s certificate has expired", "run thermite upgrade on the server: it renews it")
    if ca is None:
        return Problem(f"{url} presents a certificate this computer does not trust: Thermite gateways use "
                       "a private certificate authority", copy_hint(url))
    return Problem(f"{url} presents a certificate the authority in {ca} ({source}) did not sign",
                   "that is another server's authority, or an older one: " + copy_hint(url))


def fetch(url: str, ca: str | None, source: str) -> bytes:
    """The gateway's house model, as a Guest, over verified TLS; raises Problem."""
    context = ssl.create_default_context()
    if ca:
        context.load_verify_locations(cafile=ca)
    from oh_access.client import NoRedirect

    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    oh_cli.trace(f"trust {ca or 'the system certificates'} ({source})")
    oh_cli.trace_http("GET", url + MODEL_PATH)
    host = urlsplit(url).hostname
    try:
        with opener.open(Request(url + MODEL_PATH), timeout=TIMEOUT) as response:
            return response.read(remote.MODEL_LIMIT + 1)
    except HTTPError as exc:
        exc.close()
        raise Problem(f"{url} answered HTTP {exc.code} for the house's remote model",
                      "is it a Thermite gateway, installed and generated? (thermite status on the server)") from None
    except (URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise _untrusted(url, ca, source, reason) from None
        if isinstance(reason, socket.gaierror):
            if host == HOST:
                raise Problem(f"cannot find a computer named {HOST} from here",
                              f"most homes name their Thermite server {HOST}; if yours has another name, or "
                              "this network does not resolve it, pass its name or address with --url, "
                              "e.g. thermite tui --url 192.0.2.50") from None
            raise Problem(f"cannot find {host}", "check the name, or pass the server's address with --url") from None
        if isinstance(reason, ssl.SSLError):
            raise Problem(f"cannot make a secure connection to {url}",
                          f"is it a Thermite gateway? (its port is {oh_ports.ACCESS_PORT} unless the host file "
                          "moves it)") from None
        if isinstance(reason, ConnectionRefusedError):
            raise Problem(f"nothing answers at {url}",
                          "check that the server is up (thermite status on it), or pass another --url") from None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise Problem(f"no answer from {url} within {TIMEOUT} seconds",
                          "check the address and the network, or pass another --url") from None
        raise Problem(f"cannot reach {url}: {getattr(reason, 'strerror', None) or reason}",
                      "check the address and the network, or pass another --url") from None


def remember(url: str, ca: str) -> None:
    """Save ca for url where thermite access login --ca-file saves it."""
    from oh_access import AccessError, client
    from oh_access.files import json_private

    try:
        path = client.credential_path(url)
        if client.saved_ca(path) != ca:
            json_private(client.ca_path(path), {"version": 1, "ca_file": ca})
            print(f"thermite tui: trusting {ca} for {url} from now on", file=sys.stderr)
    except (AccessError, OSError):
        print(f"thermite tui: could not remember {ca} for {url}; pass --ca-file again next time", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    parser = oh_cli.command("tui", DESCRIPTION, EXAMPLES, TRUST + "\n" + remote.NOTES + "\n" + EXIT)
    parser.add_argument("--url", metavar="URL",
                        help=f"the gateway: https://HOST:PORT, HOST:PORT or HOST (default: {DEFAULT_URL})")
    parser.add_argument("--ca-file", type=Path, metavar="FILE",
                        help=f"trust this copy of the server's {AUTHORITY}; remembered for that address")
    remote.add_view(parser, "thermite")
    parser.add_argument("--state", type=Path, metavar="FILE",
                        help="where the last room, volume mode, theme mode, motion and log visibility are kept "
                             "(default: ~/.local/state/thermite/tui-remote/state.json, under $XDG_STATE_HOME)")
    return parser


def main(argv: list[str] | None = None) -> int:
    options = parser()
    arguments = options.parse_args(argv)
    if oh_cli.switches.quiet:
        options.error("--quiet would hide the remote itself")
    try:
        url = gateway(arguments.url)
    except ValueError as exc:
        options.error(str(exc))
    import oh_brand as brand

    chosen = remote.colors("tui", lambda: brand.load(arguments.theme))
    if chosen is None:
        return 2
    try:
        ca, source = trust(url, arguments.ca_file)
        content = fetch(url, ca, source)
        try:
            model = remote.cached_model(content, cache() / "models")
        except (ValueError, OSError):
            raise Problem(f"{url} serves a house model this thermite cannot read",
                          "update thermite here or on the server, so both run the same release") from None
    except Problem as problem:
        for line in problem.lines:
            print(f"thermite tui: {line}", file=sys.stderr)
        return 2
    if arguments.ca_file is not None and ca:
        remember(url, ca)
    where = build()
    state = arguments.state or state_file()

    def prepare(node: str) -> tuple[list[str], dict]:
        from oh_access import client

        environment = {**os.environ, remote.LOOK: remote.look(chosen), "THERMITE_REMOTE_HOUSE_FILE": str(model)}
        credential = client.credential_path(url) if url.startswith("https:") else None
        return (remote.main_js(node, where, state, ["--url", url], arguments),
                remote.gateway_environment(environment, credential, ca))

    return remote.launch("tui", prepare, where)
