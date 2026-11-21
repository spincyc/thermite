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
# The tokens' leading word boundary (\boh, \bgh) is checked just after the
# literal, as "not after a word character", so that re can skip ahead to each
# "oh" or "gh": the same matches as with \b first, far faster over a tree.
PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openHAB API token", re.compile(r"oh(?<!\woh)\.[A-Za-z0-9]+\.[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"gh(?<!\wgh)[pousr]_[A-Za-z0-9]{30,}")),
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
    return path.suffix in {".py", ".js", ".cjs", ".java"} or (first.startswith("#!") and "python" in first)


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

#: redact(): a secret's value in text meant to be read or shared (logs),
#: where scan_text() only reports a line. The key or scheme stays, so the
#: reader still sees what was there.
_PEM = re.compile(r"(-----BEGIN ([A-Z ]*)PRIVATE KEY-----).*?(?:(-----END \2PRIVATE KEY-----)|\Z)", re.DOTALL)
_COOKIE = re.compile(r"(?im)(?<![\w-])((?:set-)?cookie\s*:[ \t]*)([^\r\n]+)")
_AUTHORIZATION = re.compile(r"(?i)(?<![\w-])((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)(\w+)(\s+)([^\s\"',;]+)")
#: A bearer token without its header, as a command line or an error quotes it.
_BEARER = re.compile(r"(?i)(?<![\w-])(bearer)(\s+)([A-Za-z0-9._~+/=-]{8,})")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):([^/\s@]+)@")
_REDACTED_KEYS = (r"password|passwd|passphrase|secret|token|api[_-]?key|apitoken|psk|pre[_-]?shared[_-]?key|"
                  r"authorization|cookie|session(?:[_-]?id)?")
#: A quoted value ends at its first unescaped quote, or at the end of its
#: line; an object or array is hidden whole, and one nested deeper than
#: this can match hides the rest of its line.
_KEYED_VALUE = re.compile(
    r"(?i)(?<![\w.-])(?P<key>[\w.-]*?(?:" + _REDACTED_KEYS + r"))(?P<sep>\"?'?\s*[:=]\s*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>(?:(?!(?P=quote))[^\\\r\n]|\\.)*)(?P<end>(?P=quote))?"
    r"|(?P<nested>\{[^{}]*\}|\[[^\[\]]*\]|[{\[][^\r\n]*)|(?P<bare>[^\s\"',;&}\]]+))")
_SCHEMES = frozenset(("bearer", "basic", "digest"))
_OPENHAB_TOKEN = dict(PATTERNS)["openHAB API token"]
_GITHUB_TOKEN = dict(PATTERNS)["GitHub token"]
MASK = "***"


def _keyed(match: re.Match) -> str:
    if match["nested"] is not None:
        return match.group(0) if match["nested"] in ("{}", "[]") else f"{match['key']}{match['sep']}{MASK}"
    value = match["quoted"] if match["quote"] else match["bare"]
    if PLACEHOLDER.match(value) or value.lower() in _SCHEMES or MASK in value:
        return match.group(0)
    return f"{match['key']}{match['sep']}{match['quote'] or ''}{MASK}{match['end'] or ''}"


def unclosed_key(text: str) -> bool:
    """Whether text ends inside a private key block: its last BEGIN line has
    no END line after it, so the lines that follow are the key's."""
    last = None
    for match in _PEM.finditer(text):
        last = match
    return last is not None and last.group(3) is None


def redact(text: str) -> str:
    """text with every secret value scan_text() would look for replaced by
    ***: private key blocks, cookies, Authorization credentials, a URL's
    password, openHAB and GitHub tokens, and the value of any key naming a
    password, secret, token, key, cookie or session. Addresses, host names
    and device names stay: they are what a log is read for."""
    text = _PEM.sub(lambda m: f"{m.group(1)}\n{MASK}\n{m.group(3) or ''}".rstrip("\n"), text)
    text = _COOKIE.sub(lambda m: m.group(1) + MASK, text)
    text = _AUTHORIZATION.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _USERINFO.sub(lambda m: f"{m.group(1)}:{MASK}@", text)
    text = _OPENHAB_TOKEN.sub(lambda m: m.group(0).rsplit(".", 1)[0] + "." + MASK, text)
    text = _GITHUB_TOKEN.sub(lambda m: m.group(0)[:4] + MASK, text)
    return _KEYED_VALUE.sub(_keyed, text)
