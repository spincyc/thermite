"""Build the reviewed portable application wheel; never package the private inventory."""
from pathlib import Path
import json
import sys
import oh_cli
import oh_package


def main(argv=None):
    parser = oh_cli.command("package", __doc__, (("thermite package --out .scratch/releases", "privacy-check portable source and build a deterministic wheel"),))
    parser.add_argument("--out", type=Path, required=True, help="owned directory for the wheel and release receipt")
    args = parser.parse_args(argv)
    try:
        public = oh_package.public_files()
        wheel, release = oh_package.wheel(public, args.out)
        (args.out / "release.json").write_text(json.dumps(release, indent=2) + "\n")
    except (OSError, ValueError) as exc:
        print(f"thermite package: {exc}", file=sys.stderr); return 2
    print(f"wrote {wheel} (application {release['application']}, release {release['digest'][:12]})")
    return 0
