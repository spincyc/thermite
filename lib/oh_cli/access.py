"""Local identity bootstrap, TLS service composition, worker, and site login."""

import getpass
import json
from pathlib import Path
import sys

import oh_cli
import oh_ports
from oh_access import AccessError

DESCRIPTION = """Run a site's HTTPS boundary and room-policy worker, or log
this terminal into a site. The Security page holds what guests see. Local
setup issues a one-use code to create the first admin there; bootstrap instead
prompts privately and creates it at once. If every administrator's password
is lost, reset-admin gives one existing account a new one.

Normal deployment uses thermite install and updates use thermite upgrade.
Those commands stage, hand over, migrate and start the gateway, preserve
callbacks and verify the native network boundary; no action here stages a
release or installs a unit. Migrate, verify, serve and worker are what its
services and the installer run, not a separate deployment sequence.
"""

#: The terminal's subcommands: like thermite tui, they need no private site.
TERMINAL = ("login", "logout", "status")
#: --descriptor's help in the actions that run as the service account. It is
#: not spelled --config: that selects the private site in every verb, before
#: any parser runs (oh_paths.select_args).
SERVICE_DESCRIPTOR = "the release's private service descriptor (its config.json)"
EXAMPLES = (("thermite access login --url https://thermite.example:13601 --user operator", "save this site's personal credential privately"),
            ("thermite access setup --descriptor /srv/thermite/access/releases/RELEASE/config.json", "arm first admin setup on the HTTPS Security page"),
            ("thermite access migrate --descriptor /srv/thermite/access/releases/RELEASE/config.json", "upgrade identity schema after stopping services and backing up state"),
            ("thermite access bootstrap --descriptor /srv/thermite/access/releases/RELEASE/config.json --user admin", "initialize one site's first admin locally"),
            ("thermite access reset-admin --descriptor /srv/thermite/access/releases/RELEASE/config.json --user admin", "give an account a new password and make it an administrator, after a lost password"),
            ("thermite access verify --descriptor /srv/thermite/access/releases/RELEASE/config.json", "check service ownership and configuration as thermite before activation"),
            ("thermite access serve --descriptor /srv/thermite/access/releases/RELEASE/config.json", "serve the compiled site over verified TLS"),
            ("thermite access worker --descriptor /srv/thermite/access/releases/RELEASE/config.json", "run the single supervised room-policy worker"),
            ("thermite access prune --descriptor /srv/thermite/access/releases/RELEASE/config.json", "delete saved policies for rooms the house no longer has"),
            ("thermite access audit --descriptor /srv/thermite/access/releases/RELEASE/config.json --since 7d --action login", "read the security audit as the service account"),
            ("thermite access music-tidal --descriptor /srv/thermite/access/releases/RELEASE/config.json", "sign Music Assistant into TIDAL as the service account"))


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
    for name in ("bootstrap", "setup", "migrate", "serve", "worker", "verify", "prune"):
        sub = oh_cli.action(commands, name, {"bootstrap": "initialize the first admin locally", "setup": "issue a one-use code for first admin setup on the Security page", "migrate": "upgrade identity schema after backing up state and stopping access services", "serve": "serve one HTTPS site", "worker": "run the single room-policy worker", "verify": "verify service custody without initializing state", "prune": "delete saved policies for rooms the compiled house no longer has"}[name], examples=[example for example in EXAMPLES if " " + name + " " in example[0]])
        sub.add_argument("--descriptor", required=True, type=Path, metavar="FILE", help=SERVICE_DESCRIPTOR)
        if name == "bootstrap":
            sub.add_argument("--user", required=True, help="first admin's username")
    sub = oh_cli.action(commands, "reset-admin", "recover from a lost administrator password: set a new one on an "
                        "existing account and make it an active administrator",
                        description="""Run this on the host as the thermite service account, which already owns the site's
accounts and keys, with the gateway running or stopped. It asks for the new
password at the terminal, ends that account's sessions and tokens, changes no
other account, lock, setting or pairing, and is recorded in the security audit
as user.reset_admin by a local principal.""",
                        examples=[example for example in EXAMPLES if " reset-admin " in example[0]])
    sub.add_argument("--descriptor", required=True, type=Path, metavar="FILE", help=SERVICE_DESCRIPTOR)
    sub.add_argument("--user", required=True, help="the existing account's username")
    sub = oh_cli.action(commands, "audit", "read the security audit (logins, tokens, accounts, locks, what guests see)",
                        examples=[example for example in EXAMPLES if " audit " in example[0]])
    sub.add_argument("--descriptor", required=True, type=Path, metavar="FILE", help=SERVICE_DESCRIPTOR)
    sub.add_argument("--since", metavar="WHEN", help="only rows since 30m, 12h, 7d, or an ISO date or time (site timezone)")
    sub.add_argument("--action", action="append", default=[], help="only this action or family, such as login, token or room.lock (repeatable)")
    oh_cli.add_json(sub)
    sub = oh_cli.action(commands, "music-tidal", "sign the site's Music Assistant into TIDAL: show a code to approve at "
                        "link.tidal.com and wait", examples=[example for example in EXAMPLES if " music-tidal " in example[0]])
    sub.add_argument("--descriptor", required=True, type=Path, metavar="FILE", help=SERVICE_DESCRIPTOR)
    sub = oh_cli.action(commands, "callbacks", "run the explicitly configured plaintext UPnP NOTIFY listener", examples=[("thermite access callbacks --descriptor /srv/thermite/access/releases/RELEASE/callbacks.json", "run only verified callback paths and peers")])
    sub.add_argument("--descriptor", required=True, type=Path, metavar="FILE",
                     help="the release's private mode0600 callback-only descriptor (its callbacks.json)")
    for name in TERMINAL:
        sub = oh_cli.action(commands, name, {"login": "log this terminal into a site", "logout": "revoke this terminal's site credential", "status": "read this terminal's current site identity and policies"}[name], examples=[("thermite access " + name + " --url https://thermite.example:13601" + (" --user operator" if name == "login" else ""), "use this site's private terminal credential")])
        sub.add_argument("--url", metavar="URL", help="the gateway as https://HOST:PORT, HOST:PORT or HOST, as thermite tui "
                         f"takes it (default: {oh_cli.URL_VARIABLE} when it names a server, else a selected site's "
                         f"installed gateway, else https://thermite:{oh_ports.ACCESS_PORT})")
        if name == "login":
            sub.add_argument("--user", required=True, help="site username")
            sub.add_argument("--ca-file", type=Path, help="trusted private CA certificate file")
        if name == "status":
            sub.add_argument("--room", action="append", default=[], help="also read this room's policy (repeatable)")
        oh_cli.add_json(sub)
    args = parser.parse_args(argv)
    if args.command not in TERMINAL:
        # The server's subcommands run on its private site, so they check it
        # as a site verb's parser does; the terminal's need none.
        import oh_paths
        try:
            oh_paths.require_compatible()
        except ValueError as exc:
            return oh_cli.usage_error(str(exc))
    try:
        if args.command in TERMINAL:
            from oh_access import client
            from oh_cli import tui
            # The gateway and its trust as thermite tui finds them, so both
            # use the one credential saved for that address.
            try:
                args.url = tui.resolve(args.url)
            except ValueError as exc:
                raise AccessError(str(exc)) from None
            if args.command == "login":
                try:
                    ca, _ = tui.trust(args.url, args.ca_file)
                except tui.Problem as exc:
                    raise AccessError("; ".join(exc.args)) from None
                result = client.login(args.url, args.user, hidden_password(), ca_file=ca)
            elif args.command == "logout":
                result = client.logout(args.url)
            else:
                result = client.status(args.url, rooms=args.room)
        elif args.command == "setup":
            if not sys.stdout.isatty() or not sys.stdin.isatty():
                raise AccessError("Setup codes require an interactive terminal; do not redirect them to logs")
            from oh_access.service import prepare_setup
            code = prepare_setup(args.descriptor)
            print("Open the site's HTTPS Security page and create the first administrator.")
            print("One-use setup code (expires in 15 minutes): " + code)
            return 0
        elif args.command == "migrate":
            from oh_access.service import migrate
            result = migrate(args.descriptor)
        elif args.command == "prune":
            from oh_access.service import prune_policies
            result = prune_policies(args.descriptor)
        elif args.command == "music-tidal":
            from oh_access.files import require_service_account
            require_service_account()
            from oh_music import tidal
            return tidal.sign_in(*tidal.site(args.descriptor))
        elif args.command == "audit":
            from oh_access import audit
            result = audit.rows(args.descriptor, after=args.since, actions=args.action)
            if not args.json:
                for entry in result:
                    print(audit.text(entry))
                return 0
        elif args.command == "bootstrap":
            from oh_access.files import require_service_account
            require_service_account()
            from oh_access.service import bootstrap
            result = bootstrap(args.descriptor, args.user, hidden_password("First admin password: ", confirm=True))
        elif args.command == "reset-admin":
            from oh_access.files import require_service_account
            require_service_account()
            from oh_access.service import reset_admin
            result = reset_admin(args.descriptor, args.user, hidden_password("New administrator password: ", confirm=True))
        elif args.command == "verify":
            from oh_access.staging import verify
            result = verify(args.descriptor)
        elif args.command == "serve":
            from oh_access.service import serve
            serve(args.descriptor)
            return 0
        elif args.command == "worker":
            from oh_access.worker import run
            run(args.descriptor)
            return 0
        elif args.command == "callbacks":
            from oh_access.callback_service import serve
            serve(args.descriptor)
            return 0
        print(json.dumps(result, indent=2))
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
