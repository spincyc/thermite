"""Bounded onboarding of a private, Thermite-owned native runtime identity.

The caller locks an owner-private journal and supplies durable load/save/publish
callbacks. This module owns no files and emits no output. The pinned openHAB
5.2.3 authorization/token pages are protocol adapters, never operator UI.
"""
from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
import http.client
import io
import json
import math
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

MAX_BODY = 128 * 1024
MAX_HEADERS = 32 * 1024
REQUEST_SECONDS = 5
_MESSAGES = {
    "origin": "Native runtime onboarding requires a literal loopback HTTP origin.",
    "storage": "Native runtime onboarding credentials could not be stored safely.",
    "journal": "Native runtime onboarding credentials require owner repair.",
    "retired": "Native runtime onboarding credentials were retired by an explicit credential import.",
    "identity": "Native runtime identity could not be verified against its managed credentials.",
    "existing_users": "Native runtime already has accounts; managed onboarding credentials are required.",
    "credentials": "Native runtime rejected its managed onboarding credentials; no credentials were replaced.",
    "collision": "Native runtime already has the reserved token name; no token was replaced.",
    "unavailable": "Native runtime onboarding is not ready.",
    "transport": "Native runtime onboarding stopped after an unconfirmed request.",
    "deadline": "Native runtime onboarding exceeded its startup allowance.",
    "oversize": "Native runtime onboarding received an oversized response.",
    "protocol": "Native runtime onboarding received an unexpected protocol response.",
}


class NativeAuthError(RuntimeError):
    """Only fixed diagnostics escape; response bodies and credentials never do."""

    def __init__(self, code, *, retryable=False):
        self.code = code if isinstance(code, str) and code in _MESSAGES else "protocol"
        self.retryable = bool(retryable)
        super().__init__(_MESSAGES[self.code])


def _origin(url):
    if not isinstance(url, str) or not (match := re.fullmatch(r"http://(127\.0\.0\.1|\[::1\])(?::([1-9][0-9]{0,4}))?/?", url)):
        raise NativeAuthError("origin")
    port = int(match[2] or 80)
    if port > 65535:
        raise NativeAuthError("origin")
    return f"http://{match[1]}:{port}"


class _Reader(io.RawIOBase):
    def __init__(self, stream, sock, remaining):
        self.stream, self.sock, self.remaining = stream, sock, remaining
        self.received = 0

    def readable(self):
        return True

    def readinto(self, target):
        self.sock.settimeout(self.remaining())
        count = self.stream.readinto(memoryview(target)[:MAX_BODY + MAX_HEADERS + 1 - self.received])
        self.received += count or 0
        if self.received > MAX_BODY + MAX_HEADERS:
            raise NativeAuthError("oversize")
        return count

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class _Socket:
    def __init__(self, sock, remaining):
        self.sock, self.remaining = sock, remaining

    def makefile(self, mode):
        return io.BufferedReader(_Reader(self.sock.makefile(mode, buffering=0), self.sock, self.remaining))


class _Client:
    def __init__(self, origin, deadline, clock):
        self.origin, self.deadline, self.clock = origin, deadline, clock

    def remaining(self):
        try:
            left = self.deadline - self.clock()
        except Exception:
            raise NativeAuthError("deadline") from None
        if not isinstance(left, (int, float)) or not math.isfinite(left) or left <= 0:
            raise NativeAuthError("deadline")
        return min(REQUEST_SECONDS, left)

    def request(self, method, path, *, form=None, bearer=None):
        target = urlsplit(self.origin)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=self.remaining())
        response_type = connection.response_class
        connection.response_class = lambda sock, *args, **kwargs: response_type(_Socket(sock, self.remaining), *args, **kwargs)
        headers = {"Accept": "application/json, text/html, text/plain", "Connection": "close"}
        body = None
        if form is not None:
            body = urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        try:
            connection.timeout = self.remaining()
            connection.request(method, path, body=body, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(self.remaining())
            with connection.getresponse() as response:
                fields = response.headers
                if sum(len(key) + len(value) + 4 for key, value in fields.raw_items()) > MAX_HEADERS:
                    raise NativeAuthError("oversize")
                for key in ("Content-Length", "Transfer-Encoding", "Content-Type", "Location", "Content-Encoding"):
                    if len(fields.get_all(key, [])) > 1:
                        raise NativeAuthError("protocol")
                if fields.get("Transfer-Encoding") and fields.get("Content-Length"):
                    raise NativeAuthError("protocol")
                if fields.get("Content-Length") is not None and not re.fullmatch(r"[0-9]+", fields["Content-Length"]):
                    raise NativeAuthError("protocol")
                if fields.get("Transfer-Encoding", "chunked").lower() != "chunked" or fields.get("Content-Encoding", "identity").lower() != "identity":
                    raise NativeAuthError("protocol")
                if response.length is not None and response.length > MAX_BODY:
                    raise NativeAuthError("oversize")
                payload = response.read(MAX_BODY + 1)
                if len(payload) > MAX_BODY:
                    raise NativeAuthError("oversize")
                self.remaining()
                return response.status, fields, payload
        except NativeAuthError:
            raise
        except (OSError, http.client.HTTPException):
            self.remaining()
            raise NativeAuthError("unavailable" if method == "GET" else "transport", retryable=method == "GET") from None
        finally:
            connection.close()


def _expect(response, status, content_type=None, *, read=False):
    code, fields, payload = response
    if code != status:
        if read and code in (404, 429, 500, 502, 503, 504):
            raise NativeAuthError("unavailable", retryable=True)
        raise NativeAuthError("credentials" if code in (401, 403) else "protocol")
    if content_type and fields.get_content_type() != content_type:
        raise NativeAuthError("protocol")
    return payload


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _json(response, *, read=True):
    try:
        return json.loads(_expect(response, 200, "application/json", read=read).decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise NativeAuthError("protocol") from None


class _Page(HTMLParser):
    def __init__(self, payload):
        super().__init__(convert_charrefs=True)
        self.forms, self.inputs, self.codes = [], {}, []
        self.inside, self.code = False, None
        try:
            self.feed(payload.decode("utf-8"))
            self.close()
        except (ValueError, UnicodeError, RecursionError):
            raise NativeAuthError("protocol") from None
        if self.inside or self.code is not None or len(self.forms) != 1:
            raise NativeAuthError("protocol")

    def handle_starttag(self, tag, attrs):
        if self.code is not None:
            raise NativeAuthError("protocol")
        values = dict(attrs)
        if len(values) != len(attrs) or tag == "base":
            raise NativeAuthError("protocol")
        if tag == "form":
            if self.inside:
                raise NativeAuthError("protocol")
            self.forms.append(values)
            self.inside = True
        elif tag == "input" and self.inside and (name := values.get("name")):
            if name in self.inputs or "formaction" in values or "form" in values:
                raise NativeAuthError("protocol")
            kind, value = values.get("type", "text"), values.get("value", "")
            if not isinstance(kind, str) or not isinstance(value, str):
                raise NativeAuthError("protocol")
            self.inputs[name] = (kind.lower(), value)
        elif tag == "code":
            self.code = ""

    def handle_endtag(self, tag):
        if tag == "form":
            if not self.inside:
                raise NativeAuthError("protocol")
            self.inside = False
        if tag == "code":
            if self.code is None:
                raise NativeAuthError("protocol")
            self.codes.append(self.code)
            self.code = None

    def handle_data(self, value):
        if self.code is not None:
            self.code += value

    def form(self, action, *, hidden=False):
        values = self.forms[0]
        method = values.get("method", "")
        if (values.get("action") != action or not isinstance(method, str) or method.upper() != "POST"
                or values.get("class") != ("hide" if hidden else "show") or values.get("target")
                or values.get("enctype", "application/x-www-form-urlencoded") != "application/x-www-form-urlencoded"):
            raise NativeAuthError("protocol")

    def fields(self, action, expected):
        self.form(action)
        if set(self.inputs) != set(expected):
            raise NativeAuthError("protocol")
        for name, kind in expected.items():
            if self.inputs[name][0] != kind:
                raise NativeAuthError("protocol")
        csrf = self.inputs["csrf_token"][1]
        if not re.fullmatch(r"[0-9a-f]{32}", csrf):
            raise NativeAuthError("protocol")
        return csrf


_PASSWORD_INPUT_TYPE = "password"
_FORM_FIELDS = {"csrf_token": "hidden", "username": "text", "password": _PASSWORD_INPUT_TYPE, "new_password": "hidden",
                "password_repeat": "hidden", "token_name": "text", "token_scope": "text"}


def _auth_fields(page, params):
    signup = page.inputs.get("password_repeat", (None,))[0] == _PASSWORD_INPUT_TYPE
    expected = _FORM_FIELDS | {"token_name": "hidden", "token_scope": "hidden",
                               "password_repeat": _PASSWORD_INPUT_TYPE if signup else "hidden"}
    expected.update({key: "hidden" for key in params})
    csrf = page.fields("/auth", expected)
    if any(page.inputs[key][1] != value for key, value in params.items()):
        raise NativeAuthError("protocol")
    return csrf, signup


def _auth_form(client):
    verifier, state = secrets.token_urlsafe(48), secrets.token_hex(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    params = {"response_type": "code", "client_id": client.origin, "redirect_uri": client.origin,
              "scope": "admin", "state": state, "code_challenge_method": "S256", "code_challenge": challenge}
    page = _Page(_expect(client.request("GET", "/auth?" + urlencode(params)), 200, "text/html", read=True))
    csrf, signup = _auth_fields(page, params)
    return params | {"csrf_token": csrf}, verifier, signup


def _authenticate(client, journal, form, verifier, signup):
    submitted = form | {"username": journal["username"], "password": journal["password"]}
    if signup:
        submitted["password_repeat"] = journal["password"]
    response = client.request("POST", "/auth", form=submitted)
    if response[0] == 200:
        _auth_fields(_Page(_expect(response, 200, "text/html")), {key: value for key, value in form.items() if key != "csrf_token"})
        raise NativeAuthError("credentials")
    _expect(response, 302)
    try:
        redirect = urlsplit(response[1].get("Location", ""))
        if _origin(redirect._replace(path="", query="", fragment="").geturl()) != client.origin or redirect.path not in ("", "/") or redirect.fragment:
            raise ValueError()
        query = parse_qs(redirect.query, strict_parsing=True, max_num_fields=3)
        if set(query) != {"code", "state"} or any(len(value) != 1 for value in query.values()) or query["state"][0] != form["state"] or not re.fullmatch(r"[0-9a-f]{32}", query["code"][0]):
            raise ValueError()
    except (ValueError, NativeAuthError):
        raise NativeAuthError("protocol") from None
    result = _json(client.request("POST", "/rest/auth/token", form={
        "grant_type": "authorization_code", "code": query["code"][0], "client_id": client.origin,
        "redirect_uri": client.origin, "code_verifier": verifier}), read=False)
    if not isinstance(result, dict) or not isinstance(user := result.get("user"), dict):
        raise NativeAuthError("protocol")
    access, refresh = result.get("access_token"), result.get("refresh_token")
    if (user.get("name") != journal["username"] or not isinstance(user.get("roles"), list)
            or "administrator" not in user["roles"] or result.get("scope") != "admin"
            or str(result.get("token_type")).lower() != "bearer" or not isinstance(access, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", access)
            or len(access) > 8192 or not isinstance(refresh, str) or not re.fullmatch(r"[0-9a-f]{32}", refresh)):
        raise NativeAuthError("credentials")
    return access, refresh


def _metadata(client, bearer):
    values = _json(client.request("GET", "/rest/auth/apitokens", bearer=bearer))
    if not isinstance(values, list) or len(values) > 512:
        raise NativeAuthError("protocol")
    found = {}
    for value in values:
        if (not isinstance(value, dict) or not isinstance(name := value.get("name"), str) or len(name) > 256
                or name in found or not isinstance(value.get("scope", ""), str)):
            raise NativeAuthError("protocol")
        found[name] = value.get("scope", "")
    return found


def _token_valid(client, journal):
    if _metadata(client, journal["token"]).get(journal["token_name"]) != "":
        raise NativeAuthError("credentials")


def _store(callback, value):
    try:
        callback(dict(value))
    except Exception:
        raise NativeAuthError("storage") from None


def _publish(client, journal, save, publish):
    _token_valid(client, journal)
    client.remaining()
    try:
        publish(journal["token"])
    except Exception:
        raise NativeAuthError("storage") from None
    client.remaining()
    journal["phase"] = "published"
    _store(save, journal)
    client.remaining()
    return journal["token"]


def _journal(data, origin):
    fields = {"version", "origin", "uuid", "username", "password", "token_name", "phase", "token"}
    if (not isinstance(data, dict) or set(data) != fields or type(data.get("version")) is not int or data["version"] != 1
            or data["origin"] != origin or data["phase"] not in ("prepared", "issuing", "recorded", "published", "retired")):
        raise NativeAuthError("journal")
    for key, pattern in (("username", r"thermite[0-9a-f]{32}"), ("password", r"[A-Za-z0-9_-]{64,128}"),
                         ("token_name", r"Thermite[0-9a-f]{32}"), ("uuid", r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")):
        if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
            raise NativeAuthError("journal")
    if data["phase"] in ("prepared", "issuing"):
        valid = data["token"] is None
    elif data["phase"] == "retired" and data["token"] is None:
        valid = True
    else:
        valid = isinstance(data["token"], str) and re.fullmatch(r"oh\." + data["token_name"] + r"\.[A-Za-z0-9]{64,128}", data["token"])
    if not valid:
        raise NativeAuthError("journal")
    return dict(data)


def provision(url, *, load, save, publish, deadline, clock=time.monotonic):
    """Return a validated API token only after durable local publication.

    Only an unrecorded issuing token can be revoked/reissued after an ambiguous
    response. Recorded credentials are revalidated and republished unchanged.
    Generated PKCE sessions are retired best-effort within the same deadline;
    abrupt process death can leave a session whose secret was not persisted.
    """
    origin = _origin(url)
    client = _Client(origin, deadline, clock)
    try:
        data = load()
    except Exception:
        raise NativeAuthError("storage") from None
    journal = _journal(data, origin) if data is not None else None
    if journal is not None and journal["phase"] == "retired":
        raise NativeAuthError("retired")
    response = client.request("GET", "/rest/uuid")
    if response[0] == 401 and journal is not None and journal["token"] is not None:
        response = client.request("GET", "/rest/uuid", bearer=journal["token"])
    try:
        native_id = str(uuid.UUID(_expect(response, 200, "text/plain", read=True).decode("ascii").strip()))
    except (ValueError, UnicodeError):
        raise NativeAuthError("identity") from None
    if journal is not None and journal["uuid"] != native_id:
        raise NativeAuthError("identity")
    if journal is not None and journal["token"] is not None:
        return _publish(client, journal, save, publish)
    form, verifier, signup = _auth_form(client)
    if journal is None:
        if not signup:
            raise NativeAuthError("existing_users")
        journal = {"version": 1, "origin": origin, "uuid": native_id, "username": "thermite" + secrets.token_hex(16),
                   "password": secrets.token_urlsafe(64), "token_name": "Thermite" + secrets.token_hex(16),
                   "phase": "prepared", "token": None}
        _store(save, journal)
    elif signup and journal["phase"] != "prepared":
        raise NativeAuthError("identity")
    access, refresh = _authenticate(client, journal, form, verifier, signup)
    try:
        tokens = _metadata(client, access)
        if journal["token_name"] in tokens:
            if journal["phase"] != "issuing":
                raise NativeAuthError("collision")
            result = client.request("DELETE", "/rest/auth/apitokens/" + journal["token_name"], bearer=access)
            if result[0] not in (200, 404):
                raise NativeAuthError("protocol")
        journal["phase"] = "issuing"
        _store(save, journal)
        page = _Page(_expect(client.request("GET", "/createApiToken"), 200, "text/html", read=True))
        csrf = page.fields("/createApiToken", _FORM_FIELDS)
        result = client.request("POST", "/createApiToken", form={"csrf_token": csrf, "username": journal["username"],
                                "password": journal["password"], "token_name": journal["token_name"], "token_scope": ""})
        page = _Page(_expect(result, 200, "text/html"))
        page.form("/createApiToken", hidden=True)
        if len(page.codes) != 1 or "csrf_token" in page.inputs or not re.fullmatch(r"oh\." + journal["token_name"] + r"\.[A-Za-z0-9]{64,128}", page.codes[0]):
            raise NativeAuthError("protocol")
        journal.update(phase="recorded", token=page.codes[0])
        _store(save, journal)
        return _publish(client, journal, save, publish)
    finally:
        try:
            client.request("POST", "/rest/auth/logout", form={"refresh_token": refresh}, bearer=access)
        except NativeAuthError:
            pass
