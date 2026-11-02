"""Finite Alertmanager webhook to a loopback ntfy server, standalone in its container.

Only catalogued, canonical text is delivered. Raw alert labels/annotations,
service response bodies, URLs and credentials are never forwarded or logged.
"""
from __future__ import annotations
import argparse
import http.server
import ipaddress
import json
import threading
import time
import urllib.parse
import urllib.request

LIMIT = 131072


def loopback_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("ntfy must be an HTTP loopback origin")
    if not ipaddress.ip_address(parsed.hostname).is_loopback or not parsed.port:
        raise ValueError("ntfy must be an HTTP loopback origin")
    return value.rstrip("/")


def messages(value, catalog, heartbeat=False):
    if not isinstance(value, dict) or value.get("version") != "4" or not isinstance(value.get("alerts"), list) or not 1 <= len(value["alerts"]) <= 64:
        raise ValueError("invalid webhook")
    result = []
    for alert in value["alerts"]:
        if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
            raise ValueError("invalid alert")
        name, status = alert["labels"].get("alertname"), alert.get("status")
        if status not in ("firing", "resolved") or not isinstance(name, str) or name not in catalog:
            raise ValueError("uncatalogued alert")
        if (name == "ThermiteWatchdog") != heartbeat:
            raise ValueError("wrong notification route")
        title, detail = catalog[name]
        if not all(isinstance(s, str) and len(s) <= 500 and not any(ord(c) < 32 for c in s) for s in (title, detail)):
            raise ValueError("invalid catalog")
        message = {"title": ("Resolved: " if status == "resolved" else "") + title,
                   "message": detail, "priority": 1 if heartbeat or status == "resolved" else 3}
        if message not in result:
            result.append(message)
    return result


class Relay:
    def __init__(self, ntfy, catalog, now=time.time, publish=None):
        self.ntfy = loopback_url(ntfy)
        self.catalog, self.now = catalog, now
        self.started = now()
        self.heartbeat = self.success = 0
        self.failures = self.delivered = 0
        self.lock = threading.Lock()
        self.publish = publish or self.send

    def send(self, value):
        request = urllib.request.Request(self.ntfy + "/", data=json.dumps(value).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        # No ambient proxies or redirects; every destination is fixed loopback.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=5) as response:
            answer = json.loads(response.read(8192))
            if response.status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or not answer["id"]:
                raise ValueError("ntfy did not acknowledge")

    def accept(self, value, heartbeat=False):
        batch = messages(value, self.catalog, heartbeat)
        for message in batch:
            self.publish({"topic": "thermite-heartbeat" if heartbeat else "thermite-alerts", **message})
            with self.lock:
                self.success = self.now(); self.delivered += 1
                if heartbeat:
                    self.heartbeat = self.success if any(a["status"] == "firing" for a in value["alerts"]) else 0

    def metrics(self):
        with self.lock:
            values = {"thermite_notification_delivered_total": self.delivered,
                      "thermite_notification_errors_total": self.failures,
                      "thermite_notification_last_success_timestamp_seconds": self.success,
                      "thermite_notification_last_heartbeat_timestamp_seconds": self.heartbeat,
                      "thermite_notification_heartbeat_age_seconds": max(0, self.now() - (self.heartbeat or self.started))}
        return "".join(f"{name} {value}\n" for name, value in values.items())


def server(address, relay):
    if not ipaddress.ip_address(address[0]).is_loopback:
        raise ValueError("listener must be loopback")
    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(5)
        def log_message(self, *_):
            pass
        def reply(self, code, body=b"", kind="text/plain"):
            self.send_response(code); self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == "/metrics": self.reply(200, relay.metrics().encode())
            elif self.path == "/health": self.reply(200, b"ok\n")
            elif self.path == "/ready": self.reply(200 if relay.heartbeat and relay.now()-relay.heartbeat < 900 else 503)
            else: self.reply(404)
        def do_POST(self):
            if self.path not in ("/alerts", "/heartbeat"): return self.reply(404)
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json": return self.reply(400)
            try: length = int(self.headers.get("Content-Length", "0"))
            except ValueError: return self.reply(400)
            if not 0 < length <= LIMIT: return self.reply(413)
            try:
                value = json.loads(self.rfile.read(length)); messages(value, relay.catalog, self.path == "/heartbeat")
            except (ValueError, TypeError): return self.reply(400)
            try: relay.accept(value, self.path == "/heartbeat")
            except Exception:
                with relay.lock: relay.failures += 1
                return self.reply(503, b"local notification delivery unavailable\n")
            self.reply(200, b"accepted\n")
    result = http.server.ThreadingHTTPServer(address, Handler)
    result.daemon_threads = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:9481")
    parser.add_argument("--ntfy", required=True)
    parser.add_argument("--catalog", required=True)
    args = parser.parse_args()
    from pathlib import Path
    catalog = json.loads(Path(args.catalog).read_text())
    host, port = args.listen.rsplit(":", 1)
    httpd = server((host, int(port)), Relay(args.ntfy, catalog))
    try: httpd.serve_forever()
    finally: httpd.server_close()


if __name__ == "__main__":
    main()
