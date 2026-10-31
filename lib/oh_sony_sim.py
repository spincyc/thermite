"""A stand-in Sony BRAVIA TV's IP control on 127.0.0.1, for thermite test
controls (lib/oh_testing/controls.py): the JSON services the
sony_ip_control rule and sony_rest's Thing use (system, avContent, audio,
accessControl) and IRCC, with Sony's Normal authentication.

The TV takes a request but getPowerStatus only with an auth cookie it gave:
actRegister without credentials answers 401 (a real TV then shows its PIN),
and with the PIN as HTTP Basic credentials 200 and a new cookie. A stand-in
made with a cookie is paired already: registration() is what the rule keeps
in userdata for it.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Sony's cookie lifetime, in seconds.
LIFETIME = 14 * 24 * 60 * 60
HDMI = re.compile(r"extInput:hdmi\?port=([1-4])")
IRCC = re.compile(r"<IRCCCode>([^<]+)</IRCCCode>")
IRCC_ACTION = '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'


class TV:
    """What the TV shows, and every request that changed something."""

    def __init__(self, pin: str = "0000", cookie: str | None = None, max_volume: int = 100) -> None:
        self.lock = threading.Lock()
        self.pin = pin
        self.cookies = {cookie} if cookie else set()
        self.power = True
        self.uri = "extInput:hdmi?port=1"
        self.volume = 10
        self.muted = False
        self.max_volume = max_volume
        #: Every set* method, actRegister and IRCC code, in order.
        self.commands: list[str] = []
        self.issued = 0

    def registration(self, clientid: str = "openHAB:stand-in") -> dict:
        """What the sony_ip_control rule keeps in userdata for this TV, paired."""
        cookie = next(iter(self.cookies))
        return {"clientid": clientid, "nickname": "openHAB", "pin": self.pin, "refused": False, "cookie": cookie,
                "expires": int((time.time() + LIFETIME) * 1000)}

    def register(self, authorization: str | None) -> tuple[int, dict, str | None]:
        self.commands.append("actRegister")
        expected = "Basic " + base64.b64encode(f":{self.pin}".encode()).decode()
        if authorization != expected:
            return 401, {"error": [401, "Unauthorized"], "id": 8}, None
        self.issued += 1
        cookie = f"stand-in-{self.issued}"
        self.cookies.add(cookie)
        return 200, {"result": [], "id": 8}, f"auth={cookie}; Path=/sony/; Max-Age={LIFETIME}"

    def answer(self, method: str, params: list) -> dict:
        if method == "getPowerStatus":
            return {"result": [{"status": "active" if self.power else "standby"}]}
        if method == "setPowerStatus":
            self.commands.append(f"setPowerStatus {params[0]['status']}")
            self.power = params[0]["status"] is True
            return {"result": []}
        if not self.power:
            return {"error": [40005, "Display Is Turned off"]}
        if method == "getPlayingContentInfo":
            return {"result": [{"uri": self.uri, "source": "extInput:hdmi", "title": "HDMI"}]}
        if method == "setPlayContent":
            match = HDMI.fullmatch(str(params[0].get("uri")))
            if not match:
                return {"error": [3, "Illegal Argument"]}
            self.commands.append(f"setPlayContent {params[0]['uri']}")
            self.uri = params[0]["uri"]
            return {"result": []}
        if method == "getVolumeInformation":
            return {"result": [[{"target": "speaker", "volume": self.volume, "mute": self.muted,
                                 "maxVolume": self.max_volume, "minVolume": 0}]]}
        if method == "setAudioVolume":
            volume = str(params[0].get("volume"))
            if params[0].get("target") != "speaker" or not volume.isdigit():
                return {"error": [3, "Illegal Argument"]}
            self.commands.append(f"setAudioVolume {volume}")
            self.volume = min(int(volume), self.max_volume)
            return {"result": [0]}
        if method == "setAudioMute":
            self.commands.append(f"setAudioMute {params[0]['status']}")
            self.muted = params[0]["status"] is True
            return {"result": [0]}
        return {"error": [12, "No Such Method"]}

    def sets(self) -> list[str]:
        with self.lock:
            return [c for c in self.commands if not c.startswith("ircc ")]

    def keys(self) -> list[str]:
        """The IRCC codes it was sent, in order."""
        with self.lock:
            return [c[len("ircc "):] for c in self.commands if c.startswith("ircc ")]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def reply(self, status: int, body: bytes, kind: str = "application/json", cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        tv: TV = self.server.tv
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        match = re.fullmatch(r"auth=(.+)", self.headers.get("Cookie") or "")
        authed = bool(match) and match.group(1) in tv.cookies
        with tv.lock:
            if self.path == "/sony/IRCC":
                code = IRCC.search(body)
                if self.headers.get("SOAPACTION") != IRCC_ACTION or not code:
                    return self.reply(500, b"", "text/xml")
                if not authed:
                    return self.reply(403, b"", "text/xml")
                tv.commands.append(f"ircc {code.group(1)}")
                return self.reply(200, b'<?xml version="1.0"?><s:Envelope/>', "text/xml")
            if self.path not in ("/sony/system", "/sony/avContent", "/sony/audio", "/sony/accessControl"):
                return self.reply(404, b"")
            try:
                request = json.loads(body)
                method, params = request["method"], request.get("params") or []
            except (ValueError, KeyError, TypeError):
                return self.reply(400, b"")
            if method == "actRegister":
                status, answer, cookie = tv.register(self.headers.get("Authorization"))
                return self.reply(status, json.dumps(answer).encode(), cookie=cookie)
            if method != "getPowerStatus" and not authed:
                return self.reply(403, b"")
            answer = {**tv.answer(method, params), "id": request.get("id")}
            self.reply(200, json.dumps(answer).encode())


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class Server:
    """One stand-in TV served on 127.0.0.1."""

    def __init__(self, tv: TV, port: int = 0) -> None:
        self.tv = tv
        self.httpd = _Server(("127.0.0.1", port), _Handler)
        self.httpd.tv = tv
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
