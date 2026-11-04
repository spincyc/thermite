"""Scan the LAN read-only and compare what answers with instance/devices.yaml.

    thermite discover [--no-ping] [--quick] [--network CIDR] [--save | --output DIR] [--json]

Prints the house file's devices that moved address or did not answer, and
AV-looking hosts the house file does not know; with --save or --output it
also writes the whole scan to scan.json. Nothing is commanded; see
lib/oh_discover.py.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import subprocess
import sys
from pathlib import Path

import oh_discover as discover
import oh_cli
import oh_inventory as inventory
from oh_cli import AREA

EXAMPLES = (
    ("thermite discover", "ping the LAN, listen for device announcements, and compare"),
    ("thermite discover --no-ping --quick", "the neighbour table and short listening windows only"),
    ("thermite discover --network 192.0.2.0/24", "ping that network instead of this host's"),
    ("thermite discover --save", "also keep the whole scan under var/discovery/<UTC time>/"),
    ("thermite discover --json", "the comparison as JSON"),
)

NOTES = f"""\
It pings every address of this host's network only when that network is a
/{discover.SWEEP_PREFIX} or narrower; name a wider one with --network to ping it.

exit status:
  0  every house device with a mac answered at its address, and no address
     is reused
  1  a house device moved address or did not answer, or its address now
     answers with another MAC
  2  a wrong switch, a house file with a problem, or no network to scan
"""


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("discover", __doc__.splitlines()[0], EXAMPLES, NOTES)
    parser.add_argument("--no-ping", action="store_true",
                        help="use the neighbour table only; do not ping the network")
    parser.add_argument("--quick", action="store_true",
                        help="listen for device announcements for less time")
    parser.add_argument("--network", type=ipaddress.IPv4Network, metavar="CIDR",
                        help="the network to scan (default: this host's, on its default route)")
    save = parser.add_mutually_exclusive_group()
    save.add_argument("--save", action="store_true", help="write scan.json under var/discovery/<UTC time>/")
    save.add_argument("--output", type=Path, metavar="DIR", help="write scan.json in DIR")
    oh_cli.add_house(parser)
    oh_cli.add_json(parser)
    arguments = parser.parse_args(argv)
    data = inventory.checked(arguments.house)
    try:
        local, network = discover.local_interface()
    except (OSError, subprocess.CalledProcessError, RuntimeError, AttributeError) as exc:
        print(f"thermite discover: cannot find this host's network: {exc}", file=sys.stderr)
        return 2
    network = arguments.network or network
    if not arguments.no_ping:
        refusal = discover.sweep_refusal(network, arguments.network is not None)
        if refusal:
            print(f"thermite discover: {refusal}", file=sys.stderr)
            return 2
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
    report = discover.compare(data, hosts)
    out = None
    if arguments.save or arguments.output:
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        out = (arguments.output or AREA / "var/discovery" / stamp) / "scan.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"local": local, "network": str(network), "hosts": hosts, "report": report},
                                  indent=1, sort_keys=True) + "\n")
    if arguments.json:
        oh_cli.emit(report)
    else:
        print(f"scanned {network} from {local}: {len(hosts)} hosts" + (f" -> {out}" if out else ""))
        for device in report["moved"]:
            print(f"  moved    {device['id']}: {device['inventory']} -> {device['seen']}")
        for device_id in report["missing"]:
            print(f"  missing  {device_id} (no answer by MAC; off, asleep or gone)")
        for entry in report["address_reused"]:
            print(f"  reused   {entry['ip']}: house file {entry['inventory']}, now {entry['mac']}")
        for host in report["unknown"]:
            print(f"  unknown  {host['ip']} {host['mac']} {host['vendor']}")
        for device_id in report["no_mac"]:
            print(f"  no mac   {device_id} (the house file has no mac to match it by)")
    return 1 if report["moved"] or report["missing"] or report["address_reused"] else 0


def ipaddress_in(ip: str, network) -> bool:
    try:
        return ipaddress.IPv4Address(ip) in network
    except ValueError:
        return False
