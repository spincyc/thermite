"""Validate a learned IR profile or print a packet without transmitting."""
from __future__ import annotations

from pathlib import Path
import sys

import oh_cli

DESCRIPTION = """Check a model-matched learned/verified Sony IR profile. Show its verified,
pending and missing keys. --dry-run prints the packet the native iTach
IP2IR binding would send; it never connects to an emitter or transmits IR.
No code, endpoint, device power or playback state is inferred."""
EXAMPLES = (("thermite ir-profile instance/ir/sony-bluray.json", "validate the profile without transmitting"),
            ("thermite ir-profile instance/ir/sony-bluray.json --dry-run --key play --connector 2", "print the exact packet only"),
            ("thermite ir-profile instance/ir/sony-bluray.json --json", "the keys (and any packet) as JSON"))


def main(argv: list[str] | None = None) -> int:
    from oh_integrations import globalcache_ir as ir
    parser = oh_cli.command("ir-profile", DESCRIPTION, EXAMPLES)
    parser.add_argument("profile", type=Path, help="model-matched learned/verified Sony profile JSON file")
    oh_cli.add_json(parser)
    oh_cli.add_dry_run(parser)
    parser.add_argument("--key", choices=sorted(ir.KEYS), help="verified player key to print with --dry-run")
    parser.add_argument("--connector", type=int, choices=(1, 2, 3), help="iTach IR connector for --dry-run")
    args = parser.parse_args(argv)
    try:
        profile = ir.load_profile(args.profile)
        result = {"model": profile["model"], "verified": sorted(profile["commands"]),
                  "pending": profile["pending"], "missing": profile["missing"], "transmitted": False}
        if args.dry_run:
            if args.key not in profile["commands"] or args.connector is None:
                raise ir.ProfileError("dry-run requires a verified --key and explicit --connector")
            result["packet"] = ir.dry_packet(profile["commands"][args.key], args.connector)
        elif args.key is not None or args.connector is not None:
            raise ir.ProfileError("--key and --connector are only used with --dry-run")
    except ir.ProfileError as error:
        print(f"thermite ir-profile: {error}", file=sys.stderr)
        return 2
    if args.json:
        oh_cli.emit(result)
        return 0
    print(f"model    {result['model']}")
    for key in ("verified", "pending", "missing"):
        print(f"{key:<9}{', '.join(result[key]) if result[key] else '(none)'}")
    if "packet" in result:
        print(f"packet   {result['packet']}")
    print("transmitted nothing")
    return 0
