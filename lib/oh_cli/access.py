"""Local identity bootstrap, TLS service composition, worker, and site login."""

import getpass
import json
import os
from pathlib import Path
import shlex
import sys

import oh_cli
import oh_ports
from oh_access import AccessError

DESCRIPTION = """Run a site's HTTPS boundary and room-policy worker, or log
this terminal into a site. The Security page holds what guests see. Local
setup issues a one-use code to create the first admin there; bootstrap instead
prompts privately and creates it at once. If every administrator's password
is lost, reset-admin gives one existing account a new one. Staging prepares
persistent code, private authority, a pinned
environment, and owner-installable units; it starts no service.

Normal deployment uses thermite install and updates use thermite upgrade.
Those commands stage and migrate the gateway, preserve callbacks and verify
the native network boundary. Stage, migrate, verify, serve and worker here
are service and recovery primitives, not a separate deployment sequence.
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


def print_stage(result, args):
    target = Path(os.path.abspath(Path(result.get("destination", args.destination)).expanduser()))
    commands = result.get("ownership_commands", [])
    paths = result.get("custody_paths", 0)
    print("Staged release: " + str(target))
    print("Persistent state: " + str(result.get("persistent_state", args.state)))
    print(f"Custody: {paths} private paths; {len(commands)} ownership commands for thermite:thermite.")
    plan = Path(os.path.abspath(args.plan_directory.expanduser()))
    print("Full receipt: " + str(plan / "stage.json"))
    print("Review: " + shlex.join(["more", str(plan / "handoff.sh")]))
    print("Hand off and verify: " + shlex.join(["sh", str(plan / "handoff.sh")]))
    print("Review activation: " + shlex.join(["more", str(plan / "activate.sh")]))
    if result.get("bootstrap_command"):
        print("Only for a new site: " + shlex.join(result["bootstrap_command"]))
    print("Before activation:")
    for requirement in result.get("activation_requirements", ["Complete the TLS, native-listener and callback checklist in the receipt."]):
        print("  - " + requirement)
    print("After completing those checks: " + shlex.join(["sh", str(plan / "activate.sh")]))
    print("No services started; the security boundary is not active.")


def _site_host():
    """A selected site's host overrides participate in separate access
    staging: its listeners' ports and its music (oh_music.settings)."""
    import oh_deploy
    import oh_music
    path = oh_cli.AREA / "instance/host.yaml"
    try:
        host = oh_deploy.load_host(path)
        return oh_ports.effective(host), oh_music.settings(host)
    except FileNotFoundError:
        if not path.is_symlink():
            return None, None
        raise AccessError("Cannot validate staged listeners against the selected site's instance/host.yaml") from None
    except (OSError, ValueError) as exc:
        raise AccessError(f"Cannot validate staged listeners against the selected site's host file: {exc}") from None


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
                         f"takes it (default: a selected site's installed gateway, else https://thermite:{oh_ports.ACCESS_PORT})")
        if name == "login":
            sub.add_argument("--user", required=True, help="site username")
            sub.add_argument("--ca-file", type=Path, help="trusted private CA certificate file")
        if name == "status":
            sub.add_argument("--room", action="append", default=[], help="also read this room's policy (repeatable)")
        oh_cli.add_json(sub)
    sub = oh_cli.action(commands, "stage", "prepare a persistent release and owner-installable units", examples=[("thermite access stage --help", "list the explicit custody and listener inputs")])
    sub.add_argument("--destination", required=True, type=Path, help="new persistent release directory outside this checkout")
    sub.add_argument("--state", required=True, type=Path, help="site-owned mode0700 state directory outside releases and source checkouts; reuse on upgrades")
    sub.add_argument("--plan-directory", required=True, type=Path, help="new operator-owned plan directory under an existing mode0700 parent; saves private diagnostics, the receipt, handoff.sh and activate.sh")
    oh_cli.add_house(sub)
    sub.add_argument("--assets", required=True, type=Path, help="complete generated conf/html/remote directory for the selected inventory")
    sub.add_argument("--runtime-url", required=True, help="explicit native loopback HTTP origin")
    sub.add_argument("--runtime-token-file", required=True, type=Path, help="operator-owned mode0600 native bearer input; copied for service handoff")
    sub.add_argument("--tls-cert", required=True, type=Path, help="owner-managed TLS certificate chain")
    sub.add_argument("--tls-key", required=True, type=Path, help="operator-owned mode0600 TLS private key input; copied for service handoff")
    sub.add_argument("--allowed-host", required=True, action="append", dest="allowed_hosts", help="exact HTTP Host name (repeatable; no wildcard)")
    sub.add_argument("--listen", action="append", metavar="ADDRESS", help="HTTPS listener address (repeatable; default: all IPv4 interfaces)")
    sub.add_argument("--port", type=int, help=f"unprivileged HTTPS port (default: the selected site's gateway.port, else {oh_ports.ACCESS_PORT})")
    sub.add_argument("--timezone", default="UTC", help="site's IANA timezone (default: UTC)")
    sub.add_argument("--service-user", dest="user", choices=("thermite",), default="thermite", help="fixed service account (thermite); ownership handoff is reported explicitly")
    sub.add_argument("--wheelhouse", type=Path, help="install pinned dependencies offline from this directory")
    sub.add_argument("--callbacks", type=Path, help="optional private mode0600 exact callback map; stage a separate disabled listener unit")
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
            profile = tui.installed_profile()
            try:
                args.url = tui.gateway(args.url or (profile["url"] if profile else None))
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
        else:
            from oh_access.staging import stage
            values = vars(args).copy()
            values.pop("command")
            values.pop("json")
            values["host_ports"], values["music"] = _site_host()
            values["listen"] = values["listen"] or list(oh_ports.GATEWAY_LISTEN)
            if values["port"] is None:
                values["port"] = (values["host_ports"] or oh_ports.DEFAULTS)["access"]
            result = stage(**values)
        if args.command == "stage" and not args.json:
            print_stage(result, args)
        else:
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
