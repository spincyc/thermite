"""List the UniFi DHCP reservations instance/devices.yaml needs.

    thermite dhcp-reservations [--csv] [--house FILE]

One row per device with its own address and a MAC: the client alias to set
in UniFi, the MAC and the fixed IP. Prints only; the owner enters them in
UniFi. A device with an address but no mac gets no reservation; it is
named on stderr.
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

NOTES = """\
exit status:
  0  every device with its own address has a reservation
  1  a device with its own address has no mac, so no reservation
  2  a wrong switch, or a house file that is missing, cannot be read or
     has a problem
"""


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("dhcp-reservations", __doc__.splitlines()[0], EXAMPLES, NOTES)
    parser.add_argument("--csv", action="store_true", help="print CSV: alias, mac, ip")
    oh_cli.add_house(parser)
    arguments = parser.parse_args(argv)
    data = inventory.checked(arguments.house)
    rows = inventory.reservations(data)
    lacking = inventory.without_mac(data)
    for device_id in lacking:
        print(f"thermite dhcp-reservations: {device_id}: no mac in the house file, so no reservation",
              file=sys.stderr)
    if arguments.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["alias", "mac", "ip"])
        writer.writerows(rows)
        return 1 if lacking else 0
    width = max((len(alias) for alias, _, _ in rows), default=0)
    for alias, mac, ip in rows:
        print(f"{alias:<{width}}  {mac}  {ip}")
    print(f"{len(rows)} reservations" + (f", {len(lacking)} devices without a mac" if lacking else ""))
    return 1 if lacking else 0
