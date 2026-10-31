"""Scan the LAN read-only and compare what answers with instance/devices.yaml.

    thermite discover [--no-ping] [--quick] [--output DIR] [--json]

Writes scan.json under var/discovery/<UTC time>/ and prints the house
file's devices that moved address or did not answer, and AV-looking hosts
the house file does not know. Nothing is commanded; see lib/oh_discover.py.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import oh_discover as discover
import oh_cli
import oh_inventory as inventory
from oh_cli import AREA

EXAMPLES = (
    ("thermite discover", "ping the LAN, listen for device announcements, and compare"),
    ("thermite discover --no-ping --quick", "the neighbour table and short listening windows only"),
    ("thermite discover --json", "the comparison as JSON"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("discover", __doc__.splitlines()[0], EXAMPLES)
    parser.add_argument("--no-ping", action="store_true",
                        help="use the neighbour table only; do not ping the network")
    parser.add_argument("--quick", action="store_true",
                        help="listen for device announcements for less time")
    parser.add_argument("--output", type=Path, metavar="DIR",
                        help="where to write scan.json (default: var/discovery/<UTC time>/)")
    oh_cli.add_json(parser)
    arguments = parser.parse_args(argv)
    local, network = discover.local_interface()
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = arguments.output or AREA / "var/discovery" / stamp
    if not arguments.no_ping:
        discover.ping_sweep(network)
    macs = discover.neighbours()
    oui = discover.load_oui()
    hosts = {ip: {"mac": mac, "vendor": discover.vendor(mac, oui)} for ip, mac in macs.items()
             if ipaddress_in(ip, network)}
    for ip, ports in discover.probe_ports(sorted(hosts)).items():
        hosts[ip]["ports"] = ports
    wait = 3.0 if arguments.quick else None
    for ip, entry in discover.ssdp_search(local, **({"wait": wait} if wait else {})).items():
        hosts.setdefault(ip, {})["ssdp"] = entry
    for ip, entry in discover.sddp_search(local, **({"wait": wait * 2} if wait else {})).items():
        hosts.setdefault(ip, {})["sddp"] = entry
    for ip, entry in discover.ddp_search(network).items():
        hosts.setdefault(ip, {})["ddp"] = entry
    lines = discover.avahi_browse(timeout=10 if arguments.quick else 25)
    for ip, entry in discover.parse_avahi(lines).items():
        hosts.setdefault(ip, {})["mdns"] = entry
    report = discover.compare(inventory.load(oh_cli.HOUSE), hosts)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scan.json").write_text(json.dumps({"local": local, "network": str(network),
                                               "hosts": hosts, "report": report},
                                              indent=1, sort_keys=True) + "\n")
    if arguments.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"scanned {network} from {local}: {len(hosts)} hosts -> {out / 'scan.json'}")
        for device in report["moved"]:
            print(f"  moved    {device['id']}: {device['inventory']} -> {device['seen']}")
        for device_id in report["missing"]:
            print(f"  missing  {device_id} (no answer by MAC; off, asleep or gone)")
        for entry in report["address_reused"]:
            print(f"  reused   {entry['ip']}: house file {entry['inventory']}, now {entry['mac']}")
        for host in report["unknown"]:
            print(f"  unknown  {host['ip']} {host['mac']} {host['vendor']}")
    return 0


def ipaddress_in(ip: str, network) -> bool:
    import ipaddress
    try:
        return ipaddress.IPv4Address(ip) in network
    except ValueError:
        return False
