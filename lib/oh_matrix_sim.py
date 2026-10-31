"""Stand-ins for the matrices' JSON API (oh_integrations._instr), the
HDFury VRROOM's web API (oh_integrations.vrroom_http) and the AVPro
matrix's telnet (oh_integrations.avpro_telnet), for
bin/thermite test routing and the tests.

Each answers like the BK-808, UKM-404, VRROOM or AVPro it stands in for
(`POST /cgi-bin/instr`, the VRROOM's GETs, the AVPro's lines): the same
comheads, replies of the same shape (tests/fixtures/matrices holds the real
ones) and, over HTTP, one request at a time. Every request is recorded, so
a check can prove what openHAB sent. Over HTTP, to a comhead it does not
know, or while muted, it closes the connection unanswered at once, where
the real CGI keeps the client waiting until it times out (and stalls the
next request too).
"""

from __future__ import annotations

import copy
import json
import re
import socket
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from oh_integrations import _instr as instr

PATH = instr.PATH


def _aggregate(values: list, differ: int) -> int:
    """The 9th, "all outputs" entry of a BK-808 per-output array."""
    return values[0] if len(set(values)) == 1 else differ


class Matrix:
    """State, replies and the request log of one stand-in.

    Its ports are what its integration's switch_item() Items hold: get(port)
    and set(port, value) read and change them as Control4 or the front panel
    would, without a request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.replies: list[dict | None] = []
        self.unknown: list[str] = []
        #: When set, every request gets its connection closed unanswered:
        #: the matrix does not answer.
        self.muted = False
        self.lock = threading.Lock()

    def answer(self, request: dict) -> dict | None:
        with self.lock:
            self.requests.append(request)
            comhead = request.get("comhead")
            handler = self.handlers().get(comhead)
            if handler is None:
                self.unknown.append(str(comhead))
            reply = None if handler is None else handler(request)
            self.replies.append(reply)
            return reply

    def handlers(self) -> dict:
        raise NotImplementedError

    def parse_get(self, path: str) -> dict | None:
        """The request a GET of `path` makes; None: it takes no GETs."""
        return None

    def get(self, port: int) -> int:
        raise NotImplementedError

    def set(self, port: int, value: int) -> None:
        raise NotImplementedError

    def comheads(self) -> list[str]:
        """Every comhead received, in order."""
        with self.lock:
            return [str(r.get("comhead")) for r in self.requests]


def _pair(request: dict, key: str, first: range, second: range) -> tuple[int, int] | None:
    value = request.get(key)
    if (isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value)
            and value[0] in first and value[1] in second):
        return value[0], value[1]
    return None


class BK808(Matrix):
    """OREI BK-808: 8 inputs, 8 outputs."""

    INPUTS = OUTPUTS = 8

    def __init__(self, status: dict[str, dict] | None = None) -> None:
        super().__init__()
        self.status = copy.deepcopy(status) if status else self.default_status()

    @classmethod
    def default_status(cls) -> dict[str, dict]:
        n = cls.OUTPUTS
        inputs = [f"Input{i}" for i in range(1, cls.INPUTS + 1)]
        outputs = [f"Output{i}" for i in range(1, n + 1)]
        presets = [f"Preset{i}" for i in range(1, 9)]
        return {
            "get video status": {"comhead": "get video status", "power": 1, "allsource": [1] * (n + 1),
                                 "allinputname": inputs, "alloutputname": outputs, "allname": presets},
            "get input status": {"comhead": "get input status", "power": 1, "edid": [36] * cls.INPUTS,
                                 "inactive": [0] * cls.INPUTS, "inname": inputs},
            "get output status": {"comhead": "get output status", "power": 1, "allconnect": [1] * n,
                                  "name": outputs, "allscaler": [0] * (n + 1), "allhdr": [0] * (n + 1),
                                  "allhdcp": [3] * (n + 1), "allarc": [0] * (n + 1), "allout": [1] * (n + 1),
                                  "allaudiomute": [0] * (n + 1)},
        }

    def handlers(self) -> dict:
        def read(comhead):
            return lambda request: copy.deepcopy(self.status[comhead])
        out = {comhead: read(comhead) for comhead in self.status}
        out["video switch"] = lambda r: self._set(r, "source", "get video status", "allsource",
                                                  range(1, self.INPUTS + 1), 0)
        out["set video scaler"] = lambda r: self._set(r, "scaler", "get output status", "allscaler", range(0, 5), 255)
        out["set hdr conversion"] = lambda r: self._set(r, "hdr", "get output status", "allhdr", range(0, 3), 255)
        out["tx hdcp"] = lambda r: self._set(r, "hdcp", "get output status", "allhdcp", range(1, 6), 255)
        out["set edid"] = self._set_edid
        return out

    def _set(self, request: dict, key: str, comhead: str, field: str, values: range, differ: int) -> dict:
        pair = _pair(request, key, range(0, self.OUTPUTS + 1), values)
        if pair is None:
            return {"comhead": request["comhead"], "result": 0}
        port, value = pair
        array = self.status[comhead][field]
        for index in range(self.OUTPUTS) if port == 0 else [port - 1]:
            array[index] = value
        array[self.OUTPUTS] = _aggregate(array[:self.OUTPUTS], differ)
        return {"comhead": request["comhead"], "result": 1}

    def _set_edid(self, request: dict) -> dict:
        pair = _pair(request, "edid", range(0, self.INPUTS + 1), range(1, 48))
        if pair is None:
            return {"comhead": "set edid", "result": 0}
        port, value = pair
        edid = self.status["get input status"]["edid"]
        for index in range(self.INPUTS) if port == 0 else [port - 1]:
            edid[index] = value
        return {"comhead": "set edid", "result": 1}

    def get(self, port: int) -> int:
        """The input routed to output `port`."""
        with self.lock:
            return self.status["get video status"]["allsource"][port - 1]

    def set(self, port: int, value: int) -> None:
        with self.lock:
            self._set({"comhead": "video switch", "source": [port, value]}, "source", "get video status",
                      "allsource", range(1, self.INPUTS + 1), 0)

    def edid(self, input_: int) -> int:
        """The EDID code input `input_` presents."""
        with self.lock:
            return self.status["get input status"]["edid"][input_ - 1]

    def set_edid(self, input_: int, code: int) -> None:
        with self.lock:
            self.status["get input status"]["edid"][input_ - 1] = code


class UKM404(Matrix):
    """4x4 USB matrix: 4 hosts, 4 device ports."""

    HOSTS = DEVICES = 4

    def __init__(self, status: dict | None = None) -> None:
        super().__init__()
        self.status = copy.deepcopy(status) if status else self.default_status()

    @classmethod
    def default_status(cls) -> dict:
        return {"comhead": "get_matrix_status", "power": 1, "all_host": [1] * cls.DEVICES,
                "all_devicename": [f"Device{i}" for i in range(1, cls.DEVICES + 1)],
                "all_hostname": [f"Host{i}" for i in range(1, cls.HOSTS + 1)],
                "all_name": [f"Preset{i}" for i in range(1, 9)]}

    def handlers(self) -> dict:
        return {"get_matrix_status": lambda r: copy.deepcopy(self.status), "host_switch": self._switch}

    def _switch(self, request: dict) -> dict:
        pair = _pair(request, "device", range(1, self.DEVICES + 1), range(1, self.HOSTS + 1))
        if pair is None:
            return {"comhead": "host_switch", "result": 0}
        self.status["all_host"][pair[0] - 1] = pair[1]
        return {"comhead": "host_switch", "result": 1}

    def get(self, port: int) -> int:
        """The host device port `port` is attached to."""
        with self.lock:
            return self.status["all_host"][port - 1]

    def set(self, port: int, value: int) -> None:
        with self.lock:
            self.status["all_host"][port - 1] = value


class VRROOM(Matrix):
    """HDFury VRROOM (oh_integrations.vrroom_http): inputs 0-3 on TX0 and
    TX1, over GET. Its ports are its outputs, 0 for TX0 and 1 for TX1, each
    holding an input or COPY (a copy of the other output). It records
    GET /ssi/<page> as "get <page>" and GET /cmd?<name>=<value> as <name>;
    a /cmd it refuses gets a 404, as the real one's do."""

    INPUTS = range(0, 4)
    COPY = 4

    def __init__(self, status: dict | None = None) -> None:
        super().__init__()
        self.status = copy.deepcopy(status) if status else self.default_status()

    @classmethod
    def default_status(cls) -> dict:
        fields = ("AUD0", "AUD1", "AUDOUT", "EARCRX", "EDIDA0", "EDIDA1", "EDIDA2", "RX0", "RX1", "SINK0", "SINK1",
                  "SINK2", "SPDASC", "SPDASC1", "TX0", "TX1", "arcst", "earcout5v", "earcst", "opmode", "pcbv",
                  "portseltx0", "portseltx1", "rx0in5v", "rx1in5v", "rx2in5v", "rx3in5v", "tx0hpd", "tx0ns",
                  "tx0rs", "tx0scerr", "tx0tmds", "tx1hpd", "tx1ns", "tx1rs", "tx1scerr", "tx1tmds", "txaudhpd",
                  "txaudrs")
        return {**dict.fromkeys(fields, "0"), "portseltx1": str(cls.COPY), "pcbv": "3"}

    def parse_get(self, path: str) -> dict:
        url = urllib.parse.urlsplit(path)
        if url.path.startswith("/ssi/"):
            return {"comhead": f"get {url.path[len('/ssi/'):]}"}
        query = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        if url.path == "/cmd" and len(query) == 1:
            name, value = query[0]
            return {"comhead": name, name: value}
        return {"comhead": f"GET {path}"}

    def handlers(self) -> dict:
        return {"get infopage.ssi": lambda r: copy.deepcopy(self.status), "insel": self._insel}

    def _insel(self, request: dict) -> dict:
        values = str(request.get("insel")).split(" ")
        allowed = {str(n) for n in [*self.INPUTS, self.COPY]}
        if len(values) != 2 or not set(values) <= allowed or values == [str(self.COPY)] * 2:
            return {"comhead": "insel", "result": 0}
        self.status["portseltx0"], self.status["portseltx1"] = values
        return {"comhead": "insel", "result": 1}

    def get(self, port: int) -> int:
        """The input output `port` (0 for TX0) shows."""
        with self.lock:
            return int(self.status[f"portseltx{port}"])

    def set(self, port: int, value: int) -> None:
        with self.lock:
            self.status[f"portseltx{port}"] = str(value)

class AVPro(Matrix):
    """AVPro Edge AC-MX-44X (oh_integrations.avpro_telnet): 4 inputs, 4
    outputs, over telnet (serve() gives it a TelnetServer). Its ports are
    its outputs, each holding the input routed to it. On connect it lists
    every output's input and audio level, as the real one does; it answers
    `GET OUT<n> VS` (0: every output) with one `OUT<n> VS IN<m>` line per
    output, and `SET OUT<n> VS IN<m>` with the new route's line (what the
    real one answers to SET is not recorded). Either may carry the web
    page's address prefix (A00). It records GET as "get vs" and SET as "set
    vs"; it answers a SET it refuses, and any line it does not know, with
    nothing."""

    INPUTS = OUTPUTS = 4
    ROUTE = re.compile(r"(?:A\d+ )?(GET|SET) OUT(\d+) VS(?: IN(\d+))?")

    def __init__(self, routes: list[int] | None = None) -> None:
        super().__init__()
        self.routes = list(routes or [1] * self.OUTPUTS)
        #: Seconds an idle connection stays open.
        self.idle_s = 60.0

    def listing(self) -> list[str]:
        """What it sends on connect."""
        with self.lock:
            return ([f"OUT{n} VS IN{m}" for n, m in enumerate(self.routes, 1)]
                    + [f"OUT{n} EXAUD LEV100" for n in range(1, self.OUTPUTS + 1)])

    def parse_line(self, line: str) -> dict:
        """The request a line makes."""
        match = self.ROUTE.fullmatch(line)
        if match is None:
            return {"comhead": line}
        verb, output, input_ = match.groups()
        request = {"comhead": f"{verb.lower()} vs", "output": int(output)}
        if input_ is not None:
            request["input"] = int(input_)
        return request

    def handlers(self) -> dict:
        return {"get vs": self._get, "set vs": self._set}

    def _lines(self, outputs) -> list[str]:
        return [f"OUT{n} VS IN{self.routes[n - 1]}" for n in outputs]

    def _get(self, request: dict) -> dict:
        output = request["output"]
        if "input" in request or not 0 <= output <= self.OUTPUTS:
            return {"comhead": "get vs", "result": 0, "lines": []}
        outputs = range(1, self.OUTPUTS + 1) if output == 0 else [output]
        return {"comhead": "get vs", "result": 1, "lines": self._lines(outputs)}

    def _set(self, request: dict) -> dict:
        output, input_ = request["output"], request.get("input")
        if not (1 <= output <= self.OUTPUTS and input_ is not None and 1 <= input_ <= self.INPUTS):
            return {"comhead": "set vs", "result": 0, "lines": []}
        self.routes[output - 1] = input_
        return {"comhead": "set vs", "result": 1, "lines": self._lines([output])}

    def get(self, port: int) -> int:
        """The input routed to output `port`."""
        with self.lock:
            return self.routes[port - 1]

    def set(self, port: int, value: int) -> None:
        with self.lock:
            self.routes[port - 1] = value


#: The stand-in for each matrix integration, by its NAME.
SIMS = {"bk808_http": BK808, "ukm404_http": UKM404, "vrroom_http": VRROOM, "avpro_telnet": AVPro}


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        matrix = self.server.matrix
        request = None if matrix.muted else matrix.parse_get(self.path)
        reply = None if request is None else matrix.answer(request)
        if reply is None:
            self.close_connection = True
            return
        self._send(404 if reply.get("result") == 0 else 200, reply)

    def do_POST(self) -> None:  # noqa: N802 (http.server's name)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        matrix = self.server.matrix
        if self.path != PATH or matrix.muted:
            self.close_connection = True
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError:
            request = {"comhead": None, "body": raw.decode("utf-8", errors="replace")}
        if not isinstance(request, dict):
            request = {"comhead": None, "body": request}
        reply = matrix.answer(request)
        if reply is None:
            self.close_connection = True
            return
        self._send(200, reply)

    def _send(self, status: int, reply: dict) -> None:
        data = json.dumps(reply, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _Server(HTTPServer):
    matrix: Matrix


class Server:
    """One stand-in served on 127.0.0.1 (single-threaded, as the real CGI)."""

    def __init__(self, matrix: Matrix, port: int = 0) -> None:
        self.matrix = matrix
        self.httpd = _Server(("127.0.0.1", port), _Handler)
        self.httpd.matrix = matrix
        # stop() waits up to one poll_interval for the loop to notice.
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def address(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.httpd.shutdown()
            self.thread.join(timeout=5)
        self.httpd.server_close()

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

class _TelnetHandler(socketserver.StreamRequestHandler):
    server: "_TelnetServer"

    def handle(self) -> None:
        matrix = self.server.matrix
        if matrix.muted:
            return
        self.connection.settimeout(matrix.idle_s)
        self._send(matrix.listing())
        while True:
            try:
                raw = self.rfile.readline(513)
            except (TimeoutError, OSError):
                return
            if not raw.endswith(b"\n"):
                return
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            if matrix.muted:
                return
            reply = matrix.answer(matrix.parse_line(line))
            self._send(reply["lines"] if reply else [])

    def _send(self, lines: list[str]) -> None:
        try:
            self.wfile.write(b"".join(line.encode("ascii") + b"\r\n" for line in lines))
        except OSError:
            # The client has gone; what it missed is its loss.
            pass


class _TelnetServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    matrix: AVPro


class TelnetServer:
    """One stand-in that speaks telnet (AVPro) served on 127.0.0.1; it
    takes several connections at once. A muted one closes each connection
    at once, unanswered."""

    def __init__(self, matrix: AVPro, port: int = 0) -> None:
        self.matrix = matrix
        self.tcp = _TelnetServer(("127.0.0.1", port), _TelnetHandler)
        self.tcp.matrix = matrix
        # stop() waits up to one poll_interval for the loop to notice.
        self.thread = threading.Thread(target=self.tcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def address(self) -> str:
        host, port = self.tcp.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> "TelnetServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.tcp.shutdown()
            self.thread.join(timeout=5)
        self.tcp.server_close()

    def __enter__(self) -> "TelnetServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def serve(matrix: Matrix) -> "Server | TelnetServer":
    """A server for the stand-in, over the protocol its matrix speaks."""
    return TelnetServer(matrix) if isinstance(matrix, AVPro) else Server(matrix)


def exchange(address: str, lines: list[str], timeout: float = 0.3) -> tuple[list[str], list[list[str]]]:
    """(listing, answers) of one telnet connection sending `lines` in turn
    to an AVPro at HOST:PORT: every line that came before the first was
    sent, and after each, until none came for `timeout` seconds."""
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as conn:
        # Not conn.makefile(): a file object that timed out once reads no more.
        pending = bytearray()

        def read() -> list[str]:
            while True:
                try:
                    chunk = conn.recv(4096)
                except (TimeoutError, OSError):
                    chunk = b""
                if not chunk:
                    lines = bytes(pending).split(b"\r\n")
                    pending[:] = lines.pop()
                    return [raw.decode("ascii", errors="replace") for raw in lines]
                pending.extend(chunk)

        listing = read()
        answers = []
        for line in lines:
            conn.sendall(line.encode("ascii") + b"\r\n")
            answers.append(read())
        return listing, answers
