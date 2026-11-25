"""Pair a device with thermite, or read pairable devices' pairing status.

    thermite pair DEVICE [CODE] [--production | --sandbox]
                     pair any device whose integration implements the
                     _pairing Item contract (Sony TV, Android TV / SHIELD,
                     Apple TV): ask it to show a code and send the code back
    thermite pair [--production | --sandbox] [--json]
                     whether each pairable device is paired with thermite
    thermite pair DEVICE --status [--production | --sandbox] [--json]
                     whether that device is paired with thermite
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse

import oh_cli
import oh_deploy as deploy
import oh_integrations as integrations
import oh_sandbox as sandbox
from oh_conf import House
from oh_integrations import _pairing

DESCRIPTION = """\
Pair a device with thermite through its pairing Items (the same _pairing
Item contract the Pairing page drives), for every integration in the house
file (instance/devices.yaml) that defines a pairing() hook (Sony TVs,
Android TVs / NVIDIA SHIELDs, and Apple TVs):

  - Without DEVICE: reads every pairable device's pairing status from
    production thermite (or with --sandbox, the running sandbox).
  - With DEVICE --status: reads that device's pairing status alone.
  - With DEVICE: asks thermite to make the device show a pairing code on
    its screen, asks you to type that code, and sends it back through the
    device's pairing Item. When a paired device requires confirmation to
    pair again, asks before replacing its pairing.
  - With DEVICE CODE: sends the code the device is already showing.
"""

EXAMPLES = (
    ("thermite pair DEVICE", "have DEVICE show a pairing code, then type it to pair with production"),
    ("thermite pair DEVICE --sandbox", "the same with the sandbox"),
    ("thermite pair DEVICE 1234", "send the code DEVICE shows already"),
    ("thermite pair", "whether every pairable device is paired with production"),
    ("thermite pair DEVICE --status --sandbox", "whether DEVICE is paired with the sandbox"),
    ("thermite pair --json", "every pairable device's pairing status as JSON"),
)

NOTES = """\
exit status:
  0  done: the device is paired, every device checked is paired, or
     re-pairing was declined
  1  a device showed no code, refused the code, did not answer, its pairing
     port was closed, or a checked device is not paired
  2  the house file is invalid, no such pairable device, invalid code
     format, thermite could not be reached or has no such Items, or the
     sandbox is not running

--json:
  Without DEVICE, or with --status, one object: target (production or
  sandbox), paired (whether every device read is), and tvs, one entry for
  each pairable device: id, label, paired, and its state and problem as
  thermite reports them, or error where its Items could not be read.
"""

#: How long thermite's rule may take to settle a pairing command (Apple TV
#: helper discovery + pairing start can take up to 35 s).
STATUS_TIMEOUT = 35.0
POLL = 0.25


class OpenHABError(Exception):
    """thermite could not be reached, or refused a request, with the reason."""


def pairable(house: House) -> dict[str, tuple[dict, dict]]:
    """Device id -> (device, pairing spec) for every device whose control
    integration declares a pairing() hook."""
    out: dict[str, tuple[dict, dict]] = {}
    for device in house.devices:
        spec = next((found for module in integrations.defining("pairing")
                     if (found := module.pairing(device)) is not None), None)
        if spec is not None:
            out[device["id"]] = (device, spec)
    return out


def name(device: dict) -> str:
    return f"{device.get('name') or device['id']} ({device['id']}, {device['ip']})"


def error(message: str) -> int:
    print(f"thermite pair: {message}", file=sys.stderr)
    return 2


def openhab(use_sandbox: bool) -> sandbox.Http:
    """A client of production thermite on this host, with the API token
    bin/thermite install saved, or of the running sandbox, with its own."""
    if use_sandbox:
        inst = sandbox.sandbox_instance()
        if not sandbox.running_pgid(inst):
            raise OpenHABError("the sandbox is not running; start it first (thermite sandbox start)")
        url, token_file = inst.url, inst.token_file
        hint = "start the sandbox without --no-token"
    else:
        host = deploy.load(oh_cli.AREA)[1]
        url, token_file = deploy.production_url(host), oh_cli.AREA / deploy.token_file(deploy.hostname())
        hint = "bin/thermite install saves production's API token there (docs/installation.md)"
    if not token_file.is_file():
        raise OpenHABError(f"no API token in {token_file}: {hint}")
    return sandbox.Http(url, token=sandbox.read_token(token_file), timeout=10, allow_write=True)


def item_state(http: sandbox.Http, item: str) -> str:
    """An Item's state; OpenHABError if thermite has no such Item or does not answer."""
    code, body = http.try_get_json(f"/rest/items/{urllib.parse.quote(item)}")
    if code != 200 or not isinstance(body, dict):
        reason = f"HTTP {code}" if code else "no answer"
        raise OpenHABError(f"thermite at {http.base} has no Item {item} ({reason}); is this conf/ deployed?")
    return str(body.get("state"))


def command(http: sandbox.Http, item: str, value: str) -> None:
    try:
        http.request("POST", f"/rest/items/{urllib.parse.quote(item)}", value.encode(),
                     {"Content-Type": "text/plain"})
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OpenHABError(f"thermite at {http.base} refused a command to {item} (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise OpenHABError(f"thermite at {http.base} did not answer ({getattr(exc, 'reason', exc)})") from exc


def settle(http: sandbox.Http, item: str, done, timeout: float) -> str:
    """The Item's state once done(state) holds, or its last state after timeout seconds."""
    deadline = time.monotonic() + timeout
    while True:
        state = item_state(http, item)
        if done(state) or time.monotonic() >= deadline:
            return state
        time.sleep(POLL)


def pairing_command(device: dict, spec: dict, http: sandbox.Http, value: str, timeout: float) -> dict:
    """Send `value` to the device's pairing Item and wait for its outcome.

    Integrations that expose a result Item carry a unique acknowledgement id
    and safe failure detail; others settle on their status Item transitioning
    to a terminal _pairing.STATES value.
    """
    result_item = spec.get("result")
    if result_item:
        before = item_state(http, result_item)
        command(http, spec["item"], value)

        def completed(text: str) -> bool:
            if text == before:
                return False
            try:
                answer = json.loads(text)
                return isinstance(answer, dict) and bool(answer.get("id")) and answer.get("state") in _pairing.STATES
            except (ValueError, TypeError):
                return False

        answer = settle(http, result_item, completed, timeout)
        return json.loads(answer) if completed(answer) else {"state": "checking"}

    status_item = spec["status"]
    before = item_state(http, status_item)
    command(http, spec["item"], value)
    if value == _pairing.REQUEST:
        terminal = {"code_shown", "paired", "failed", "closed", "unreachable", "unavailable"}
    elif value == _pairing.AGAIN:
        terminal = {"code_shown", "repairing", "failed", "closed", "unreachable", "unavailable"}
    else:
        terminal = {"paired", "failed", "unpaired", "closed", "unreachable", "unavailable"}
    changed = False

    def settled(state: str) -> bool:
        nonlocal changed
        if state != before:
            changed = True
        return state in terminal and (state != before or changed)

    return {"state": settle(http, status_item, settled, timeout)}


def registration_error(answer: dict) -> str | None:
    """Safe detail from a structured pairing acknowledgement; never echo raw text."""
    detail = answer.get("error")
    if not isinstance(detail, dict):
        return None
    why = ("error 1 (Internal Server Error)" if type(detail.get("code")) is int and detail["code"] == 1
           else "a device API error" if detail.get("api") is True else "no authentication credential")
    status = detail.get("http")
    http = f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
    return f"device registration failed: {why}{http}."


def _code_description(spec: dict) -> str:
    chars = "digits (0-9)" if spec["chars"] == "digits" else "letters and digits (0-9, A-F)"
    return f"{spec['length']} {chars}"


def send_code(device: dict, spec: dict, http: sandbox.Http, code: str, timeout: float, log=print) -> int:
    ident = device["id"]
    answer = pairing_command(device, spec, http, code, timeout)
    state = answer["state"]
    if state == "paired":
        log(f"paired  {name(device)}: thermite commands it from now on")
        return 0
    if state == "failed":
        detail = registration_error(answer)
        log(f"{name(device)}: {detail}" if detail else
            f"{name(device)} did not take that code; ask for a new one: bin/thermite pair {ident}")
    elif state == "closed":
        log(f"{name(device)}: its pairing port is closed; restart the device and try again")
    elif state == "unreachable":
        log(f"{name(device)} did not answer; check that it is on and reachable")
    elif state == "unavailable":
        log(f"{name(device)} cannot pair right now; check that its binding or helper is running")
    else:
        log(f"thermite reported {_pairing.STATES.get(state, state)!r} for {name(device)}; see openhab.log")
    return 1


def pair_device(device: dict, spec: dict, http: sandbox.Http, code: str | None, *, ask=input,
                timeout: float = STATUS_TIMEOUT, log=print) -> int:
    """Have a pairable device show a code through its _pairing Items, ask for
    it and send it back; with code, send the code the device shows already."""
    ident = device["id"]
    pattern = _pairing.code_pattern(spec["length"], spec["chars"])
    before = item_state(http, spec["status"])
    if code is not None:
        if not pattern.fullmatch(code):
            return error(f"{code!r} is not {_code_description(spec)}")
        if before not in ("code_shown", "repairing"):
            log(f"{name(device)} is showing no code for thermite ({_pairing.STATES.get(before, before)}); "
                f"ask for one: bin/thermite pair {ident}")
            return 1
        return send_code(device, spec, http, code, timeout, log)
    action = _pairing.REQUEST
    if before == "paired" and spec.get("confirm"):
        if not spec.get("again"):
            log(f"paired  {name(device)} is paired already")
            return 0
        reply = ask(f"{spec['confirm']} [y/N]: ").strip().lower()
        if reply not in ("y", "yes"):
            log(f"cancelled pairing {name(device)}")
            return 0
        action = _pairing.AGAIN
    answer = pairing_command(device, spec, http, action, timeout)
    state = answer["state"]
    if state == "paired":
        log(f"paired  {name(device)} is paired without needing a new code")
        return 0
    if state == "failed":
        detail = registration_error(answer)
        log(f"{name(device)}: {detail}" if detail else f"{name(device)} showed no code.")
        log(spec["hint"])
        return 1
    if state == "closed":
        log(f"{name(device)}: its pairing port is closed; restart the device and try again")
        return 1
    if state == "unreachable":
        log(f"{name(device)} did not answer; check that it is on and reachable")
        return 1
    if state == "unavailable":
        log(f"{name(device)} cannot pair right now; check that its binding or helper is running")
        return 1
    if state not in ("code_shown", "repairing"):
        log(f"thermite did not report a code on {name(device)} ({_pairing.STATES.get(state, state)}); "
            "see openhab.log")
        return 1
    typed = ask(f"The {_code_description(spec)} {device.get('name') or ident} shows: ").strip()
    if not pattern.fullmatch(typed):
        log(f"not {_code_description(spec)}; nothing sent. Ask for a new code: bin/thermite pair {ident}")
        return 2
    return send_code(device, spec, http, typed, timeout, log)


def check(entries: list[tuple[dict, dict]], http: sandbox.Http, log=print) -> int:
    status = 0
    for device, spec in entries:
        state = item_state(http, spec["status"])
        problem = item_state(http, spec["problem"]) if spec.get("problem") else "UNDEF"
        line = f"{name(device)}: {_pairing.STATES.get(state, state).lower()}"
        if problem not in ("NULL", "UNDEF"):
            line += f" ({problem})"
        log(line)
        if state != "paired" or problem not in ("NULL", "UNDEF"):
            status = 1
    return status


def report(entries: list[tuple[dict, dict]], http: sandbox.Http) -> tuple[list[dict], int]:
    """(each device's pairing for --json, the exit status check() gives, or 2
    where a device's Items could not be read)."""
    rows, status = [], 0
    for device, spec in entries:
        entry: dict = {"id": device["id"], "label": device.get("name") or device["id"]}
        try:
            state = item_state(http, spec["status"])
            problem = item_state(http, spec["problem"]) if spec.get("problem") else "UNDEF"
        except OpenHABError as exc:
            rows.append({**entry, "paired": False, "error": str(exc)})
            status = 2
            continue
        fault = None if problem in ("NULL", "UNDEF") else problem
        paired = state == "paired" and fault is None
        rows.append({**entry, "paired": paired, "state": _pairing.STATES.get(state, state).lower(),
                     "problem": fault})
        if not paired:
            status = max(status, 1)
    return rows, status


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("pair", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("device", nargs="?", metavar="DEVICE", help="the device's id in the house file")
    parser.add_argument("code", nargs="?", metavar="CODE",
                        help="the pairing code the device shows (omit to ask the device for one)")
    parser.add_argument("--status", action="store_true",
                        help="read DEVICE's pairing instead of pairing it (without DEVICE: every pairable device's)")
    oh_cli.add_renamed(parser, "--check", "--status")
    oh_cli.add_json(parser)
    oh_cli.add_target(parser, "production", "sandbox", default="production", meaning={
        "production": "production thermite on this host, with the API token bin/thermite install saved",
        "sandbox": "the running sandbox",
    })
    oh_cli.add_house(parser)
    oh_cli.add_timeout(parser, STATUS_TIMEOUT, "a pairing command to settle")
    args = parser.parse_args(argv)

    house = House.load(args.house)
    known = pairable(house)
    if args.status or args.device is None:
        if args.code is not None:
            parser.error("--status takes one DEVICE at most")
        wanted = [args.device] if args.device else list(known)
        unknown = [ident for ident in wanted if ident not in known]
        if unknown:
            return error(f"not a device that pairs: {', '.join(unknown)} "
                         f"(those are: {', '.join(known) or 'none'})")
        try:
            http = openhab(args.sandbox)
            selected = [known[ident] for ident in wanted]
            if not args.json:
                return check(selected, http)
            rows, status = report(selected, http)
        except OpenHABError as exc:
            return error(str(exc))
        oh_cli.emit({"target": "sandbox" if args.sandbox else "production",
                     "paired": all(row["paired"] for row in rows), "tvs": rows})
        return status
    if args.json:
        parser.error("--json reads the pairing: without DEVICE, or with --status")
    if args.device not in known:
        return error(f"not a device that pairs: {args.device} (those are: {', '.join(known) or 'none'})")
    device, spec = known[args.device]
    pattern = _pairing.code_pattern(spec["length"], spec["chars"])
    if args.code is not None and not pattern.fullmatch(args.code):
        return error(f"{args.code!r} is not {_code_description(spec)}")
    try:
        return pair_device(device, spec, openhab(args.sandbox), args.code, timeout=args.timeout)
    except OpenHABError as exc:
        return error(str(exc))
