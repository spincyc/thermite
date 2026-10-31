"""Documented `make` targets must exist.

The factory's doc-make-target-drift does this for homelab-* targets; this
area's targets are openhab-*, so it carries its own small equivalent.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFINED = re.compile(r"^([A-Za-z][\w-]*)\s*:(?!=)", re.MULTILINE)
#: A systemd unit's name (openhab-atv-restart.path) is not a target.
CITED = re.compile(r"(?<![/\w-])(openhab-[a-z0-9-]+)(?![a-z0-9-]|\.(?:service|path|container|timer)\b)")


def stale_targets(makefile: Path, documents: list[Path]) -> list[str]:
    defined = set(DEFINED.findall(Path(makefile).read_text(encoding="utf-8")))
    problems: list[str] = []
    for document in documents:
        lines = Path(document).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            for name in CITED.findall(line):
                name = name.rstrip("-")
                if name in defined or any(t.startswith(name + "-") for t in defined):
                    continue
                if (Path(makefile).parent / "bin" / name).exists():
                    continue
                problems.append(f"{document}:{number}: make target {name} "
                                "is not defined")
    return problems
