"""The remote-control web page (web/remote/), served by openHAB.

openHAB serves conf/html/ at /static/, so this copies the page's files into
conf/html/remote/; oh_routes gives pages their public URLs. Served HTML adds
absolute asset URLs and clean navigation links; the manifest starts at Home.
Other assets are copied byte for byte. Home and System are always
available, including sites with no configured rooms or cameras.

THEMED, the page's colors and icons, are not copied but drawn by oh_brand
from the house's theme: `theme` in devices.yaml, a built-in theme's name or
a theme file relative to instance/, and thermite's own without one. The
copies in web/remote/ are the default theme's, for a preview from disk;
tests/test_remote_ui.py keeps them so.
"""

from __future__ import annotations

import json
import re

import oh_access_mode
import oh_brand as brand
import oh_entry
import oh_routes
from oh_conf import LIB, Addon, Conf, House

from . import remote_activity, remote_cameras, remote_diagnostics, remote_topology, remote_model, remote_system

NAME = "remote"
SOURCE = LIB.parent / "web/remote"
from oh_paths import SITE
INSTANCE = SITE / "instance"
TARGET = "html/remote"
#: Every file the page loads; README.md stays behind.
FILES = ("index.html", "theme.css", "remote.css", "house.js", "remotes.js", "activity.js", "activity.json", "diagnostics.js", "diagnostics.json", "backend.js", "session.js", "log.js", "remote.js",
         "appearance.js", "navigation.js", "shell.js", "shell.css", "home.html", "home.css", "home.js", "security.html",
         "system.html", "system.js", "system.json",
         "access-config.js", "access.js", "access-ui.js", "access.css", "access.html", "users.html", "settings.html",
         "manifest.webmanifest", "icon.svg", "mark.svg", "mark-light.svg", "apple-touch-icon.png", "pair.html", "pair.js", "pair.css",
         "cameras.html", "cameras.js", "cameras.css", "cameras.json", "topology.html", "topology.js", "topology.css", "topology.json")
#: The files of FILES drawn from the theme, and the `thermite theme export`
#: format that writes each.
THEMED = {"theme.css": "css", "icon.svg": "icon", "mark.svg": "mark", "mark-light.svg": "mark --light", "apple-touch-icon.png": "png"}
ASSET_URL = "/static/remote/"
URL = "/remotes/"


def served_page(content: str, versions: dict[str, str] | None = None) -> str:
    """Give generated pages stable URLs while source previews stay portable."""
    routes = {**oh_routes.ASSET_ROUTES, "./": URL}
    content = content.replace('<html ', '<html data-thermite-hosted="true" ', 1)
    def address(match):
        before, name, after = match.groups()
        target = routes.get(name, "/static/remote/" + name if name in FILES else name)
        if name not in routes and versions and name in versions:
            target += "?v=" + versions[name]
        return before + target + after
    # Only owned, exact URLs change. In particular inline SVG #fragments stay local.
    return re.sub(r'(\b(?:src|href)=")([^"?#]+)(")', address, content)


def served_manifest(content: str, versions: dict[str, str] | None = None) -> str:
    manifest = json.loads(content)
    manifest.update(start_url="/", scope="/")
    if versions:
        for icon in manifest["icons"]:
            icon["src"] += "?v=" + versions[icon["src"]]
    return json.dumps(manifest, indent=2) + "\n"


def asset_versions(files: dict) -> dict[str, str]:
    return {name: oh_routes.asset_version(value.encode() if isinstance(value, str) else value)
            for name in FILES if not name.endswith(".html")
            for value in [files[f"{TARGET}/{name}"]]}


def palette(data: dict) -> brand.Palette:
    """The house's theme; raises brand.PaletteError."""
    spec = data.get("theme")
    if spec is None:
        return brand.load()
    return brand.load(spec if spec in brand.builtin() else INSTANCE / spec, root=INSTANCE)


def themed(chosen: brand.Palette) -> dict[str, bytes]:
    """THEMED's files in a theme: its CSS custom properties (both modes),
    and the torch on a tile in its dark mode, as SVG and as a 180 px PNG."""
    return {
        "theme.css": brand.css(chosen).encode("utf-8"),
        "icon.svg": brand.svg(chosen, "dark", tile=True).encode("utf-8"),
        "mark.svg": brand.mark_svg(chosen, "dark").encode("utf-8"),
        "mark-light.svg": brand.mark_svg(chosen, "light").encode("utf-8"),
        "apple-touch-icon.png": brand.png(chosen, "dark"),
    }


def validate(data: dict) -> list[str]:
    camera_problems = remote_cameras.validate(data) + remote_diagnostics.validate(data)
    spec = data.get("theme")
    if spec is None:
        return camera_problems
    if not isinstance(spec, str) or not spec.strip():
        return ["theme: must be a built-in theme's name or a theme file under instance/"]
    try:
        chosen = palette(data)
    except brand.PaletteError as exc:
        return [f"theme: {exc}"]
    return camera_problems + [f"theme: {spec}: {problem} (thermite theme check)" for problem in brand.contrast_problems(chosen)]


def render(house: House, conf: Conf) -> None:
    conf.files.update(oh_entry.files())
    conf.add_addon(Addon("automation", "jsscripting", "Thermite Home entry routing"))
    drawn = themed(palette(house.data))
    for name in FILES:
        if name == "house.js":
            conf.files[f"{TARGET}/{name}"] = remote_model.script(house)
        elif name == "access-config.js":
            conf.files[f"{TARGET}/{name}"] = oh_access_mode.script(house.data)
        elif name == "system.json":
            metadata = conf.system_metadata if conf.system_metadata is not None else remote_system.selected(mode=oh_access_mode.mode(house.data))
            conf.files[f"{TARGET}/{name}"] = json.dumps(metadata, separators=(",", ":")) + "\n"
        elif name == "activity.json":
            conf.files[f"{TARGET}/{name}"] = json.dumps(remote_activity.manifest(house, conf), separators=(",", ":")) + "\n"
        elif name == "diagnostics.json":
            conf.files[f"{TARGET}/{name}"] = json.dumps(remote_diagnostics.manifest(house, conf), separators=(",", ":")) + "\n"
        elif name == "cameras.json":
            conf.files[f"{TARGET}/{name}"] = json.dumps(remote_cameras.manifest(house, conf), separators=(",", ":")) + "\n"
        elif name == "topology.json":
            conf.files[f"{TARGET}/{name}"] = json.dumps(remote_topology.manifest(house, conf), separators=(",", ":")) + "\n"
        elif name in THEMED:
            conf.files[f"{TARGET}/{name}"] = drawn[name]
        else:
            conf.add_web_file(f"{TARGET}/{name}", SOURCE / name)
    # Native static assets are long-cached. Version only owned asset URLs so a
    # fresh no-store page sees the deployed bytes without a polling timer.
    versions = asset_versions(conf.files)
    conf.files[f"{TARGET}/manifest.webmanifest"] = served_manifest((SOURCE / "manifest.webmanifest").read_text(), versions).encode()
    versions = asset_versions(conf.files)
    for name in FILES:
        if name.endswith(".html"):
            conf.files[f"{TARGET}/{name}"] = served_page((SOURCE / name).read_text(), versions).encode()
