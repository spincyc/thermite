"""Find credentials before they reach Git.

The repository is private, but ADR 0082 still keeps every credential out of
it: they live in the area's ignored var/. This scan is the gate for that.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ALLOW = "lint: allow"
KEYED = re.compile(
    r"(?i)(?<![\w-])(password|passwd|passphrase|secret|token|api[_-]?key|"
    r"apitoken|psk|pre[_-]?shared[_-]?key|x-auth-psk|authorization)"
    r"[\"']?\s*[:=]\s*([\"']?)([^\"'\s,}#]*)")
PLACEHOLDER = re.compile(
    r"^(?:|\$\{[^}]*\}?|\$[A-Z_]+|<[^>]*>?|null|none|~|true|false|"
    r"[A-Za-z_]*_FILE|\*+|x+|\.\.\.)$", re.IGNORECASE)
PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openHAB API token", re.compile(r"\boh\.[A-Za-z0-9]+\.[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
)
RUNTIME = ("var/", "build/")
NEVER_CONTENT = {".git", "__pycache__"}


def scan_text(text: str, code: bool = False) -> list[tuple[int, str]]:
    """(line, reason) for every line that looks like it carries a secret.

    In source code a credential is only a quoted literal: `token = read(path)`
    names a variable, while `token = "..."` would embed a value.
    """
    if not code:
        import html
        text = html.unescape(text).replace("Bearer $(cat var/sandbox/api-token)", "<token>")
    findings: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW in line:
            continue
        for reason, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((number, reason))
        for match in KEYED.finditer(line):
            quote, value = match.group(2), match.group(3)
            if code and not quote:
                continue
            if len(value) >= 4 and not PLACEHOLDER.match(value):
                findings.append((number, f"literal value for '{match.group(1)}'"))
    return findings


def is_code(path: Path, text: str) -> bool:
    first = text.split("\n", 1)[0]
    return path.suffix == ".py" or (first.startswith("#!") and "python" in first)


def walk(area: Path) -> list[str]:
    """Area-relative files outside runtime state, for a tree Git does not list."""
    files: list[str] = []
    for top, dirs, names in os.walk(area):
        relative_top = Path(top).relative_to(area).as_posix()
        prefix = "" if relative_top == "." else relative_top + "/"
        dirs[:] = sorted(d for d in dirs if d not in NEVER_CONTENT
                         and not (prefix + d + "/").startswith(RUNTIME))
        files.extend(prefix + name for name in sorted(names))
    return files


def scan(area: Path, paths: list[str]) -> list[str]:
    """Problems for the given area-relative paths (tracked or about to be)."""
    problems: list[str] = []
    for relative in paths:
        if relative.startswith(RUNTIME):
            problems.append(f"{relative}: runtime state must stay ignored")
            continue
        path = Path(area) / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for number, reason in scan_text(text, code=is_code(path, text)):
            problems.append(f"{relative}:{number}: {reason}")
    return problems
