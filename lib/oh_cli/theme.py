"""Show, check and export thermite's look: the flaming bowl mark and the color themes.

    thermite theme show [--theme NAME|FILE] [--dark | --light] [--size SIZE]
    thermite theme list
    thermite theme check FILE ...
    thermite theme export FORMAT [--theme NAME|FILE] [--dark | --light] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import oh_brand as brand
import oh_cli
from oh_cli import action, actions, add_theme, command

SIZES = {"small": 8, "medium": 12, "large": 16}
FORMATS = {
    "css": "the theme as CSS custom properties (--th-<role>), both modes",
    "json": "the theme with everything it extends filled in",
    "svg": "the flaming bowl, in one mode's colors",
    "mark": "the same bowl without a tile, cropped for page headers",
    "icon": "the flaming bowl on a rounded tile, as an app icon",
    "png": "the flaming bowl on a square tile, 180 px, as an iOS home-screen icon (PNG)",
    "cells": "the terminal drawing as JSON rows of [character, fg, bg] cells",
}
#: How `show` groups the roles into rows of swatches.
ROWS = (
    ("ground", ("bg", "surface", "raised", "line")),
    ("ink", ("text", "muted", "faint")),
    ("accent", ("accent", "accent-ink", "focus")),
    ("state", ("ok", "busy", "bad", "info")),
    ("flame", ("ember", "flame", "glow", "core", "spark")),
    ("metal", ("metal", "metal-shade", "rust")),
)

DESCRIPTION = """\
thermite's mark is a bowl of flame, drawn once and shown two ways: as SVG on the
web and in a terminal as braille dots, eight to a character, for the flame
and quadrant blocks for the metal. Its colors, and the front
ends', come from a theme: a color for each role (the background, text,
the accent, the flame...) in a dark and a light mode.

thermite ships the themes `thermite theme list` names. A theme file of
your own (JSON, or YAML) can start from one of them with `extends` and
change only the roles you want; `thermite theme check` tells you what is
wrong with it, including text too faint to read.
"""

NOTES = """\
a theme file:
  {"name": "harbor", "extends": "thermite",
   "dark": {"accent": "#4fb3bf", "accent-ink": "#06181b"},
   "light": {"accent": "#1f6f78"}}

  Roles: """ + ", ".join(brand.ROLES) + """, and hues
  (five panel colors). Colors are #rrggbb. theme.schema.json, beside the
  built-in themes, describes the shape for an editor.
"""


def color_depth(choice: str) -> int:
    if choice == "never" or oh_cli.switches.no_color:
        return 0
    depth = brand.color_depth(None if choice == "always" else sys.stdout)
    return depth or (24 if choice == "always" else 0)


def paint(color: str, text: str, depth: int, bold: bool = False) -> str:
    if depth == 0:
        return text
    return brand._sgr(color, depth, False) + ("\x1b[1m" if bold else "") + text + "\x1b[0m"


def cmd_show(args) -> int:
    palette = brand.load(args.theme)
    mode = args.mode or "dark"
    depth = color_depth(args.color)
    colors = palette.modes[mode]
    torch = brand.terminal(palette, mode, SIZES[args.size], depth)
    side = [paint(colors["text"], "thermite", depth, bold=True),
            paint(colors["muted"], f"{palette.name} theme, {mode}", depth), ""]
    for label, roles in ROWS:
        if depth:
            swatches = " ".join(paint(colors[role], "██", depth) for role in roles)
        else:
            swatches = " ".join(f"{role}={colors[role]}" for role in roles)
        side.append(f"{paint(colors['muted'], f'{label:<7}', depth)}{swatches}")
    hues = " ".join(paint(hue, "██", depth) for hue in colors["hues"]) if depth else " ".join(colors["hues"])
    side.append(f"{paint(colors['muted'], 'hues   ', depth)}{hues}")
    width = brand.terminal_size(SIZES[args.size])[0] + 3
    start = max(0, (len(torch) - len(side)) // 2)
    for index in range(max(len(torch), start + len(side))):
        left = torch[index] if index < len(torch) else ""
        visible = len(_plain(left))
        right = side[index - start] if 0 <= index - start < len(side) else ""
        print(f"{left}{' ' * (width - visible)}{right}".rstrip())
    return 0


def _plain(text: str) -> str:
    out, skipping = [], False
    for char in text:
        if char == "\x1b":
            skipping = True
        elif skipping:
            skipping = char != "m"
        else:
            out.append(char)
    return "".join(out)


def cmd_list(args) -> int:
    names = brand.builtin()
    width = max(len(name) for name in names) + 2
    for name in names:
        palette = brand.load(name)
        mark = " (default)" if name == brand.DEFAULT else ""
        print(f"{name:<{width}}{palette.description}{mark}")
    return 0


def cmd_check(args) -> int:
    status = 0
    for spec in args.files:
        try:
            palette = brand.load(spec)
        except brand.PaletteError as exc:
            print(f"{spec}: cannot be used", file=sys.stderr)
            for problem in exc.problems:
                print(f"  {problem}", file=sys.stderr)
            status = 1
            continue
        problems = brand.contrast_problems(palette)
        if problems:
            print(f"{spec}: usable, but hard to read in places", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            status = 1
        else:
            print(f"{spec}: {palette.name} is complete and every pair reads (WCAG contrast)")
    return status


def cmd_export(args) -> int:
    palette = brand.load(args.theme)
    mode = args.mode or "dark"
    if args.format == "css":
        out = brand.css(palette)
    elif args.format == "json":
        out = brand.as_json(palette)
    elif args.format == "svg":
        out = brand.svg(palette, mode)
    elif args.format == "mark":
        out = brand.mark_svg(palette, mode)
    elif args.format == "icon":
        out = brand.svg(palette, mode, tile=True)
    elif args.format == "png":
        data = brand.png(palette, mode)
        if args.out:
            args.out.write_bytes(data)
            print(f"wrote {args.out}")
        elif sys.stdout.isatty():
            print("thermite theme: png is binary; give --out FILE", file=sys.stderr)
            return 2
        else:
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
        return 0
    else:
        rows = SIZES[args.size]
        out = json.dumps({"columns": brand.terminal_size(rows)[0], "roles": sorted(brand.ROLES),
                          "rows": brand.cells(rows)}) + "\n"
    if args.out:
        args.out.write_text(out, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = command("theme", DESCRIPTION, (
        ("thermite theme show", "the flaming bowl and the default theme, in this terminal"),
        ("thermite theme show --light --size large", "the light mode, larger"),
        ("thermite theme check my-theme.json", "is a theme of yours complete and readable?"),
        ("thermite theme export css --theme my-theme.json", "your theme for a web page"),
    ), notes=NOTES)
    sub = actions(parser)

    show = action(sub, "show", "draw the flaming bowl and a theme's swatches in this terminal", examples=(
        ("thermite theme show", "dark mode, medium size"),
        ("thermite theme show --theme contrast --light", "the high-contrast theme's light mode"),
        ("NO_COLOR=1 thermite theme show", "without color: the shape, and the colors as #rrggbb"),
    ))
    add_theme(show)
    show.add_argument("--size", choices=tuple(SIZES), default="medium",
                      help="small, medium or large: 8, 12 or 16 rows (default: medium)")
    show.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                      help="auto: color when this is a terminal and neither NO_COLOR nor --no-color is set; "
                           "--no-color beats always (default: auto)")
    show.set_defaults(func=cmd_show)

    listing = action(sub, "list", "the built-in themes", examples=(("thermite theme list", "their names"),))
    listing.set_defaults(func=cmd_list)

    check = action(sub, "check", "check theme files: their shape, and that text stays readable", examples=(
        ("thermite theme check my-theme.json", "one file"),
        ("thermite theme check thermite contrast", "the built-in themes, by name"),
    ))
    check.add_argument("files", nargs="+", metavar="FILE", help="theme files, or built-in theme names")
    check.set_defaults(func=cmd_check)

    export = action(sub, "export", "write the theme or the flaming bowl for another program", examples=(
        ("thermite theme export css", "the default theme as CSS"),
        ("thermite theme export svg --light --out torch.svg", "the flaming bowl for a light page"),
        ("thermite theme export icon --theme my-theme.json", "an app icon in your colors"),
        ("thermite theme export png --out apple-touch-icon.png", "the same for an iPhone's home screen"),
    ), notes="formats:\n" + "\n".join(f"  {name:<8}{what}" for name, what in FORMATS.items()))
    export.add_argument("format", choices=tuple(FORMATS), metavar="FORMAT", help=", ".join(FORMATS))
    add_theme(export)
    export.add_argument("--size", choices=tuple(SIZES), default="medium",
                        help="cells only: small, medium or large (default: medium)")
    export.add_argument("--out", type=Path, metavar="FILE", help="write it there (default: standard output)")
    export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except brand.PaletteError as exc:
        print(f"thermite theme: {exc}", file=sys.stderr)
        return 2
