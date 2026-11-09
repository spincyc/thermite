"""Stand-ins for the matrices' JSON API (oh_integrations._instr) and the
telnet of the HDFury VRROOM (oh_integrations.vrroom_telnet) and the AVPro
matrix (oh_integrations.avpro_telnet), for bin/thermite test routing and
the tests.

Each answers like the BK-808, UKM-404, VRROOM or AVPro it stands in for
(`POST /cgi-bin/instr`, the VRROOM's and the AVPro's lines): the same
comheads, replies of the same shape and, over HTTP, one request at a time.
Every request is recorded, so a check can prove what openHAB sent. Over
HTTP, to a comhead it does not know, or while muted, it closes the
connection unanswered at once, where the real CGI keeps the client waiting
until it times out (and stalls the next request too). Over telnet, a line
it does not know gets no answer, and while muted a connection is closed at
its next line.
"""

from __future__ import annotations

import copy
import json
import re
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from oh_integrations import _instr as instr

PATH = instr.PATH
#: The longest request body taken; the matrices' JSON requests are a few
#: hundred bytes.
MAX_BODY = 64 * 1024


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
        #: Seconds a quiet connection is held, per read: over HTTP, one at a
        #: time, so a client that connects and says nothing holds every
        #: other client only this long.
        self.idle_s = 5.0
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


class TelnetMatrix(Matrix):
    """A stand-in that speaks telnet: serve() gives it a TelnetServer. Each
    line it is sent is a request (parse_line()); its reply's "lines" are
    the answer, and no reply, none."""

    def __init__(self) -> None:
        super().__init__()
        #: Seconds an idle connection stays open.
        self.idle_s = 60.0
        #: The open connections' senders, for what it says unasked.
        self.links: set = set()
        self.links_lock = threading.Lock()

    def listing(self) -> list[str]:
        """What it sends on connect."""
        return []

    def parse_line(self, line: str) -> dict:
        raise NotImplementedError

    def accepts(self) -> bool:
        """Whether a new connection is served (else closed at once)."""
        return not self.muted

    def attach(self, link) -> None:
        with self.links_lock:
            self.links.add(link)

    def detach(self, link) -> None:
        with self.links_lock:
            self.links.discard(link)

    def tell(self, lines: list[str]) -> None:
        """Say `lines` unasked on every open connection."""
        with self.links_lock:
            links = list(self.links)
        for link in links:
            link.send(lines)

    def hang_up(self) -> None:
        """Close every open connection."""
        with self.links_lock:
            links = list(self.links)
        for link in links:
            link.close()


class VRROOM(TelnetMatrix):
    """HDFury VRROOM (oh_integrations.vrroom_telnet): inputs 0-3 on TX0 and
    TX1, over telnet on its port 2222. Its ports are its outputs, 0 for TX0
    and 1 for TX1, each holding an input or COPY (a copy of the other
    output). It answers `get insel` with `insel <tx0> <tx1>`, and `get
    ipinterrupt` with `ipinterrupt on` or `off`; a set of either with the
    line a get would now answer (the real one's acknowledgement of set
    insel is not recorded word for word), and a set it refuses, or any line
    it does not know (an upper-case one too), with nothing. While its
    ipinterrupt is on (a setting, off at first, that a reboot keeps) it says
    `int` on every connection shortly after an output's input changes,
    whoever changed it. `set reboot` gets no answer: shortly after, it
    closes every connection and refuses new ones for reboot_s seconds,
    keeping its settings. It records each request as its first two words
    (`get insel`, `set insel`, `get ipinterrupt`, `set ipinterrupt`, `set
    reboot`) and any other line whole."""

    INPUTS = range(0, 4)
    COPY = 4
    COMMAND = re.compile(r"(get|set) (insel|ipinterrupt|reboot)((?: [a-z0-9]+)*)")
    #: Seconds from a change to its `int`, and from `set reboot` to the
    #: restart.
    DELAY_S = 0.05

    def __init__(self, insel: list[int] | None = None) -> None:
        super().__init__()
        self.insel = list(insel or [0, self.COPY])
        self.interrupts = False
        self.reboot_s = 15.0
        self.reboots = 0
        self.down_until = 0.0

    def accepts(self) -> bool:
        return super().accepts() and time.monotonic() >= self.down_until

    def parse_line(self, line: str) -> dict:
        match = self.COMMAND.fullmatch(line)
        if match is None:
            return {"comhead": line}
        verb, name, values = match.groups()
        return {"comhead": f"{verb} {name}", "values": values.split()}

    def handlers(self) -> dict:
        return {"get insel": self._get_insel, "set insel": self._set_insel,
                "get ipinterrupt": self._get_interrupts, "set ipinterrupt": self._set_interrupts,
                "set reboot": self._reboot}

    def _reply(self, request: dict, lines: list[str], ok: bool = True) -> dict:
        return {"comhead": request["comhead"], "result": int(ok), "lines": lines if ok else []}

    def _get_insel(self, request: dict) -> dict:
        return self._reply(request, [f"insel {self.insel[0]} {self.insel[1]}"], not request["values"])

    def _set_insel(self, request: dict) -> dict:
        allowed = {str(n) for n in [*self.INPUTS, self.COPY]}
        values = request["values"]
        if len(values) != 2 or not set(values) <= allowed or values == [str(self.COPY)] * 2:
            return self._reply(request, [], False)
        self._route([int(value) for value in values])
        return self._get_insel({**request, "values": []})

    def _interrupts(self) -> list[str]:
        return [f"ipinterrupt {'on' if self.interrupts else 'off'}"]

    def _get_interrupts(self, request: dict) -> dict:
        return self._reply(request, self._interrupts(), not request["values"])

    def _set_interrupts(self, request: dict) -> dict:
        if request["values"] not in (["on"], ["off"]):
            return self._reply(request, [], False)
        self.interrupts = request["values"] == ["on"]
        return self._reply(request, self._interrupts())

    def _reboot(self, request: dict) -> dict:
        if request["values"]:
            return self._reply(request, [], False)
        self.reboots += 1
        threading.Timer(self.DELAY_S, self._restart).start()
        return self._reply(request, [])

    def _restart(self) -> None:
        with self.lock:
            self.down_until = time.monotonic() + self.reboot_s
        self.hang_up()

    def _route(self, insel: list[int]) -> None:
        """Change the outputs' inputs (the lock held), and say so if asked to."""
        changed = insel != self.insel
        self.insel = insel
        if changed and self.interrupts:
            threading.Timer(self.DELAY_S, self.tell, args=(["int"],)).start()

    def get(self, port: int) -> int:
        """The input output `port` (0 for TX0) shows."""
        with self.lock:
            return self.insel[port]

    def set(self, port: int, value: int) -> None:
        with self.lock:
            insel = list(self.insel)
            insel[port] = value
            self._route(insel)


class AVPro(TelnetMatrix):
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
SIMS = {"bk808_http": BK808, "ukm404_http": UKM404, "vrroom_telnet": VRROOM, "avpro_telnet": AVPro}


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def setup(self) -> None:
        self.timeout = self.server.matrix.idle_s
        super().setup()
        # The connection in hand, for stop() to end.
        self.server.connection = self.connection

    def do_POST(self) -> None:  # noqa: N802 (http.server's name)
        # A length that is not a byte count up to MAX_BODY is refused before
        # any read: rfile.read() of a negative one reads to EOF.
        length = (self.headers.get("Content-Length") or "0").strip()
        if not (length.isascii() and length.isdigit() and int(length) <= MAX_BODY):
            self.send_error(400, "bad Content-Length")
            return
        raw = self.rfile.read(int(length))
        matrix = self.server.matrix
        if self.path != PATH or matrix.muted:
            self.close_connection = True
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
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
    connection: socket.socket | None = None


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
            # shutdown() waits for the request in hand, which a client that
            # trickles or says nothing holds open: ask for it on the side,
            # end that connection, and wait a bounded time.
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
            connection = self.httpd.connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
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
        if not matrix.accepts():
            return
        self.sending = threading.Lock()
        self.connection.settimeout(matrix.idle_s)
        self.send(matrix.listing())
        matrix.attach(self)
        try:
            self._serve(matrix)
        finally:
            matrix.detach(self)

    def _serve(self, matrix: TelnetMatrix) -> None:
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
            self.send(reply["lines"] if reply else [])

    def send(self, lines: list[str]) -> None:
        if not lines:
            return
        try:
            with self.sending:
                self.wfile.write(b"".join(line.encode("ascii") + b"\r\n" for line in lines))
        except OSError:
            # The client has gone; what it missed is its loss.
            pass

    def close(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _TelnetServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    matrix: TelnetMatrix


class TelnetServer:
    """One stand-in that speaks telnet (VRROOM, AVPro) served on 127.0.0.1;
    it takes several connections at once. A muted one closes each
    connection at once, unanswered."""

    def __init__(self, matrix: TelnetMatrix, port: int = 0) -> None:
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
    return TelnetServer(matrix) if isinstance(matrix, TelnetMatrix) else Server(matrix)


def exchange(address: str, lines: list[str], timeout: float = 0.3) -> tuple[list[str], list[list[str]]]:
    """(listing, answers) of one telnet connection sending `lines` in turn
    to a telnet stand-in at HOST:PORT: every line that came before the
    first was sent, and after each, until none came for `timeout` seconds."""
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
