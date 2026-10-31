"""thermite and runtime versions: pinned and running here.

    thermite version [--json] [--timeout SECONDS]
"""

from __future__ import annotations

import json
import sys

import oh_cli
from oh_cli import AREA

DESCRIPTION = """\
Print thermite's version and the commit of this checkout it runs from
(`, modified` when the area's tracked files differ from it), the runtime
version versions.json pins, and the one production runtime on this host
reports, or that nothing answers there. thermite --version (-V) prints only
the first line; without Git, or outside a repository, it has no commit.
"""

EXAMPLES = (
    ("thermite version", "thermite's version, the pinned runtime and the one running here"),
    ("thermite version --json", "the same, as JSON"),
    ("thermite --version", "only thermite's"),
)


def report(timeout: float) -> dict:
    found = oh_cli.commit()
    pinned = json.loads((AREA / "versions.json").read_text(encoding="utf-8"))["openhab"]["version"]
    url = running = None
    try:
        import oh_deploy as deploy

        url = deploy.production_url(deploy.load(AREA)[1])
        running = deploy.openhab_version(url, timeout=timeout)
    except (ImportError, OSError, KeyError) as exc:
        print(f"thermite version: cannot tell where production listens: {exc}", file=sys.stderr)
    return {"thermite": oh_cli.VERSION, "commit": found[0] if found else None,
            "modified": found[1] if found else None,
            "openhab": {"pinned": pinned, "production": running, "url": url}}


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("version", DESCRIPTION, EXAMPLES)
    oh_cli.add_json(parser)
    oh_cli.add_timeout(parser, 2, "production runtime to answer")
    args = parser.parse_args(argv)
    found = report(args.timeout)
    if args.json:
        json.dump(found, sys.stdout, indent=2)
        print()
        return 0
    commit = f" ({found['commit'][:12]}{', modified' if found['modified'] else ''})" if found["commit"] else ""
    openhab = found["openhab"]
    print(f"thermite {found['thermite']}{commit}")
    print(f"runtime  {openhab['pinned']} pinned (versions.json)")
    if openhab["url"]:
        print(f"         {openhab['production'] + ' answers' if openhab['production'] else 'not answering'} "
              f"in production on this host, {openhab['url']}")
    return 0
