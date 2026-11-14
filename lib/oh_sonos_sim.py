"""Loopback Sonos Item-contract stand-in; not a UPnP/SOAP implementation.

The controls check exercises the native Item profiles and remote-key rule
against these deterministic fields. Actual Sonos binding discovery/channel
support is verified separately; this transport never contacts a player.
"""
from __future__ import annotations

import http.server
import threading
import urllib.parse


class Player:
    #: Each setting a page commands (oh_integrations.sonos SETTINGS and
    #: ARC_SETTINGS) -> the values it takes; it starts at 0, else the first.
    SETTINGS = {**{name: ("OFF", "ON") for name in ("shuffle", "loudness", "night", "speech", "led", "subwoofer",
                                                     "surround")},
                "repeat": ("OFF", "ONE", "ALL"), "music_mode": ("0", "1"),
                **{name: tuple(str(n) for n in range(-10, 11)) for name in ("bass", "treble", "height")},
                **{name: tuple(str(n) for n in range(-15, 16)) for name in ("sub_gain", "surround_tv", "surround_music")}}

    def __init__(self, udn: str) -> None:
        self.udn = udn
        self.volume = 25
        self.mute = "OFF"
        self.playing = True
        self.settings = {name: "0" if "0" in values else values[0] for name, values in self.SETTINGS.items()}
        self.commands: list[tuple[str, str]] = []
        self.presses: list[str] = []

    def read(self, point: str) -> str | None:
        return {"volume": str(self.volume), "mute": self.mute, "state": "PLAYING" if self.playing else "PAUSED_PLAYBACK",
                "codec": "Dolby Digital", "line_in": "ON", "title": "TV", "artist": "", "album": "",
                **self.settings}.get(point)

    def command(self, point: str, value: str) -> bool:
        key = None
        if point == "volume" and value.isdigit() and 0 <= int(value) <= 100:
            self.volume = int(value)
        elif point == "mute" and value in ("ON", "OFF"):
            self.mute = value
        elif point in self.SETTINGS and value in self.SETTINGS[point]:
            self.settings[point] = value
            # The remote's Night and Speech keys reach their switches.
            key = point if point in ("night", "speech") else None
        elif point == "player" and value in ("PREVIOUS", "NEXT", "PLAY", "PAUSE"):
            key = {"PREVIOUS": "previous", "NEXT": "next", "PLAY": "play_pause", "PAUSE": "play_pause"}[value]
            if value in ("PLAY", "PAUSE"):
                self.playing = value == "PLAY"
        elif point == "tv" and value == self.udn:
            key = "tv"
            self.playing = True
        else:
            return False
        self.commands.append((point, value))
        if key:
            self.presses.append(key)
        return True


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # The binding polls each of a Thing's requests at once; past the listen
    # backlog (socketserver's 5) a connection is dropped and its client
    # retries only a second later, which the latency check would time.
    request_queue_size = 128


class Server:
    def __init__(self, player: Player) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qs(url.query, keep_blank_values=True)
                point = params.get("point", [""])[0]
                value = player.read(point) if url.path == "/state" else None
                if url.path == "/command":
                    value = "OK" if player.command(point, params.get("value", [""])[0]) else None
                body = (value if value is not None else "Not found").encode()
                self.send_response(200 if value is not None else 404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = _Server(("127.0.0.1", 0), Handler)
        self.thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"{host}:{port}"

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.thread:
            self.httpd.shutdown()
            self.thread.join(timeout=3)
            self.thread = None
        self.httpd.server_close()
