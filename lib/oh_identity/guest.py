"""Requests through the plain-HTTP Guest listener (oh_access.guest).

The listener marks its requests in the ASGI scope; no header can. Such a
request names one of the listener's own Host values, carries no credential,
uses only ROUTES, and for a control comes from one of the listener's origins
as JSON instead of carrying a CSRF cookie. It is therefore always a Guest,
every room check still applies, and no response sets a cookie.
"""

import json
import re

from django.conf import settings
from django.middleware.security import SecurityMiddleware
from django.urls import Resolver404, resolve

from oh_access.guest import SCOPE

from .auth import failure

READ, GET, POST = ("GET", "HEAD"), ("GET",), ("POST",)
#: The Home and Remotes pages and every file they load. Account, Security,
#: camera, pairing, site and administration pages stay on the HTTPS site.
ASSETS = frozenset({
    "home.html", "index.html", "access-config.js", "access.js", "navigation.js", "appearance.js", "shell.js",
    "house.js", "remotes.js", "activity.js", "activity.json", "diagnostics.js", "diagnostics.json", "backend.js",
    "session.js", "log.js", "remote.js", "home.js", "theme.css", "shell.css", "remote.css", "home.css",
    "manifest.webmanifest", "icon.svg", "mark.svg", "mark-light.svg", "apple-touch-icon.png"})
#: The routes, as urls.py writes them, a guest-listener request may use, and
#: their methods. Anything else is not found. Opening another read or control
#: to guests is one entry here.
ROUTES = {
    # Home and Remotes, their aliases, and the files above.
    "": READ, "overview": READ, "overview/": READ, "remotes": READ, "remotes/": READ,
    "static/remote/": READ, "static/remote/<str:name>": READ,
    # Who the client is, whether accounts are on, and what rooms report.
    "api/v1/me": GET, "api/v1/security": GET,
    "api/v1/events/remote": GET, "api/v1/events/home": GET, "rest/items/<slug:item>/state": GET,
    "api/v1/locks": GET, "api/v1/rooms/<room:room>/policy": GET,
    # Controls, with every room check (locks included) of the HTTPS site.
    "api/v1/rooms/<room:room>/commands": POST, "api/v1/all-off": POST,
    # What the page could not see the gateway answer (oh_identity.telemetry).
    "api/v1/telemetry": POST,
}


def listener(request):
    """The listener this request came through (oh_access.guest.Listener), or
    None for the HTTPS listener."""
    scope = getattr(request, "scope", None)
    return scope.get(SCOPE) if isinstance(scope, dict) else None


def access_config(request):
    """The page's access mode script. On the guest listener the client is a
    Guest only, and its sign-in link is the HTTPS site's Account page."""
    guest = listener(request)
    value = {"version": 1, "mode": "guest", "secure": guest.secure} if guest else {"version": 1, "mode": "authenticated"}
    return "globalThis.THERMITE_ACCESS_CONFIG = Object.freeze(" + json.dumps(value, separators=(",", ":")) + ");\n"


def refusal(request, guest):
    """The problem response for a request this listener does not admit, or None."""
    if request.META.get("HTTP_HOST", "").lower() not in guest.hosts:
        # DNS rebinding: a page elsewhere can reach this port, never as this Host.
        return failure("invalid_host", 400)
    # Split as browsers join cookies, and as servers join repeated Cookie headers.
    cookies = re.split("[;,]", request.META.get("HTTP_COOKIE", ""))
    if request.META.get("HTTP_AUTHORIZATION") or any(name.strip().startswith(settings.COOKIE_PREFIX) for name in cookies):
        # A credential sent over plain HTTP is never used, even when valid.
        return failure("secure_connection_required")
    try:
        match = resolve(request.path_info)
    except Resolver404:
        match = None
    methods = ROUTES.get(match.route) if match else None
    if methods is None or match.kwargs.get("name") not in (None, *ASSETS):
        return failure("not_found", 404)
    if request.method not in methods:
        return failure("method_not_allowed", 405, allow=methods)
    if request.method == "POST":
        # In place of the CSRF cookie: browsers set Origin on every POST, and a
        # cross-site page can send JSON only after a preflight this site never answers.
        if request.headers.get("Origin") not in guest.origins:
            return failure("invalid_origin")
        if request.content_type != "application/json":
            return failure("json_required", 415)
    return None


class GuestListener:
    """Admit guest-listener requests (see refusal) as a cookieless Guest."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        guest = listener(request)
        if guest is None:
            return self.get_response(request)
        response = refusal(request, guest)
        if response is None:
            # Another service's cookies on this host are never read here.
            request.META.pop("HTTP_COOKIE", None)
            request.COOKIES = {}
            response = self.get_response(request)
        response.cookies.clear()
        return response


class TransportSecurity(SecurityMiddleware):
    """Django's SecurityMiddleware, except on the guest listener, which is
    plain HTTP by design: no redirect to HTTPS, and no header that browsers
    ignore on an insecure origin. HSTS stays off everywhere (hardening.py)."""

    def process_request(self, request):
        return None if listener(request) else super().process_request(request)

    def process_response(self, request, response):
        response = super().process_response(request, response)
        if listener(request):
            del response["Cross-Origin-Opener-Policy"]
        return response
