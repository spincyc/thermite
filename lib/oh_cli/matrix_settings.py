"""Compare the matrices' fixed settings with instance/devices.yaml, and apply them on request."""

from __future__ import annotations

import sys
import time
from types import ModuleType

import oh_integrations as integrations
import oh_inventory as inventory
import oh_cli
import oh_render as render
from oh_conf import uses
from oh_integrations import _instr as instr

DESCRIPTION = """\
Read every matrix that has `settings` in instance/devices.yaml (on the
BK-808: scaler, HDR and HDCP on each output, and the EDID of any input fixed
there) and print each setting that differs from what devices.yaml asks for.
With --apply, send those changes one at a time, read the settings back and
fail on any that did not take or got no answer. Nothing else is written, and
runtime never writes these settings itself. (The EDIDs of the inputs that
sources use follow the routing instead: routing.edid in devices.yaml.)

A change can blank the room on that output, or the rooms watching that
input, for a few seconds while the display and the source renegotiate:
apply when nobody is watching.
"""

EXAMPLES = (
    ("thermite matrix-settings", "each setting that differs from the house file; sends nothing"),
    ("thermite matrix-settings --apply", "send those changes, when nobody is watching"),
)

NOTES = """\
exit status:
  0  every setting matches (with --apply: every change took)
  1  settings differ (without --apply), or a change did not take
  2  the house file is invalid, or a matrix did not answer
"""

#: Seconds between two writes: the CGI serves one request at a time, and a
#: change can set off a hot-plug and a renegotiation.
PAUSE = 0.5


def settings_module(device: dict) -> ModuleType | None:
    return next((m for m in integrations.REGISTRY if hasattr(m, "plan") and uses(device, m.PHASE, m.NAME)),
                None)


def targets(data: dict, only: str | None = None) -> list[tuple[dict, ModuleType]]:
    """(device, integration) for every device with settings to apply."""
    out = []
    for device in data["devices"]:
        module = settings_module(device)
        if device.get("settings") and module and only in (None, device["id"]):
            out.append((device, module))
    return out


def read_status(module: ModuleType, address: str, request=instr.request) -> dict[str, dict]:
    return {comhead: request(address, instr.body(comhead, language=0)) for comhead in module.STATUS}


def send(changes: list[tuple[str, str]], address: str, request=instr.request, pause: float = PAUSE,
         log=print) -> list[str]:
    """Send each change; return those the matrix refused or did not answer.
    A change without an answer does not stop the rest: the read-back that
    follows shows what took."""
    refused = []
    for index, (what, body) in enumerate(changes):
        if index:
            time.sleep(pause)
        try:
            reply = request(address, body)
        except (OSError, ValueError) as exc:
            refused.append(f"{what} (no answer: {exc})")
            log(f"  no answer  {what}")
            continue
        if not isinstance(reply, dict) or reply.get("result", 1) != 1:
            refused.append(f"{what} (answer {reply})")
        log(f"  sent  {what}")
    return refused


def run(device: dict, module: ModuleType, address: str, apply: bool, request=instr.request,
        pause: float = PAUSE, log=print) -> int:
    """Check (and with apply, fix) one matrix; returns its exit status."""
    where = f"{device['id']} ({device.get('name') or device.get('model')}, {address})"
    changes = module.plan(device, read_status(module, address, request))
    if not changes:
        log(f"{where}: every setting matches")
        return 0
    log(f"{where}: {len(changes)} setting(s) differ")
    if not apply:
        for what, _ in changes:
            log(f"  {what}")
        return 1
    refused = send(changes, address, request, pause, log)
    remaining = module.plan(device, read_status(module, address, request))
    for what in refused:
        log(f"  REFUSED  {what}")
    for what, _ in remaining:
        log(f"  NOT TAKEN  {what}")
    if refused or remaining:
        return 1
    log(f"{where}: applied; every setting matches")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("matrix-settings", DESCRIPTION, EXAMPLES, NOTES)
    oh_cli.add_apply(parser, "send the settings to the matrices")
    parser.add_argument("--device", metavar="ID", help="only this device id")
    parser.add_argument("--address", metavar="HOST[:PORT]",
                        help="talk to this address instead of the device's ip (a simulator); needs --device")
    oh_cli.add_house(parser, render.INVENTORY)
    args = parser.parse_args(argv)
    if args.address and not args.device:
        parser.error("--address needs --device")
    data = inventory.load(args.house)
    problems = render.validate(data)
    if problems:
        for problem in problems:
            print(f"thermite matrix-settings: {problem}", file=sys.stderr)
        return 2
    chosen = targets(data, args.device)
    if not chosen:
        print(f"thermite matrix-settings: no device{' ' + args.device if args.device else ''} has settings "
              "to apply", file=sys.stderr)
        return 2
    status = 0
    for device, module in chosen:
        try:
            status = max(status, run(device, module, args.address or device["ip"], args.apply))
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            print(f"thermite matrix-settings: {device['id']}: no usable answer: {exc}", file=sys.stderr)
            status = 2
    return status
