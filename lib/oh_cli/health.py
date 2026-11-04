"""Read-only health check of the running runtime (sandbox or production)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import oh_cli
import oh_deploy as deploy
import oh_entry
import oh_http
import oh_ports
import oh_sandbox as lib
from oh_cli import AREA

SANDBOX_TOKEN = AREA / "var/sandbox/api-token"

DESCRIPTION = f"""\
Read-only checks against the running runtime: every Thing ONLINE, Items whose
state is NULL or UNDEF, and the installed add-ons (compared with an
addons.cfg when --addons is given). Only GET and HEAD requests are sent.
An online HEOS bridge without either optional account field reports its
missing-account diagnostic as a visible warning, preserving local transport.

The runtime answers /rest/things and /rest/addons only with credentials; pass
an API token file (the sandbox writes var/sandbox/api-token). --sandbox
checks the sandbox on {oh_ports.SANDBOX_URL.removeprefix('http://')}, with that token when it exists.
--production checks production runtime on this host, on 127.0.0.1 at
instance/host.yaml's http_port, with the token thermite install saved in
var/<this host's name>/api-token, and the metrics services beside it:
Prometheus ready with every scrape target up, and Grafana answering.
Before thermite upgrade has installed them it says they are not installed yet.

--production --installation checks installation readiness with the same
Thing policy as the installer's native readiness wait. Offline devices
waiting for pairing or connectivity are visible warnings. Thing configuration
errors, required add-ons and monitoring failures still fail, and Thermite's
web entry must redirect correctly. Things still initializing (missing or
reloading handlers, UNINITIALIZED, INITIALIZING, UNKNOWN) are polled for up
to --settle seconds (default 120 with --installation), as are transient REST
outages; any still initializing then are reported as warnings. Normal health
checks remain strict and take one snapshot.
"""

EXAMPLES = (
    ("thermite health --sandbox", "check the sandbox, with its API token"),
    ("thermite health --production", "check production runtime on this host, with the token the install saved"),
    ("thermite health --sandbox --addons conf/services/addons.cfg",
     "also fail if an add-on conf/ asks for is not installed"),
    (f"thermite health --url http://thermite.example:{oh_ports.HTTP_PORT} --json", "runtime at that URL, without a token, as JSON"),
)

NOTES = """\
exit status:
  0  every Thing ONLINE (or allowed) and no requested add-on missing
  1  a Thing not ONLINE and not allowed, a requested add-on missing, or
     (--production) the metrics services installed but not answering, or a
     scrape target down
  2  runtime could not be queried (unreachable, the token refused, or 401/403
     without a token)

Items in NULL/UNDEF are reported but do not fail the check; Group Items
without an aggregation function are skipped, since they never carry state.
With --installation, exit 0 means installation is ready, possibly with
device warnings or Things still initializing after the settle window; only
configuration errors, missing add-ons, monitoring or entry failures exit 1.
Compact output omits individual undefined Item states.
"""


#: How long --installation polls Things still initializing, by default.
SETTLE = 120
POLL = 5


class Unavailable(lib.SandboxError):
    """A transient REST outcome: a failed connection or a reloading resource."""


def unavailable(code) -> type[lib.SandboxError]:
    """The error for a failed REST read; only transient outcomes are retried."""
    return Unavailable if code != 200 and oh_http.retryable("GET", code) else lib.SandboxError


def settled(args, *, clock=time.monotonic, sleep=time.sleep) -> dict:
    """One report; with --installation, poll pending Things within --settle seconds.

    Configuration errors and authentication or data-shape problems end the
    wait at once. Things still pending at the end stay in the report as
    warnings: the installer's own readiness wait has already required complete
    initialization, so a later handler reload must not fail a deployed run.
    """
    window = getattr(args, "settle", None)
    if not getattr(args, "installation", False):
        return run(args)
    deadline = clock() + (SETTLE if window is None else window)
    while True:
        try:
            report = run(args)
        except Unavailable:
            if clock() >= deadline:
                raise
            sleep(max(0, min(POLL, deadline - clock())))
            continue
        if not report["things"].get("pending") or report["things"]["failed"] or clock() >= deadline:
            return report
        sleep(max(0, min(POLL, deadline - clock())))


def run(args) -> dict:
    installation = bool(getattr(args, "installation", False))
    if installation and (getattr(args, "host", None) is None or args.allow_offline):
        raise lib.SandboxError("installation health requires production settings and no --allow-offline override")
    token = lib.read_token(args.token_file) if args.token_file else None
    http = lib.Http(args.url, token=token, timeout=args.timeout)
    code, root = http.try_get_json("/rest/")
    if code in (401, 403) and token:
        raise lib.SandboxError(f"{args.url} refused the API token in {args.token_file} (HTTP {code})")
    if code != 200 or not isinstance(root, dict):
        raise unavailable(code)(f"{args.url}/rest/ did not answer (HTTP {code or 'connection failed'})")
    runtime = root.get("runtimeInfo")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("version"), str) or not runtime["version"]:
        raise lib.SandboxError("/rest/ returned malformed runtime metadata")
    code, things = http.try_get_json("/rest/things?summary=false")
    if code in (401, 403):
        raise lib.SandboxError(f"/rest/things answered {code}: pass --token-file with an API token")
    if code != 200:
        raise unavailable(code)(f"/rest/things answered HTTP {code or 'connection failed'}")
    if not isinstance(things, list) or not all(isinstance(row, dict)
            and isinstance(row.get("UID"), str) and bool(row["UID"])
            and (row.get("label") is None or isinstance(row["label"], str))
            and isinstance(row.get("statusInfo"), dict)
            and isinstance(row["statusInfo"].get("status"), str) and bool(row["statusInfo"]["status"])
            and (row["statusInfo"].get("statusDetail") is None or isinstance(row["statusInfo"]["statusDetail"], str))
            and (row["statusInfo"].get("description") is None or isinstance(row["statusInfo"]["description"], str))
            for row in things):
        raise lib.SandboxError("/rest/things returned malformed Thing status data")
    code, items = http.try_get_json("/rest/items?recursive=false&fields=name,type,state,groupType")
    if code != 200:
        raise unavailable(code)(f"/rest/items answered HTTP {code or 'connection failed'}")
    if not isinstance(items, list) or not all(isinstance(row, dict)
            and all(isinstance(row.get(key), str) and bool(row[key]) for key in ("name", "type"))
            and isinstance(row.get("state"), str)
            and (row.get("groupType") is None or isinstance(row["groupType"], str)) for row in items):
        raise lib.SandboxError("/rest/items returned malformed Item data")
    acode, addons = http.try_get_json("/rest/addons?serviceId=all")

    allow = frozenset(args.allow_offline or ())
    summary = lib.summarize_things(things or [], require_online=True, allow=allow, installation=installation)
    unknown_allowed = sorted(allow - {t["uid"] for t in summary["things"]})

    undefined = []
    for item in items or []:
        if item.get("type") == "Group" and not item.get("groupType"):
            continue
        if item.get("state") in ("NULL", "UNDEF"):
            undefined.append({"name": item.get("name"), "type": item.get("type"), "state": item.get("state")})

    addon_report: dict = {"http": acode}
    if acode == 200:
        if not isinstance(addons, list) or not all(isinstance(row, dict) and type(row.get("installed")) is bool
                and (isinstance(row.get("uid"), str) and bool(row["uid"])
                     or all(isinstance(row.get(key), str) and bool(row[key]) for key in ("type", "id")))
                for row in addons):
            raise lib.SandboxError("/rest/addons returned malformed add-on data")
        installed = sorted(lib.installed_addon_uids(addons or []))
        addon_report["installed"] = installed
        requested = args.addons.read_text(encoding="utf-8") if args.addons else getattr(args, "_addons_text", None)
        if requested is not None:
            wanted = lib.addon_uids(lib.parse_addons_cfg(requested))
            addon_report["requested"] = wanted
            addon_report["missing"] = [uid for uid in wanted if uid not in installed]
    elif args.addons or getattr(args, "_addons_text", None) is not None:
        raise unavailable(acode)(f"/rest/addons answered HTTP {acode}; --addons needs an admin API token")

    metrics = metrics_report(args.host) if getattr(args, "host", None) is not None else None
    failures = []
    if summary["failed"]:
        failures.append(f"{len(summary['failed'])} Thing(s) " +
                        ("have configuration errors" if installation else "not ONLINE"))
    if addon_report.get("missing"):
        failures.append("add-ons not installed: " + ", ".join(addon_report["missing"]))
    if metrics and metrics["installed"] and not metrics["ok"]:
        failures.append("metrics: " + metrics["summary"])
    if installation and not metrics["installed"]:
        failures.append("monitoring services are not installed")
    elif installation and metrics["ok"] and metrics.get("systemd_collector") is not True:
        failures.append("monitoring: systemd collector readiness could not be verified")
    entry_ready = oh_entry.ready(args.url, timeout=args.timeout) if installation else None
    if entry_ready is False:
        failures.append("Thermite web entry is not ready")
    return {
        **({"mode": "installation"} if installation else {}),
        **({"web_entry": {"ok": entry_ready}} if installation else {}),
        "url": args.url,
        "openhab_version": runtime["version"],
        "things": summary,
        "allowed_but_absent": unknown_allowed,
        "items": {"count": len(items or []), "null_or_undef": undefined},
        "addons": addon_report,
        **({"metrics": metrics} if metrics is not None else {}),
        "failures": failures,
    }


def metrics_report(host: dict, installed: bool | None = None, health=None) -> dict:
    """The metrics services on this host; only installed ones can fail."""
    if not (deploy.metrics_installed() if installed is None else installed):
        return {"installed": False, "ok": True, "summary": "not installed yet; thermite upgrade installs them"}
    health = health or deploy.metrics_health(host, deploy.lan_address())
    ok = health.prometheus_ok and health.targets_ok and health.grafana_ok and health.systemd is not False
    return {"installed": True, "ok": ok,
            "summary": health.describe() + (f"; down: {', '.join(health.down)}" if health.down else ""),
            "prometheus": health.prometheus, "targets_up": health.up, "targets": health.total,
            "down": list(health.down), "grafana": health.grafana, "systemd_collector": health.systemd}


def print_human(report: dict) -> None:
    if report.get("mode") == "installation" and not oh_cli.switches.verbose:
        rows = report["things"]["things"]
        for row in rows:
            if row["verdict"] in ("fail", "warn", "pending"):
                description = row.get("description") or f"{row['status']}/{row['detail']}"
                if row["verdict"] == "pending":
                    description = f"{row['uid']} {row['status']}/{row['detail']}, still initializing"
                mark = {"pending": "WAIT"}.get(row["verdict"], row["verdict"].upper())
                print(f"  {mark:4s} {row.get('label') or row['uid']}: {description}")
        if any(row["status"] == "OFFLINE" and str(row["uid"]).startswith("androidtv:googletv:")
               and row.get("description") == "GoogleTV: PIN Process Incomplete" for row in rows):
            print("Pair devices from Thermite's Pairing screen (thermite remote --pair).")
        if report["failures"]:
            print("RESULT: FAIL - " + "; ".join(report["failures"]))
        else:
            warnings = sum(row["verdict"] == "warn" for row in rows)
            pending = sum(row["verdict"] == "pending" for row in rows)
            print("RESULT: installation ready" + (f"; {warnings} device warning(s)" if warnings else "")
                  + (f"; warning: {pending} Thing(s) still initializing; recheck with "
                     "thermite health --production" if pending else ""))
        return
    th = report["things"]
    print(f"runtime {report['openhab_version']} at {report['url']}")
    print(f"Things: {th['count']}  " + ", ".join(f"{k} {v}" for k, v in th["by_status"].items()))
    for row in th["things"]:
        if row["verdict"] in ("fail", "allowed", "warn", "pending"):
            mark = {"fail": "FAIL", "allowed": "allowed", "warn": "warn", "pending": "waiting"}[row["verdict"]]
            desc = f"  {row['description']}" if row.get("description") else ""
            print(f"  {mark:7s} {row['uid']}  {row['status']}/{row['detail']}{desc}")
    if report["allowed_but_absent"]:
        print("  --allow-offline names unknown Things: " + ", ".join(report["allowed_but_absent"]))
    it = report["items"]
    print(f"Items: {it['count']}, NULL/UNDEF: {len(it['null_or_undef'])}")
    for item in it["null_or_undef"]:
        print(f"  {item['state']:5s} {item['name']} ({item['type']})")
    ad = report["addons"]
    if ad["http"] == 200:
        print(f"Add-ons installed ({len(ad['installed'])}): {', '.join(ad['installed']) or '-'}")
        if ad.get("missing"):
            print(f"  MISSING: {', '.join(ad['missing'])}")
    else:
        print(f"Add-ons: not readable (HTTP {ad['http'] or 'connection failed'}; needs an admin API token)")
    if "metrics" in report:
        print(f"Metrics: {report['metrics']['summary']}")
    print("RESULT: " + ("FAIL - " + "; ".join(report["failures"]) if report["failures"] else "ok"))


def production(args) -> None:
    """Point args at production on this host and the token the install saved."""
    from oh_paths import SITE_FILE
    from oh_cli.deploy import deployment_selection
    revision = getattr(args, "revision", None)
    requirement = None
    if revision is not None or (AREA / SITE_FILE).is_file():
        revision, args.host, requirement = deployment_selection(revision or "HEAD", area=AREA)
    else:
        args.host = deploy.load(AREA)[1]
    args.url = deploy.production_url(args.host)
    args.token_file = args.token_file or AREA / deploy.token_file(deploy.hostname())
    if getattr(args, "installation", False) and not args.addons:
        if requirement is not None:
            files, _ = deploy.prepared_files(AREA, revision, AREA / "var")
            args._addons_text = files["conf/services/addons.cfg"].data.decode("utf-8")
        else:
            args.addons = AREA / "conf/services/addons.cfg"
    if not args.token_file.is_file():
        raise lib.SandboxError(f"no {args.token_file}: thermite install saves production's API token there "
                               "(docs/installation.md)")


def sandbox(args) -> None:
    """Point args at the sandbox and, unless one is given, its API token."""
    args.url = oh_cli.SANDBOX_URL
    if not args.token_file and SANDBOX_TOKEN.is_file():
        args.token_file = SANDBOX_TOKEN


def main(argv=None) -> int:
    parser = oh_cli.command("health", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_target(parser, "production", "sandbox", "url")
    parser.add_argument("--revision", help=argparse.SUPPRESS)
    parser.add_argument("--token-file", type=Path, metavar="FILE", help="file holding a runtime API token")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--allow-offline", nargs="+", metavar="ID", help="Thing UIDs that may be offline")
    mode.add_argument("--installation", action="store_true",
                      help="with --production, verify installation and report offline devices as warnings")
    parser.add_argument("--addons", type=Path, metavar="FILE",
                        help="fail if an add-on listed in this addons.cfg is missing")
    parser.add_argument("--settle", type=int, metavar="SECONDS",
                        help=f"with --installation, poll Things still initializing this long (default {SETTLE})")
    oh_cli.add_timeout(parser, 15, "each answer")
    oh_cli.add_json(parser)
    args = parser.parse_args(argv)
    if args.installation and not args.production:
        parser.error("--installation requires --production")
    if args.revision is not None and not args.production:
        parser.error("--revision requires --production")
    if args.settle is not None and (not args.installation or not 0 <= args.settle <= 3600):
        parser.error("--settle requires --installation and takes 0 to 3600 seconds")
    try:
        if args.production:
            production(args)
        elif args.sandbox:
            sandbox(args)
        report = settled(args)
    except (lib.SandboxError, deploy.DeployError, OSError) as exc:
        lib.die(str(exc))
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print_human(report)
    return 1 if report["failures"] else 0
