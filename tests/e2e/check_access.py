"""Exercise the real authenticated gateway inside a disposable dummy-house guest.

Credentials stay in memory. No operator login files, service configuration or
firewall rules are written. The dummy room's policy is unlocked on completion.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener

ROOM = "example_west"
SOURCE = "example_computer"
FIELD = "example_matrix_output1_input"
LIMITATION = "passive_endpoint:example_tv"
MAXIMUM = 2 * 1024 * 1024


class CheckFailure(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def origin(value, *, tls):
    parsed = urlsplit(value)
    require(parsed.scheme == ("https" if tls else "http") and parsed.hostname is not None
            and not parsed.username and not parsed.password and parsed.path in ("", "/")
            and not parsed.query and not parsed.fragment, "an explicit loopback origin is required")
    host = parsed.hostname
    if tls:
        require(host in {"localhost", "127.0.0.1", "::1"}, "gateway must be localhost or a loopback literal")
    else:
        try:
            require(ipaddress.ip_address(host).is_loopback, "matrix must use a loopback literal")
        except ValueError:
            raise CheckFailure("matrix must use a loopback literal") from None
    # Catch an unexpected localhost resolver configuration before any request.
    require(all(ipaddress.ip_address(entry[4][0]).is_loopback
                for entry in socket.getaddrinfo(host, parsed.port or (443 if tls else 80), type=socket.SOCK_STREAM)),
            "origin resolved outside loopback")
    shown = "[" + host + "]" if ":" in host else host
    return parsed.scheme + "://" + shown + (":" + str(parsed.port) if parsed.port else "")


def password_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and 0 < info.st_size <= 4096,
                "password file must be owned, regular, private and bounded")
        value = os.read(descriptor, 4097).decode().rstrip("\r\n")
        require(12 <= len(value) <= 1024 and "\n" not in value and "\r" not in value,
                "password file contains an invalid bootstrap password")
        return value
    finally:
        os.close(descriptor)


class Gateway:
    def __init__(self, url, certificate, timeout):
        self.base = url
        self.timeout = min(timeout, 30)
        self.context = ssl.create_default_context(cafile=str(certificate))
        self.opener = self.open()
        self.token = None

    def open(self, *handlers):
        return build_opener(ProxyHandler({}), HTTPSHandler(context=self.context), NoRedirect(), *handlers)

    def request(self, path, *, method="GET", data=None, authenticated=True, opener=None, headers=None):
        supplied = {"Accept": "application/json", **(headers or {})}
        if authenticated and self.token:
            supplied["Authorization"] = "Bearer " + self.token
        content = None
        if data is not None:
            supplied["Content-Type"] = "application/json"
            content = json.dumps(data, separators=(",", ":")).encode()
        request = Request(self.base + path, method=method, data=content, headers=supplied)
        try:
            response = (opener or self.opener).open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(MAXIMUM + 1)
            require(len(raw) <= MAXIMUM, "gateway response exceeded the acceptance bound")
            return response.status, response.headers, raw

    def json(self, path, *, status=200, **values):
        actual, _, raw = self.request(path, **values)
        require(actual == status, "unexpected HTTP status for " + path + ": " + str(actual))
        result = json.loads(raw)
        require(isinstance(result, dict), "gateway returned invalid JSON metadata")
        return result

    def state(self, item):
        status, _, raw = self.request("/rest/items/" + item + "/state")
        require(status == 200, "authenticated native Item read failed")
        return raw.decode().strip()


def poll(timeout, label, check):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if answer := check():
                return answer
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise CheckFailure("timed out waiting for " + label)
        time.sleep(0.5)


def policy(gateway):
    return gateway.json("/api/v1/rooms/" + ROOM + "/policy")


def save_policy(gateway, config):
    current = policy(gateway)
    return gateway.json("/api/v1/rooms/" + ROOM + "/policy", method="PUT",
                        data={"revision": current["revision"], "config": config})


def unlock(gateway):
    save_policy(gateway, {"version": 1, "enabled": False, "daily_off": [], "locks": []})
    require(policy(gateway)["effective"]["locked"] is False, "dummy room remained locked")


def browser_session(gateway, username, secret):
    jar = http.cookiejar.CookieJar()
    browser = gateway.open(HTTPCookieProcessor(jar))
    csrf = gateway.json("/api/v1/session", authenticated=False, opener=browser)["csrf"]
    headers = {"X-CSRFToken": csrf, "Origin": gateway.base,
               "Referer": gateway.base + "/static/remote/access.html"}
    answer = gateway.json("/api/v1/session", method="POST", data={"username": username, "password": secret},
                          authenticated=False, opener=browser, headers=headers)
    try:
        require(answer["user"]["username"] == username and answer["user"]["can_edit"], "browser admin login failed")
        sessions = [cookie for cookie in jar if cookie.name.endswith("_session")]
        require(len(sessions) == 1 and sessions[0].secure and sessions[0].has_nonstandard_attr("HttpOnly")
                and sessions[0].get_nonstandard_attr("SameSite") == "Lax", "browser session cookie protections failed")
        for path in ("/static/remote/home.html", "/static/remote/house.js"):
            require(gateway.request(path, authenticated=False, opener=browser)[0] == 200,
                    "browser session could not read private assets")
    finally:
        headers["X-CSRFToken"] = answer["csrf"]
        gateway.json("/api/v1/logout", method="POST", data={}, authenticated=False, opener=browser, headers=headers)


def matrix_request(base, timeout, value):
    request = Request(base + "/cgi-bin/instr", data=json.dumps(value).encode(),
                      headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=min(timeout, 30)) as response:
        raw = response.read(MAXIMUM + 1)
        require(response.status == 200 and len(raw) <= MAXIMUM, "dummy matrix HTTP request failed")
        answer = json.loads(raw)
        require(isinstance(answer, dict), "dummy matrix returned invalid metadata")
        return answer


def run(args):
    gateway = Gateway(origin(args.url, tls=True), args.ca_file, args.timeout)
    matrix = origin(args.matrix_url, tls=False)
    secret = password_file(args.password_file)
    proof = {"version": 1, "tls_verified": True, "room": ROOM, "credential_file_written": False}
    touched_policy = False
    failure = None
    try:
        matrix_status = matrix_request(matrix, args.timeout, {"comhead": "get video status"})
        require(matrix_status.get("comhead") == "get video status"
                and isinstance(matrix_status.get("allsource"), list) and len(matrix_status["allsource"]) == 9,
                "loopback matrix is not the BK-808 dummy fixture")
        denied = ("/api/v1/me", "/api/v1/rooms/" + ROOM + "/policy", "/static/remote/house.js",
                  "/static/remote/activity.json")
        for path in denied:
            require(gateway.request(path, authenticated=False)[0] == 401, "anonymous private resource was not denied")
        status, headers, _ = gateway.request("/static/remote/home.html", authenticated=False)
        require(status == 302 and headers.get("Location") == "/static/remote/access.html?next=home.html",
                "anonymous private page did not redirect to login")
        for path in ("/static/remote/access.html", "/static/remote/access.js"):
            require(gateway.request(path, authenticated=False)[0] == 200, "public login resource was unavailable")
        proof["anonymous_denied"] = True
        issued = gateway.json("/api/v1/token-login", status=201, method="POST", authenticated=False,
                              data={"username": args.admin, "password": secret, "name": "dummy guest acceptance", "days": 1})
        require(isinstance(issued.get("token"), str) and re.fullmatch(r"thm_[A-Za-z0-9_-]{32,200}", issued["token"]),
                "personal token was invalid")
        gateway.token = issued["token"]
        me = gateway.json("/api/v1/me")
        require(me["username"] == args.admin and me["can_edit"] and "room.control" in me["permissions"],
                "personal token did not identify an admin")
        current = policy(gateway)
        require(current["room"] == ROOM and current["can_edit"], "dummy room policy was unavailable to admin")
        for name in ("home.html", "house.js", "activity.json"):
            status, _, raw = gateway.request("/static/remote/" + name)
            require(status == 200, "authenticated private asset was unavailable")
            if name == "house.js":
                require(all(value.encode() in raw for value in (ROOM, SOURCE, "example_matrix")),
                        "gateway does not serve the routed dummy fixture")
        proof["admin_token_and_assets"] = True
        browser_session(gateway, args.admin, secret)
        proof["browser_session"] = True
        touched_policy = True
        unlock(gateway)
        for source, number in (("off", 8), (SOURCE, 1)):
            accepted = gateway.json("/api/v1/rooms/" + ROOM + "/commands", status=202,
                                    method="POST", data={"action": "source", "value": source})
            require(accepted.get("accepted") is True, "dummy room source command was refused")
            poll(args.timeout, "native route " + source,
                 lambda: float(gateway.state(FIELD)) == number and gateway.state("loc_" + ROOM + "_source") == source)
        proof["commands_and_native_readback"] = True
        save_policy(gateway, {"version": 1, "enabled": True, "daily_off": [], "locks": [
            {"id": "e2e-hold", "start": "00:00", "end": "00:00", "days": list(range(7)),
             "desired": "off", "unlock": "leave"}]})
        poll(args.timeout, "worker initial Off enforcement", lambda: float(gateway.state(FIELD)) == 8)
        denied = gateway.json("/api/v1/rooms/" + ROOM + "/commands", status=423, method="POST",
                              data={"action": "source", "value": SOURCE})
        require(denied.get("error") == "room_locked", "active policy did not deny room control")
        changed = datetime.now(timezone.utc)
        drift = matrix_request(matrix, args.timeout, {"comhead": "video switch", "source": [1, 1]})
        require(drift.get("result") == 1, "dummy matrix did not accept injected drift")

        def repaired():
            raw = matrix_request(matrix, args.timeout, {"comhead": "get video status"})
            if raw.get("allsource", [None])[0] != 8 or float(gateway.state(FIELD)) != 8:
                return False
            row = policy(gateway)
            state = row.get("enforcement", {})
            updated = state.get("updated_at")
            if not updated or datetime.fromisoformat(updated.replace("Z", "+00:00")) <= changed:
                return False
            return state if (row["effective"]["locked"] and state.get("state") == "degraded"
                             and FIELD in state.get("matched", []) and not state.get("mismatched")
                             and LIMITATION in state.get("limitations", [])
                             and state.get("scope") == "configured_native_controls") else False

        state = poll(args.timeout, "worker matrix drift repair with honest passive limitation", repaired)
        proof["worker_drift_repaired"] = True
        proof["enforcement"] = {"state": state["state"], "matched": state["matched"],
                                "limitations": state["limitations"], "scope": state["scope"]}
    except BaseException as error:
        failure = error
    finally:
        if gateway.token:
            try:
                if touched_policy:
                    unlock(gateway)
                    proof["policy_cleared"] = True
            except Exception:
                failure = failure or CheckFailure("could not clear the dummy room policy")
            try:
                gateway.json("/api/v1/logout", method="POST", data={})
                require(gateway.request("/api/v1/me")[0] == 401, "acceptance token was not revoked")
                proof["token_revoked"] = True
            except Exception:
                failure = failure or CheckFailure("could not revoke the acceptance token")
            gateway.token = None
    if failure:
        raise failure
    proof["passed"] = True
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--admin", default="admin")
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--matrix-url", required=True, help="explicit loopback HTTP origin of the dummy BK-808")
    parser.add_argument("--timeout", type=float, default=15, help="seconds per bounded convergence check; use 180 for TCG")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be greater than zero and at most 3600 seconds")
    try:
        print(json.dumps(run(args), sort_keys=True, separators=(",", ":")))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"passed": False, "error": "interrupted"}))
        return 130
    except Exception as error:
        message = str(error) if isinstance(error, CheckFailure) else "gateway acceptance failed; check guest services, TLS and fixture inputs"
        print(json.dumps({"passed": False, "error": message}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
