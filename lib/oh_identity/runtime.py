"""Finite runtime contract and typed dispatcher. No caller chooses an upstream URL."""

import copy
import hashlib
import hmac
import json
import math
import re
from pathlib import Path
from types import MappingProxyType
from contextlib import nullcontext

from .services import Problem, permissions

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET = re.compile(r"(?:^|_)(?:password|passwd|secret|token|psk|credential|authorization|auth)(?:_|$)", re.I)
PAIR_STATES = {"unpaired", "paired", "checking", "code_shown", "repairing", "failed", "unavailable", "closed", "unreachable"}
UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
SENSITIVE_TEXT = re.compile(r"\b(?:authorization|bearer|cookie|password|pin|secret|token)\b", re.I)
_adapter = None


def frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({key: frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    return value


def plain(value):
    if isinstance(value, MappingProxyType):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


def safe_item(name):
    return isinstance(name, str) and bool(NAME.fullmatch(name)) and not SECRET.search(name) and (
        "_pairing" not in name or name.endswith(("_pairing_status", "_pairing_result")))


def json_value(raw, schema, depth=0):
    """Projection recipes contain finite provider fields, never arbitrary key copies."""
    if depth > 8 or not hasattr(schema, "get"):
        return None
    kind = schema.get("kind")
    if kind == "object" and isinstance(raw, dict):
        result = {}
        for key, child in schema.get("fields", {}).items():
            if not isinstance(key, str) or any(word in key.lower() for word in ("password", "authorization", "credential", "cookie", "secret", "token", "pincode")) or key not in raw:
                continue
            if raw[key] is None and child.get("nullable") is True:
                result[key] = None
                continue
            value = json_value(raw[key], child, depth + 1)
            if value is not None:
                result[key] = value
        return result
    if kind == "number" and type(raw) in (float, int) and math.isfinite(raw) and schema.get("min", -1e13) <= raw <= schema.get("max", 1e13):
        return raw
    if kind == "boolean" and type(raw) is bool:
        return raw
    if kind == "enum" and type(raw) in (str, int) and raw in schema.get("values", ()):
        return raw
    if kind == "text" and isinstance(raw, str) and len(raw) <= min(schema.get("max_length", 200), 1000):
        pattern = schema.get("pattern")
        if pattern and raw.isprintable() and not SENSITIVE_TEXT.search(raw) and re.fullmatch(pattern, raw):
            return raw
    return None


class RuntimeContract:
    """Compiled server-side facts, independent of browser-provided manifests.

    rooms: id -> {sources, source_item, actions: action -> typed Item spec}.
    states: Item -> {kind, values/fields}; assets: basename -> {private}.
    Source action is derived from source_item; other actions are explicit.
    """

    def __init__(self, rooms, states=None, routing=None, house=None, assets=None, asset_root=None,
                 pairings=None, groups=None, media=None):
        data = copy.deepcopy(dict(rooms))
        reads = copy.deepcopy(states or {})
        for ident, room in data.items():
            if not NAME.fullmatch(ident) or not NAME.fullmatch(room["source_item"]):
                raise ValueError("Invalid runtime room contract")
            source_action = room.setdefault("actions", {}).setdefault("source", {})
            source_action.update(item=room["source_item"], kind="enum", values=["off", *room["sources"]])
            source_action.setdefault("resources", room.get("resources", [room["source_item"]]))
            reads.setdefault(room["source_item"], {"kind": "enum", "values": ["off", *room["sources"]]})
        self.rooms = frozen(data)
        self.states = frozen({name: spec for name, spec in reads.items() if safe_item(name)})
        self.routing = frozen(copy.deepcopy(routing or {}))
        self.house = frozen(copy.deepcopy(house or {}))
        self.assets = frozen(copy.deepcopy(assets or {}))
        self.asset_root = Path(asset_root).resolve() if asset_root else None
        self.pairings = frozen(copy.deepcopy(pairings or {}))
        self.groups = frozen(copy.deepcopy(groups or {}))
        self.media = frozen(copy.deepcopy(media or {}))

    def policy_contract(self):
        return {"rooms": {key: {"sources": list(value["sources"])} for key, value in self.rooms.items()},
                "routing": plain(self.routing), "house": plain(self.house)}

    def command(self, room_id, action, value):
        room = self.rooms.get(room_id)
        spec = room and room["actions"].get(action)
        if not spec or not safe_item(spec.get("item")):
            raise Problem("unknown_action", 404)
        kind = spec.get("kind")
        if kind in {"enum", "switch", "key"}:
            if not isinstance(value, str) or value not in spec.get("values", ()):
                raise Problem("invalid_command")
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise Problem("invalid_command")
            low, high, step = spec.get("min", 0), spec["max"], spec.get("step", 1)
            if not low <= value <= high or abs((value - low) / step - round((value - low) / step)) > 1e-7:
                raise Problem("invalid_command")
        else:
            raise Problem("unknown_action", 404)
        affected = tuple(spec.get("affected_by_value", {}).get(str(value), spec.get("affected_rooms", (room_id,))))
        if any(ident not in self.rooms for ident in affected):
            raise Problem("invalid_contract", 503)
        return spec["item"], str(value), affected


def project(name, value, spec):
    """Fail closed for free text and credential-bearing snapshots/results."""
    if not safe_item(name):
        raise Problem("not_found", 404)
    if not isinstance(value, str):
        return "UNDEF"
    if len(value) > 65536:
        return "UNDEF"
    if value in ("NULL", "UNDEF"):
        return value
    kind = spec.get("kind")
    if kind == "redacted":
        from django.conf import settings
        # Only compiled public activity Items receive this recipe. Keyed and
        # Item-scoped so low-entropy values cannot be recovered from raw hashes.
        digest = hmac.new(settings.SECRET_KEY.encode(), (name + "\0" + value).encode(), hashlib.sha256).hexdigest()
        return "hidden:" + digest
    if kind == "json":
        try:
            raw = json.loads(value, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            result = json_value(raw, {"kind": "object", "fields": spec.get("fields", {})})
        except (ValueError, RecursionError):
            result = None
        return json.dumps(result, separators=(",", ":"), allow_nan=False) if result else "UNDEF"
    if kind == "text":
        return json_value(value, spec) or "UNDEF"
    if kind in {"enum", "switch"}:
        return value if value in spec.get("values", ("ON", "OFF")) else "UNDEF"
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "UNDEF"
        return str(value) if math.isfinite(number) and spec.get("min", 0) <= number <= spec.get("max", 100) else "UNDEF"
    if kind == "datetime":
        from django.utils.dateparse import parse_datetime
        try:
            date = parse_datetime(value)
        except ValueError:
            date = None
        return date.isoformat() if date and date.tzinfo else "UNDEF"
    if kind == "problem":
        return "UNDEF" if value in ("", None) else "Device control unavailable"
    if kind == "pairing_status":
        return value if value in PAIR_STATES else "UNDEF"
    if kind == "pairing_result":
        try:
            raw = json.loads(value)
        except (TypeError, ValueError):
            return "UNDEF"
        if not isinstance(raw, dict) or not isinstance(raw.get("state"), str) or raw["state"] not in PAIR_STATES:
            return "UNDEF"
        result = {"state": raw["state"]}
        if isinstance(raw.get("id"), str) and UUID.fullmatch(raw["id"]):
            result["id"] = raw["id"]
        error = raw.get("error")
        if isinstance(error, dict) and type(error.get("code")) is int and error["code"] in {1, 2, 3, 5, 7, 12, 15, 400, 401, 403, 404, 408, 500, 503}:
            result["error"] = {"code": error["code"]}
            for field in ("api",):
                if type(error.get(field)) is bool:
                    result["error"][field] = error[field]
            if type(error.get("http")) is int and 100 <= error["http"] <= 599:
                result["error"]["http"] = error["http"]
        return json.dumps(result, separators=(",", ":"))
    # Arbitrary String Items must never become a plaintext data/log proxy.
    return "UNDEF"


class RuntimeAdapter:
    def __init__(self, contract, transport, policy=None, command_lock=None):
        self.contract, self.transport = contract, transport
        self.policy = policy
        # Root composition supplies the same room/resource lock authority as the worker.
        self.command_lock = command_lock or (lambda rooms, resources: nullcontext())

    def allowed(self, user, room_id):
        if room_id not in self.contract.rooms:
            raise Problem("unknown_room", 404)
        if "room.control" not in permissions(user) or user.rooms is not None and room_id not in user.rooms:
            raise Problem("forbidden", 403)
        self.check_policy(room_id)

    def check_policy(self, room_id):
        from .models import RoomPolicy
        from django.utils import timezone
        saved = RoomPolicy.objects.filter(room_id=room_id).first()
        if not saved or saved.config.get("enabled") is False:
            return
        # A persisted enabled policy cannot silently become unenforced if engine missing.
        if not self.policy:
            raise Problem("policy_unavailable", 503)
        answer = self.policy.authorize(saved.config, timezone.now())
        if not answer.get("allowed"):
            raise Problem("room_locked", 423)

    def dispatch(self, user, room_id, action, value):
        item, command, affected = self.contract.command(room_id, action, value)
        spec = self.contract.rooms[room_id]["actions"][action]
        resources = tuple(spec.get("resources", (item,)))
        with self.command_lock(tuple(sorted(set((room_id, *affected)))), resources):
            return self._dispatch(user, room_id, action, item, command, affected)

    def _dispatch(self, user, room_id, action, item, command, affected):
        from .models import User
        from .auth import revalidate
        # Re-read after validation and before I/O: a queued stale principal is insufficient.
        fresh = User.objects.select_related("role").get(pk=user.pk)
        if fresh.auth_revision != user.auth_revision:
            raise Problem("authorization_changed", 401)
        revalidate(user)
        self.allowed(fresh, room_id)
        for ident in affected:
            self.allowed(fresh, ident)
        try:
            self.transport.command(item, command)
        except Exception:
            raise Problem("runtime_unavailable", 502) from None
        return {"accepted": True, "room": room_id, "action": action}

    def read(self, item):
        spec = self.contract.states.get(item)
        if spec is None:
            raise Problem("not_found", 404)
        try:
            value = self.transport.read(item)
        except Exception:
            raise Problem("runtime_unavailable", 502) from None
        return project(item, value, spec)

    def pairing_list(self):
        result = []
        for item, metadata in self.contract.pairings.items():
            spec = plain(metadata)
            config = spec.get("config", {})
            # The command Item's state is deliberately never read or forwarded.
            result.append({"name": item, "state": "NULL", "metadata": {"pairing": spec}})
            for field in ("status", "result"):
                name = config.get(field)
                if name in self.contract.states:
                    result.append({"name": name, "state": self.read(name)})
        return result

    def pair(self, user, item, value):
        from .models import User
        from .services import is_admin
        from .auth import revalidate
        revalidate(user)
        fresh = User.objects.select_related("role").get(pk=user.pk)
        if fresh.auth_revision != user.auth_revision or not is_admin(fresh):
            raise Problem("forbidden", 403)
        spec = self.contract.pairings.get(item)
        if not spec or not isinstance(value, str):
            raise Problem("not_found", 404)
        config = spec.get("config", {})
        length, chars = config.get("length"), config.get("chars")
        character_class = {"digits": "0-9", "hex": "0-9A-Fa-f"}.get(chars)
        code = type(length) is int and 1 <= length <= 16 and character_class and re.fullmatch("[" + character_class + "]{" + str(length) + "}", value)
        if value not in {"REQUEST", "AGAIN", "CANCEL"} and not code:
            raise Problem("invalid_pairing_command")
        try:
            self.transport.command(item, value)
        except Exception:
            raise Problem("runtime_unavailable", 502) from None
        # Neither acknowledgement nor audit echoes the submitted code.
        return {"accepted": True}

    def group(self, name):
        names = self.contract.groups.get(name)
        if names is None:
            raise Problem("not_found", 404)
        try:
            raw = self.transport.group(name)
        except Exception:
            raise Problem("runtime_unavailable", 502) from None
        members = raw.get("members", []) if isinstance(raw, dict) else []
        states = {row.get("name"): row.get("state") for row in members[:1000]
                  if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"] in names}
        return {"name": name, "members": [{"name": item, "state": project(item, states.get(item, "NULL"), self.contract.states[item])}
                                          for item in names if item in self.contract.states]}

    def media(self, resource, method, body=b""):
        spec = self.contract.media.get(resource)
        if not spec or resource.startswith("//") or "?" in resource or "#" in resource:
            raise Problem("not_found", 404)
        kind = spec.get("kind")
        if kind == "snapshot" and method == "GET":
            expected, maximum = {"image/jpeg", "image/png"}, 4 * 1024 * 1024
        elif kind == "sdp" and method == "POST":
            try:
                offer = body.decode("ascii")
            except UnicodeError:
                raise Problem("invalid_sdp") from None
            lines = offer.replace("\r\n", "\n").split("\n")
            media = [line for line in lines if line.startswith("m=")]
            if len(body) > 65536 or not offer.startswith("v=0") or len(media) != 1 or not media[0].startswith("m=video ") or not any(line == "a=recvonly" for line in lines) or any(line in {"a=sendrecv", "a=sendonly"} for line in lines):
                raise Problem("invalid_sdp")
            expected, maximum = {"application/sdp"}, 65536
        else:
            raise Problem("method_not_allowed", 405)
        try:
            content_type, data = self.transport.media(resource, method, body, maximum)
        except Exception:
            raise Problem("media_unavailable", 502) from None
        if content_type not in expected or not isinstance(data, bytes) or len(data) > maximum:
            raise Problem("media_unavailable", 502)
        return content_type, data


def install_adapter(adapter):
    """Service composition/test seam; never exposed by an HTTP endpoint."""
    global _adapter
    _adapter = adapter


def get_adapter():
    if _adapter is None:
        raise Problem("runtime_not_configured", 503)
    return _adapter
