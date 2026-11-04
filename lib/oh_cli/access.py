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
this terminal into a site. The Security page controls authentication. Local
setup issues a one-use code to create the first admin there; bootstrap instead
prompts privately and enables authentication immediately. Staging prepares
persistent code, private authority, a pinned
environment, and owner-installable units; it starts no service.

Normal deployment uses thermite install and updates use thermite upgrade.
Those commands stage and migrate the gateway, preserve callbacks and verify
the native network boundary. Stage, migrate, verify, serve and worker here
are service and recovery primitives, not a separate deployment sequence.
"""

EXAMPLES = (("thermite access login --url https://thermite.example:13600 --user operator", "save this site's personal credential privately"),
            ("thermite access setup --config /srv/thermite/config.json", "arm first admin setup on the HTTPS Security page"),
            ("thermite access migrate --config /srv/thermite/config.json", "upgrade identity schema after stopping services and backing up state"),
            ("thermite access bootstrap --config /srv/thermite/config.json --user admin", "initialize one site's first admin locally"),
            ("thermite access verify --config /srv/thermite-access/releases/current/config.json", "check service ownership and configuration as thermite before activation"),
            ("thermite access serve --config /srv/thermite/config.json", "serve the compiled site over verified TLS"),
            ("thermite access worker --config /srv/thermite/config.json", "run the single supervised room-policy worker"),
            ("thermite access prune --config /srv/thermite/config.json", "delete saved policies for rooms the house no longer has"))


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


def _site_ports():
    """A selected site's host overrides participate in separate access staging."""
    import oh_deploy
    path = oh_cli.AREA / "instance/host.yaml"
    try:
        return oh_ports.effective(oh_deploy.load_host(path))
    except FileNotFoundError:
        if not path.is_symlink():
            return None
        raise AccessError("Cannot validate staged listeners against the selected site's instance/host.yaml") from None
    except (OSError, ValueError) as exc:
        raise AccessError(f"Cannot validate staged listeners against the selected site's host file: {exc}") from None


def main(argv=None):
    parser = oh_cli.command("access", DESCRIPTION, EXAMPLES)
    commands = oh_cli.actions(parser)
    for name in ("bootstrap", "setup", "migrate", "serve", "worker", "verify", "prune"):
        sub = oh_cli.action(commands, name, {"bootstrap": "initialize the first admin locally", "setup": "issue a one-use code for first admin setup on the Security page", "migrate": "upgrade identity schema after backing up state and stopping access services", "serve": "serve one HTTPS site", "worker": "run the single room-policy worker", "verify": "verify service custody without initializing state", "prune": "delete saved policies for rooms the compiled house no longer has"}[name], examples=[example for example in EXAMPLES if " " + name + " " in example[0]])
        sub.add_argument("--config", required=True, type=Path, help="private site service configuration")
        if name == "bootstrap":
            sub.add_argument("--user", required=True, help="first admin's username")
    sub = oh_cli.action(commands, "callbacks", "run the explicitly configured plaintext UPnP NOTIFY listener", examples=[("thermite access callbacks --config /srv/thermite/release/callbacks.json", "run only verified callback paths and peers")])
    sub.add_argument("--config", required=True, type=Path, help="private mode0600 callback-only configuration")
    for name in ("login", "logout", "status"):
        sub = oh_cli.action(commands, name, {"login": "log this terminal into a site", "logout": "revoke this terminal's site credential", "status": "read this terminal's current site identity and policies"}[name], examples=[("thermite access " + name + " --url https://thermite.example:13600" + (" --user operator" if name == "login" else ""), "use this site's private terminal credential")])
        sub.add_argument("--url", metavar="URL", help="site's HTTPS origin (default: this site's verified installation)")
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
    sub.add_argument("--bind-host", default="0.0.0.0", help="HTTPS listener address (default: all IPv4 interfaces)")
    sub.add_argument("--port", type=int, default=oh_ports.ACCESS_PORT, help=f"unprivileged HTTPS port (default: {oh_ports.ACCESS_PORT})")
    sub.add_argument("--timezone", default="UTC", help="site's IANA timezone (default: UTC)")
    sub.add_argument("--service-user", dest="user", choices=("thermite",), default="thermite", help="fixed service account (thermite); ownership handoff is reported explicitly")
    sub.add_argument("--wheelhouse", type=Path, help="install pinned dependencies offline from this directory")
    sub.add_argument("--callbacks", type=Path, help="optional private mode0600 exact callback map; stage a separate disabled listener unit")
    oh_cli.add_json(sub)
    args = parser.parse_args(argv)
    try:
        if args.command in {"login", "logout", "status"}:
            from oh_access import client
            profile = client.deployment(oh_cli.AREA)
            args.url = args.url or (profile["url"] if profile else None)
            if args.url is None:
                raise AccessError("Select the installed private site with --config DIR, or supply --url")
            if args.command == "login":
                ca = args.ca_file or (profile["ca_file"] if profile and profile["url"] == client.origin(args.url) else None)
                result = client.login(args.url, args.user, hidden_password(), ca_file=ca)
            elif args.command == "logout":
                result = client.logout(args.url)
            else:
                result = client.status(args.url, rooms=args.room)
        elif args.command == "setup":
            if not sys.stdout.isatty() or not sys.stdin.isatty():
                raise AccessError("Setup codes require an interactive terminal; do not redirect them to logs")
            from oh_access.service import prepare_setup
            code = prepare_setup(args.config)
            print("Open the site's HTTPS Security page and enable authentication.")
            print("One-use setup code (expires in 15 minutes): " + code)
            return 0
        elif args.command == "migrate":
            from oh_access.service import migrate
            result = migrate(args.config)
        elif args.command == "prune":
            from oh_access.service import prune_policies
            result = prune_policies(args.config)
        elif args.command == "bootstrap":
            from oh_access.files import require_service_account
            require_service_account()
            from oh_access.service import bootstrap
            result = bootstrap(args.config, args.user, hidden_password("First admin password: ", confirm=True))
        elif args.command == "verify":
            from oh_access.staging import verify
            result = verify(args.config)
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
            values["host_ports"] = _site_ports()
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
