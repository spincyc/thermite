"""Refuse credentials and runtime state in this area's Git content, and docs
that name a make target the Makefile lacks.

    thermite lint
"""

from __future__ import annotations

import subprocess
import sys

import oh_cli
import oh_docs
import oh_secrets as secrets
from oh_cli import AREA

DESCRIPTION = """\
Refuse credentials and runtime state in this area's Git content, and docs
that name a make target the Makefile lacks. It runs two checks, and exits 1
if either finds anything:

  - the secret scan: every tracked file and every untracked file Git would
    add. Where Git lists nothing (outside a work tree, or a tree exported
    below an ignored path) it scans every file outside var/ and build/
    instead, so an empty listing never passes. A line that must carry a
    credential-looking word for a documented reason ends with the marker
    `lint: allow`.
  - the docs' make targets: every openhab-* target README.md or docs/*.md
    cites must be one the Makefile defines.
"""

EXAMPLES = (("thermite lint", "scan the area's files Git has or would add, and check the docs' make targets"),)


def secret_scan() -> int:
    argv = ["git", "-C", str(AREA), "ls-files", "-co", "--exclude-standard", "--", "."]
    oh_cli.trace_run(argv)
    try:
        git = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        git = subprocess.CompletedProcess(argv, 127, "", "")
    listed = git.stdout.splitlines() if git.returncode == 0 else []
    if not listed:
        listed = secrets.walk(AREA)
    problems = secrets.scan(AREA, listed)
    for problem in problems:
        print(f"thermite lint: {problem}", file=sys.stderr)
    print(f"secret scan: {len(listed)} files, {len(problems)} findings")
    return 1 if problems else 0


def doc_targets() -> int:
    documents = [AREA / "README.md", *sorted((AREA / "docs").glob("*.md"))]
    problems = oh_docs.stale_targets(AREA / "Makefile", [d for d in documents if d.exists()])
    for problem in problems:
        print(f"thermite lint: {problem}", file=sys.stderr)
    print(f"make targets in the docs: {len(documents)} documents, {len(problems)} stale")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    oh_cli.command("lint", DESCRIPTION, EXAMPLES).parse_args(argv)
    return max(secret_scan(), doc_targets())
