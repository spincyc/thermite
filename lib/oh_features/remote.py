"""The remote-control web page (web/remote/), served by openHAB.

openHAB serves conf/html/ at /static/, so this copies the page's files into
conf/html/remote/ and phones open /static/remote/. The copies are byte for
byte (a manifest or an image has no room for a "do not edit" header);
`make check` keeps them identical to web/remote/. The page needs the room
Source selectors, so a house without routing gets no remote.

THEMED, the page's colors and icons, are not copied but drawn by oh_brand
from the house's theme: `theme` in devices.yaml, a built-in theme's name or
a theme file relative to instance/, and thermite's own without one. The
copies in web/remote/ are the default theme's, for a preview from disk;
tests/test_remote_ui.py keeps them so.
"""

from __future__ import annotations

import json

import oh_access_mode
import oh_brand as brand
from oh_conf import LIB, Conf, House

from . import remote_activity, remote_cameras, remote_diagnostics, remote_topology, remote_model

NAME = "remote"
SOURCE = LIB.parent / "web/remote"
INSTANCE = LIB.parent / "instance"
TARGET = "html/remote"
#: Every file the page loads; README.md stays behind.
FILES = ("index.html", "theme.css", "remote.css", "house.js", "remotes.js", "activity.js", "activity.json", "diagnostics.js", "diagnostics.json", "backend.js", "session.js", "log.js", "remote.js",
         "appearance.js", "shell.js", "shell.css", "home.html", "home.js", "security.html",
         "access-config.js", "access.js", "access-ui.js", "access.css", "access.html", "users.html", "settings.html",
         "manifest.webmanifest", "icon.svg", "mark.svg", "mark-light.svg", "apple-touch-icon.png", "pair.html", "pair.js", "pair.css",
         "cameras.html", "cameras.js", "cameras.css", "cameras.json", "topology.html", "topology.js", "topology.css", "topology.json")
#: The files of FILES drawn from the theme, and the `thermite theme export`
#: format that writes each.
THEMED = {"theme.css": "css", "icon.svg": "icon", "mark.svg": "mark", "mark-light.svg": "mark --light", "apple-touch-icon.png": "png"}
URL = "/static/remote/"


def palette(data: dict) -> brand.Palette:
    """The house's theme; raises brand.PaletteError."""
    spec = data.get("theme")
    if spec is None:
        return brand.load()
    return brand.load(spec if spec in brand.builtin() else INSTANCE / spec)


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
    if house.data.get("routing") is None and not remote_cameras.manifest(house)["cameras"]:
        return
    drawn = themed(palette(house.data))
    for name in FILES:
        if name == "house.js":
            conf.files[f"{TARGET}/{name}"] = remote_model.script(house, remote_model.presentation(SOURCE / name))
        elif name == "access-config.js":
            conf.files[f"{TARGET}/{name}"] = oh_access_mode.script(house.data)
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
