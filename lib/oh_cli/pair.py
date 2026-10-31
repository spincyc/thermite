"""Pair a TV with thermite, or check the Sony TVs' pairing.

    thermite pair DEVICE [CODE] [--production | --sandbox]
                     what the device needs, by its integration in the house
                     file: a Sony TV's PIN (asked for once the TV shows it,
                     and sent through thermite), or an Android TV's pairing
                     code
    thermite pair --check [DEVICE] [--production | --sandbox]
                     whether each Sony TV is paired with thermite
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.parse

import oh_cli
import oh_deploy as deploy
import oh_inventory as inventory
import oh_render as render
import oh_sandbox as sandbox
from oh_conf import uses
from oh_integrations import _pairing
from oh_integrations import androidtv_googletv as googletv
from oh_integrations import pyatv_http
from oh_integrations import sony_ip_control

DESCRIPTION = """\
Pair a device with thermite, as its integration in the house file
(instance/devices.yaml) needs:

  - a Sony TV under sony_ip_control control: thermite commands it over
    Sony's IP control once the TV has registered thermite, with a PIN it
    shows. On the TV: Settings, Network (and Internet), Home network, IP
    control: Authentication "Normal", and Remote Start on so that it
    answers, and turns on, from standby (the menu path varies by model).
    Stand at the TV: this asks thermite (production, or with --sandbox the
    sandbox) to have the TV show four digits, through the TV's Pairing
    Items, as the Pairing page does, then asks for the digits and sends
    them back. thermite keeps the TV's registration in its own userdata;
    the PIN is never shown, logged or put in Git. With CODE it sends four
    digits the TV shows already. --check reads each TV's pairing status.
  - an Android TV or Google TV under androidtv_googletv control, with
    --sandbox: the binding reaches a TV only once the TV trusts its
    certificate, kept in the sandbox's userdata/androidtv/. Stand at the
    TV: without CODE it asks the TV to show six letters and digits, once
    the TV's pairing port (the Thing's port + 1) connects; with CODE it
    sends them (the Karaf console's openhab:androidtv <Thing UID> pincode
    REQUEST|CODE) and waits for the Thing to come ONLINE.
    Production pairs it from the remote's Pairing screen (thermite remote
    --pair) or the Pairing page.
  - an Apple TV: it pairs from the remote's Pairing screen or the Pairing
    page, through the Apple TV helper (thermite apple-tv).
"""

EXAMPLES = (
    ("thermite pair living_room_tv", "have that Sony TV show a PIN, then type it to pair it with production"),
    ("thermite pair living_room_tv --sandbox", "the same with the sandbox"),
    ("thermite pair living_room_tv 1234", "send the PIN the TV shows already"),
    ("thermite pair bedroom_tv --sandbox", "ask an Android TV to show a pairing code"),
    ("thermite pair bedroom_tv A1B2C3 --sandbox", "send the code it shows; wait for it to come ONLINE"),
    ("thermite pair --check", "whether every Sony TV is paired with production"),
)

NOTES = """\
exit status:
  0  done: the TV is paired, every TV checked is paired, or the TV was
     asked for a code
  1  a TV showed no PIN, refused the PIN, did not answer, is not paired,
     its pairing port does not connect, or it is not ONLINE after its code
  2  the house file is invalid, no such device, thermite could not be
     reached or has no such Items, the sandbox is not running, or the
     device pairs elsewhere
"""

#: How long an Android TV's Thing may take to come ONLINE after its code.
ONLINE_TIMEOUT = 30.0
#: How long an Android TV's pairing port has to connect.
CONNECT_TIMEOUT = 3.0
#: How long thermite's rule may take to report what a Sony TV made of a
#: pairing command: it waits up to 6 s for the TV.
STATUS_TIMEOUT = 15.0
POLL = 0.25
#: Where production pairs what this verb does not.
PAIRING_SCREEN = "the remote's Pairing screen (thermite remote --pair) or the Pairing page"


class OpenHABError(Exception):
    """thermite could not be reached, or refused a request, with the reason."""


def tvs(data: dict) -> dict[str, dict]:
    """Device id -> device, for every TV under sony_ip_control control."""
    return {d["id"]: d for d in data["devices"] if uses(d, sony_ip_control.PHASE, sony_ip_control.NAME)}


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
        host = deploy.load(render.AREA)[1]
        url, token_file = deploy.production_url(host), render.AREA / deploy.token_file(deploy.hostname())
        hint = "bin/thermite install saves production's API token there (docs/deploy.md)"
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


def pairing_command(device: dict, http: sandbox.Http, value: str, timeout: float) -> dict:
    """Wait for this command's acknowledgement, even when its status stays the same.

    A POST queues an Item command; its rule may not have run when the next
    GET returns. The result carries both a fresh id and the final status, so
    neither an old status nor a missed, brief checking state can settle it.
    """
    result = sony_ip_control.pairing_result(device)
    before = item_state(http, result)
    command(http, _pairing.item(device), value)

    def completed(text: str) -> bool:
        if text == before:
            return False
        try:
            answer = json.loads(text)
            return isinstance(answer, dict) and bool(answer.get("id")) and answer.get("state") in _pairing.STATES
        except (ValueError, TypeError):
            return False

    answer = settle(http, result, completed, timeout)
    return json.loads(answer) if completed(answer) else {"state": "checking"}


def registration_error(answer: dict) -> str | None:
    """Safe detail from a Sony acknowledgement; never echo arbitrary response text."""
    detail = answer.get("error")
    if not isinstance(detail, dict):
        return None
    why = ("Sony error 1 (Internal Server Error)" if type(detail.get("code")) is int and detail["code"] == 1
           else "a Sony API error" if detail.get("api") is True else "no authentication cookie")
    status = detail.get("http")
    http = f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
    return f"TV registration failed: {why}{http}."


def send_pin(device: dict, http: sandbox.Http, pin: str, timeout: float, log=print) -> int:
    ident = device["id"]
    answer = pairing_command(device, http, pin, timeout)
    state = answer["state"]
    if state == "paired":
        log(f"paired  {name(device)}: thermite commands it from now on")
        return 0
    if state == "failed":
        detail = registration_error(answer)
        log(f"{name(device)}: {detail}" if detail else
            f"{name(device)} did not take that PIN; ask for a new one: bin/thermite pair {ident}")
    elif state == "unreachable":
        log(f"{name(device)} did not answer; is it on, with Remote Start on?")
    else:
        log(f"thermite reported {_pairing.STATES.get(state, state)!r} for {name(device)}; see openhab.log")
    return 1


def pair_sony(device: dict, http: sandbox.Http, code: str | None, ask=input, timeout: float = STATUS_TIMEOUT,
              log=print) -> int:
    """Have a Sony TV show a PIN through thermite's pairing Items, ask for it
    and send it back; with code, send that, the PIN the TV shows already."""
    ident = device["id"]
    pin = _pairing.code_pattern(sony_ip_control.PAIRING["length"], sony_ip_control.PAIRING["chars"])
    status = _pairing.status_item(device)
    before = item_state(http, status)
    if code is not None:
        if before != "code_shown":
            log(f"{name(device)} is showing no PIN for thermite ({_pairing.STATES.get(before, before)}); "
                f"ask for one: bin/thermite pair {ident}")
            return 1
        return send_pin(device, http, code, timeout, log)
    answer = pairing_command(device, http, _pairing.REQUEST, timeout)
    state = answer["state"]
    if state == "paired":
        log(f"paired  {name(device)} registered thermite again without a PIN")
        return 0
    if state == "failed":
        detail = registration_error(answer)
        log(f"{name(device)}: {detail}" if detail else
            f"{name(device)} showed no PIN: is its IP control Authentication Normal (Settings, Network, "
            "Home network, IP control)?")
        log(sony_ip_control.PAIRING["hint"])
        return 1
    if state == "unreachable":
        log(f"{name(device)} did not answer; is it on, with Remote Start on?")
        return 1
    if state != "code_shown":
        log(f"thermite did not report a PIN on {name(device)} ({_pairing.STATES.get(state, state)}); "
            "see openhab.log")
        return 1
    typed = ask(f"The four digits {device.get('name') or ident} shows: ").strip()
    if not pin.fullmatch(typed):
        log("not four digits; nothing sent. Ask for a new PIN: "
            f"bin/thermite pair {ident}")
        return 2
    return send_pin(device, http, typed, timeout, log)


def check(devices: list[dict], http: sandbox.Http, log=print) -> int:
    status = 0
    for device in devices:
        state = item_state(http, _pairing.status_item(device))
        problem = item_state(http, sony_ip_control.problem(device))
        line = f"{name(device)}: {_pairing.STATES.get(state, state).lower()}"
        if problem not in ("NULL", "UNDEF"):
            line += f" ({problem})"
        log(line)
        if state != "paired" or problem not in ("NULL", "UNDEF"):
            status = 1
    return status

def thing_status(http: sandbox.Http, uid: str) -> tuple[str | None, str]:
    """(status, its description) of a Thing; (None, why) if thermite has none."""
    code, thing = http.try_get_json(f"/rest/things/{urllib.parse.quote(uid, safe=':')}")
    if code != 200 or not thing:
        return None, f"HTTP {code}"
    info = thing.get("statusInfo") or {}
    return info.get("status"), info.get("description") or info.get("statusDetail") or ""


def closed_pairing_port(thing: dict, timeout: float = CONNECT_TIMEOUT) -> str | None:
    """"host:port" of a googletv Thing's pairing port (its googletvPort + 1)
    when it does not connect, else None (also when the Thing names no
    address: the binding is left to try)."""
    config = thing.get("configuration") or {}
    host, port = config.get("ipAddress"), config.get("googletvPort")
    if not host or not isinstance(port, (int, float)):
        return None
    address = f"{host}:{int(port) + 1}"
    try:
        with socket.create_connection((host, int(port) + 1), timeout=timeout):
            return None
    except OSError:
        return address


def pair_android(device: dict, code: str | None, timeout: float = ONLINE_TIMEOUT, log=print) -> int:
    """Ask an Android TV for a pairing code (code None), or send the code
    it shows and wait for its Thing to come ONLINE, on the sandbox."""
    try:
        command = googletv.pair_command(device, code)
    except ValueError as exc:
        return error(str(exc))
    inst = sandbox.sandbox_instance()
    if not sandbox.running_pgid(inst):
        return error("the sandbox is not running; start it first (thermite sandbox start)")
    if not inst.token_file.exists():
        return error(f"no API token in {inst.token_file}; start the sandbox without --no-token")
    rt = sandbox.require_runtime(sandbox.load_versions())
    uid = googletv.thing_uid(device)
    http = sandbox.Http(inst.url, token=sandbox.read_token(inst.token_file), timeout=10)
    status, why = thing_status(http, uid)
    if status is None:
        return error(f"thermite has no Thing {uid} ({why}); run thermite generate and thermite sandbox reload first")
    if status == "ONLINE" and code is None:
        log(f"paired  {uid} is ONLINE already: nothing to do")
        return 0
    if code is None:
        # The binding drops a request it cannot send there without a word.
        _, thing = http.try_get_json(f"/rest/things/{urllib.parse.quote(uid, safe=':')}")
        closed = closed_pairing_port(thing or {})
        if closed:
            print(f"thermite pair: the pairing port {closed} does not connect, so no code was asked for "
                  "(the binding would drop the request); restart the device, then try again", file=sys.stderr)
            return 1
    sandbox.karaf_command(rt, inst, command)
    if code is None:
        log(f"asked   {device.get('name', device['id'])} ({device['ip']}) to show a pairing code on its screen")
        log(f"next    bin/thermite pair {device['id']} CODE --sandbox  (the six letters and digits it shows)")
        return 0
    deadline = time.monotonic() + timeout
    while True:
        status, why = thing_status(http, uid)
        if status == "ONLINE":
            log(f"paired  {uid} is ONLINE")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    print(f"not yet {uid} is {status} ({why}) after {timeout:g}s; if the TV no longer shows the code, "
          f"ask for a new one: bin/thermite pair {device['id']} --sandbox", file=sys.stderr)
    return 1


def kinds(device: dict) -> set[str]:
    """How the device pairs: "sony" (a Sony TV's PIN), "android" (an
    Android TV's pairing code), "apple" (an Apple TV)."""
    out = set()
    if uses(device, sony_ip_control.PHASE, sony_ip_control.NAME):
        out.add("sony")
    if uses(device, googletv.PHASE, googletv.NAME):
        out.add("android")
    if uses(device, pyatv_http.PHASE, pyatv_http.NAME):
        out.add("apple")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("pair", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("device", nargs="?", metavar="DEVICE", help="the device's id in the house file")
    parser.add_argument("code", nargs="?", metavar="CODE",
                        help="the code the device shows: a Sony TV's four digits, an Android TV's six letters "
                             "and digits (omit to ask the device for them)")
    oh_cli.add_check(parser, "a Sony TV's pairing (DEVICE's, or any one's)")
    oh_cli.add_target(parser, "production", "sandbox", default="production", meaning={
        "production": "production thermite on this host, with the API token bin/thermite install saved",
        "sandbox": "the sandbox, and its Android TV pairings",
    })
    oh_cli.add_house(parser, render.INVENTORY)
    oh_cli.add_timeout(parser, ONLINE_TIMEOUT, "an Android TV to come ONLINE after its code")
    args = parser.parse_args(argv)

    data = inventory.load(args.house)
    problems = render.validate(data)
    if problems:
        for problem in problems:
            print(f"thermite pair: {problem}", file=sys.stderr)
        return 2
    known = tvs(data)
    if args.check:
        if args.code is not None:
            parser.error("--check takes one DEVICE at most")
        wanted = [args.device] if args.device else list(known)
        unknown = [ident for ident in wanted if ident not in known]
        if unknown:
            return error(f"not a TV under {sony_ip_control.NAME} control: {', '.join(unknown)} "
                         f"(those are: {', '.join(known) or 'none'})")
        try:
            return check([known[ident] for ident in wanted], openhab(args.sandbox))
        except OpenHABError as exc:
            return error(str(exc))
    if args.device is None:
        parser.error("name the DEVICE to pair, or --check")
    devices = {d["id"]: d for d in data["devices"]}
    device = devices.get(args.device)
    how = kinds(device) if device else set()
    if not how:
        pairing = [ident for ident, d in devices.items() if kinds(d)]
        return error(f"not a device that pairs: {args.device} (those are: {', '.join(pairing) or 'none'})")
    if "apple" in how:
        return error(f"{name(device)} is an Apple TV: pair it from {PAIRING_SCREEN}")
    if "sony" in how:
        pin = _pairing.code_pattern(sony_ip_control.PAIRING["length"], sony_ip_control.PAIRING["chars"])
        if args.code is not None and not pin.fullmatch(args.code):
            return error(f"{args.code!r} is not the four digits a Sony TV shows")
        try:
            return pair_sony(device, openhab(args.sandbox), args.code)
        except OpenHABError as exc:
            return error(str(exc))
    if not args.sandbox:
        return error(f"{name(device)} pairs with production from {PAIRING_SCREEN}; "
                     "thermite pair pairs it only with --sandbox")
    return pair_android(device, args.code, args.timeout)
