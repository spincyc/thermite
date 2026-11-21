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
import oh_trace
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
  - the docs' make targets: every `make TARGET` that README.md, FAQ.md,
    docs/*.md or site/pages/*.md cite in code must be one the Makefile
    defines.
"""

EXAMPLES = (("thermite lint", "scan the area's files Git has or would add, and check the docs' make targets"),)


PROGRAM = "thermite lint"


def secret_scan() -> dict:
    """{"files": how many were scanned, "findings": [what each found]}."""
    argv = ["git", "-C", str(AREA), "ls-files", "-co", "--exclude-standard", "--", "."]
    oh_trace.run(argv)
    try:
        git = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        git = subprocess.CompletedProcess(argv, 127, "", "")
    listed = git.stdout.splitlines() if git.returncode == 0 else []
    if not listed:
        listed = secrets.walk(AREA)
    return {"files": len(listed), "findings": secrets.scan(AREA, listed)}


def doc_targets() -> dict | None:
    """{"documents": how many were read, "stale": [each target the Makefile
    lacks]}, or None where there is nothing to check."""
    # Configuration-only sites have no Makefile or application command wrappers.
    import oh_paths
    if not (AREA / "Makefile").exists() and oh_paths.private(AREA):
        return None
    documents = [AREA / "README.md", AREA / "FAQ.md", *sorted((AREA / "docs").glob("*.md")),
                 *sorted((AREA / "site/pages").glob("*.md"))]
    documents = [d for d in documents if d.exists()]
    return {"documents": len(documents), "stale": oh_docs.stale_targets(AREA / "Makefile", documents)}


def checked() -> dict:
    """Both checks' reports: {"secret_scan": ..., "doc_targets": ...}."""
    return {"secret_scan": secret_scan(), "doc_targets": doc_targets()}


def status(report: dict) -> int:
    targets = report["doc_targets"]
    return 1 if report["secret_scan"]["findings"] or (targets and targets["stale"]) else 0


def said(report: dict, program: str = PROGRAM) -> None:
    """The report as text: each finding on stderr, each check's count on stdout."""
    scan, targets = report["secret_scan"], report["doc_targets"]
    for problem in scan["findings"]:
        print(f"{program}: {problem}", file=sys.stderr)
    print(f"secret scan: {scan['files']} files, {len(scan['findings'])} findings")
    if targets is not None:
        for problem in targets["stale"]:
            print(f"{program}: {problem}", file=sys.stderr)
        print(f"make targets in the docs: {targets['documents']} documents, {len(targets['stale'])} stale")


def main(argv: list[str] | None = None) -> int:
    oh_cli.command("lint", DESCRIPTION, EXAMPLES).parse_args(argv)
    report = checked()
    said(report)
    return status(report)
