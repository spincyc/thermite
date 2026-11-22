"""What is deployed and running on this host, against this checkout, and
read-only checks of its runtime.

    thermite status [--production] [--json] [--state-dir DIR]
    thermite status --runtime [--production | --sandbox | --runtime-url URL] [--token-file FILE]
                    [--addons FILE] [--allow-offline UID ...] [--timeout SECONDS] [--json]
    thermite status --installation [--settle SECONDS] [--addons FILE] [--json]

The runtime checks are oh_cli.health's, which thermite health ran before
status absorbed it: --runtime runs them as it did, with the same output
and exit status.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

import oh_cli
import oh_deploy as deploy
import oh_host
import oh_music
import oh_ports
import oh_sandbox as lib
from oh_access import AccessError, guest
from oh_cli import AREA, health
from oh_cli.history import lines as history_lines

DESCRIPTION = """\
Report, read-only, what runs here: the commit thermite install deployed
(and whether HEAD is it, or how many commits after it), whether the
installed conf/, Apple TV helper, metrics/ and metrics exporter still match
that deploy, whether each installed systemd unit matches deploy/ (the owner
installs one that differs, with thermite upgrade), the services, whether
runtime answers, the HTTPS application's address on the port
instance/host.yaml sets (and whether the installed gateway still serves
another), the plain-HTTP Guest addresses instance/host.yaml opens,
Grafana's address on the LAN and whether it answers, Prometheus' scrape
targets (up of all), on a host with music: whether Music Assistant runs
with its API on loopback alone, whether systemd has each service of an
installed gateway active (the gateway, its policy worker and, where the
house has one, its callback relay), the install transaction that installed
this version, any install transaction that has not finished and whether a
command is running one now, and the runtime's own checks: its Things, read
with the same Thing policy as installation readiness (--installation), Items
whose state is NULL or UNDEF, none of thermite's rules disabled, and the
installed add-ons, read with the API token thermite install saved in
var/<this host's name>/api-token. A Thing offline, waiting for pairing or
connectivity, or not initialized is a warning, listed and in --json's
warnings, and does not fail status; a Thing configuration error fails it.
While runtime does not answer the checks are skipped, that being the
problem; when they cannot run (no token file, the token refused, an answer
malformed) that is a problem too. Last come the last deploys (thermite
history lists more).

--runtime runs the runtime's checks alone, and strictly: every Thing
ONLINE, unless --allow-offline names it. It sends only GET and HEAD
requests: of production on this host by default, with that token, and then
also of the services beside it: Prometheus ready with every scrape target
up and Grafana answering; on a host with music:, Music Assistant running,
its API on loopback alone and its streams answering; and the gateway's
services. Before thermite upgrade has installed them it says they are not
installed yet. --sandbox checks the sandbox on {sandbox}, with
var/sandbox/api-token when it exists, and --runtime-url the runtime's REST
API at URL; either implies --runtime. The runtime answers /rest/things and
/rest/addons only with credentials: --token-file names a file holding an
API token. --addons also fails an add-on an addons.cfg lists that is not
installed, and --allow-offline lets the Things it names be offline; with
--token-file and --timeout they shape the runtime's checks in plain status
too, where a Thing --allow-offline names is not even a warning. An online
HEOS bridge without either optional account field reports its
missing-account diagnostic as a visible warning, preserving local
transport.

The runtime keeps a rule's disabled flag through restarts and installs and
logs nothing for it, so a rule switched off in its own pages leaves a device
quiet with no error anywhere. thermite's rules are the ones its scripts
define, which the runtime lists as not editable; every thermite install or
upgrade switches any that is disabled back on.

--installation, which implies --runtime of production, checks installation
readiness with the same Thing policy as the installer's native readiness
wait; thermite install and upgrade run it last. Offline devices waiting for
pairing or connectivity are visible warnings. Thing configuration errors,
required add-ons (conf/services/addons.cfg's, unless --addons names
another) and monitoring failures still fail. A runtime that has just
started gets --settle seconds (default {settle}) to finish: Things still
initializing (missing or reloading handlers, UNINITIALIZED, INITIALIZING,
UNKNOWN), transient REST outages, and add-ons, monitoring, music or a
gateway service not ready yet are polled that long. Things still
initializing then are reported as warnings; anything else still failing
then fails. A Thing configuration error fails at once. --settle 0 takes one
snapshot, as the other checks do; they remain strict.
""".format(sandbox=oh_cli.SANDBOX_URL.removeprefix("http://"), settle=health.SETTLE)

EXAMPLES = (
    ("thermite status", "the deployed commit against HEAD, the units, the services, runtime and its checks"),
    ("thermite status --json", "the same, as JSON, with the problems that make it exit 1 and the warnings"),
    ("thermite status --state-dir /tmp/state", "the same for a runtime state somewhere else"),
    ("thermite status --runtime", "the runtime's checks alone, and the services beside it"),
    ("thermite status --sandbox", "the same checks of the sandbox, with its API token"),
    ("thermite status --sandbox --addons conf/services/addons.cfg",
     "also fail if an add-on conf/ asks for is not installed"),
    (f"thermite status --runtime-url http://thermite.example:{oh_ports.HTTP_PORT} --json",
     "runtime at that URL, without a token, as JSON"),
    ("thermite status --installation", "installation readiness, as thermite install checks it last"),
)

NOTES = """\
exit status:
  0  what HEAD (or the selected commit) holds is deployed, unchanged, its
     services and runtime are up and the runtime's checks pass, possibly
     with Things offline or not initialized, which are warnings; with
     --runtime, every Thing ONLINE (or allowed), none of thermite's rules
     disabled and no requested add-on missing
  1  nothing is deployed, HEAD is not what is deployed, a deployed file or
     unit differs or is missing, or is still installed without music:, a
     service is not active, runtime does not answer, Music Assistant does
     not run with its API on loopback alone, the installed gateway serves
     HTTPS on another port or one of its services is not active, an
     upgrade's install transaction has not finished, or a runtime check
     failed or could not run: a Thing configuration error, one of
     thermite's rules disabled or a requested add-on missing; with
     --runtime, a Thing not ONLINE and not allowed, one of thermite's rules
     disabled, a requested add-on missing,
     or (of production) the metrics services installed but not answering, a
     scrape target down, Music Assistant installed but not running with its
     API on loopback alone, or a gateway service not active
  2  a wrong switch, or the host file or deploy record cannot be read; with
     --runtime, the runtime could not be queried (unreachable, the token
     refused, or 401/403 without a token)

Items in NULL/UNDEF are reported but do not fail the checks; Group Items
without an aggregation function are skipped, since they never carry state.
With --installation, exit 0 means installation is ready, possibly with
device warnings, Things still initializing after the settle window, or a
rule that loaded disabled after the install switched thermite's rules on,
which a rollback would not change; only configuration errors, missing
add-ons, monitoring or music failures exit 1. Its compact output omits
individual undefined Item states.
"""

#: Seconds the runtime's checks wait for each answer, by default.
TIMEOUT = 15


def show_metrics(host: dict, installed: dict[str, Path] | None = None, report: dict | None = None) -> None:
    say = print if report is None else _quiet
    report = {} if report is None else report
    if not deploy.metrics_installed(installed):
        report["metrics"] = None
        say("metrics  not installed; thermite upgrade installs them")
        return
    health = deploy.metrics_health(host, deploy.lan_address())
    report["metrics"] = {"grafana": health.grafana, "grafana_ok": bool(health.grafana_ok),
                         "prometheus": health.prometheus, "prometheus_ok": bool(health.prometheus_ok),
                         "targets_up": health.up, "targets": health.total, "down": list(health.down),
                         "systemd": health.systemd}
    say(f"grafana  {health.grafana}: " + ("answers; anonymous visitors view the dashboards" if health.grafana_ok else
                                         "not answering"))
    if not health.prometheus_ok:
        say(f"prometheus {health.prometheus}: not answering")
    elif health.total is None:
        say(f"prometheus {health.prometheus}: ready; its targets could not be read")
    else:
        say(f"prometheus {health.prometheus}: ready, targets {health.up}/{health.total} up"
            + (f"; down: {', '.join(health.down)}" if health.down else ""))
    if health.systemd is not None:
        say("systemd  " + ("node_exporter reads the units' states over D-Bus" if health.systemd else
                           "node_exporter's systemd collector fails: sudo journalctl -u "
                           "thermite-node-exporter.service says why"))


def show_music(host: dict, report: dict, say, problems: list[str]) -> None:
    """Music Assistant, on a host with music:: whether it runs, with its API
    on loopback alone, and its streams answer. Unlike the metrics, which
    only watch, it is a problem when it does not: the house plays through
    it, as it is run through openHAB."""
    if not oh_music.enabled(host):
        return
    if not deploy.music_installed():
        report["music"] = None
        say("music    not installed; thermite upgrade installs it")
        return
    health = deploy.music_health(host)
    report["music"] = health.report()
    if not health.ok:
        problems.append("music: " + health.describe())
    say(f"music    {health.describe()}")


def show_gateway(host: dict, report: dict, say, problems: list[str]) -> None:
    """The HTTPS application's address on instance/host.yaml's port, where
    everyone signs in, and a problem while the installed gateway, as its
    verified profile records it, still serves another port."""
    port = oh_ports.gateway_port(host)
    url = guest.origin("https", deploy.hostname(), port) + "/"
    address = deploy.lan_address()
    lan = guest.origin("https", address, port) + "/" if address else None
    try:
        from oh_access import client
        profile = client.deployment(AREA)
    except AccessError:
        profile = None
    installed = profile["url"] + "/" if profile else None
    report["gateway"] = {"url": url, "lan": lan, "port": port, "installed": installed}
    say(f"gateway  {url}: the HTTPS application, where everyone signs in"
        + (f"; from the LAN: {lan}" if lan else ""))
    if installed and (urlsplit(installed).port or 443) != port:
        problems.append(f"gateway {installed}: instance/host.yaml moves HTTPS to port {port}")
        say(f"         installed on {installed}; thermite upgrade moves it to port {port}")


def show_gateway_services(report: dict, say, problems: list[str]) -> None:
    """Whether systemd has each service of an installed gateway active, as
    health.gateway_report reads them; nothing before one is installed."""
    found = health.gateway_report()
    report["gateway"]["services"] = found
    if not found["installed"]:
        return
    if not found["ok"]:
        problems.append("gateway: " + found["summary"])
    say(f"         {found['summary']}")


def show_guest(host: dict, report: dict, say) -> None:
    """The opt-in plain-HTTP Guest listener's addresses, from instance/host.yaml."""
    settings = guest.settings(host.get("guest_http"))
    report["guest_http"] = [url + "/" for url in guest.origins(settings)] if settings else []
    for url in report["guest_http"]:
        say(f"guest    {url}: Guest controls over plain HTTP, for {', '.join(settings['clients'])}")


def show_transaction(host: dict, report: dict, say, problems: list[str], *, manifest: Path | None = None) -> None:
    """The install transaction that installed this version, from the
    installed manifest (`manifest`, by default the system's), an install
    transaction that has not finished, and the run that holds the site's
    transaction scope now: its transaction is in progress, not waiting for
    a command."""
    import json
    from oh_install import changeover, engine
    root = deploy.installation(host).root
    try:
        installed = json.loads((manifest or Path("/") / engine.MANIFEST).read_text())
    except (OSError, ValueError):
        installed = None
    if isinstance(installed, dict) and isinstance(installed.get("transaction"), str):
        report["transaction"] = {"installed": installed["transaction"], "at": installed.get("committed_at"),
                                 "kept": f"{root}/.transaction/{installed.get('kept')}"}
        say(f"install  transaction {installed['transaction']} committed {installed.get('committed_at')}; "
            f"the version before it is set aside in {root}/.transaction/{installed.get('kept')}")
    pending = changeover.open_transaction(AREA / "var")
    holder = changeover.in_progress(AREA)
    report["open_transaction"] = pending
    report["in_progress"] = ({"verb": holder.verb, "pid": holder.pid, "since": holder.since}
                             if holder is not None else None)
    if pending and holder is not None:
        problems.append(f"install transaction {pending} is in progress")
        say(f"install  transaction {pending} is in progress: {holder} runs it")
    elif pending:
        problems.append(f"install transaction {pending} has not finished")
        say(f"install  transaction {pending} has not finished: {changeover.pending_command(AREA)} commits or "
            "rolls it back, then stops")
    elif holder is not None:
        say(f"install  {holder} is in progress; it has not begun a transaction")


def show_runtime_checks(args, report: dict, say, problems: list[str], warnings: list[str] | None = None) -> None:
    """The runtime's own checks (health.run): its Things, Items, thermite's
    rules and add-ons, without the metrics, music and gateway, which status
    reads itself. Skipped while the runtime does not answer, which is a
    problem already; a problem when they cannot run. Things are read as
    installation readiness reads them: a Thing offline or not initialized is
    a warning, one with a configuration error a problem."""
    warnings = [] if warnings is None else warnings
    url = report["runtime"]["url"]
    report["runtime_checks"], report["runtime_checks_reason"] = None, None
    if not report["runtime"]["version"]:
        report["runtime_checks_reason"] = f"runtime {url} is not answering"
        return
    token = args.token_file or AREA / deploy.token_file(deploy.hostname())
    checks = argparse.Namespace(url=url, token_file=token, timeout=args.timeout, allow_offline=args.allow_offline,
                                addons=args.addons, installation=False, offline_warns=True)
    try:
        if not token.is_file():
            raise lib.SandboxError(f"no {token}: thermite install saves production's API token there "
                                   "(docs/installation.md)")
        found = health.run(checks)
    except (lib.SandboxError, OSError) as exc:
        report["runtime_checks_reason"] = str(exc)
        problems.append(f"runtime checks could not run: {exc}")
        say(f"checks   could not run: {exc}")
        return
    report["runtime_checks"] = found
    problems.extend(f"runtime: {failure}" for failure in found["failures"])
    for row in found["things"]["things"]:
        if row["verdict"] in ("warn", "pending"):
            warnings.append(f"runtime: Thing {row['uid']} is {row['status']}/{row['detail']}"
                            + (f" ({row['description']})" if row.get("description") else "")
                            + ("; it has not initialized" if row["verdict"] == "pending" else ""))
    say(f"checks   the runtime's own, with the API token in {token}")
    things = found["things"]
    say(f"things   {things['count']}: " + ", ".join(f"{status} {count}" for status, count in things["by_status"].items()))
    marks = {"fail": "FAIL", "allowed": "allowed", "warn": "warn", "pending": "waiting"}
    for row in things["things"]:
        if row["verdict"] in marks:
            say(f"           {marks[row['verdict']]:<8}{row['uid']}  {row['status']}/{row['detail']}"
                + (f"  {row['description']}" if row.get("description") else ""))
    if found["allowed_but_absent"]:
        say("           --allow-offline names unknown Things: " + ", ".join(found["allowed_but_absent"]))
    items = found["items"]
    say(f"items    {items['count']}, NULL/UNDEF: {len(items['null_or_undef'])}")
    for item in items["null_or_undef"]:
        say(f"           {item['state']:<6}{item['name']} ({item['type']})")
    rules = found["rules"]
    say(f"rules    {rules['count']}, thermite's disabled: {len(rules['disabled'])}"
        + ("; the next thermite install or upgrade switches them back on" if rules["disabled"] else ""))
    for rule in rules["disabled"]:
        say(f"           FAIL    {rule['uid']}" + (f"  {rule['name']}" if rule.get("name") else ""))
    addons = found["addons"]
    if addons["http"] == 200:
        say(f"add-ons  {len(addons['installed'])} installed: {', '.join(addons['installed']) or '-'}")
        if addons.get("missing"):
            say(f"           MISSING {', '.join(addons['missing'])}")
    else:
        say(f"add-ons  not readable (HTTP {addons['http'] or 'connection failed'}; needs an admin API token)")


def _quiet(*_args, **_kwargs) -> None:
    pass


def show(args, host: dict) -> int:
    """Print (or with args.json, emit as JSON) what runs here; 1 when
    something drifted from the deploy or is down."""
    as_json = getattr(args, "json", False)
    say = _quiet if as_json else print
    report: dict = {"deployed": None, "trees": {}, "units": {}, "services": {}, "problems": [], "warnings": []}
    problems, warnings = report["problems"], report["warnings"]
    state_dir = getattr(args, "state_dir", None)
    layout = deploy.State(state_dir) if state_dir else deploy.state(host)
    record = deploy.read_record(layout)
    if record:
        report["deployed"] = {"commit": record["commit"], "subject": record.get("subject", ""),
                              "at": record.get("deployed_at"), "by": record.get("by"),
                              "boot_checked": bool(record.get("boot_checked"))}
        say(f"deployed {record['commit'][:12]} {record.get('subject', '')}")
        say(f"         at {record.get('deployed_at')} by {record.get('by')}"
            + ("" if record.get("boot_checked") else " (without passing thermite test boot)"))
        revision = getattr(args, "revision", None)
        head = deploy.resolve(AREA, revision or "HEAD")
        label = "selected" if revision else "HEAD"
        report["selected"] = head
        if head == record["commit"]:
            say(f"{label:<9}is what is deployed")
        else:
            problems.append(f"{label} {head[:12]} is not what is deployed")
            try:
                say(f"{label:<9}{head[:12]}, {deploy.ahead(AREA, record['commit'], head)} commit(s) after "
                    "the deployed one")
            except deploy.DeployError:
                say(f"{label:<9}{head[:12]}; the deployed commit is not in this clone")
        for label, target, key in (("conf", layout.conf, "conf"), ("helper", layout.atv_app, "atv_app"),
                                   ("metrics", layout.metrics, "metrics"),
                                   ("exporter", layout.exporter_app, "exporter_app")):
            if key not in record and not target.is_dir():
                report["trees"][label] = None
                say(f"{label:<9}not deployed: {target} does not exist yet (thermite upgrade makes it)")
                continue
            drift = deploy.drift(target, record.get(key, {}))
            report["trees"][label] = {kind: list(paths) for kind, paths in drift.items()}
            changed = [f"{len(v)} {k}" for k, v in drift.items() if v and k != "foreign"]
            if changed:
                problems.append(f"{label}: {', '.join(changed)} since the deploy")
            say(f"{label:<9}{'as deployed' if not changed else ', '.join(changed) + ' since the deploy'}"
                + (f"; {len(drift['foreign'])} file(s) no deploy installed" if drift["foreign"] else ""))
            for kind in ("modified", "missing"):
                for path in drift[kind]:
                    say(f"           {kind:<9}{path}")
    else:
        problems.append("nothing is deployed")
        say(f"deployed nothing yet ({layout.record} does not exist)")
    revision = getattr(args, "revision", None)
    units = deploy.installed_units(AREA, rev=revision) if revision else deploy.installed_units(AREA)
    for name, target, how in units:
        report["units"][str(target)] = how
        if how in ("differs", "missing", "unwanted"):
            problems.append(f"unit {target}: {how}")
        say(f"unit     {target}: {how}" + (f" (the owner installs {name}: thermite upgrade)"
                                           if how in ("differs", "missing") else
                                           " (instance/host.yaml no longer needs it; thermite upgrade removes it)"
                                           if how == "unwanted" else ""))
    for service, active in deploy.service_states(deploy.services(host)).items():
        report["services"][service] = active
        if active != "active":
            problems.append(f"service {service}: {active}")
        say(f"service  {service}: {active}")
    url = deploy.production_url(host)
    version = deploy.openhab_version(url)
    lan = deploy.production_url(host, deploy.lan_address() or deploy.hostname())
    report["runtime"] = {"url": url, "lan": lan, "version": version}
    if not version:
        problems.append(f"runtime {url}: not answering")
    say(f"runtime  {url}: " + (f"runtime {version} answers; from the LAN: {lan}" if version else "not answering"))
    show_gateway(host, report, say, problems)
    show_gateway_services(report, say, problems)
    show_guest(host, report, say)
    show_transaction(host, report, say, problems)
    show_metrics(host, report=report if as_json else None)
    show_music(host, report, say, problems)
    if getattr(args, "no_runtime_checks", False):
        report["runtime_checks"], report["runtime_checks_reason"] = None, "not asked for"
    else:
        show_runtime_checks(args, report, say, problems, warnings)
    entries = deploy.deploys(layout)
    report["deploys"] = len(entries)
    if entries:
        say("history  (latest last; * deployed now)")
        for line in history_lines(entries[-5:]):
            say(f"       {line}")
        say(f"         thermite history shows more ({len(entries)} deploy{'s' if len(entries) != 1 else ''} here)")
    if as_json:
        oh_cli.emit(report)
    return 1 if problems else 0


def runtime(args) -> int:
    """`status --runtime`: the runtime's checks alone, as thermite health
    ran them, with its output and exit status."""
    args.url = args.runtime_url
    try:
        if args.production:
            health.production(args)
        elif args.sandbox:
            health.sandbox(args)
        report = health.settled(args)
    except (lib.SandboxError, deploy.DeployError, oh_host.HostError, OSError) as exc:
        print(f"thermite status: {exc}", file=sys.stderr)
        return 2
    if args.json:
        oh_cli.emit(report)
    else:
        health.print_human(report)
    return 1 if report["failures"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("status", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("--runtime", action="store_true",
                        help="only the runtime's checks (--sandbox and --runtime-url imply it)")
    oh_cli.add_target(parser, "production", "sandbox", "runtime-url", default="production")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installation", action="store_true",
                      help="installation readiness, as thermite install checks it: the runtime's checks of "
                           "production, with offline devices as warnings")
    mode.add_argument("--allow-offline", nargs="+", metavar="UID", help="Thing UIDs that may be offline")
    parser.add_argument("--settle", type=int, metavar="SECONDS",
                        help=f"with --installation, poll what a start has not finished this long "
                             f"(default: {health.SETTLE})")
    parser.add_argument("--addons", type=Path, metavar="FILE",
                        help="fail if an add-on listed in this addons.cfg is missing")
    oh_cli.add_token_file(parser, "file holding the runtime's API token (default: production's, "
                                  "var/<this host's name>/api-token; the sandbox's, var/sandbox/api-token)")
    oh_cli.add_timeout(parser, TIMEOUT, "each answer of the runtime")
    oh_cli.add_state_dir(parser)
    oh_cli.add_json(parser)
    parser.add_argument("--revision", help=argparse.SUPPRESS)
    # What is deployed alone: the installer reads the runtime's readiness
    # with --installation just before, where offline devices only warn.
    parser.add_argument("--no-runtime-checks", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    elsewhere = args.sandbox or args.runtime_url is not None
    args.runtime = args.runtime or args.installation or elsewhere
    args.production = not elsewhere
    if args.installation and elsewhere:
        parser.error("--installation requires --production")
    if args.revision is not None and elsewhere:
        parser.error("--revision requires --production")
    if args.settle is not None and (not args.installation or not 0 <= args.settle <= 3600):
        parser.error("--settle requires --installation and takes 0 to 3600 seconds")
    if args.runtime and args.state_dir is not None:
        parser.error("--state-dir names this host's deployment state, which --runtime does not read")
    if args.runtime and args.no_runtime_checks:
        parser.error("--no-runtime-checks does not go with --runtime")
    if args.runtime:
        return runtime(args)
    try:
        from oh_paths import private
        if args.revision is not None or private(AREA):
            commit, host, requirement = deploy.selection(AREA, args.revision or "HEAD")
            if requirement is not None or args.revision is not None:
                args.revision = commit
        else:
            _, host = deploy.load(AREA)
        return show(args, host)
    except (deploy.DeployError, oh_host.HostError) as exc:
        print(f"thermite status: {exc}", file=sys.stderr)
        return 2
