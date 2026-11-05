"""What is deployed and running on this host, against this checkout.

    thermite status [--state DIR] [--json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

import oh_cli
import oh_deploy as deploy
import oh_ports
from oh_access import AccessError, guest
from oh_cli import AREA
from oh_cli.history import lines as history_lines

DESCRIPTION = """\
Report, read-only, what runs here: the commit thermite deploy installed
(and whether HEAD is it, or how many commits after it), whether the
installed conf/, Apple TV helper, metrics/ and metrics exporter still match
that deploy, whether each installed systemd unit matches deploy/ (the owner
installs one that differs, with thermite upgrade), the services, whether
runtime answers, the HTTPS application's address on the port
instance/host.yaml sets (and whether the installed gateway still serves
another), the plain-HTTP Guest addresses instance/host.yaml opens,
Grafana's address on the LAN and whether it answers, Prometheus' scrape
targets (up of all), the install transaction that installed this version
and any upgrade transaction that has not finished, and the last deploys
(thermite history lists more).
"""

EXAMPLES = (
    ("thermite status", "the deployed commit against HEAD, the units, the services, runtime"),
    ("thermite status --json", "the same, as JSON, with the problems that make it exit 1"),
    ("thermite status --state /tmp/state", "the same for a runtime state somewhere else"),
)

NOTES = """\
exit status:
  0  what HEAD (or the selected commit) holds is deployed, unchanged, and
     its services and runtime are up
  1  nothing is deployed, HEAD is not what is deployed, a deployed file or
     unit differs or is missing, a service is not active, runtime does not
     answer, the installed gateway serves HTTPS on another port, or an
     upgrade's install transaction has not finished
  2  a wrong switch, or the host file or deploy record cannot be read
"""


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


def show_guest(host: dict, report: dict, say) -> None:
    """The opt-in plain-HTTP Guest listener's addresses, from instance/host.yaml."""
    settings = guest.settings(host.get("guest_http"))
    report["guest_http"] = [url + "/" for url in guest.origins(settings)] if settings else []
    for url in report["guest_http"]:
        say(f"guest    {url}: Guest controls over plain HTTP, for {', '.join(settings['clients'])}")


def show_transaction(host: dict, report: dict, say, problems: list[str], *, manifest: Path | None = None) -> None:
    """The install transaction that installed this version, from the
    installed manifest (`manifest`, by default the system's), and an
    upgrade's transaction that has not finished."""
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
    report["open_transaction"] = pending
    if pending:
        problems.append(f"upgrade transaction {pending} has not finished")
        say(f"upgrade  transaction {pending} has not finished: {changeover.upgrade_command(AREA)} commits or "
            "rolls it back, then stops")


def _quiet(*_args, **_kwargs) -> None:
    pass


def show(args, host: dict) -> int:
    """Print (or with args.json, emit as JSON) what runs here; 1 when
    something drifted from the deploy or is down."""
    as_json = getattr(args, "json", False)
    say = _quiet if as_json else print
    report: dict = {"deployed": None, "trees": {}, "units": {}, "services": {}, "problems": []}
    problems = report["problems"]
    layout = deploy.State(args.state) if args.state else deploy.state(host)
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
        if how in ("differs", "missing"):
            problems.append(f"unit {target}: {how}")
        say(f"unit     {target}: {how}" + (f" (the owner installs {name}: thermite upgrade)"
                                           if how in ("differs", "missing") else ""))
    for service, active in deploy.service_states().items():
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
    show_guest(host, report, say)
    show_transaction(host, report, say, problems)
    show_metrics(host, report=report if as_json else None)
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


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("status", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_json(parser)
    parser.add_argument("--revision", help=argparse.SUPPRESS)
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the runtime state (default: runtime/ in instance/host.yaml's root, /srv/thermite)")
    args = parser.parse_args(argv)
    try:
        from oh_paths import SITE_FILE
        from oh_cli.deploy import deployment_selection
        if args.revision is not None or (AREA / SITE_FILE).is_file():
            commit, host, requirement = deployment_selection(args.revision or "HEAD", area=AREA)
            if requirement is not None or args.revision is not None:
                args.revision = commit
        else:
            _, host = deploy.load(AREA)
        return show(args, host)
    except deploy.DeployError as exc:
        print(f"thermite status: {exc}", file=sys.stderr)
        return 2
