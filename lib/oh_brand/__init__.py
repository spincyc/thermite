"""thermite's look: the flaming bowl mark and color themes (palettes, here).

The bowl is one drawing, TORCH: rounded contours in a 64 by 64
box, each painted in a palette role. svg() draws it for the web and
terminal() in braille dots (eight a cell) and quadrant blocks, from the
same paths, so the mark is the same in a browser and in a terminal;
png() paints it for an app icon where SVG will not do.

A palette gives every role in ROLES a color in a dark and a light mode.
The built-in ones live in palettes/; a palette file of the user's (JSON,
or YAML where PyYAML is installed) has the same shape, may `extends` a
built-in palette or another file and change only some roles, and is
checked for shape and for contrast (CONTRAST) before anything uses it.
theme.schema.json describes the shape for editors.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import json
import math
import os
import re
import struct
import zlib
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
PALETTES = HERE / "palettes"
SCHEMA = HERE / "theme.schema.json"
DEFAULT = "thermite"
MODES = ("dark", "light")

#: role: what it colors. Every mode of a palette gives each one.
ROLES = {
    "bg": "the page or terminal background",
    "surface": "panels and sheets on the background",
    "raised": "keys, cards and anything raised above a surface",
    "line": "borders and dividers",
    "text": "body text",
    "muted": "secondary text",
    "faint": "hints, disabled text and decoration",
    "accent": "the brand color: links, the current choice, primary buttons",
    "accent-ink": "text on accent",
    "focus": "the keyboard focus ring",
    "ok": "done, online",
    "busy": "working, waiting",
    "bad": "failed, offline, a problem",
    "info": "neutral notices",
    "ember": "deep ember accents around the flame",
    "flame": "the bowl's flame contour",
    "glow": "the flame's warm glow",
    "core": "the hot inner flame stroke",
    "spark": "brief sparks above the bowl",
    "metal": "the bowl's metal rim and cup",
    "metal-shade": "shaded metal accents",
    "rust": "warm rust accents",
}
#: Panel colors, in order (the terminal remote's boxes, as in btop).
HUES = 5

#: (foreground, background, minimum WCAG contrast ratio). 4.5 is WCAG AA
#: for text, 3 for large text and for the parts of controls.
CONTRAST = (
    ("text", "bg", 4.5), ("text", "surface", 4.5), ("text", "raised", 4.5),
    ("muted", "bg", 4.5), ("muted", "surface", 4.5),
    ("accent", "bg", 3.0), ("accent-ink", "accent", 4.5), ("focus", "bg", 3.0),
    ("ok", "bg", 3.0), ("busy", "bg", 3.0), ("bad", "bg", 3.0), ("info", "bg", 3.0),
    ("flame", "bg", 3.0), ("metal", "bg", 3.0),
)

HEX = re.compile(r"#[0-9a-fA-F]{6}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class PaletteError(ValueError):
    """A palette that cannot be used, with every reason."""

    def __init__(self, source: str, problems: list[str]) -> None:
        super().__init__(f"{source}: " + "; ".join(problems))
        self.source = source
        self.problems = problems


@dataclasses.dataclass(frozen=True)
class Palette:
    name: str
    description: str
    modes: dict  # mode: {role: "#rrggbb", "hues": [...]}
    source: str

    def color(self, mode: str, role: str) -> str:
        return self.modes[mode][role]

    def hues(self, mode: str) -> list[str]:
        return list(self.modes[mode]["hues"])


def builtin() -> list[str]:
    return sorted(path.stem for path in PALETTES.glob("*.json"))


def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise PaletteError(str(path), ["YAML themes need PyYAML; write it as JSON instead"]) from None
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PaletteError(str(path), [f"not JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise PaletteError(str(path), ["a theme is an object with name, dark and light"])
    return data


def shape_problems(data: dict) -> list[str]:
    """What is wrong with a palette file's own content, before `extends`."""
    problems = []
    known = {"$schema", "name", "description", "extends", *MODES}
    problems += [f"unknown key {key!r}" for key in data if key not in known]
    if not isinstance(data.get("name"), str) or not NAME.fullmatch(data.get("name", "")):
        problems.append("name must be lowercase letters, digits and hyphens")
    if "description" in data and not isinstance(data["description"], str):
        problems.append("description must be text")
    if "extends" in data and not isinstance(data["extends"], str):
        problems.append("extends must be a theme's name or file")
    for mode in MODES:
        colors = data.get(mode, {})
        if not isinstance(colors, dict):
            problems.append(f"{mode} must be an object of role: color")
            continue
        for role, value in colors.items():
            if role == "hues":
                if not (isinstance(value, list) and len(value) == HUES
                        and all(isinstance(v, str) and HEX.fullmatch(v) for v in value)):
                    problems.append(f"{mode}.hues must be {HUES} colors like #ff7a1f")
            elif role not in ROLES:
                close = [r for r in ROLES if r.replace("-", "") == role.replace("-", "").replace("_", "")]
                problems.append(f"{mode}: unknown role {role!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
            elif not (isinstance(value, str) and HEX.fullmatch(value)):
                problems.append(f"{mode}.{role} must be a color like #ff7a1f, not {value!r}")
    return problems


def _resolve(spec: str | Path, base: Path, seen: tuple[str, ...]) -> tuple[dict, str]:
    """The merged content of a palette (name or file) and its source."""
    text = str(spec)
    if NAME.fullmatch(text) and (PALETTES / f"{text}.json").exists():
        path = PALETTES / f"{text}.json"
    else:
        path = (base / Path(text).expanduser()).resolve()
        if not path.exists():
            names = ", ".join(builtin())
            raise PaletteError(text, [f"no such theme or file (built-in: {names})"])
    key = str(path)
    if key in seen:
        raise PaletteError(text, ["extends itself: " + " -> ".join([*seen, key])])
    data = _read(path)
    problems = shape_problems(data)
    if problems:
        raise PaletteError(str(path), problems)
    if "extends" in data:
        parent, _ = _resolve(data["extends"], path.parent, (*seen, key))
        merged = {mode: {**parent.get(mode, {}), **data.get(mode, {})} for mode in MODES}
    else:
        merged = {mode: dict(data.get(mode, {})) for mode in MODES}
    merged["name"] = data["name"]
    merged["description"] = data.get("description", "")
    return merged, str(path)


def load(spec: str | Path | None = None) -> Palette:
    """A built-in palette by name, or a palette file; checked for shape and
    completeness (not contrast: see contrast_problems). Older complete
    palettes reuse flame for ember and metal-shade for rust unless given."""
    merged, source = _resolve(spec or DEFAULT, Path.cwd(), ())
    problems = []
    for mode in MODES:
        # These two roles were added after themes could be exported as full
        # files. Keep those files usable with their existing torch colors;
        # _resolve has already validated every explicitly supplied value.
        for role, prior in (("ember", "flame"), ("rust", "metal-shade")):
            if role not in merged[mode] and prior in merged[mode]:
                merged[mode][role] = merged[mode][prior]
        missing = [role for role in ROLES if role not in merged[mode]]
        if missing:
            problems.append(f"{mode} lacks {', '.join(missing)} (or `extends` a theme that has them)")
        if "hues" not in merged[mode]:
            problems.append(f"{mode} lacks hues")
    if problems:
        raise PaletteError(source, problems)
    return Palette(merged["name"], merged["description"], {m: merged[m] for m in MODES}, source)


# --------------------------------------------------------------------------
# contrast


def rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """The WCAG contrast ratio of two colors, 1 to 21."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def contrast_problems(palette: Palette) -> list[str]:
    out = []
    for mode in MODES:
        colors = palette.modes[mode]
        for fg, bg, minimum in CONTRAST:
            ratio = contrast(colors[fg], colors[bg])
            if ratio < minimum:
                out.append(f"{mode}: {fg} on {bg} is {ratio:.2f}:1, under {minimum:g}:1")
        for index, hue in enumerate(colors["hues"], 1):
            ratio = contrast(hue, colors["bg"])
            if ratio < 3.0:
                out.append(f"{mode}: hue {index} on bg is {ratio:.2f}:1, under 3:1")
    return out


# --------------------------------------------------------------------------
# the bowl

#: A broad, shallow metalworking bowl with a restrained asymmetric flame.
#: SVG, app icons and terminal exports share these exact rounded strokes.
TORCH = (
    ("flame", "M16 44 C12 39 15 33 21 29 C19 35 24 37 26 36 C23 28 31 23 34 14 "
              "C39 21 35 28 41 32 C49 36 51 41 45 45 C37 48 23 48 16 44"),
    ("core", "M29 42 C26 38 30 35 32 32 C32 37 36 39 34 43"),
    ("metal", "M5 47 L59 47 C56 59 8 59 5 47"),
)
STROKE_WIDTHS = {"flame": 1.8, "core": 1.6, "metal": 2.8}
WIDTH, HEIGHT = 64, 64
#: Frame the square app icon with quiet space around the bowl.
TILE = (-4, -4, 72)
#: A transparent interface mark, tightly framing the same 64-square paths.
MARK = (2, 11, 60, 48)
#: The spark's center. A drop a few dots across can sample to nothing,
#: so the terminal drawing always lights the dot here.
SPARK = (54.0, 6.0)
FLAMES = ("ember", "flame", "glow", "core")
BODY = ("rust", "metal", "metal-shade")
#: The terminal drawing's frame starts this far above the bowl, room for
#: the embers the terminal remote sends up. Each braille dot takes
#: SAMPLES x SAMPLES points of the paths, their curves cut into STEPS lines.
TERMINAL_TOP, TERMINAL_SAMPLES, TERMINAL_STEPS = -6.0, 3, 8
#: Percent: the ground of a flame cell with every dot lit, its own color
#: over the background, so the flame reads solid and glows.
GLOW = 30
#: Braille dots (U+2800 + bits) in reading order: left, right, then down;
#: and the quadrant blocks by their quarters (1 upper left, 2 upper right,
#: 4 lower left, 8 lower right).
DOTS = (0x01, 0x08, 0x02, 0x10, 0x04, 0x20, 0x40, 0x80)
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"

_TOKEN = re.compile(r"[MLCZ]|-?\d+(?:\.\d+)?")


def _commands(path: str) -> list[tuple[str, list[float]]]:
    out: list[tuple[str, list[float]]] = []
    for token in _TOKEN.findall(path):
        if token in "MLCZ":
            out.append((token, []))
        else:
            out[-1][1].append(float(token))
    return out


def polygon(path: str, steps: int = 16) -> list[tuple[float, float]]:
    """The path's outline as points, its curves cut into `steps` lines.
    tui/remote/torch.js does the same arithmetic in the same order, so
    both draw the same dots."""
    points: list[tuple[float, float]] = []
    for op, args in _commands(path):
        if op in "ML":
            points.append((args[0], args[1]))
        elif op == "C":
            (x0, y0), (x1, y1, x2, y2, x3, y3) = points[-1], args
            for i in range(1, steps + 1):
                t = i / steps
                u = 1 - t
                points.append((u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                               u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3))
    return points


@functools.lru_cache(maxsize=24)
def stroke_shapes(path: str, width: float, steps: int = 16) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Round SVG strokes as overlapping closed polygons for raster exports.

    The common scanline painter unions the pieces by painting the same ink;
    no raster library or platform font can change the app icon's geometry.
    """
    points = polygon(path, steps)
    radius = width / 2
    shapes = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if not length:
            continue
        dx, dy = radius * (y1 - y0) / length, radius * (x0 - x1) / length
        shapes.append(((x0 + dx, y0 + dy), (x1 + dx, y1 + dy), (x1 - dx, y1 - dy), (x0 - dx, y0 - dy)))
    for x, y in points:
        shapes.append(tuple((x + radius * math.cos(index * math.tau / 12), y + radius * math.sin(index * math.tau / 12))
                            for index in range(12)))
    return tuple(shapes)


def mark_colors(palette: Palette, mode: str) -> dict:
    """Keep the hot inner stroke legible on paper as well as dark ground."""
    colors = dict(palette.modes[mode])
    if contrast(colors["core"], colors["bg"]) < 3:
        colors["core"] = colors["flame"]
    return colors


def svg(palette: Palette | None = None, mode: str = "dark", *, tile: bool = False, size: int | None = None,
        title: str = "thermite", css_vars: bool = False, crop: bool = False) -> str:
    """The flaming bowl as SVG; `tile` puts it on a rounded square of
    the palette's surface, as an app icon. With `css_vars` each fill is
    var(--th-<role>) (css()), so the page's palette and theme color it.
    `crop` tightly frames the transparent interface mark without stretching."""
    palette = palette or load()
    colors = ({role: f"var(--th-{role})" for role in ROLES} if css_vars else mark_colors(palette, mode))
    if css_vars:
        colors["core"] = "var(--th-mark-core)"
    if tile:
        left, top, side = TILE
        box = f"{left} {top} {side} {side}"
        width, height = (size or 112), (size or 112)
        back = (f'<rect x="{left}" y="{top}" width="{side}" height="{side}" rx="22" '
                f'fill="{colors["surface"]}"/>')
    elif crop:
        left, top, box_width, box_height = MARK
        box = f"{left} {top} {box_width} {box_height}"
        width = size or 80
        height = max(1, round(width * box_height / box_width))
        back = ""
    else:
        box = f"0 0 {WIDTH} {HEIGHT}"
        width, height = (size or WIDTH), (size or WIDTH) * HEIGHT // WIDTH
        back = ""
    shapes = "".join(f'<path fill="none" stroke="{colors[role]}" stroke-width="{STROKE_WIDTHS[role]}" '
                     f'stroke-linecap="round" stroke-linejoin="round" d="{path}"/>' for role, path in TORCH)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" width="{width}" height="{height}" '
            f'role="img" aria-label="{title}"><title>{title}</title>{back}{shapes}</svg>\n')


def mark_svg(palette: Palette | None = None, mode: str = "dark", *, size: int | None = None,
             title: str = "thermite", css_vars: bool = False) -> str:
    """The same bowl, cropped for an interface. External images use a
    separate export per mode because they cannot inherit the page's theme."""
    return svg(palette, mode, size=size, title=title, css_vars=css_vars, crop=True)


def terminal_size(rows: int) -> tuple[int, float, float]:
    """(columns, the side of a dot, the frame's left edge) of the terminal
    drawing `rows` cells tall: a braille dot is about square in a cell
    twice as tall as wide."""
    unit = (HEIGHT - TERMINAL_TOP) / (rows * 4)
    columns = math.ceil(WIDTH / unit / 2)
    return columns, unit, WIDTH / 2 - columns * unit


def dots(rows: int = 12, layers: tuple[tuple[str, str], ...] = TORCH) -> list[list[str | None]]:
    """The bowl as braille dots, 4 a cell down and 2 across: each dot
    takes the role most of its samples fall in (the front one of a tie),
    or None where most fall outside the bowl."""
    columns, unit, left = terminal_size(rows)
    samples = TERMINAL_SAMPLES
    step = unit / samples
    count = columns * 2 * samples
    names = list(dict.fromkeys(role for role, _ in layers))
    shapes = [(names.index(role) + 1, shape) for role, path in layers
              for shape in stroke_shapes(path, STROKE_WIDTHS.get(role, 1.8), TERMINAL_STEPS)]
    grid: list[list[str | None]] = []
    for row in range(rows * 4):
        tally = [[0] * (len(names) + 1) for _ in range(columns * 2)]
        for sub in range(samples):
            y = TERMINAL_TOP + (row * samples + sub + 0.5) * step
            line = [0] * count
            for which, points in shapes:
                for first, end in _spans(points, y, left, step, count):
                    line[first:end] = [which] * (end - first)
            for x, which in enumerate(line):
                tally[x // samples][which] += 1
        out: list[str | None] = []
        for votes in tally:
            best = max(range(1, len(votes)), key=lambda k: (votes[k], k))
            # Lit from five in eleven samples inside: edges keep their weight.
            lit = votes[best] and (sum(votes) - votes[0]) * 6 >= votes[0] * 5
            out.append(names[best - 1] if lit else None)
        grid.append(out)
    if "spark" in names:
        x, y = math.floor((SPARK[0] - left) / unit), math.floor((SPARK[1] - TERMINAL_TOP) / unit)
        if 0 <= y < len(grid) and 0 <= x < len(grid[y]) and grid[y][x] is None:
            grid[y][x] = "spark"
    return grid


def _most(roles: list[str], order: list[str]) -> str:
    return max(dict.fromkeys(roles), key=lambda role: (roles.count(role), order.index(role)))


def _cell(block: list[str | None], order: list[str], glow: int) -> tuple[str, str | None, str | None, str] | None:
    """One cell from its 8 dots: (character, fg, bg, the character without
    color). Mostly metal and rust: quadrant blocks, solid as metal is, in
    up to two roles; the flame and the spark: braille dots in one role,
    on a ground of its own glow where every dot is lit."""
    lit = [role for role in block if role]
    if not lit:
        return None
    if sum(role in BODY for role in lit) * 2 >= len(lit):
        quads = [block[0] or block[2], block[1] or block[3], block[4] or block[6], block[5] or block[7]]
        present = [role for role in quads if role]
        fg = _most([role for role in present if role in BODY] or [role for role in lit if role in BODY], order)
        others = [role for role in present if role != fg]
        # Metal's two roles can share a cell; flame over the rim cannot, its
        # quads simply stay dark (the braille flame covers it).
        solid = all(role in BODY for role in present)
        bg = _most(others, order) if others and len(present) == 4 and solid else None
        mask = sum(1 << k for k, role in enumerate(quads)
                   if role and (role == fg or (bg is None and role in BODY)))
        shape = sum(1 << k for k, role in enumerate(quads) if role)
        return QUADRANTS[mask], fg, bg, QUADRANTS[shape]
    fg = _most(lit, order)
    glyph = chr(0x2800 + sum(bit for bit, role in zip(DOTS, block) if role))
    bg = f"{fg}%{glow}" if glow and len(lit) == 8 and fg in FLAMES else None
    return glyph, fg, bg, glyph


def _cells(grid: list[list[str | None]], order: list[str], glow: int = GLOW):
    rows, columns = len(grid) // 4, len(grid[0]) // 2
    return [[_cell([grid[row * 4 + j][column * 2 + i] for j in range(4) for i in range(2)], order, glow)
             for column in range(columns)] for row in range(rows)]


def cells(rows: int = 12) -> list[list[list[str | None] | None]]:
    """The terminal drawing as data, for front ends that paint cells
    themselves: rows of [character, fg, bg] cells, None where empty. fg
    is a role; bg is a role, "role%N" (N percent of the role over the
    background) or None (the background)."""
    order = list(dict.fromkeys(role for role, _ in TORCH))
    return [[list(cell[:3]) if cell else None for cell in line] for line in _cells(dots(rows), order)]


def spec() -> dict:
    """What tui/remote/torch.js needs to draw the terminal drawing, and to
    make it flicker: the layers, and how dots() samples them."""
    return {"layers": [list(layer) for layer in TORCH], "width": WIDTH, "height": HEIGHT, "top": TERMINAL_TOP,
            "samples": TERMINAL_SAMPLES, "steps": TERMINAL_STEPS, "spark": list(SPARK), "flames": list(FLAMES),
            "body": list(BODY), "glow": GLOW, "strokes": dict(STROKE_WIDTHS)}

def color_depth(stream=None, env: dict | None = None) -> int:
    """24, 8 or 0 bits: what the terminal shows, honoring NO_COLOR."""
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return 0
    if stream is not None and not (hasattr(stream, "isatty") and stream.isatty()):
        return 0
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 24
    return 8 if "256" in env.get("TERM", "") or env.get("TERM", "").startswith("xterm") else 0


def xterm256(color: str) -> int:
    """The nearest of xterm's 256 colors (its 6x6x6 cube or gray ramp)."""
    r, g, b = rgb(color)
    levels = (0, 95, 135, 175, 215, 255)
    cube = [min(range(6), key=lambda i: abs(levels[i] - v)) for v in (r, g, b)]
    cube_rgb = [levels[i] for i in cube]
    gray = min(range(24), key=lambda i: abs(8 + 10 * i - (r + g + b) / 3))
    gray_rgb = [8 + 10 * gray] * 3

    def distance(other):
        return sum((a - b) ** 2 for a, b in zip((r, g, b), other))
    if distance(gray_rgb) < distance(cube_rgb):
        return 232 + gray
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def _sgr(color: str, depth: int, background: bool) -> str:
    if depth == 24:
        r, g, b = rgb(color)
        return f"\x1b[{48 if background else 38};2;{r};{g};{b}m"
    return f"\x1b[{48 if background else 38};5;{xterm256(color)}m"


def mix(a: str, b: str, share: float) -> str:
    """share of the way from color a to color b. A half goes up, as
    Math.round() has it in theme.js."""
    return "#" + "".join(f"{int(x + (y - x) * share + 0.5):02x}" for x, y in zip(rgb(a), rgb(b)))


def ground(colors: dict, spec: str) -> str:
    """A cell's ground as cells() names it: a role, or "role%N"."""
    role, _, share = spec.partition("%")
    return mix(colors["bg"], colors[role], int(share) / 100) if share else colors[role]


def terminal(palette: Palette | None = None, mode: str = "dark", rows: int = 12, depth: int = 24) -> list[str]:
    """The torch as lines of braille dots and quadrant blocks, `rows`
    cells tall (cells()); empty cells keep the terminal's own background.
    depth 0 draws the shape without color."""
    palette = palette or load()
    colors = mark_colors(palette, mode)
    order = list(dict.fromkeys(role for role, _ in TORCH))
    lines = []
    for line in _cells(dots(rows), order):
        out = []
        for cell in line:
            if cell is None:
                out.append(" ")
                continue
            glyph, fg, bg, shape = cell
            if depth == 0:
                out.append(shape)
            else:
                out.append(_sgr(colors[fg], depth, False) + (_sgr(ground(colors, bg), depth, True) if bg else "")
                           + glyph + "\x1b[0m")
        lines.append("".join(out).rstrip())
    return lines

def _spans(points: list[tuple[float, float]], y: float, left: float, step: float, count: int):
    """(first, end) indexes of the samples left + (i + 0.5) * step on the
    line at y that fall inside the outline, by the even-odd rule."""
    xs = []
    j = len(points) - 1
    for i, (xi, yi) in enumerate(points):
        xj, yj = points[j]
        if (yi > y) != (yj > y):
            xs.append(xi + (y - yi) * (xj - xi) / (yj - yi))
        j = i
    xs.sort()
    for x0, x1 in zip(xs[0::2], xs[1::2]):
        first = max(0, math.ceil((x0 - left) / step - 0.5))
        end = min(count, math.ceil((x1 - left) / step - 0.5))
        if end > first:
            yield first, end


def png(palette: Palette | None = None, mode: str = "dark", size: int = 180, samples: int = 4) -> bytes:
    """The torch on a square of the palette's surface, `size` pixels a
    side, as a PNG: an apple-touch-icon, which iOS rounds itself. Each
    pixel averages samples x samples points, and the bytes are the same on
    every machine (deflate()), so a copy of them can be checked."""
    palette = palette or load()
    colors = mark_colors(palette, mode)
    return _painted(colors["surface"], tuple((colors[role], path, STROKE_WIDTHS[role]) for role, path in TORCH), size, samples)


@functools.lru_cache(maxsize=8)
def _painted(surface: str, layers: tuple[tuple[str, str, float], ...], size: int, samples: int) -> bytes:
    left, top, side = (float(value) for value in TILE)
    count = size * samples
    step = side / count
    shapes = [(rgb(color), shape) for color, path, width in layers for shape in stroke_shapes(path, width)]
    back = rgb(surface)
    total = samples * samples
    rows = []
    for row in range(size):
        sums = [0] * (size * 3)
        for sub in range(samples):
            y = top + (row * samples + sub + 0.5) * step
            line = [back] * count
            for color, points in shapes:
                for first, end in _spans(points, y, left, step, count):
                    line[first:end] = [color] * (end - first)
            for x, (r, g, b) in enumerate(line):
                at = x // samples * 3
                sums[at] += r
                sums[at + 1] += g
                sums[at + 2] += b
        rows.append(bytes((value + total // 2) // total for value in sums))
    return _png(rows, size, size)


def _png(rows: list[bytes], width: int, height: int) -> bytes:
    """8-bit RGB rows as a PNG, each row filtered the way that leaves it
    the most zeros (None, Sub or Up), which deflate() packs best."""
    raw = bytearray()
    previous = bytes(width * 3)
    for row in rows:
        sub = bytes((row[i] - (row[i - 3] if i >= 3 else 0)) & 255 for i in range(len(row)))
        up = bytes((a - b) & 255 for a, b in zip(row, previous))
        kind, data = max(((0, row), (1, sub), (2, up)), key=lambda choice: choice[1].count(0))
        raw.append(kind)
        raw += data
        previous = row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", deflate(bytes(raw)))
            + chunk(b"IEND", b""))


_LENGTHS = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131,
            163, 195, 227, 258)
_LENGTH_BITS = (0,) * 8 + (1,) * 4 + (2,) * 4 + (3,) * 4 + (4,) * 4 + (5,) * 4 + (0,)


def deflate(data: bytes) -> bytes:
    """data as a zlib stream: one block of deflate's fixed codes whose only
    matches repeat the byte before (distance 1). zlib.compress's bytes
    change with the zlib build; these depend on the data alone."""
    out = bytearray(b"\x78\x01")
    pending = [0, 0]  # bits not yet written, and how many

    def put(value: int, count: int) -> None:
        pending[0] |= value << pending[1]
        pending[1] += count
        while pending[1] >= 8:
            out.append(pending[0] & 255)
            pending[0] >>= 8
            pending[1] -= 8

    def symbol(value: int) -> None:
        if value < 144:
            code, count = 0x30 + value, 8
        elif value < 256:
            code, count = 0x190 + value - 144, 9
        elif value < 280:
            code, count = value - 256, 7
        else:
            code, count = 0xC0 + value - 280, 8
        # A Huffman code goes out from its most significant bit.
        put(int(format(code, f"0{count}b")[::-1], 2), count)

    put(1, 1)
    put(1, 2)
    index = 0
    while index < len(data):
        byte = data[index]
        run = 1
        while index + run < len(data) and data[index + run] == byte and run <= 258:
            run += 1
        symbol(byte)
        index += 1
        if run > 3:
            length = run - 1
            at = bisect.bisect_right(_LENGTHS, length) - 1
            symbol(257 + at)
            put(length - _LENGTHS[at], _LENGTH_BITS[at])
            put(0, 5)
            index += length
    symbol(256)
    if pending[1]:
        out.append(pending[0] & 255)
    return bytes(out) + struct.pack(">I", zlib.adler32(data))


# --------------------------------------------------------------------------
# exports


def css(palette: Palette | None = None, prefix: str = "--th-") -> str:
    """The palette as CSS custom properties: dark by default and under
    [data-theme="dark"], light under [data-theme="light"] and, without a
    data-theme, wherever the system prefers light."""
    palette = palette or load()

    def block(mode: str) -> list[str]:
        colors = palette.modes[mode]
        lines = [f"  color-scheme: {mode};"]
        lines += [f"  {prefix}{role}: {colors[role]};" for role in ROLES]
        lines.append(f"  {prefix}mark-core: {mark_colors(palette, mode)['core']};")
        lines += [f"  {prefix}hue-{i}: {hue};" for i, hue in enumerate(colors["hues"], 1)]
        return lines
    out = [f"/* thermite theme {palette.name}: {palette.description} */",
           ':root, [data-theme="dark"] {', *block("dark"), "}",
           '[data-theme="light"] {', *block("light"), "}",
           "@media (prefers-color-scheme: light) {", "  :root:not([data-theme]) {",
           *["  " + line for line in block("light")], "  }", "}"]
    return "\n".join(out) + "\n"


def as_json(palette: Palette) -> str:
    data = {"name": palette.name, "description": palette.description,
            **{mode: palette.modes[mode] for mode in MODES}}
    return json.dumps(data, indent=2) + "\n"


def swatches(palette: Palette, mode: str, roles: Iterable[str] = ROLES) -> list[tuple[str, str, str]]:
    """(role, color, what) rows, for docs."""
    return [(role, palette.modes[mode][role], ROLES[role]) for role in roles]
