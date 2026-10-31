"""Read-only health check of the running runtime (sandbox or production)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
import oh_sandbox as lib
from oh_cli import AREA

SANDBOX_TOKEN = AREA / "var/sandbox/api-token"

DESCRIPTION = """\
Read-only checks against the running runtime: every Thing ONLINE, Items whose
state is NULL or UNDEF, and the installed add-ons (compared with an
addons.cfg when --addons is given). Only GET requests are sent.

The runtime answers /rest/things and /rest/addons only with credentials; pass
an API token file (the sandbox writes var/sandbox/api-token). --sandbox
checks the sandbox on 127.0.0.1:18080, with that token when it exists.
--production checks production runtime on this host, on 127.0.0.1 at
instance/host.yaml's http_port, with the token bin/thermite install saved in
var/<this host's name>/api-token, and the metrics services beside it:
Prometheus ready with every scrape target up, and Grafana answering.
Before thermite upgrade has installed them it says they are not installed yet.
"""

EXAMPLES = (
    ("thermite health --sandbox", "check the sandbox, with its API token"),
    ("thermite health --production", "check production runtime on this host, with the token the install saved"),
    ("thermite health --sandbox --addons conf/services/addons.cfg",
     "also fail if an add-on conf/ asks for is not installed"),
    ("thermite health --url http://thermite.example:8080 --json", "runtime at that URL, without a token, as JSON"),
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
"""


def run(args) -> dict:
    token = lib.read_token(args.token_file) if args.token_file else None
    http = lib.Http(args.url, token=token, timeout=args.timeout)
    code, root = http.try_get_json("/rest/")
    if code in (401, 403) and token:
        raise lib.SandboxError(f"{args.url} refused the API token in {args.token_file} (HTTP {code})")
    if code != 200 or not isinstance(root, dict):
        raise lib.SandboxError(f"{args.url}/rest/ did not answer (HTTP {code or 'connection failed'})")
    code, things = http.try_get_json("/rest/things?summary=true")
    if code in (401, 403):
        raise lib.SandboxError(f"/rest/things answered {code}: pass --token-file with an API token")
    if code != 200:
        raise lib.SandboxError(f"/rest/things answered HTTP {code or 'connection failed'}")
    code, items = http.try_get_json("/rest/items?recursive=false&fields=name,type,state,groupType")
    if code != 200:
        raise lib.SandboxError(f"/rest/items answered HTTP {code or 'connection failed'}")
    acode, addons = http.try_get_json("/rest/addons?serviceId=all")

    allow = frozenset(args.allow_offline or ())
    summary = lib.summarize_things(things or [], require_online=True, allow=allow)
    unknown_allowed = sorted(allow - {t["uid"] for t in summary["things"]})

    undefined = []
    for item in items or []:
        if item.get("type") == "Group" and not item.get("groupType"):
            continue
        if item.get("state") in ("NULL", "UNDEF"):
            undefined.append({"name": item.get("name"), "type": item.get("type"), "state": item.get("state")})

    addon_report: dict = {"http": acode}
    if acode == 200:
        installed = sorted(lib.installed_addon_uids(addons or []))
        addon_report["installed"] = installed
        if args.addons:
            wanted = lib.addon_uids(lib.parse_addons_cfg(args.addons.read_text(encoding="utf-8")))
            addon_report["requested"] = wanted
            addon_report["missing"] = [uid for uid in wanted if uid not in installed]
    elif args.addons:
        raise lib.SandboxError(f"/rest/addons answered HTTP {acode}; --addons needs an admin API token")

    metrics = metrics_report(args.host) if getattr(args, "host", None) is not None else None
    failures = []
    if summary["failed"]:
        failures.append(f"{len(summary['failed'])} Thing(s) not ONLINE")
    if addon_report.get("missing"):
        failures.append("add-ons not installed: " + ", ".join(addon_report["missing"]))
    if metrics and metrics["installed"] and not metrics["ok"]:
        failures.append("metrics: " + metrics["summary"])
    return {
        "url": args.url,
        "openhab_version": root.get("runtimeInfo", {}).get("version"),
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
    th = report["things"]
    print(f"runtime {report['openhab_version']} at {report['url']}")
    print(f"Things: {th['count']}  " + ", ".join(f"{k} {v}" for k, v in th["by_status"].items()))
    for row in th["things"]:
        if row["verdict"] in ("fail", "allowed"):
            mark = "FAIL" if row["verdict"] == "fail" else "allowed"
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
    args.host = deploy.load(AREA)[1]
    args.url = deploy.production_url(args.host)
    args.token_file = args.token_file or AREA / deploy.token_file(deploy.hostname())
    if not args.token_file.is_file():
        raise lib.SandboxError(f"no {args.token_file}: bin/thermite install saves production's API token there "
                               "(docs/deploy.md)")


def sandbox(args) -> None:
    """Point args at the sandbox and, unless one is given, its API token."""
    args.url = oh_cli.SANDBOX_URL
    if not args.token_file and SANDBOX_TOKEN.is_file():
        args.token_file = SANDBOX_TOKEN


def main(argv=None) -> int:
    parser = oh_cli.command("health", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_target(parser, "production", "sandbox", "url")
    parser.add_argument("--token-file", type=Path, metavar="FILE", help="file holding a runtime API token")
    parser.add_argument("--allow-offline", nargs="+", metavar="ID", help="Thing UIDs that may be offline")
    parser.add_argument("--addons", type=Path, metavar="FILE",
                        help="fail if an add-on listed in this addons.cfg is missing")
    oh_cli.add_timeout(parser, 15, "each answer")
    oh_cli.add_json(parser)
    args = parser.parse_args(argv)
    try:
        if args.production:
            production(args)
        elif args.sandbox:
            sandbox(args)
        report = run(args)
    except (lib.SandboxError, OSError) as exc:
        lib.die(str(exc))
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print_human(report)
    return 1 if report["failures"] else 0
