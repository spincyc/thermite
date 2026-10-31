"""Generate conf/ from instance/devices.yaml, metrics/ from it and
instance/host.yaml, and deploy/'s systemd units from versions.json and
instance/host.yaml; never edit any of them by hand.

    thermite generate [--house FILE] [--conf DIR]
                     validate the house file, then write every file under
                     conf/, metrics/ and deploy/ that differs
    thermite generate --check [--house FILE] [--conf DIR]
                     validate, then exit 1 naming every file under conf/,
                     metrics/ or deploy/ that is stale, missing or not
                     generated

Writing never deletes: a file under conf/ or metrics/ that nothing
generates is reported and makes the exit status 1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
import oh_inventory as inventory
import oh_metrics as metrics
import oh_render as render
from oh_cli import AREA

DESCRIPTION = """\
Generate conf/ from the house file (instance/devices.yaml), metrics/ (the
Prometheus, blackbox_exporter, Grafana and exporter configuration, and the
dashboards) from the house file and the host file (instance/host.yaml), and
deploy/'s systemd units from versions.json and the host file. All three are
generated: change the house file, the host file, versions.json or lib/,
then run this; never edit conf/, metrics/ or deploy/ by hand.

It validates the house file first and writes nothing while it has a
problem. Writing never deletes: a file under conf/ or metrics/ that nothing
generates is reported and makes the exit status 1.
"""

EXAMPLES = (
    ("thermite generate", "validate the house file, then write every file under conf/, metrics/ and deploy/ "
                          "that differs"),
    ("thermite generate --check", "change nothing; name every stale, missing or stray file"),
)

NOTES = """\
exit status:
  0  conf/, metrics/ and deploy/ are what the house file, the host file and
     versions.json generate (with --check: and nothing was written)
  1  the house file has a problem, a file is stale or missing (with
     --check), or conf/ or metrics/ holds a file nothing generates
  2  a wrong switch, or a file that cannot be read
"""


def stale_units(units: dict[str, str], root: Path) -> list[str]:
    return [name for name, text in units.items()
            if not (root / name).exists() or (root / name).read_text(encoding="utf-8") != text]


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("generate", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_check(parser, "conf/, metrics/ or deploy/")
    oh_cli.add_house(parser, render.INVENTORY)
    oh_cli.add_conf(parser, render.CONF)
    # deploy/'s and metrics/'s parent, so that the tests can write them somewhere else.
    parser.add_argument("--root", type=Path, default=AREA, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    data = inventory.load(arguments.house)
    problems = render.validate(data)
    for problem in problems:
        print(f"thermite generate: {problem}", file=sys.stderr)
    if problems:
        print(f"generate: {len(problems)} problems in {arguments.house.name}; nothing generated")
        return 1
    files = render.render(data)
    devices = len(data.get("devices") or [])
    versions, host = deploy.load(AREA)
    units = deploy.render_units(versions, host)
    root = arguments.root
    trees = [(arguments.conf, arguments.conf.name, files,
              "conf/ holds only generated files (add it to a module under lib/, or delete it)"),
             (root / "metrics", "metrics", metrics.render(data, host, versions),
              "metrics/ holds only generated files (add it to lib/oh_metrics, or delete it)")]
    stale: list[str] = []
    missing: list[str] = []
    stray: list[tuple[str, str]] = []
    written: list[str] = []
    for directory, shown, tree, why in trees:
        if arguments.check:
            old, absent, extra = render.compare(directory, tree)
            for path in old:
                print(f"thermite generate: {shown}/{path} is stale; run bin/thermite generate", file=sys.stderr)
            for path in absent:
                print(f"thermite generate: {shown}/{path} is missing; run bin/thermite generate", file=sys.stderr)
            stale += old
            missing += absent
        else:
            for path in render.write(directory, tree):
                print(f"wrote {shown}/{path}")
                written.append(path)
            extra = render.compare(directory, tree)[2]
        stray += [(f"{shown}/{path}", why) for path in extra]
    if arguments.check:
        stale_unit_names = stale_units(units, root)
        for name in stale_unit_names:
            print(f"thermite generate: {name} is stale; run bin/thermite generate", file=sys.stderr)
    else:
        stale_unit_names = []
        written_units = stale_units(units, root)
        for name in written_units:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(units[name], encoding="utf-8")
            print(f"wrote {name}")
    for path, why in stray:
        print(f"thermite generate: {path} is not generated; {why}", file=sys.stderr)
    count = len(files) + len(trees[1][2])
    summary = f"generate: {devices} devices, {count} files, {len(units)} units"
    if arguments.check:
        summary += (f", {len(stale)} stale, {len(missing)} missing, {len(stray)} stray, "
                    f"{len(stale_unit_names)} stale units")
    else:
        summary += f", {len(written)} written, {len(stray)} stray, {len(written_units)} units written"
    print(summary)
    return 1 if stale or missing or stray or stale_unit_names else 0
