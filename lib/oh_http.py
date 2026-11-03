"""HTTP response reads with an optional absolute deadline.

Callers supply remaining() so their own clock, error type and safe diagnostic
stay local. The budget covers headers and body even when bytes keep arriving.
Without that callback, request() retains urllib's ordinary timeout behavior.
"""

import http.client
import io
import urllib.error
import urllib.request


class DeadlineExpired(RuntimeError):
    """The caller's absolute HTTP deadline expired."""


class _DeadlineReader(io.RawIOBase):
    def __init__(self, stream, sock, budget):
        self.stream, self.sock, self.budget = stream, sock, budget

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(self.budget())
        return self.stream.readinto(buffer)

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, sock, budget):
        self.sock, self.budget = sock, budget

    def makefile(self, mode):
        # SocketIO retains the socket after urllib closes its connection;
        # closing HTTPResponse releases this stream and the socket together.
        raw = self.sock.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self.sock, self.budget))


class _DeadlineHandler:
    def __init__(self, budget):
        super().__init__()
        self.budget = budget

    def do_open(self, http_class, request, **kwargs):
        def connection(*args, **options):
            # Recalculate for each connection, including redirects.
            options["timeout"] = self.budget()
            conn = http_class(*args, **options)
            response_class = conn.response_class
            conn.response_class = lambda sock, *response_args, **response_options: response_class(
                _DeadlineSocket(sock, self.budget), *response_args, **response_options)
            return conn
        return super().do_open(connection, request, **kwargs)


class _DeadlineHTTPHandler(_DeadlineHandler, urllib.request.HTTPHandler):
    pass


class _DeadlineHTTPSHandler(_DeadlineHandler, urllib.request.HTTPSHandler):
    pass


def request(req, *, timeout, remaining=None):
    """Return (status, bytes); HTTP errors keep urllib's exception semantics."""
    if remaining is None:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    budget = lambda: min(timeout, remaining())
    opener = urllib.request.build_opener(_DeadlineHTTPHandler(budget), _DeadlineHTTPSHandler(budget))
    try:
        with opener.open(req, timeout=budget()) as response:
            result = response.status, response.read()
    except (OSError, http.client.HTTPException) as exc:
        # A socket timeout at the deadline is terminal, not a retryable outage.
        try:
            remaining()
        except BaseException:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise
        raise
    remaining()
    return result
