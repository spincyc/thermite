"""Local identity bootstrap, TLS service composition, worker, and site login."""

import getpass
import json
from pathlib import Path
import sys

import oh_cli
from oh_access import AccessError

DESCRIPTION = """Run a site's authenticated HTTPS boundary and room-policy worker, or log
this terminal into a site. Bootstrap is local and prompts privately for the first
admin password. Staging prepares persistent code, private authority, a pinned
environment, and owner-installable units; it starts no service.

The gateway is not an effective authorization boundary until every bypassing
native listener is sealed and device callbacks are preserved. Staging reports
that owner activation checklist explicitly.
"""

EXAMPLES = (("thermite access login --url https://thermite.example:9443 --user operator", "save this site's personal credential privately"),
            ("thermite access bootstrap --config /srv/thermite/config.json --user admin", "initialize one site's first admin locally"),
            ("thermite access serve --config /srv/thermite/config.json", "serve the compiled site over verified TLS"),
            ("thermite access worker --config /srv/thermite/config.json", "run the single supervised room-policy worker"))


def hidden_password(prompt="Password: ", *, confirm=False):
    if not sys.stdin.isatty():
        raise AccessError("Password entry requires an interactive terminal")
    secret = getpass.getpass(prompt)
    if confirm and (len(secret) < 12 or secret != getpass.getpass("Confirm password: ")):
        raise AccessError("Passwords must match and contain at least 12 characters")
    return secret


def main(argv=None):
    parser = oh_cli.command("access", DESCRIPTION, EXAMPLES)
    commands = oh_cli.actions(parser)
    for name in ("bootstrap", "serve", "worker"):
        sub = oh_cli.action(commands, name, {"bootstrap": "initialize the first admin locally", "serve": "serve one HTTPS site", "worker": "run the single room-policy worker"}[name], examples=[example for example in EXAMPLES if " " + name + " " in example[0]])
        sub.add_argument("--config", required=True, type=Path, help="private site service configuration")
        if name == "bootstrap":
            sub.add_argument("--user", required=True, help="first admin's username")
    sub = oh_cli.action(commands, "callbacks", "run the explicitly configured plaintext UPnP NOTIFY listener", examples=[("thermite access callbacks --config /srv/thermite/release/callbacks.json", "run only verified callback paths and peers")])
    sub.add_argument("--config", required=True, type=Path, help="private mode0600 callback-only configuration")
    for name in ("login", "logout", "status"):
        sub = oh_cli.action(commands, name, {"login": "log this terminal into a site", "logout": "revoke this terminal's site credential", "status": "read this terminal's current site identity and policies"}[name], examples=[("thermite access " + name + " --url https://thermite.example:9443" + (" --user operator" if name == "login" else ""), "use this site's private terminal credential")])
        sub.add_argument("--url", required=True, metavar="URL", help="site's HTTPS origin")
        if name == "login":
            sub.add_argument("--user", required=True, help="site username")
            sub.add_argument("--ca-file", type=Path, help="trusted private CA certificate file")
        if name == "status":
            sub.add_argument("--room", action="append", default=[], help="also read this room's policy (repeatable)")
        oh_cli.add_json(sub)
    sub = oh_cli.action(commands, "stage", "prepare a persistent release and owner-installable units", examples=[("thermite access stage --help", "list the explicit custody and listener inputs")])
    sub.add_argument("--destination", required=True, type=Path, help="new persistent release directory outside this checkout")
    sub.add_argument("--state", required=True, type=Path, help="site-owned mode0700 state directory outside releases and source checkouts; reuse on upgrades")
    oh_cli.add_house(sub)
    sub.add_argument("--assets", required=True, type=Path, help="complete deployed native conf/html/remote directory")
    sub.add_argument("--runtime-url", required=True, help="explicit native loopback HTTP origin")
    sub.add_argument("--runtime-token-file", required=True, type=Path, help="service-owned mode0600 native bearer file")
    sub.add_argument("--tls-cert", required=True, type=Path, help="owner-managed TLS certificate chain")
    sub.add_argument("--tls-key", required=True, type=Path, help="service-owned mode0600 TLS private key")
    sub.add_argument("--allowed-host", required=True, action="append", dest="allowed_hosts", help="exact HTTP Host name (repeatable; no wildcard)")
    sub.add_argument("--bind-host", default="0.0.0.0", help="HTTPS listener address (default: all IPv4 interfaces)")
    sub.add_argument("--port", type=int, default=9443, help="unprivileged HTTPS port (default: 9443)")
    sub.add_argument("--timezone", default="UTC", help="site's IANA timezone (default: UTC)")
    sub.add_argument("--service-user", dest="user", help="existing unprivileged unit user (default: current user)")
    sub.add_argument("--wheelhouse", type=Path, help="install pinned dependencies offline from this directory")
    sub.add_argument("--callbacks", type=Path, help="optional private mode0600 exact callback map; stage a separate disabled listener unit")
    oh_cli.add_json(sub)
    args = parser.parse_args(argv)
    try:
        if args.command in {"login", "logout", "status"}:
            from oh_access import client
            if args.command == "login":
                result = client.login(args.url, args.user, hidden_password(), ca_file=args.ca_file)
            elif args.command == "logout":
                result = client.logout(args.url)
            else:
                result = client.status(args.url, rooms=args.room)
        elif args.command == "bootstrap":
            from oh_access.service import bootstrap
            result = bootstrap(args.config, args.user, hidden_password("First admin password: ", confirm=True))
        elif args.command == "serve":
            from oh_access.service import serve
            serve(args.config)
            return 0
        elif args.command == "worker":
            from oh_access.worker import run
            run(args.config)
            return 0
        elif args.command == "callbacks":
            from oh_access.callback_service import serve
            serve(args.config)
            return 0
        else:
            from oh_access.staging import stage
            values = vars(args).copy()
            values.pop("command")
            values.pop("json")
            result = stage(**values)
        print(json.dumps(result, indent=2) if getattr(args, "json", False) else
              json.dumps(result, indent=2))
        return 0
    except AccessError as exc:
        print("thermite access: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Do not print exceptions from dependencies, TLS clients, or password APIs.
        print("thermite access: operation refused; check configuration, custody permissions, credentials, and HTTPS trust", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
