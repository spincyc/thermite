"""A stand-in for a Sony projector's ADCP server (oh_integrations.sony_adcp),
for bin/thermite test controls and the tests.

It speaks ADCP as the VPL-VW915ES does on TCP 53595: a NOKEY greeting (no
authentication), then one CRLF-terminated request line and one answer line
at a time: `ok`, a quoted value or an `err_*` token, `err_cmd` for a request
it does not know. Power runs through startup and cooling1/cooling2 for
startup_s and cooling_s seconds of its clock (0 for none); input,
picture_mode, aspect and key need the projector on, as the real one's do,
while power_status, input and modelname answer in standby too. It records
every request line, closes a connection idle for idle_s seconds, and can be
told to answer nothing (muted) or to leave the first request of its next
`wake` connections unanswered, as a VW series projector waking from standby
may.
"""

from __future__ import annotations

import re
import socket
import socketserver
import threading
import time
from collections.abc import Callable

PORT = 53595
INPUTS = ("hdmi1", "hdmi2")
# What the example's VPL-VW915ES (firmware 6.003) lists for `<name> ? --info`.
PICTURE_MODES = ("cinema_film1", "cinema_film2", "reference", "tv", "photo", "game", "brt_cinema", "brt_tv",
                 "user")
ASPECTS = ("1.85_1_zoom", "2.35_1_zoom", "stretch", "v_stretch", "normal", "squeeze")
KEY_CODES = frozenset("""
    motionflow black_level power picture+ picture- color+ color- bright+ bright- hue+ hue- sharpness+
    sharpness- blank status_on status_off menu input_a input_b power_on power_off dec right left up down
    adj_r adj_g adj_b cutoff_r wb_gain wb_bias input enter memory lens_shift_up lens_shift_down
    lens_focus_far lens_focus_near lens_zoom_up lens_zoom_down reset lens_shift_left lens_shift_right 3d
    color_mode reality_creation picmode1 picmode2 picmode3 picmode4 picmode5 picmode6 picmode7 picmode8
    picmode9 picmode color_temp gamma_correction iris_mode lens_zoom lens_shift lens_focus aspect
    lens_control 2d_3d_display_select 3d_format 3d_depth+ 3d_depth- simulated_3d_effect lens_position
    color_correction lens_position1 lens_position2 lens_position3 lens_position4 lens_position5
    2d_3d_display_select_auto 2d_3d_display_select_3d 3d_format_over_under 3d_format_side_by_side
    3d_format_simulated_3d simulated_3d_effect_high simulated_3d_effect_middle simulated_3d_effect_low
    aspect_normal aspect_v_stretch aspect_1.85_1_zoom aspect_2.35_1_zoom aspect_stretch aspect_squeeze
    laser_brightness+ laser_brightness-
""".split())
REQUEST = re.compile(r'([a-z0-9_]+)(?: (\?|"[^"\\]*"))?')


def is_query(line: str) -> bool:
    """Whether a request line only reads (`name ?`)."""
    return line.endswith(" ?")


class Projector:
    """State, answers and the request log of one stand-in projector."""

    def __init__(self, power_status: str = "standby", input_: str = "hdmi1", picture_mode: str = "reference",
                 aspect: str = "normal", model: str = "VPL-VW915ES", *, startup_s: float = 0.0,
                 cooling_s: float = 0.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.requests: list[str] = []
        self.greeting = "NOKEY"
        #: When set, it greets and answers nothing.
        self.muted = False
        #: How many coming connections get no answer to their first request.
        self.wake = 0
        self.idle_s = 60.0
        self.startup_s = startup_s
        self.cooling_s = cooling_s
        self.input = input_
        self.picture_mode = picture_mode
        self.aspect = aspect
        self.model = model
        self.lock = threading.Lock()
        self.clock = clock
        self._phase = (power_status, clock())

    @property
    def power_status(self) -> str:
        state, since = self._phase
        elapsed = self.clock() - since
        if state == "startup" and elapsed >= self.startup_s:
            return "on"
        if state == "cooling1" and elapsed >= self.cooling_s:
            return "standby"
        if state == "cooling1" and elapsed >= self.cooling_s / 2:
            return "cooling2"
        return state

    def set_power(self, state: str) -> None:
        """Put it in a power_status at once, as its own remote would."""
        with self.lock:
            self._phase = (state, self.clock())

    def answer(self, line: str) -> str:
        with self.lock:
            self.requests.append(line)
            return self._answer(line)

    def _answer(self, line: str) -> str:
        match = REQUEST.fullmatch(line)
        if match is None:
            return "err_cmd"
        name, arg = match.groups()
        value = arg[1:-1] if arg and arg != "?" else None
        status = self.power_status
        on = status == "on"
        if name == "power_status" and arg == "?":
            return f'"{status}"'
        if name == "modelname" and arg == "?":
            return f'"{self.model}"'
        if name == "power" and value is not None:
            return self._power(value, status)
        if name == "input" and arg == "?":
            return f'"{self.input}"'
        settings = {"input": INPUTS, "picture_mode": PICTURE_MODES, "aspect": ASPECTS, "key": KEY_CODES}
        if name not in settings or arg is None or (name == "key" and arg == "?"):
            return "err_cmd"
        if not on:
            return "err_inactive"
        if arg == "?":
            return f'"{getattr(self, name)}"'
        if value not in settings[name]:
            return "err_val"
        if name != "key":
            setattr(self, name, value)
        return "ok"

    def _power(self, value: str, status: str) -> str:
        if value not in ("on", "off"):
            return "err_val"
        if value == "on" and status == "standby":
            self._phase = ("startup", self.clock())
        elif value == "on" and status.startswith("cooling"):
            return "err_inactive"
        elif value == "off" and status in ("startup", "on"):
            self._phase = ("cooling1", self.clock())
        return "ok"

    def lines(self) -> list[str]:
        """Every request line received, in order."""
        with self.lock:
            return list(self.requests)

    def sets(self) -> list[str]:
        """Every request line that could change something."""
        return [line for line in self.lines() if not is_query(line)]


#: The stand-in for each integration, by its NAME.
SIMS = {"sony_adcp": Projector}


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        projector = self.server.projector
        with projector.lock:
            wakes = projector.wake > 0
            if wakes:
                projector.wake -= 1
        self.connection.settimeout(projector.idle_s)
        self.wfile.write(projector.greeting.encode("ascii") + b"\r\n")
        first = True
        while True:
            try:
                raw = self.rfile.readline(513)
            except (TimeoutError, OSError):
                return
            if not raw.endswith(b"\r\n"):
                return
            line = raw[:-2].decode("ascii", errors="replace")
            if projector.muted or (first and wakes):
                with projector.lock:
                    projector.requests.append(line)
            else:
                self.wfile.write(projector.answer(line).encode("ascii") + b"\r\n")
            first = False


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    projector: Projector


class Server:
    """One stand-in projector served on 127.0.0.1."""

    def __init__(self, projector: Projector, port: int = 0) -> None:
        self.projector = projector
        self.tcp = _Server(("127.0.0.1", port), _Handler)
        self.tcp.projector = projector
        # stop() waits up to one poll_interval for the loop to notice.
        self.thread = threading.Thread(target=self.tcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def address(self) -> str:
        host, port = self.tcp.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.tcp.shutdown()
            self.thread.join(timeout=5)
        self.tcp.server_close()

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def exchange(address: str, lines: list[str], timeout: float = 3.0) -> tuple[str | None, list[str | None]]:
    """(greeting, answers) of one connection sending `lines` in turn to an
    ADCP server at HOST:PORT; None where nothing came within `timeout`, after
    which the connection answers nothing more."""
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as conn:
        reader = conn.makefile("rb")

        def read() -> str | None:
            try:
                raw = reader.readline(513)
            except (TimeoutError, OSError):
                return None
            return raw[:-2].decode("ascii", errors="replace") if raw.endswith(b"\r\n") else None

        greeting = read()
        answers: list[str | None] = []
        for line in lines:
            if answers and answers[-1] is None:
                answers.append(None)
                continue
            conn.sendall(line.encode("ascii") + b"\r\n")
            answers.append(read())
        return greeting, answers
