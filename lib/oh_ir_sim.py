"""Loopback-only iTach IR protocol stand-in; never transmits infrared.

Native Global Caché 5.2.3 connects to fixed TCP 4998 and considers a socket
connection ONLINE. Its periodic getversion query and completeir transport
acknowledgement do not report disc-player power or playback state.
"""
from __future__ import annotations

import socket
import socketserver
import threading

PORT = 4998


class Emitter:
    def __init__(self, commands: dict[str, str], connector: int):
        self.commands = commands
        self.connector = connector
        self.requests: list[str] = []
        self.packets: list[str] = []
        self.unknown: list[str] = []
        self.lock = threading.Lock()

    def answer(self, line: str) -> str:
        with self.lock:
            self.requests.append(line)
            if line == "getversion":
                return "version,710-1001-05"
            parts = line.split(",", 3)
            if (len(parts) == 4 and parts[0] == "sendir" and parts[1] == f"1:{self.connector}"
                    and parts[2].isdigit() and 0 <= int(parts[2]) <= 65535 and parts[3] in self.commands.values()):
                self.packets.append(line)
                return f"completeir,{parts[1]},{parts[2]}"
            self.unknown.append(line)
            return "ERR_001"

    def sent(self) -> list[str]:
        with self.lock:
            return list(self.packets)

    def reached(self, button: str) -> bool:
        sent = self.sent()
        return bool(sent and sent[-1].split(",", 3)[3] == self.commands.get(button))


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        # Native requests are CR terminated, not CRLF. Bound the line and
        # connection; server shutdown closes active clients explicitly.
        self.connection.settimeout(2)
        self.server.clients.add(self.connection)
        pending = bytearray()
        try:
            while True:
                try:
                    value = self.connection.recv(1)
                except TimeoutError:
                    if self.server.stopping.is_set(): return
                    continue
                if not value: return
                if value == b"\r":
                    line = pending.decode("ascii", "replace"); pending.clear()
                    self.connection.sendall(self.server.emitter.answer(line).encode("ascii") + b"\r\n")
                elif value != b"\n":
                    pending.extend(value)
                    if len(pending) > 6200: return
        except OSError:
            return
        finally:
            self.server.clients.discard(self.connection)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server:
    """Fixed native port, distinct loopback addresses for multiple emitters."""
    def __init__(self, emitter: Emitter):
        self.tcp = None
        for suffix in range(2, 255):
            try:
                self.tcp = _Server((f"127.0.0.{suffix}", PORT), _Handler)
                break
            except OSError:
                continue
        if self.tcp is None:
            raise OSError("no free loopback iTach stand-in address")
        self.tcp.emitter = emitter
        self.tcp.clients = set()
        self.tcp.stopping = threading.Event()
        self.thread = threading.Thread(target=self.tcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def address(self) -> str:
        return f"{self.tcp.server_address[0]}:{PORT}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.tcp.stopping.set()
        for client in list(self.tcp.clients):
            try: client.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            client.close()
        if self.thread.is_alive():
            self.tcp.shutdown()
            self.thread.join(timeout=3)
        self.tcp.server_close()

    def __enter__(self): return self.start()
    def __exit__(self, *args): self.stop()
