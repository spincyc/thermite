"""Run the NuHeat climate helper, commission thermostats privately, or inspect
thermostat status.

    thermite climate serve [--listen 127.0.0.1:13629] [--secrets DIR] [--allow-remote]
    thermite climate commission <DEVICE> --serial <SERIAL> --encoding <ENC> --min-raw <N> --max-raw <N> [--modes Auto,Hold,Manual] [--mode-code Auto=1 ...] [--secrets DIR]
    thermite climate status [DEVICE] [--url http://127.0.0.1:13629] [--secrets DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import oh_cli
from oh_climate import daemon, nuheat_api

DESCRIPTION = """\
Manage the localhost NuHeat climate helper (lib/oh_climate/daemon.py): serve
its loopback HTTP API, commission per-device NuHeat contracts privately into
0600 service state, or query thermostat status and freshness.
"""

SERVE = """\
`serve` runs the localhost climate helper daemon on 127.0.0.1:13629 until
SIGTERM or Ctrl-C, refusing non-loopback addresses without --allow-remote.
"""

COMMISSION = """\
`commission` validates a per-device NuHeat contract (serial number, verified
raw temperature encoding, raw min/max bounds, writable modes, and optional
readback mode codes) via nuheat_api.DeviceContract and writes it atomically
with mode 0600 inside --secrets DIR. Serial numbers stay private and are never
stored in devices.yaml or printed back.
"""

STATUS = """\
`status` queries the localhost climate helper daemon and reports thermostat
readings, cloud-receipt freshness, commissioning state, and last write state.
"""

NOTES = """\
private state:
  Commissioning contracts and OAuth tokens are stored under --secrets DIR
  (default var/secrets/climate; directory 0700, files 0600) and are never
  written to devices.yaml, Git, logs, or HTTP responses.
"""

EXAMPLES = (
    ("thermite climate serve", f"run the localhost helper on {daemon.LISTEN}"),
    ("thermite climate commission bath_floor --serial SN1 --encoding celsius_x100 --min-raw 500 --max-raw 3500 --modes Auto,Hold,Manual --mode-code Auto=1 --mode-code Hold=2 --mode-code Manual=3",
     "write a private 0600 commissioning contract"),
    ("thermite climate status bath_floor --json", "query one thermostat's status as JSON"),
)

SERVE_EXAMPLES = (
    ("thermite climate serve", f"serve on {daemon.LISTEN} until Ctrl-C"),
    ("thermite climate serve --secrets /secrets", "as thermite-climate.service runs it"),
)

COMMISSION_EXAMPLES = (
    ("thermite climate commission bath_floor --serial SN1 --encoding celsius_x100 --min-raw 500 --max-raw 3500",
     "commission a thermostat with verified celsius_x100 limits"),
)

STATUS_EXAMPLES = (
    ("thermite climate status", "query health and every commissioned thermostat"),
    ("thermite climate status bath_floor --json", "print one thermostat's status as JSON"),
)


def _parse_modes(text: str) -> tuple[str, ...]:
    if not text.strip():
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _parse_mode_codes(entries: list[str] | None) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for entry in entries or ():
        name, sep, raw = entry.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"invalid --mode-code {entry!r}; use MODE=INT (e.g. Auto=1)")
        try:
            code = int(raw.strip())
        except ValueError:
            raise ValueError(f"invalid --mode-code {entry!r}; code must be an integer") from None
        mapping[name.strip()] = code
    return mapping


def _get_json(url: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read()
        error.close()
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {"ok": False, "error": f"HTTP {error.code}"}
        return error.code, body


def _format_device(entry: dict[str, Any]) -> str:
    ident = entry.get("id", "?")
    if not entry.get("commissioned"):
        return f"{ident}: not commissioned"
    state = "online" if entry.get("online") is True else "offline" if entry.get("online") is False else "unknown"
    temp = entry.get("temperature_display")
    if temp is None:
        temp = entry.get("temperature_raw")
    sp = entry.get("setpoint_display")
    if sp is None:
        sp = entry.get("setpoint_raw")
    mode = entry.get("mode") or (f"raw:{entry['mode_raw']}" if entry.get("mode_raw") is not None else "unknown")
    parts = [f"{ident}: {state}", f"mode={mode}"]
    if temp is not None:
        parts.append(f"temp={temp}")
    if sp is not None:
        parts.append(f"setpoint={sp}")
    if entry.get("heating_call") is True:
        parts.append("heating")
    if entry.get("stale") is True:
        parts.append("STALE")
    if entry.get("has_error") is True:
        parts.append("FAULT")
    write_state = entry.get("last_write_state")
    if write_state and write_state != "idle":
        parts.append(f"write={write_state}")
    if entry.get("error"):
        parts.append(f"error={entry['error']}")
    return "  ".join(parts)


def cmd_serve(arguments: argparse.Namespace) -> int:
    argv = ["--listen", arguments.listen, "--secrets", str(arguments.secrets)]
    if arguments.allow_remote:
        argv.append("--allow-remote")
    return daemon.main(argv)


def cmd_commission(arguments: argparse.Namespace) -> int:
    try:
        modes = _parse_modes(arguments.modes)
        codes = _parse_mode_codes(arguments.mode_code)
        contract = nuheat_api.DeviceContract(
            serial=arguments.serial,
            writable_modes=modes,
            temperature_encoding=arguments.encoding,
            minimum_raw=arguments.min_raw,
            maximum_raw=arguments.max_raw,
            mode_codes=codes,
        )
        path = daemon.write_contract(arguments.secrets, arguments.device, contract)
    except (ValueError, OSError) as exc:
        print(f"thermite climate commission: {exc}", file=sys.stderr)
        return 2
    summary = {
        "device": arguments.device,
        "encoding": contract.temperature_encoding,
        "min_raw": contract.minimum_raw,
        "max_raw": contract.maximum_raw,
        "writable_modes": list(contract.writable_modes),
        "mode_codes": dict(contract.mode_codes),
        "path": str(path),
    }
    if arguments.json:
        oh_cli.emit(summary)
    else:
        print(
            f"commissioned {arguments.device}: encoding={contract.temperature_encoding} "
            f"limits={contract.minimum_raw}..{contract.maximum_raw} "
            f"modes={','.join(contract.writable_modes) or 'none'} ({path})"
        )
    return 0


def cmd_status(arguments: argparse.Namespace) -> int:
    base = arguments.url.rstrip("/")
    try:
        if arguments.device:
            code, data = _get_json(f"{base}/devices/{arguments.device}")
            if arguments.json:
                oh_cli.emit(data)
            else:
                print(_format_device(data))
            return 0 if code == 200 and data.get("ok") is True else 1
        code, health = _get_json(f"{base}/health")
        if code != 200 or not isinstance(health, dict) or not health.get("ok"):
            report = {"ok": False, "health": health, "devices": {}}
            if arguments.json:
                oh_cli.emit(report)
            else:
                print(f"climate helper at {base}: unhealthy (HTTP {code})", file=sys.stderr)
            return 1
        devices: dict[str, Any] = {}
        ok = True
        for ident in sorted(daemon.load_contracts(arguments.secrets)):
            dcode, ddata = _get_json(f"{base}/devices/{ident}")
            devices[ident] = ddata
            if dcode != 200 or ddata.get("ok") is not True or ddata.get("has_error") is True:
                ok = False
        report = {"ok": ok, "health": health, "devices": devices}
        if arguments.json:
            oh_cli.emit(report)
        else:
            print(f"climate helper {base}: ok (commissioned={health.get('commissioned', 0)})")
            for entry in devices.values():
                print("  " + _format_device(entry))
        return 0 if ok else 1
    except (OSError, ValueError) as exc:
        if arguments.json:
            oh_cli.emit({"ok": False, "error": str(exc)})
        else:
            print(f"thermite climate status: cannot reach {base}: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("climate", "\n".join((DESCRIPTION, SERVE, COMMISSION, STATUS)), EXAMPLES, NOTES)
    commands = oh_cli.actions(parser)

    serve_parser = oh_cli.action(commands, "serve", "run the localhost climate helper daemon",
                                 description=SERVE, examples=SERVE_EXAMPLES, notes=NOTES)
    serve_parser.add_argument("--listen", default=daemon.LISTEN, metavar="HOST:PORT",
                              help=f"address to serve on (default {daemon.LISTEN})")
    serve_parser.add_argument("--secrets", type=Path, default=daemon.SECRETS, metavar="DIR",
                              help="where commissioning contracts and OAuth tokens are stored (default var/secrets/climate)")
    serve_parser.add_argument("--allow-remote", action="store_true",
                              help="allow a non-loopback --listen address (no authentication)")

    commission_parser = oh_cli.action(commands, "commission", "validate and store a private 0600 device contract",
                                      description=COMMISSION, examples=COMMISSION_EXAMPLES, notes=NOTES)
    commission_parser.add_argument("device", metavar="DEVICE", help="inventory device id (snake_case)")
    commission_parser.add_argument("--serial", required=True, metavar="SERIAL",
                                   help="thermostat serial number (stored privately in 0600 state only)")
    commission_parser.add_argument("--encoding", required=True, metavar="ENC",
                                   help="verified raw temperature encoding (e.g. celsius_x100, fahrenheit_x100, nuheat_raw_f)")
    commission_parser.add_argument("--min-raw", required=True, type=int, metavar="N",
                                   help="verified minimum setpoint in raw units")
    commission_parser.add_argument("--max-raw", required=True, type=int, metavar="N",
                                   help="verified maximum setpoint in raw units")
    commission_parser.add_argument("--modes", default="Auto,Hold,Manual", metavar="MODES",
                                   help="comma-separated writable modes (default: Auto,Hold,Manual)")
    commission_parser.add_argument("--mode-code", action="append", default=[], metavar="MODE=INT",
                                   help="verified readback mode integer mapping (repeatable, e.g. Auto=1)")
    commission_parser.add_argument("--secrets", type=Path, default=daemon.SECRETS, metavar="DIR",
                                   help="private secrets directory (default var/secrets/climate)")
    oh_cli.add_json(commission_parser)

    status_parser = oh_cli.action(commands, "status", "query thermostat status from the localhost climate helper",
                                  description=STATUS, examples=STATUS_EXAMPLES)
    status_parser.add_argument("device", nargs="?", metavar="DEVICE", help="optional thermostat device id")
    status_parser.add_argument("--url", default=f"http://{daemon.LISTEN}", metavar="URL",
                               help=f"climate helper base URL (default http://{daemon.LISTEN})")
    status_parser.add_argument("--secrets", type=Path, default=daemon.SECRETS, metavar="DIR",
                               help="private secrets directory used when listing commissioned devices")
    oh_cli.add_json(status_parser)

    arguments = parser.parse_args(argv)
    if arguments.command == "serve":
        return cmd_serve(arguments)
    if arguments.command == "commission":
        return cmd_commission(arguments)
    return cmd_status(arguments)
