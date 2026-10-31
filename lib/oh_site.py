"""thermite's docs site, built from site/: its Markdown pages, every
verb's own help, and the torch and color themes from oh_brand.

build() returns the whole site as {path: bytes}: plain HTML, CSS, SVG and
JSON, with no build step where it is served. export() writes it into a
checkout of the thermite repository (the site under docs/, which its Pages
workflow publishes, and README.md and FAQ.md at the top). guard() refuses
anything that names this house (its addresses, MACs, host name, device and
source ids): they live in the private homelab repository, and the site is
public.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import socket
from pathlib import Path

import oh_brand as brand
import oh_cli

AREA = oh_cli.AREA
SOURCE = AREA / "site"
SITE_URL = "https://spincyc.github.io/thermite/"
#: (page, title in the navigation, <title>, description)
PAGES = (
    ("index", "Home", "thermite", "Run your whole house on open software."),
    ("start", "Get started", "Get started", "Describe the house, try it, install it, use it."),
    ("dashboards", "Dashboards", "Dashboards", "The history of the house, openHAB and its machine, in Grafana."),
    ("commands", "Commands", "Commands", "Every thermite command, its switches and examples."),
    ("colors", "Colors", "Colors and the bowl", "The bowl, the themes, and themes of your own."),
    ("faq", "FAQ", "Questions", "Short answers about thermite."),
)
EXAMPLES = ("harbor",)
TOP = ("README.md", "FAQ.md")
HOST_FILE = AREA / "instance/host.yaml"
PRIVATE_WORDS = SOURCE / "private-words.txt"
#: Makes and products named by public help, independently of this house's inventory.
PUBLIC_MAKES = {"sony", "apple", "sonos", "unifi", "homebridge"}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAC = re.compile(r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])")
#: Loopback, unspecified, and the ranges RFC 5737 sets aside for documentation.
_SAFE_IP = re.compile(r"127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+")


# --------------------------------------------------------------------------
# Markdown, the part of it the pages use


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")


def _link(url: str) -> str:
    if url.startswith(SITE_URL):
        url = url[len(SITE_URL):] or "index.html"
    if not re.match(r"[a-z]+:", url):
        url = re.sub(r"(^|/)(README|FAQ)\.md(#|$)", lambda m: m.group(1) + ("index" if m.group(2) == "README"
                                                                          else "faq") + ".html" + m.group(3), url)
        url = re.sub(r"\.md(#|$)", r".html\1", url)
    return url


def inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
            continue
        part = html.escape(part, quote=False)
        part = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                      lambda m: f'<a href="{html.escape(_link(m.group(2)))}">{m.group(1)}</a>', part)
        part = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", part)
        part = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<em>\1</em>", part)
        out.append(part)
    return "".join(out)


def markdown(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            language = line[3:].strip()
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            css = f' class="language-{language}"' if language else ""
            out.append(f"<pre><code{css}>{html.escape(chr(10).join(body), quote=False)}</code></pre>")
        elif line.lstrip().startswith("<"):
            block = []
            while i < len(lines) and lines[i].strip():
                block.append(lines[i])
                i += 1
            out.append("\n".join(block))
        elif re.match(r"#{1,4} ", line):
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip()
            out.append(f'<h{level} id="{slug(title)}">{inline(title)}</h{level}>')
            i += 1
        elif line.startswith("|") and i + 1 < len(lines) and re.match(r"\|[\s:|-]+\|$", lines[i + 1].strip()):
            def cells(row: str) -> list[str]:
                return [cell.strip() for cell in row.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in rows)
                       + "</tbody></table>")
        elif re.match(r"(- |\d+\. )", line):
            ordered = not line.startswith("- ")
            items: list[str] = []
            while i < len(lines) and (re.match(r"(- |\d+\. )", lines[i]) or
                                      (lines[i].startswith("  ") and items)):
                if lines[i].startswith("  "):
                    items[-1] += " " + lines[i].strip()
                else:
                    items.append(re.sub(r"^(- |\d+\. )", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{tag}>")
        elif line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i][1:].strip())
                i += 1
            out.append(f"<blockquote><p>{inline(' '.join(quote))}</p></blockquote>")
        else:
            paragraph = []
            while (i < len(lines) and lines[i].strip() and not re.match(r"(#{1,4} |```|- |\d+\. |> |\|)", lines[i])
                   and not lines[i].lstrip().startswith("<")):
                paragraph.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(paragraph))}</p>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# what the pages are made of


def parser_of(verb: str) -> argparse.ArgumentParser:
    return oh_cli.parser_of(verb)


def subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for item in parser._actions:
        if isinstance(item, argparse._SubParsersAction):
            seen, out = set(), {}
            for name, sub in item.choices.items():
                if id(sub) not in seen:
                    seen.add(id(sub))
                    out[name] = sub
            return out
    return {}


def plain(text: str) -> str:
    return _ANSI.sub("", text)


def public_verbs() -> list[str]:
    return [verb for verb in oh_cli.VERBS if verb not in oh_cli.HOUSE_ONLY]


def commands_page() -> str:
    verbs = public_verbs()
    parts = ['<h1 id="commands">Commands</h1>',
             "<p>One program, <code>thermite</code>, with a verb for each job. Every verb answers "
             "<code>--help</code> (or <code>thermite help VERB</code>) with what it does, its switches and "
             "examples; this page is that help, word for word.</p>"]
    for group, heading in oh_cli.GROUPS:
        rows = [(verb, summary) for verb, (g, summary) in oh_cli.VERBS.items() if g == group and verb in verbs]
        parts.append(f'<h2 id="{group}">{html.escape(heading[:1].upper() + heading[1:])}</h2>')
        parts.append('<table class="verbs"><tbody>' + "".join(
            f'<tr><td><a href="#{verb}"><code>thermite {verb}</code></a></td><td>{html.escape(summary)}</td></tr>'
            for verb, summary in rows) + "</tbody></table>")
    parts.append('<h2 id="overview">thermite --help</h2>')
    parts.append(f'<pre class="help">{html.escape(plain(oh_cli.overview(verbs)))}</pre>')
    for verb in verbs:
        summary = oh_cli.VERBS[verb][1]
        parser = parser_of(verb)
        parts.append(f'<section class="verb" id="{verb}"><h3><code>thermite {verb}</code></h3>'
                     f"<p>{html.escape(summary[:1].upper() + summary[1:])}.</p>"
                     f'<pre class="help">{html.escape(plain(parser.format_help()))}</pre>')
        for name, sub in subparsers(parser).items():
            parts.append(f'<details id="{verb}-{name}"><summary><code>thermite {verb} {name}</code></summary>'
                         f'<pre class="help">{html.escape(plain(sub.format_help()))}</pre></details>')
        parts.append("</section>")
    return "\n".join(parts) + "\n"


def terminal_html(rows: int = 16) -> str:
    """The terminal drawing, cell for cell, in the page's own theme: each
    cell 2 by 4 units, its braille dots as circles and its quadrant blocks
    as rectangles, so no font has to have them."""
    lines = brand.cells(rows)
    columns = len(lines[0])
    shapes: dict[str, list[str]] = {}

    def fill(spec: str) -> str:
        role, _, share = spec.partition("%")
        if share:
            return f"color-mix(in srgb, var(--th-{role}) {share}%, var(--th-bg))"
        return f"var(--th-{role})"
    for y, line in enumerate(lines):
        for x, cell in enumerate(line):
            if not cell:
                continue
            glyph, fg, bg = cell
            if bg:
                shapes.setdefault(fill(bg), []).append(f'<rect x="{x * 2}" y="{y * 4}" width="2" height="4"/>')
            out = shapes.setdefault(fill(fg), [])
            if glyph in brand.QUADRANTS:
                mask = brand.QUADRANTS.index(glyph)
                out += [f'<rect x="{x * 2 + k % 2}" y="{y * 4 + k // 2 * 2}" width="1" height="2"/>'
                        for k in range(4) if mask & 1 << k]
            else:
                bits = ord(glyph) - 0x2800
                out += [f'<circle cx="{x * 2 + k % 2 + 0.5}" cy="{y * 4 + k // 2 + 0.5}" r="0.36"/>'
                        for k, bit in enumerate(brand.DOTS) if bits & bit]
    body = "".join(f'<g fill="{color}">{"".join(parts)}</g>' for color, parts in shapes.items())
    return (f'<div class="term" style="--cols:{columns}"><svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {columns * 2} {rows * 4}" role="img" aria-label="the bowl in a terminal">'
            f"{body}</svg></div>")


def palette_cards(names: list[str]) -> str:
    out = ['<div class="palettes">']
    for name in names:
        palette = brand.load(PALETTE_FILES.get(name, name))
        out.append(f'<div class="palette-card"><div class="palette-head"><h3>{name}</h3>'
                   f'<button type="button" class="button small" data-palette="{name}">Use on this site</button></div>'
                   f"<p>{html.escape(palette.description)}</p>")
        for mode in brand.MODES:
            colors = palette.modes[mode]
            chips = "".join(f'<span class="chip" title="{role} {colors[role]}" style="background:{colors[role]}"></span>'
                            for role in brand.ROLES)
            chips += "".join(f'<span class="chip" title="hue {i} {hue}" style="background:{hue}"></span>'
                             for i, hue in enumerate(colors["hues"], 1))
            out.append(f'<div class="strip" style="background:{colors["bg"]};color:{colors["text"]}">'
                       f'<span class="strip-mark">{brand.svg(palette, mode, size=18).strip()}</span>'
                       f'<span class="strip-mode">{mode}</span><span class="chips">{chips}</span></div>')
        out.append("</div>")
    out.append("</div>")
    return "\n".join(out)


def roles_table() -> str:
    dark, light = brand.load(), brand.load()
    rows = "".join(
        f"<tr><td><code>{role}</code></td><td>{html.escape(what)}</td>"
        f'<td><span class="dot" style="background:{dark.modes["dark"][role]}"></span><code>{dark.modes["dark"][role]}</code></td>'
        f'<td><span class="dot" style="background:{light.modes["light"][role]}"></span><code>{light.modes["light"][role]}</code></td></tr>'
        for role, what in brand.ROLES.items())
    rows += ('<tr><td><code>hues</code></td><td>five panel colors, in order</td><td>'
             + "".join(f'<span class="dot" style="background:{h}"></span>' for h in dark.hues("dark"))
             + "</td><td>" + "".join(f'<span class="dot" style="background:{h}"></span>' for h in light.hues("light"))
             + "</td></tr>")
    return ('<table class="roles"><thead><tr><th>role</th><th>what it colors</th><th>thermite, dark</th>'
            f"<th>thermite, light</th></tr></thead><tbody>{rows}</tbody></table>")


def contrast_list() -> str:
    return "<ul>" + "".join(f"<li><code>{fg}</code> on <code>{bg}</code>: at least {minimum:g}:1</li>"
                            for fg, bg, minimum in brand.CONTRAST) + "<li>each hue on <code>bg</code>: at least 3:1</li></ul>"


PALETTE_FILES = {name: SOURCE / "examples" / f"{name}.json" for name in EXAMPLES}


def all_palettes() -> list[str]:
    return [*brand.builtin(), *EXAMPLES]


def fill(text: str) -> str:
    """The {{name}} pieces a page or the template may use."""
    pieces = {
        "torch": brand.svg(css_vars=True, size=18).strip(),
        "torch_large": brand.svg(css_vars=True, size=132, title="the thermite bowl").strip(),
        "torch_sizes": "".join(f'<span class="torch-size">{brand.svg(css_vars=True, size=s).strip()}'
                               f"<small>{s}px</small></span>" for s in (96, 48, 24, 16)),
        "terminal": terminal_html(12),
        "terminal_large": terminal_html(16),
        "palettes": palette_cards(all_palettes()),
        "roles": roles_table(),
        "contrast": contrast_list(),
        "harbor": (SOURCE / "examples/harbor.json").read_text(encoding="utf-8").strip(),
        "palette_names": " ".join(all_palettes()),
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: pieces[m.group(1)], text)


def page(name: str, content: str) -> str:
    template = (SOURCE / "template.html").read_text(encoding="utf-8")
    nav = "".join(f'<a href="{p}.html"{" aria-current=\"page\"" if p == name else ""}>{label}</a>'
                  for p, label, _, _ in PAGES)
    _, _, title, description = next(p for p in PAGES if p[0] == name)
    out = template
    for key, value in (("{{title}}", f"{title} · thermite" if name != "index" else "thermite"),
                       ("{{description}}", html.escape(description)), ("{{nav}}", nav),
                       ("{{slug}}", name), ("{{content}}", content)):
        out = out.replace(key, value)
    return fill(out)


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name, *_ in PAGES:
        if name == "commands":
            content = commands_page()
        else:
            source = SOURCE / ("FAQ.md" if name == "faq" else f"pages/{name}.md")
            content = markdown(fill(source.read_text(encoding="utf-8")))
        files[f"{name}.html"] = page(name, content).encode("utf-8")
    for asset in ("site.css", "site.js"):
        files[f"assets/{asset}"] = (SOURCE / asset).read_bytes()
    files["assets/torch.svg"] = brand.svg(size=64).encode("utf-8")
    files["assets/torch-light.svg"] = brand.svg(mode="light", size=64).encode("utf-8")
    files["assets/icon.svg"] = brand.svg(tile=True, size=512).encode("utf-8")
    for name in all_palettes():
        palette = brand.load(PALETTE_FILES.get(name, name))
        files[f"assets/palettes/{name}.css"] = brand.css(palette).encode("utf-8")
    for name in brand.builtin():
        files[f"themes/{name}.json"] = (brand.PALETTES / f"{name}.json").read_bytes()
    for name in EXAMPLES:
        files[f"examples/{name}.json"] = PALETTE_FILES[name].read_bytes()
    schema = json.loads(brand.SCHEMA.read_text(encoding="utf-8"))
    files["theme.schema.json"] = (json.dumps(schema, indent=2) + "\n").encode("utf-8")
    return files


# --------------------------------------------------------------------------
# keeping the house out of it


def house_facts(house: Path = oh_cli.HOUSE, host_file: Path = HOST_FILE) -> set[str]:
    """Strings that would name this house: every address, MAC and compound
    id (one-word ids such as a room called office are ordinary words), the
    devices' names and models, their makes but PUBLIC_MAKES, the rooms'
    labels, the host's name, state root and time zone."""
    import yaml
    data = yaml.safe_load(house.read_text(encoding="utf-8")) or {}
    facts: set[str] = set()
    for device in [*(data.get("devices") or []), *(data.get("later") or [])]:
        for key in ("name", "model", "make"):
            if isinstance(device.get(key), str):
                facts.add(device[key].lower())
    facts -= PUBLIC_MAKES
    facts.update(str(room.get("label", "")).lower() for room in (data.get("rooms") or {}).values())
    facts.update(name.lower() for name in (data.get("control4_rooms") or {}))
    facts.discard("")

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and "_" in key and key.islower():
                    facts.add(key)
                if key in ("id",) and isinstance(item, str) and "_" in item:
                    facts.add(item)
                if key in ("ip", "mac") and isinstance(item, str):
                    facts.add(item.lower())
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for section in ("devices", "later"):
        walk(data.get(section) or [])
    facts.update(key for key in (data.get("rooms") or {}) if "_" in key)
    facts.update(key for key in ((data.get("routing") or {}).get("sources") or {}) if "_" in key)
    facts.add(socket.gethostname().split(".")[0].lower())
    host = (yaml.safe_load(host_file.read_text(encoding="utf-8")) if host_file.exists() else None) or {}
    if host.get("state_root"):
        facts.add(str(host["state_root"]).rstrip("/").lower())
    if host.get("timezone"):
        zone = str(host["timezone"]).lower()
        facts.update({zone, zone.rsplit("/", 1)[-1].replace("_", " ")})
    # Role names of the house file's schema, not facts about this house.
    facts -= {"hdmi_input", "usb_host", "hdmi_output", "on_actions", "off_actions", "c4_item"}
    return facts - set(['', '/srv/thermite', 'apple', 'computer', 'etc/utc', 'example computer', 'example display', 'example east', 'example matrix', 'example player', 'example tv', 'example west', 'example_computer', 'example_display', 'example_matrix', 'example_player', 'example_tv', 'hdmi matrix', 'sony', 'utc'])


def private_words(path: Path = PRIVATE_WORDS) -> list[str]:
    if not path.exists():
        return []
    lines = (line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def guard(files: dict[str, bytes], facts: set[str], words: list[str] | None = None) -> list[str]:
    """What in `files` names this house: addresses (but documentation and
    loopback ones), MACs, `facts` as whole words, `words` anywhere."""
    words = private_words() if words is None else words
    problems = []
    ordered = sorted(facts, key=len, reverse=True)
    pattern = (re.compile(r"(?<![\w.:-])(" + "|".join(re.escape(f) for f in ordered) + r")(?![\w:-])")
               if ordered else None)
    for path, data in sorted(files.items()):
        if not path.endswith((".html", ".md", ".css", ".js", ".json", ".svg", ".txt")):
            continue
        text = data.decode("utf-8").lower()
        for match in _IPV4.findall(text):
            if not _SAFE_IP.fullmatch(match):
                problems.append(f"{path}: an address, {match} (use 192.0.2.x in examples)")
        for match in _MAC.findall(text):
            problems.append(f"{path}: a MAC address, {match}")
        if pattern:
            for match in sorted(set(pattern.findall(text))):
                problems.append(f"{path}: names this house: {match}")
        for word in words:
            if word in text:
                problems.append(f"{path}: a private word: {word}")
    return problems


def repository_files() -> dict[str, bytes]:
    """Everything export() writes into the thermite repository."""
    out = {f"docs/{path}": data for path, data in build().items()}
    for name in TOP:
        out[name] = (SOURCE / name).read_bytes()
    out[".github/workflows/pages.yml"] = (SOURCE / "pages.yml").read_bytes()
    return out


def compare(root: Path, files: dict[str, bytes]) -> tuple[list[str], list[str]]:
    """(paths that differ or are missing, stray files under docs/)."""
    stale = [path for path, data in files.items()
             if not (root / path).is_file() or (root / path).read_bytes() != data]
    present = {p.relative_to(root).as_posix() for p in (root / "docs").rglob("*") if p.is_file()} \
        if (root / "docs").is_dir() else set()
    stray = sorted(present - set(files))
    return sorted(stale), stray


def write(root: Path, files: dict[str, bytes]) -> tuple[list[str], list[str]]:
    stale, stray = compare(root, files)
    for path in stale:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[path])
    for path in stray:
        (root / path).unlink()
    return stale, stray
