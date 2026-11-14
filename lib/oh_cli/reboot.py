"""Restart a device now, or show how each device's last reboot went.

    thermite reboot [DEVICE]
                     restart DEVICE as the nightly reboots do and wait for
                     it to answer again; without DEVICE, each device
                     thermite can restart and its last reboot
"""

from __future__ import annotations

import datetime
import json
import sys
import time

import oh_cli
import oh_inventory as inventory
import oh_render as render
from oh_cli import pair
from oh_conf import House
from oh_features import reboots

DESCRIPTION = """\
Restart a device as the nightly reboots do (lib/oh_features/reboots):
production thermite restarts it only if it answers and is idle (a receiver
with every zone off, a switch whose input has no signal), then waits for
it to stop answering and answer again, and reports how that went. DEVICE
need not have `reboot: nightly` in the house file; it needs an integration
under control that can restart it without cutting its power. Without
DEVICE, each such device: whether it restarts nightly, and its last
reboot.
"""

EXAMPLES = (
    ("thermite reboot", "each device thermite can restart, and its last reboot"),
    ("thermite reboot DEVICE", "restart it now; wait until it answers again"),
)

NOTES = """\
exit status:
  0  the device is back, or the list was shown
  1  it was skipped (in use, or not answering), was not seen to restart,
     or did not answer again in time
  2  the house file is invalid, no such device thermite can restart, or
     thermite could not be reached or has no house_reboot Item
"""

#: The rule waits up to 5 minutes for a device to answer again, after up
#: to 2 minutes for one restarting before it.
TIMEOUT = 480.0
POLL = 2.0


def error(message: str) -> int:
    print(f"thermite reboot: {message}", file=sys.stderr)
    return 2


def report(http) -> dict:
    """Each device's last reboot, as the rule reports it ({} before any)."""
    try:
        devices = json.loads(pair.item_state(http, reboots.ITEM)).get("devices")
    except (ValueError, AttributeError):
        return {}
    return devices if isinstance(devices, dict) else {}


def when(last: dict) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(last.get("at")).replace("Z", "+00:00"))
    except ValueError:
        return None


def outcome(last: dict) -> str:
    at = when(last)
    text = str(last.get("outcome"))
    if last.get("detail"):
        text += f" ({last['detail']})"
    if isinstance(last.get("seconds"), int):
        text += f" after {last['seconds']} s"
    return text + (f", {at.astimezone():%Y-%m-%d %H:%M}" if at else "")


def listing(house: House, http, log=print) -> int:
    last = report(http)
    for entry in reboots.table(house)["devices"]:
        device = next(d for d in house.devices if d["id"] == entry["id"])
        how = f"nightly at {reboots.time_of(house.data)}" if entry["nightly"] else "on command only"
        done = f"last: {outcome(last[entry['id']])}" if isinstance(last.get(entry["id"]), dict) else "not restarted yet"
        log(f"{house.label(device)} ({entry['id']}): {how}; {done}")
    return 0


def restart(house: House, ident: str, http, timeout: float, log=print, clock=time.monotonic, sleep=time.sleep) -> int:
    device = next(d for d in house.devices if d["id"] == ident)
    asked = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    pair.command(http, reboots.ITEM, ident)
    log(f"Restarting {house.label(device)} ({ident}) once it is idle; waiting for it to answer again.")
    deadline = clock() + timeout
    while True:
        last = report(http).get(ident)
        at = when(last) if isinstance(last, dict) else None
        if at and at >= asked and last.get("outcome") != "rebooting":
            log(f"{ident}: {outcome(last)}")
            return 0 if last.get("outcome") == "back" else 1
        if clock() >= deadline:
            log(f"{ident}: no outcome within {timeout:g} s; thermite reboot lists it once there is one")
            return 1
        sleep(POLL)


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("reboot", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("device", nargs="?", metavar="DEVICE", help="the device's id in the house file")
    oh_cli.add_house(parser, render.INVENTORY)
    oh_cli.add_timeout(parser, TIMEOUT, "the device to answer again")
    args = parser.parse_args(argv)

    data = inventory.load(args.house)
    problems = render.validate(data)
    if problems:
        for problem in problems:
            print(f"thermite reboot: {problem}", file=sys.stderr)
        return 2
    house = House(data)
    able = [entry["id"] for entry in reboots.table(house)["devices"]]
    if args.device is not None and args.device not in able:
        return error(f"not a device thermite can restart: {args.device} (those are: {', '.join(able) or 'none'})")
    if not able:
        return error("no device in the house file has an integration that can restart it")
    try:
        http = pair.openhab(False)
        if args.device is None:
            return listing(house, http)
        return restart(house, args.device, http, args.timeout)
    except pair.OpenHABError as exc:
        return error(str(exc))
