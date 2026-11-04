"""List the UniFi DHCP reservations instance/devices.yaml needs.

    thermite dhcp-reservations [--csv]

One row per device with its own address: the client alias to set in UniFi,
the MAC and the fixed IP. Prints only; the owner enters them in UniFi.
"""

from __future__ import annotations

import csv
import sys

import oh_cli
import oh_inventory as inventory

EXAMPLES = (
    ("thermite dhcp-reservations", "one line per reservation: alias, MAC and IP"),
    ("thermite dhcp-reservations --csv", "the same as CSV, with a header row"),
)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("dhcp-reservations", __doc__.splitlines()[0], EXAMPLES)
    parser.add_argument("--csv", action="store_true", help="print CSV: alias, mac, ip")
    arguments = parser.parse_args(argv)
    rows = inventory.reservations(inventory.load(oh_cli.HOUSE))
    if arguments.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["alias", "mac", "ip"])
        writer.writerows(rows)
        return 0
    width = max(len(alias) for alias, _, _ in rows)
    for alias, mac, ip in rows:
        print(f"{alias:<{width}}  {mac}  {ip}")
    print(f"{len(rows)} reservations")
    return 0
