"""Inspect cameras or provision the native Protect bridges without Git secrets."""

from __future__ import annotations

import getpass
import datetime
import ipaddress
import json
import socket
import stat
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import oh_cli
import oh_inventory
import oh_sandbox
from oh_cli import pair
from oh_conf import House, uses
from oh_features import remote_cameras
from oh_integrations import unifi_protect as protect

DESCRIPTION = """\
Show the house's UniFi Protect cameras, or configure their native runtime
bridges. Homebridge and Apple Home are independent and stay in place.

`configure` stores a local console login and an existing Protect API token
in the runtime's private configuration database. It never creates, renews
or deletes a console API key. The native NVR's autoManageToken is always
false. Passwords and API tokens are prompted without echo or read from
files; they are never arguments, output or generated configuration.

The native binding manages WebRTC stream sessions when explicitly enabled
with configure or streams; see docs/cameras.md for resource and permission needs.
"""
EXAMPLES = (
    ("thermite cameras list", "read the configured cameras' status"),
    ("thermite cameras configure --dry-run", "show the bridge plan without reading credentials or changing anything"),
    ("thermite cameras configure --stream-camera CAMERA_ID", "privately configure Protect, initially streaming one camera from cameras list"),
    ("thermite cameras streams --all", "enable native WebRTC for all configured cameras after the first camera is verified"),
    ("thermite cameras streams --none", "stop native WebRTC sessions while keeping status and snapshots"),
    ("thermite cameras configure --homebridge /var/lib/homebridge/config.json --protect-token-file var/protect-token",
     "reuse the existing console login and an explicitly supplied mode-0600 Protect token"),
)


class CameraError(Exception):
    """A safe error: never contains a request/response body or credentials."""


def console_data(payload: object, expected: type):
    """Validate the console key API envelope even when transport returns 200.

    This is also used by the coordinator's reviewed private key-setup
    helper. A successful HTTP status cannot stand in for API success.
    """
    if (not isinstance(payload, dict) or type(payload.get("data")) is not expected
            or payload.get("error") not in (None, False, 0, "")
            or payload.get("code") not in (None, 0, "0", 200)
            or payload.get("codeS") not in (None, "", "OK", "SUCCESS")):
        raise CameraError("console returned an error or unexpected envelope; response body suppressed")
    return payload["data"]


def private_text(path: Path) -> str:
    """Refuse group/world-readable secrets and symlinks."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise CameraError("token file must be a regular file with mode 0600 or stricter")
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CameraError("could not read token file") from exc
    if not value or any(c.isspace() for c in value):
        raise CameraError("token file must contain one nonempty token")
    return value


def bridge_plan(config: dict, username: str, password: str, token: str) -> list[dict]:
    if not all(isinstance(v, str) and v and not any(ord(c) < 32 for c in v) for v in (username, password, token)):
        raise CameraError("a local username, password and existing Protect API token are required")
    return [
        {"UID": protect.CONTROLLER_UID, "thingTypeUID": "unifi:controller", "label": "Protect console",
         "configuration": {"host": config["host"], "port": config.get("port", 443), "unifios": True,
                           "username": username, "password": password, "timeoutSeconds": 15}},
        {"UID": protect.NVR_UID, "thingTypeUID": "unifi:nvr", "bridgeUID": protect.CONTROLLER_UID,
         "label": "Protect cameras", "configuration": {"token": token, "autoManageToken": False,
                                                          "tokenAutoManaged": False}},
    ]


def provision(http, plan: list[dict]) -> None:
    """Preflight both UIDs before making a change; send only these two Things."""
    existing = []
    for thing in plan:
        code, found = http.try_get_json("/rest/things/" + urllib.parse.quote(thing["UID"], safe=""))
        if code not in {200, 404}:
            raise CameraError(f"could not inspect the native bridge (HTTP {code})")
        if code == 200 and (not isinstance(found, dict) or found.get("thingTypeUID") != thing["thingTypeUID"]
                            or found.get("editable") is False):
            raise CameraError("the bridge UID belongs to a different or file-owned Thing; no changes made")
        if code == 200:
            config = found.get("configuration") or {}
            expected = thing["configuration"]
            if (thing.get("bridgeUID") and found.get("bridgeUID") != thing["bridgeUID"] or
                    "deviceId" in expected and config.get("deviceId") != expected["deviceId"] or
                    "host" in expected and config.get("host") != expected["host"]):
                raise CameraError("a native Thing ID belongs to a different console or camera; no changes made")
        existing.append(code == 200)
    for thing, update in zip(plan, existing):
        path = "/rest/things/" + urllib.parse.quote(thing["UID"], safe="") if update else "/rest/things"
        try:
            code, _ = http.request("PUT" if update else "POST", path, json.dumps(thing).encode(),
                                   {"Content-Type": "application/json"})
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise CameraError("runtime refused bridge configuration; no response body was logged") from exc
        if code not in {200, 201, 202, 204}:
            raise CameraError(f"runtime refused bridge configuration (HTTP {code})")


def camera_plan(data: dict, streaming: set[str]) -> list[dict]:
    devices = [d for d in data.get("devices", []) if uses(d, protect.PHASE, protect.NAME)]
    if streaming - {d["id"] for d in devices}:
        raise CameraError("stream selection includes an unknown Protect camera")
    return [protect.native_thing(device, House(data), webrtc=device["id"] in streaming) for device in devices]


def set_streams(http, data: dict, streaming: set[str]) -> None:
    """Change only native media enablement, without re-reading bridge secrets."""
    plan = camera_plan(data, streaming)
    for thing in plan:
        code, found = http.try_get_json("/rest/things/" + urllib.parse.quote(thing["UID"], safe=""))
        if (code != 200 or not isinstance(found, dict) or found.get("thingTypeUID") != "unifi:camera"
                or found.get("editable") is False or found.get("bridgeUID") != protect.NVR_UID
                or (found.get("configuration") or {}).get("deviceId") != thing["configuration"]["deviceId"]):
            raise CameraError("native cameras are not configured as expected; run cameras configure first")
    provision(http, plan)


def homebridge_login(path: Path, host: str) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        controllers = [c for p in data.get("platforms", []) if p.get("platform") == "UniFi Protect"
                       for c in p.get("controllers", [])]
        def matches_host(controller):
            address = controller.get("address")
            if address == host:
                return True
            if not isinstance(address, str) or not address:
                return False
            try:
                return socket.gethostbyname(address) == host
            except OSError:
                return False

        matches = [c for c in controllers if matches_host(c)]
        if len(matches) != 1 or not all(isinstance(matches[0].get(k), str) and matches[0][k]
                                       for k in ("username", "password")):
            raise CameraError("Homebridge has no unique local login for the configured Protect console")
        return matches[0]["username"], matches[0]["password"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise CameraError("could not read a matching Homebridge Protect login") from exc


def runtime(arguments):
    if not arguments.url:
        return pair.openhab(False)
    url = urllib.parse.urlsplit(arguments.url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise CameraError("runtime URL must be an HTTP(S) origin without credentials")
    try:
        loopback = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = False
    if url.scheme != "https" and not loopback:
        raise CameraError("use HTTPS for a remote runtime so console credentials stay encrypted")
    token = private_text(arguments.runtime_token_file) if arguments.runtime_token_file else None
    return oh_sandbox.Http(arguments.url, token=token, allow_write=True)


def status(http, data: dict) -> list[dict]:
    code, group = http.try_get_json(f"/rest/items/{protect.STATUS_GROUP}?fields=members(name,state)")
    if code != 200 or not isinstance(group, dict) or not isinstance(group.get("members"), list):
        raise CameraError("camera status is unavailable; is the camera configuration deployed?")
    approved = {item for camera in remote_cameras.manifest(House(data))["cameras"] for item in camera["items"].values()}
    reported = {member.get("name"): member.get("state") for member in group["members"]
                if isinstance(member, dict) and member.get("name") in approved}
    rows = []
    for camera in remote_cameras.manifest(House(data))["cameras"]:
        states = {}
        for role, item in camera["items"].items():
            raw = reported.get(item)
            # Only canonical states or timestamps, never arbitrary response text.
            allowed = {"ON", "OFF"} if role in {"online", "recording", "ring"} else {"OPEN", "CLOSED"}
            value = raw if isinstance(raw, str) and raw in allowed else "unknown"
            if role in {"lastMotion", "lastRing"} and isinstance(raw, str) and len(raw) < 60:
                try:
                    timestamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if timestamp.tzinfo is not None:
                        value = timestamp.isoformat()
                except ValueError:
                    pass
            states[role] = value
        if states["online"] != "ON":
            for role in ("motion", "recording", "ring"):
                if role in states:
                    states[role] = "unknown"
        rows.append({"id": camera["id"], "label": camera["label"], "states": states})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("cameras", DESCRIPTION, EXAMPLES)
    actions = oh_cli.actions(parser)
    for name, summary in (("list", "read the configured camera status"), ("configure", "configure native Protect privately"),
                          ("streams", "enable or stop native camera WebRTC sessions")):
        command = oh_cli.action(actions, name, summary, examples=EXAMPLES)
        oh_cli.add_house(command)
        oh_cli.add_target(command, "production", "url", default="production")
        command.add_argument("--runtime-token-file", type=Path, metavar="FILE", help="a mode-0600 API token for --url")
        if name == "list":
            oh_cli.add_json(command)
        elif name == "configure":
            oh_cli.add_dry_run(command)
            command.add_argument("--homebridge", type=Path, metavar="FILE", help="read the matching local console login from this Homebridge configuration")
            command.add_argument("--protect-token-file", type=Path, metavar="FILE", help="read an existing Protect API token from this mode-0600 file")
            command.add_argument("--stream-camera", action="append", default=[], metavar="ID", help="enable WebRTC for this camera initially; repeat for several (default: status and snapshots only)")
        else:
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument("--all", action="store_true", help="enable native WebRTC for every configured camera")
            selection.add_argument("--none", action="store_true", help="disable all native WebRTC sessions; keep status and snapshots")
            selection.add_argument("--camera", action="append", default=[], metavar="ID", help="enable only this camera; repeat for several")
            oh_cli.add_dry_run(command)
    arguments = parser.parse_args(argv)
    try:
        data = oh_inventory.load(arguments.house)
        problems = oh_inventory.validate(data)
        if problems:
            raise CameraError("; ".join(problems))
        problems += remote_cameras.validate(data)
        devices = [d for d in data.get("devices", []) if uses(d, protect.PHASE, protect.NAME)]
        for device in devices:
            problems += protect.validate(device)
        if problems or not devices:
            raise CameraError("; ".join(problems) if problems else "the house has no configured Protect cameras")
        if arguments.command == "configure":
            selected = set(arguments.stream_camera)
            native = camera_plan(data, selected)
            if arguments.dry_run:
                print(f"Configure {protect.CONTROLLER_UID} at {data['protect']['host']}:{data['protect'].get('port', 443)}")
                print(f"Configure {protect.NVR_UID}: existing API token only, autoManageToken=false")
                print(f"{len(devices)} read-only cameras; native WebRTC enabled for {len(selected)}")
                print("No console account/key or camera setting changes; native media enablement creates streaming sessions.")
                return 0
            username, password = (homebridge_login(arguments.homebridge, data["protect"]["host"]) if arguments.homebridge else
                                  (input("Local Protect username: ").strip(), getpass.getpass("Local Protect password: ")))
            token = private_text(arguments.protect_token_file) if arguments.protect_token_file else getpass.getpass("Existing Protect API token: ").strip()  # lint: allow -- private prompt, no literal credential
            plan = bridge_plan(data["protect"], username, password, token)
            provision(runtime(arguments), plan + native)
            print("Protect bridges configured in runtime private storage; credentials were not printed or saved in Git.")
        elif arguments.command == "streams":
            selected = {d["id"] for d in devices} if arguments.all else set(arguments.camera)
            camera_plan(data, selected)
            if not arguments.dry_run:
                set_streams(runtime(arguments), data, selected)
            print(f"Native WebRTC {'would be' if arguments.dry_run else 'is'} enabled for {len(selected)} of {len(devices)} cameras.")
        else:
            rows = status(runtime(arguments), data)
            if arguments.json:
                print(json.dumps({"cameras": rows}, indent=2))
            else:
                for row in rows:
                    print(f"{row['label']}: connected {row['states']['online']}, motion {row['states']['motion']}, recording {row['states']['recording']}")
        return 0
    except (CameraError, pair.OpenHABError, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        message = str(exc) if isinstance(exc, (CameraError, pair.OpenHABError)) else "could not complete the camera request"
        print(f"thermite cameras: {message}", file=sys.stderr)
        return 2
