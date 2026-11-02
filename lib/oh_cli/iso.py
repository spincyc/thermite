"""Build a bootable Thermite installer; disk selection happens in its UI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import oh_cli
import oh_iso


DESCRIPTION = """Build a hybrid BIOS/UEFI Ubuntu Server installer containing the reviewed
Thermite application. It asks for disk, network and owner account settings.
No private site or credentials are embedded. Runtime commissioning follows
first boot and needs Internet access. Building requires xorriso; no root.

fetch downloads the pinned base ISO and Python dependency over HTTPS and
checks SHA256. build uses those cached inputs and writes an ISO plus a
checksum and provenance receipt. Existing outputs are never overwritten.
"""

EXAMPLES = (
    ("thermite iso fetch --cache .scratch/iso/cache", "download and verify fixed inputs"),
    ("thermite iso build --cache .scratch/iso/cache --work .scratch/iso/work --out .scratch/iso/thermite.iso",
     "build the bootable installer and checksum"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("iso", DESCRIPTION, EXAMPLES)
    actions = oh_cli.actions(parser)
    fetch = oh_cli.action(actions, "fetch", "download and verify pinned installer inputs", examples=[EXAMPLES[0]])
    fetch.add_argument("--cache", type=Path, required=True, metavar="DIR", help="directory for verified input downloads")
    build = oh_cli.action(actions, "build", "build a bootable ISO from the verified cache", examples=[EXAMPLES[1]])
    build.add_argument("--cache", type=Path, required=True, metavar="DIR", help="directory containing verified inputs")
    build.add_argument("--work", type=Path, required=True, metavar="DIR", help="disposable staging directory")
    build.add_argument("--out", type=Path, required=True, metavar="FILE", help="new output ISO filename")
    build.add_argument("--xorriso", type=Path, help="explicit xorriso executable")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch": result = oh_iso.fetch(args.cache)
        else: result = oh_iso.build(args.cache, args.out, args.work, xorriso=args.xorriso)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"thermite iso: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0
