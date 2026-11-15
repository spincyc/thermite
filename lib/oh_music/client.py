"""Music Assistant's loopback HTTP API, as thermite drives it: its unauthenticated
server info, one command per POST /api with thermite's bearer token, and its
image proxy. Only the gateway (oh_identity.music) and the policy worker
(oh_music.provision) call it, on loopback; nothing it answers is forwarded.

Music Assistant answers a command with its raw result. A refused token is
401 (or 403, a scope); a server with no user yet, 503; a bad command or bad
data, 400; every other failure a bare 500 with no code. So a failure is one
of three exceptions here, and none keeps Music Assistant's text.
"""

from __future__ import annotations

import json
import secrets

import httpx

HOST = "127.0.0.1"
#: Seconds one request may take: a command or a read, which Music Assistant
#: answers at once and the gateway may make holding rooms' locks; and a
#: search across providers, a long TIDAL listing or a provider's setup,
#: which can take seconds (a call's own timeout).
TIMEOUT = 5.0
SLOW = 20.0
#: Bytes of one command's answer and of one image.
ANSWER_BYTES = 8 * 1024 * 1024
IMAGE_BYTES = 2 * 1024 * 1024
#: The image types the proxy may return: their media types and magic bytes.
IMAGES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}


class Unavailable(Exception):
    """Music Assistant cannot be reached, is starting, or has no user yet."""


class NoUser(Unavailable):
    """Music Assistant has no user: it is fresh, or its state was reset."""


class Unauthorized(Exception):
    """Music Assistant refused thermite's token."""


class Failed(Exception):
    """Music Assistant refused or failed a command. code is its HTTP status;
    its text is never kept."""

    def __init__(self, code):
        super().__init__("Music command failed")
        self.code = code


class Client:
    """One connection pool to Music Assistant on loopback. token is thermite's
    bearer token; without one only info(), setup() and image() work.
    transport is the tests' seam (httpx.MockTransport)."""

    def __init__(self, port, token=None, *, timeout=TIMEOUT, transport=None):
        self.port, self.token = port, token
        self._http = httpx.Client(base_url=f"http://{HOST}:{port}", timeout=timeout, trust_env=False,
                                  follow_redirects=False, transport=transport)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method, path, limit, timeout=None, **options):
        if timeout is not None:
            options["timeout"] = timeout
        try:
            with self._http.stream(method, path, **options) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise Failed(response.status_code)
                return response, bytes(body)
        except httpx.HTTPError:
            raise Unavailable("Music Assistant is unreachable") from None

    def info(self):
        """The server's info: its version, status ("running" once started),
        whether onboarding is done and whether remote access is on."""
        response, body = self._request("GET", "/info", ANSWER_BYTES)
        if response.status_code != 200:
            raise Unavailable("Music Assistant is not serving")
        try:
            info = json.loads(body)
        except ValueError:
            raise Unavailable("Music Assistant is not serving") from None
        if not isinstance(info, dict):
            raise Unavailable("Music Assistant is not serving")
        return info

    def has_user(self):
        """Whether Music Assistant has a user: without one it refuses every
        command 503, before it looks for a token, and with one 401."""
        response, _ = self._request("POST", "/api", ANSWER_BYTES, json={"message_id": secrets.token_hex(8),
                                                                        "command": "info", "args": {}})
        if response.status_code in (401, 403):
            return True
        if response.status_code == 503:
            return False
        raise Unavailable("Music Assistant is not serving")

    def login(self, username, password, device_name):
        """A session token for a user's password, or None when Music Assistant
        refuses the password; NoUser when it has no user."""
        response, body = self._request("POST", "/auth/login", ANSWER_BYTES, json={
            "provider_id": "builtin", "credentials": {"username": username, "password": password},
            "device_name": device_name})
        if response.status_code == 401:
            return None
        if response.status_code == 403:
            raise NoUser("Music Assistant has no user")
        if response.status_code != 200:
            raise Unavailable("Music Assistant refused the login")
        try:
            answer = json.loads(body)
            return answer["token"] if answer.get("success") is True and isinstance(answer["token"], str) else None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Unavailable("Music Assistant refused the login") from None

    def setup(self, username, password, device_name):
        """Music Assistant's first admin user; its session token, or None when
        the server already has a user."""
        response, body = self._request("POST", "/setup", ANSWER_BYTES, json={
            "username": username, "password": password, "device_name": device_name})
        if response.status_code == 400:
            return None
        if response.status_code != 200:
            raise Unavailable("Music Assistant refused setup")
        try:
            answer = json.loads(body)
            return answer["token"] if answer.get("success") is True and isinstance(answer["token"], str) else None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Unavailable("Music Assistant refused setup") from None

    def call(self, command, timeout=None, /, **args):
        """One command's raw result, within timeout seconds (the client's
        own by default)."""
        if not self.token:
            raise Unauthorized("No Music Assistant token")
        response, body = self._request(
            "POST", "/api", ANSWER_BYTES, timeout, json={"message_id": secrets.token_hex(8), "command": command,
                                                         "args": args},
            headers={"Authorization": "Bearer " + self.token})  # lint: allow (scheme only; private token file)
        if response.status_code in (401, 403):
            raise Unauthorized("Music Assistant refused the token")
        if response.status_code == 503:
            raise NoUser("Music Assistant has no user")
        if response.status_code != 200:
            raise Failed(response.status_code)
        try:
            return json.loads(body) if body else None
        except ValueError:
            raise Failed(response.status_code) from None

    def image(self, proxy_id, size):
        """(media type, bytes) of one image at one size, as JPEG; Failed for
        anything but a bounded JPEG or PNG."""
        response, body = self._request("GET", f"/imageproxy/{proxy_id}", IMAGE_BYTES,
                                       params={"size": str(size), "fmt": "jpg"})
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        kind = "image/jpeg" if kind == "image/jpg" else kind
        if response.status_code != 200 or kind not in IMAGES or not body.startswith(IMAGES[kind]):
            raise Failed(response.status_code)
        return kind, body

