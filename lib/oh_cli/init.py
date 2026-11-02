"""Create a separate private site, or explicitly select an installed upgrade."""
from pathlib import Path
import json
import sys
import oh_cli
from oh_init import initialize


def main(argv=None):
    parser = oh_cli.command("init", __doc__, (("thermite init ~/my-site", "write a fictional private site; start no service"),
                                             ("thermite init --upgrade ~/my-site", "pin an installed upgrade while preserving all configuration and prior releases")))
    parser.add_argument("directory", type=Path, help="new private site directory, or the existing site for --upgrade")
    parser.add_argument("--upgrade", action="store_true", help="update the application requirement and launcher; preserve configuration and state")
    oh_cli.add_json(parser)
    args = parser.parse_args(argv)
    try: report = initialize(args.directory, upgrade=args.upgrade)
    except (OSError, ValueError) as exc:
        print(f"thermite init: {exc}", file=sys.stderr); return 2
    if args.json: print(json.dumps(report, indent=2))
    else:
        print(f"{'Updated' if args.upgrade else 'Created'} private site {args.directory} for thermite {report['application']}.")
        print("Edit the fictional instance files, then run thermite --site DIR generate and validate. Commit generated outputs only to private Git before deployment. No service was activated.")
    return 0
