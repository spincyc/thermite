"""Compare the matrices' fixed settings with instance/devices.yaml, and apply them on request.

    thermite matrix-settings [DEVICE] [--apply] [--address HOST[:PORT]] [--house FILE] [--json]
"""

from __future__ import annotations

import argparse
import sys
import time
from types import ModuleType

import oh_integrations as integrations
import oh_cli
from oh_conf import House, uses
from oh_features import routing
from oh_integrations import _instr as instr

DESCRIPTION = """\
Compare each matrix (or only DEVICE) with its `settings` in
instance/devices.yaml and print the differences. On the BK-808 these settings cover output scalers, HDR
conversion, HDCP and fixed input EDIDs. When a room has hdmi_audio_scaler,
routing sets the scaler on its audio output and this command skips it.
Inputs with a routing EDID follow routing.edid when a room selects a source.

With --apply, send the fixed-setting changes one at a time, read them back
and fail on any that did not take or got no answer. With --json, one JSON
object instead: for each matrix, the settings that differ and, with
--apply, those that took, those refused and those not taken.

A change can blank the room on that output, or the rooms watching that
input, for a few seconds while the display and the source renegotiate:
apply when nobody is watching.
"""

EXAMPLES = (
    ("thermite matrix-settings", "each setting that differs from the house file; sends nothing"),
    ("thermite matrix-settings --apply", "send those changes, when nobody is watching"),
    ("thermite matrix-settings DEVICE", "only that matrix"),
    ("thermite matrix-settings --json", "the settings that differ, as JSON"),
)

NOTES = """\
exit status:
  0  every setting matches (with --apply: every change took)
  1  settings differ (without --apply), or a change did not take
  2  the house file is invalid, no device (or not DEVICE) has settings to
     apply, or a matrix did not answer
"""

#: Seconds between two writes: the CGI serves one request at a time, and a
#: change can set off a hot-plug and a renegotiation.
PAUSE = 0.5


def settings_module(device: dict) -> ModuleType | None:
    return next((m for m in integrations.defining("plan") if uses(device, m.PHASE, m.NAME)), None)


def targets(house: House, only: str | None = None) -> list[tuple[dict, ModuleType]]:
    """(device, integration) for every device with settings to apply."""
    out = []
    for device in house.devices:
        module = settings_module(device)
        if device.get("settings") and module and only in (None, device["id"]):
            out.append((device, module))
    return out


def read_status(module: ModuleType, address: str, request=instr.request) -> dict[str, dict]:
    return {comhead: request(address, instr.body(comhead, language=0)) for comhead in module.STATUS}


def sent(changes: list[tuple[str, str]], address: str, request=instr.request, pause: float = PAUSE,
         log=print) -> list[tuple[str, str]]:
    """Send each change; return (change, why) for each the matrix refused
    or did not answer. A change without an answer does not stop the rest:
    the read-back that follows shows what took."""
    refused = []
    for index, (what, body) in enumerate(changes):
        if index:
            time.sleep(pause)
        try:
            reply = request(address, body)
        except (OSError, ValueError) as exc:
            refused.append((what, f"no answer: {exc}"))
            log(f"  no answer  {what}")
            continue
        if not isinstance(reply, dict) or reply.get("result", 1) != 1:
            refused.append((what, f"answer {reply}"))
        log(f"  sent  {what}")
    return refused


def send(changes: list[tuple[str, str]], address: str, request=instr.request, pause: float = PAUSE,
         log=print) -> list[str]:
    """Send each change; return those the matrix refused or did not answer."""
    return [f"{what} ({why})" for what, why in sent(changes, address, request, pause, log)]


def run(device: dict, module: ModuleType, address: str, apply: bool, request=instr.request,
        pause: float = PAUSE, log=print, routed: dict[int, str] | None = None, record: dict | None = None) -> int:
    """Check one matrix's fixed settings, applying changes when requested.
    `routed` maps each routing-owned scaler output to its room. `record`,
    if given, gets the --json report on the matrix, as far as it got."""
    where = f"{device['id']} ({device.get('name') or device.get('model')}, {address})"
    routed = routed or {}
    skip = frozenset(routed)
    record = {} if record is None else record
    record.update(device=device["id"], address=address, routed={str(output): room for output, room in routed.items()})
    for output, room in sorted(routed.items()):
        log(f"{where}: output {output} scaler left to the routing ({room})")
    changes = module.plan(device, read_status(module, address, request), skip)
    record["differ"] = [what for what, _ in changes]
    if not changes:
        log(f"{where}: every setting matches")
        record["outcome"] = "matches"
        return 0
    log(f"{where}: {len(changes)} setting(s) differ")
    if not apply:
        for what, _ in changes:
            log(f"  {what}")
        record["outcome"] = "differs"
        return 1
    refused = sent(changes, address, request, pause, log)
    remaining = module.plan(device, read_status(module, address, request), skip)
    for what, why in refused:
        log(f"  REFUSED  {what} ({why})")
    for what, _ in remaining:
        log(f"  NOT TAKEN  {what}")
    failed = {what for what, _ in refused} | {what for what, _ in remaining}
    record.update({"set": [what for what, _ in changes if what not in failed],
                   "refused": [f"{what} ({why})" for what, why in refused],
                   "not_taken": [what for what, _ in remaining],
                   "outcome": "not applied" if refused or remaining else "applied"})
    if refused or remaining:
        return 1
    log(f"{where}: applied; every setting matches")
    return 0


#: The --json report's status, by exit status.
STATUS = ("ok", "problems", "failed")


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("matrix-settings", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("device", nargs="?", metavar="DEVICE", help="only this device (its id in the house file)")
    oh_cli.add_apply(parser, "send the settings to the matrices")
    # DEVICE's spelling before it was a positional, for one release.
    parser.add_argument("--device", dest="old_device", metavar="ID", help=argparse.SUPPRESS)
    parser.add_argument("--address", metavar="HOST[:PORT]",
                        help="talk to this address instead of the device's ip (a simulator); needs DEVICE")
    oh_cli.add_house(parser)
    oh_cli.add_json(parser)
    args = parser.parse_args(argv)
    if args.old_device is not None:
        if args.device is not None:
            parser.error("DEVICE and --device both name a device; give only DEVICE")
        oh_cli.renamed(parser.prog, "--device", "DEVICE")
        args.device = args.old_device
    if args.address and not args.device:
        parser.error("--address needs DEVICE")
    log = (lambda *_: None) if args.json else print
    house = House.load(args.house)
    chosen = targets(house, args.device)
    if not chosen:
        message = f"no device{' ' + args.device if args.device else ''} has settings to apply"
        if args.json:
            oh_cli.emit({"apply": args.apply, "devices": [], "error": message, "status": STATUS[2]})
        else:
            print(f"thermite matrix-settings: {message}", file=sys.stderr)
        return 2
    status, records = 0, []
    for device, module in chosen:
        record: dict = {"device": device["id"]}
        records.append(record)
        try:
            status = max(status, run(device, module, args.address or device["ip"], args.apply, instr.request, PAUSE,
                                     log, routing.routed_scalers(house, device["id"]), record))
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            if not args.json:
                print(f"thermite matrix-settings: {device['id']}: no usable answer: {exc}", file=sys.stderr)
            record.update(outcome="no answer", error=f"no usable answer: {exc}")
            status = 2
    if args.json:
        oh_cli.emit({"apply": args.apply, "devices": records, "status": STATUS[status]})
    return status
