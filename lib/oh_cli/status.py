"""What is deployed and running on this host, against this checkout.

    thermite status [--state DIR]
"""

from __future__ import annotations

import sys
from pathlib import Path

import oh_cli
import oh_deploy as deploy
from oh_cli import AREA
from oh_cli.history import lines as history_lines

DESCRIPTION = """\
Report, read-only, what runs here: the commit thermite deploy installed
(and whether HEAD is it, or how many commits after it), whether the
installed conf/, Apple TV helper, metrics/ and metrics exporter still match
that deploy, whether each installed systemd unit matches deploy/ (the owner
installs one that differs, with thermite upgrade), the services, whether
runtime answers, Grafana's address on the LAN and whether it answers,
Prometheus' scrape targets (up of all), and the last deploys (thermite
history lists more).
"""

EXAMPLES = (
    ("thermite status", "the deployed commit against HEAD, the units, the services, runtime"),
    ("thermite status --state /tmp/state", "the same for a state root somewhere else"),
)


def show_metrics(host: dict, installed: dict[str, Path] | None = None) -> None:
    if not deploy.metrics_installed(installed):
        print("metrics  not installed; thermite upgrade installs them")
        return
    health = deploy.metrics_health(host, deploy.lan_address())
    print(f"grafana  {health.grafana}: " + ("answers; anonymous visitors view the dashboards" if health.grafana_ok else
                                           "not answering"))
    if not health.prometheus_ok:
        print(f"prometheus {health.prometheus}: not answering")
    elif health.total is None:
        print(f"prometheus {health.prometheus}: ready; its targets could not be read")
    else:
        print(f"prometheus {health.prometheus}: ready, targets {health.up}/{health.total} up"
              + (f"; down: {', '.join(health.down)}" if health.down else ""))
    if health.systemd is not None:
        print("systemd  " + ("node_exporter reads the units' states over D-Bus" if health.systemd else
                             "node_exporter's systemd collector fails: sudo journalctl -u "
                             "openhab-node-exporter.service says why"))


def show(args, host: dict) -> int:
    layout = deploy.State(args.state) if args.state else deploy.state(host)
    record = deploy.read_record(layout)
    if record:
        print(f"deployed {record['commit'][:12]} {record.get('subject', '')}")
        print(f"         at {record.get('deployed_at')} by {record.get('by')}"
              + ("" if record.get("boot_checked") else " (without passing thermite test boot)"))
        head = deploy.resolve(AREA, "HEAD")
        if head == record["commit"]:
            print("HEAD     is what is deployed")
        else:
            try:
                print(f"HEAD     {head[:12]}, {deploy.ahead(AREA, record['commit'], head)} commit(s) after "
                      "the deployed one")
            except deploy.DeployError:
                print(f"HEAD     {head[:12]}; the deployed commit is not in this clone")
        for label, target, key in (("conf", layout.conf, "conf"), ("helper", layout.atv_app, "atv_app"),
                                   ("metrics", layout.metrics, "metrics"),
                                   ("exporter", layout.exporter_app, "exporter_app")):
            if key not in record and not target.is_dir():
                print(f"{label:<9}not deployed: {target} does not exist yet (thermite upgrade makes it)")
                continue
            report = deploy.drift(target, record.get(key, {}))
            problems = [f"{len(v)} {k}" for k, v in report.items() if v and k != "foreign"]
            print(f"{label:<9}{'as deployed' if not problems else ', '.join(problems) + ' since the deploy'}"
                  + (f"; {len(report['foreign'])} file(s) no deploy installed" if report["foreign"] else ""))
            for kind in ("modified", "missing"):
                for path in report[kind]:
                    print(f"           {kind:<9}{path}")
    else:
        print(f"deployed nothing yet ({layout.record} does not exist)")
    for name, target, how in deploy.installed_units(AREA):
        print(f"unit     {target}: {how}" + (f" (the owner installs {name}: thermite upgrade)"
                                             if how in ("differs", "missing") else ""))
    for service, active in deploy.service_states().items():
        print(f"service  {service}: {active}")
    url = deploy.production_url(host)
    version = deploy.openhab_version(url)
    lan = deploy.production_url(host, deploy.lan_address() or deploy.hostname())
    print(f"runtime  {url}: " + (f"runtime {version} answers; from the LAN: {lan}" if version else "not answering"))
    show_metrics(host)
    entries = deploy.deploys(layout)
    if entries:
        print("history  (latest last; * deployed now)")
        for line in history_lines(entries[-5:]):
            print(f"       {line}")
        print(f"         thermite history shows more ({len(entries)} deploy{'s' if len(entries) != 1 else ''} here)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("status", DESCRIPTION, EXAMPLES)
    parser.add_argument("--state", type=Path, metavar="DIR",
                        help="the state root (default: instance/host.yaml state_root)")
    args = parser.parse_args(argv)
    _, host = deploy.load(AREA)
    try:
        return show(args, host)
    except deploy.DeployError as exc:
        print(f"thermite status: {exc}", file=sys.stderr)
        return 2
