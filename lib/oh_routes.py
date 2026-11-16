"""Finite Thermite page routes shared by native and household HTTP entry points.

Native /settings/ belongs to the deliberate advanced openHAB interface.
Static assets remain under /static/remote/; served HTML uses absolute asset URLs,
each non-HTML one with ?v=<asset_version> of the bytes it names.
"""

import hashlib

PAGES = (
    ("home", "/", "home.html"),
    ("remotes", "/remotes/", "index.html"),
    ("pairing", "/pairing/", "pair.html"),
    ("security", "/security/", "security.html"),
    ("cameras", "/cameras/", "cameras.html"),
    ("site", "/site/", "topology.html"),
    ("system", "/system/", "system.html"),
    ("settings", "/room-settings/", "settings.html"),
    ("users", "/users/", "users.html"),
    ("access", "/account/", "access.html"),
)
ROUTES = {path: asset for _, path, asset in PAGES}
ASSET_ROUTES = {asset: path for _, path, asset in PAGES}
ALIASES = {path[:-1]: path for path in ROUTES if path != "/"}
ALIASES.update({"/overview": "/", "/overview/": "/"})


def asset_version(data: bytes) -> str:
    """The ?v= value for these exact asset bytes. The gateway sends a URL
    carrying the current value as immutable, and any other as no-cache."""
    return hashlib.sha256(data).hexdigest()[:16]
