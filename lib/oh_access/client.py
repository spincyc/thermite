"""HTTPS-only login and per-origin personal credentials; no cookie jar."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import AccessError
from .files import json_private, private_directory, read_private

TOKEN = re.compile(r"thm_[A-Za-z0-9_-]{32,200}\Z")


def origin(value):
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        host = host.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9.:-]+", host) or ".." in host or host.endswith("."):
            raise ValueError()
        shown = "[" + host + "]" if ":" in host else host
        return "https://" + shown + (":" + str(port) if port not in (None, 443) else "")
    except (ValueError, UnicodeError):
        raise AccessError("Site URL must be an HTTPS origin without a path or credentials") from None


def credential_path(url, config_home=None):
    base = Path(config_home or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    directory = private_directory(base / "thermite")
    directory = private_directory(directory / "sites")
    return directory / (hashlib.sha256(origin(url).encode()).hexdigest() + ".json")


def ca_path(path):
    return path.with_suffix(".ca.json")


def load(url, config_home=None):
    canonical = origin(url)
    path = credential_path(canonical, config_home)
    try:
        value = json.loads(read_private(path, 4096))
        if not isinstance(value, dict) or set(value) != {"version", "origin", "token", "id", "expires_at"} or value["version"] != 1 or value["origin"] != canonical or not isinstance(value["token"], str) or not TOKEN.fullmatch(value["token"]) or not isinstance(value["id"], str):
            raise ValueError()
        expiry = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OSError):
        raise AccessError("No valid site credential; run thermite access login") from None
    return path, value


def saved_ca(path):
    if not ca_path(path).exists():
        return None
    try:
        value = json.loads(read_private(ca_path(path), 8192))
        if set(value) != {"version", "ca_file"} or value["version"] != 1:
            raise ValueError()
        result = str(Path(value["ca_file"]).expanduser().resolve(strict=True))
        ssl.create_default_context(cafile=result)
        return result
    except (ValueError, TypeError, KeyError, OSError, ssl.SSLError):
        raise AccessError("Saved site CA file is unavailable or invalid") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, path, *, method="GET", data=None, token=None, ca_file=None, timeout=10):
    canonical = origin(url)
    if path != "/api/v1/me" and path not in {"/api/v1/token-login", "/api/v1/logout"} and not re.fullmatch(r"/api/v1/rooms/[a-z][a-z0-9_]*/policy", path):
        raise AccessError("Unknown access API path")
    context = ssl.create_default_context(cafile=ca_file)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    headers = {"Accept": "application/json"}
    content = None
    if data is not None:
        content = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token  # lint: allow (scheme only; private credential file)
    message = Request(canonical + path, method=method, data=content, headers=headers)
    try:
        response = opener.open(message, timeout=timeout)
    except HTTPError as exc:
        exc.close()
        raise AccessError("Site request refused (HTTP " + str(exc.code) + ")") from None
    except (URLError, OSError, ssl.SSLError):
        raise AccessError("Could not securely reach the site") from None
    with response:
        raw = response.read(262145)
        if len(raw) > 262144 or response.headers.get_content_type() != "application/json":
            raise AccessError("Invalid site response")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise AccessError("Invalid site response") from None


def login(url, username, password, *, ca_file=None, config_home=None, http=request):
    canonical = origin(url)
    path = credential_path(canonical, config_home)
    for existing in (path, ca_path(path)):
        if existing.exists() or existing.is_symlink():
            read_private(existing, 8192)
    if ca_file:
        ca_file = str(Path(ca_file).expanduser().resolve(strict=True))
        ssl.create_default_context(cafile=ca_file)
    result = http(canonical, "/api/v1/token-login", method="POST",
                  data={"username": username, "password": password, "name": "thermite CLI", "days": 30}, ca_file=ca_file)
    try:
        value = {"version": 1, "origin": canonical, **{key: result[key] for key in ("token", "id", "expires_at")}}
        if not TOKEN.fullmatch(value["token"]) or not isinstance(value["id"], str):
            raise ValueError()
        expiry = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise AccessError("Invalid login response") from None
    json_private(path, value)
    if ca_file:
        json_private(ca_path(path), {"version": 1, "ca_file": ca_file})
    else:
        ca_path(path).unlink(missing_ok=True)
    return {"origin": canonical, "credential_file": str(path), "expires_at": value["expires_at"]}


def logout(url, *, config_home=None, http=request):
    path, value = load(url, config_home)
    before = read_private(path, 4096)
    identity = path.stat()
    http(value["origin"], "/api/v1/logout", method="POST", data={}, token=value["token"], ca_file=saved_ca(path))
    # Another login could have replaced the file while the request was in flight.
    current = path.lstat()
    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and read_private(path, 4096) == before:
        path.unlink()
        ca_path(path).unlink(missing_ok=True)
    return {"origin": value["origin"], "logged_out": True}


def status(url, *, config_home=None, http=request, rooms=()):
    path, value = load(url, config_home)
    arguments = {"token": value["token"], "ca_file": saved_ca(path)}
    me = http(value["origin"], "/api/v1/me", **arguments)
    allowed = {"id", "username", "role", "rooms", "permissions", "can_edit", "site", "active"}
    result = {"origin": value["origin"], "user": {key: val for key, val in me.items() if key in allowed}, "policies": {}}
    for room in rooms:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", room):
            raise AccessError("Invalid room ID")
        result["policies"][room] = http(value["origin"], "/api/v1/rooms/" + room + "/policy", **arguments)
    return result
