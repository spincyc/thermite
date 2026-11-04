"""Documented `make` targets must exist (bin/thermite lint).

Every `make TARGET` the docs cite in code (an inline code span or a fenced
block) must name a target the Makefile defines; prose such as "make sure"
is not a command.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFINED = re.compile(r"^([A-Za-z][\w.-]*)\s*:(?!=)", re.MULTILINE)
SPAN = re.compile(r"`([^`]+)`")
FENCE = re.compile(r"^\s*(```|~~~)")
#: `make`, then its words; VAR=value, -switches and paths are not targets.
CITED = re.compile(r"(?<![\w/.-])make((?:[ \t]+[\w./=*-]+)+)")
#: A target, or with a trailing * every target it begins (make sandbox-*).
TARGET = re.compile(r"[A-Za-z][\w.-]*\*?")


def cited(text: str) -> list[tuple[int, str]]:
    """(line number, target) for every make target cited in code in a Markdown text."""
    found: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        for code in [line] if fenced else SPAN.findall(line):
            for words in CITED.findall(code):
                found += [(number, word) for word in words.split() if TARGET.fullmatch(word)]
    return found


def stale_targets(makefile: Path, documents: list[Path]) -> list[str]:
    defined = set(DEFINED.findall(Path(makefile).read_text(encoding="utf-8")))
    return [f"{document}:{number}: make target {name} is not defined"
            for document in documents
            for number, name in cited(Path(document).read_text(encoding="utf-8"))
            if name not in defined and not (name.endswith("*") and any(t.startswith(name[:-1]) for t in defined))]
