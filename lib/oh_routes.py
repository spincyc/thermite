"""Finite Thermite page routes shared by native and household HTTP entry points.

Native /settings/ belongs to the deliberate advanced openHAB interface.
Static assets remain under /static/remote/; served HTML uses absolute asset URLs.
"""

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
