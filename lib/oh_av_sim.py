"""Stand-ins for a Marantz receiver's HTTP API
(oh_integrations.denonmarantz_http) and a WiiM player's LinkPlay HTTP API
(oh_integrations.wiim_http), for bin/thermite test controls and the tests.

Receiver answers as the CINEMA 60 and CINEMA 30 do on port 8080:
Deviceinfo.xml, a 403 for the legacy main zone status pages (so the binding
polls with AppCommand.xml, as it must on these models), AppCommand.xml POSTs
for power, volume, mute, source, surround mode and net audio status, and
formZone<n>_Zone<n>XmlStatusLite.xml for the other zones. A GET of
formiPhoneAppDirect.xml?<command> runs one control-protocol command: power,
volume (MV, the 0-98 scale), mute, source (SI), surround mode (MS), menu
(MN) and zone 2 power and source (Z2, SOURCE among them); it answers 200 to any other too, as the receiver
does, and lists it in `unknown`.

Player answers GET /httpapi.asp?command=<command> as the WiiM Amp Pro does,
in plain HTTP (the real one is HTTPS only): getPlayerStatus, EQGetBand,
getMetaInfo, getStatusEx, getSubLPF and LED_SWITCH_GET; the setPlayerCmd
commands vol, mute, switchmode, loopmode, onepause, play, pause, prev and
next; EQOn, EQOff, EQLoad:<preset>, setSubLPF:level:<dB> and
LED_SWITCH_SET:<0|1>. It reads the query as a web server does, so an
unescaped "&" in a value ends the command there. A setPlayerCmd it does
not take answers Failed, another command it does not take Failed or
`unknown command`, as the player does.

Both record every request and every command, apply commands whatever the
power state, and can be told to answer nothing (muted).
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ElementTree
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

#: A reply: HTTP status, Content-Type, body.
Reply = tuple[int, str, bytes]
XML = "text/xml; charset=utf-8"
#: The longest request body taken.
MAX_BODY = 64 * 1024


class Device:
    """The request log of one stand-in, and its answers."""

    def __init__(self) -> None:
        #: {"method", "path", "body"} of every request, in order.
        self.requests: list[dict] = []
        #: Every command received (a control-protocol command, a LinkPlay
        #: command), decoded, in order.
        self.commands: list[str] = []
        self.unknown: list[str] = []
        #: When set, every request gets its connection closed unanswered.
        self.muted = False
        self.lock = threading.Lock()

    def answer(self, method: str, path: str, body: bytes) -> Reply | None:
        with self.lock:
            self.requests.append({"method": method, "path": path, "body": body.decode("utf-8", "replace")})
            return self.reply(method, path, body)

    def reply(self, method: str, path: str, body: bytes) -> Reply | None:
        raise NotImplementedError


def _xml(text: str) -> Reply:
    return 200, XML, ('<?xml version="1.0" encoding="utf-8" ?>\n' + text).encode()


def _on(value: bool) -> str:
    return "ON" if value else "OFF"


class Receiver(Device):
    """A Marantz CINEMA receiver. `volume` is on the receiver's 0-98 scale
    (80 is 0 dB), as the binding's Dimmer shows it."""

    MAX_VOLUME = 98
    #: Every source code SI takes (Denon and Marantz AVR control protocol).
    SOURCES = frozenset({"PHONO", "CD", "DVD", "BD", "TV", "SAT/CBL", "MPLAY", "GAME", "GAME1", "GAME2", "TUNER",
                         "AUX1", "AUX2", "NET", "BT", "USB/IPOD", "USB", "8K"})
    #: SI codes the receiver reports by another name.
    REPORTED = {"GAME": "GAME1"}
    MENU = frozenset({"MNCUP", "MNCDN", "MNCLT", "MNCRT", "MNENT", "MNRTN", "MNOPT", "MNINF", "MNMEN ON",
                      "MNMEN OFF"})
    SURROUND = {"MOVIE": "Movie", "MUSIC": "Music", "GAME": "Game", "PURE DIRECT": "Pure Direct",
                "DIRECT": "Direct", "STEREO": "Stereo", "AUTO": "Auto"}
    LEGACY = ("/goform/formMainZone_MainZoneXml.xml", "/goform/formMainZone_MainZoneXmlStatus.xml")
    ZONE_LITE = re.compile(r"/goform/formZone(\d)_Zone(\d)XmlStatusLite\.xml")
    VOLUME = re.compile(r"MV(\d\d)(5?)")

    def __init__(self, model: str = "CINEMA 60", zones: int = 2, *, power: bool = False, volume: float = 52.0,
                 mute: bool = False, source: str = "SAT/CBL", surround: str = "Multi Ch Stereo") -> None:
        super().__init__()
        self.model = model
        self.zones = zones
        self.power = power
        self.volume = volume
        self.mute = mute
        self.source = source
        self.surround = surround
        # The verified main-zone display enum: 1 absolute, 2 relative dB.
        self.volume_scale = 1
        #: Zone number -> {"power", "source", "volume" (dB), "mute"}.
        self.zone = {n: {"power": False, "source": "SOURCE", "volume": -40, "mute": False}
                     for n in range(2, zones + 1)}

    def reply(self, method: str, path: str, body: bytes) -> Reply | None:
        url = urllib.parse.urlsplit(path)
        if method == "GET" and url.path == "/goform/formiPhoneAppDirect.xml":
            self.run(urllib.parse.unquote_plus(url.query))
            return 200, XML, b""
        if method == "GET" and url.path == "/goform/Deviceinfo.xml":
            return _xml(f"<Device_Info><DeviceZones>{self.zones}</DeviceZones>"
                        f"<ModelName>*{escape(self.model)}</ModelName></Device_Info>")
        if method == "GET" and url.path == "/ajax/globals/get_config" and url.query == "type=12":
            return _xml(f"<listGlobals><MainZone><Volume>{round(self.volume * 10)}</Volume>"
                        f"<VolumeScale>{self.volume_scale}</VolumeScale>"
                        f"<Max>{self.MAX_VOLUME * 10}</Max></MainZone></listGlobals>")
        if method == "GET" and url.path in self.LEGACY:
            return 403, "text/html", b"<html><body>403 Forbidden</body></html>"
        zone = self.ZONE_LITE.fullmatch(url.path)
        if method == "GET" and zone and zone[1] == zone[2] and int(zone[1]) in self.zone:
            state = self.zone[int(zone[1])]
            return _xml(f"<item><Power><value>{_on(state['power'])}</value></Power>"
                        f"<InputFuncSelect><value>{escape(state['source'])}</value></InputFuncSelect>"
                        "<VolumeDisplay><value>Absolute</value></VolumeDisplay>"
                        f"<MasterVolume><value>{state['volume']}</value></MasterVolume>"
                        f"<Mute><value>{'on' if state['mute'] else 'off'}</value></Mute></item>")
        if method == "POST" and url.path == "/goform/AppCommand.xml":
            return self.app_command(body)
        if method == "POST" and url.path == "/goform/AppCommand0300.xml":
            return self.signal_command(body)
        return 404, "text/html", b"<html><body>404 Not Found</body></html>"

    def signal_command(self, body: bytes) -> Reply:
        """The verified read-only signal methods; no control commands here."""
        fields = {"GetAudioInfo": {"inputmode": "HDMI", "output": "Speaker", "signal": "PCM",
                                   "sound": self.surround, "fs": "48 kHz"},
                  "GetVideoInfo": {"videooutput": "Auto(Dual)", "hdmisigin": "4K60", "hdmisigout": "4K60"}}
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return 400, XML, b""
        replies = []
        for cmd in root.findall("cmd"):
            name = cmd.findtext("name") or ""
            params = [param.get("name") for param in cmd.findall("list/param")]
            if cmd.get("id") != "3" or name not in fields or params != list(fields[name]):
                self.unknown.append(f"AppCommand0300 {name}")
                return 400, XML, b""
            values = "".join(f'<param name="{key}" control="1">{escape(value)}</param>'
                             for key, value in fields[name].items())
            replies.append(f"<cmd><name>{name}</name><list>{values}</list></cmd>")
        return _xml("<rx>" + "".join(replies) + "</rx>")

    def app_command(self, body: bytes) -> Reply:
        try:
            names = [(cmd.text or "").strip() for cmd in ElementTree.fromstring(body).iter("cmd")]
        except ElementTree.ParseError:
            return 400, "text/html", b""
        replies = []
        for name in names:
            if name == "GetAllZonePowerStatus":
                zones = "".join(f"<zone{n}>{_on(state['power'])}</zone{n}>" for n, state in self.zone.items())
                replies.append(f"<zone1>{_on(self.power)}</zone1>{zones}")
            elif name == "GetVolumeLevel":
                replies.append(f"<volume>{self.volume - 80:.1f}</volume><state>variable</state><limit>OFF</limit>"
                               f"<disptype>ABSOLUTE</disptype><dispvalue>{self.volume:.1f}</dispvalue>")
            elif name == "GetMuteStatus":
                replies.append(f"<mute>{'on' if self.mute else 'off'}</mute>")
            elif name == "GetSourceStatus":
                replies.append(f"<source>{escape(self.source)}</source>")
            elif name == "GetSurroundModeStatus":
                replies.append(f"<surround>{escape(self.surround)}</surround>")
            else:
                if name != "GetNetAudioStatus":
                    self.unknown.append(f"AppCommand {name}")
                replies.append("")
        return _xml("<rx>" + "".join(f"<cmd>{reply}</cmd>" for reply in replies) + "</rx>")

    def run(self, command: str) -> None:
        """Run one control-protocol command, as formiPhoneAppDirect.xml does."""
        self.commands.append(command)
        volume = self.VOLUME.fullmatch(command)
        if command in ("PWON", "ZMON"):
            self.power = True
        elif command in ("PWSTANDBY", "ZMOFF"):
            self.power = False
            if command == "PWSTANDBY":
                for state in self.zone.values():
                    state["power"] = False
        elif command in ("MUON", "MUOFF"):
            self.mute = command == "MUON"
        elif command in ("MVUP", "MVDOWN"):
            self.volume = min(max(self.volume + (0.5 if command == "MVUP" else -0.5), 0), self.MAX_VOLUME)
        elif volume:
            self.volume = min(int(volume[1]) + (0.5 if volume[2] else 0), self.MAX_VOLUME)
        elif command.startswith("SI") and command[2:] in self.SOURCES:
            self.source = self.REPORTED.get(command[2:], command[2:])
        elif command.startswith("MS") and command[2:] in self.SURROUND:
            self.surround = self.SURROUND[command[2:]]
        elif command in self.MENU or command.endswith("?"):
            pass
        elif command in ("Z2ON", "Z2OFF") and 2 in self.zone:
            self.zone[2]["power"] = command == "Z2ON"
        elif command.startswith("Z2") and 2 in self.zone and command[2:] in self.SOURCES | {"SOURCE"}:
            self.zone[2]["source"] = self.REPORTED.get(command[2:], command[2:])
        else:
            self.unknown.append(command)


def hex_text(text: str) -> str:
    """Text as getPlayerStatus sends Title and Artist: hex-encoded UTF-8."""
    return text.encode("utf-8").hex().upper()


class Player(Device):
    """A WiiM Amp Pro."""

    MAX_VOLUME = 100
    #: switchmode name -> the getPlayerStatus mode it selects, for the
    #: inputs the Amp Pro has (getStatusEx plm_support).
    MODES = {"wifi": "10", "line-in": "40", "bluetooth": "41", "optical": "43", "HDMI": "49"}
    PLM_SUPPORT = "0x2b00416"
    #: EQGetList on an Amp Pro (read 2026-11-14).
    EQ_PRESETS = ("Flat", "Acoustic", "Bass Booster", "Bass Reducer", "Classical", "Dance", "Deep", "Electronic",
                  "Game", "Hip-Hop", "Jazz", "Latin", "Loudness", "Lounge", "Movie", "Piano", "Pop", "R&B", "Rock",
                  "Small Speakers", "Spoken Word", "Treble Booster", "Treble Reducer", "Vocal Booster")
    #: setPlayerCmd:loopmode values it takes (Arylic's table).
    LOOPS = ("0", "1", "2", "3", "4", "5")

    def __init__(self, *, status: str = "play", volume: int = 30, mute: bool = False, mode: str = "43",
                 title: str = "", artist: str = "", album: str = "", vendor: str = "", name: str = "WiiM Amp Pro",
                 position: int = 0, duration: int = 0, sample_rate: str = "unknow", bit_depth: str = "unknow") -> None:
        super().__init__()
        self.status = status
        self.volume = volume
        self.mute = mute
        self.mode = mode
        self.title = title
        self.artist = artist
        self.album = album
        self.vendor = vendor
        self.name = name
        self.position = position
        self.duration = duration
        self.sample_rate = sample_rate
        self.bit_depth = bit_depth
        self.loop = "4"
        self.eq = True
        self.eq_preset = "Flat"
        self.sub_level = 0
        self.light = True

    def reply(self, method: str, path: str, body: bytes) -> Reply | None:
        url = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        if method != "GET" or url.path != "/httpapi.asp" or len(query.get("command", ())) != 1:
            return 404, "text/html", b"<html><body>404 Not Found</body></html>"
        command = query["command"][0]
        self.commands.append(command)
        if command == "getPlayerStatus":
            return self._json({"type": "0", "ch": "0", "mode": self.mode, "loop": self.loop, "eq": "0",
                               "vendor": self.vendor, "status": self.status, "curpos": str(self.position),
                               "offset_pts": "0", "totlen": str(self.duration), "Title": hex_text(self.title),
                               "Artist": hex_text(self.artist), "Album": hex_text(self.album), "alarmflag": "0",
                               "plicount": "0", "plicurr": "0", "vol": str(self.volume),
                               "mute": "1" if self.mute else "0"})
        if command == "EQGetBand":
            return self._json({"status": "OK", "EQStat": "On" if self.eq else "Off", "Name": self.eq_preset,
                               "source_name": "optical", "pluginURI": "http://moddevices.com/plugins/caps/Eq10HP"})
        if command == "getMetaInfo":
            return self._json({"metaData": {"album": self.album or "unknow", "title": self.title or "unknow",
                                            "artist": self.artist or "unknow", "albumArtURI": "un_known",
                                            "sampleRate": self.sample_rate, "bitDepth": self.bit_depth,
                                            "bitRate": "unknow", "trackId": "0"}})
        if command == "getStatusEx":
            return self._json({"DeviceName": self.name, "project": "WiiM_Amp_Pro", "plm_support": self.PLM_SUPPORT,
                               "firmware": "Linkplay.5.2.833845", "VersionUpdate": "0", "NewVer": "0",
                               "internet": "1", "temperature_cpu": "38", "temperature_tmp102": "31",
                               "BleRemoteControl": "1", "BleRemoteConnected": "1", "BleRemoteBatterylevel": "40"})
        if command == "getSubLPF":
            return self._json({"status": 1, "plugged": 1, "cross": 90, "phase": 0, "level": self.sub_level,
                               "main_filter": 1, "sub_filter": 1, "sub_delay": 0})
        if command == "LED_SWITCH_GET":
            return self._text("1" if self.light else "0")
        if command.startswith("setPlayerCmd:"):
            return self._text("OK" if self.player_command(command[len("setPlayerCmd:"):]) else "Failed")
        if command in ("EQOn", "EQOff"):
            self.eq = command == "EQOn"
            return self._json({"status": "OK"})
        if command.startswith("EQLoad:") and command[len("EQLoad:"):] in self.EQ_PRESETS:
            self.eq, self.eq_preset = True, command[len("EQLoad:"):]
            return self._json({"status": "OK"})
        if re.fullmatch(r"setSubLPF:level:-?\d+", command) and -15 <= int(command.rsplit(":", 1)[1]) <= 15:
            self.sub_level = int(command.rsplit(":", 1)[1])
            return self._text("OK")
        if command in ("LED_SWITCH_SET:0", "LED_SWITCH_SET:1"):
            self.light = command.endswith("1")
            return self._text("OK")
        self.unknown.append(command)
        return self._json({"status": "Failed"}) if command.startswith(("EQLoad:", "setSubLPF:")) else \
            self._text("unknown command")

    def player_command(self, command: str) -> bool:
        name, _, value = command.partition(":")
        if name == "vol" and value.isdigit() and int(value) <= self.MAX_VOLUME:
            self.volume = int(value)
        elif name == "mute" and value in ("0", "1"):
            self.mute = value == "1"
        elif name == "switchmode" and value in self.MODES:
            self.mode = self.MODES[value]
        elif name == "loopmode" and value in self.LOOPS:
            self.loop = value
        elif command == "onepause":
            self.status = "pause" if self.status == "play" else "play"
        elif command in ("play", "resume"):
            self.status = "play"
        elif command in ("pause", "stop"):
            self.status = command
        elif command not in ("prev", "next"):
            self.unknown.append(f"setPlayerCmd:{command}")
            return False
        return True

    @staticmethod
    def _json(value: dict) -> Reply:
        return 200, "text/html", json.dumps(value, separators=(",", ":")).encode()

    @staticmethod
    def _text(text: str) -> Reply:
        return 200, "text/html", text.encode()


#: The stand-in for each AV integration, by its NAME.
SIMS = {"denonmarantz_http": Receiver, "wiim_http": Player}


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        self._answer(b"")

    def do_POST(self) -> None:  # noqa: N802 (http.server's name)
        # A length that is not a byte count up to MAX_BODY is refused before
        # any read: rfile.read() of a negative one reads to EOF.
        length = (self.headers.get("Content-Length") or "0").strip()
        if not (length.isascii() and length.isdigit() and int(length) <= MAX_BODY):
            self.send_error(400, "bad Content-Length")
            return
        self._answer(self.rfile.read(int(length)))

    def _answer(self, body: bytes) -> None:
        device = self.server.device
        reply = None if device.muted else device.answer(self.command, self.path, body)
        if reply is None:
            self.close_connection = True
            return
        status, content_type, data = reply
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The binding polls each of a Thing's requests at once; past the listen
    # backlog (socketserver's 5) a connection is dropped and its client
    # retries only a second later, which the latency check would time.
    request_queue_size = 128
    device: Device


class Server:
    """One stand-in served on 127.0.0.1."""

    def __init__(self, device: Device, port: int = 0) -> None:
        self.device = device
        self.httpd = _Server(("127.0.0.1", port), _Handler)
        self.httpd.device = device
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
